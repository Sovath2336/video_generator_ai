import base64
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import wave
from difflib import SequenceMatcher

import imageio_ffmpeg
import requests
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv

# Resolve writable app-data directory (same logic as main.py)
def _ai_app_data_dir() -> str:
    if getattr(sys, 'frozen', False):
        base = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')),
                            'InfographicVideoGenerator')
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(base, exist_ok=True)
    return base

# Load .env from the writable user-data location
_env_path = os.path.join(_ai_app_data_dir(), '.env')
load_dotenv(_env_path, override=True)

# File-based logger — works in frozen EXE where stdout is invisible
def _setup_logger() -> logging.Logger:
    _log = logging.getLogger("video_generator_ai")
    if _log.handlers:
        return _log
    _log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    _log_path = os.path.join(_ai_app_data_dir(), "app.log")
    fh = logging.FileHandler(_log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    _log.addHandler(fh)
    # Mirror all messages to the terminal so the dev can see live activity
    if not getattr(sys, 'frozen', False):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        _log.addHandler(sh)
    return _log

logger = _setup_logger()

# Thread-safe genai.Client cache — avoids creating a new HTTP connection pool on every call.
# genai.Client is stateless (no session tokens), so sharing across threads is safe.
_genai_client_cache: dict[str, genai.Client] = {}
_genai_client_lock = threading.Lock()

def _get_genai_client(api_key: str) -> genai.Client:
    with _genai_client_lock:
        if api_key not in _genai_client_cache:
            _genai_client_cache[api_key] = genai.Client(api_key=api_key)
        return _genai_client_cache[api_key]

# Resolve ffmpeg — in a frozen EXE use a writable temp alias dir
_ffmpeg_exe = None
try:
    _ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    _ffmpeg_dir = os.path.dirname(_ffmpeg_exe)
    if _ffmpeg_dir and _ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
    # pydub needs a binary literally named ffmpeg(.exe).
    if getattr(sys, 'frozen', False):
        _alias_dir = os.path.join(tempfile.gettempdir(), "InfographicVideoGen_ffmpeg")
    else:
        _alias_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ffmpeg_bin")
    os.makedirs(_alias_dir, exist_ok=True)
    _alias_exe = os.path.join(_alias_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not os.path.exists(_alias_exe):
        shutil.copy2(_ffmpeg_exe, _alias_exe)
    if _alias_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _alias_dir + os.pathsep + os.environ.get("PATH", "")
    os.environ["FFMPEG_BINARY"] = _alias_exe
except Exception:
    _ffmpeg_exe = None

from pydub import AudioSegment
import pydub.utils as _pydub_utils

# Suppress console window for pydub's ffmpeg subprocesses on Windows
if os.name == "nt":
    import subprocess as _subprocess
    _orig_pydub_Popen = _subprocess.Popen
    def _silent_Popen(*args, **kwargs):
        kwargs.setdefault("creationflags", 0)
        kwargs["creationflags"] |= 0x08000000  # CREATE_NO_WINDOW
        return _orig_pydub_Popen(*args, **kwargs)
    _subprocess.Popen = _silent_Popen
    _pydub_utils.Popen = _silent_Popen

# Ensure pydub can discover ffmpeg without requiring system PATH setup.
try:
    if _ffmpeg_exe:
        AudioSegment.converter = _ffmpeg_exe
        AudioSegment.ffmpeg = _ffmpeg_exe
except Exception:
    pass

def correct_topic_title(topic: str) -> str:
    """
    Uses Gemini Flash to fix typos, grammar, and capitalization in a user-entered topic title.
    Returns the corrected title, or the original string if correction fails for any reason.
    Fast (~1s) because it uses a lightweight model with a tiny prompt.
    """
    if not topic or not topic.strip():
        return topic
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_gemini_api_key_here":
        return topic
    try:
        client = _get_genai_client(api_key)
        result = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=(
                "Fix any spelling mistakes, grammar errors, and capitalization in this video topic title. "
                "Return ONLY the corrected title — no quotes, no explanation, no punctuation at the end.\n\n"
                f"Topic: {topic}"
            ),
        )
        corrected = (result.text or "").strip().strip('"').strip("'").strip()
        if corrected:
            logger.info("Topic corrected: %r -> %r", topic, corrected)
            return corrected
    except Exception as e:
        logger.warning("correct_topic_title failed (using original): %s", e)
    return topic


def generate_script_from_topic(topic: str, duration_minutes: int = 5, use_web_search: bool = False, part_info: dict | None = None, ignore_number: bool = False):
    """
    Uses Gemini 2.5 Pro to generate a structured infographic script from a user-provided topic.
    When use_web_search=True, activates Gemini's native Google Search grounding so it
    fetches live, up-to-date web results before writing the script.
    Yields the response in chunks for streaming.
    """
    logger.info("generate_script_from_topic: topic=%r, duration=%dm, web_search=%s",
                topic, duration_minutes, use_web_search)
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == 'your_gemini_api_key_here':
        logger.error("generate_script_from_topic: GEMINI_API_KEY not set.")
        yield "[Error]: A valid GEMINI_API_KEY was not found in the .env file. Please add your API key."
        return

    try:
        client = genai.Client(api_key=api_key)

        estimated_scenes_min = max(6, int(duration_minutes * 1.5) + 1)
        estimated_scenes_max = max(8, int(duration_minutes * 2) + 2)

        # If the topic contains a specific number (e.g. "Top 5 Ways", "7 Reasons"),
        # lock the scene count to match: 1 title card + N main content + 1 CTA outro.
        _topic_number_match = re.search(r'\b(\d+)\b', topic)
        _topic_number = None
        if not ignore_number and _topic_number_match:
            n = int(_topic_number_match.group(1))
            if 2 <= n <= 20:
                _topic_number = n
                estimated_scenes_min = estimated_scenes_max = n + 2
                logger.info("Topic number detected: %d → forcing %d scenes total.", n, n + 2)

        if part_info is not None:
            _topic_number = part_info['items_per_part']
            estimated_scenes_min = estimated_scenes_max = _topic_number + 2
            logger.info(
                "Part generation: Part %d of %d, items %d-%d, forcing %d scenes.",
                part_info['current'], part_info['total'],
                part_info['item_start'], part_info['item_end'],
                _topic_number + 2,
            )

        words_per_scene = int((duration_minutes * 140) / ((estimated_scenes_min + estimated_scenes_max) / 2))
        logger.info("Script plan: %d-%d scenes, ~%d words/scene",
                    estimated_scenes_min, estimated_scenes_max, words_per_scene)

        web_note = (
            "You have access to Google Search. Before writing, SEARCH the web for the most "
            "recent, accurate, and detailed information about this topic. Use real facts, "
            "statistics, and up-to-date information found in search results."
            if use_web_search else
            "Use your own extensive knowledge to write accurate content."
        )

        if ignore_number:
            web_note += (
                "\n\nIMPORTANT: The topic title may contain a number but you must NOT try to cover every "
                "individual item in a separate scene. Summarize and group related items to fit all content "
                "within the target duration. Prioritise depth and narrative flow over exhaustive enumeration."
            )

        _part_note = ""
        if part_info is not None:
            _part_note = (
                f"\n\nSERIES CONTEXT: This script is Part {part_info['current']} of {part_info['total']} "
                f"in a multi-part series about \"{part_info['base_topic']}\".\n"
                f"Cover ONLY items {part_info['item_start']} through {part_info['item_end']} "
                f"(out of {part_info['topic_number']} total). Do NOT cover items outside this range.\n"
                f"The title card (Scene 1) visual prompt must mention that this is "
                f"Part {part_info['current']} of {part_info['total']}.\n"
                f"Each of the {_topic_number} main content scenes must cover exactly ONE item "
                f"from items {part_info['item_start']}–{part_info['item_end']}."
            )

        cta_visual = _cta_visual_instruction()

        _scenes_instruction = (
            f"SCENES: Generate exactly {estimated_scenes_min} scenes "
            f"(1 title card intro + {_topic_number} main content scenes + 1 CTA outro). "
            f"The {_topic_number} main content scenes must each cover one of the {_topic_number} items implied by the title."
            if _topic_number is not None
            else f"SCENES: Generate between {estimated_scenes_min} and {estimated_scenes_max} scenes."
        )

        prompt = f"""You are an expert YouTube infographic video scriptwriter with a talent for cinematic storytelling and audience retention.
{web_note}{_part_note}

Write a complete, YouTube-ready infographic video script about: "{topic}"

TARGET: Exactly {duration_minutes} minutes of narration (~{duration_minutes * 140} words total across all scenes).
{_scenes_instruction}
NARRATION PER SCENE: Each main content scene must have ~{words_per_scene} words of narration (minimum {int(words_per_scene * 0.8)}, maximum {int(words_per_scene * 1.2)}). Do NOT write short 1-2 sentence narrations. Each scene should feel like a full paragraph of documentary narration.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANDATORY SCENE STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[Scene 1] — BRIEF TITLE CARD INTRO (mandatory, separate intro scene)
Visual: A bold, cinematic title card image. The ONLY readable text allowed in this image is the exact title "{topic}" as large stylised text centred on screen. Do NOT include any subtitles, taglines, descriptions, episode numbers, dates, bylines, captions, or any other words, letters, or typography beyond the title itself. Set the title against a dramatic thematic background that immediately communicates the subject. The image should feel like a movie poster or YouTube thumbnail — eye-catching, high contrast, professional. STRICT RULE: title text only, nothing else written anywhere in the image.
Narration: Keep this separate intro extremely brief: 0-1 very short sentence, about 3-12 words total. This scene is only the title-card intro. Do NOT begin the main explanation yet.

[Scene 2] — HOOK & CONTENT INTRO (mandatory, first real content scene)
Visual: Create a vivid, story-driven image that directly depicts the first real narrated idea of the video. It should feel distinct from the title card and immediately launch the viewer into the subject matter.
Narration: This is where the real narration begins. Open with a powerful hook — a surprising fact, a bold question, or a provocative statement about {topic} that grabs attention fast. Then briefly introduce what the video will cover.

[Scene 3 … Scene N-1] — MAIN CONTENT SCENES (the story)
Each scene must:
• Flow naturally FROM the previous scene and INTO the next, like chapters in a book.
• Build on the last idea — use transition phrases ("But here's where it gets interesting…", "This leads us to…", "Now consider this…").
• Cover one focused idea, fact, or narrative beat — not a list dump.
- Have a visual that directly and vividly illustrates that scene's core narration idea (no generic stock-photo concepts, no random symbolic filler).
• Have narration that feels conversational, curious, and authoritative — like a great documentary. Write full, rich paragraphs (~{words_per_scene} words). Do NOT write only 1-2 sentences per scene.
• The final MAIN CONTENT scene must end the educational/story content naturally before any call to action appears.
• Do NOT ask viewers to like, subscribe, or hit the bell in any main content scene.
• Do NOT use the title card visual style again after Scene 1.

[Scene N] — BRIEF CALL TO ACTION OUTRO (mandatory, separate final scene)
{cta_visual}
Narration: This must be a very short, separate outro after all main content is finished. Keep it brief: 1-2 short sentences, about 8-20 words total. Thank the viewer briefly and ask them to like, subscribe, and hit the bell. Do not repeat the main explanation here, and do not let this scene replace the final educational/content scene.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMAT RULES (follow exactly)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[Scene X]
Visual: (1-2 vivid sentences for an AI image generator. Describe lighting, mood, composition, colours. No generic descriptions. No readable text, letters, captions, or typography except the mandatory title in Scene 1.)
Narration: (Exact voiceover text. Conversational tone. Each scene's narration must connect logically to the next.)

CRITICAL:
- Begin IMMEDIATELY with [Scene 1]. No preamble, no meta-commentary, no greetings.
- Scene 1 must be a separate title-card intro, not the first main content scene.
- The first real content narration starts in Scene 2.
- The subscribe/call-to-action must appear only in the final brief outro scene.
- The final main content scene must remain a real content scene, not a subscribe scene."""

        # Build generation config — add Google Search tool if requested
        config = None
        if use_web_search:
            logger.info("Web search grounding: ON — Gemini will query Google Search before writing.")
            config = genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())]
            )
        else:
            logger.info("Web search grounding: OFF — using Gemini knowledge only.")

        logger.info("Calling gemini-3-flash-preview (streaming)...")
        chunk_count = 0
        response = client.models.generate_content_stream(
            model='gemini-3-flash-preview',
            contents=prompt,
            config=config,
        )
        for chunk in response:
            if chunk.text:
                chunk_count += 1
                yield chunk.text
        logger.info("Script stream complete (%d chunks received).", chunk_count)
    except Exception as e:
        logger.exception("generate_script_from_topic failed: %s", e)
        yield f"[Error occurred during Gemini 2.5 generation]:\n{str(e)}"

def analyze_text_to_scenes(source_text: str):
    """
    Takes arbitrary user-pasted text (article, blog, essay, etc.) and uses
    Gemini 2.5 Pro to intelligently split it into infographic video scenes.
    Yields the response in chunks for streaming.
    """
    logger.info("analyze_text_to_scenes: source_text length=%d chars", len(source_text))
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == 'your_gemini_api_key_here':
        logger.error("analyze_text_to_scenes: GEMINI_API_KEY not set.")
        yield "[Error]: A valid GEMINI_API_KEY was not found in the .env file."
        return

    try:
        client = genai.Client(api_key=api_key)
        cta_visual = _cta_visual_instruction()

        prompt = f"""You are an expert YouTube infographic video scriptwriter and content analyst with a talent for cinematic storytelling.

A user has provided the following source text:

\"\"\"
{source_text}
\"\"\"

Your task: transform this source text into a polished, YouTube-ready infographic video script with a clear narrative arc from start to finish.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANDATORY SCENE STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[Scene 1] — BRIEF TITLE CARD INTRO (mandatory, separate intro scene)
Derive the video title from the source text's main subject.
Visual: A bold, cinematic title card image. Show the topic title as large stylised text centred on screen, set against a dramatic thematic background that immediately communicates the subject - like a movie poster or YouTube thumbnail. Eye-catching, high contrast, professional.
Narration: Keep this separate intro extremely brief: 0-1 very short sentence, about 3-12 words total. This scene is only the title-card intro. Do NOT begin the main explanation yet.

[Scene 2] — HOOK & CONTENT INTRO (mandatory, first real content scene)
Visual: Create a vivid, story-driven image that directly depicts the first real narrated idea from the source text. It should feel distinct from the title card and immediately launch the viewer into the subject matter.
Narration: This is where the real narration begins. Open with a powerful hook — a surprising fact, bold question, or provocative statement drawn from the source text that grabs attention fast. Then briefly introduce what the video will reveal.

[Scene 3 … Scene N-1] — MAIN CONTENT SCENES (the story)
• Extract the key ideas, facts, and narrative beats from the source text.
• Order them so each scene flows naturally FROM the previous and INTO the next — use bridging phrases ("But here's the twist…", "This brings us to…", "Now consider…").
• One focused idea per scene — no list dumps.
- Visuals must directly illustrate the core idea of that scene's narration (vivid lighting, mood, composition, colours - no generic concepts, no random symbolic filler). No readable text, letters, captions, or typography.
• Narration must be conversational, curious, and authoritative — faithful to the source text's facts. Write full rich paragraphs of 60-100 words per scene. Do NOT write short 1-2 sentence narrations.
• The final MAIN CONTENT scene must finish the actual explanation before any call to action appears.
• Do NOT ask viewers to like, subscribe, or hit the bell in any main content scene.
• Do NOT use the title card visual style again after Scene 1.

[Scene N] — BRIEF CALL TO ACTION OUTRO (mandatory, separate final scene)
{cta_visual}
Narration: This must be a very short, separate outro after all main content is finished. Keep it brief: 1-2 short sentences, about 8-20 words total. Thank the viewer briefly and ask them to like, subscribe, and hit the bell. Do not continue the main explanation here, and do not let this scene replace the final content scene.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMAT RULES (follow exactly)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[Scene X]
Visual: (1-2 vivid sentences for an AI image generator. Describe lighting, mood, composition, colours. No generic descriptions. No readable text, letters, captions, or typography except the mandatory title in Scene 1.)
Narration: (Exact voiceover text. Conversational tone. Must connect logically to the next scene.)

CRITICAL STRICT RULE: Begin IMMEDIATELY with [Scene 1]. Do NOT include ANY introductory text, greetings, or meta-commentary. Output ONLY the scene blocks.
Scene 1 must be a separate title-card intro, and the first real content narration starts in Scene 2.
The subscribe/call-to-action must appear only in the final brief outro scene, never inside the main content scenes.
"""

        logger.info("Calling gemini-2.5-pro for text-to-scenes (streaming)...")
        chunk_count = 0
        response = client.models.generate_content_stream(
            model='gemini-2.5-pro',
            contents=prompt,
        )
        for chunk in response:
            if chunk.text:
                chunk_count += 1
                yield chunk.text
        logger.info("Text-to-scenes stream complete (%d chunks received).", chunk_count)
    except Exception as e:
        logger.exception("analyze_text_to_scenes failed: %s", e)
        yield f"[Error during text analysis]:\n{str(e)}"

def _is_cta_image_prompt(prompt: str) -> bool:
    """Heuristic to detect the short subscribe/outro scene prompt."""
    text = (prompt or "").lower()
    cta_markers = [
        "subscribe button",
        "notification bell",
        "like, subscribe",
        "like and subscribe",
        "call to action",
        "closing image",
        "confetti",
        "shared outro image",
        "reuse the existing shared outro image",
        "reuse the existing shared outro image.",
    ]
    return sum(1 for marker in cta_markers if marker in text) >= 2


def _shared_cta_image_path(mobile_friendly: bool = False) -> str:
    assets_dir = os.path.join(_ai_app_data_dir(), "assets", "Outro Image")
    os.makedirs(assets_dir, exist_ok=True)
    suffix = "portrait" if mobile_friendly else "landscape"
    return os.path.join(assets_dir, f"cta_scene_{suffix}.jpg")


def _cta_visual_instruction() -> str:
    return (
        "Visual: A vibrant, warm closing image ? a glowing YouTube subscribe button "
        "with a notification bell, surrounded by dynamic abstract shapes or confetti "
        "in the video's colour palette. Feels celebratory and inviting."
    )

def _normalize_image_text(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip().strip(' "\'')
    return text[:48]


def _is_title_card_prompt(prompt: str) -> bool:
    text = (prompt or "").lower()
    return "title card" in text or "topic title" in text


def _build_image_prompt(prompt: str, image_text: str | None = None, narration: str | None = None, force_title_card: bool = False, mobile_friendly: bool = True) -> str:
    aspect = "9:16" if mobile_friendly else "16:9"
    base_prompt = f"Generate an image with {aspect} aspect ratio of: {prompt}"
    image_text = _normalize_image_text(image_text)
    narration = re.sub(r"\s+", " ", (narration or "")).strip()
    if (force_title_card or _is_title_card_prompt(prompt)) and image_text:
        clean = re.sub(r'[Ii]n the cent(?:er|re),?\s+the (?:exact )?text\s+["\'].*?["\'][^.]*\.?', '', prompt)
        clean = re.sub(r'[Tt]he (?:exact )?text\s+["\'].*?["\'][^.]*\.?', '', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        base_prompt = f"Generate an image with {aspect} aspect ratio of: {clean}"
        return (
            f"{base_prompt}\n\n"
            f'Important: the ONLY text that may appear anywhere in this image is the exact title: "{image_text}". '
            "Render it large, bold, and centred as the sole typographic element. "
            "Do NOT add any subtitles, taglines, descriptions, captions, bylines, episode numbers, dates, "
            "or any other words, letters, or symbols beyond the title itself."
        )
    core_idea_note = ""
    if narration:
        core_idea_note = (
            f' Core narration idea to depict exactly: "{narration[:280]}". '
            "The image must directly visualize that main idea, subject, action, or event. "
            "Do not use random decorative imagery, generic symbolism, or loosely related stock-photo concepts. "
            "Choose one clear focal subject that best represents the narration."
        )
    return (
        f"{base_prompt}\n\n"
        "Important: do not include any readable text, letters, captions, logos, UI labels, "
        f"or typography in the image.{core_idea_note}"
    )


_VALID_IMAGE_SIZES = {"512", "1K", "2K", "4K"}

def generate_image_from_prompt(prompt: str, output_path: str, overlay_text: str | None = None, narration: str | None = None, is_title_card: bool = False, mobile_friendly: bool = True) -> bool:
    """
    Uses Imagen 3 via the Gemini API to generate an image and save it to output_path.
    `overlay_text` is only used for intro title-card scenes. Other scenes stay text-free.
    `narration` is used to anchor the image to the scene's core idea.
    `is_title_card` forces the overlay_text to be embedded as the title regardless of the visual prompt wording.
    Returns True if successful, False otherwise.
    """
    scene_label = os.path.basename(output_path)
    if _is_cta_image_prompt(prompt):
        shared_cta_path = _shared_cta_image_path(mobile_friendly)
        try:
            if os.path.exists(shared_cta_path) and os.path.getsize(shared_cta_path) > 0:
                logger.info("[%s] CTA image: reusing cached outro image.", scene_label)
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                shutil.copy2(shared_cta_path, output_path)
                return True
        except Exception as e:
            logger.warning("CTA cache copy failed (will regenerate): %s", e)

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == 'your_gemini_api_key_here':
        logger.warning("generate_image_from_prompt: GEMINI_API_KEY not set, skipping.")
        return False

    # Validate IMAGE_RESOLUTION — strip whitespace and normalize case
    raw_res = os.getenv("IMAGE_RESOLUTION", "1K").strip().upper()
    if raw_res not in _VALID_IMAGE_SIZES:
        logger.error(
            "IMAGE_RESOLUTION '%s' is invalid (must be one of %s). Falling back to '1K'.",
            raw_res, sorted(_VALID_IMAGE_SIZES),
        )
        raw_res = "1K"
    # The API accepts "1K"/"2K"/"4K" in uppercase and "512" as-is
    image_size = raw_res

    logger.info("[%s] Generating image — size=%s, prompt=%r...", scene_label, image_size, prompt[:60])
    try:
        client = _get_genai_client(api_key)
        result = None
        for _attempt in range(4):
            try:
                result = client.models.generate_content(
                    model='gemini-3.1-flash-image-preview',
                    contents=_build_image_prompt(prompt, overlay_text, narration, force_title_card=is_title_card, mobile_friendly=mobile_friendly),
                    config=genai_types.GenerateContentConfig(
                        response_modalities=['TEXT', 'IMAGE'],
                        image_config=genai_types.ImageConfig(
                            aspect_ratio='9:16' if mobile_friendly else '16:9',
                            image_size=image_size,
                        ),
                    ),
                )
                break
            except Exception as _e:
                _msg = str(_e)
                if ("429" in _msg or "RESOURCE_EXHAUSTED" in _msg or "quota" in _msg.lower()) and _attempt < 3:
                    _wait = 2 ** _attempt
                    logger.warning("[%s] Image rate limited (attempt %d/4), retrying in %ds...",
                                   scene_label, _attempt + 1, _wait)
                    time.sleep(_wait)
                    continue
                raise
        if result is None:
            logger.error("[%s] Image generation exhausted all retries.", scene_label)
            return False
        # Returns images inline in parts[].inline_data (TEXT+IMAGE modalities may both appear)
        if result.candidates and result.candidates[0].content.parts:
            for part in result.candidates[0].content.parts:
                if hasattr(part, 'inline_data') and part.inline_data and part.inline_data.data:
                    byte_size = len(part.inline_data.data)
                    os.makedirs(os.path.dirname(output_path), exist_ok=True)
                    with open(output_path, "wb") as f:
                        f.write(part.inline_data.data)
                    logger.info("[%s] Image saved — %d bytes -> %s", scene_label, byte_size, output_path)
                    if _is_cta_image_prompt(prompt):
                        try:
                            shutil.copy2(output_path, _shared_cta_image_path(mobile_friendly))
                        except Exception as e:
                            logger.warning("CTA cache save failed: %s", e)
                    return True
            # API responded but contained no image data — log what we actually got
            parts_summary = [
                f"part[{i}]: inline_data={'yes' if (hasattr(p, 'inline_data') and p.inline_data) else 'no'}, text={repr(getattr(p, 'text', None))[:80] if hasattr(p, 'text') else 'n/a'}"
                for i, p in enumerate(result.candidates[0].content.parts)
            ]
            finish_reason = getattr(result.candidates[0], 'finish_reason', 'unknown')
            logger.error(
                "Image API returned no inline image data. finish_reason=%s, parts=[%s]",
                finish_reason, "; ".join(parts_summary),
            )
        else:
            finish_reason = getattr(result.candidates[0], 'finish_reason', 'unknown') if result.candidates else 'no_candidates'
            logger.error(
                "Image API returned empty candidates/parts. finish_reason=%s", finish_reason,
            )

    except Exception as e:
        logger.exception("generate_image_from_prompt failed (prompt=%r, size=%s): %s", prompt[:80], image_size, e)

    logger.warning("[%s] Image generation FAILED.", scene_label)
    return False


def _subtitle_word_items(text: str):
    """Return a stable list of spoken-word tokens derived from narration text."""
    return [
        {
            "index": idx,
            "text": match.group(0),
            "spoken": match.group(0).replace("’", "'").lower(),
        }
        for idx, match in enumerate(re.finditer(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*", text or ""))
    ]


def _word_timing_sidecar_path(audio_path: str) -> str:
    base, _ = os.path.splitext(audio_path)
    return base + ".words.json"


def _audio_duration_seconds(audio_path: str) -> float:
    return max(0.0, len(AudioSegment.from_file(audio_path)) / 1000.0)


def _parse_google_time_offset(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if text.endswith("s"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return 0.0




def _estimate_word_timings(script_text: str, audio_path: str):
    """Fallback timing model when cloud word alignment is unavailable."""
    script_words = _subtitle_word_items(script_text)
    if not script_words:
        return []
    duration_sec = _audio_duration_seconds(audio_path)
    total_words = len(script_words)
    if duration_sec <= 0 or total_words == 0:
        return None

    step = duration_sec / total_words
    timings = []
    for idx, word in enumerate(script_words):
        start_sec = step * idx
        end_sec = duration_sec if idx == total_words - 1 else step * (idx + 1)
        timings.append(
            {
                "index": idx,
                "text": word["text"],
                "spoken": word["spoken"],
                "start_sec": start_sec,
                "end_sec": max(end_sec, start_sec),
            }
        )
    return timings


def _interpolate_missing_timings(script_words, aligned_words, duration_sec: float):
    """
    Fill in unmatched words by interpolating between surrounding aligned anchors.
    """
    resolved = list(aligned_words)
    total = len(resolved)

    idx = 0
    while idx < total:
        if resolved[idx] is not None:
            idx += 1
            continue

        run_start = idx
        while idx < total and resolved[idx] is None:
            idx += 1
        run_end = idx

        prev_word = resolved[run_start - 1] if run_start > 0 else None
        next_word = resolved[run_end] if run_end < total else None

        start_bound = prev_word["end_sec"] if prev_word else 0.0
        end_bound = next_word["start_sec"] if next_word else duration_sec
        if end_bound < start_bound:
            end_bound = start_bound

        span = max(end_bound - start_bound, 0.0)
        count = run_end - run_start
        step = span / count if count else 0.0

        for offset, word_idx in enumerate(range(run_start, run_end)):
            start_sec = start_bound + (step * offset)
            end_sec = end_bound if word_idx == run_end - 1 else start_bound + (step * (offset + 1))
            resolved[word_idx] = {
                "index": word_idx,
                "text": script_words[word_idx]["text"],
                "spoken": script_words[word_idx]["spoken"],
                "start_sec": start_sec,
                "end_sec": max(end_sec, start_sec),
            }

    for i, word in enumerate(resolved):
        word["start_sec"] = max(0.0, min(float(word["start_sec"]), duration_sec))
        word["end_sec"] = max(word["start_sec"], min(float(word["end_sec"]), duration_sec))
        if i < total - 1:
            next_start = max(word["start_sec"], resolved[i + 1]["start_sec"])
            word["end_sec"] = max(word["start_sec"], min(word["end_sec"], next_start))
    if resolved:
        resolved[-1]["end_sec"] = max(resolved[-1]["end_sec"], duration_sec)
    return resolved


def _align_script_words_to_offsets(script_text: str, recognized_words, duration_sec: float):
    script_words = _subtitle_word_items(script_text)
    if not script_words:
        return []
    if not recognized_words:
        return None

    script_spoken = [item["spoken"] for item in script_words]
    recognized_spoken = [item["spoken"] for item in recognized_words]
    matcher = SequenceMatcher(a=script_spoken, b=recognized_spoken, autojunk=False)

    aligned = [None] * len(script_words)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rec = recognized_words[j1 + offset]
                aligned[i1 + offset] = {
                    "index": i1 + offset,
                    "text": script_words[i1 + offset]["text"],
                    "spoken": script_words[i1 + offset]["spoken"],
                    "start_sec": rec["start_sec"],
                    "end_sec": rec["end_sec"],
                }
        elif tag == "replace":
            pair_count = min(i2 - i1, j2 - j1)
            for offset in range(pair_count):
                rec = recognized_words[j1 + offset]
                aligned[i1 + offset] = {
                    "index": i1 + offset,
                    "text": script_words[i1 + offset]["text"],
                    "spoken": script_words[i1 + offset]["spoken"],
                    "start_sec": rec["start_sec"],
                    "end_sec": rec["end_sec"],
                }

    if not any(word is not None for word in aligned):
        return None
    return _interpolate_missing_timings(script_words, aligned, duration_sec)


def ensure_word_timing_data(script_text: str, audio_path: str, force: bool = False):
    """
    Return per-word timing metadata for exported subtitles, generating a cached sidecar if needed.
    """
    script_words = _subtitle_word_items(script_text)
    if not script_words:
        return []

    sidecar_path = _word_timing_sidecar_path(audio_path)
    audio_size = os.path.getsize(audio_path) if audio_path and os.path.exists(audio_path) else None

    if not force and os.path.exists(sidecar_path):
        try:
            with open(sidecar_path, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if (
                cached.get("script_text") == script_text
                and cached.get("audio_size") == audio_size
                and isinstance(cached.get("words"), list)
                and len(cached["words"]) == len(script_words)
            ):
                return cached["words"]
        except Exception:
            pass

    aligned_words = None

    if aligned_words is None:
        aligned_words = _estimate_word_timings(script_text, audio_path)
    if aligned_words is None:
        return None

    payload = {
        "script_text": script_text,
        "audio_path": audio_path,
        "audio_size": audio_size,
        "duration_sec": max((word.get("end_sec", 0.0) for word in aligned_words), default=0.0),
        "words": aligned_words,
    }
    try:
        with open(sidecar_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except Exception:
        pass
    return aligned_words

def _parse_audio_mime(mime_type: str):
    """
    Parse minimal audio mime metadata from Gemini inline_data.mime_type.
    Returns (mime_lower, sample_rate, channels).
    """
    mime = (mime_type or "").lower()
    rate_match = re.search(r"rate=(\d+)", mime)
    channels_match = re.search(r"channels=(\d+)", mime)
    sample_rate = int(rate_match.group(1)) if rate_match else 24000
    channels = int(channels_match.group(1)) if channels_match else 1
    return mime, sample_rate, channels

def _save_gemini_audio_bytes(audio_bytes: bytes, mime_type: str, output_path: str) -> bool:
    """
    Save Gemini audio bytes to a playable file.
    - audio/L16 (PCM) is wrapped into WAV.
    - Compressed formats are written as-is.
    """
    mime, sample_rate, channels = _parse_audio_mime(mime_type)

    if "audio/l16" in mime or "audio/pcm" in mime:
        with wave.open(output_path, "wb") as wav_out:
            wav_out.setnchannels(max(1, channels))
            wav_out.setsampwidth(2)  # 16-bit PCM
            wav_out.setframerate(sample_rate)
            wav_out.writeframes(audio_bytes)
        return True

    with open(output_path, "wb") as out:
        out.write(audio_bytes)
    return True


def generate_audio_from_text(text: str, output_path: str, engine: str = "gemini", voice: str | None = None) -> bool:
    import traceback as _tb
    scene_label = os.path.basename(output_path)
    try:
        if engine == "gemini":
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key or api_key == 'your_gemini_api_key_here':
                logger.warning("[%s] Audio: GEMINI_API_KEY not set, skipping.", scene_label)
                return False

            try:
                import time as _time
                client = _get_genai_client(api_key)
                tts_model = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
                tts_voice = voice or os.getenv("GEMINI_TTS_VOICE", "puck")
                logger.info("[%s] Generating audio — model=%s, voice=%s, text_len=%d chars",
                            scene_label, tts_model, tts_voice, len(text))

                response = None
                for _attempt in range(4):
                    try:
                        response = client.models.generate_content(
                            model=tts_model,
                            contents=text,
                            config=genai_types.GenerateContentConfig(
                                response_modalities=["audio"],
                                speech_config=genai_types.SpeechConfig(
                                    voice_config=genai_types.VoiceConfig(
                                        prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                                            voice_name=tts_voice
                                        )
                                    ),
                                    language_code="en-US",
                                )
                            )
                        )
                        break
                    except Exception as _e:
                        _msg = str(_e)
                        if "500" in _msg or "503" in _msg or "INTERNAL" in _msg or "UNAVAILABLE" in _msg:
                            if _attempt < 3:
                                _wait = 2 ** _attempt
                                logger.warning("[%s] TTS server error (attempt %d), retrying in %ds...",
                                               scene_label, _attempt + 1, _wait)
                                _time.sleep(_wait)
                                continue
                        raise

                if response and response.candidates and response.candidates[0].content.parts:
                    for part in response.candidates[0].content.parts:
                        if hasattr(part, 'inline_data') and part.inline_data and part.inline_data.data:
                            byte_size = len(part.inline_data.data)
                            result = _save_gemini_audio_bytes(
                                part.inline_data.data,
                                getattr(part.inline_data, "mime_type", ""),
                                output_path,
                            )
                            if result:
                                logger.info("[%s] Audio saved — %d bytes -> %s", scene_label, byte_size, output_path)
                            else:
                                logger.warning("[%s] Audio save returned False.", scene_label)
                            return result
                logger.warning("[%s] Audio: API returned no audio data.", scene_label)
                return False
            except Exception as e:
                logger.exception("[%s] Gemini TTS error: %s", scene_label, e)
                return False
    except Exception as e:
        logger.exception("[%s] Audio generation unexpected error: %s", scene_label, e)

    logger.warning("[%s] Audio generation FAILED.", scene_label)
    return False

def generate_and_mix_audio(
    text: str,
    music_path: str,
    output_path: str,
    engine: str = "gemini",
    music_gain_db: int = -20,
) -> bool:
    """
    Generates narration audio, then mixes it with a background music track.
    - Speech is generated to a temporary WAV.
    - Music is gain-adjusted, looped/trimmed to speech length, then overlaid.
    - Output format follows output_path extension (mp3/wav/etc supported by ffmpeg).
    """
    if not music_path or not os.path.exists(music_path):
        print("Mix Error: Background music file not found.")
        return False

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tf:
            temp_path = tf.name

        if not generate_audio_from_text(text, temp_path, engine=engine):
            print("Mix Error: Failed to generate narration audio.")
            return False

        speech = AudioSegment.from_file(temp_path)
        background = AudioSegment.from_file(music_path) + music_gain_db

        if len(background) < len(speech):
            background = background * (len(speech) // len(background) + 1)
        background = background[:len(speech)]

        combined = background.overlay(speech)

        ext = os.path.splitext(output_path)[1].lower().lstrip(".") or "mp3"
        combined.export(output_path, format=ext)
        return True
    except Exception as e:
        print(f"Mix Error: {e}")
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
