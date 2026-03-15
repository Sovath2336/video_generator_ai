# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

block_cipher = None

project_dir = os.path.abspath(os.path.dirname(SPEC))

added_files = [
    (os.path.join(project_dir, 'db.py'),              '.'),
    (os.path.join(project_dir, 'ai_generator.py'),    '.'),
    (os.path.join(project_dir, '.ffmpeg_bin', 'ffmpeg.exe'), '.ffmpeg_bin'),
    (os.path.join(project_dir, 'gen-lang-client-0347255924-598b1319c8a4.json'), '.'),
    (os.path.join(project_dir, 'app_icon.ico'),       '.'),
    (os.path.join(project_dir, '.env'),                '.'),
]

hidden_imports = [
    'PyQt5.QtMultimedia',
    'PyQt5.sip',
    'google.genai',
    'google.cloud.texttospeech',
    'moviepy.editor',
    'moviepy.video.io.VideoFileClip',
    'moviepy.audio.io.AudioFileClip',
    'PIL._tkinter_finder',
    'imageio_ffmpeg',
    'pydub',
    'sqlite3',
    'dotenv',
]

hidden_imports += collect_submodules('google.genai')
hidden_imports += collect_submodules('google.cloud.texttospeech')
hidden_imports += collect_submodules('moviepy')

added_files += collect_data_files('moviepy')
added_files += collect_data_files('imageio_ffmpeg')
added_files += collect_data_files('imageio')

for _pkg in ['imageio', 'imageio_ffmpeg', 'moviepy', 'PIL', 'google-generativeai',
             'google-genai', 'requests', 'certifi', 'charset-normalizer',
             'pydub', 'python-dotenv']:
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
