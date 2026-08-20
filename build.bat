@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create virtual environment. Make sure Python 3 is installed and on PATH.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"
echo Installing dependencies...
python -m pip install -q -r requirements.txt pyinstaller pillow

echo Building MichaelTVPlayer.exe ...
pyinstaller --noconfirm MichaelTVPlayer.spec
if errorlevel 1 (
    echo Build FAILED.
    pause
    exit /b 1
)

echo Copying private VLC runtime into dist\vlc ...
robocopy "vlc" "dist\vlc" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo WARNING: failed to copy the bundled VLC runtime from vlc\ —
    echo the exe will fall back to the user's installed VLC.
)

echo.
echo Done. Double-click:  dist\MichaelTV.exe
echo Runs fully isolated on dist\vlc\ — the installed VLC is not used.
pause
