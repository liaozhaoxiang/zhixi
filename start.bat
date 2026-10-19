@echo off
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "URL=http://127.0.0.1:8010/"
if not exist "%~dp0logs" mkdir "%~dp0logs"
title ZHI-XI Launcher

where curl.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] curl.exe not found. Windows 10/11 should ship it.
  pause
  exit /b 1
)

rem Probe with --noproxy: a stale Clash system proxy must not fake a failure.
curl.exe -s -m 3 --noproxy "*" -o nul "%URL%api/health"
if not errorlevel 1 goto open

:start_it
echo Starting ZHI-XI ...
start "" /b "%~dp0.venv\Scripts\pythonw.exe" "%~dp0_run_server.py"

set /a N=0
:wait
timeout /t 1 /nobreak >nul
curl.exe -s -m 3 --noproxy "*" -o nul "%URL%api/health"
if not errorlevel 1 goto open
set /a N+=1
if %N% GEQ 30 goto fail
goto wait

:fail
echo [ERROR] service did not start within 30s. See logs\server.log
pause
exit /b 1

:open
echo ZHI-XI is running: %URL%
start "" "%URL%"
exit /b 0
