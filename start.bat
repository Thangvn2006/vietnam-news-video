@echo off
chcp 65001 >nul
setlocal
title VietNamNewsVideo - Control Center

cd /d "%~dp0"
set "CURRENT_DIR=%CD%"
set "PYTHONPATH=%CURRENT_DIR%"

:MENU
cls
echo ======================================================================
echo       CONTROL CENTER - VIETNAMNEWSVIDEO
echo ======================================================================
echo.
echo   [1] Launch WebUI (AI Video Creation Interface) - [Recommended]
echo   [2] Install / Update Dependencies
echo   [3] Launch API Backend Server (FastAPI / Swagger Docs)
echo   [0] Exit
echo.
echo ======================================================================
choice /c 1230 /n /m "Enter your choice [1, 2, 3, 0]: "

if errorlevel 4 exit /b 0
if errorlevel 3 goto START_API
if errorlevel 2 goto RUN_INSTALL
if errorlevel 1 goto START_WEBUI
goto MENU

:START_WEBUI
call "%CURRENT_DIR%\khoi_dong.bat"
goto END

:RUN_INSTALL
call "%CURRENT_DIR%\cai_dat.bat"
echo.
pause
goto MENU

:START_API
cls
echo ======================================================================
echo    STARTING API BACKEND SERVER
echo ======================================================================
echo.
set "PYTHON_EXE="
if exist "%CURRENT_DIR%\venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CURRENT_DIR%\venv\Scripts\python.exe"
) else if exist "%CURRENT_DIR%\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CURRENT_DIR%\.venv\Scripts\python.exe"
)

if not defined PYTHON_EXE (
    echo [ERROR] Virtual environment venv not found. Please choose option [2] to install first.
    pause
    goto MENU
)

if not exist "%CURRENT_DIR%\config.toml" (
    if exist "%CURRENT_DIR%\config.example.toml" copy "%CURRENT_DIR%\config.example.toml" "%CURRENT_DIR%\config.toml" >nul
)

echo Opening browser to Swagger API Docs: http://127.0.0.1:8080/docs ...
start "" powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8080/docs'"

"%PYTHON_EXE%" main.py
if errorlevel 1 pause
goto MENU

:END
