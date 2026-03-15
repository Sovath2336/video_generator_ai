import base64
import json
import os
import re
import shutil
import tempfile
import time
import wave
from difflib import SequenceMatcher

import imageio_ffmpeg
import requests
from google import genai
from google.genai import types as genai_types
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import texttospeech
from google.oauth2 import service_account
from dotenv import load_dotenv

# Ensure ffmpeg is discoverable before importing pydub (prevents runtime warning).
_ffmpeg_exe = None
try:
    _ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    _ffmpeg_dir = os.path.dirname(_ffmpeg_exe)
    if _ffmpeg_dir and _ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
    # pydub expects a binary named ffmpeg(.exe). Create a local alias if needed.
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

# Load environment variables (API keys) from .env file
load_dotenv()

# Ensure pydub can discover ffmpeg without requiring system PATH setup.
try:
    if _ffmpeg_exe:
        AudioSegment.converter = _ffmpeg_exe
        AudioSegment.ffmpeg = _ffmpeg_exe
except Exception:
    pass

def generate_script_from_topic(topic: str, duration_minutes: int = 5, use_web_search: bool = False):
    """
    Uses Gemini 2.5 Pro to generate a structured infographic script from a user-provided topic.
    When use_web_search=True, activates Gemini's native Google Search grounding so it
    fetches live, up-to-date web results before writing the script.
    Yields the response in chunks for streaming.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == 'your_gemini_api_key_here':
        yield "[Error]: A valid GEMINI_API_KEY was not found in the .env file. Please add your API key."
        return

    try:
        client = genai.Client(api_key=api_key)
        
        estimated_scenes_min = max(6, int(duration_minutes * 1.5) + 1)
        estimated_scenes_max = max(8, int(duration_minutes * 2) + 2)
        words_per_scene = int((duration_minutes * 140) / ((estimated_scenes_min + estimated_scenes_max) / 2))

        web_note = (
            "You have access to Google Search. Before writing, SEARCH the web for the most "
            "recent, accurate, and detailed information about this topic. Use real facts, "
            "statistics, and up-to-date information found in search results."
            if use_web_search else
            "Use your own extensive knowledge to write accurate content."
        )
        
        cta_visual = _cta_visual_instruction()

        prompt = f"""You are an expert YouTube infographic video scriptwriter with a talent for cinematic storytelling and audience retention.
{web_note}

Write a complete, YouTube-ready infographic video script about: "{topic}"

TARGET: Exactly {duration_minutes} minutes of narration (~{duration_minutes * 140} words total across all scenes).
SCENES: Generate between {estimated_scenes_min} and {estimated_scenes_max} scenes.
NARRATION PER SCENE: Each main content scene must have ~{words_per_scene} words of narration (minimum {int(words_per_scene * 0.8)}, maximum {int(words_per_scene * 1.2)}). Do NOT write short 1-2 sentence narrations. Each scene should feel like a full paragraph of documentary narration.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANDATORY SCENE STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[Scene 1] — BRIEF TITLE CARD INTRO (mandatory, separate intro scene)
Visual: A bold, cinematic title card image. Do not show other texts other than the topic title "{topic}" as large stylised text centred on screen, set against a dramatic thematic background that immediately communicates the subject. The image should feel like a movie poster or YouTube thumbnail - eye-catching, high contrast, professional.
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
            config = genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())]
            )

        response = client.models.generate_content_stream(
            model='gemini-2.5-pro',
            contents=prompt,
            config=config,
        )
        for chunk in response:
            if chunk.text:
                yield chunk.text
    except Exception as e:
        yield f"[Error occurred during Gemini 2.5 generation]:\n{str(e)}"

def analyze_text_to_scenes(source_text: str):
    """
    Takes arbitrary user-pasted text (article, blog, essay, etc.) and uses
    Gemini 2.5 Pro to intelligently split it into infographic video scenes.
    Yields the response in chunks for streaming.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == 'your_gemini_api_key_here':
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

        response = client.models.generate_content_stream(
            model='gemini-2.5-pro',
            contents=prompt,
        )
        for chunk in response:
            if chunk.text:
                yield chunk.text
    except Exception as e:
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


def _shared_cta_image_path() -> str:
    assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    shared_dir = os.path.join(assets_dir, "Outro Image")
    os.makedirs(shared_dir, exist_ok=True)

    new_path = os.path.join(shared_dir, "cta_scene.jpg")
    old_path = os.path.join(assets_dir, "_shared", "cta_scene.jpg")
    if not os.path.exists(new_path) and os.path.exists(old_path):
        try:
            shutil.copy2(old_path, new_path)
        except Exception:
            pass
    return new_path


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


def _build_image_prompt(prompt: str, image_text: str | None = None, narration: str | None = None) -> str:
    base_prompt = f"Generate an image with 16:9 aspect ratio of: {prompt}"
    image_text = _normalize_image_text(image_text)
    narration = re.sub(r"\s+", " ", (narration or "")).strip()
    if _is_title_card_prompt(prompt) and image_text:
        return (
            f"{base_prompt}\n\n"
            f'Important: include this exact title text in the image: "{image_text}". '
            "Make it large, clean, and central as part of the title-card design. "
            "Do not add any other readable text."
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


def generate_image_from_prompt(prompt: str, output_path: str, overlay_text: str | None = None, narration: str | None = None) -> bool:
    """
    Uses Imagen 3 via the Gemini API to generate an image and save it to output_path.
    `overlay_text` is only used for intro title-card scenes. Other scenes stay text-free.
    `narration` is used to anchor the image to the scene's core idea.
    Returns True if successful, False otherwise.
    """
    if _is_cta_image_prompt(prompt):
        shared_cta_path = _shared_cta_image_path()
        try:
            if os.path.exists(shared_cta_path) and os.path.getsize(shared_cta_path) > 0:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                shutil.copy2(shared_cta_path, output_path)
                return True
        except Exception:
            pass

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == 'your_gemini_api_key_here':
        return False
        
    try:
        client = genai.Client(api_key=api_key)
        # Using nano-banana model via standard generate_content
        result = client.models.generate_content(
            model='nano-banana-pro-preview',
            contents=_build_image_prompt(prompt, overlay_text, narration),
        )
        
        # The nano-banana model returns images inline
        if result.candidates and result.candidates[0].content.parts:
            part = result.candidates[0].content.parts[0]
            if hasattr(part, 'inline_data') and part.inline_data:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "wb") as f:
                    f.write(part.inline_data.data)
                if _is_cta_image_prompt(prompt):
                    try:
                        shutil.copy2(output_path, _shared_cta_image_path())
                    except Exception:
                        pass
                return True
            
    except Exception as e:
        print(f"Image Gen Error: {e}")
        
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


def _fetch_google_access_token() -> str:
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not cred_path:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is not set.")
    if not os.path.isabs(cred_path):
        cred_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), cred_path)

    creds = service_account.Credentials.from_service_account_file(
        cred_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    creds.refresh(GoogleAuthRequest())
    if not creds.token:
        raise RuntimeError("Unable to fetch Google Cloud access token.")
    return creds.token


def _recognize_word_offsets(script_text: str, audio_path: str):
    """
    Transcribe scene audio with Google Speech-to-Text and return word offsets.
    Uses the script text as phrase context so the recognizer stays close to the narration.
    """
    script_words = _subtitle_word_items(script_text)
    if not script_words:
        return [], _audio_duration_seconds(audio_path)

    token = _fetch_google_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    temp_wav = None
    def _speech_config():
        phrases = [script_text] + [item["text"] for item in script_words[:500]]
        return {
            "encoding": "LINEAR16",
            "sampleRateHertz": 16000,
            "languageCode": "en-US",
            "enableWordTimeOffsets": True,
            "enableAutomaticPunctuation": False,
            "model": "video",
            "speechContexts": [{"phrases": phrases, "boost": 20.0}],
        }

    try:
        audio = AudioSegment.from_file(audio_path).set_channels(1).set_frame_rate(16000).set_sample_width(2)
        duration_sec = max(0.0, len(audio) / 1000.0)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tf:
            temp_wav = tf.name
        audio.export(temp_wav, format="wav")
        with open(temp_wav, "rb") as fh:
            audio_b64 = base64.b64encode(fh.read()).decode("ascii")

        def _extract_words(data: dict):
            recognized = []
            for result in data.get("results", []):
                alternatives = result.get("alternatives", [])
                if not alternatives:
                    continue
                for word in alternatives[0].get("words", []):
                    text = str(word.get("word", "")).strip()
                    spoken = text.replace("’", "'").lower()
                    if not spoken:
                        continue
                    recognized.append(
                        {
                            "text": text,
                            "spoken": spoken,
                            "start_sec": _parse_google_time_offset(word.get("startTime")),
                            "end_sec": _parse_google_time_offset(word.get("endTime")),
                        }
                    )
            return recognized

        recognized = []
        if duration_sec <= 55.0:
            payload = {
                "config": _speech_config(),
                "audio": {"content": audio_b64},
            }
            response = requests.post(
                "https://speech.googleapis.com/v1/speech:recognize",
                headers=headers,
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            recognized = _extract_words(response.json())

        if not recognized:
            payload = {
                "config": _speech_config(),
                "audio": {"content": audio_b64},
            }
            response = requests.post(
                "https://speech.googleapis.com/v1/speech:longrunningrecognize",
                headers=headers,
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            operation = response.json()
            op_name = operation.get("name", "")
            if not op_name:
                raise RuntimeError("Speech-to-Text did not return an operation id.")

            deadline = time.time() + 180
            while time.time() < deadline:
                poll = requests.get(
                    f"https://speech.googleapis.com/v1/operations/{op_name}",
                    headers=headers,
                    timeout=60,
                )
                poll.raise_for_status()
                op_data = poll.json()
                if op_data.get("done"):
                    if "error" in op_data:
                        raise RuntimeError(op_data["error"].get("message", "Speech-to-Text alignment failed."))
                    recognized = _extract_words(op_data.get("response", {}))
                    break
                time.sleep(2)

        if not recognized:
            raise RuntimeError("Speech-to-Text returned no word timings for this audio.")

        return recognized, duration_sec
    finally:
        if temp_wav and os.path.exists(temp_wav):
            try:
                os.remove(temp_wav)
            except OSError:
                pass


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

    try:
        recognized_words, duration_sec = _recognize_word_offsets(script_text, audio_path)
        aligned_words = _align_script_words_to_offsets(script_text, recognized_words, duration_sec)
    except Exception:
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

def _generate_audio_with_google_tts(text: str, output_path: str) -> bool:
    """
    Uses Google Cloud TTS and saves MP3 audio to output_path.
    """
    # Client will automatically look for GOOGLE_APPLICATION_CREDENTIALS env var
    client = texttospeech.TextToSpeechClient()

    # Convert plain text to SSML to add natural pauses between sentences.
    ssml_text = re.sub(r'([.?!])\s+', r'\1 <break time="500ms"/> ', text)
    ssml = f"<speak>{ssml_text}</speak>"

    synthesis_input = texttospeech.SynthesisInput(ssml=ssml)
    ext = os.path.splitext(output_path)[1].lower()
    audio_encoding = (
        texttospeech.AudioEncoding.LINEAR16
        if ext == ".wav"
        else texttospeech.AudioEncoding.MP3
    )
    # Journey voices can reject some encodings; use a broadly compatible voice for WAV.
    voice_name = "en-US-Standard-D" if audio_encoding == texttospeech.AudioEncoding.LINEAR16 else "en-US-Journey-D"
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name=voice_name
    )
    audio_config = texttospeech.AudioConfig(audio_encoding=audio_encoding)

    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )

    with open(output_path, "wb") as out:
        out.write(response.audio_content)
    return True

def generate_audio_from_text(text: str, output_path: str, engine: str = "gemini", voice: str | None = None) -> bool:
    import traceback as _tb
    try:
        if engine == "gemini":
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key or api_key == 'your_gemini_api_key_here':
                return False

            try:
                client = genai.Client(api_key=api_key)
                tts_model = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
                # Caller-supplied voice takes priority, then env var, then default male voice.
                tts_voice = voice or os.getenv("GEMINI_TTS_VOICE", "puck")

                response = client.models.generate_content(
                    model=tts_model,
                    contents=text,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=f"You are a {tts_voice} voice narrator. Always speak in the same consistent voice throughout.",
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
                
                if response.candidates and response.candidates[0].content.parts:
                    for part in response.candidates[0].content.parts:
                        if hasattr(part, 'inline_data') and part.inline_data and part.inline_data.data:
                            return _save_gemini_audio_bytes(
                                part.inline_data.data,
                                getattr(part.inline_data, "mime_type", ""),
                                output_path,
                            )
                return False
            except Exception as e:
                print(f"[Gemini TTS] Error:\n{_tb.format_exc()}")
                try:
                    return _generate_audio_with_google_tts(text, output_path)
                except Exception as g_e:
                    print(f"[Google Cloud TTS] Fallback error:\n{_tb.format_exc()}")
                    return False

        else:
            return _generate_audio_with_google_tts(text, output_path)
    except Exception as e:
        print(f"[Audio Gen] Unexpected error:\n{_tb.format_exc()}")

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
