import sys
import os
import re
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
    QTextBrowser, QCheckBox, QGridLayout, QSystemTrayIcon, QComboBox, QDialog, QSlider
)
from PyQt5.QtGui import QPixmap, QIcon
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer, QUrl
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
from ai_generator import (
    generate_script_from_topic,
    analyze_text_to_scenes,
    generate_image_from_prompt,
    generate_audio_from_text
)
import db
import moviepy.editor as mp
import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS

def make_safe_topic(topic: str) -> str:
    """Returns a filesystem-safe folder/file name derived from the topic."""
    safe = re.sub(r'[\\/:*?"<>|]', '_', topic or 'infographic').strip()
    return safe[:80]

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
    
    def __init__(self, text, output_path, engine="gemini"):
        super().__init__()
        self.text = text
        self.output_path = output_path
        self.engine = engine
        
    def run(self):
        success = generate_audio_from_text(self.text, self.output_path, self.engine)
        self.finished.emit(success, self.output_path)

class BulkGenerationThread(QThread):
    """
    Processes ALL scenes sequentially: image then audio for each scene, in order.
    Saves all assets into assets/{topic_folder}/scene_X.{ext}
    """
    scene_progress = pyqtSignal(int, str, str)
    all_done = pyqtSignal(bool, str)

    def __init__(self, scenes, topic_folder, tts_engine="gemini"):
        super().__init__()
        self.scenes = scenes
        self.topic_folder = topic_folder  # absolute path to topic sub-folder
        self.tts_engine = tts_engine
        self.is_cancelled = False

    def cancel(self):
        self.is_cancelled = True

    def run(self):
        os.makedirs(self.topic_folder, exist_ok=True)

        total = len(self.scenes)
        failed = []

        for i, scene in enumerate(self.scenes):
            if self.is_cancelled:
                self.all_done.emit(False, "Generation was stopped by the user.")
                return

            idx = i + 1

            # --- Image ---
            self.scene_progress.emit(i, 'img', f'⏳ Generating image {idx}/{total}...')
            img_path = os.path.join(self.topic_folder, f"scene_{idx}.jpg")
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
            audio_ext = "wav" if self.tts_engine == "gemini" else "mp3"
            aud_path = os.path.join(self.topic_folder, f"scene_{idx}.{audio_ext}")
            aud_ok = generate_audio_from_text(scene.get('narration', ''), aud_path, self.tts_engine)
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
    progress_msg = pyqtSignal(str)
    progress_pct = pyqtSignal(int)      # 0..100
    finished = pyqtSignal(bool, str)

    PAUSE_DURATION = 0.4  # seconds of freeze between scenes
    FPS = 24

    def __init__(self, scenes, output_path):
        super().__init__()
        self.scenes = scenes
        self.output_path = output_path

    def run(self):
        try:
            total = len(self.scenes)
            clips = []

            self.progress_msg.emit(f"Building {total} scene clips...")
            self.progress_pct.emit(0)

            for i, scene in enumerate(self.scenes):
                img_path = scene.get('img_path')
                aud_path = scene.get('audio_path')

                if not img_path or not os.path.exists(img_path):
                    self.finished.emit(False, f"Missing Image for Scene {i+1}.")
                    return
                if not aud_path or not os.path.exists(aud_path):
                    self.finished.emit(False, f"Missing Audio for Scene {i+1}.")
                    return

                self.progress_msg.emit(f"Scene {i+1}/{total} — adding motion FX...")
                pct = int((i / total) * 60)  # first 60% of progress bar = clip building
                self.progress_pct.emit(pct)

                audio_clip = mp.AudioFileClip(aud_path)
                dur = audio_clip.duration

                # Cinematic slow zoom-in (1.0x → 1.05x)
                img_clip = mp.ImageClip(img_path).set_duration(dur)
                img_zoomed = img_clip.resize(lambda t: 1.0 + 0.05 * (t / dur))
                scene_clip = (
                    mp.CompositeVideoClip([img_zoomed.set_pos('center')], size=img_clip.size)
                    .set_duration(dur)
                    .set_audio(audio_clip)
                )

                # Add pause — freeze last frame of scene (silent) for natural breathing room
                freeze = mp.ImageClip(img_path).set_duration(self.PAUSE_DURATION)

                clips.append(scene_clip)
                clips.append(freeze)  # pause after EVERY scene including the last

            self.progress_msg.emit("Applying crossfade transitions...")
            self.progress_pct.emit(62)

            crossfade = 0.5
            # Interleave: scene -> pause -> scene -> pause ...
            # Apply crossfade to non-pause clips only (pause clips are silent/simple)
            final_clips = [clips[0]]  # first scene clip (no crossfade on very first)
            for j, c in enumerate(clips[1:], start=1):
                # Only crossfade on actual scene clips, not freeze frames
                if j % 2 == 0:  # even indices = scene clips
                    final_clips.append(c.crossfadein(crossfade))
                else:           # odd indices = freeze pauses
                    final_clips.append(c)

            self.progress_msg.emit("Concatenating timeline...")
            self.progress_pct.emit(70)
            final_video = mp.concatenate_videoclips(final_clips, padding=-crossfade, method="compose")

            self.progress_msg.emit(f"Encoding video (ultrafast)...")
            self.progress_pct.emit(75)

            final_video.write_videofile(
                self.output_path,
                fps=self.FPS,
                codec="libx264",
                audio_codec="aac",
                preset="ultrafast",
                threads=4,
                logger=None
            )

            self.progress_pct.emit(100)
            self.finished.emit(True, self.output_path)
        except Exception as e:
            self.finished.emit(False, str(e))

class ScriptGenerationThread(QThread):
    chunk_received = pyqtSignal(str)
    finished = pyqtSignal()
    
    def __init__(self, topic, duration, use_web_search=False):
        super().__init__()
        self.topic = topic
        self.duration = duration
        self.use_web_search = use_web_search
        
    def run(self):
        for chunk in generate_script_from_topic(self.topic, self.duration, self.use_web_search):
            self.chunk_received.emit(chunk)
        self.finished.emit()

class AnalyzeTextThread(QThread):
    chunk_received = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, source_text):
        super().__init__()
        self.source_text = source_text

    def run(self):
        for chunk in analyze_text_to_scenes(self.source_text):
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

        self.web_search_chk = QCheckBox("🌐 Search Web")
        self.web_search_chk.setToolTip(
            "When checked, Gemini will search Google for up-to-date\n"
            "information about the topic before writing the script."
        )
        self.web_search_chk.setStyleSheet("QCheckBox { font-size: 13px; color: #cdd6f4; }")
        
        topic_layout.addWidget(QLabel("🚀 Topic:"))
        topic_layout.addWidget(self.topic_input)
        topic_layout.addWidget(QLabel("⏱️ Length:"))
        topic_layout.addWidget(self.duration_spin)
        topic_layout.addWidget(self.web_search_chk)
        topic_layout.addWidget(self.generate_btn)
        layout.addLayout(topic_layout)

        # --- Analyze Text Panel ---
        analyze_frame = QFrame()
        analyze_frame.setStyleSheet("background-color: #2b2b36; border-radius: 8px; padding: 8px;")
        analyze_frame_layout = QVBoxLayout(analyze_frame)
        analyze_frame_layout.setSpacing(6)

        analyze_header = QHBoxLayout()
        analyze_title = QLabel("📝 Analyze & Convert Text to Scenes")
        analyze_title.setProperty("class", "h2")
        analyze_header.addWidget(analyze_title)
        analyze_header.addStretch()
        analyze_frame_layout.addLayout(analyze_header)

        self.analyze_input = QTextEdit()
        self.analyze_input.setPlaceholderText(
            "Paste any text here (article, blog post, notes, essay...)\n"
            "AI will analyze it and extract scenes with visual prompts and narration."
        )
        self.analyze_input.setMaximumHeight(120)
        analyze_frame_layout.addWidget(self.analyze_input)

        analyze_btn_row = QHBoxLayout()
        self.analyze_btn = QPushButton("🔍 Analyze & Generate Scenes")
        self.analyze_btn.setProperty("class", "primary-button")
        self.analyze_btn.clicked.connect(self.analyze_text)
        self.analyze_status = QLabel("")
        self.analyze_status.setStyleSheet("color: #a6e3a1;")
        analyze_btn_row.addWidget(self.analyze_btn)
        analyze_btn_row.addWidget(self.analyze_status)
        analyze_btn_row.addStretch()
        analyze_frame_layout.addLayout(analyze_btn_row)

        layout.addWidget(analyze_frame)
        
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
        if self.web_search_chk.isChecked():
            self.generate_btn.setText("🌐 Searching & Generating...")
        
        self.script_editor.clear()
        use_web = self.web_search_chk.isChecked()
        self.thread = ScriptGenerationThread(topic, duration, use_web)
        self.thread.chunk_received.connect(self.on_chunk_received)
        self.thread.finished.connect(self.on_script_generated)
        self.thread.start()

    def analyze_text(self):
        source_text = self.analyze_input.toPlainText().strip()
        if not source_text:
            QMessageBox.warning(self, "Empty Input", "Please paste some text to analyze.")
            return
        
        self.analyze_btn.setEnabled(False)
        self.analyze_btn.setText("⏳ Analyzing...")
        self.analyze_status.setText("⏳ Sending to Gemini...")
        self.analyze_status.setStyleSheet("color: #f9e2af;")
        self.script_editor.clear()
        
        self.analyze_thread = AnalyzeTextThread(source_text)
        self.analyze_thread.chunk_received.connect(self.on_chunk_received)
        self.analyze_thread.finished.connect(self.on_analyze_done)
        self.analyze_thread.start()
        
    def on_analyze_done(self):
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("🔍 Analyze & Generate Scenes")
        self.analyze_status.setText("✅ Done! Review script below then click Next.")
        self.analyze_status.setStyleSheet("color: #a6e3a1;")
        
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
        self.tts_engine_combo = QComboBox()
        self.tts_engine_combo.addItems(["Gemini (Puck Voice)", "Google Cloud (Journey-D)"])
        self.tts_engine_combo.setToolTip("Select the Text-to-Speech Engine for audio generation.")
        
        self.generate_all_btn = QPushButton("⚡ Auto-Generate All Scenes")
        self.generate_all_btn.setProperty("class", "success-button")
        
        self.stop_btn = QPushButton("⏹️ Stop")
        self.stop_btn.setProperty("class", "danger-button")
        self.stop_btn.hide()
        
        header_layout.addWidget(label)
        header_layout.addStretch()
        header_layout.addWidget(QLabel("TTS Engine:"))
        header_layout.addWidget(self.tts_engine_combo)
        header_layout.addWidget(self.generate_all_btn)
        header_layout.addWidget(self.stop_btn)
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

    def load_scenes(self, scenes, topic=''):
        self._topic_folder = os.path.join(
            os.path.dirname(__file__), 'assets', make_safe_topic(topic)
        )
        os.makedirs(self._topic_folder, exist_ok=True)
                
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
            def create_img_handler(idx=i, prompt=scene['visual'], lbl=status_lbl_img, btn=gen_img_btn, p_lbl=img_preview, view_btn=view_img_btn, tdir=self._topic_folder):
                os.makedirs(tdir, exist_ok=True)
                out_path = os.path.join(tdir, f"scene_{idx+1}.jpg")
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

            def create_aud_handler(idx=i, text=scene['narration'], lbl=status_lbl_aud, btn=gen_aud_btn, play_btn=play_aud_btn, tdir=self._topic_folder):
                os.makedirs(tdir, exist_ok=True)
                audio_ext = "wav" if "Gemini" in self.tts_engine_combo.currentText() else "mp3"
                out_path = os.path.join(tdir, f"scene_{idx+1}.{audio_ext}")
                btn.setEnabled(False)
                lbl.setText("⏳ Generating Audio...")
                lbl.setStyleSheet("color: #f9e2af;")
                
                engine = "gemini" if "Gemini" in self.tts_engine_combo.currentText() else "google"
                thread = AudioGenerationThread(text, out_path, engine)
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
            self.stop_btn.show()
            self.stop_btn.setEnabled(True)
            self.stop_btn.setText("⏹️ Stop")
            
            engine = "gemini" if "Gemini" in self.tts_engine_combo.currentText() else "google"

            # Build a quick lookup from scene index to its UI labels
            status_map = {}  # index -> (img_lbl, aud_lbl, img_preview_lbl, view_img_btn, play_aud_btn)
            for j, (sc, lbl_img, lbl_aud, p_lbl, v_btn, pl_btn) in enumerate(scene_ui_refs):
                status_map[j] = (lbl_img, lbl_aud, p_lbl, v_btn, pl_btn)

            bulk_thread = BulkGenerationThread(scenes, self._topic_folder, engine)
            setattr(self, '_bulk_thread', bulk_thread)

            try:
                self.stop_btn.clicked.disconnect()
            except TypeError:
                pass
            
            def stop_generation():
                bulk_thread.cancel()
                self.stop_btn.setEnabled(False)
                self.stop_btn.setText("⏳ Stopping...")
                
            self.stop_btn.clicked.connect(stop_generation)

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
                self.stop_btn.hide()
                if all_ok:
                    self.generate_all_btn.setText("✅ All Scenes Generated")
                else:
                    QMessageBox.warning(self, "Generation Issues/Stopped", msg)

            bulk_thread.scene_progress.connect(on_scene_progress)
            bulk_thread.all_done.connect(on_all_done)
            bulk_thread.start()

        self.generate_all_btn.clicked.connect(trigger_all)

class ExportTab(QWidget):
    back_requested = pyqtSignal()
    start_stitch = pyqtSignal()

    def __init__(self):
        super().__init__()
        root = QVBoxLayout()
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        header_row = QHBoxLayout()
        title = QLabel("🎬 Export Video")
        title.setProperty("class", "h2")
        self.back_btn = QPushButton("⬅ Back to Storyboard")
        self.back_btn.clicked.connect(self.back_requested.emit)
        header_row.addWidget(title)
        header_row.addStretch()
        header_row.addWidget(self.back_btn)
        root.addLayout(header_row)

        # ── Thumbnail Grid ────────────────────────────────────────────────
        thumb_label = QLabel("🖼️ Scene Thumbnails")
        thumb_label.setStyleSheet("font-weight:bold; color:#cdd6f4;")
        root.addWidget(thumb_label)

        self.thumb_scroll = QScrollArea()
        self.thumb_scroll.setWidgetResizable(True)
        self.thumb_scroll.setMaximumHeight(160)
        self.thumb_scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self.thumb_widget = QWidget()
        self.thumb_layout = QHBoxLayout(self.thumb_widget)
        self.thumb_layout.setSpacing(8)
        self.thumb_layout.setContentsMargins(0, 0, 0, 0)
        self.thumb_layout.addStretch()
        self.thumb_scroll.setWidget(self.thumb_widget)
        root.addWidget(self.thumb_scroll)

        # ── Status & Progress ────────────────────────────────────────────
        status_frame = QFrame()
        status_frame.setStyleSheet("background:#2b2b36; border-radius:8px; padding:12px;")
        sf_layout = QVBoxLayout(status_frame)

        self.status_lbl = QLabel("Ready to export once all assets are generated.",
                                 alignment=Qt.AlignCenter)
        self.status_lbl.setWordWrap(True)
        sf_layout.addWidget(self.status_lbl)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(10)
        self.progress_bar.setStyleSheet("""
            QProgressBar { background:#181825; border-radius:5px; }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #2563eb, stop:1 #a855f7); border-radius:5px; }
        """)
        self.progress_bar.hide()
        sf_layout.addWidget(self.progress_bar)

        self.pct_lbl = QLabel("", alignment=Qt.AlignCenter)
        self.pct_lbl.setStyleSheet("color:#89b4fa; font-weight:bold; font-size:13px;")
        self.pct_lbl.hide()
        sf_layout.addWidget(self.pct_lbl)

        root.addWidget(status_frame)
        root.addStretch()

        # ── Render button ────────────────────────────────────────────────
        self.render_btn = QPushButton("🎬 Stitch Final Video")
        self.render_btn.setProperty("class", "success-button")
        self.render_btn.setMinimumHeight(44)
        self.render_btn.clicked.connect(self.start_stitch.emit)
        root.addWidget(self.render_btn)

        # ── Post-render action buttons (hidden until video is ready) ─────
        action_row = QHBoxLayout()
        self.view_video_btn = QPushButton("🎬 View Video")
        self.view_video_btn.setProperty("class", "success-button")
        self.view_video_btn.setMinimumHeight(40)
        self.view_video_btn.hide()
        self.open_folder_btn = QPushButton("📂 Open Folder")
        self.open_folder_btn.setProperty("class", "secondary-button")
        self.open_folder_btn.setMinimumHeight(40)
        self.open_folder_btn.hide()
        action_row.addWidget(self.view_video_btn)
        action_row.addWidget(self.open_folder_btn)
        root.addLayout(action_row)

        self.setLayout(root)

        # Spinner timer for animated dots while rendering
        self._spin_dots = 0
        self._spin_timer = QTimer()
        self._spin_timer.timeout.connect(self._spin_tick)

    # ── Helpers ──────────────────────────────────────────────────────────
    def populate_thumbnails(self, scenes):
        """Populate the horizontal thumbnail grid from scene img_paths."""
        # Clear old thumbs
        while self.thumb_layout.count() > 1:
            item = self.thumb_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for i, scene in enumerate(scenes):
            img_path = scene.get('img_path', '')
            cell = QFrame()
            cell.setFixedSize(130, 90)
            cell.setStyleSheet("background:#181825; border-radius:4px;")
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)

            thumb = QLabel()
            thumb.setFixedSize(130, 73)
            thumb.setAlignment(Qt.AlignCenter)
            if img_path and os.path.exists(img_path):
                pix = QPixmap(img_path).scaled(130, 73, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                thumb.setPixmap(pix)
            else:
                thumb.setText("No Image")
                thumb.setStyleSheet("color:#585b70; font-size:10px;")

            num_lbl = QLabel(f"Scene {i+1}", alignment=Qt.AlignCenter)
            num_lbl.setStyleSheet("color:#a6adc8; font-size:10px;")

            cell_layout.addWidget(thumb)
            cell_layout.addWidget(num_lbl)
            self.thumb_layout.insertWidget(self.thumb_layout.count() - 1, cell)

    def set_progress(self, pct: int, msg: str):
        self.status_lbl.setText(msg)
        self.progress_bar.setValue(pct)
        self.pct_lbl.setText(f"{pct}%")

    def start_render_ui(self):
        self.render_btn.setEnabled(False)
        self.render_btn.setText("⏳ Stitching...")
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.pct_lbl.setText("0%")
        self.pct_lbl.show()
        self.status_lbl.setStyleSheet("color:#f9e2af;")
        self._spin_timer.start(500)

    def stop_render_ui(self, success: bool, msg: str):
        self._spin_timer.stop()
        self.render_btn.setEnabled(True)
        self.render_btn.setText("🎬 Stitch Final Video")
        self.progress_bar.hide()
        self.pct_lbl.hide()
        if success:
            self.status_lbl.setStyleSheet("color:#a6e3a1;")
            self.status_lbl.setText(f"✅ Video saved:\n{msg}")
            # Wire and show action buttons
            try:
                self.view_video_btn.clicked.disconnect()
                self.open_folder_btn.clicked.disconnect()
            except TypeError:
                pass
            self.view_video_btn.clicked.connect(lambda: os.startfile(os.path.abspath(msg)))
            self.open_folder_btn.clicked.connect(
                lambda: os.startfile(os.path.abspath(os.path.dirname(msg)))
            )
            self.view_video_btn.show()
            self.open_folder_btn.show()
        else:
            self.status_lbl.setStyleSheet("color:#f38ba8;")
            self.status_lbl.setText(f"❌ Failed:\n{msg}")
            self.view_video_btn.hide()
            self.open_folder_btn.hide()

    def _spin_tick(self):
        self._spin_dots = (self._spin_dots + 1) % 4
        dots = '.' * self._spin_dots
        cur = self.status_lbl.text()
        # Strip old dots suffix cleanly
        base = cur.rstrip('.')
        self.status_lbl.setText(base.rstrip() + dots)

    def reset_ui(self):
        self.status_lbl.setText("Ready to stitch.")
        self.status_lbl.setStyleSheet("color: #cdd6f4;")
        self.render_btn.setEnabled(True)
        self.progress_bar.hide()
        self.pct_lbl.hide()
        self.view_video_btn.hide()
        self.open_folder_btn.hide()

class HistoryTab(QWidget):
    """Shows all previously generated scripts and lets the user inspect assets and play the final video."""
    restitch_requested = pyqtSignal(list, str)  # (scenes_as_dicts, topic)
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

        # ---- Right panel widgets (defined first, added to layout at the end) ----

        self.detail_header = QLabel("Select a topic on the left to view details.")
        self.detail_header.setProperty("class", "h2")
        self.detail_header.setWordWrap(True)

        # Final video row
        vid_row = QHBoxLayout()
        self.video_lbl = QLabel("No final video found for this topic.")
        self.video_lbl.setStyleSheet("color: #585b70;")
        self.play_video_btn = QPushButton("▶️  Play Final Video")
        self.play_video_btn.setProperty("class", "success-button")
        self.play_video_btn.hide()
        self.play_video_btn.clicked.connect(self._play_video)
        self.restitch_btn = QPushButton("🔁 Re-Stitch Video")
        self.restitch_btn.setProperty("class", "primary-button")
        self.restitch_btn.hide()
        self.restitch_btn.clicked.connect(self._on_restitch_clicked)
        self.view_video_btn = QPushButton("🎬 View Video")
        self.view_video_btn.setProperty("class", "success-button")
        self.view_video_btn.hide()
        self.open_folder_btn = QPushButton("📂 Open Folder")
        self.open_folder_btn.setProperty("class", "secondary-button")
        self.open_folder_btn.hide()
        vid_row.addWidget(self.video_lbl)
        vid_row.addStretch()
        vid_row.addWidget(self.restitch_btn)
        vid_row.addWidget(self.view_video_btn)
        vid_row.addWidget(self.open_folder_btn)
        vid_row.addWidget(self.play_video_btn)

        self.restitch_status = QLabel("")
        self.restitch_status.setAlignment(Qt.AlignCenter)
        self.restitch_status.setWordWrap(True)
        self.restitch_status.hide()
        self.restitch_progress = QProgressBar()
        self.restitch_progress.setRange(0, 100)
        self.restitch_progress.setFixedHeight(8)
        self.restitch_progress.setTextVisible(False)
        self.restitch_progress.setStyleSheet("""
            QProgressBar { background:#181825; border-radius:4px; }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #2563eb, stop:1 #a855f7); border-radius:4px; }
        """)
        self.restitch_progress.hide()

        # Script area
        self.script_view = QTextBrowser()
        self.script_view.setMaximumHeight(140)
        self.script_view.setStyleSheet(
            "QTextBrowser { background: #11111b; border: 1px solid #313244; border-radius: 6px; color: #cdd6f4; padding: 8px; }"
        )
        self.script_view.setPlaceholderText("Script will appear here...")

        # Scene controls
        scene_ctrl_row = QHBoxLayout()
        self.history_tts_engine_combo = QComboBox()
        self.history_tts_engine_combo.addItems(["Gemini (Puck Voice)", "Google Cloud (Journey-D)"])
        self.history_tts_engine_combo.setToolTip("Select TTS engine for history scene audio generation.")
        self.history_generate_all_btn = QPushButton("⚡ Auto-Generate All Scenes")
        self.history_generate_all_btn.setProperty("class", "success-button")
        self.history_generate_all_btn.clicked.connect(self._on_history_generate_all)
        self.history_stop_btn = QPushButton("⏹️ Stop")
        self.history_stop_btn.setProperty("class", "danger-button")
        self.history_stop_btn.hide()
        scene_ctrl_row.addWidget(QLabel("TTS Engine:"))
        scene_ctrl_row.addWidget(self.history_tts_engine_combo)
        scene_ctrl_row.addStretch()
        scene_ctrl_row.addWidget(self.history_generate_all_btn)
        scene_ctrl_row.addWidget(self.history_stop_btn)

        # Scenes scroll
        self.scenes_scroll = QScrollArea()
        self.scenes_scroll.setWidgetResizable(True)
        self.scenes_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.scenes_widget = QWidget()
        self.scenes_layout = QVBoxLayout(self.scenes_widget)
        self.scenes_layout.setSpacing(8)
        self.scenes_layout.addStretch()
        self.scenes_scroll.setWidget(self.scenes_widget)

        # History audio player (in-app) — pinned to bottom
        self.history_player_frame = QFrame()
        self.history_player_frame.setStyleSheet(
            "background:#1e1e2e; border:1px solid #313244; border-radius:8px;"
        )
        hp_layout = QVBoxLayout(self.history_player_frame)
        hp_layout.setContentsMargins(10, 6, 10, 6)
        hp_layout.setSpacing(2)

        player_head = QHBoxLayout()
        self.history_now_playing_lbl = QLabel("🎧 Now Playing: None")
        self.history_now_playing_lbl.setStyleSheet("color:#cdd6f4; font-weight:600;")
        self.history_audio_state_lbl = QLabel("Stopped")
        self.history_audio_state_lbl.setStyleSheet("color:#a6adc8;")
        self.history_now_playing_lbl.hide()
        self.history_audio_state_lbl.hide()
        player_head.addWidget(self.history_now_playing_lbl)
        player_head.addStretch()
        player_head.addWidget(self.history_audio_state_lbl)
        hp_layout.addLayout(player_head)

        self.history_audio_slider = QSlider(Qt.Horizontal)
        self.history_audio_slider.setRange(0, 0)
        self.history_audio_slider.setEnabled(False)
        self.history_audio_slider.setStyleSheet(
            "QSlider::groove:horizontal{height:6px;background:#181825;border-radius:3px;}"
            "QSlider::handle:horizontal{background:#89b4fa;border:1px solid #b4befe;width:14px;margin:-5px 0;border-radius:7px;}"
            "QSlider::sub-page:horizontal{background:#2563eb;border-radius:3px;}"
        )
        hp_layout.addWidget(self.history_audio_slider)

        time_row = QHBoxLayout()
        self.history_cur_time_lbl = QLabel("00:00")
        self.history_cur_time_lbl.setStyleSheet("color:#a6adc8;")
        self.history_total_time_lbl = QLabel("00:00")
        self.history_total_time_lbl.setStyleSheet("color:#a6adc8;")
        self.history_cur_time_lbl.hide()
        self.history_total_time_lbl.hide()
        time_row.addWidget(self.history_cur_time_lbl)
        time_row.addStretch()
        time_row.addWidget(self.history_total_time_lbl)
        hp_layout.addLayout(time_row)

        player_btn_row = QHBoxLayout()
        self.history_player_play_btn = QPushButton("▶️ Play")
        self.history_player_play_btn.setProperty("class", "secondary-button")
        self.history_player_pause_btn = QPushButton("⏸️ Pause")
        self.history_player_pause_btn.setProperty("class", "secondary-button")
        self.history_player_stop_btn = QPushButton("⏹️ Stop")
        self.history_player_stop_btn.setProperty("class", "secondary-button")
        player_btn_row.addWidget(self.history_player_play_btn)
        player_btn_row.addWidget(self.history_player_pause_btn)
        player_btn_row.addWidget(self.history_player_stop_btn)
        player_btn_row.addStretch()
        hp_layout.addLayout(player_btn_row)

        # Add to right_layout in final logical order
        right_layout.addWidget(self.detail_header)
        right_layout.addLayout(vid_row)
        right_layout.addWidget(self.restitch_status)
        right_layout.addWidget(self.restitch_progress)
        right_layout.addWidget(QLabel("📄 Generated Script:"))
        right_layout.addWidget(self.script_view)
        right_layout.addWidget(QLabel("🎬 Scenes & Assets:"))
        right_layout.addLayout(scene_ctrl_row)
        right_layout.addWidget(self.scenes_scroll)
        right_layout.addWidget(self.history_player_frame)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([260, 780])
        root_layout.addWidget(splitter)
        self.setLayout(root_layout)

        self._topic_rows = []
        self._current_video_path = None
        self._current_scenes = []   # scene dicts built from DB for re-stitching
        self._current_topic = ""
        self._history_topic_folder = ""
        self._history_scene_refs = []
        self._history_scene_audio_controls = {}
        self._history_scene_audio_paths = {}
        self._audio_player = QMediaPlayer(self)
        self._audio_player_scene_idx = None
        self._audio_slider_dragging = False
        self._audio_player.positionChanged.connect(self._on_history_audio_position_changed)
        self._audio_player.durationChanged.connect(self._on_history_audio_duration_changed)
        self._audio_player.stateChanged.connect(self._on_history_audio_state_changed)
        self.history_audio_slider.sliderPressed.connect(self._on_history_audio_slider_pressed)
        self.history_audio_slider.sliderReleased.connect(self._on_history_audio_slider_released)
        self.history_audio_slider.sliderMoved.connect(self._on_history_audio_slider_moved)
        self.history_player_play_btn.clicked.connect(self._on_history_player_play_clicked)
        self.history_player_pause_btn.clicked.connect(self._on_history_player_pause_clicked)
        self.history_player_stop_btn.clicked.connect(self._on_history_player_stop_clicked)
        self._reset_history_player_ui()
        self.history_generate_all_btn.setEnabled(False)
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
        self._current_topic = topic
        self.detail_header.setText(f"🎬 {topic}  ({duration} min)  ·  {created_at[:16]}")
        self.script_view.setPlainText(script_text or "")

        # Build scene dicts usable by VideoStitchingThread
        self._current_scenes = [
            {
                'visual': visual,
                'narration': narration,
                'img_path': img_path or '',
                'audio_path': audio_path or '',
                'db_id': sid,
            }
            for (sid, order, visual, narration, img_path, audio_path) in scenes
        ]

        # Check for a final video in the topic sub-folder
        safe_topic = make_safe_topic(topic)
        video_path = os.path.join(os.path.dirname(__file__), 'assets', safe_topic, f"{safe_topic}.mp4")
        if os.path.exists(video_path):
            self._current_video_path = video_path
            self.video_lbl.setText(f"✅ {os.path.basename(video_path)}")
            self.video_lbl.setStyleSheet("color: #a6e3a1;")
            try:
                self.view_video_btn.clicked.disconnect()
                self.open_folder_btn.clicked.disconnect()
            except TypeError:
                pass
            self.view_video_btn.clicked.connect(
                lambda: os.startfile(os.path.abspath(video_path))
            )
            self.open_folder_btn.clicked.connect(
                lambda: os.startfile(os.path.abspath(os.path.dirname(video_path)))
            )
            self.view_video_btn.show()
            self.open_folder_btn.show()
            self.play_video_btn.hide()
        else:
            self._current_video_path = None
            self.video_lbl.setText("No final video yet.")
            self.video_lbl.setStyleSheet("color: #585b70;")
            self.play_video_btn.hide()
            self.view_video_btn.hide()
            self.open_folder_btn.hide()

        self._history_topic_folder = os.path.join(
            os.path.dirname(__file__), 'assets', make_safe_topic(topic)
        )
        os.makedirs(self._history_topic_folder, exist_ok=True)
        self._audio_player.stop()
        self._audio_player_scene_idx = None
        self._reset_history_player_ui()
        self.history_generate_all_btn.setEnabled(bool(self._current_scenes))
        self._update_restitch_button_visibility()
        self.restitch_status.hide()
        self.restitch_progress.hide()
        self._render_history_scene_cards()

    def _update_restitch_button_visibility(self):
        all_ready = all(
            s['img_path'] and os.path.exists(s['img_path']) and
            s['audio_path'] and os.path.exists(s['audio_path'])
            for s in self._current_scenes
        ) and bool(self._current_scenes)
        self.restitch_btn.setVisible(all_ready)

    def _render_history_scene_cards(self):
        while self.scenes_layout.count() > 1:
            item = self.scenes_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._history_scene_refs = []

        for i, scene in enumerate(self._current_scenes):
            card = QFrame()
            card.setStyleSheet("background-color: #2b2b36; border-radius: 6px; padding: 8px;")
            card_layout = QHBoxLayout(card)

            text_block = QVBoxLayout()
            lbl_title = QLabel(f"<b>Scene {i+1}</b>")
            lbl_visual = QLabel(f"<i>Visual:</i> {scene['visual']}")
            lbl_visual.setWordWrap(True)
            lbl_narr = QLabel(f"<i>Narration:</i> {scene['narration']}")
            lbl_narr.setWordWrap(True)
            text_block.addWidget(lbl_title)
            text_block.addWidget(lbl_visual)
            text_block.addWidget(lbl_narr)

            img_row = QHBoxLayout()
            gen_img_btn = QPushButton("🖼️ Generate Image")
            gen_img_btn.setProperty("class", "primary-button")
            view_img_btn = QPushButton("👁️ View Image")
            view_img_btn.setProperty("class", "secondary-button")
            view_img_btn.hide()
            img_status = QLabel("")
            img_status.setStyleSheet("color: #a6e3a1;")
            img_row.addWidget(gen_img_btn)
            img_row.addWidget(view_img_btn)
            img_row.addWidget(img_status)
            img_row.addStretch()
            text_block.addLayout(img_row)

            aud_row = QHBoxLayout()
            gen_aud_btn = QPushButton("🎙️ Generate Audio")
            gen_aud_btn.setProperty("class", "primary-button")
            play_aud_btn = QPushButton("🔊 Play Audio")
            play_aud_btn.setProperty("class", "secondary-button")
            pause_aud_btn = QPushButton("⏸️ Pause")
            pause_aud_btn.setProperty("class", "secondary-button")
            stop_aud_btn = QPushButton("⏹️ Stop")
            stop_aud_btn.setProperty("class", "secondary-button")
            play_aud_btn.hide()
            pause_aud_btn.hide()
            stop_aud_btn.hide()
            aud_status = QLabel("")
            aud_status.setStyleSheet("color: #a6e3a1;")
            aud_row.addWidget(gen_aud_btn)
            aud_row.addWidget(play_aud_btn)
            aud_row.addWidget(pause_aud_btn)
            aud_row.addWidget(stop_aud_btn)
            aud_row.addWidget(aud_status)
            aud_row.addStretch()
            text_block.addLayout(aud_row)

            thumb = QLabel()
            thumb.setFixedSize(142, 80)
            thumb.setAlignment(Qt.AlignCenter)
            if scene.get('img_path') and os.path.exists(scene['img_path']):
                pix = QPixmap(scene['img_path'])
                thumb.setPixmap(pix.scaled(142, 80, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
                view_img_btn.show()
                try:
                    view_img_btn.clicked.disconnect()
                except TypeError:
                    pass
                view_img_btn.clicked.connect(lambda _, p=scene['img_path']: self._show_history_image_preview(p))
            else:
                thumb.setText("No Image")
                thumb.setStyleSheet("background:#181825; color:#585b70; border-radius:4px;")

            if scene.get('audio_path') and os.path.exists(scene['audio_path']):
                self._history_scene_audio_paths[i] = scene['audio_path']
                play_aud_btn.show()
                pause_aud_btn.show()
                stop_aud_btn.show()
                try:
                    play_aud_btn.clicked.disconnect()
                except TypeError:
                    pass
                try:
                    pause_aud_btn.clicked.disconnect()
                except TypeError:
                    pass
                try:
                    stop_aud_btn.clicked.disconnect()
                except TypeError:
                    pass
                play_aud_btn.clicked.connect(
                    lambda _, p=scene['audio_path'], idx=i, lbl=aud_status, pb=pause_aud_btn:
                    self._play_history_audio(p, idx, lbl, pb)
                )
                pause_aud_btn.clicked.connect(
                    lambda _, idx=i, lbl=aud_status, pb=pause_aud_btn:
                    self._pause_resume_history_audio(idx, lbl, pb)
                )
                stop_aud_btn.clicked.connect(
                    lambda _, idx=i, lbl=aud_status, pb=pause_aud_btn:
                    self._stop_history_audio(idx, lbl, pb)
                )

            def create_img_handler(idx=i, s=scene, status_lbl=img_status, btn=gen_img_btn, preview_lbl=thumb, view_btn=view_img_btn):
                out_path = os.path.join(self._history_topic_folder, f"scene_{idx+1}.jpg")
                btn.setEnabled(False)
                status_lbl.setText("⏳ Generating Image...")
                status_lbl.setStyleSheet("color: #f9e2af;")
                thread = ImageGenerationThread(s.get('visual', ''), out_path)
                setattr(self, f"history_img_thread_{idx}", thread)

                def on_finish(success, path):
                    btn.setEnabled(True)
                    if success:
                        status_lbl.setText("✅ Image Generated!")
                        status_lbl.setStyleSheet("color: #a6e3a1;")
                        s['img_path'] = path
                        db.update_scene_asset(s.get('db_id'), 'img_path', path)
                        pix = QPixmap(path)
                        preview_lbl.setPixmap(pix.scaled(142, 80, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
                        view_btn.show()
                        try:
                            view_btn.clicked.disconnect()
                        except TypeError:
                            pass
                        view_btn.clicked.connect(lambda _, p=path: self._show_history_image_preview(p))
                    else:
                        status_lbl.setText("❌ Image Failed.")
                        status_lbl.setStyleSheet("color: #f38ba8;")
                    self._update_restitch_button_visibility()

                thread.finished.connect(on_finish)
                thread.start()

            def create_aud_handler(
                idx=i, s=scene, status_lbl=aud_status, btn=gen_aud_btn,
                play_btn=play_aud_btn, pause_btn=pause_aud_btn, stop_btn=stop_aud_btn
            ):
                engine = "gemini" if "Gemini" in self.history_tts_engine_combo.currentText() else "google"
                audio_ext = "wav" if engine == "gemini" else "mp3"
                out_path = os.path.join(self._history_topic_folder, f"scene_{idx+1}.{audio_ext}")
                btn.setEnabled(False)
                status_lbl.setText("⏳ Generating Audio...")
                status_lbl.setStyleSheet("color: #f9e2af;")
                thread = AudioGenerationThread(s.get('narration', ''), out_path, engine)
                setattr(self, f"history_aud_thread_{idx}", thread)

                def on_finish(success, path):
                    btn.setEnabled(True)
                    if success:
                        status_lbl.setText("✅ Audio Saved.")
                        status_lbl.setStyleSheet("color: #a6e3a1;")
                        s['audio_path'] = path
                        self._history_scene_audio_paths[idx] = path
                        db.update_scene_asset(s.get('db_id'), 'audio_path', path)
                        play_btn.show()
                        pause_btn.show()
                        stop_btn.show()
                        try:
                            play_btn.clicked.disconnect()
                        except TypeError:
                            pass
                        try:
                            pause_btn.clicked.disconnect()
                        except TypeError:
                            pass
                        try:
                            stop_btn.clicked.disconnect()
                        except TypeError:
                            pass
                        play_btn.clicked.connect(
                            lambda _, p=path, scene_idx=idx, lbl=status_lbl, pb=pause_btn:
                            self._play_history_audio(p, scene_idx, lbl, pb)
                        )
                        pause_btn.clicked.connect(
                            lambda _, scene_idx=idx, lbl=status_lbl, pb=pause_btn:
                            self._pause_resume_history_audio(scene_idx, lbl, pb)
                        )
                        stop_btn.clicked.connect(
                            lambda _, scene_idx=idx, lbl=status_lbl, pb=pause_btn:
                            self._stop_history_audio(scene_idx, lbl, pb)
                        )
                    else:
                        status_lbl.setText("❌ Audio Failed.")
                        status_lbl.setStyleSheet("color: #f38ba8;")
                    self._update_restitch_button_visibility()

                thread.finished.connect(on_finish)
                thread.start()

            gen_img_btn.clicked.connect(create_img_handler)
            gen_aud_btn.clicked.connect(create_aud_handler)

            card_layout.addLayout(text_block, stretch=3)
            card_layout.addWidget(thumb)
            self.scenes_layout.insertWidget(self.scenes_layout.count() - 1, card)
            self._history_scene_refs.append((scene, img_status, aud_status, thumb, view_img_btn, play_aud_btn, pause_aud_btn, stop_aud_btn))
            self._history_scene_audio_controls[i] = (aud_status, pause_aud_btn)

    def _on_history_generate_all(self):
        if not self._current_scenes:
            return

        self.history_generate_all_btn.setEnabled(False)
        self.history_generate_all_btn.setText("⏳ Generating...")
        self.history_stop_btn.show()
        self.history_stop_btn.setEnabled(True)
        self.history_stop_btn.setText("⏹️ Stop")

        engine = "gemini" if "Gemini" in self.history_tts_engine_combo.currentText() else "google"
        status_map = {idx: refs for idx, refs in enumerate(self._history_scene_refs)}

        self._history_bulk_thread = BulkGenerationThread(self._current_scenes, self._history_topic_folder, engine)

        try:
            self.history_stop_btn.clicked.disconnect()
        except TypeError:
            pass

        def stop_generation():
            self._history_bulk_thread.cancel()
            self.history_stop_btn.setEnabled(False)
            self.history_stop_btn.setText("⏳ Stopping...")

        self.history_stop_btn.clicked.connect(stop_generation)

        def on_scene_progress(idx, asset_type, msg):
            if idx not in status_map:
                return
            scene, lbl_img, lbl_aud, thumb, view_btn, play_btn, pause_btn, stop_btn = status_map[idx]
            msg_l = (msg or "").lower()
            is_done = "done" in msg_l
            is_in_progress = "generating" in msg_l
            if asset_type == 'img':
                lbl_img.setText(msg)
                lbl_img.setStyleSheet("color: #a6e3a1;" if is_done else ("color: #f9e2af;" if is_in_progress else "color: #f38ba8;"))
                if is_done and scene.get('img_path') and os.path.exists(scene['img_path']):
                    pix = QPixmap(scene['img_path'])
                    thumb.setPixmap(pix.scaled(142, 80, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
                    view_btn.show()
                    try:
                        view_btn.clicked.disconnect()
                    except TypeError:
                        pass
                    view_btn.clicked.connect(lambda _, p=scene['img_path']: self._show_history_image_preview(p))
            else:
                lbl_aud.setText(msg)
                lbl_aud.setStyleSheet("color: #a6e3a1;" if is_done else ("color: #f9e2af;" if is_in_progress else "color: #f38ba8;"))
                if is_done and scene.get('audio_path') and os.path.exists(scene['audio_path']):
                    self._history_scene_audio_paths[idx] = scene['audio_path']
                    play_btn.show()
                    pause_btn.show()
                    stop_btn.show()
                    try:
                        play_btn.clicked.disconnect()
                    except TypeError:
                        pass
                    try:
                        pause_btn.clicked.disconnect()
                    except TypeError:
                        pass
                    try:
                        stop_btn.clicked.disconnect()
                    except TypeError:
                        pass
                    play_btn.clicked.connect(
                        lambda _, p=scene['audio_path'], scene_idx=idx, lbl=lbl_aud, pb=pause_btn:
                        self._play_history_audio(p, scene_idx, lbl, pb)
                    )
                    pause_btn.clicked.connect(
                        lambda _, scene_idx=idx, lbl=lbl_aud, pb=pause_btn:
                        self._pause_resume_history_audio(scene_idx, lbl, pb)
                    )
                    stop_btn.clicked.connect(
                        lambda _, scene_idx=idx, lbl=lbl_aud, pb=pause_btn:
                        self._stop_history_audio(scene_idx, lbl, pb)
                    )

        def on_all_done(all_ok, msg):
            self.history_generate_all_btn.setEnabled(True)
            self.history_generate_all_btn.setText("⚡ Auto-Generate All Scenes")
            self.history_stop_btn.hide()
            self._update_restitch_button_visibility()
            if not all_ok:
                QMessageBox.warning(self, "Generation Issues/Stopped", msg)

        self._history_bulk_thread.scene_progress.connect(on_scene_progress)
        self._history_bulk_thread.all_done.connect(on_all_done)
        self._history_bulk_thread.start()

    def _play_video(self):
        return

    def _format_ms(self, ms: int) -> str:
        total_sec = max(0, int(ms // 1000))
        mins = total_sec // 60
        secs = total_sec % 60
        return f"{mins:02d}:{secs:02d}"

    def _reset_history_player_ui(self):
        self.history_now_playing_lbl.setText("Now Playing: None")
        self.history_audio_state_lbl.setText("Stopped")
        self.history_cur_time_lbl.setText("00:00")
        self.history_total_time_lbl.setText("00:00")
        self.history_audio_slider.setRange(0, 0)
        self.history_audio_slider.setValue(0)
        self.history_audio_slider.setEnabled(False)
        self.history_player_pause_btn.setText("Pause")

    def _on_history_audio_position_changed(self, pos: int):
        if self._audio_slider_dragging:
            return
        self.history_audio_slider.setValue(pos)
        self.history_cur_time_lbl.setText(self._format_ms(pos))

    def _on_history_audio_duration_changed(self, dur: int):
        self.history_audio_slider.setRange(0, max(0, dur))
        self.history_audio_slider.setEnabled(dur > 0)
        self.history_total_time_lbl.setText(self._format_ms(dur))

    def _on_history_audio_state_changed(self, state):
        if state == QMediaPlayer.PlayingState:
            self.history_audio_state_lbl.setText("Playing")
            self.history_player_pause_btn.setText("Pause")
        elif state == QMediaPlayer.PausedState:
            self.history_audio_state_lbl.setText("Paused")
            self.history_player_pause_btn.setText("Resume")
        else:
            self.history_audio_state_lbl.setText("Stopped")
            self.history_player_pause_btn.setText("Pause")

    def _on_history_audio_slider_pressed(self):
        self._audio_slider_dragging = True

    def _on_history_audio_slider_moved(self, val: int):
        self.history_cur_time_lbl.setText(self._format_ms(val))

    def _on_history_audio_slider_released(self):
        self._audio_slider_dragging = False
        self._audio_player.setPosition(self.history_audio_slider.value())

    def _on_history_player_play_clicked(self):
        if self._audio_player_scene_idx is None:
            if not self._history_scene_audio_paths:
                return
            first_idx = sorted(self._history_scene_audio_paths.keys())[0]
            ctrl = self._history_scene_audio_controls.get(first_idx)
            if ctrl:
                lbl, pause_btn = ctrl
                self._play_history_audio(self._history_scene_audio_paths[first_idx], first_idx, lbl, pause_btn)
            return

        if self._audio_player.state() == QMediaPlayer.PausedState:
            self._audio_player.play()
            return

        ctrl = self._history_scene_audio_controls.get(self._audio_player_scene_idx)
        path = self._history_scene_audio_paths.get(self._audio_player_scene_idx, "")
        if ctrl and path:
            lbl, pause_btn = ctrl
            self._play_history_audio(path, self._audio_player_scene_idx, lbl, pause_btn)

    def _on_history_player_pause_clicked(self):
        if self._audio_player_scene_idx is None:
            return
        ctrl = self._history_scene_audio_controls.get(self._audio_player_scene_idx)
        if not ctrl:
            return
        lbl, pause_btn = ctrl
        self._pause_resume_history_audio(self._audio_player_scene_idx, lbl, pause_btn)

    def _on_history_player_stop_clicked(self):
        if self._audio_player_scene_idx is None:
            return
        ctrl = self._history_scene_audio_controls.get(self._audio_player_scene_idx)
        if not ctrl:
            return
        lbl, pause_btn = ctrl
        self._stop_history_audio(self._audio_player_scene_idx, lbl, pause_btn)

    def _show_history_image_preview(self, path: str):
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "Image Missing", "Image file not found.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Image Preview")
        dlg.resize(960, 540)
        layout = QVBoxLayout(dlg)
        img_lbl = QLabel()
        img_lbl.setAlignment(Qt.AlignCenter)
        img_lbl.setStyleSheet("background:#11111b; border-radius:6px;")
        pix = QPixmap(path)
        img_lbl.setPixmap(pix.scaled(920, 500, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(img_lbl)
        dlg.exec_()
    def _play_history_audio(self, path: str, scene_idx: int, status_label: QLabel, pause_btn: QPushButton):
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "Audio Missing", "Audio file not found.")
            return
        self._audio_player.stop()
        self._audio_player.setMedia(QMediaContent(QUrl.fromLocalFile(os.path.abspath(path))))
        self._audio_player_scene_idx = scene_idx
        self._audio_player.play()
        self.history_now_playing_lbl.setText(f"Now Playing: Scene {scene_idx + 1}")
        status_label.setText("Playing")
        status_label.setStyleSheet("color: #a6e3a1;")
        pause_btn.setText("Pause")

    def _pause_resume_history_audio(self, scene_idx: int, status_label: QLabel, pause_btn: QPushButton):
        if self._audio_player_scene_idx != scene_idx:
            status_label.setText("Play this scene first")
            status_label.setStyleSheet("color: #f9e2af;")
            return
        state = self._audio_player.state()
        if state == QMediaPlayer.PlayingState:
            self._audio_player.pause()
            status_label.setText("Paused")
            status_label.setStyleSheet("color: #f9e2af;")
            pause_btn.setText("Resume")
        elif state == QMediaPlayer.PausedState:
            self._audio_player.play()
            status_label.setText("Playing")
            status_label.setStyleSheet("color: #a6e3a1;")
            pause_btn.setText("Pause")

    def _stop_history_audio(self, scene_idx: int, status_label: QLabel, pause_btn: QPushButton):
        if self._audio_player_scene_idx != scene_idx:
            status_label.setText("Stopped")
            status_label.setStyleSheet("color: #cdd6f4;")
            return
        self._audio_player.stop()
        self._audio_player_scene_idx = None
        self._reset_history_player_ui()
        status_label.setText("Stopped")
        status_label.setStyleSheet("color: #cdd6f4;")
        pause_btn.setText("Pause")

    def _on_restitch_clicked(self):
        if not self._current_scenes:
            return
        missing = [
            f"Scene {i+1}: {'Image' if not (s['img_path'] and os.path.exists(s['img_path'])) else 'Audio'} missing"
            for i, s in enumerate(self._current_scenes)
            if not (s['img_path'] and os.path.exists(s['img_path']))
            or not (s['audio_path'] and os.path.exists(s['audio_path']))
        ]
        if missing:
            QMessageBox.warning(self, "Assets Missing",
                "Cannot re-stitch — some assets are missing:\n\n" + "\n".join(missing))
            return
        self.restitch_requested.emit(self._current_scenes, self._current_topic)

    def update_restitch_progress(self, pct: int, msg: str):
        self.restitch_status.setText(msg)
        self.restitch_status.setStyleSheet("color:#f9e2af;")
        self.restitch_status.show()
        self.restitch_progress.setValue(pct)
        self.restitch_progress.show()
        self.restitch_btn.setEnabled(False)
        self.restitch_btn.setText("⏳ Stitching...")

    def finish_restitch(self, success: bool, result_msg: str):
        self.restitch_progress.hide()
        self.restitch_btn.setEnabled(True)
        self.restitch_btn.setText("🔁 Re-Stitch Video")
        if success:
            self.restitch_status.setText(f"✅ Done! Saved to: {os.path.basename(result_msg)}")
            self.restitch_status.setStyleSheet("color:#a6e3a1;")
            self._current_video_path = result_msg
            self.video_lbl.setText(f"✅ {os.path.basename(result_msg)}")
            self.video_lbl.setStyleSheet("color:#a6e3a1;")
            try:
                self.view_video_btn.clicked.disconnect()
                self.open_folder_btn.clicked.disconnect()
            except TypeError:
                pass
            self.view_video_btn.clicked.connect(
                lambda: os.startfile(os.path.abspath(result_msg))
            )
            self.open_folder_btn.clicked.connect(
                lambda: os.startfile(os.path.abspath(os.path.dirname(result_msg)))
            )
            self.view_video_btn.show()
            self.open_folder_btn.show()
            self.play_video_btn.hide()
        else:
            self.restitch_status.setText(f"❌ Failed: {result_msg[:120]}")
            self.restitch_status.setStyleSheet("color:#f38ba8;")


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
        # Re-stitching from history
        self.history_tab.restitch_requested.connect(self.on_history_restitch)

        # System tray for "done" notifications (works even when app is in background)
        self._tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self)
            # Use the window icon or a blank pixmap as tray icon
            self._tray.setIcon(self.style().standardIcon(self.style().SP_ComputerIcon))
            self._tray.setToolTip("Infographic Video Generator")
            self._tray.show()

    def on_script_next(self, scenes):
        self.current_scenes = scenes
        self.storyboard_tab.load_scenes(self.current_scenes, topic=self.current_topic)
        self.tabs.setCurrentIndex(1)
        self.export_tab.reset_ui()
        self.export_tab.populate_thumbnails(self.current_scenes)
        
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
            
        self.export_tab.start_render_ui()
        self.export_tab.set_progress(0, "Preparing clips…")

        # Video goes into the same topic sub-folder
        safe_topic = make_safe_topic(self.current_topic)
        topic_folder = os.path.join(os.path.dirname(__file__), 'assets', safe_topic)
        os.makedirs(topic_folder, exist_ok=True)
        out_path = os.path.join(topic_folder, f"{safe_topic}.mp4")

        self.stitch_thread = VideoStitchingThread(self.current_scenes, out_path)
        self.stitch_thread.progress_msg.connect(
            lambda msg: self.export_tab.status_lbl.setText(msg)
        )
        self.stitch_thread.progress_pct.connect(
            lambda pct: self.export_tab.set_progress(pct, self.export_tab.status_lbl.text())
        )
        self.stitch_thread.finished.connect(self.on_stitch_finished)
        self.stitch_thread.start()
        
    def on_stitch_finished(self, success, result_msg):
        self.export_tab.stop_render_ui(success, result_msg)
        
        # Desktop notification — works even if the user has switched tabs
        if hasattr(self, '_tray') and self._tray:
            if success:
                self._tray.showMessage(
                    "Video Ready! 🎬",
                    f"{os.path.basename(result_msg)} has been saved.",
                    QSystemTrayIcon.Information, 5000
                )
                # Refresh thumbnails with any newly generated images
                self.export_tab.populate_thumbnails(self.current_scenes)
            else:
                self._tray.showMessage(
                    "Stitching Failed ❌",
                    result_msg[:120],
                    QSystemTrayIcon.Critical, 5000
                )

    def on_history_restitch(self, scenes, topic):
        """Triggered by History tab Re-Stitch button — runs stitch in background, reports back inline."""
        safe_topic = make_safe_topic(topic)
        topic_folder = os.path.join(os.path.dirname(__file__), 'assets', safe_topic)
        os.makedirs(topic_folder, exist_ok=True)
        out_path = os.path.join(topic_folder, f"{safe_topic}.mp4")

        self._history_stitch_thread = VideoStitchingThread(scenes, out_path)
        self._history_stitch_thread.progress_msg.connect(
            lambda msg: self.history_tab.update_restitch_progress(
                self.history_tab.restitch_progress.value(), msg
            )
        )
        self._history_stitch_thread.progress_pct.connect(
            lambda pct: self.history_tab.update_restitch_progress(pct, self.history_tab.restitch_status.text())
        )
        self._history_stitch_thread.finished.connect(self.on_history_restitch_done)
        self._history_stitch_thread.start()

        if self._tray:
            self._tray.showMessage("Re-Stitching Started ⏳",
                f"Building video for “{topic}” in the background.",
                QSystemTrayIcon.Information, 3000)

    def on_history_restitch_done(self, success, result_msg):
        self.history_tab.finish_restitch(success, result_msg)
        if self._tray:
            if success:
                self._tray.showMessage("Re-Stitch Done! 🎬",
                    f"{os.path.basename(result_msg)} saved.",
                    QSystemTrayIcon.Information, 5000)
            else:
                self._tray.showMessage("Re-Stitch Failed ❌",
                    result_msg[:120], QSystemTrayIcon.Critical, 5000)

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

