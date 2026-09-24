@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 python -m pip install -r requirements.txt
start "" "http://127.0.0.1:8000"
python -m uvicorn server:app --host 127.0.0.1 --port 8000
pause
