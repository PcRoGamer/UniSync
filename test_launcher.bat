@echo off
setlocal enabledelayedexpansion

set "PYTHON_EXE="
if exist "%USERPROFILE%\miniconda3\python.exe" set "PYTHON_EXE=%USERPROFILE%\miniconda3\python.exe"
if not defined PYTHON_EXE if exist "C:\Users\Admin\miniconda3\python.exe" set "PYTHON_EXE=C:\Users\Admin\miniconda3\python.exe"

echo Python executable: !PYTHON_EXE!

:: Check if server is already running
curl.exe -s -o nul -m 1 http://127.0.0.1:8000/health
if !ERRORLEVEL! equ 0 (
    echo [OK] Already running
    exit /b 0
)

echo Starting server...
cd /d "%~dp0"
set "PYTHONPATH=C:\Users\Admin\AppData\Local\npm-cache\_npx\4b4c857f6efdfb61\node_modules\@wonderwhy-er\desktop-commander\dist\backend"

start "UniSync Server" /min "!PYTHON_EXE!" -m uvicorn unisync.api.main:app --host 127.0.0.1 --port 8000

echo Waiting for health...
for /l %%i in (1,1,15) do (
    curl.exe -s -o nul -m 1 http://127.0.0.1:8000/health
    if !ERRORLEVEL! equ 0 (
        echo [OK] Server started and ready on attempt %%i!
        exit /b 0
    )
    ping -n 2 127.0.0.1 >nul
)

echo [FAIL] Server did not become ready
exit /b 1
