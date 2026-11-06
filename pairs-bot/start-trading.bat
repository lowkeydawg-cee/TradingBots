@echo off
rem Start the demo trading and open the Approaches page. Double-click this file.
rem Demo money only: the watcher sends no orders. Close the three small windows to stop it.
cd /d "%~dp0"
set UNIVERSE=--universe crypto
if exist .env (
  set UNIVERSE=
) else (
  echo No .env with Alpaca keys, so this watches crypto only.
)
start "Trading watcher" /min ".venv\Scripts\python.exe" watcher.py --source alpaca %UNIVERSE%
start "Trading heartbeat" /min ".venv\Scripts\python.exe" heartbeat.py
start "Trading dashboard" /min ".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501 --server.headless true
timeout /t 8 /nobreak >nul
start "" http://localhost:8501/Approaches
