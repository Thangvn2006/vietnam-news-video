@echo off
chcp 65001 >nul
setlocal
title Automated Setup - VietNamNewsVideo

cd /d "%~dp0"
set "CURRENT_DIR=%CD%"

echo ======================================================================
echo    AUTOMATED SETUP - VIETNAMNEWSVIDEO
echo ======================================================================
echo.

echo [1/5] Checking Python...
where python >nul 2>nul
if errorlevel 1 goto NO_PYTHON

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
echo [OK] Found: %PY_VER%
echo.
goto CHECK_VENV

:NO_PYTHON
echo.
echo [ERROR] Python is not found on your system!
echo Please download and install Python (Python 3.10, 3.11, or 3.12 recommended) from:
echo   https://www.python.org/downloads/
echo IMPORTANT: During installation, make sure to check "Add python.exe to PATH".
echo.
pause
exit /b 1

:CHECK_VENV
echo [2/5] Checking virtual environment (venv)...
if exist "venv\Scripts\python.exe" goto VENV_EXISTS

echo [*] Creating virtual environment 'venv' (this may take 10-20 seconds)...
python -m venv venv
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment venv. Please check your Python installation.
    pause
    exit /b 1
)
echo [OK] Successfully created virtual environment 'venv'.
echo.
goto INSTALL_DEPS

:VENV_EXISTS
echo [OK] Virtual environment 'venv' is ready.
echo.
goto INSTALL_DEPS

:INSTALL_DEPS
echo [3/5] Upgrading pip and installing required dependencies...
echo (This depends on your network speed, please wait a moment...)
echo.

"venv\Scripts\python.exe" -m pip install --upgrade pip
if exist "requirements.txt" (
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependency installation encountered an issue. Please check your network and retry.
        pause
        exit /b 1
    )
    echo.
    echo [OK] All dependencies installed successfully!
) else (
    echo [WARNING] requirements.txt not found!
)
echo.
goto CHECK_CONFIG

:CHECK_CONFIG
echo [4/5] Checking configuration file config.toml...
if exist "config.toml" (
    echo [OK] Configuration file 'config.toml' is ready.
) else (
    if exist "config.example.toml" (
        copy "config.example.toml" "config.toml" >nul
        echo [OK] Automatically initialized 'config.toml' from example template.
    ) else (
        echo [WARNING] config.example.toml not found.
    )
)
echo.
goto CHECK_FFMPEG

:CHECK_FFMPEG
echo [5/5] Checking FFmpeg decoder...
"venv\Scripts\python.exe" -c "from app.utils import utils; utils.check_ffmpeg_ready()" 2>nul
if errorlevel 1 (
    echo [INFO] Using bundled FFmpeg from imageio-ffmpeg package.
) else (
    echo [OK] FFmpeg is ready!
)
echo.

echo ======================================================================
echo    CONGRATULATIONS: SETUP COMPLETED SUCCESSFULLY!
echo ======================================================================
echo You can now launch the application:
echo    - Double-click: run.bat (or khoi_dong.bat)
echo    - Or run: start.bat to open the Control Center
echo ======================================================================
echo.
pause
