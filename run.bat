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
python -m pip install -q -r requirements.txt

python main.py
endlocal
