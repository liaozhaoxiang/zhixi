@echo off
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
if not exist "%~dp0logs" mkdir "%~dp0logs"
start "" /b "%~dp0.venv\Scripts\pythonw.exe" "%~dp0_run_server.py"
exit /b 0
