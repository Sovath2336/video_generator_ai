@echo off
setlocal

set PROJECT_DIR=%~dp0
set VENV_PYTHON=%PROJECT_DIR%.venv\Scripts\python.exe
set VENV_PYINSTALLER=%PROJECT_DIR%.venv\Scripts\pyinstaller.exe

echo ============================================
echo  Infographic Video Generator - Build Tool
echo ============================================
echo.

:: Step 1: PyInstaller bundle
echo [1/2] Building with PyInstaller...
"%VENV_PYINSTALLER%" "%PROJECT_DIR%build.spec" --distpath "%PROJECT_DIR%dist" --workpath "%PROJECT_DIR%build" --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed. Check output above.
    pause
    exit /b 1
)
echo       Done. Output: dist\VideoGeneratorAI\
echo.

:: Step 2: Inno Setup compile
echo [2/2] Compiling Inno Setup installer...
set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
    echo WARNING: Inno Setup not found at default path.
    echo          Install from https://jrsoftware.org/isdl.php
    echo          Then re-run this script, or compile installer.iss manually.
    echo.
    pause
    exit /b 0
)

if not exist "%PROJECT_DIR%installer_output" mkdir "%PROJECT_DIR%installer_output"
%ISCC% "%PROJECT_DIR%installer.iss"
if errorlevel 1 (
    echo.
    echo ERROR: Inno Setup compilation failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Build complete!
echo  Installer: installer_output\VideoGeneratorAI_Setup_v1.0.0.exe
echo ============================================
pause
