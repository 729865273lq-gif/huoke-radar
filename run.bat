@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [first run] creating environment, please wait...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
    ".venv\Scripts\python.exe" -m playwright install chromium
    echo [done] environment ready.
)

echo Starting Huoke Radar... browser will open in a few seconds.
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:8080"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8080
pause
