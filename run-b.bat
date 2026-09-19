@echo off
cd /d "%~dp0"

set PORT=8081
set DB_PATH=%~dp0data-b\leads.db
set COLLECTOR_PROFILE_DIR=%~dp0data-b\browser-profile
set SENDER_PROFILE_DIR=%~dp0data-b\sender-profile
set XHS_COLLECTOR_PROFILE_DIR=%~dp0data-b\xhs-collector-profile

echo Starting Huoke Radar (B) ... browser will open in a few seconds.
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:8081"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8081
pause
