import os
from google import genai
from google.cloud import texttospeech
from dotenv import load_dotenv

# Load environment variables (API keys) from .env file
load_dotenv()

def generate_script_from_topic(topic: str, duration_minutes: int = 5):
    """
    Uses Gemini 2.5 Pro to generate a structured infographic script from a user-provided topic.
    Yields the response in chunks for streaming.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == 'your_gemini_api_key_here':
        yield "[Error]: A valid GEMINI_API_KEY was not found in the .env file. Please add your API key."
        return

    try:
        # Initialize the new Google GenAI Client
        client = genai.Client(api_key=api_key)
        
        # A normal narrator speaks about 130-150 words per minute. So 1 minute is roughly 140 words.
        # Estimate number of scenes needed and word count. For infographics, usually ~15 seconds per scene.
        estimated_scenes_min = int(duration_minutes * 4)
        estimated_scenes_max = estimated_scenes_min + 3
        
        prompt = f"""
You are an expert infographic video scriptwriter. 
Write a highly engaging script for an infographic video about the following topic: "{topic}"

The user has explicitly requested this video to be exactly {duration_minutes} minutes long.
You must generate enough content and narration to fill a {duration_minutes}-minute video (roughly {duration_minutes * 140} words of total narration).

Break the script down into logical scenes so I can pass them to an image generator and a text-to-speech engine. 
Follow this EXACT format for each scene (do not miss the brackets):

[Scene X]
Visual: (Describe in 1-2 detailed sentences exactly what should be shown on screen. This will be used as the prompt for Google's Nano-Banana. Focus on visual description, not text overlays.)
Narration: (The exact voiceover text to be spoken by the narrator for this particular scene. Make sure each narration block is substantial enough to fill time.)

Generate between {estimated_scenes_min} and {estimated_scenes_max} scenes to cover the {duration_minutes}-minute duration efficiently.

CRITICAL STRICT RULE: You must ONLY return the script blocks. Do NOT include ANY conversational filler, greetings, or introductory text (like "Here is a highly engaging script..."). Begin immediately with [Scene 1].
"""

        response = client.models.generate_content_stream(
            model='gemini-2.5-pro',
            contents=prompt,
        )
        for chunk in response:
            if chunk.text:
                yield chunk.text
    except Exception as e:
        yield f"[Error occurred during Gemini 2.5 generation]:\n{str(e)}"

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
