import sys
import os
import re
import ctypes
import subprocess
import tempfile
import shutil
from dotenv import load_dotenv

def _get_app_data_base() -> str:
    if getattr(sys, 'frozen', False):
        base = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')),
                            'InfographicVideoGenerator')
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(base, exist_ok=True)
    return base

_ENV_PATH = os.path.join(_get_app_data_base(), ".env")

# On first frozen run, copy bundled .env into writable location if it doesn't exist yet
if getattr(sys, 'frozen', False):
    _bundled_env = os.path.join(sys._MEIPASS, ".env.example")
    if os.path.exists(_bundled_env) and not os.path.exists(_ENV_PATH):
        import shutil as _sh
        _sh.copy2(_bundled_env, _ENV_PATH)

load_dotenv(_ENV_PATH, override=True)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QLabel,
    QTextEdit, QPushButton, QHBoxLayout, QLineEdit, QMessageBox, QFrame,
    QScrollArea, QProgressBar, QSpinBox, QSplitter, QListWidget, QListWidgetItem,
    QTextBrowser, QCheckBox, QGridLayout, QSystemTrayIcon, QComboBox, QDialog, QSlider,
    QFileDialog
)
from PyQt5.QtGui import QPixmap, QIcon, QPainter, QPen, QColor, QFont
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer, QUrl, QRectF, QEvent
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
from ai_generator import (
    correct_topic_title,
    generate_script_from_topic,
    analyze_text_to_scenes,
    generate_image_from_prompt,
    generate_audio_from_text,
    ensure_word_timing_data,
    logger,
)
import db
import moviepy.editor as mp
import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS

_GEMINI_VOICE_MAP = {
    "puck": "puck",
    "charon": "charon",
    "fenrir": "fenrir",
    "kore": "kore",
    "aoede": "aoede",
    "leda": "leda",
}

def _load_env():
    from dotenv import load_dotenv as _load
    _load(_ENV_PATH, override=True)

def _save_env_key(key: str, value: str):
    lines = []
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, "r") as f:
            lines = f.readlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}\n")
    with open(_ENV_PATH, "w") as f:
        f.writelines(lines)
    os.environ[key] = value

def parse_tts_selection(combo_text: str):
    import re as _re
    m = _re.search(r"—\s+(\w+)\s+\(", combo_text)
    voice = m.group(1).lower() if m else "kore"
    return "gemini", voice

def _app_data_dir() -> str:
    return _get_app_data_base()

def _get_video_output_folder() -> str:
    """Returns the saved video output folder, or '' if not set / no longer exists."""
    folder = os.getenv("VIDEO_OUTPUT_FOLDER", "").strip()
    return folder if folder and os.path.isdir(folder) else ""

def make_safe_topic(topic: str) -> str:
    """Returns a filesystem-safe folder/file name derived from the topic."""
    safe = re.sub(r'[\\/:*?"<>|]', '_', topic or 'infographic').strip()
    return safe[:80]


def make_scene_overlay_text(narration: str, visual: str = "") -> str:
    """Create a short in-image label that represents the visual concept."""
    visual_text = re.sub(r"\s+", " ", (visual or "")).strip()
    narration_text = re.sub(r"\s+", " ", (narration or "")).strip()
    visual_lower = visual_text.lower()
    small_words = {"and", "or", "of", "the", "a", "an", "to", "in", "on", "for", "with"}

    def _title_case(text: str) -> str:
        words = text.split()
        titled = []
        for idx, word in enumerate(words):
            low = word.lower()
            titled.append(low if idx > 0 and low in small_words else low.capitalize())
        return " ".join(titled)

    def _compress(text: str, max_words: int = 4) -> str:
        text = re.sub(r"\s+", " ", text).strip(' "\'').strip(".,;:-")
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words])
        return _title_case(text[:40].strip(' "\''))

    def _from_visual(text: str) -> str:
        cleaned = re.sub(
            r"^(show|create|depict|illustrate|image of|scene of|visual of|picture of|portrait of)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"^(a|an|the)\s+((bold|cinematic|dramatic|vivid|vibrant|professional|warm|glowing|eye-catching|high-contrast|stylised|story-driven|detailed)\s+){0,4}",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        parts = re.split(r"(?<=[.!?])\s+|\s+[—-]\s+|;\s+|:\s+", cleaned)
        headline = next((part.strip() for part in parts if part.strip()), cleaned)
        headline = re.split(
            r"\b(with|against|on|in|under|over|inside|outside|surrounded by|set against|featuring|showing)\b",
            headline,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        return _compress(headline)

    if any(marker in visual_lower for marker in ("subscribe button", "notification bell", "call to action")):
        return "Like and Subscribe"

    title_match = re.search(r'topic title\s+"([^"]+)"', visual_text, re.IGNORECASE)
    if title_match:
        return _title_case(title_match.group(1).strip()[:40])

    if visual_text:
        visual_headline = _from_visual(visual_text)
        if visual_headline:
            return visual_headline

    if narration_text:
        return _compress(narration_text)

    return ""

class ImageGenerationThread(QThread):
    generation_done = pyqtSignal(bool, str)

    def __init__(self, prompt, output_path, overlay_text="", narration="", is_title_card=False, mobile_friendly=True):
        super().__init__()
        self.prompt = prompt
        self.output_path = output_path
        self.overlay_text = overlay_text
        self.narration = narration
        self.is_title_card = is_title_card
        self.mobile_friendly = mobile_friendly

    def run(self):
        success = generate_image_from_prompt(
            self.prompt,
            self.output_path,
            self.overlay_text,
            self.narration,
            self.is_title_card,
            self.mobile_friendly,
        )
        self.generation_done.emit(success, self.output_path)

class AudioGenerationThread(QThread):
    generation_done = pyqtSignal(bool, str, str)

    def __init__(self, text, output_path, engine="gemini", voice=None):
        super().__init__()
        self.text = text
        self.output_path = output_path
        self.engine = engine
        self.voice = voice

    def run(self):
        import traceback as _tb
        try:
            success = generate_audio_from_text(self.text, self.output_path, self.engine, self.voice)
            self.generation_done.emit(success, self.output_path, "" if success else "generate_audio_from_text returned False")
        except Exception as e:
            logger.exception("AudioGenerationThread error: %s", e)
            self.generation_done.emit(False, self.output_path, str(e))

_BULK_WORKERS = 3  # Max concurrent scene workers. Keep ≤3 to stay within Gemini free-tier rate limits.

class BulkGenerationThread(QThread):
    """
    Processes scenes concurrently (up to _BULK_WORKERS at a time): image then audio per scene.
    Saves all assets into assets/{topic_folder}/scene_X.{ext}
    """
    scene_progress = pyqtSignal(int, str, str)
    all_done = pyqtSignal(bool, str)

    def __init__(self, scenes, topic_folder, tts_engine="gemini", tts_voice=None, skip_existing=False, topic="", mobile_friendly=True):
        super().__init__()
        self.scenes = scenes
        self.topic_folder = topic_folder  # absolute path to topic sub-folder
        self.tts_engine = tts_engine
        self.tts_voice = tts_voice
        self.skip_existing = skip_existing
        self.topic = topic
        self.mobile_friendly = mobile_friendly
        self.is_cancelled = False

    def cancel(self):
        self.is_cancelled = True

    def run(self):
        try:
            self._run_inner()
        except BaseException as e:
            logger.exception("BulkGenerationThread unhandled error: %s", e)
            self.all_done.emit(False, f"Generation error: {type(e).__name__}: {e}")

    def _run_inner(self):
        import threading as _threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        os.makedirs(self.topic_folder, exist_ok=True)

        total = len(self.scenes)
        workers = min(_BULK_WORKERS, total)
        failed = []
        lock = _threading.Lock()
        counters = {'img': 0, 'aud': 0}

        logger.info("BulkGeneration started: %d scenes, workers=%d, engine=%s, voice=%s, skip_existing=%s",
                    total, workers, self.tts_engine, self.tts_voice, self.skip_existing)

        def process_scene(i, scene):
            # Each worker handles one scene: image first, then audio (sequential within the scene).
            if self.is_cancelled:
                return

            idx = i + 1
            # Use db_id as filename so each generation session gets unique files,
            # even across re-runs of the same topic. Falls back to positional index.
            file_key = scene.get('db_id') or idx

            # --- Resolve paths ---
            img_path = os.path.join(self.topic_folder, f"scene_{file_key}.jpg")
            existing_img = scene.get('img_path', '')
            img_exists = os.path.exists(img_path) or (existing_img and os.path.exists(existing_img))

            audio_ext = "wav" if self.tts_engine == "gemini" else "mp3"
            aud_path = os.path.join(self.topic_folder, f"scene_{file_key}.{audio_ext}")
            alt_ext = "mp3" if audio_ext == "wav" else "wav"
            alt_aud_path = os.path.join(self.topic_folder, f"scene_{file_key}.{alt_ext}")
            existing_aud = scene.get('audio_path', '')
            aud_exists = (
                os.path.exists(aud_path) or
                os.path.exists(alt_aud_path) or
                (existing_aud and os.path.exists(existing_aud))
            )

            skip_img = self.skip_existing and img_exists
            skip_aud = self.skip_existing and aud_exists

            logger.info("--- Scene %d/%d (worker) ---", idx, total)

            # --- Image ---
            if skip_img:
                scene['img_path'] = img_path if os.path.exists(img_path) else existing_img
                logger.info("Scene %d: image skipped (already exists).", idx)
                self.scene_progress.emit(i, 'img', '⏭️ Image skipped (exists)')
            else:
                self.scene_progress.emit(i, 'img', '⏳ Generating image...')
                # For the first scene (title card), always use the topic title as overlay text
                is_title_card = (i == 0)
                overlay = (
                    self.topic
                    if is_title_card and self.topic
                    else make_scene_overlay_text(scene.get('narration', ''), scene.get('visual', ''))
                )
                try:
                    img_ok = generate_image_from_prompt(
                        scene.get('visual', ''),
                        img_path,
                        overlay,
                        scene.get('narration', ''),
                        is_title_card,
                        self.mobile_friendly,
                    )
                except BaseException as e:
                    logger.exception("Scene %d image generation raised: %s", idx, e)
                    img_ok = False
                if img_ok:
                    with lock:
                        counters['img'] += 1
                        done = counters['img']
                    scene['img_path'] = img_path
                    logger.info("Scene %d: image OK (%d/%d done).", idx, done, total)
                    try:
                        db.update_scene_asset(scene.get('db_id'), 'img_path', img_path)
                    except Exception:
                        pass
                    self.scene_progress.emit(i, 'img', f'✅ Image done ({done}/{total})')
                else:
                    logger.warning("Scene %d: image FAILED.", idx)
                    with lock:
                        failed.append(f"Scene {idx} Image")
                    self.scene_progress.emit(i, 'img', '❌ Image failed')

            if self.is_cancelled:
                return

            # --- Audio ---
            if skip_aud:
                if os.path.exists(aud_path):
                    scene['audio_path'] = aud_path
                elif os.path.exists(alt_aud_path):
                    scene['audio_path'] = alt_aud_path
                else:
                    scene['audio_path'] = existing_aud
                logger.info("Scene %d: audio skipped (already exists).", idx)
                self.scene_progress.emit(i, 'aud', '⏭️ Audio skipped (exists)')
            else:
                self.scene_progress.emit(i, 'aud', '⏳ Generating audio...')
                try:
                    aud_ok = generate_audio_from_text(
                        scene.get('narration', ''), aud_path, self.tts_engine, self.tts_voice
                    )
                except BaseException as e:
                    logger.exception("Scene %d audio generation raised: %s", idx, e)
                    aud_ok = False
                if aud_ok:
                    with lock:
                        counters['aud'] += 1
                        done = counters['aud']
                    scene['audio_path'] = aud_path
                    logger.info("Scene %d: audio OK (%d/%d done).", idx, done, total)
                    try:
                        db.update_scene_asset(scene.get('db_id'), 'audio_path', aud_path)
                        db.update_scene_asset(scene.get('db_id'), 'tts_voice', self.tts_voice or '')
                    except Exception:
                        pass
                    self.scene_progress.emit(i, 'aud', f'✅ Audio done ({done}/{total})')
                else:
                    logger.warning("Scene %d: audio FAILED.", idx)
                    with lock:
                        failed.append(f"Scene {idx} Audio")
                    self.scene_progress.emit(i, 'aud', '❌ Audio failed')

        # Submit all scenes to the thread pool. Workers are capped at _BULK_WORKERS so at most
        # that many scenes run simultaneously. Each worker does image → audio in sequence,
        # keeping peak concurrent API calls at _BULK_WORKERS (not 2×).
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for i, scene in enumerate(self.scenes):
                if self.is_cancelled:
                    break
                futures[executor.submit(process_scene, i, scene)] = i

            for future in as_completed(futures):
                if self.is_cancelled:
                    break
                try:
                    future.result()
                except Exception as e:
                    logger.exception("Scene %d worker raised unexpected error: %s",
                                     futures[future] + 1, e)
        # executor.__exit__ waits for any still-running workers to finish naturally.

        if self.is_cancelled:
            self.all_done.emit(False, "Generation was stopped by the user.")
        elif failed:
            logger.warning("BulkGeneration finished with %d failure(s): %s", len(failed), failed)
            self.all_done.emit(False, f"Completed with failures: {', '.join(failed)}")
        else:
            logger.info("BulkGeneration complete: all %d scenes generated successfully.", total)
            self.all_done.emit(True, f"All {total} scenes generated successfully!")

class VideoStitchingThread(QThread):
    progress_msg = pyqtSignal(str)
    progress_pct = pyqtSignal(int)      # 0..100
    finished = pyqtSignal(bool, str)

    PAUSE_DURATION = 1.5  # seconds of freeze between scenes
    FPS = 24
    OUTPUT_W = 1920
    OUTPUT_H = 1080
    SAMPLE_RATE = 44100  # normalise all audio to this rate
    MAX_ZOOM = 1.08

    def __init__(self, scenes, output_path, mobile_friendly=True):
        super().__init__()
        self.scenes = scenes
        self.output_path = output_path
        self.mobile_friendly = mobile_friendly
        if mobile_friendly:
            self._out_w, self._out_h = 1080, 1920
        else:
            self._out_w, self._out_h = self.OUTPUT_W, self.OUTPUT_H

    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _escape_drawtext_value(text: str) -> str:
        """Escape characters that are special inside ffmpeg drawtext option values."""
        return (
            text
            .replace("\\", "\\\\")
            .replace("'",  "\u2019")   # curly apostrophe avoids quote issues
            .replace(":",  "\\:")
            .replace(",",  "\\,")
            .replace("%",  "%%")
            .replace("\n", r"\n")
        )

    @staticmethod
    def _escape_drawtext_path(path: str) -> str:
        """Escape a filesystem path for ffmpeg drawtext textfile/fontfile options."""
        return (
            path.replace("\\", "/")
            .replace(":", "\\:")
            .replace("'", r"\'")
        )

    @staticmethod
    def _find_subtitle_font() -> str:
        """Prefer a bold Windows font so subtitles render larger and heavier."""
        candidates = [
            os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arialbd.ttf"),
            os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeuib.ttf"),
            os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "calibrib.ttf"),
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return ""

    _SUBTITLE_MAX_WORDS = 12
    _BREAK_WORDS = {
        'and', 'but', 'or', 'so', 'because', 'when', 'while', 'after',
        'before', 'although', 'though', 'if', 'that', 'which', 'who',
        'where', 'then', 'yet', 'as', 'since', 'until', 'unless',
    }

    @staticmethod
    def _sentence_word_spans(narration: str, max_w: int = None) -> list:
        if max_w is None:
            max_w = VideoStitchingThread._SUBTITLE_MAX_WORDS
        break_words = VideoStitchingThread._BREAK_WORDS
        sentence_re = re.compile(r'[^.!?,;]+[.!?,;]*', re.UNICODE)
        word_re = re.compile(r"[A-Za-z0-9]+(?:[\x27'][A-Za-z0-9]+)*", re.UNICODE)
        spans = []
        for sent_m in sentence_re.finditer(narration):
            sent_text = sent_m.group()
            sent_start = sent_m.start()
            words = list(word_re.finditer(sent_text))
            if not words:
                continue
            if len(words) <= max_w:
                spans.append((sent_start + words[0].start(), sent_start + words[-1].end()))
                continue
            remaining = list(range(len(words)))
            while remaining:
                if len(remaining) <= max_w:
                    part = [words[i] for i in remaining]
                    spans.append((sent_start + part[0].start(), sent_start + part[-1].end()))
                    break
                split_at = None
                for i in range(min(max_w - 1, len(remaining) - 1), max_w // 2, -1):
                    if words[remaining[i]].group().lower() in break_words:
                        split_at = i
                        break
                if split_at is None:
                    split_at = max_w
                part = [words[i] for i in remaining[:split_at]]
                spans.append((sent_start + part[0].start(), sent_start + part[-1].end()))
                remaining = remaining[split_at:]
        return spans

    @staticmethod
    def _fallback_subtitle_chunks(narration: str, duration: float, max_w: int = None) -> list:
        narration = (narration or "").strip()
        if not narration or duration <= 0:
            return []
        spans = VideoStitchingThread._sentence_word_spans(narration, max_w)
        if not spans:
            return [{"text": narration, "start_sec": 0.0, "end_sec": duration}]
        texts = [narration[s:e].strip() for s, e in spans if narration[s:e].strip()]
        if not texts:
            return [{"text": narration, "start_sec": 0.0, "end_sec": duration}]
        step = duration / len(texts)
        return VideoStitchingThread._fix_quote_marks([
            {
                "text": text,
                "start_sec": step * idx,
                "end_sec": duration if idx == len(texts) - 1 else step * (idx + 1),
            }
            for idx, text in enumerate(texts)
        ])

    @staticmethod
    def _subtitle_chunks(narration: str, word_timings: list, duration: float, max_w: int = None) -> list:
        narration = narration or ""
        if not narration.strip():
            return []
        if not word_timings:
            return VideoStitchingThread._fallback_subtitle_chunks(narration, duration, max_w)
        word_re = re.compile(r"[A-Za-z0-9]+(?:[\x27’][A-Za-z0-9]+)*", re.UNICODE)
        word_matches = list(word_re.finditer(narration))
        spans = VideoStitchingThread._sentence_word_spans(narration, max_w)
        if not spans:
            return VideoStitchingThread._fallback_subtitle_chunks(narration, duration, max_w)
        if len(word_matches) != len(word_timings):
            chunks = []
            for span_idx, (s, e) in enumerate(spans):
                span_text = narration[s:e].strip()
                if not span_text:
                    continue
                span_wm = list(word_re.finditer(narration, s, e))
                if not span_wm:
                    continue
                first_pos = sum(1 for m in word_matches if m.start() < span_wm[0].start())
                first_pos = min(first_pos, len(word_timings) - 1)
                start_sec = max(0.0, float(word_timings[first_pos].get("start_sec", 0.0)))
                if span_idx + 1 < len(spans):
                    ns, _ = spans[span_idx + 1]
                    nwm = list(word_re.finditer(narration, ns))
                    if nwm:
                        nwp = min(sum(1 for m in word_matches if m.start() < nwm[0].start()), len(word_timings) - 1)
                        end_sec = max(0.0, float(word_timings[nwp].get("start_sec", duration)))
                    else:
                        end_sec = duration
                else:
                    end_sec = duration
                chunks.append({"text": span_text, "start_sec": start_sec, "end_sec": end_sec})
            return chunks or VideoStitchingThread._fallback_subtitle_chunks(narration, duration, max_w)
        chunks = []
        for span_idx, (s, e) in enumerate(spans):
            span_text = narration[s:e].strip()
            if not span_text:
                continue
            span_word_indices = [j for j, m in enumerate(word_matches) if m.start() >= s and m.end() <= e + 1]
            if not span_word_indices:
                continue
            first_idx = span_word_indices[0]
            start_sec = max(0.0, float(word_timings[first_idx].get("start_sec", 0.0)))
            if span_idx + 1 < len(spans):
                ns, _ = spans[span_idx + 1]
                next_indices = [j for j, m in enumerate(word_matches) if m.start() >= ns]
                end_sec = (
                    max(0.0, float(word_timings[next_indices[0]].get("start_sec", duration)))
                    if next_indices else duration
                )
            else:
                end_sec = duration
            chunks.append({"text": span_text, "start_sec": start_sec, "end_sec": end_sec})
        return VideoStitchingThread._fix_quote_marks(
            chunks or VideoStitchingThread._fallback_subtitle_chunks(narration, duration, max_w)
        )

    @staticmethod
    def _fix_quote_marks(chunks: list) -> list:
        in_quote = False
        for chunk in chunks:
            text = chunk["text"]
            count = text.count('"')
            if in_quote and not text.startswith('"'):
                text = '"' + text
            if in_quote:
                count = text.count('"')
            if count % 2 != 0:
                if in_quote:
                    text = text + '"'
                    in_quote = False
                else:
                    text = text + '"'
                    in_quote = True
            else:
                in_quote = False
            chunk["text"] = text
        return chunks

    @classmethod
    def _subtitle_vf(cls, narration: str, word_timings: list, duration: float, subtitle_dir: str, scene_idx: int, out_w: int = 1920) -> str:
        """
        Build chained ffmpeg drawtext filters from aligned word timings.
        Each subtitle group shows at most 10 words and advances using the
        actual spoken-word timing data for that scene.
        """
        if not narration.strip() or duration <= 0:
            return ""

        is_portrait = out_w <= 1080
        fontsize = 30 if is_portrait else 42
        max_words = 5 if is_portrait else None
        y_offset = 180 if is_portrait else 80

        os.makedirs(subtitle_dir, exist_ok=True)
        chunks = cls._subtitle_chunks(narration, word_timings, duration, max_words)
        if not chunks:
            chunks = cls._fallback_subtitle_chunks(narration, duration, max_words)
        if not chunks:
            return ""
        font_path = cls._find_subtitle_font()
        filters = []
        for idx, chunk in enumerate(chunks):
            chunk_text = chunk.get("text", "").strip()
            if not chunk_text:
                continue
            start_time = max(0.0, float(chunk.get("start_sec", 0.0)))
            end_time = duration if idx == len(chunks) - 1 else max(start_time, float(chunk.get("end_sec", duration)))
            enable_expr = (
                f"gte(t,{start_time:.4f})"
                if idx == len(chunks) - 1
                else f"gte(t,{start_time:.4f})*lt(t,{end_time:.4f})"
            )

            subtitle_path = os.path.join(subtitle_dir, f"scene_{scene_idx + 1:03d}_{idx:03d}.txt")
            with open(subtitle_path, "w", encoding="utf-8") as fh:
                fh.write(chunk_text)

            drawtext_parts = [
                "drawtext=",
                f"textfile='{cls._escape_drawtext_path(subtitle_path)}':",
            ]
            if font_path:
                drawtext_parts.append(f"fontfile='{cls._escape_drawtext_path(font_path)}':")
            filters.append(
                "".join(drawtext_parts)
                + f"fontsize={fontsize}:"
                f"fontcolor=white:"
                f"x=(w-text_w)/2:"
                f"y=h-text_h-{y_offset}:"
                f"line_spacing=8:"
                f"borderw=3:"
                f"bordercolor=0x000000E0:"
                f"box=1:"
                f"boxcolor=0x000000C8:"
                f"boxborderw=28:"
                f"fix_bounds=1:"
                f"enable='{cls._escape_drawtext_value(enable_expr)}'"
            )

        return ",".join(filters)

    def _find_ffmpeg(self) -> str:
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"

    def _scene_motion_filters(self, zoom_duration: float, hold_duration: float = 0.0) -> list:
        """
        Build a smoother center-zoom using crop+scale.
        Hold the final zoom level steady before cutting to the next scene.
        This avoids the jittery stepping produced by the previous zoompan filter.
        """
        safe_duration = max(zoom_duration, 0.001)
        zoom_delta = self.MAX_ZOOM - 1.0
        if hold_duration > 0:
            zoom_expr = (
                f"if(lt(t\\,{safe_duration:.4f}),"
                f"1+{zoom_delta:.4f}*(t/{safe_duration:.4f}),"
                f"{self.MAX_ZOOM:.4f})"
            )
        else:
            zoom_expr = f"1+{zoom_delta:.4f}*(t/{safe_duration:.4f})"
        is_portrait = self._out_w < self._out_h
        if is_portrait:
            base = [
                f"scale={self._out_w}:-2",
                f"pad={self._out_w}:max({self._out_h}\\,ih):0:(oh-ih)/2",
                f"crop={self._out_w}:{self._out_h}:0:(ih-{self._out_h})/2:exact=1",
            ]
        else:
            base = [
                f"scale={self._out_w}:{self._out_h}:force_original_aspect_ratio=decrease",
                f"pad={self._out_w}:{self._out_h}:(ow-iw)/2:(oh-ih)/2",
            ]
        return base + [
            f"scale=w='iw*({zoom_expr})':h='ih*({zoom_expr})':eval=frame",
            f"crop={self._out_w}:{self._out_h}:(iw-{self._out_w})/2:(ih-{self._out_h})/2:exact=1",
            "setsar=1",
        ]

    def _freeze_motion_filters(self) -> list:
        is_portrait = self._out_w < self._out_h
        if is_portrait:
            base = [
                f"scale={self._out_w}:-2",
                f"pad={self._out_w}:max({self._out_h}\\,ih):0:(oh-ih)/2",
                f"crop={self._out_w}:{self._out_h}:0:(ih-{self._out_h})/2:exact=1",
            ]
        else:
            base = [
                f"scale={self._out_w}:{self._out_h}:force_original_aspect_ratio=decrease",
                f"pad={self._out_w}:{self._out_h}:(ow-iw)/2:(oh-ih)/2",
            ]
        return base + [
            f"scale={int(round(self._out_w * self.MAX_ZOOM))}:{int(round(self._out_h * self.MAX_ZOOM))}",
            f"crop={self._out_w}:{self._out_h}:(iw-{self._out_w})/2:(ih-{self._out_h})/2:exact=1",
            "setsar=1",
        ]

    def _probe_image_dimensions(self, ffmpeg_path: str, img_path: str) -> tuple:
        """Return (width, height) of an image file via ffprobe. Falls back to OUTPUT_W/H."""
        ffprobe = os.path.join(
            os.path.dirname(ffmpeg_path),
            "ffprobe" + (".exe" if os.name == "nt" else ""),
        )
        if not os.path.exists(ffprobe):
            ffprobe = "ffprobe"
        try:
            _no_win = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=s=x:p=0", img_path],
                capture_output=True, text=True, timeout=10,
                **_no_win,
            )
            parts = r.stdout.strip().split("x")
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return self.OUTPUT_W, self.OUTPUT_H

    def _probe_duration(self, ffmpeg_path: str, media_path: str) -> float:
        """Return media duration in seconds via ffprobe, falling back to moviepy."""
        ffprobe = os.path.join(
            os.path.dirname(ffmpeg_path),
            "ffprobe" + (".exe" if os.name == "nt" else ""),
        )
        if not os.path.exists(ffprobe):
            ffprobe = "ffprobe"
        try:
            _no_win = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            r = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", media_path],
                capture_output=True, text=True, timeout=15,
                **_no_win,
            )
            return float(r.stdout.strip())
        except Exception:
            clip = mp.AudioFileClip(media_path)
            dur = clip.duration
            clip.close()
            return dur

    # ── main entry point ───────────────────────────────────────────────────────

    def run(self):
        temp_dir = tempfile.mkdtemp(prefix="vgen_")
        try:
            self._encode(temp_dir)
        except Exception as e:
            self.finished.emit(False, str(e))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # ── real-time ffmpeg runner ────────────────────────────────────────────────

    def _run_ffmpeg(
        self,
        cmd: list,
        total_frames: int,
        pct_start: int,
        pct_end: int,
        label: str,
    ) -> tuple:
        """
        Run an ffmpeg command and stream real-time frame progress.
        Injects -progress pipe:1 before the output path (last arg).
        Returns (success: bool, error_text: str).
        """
        import threading

        # Inject progress reporting flags just before the output path
        progress_cmd = (
            cmd[:-1]
            + ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]
            + [cmd[-1]]
        )

        _no_win = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        proc = subprocess.Popen(
            progress_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            **_no_win,
        )

        # Drain stderr in background so it never blocks stdout reads
        stderr_buf = []
        def _drain(pipe):
            for line in pipe:
                stderr_buf.append(line)
        drain_thread = threading.Thread(target=_drain, args=(proc.stderr,), daemon=True)
        drain_thread.start()

        # Read ffmpeg's structured progress from stdout
        frame = 0
        for raw in proc.stdout:
            line = raw.strip()
            if line.startswith("frame="):
                try:
                    frame = int(line.split("=", 1)[1])
                except ValueError:
                    pass
                if total_frames > 0:
                    ratio = min(frame / total_frames, 1.0)
                    pct = pct_start + int(ratio * (pct_end - pct_start))
                    self.progress_pct.emit(pct)
                    self.progress_msg.emit(
                        f"{label}  {int(ratio * 100)}%  ({frame}/{total_frames} frames)"
                    )

        proc.wait()
        drain_thread.join(timeout=5)
        err = "".join(stderr_buf)
        if proc.returncode != 0:
            return False, err[-800:]
        return True, ""

    # ── encode pipeline ────────────────────────────────────────────────────────

    def _encode(self, temp_dir: str):
        total = len(self.scenes)
        ffmpeg = self._find_ffmpeg()
        threads = str(os.cpu_count() or 4)
        fade_d = 0.5  # seconds of fade-in/out baked into every clip


        clip_paths: list = []
        subtitle_dir = os.path.join(temp_dir, "subtitles")

        # Progress: 0-90% for per-scene ffmpeg calls, 90-100% for assembly.
        total_calls = max(total, 1)

        def call_pct(call_idx):
            s = int(call_idx / total_calls * 90)
            e = int((call_idx + 1) / total_calls * 90)
            return s, e

        # ── Step 1: encode each scene clip + freeze, fades baked in ───────────
        for i, scene in enumerate(self.scenes):
            img_path = scene.get("img_path")
            aud_path = scene.get("audio_path")

            if not img_path or not os.path.exists(img_path):
                self.finished.emit(False, f"Missing image for scene {i+1}.")
                return
            if not aud_path or not os.path.exists(aud_path):
                self.finished.emit(False, f"Missing audio for scene {i+1}.")
                return

            dur = self._probe_duration(ffmpeg, aud_path)
            total_dur = dur + self.PAUSE_DURATION
            frames = max(1, int(round(total_dur * self.FPS)))

            # ?? scene clip: zoom, hold final frame, subtitle, fade in/out ??
            narration = scene.get("narration", "")
            word_timings = []
            if narration.strip():
                self.progress_msg.emit(f"Scene {i+1}/{total} - aligning subtitles...")
                try:
                    word_timings = ensure_word_timing_data(narration, aud_path)
                except Exception as e:
                    self.finished.emit(False, f"Scene {i+1} subtitle alignment failed:\n{e}")
                    return
                if word_timings is None:
                    self.finished.emit(False, f"Scene {i+1} subtitle alignment failed.")
                    return

            subtitle_vf = self._subtitle_vf(narration, word_timings, dur, subtitle_dir, i, out_w=self._out_w)
            scene_filters = self._scene_motion_filters(dur, self.PAUSE_DURATION)
            if subtitle_vf:
                scene_filters.append(subtitle_vf)
            scene_filters.append(f"fade=t=in:st=0:d={fade_d}:color=black")
            pause_fade_start = max(0.0, total_dur - fade_d)
            scene_filters.append(f"fade=t=out:st={pause_fade_start:.4f}:d={fade_d}:color=black")
            vf_scene = ",".join(scene_filters)
            af_scene = f"apad=pad_dur={self.PAUSE_DURATION:.4f},afade=t=in:st=0:d={fade_d}"
            clip_path = os.path.join(temp_dir, f"s{i:03d}.mp4")
            ps, pe = call_pct(i)
            ok, err = self._run_ffmpeg([
                ffmpeg, "-y",
                "-loop", "1", "-framerate", str(self.FPS), "-i", img_path,
                "-i", aud_path,
                "-vf", vf_scene,
                "-af", af_scene,
                "-c:v", "libx264", "-preset", "fast", "-tune", "stillimage", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ar", str(self.SAMPLE_RATE),
                "-t", f"{total_dur:.4f}",
                "-pix_fmt", "yuv420p",
                "-threads", threads,
                clip_path,
            ], frames, ps, pe, f"Scene {i+1}/{total} ? encoding")
            if not ok:
                self.finished.emit(False, f"Scene {i+1} encode failed:\n{err}")
                return
            clip_paths.append(clip_path)

        # ── Step 2: assemble with concat demuxer + stream copy ─────────────────
        # All clips share the same codec/resolution/fps/sample-rate so -c copy is safe.
        # Stream copy never re-encodes → structurally valid output, no corruption.
        self.progress_msg.emit("Assembling final video...")
        self.progress_pct.emit(92)

        concat_list = os.path.join(temp_dir, "playlist.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for p in clip_paths:
                f.write(f"file '{p.replace(chr(92), '/')}'\n")

        _no_win = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

        # Check whether we can embed the title card as the video's cover art/thumbnail
        thumbnail_img = self.scenes[0].get("img_path") if self.scenes else None
        has_thumbnail = bool(thumbnail_img and os.path.exists(thumbnail_img))

        if has_thumbnail:
            # Assemble to a temp file first, then re-run with thumbnail attached
            temp_assembled = self.output_path + ".tmp_assembled.mp4"
            r = subprocess.run([
                ffmpeg, "-y",
                "-f", "concat", "-safe", "0", "-i", concat_list,
                "-c", "copy",
                "-movflags", "+faststart",
                temp_assembled,
            ], capture_output=True, **_no_win)

            if r.returncode != 0:
                self.finished.emit(False,
                    f"Assembly failed:\n{r.stderr.decode(errors='replace')[-1000:]}")
                return

            # Embed the title card image as the MP4 cover art so it shows as the thumbnail
            r2 = subprocess.run([
                ffmpeg, "-y",
                "-i", temp_assembled,
                "-i", thumbnail_img,
                "-map", "0",
                "-map", "1",
                "-c", "copy",
                "-c:v:1", "mjpeg",
                "-disposition:v:1", "attached_pic",
                "-movflags", "+faststart",
                self.output_path,
            ], capture_output=True, **_no_win)

            if r2.returncode != 0:
                logger.warning("Thumbnail embedding failed (non-fatal): %s",
                               r2.stderr.decode(errors='replace')[-200:])
                # Fall back: use the assembled video without embedded thumbnail
                try:
                    os.replace(temp_assembled, self.output_path)
                except OSError:
                    pass
            else:
                try:
                    os.remove(temp_assembled)
                except OSError:
                    pass
        else:
            r = subprocess.run([
                ffmpeg, "-y",
                "-f", "concat", "-safe", "0", "-i", concat_list,
                "-c", "copy",
                "-movflags", "+faststart",
                self.output_path,
            ], capture_output=True, **_no_win)

            if r.returncode != 0:
                self.finished.emit(False,
                    f"Assembly failed:\n{r.stderr.decode(errors='replace')[-1000:]}")
                return

        self.progress_pct.emit(100)
        self.finished.emit(True, self.output_path)

class ScriptGenerationThread(QThread):
    chunk_received = pyqtSignal(str)
    finished = pyqtSignal()
    
    def __init__(self, topic, duration, use_web_search=False, part_info=None, ignore_number=False):
        super().__init__()
        self.topic = topic
        self.duration = duration
        self.use_web_search = use_web_search
        self.part_info = part_info
        self.ignore_number = ignore_number

    def run(self):
        for chunk in generate_script_from_topic(self.topic, self.duration, self.use_web_search, self.part_info, self.ignore_number):
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

class TopicCorrectionThread(QThread):
    """Runs correct_topic_title() off the GUI thread so the UI stays responsive."""
    correction_done = pyqtSignal(str)  # emits corrected title (or original on failure)

    def __init__(self, topic: str):
        super().__init__()
        self.topic = topic

    def run(self):
        corrected = correct_topic_title(self.topic)
        self.correction_done.emit(corrected)


class SpinnerOverlay(QWidget):
    """
    Animated arc-spinner overlay rendered on top of a parent widget.
    Call start() to show and stop() to hide.
    """
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(12)          # ~83 fps → smooth arc rotation
        self._timer.timeout.connect(self._tick)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(parent.rect())
        self.hide()

    def start(self):
        self._angle = 0
        self.setGeometry(self.parent().rect())
        self.show()
        self.raise_()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 3) % 360
        self.update()

    def paintEvent(self, event):          # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Semi-transparent dark backdrop
        p.fillRect(self.rect(), QColor(24, 24, 37, 215))

        cx, cy = self.width() // 2, self.height() // 2
        r = max(36, min(self.height() // 6, 60))
        offset_y = 20                         # shift arc slightly above center

        arc_rect = QRectF(cx - r, cy - r - offset_y, r * 2, r * 2)

        # Dim track ring
        track = QPen(QColor(49, 50, 68))
        track.setWidth(8)
        track.setCapStyle(Qt.RoundCap)
        p.setPen(track)
        p.drawArc(arc_rect, 0, 360 * 16)

        # Bright spinning arc
        arc = QPen(QColor(137, 180, 250))     # #89b4fa – accent blue
        arc.setWidth(8)
        arc.setCapStyle(Qt.RoundCap)
        p.setPen(arc)
        p.drawArc(arc_rect, (-self._angle) * 16, 260 * 16)

        # Label below the arc
        p.setPen(QColor(166, 173, 200))       # #a6adc8
        f = QFont("Segoe UI", 13)
        p.setFont(f)
        label_rect = QRectF(0, cy + r - offset_y + 14, self.width(), 28)
        p.drawText(label_rect, Qt.AlignHCenter | Qt.AlignVCenter, "Generating script…")

        p.end()


class ScriptTab(QWidget):
    next_requested = pyqtSignal(list)
    next_requested_topic = pyqtSignal(str)
    next_requested_part_label = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._part_info = None   # set when generating parts
        self._part_label = ''    # e.g. "Part 1 of 7"
        root = QVBoxLayout()
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(0)

        self._script_tabs = QTabWidget()
        self._script_tabs.setStyleSheet("QTabBar::tab { min-width: 160px; }")

        draft_widget = QWidget()
        draft_layout = QVBoxLayout(draft_widget)
        draft_layout.setSpacing(15)
        draft_layout.setContentsMargins(0, 12, 0, 0)

        topic_layout = QHBoxLayout()
        self.topic_input = QLineEdit()
        self.topic_input.setPlaceholderText("Enter a topic (e.g., 'How photosynthesis works')")
        self.topic_input.setMinimumHeight(38)

        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 60)
        self.duration_spin.setValue(5)
        self.duration_spin.setSuffix(" min")
        self.duration_spin.setFixedSize(78, 28)

        self.generate_btn = QPushButton("✨ Generate Script")
        self.generate_btn.setProperty("class", "primary-button")
        self.generate_btn.clicked.connect(self.generate_script)
        self.topic_input.returnPressed.connect(self.generate_script)

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
        draft_layout.addLayout(topic_layout)

        lbl = QLabel("Draft or Edit Your Script:")
        lbl.setProperty("class", "h2")
        draft_layout.addWidget(lbl)

        self.script_editor = QTextEdit()
        self.script_editor.setAcceptRichText(False)
        self.script_editor.setPlaceholderText("Enter the infographic script. Break it down into logical scenes.\\nExample:\\n[Scene 1]\\nVisual: A sun shining on a leaf.\\nNarration: Photosynthesis starts here.")
        self._spinner = SpinnerOverlay(self.script_editor)
        self._waiting_for_first_chunk = False
        self.script_editor.installEventFilter(self)
        draft_layout.addWidget(self.script_editor)

        btn_layout = QHBoxLayout()
        self.enhance_btn = QPushButton("🪄 Enhance Script")
        self.enhance_btn.setProperty("class", "secondary-button")
        self.split_btn = QPushButton("✂️ Split into Scenes")
        self.split_btn.setProperty("class", "primary-button")
        self.split_btn.clicked.connect(self.parse_and_go_next)
        btn_layout.addWidget(self.enhance_btn)
        btn_layout.addWidget(self.split_btn)
        btn_layout.addStretch()
        self.next_btn = QPushButton("Next: Storyboard ➔")
        self.next_btn.setProperty("class", "success-button")
        self.next_btn.clicked.connect(self.parse_and_go_next)
        btn_layout.addWidget(self.next_btn)
        self._next_part_btn = QPushButton("▶ Generate Part 2")
        self._next_part_btn.setProperty("class", "success-button")
        self._next_part_btn.hide()
        self._next_part_btn.clicked.connect(self._generate_next_part)
        btn_layout.addWidget(self._next_part_btn)
        draft_layout.addLayout(btn_layout)

        self._script_tabs.addTab(draft_widget, "Generate with Gemini")

        analyze_widget = QWidget()
        analyze_layout = QVBoxLayout(analyze_widget)
        analyze_layout.setSpacing(12)
        analyze_layout.setContentsMargins(0, 12, 0, 0)

        analyze_title = QLabel("📝 Analyze & Convert Text to Scenes")
        analyze_title.setProperty("class", "h2")
        analyze_layout.addWidget(analyze_title)

        self.analyze_input = QTextEdit()
        self.analyze_input.setAcceptRichText(False)
        self.analyze_input.setPlaceholderText(
            "Paste any text here (article, blog post, notes, essay...)\n"
            "AI will analyze it and extract scenes with visual prompts and narration."
        )
        self.analyze_input.setMinimumHeight(150)
        analyze_layout.addWidget(self.analyze_input)

        analyze_btn_row = QHBoxLayout()
        self.analyze_btn = QPushButton("🔍 Analyze & Generate Scenes")
        self.analyze_btn.setProperty("class", "primary-button")
        self.analyze_btn.clicked.connect(self.analyze_text)
        self.analyze_status = QLabel("")
        self.analyze_status.setStyleSheet("color: #a6e3a1;")
        analyze_btn_row.addWidget(self.analyze_btn)
        analyze_btn_row.addWidget(self.analyze_status)
        analyze_btn_row.addStretch()
        analyze_layout.addLayout(analyze_btn_row)

        self._script_tabs.addTab(analyze_widget, "Paste Custom Script")

        root.addWidget(self._script_tabs)
        self.setLayout(root)

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
        db_topic = f"{topic} — {self._part_label}" if self._part_label else topic
        db.save_script_and_scenes(db_topic, duration, text, scenes)

        self.next_requested.emit(scenes)
        self.next_requested_topic.emit(db_topic)
        self.next_requested_part_label.emit(self._part_label)

    def generate_script(self):
        topic = self.topic_input.text().strip()
        duration = self.duration_spin.value()

        if not topic:
            QMessageBox.warning(self, "Input Error", "Please enter a topic first.")
            return

        self.generate_btn.setEnabled(False)
        self.generate_btn.setText("✏️ Correcting topic...")
        self.script_editor.clear()
        self._waiting_for_first_chunk = True
        self._spinner.start()

        # Step 1: correct the topic title via Gemini, then kick off script generation.
        self._correction_thread = TopicCorrectionThread(topic)
        self._correction_thread.correction_done.connect(self._on_topic_corrected)
        self._correction_thread.start()

    def _on_topic_corrected(self, corrected_topic: str):
        self.topic_input.setText(corrected_topic)
        duration = self.duration_spin.value()
        use_web = self.web_search_chk.isChecked()

        # Check if topic number overflows the duration
        overflow = self._detect_overflow(corrected_topic, duration)
        if overflow:
            self._show_overflow_dialog(corrected_topic, duration, use_web, overflow)
            return

        self._part_info = None
        self._part_label = ''
        self._start_generation(corrected_topic, duration, use_web)

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
        self._waiting_for_first_chunk = True
        self._spinner.start()
        
        self.analyze_thread = AnalyzeTextThread(source_text)
        self.analyze_thread.chunk_received.connect(self.on_chunk_received)
        self.analyze_thread.finished.connect(self.on_analyze_done)
        self.analyze_thread.start()

    def on_chunk_received(self, chunk):
        # Hide the spinner the moment the first character arrives
        if self._waiting_for_first_chunk:
            self._waiting_for_first_chunk = False
            self._spinner.stop()
        # Insert raw text continuously to the end
        cursor = self.script_editor.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(chunk)
        self.script_editor.setTextCursor(cursor)

    def on_script_generated(self):
        self._spinner.stop()
        self._waiting_for_first_chunk = False
        self.generate_btn.setEnabled(True)
        self.generate_btn.setText("✨ Generate Script")
        # Show "next part" button if there are more parts to generate
        if self._part_info and self._part_info['current'] < self._part_info['total']:
            next_part = self._part_info['current'] + 1
            self._next_part_btn.setText(f"▶ Generate Part {next_part} of {self._part_info['total']}")
            self._next_part_btn.show()
        else:
            self._next_part_btn.hide()

    def on_analyze_done(self):
        self._spinner.stop()
        self._waiting_for_first_chunk = False
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("🔍 Analyze & Generate Scenes")
        self.analyze_status.setText("✅ Done! Switch to Draft tab to review, then click Next.")
        self.analyze_status.setStyleSheet("color: #a6e3a1;")
        self._script_tabs.setCurrentIndex(0)

    def _detect_overflow(self, topic: str, duration: int):
        """
        Returns a dict if the topic's item count overflows the duration, else None.
        Dict keys: topic_number, items_per_part, total_parts, needed_duration
        """
        import math
        m = re.search(r'\b(\d+)\b', topic)
        if not m:
            return None
        n = int(m.group(1))
        if not (2 <= n <= 200):
            return None
        MIN_WORDS_PER_SCENE = 80
        content_words = max(1, duration * 140 - 25)  # minus short intro+CTA
        max_items = max(1, int(content_words / MIN_WORDS_PER_SCENE))
        if n <= max_items:
            return None
        items_per_part = max_items
        total_parts = math.ceil(n / items_per_part)
        needed_duration = math.ceil((n * MIN_WORDS_PER_SCENE + 25) / 140)
        return {
            'topic_number': n,
            'items_per_part': items_per_part,
            'total_parts': total_parts,
            'needed_duration': needed_duration,
        }

    def _show_overflow_dialog(self, topic: str, duration: int, use_web: bool, overflow: dict):
        import math
        n = overflow['topic_number']
        total_parts = overflow['total_parts']
        items_per_part = overflow['items_per_part']
        needed = overflow['needed_duration']

        dlg = QDialog(self)
        dlg.setWindowTitle("Content Overflow Detected")
        dlg.setMinimumWidth(480)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(14)

        info = QLabel(
            f"<b>\"{topic}\"</b> has <b>{n} items</b>, but a <b>{duration}-minute</b> video "
            f"can only fit about <b>{items_per_part} items</b> with meaningful narration.<br><br>"
            f"How would you like to proceed?"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        btn_extend = QPushButton(f"⏱️  Extend to {needed} min  (fit all {n} items in one video)")
        btn_extend.setProperty("class", "primary-button")

        btn_cram = QPushButton(f"📦  Cram into {duration} min  (AI will summarise & group items)")
        btn_cram.setProperty("class", "secondary-button")

        btn_parts = QPushButton(f"📚  Generate as {total_parts} parts  (~{items_per_part} items each, {duration} min/part)")
        btn_parts.setProperty("class", "success-button")

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setProperty("class", "secondary-button")

        layout.addWidget(btn_extend)
        layout.addWidget(btn_cram)
        layout.addWidget(btn_parts)
        layout.addWidget(btn_cancel)

        chosen = {'action': None}

        def pick(action):
            chosen['action'] = action
            dlg.accept()

        btn_extend.clicked.connect(lambda: pick('extend'))
        btn_cram.clicked.connect(lambda: pick('cram'))
        btn_parts.clicked.connect(lambda: pick('parts'))
        btn_cancel.clicked.connect(dlg.reject)

        if dlg.exec_() != QDialog.Accepted or chosen['action'] is None:
            # Cancelled — re-enable generate button
            self.generate_btn.setEnabled(True)
            self.generate_btn.setText("✨ Generate Script")
            self._spinner.stop()
            return

        action = chosen['action']

        if action == 'extend':
            self.duration_spin.setValue(needed)
            self._part_info = None
            self._part_label = ''
            self._start_generation(topic, needed, use_web)

        elif action == 'cram':
            self._part_info = None
            self._part_label = ''
            self._start_generation(topic, duration, use_web, ignore_number=True)

        else:  # parts
            self._part_info = {
                'current': 1,
                'total': total_parts,
                'items_per_part': items_per_part,
                'item_start': 1,
                'item_end': min(items_per_part, n),
                'topic_number': n,
                'base_topic': topic,
            }
            self._part_label = f"Part 1 of {total_parts}"
            self._start_generation(topic, duration, use_web, part_info=self._part_info)

    def _start_generation(self, topic: str, duration: int, use_web: bool, part_info=None, ignore_number=False):
        if use_web:
            self.generate_btn.setText("🌐 Searching & Generating...")
        else:
            self.generate_btn.setText("⏳ Generating...")
        self.thread = ScriptGenerationThread(topic, duration, use_web, part_info, ignore_number)
        self.thread.chunk_received.connect(self.on_chunk_received)
        self.thread.finished.connect(self.on_script_generated)
        self.thread.start()

    def _generate_next_part(self):
        if not self._part_info:
            return
        pi = self._part_info
        next_part = pi['current'] + 1
        if next_part > pi['total']:
            return
        item_start = (next_part - 1) * pi['items_per_part'] + 1
        item_end = min(next_part * pi['items_per_part'], pi['topic_number'])
        self._part_info = {
            'current': next_part,
            'total': pi['total'],
            'items_per_part': pi['items_per_part'],
            'item_start': item_start,
            'item_end': item_end,
            'topic_number': pi['topic_number'],
            'base_topic': pi['base_topic'],
        }
        self._part_label = f"Part {next_part} of {pi['total']}"
        self._next_part_btn.hide()
        self.script_editor.clear()
        self.generate_btn.setEnabled(False)
        self.generate_btn.setText("⏳ Generating...")
        self._waiting_for_first_chunk = True
        self._spinner.start()
        duration = self.duration_spin.value()
        use_web = self.web_search_chk.isChecked()
        self._start_generation(pi['base_topic'], duration, use_web, part_info=self._part_info)

    def eventFilter(self, obj, event):
        # Keep the spinner overlay filling the script editor as it resizes
        if obj is self.script_editor and event.type() == QEvent.Resize:
            self._spinner.setGeometry(self.script_editor.rect())
        return super().eventFilter(obj, event)

class StoryboardTab(QWidget):
    next_requested = pyqtSignal()
    back_requested = pyqtSignal()
    bulk_job_started = pyqtSignal(object, str, object)  # (thread, topic, scenes)

    def __init__(self):
        super().__init__()
        self._topic = ''
        self._part_label = ''
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)

        header_layout = QHBoxLayout()
        label = QLabel("Storyboard (Generate Image & TTS Audio)")
        label.setProperty("class", "h2")
        self.tts_engine_combo = QComboBox()
        self.tts_engine_combo.addItems([
            "Gemini — Puck (Male)",
            "Gemini — Charon (Male)",
            "Gemini — Fenrir (Male)",
            "Gemini — Kore (Female)",
            "Gemini — Aoede (Female)",
            "Gemini — Leda (Female)",
        ])
        self.tts_engine_combo.setCurrentText("Gemini — Kore (Female)")
        self.tts_engine_combo.setToolTip("Select the Text-to-Speech voice. Gemini voices are locked to a single gender per call.")
        
        self.generate_all_btn = QPushButton("⚡ Auto-Generate All Scenes")
        self.generate_all_btn.setProperty("class", "success-button")

        self.stop_btn = QPushButton("⏹️ Stop")
        self.stop_btn.setProperty("class", "danger-button")
        self.stop_btn.hide()

        self.mobile_friendly_chk = QCheckBox("Mobile Friendly")
        self.mobile_friendly_chk.setChecked(True)
        self.mobile_friendly_chk.setToolTip("Checked: 9:16 portrait aspect ratio (mobile). Unchecked: 16:9 landscape aspect ratio.")

        header_layout.addWidget(label)
        header_layout.addStretch()
        header_layout.addWidget(self.mobile_friendly_chk)
        header_layout.addWidget(QLabel("TTS Engine:"))
        header_layout.addWidget(self.tts_engine_combo)
        header_layout.addWidget(self.generate_all_btn)
        header_layout.addWidget(self.stop_btn)
        layout.addLayout(header_layout)

        self._preview_labels = {}  # id(QLabel) -> {'lbl': QLabel, 'has_image': bool}
        self.mobile_friendly_chk.stateChanged.connect(self._on_mobile_friendly_changed)

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
        self.back_btn.setProperty("class", "secondary-button")
        self.back_btn.clicked.connect(self.back_requested.emit)
        
        self.next_btn = QPushButton("Next: Export Video ➔")
        self.next_btn.setProperty("class", "success-button")
        self.next_btn.clicked.connect(self.next_requested.emit)
        
        nav_layout.addWidget(self.back_btn)
        nav_layout.addStretch()
        nav_layout.addWidget(self.next_btn)
        layout.addLayout(nav_layout)
        self.setLayout(layout)

    def load_scenes(self, scenes, topic='', part_label=''):
        self._topic = topic
        self._part_label = part_label
        self._topic_folder = os.path.join(
            _app_data_dir(), 'assets', make_safe_topic(topic)
        )
        os.makedirs(self._topic_folder, exist_ok=True)
                
        # Disconnect any old signal from previous renders
        try:
            self.generate_all_btn.clicked.disconnect()
        except TypeError:
            pass

        self._preview_labels = {}

        # Clear existing layout except the stretch
        while self.scroll_layout.count() > 1:
            item = self.scroll_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        scene_ui_refs = []  # list of (scene, status_lbl_img, status_lbl_aud, img_preview, view_img_btn, play_aud_btn)
        
        for i, scene in enumerate(scenes):
            card = QFrame()
            card.setStyleSheet("QFrame { background-color: #2b2b36; border-radius: 8px; padding: 10px; }")
            card_main_layout = QHBoxLayout() # Horizontal wrap to put image on the right
            
            # Left side: Text and Buttons
            left_layout = QVBoxLayout()
            
            header = QLabel(f"🎬 Scene {i+1}")
            header.setProperty("class", "h2")
            left_layout.addWidget(header)

            # Scene 1 (intro card): editable visual prompt so the user can tweak and regenerate
            _visual_edit = None
            if i == 0:
                visual_hdr = QLabel("<b>Visual Prompt (intro card — editable):</b>")
                visual_hdr.setWordWrap(True)
                left_layout.addWidget(visual_hdr)
                _visual_edit = QTextEdit()
                _visual_edit.setPlainText(scene['visual'])
                _visual_edit.setAcceptRichText(False)
                _visual_edit.setMaximumHeight(88)
                _visual_edit.setStyleSheet(
                    "QTextEdit{background:#1e1e2e;color:#cdd6f4;"
                    "border:1px solid #45475a;border-radius:4px;font-size:12px;padding:4px;}"
                )
                _visual_edit.textChanged.connect(
                    lambda s=scene, e=_visual_edit: s.update({'visual': e.toPlainText()})
                )
                left_layout.addWidget(_visual_edit)
            else:
                visual_lbl = QLabel(f"<b>Visual (Nano-Banana Prompt):</b><br>{scene['visual']}")
                visual_lbl.setWordWrap(True)
                left_layout.addWidget(visual_lbl)
            
            narration_lbl = QLabel(f"<b>Narration (TTS):</b><br>{scene['narration']}")
            narration_lbl.setWordWrap(True)
            left_layout.addWidget(narration_lbl)
            
            # Action Buttons & Status (Images)
            btn_layout_img = QHBoxLayout()
            _existing_img_check = scene.get('img_path', '')
            _img_exists = bool(_existing_img_check and os.path.exists(_existing_img_check))
            gen_img_btn = QPushButton("🖼️ Re-Generate Image" if _img_exists else "🖼️ Generate Image")
            gen_img_btn.setProperty("class", "scene-button")
            view_img_btn = QPushButton("👁 View Image")
            view_img_btn.setProperty("class", "scene-button")
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
            _existing_aud = scene.get('audio_path', '')
            _aud_exists = bool(_existing_aud and os.path.exists(_existing_aud))
            gen_aud_btn = QPushButton("🎙️ Re-Generate Audio" if _aud_exists else "🎙️ Generate Audio")
            gen_aud_btn.setProperty("class", "scene-button")
            play_aud_btn = QPushButton("▶ Play Audio")
            play_aud_btn.setProperty("class", "scene-button")
            play_aud_btn.hide()

            status_lbl_aud = QLabel("")
            status_lbl_aud.setStyleSheet("color: #a6e3a1;")
            if _aud_exists:
                play_aud_btn.show()

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
            _ph_w, _ph_h = (112, 200) if self.mobile_friendly_chk.isChecked() else (200, 112)
            img_preview.setFixedSize(_ph_w, _ph_h)
            if _img_exists:
                _pix = QPixmap(_existing_img_check)
                _scaled = _pix.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                img_preview.setFixedSize(_scaled.width(), _scaled.height())
                img_preview.setPixmap(_scaled)
                view_img_btn.show()
                view_img_btn.clicked.connect(lambda _, p=_existing_img_check: os.startfile(os.path.abspath(p)) if os.name == 'nt' else None)
            self._preview_labels[id(img_preview)] = {'lbl': img_preview, 'has_image': _img_exists}
            right_layout.addWidget(img_preview)
            audio_icon_lbl = QLabel("🔊 Audio Ready")
            audio_icon_lbl.setAlignment(Qt.AlignCenter)
            audio_icon_lbl.setStyleSheet("color: #a6e3a1; font-size: 11px;")
            audio_icon_lbl.setVisible(_aud_exists)
            right_layout.addWidget(audio_icon_lbl)
            
            card_main_layout.addLayout(left_layout, stretch=3)
            card_main_layout.addLayout(right_layout, stretch=1)
            
            # Capture the scope for the handlers
            _is_title_card_scene = (i == 0)
            if _is_title_card_scene and self._topic:
                _title_overlay = self._topic
                if self._part_label:
                    _title_overlay = f"{self._topic}\n{self._part_label}"
                _overlay = _title_overlay
            else:
                _overlay = make_scene_overlay_text(scene.get('narration', ''), scene.get('visual', ''))

            def create_img_handler(
                idx=i,
                prompt=scene['visual'],
                prompt_edit=_visual_edit,
                overlay_text=_overlay,
                narration=scene.get('narration', ''),
                lbl=status_lbl_img,
                btn=gen_img_btn,
                p_lbl=img_preview,
                view_btn=view_img_btn,
                tdir=self._topic_folder,
                title_card=_is_title_card_scene,
                _file_key=scene.get('db_id') or (i + 1),
                _mobile_chk=self.mobile_friendly_chk,
            ):
                os.makedirs(tdir, exist_ok=True)
                out_path = os.path.join(tdir, f"scene_{_file_key}.jpg")
                btn.setEnabled(False)
                lbl.setText("⏳ Generating Image...")
                lbl.setStyleSheet("color: #f9e2af;") # Yellow

                actual_prompt = prompt_edit.toPlainText().strip() if prompt_edit else prompt
                thread = ImageGenerationThread(actual_prompt, out_path, overlay_text, narration, title_card, _mobile_chk.isChecked())
                # Store reference so it is not garbage collected
                setattr(self, f"img_thread_{idx}", thread)
                
                def on_finish(success, path):
                    try:
                        btn.setEnabled(True)
                        if success:
                            lbl.setText("✅ Image Generated!")
                            lbl.setStyleSheet("color: #a6e3a1;")
                            btn.setText("🖼️ Re-Generate Image")
                            pixmap = QPixmap(path)
                            _scaled = pixmap.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                            p_lbl.setFixedSize(_scaled.width(), _scaled.height())
                            p_lbl.setPixmap(_scaled)
                            _key = id(p_lbl)
                            if _key in self._preview_labels:
                                self._preview_labels[_key]['has_image'] = True
                            scene['img_path'] = path
                            db.update_scene_asset(scene.get('db_id'), 'img_path', path)
                            view_btn.show()
                            view_btn.clicked.connect(lambda: os.startfile(os.path.abspath(path)) if os.name == 'nt' else None)
                        else:
                            lbl.setText("❌ Image Failed.")
                            lbl.setStyleSheet("color: #f38ba8;")
                    except RuntimeError:
                        pass

                thread.generation_done.connect(on_finish)
                thread.start()

            def create_aud_handler(idx=i, text=scene['narration'], lbl=status_lbl_aud, btn=gen_aud_btn, play_btn=play_aud_btn, tdir=self._topic_folder, aud_icon=audio_icon_lbl):
                os.makedirs(tdir, exist_ok=True)
                engine, voice = parse_tts_selection(self.tts_engine_combo.currentText())
                audio_ext = "wav" if engine == "gemini" else "mp3"
                out_path = os.path.join(tdir, f"scene_{idx+1}.{audio_ext}")
                btn.setEnabled(False)
                lbl.setText("⏳ Generating Audio...")
                lbl.setStyleSheet("color: #f9e2af;")

                thread = AudioGenerationThread(text, out_path, engine, voice)
                setattr(self, f"aud_thread_{idx}", thread)
                
                def on_finish(success, path, err_msg):
                    try:
                        btn.setEnabled(True)
                        if success:
                            lbl.setText("✅ Audio Saved.")
                            lbl.setStyleSheet("color: #a6e3a1;")
                            btn.setText("🎙️ Re-Generate Audio")
                            aud_icon.setVisible(True)
                            scene['audio_path'] = path
                            db.update_scene_asset(scene.get('db_id'), 'audio_path', path)
                            db.update_scene_asset(scene.get('db_id'), 'tts_voice', voice or '')
                            scene['tts_voice'] = voice or ''
                            play_btn.show()
                            play_btn.clicked.connect(lambda: os.startfile(os.path.abspath(path)) if os.name == 'nt' else None)
                        else:
                            lbl.setText(f"❌ Audio Failed: {err_msg}" if err_msg else "❌ Audio Failed.")
                            lbl.setStyleSheet("color: #f38ba8;")
                    except RuntimeError:
                        pass

                thread.generation_done.connect(on_finish)
                thread.start()
                
                
            gen_img_btn.clicked.connect(lambda _, f=create_img_handler: f())
            gen_aud_btn.clicked.connect(lambda _, f=create_aud_handler: f())
            
            # Register this card's UI refs so BulkGenerationThread can update them
            scene_ui_refs.append((scene, status_lbl_img, status_lbl_aud, img_preview, view_img_btn, play_aud_btn))
            
            card.setLayout(card_main_layout)
            # Insert before the stretch
            self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, card)

        def trigger_all():
            """Spawn a sequential BulkGenerationThread that processes each scene in order."""
            has_existing_assets = any(
                (sc.get('img_path') and os.path.exists(sc['img_path']))
                or (sc.get('audio_path') and os.path.exists(sc['audio_path']))
                for sc in scenes
            )
            skip_existing = False

            if has_existing_assets:
                # Ask whether to skip already-generated assets or redo everything
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle("Generate Assets")
                msg_box.setText("What would you like to generate?")
                msg_box.setInformativeText(
                    "• <b>Missing only</b> — skip scenes that already have an image and audio file.<br>"
                    "• <b>Regenerate all</b> — overwrite every image and audio, even if they exist."
                )
                btn_missing = msg_box.addButton("Missing only", QMessageBox.AcceptRole)
                btn_all     = msg_box.addButton("Regenerate all", QMessageBox.DestructiveRole)
                msg_box.addButton(QMessageBox.Cancel)
                msg_box.exec_()

                clicked = msg_box.clickedButton()
                if clicked is None or clicked == msg_box.button(QMessageBox.Cancel):
                    return
                skip_existing = (clicked == btn_missing)

            self.generate_all_btn.setEnabled(False)
            self.generate_all_btn.setText("⏳ Generating...")
            self.stop_btn.show()
            self.stop_btn.setEnabled(True)
            self.stop_btn.setText("⏹️ Stop")

            engine, voice = parse_tts_selection(self.tts_engine_combo.currentText())

            # Build a quick lookup from scene index to its UI labels
            status_map = {}  # index -> (img_lbl, aud_lbl, img_preview_lbl, view_img_btn, play_aud_btn)
            for j, (sc, lbl_img, lbl_aud, p_lbl, v_btn, pl_btn) in enumerate(scene_ui_refs):
                status_map[j] = (lbl_img, lbl_aud, p_lbl, v_btn, pl_btn)

            bulk_thread = BulkGenerationThread(scenes, self._topic_folder, engine, voice, skip_existing, self._topic, self.mobile_friendly_chk.isChecked())
            setattr(self, '_bulk_thread', bulk_thread)
            self.bulk_job_started.emit(bulk_thread, self._topic, scenes)

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
                total_steps = max(1, len(scenes) * 2)
                step_idx = idx * 2 + (1 if asset_type == 'aud' else 0)
                msg_l = (msg or "").lower()
                if 'done' in msg_l or 'skipped' in msg_l:
                    completed_steps = step_idx + 1
                else:
                    completed_steps = step_idx
                pct = min(100, max(0, int(round(completed_steps / total_steps * 100))))
                self.generate_all_btn.setText(f"⏳ Generating... {pct}%")
                lbl_img, lbl_aud, p_lbl, v_btn, pl_btn = status_map[idx]
                is_done        = 'done'       in msg_l or 'skipped' in msg_l
                is_in_progress = 'generating' in msg_l
                try:
                    if asset_type == 'img':
                        lbl_img.setText(msg)
                        lbl_img.setStyleSheet("color: #a6e3a1;" if is_done else ("color: #f9e2af;" if is_in_progress else "color: #f38ba8;"))
                        if is_done:
                            path = scenes[idx].get('img_path', '')
                            if path and os.path.exists(path):
                                pix = QPixmap(path)
                                p_lbl.setPixmap(pix.scaled(200, 112, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
                                v_btn.show()
                                v_btn.clicked.connect(lambda _, p=path: os.startfile(os.path.abspath(p)) if os.name == 'nt' else None)
                    else:
                        lbl_aud.setText(msg)
                        lbl_aud.setStyleSheet("color: #a6e3a1;" if is_done else ("color: #f9e2af;" if is_in_progress else "color: #f38ba8;"))
                        if is_done:
                            path = scenes[idx].get('audio_path', '')
                            if path:
                                pl_btn.show()
                                pl_btn.clicked.connect(lambda _, p=path: os.startfile(os.path.abspath(p)) if os.name == 'nt' else None)
                except RuntimeError:
                    pass

            def on_all_done(all_ok, msg):
                self.generate_all_btn.setEnabled(True)
                self.generate_all_btn.setText("⚡ Auto-Generate All Scenes")
                self.stop_btn.hide()
                if all_ok:
                    self.generate_all_btn.setText("✅ All Scenes Generated")
                else:
                    QMessageBox.warning(self, "Generation Issues/Stopped", msg)

            bulk_thread.scene_progress.connect(on_scene_progress, Qt.QueuedConnection)
            bulk_thread.all_done.connect(on_all_done, Qt.QueuedConnection)
            bulk_thread.start()

        self.generate_all_btn.clicked.connect(trigger_all)

    def _on_mobile_friendly_changed(self):
        """Resize placeholder previews (no image yet) to match the selected aspect ratio."""
        ph_w, ph_h = (112, 200) if self.mobile_friendly_chk.isChecked() else (200, 112)
        for entry in self._preview_labels.values():
            if not entry['has_image']:
                try:
                    entry['lbl'].setFixedSize(ph_w, ph_h)
                except RuntimeError:
                    pass  # widget already deleted

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
        self.back_btn.setProperty("class", "secondary-button")
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
        status_frame.setStyleSheet("QFrame { background:#2b2b36; border-radius:8px; padding:12px; }")
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
        self.render_btn.setMinimumHeight(32)
        self.render_btn.setEnabled(False)
        self.render_btn.clicked.connect(self.start_stitch.emit)
        root.addWidget(self.render_btn)

        # ── Post-render action buttons (hidden until video is ready) ─────
        action_row = QHBoxLayout()
        self.view_video_btn = QPushButton("🎬 View Video")
        self.view_video_btn.setProperty("class", "success-button")
        self.view_video_btn.setMinimumHeight(32)
        self.view_video_btn.hide()
        self.open_folder_btn = QPushButton("📂 Open Folder")
        self.open_folder_btn.setProperty("class", "secondary-button")
        self.open_folder_btn.setMinimumHeight(32)
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
    def populate_thumbnails(self, scenes, video_path: str = ""):
        """Populate the horizontal thumbnail grid from scene img_paths."""
        self.render_btn.setEnabled(bool(scenes))
        if scenes and video_path and os.path.exists(video_path):
            self.render_btn.setText("🔁 Re-Stitch Video")
        elif scenes:
            self.render_btn.setText("🎬 Stitch Final Video")
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
        self.render_btn.setText("🔁 Re-Stitch Video" if success else "🎬 Stitch Final Video")
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
        self.render_btn.setEnabled(False)
        self.render_btn.setText("🎬 Stitch Final Video")
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
        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setProperty("class", "secondary-button")
        refresh_btn.clicked.connect(self.load_history)
        title_row.addWidget(title)
        title_row.addStretch()
        self.delete_topic_btn = QPushButton("Delete")
        self.delete_topic_btn.setProperty("class", "danger-button")
        self.delete_topic_btn.setEnabled(False)
        self.delete_topic_btn.clicked.connect(self._delete_current_topic)
        title_row.addWidget(self.delete_topic_btn)
        title_row.addWidget(refresh_btn)
        root_layout.addLayout(title_row)

        # 3-column splitter
        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: #313244; width: 2px; }")

        # ── COL 1: Topic list ──────────────────────────────────────────────────
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        list_label = QLabel("📋 Topics")
        list_label.setProperty("class", "h2")
        left_layout.addWidget(list_label)

        self.topic_list = QListWidget()
        self.topic_list.setStyleSheet("""
            QListWidget { background: #181825; border: 1px solid #313244; border-radius: 6px; }
            QListWidget::item { padding: 10px 14px; border-bottom: 1px solid #2b2b36; }
            QListWidget::item:selected { background: #2563eb; color: #fff; border-radius: 4px; }
            QListWidget::item:hover:!selected { background: #28283d; }
        """)
        self.topic_list.currentRowChanged.connect(self._on_topic_selected)
        left_layout.addWidget(self.topic_list)
        left_panel.setMinimumWidth(200)

        # ── COL 2: Script ──────────────────────────────────────────────────────
        mid_panel = QWidget()
        mid_layout = QVBoxLayout(mid_panel)
        mid_layout.setContentsMargins(6, 0, 6, 0)
        mid_layout.setSpacing(8)

        self.detail_header = QLabel("Select a topic to view details.")
        self.detail_header.setProperty("class", "h2")
        self.detail_header.setWordWrap(True)
        mid_layout.addWidget(self.detail_header)

        # Video action row
        vid_layout = QVBoxLayout()
        vid_row = QHBoxLayout()
        self.video_lbl = QLabel("No final video yet.")
        self.video_lbl.setStyleSheet("color: #585b70;")
        self.video_lbl.setWordWrap(True)
        vid_layout.addWidget(self.video_lbl)
        self.play_video_btn = QPushButton("▶️ Play")
        self.play_video_btn.setProperty("class", "success-button")
        self.play_video_btn.hide()
        self.play_video_btn.clicked.connect(self._play_video)
        self.restitch_btn = QPushButton("🔁 Re-Stitch")
        self.restitch_btn.setProperty("class", "primary-button")
        self.restitch_btn.hide()
        self.restitch_btn.clicked.connect(self._on_restitch_clicked)
        self.view_video_btn = QPushButton("🎬 View")
        self.view_video_btn.setProperty("class", "success-button")
        self.view_video_btn.hide()
        self.open_folder_btn = QPushButton("📂 Folder")
        self.open_folder_btn.setProperty("class", "secondary-button")
        self.open_folder_btn.hide()
        vid_row.addWidget(self.restitch_btn)
        vid_row.addWidget(self.view_video_btn)
        vid_row.addWidget(self.open_folder_btn)
        vid_row.addWidget(self.play_video_btn)
        vid_row.addStretch()
        vid_layout.addLayout(vid_row)
        mid_layout.addLayout(vid_layout)

        self.restitch_status = QLabel("")
        self.restitch_status.setAlignment(Qt.AlignCenter)
        self.restitch_status.setWordWrap(True)
        self.restitch_status.hide()
        mid_layout.addWidget(self.restitch_status)

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
        mid_layout.addWidget(self.restitch_progress)

        script_label = QLabel("📄 Script")
        script_label.setProperty("class", "h2")
        mid_layout.addWidget(script_label)

        self.script_view = QTextBrowser()
        self.script_view.setStyleSheet(
            "QTextBrowser { background: #11111b; border: 1px solid #313244; border-radius: 6px; color: #cdd6f4; padding: 8px; }"
        )
        self.script_view.setPlaceholderText("Script will appear here...")
        mid_layout.addWidget(self.script_view, stretch=1)

        # Audio player pinned to bottom of middle column
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
        mid_layout.addWidget(self.history_player_frame)
        mid_panel.setMinimumWidth(260)

        # ── COL 3: Scenes & assets ─────────────────────────────────────────────
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_layout.setSpacing(8)

        scenes_header = QLabel("🎬 Scenes & Assets")
        scenes_header.setProperty("class", "h2")
        right_layout.addWidget(scenes_header)

        scene_ctrl_row = QHBoxLayout()
        self.history_tts_engine_combo = QComboBox()
        self.history_tts_engine_combo.addItems([
            "Gemini — Puck (Male)",
            "Gemini — Charon (Male)",
            "Gemini — Fenrir (Male)",
            "Gemini — Kore (Female)",
            "Gemini — Aoede (Female)",
            "Gemini — Leda (Female)",
        ])
        self.history_tts_engine_combo.setCurrentText("Gemini — Kore (Female)")
        self.history_tts_engine_combo.setToolTip("Select TTS voice for history scene audio generation.")
        self.history_generate_all_btn = QPushButton("⚡ Generate All")
        self.history_generate_all_btn.setProperty("class", "success-button")
        self.history_generate_all_btn.clicked.connect(self._on_history_generate_all)
        self.history_stop_btn = QPushButton("⏹️ Stop")
        self.history_stop_btn.setProperty("class", "danger-button")
        self.history_stop_btn.hide()
        self.history_mobile_friendly_chk = QCheckBox("Mobile Friendly")
        self.history_mobile_friendly_chk.setChecked(True)
        self.history_mobile_friendly_chk.setToolTip("Checked: 9:16 portrait aspect ratio (mobile). Unchecked: 16:9 landscape aspect ratio.")
        scene_ctrl_row.addWidget(self.history_tts_engine_combo)
        scene_ctrl_row.addStretch()
        scene_ctrl_row.addWidget(self.history_mobile_friendly_chk)
        scene_ctrl_row.addWidget(self.history_generate_all_btn)
        scene_ctrl_row.addWidget(self.history_stop_btn)
        right_layout.addLayout(scene_ctrl_row)

        self.scenes_scroll = QScrollArea()
        self.scenes_scroll.setWidgetResizable(True)
        self.scenes_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.scenes_widget = QWidget()
        self.scenes_layout = QVBoxLayout(self.scenes_widget)
        self.scenes_layout.setSpacing(8)
        self.scenes_layout.addStretch()
        self.scenes_scroll.setWidget(self.scenes_widget)
        right_layout.addWidget(self.scenes_scroll, stretch=1)
        right_panel.setMinimumWidth(320)

        splitter.addWidget(left_panel)
        splitter.addWidget(mid_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([220, 360, 620])
        root_layout.addWidget(splitter)
        self.setLayout(root_layout)

        self._topic_rows = []
        self._current_topic_id = None
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
        self.delete_topic_btn.setEnabled(False)
        for row in self._topic_rows:
            tid, topic, duration, created_at = row
            item = QListWidgetItem(f"{topic}\n{duration} min  |  {created_at[:16]}")
            item.setSizeHint(QSize(0, 54))
            self.topic_list.addItem(item)
        if not self._topic_rows:
            self._clear_history_detail()

    def select_topic(self, topic: str):
        for i, row in enumerate(self._topic_rows):
            if row[1] == topic:
                self.topic_list.setCurrentRow(i)
                return

    def _clear_history_detail(self):
        self._current_topic_id = None
        self._current_topic = ""
        self._current_video_path = None
        self._current_scenes = []
        self._history_topic_folder = ""
        self.detail_header.setText("Select a topic to view details.")
        self.video_lbl.setText("No final video yet.")
        self.video_lbl.setStyleSheet("color: #585b70;")
        self.script_view.clear()
        self.view_video_btn.hide()
        self.open_folder_btn.hide()
        self.play_video_btn.hide()
        self.restitch_btn.hide()
        self.restitch_status.hide()
        self.restitch_progress.hide()
        self.history_generate_all_btn.setEnabled(False)
        self.delete_topic_btn.setEnabled(False)
        self._audio_player.stop()
        self._audio_player_scene_idx = None
        self._reset_history_player_ui()
        while self.scenes_layout.count() > 1:
            item = self.scenes_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._history_scene_refs = []
        self._history_scene_audio_controls = {}
        self._history_scene_audio_paths = {}

    def _delete_current_topic(self):
        if not self._current_topic_id or not self._current_topic:
            return

        topic = self._current_topic
        reply = QMessageBox.question(
            self,
            "Delete Topic",
            f"Delete \"{topic}\" and all of its saved scenes, assets, and video?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return

        topic_folder = os.path.join(
            _app_data_dir(), 'assets', make_safe_topic(topic)
        )
        self._audio_player.stop()
        self._audio_player_scene_idx = None
        db.delete_topic(self._current_topic_id)
        shutil.rmtree(topic_folder, ignore_errors=True)
        self.load_history()
        self._clear_history_detail()

    def _on_topic_selected(self, index):
        if index < 0 or index >= len(self._topic_rows):
            self._clear_history_detail()
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        tid = self._topic_rows[index][0]
        topic_row, scenes = db.get_topic_detail(tid)
        if not topic_row:
            QApplication.restoreOverrideCursor()
            return

        _, topic, duration, script_text, created_at = topic_row
        self._current_topic_id = tid
        self._current_topic = topic
        self.delete_topic_btn.setEnabled(True)
        self.detail_header.setText(f"🎬 {topic.title()}  ({duration} min)  ·  {created_at[:16]}")
        self.script_view.setPlainText(script_text or "")

        # Build scene dicts usable by VideoStitchingThread
        self._current_scenes = [
            {
                'visual': visual,
                'narration': narration,
                'img_path': img_path or '',
                'audio_path': audio_path or '',
                'tts_voice': tts_voice or '',
                'db_id': sid,
            }
            for (sid, order, visual, narration, img_path, audio_path, tts_voice) in scenes
        ]

        # Check for a final video — custom output folder first, then app-data fallback
        safe_topic = make_safe_topic(topic)
        _custom_folder = _get_video_output_folder()
        _custom_path = os.path.join(_custom_folder, f"{safe_topic}.mp4") if _custom_folder else ""
        video_path = (
            _custom_path if _custom_path and os.path.exists(_custom_path)
            else os.path.join(_app_data_dir(), 'assets', safe_topic, f"{safe_topic}.mp4")
        )
        if os.path.exists(video_path):
            self._current_video_path = video_path
            self.video_lbl.setText(f"✅ {os.path.basename(video_path)}")
            self.video_lbl.setStyleSheet("color: #a6e3a1;")
            try:
                self.view_video_btn.clicked.disconnect()
                self.open_folder_btn.clicked.disconnect()
            except TypeError:
                pass
            def _open_video(p=video_path):
                if not os.path.exists(p):
                    QMessageBox.warning(self, "Video Not Found", f"Video file not found:\n{p}")
                    return
                os.startfile(os.path.abspath(p))

            def _open_folder(p=video_path):
                folder = os.path.abspath(os.path.dirname(p))
                if not os.path.exists(folder):
                    QMessageBox.warning(self, "Folder Not Found", f"Folder not found:\n{folder}")
                    return
                os.startfile(folder)

            self.view_video_btn.clicked.connect(lambda *_: _open_video())
            self.open_folder_btn.clicked.connect(lambda *_: _open_folder())
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
            _app_data_dir(), 'assets', make_safe_topic(topic)
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
        QApplication.restoreOverrideCursor()

    def _update_restitch_button_visibility(self):
        all_ready = all(
            s['img_path'] and os.path.exists(s['img_path']) and
            s['audio_path'] and os.path.exists(s['audio_path'])
            for s in self._current_scenes
        ) and bool(self._current_scenes)
        self.restitch_btn.setVisible(all_ready)
        if all_ready:
            has_video = bool(self._current_video_path and os.path.exists(self._current_video_path))
            self.restitch_btn.setText("🔁 Re-Stitch" if has_video else "🎬 Stitch Video")

    def _render_history_scene_cards(self):
        while self.scenes_layout.count() > 1:
            item = self.scenes_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._history_scene_refs = []

        for i, scene in enumerate(self._current_scenes):
            card = QFrame()
            card.setStyleSheet("QFrame { background-color: #2b2b36; border-radius: 6px; padding: 8px; }")
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
            _h_img_exists = bool(scene.get('img_path') and os.path.exists(scene['img_path']))
            gen_img_btn = QPushButton("🖼️ Re-Generate Image" if _h_img_exists else "🖼️ Generate Image")
            gen_img_btn.setProperty("class", "scene-button")
            view_img_btn = QPushButton("👁 View Image")
            view_img_btn.setProperty("class", "scene-button")
            view_img_btn.hide()
            img_status = QLabel("")
            img_status.setStyleSheet("color: #a6e3a1;")
            img_row.addWidget(gen_img_btn)
            img_row.addWidget(view_img_btn)
            img_row.addWidget(img_status)
            img_row.addStretch()
            text_block.addLayout(img_row)

            aud_row = QHBoxLayout()
            _h_aud_exists = bool(scene.get('audio_path') and os.path.exists(scene['audio_path']))
            gen_aud_btn = QPushButton("🎙️ Re-Generate Audio" if _h_aud_exists else "🎙️ Generate Audio")
            gen_aud_btn.setProperty("class", "scene-button")
            play_aud_btn = QPushButton("▶ Play Audio")
            play_aud_btn.setProperty("class", "scene-button")
            play_aud_btn.hide()
            aud_status = QLabel("")
            aud_status.setStyleSheet("color: #a6e3a1;")
            aud_row.addWidget(gen_aud_btn)
            aud_row.addWidget(play_aud_btn)
            aud_row.addWidget(aud_status)
            aud_row.addStretch()
            text_block.addLayout(aud_row)

            right_col = QVBoxLayout()
            right_col.setSpacing(4)
            thumb = QLabel()
            thumb.setFixedSize(142, 80)  # default placeholder size
            thumb.setAlignment(Qt.AlignCenter)
            thumb.hide()
            if scene.get('img_path') and os.path.exists(scene['img_path']):
                pix = QPixmap(scene['img_path'])
                _scaled = pix.scaled(142, 142, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                thumb.setFixedSize(_scaled.width(), _scaled.height())
                thumb.setPixmap(_scaled)
                thumb.show()
                view_img_btn.show()
                try:
                    view_img_btn.clicked.disconnect()
                except TypeError:
                    pass
                view_img_btn.clicked.connect(lambda _, p=scene['img_path']: self._show_history_image_preview(p))
            else:
                thumb.setText("No Image")
                thumb.setStyleSheet("background:#181825; color:#585b70; border-radius:4px;")

            h_aud_icon = QLabel("🔊 Audio Ready")
            h_aud_icon.setAlignment(Qt.AlignCenter)
            h_aud_icon.setStyleSheet("color: #a6e3a1; font-size: 11px;")
            h_aud_icon.hide()

            if scene.get('audio_path') and os.path.exists(scene['audio_path']):
                self._history_scene_audio_paths[i] = scene['audio_path']
                h_aud_icon.show()
                play_aud_btn.show()
                try:
                    play_aud_btn.clicked.disconnect()
                except TypeError:
                    pass
                play_aud_btn.clicked.connect(
                    lambda _, p=scene['audio_path'], idx=i, lbl=aud_status:
                    self._play_history_audio(p, idx, lbl, None)
                )

            right_col.addWidget(thumb)
            right_col.addWidget(h_aud_icon)

            def create_img_handler(idx=i, s=scene, status_lbl=img_status, btn=gen_img_btn, preview_lbl=thumb, view_btn=view_img_btn, _title_card=(i == 0), _mobile_chk=self.history_mobile_friendly_chk):
                out_path = os.path.join(self._history_topic_folder, f"scene_{idx+1}.jpg")
                btn.setEnabled(False)
                status_lbl.setText("⏳ Generating Image...")
                status_lbl.setStyleSheet("color: #f9e2af;")
                _overlay = (
                    self._current_topic
                    if _title_card and self._current_topic
                    else make_scene_overlay_text(s.get('narration', ''), s.get('visual', ''))
                )
                thread = ImageGenerationThread(
                    s.get('visual', ''),
                    out_path,
                    _overlay,
                    s.get('narration', ''),
                    _title_card,
                    _mobile_chk.isChecked(),
                )
                setattr(self, f"history_img_thread_{idx}", thread)

                def on_finish(success, path):
                    try:
                        btn.setEnabled(True)
                        if success:
                            status_lbl.setText("✅ Image Generated!")
                            status_lbl.setStyleSheet("color: #a6e3a1;")
                            btn.setText("🖼️ Re-Generate Image")
                            s['img_path'] = path
                            db.update_scene_asset(s.get('db_id'), 'img_path', path)
                            pix = QPixmap(path)
                            _scaled = pix.scaled(142, 142, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                            preview_lbl.setFixedSize(_scaled.width(), _scaled.height())
                            preview_lbl.setPixmap(_scaled)
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
                    except RuntimeError:
                        pass

                thread.generation_done.connect(on_finish)
                thread.start()

            def create_aud_handler(
                idx=i, s=scene, status_lbl=aud_status, btn=gen_aud_btn, play_btn=play_aud_btn, aud_icon=h_aud_icon
            ):
                engine, voice = parse_tts_selection(self.history_tts_engine_combo.currentText())
                stored_voice = s.get('tts_voice', '')
                if stored_voice and voice and stored_voice != voice:
                    ans = QMessageBox.warning(
                        self, "Voice Mismatch",
                        f"This scene was originally generated with voice <b>{stored_voice}</b>.<br>"
                        f"You selected <b>{voice}</b>.<br><br>"
                        "Regenerating with a different voice may cause inconsistency across scenes.<br>"
                        "Continue anyway?",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No
                    )
                    if ans != QMessageBox.Yes:
                        return
                audio_ext = "wav" if engine == "gemini" else "mp3"
                out_path = os.path.join(self._history_topic_folder, f"scene_{idx+1}.{audio_ext}")
                btn.setEnabled(False)
                status_lbl.setText("⏳ Generating Audio...")
                status_lbl.setStyleSheet("color: #f9e2af;")
                thread = AudioGenerationThread(s.get('narration', ''), out_path, engine, voice)
                setattr(self, f"history_aud_thread_{idx}", thread)

                def on_finish(success, path, err_msg):
                    try:
                        btn.setEnabled(True)
                        if success:
                            status_lbl.setText("✅ Audio Saved.")
                            status_lbl.setStyleSheet("color: #a6e3a1;")
                            btn.setText("🎙️ Re-Generate Audio")
                            aud_icon.show()
                            s['audio_path'] = path
                            s['tts_voice'] = voice
                            self._history_scene_audio_paths[idx] = path
                            db.update_scene_asset(s.get('db_id'), 'audio_path', path)
                            db.update_scene_asset(s.get('db_id'), 'tts_voice', voice or '')
                            play_btn.show()
                            try:
                                play_btn.clicked.disconnect()
                            except TypeError:
                                pass
                            play_btn.clicked.connect(
                                lambda _, p=path, scene_idx=idx, lbl=status_lbl:
                                self._play_history_audio(p, scene_idx, lbl, None)
                            )
                        else:
                            status_lbl.setText(f"❌ Audio Failed: {err_msg}" if err_msg else "❌ Audio Failed.")
                            status_lbl.setStyleSheet("color: #f38ba8;")
                        self._update_restitch_button_visibility()
                    except RuntimeError:
                        pass

                thread.generation_done.connect(on_finish)
                thread.start()

            gen_img_btn.clicked.connect(lambda _, f=create_img_handler: f())
            gen_aud_btn.clicked.connect(lambda _, f=create_aud_handler: f())

            card_layout.addLayout(text_block, stretch=3)
            card_layout.addLayout(right_col)
            self.scenes_layout.insertWidget(self.scenes_layout.count() - 1, card)
            self._history_scene_refs.append((scene, img_status, aud_status, thumb, view_img_btn, play_aud_btn))
            self._history_scene_audio_controls[i] = aud_status

    def _on_history_generate_all(self):
        if not self._current_scenes:
            return

        has_existing_assets = any(
            (scene.get('img_path') and os.path.exists(scene['img_path']))
            or (scene.get('audio_path') and os.path.exists(scene['audio_path']))
            for scene in self._current_scenes
        )
        skip_existing = False

        if has_existing_assets:
            # Ask the user whether to regenerate everything or only missing assets
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Generate Assets")
            msg_box.setText("What would you like to generate?")
            msg_box.setInformativeText(
                "• <b>Missing only</b> — skip scenes that already have an image and audio file.<br>"
                "• <b>Regenerate all</b> — overwrite every image and audio, even if they exist."
            )
            btn_missing = msg_box.addButton("Missing only", QMessageBox.AcceptRole)
            btn_all     = msg_box.addButton("Regenerate all", QMessageBox.DestructiveRole)
            msg_box.addButton(QMessageBox.Cancel)
            msg_box.exec_()

            clicked = msg_box.clickedButton()
            if clicked is None or clicked == msg_box.button(QMessageBox.Cancel):
                return
            skip_existing = (clicked == btn_missing)

        self.history_generate_all_btn.setEnabled(False)
        self.history_generate_all_btn.setText("⏳ Generating...")
        self.history_stop_btn.show()
        self.history_stop_btn.setEnabled(True)
        self.history_stop_btn.setText("⏹️ Stop")

        engine, voice = parse_tts_selection(self.history_tts_engine_combo.currentText())
        status_map = {idx: refs for idx, refs in enumerate(self._history_scene_refs)}

        self._history_bulk_thread = BulkGenerationThread(
            self._current_scenes, self._history_topic_folder, engine, voice, skip_existing, self._current_topic, self.history_mobile_friendly_chk.isChecked()
        )

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
            total_steps = max(1, len(self._current_scenes) * 2)
            step_idx = idx * 2 + (1 if asset_type == 'aud' else 0)
            msg_l = (msg or "").lower()
            if "done" in msg_l or "skipped" in msg_l:
                completed_steps = step_idx + 1
            else:
                completed_steps = step_idx
            pct = min(100, max(0, int(round(completed_steps / total_steps * 100))))
            self.history_generate_all_btn.setText(f"⏳ Generating... {pct}%")
            scene, lbl_img, lbl_aud, thumb, view_btn, play_btn = status_map[idx]
            is_done = "done" in msg_l
            is_in_progress = "generating" in msg_l
            try:
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
                        try:
                            play_btn.clicked.disconnect()
                        except TypeError:
                            pass
                        play_btn.clicked.connect(
                            lambda _, p=scene['audio_path'], scene_idx=idx, lbl=lbl_aud:
                            self._play_history_audio(p, scene_idx, lbl, None)
                        )
            except RuntimeError:
                # Widget was deleted (user navigated away) before the thread finished — ignore.
                pass

        def on_all_done(all_ok, msg):
            self.history_generate_all_btn.setEnabled(True)
            self.history_generate_all_btn.setText("⚡ Auto-Generate All Scenes")
            self.history_stop_btn.hide()
            self._update_restitch_button_visibility()
            if not all_ok:
                QMessageBox.warning(self, "Generation Issues/Stopped", msg)

        self._history_bulk_thread.scene_progress.connect(on_scene_progress, Qt.QueuedConnection)
        self._history_bulk_thread.all_done.connect(on_all_done, Qt.QueuedConnection)
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
            lbl = self._history_scene_audio_controls.get(first_idx)
            if lbl:
                self._play_history_audio(self._history_scene_audio_paths[first_idx], first_idx, lbl, None)
            return

        if self._audio_player.state() == QMediaPlayer.PausedState:
            self._audio_player.play()
            return

        lbl = self._history_scene_audio_controls.get(self._audio_player_scene_idx)
        path = self._history_scene_audio_paths.get(self._audio_player_scene_idx, "")
        if lbl and path:
            self._play_history_audio(path, self._audio_player_scene_idx, lbl, None)

    def _on_history_player_pause_clicked(self):
        if self._audio_player_scene_idx is None:
            return
        lbl = self._history_scene_audio_controls.get(self._audio_player_scene_idx)
        if not lbl:
            return
        self._pause_resume_history_audio(self._audio_player_scene_idx, lbl)

    def _on_history_player_stop_clicked(self):
        if self._audio_player_scene_idx is None:
            return
        lbl = self._history_scene_audio_controls.get(self._audio_player_scene_idx)
        if not lbl:
            return
        self._stop_history_audio(self._audio_player_scene_idx, lbl)

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

    def _pause_resume_history_audio(self, scene_idx: int, status_label: QLabel):
        if self._audio_player_scene_idx != scene_idx:
            status_label.setText("Play this scene first")
            status_label.setStyleSheet("color: #f9e2af;")
            return
        state = self._audio_player.state()
        if state == QMediaPlayer.PlayingState:
            self._audio_player.pause()
            status_label.setText("Paused")
            status_label.setStyleSheet("color: #f9e2af;")
        elif state == QMediaPlayer.PausedState:
            self._audio_player.play()
            status_label.setText("Playing")
            status_label.setStyleSheet("color: #a6e3a1;")

    def _stop_history_audio(self, scene_idx: int, status_label: QLabel):
        if self._audio_player_scene_idx != scene_idx:
            status_label.setText("Stopped")
            status_label.setStyleSheet("color: #cdd6f4;")
            return
        self._audio_player.stop()
        self._audio_player_scene_idx = None
        self._reset_history_player_ui()
        status_label.setText("Stopped")
        status_label.setStyleSheet("color: #cdd6f4;")

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


class ApiKeyDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Gemini API Key Required")
        self.setFixedWidth(480)
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("Welcome to Infographic Video Generator")
        title.setProperty("class", "h2")
        layout.addWidget(title)

        info = QLabel("A Gemini API key is required to generate scripts, images, and audio.")
        info.setWordWrap(True)
        layout.addWidget(info)

        steps_frame = QFrame()
        steps_frame.setStyleSheet("background: #1e1e2e; border-radius: 6px; padding: 4px;")
        steps_layout = QVBoxLayout(steps_frame)
        steps_layout.setContentsMargins(12, 10, 12, 10)
        steps_layout.setSpacing(4)

        steps_title = QLabel("How to get your free API key:")
        steps_title.setStyleSheet("font-weight: bold; color: #89b4fa; font-size: 12px;")
        steps_layout.addWidget(steps_title)

        steps = [
            "1. Open: https://aistudio.google.com/app/apikey",
            "2. Sign in with your Google account",
            "3. Click \"Create API key\"",
            "4. Copy and paste it below",
        ]
        for step in steps:
            lbl = QLabel(step)
            lbl.setStyleSheet("color: #cdd6f4; font-size: 12px;")
            steps_layout.addWidget(lbl)

        open_btn = QPushButton("🌐 Open Google AI Studio")
        open_btn.setProperty("class", "secondary-button")
        open_btn.setFixedHeight(28)
        open_btn.clicked.connect(lambda: __import__('webbrowser').open("https://aistudio.google.com/app/apikey"))
        steps_layout.addWidget(open_btn)
        layout.addWidget(steps_frame)

        key_row = QHBoxLayout()
        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("Paste your Gemini API key here...")
        self.key_input.setEchoMode(QLineEdit.Password)
        key_row.addWidget(self.key_input)
        self.show_btn = QPushButton("👁")
        self.show_btn.setFixedWidth(36)
        self.show_btn.setCheckable(True)
        self.show_btn.toggled.connect(lambda checked: self.key_input.setEchoMode(
            QLineEdit.Normal if checked else QLineEdit.Password))
        key_row.addWidget(self.show_btn)
        layout.addLayout(key_row)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color: #f38ba8;")
        layout.addWidget(self.status_lbl)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save & Continue")
        save_btn.setProperty("class", "success-button")
        save_btn.clicked.connect(self._save)
        skip_btn = QPushButton("Skip for now")
        skip_btn.setProperty("class", "secondary-button")
        skip_btn.clicked.connect(self.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(skip_btn)
        layout.addLayout(btn_row)

    def _save(self):
        key = self.key_input.text().strip()
        if not key:
            self.status_lbl.setText("Please enter a valid API key.")
            return
        _save_env_key("GEMINI_API_KEY", key)
        self.accept()


class SettingsTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignTop)

        title = QLabel("Settings")
        title.setProperty("class", "h2")
        layout.addWidget(title)

        # Gemini API Key
        api_frame = QFrame()
        api_frame.setProperty("class", "card")
        api_layout = QVBoxLayout(api_frame)
        api_layout.setSpacing(10)

        api_title = QLabel("Gemini API Key")
        api_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        api_layout.addWidget(api_title)

        api_desc = QLabel("Used for script generation, image generation, and TTS audio.")
        api_desc.setStyleSheet("color: #a6adc8; font-size: 12px;")
        api_layout.addWidget(api_desc)

        key_row = QHBoxLayout()
        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("Enter Gemini API key...")
        self.key_input.setEchoMode(QLineEdit.Password)
        current_key = os.getenv("GEMINI_API_KEY", "")
        if current_key and current_key != "your_gemini_api_key_here":
            self.key_input.setText(current_key)
        key_row.addWidget(self.key_input)

        self.show_btn = QPushButton("👁 Show Key")
        self.show_btn.setProperty("class", "secondary-button")
        self.show_btn.setFixedWidth(130)
        self.show_btn.setCheckable(True)
        self.show_btn.toggled.connect(self._toggle_visibility)
        key_row.addWidget(self.show_btn)
        api_layout.addLayout(key_row)

        save_row = QHBoxLayout()
        self.save_btn = QPushButton("💾 Save API Key")
        self.save_btn.setProperty("class", "success-button")
        self.save_btn.setFixedWidth(160)
        self.save_btn.clicked.connect(self._save_key)
        self.status_lbl = QLabel("")
        save_row.addWidget(self.save_btn)
        save_row.addWidget(self.status_lbl)
        save_row.addStretch()
        api_layout.addLayout(save_row)

        layout.addWidget(api_frame)

        # Image Resolution
        res_frame = QFrame()
        res_frame.setProperty("class", "card")
        res_layout = QVBoxLayout(res_frame)
        res_layout.setSpacing(10)

        res_title = QLabel("Image Resolution")
        res_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        res_layout.addWidget(res_title)

        res_desc = QLabel("Higher resolution produces better quality images but takes longer to generate.")
        res_desc.setStyleSheet("color: #a6adc8; font-size: 12px;")
        res_desc.setWordWrap(True)
        res_layout.addWidget(res_desc)

        res_row = QHBoxLayout()
        res_lbl = QLabel("Resolution:")
        self.res_combo = QComboBox()
        self.res_combo.addItems(["512 (fastest)", "1K (default)", "2K", "4K (best quality)"])
        saved_res = os.getenv("IMAGE_RESOLUTION", "1K").strip().upper()
        res_map = {"512": 0, "1K": 1, "2K": 2, "4K": 3}
        self.res_combo.setCurrentIndex(res_map.get(saved_res, 1))
        res_row.addWidget(res_lbl)
        res_row.addWidget(self.res_combo)
        res_row.addStretch()
        res_layout.addLayout(res_row)

        save_res_row = QHBoxLayout()
        save_res_btn = QPushButton("💾 Save Resolution")
        save_res_btn.setProperty("class", "success-button")
        save_res_btn.setFixedWidth(170)
        self.res_status_lbl = QLabel("")
        save_res_btn.clicked.connect(self._save_resolution)
        save_res_row.addWidget(save_res_btn)
        save_res_row.addWidget(self.res_status_lbl)
        save_res_row.addStretch()
        res_layout.addLayout(save_res_row)

        layout.addWidget(res_frame)

        # Video Output Folder
        folder_frame = QFrame()
        folder_frame.setProperty("class", "card")
        folder_layout = QVBoxLayout(folder_frame)
        folder_layout.setSpacing(10)

        folder_title = QLabel("Video Output Folder")
        folder_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        folder_layout.addWidget(folder_title)

        folder_desc = QLabel("Where finished videos are saved. You will be asked on the first stitch if not set.")
        folder_desc.setStyleSheet("color: #a6adc8; font-size: 12px;")
        folder_desc.setWordWrap(True)
        folder_layout.addWidget(folder_desc)

        folder_row = QHBoxLayout()
        self.folder_input = QLineEdit()
        self.folder_input.setReadOnly(True)
        self.folder_input.setPlaceholderText("Not set — will ask on first stitch")
        saved_folder = os.getenv("VIDEO_OUTPUT_FOLDER", "").strip()
        if saved_folder:
            self.folder_input.setText(saved_folder)
        folder_row.addWidget(self.folder_input)
        browse_btn = QPushButton("📂 Browse")
        browse_btn.setProperty("class", "secondary-button")
        browse_btn.setFixedWidth(110)
        browse_btn.clicked.connect(self._browse_output_folder)
        folder_row.addWidget(browse_btn)
        clear_btn = QPushButton("✕ Clear")
        clear_btn.setProperty("class", "danger-button")
        clear_btn.setFixedWidth(80)
        clear_btn.clicked.connect(self._clear_output_folder)
        folder_row.addWidget(clear_btn)
        folder_layout.addLayout(folder_row)

        self.folder_status_lbl = QLabel("")
        folder_layout.addWidget(self.folder_status_lbl)

        layout.addWidget(folder_frame)
        layout.addStretch()

    def _browse_output_folder(self):
        current = os.getenv("VIDEO_OUTPUT_FOLDER", "").strip() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose Video Output Folder", current)
        if not chosen:
            return
        _save_env_key("VIDEO_OUTPUT_FOLDER", chosen)
        _load_env()
        self.folder_input.setText(chosen)
        self.folder_status_lbl.setText("✅ Saved.")
        self.folder_status_lbl.setStyleSheet("color: #a6e3a1;")
        QTimer.singleShot(3000, lambda: self.folder_status_lbl.setText(""))

    def _clear_output_folder(self):
        _save_env_key("VIDEO_OUTPUT_FOLDER", "")
        _load_env()
        self.folder_input.clear()
        self.folder_status_lbl.setText("Cleared — will ask on next stitch.")
        self.folder_status_lbl.setStyleSheet("color: #a6adc8;")
        QTimer.singleShot(3000, lambda: self.folder_status_lbl.setText(""))

    def _save_resolution(self):
        idx = self.res_combo.currentIndex()
        val = ["512", "1K", "2K", "4K"][idx]
        _save_env_key("IMAGE_RESOLUTION", val)
        _load_env()
        self.res_status_lbl.setText("✅ Saved.")
        self.res_status_lbl.setStyleSheet("color: #a6e3a1;")
        QTimer.singleShot(3000, lambda: self.res_status_lbl.setText(""))

    def _toggle_visibility(self, checked: bool):
        self.key_input.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        self.show_btn.setText("🙈 Hide Key" if checked else "👁 Show Key")

    def _save_key(self):
        key = self.key_input.text().strip()
        if not key:
            self.status_lbl.setText("⚠️ Key cannot be empty.")
            self.status_lbl.setStyleSheet("color: #f38ba8;")
            return
        _save_env_key("GEMINI_API_KEY", key)
        _load_env()
        self.status_lbl.setText("✅ Saved successfully.")
        self.status_lbl.setStyleSheet("color: #a6e3a1;")
        QTimer.singleShot(3000, lambda: self.status_lbl.setText(""))


class HowToUseTab(QWidget):
    _CONTENT = [
        ("🔑 Gemini API Key", """
<b>Step 1:</b> Go to <a href="https://aistudio.google.com/app/apikey" style="color:#89b4fa;">https://aistudio.google.com/app/apikey</a><br>
<b>Step 2:</b> Sign in with your Google account.<br>
<b>Step 3:</b> Click <b>Create API key</b> and copy the generated key.<br>
<b>Step 4:</b> Open the <b>⚙️ Settings</b> tab, paste your key, and click <b>💾 Save API Key</b>.<br><br>
<i>The key is stored locally in your <code>.env</code> file and never sent anywhere except directly to Google's API.</i>
"""),
        ("✍️ Generating a Script", """
<b>From Topic (AI-generated):</b><br>
1. Go to the <b>✍️ Scripting</b> tab.<br>
2. Select <b>Generate from Topic</b>.<br>
3. Type your topic (e.g. "How black holes form").<br>
4. Set the desired video duration in minutes.<br>
5. Optionally enable <b>🌐 Web Search</b> to ground the script in current facts.<br>
6. Click <b>Generate Script</b>. Gemini will write a full multi-scene documentary script.<br><br>
<b>From Your Own Text:</b><br>
1. Select <b>Analyze Text</b>.<br>
2. Paste your article, notes, or essay.<br>
3. Click <b>Analyze</b>. The AI splits it into scenes automatically.
"""),
        ("🌐 Web Search Mode", """
When enabled, the AI searches the web for up-to-date information before writing the script.<br><br>
• Best for news topics, recent events, or anything time-sensitive.<br>
• Slightly slower than pure AI generation.<br>
• The resulting script will cite real, current facts rather than relying solely on training data.<br><br>
Toggle it with the <b>🌐 Web Search</b> checkbox on the Scripting tab before clicking Generate.
"""),
        ("🎨 Generating Scenes", """
After a script is generated you land on the <b>🎨 Storyboard</b> tab.<br><br>
<b>Auto-generate everything at once:</b><br>
Click <b>⚡ Auto-Generate All Scenes</b> — images and audio are generated in parallel for every scene.<br><br>
<b>Generate individually:</b><br>
Each scene card has:<br>
&nbsp;&nbsp;• <b>🖼 Generate Image</b> — creates an AI image for that scene's visual prompt.<br>
&nbsp;&nbsp;• <b>🎙 Generate Audio</b> — synthesises the narration using the selected TTS voice.<br>
&nbsp;&nbsp;• <b>👁️ View Image</b> — preview the generated image.<br>
&nbsp;&nbsp;• <b>▶️ Play Audio</b> — listen to the narration.<br><br>
<b>TTS Voice:</b> Choose from the dropdown at the top of the Storyboard tab. Default is <b>Kore (Female)</b>.
"""),
        ("🎬 Stitching the Video", """
Once all scenes have both an image and audio:<br><br>
1. Go to the <b>🎬 Export Video</b> tab.<br>
2. The button will read <b>🎬 Stitch Final Video</b> (disabled if no scenes are ready).<br>
3. Click it — the app combines all scene images, audio, zoom animations, and subtitles into a single MP4.<br>
4. Progress is shown live. A desktop notification appears when done.<br>
5. Use <b>🎬 View Video</b> or <b>📂 Open Folder</b> to find the output.<br><br>
The video is saved inside <code>assets/&lt;topic&gt;/&lt;topic&gt;.mp4</code>.
"""),
        ("🔁 Re-Stitching", """
Already stitched a video but want to change something?<br><br>
• After a successful stitch the button changes to <b>🔁 Re-Stitch Video</b>.<br>
• Re-generate individual images or audio on the Storyboard, then re-stitch.<br>
• Re-stitching overwrites the previous MP4.<br><br>
<b>From History:</b><br>
Open the <b>📚 History</b> tab, select a past project, and click <b>🔁 Re-Stitch</b> to rebuild it.
"""),
        ("📚 History", """
The <b>📚 History</b> tab keeps a record of every project you've generated.<br><br>
• Click any project to expand its scenes.<br>
• Re-generate individual scene images or audio using the same controls as the Storyboard.<br>
• Click <b>🔁 Re-Stitch</b> to rebuild the video from updated assets.<br>
• Projects are stored in a local SQLite database (<code>video_generator.db</code>).
"""),
        ("🎙 TTS Voices", """
<b>Gemini voices</b> (cloud-based, free tier available):<br>
&nbsp;&nbsp;• <b>Kore</b> — Female (default)<br>
&nbsp;&nbsp;• <b>Aoede</b> — Female<br>
&nbsp;&nbsp;• <b>Leda</b> — Female<br>
&nbsp;&nbsp;• <b>Puck</b> — Male<br>
&nbsp;&nbsp;• <b>Charon</b> — Male<br>
&nbsp;&nbsp;• <b>Fenrir</b> — Male<br><br>
Select your preferred voice from the dropdown at the top of the Storyboard or History tab before generating audio.
"""),
        ("⚙️ Settings", """
The <b>⚙️ Settings</b> tab lets you manage your API key:<br><br>
• <b>💾 Save API Key</b> — saves your Gemini API key to the local <code>.env</code> file.<br>
• <b>👁 Show Key / 🙈 Hide Key</b> — toggles key visibility.<br>
• Changes take effect immediately without restarting the app.<br><br>
<i>On first launch, a prompt will automatically ask for your API key if none is found.</i>
"""),
        ("💡 Tips & Troubleshooting", """
<b>Video stitching fails:</b><br>
&nbsp;&nbsp;→ Make sure all scenes have both an image and audio generated before stitching.<br><br>
<b>Images or audio not generating:</b><br>
&nbsp;&nbsp;→ Check your Gemini API key in Settings. Ensure you have an active internet connection.<br><br>
<b>Subtitles look off:</b><br>
&nbsp;&nbsp;→ Subtitles are auto-aligned to speech. Short scenes may have less precise timing.<br><br>
<b>Video duration feels too short/long:</b><br>
&nbsp;&nbsp;→ Adjust the duration slider on the Scripting tab before generating.<br><br>
<b>Re-stitch to apply changes:</b><br>
&nbsp;&nbsp;→ Any time you regenerate an image or audio for a scene, re-stitch to get the updated video.
"""),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Left: topic list ──────────────────────────────────────────────
        left_panel = QWidget()
        left_panel.setFixedWidth(220)
        left_panel.setStyleSheet("background: #181825; border-right: 1px solid #313244;")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        left_header = QLabel("  How to Use")
        left_header.setFixedHeight(48)
        left_header.setStyleSheet(
            "background: #1e1e2e; color: #cdd6f4; font-weight: bold; font-size: 13px;"
            "border-bottom: 1px solid #313244; padding-left: 8px;"
        )
        left_layout.addWidget(left_header)

        self._list = QListWidget()
        self._list.setStyleSheet("""
            QListWidget {
                background: transparent;
                border: none;
                outline: none;
                font-size: 13px;
                color: #a6adc8;
            }
            QListWidget::item {
                padding: 10px 16px;
                border-bottom: 1px solid #1e1e2e;
            }
            QListWidget::item:selected {
                background: #313244;
                color: #cdd6f4;
                border-left: 3px solid #89b4fa;
            }
            QListWidget::item:hover:!selected {
                background: #242434;
                color: #cdd6f4;
            }
        """)
        for title, _ in self._CONTENT:
            self._list.addItem(title)
        left_layout.addWidget(self._list)
        root.addWidget(left_panel)

        # ── Right: content area ───────────────────────────────────────────
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._content_title = QLabel()
        self._content_title.setFixedHeight(48)
        self._content_title.setStyleSheet(
            "background: #1e1e2e; color: #cdd6f4; font-weight: bold; font-size: 14px;"
            "border-bottom: 1px solid #313244; padding-left: 24px;"
        )
        right_layout.addWidget(self._content_title)

        self._content_browser = QTextBrowser()
        self._content_browser.setOpenExternalLinks(True)
        self._content_browser.setFrameShape(QFrame.NoFrame)
        self._content_browser.setStyleSheet(
            "background: #1e1e2e; color: #cdd6f4; font-size: 13px; padding: 24px;"
        )
        right_layout.addWidget(self._content_browser)
        root.addWidget(right_panel, 1)

        self._list.currentRowChanged.connect(self._on_select)
        self._list.setCurrentRow(0)

    def _on_select(self, row: int):
        if row < 0 or row >= len(self._CONTENT):
            return
        title, html = self._CONTENT[row]
        self._content_title.setText(f"  {title}")
        self._content_browser.setHtml(
            f"<div style='color:#cdd6f4; font-size:13px; line-height:1.8;'>{html.strip()}</div>"
        )



class BackgroundJobsTab(QWidget):
    """
    Shows all active and completed background jobs grouped by topic.
    Each topic gets one card; generation and stitching rows are added inside it.
    """
    load_job_requested = pyqtSignal(list, str)  # (scenes, topic) → open in Storyboard
    open_in_history_requested = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._job_counter = 0
        self._job_refs = {}      # job_id → dict  (keeps threads/widgets alive)
        self._topic_cards = {}   # topic str → {'rows_layout': QVBoxLayout, 'card': QFrame}

        root = QVBoxLayout()
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        hdr = QLabel("⏳ Background Jobs")
        hdr.setProperty("class", "h2")
        root.addWidget(hdr)

        desc = QLabel(
            "Each topic's generation and stitching are grouped in one card. "
            "Start new topics at any time — all jobs run independently."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#a6adc8; font-size:12px;")
        root.addWidget(desc)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setSpacing(14)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._empty_lbl = QLabel(
            "No jobs yet.\nClick ⚡ Auto-Generate All Scenes in the Storyboard tab to start one."
        )
        self._empty_lbl.setAlignment(Qt.AlignCenter)
        self._empty_lbl.setStyleSheet("color:#585b70; font-size:13px; padding:40px;")
        self._list_layout.addWidget(self._empty_lbl)
        self._list_layout.addStretch()
        scroll.setWidget(self._list_widget)
        root.addWidget(scroll)
        self.setLayout(root)

    # ── internal helpers ──────────────────────────────────────────────────

    def _get_or_create_topic_card(self, topic: str) -> dict:
        """Return the existing card dict for `topic`, or create a new one."""
        if topic in self._topic_cards:
            return self._topic_cards[topic]

        self._empty_lbl.hide()

        # Outer topic card
        card = QFrame()
        card.setStyleSheet(
            "QFrame#topicCard{"
            "  background:#2b2b36;"
            "  border-radius:10px;"
            "  border:1px solid #313244;"
            "}"
        )
        card.setObjectName("topicCard")
        card_vl = QVBoxLayout(card)
        card_vl.setContentsMargins(16, 14, 16, 14)
        card_vl.setSpacing(0)

        # Topic header
        hdr = QLabel(f"🎬  {topic}")
        hdr.setStyleSheet(
            "font-weight:bold; color:#cdd6f4; font-size:15px;"
            "padding-bottom:10px; border-bottom:1px solid #45475a;"
        )
        card_vl.addWidget(hdr)

        # Container for job rows
        rows_w = QWidget()
        rows_w.setStyleSheet("background:transparent;")
        rows_vl = QVBoxLayout(rows_w)
        rows_vl.setContentsMargins(0, 10, 0, 0)
        rows_vl.setSpacing(10)
        card_vl.addWidget(rows_w)

        # Insert newest topic card at top (above stretch)
        self._list_layout.insertWidget(0, card)

        entry = {'card': card, 'rows_layout': rows_vl}
        self._topic_cards[topic] = entry
        return entry

    def _make_job_row(self, rows_layout, icon: str, label: str, bar_css: str):
        """Build a job-row widget inside a topic card. Returns (bar, status_lbl, btn_layout)."""
        row = QFrame()
        row.setStyleSheet("QFrame{background:#1e1e2e; border-radius:6px;}")
        rl = QVBoxLayout(row)
        rl.setContentsMargins(12, 8, 12, 8)
        rl.setSpacing(5)

        title_lbl = QLabel(f"{icon}  {label}")
        title_lbl.setStyleSheet("color:#a6adc8; font-size:12px; font-weight:bold;")
        rl.addWidget(title_lbl)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(7)
        bar.setStyleSheet(bar_css)
        rl.addWidget(bar)

        status_lbl = QLabel("⏳  Starting…")
        status_lbl.setStyleSheet("color:#f9e2af; font-size:12px;")
        rl.addWidget(status_lbl)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)
        rl.addLayout(btn_layout)

        rows_layout.addWidget(row)
        return bar, status_lbl, btn_layout

    # ── public API ────────────────────────────────────────────────────────

    def add_job(self, thread, topic: str, scenes: list):
        """Register a BulkGenerationThread as a row inside the topic's card."""
        job_id = f"job_{self._job_counter}"
        self._job_counter += 1
        total = max(1, len(scenes))
        done_count = [0]

        card_data = self._get_or_create_topic_card(topic)
        bar, status_lbl, btn_layout = self._make_job_row(
            card_data['rows_layout'],
            "🎨", "Generating Images & Audio",
            "QProgressBar{background:#313244;border-radius:3px;}"
            "QProgressBar::chunk{background:qlineargradient("
            "x1:0,y1:0,x2:1,y2:0,stop:0 #2563eb,stop:1 #a855f7);border-radius:3px;}",
        )

        open_btn = QPushButton("📂 Open in Storyboard")
        open_btn.setProperty("class", "success-button")
        open_btn.setMinimumHeight(30)
        open_btn.hide()
        history_btn = QPushButton("📚 Open in History")
        history_btn.setProperty("class", "secondary-button")
        history_btn.setMinimumHeight(30)
        history_btn.hide()
        stop_btn = QPushButton("⏹ Stop")
        stop_btn.setProperty("class", "danger-button")
        stop_btn.setMinimumHeight(30)
        btn_layout.addWidget(open_btn)
        btn_layout.addWidget(history_btn)
        btn_layout.addWidget(stop_btn)
        btn_layout.addStretch()

        def on_progress(idx, asset_type, msg):
            try:
                if asset_type == 'aud' and ('done' in msg.lower() or 'skipped' in msg.lower()):
                    done_count[0] = min(done_count[0] + 1, total)
                pct = int(done_count[0] / total * 100)
                bar.setValue(pct)
                status_lbl.setText(f"⏳  {done_count[0]} / {total} scenes done")
            except RuntimeError:
                pass

        def on_done(success, msg):
            try:
                stop_btn.hide()
                if success:
                    bar.setValue(100)
                    status_lbl.setText(f"✅  All {total} scenes ready")
                    status_lbl.setStyleSheet("color:#a6e3a1; font-size:12px;")
                    open_btn.show()
                    history_btn.show()
                else:
                    status_lbl.setText(f"❌  {msg[:80]}")
                    status_lbl.setStyleSheet("color:#f38ba8; font-size:12px;")
            except RuntimeError:
                pass

        thread.scene_progress.connect(on_progress, Qt.QueuedConnection)
        thread.all_done.connect(on_done, Qt.QueuedConnection)
        open_btn.clicked.connect(lambda: self.load_job_requested.emit(scenes, topic))
        history_btn.clicked.connect(lambda: self.open_in_history_requested.emit(topic))
        stop_btn.clicked.connect(lambda: (
            thread.cancel(),
            stop_btn.setEnabled(False),
            stop_btn.setText("⏳ Stopping…"),
        ))

        self._job_refs[job_id] = {
            'thread': thread, 'scenes': scenes, 'topic': topic,
            'bar': bar, 'status_lbl': status_lbl,
        }
        return job_id

    def add_stitch_job(self, thread, topic: str):
        """Register a VideoStitchingThread as a row inside the topic's card."""
        job_id = f"job_{self._job_counter}"
        self._job_counter += 1

        card_data = self._get_or_create_topic_card(topic)
        bar, status_lbl, btn_layout = self._make_job_row(
            card_data['rows_layout'],
            "🎞️", "Stitching Video",
            "QProgressBar{background:#313244;border-radius:3px;}"
            "QProgressBar::chunk{background:qlineargradient("
            "x1:0,y1:0,x2:1,y2:0,stop:0 #a855f7,stop:1 #ec4899);border-radius:3px;}",
        )

        def on_msg(msg):
            try:
                status_lbl.setText(f"⏳  {msg}")
            except RuntimeError:
                pass

        def on_pct(pct):
            try:
                bar.setValue(pct)
            except RuntimeError:
                pass

        def on_done(success, result):
            try:
                if success:
                    bar.setValue(100)
                    status_lbl.setText(f"✅  Done — {os.path.basename(result)}")
                    status_lbl.setStyleSheet("color:#a6e3a1; font-size:12px;")
                else:
                    status_lbl.setText(f"❌  {result[:80]}")
                    status_lbl.setStyleSheet("color:#f38ba8; font-size:12px;")
            except RuntimeError:
                pass

        thread.progress_msg.connect(on_msg, Qt.QueuedConnection)
        thread.progress_pct.connect(on_pct, Qt.QueuedConnection)
        thread.finished.connect(on_done, Qt.QueuedConnection)

        history_btn = QPushButton("📚 Open in History")
        history_btn.setProperty("class", "secondary-button")
        history_btn.setMinimumHeight(30)
        history_btn.clicked.connect(lambda: self.open_in_history_requested.emit(topic))
        btn_layout.addWidget(history_btn)
        btn_layout.addStretch()

        self._job_refs[job_id] = {
            'thread': thread, 'topic': topic,
            'bar': bar, 'status_lbl': status_lbl,
        }
        return job_id


class AppWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Infographic Video Generator  v1.3.0")
        self.resize(1530, 825)

        _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico")
        if os.path.exists(_icon_path):
            self.setWindowIcon(QIcon(_icon_path))
        
        QTimer.singleShot(0, self._apply_dark_title_bar)
        
        # Main Layout
        self.tabs = QTabWidget()
        
        self.script_tab = ScriptTab()
        self.storyboard_tab = StoryboardTab()
        self.export_tab = ExportTab()
        self.history_tab = HistoryTab()
        self.jobs_tab = BackgroundJobsTab()
        self.settings_tab = SettingsTab()
        self.howto_tab = HowToUseTab()

        self.tabs.addTab(self.script_tab, "✍️ Scripting")
        self.tabs.addTab(self.storyboard_tab, "🎨 Storyboard")
        self.tabs.addTab(self.export_tab, "🎬 Export Video")
        self.tabs.addTab(self.history_tab, "📚 History")
        self.tabs.addTab(self.jobs_tab, "⏳ Jobs")
        self.tabs.addTab(self.settings_tab, "⚙️ Settings")
        self.tabs.addTab(self.howto_tab, "❓ How to Use")
        
        self.setCentralWidget(self.tabs)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        
        self.current_scenes = []
        self.current_topic = ""
        self.current_part_label = ''
        self._active_threads = []  # keeps BulkGenerationThread objects alive until done

        self._job_count_timer = QTimer(self)
        self._job_count_timer.setInterval(1000)
        self._job_count_timer.timeout.connect(self._update_jobs_tab_label)
        self._job_count_timer.start()

        self._spinning_labels: dict = {}
        self._spinner_frame = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(500)
        self._spinner_timer.timeout.connect(self._tick_spinner)

        # Connect Signals for Wizard Flow
        self.script_tab.next_requested.connect(self.on_script_next)
        self.script_tab.next_requested_topic.connect(lambda t: setattr(self, 'current_topic', t))
        self.script_tab.next_requested_part_label.connect(lambda p: setattr(self, 'current_part_label', p))
        self.storyboard_tab.back_requested.connect(lambda: self.tabs.setCurrentIndex(0))
        self.storyboard_tab.next_requested.connect(lambda: self.tabs.setCurrentIndex(2))
        self.export_tab.back_requested.connect(lambda: self.tabs.setCurrentIndex(1))
        self.export_tab.start_stitch.connect(self.start_stitching_process)
        # Refresh history list whenever a new script is saved
        self.script_tab.next_requested.connect(lambda _: self.history_tab.load_history())
        # Re-stitching from history
        self.history_tab.restitch_requested.connect(self.on_history_restitch)
        self.storyboard_tab.bulk_job_started.connect(self._on_bulk_job_started)
        self.jobs_tab.load_job_requested.connect(self._on_load_job_in_storyboard)
        self.jobs_tab.open_in_history_requested.connect(self._on_open_job_in_history)

        self._APP_NAME = "Infographic Video Generator"

        # System tray for "done" notifications (works even when app is in background)
        self._tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self)
            self._tray.setIcon(self.style().standardIcon(self.style().SP_ComputerIcon))
            self._tray.setToolTip(self._APP_NAME)
            self._tray.show()

    def _notify(self, title: str, body: str, icon=QSystemTrayIcon.Information, duration_ms: int = 5000, delay_ms: int = 800):
        if not (hasattr(self, '_tray') and self._tray):
            return
        QTimer.singleShot(delay_ms, lambda: self._tray.showMessage(
            f"{self._APP_NAME} — {title}", body, icon, duration_ms
        ))

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_dark_title_bar()

    def _apply_dark_title_bar(self):
        """Request a dark native title bar on supported Windows builds."""
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            if not hwnd:
                return

            set_window_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            dark_mode = ctypes.c_int(1)
            for attr in (20, 19):
                try:
                    set_window_attribute(
                        hwnd,
                        attr,
                        ctypes.byref(dark_mode),
                        ctypes.sizeof(dark_mode),
                    )
                except Exception:
                    pass

            caption_color = ctypes.c_int(0x00202020)
            text_color = ctypes.c_int(0x00F2F2F2)
            for attr, value in ((35, caption_color), (36, text_color)):
                try:
                    set_window_attribute(
                        hwnd,
                        attr,
                        ctypes.byref(value),
                        ctypes.sizeof(value),
                    )
                except Exception:
                    pass
        except Exception:
            pass

    def _current_video_path(self) -> str:
        if not self.current_topic:
            return ""
        safe_topic = make_safe_topic(self.current_topic)
        output_folder = _get_video_output_folder()
        if output_folder:
            custom_path = os.path.join(output_folder, f"{safe_topic}.mp4")
            if os.path.exists(custom_path):
                return custom_path
        return os.path.join(_app_data_dir(), 'assets', safe_topic, f"{safe_topic}.mp4")

    def _start_spin(self, label, text):
        self._spinning_labels[label] = text
        if not self._spinner_timer.isActive():
            self._spinner_timer.start()

    def _stop_spin(self, label):
        self._spinning_labels.pop(label, None)
        if not self._spinning_labels:
            self._spinner_timer.stop()

    def _tick_spinner(self):
        _frames = ["⏳", "⌛"]
        self._spinner_frame = (self._spinner_frame + 1) % len(_frames)
        frame = _frames[self._spinner_frame]
        for label, base_text in list(self._spinning_labels.items()):
            try:
                new_text = frame + base_text[1:] if base_text[:1] in ("⏳", "⌛") else base_text
                label.setText(new_text)
            except RuntimeError:
                self._spinning_labels.pop(label, None)

    def on_script_next(self, scenes):
        self.current_scenes = scenes
        self.storyboard_tab.load_scenes(self.current_scenes, topic=self.current_topic, part_label=self.current_part_label)
        self.tabs.setCurrentIndex(1)
        self.export_tab.reset_ui()
        self.export_tab.populate_thumbnails(self.current_scenes, self._current_video_path())

    def _on_tab_changed(self, index: int):
        if self.tabs.widget(index) is self.export_tab and self.current_scenes:
            self.export_tab.populate_thumbnails(self.current_scenes, self._current_video_path())
        
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
            
        # Ensure output folder is set — ask on first run
        output_folder = _get_video_output_folder()
        if not output_folder:
            chosen = QFileDialog.getExistingDirectory(
                self, "Choose Video Output Folder", os.path.expanduser("~")
            )
            if not chosen:
                return  # user cancelled
            _save_env_key("VIDEO_OUTPUT_FOLDER", chosen)
            _load_env()
            output_folder = chosen

        self.export_tab.start_render_ui()

        safe_topic = make_safe_topic(self.current_topic)
        out_path = os.path.join(output_folder, f"{safe_topic}.mp4")

        self.stitch_thread = VideoStitchingThread(self.current_scenes, out_path, self.storyboard_tab.mobile_friendly_chk.isChecked())
        self.stitch_thread.finished.connect(self.on_stitch_finished)
        self._active_threads.append(self.stitch_thread)
        self.jobs_tab.add_stitch_job(self.stitch_thread, self.current_topic)
        self._update_jobs_tab_label()
        self.stitch_thread.finished.connect(
            lambda ok, msg, t=self.stitch_thread: self._on_any_bulk_job_done(t),
            Qt.QueuedConnection,
        )
        self.stitch_thread.start()
        self.tabs.setCurrentWidget(self.jobs_tab)
        
    def on_stitch_finished(self, success, result_msg):
        self.export_tab.stop_render_ui(success, result_msg)
        
        # Desktop notification — works even if the user has switched tabs
        if success:
            self._notify("Video Ready! 🎬", f"{os.path.basename(result_msg)} has been saved.")
            self.export_tab.populate_thumbnails(self.current_scenes, self._current_video_path())
        else:
            self._notify("Stitching Failed ❌", result_msg[:120], QSystemTrayIcon.Critical)

    def _on_bulk_job_started(self, thread, topic, scenes):
        """Called when StoryboardTab starts a BulkGenerationThread — register it in the Jobs tab."""
        self._active_threads.append(thread)
        self.jobs_tab.add_job(thread, topic, scenes)
        self._update_jobs_tab_label()
        # Remove from active list and update label when this job finishes
        thread.all_done.connect(
            lambda ok, msg, t=thread: self._on_any_bulk_job_done(t),
            Qt.QueuedConnection,
        )

    def _on_any_bulk_job_done(self, thread):
        if thread in self._active_threads:
            self._active_threads.remove(thread)
        self._update_jobs_tab_label()
        self._notify("Generation Done ✅", "A background job finished. Check the Jobs tab.")

    def _update_jobs_tab_label(self):
        self._active_threads = [t for t in self._active_threads if t.isRunning()]
        running = len(self._active_threads)
        idx = self.tabs.indexOf(self.jobs_tab)
        self.tabs.setTabText(idx, f"⏳ Jobs ({running})" if running else "⏳ Jobs")

    def _on_load_job_in_storyboard(self, scenes, topic):
        """Load a completed background job into the Storyboard tab."""
        self.current_scenes = scenes
        self.current_topic = topic
        self.storyboard_tab.load_scenes(scenes, topic=topic)
        self.export_tab.populate_thumbnails(scenes, self._current_video_path())
        self.tabs.setCurrentIndex(1)

    def _on_open_job_in_history(self, topic: str):
        self.history_tab.load_history()
        self.history_tab.select_topic(topic)
        self.tabs.setCurrentIndex(3)

    def on_history_restitch(self, scenes, topic):
        """Triggered by History tab Re-Stitch button — runs stitch in background, reports back inline."""
        output_folder = _get_video_output_folder()
        if not output_folder:
            chosen = QFileDialog.getExistingDirectory(
                self, "Choose Video Output Folder", os.path.expanduser("~")
            )
            if not chosen:
                return
            _save_env_key("VIDEO_OUTPUT_FOLDER", chosen)
            _load_env()
            output_folder = chosen

        safe_topic = make_safe_topic(topic)
        out_path = os.path.join(output_folder, f"{safe_topic}.mp4")

        self._history_stitch_thread = VideoStitchingThread(scenes, out_path, self.storyboard_tab.mobile_friendly_chk.isChecked())
        self._history_stitch_thread.finished.connect(self.on_history_restitch_done)
        self._active_threads.append(self._history_stitch_thread)
        self.jobs_tab.add_stitch_job(self._history_stitch_thread, topic)
        self._update_jobs_tab_label()
        self._history_stitch_thread.finished.connect(
            lambda ok, msg, t=self._history_stitch_thread: self._on_any_bulk_job_done(t),
            Qt.QueuedConnection,
        )
        self._history_stitch_thread.start()
        self.tabs.setCurrentWidget(self.jobs_tab)

    def on_history_restitch_done(self, success, result_msg):
        self.history_tab.finish_restitch(success, result_msg)
        if success:
            self._notify("Re-Stitch Done! 🎬", f"{os.path.basename(result_msg)} saved.")
        else:
            self._notify("Re-Stitch Failed ❌", result_msg[:120], icon=QSystemTrayIcon.Critical)

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
    border: 1px solid #1e1e2e;
    border-radius: 8px;
    background-color: #1e1e2e;
}

QTabBar {
    background: #1e1e2e;
}
QTabBar::tab {
    background: #181825;
    color: #a6adc8;
    padding: 5px 10px;
    min-width: 115px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    margin-right: 2px;
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

QSpinBox {
    background-color: #11111b;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 2px 6px;
    color: #cdd6f4;
    min-width: 56px;
}
QSpinBox:focus {
    border: 1px solid #89b4fa;
}
QSpinBox::up-button, QSpinBox::down-button {
    width: 14px;
    background: #313244;
    border: none;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background: #45475a;
}

QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: none;
    border-radius: 6px;
    padding: 4px 8px;
    font-weight: 500;
}

QPushButton:hover {
    background-color: #4c4f69;
    color: #ffffff;
}

QPushButton:pressed {
    background-color: #6c6f85;
    color: #ffffff;
}

QPushButton:disabled {
    background-color: #232436;
    color: #585b70;
}

/* Specific Button Classes setup via setProperty */
QPushButton[class="primary-button"] {
    background-color: #2563eb;
    color: #ffffff;
    font-weight: bold;
}
QPushButton[class="primary-button"]:hover {
    background-color: #3b82f6;
    color: #ffffff;
}
QPushButton[class="primary-button"]:pressed {
    background-color: #1d4ed8;
}
QPushButton[class="primary-button"]:disabled {
    background-color: #1e3a6e;
    color: #6b8cc7;
}

QPushButton[class="secondary-button"] {
    background-color: #45475a;
    color: #cdd6f4;
    font-weight: bold;
}
QPushButton[class="secondary-button"]:hover {
    background-color: #5c5f77;
    color: #ffffff;
}
QPushButton[class="secondary-button"]:pressed {
    background-color: #6c6f85;
}
QPushButton[class="secondary-button"]:disabled {
    background-color: #2c2d3a;
    color: #585b70;
}

QPushButton[class="success-button"] {
    background-color: #16a34a;
    color: #ffffff;
    font-weight: bold;
}
QPushButton[class="success-button"]:hover {
    background-color: #22c55e;
    color: #ffffff;
}
QPushButton[class="success-button"]:pressed {
    background-color: #15803d;
}
QPushButton[class="success-button"]:disabled {
    background-color: #14532d;
    color: #4ade80;
}

QPushButton[class="danger-button"] {
    background-color: #dc2626;
    color: #ffffff;
    font-weight: bold;
}
QPushButton[class="danger-button"]:hover {
    background-color: #ef4444;
    color: #ffffff;
}
QPushButton[class="danger-button"]:pressed {
    background-color: #b91c1c;
}
QPushButton[class="danger-button"]:disabled {
    background-color: #7f1d1d;
    color: #fca5a5;
}

/* Compact buttons used inside scene cards */
QPushButton[class="scene-button"] {
    background-color: #313244;
    color: #cdd6f4;
    font-weight: bold;
    padding: 4px 6px;
}
QPushButton[class="scene-button"]:hover {
    background-color: #45475a;
    color: #ffffff;
}
QPushButton[class="scene-button"]:pressed {
    background-color: #585b70;
}
QPushButton[class="scene-button"]:disabled {
    background-color: #232436;
    color: #585b70;
}
"""

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("AI Video Generator starting up")
    logger.info("Log file: %s", logger.handlers[0].baseFilename)
    logger.info("=" * 60)

    logger.info("Initialising database...")
    db.init_db()
    logger.info("Database ready.")

    logger.info("Launching Qt application...")
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    _app_icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico")
    if os.path.exists(_app_icon_path):
        app.setWindowIcon(QIcon(_app_icon_path))
    window = AppWindow()
    window.show()
    logger.info("Window shown. App is ready.")

    def _check_api_key():
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not gemini_key or gemini_key == "your_gemini_api_key_here":
            logger.warning("GEMINI_API_KEY not set — showing API key dialog.")
            dlg = ApiKeyDialog(window)
            dlg.exec_()
            _load_env()
        else:
            logger.info("GEMINI_API_KEY detected (key length=%d).", len(gemini_key))

    QTimer.singleShot(300, _check_api_key)
    exit_code = app.exec_()
    logger.info("App exited with code %d.", exit_code)
    sys.exit(exit_code)

