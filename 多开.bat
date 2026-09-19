@echo off
cd /d "%~dp0"

:: ============================================
:: Multi-instance launcher (one instance per industry/client)
:: Each instance: own port, own DB, own login.
:: ============================================

set PORT=
set NAME=
set /p PORT=Enter port (default 8081): 
if "%PORT%"=="" set PORT=8081
set /p NAME=Enter instance name (e.g. jiancai): 
if "%NAME%"=="" set NAME=extra

set DB_PATH=data-%NAME%\leads.db
set COLLECTOR_PROFILE_DIR=data-%NAME%\browser-profile
set SENDER_PROFILE_DIR=data-%NAME%\sender-profile

if not exist ".venv\Scripts\python.exe" (
    echo [first run] creating environment, please wait...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
    ".venv\Scripts\python.exe" -m playwright install chromium
)

echo Starting instance [%NAME%] at http://127.0.0.1:%PORT%
start "" http://127.0.0.1:%PORT%
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
pause
