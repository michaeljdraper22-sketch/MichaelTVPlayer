@echo off
setlocal enabledelayedexpansion
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

echo Building UninstallMichaelTV.exe ...
pyinstaller --noconfirm --onefile --windowed --name UninstallMichaelTV ^
    --workpath build_uninst --distpath dist uninstaller.py
if errorlevel 1 (
    echo WARNING: uninstaller build failed - dist will not include it.
)

echo Copying private VLC runtime into dist\vlc ...
robocopy "vlc" "dist\vlc" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo WARNING: failed to copy the bundled VLC runtime from vlc\ —
    echo the exe will fall back to the user's installed VLC.
)

rem ---- release zip (upload to GitHub Releases as MichaelTV-<version>.zip;
rem      the in-app updater downloads exactly this asset) ----
set VER=
for /f "tokens=*" %%i in ('python -c "from src.config import APP_VERSION; print(APP_VERSION)"') do set VER=%%i
if "%VER%"=="" set VER=dev
if exist "dist\MichaelTV-%VER%.zip" del "dist\MichaelTV-%VER%.zip"
powershell -NoProfile -Command ^
  "$v='%VER%'; $z=\"dist\MichaelTV-$v.zip\";" ^
  "$tmp=Join-Path $env:TEMP ('mtpzip'+$PID); New-Item -ItemType Directory $tmp | Out-Null;" ^
  "Copy-Item 'dist\MichaelTV.exe' $tmp; if (Test-Path 'dist\UninstallMichaelTV.exe') { Copy-Item 'dist\UninstallMichaelTV.exe' $tmp };" ^
  "Copy-Item 'dist\vlc' (Join-Path $tmp 'vlc') -Recurse;" ^
  "Compress-Archive -Path (Join-Path $tmp '*') -DestinationPath $z -Force;" ^
  "Remove-Item -Recurse -Force $tmp"
if exist "dist\MichaelTV-%VER%.zip" (
    echo Release package: dist\MichaelTV-%VER%.zip
)

rem ---- checksum asset: the in-app updater (v2.0.1+) verifies the zip
rem      against it when present; older releases have none and update as
rem      before (verification is strictly verify-when-present) ----
powershell -NoProfile -Command ^
  "(Get-FileHash -Algorithm SHA256 \"dist\MichaelTV-%VER%.zip\").Hash.ToLower() + '  MichaelTV-%VER%.zip' | Set-Content \"dist\MichaelTV-%VER%.zip.sha256\" -Encoding ascii"
if exist "dist\MichaelTV-%VER%.zip.sha256" (
    echo Checksum:        dist\MichaelTV-%VER%.zip.sha256
)

echo.
echo Done. Double-click:  dist\MichaelTV.exe
echo Runs fully isolated on dist\vlc\ — the installed VLC is not used.
echo Uninstaller: dist\UninstallMichaelTV.exe
pause
