@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Create the Alpha environment first: py -3.12 -m venv .venv
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -B -m ii_pro_ui --no-docker-probe %*
exit /b %ERRORLEVEL%
