import os
import wave
import tempfile
import shutil
import imageio_ffmpeg
from google import genai
from google.genai import types as genai_types
from google.cloud import texttospeech
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
        
        estimated_scenes_min = int(duration_minutes * 4)
        estimated_scenes_max = estimated_scenes_min + 3

        web_note = (
            "You have access to Google Search. Before writing, SEARCH the web for the most "
            "recent, accurate, and detailed information about this topic. Use real facts, "
            "statistics, and up-to-date information found in search results."
            if use_web_search else
            "Use your own extensive knowledge to write accurate content."
        )
        
        prompt = f"""You are an expert infographic video scriptwriter.
{web_note}

Write a highly engaging script for an infographic video about: "{topic}"

The video must be exactly {duration_minutes} minutes long ({duration_minutes * 140} total narration words).

Follow this EXACT format for each scene:

[Scene X]
Visual: (1-2 detailed sentences describing exactly what to show. Used as an AI image prompt. No text overlays.)
Narration: (The exact voiceover text. Must be substantial enough to fill time.)

Generate between {estimated_scenes_min} and {estimated_scenes_max} scenes.

CRITICAL: Begin IMMEDIATELY with [Scene 1]. No greetings, no intro text."""

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

        prompt = f"""You are an expert infographic video scriptwriter and content analyst.

A user has provided the following source text (it may be an article, blog post, essay, lecture notes, or any other content):

\"\"\"
{source_text}
\"\"\"

Your task:
1. Carefully read and understand the source text.
2. Identify the key ideas, facts, or narrative beats.
3. Convert them into a structured infographic video script, breaking it into logical scenes.

Follow this EXACT format for EVERY scene:

[Scene X]
Visual: (A detailed 1-2 sentence visual description for an AI image generator. Describe exactly what should appear on screen to represent this idea. Be vivid and specific. No text overlays.)
Narration: (The exact voiceover script for this scene. Must be faithful to the source text's facts and tone.)

Generate as many scenes as needed to cover all the key ideas in the source text.

CRITICAL STRICT RULE: Begin IMMEDIATELY with [Scene 1]. Do NOT include ANY introductory text, greetings, or meta-commentary. Output ONLY the scene blocks.
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

def generate_image_from_prompt(prompt: str, output_path: str) -> bool:
    """
    Uses Imagen 3 via the Gemini API to generate an image and save it to output_path.
    Returns True if successful, False otherwise.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == 'your_gemini_api_key_here':
        return False
        
    try:
        client = genai.Client(api_key=api_key)
        # Using nano-banana model via standard generate_content
        result = client.models.generate_content(
            model='nano-banana-pro-preview',
            contents=f"Generate an image with 16:9 aspect ratio of: {prompt}",
        )
        
        # The nano-banana model returns images inline
        if result.candidates and result.candidates[0].content.parts:
            part = result.candidates[0].content.parts[0]
            if hasattr(part, 'inline_data') and part.inline_data:
                with open(output_path, "wb") as f:
                    f.write(part.inline_data.data)
                return True
            
    except Exception as e:
        print(f"Image Gen Error: {e}")
        
    return False

import re

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

def generate_audio_from_text(text: str, output_path: str, engine: str = "gemini") -> bool:
    """
    Uses Google Cloud TTS or Gemini TTS to synthesize audio from text and saves it to output_path.
    Returns True if successful, False otherwise.
    """
    try:
        if engine == "gemini":
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key or api_key == 'your_gemini_api_key_here':
                return False
            
            try:
                client = genai.Client(api_key=api_key)
                tts_model = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
                tts_voice = os.getenv("GEMINI_TTS_VOICE", "puck")
                
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
                            )
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
                print(f"Gemini Audio Gen Error: {e}")
                try:
                    return _generate_audio_with_google_tts(text, output_path)
                except Exception as g_e:
                    print(f"Google Cloud TTS fallback error: {g_e}")
                    return False

        else: # Google Cloud TTS
            return _generate_audio_with_google_tts(text, output_path)
    except Exception as e:
        print(f"Audio Gen Error: {e}")
        
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
