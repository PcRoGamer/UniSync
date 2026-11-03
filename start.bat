@echo off
setlocal enabledelayedexpansion
title UniSync - Autonomous University Companion

echo ========================================================
echo   UniSync - Academic LMS & Course Companion
echo ========================================================
echo.

:: Check for Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python was not found in your PATH.
    echo Please install Python 3.10+ from https://www.python.org/
    pause
    exit /b 1
)

:: Set python path to include backend
cd /d "%~dp0"
set PYTHONPATH=%CD%\backend

:: Check if virtual environment exists, create if missing
if not exist ".venv" (
    echo [*] Creating virtual environment (.venv)...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    echo [*] Installing dependencies...
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

echo [*] Starting UniSync Local Backend on http://127.0.0.1:8000...
echo [*] Opening Dashboard in browser...
start http://127.0.0.1:8000/

python -m uvicorn unisync.api.main:app --host 127.0.0.1 --port 8000
pause
