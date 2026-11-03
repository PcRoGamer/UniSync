@echo off

setlocal enabledelayedexpansion

title UniSync Launcher



echo ===================================================

echo   UniSync - University of Melbourne Study Companion

echo ===================================================



:: 1. Locate the correct Python executable with UniSync dependencies

set "PYTHON_EXE="

if exist "%~dp0..\.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0..\.venv\Scripts\python.exe"

if not defined PYTHON_EXE if exist "%USERPROFILE%\miniconda3\python.exe" set "PYTHON_EXE=%USERPROFILE%\miniconda3\python.exe"

if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\miniconda3\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\miniconda3\python.exe"



:: Fallback: probe PATH but exclude Microsoft WindowsApps alias

if not defined PYTHON_EXE (

    for /f "delims=" %%p in ('where python 2^>nul') do (

        echo "%%p" | findstr /i /c:"WindowsApps" >nul

        if errorlevel 1 (

            if not defined PYTHON_EXE set "PYTHON_EXE=%%p"

        )

    )

)



if not defined PYTHON_EXE (

    echo [ERROR] Python environment with UniSync dependencies not found!

    echo Create .venv or install Python on PATH, then retry.

    pause

    exit /b 1

)



:: 2. Check if server is already running on port 8000

curl.exe -s -o nul -m 1 http://127.0.0.1:8000/health

if !ERRORLEVEL! equ 0 (

    echo [OK] UniSync server is already running.

    echo Opening dashboard at http://localhost:8000...

    start http://localhost:8000

    timeout /t 2 /nobreak >nul 2>&1

    exit /b 0

)



:: 3. Launch background server

echo Starting UniSync server...

cd /d "%~dp0.."

set "PYTHONPATH=C:\Users\Admin\AppData\Local\npm-cache\_npx\4b4c857f6efdfb61\node_modules\@wonderwhy-er\desktop-commander\dist\backend"



start "UniSync Server" /min "%PYTHON_EXE%" -m uvicorn unisync.api.main:app --host 127.0.0.1 --port 8000



:: 4. Poll health endpoint until ready (up to 15 seconds)

echo Waiting for UniSync server to become ready...

for /l %%i in (1,1,15) do (

    curl.exe -s -o nul -m 1 http://127.0.0.1:8000/health

    if !ERRORLEVEL! equ 0 (

        echo [OK] UniSync server is online!

        echo Opening dashboard at http://localhost:8000...

        start http://localhost:8000

        timeout /t 2 /nobreak >nul 2>&1

        exit /b 0

    )

    ping -n 2 127.0.0.1 >nul

)



echo [WARNING] Server startup timed out. Attempting to open dashboard anyway...

start http://localhost:8000

timeout /t 3 /nobreak >nul 2>&1

exit /b 0

