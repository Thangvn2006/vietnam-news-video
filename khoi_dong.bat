@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Vietnam News Video - MoneyPrinterTurbo WebUI

cd /d "%~dp0"
set "CURRENT_DIR=%CD%"
set "PYTHONPATH=%CURRENT_DIR%"

echo ======================================================================
echo    KHỞI ĐỘNG VIETNAM NEWS VIDEO (MONEYPRINTERTURBO)
echo ======================================================================
echo.

rem 1. Kiểm tra môi trường ảo
set "PYTHON_EXE="
if exist "%CURRENT_DIR%\venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CURRENT_DIR%\venv\Scripts\python.exe"
) else if exist "%CURRENT_DIR%\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CURRENT_DIR%\.venv\Scripts\python.exe"
) else if exist "%CURRENT_DIR%\lib\python\python.exe" (
    set "PYTHON_EXE=%CURRENT_DIR%\lib\python\python.exe"
)

if not defined PYTHON_EXE goto PROMPT_INSTALL
goto PROCEED_START

:PROMPT_INSTALL
echo [CẢNH BÁO] Chưa tìm thấy môi trường ảo (venv) chứa các thư viện của tool.
echo.
set /p AUTO_INSTALL="Bạn có muốn tự động cài đặt ngay bây giờ không? (Y/N, mặc định Y): "
if /i "!AUTO_INSTALL!"=="N" (
    echo Đã hủy. Vui lòng chạy file 'cai_dat.bat' trước khi mở tool.
    pause
    exit /b 1
)
echo Đang chuyển sang tiến trình cài đặt...
echo.
call "%CURRENT_DIR%\cai_dat.bat"
if exist "%CURRENT_DIR%\venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CURRENT_DIR%\venv\Scripts\python.exe"
) else (
    echo [LỖI] Cài đặt chưa hoàn tất.
    pause
    exit /b 1
)

:PROCEED_START
rem 2. Kiểm tra file config.toml
if not exist "%CURRENT_DIR%\config.toml" (
    if exist "%CURRENT_DIR%\config.example.toml" (
        copy "%CURRENT_DIR%\config.example.toml" "%CURRENT_DIR%\config.toml" >nul
        echo [OK] Đã tự động tạo 'config.toml' từ mẫu.
    )
)

rem 3. Cấu hình Host và Port
if not defined MPT_WEBUI_HOST set "MPT_WEBUI_HOST=127.0.0.1"
if not defined MPT_WEBUI_PORT set "MPT_WEBUI_PORT=8501"

set "SELECTED_WEBUI_PORT="
for /f %%P in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$hostAddress=$null; foreach ($address in [Net.Dns]::GetHostAddresses($env:MPT_WEBUI_HOST)) { if ($address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) { $hostAddress=$address; break } }; if ($null -eq $hostAddress) { exit 1 }; $preferred=[int]$env:MPT_WEBUI_PORT; $candidates=New-Object System.Collections.Generic.List[int]; $candidates.Add($preferred); foreach ($candidate in 8502..8599) { if ($candidate -ne $preferred) { $candidates.Add($candidate) } }; foreach ($port in $candidates) { $socket=[Net.Sockets.Socket]::new([Net.Sockets.AddressFamily]::InterNetwork,[Net.Sockets.SocketType]::Stream,[Net.Sockets.ProtocolType]::Tcp); try { $socket.Bind([Net.IPEndPoint]::new($hostAddress,$port)); $socket.Close(); Write-Output $port; exit 0 } catch { try { $socket.Close() } catch {} } }; exit 1"') do set "SELECTED_WEBUI_PORT=%%P"

if defined SELECTED_WEBUI_PORT (
    set "MPT_WEBUI_PORT=%SELECTED_WEBUI_PORT%"
)

echo [OK] Môi trường Python: %PYTHON_EXE%
echo [OK] Địa chỉ WebUI: http://%MPT_WEBUI_HOST%:%MPT_WEBUI_PORT%
echo.
echo ======================================================================
echo    Đang khởi động giao diện WebUI và tự động mở trình duyệt...
echo    (Để dừng ứng dụng, nhấn phím tắt Ctrl + C trong cửa sổ này)
echo ======================================================================
echo.

rem Mở trình duyệt sau 2 giây
start "" powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2; Start-Process 'http://%MPT_WEBUI_HOST%:%MPT_WEBUI_PORT%'"

rem Chạy Streamlit
"%PYTHON_EXE%" -m streamlit run .\webui\Main.py --server.address=%MPT_WEBUI_HOST% --server.port=%MPT_WEBUI_PORT% --browser.serverAddress=%MPT_WEBUI_HOST% --browser.gatherUsageStats=False --client.toolbarMode=minimal --logger.hideWelcomeMessage=True --server.showEmailPrompt=False --server.enableCORS=True

if errorlevel 1 (
    echo.
    echo [THÔNG BÁO] Ứng dụng đã dừng lại.
    pause
)
