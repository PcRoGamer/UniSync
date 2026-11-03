@echo off
setlocal
set PYTHON=python
set LOCAL_DIR=_local
set VENV_NAME=%LOCAL_DIR%\venv

cd /d "%~dp0"
if not exist "%LOCAL_DIR%" mkdir "%LOCAL_DIR%"

%PYTHON% -c "import sys; sys.exit(0 if sys.hexversion>=0x03000000 else 1)" >nul 2>&1
if errorlevel 1 (
    echo Python 3 is required. Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist "%VENV_NAME%\Scripts\python.exe" (
    echo First run: setting up virtual environment...
    %PYTHON% -m venv %VENV_NAME%
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo Installing dependencies...
    "%VENV_NAME%\Scripts\python.exe" -m pip install --upgrade pip
    "%VENV_NAME%\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        rmdir /S /Q %VENV_NAME%
        pause
        exit /b 1
    )
    echo Setup complete.
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [WARN] ffmpeg not found on PATH. Downloads may not be converted to MP4.
)

"%VENV_NAME%\Scripts\python.exe" echo360.py %*
endlocal
