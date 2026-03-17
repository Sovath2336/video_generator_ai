## Learned User Preferences

- Never add comments to code; write concise clean code only.
- Only generate the minimal code or patch needed — no full file rewrites unless asked.
- Do not add explanations, docstrings, or narrative text unless explicitly asked.
- Ask a short clarifying question instead of generating large speculative output.
- Do not push to GitHub until the user explicitly says to do so.
- When shell commands fail silently in Cursor's shell, instruct the user to run them in their own terminal.
- Always use the `.venv` Python when running scripts: `.venv\Scripts\python.exe` — the project uses a virtualenv, not global Python.
- NeuTTS `ref_text` is required (non-empty) — passing an empty string crashes `_to_phones` with IndexError.

## Learned Workspace Facts

- Project path: `D:\My Own Apps\Video Generator AI\video_generator_ai`
- Python environment: `.venv` inside the project root (activated with `.venv\Scripts\Activate.ps1`)
- Main files: `main.py` (PyQt5 UI), `ai_generator.py` (all AI/audio/video logic), `db.py` (database)
- TTS stack: NeuTTS (default, on-device), Gemini TTS (cloud, paid), Google Cloud TTS (fallback, Journey-D voice)
- NeuTTS uses `NEUTTS_REF_AUDIO` and `NEUTTS_REF_TEXT` env vars for voice cloning reference; both are required.
- NeuTTS model is lazily loaded as a singleton `_neutts_instance` in `ai_generator.py`.
- Video encoding uses `libx264` (not x265 — bundled ffmpeg often lacks x265 support).
- Gemini TTS can return either `audio/l16` or `audio/pcm` mime types for raw PCM audio.
- The project is on Windows 10; PowerShell is the shell.
- Cursor's shell cannot spawn torch-based Python subprocesses reliably on Windows — always defer to user's own terminal for such tasks.
- User is in Cambodia; store and display timestamps in Cambodia time (UTC+7).
