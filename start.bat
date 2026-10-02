@echo off
chcp 65001 >nul
setlocal
title Vietnam News Video - Trung Tam Dieu Khien

cd /d "%~dp0"
set "CURRENT_DIR=%CD%"
set "PYTHONPATH=%CURRENT_DIR%"

:MENU
cls
echo ======================================================================
echo       TRUNG TÂM ĐIỀU KHIỂN - VIETNAM NEWS VIDEO (MONEYPRINTERTURBO)
echo ======================================================================
echo.
echo   [1] Khởi động WebUI (Giao diện web tạo video) - [Khuyên dùng]
echo   [2] Cài đặt hoặc cập nhật thư viện (Install / Update Dependencies)
echo   [3] Chạy API Backend Server (FastAPI / Swagger Docs)
echo   [0] Thoát
echo.
echo ======================================================================
choice /c 1230 /n /m "Nhập lựa chọn của bạn [1, 2, 3, 0]: "

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
echo    KHỞI ĐỘNG API BACKEND SERVER
echo ======================================================================
echo.
set "PYTHON_EXE="
if exist "%CURRENT_DIR%\venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CURRENT_DIR%\venv\Scripts\python.exe"
) else if exist "%CURRENT_DIR%\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CURRENT_DIR%\.venv\Scripts\python.exe"
)

if not defined PYTHON_EXE (
    echo [LỖI] Chưa có môi trường ảo venv. Vui lòng chọn mục [2] để cài đặt trước.
    pause
    goto MENU
)

if not exist "%CURRENT_DIR%\config.toml" (
    if exist "%CURRENT_DIR%\config.example.toml" copy "%CURRENT_DIR%\config.example.toml" "%CURRENT_DIR%\config.toml" >nul
)

echo Đang mở trình duyệt tới Swagger API Docs: http://127.0.0.1:8080/docs ...
start "" powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8080/docs'"

"%PYTHON_EXE%" main.py
if errorlevel 1 pause
goto MENU

:END
