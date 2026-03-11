import sys
import os
import ctypes
from dotenv import load_dotenv

# Load env variables and fix Google Credentials path to be relative to this file
load_dotenv()
if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
    cred_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    if not os.path.isabs(cred_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), cred_path
        )
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QLabel,
    QTextEdit, QPushButton, QHBoxLayout, QLineEdit, QMessageBox, QFrame,
    QScrollArea, QProgressBar, QSpinBox, QSplitter, QListWidget, QListWidgetItem,
    QTextBrowser
)
from PyQt5.QtGui import QPixmap
import re
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSize

from ai_generator import (
    generate_script_from_topic,
    generate_image_from_prompt,
    generate_audio_from_text
)
import db
import moviepy.editor as mp
import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS

class ImageGenerationThread(QThread):
    finished = pyqtSignal(bool, str)
    
    def __init__(self, prompt, output_path):
        super().__init__()
        self.prompt = prompt
        self.output_path = output_path
        
    def run(self):
        success = generate_image_from_prompt(self.prompt, self.output_path)
        self.finished.emit(success, self.output_path)

class AudioGenerationThread(QThread):
    finished = pyqtSignal(bool, str)
    
    def __init__(self, text, output_path):
        super().__init__()
        self.text = text
        self.output_path = output_path
        
    def run(self):
        success = generate_audio_from_text(self.text, self.output_path)
        self.finished.emit(success, self.output_path)

class BulkGenerationThread(QThread):
    """
    Processes ALL scenes sequentially: image then audio for each scene, in order.
    This prevents API rate-limiting and ensures nothing is skipped.
    """
    scene_progress = pyqtSignal(int, str, str)  # (scene_index, asset_type, status)
    all_done = pyqtSignal(bool, str)            # (all_succeeded, summary_message)

    def __init__(self, scenes):
        super().__init__()
        self.scenes = scenes

    def run(self):
        if not os.path.exists('assets'):
            os.makedirs('assets')

        total = len(self.scenes)
        failed = []

        for i, scene in enumerate(self.scenes):
            idx = i + 1

            # --- Image ---
            self.scene_progress.emit(i, 'img', f'⏳ Generating image {idx}/{total}...')
            img_path = f"assets/scene_{idx}.jpg"
            img_ok = generate_image_from_prompt(scene.get('visual', ''), img_path)
            if img_ok:
                scene['img_path'] = img_path
                db.update_scene_asset(scene.get('db_id'), 'img_path', img_path)
                self.scene_progress.emit(i, 'img', '✅ Image done')
            else:
                failed.append(f"Scene {idx} Image")
                self.scene_progress.emit(i, 'img', '❌ Image failed')

            # --- Audio ---
            self.scene_progress.emit(i, 'aud', f'⏳ Generating audio {idx}/{total}...')
            aud_path = f"assets/scene_{idx}.mp3"
            aud_ok = generate_audio_from_text(scene.get('narration', ''), aud_path)
            if aud_ok:
                scene['audio_path'] = aud_path
                db.update_scene_asset(scene.get('db_id'), 'audio_path', aud_path)
                self.scene_progress.emit(i, 'aud', '✅ Audio done')
            else:
                failed.append(f"Scene {idx} Audio")
                self.scene_progress.emit(i, 'aud', '❌ Audio failed')

        if failed:
            self.all_done.emit(False, f"Completed with failures: {', '.join(failed)}")
        else:
            self.all_done.emit(True, f"All {total} scenes generated successfully!")

class VideoStitchingThread(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, scenes, output_path):
        super().__init__()
        self.scenes = scenes
        self.output_path = output_path

    def run(self):
        try:
            self.progress.emit("Starting video stitching process...")
            clips = []
            
            for i, scene in enumerate(self.scenes):
                img_path = scene.get('img_path')
                aud_path = scene.get('audio_path')
                
                # Wait: the user explicitly wanted merging without skipping.
                # If they skipped generating, we must abort firmly until they finish!
                if not img_path or not os.path.exists(img_path):
                    self.finished.emit(False, f"Missing Image for Scene {i+1}. Please generate all assets before stitching.")
                    return
                if not aud_path or not os.path.exists(aud_path):
                    self.finished.emit(False, f"Missing Audio for Scene {i+1}. Please generate all assets before stitching.")
                    return
                    
                self.progress.emit(f"Processing Scene {i+1} (Adding Motion & FX)...")
                
                # Load Audio
                audio_clip = mp.AudioFileClip(aud_path)
                
                # Load Image
                img_clip = mp.ImageClip(img_path).set_duration(audio_clip.duration)
                
                # Dynamic Motion Effect: Cinematic Slow Zoom In (from 1.0 to 1.05x)
                # This guarantees that the viewer feels movement instead of static shots
                img_zoomed = img_clip.resize(lambda t: 1.0 + 0.05 * (t / audio_clip.duration))
                
                # Using CompositeVideoClip safely locks the image boundary to the original 16:9 crop 
                # so that as it scales, the edges bleed cleanly out of frame.
                scene_clip = mp.CompositeVideoClip([img_zoomed.set_pos('center')], size=img_clip.size).set_duration(audio_clip.duration)
                
                # Attach audio track
                scene_clip = scene_clip.set_audio(audio_clip)
                clips.append(scene_clip)
                
            if not clips:
                self.finished.emit(False, "No valid scenes found to stitch.")
                return
                
            self.progress.emit("Applying dynamic crossfade transitions between scenes...")
            crossfade_duration = 0.5
            final_clips = [clips[0]]
            
            for c in clips[1:]:
                # Enable overlapping visually by requesting explicit "crossfade in" 
                final_clips.append(c.crossfadein(crossfade_duration))
                
            self.progress.emit("Concatenating into single timeline...")
            # We strictly set padding to negative match the transition overlap and use the 'compose' renderer
            final_video = mp.concatenate_videoclips(final_clips, padding=-crossfade_duration, method="compose")
            
            self.progress.emit(f"Writing final video to {self.output_path} (This may take a while)...")
            final_video.write_videofile(
                self.output_path, 
                fps=24, 
                codec="libx264", 
                audio_codec="aac",
                logger=None # Suppress internal printing
            )
            
            self.finished.emit(True, self.output_path)
        except Exception as e:
            self.finished.emit(False, str(e))

class ScriptGenerationThread(QThread):
    chunk_received = pyqtSignal(str)
    finished = pyqtSignal()
    
    def __init__(self, topic, duration):
        super().__init__()
        self.topic = topic
        self.duration = duration
        
    def run(self):
        for chunk in generate_script_from_topic(self.topic, self.duration):
            self.chunk_received.emit(chunk)
        self.finished.emit()

class ScriptTab(QWidget):
    next_requested = pyqtSignal(list)
    next_requested_topic = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        # Topic Input Section
        topic_layout = QHBoxLayout()
        self.topic_input = QLineEdit()
        self.topic_input.setPlaceholderText("Enter a topic (e.g., 'How photosynthesis works')")
        self.topic_input.setMinimumHeight(38)
        
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 60)
        self.duration_spin.setValue(5)
        self.duration_spin.setSuffix(" min")
        self.duration_spin.setMinimumHeight(38)
        self.duration_spin.setMinimumWidth(80)
        
        self.generate_btn = QPushButton("✨ Generate Script")
        self.generate_btn.setProperty("class", "primary-button")
        self.generate_btn.clicked.connect(self.generate_script)
        
        topic_layout.addWidget(QLabel("🚀 Topic:"))
        topic_layout.addWidget(self.topic_input)
        topic_layout.addWidget(QLabel("⏱️ Length:"))
        topic_layout.addWidget(self.duration_spin)
        topic_layout.addWidget(self.generate_btn)
        layout.addLayout(topic_layout)
        
        # Script Editor Section
        lbl = QLabel("Draft or Edit Your Script Here:")
        lbl.setProperty("class", "h2")
        layout.addWidget(lbl)
        
        self.script_editor = QTextEdit()
        self.script_editor.setPlaceholderText("Enter the infographic script. Break it down into logical scenes.\\nExample:\\n[Scene 1]\\nVisual: A sun shining on a leaf.\\nNarration: Photosynthesis starts here.")
        layout.addWidget(self.script_editor)
        
        # Additional Buttons
        btn_layout = QHBoxLayout()
        self.enhance_btn = QPushButton("🪄 Enhance Script")
        self.split_btn = QPushButton("✂️ Split into Scenes")
        self.split_btn.clicked.connect(self.parse_and_go_next)
        btn_layout.addWidget(self.enhance_btn)
        btn_layout.addWidget(self.split_btn)
        
        # Next Step
        btn_layout.addStretch()
        self.next_btn = QPushButton("Next: Storyboard ➔")
        self.next_btn.setProperty("class", "success-button")
        self.next_btn.clicked.connect(self.parse_and_go_next)
        btn_layout.addWidget(self.next_btn)
        
        layout.addLayout(btn_layout)
        self.setLayout(layout)

    def parse_and_go_next(self):
        text = self.script_editor.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "No Script", "Please generate or write a script first.")
            return

        scenes = []
        # Find blocks starting with [Scene X] up to the next [Scene ] or end of string
        parts = re.split(r'\[Scene\s*\d+\]', text, flags=re.IGNORECASE)
        for part in parts:
            if not part.strip():
                continue
                
            visual_match = re.search(r'Visual:\s*(.*?)(?=\nNarration:|\Z)', part, re.DOTALL | re.IGNORECASE)
            narration_match = re.search(r'Narration:\s*(.*?)(?=\n\[|\Z)', part, re.DOTALL | re.IGNORECASE)
            
            # If a part doesn't contain at least one of these keywords, it's likely conversational filler
            # before the first scene. We should skip it.
            if not visual_match and not narration_match:
                continue
            
            visual_text = visual_match.group(1).strip() if visual_match else "No Visual Prompt Found"
            narration_text = narration_match.group(1).strip() if narration_match else "No Narration Found"
            
            scenes.append({
                'visual': visual_text,
                'narration': narration_text
            })
            
        if not scenes:
            QMessageBox.warning(self, "Parse Error", "Could not parse any scenes. Ensure they follow the [Scene X] format.")
            return

        # Save to Local SQLite Cache
        topic = self.topic_input.text().strip() or "Custom Script"
        duration = self.duration_spin.value()
        db.save_script_and_scenes(topic, duration, text, scenes)

        self.next_requested.emit(scenes)
        self.next_requested_topic.emit(topic)

    def generate_script(self):
        topic = self.topic_input.text().strip()
        duration = self.duration_spin.value()
        
        if not topic:
            QMessageBox.warning(self, "Input Error", "Please enter a topic first.")
            return
            
        self.generate_btn.setEnabled(False)
        self.generate_btn.setText("⏳ Generating...")
        
        self.script_editor.clear()
        self.thread = ScriptGenerationThread(topic, duration)
        self.thread.chunk_received.connect(self.on_chunk_received)
        self.thread.finished.connect(self.on_script_generated)
        self.thread.start()
        
    def on_chunk_received(self, chunk):
        # Insert raw text continuously to the end
        cursor = self.script_editor.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(chunk)
        self.script_editor.setTextCursor(cursor)
        
    def on_script_generated(self):
        self.generate_btn.setEnabled(True)
        self.generate_btn.setText("✨ Generate Script")

class StoryboardTab(QWidget):
    next_requested = pyqtSignal()
    back_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)

        header_layout = QHBoxLayout()
        label = QLabel("Storyboard (Generate Image & TTS Audio)")
        label.setProperty("class", "h2")
        
        self.generate_all_btn = QPushButton("⚡ Auto-Generate All Scenes")
        self.generate_all_btn.setProperty("class", "success-button")
        
        header_layout.addWidget(label)
        header_layout.addStretch()
        header_layout.addWidget(self.generate_all_btn)
        layout.addLayout(header_layout)
        
        # Scroll Area for assets list
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")
        
        self.scroll_widget = QWidget()
        self.scroll_layout = QVBoxLayout()
        self.scroll_layout.setSpacing(15)
        self.scroll_layout.addStretch() # Push items up
        self.scroll_widget.setLayout(self.scroll_layout)
        
        self.scroll_area.setWidget(self.scroll_widget)
        layout.addWidget(self.scroll_area)

        # Bottom nav
        nav_layout = QHBoxLayout()
        self.back_btn = QPushButton("⬅ Back to Script")
        self.back_btn.clicked.connect(self.back_requested.emit)
        
        self.next_btn = QPushButton("Next: Export Video ➔")
        self.next_btn.setProperty("class", "success-button")
        self.next_btn.clicked.connect(self.next_requested.emit)
        
        nav_layout.addWidget(self.back_btn)
        nav_layout.addStretch()
        nav_layout.addWidget(self.next_btn)
        layout.addLayout(nav_layout)
        self.setLayout(layout)

    def load_scenes(self, scenes):
        # Disconnect any old signal from previous renders
        try:
            self.generate_all_btn.clicked.disconnect()
        except TypeError:
            pass
            
        # Clear existing layout except the stretch
        while self.scroll_layout.count() > 1:
            item = self.scroll_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
        scene_ui_refs = []  # list of (scene, status_lbl_img, status_lbl_aud, img_preview, view_img_btn, play_aud_btn)
        
        for i, scene in enumerate(scenes):
            card = QFrame()
            card.setStyleSheet("background-color: #2b2b36; border-radius: 8px; padding: 10px;")
            card_main_layout = QHBoxLayout() # Horizontal wrap to put image on the right
            
            # Left side: Text and Buttons
            left_layout = QVBoxLayout()
            
            header = QLabel(f"🎬 Scene {i+1}")
            header.setProperty("class", "h2")
            left_layout.addWidget(header)
            
            visual_lbl = QLabel(f"<b>Visual (Nano-Banana Prompt):</b><br>{scene['visual']}")
            visual_lbl.setWordWrap(True)
            left_layout.addWidget(visual_lbl)
            
            narration_lbl = QLabel(f"<b>Narration (TTS):</b><br>{scene['narration']}")
            narration_lbl.setWordWrap(True)
            left_layout.addWidget(narration_lbl)
            
            # Action Buttons & Status (Images)
            btn_layout_img = QHBoxLayout()
            gen_img_btn = QPushButton("🖼️ Generate Image")
            gen_img_btn.setProperty("class", "primary-button")
            view_img_btn = QPushButton("👁️ View Image")
            view_img_btn.setProperty("class", "secondary-button")
            view_img_btn.hide()
            
            status_lbl_img = QLabel("")
            status_lbl_img.setStyleSheet("color: #a6e3a1;")
            
            btn_layout_img.addWidget(gen_img_btn)
            btn_layout_img.addWidget(view_img_btn)
            btn_layout_img.addWidget(status_lbl_img)
            btn_layout_img.addStretch()
            left_layout.addLayout(btn_layout_img)
            
            # Action Buttons & Status (Audio)
            btn_layout_aud = QHBoxLayout()
            gen_aud_btn = QPushButton("🎙️ Generate Audio")
            gen_aud_btn.setProperty("class", "primary-button")
            play_aud_btn = QPushButton("▶️ Play Audio")
            play_aud_btn.setProperty("class", "secondary-button")
            play_aud_btn.hide()
            
            status_lbl_aud = QLabel("")
            status_lbl_aud.setStyleSheet("color: #a6e3a1;")
            
            btn_layout_aud.addWidget(gen_aud_btn)
            btn_layout_aud.addWidget(play_aud_btn)
            btn_layout_aud.addWidget(status_lbl_aud)
            btn_layout_aud.addStretch()
            left_layout.addLayout(btn_layout_aud)
            
            # Right side: Image Preview Area
            right_layout = QVBoxLayout()
            img_preview = QLabel("Image\nPreview")
            img_preview.setAlignment(Qt.AlignCenter)
            img_preview.setStyleSheet("background: #181825; border-radius: 6px; color: #585b70;")
            img_preview.setFixedSize(200, 112) # 16:9 aspect ratio forced
            right_layout.addWidget(img_preview)
            
            card_main_layout.addLayout(left_layout, stretch=3)
            card_main_layout.addLayout(right_layout, stretch=1)
            
            # Capture the scope for the handlers
            def create_img_handler(idx=i, prompt=scene['visual'], lbl=status_lbl_img, btn=gen_img_btn, p_lbl=img_preview, view_btn=view_img_btn):
                if not os.path.exists('assets'):
                    os.makedirs('assets')
                out_path = f"assets/scene_{idx+1}.jpg"
                btn.setEnabled(False)
                lbl.setText("⏳ Generating Image...")
                lbl.setStyleSheet("color: #f9e2af;") # Yellow
                
                thread = ImageGenerationThread(prompt, out_path)
                # Store reference so it is not garbage collected
                setattr(self, f"img_thread_{idx}", thread)
                
                def on_finish(success, path):
                    btn.setEnabled(True)
                    if success:
                        lbl.setText("✅ Image Generated!")
                        lbl.setStyleSheet("color: #a6e3a1;") # Green
                        pixmap = QPixmap(path)
                        # Scale down for preview
                        p_lbl.setPixmap(pixmap.scaled(200, 112, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
                        scene['img_path'] = path
                        
                        # Sync string path to Database
                        db.update_scene_asset(scene.get('db_id'), 'img_path', path)
                        
                        view_btn.show()
                        view_btn.clicked.connect(lambda: os.startfile(os.path.abspath(path)) if os.name == 'nt' else None)
                    else:
                        lbl.setText("❌ Image Failed.")
                        lbl.setStyleSheet("color: #f38ba8;") # Red
                        
                thread.finished.connect(on_finish)
                thread.start()

            def create_aud_handler(idx=i, text=scene['narration'], lbl=status_lbl_aud, btn=gen_aud_btn, play_btn=play_aud_btn):
                if not os.path.exists('assets'):
                    os.makedirs('assets')
                out_path = f"assets/scene_{idx+1}.mp3"
                btn.setEnabled(False)
                lbl.setText("⏳ Generating Audio...")
                lbl.setStyleSheet("color: #f9e2af;")
                
                thread = AudioGenerationThread(text, out_path)
                setattr(self, f"aud_thread_{idx}", thread)
                
                def on_finish(success, path):
                    btn.setEnabled(True)
                    if success:
                        lbl.setText(f"✅ Audio Saved.")
                        lbl.setStyleSheet("color: #a6e3a1;")
                        scene['audio_path'] = path
                        
                        # Sync String Path to Database
                        db.update_scene_asset(scene.get('db_id'), 'audio_path', path)
                        
                        play_btn.show()
                        play_btn.clicked.connect(lambda: os.startfile(os.path.abspath(path)) if os.name == 'nt' else None)
                    else:
                        lbl.setText("❌ Audio Failed.")
                        lbl.setStyleSheet("color: #f38ba8;")
                        
                thread.finished.connect(on_finish)
                thread.start()
                
                
            gen_img_btn.clicked.connect(create_img_handler)
            gen_aud_btn.clicked.connect(create_aud_handler)
            
            # Register this card's UI refs so BulkGenerationThread can update them
            scene_ui_refs.append((scene, status_lbl_img, status_lbl_aud, img_preview, view_img_btn, play_aud_btn))
            
            card.setLayout(card_main_layout)
            # Insert before the stretch
            self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, card)

        def trigger_all():
            """Spawn a sequential BulkGenerationThread that processes each scene in order."""
            self.generate_all_btn.setEnabled(False)
            self.generate_all_btn.setText("⏳ Generating...")

            # Build a quick lookup from scene index to its UI labels
            status_map = {}  # index -> (img_lbl, aud_lbl, img_preview_lbl, view_img_btn, play_aud_btn)
            for j, (sc, lbl_img, lbl_aud, p_lbl, v_btn, pl_btn) in enumerate(scene_ui_refs):
                status_map[j] = (lbl_img, lbl_aud, p_lbl, v_btn, pl_btn)

            bulk_thread = BulkGenerationThread(scenes)
            setattr(self, '_bulk_thread', bulk_thread)

            def on_scene_progress(idx, asset_type, msg):
                if idx not in status_map:
                    return
                lbl_img, lbl_aud, p_lbl, v_btn, pl_btn = status_map[idx]
                if asset_type == 'img':
                    lbl_img.setText(msg)
                    lbl_img.setStyleSheet("color: #a6e3a1;" if '✅' in msg else ("color: #f9e2af;" if '⏳' in msg else "color: #f38ba8;"))
                    if '✅' in msg:
                        path = scenes[idx].get('img_path', '')
                        if path and os.path.exists(path):
                            pix = QPixmap(path)
                            p_lbl.setPixmap(pix.scaled(200, 112, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
                            v_btn.show()
                            v_btn.clicked.connect(lambda _, p=path: os.startfile(os.path.abspath(p)) if os.name == 'nt' else None)
                else:
                    lbl_aud.setText(msg)
                    lbl_aud.setStyleSheet("color: #a6e3a1;" if '✅' in msg else ("color: #f9e2af;" if '⏳' in msg else "color: #f38ba8;"))
                    if '✅' in msg:
                        path = scenes[idx].get('audio_path', '')
                        if path:
                            pl_btn.show()
                            pl_btn.clicked.connect(lambda _, p=path: os.startfile(os.path.abspath(p)) if os.name == 'nt' else None)

            def on_all_done(all_ok, msg):
                self.generate_all_btn.setEnabled(True)
                self.generate_all_btn.setText("⚡ Auto-Generate All Scenes")
                if all_ok:
                    self.generate_all_btn.setText("✅ All Scenes Generated")
                else:
                    QMessageBox.warning(self, "Generation Issues", msg)

            bulk_thread.scene_progress.connect(on_scene_progress)
            bulk_thread.all_done.connect(on_all_done)
            bulk_thread.start()

        self.generate_all_btn.clicked.connect(trigger_all)

class ExportTab(QWidget):
    back_requested = pyqtSignal()
    start_stitch = pyqtSignal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        label = QLabel("Video Export Settings")
        label.setAlignment(Qt.AlignCenter)
        label.setProperty("class", "h2")
        layout.addWidget(label)

        frame = QFrame()
        frame.setStyleSheet("background-color: #2b2b36; border-radius: 8px;")
        self.frame_layout = QVBoxLayout()
        
        self.status_lbl = QLabel("Ready to export once all assets are generated.", alignment=Qt.AlignCenter)
        self.status_lbl.setWordWrap(True)
        self.frame_layout.addWidget(self.status_lbl)
        
        frame.setLayout(self.frame_layout)
        layout.addWidget(frame)
        
        # Bottom nav
        nav_layout = QHBoxLayout()
        self.back_btn = QPushButton("⬅ Back to Storyboard")
        self.back_btn.clicked.connect(self.back_requested.emit)
        
        self.render_btn = QPushButton("🎬 Stitch Final Video")
        self.render_btn.setProperty("class", "success-button")
        self.render_btn.clicked.connect(self.start_stitch.emit)
        
        nav_layout.addWidget(self.back_btn)
        nav_layout.addStretch()
        nav_layout.addWidget(self.render_btn)

        layout.addLayout(nav_layout)
        self.setLayout(layout)
        
    def reset_ui(self):
        self.status_lbl.setText("Ready to stitch.")
        self.status_lbl.setStyleSheet("color: #cdd6f4;")
        self.render_btn.setEnabled(True)

class HistoryTab(QWidget):
    """Shows all previously generated scripts and lets the user inspect assets and play the final video."""

    def __init__(self):
        super().__init__()
        root_layout = QVBoxLayout()
        root_layout.setContentsMargins(20, 20, 20, 20)
        root_layout.setSpacing(12)

        title_row = QHBoxLayout()
        title = QLabel("📚 History")
        title.setProperty("class", "h2")
        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.setProperty("class", "primary-button")
        refresh_btn.clicked.connect(self.load_history)
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(refresh_btn)
        root_layout.addLayout(title_row)

        # Splitter: left = list, right = detail
        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: #313244; width: 2px; }")

        # ---- LEFT PANEL: Topic List ----
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.topic_list = QListWidget()
        self.topic_list.setStyleSheet("""
            QListWidget { background: #181825; border: 1px solid #313244; border-radius: 6px; }
            QListWidget::item { padding: 10px 14px; border-bottom: 1px solid #2b2b36; }
            QListWidget::item:selected { background: #2563eb; color: #fff; border-radius: 4px; }
            QListWidget::item:hover:!selected { background: #28283d; }
        """)
        self.topic_list.currentRowChanged.connect(self._on_topic_selected)
        left_layout.addWidget(self.topic_list)
        left_panel.setMinimumWidth(240)

        # ---- RIGHT PANEL: Detail view ----
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_layout.setSpacing(10)

        # Script area
        self.detail_header = QLabel("Select a topic on the left to view details.")
        self.detail_header.setProperty("class", "h2")
        self.detail_header.setWordWrap(True)
        right_layout.addWidget(self.detail_header)

        self.script_view = QTextBrowser()
        self.script_view.setMaximumHeight(180)
        self.script_view.setStyleSheet(
            "QTextBrowser { background: #11111b; border: 1px solid #313244; border-radius: 6px; color: #cdd6f4; padding: 8px; }"
        )
        self.script_view.setPlaceholderText("Script will appear here...")
        right_layout.addWidget(QLabel("📄 Generated Script:"))
        right_layout.addWidget(self.script_view)

        # Final video row
        vid_row = QHBoxLayout()
        self.video_lbl = QLabel("No final video found for this topic.")
        self.video_lbl.setStyleSheet("color: #585b70;")
        self.play_video_btn = QPushButton("▶️  Play Final Video")
        self.play_video_btn.setProperty("class", "success-button")
        self.play_video_btn.hide()
        self.play_video_btn.clicked.connect(self._play_video)
        vid_row.addWidget(self.video_lbl)
        vid_row.addStretch()
        vid_row.addWidget(self.play_video_btn)
        right_layout.addLayout(vid_row)

        # Scene scroll area
        right_layout.addWidget(QLabel("🎬 Scenes & Assets:"))
        self.scenes_scroll = QScrollArea()
        self.scenes_scroll.setWidgetResizable(True)
        self.scenes_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.scenes_widget = QWidget()
        self.scenes_layout = QVBoxLayout(self.scenes_widget)
        self.scenes_layout.setSpacing(8)
        self.scenes_layout.addStretch()
        self.scenes_scroll.setWidget(self.scenes_widget)
        right_layout.addWidget(self.scenes_scroll)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([260, 780])
        root_layout.addWidget(splitter)
        self.setLayout(root_layout)

        self._topic_rows = []   # cache of db rows
        self._current_video_path = None
        self.load_history()

    # ------------------------------------------------------------------ #
    def load_history(self):
        self.topic_list.clear()
        self._topic_rows = db.get_all_topics()
        for row in self._topic_rows:
            tid, topic, duration, created_at = row
            item = QListWidgetItem(f"{topic}\n{duration} min  |  {created_at[:16]}")
            item.setSizeHint(QSize(0, 54))
            self.topic_list.addItem(item)

    def _on_topic_selected(self, index):
        if index < 0 or index >= len(self._topic_rows):
            return
        tid = self._topic_rows[index][0]
        topic_row, scenes = db.get_topic_detail(tid)
        if not topic_row:
            return

        _, topic, duration, script_text, created_at = topic_row
        self.detail_header.setText(f"🎬 {topic}  ({duration} min)  ·  {created_at[:16]}")
        self.script_view.setPlainText(script_text or "")

        # Check for a final video in the assets folder
        safe_topic = re.sub(r'[\\/:*?"<>|]', '_', topic).strip()[:80]
        video_path = os.path.join(os.path.dirname(__file__), 'assets', f"{safe_topic}.mp4")
        if os.path.exists(video_path):
            self._current_video_path = video_path
            self.video_lbl.setText(f"✅ {os.path.basename(video_path)}")
            self.video_lbl.setStyleSheet("color: #a6e3a1;")
            self.play_video_btn.show()
        else:
            self._current_video_path = None
            self.video_lbl.setText("No final video found for this topic.")
            self.video_lbl.setStyleSheet("color: #585b70;")
            self.play_video_btn.hide()

        # Rebuild scenes
        while self.scenes_layout.count() > 1:
            item = self.scenes_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for scene_row in scenes:
            sid, order, visual, narration, img_path, audio_path = scene_row
            card = QFrame()
            card.setStyleSheet("background-color: #2b2b36; border-radius: 6px; padding: 8px;")
            card_layout = QHBoxLayout(card)

            # Text block
            text_block = QVBoxLayout()
            lbl_title = QLabel(f"<b>Scene {order}</b>")
            lbl_visual = QLabel(f"<i>Visual:</i> {visual}")
            lbl_visual.setWordWrap(True)
            lbl_narr = QLabel(f"<i>Narration:</i> {narration}")
            lbl_narr.setWordWrap(True)
            text_block.addWidget(lbl_title)
            text_block.addWidget(lbl_visual)
            text_block.addWidget(lbl_narr)

            # Asset buttons
            btn_block = QVBoxLayout()
            btn_block.setAlignment(Qt.AlignTop)
            if img_path and os.path.exists(img_path):
                view_img = QPushButton("🖥️ View Image")
                view_img.setProperty("class", "secondary-button")
                view_img.clicked.connect(lambda checked, p=img_path: os.startfile(os.path.abspath(p)))
                btn_block.addWidget(view_img)
            if audio_path and os.path.exists(audio_path):
                play_aud = QPushButton("🔊 Play Audio")
                play_aud.setProperty("class", "secondary-button")
                play_aud.clicked.connect(lambda checked, p=audio_path: os.startfile(os.path.abspath(p)))
                btn_block.addWidget(play_aud)

            # Thumbnail
            thumb = QLabel()
            thumb.setFixedSize(142, 80)
            thumb.setAlignment(Qt.AlignCenter)
            if img_path and os.path.exists(img_path):
                pix = QPixmap(img_path)
                thumb.setPixmap(pix.scaled(142, 80, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            else:
                thumb.setText("No Image")
                thumb.setStyleSheet("background:#181825; color:#585b70; border-radius:4px;")

            card_layout.addLayout(text_block, stretch=3)
            card_layout.addLayout(btn_block)
            card_layout.addWidget(thumb)

            self.scenes_layout.insertWidget(self.scenes_layout.count() - 1, card)

    def _play_video(self):
        if self._current_video_path:
            os.startfile(os.path.abspath(self._current_video_path))


class AppWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Infographic Video Generator")
        self.resize(1050, 750)
        
        # Enable native windows dark title bar
        try:
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            set_window_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            get_parent = ctypes.windll.user32.GetParent
            hwnd = get_parent(self.winId())
            if not hwnd:
                hwnd = self.winId()
            value = ctypes.c_int(2)
            set_window_attribute(int(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass
        
        # Main Layout
        self.tabs = QTabWidget()
        
        self.script_tab = ScriptTab()
        self.storyboard_tab = StoryboardTab()
        self.export_tab = ExportTab()
        self.history_tab = HistoryTab()
        
        self.tabs.addTab(self.script_tab, "✍️ Scripting")
        self.tabs.addTab(self.storyboard_tab, "🎨 Storyboard")
        self.tabs.addTab(self.export_tab, "🎬 Export Video")
        self.tabs.addTab(self.history_tab, "📚 History")
        
        self.setCentralWidget(self.tabs)
        
        self.current_scenes = []
        self.current_topic = ""

        # Connect Signals for Wizard Flow
        self.script_tab.next_requested.connect(self.on_script_next)
        self.script_tab.next_requested_topic.connect(lambda t: setattr(self, 'current_topic', t))
        self.storyboard_tab.back_requested.connect(lambda: self.tabs.setCurrentIndex(0))
        self.storyboard_tab.next_requested.connect(lambda: self.tabs.setCurrentIndex(2))
        self.export_tab.back_requested.connect(lambda: self.tabs.setCurrentIndex(1))
        self.export_tab.start_stitch.connect(self.start_stitching_process)
        # Refresh history list whenever a new script is saved
        self.script_tab.next_requested.connect(lambda _: self.history_tab.load_history())

    def on_script_next(self, scenes):
        self.current_scenes = scenes
        self.storyboard_tab.load_scenes(self.current_scenes)
        self.tabs.setCurrentIndex(1)
        self.export_tab.reset_ui()
        
    def start_stitching_process(self):
        # Strict upfront completeness check — collect every missing asset
        missing = []
        for i, scene in enumerate(self.current_scenes):
            if not scene.get('img_path') or not os.path.exists(scene.get('img_path', '')):
                missing.append(f"Scene {i+1}: Image missing")
            if not scene.get('audio_path') or not os.path.exists(scene.get('audio_path', '')):
                missing.append(f"Scene {i+1}: Audio missing")
                
        if missing:
            QMessageBox.warning(
                self, "Assets Incomplete",
                "Cannot stitch — the following assets are still missing:\n\n" + "\n".join(missing)
            )
            return
            
        self.export_tab.render_btn.setEnabled(False)
        self.export_tab.status_lbl.setStyleSheet("color: #f9e2af;") # yellow
        self.export_tab.status_lbl.setText(f"⏳ Stitching {len(self.current_scenes)} scenes...")
        
        if not os.path.exists('assets'):
            os.makedirs('assets')

        # Sanitize the topic to be a valid filename
        safe_topic = re.sub(r'[\\/:*?"<>|]', '_', self.current_topic or 'infographic').strip()
        safe_topic = safe_topic[:80]  # Cap length
        out_path = os.path.join(os.getcwd(), 'assets', f"{safe_topic}.mp4")
        
        self.stitch_thread = VideoStitchingThread(self.current_scenes, out_path)
        self.stitch_thread.progress.connect(lambda msg: self.export_tab.status_lbl.setText(msg))
        self.stitch_thread.finished.connect(self.on_stitch_finished)
        self.stitch_thread.start()
        
    def on_stitch_finished(self, success, result_msg):
        self.export_tab.render_btn.setEnabled(True)
        if success:
            self.export_tab.status_lbl.setStyleSheet("color: #a6e3a1;") # green
            self.export_tab.status_lbl.setText(f"✅ Awesome! Video successfully saved to:\n{result_msg}")
        else:
            self.export_tab.status_lbl.setStyleSheet("color: #f38ba8;") # red
            self.export_tab.status_lbl.setText(f"❌ Failed to stitch video:\n{result_msg}")

# --- MODERN DARK THEME QSS ---
STYLESHEET = """
QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: 'Segoe UI', Inter, sans-serif;
    font-size: 14px;
}

QLabel.h2 {
    font-size: 18px;
    font-weight: bold;
    color: #b4befe;
}

QTabWidget::pane {
    border: 1px solid #313244;
    border-radius: 8px;
    background-color: #1e1e2e;
}

QTabBar::tab {
    background: #181825;
    color: #a6adc8;
    padding: 12px 32px;
    min-width: 130px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    margin-right: 4px;
    font-weight: bold;
    border: 1px solid transparent;
}

QTabBar::tab:selected {
    background: #313244;
    color: #89b4fa;
    border: 1px solid #45475a;
    border-bottom: none;
}

QTabBar::tab:hover:!selected {
    background: #28283d;
}

QLineEdit, QTextEdit {
    background-color: #11111b;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 10px;
    color: #cdd6f4;
}

QLineEdit:focus, QTextEdit:focus {
    border: 1px solid #89b4fa;
}

QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: none;
    border-radius: 6px;
    padding: 10px 18px;
    font-weight: 500;
}

QPushButton:hover {
    background-color: #45475a;
}

QPushButton:pressed {
    background-color: #585b70;
}

/* Specific Button Classes setup via setProperty */
QPushButton[class="primary-button"] {
    background-color: #2563eb;
    color: #ffffff;
    font-weight: bold;
}
QPushButton[class="primary-button"]:hover {
    background-color: #3b82f6;
}

QPushButton[class="secondary-button"] {
    background-color: #45475a;
    color: #ffffff;
    font-weight: bold;
}
QPushButton[class="secondary-button"]:hover {
    background-color: #585b70;
}

QPushButton[class="success-button"] {
    background-color: #16a34a;
    color: #ffffff;
    font-weight: bold;
}
QPushButton[class="success-button"]:hover {
    background-color: #22c55e;
}
"""

if __name__ == '__main__':
    db.init_db() # Create tables if not strictly present
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    window = AppWindow()
    window.show()
    sys.exit(app.exec_())
