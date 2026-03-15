# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

block_cipher = None

project_dir = os.path.abspath(os.path.dirname(SPEC))

added_files = [
    (os.path.join(project_dir, 'db.py'),           '.'),
    (os.path.join(project_dir, 'ai_generator.py'), '.'),
    (os.path.join(project_dir, 'app_icon.ico'),    '.'),
    (os.path.join(project_dir, '.env'),             '.'),
]

hidden_imports = [
    'PyQt5.QtMultimedia',
    'PyQt5.QtMultimediaWidgets',
    'PyQt5.sip',
    'concurrent.futures',
    'threading',
    'google.genai',
    'google.api_core',
    'google.auth',
    'moviepy.editor',
    'moviepy.video.io.VideoFileClip',
    'moviepy.audio.io.AudioFileClip',
    'PIL._tkinter_finder',
    'imageio_ffmpeg',
    'pydub',
    'pydub.utils',
    'sqlite3',
    'dotenv',
    'webbrowser',
]

hidden_imports += collect_submodules('google.genai')
hidden_imports += collect_submodules('google.api_core')
hidden_imports += collect_submodules('google.auth')
hidden_imports += collect_submodules('moviepy')

added_files += collect_data_files('moviepy')
added_files += collect_data_files('imageio_ffmpeg')
added_files += collect_data_files('imageio')
added_files += collect_data_files('certifi')

for _pkg in [
    'imageio', 'imageio_ffmpeg', 'moviepy', 'Pillow', 'PIL',
    'google-generativeai', 'google-genai',
    'google-api-core', 'google-auth',
    'requests', 'certifi', 'charset-normalizer', 'urllib3',
    'pydub', 'python-dotenv', 'numpy', 'decorator',
]:
    try:
        added_files += copy_metadata(_pkg)
    except Exception:
        pass

a = Analysis(
    [os.path.join(project_dir, 'main.py')],
    pathex=[project_dir],
    binaries=[],
    datas=added_files,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['neutts', 'torch', 'soundfile', 'tkinter', 'matplotlib', 'numpy.distutils'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VideoGeneratorAI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(project_dir, 'app_icon.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VideoGeneratorAI',
)
