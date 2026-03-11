import os
from google import genai
from google.genai import types as genai_types
from google.cloud import texttospeech
from dotenv import load_dotenv

# Load environment variables (API keys) from .env file
load_dotenv()

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

def generate_audio_from_text(text: str, output_path: str) -> bool:
    """
    Uses Google Cloud TTS to synthesize audio from text and saves it to output_path.
    Returns True if successful, False otherwise.
    """
    try:
        # Client will automatically look for GOOGLE_APPLICATION_CREDENTIALS env var
        client = texttospeech.TextToSpeechClient()

        synthesis_input = texttospeech.SynthesisInput(text=text)

        # Build the voice request, select the journey voice
        voice = texttospeech.VoiceSelectionParams(
            language_code="en-US",
            name="en-US-Journey-D" # Deep male narration voice, very high quality
        )

        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3
        )

        response = client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )

        with open(output_path, "wb") as out:
            out.write(response.audio_content)
            
        return True
    except Exception as e:
        print(f"Audio Gen Error: {e}")
        
    return False
