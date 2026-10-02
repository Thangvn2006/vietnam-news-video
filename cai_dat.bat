@echo off
chcp 65001 >nul
setlocal
title Cai Dat Tu Dong - Vietnam News Video (MoneyPrinterTurbo)

cd /d "%~dp0"
set "CURRENT_DIR=%CD%"

echo ======================================================================
echo    CÀI ĐẶT TỰ ĐỘNG - VIETNAM NEWS VIDEO (MONEYPRINTERTURBO)
echo ======================================================================
echo.

echo [1/5] Kiểm tra Python...
where python >nul 2>nul
if errorlevel 1 goto NO_PYTHON

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
echo [OK] Tìm thấy: %PY_VER%
echo.
goto CHECK_VENV

:NO_PYTHON
echo.
echo [LỖI] Không tìm thấy Python trên máy tính của bạn!
echo Vui lòng tải và cài đặt Python (khuyên dùng Python 3.10 hoặc 3.11 hoặc 3.12) tại:
echo   https://www.python.org/downloads/
echo LƯU Ý: Khi cài đặt, hãy nhớ tích chọn "Add python.exe to PATH".
echo.
pause
exit /b 1

:CHECK_VENV
echo [2/5] Kiểm tra môi trường ảo (venv)...
if exist "venv\Scripts\python.exe" goto VENV_EXISTS

echo [*] Đang khởi tạo môi trường ảo 'venv' (có thể mất 10-20 giây)...
python -m venv venv
if errorlevel 1 (
    echo [LỖI] Không thể tạo môi trường ảo venv. Vui lòng kiểm tra lại Python.
    pause
    exit /b 1
)
echo [OK] Đã tạo thành công môi trường ảo 'venv'.
echo.
goto INSTALL_DEPS

:VENV_EXISTS
echo [OK] Môi trường ảo 'venv' đã sẵn sàng.
echo.
goto INSTALL_DEPS

:INSTALL_DEPS
echo [3/5] Nâng cấp pip và cài đặt các thư viện cần thiết...
echo (Quá trình này phụ thuộc vào tốc độ mạng, vui lòng chờ trong giây lát...)
echo.

"venv\Scripts\python.exe" -m pip install --upgrade pip
if exist "requirements.txt" (
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [LỖI] Quá trình cài đặt thư viện gặp sự cố. Vui lòng kiểm tra mạng và thử lại.
        pause
        exit /b 1
    )
    echo.
    echo [OK] Đã cài đặt đầy đủ tất cả thư viện thành công!
) else (
    echo [CẢNH BÁO] Không tìm thấy file requirements.txt!
)
echo.
goto CHECK_CONFIG

:CHECK_CONFIG
echo [4/5] Kiểm tra file cấu hình config.toml...
if exist "config.toml" (
    echo [OK] File 'config.toml' đã sẵn sàng.
) else (
    if exist "config.example.toml" (
        copy "config.example.toml" "config.toml" >nul
        echo [OK] Đã tự động tạo file cấu hình 'config.toml' từ file mẫu.
    ) else (
        echo [CẢNH BÁO] Không tìm thấy config.example.toml.
    )
)
echo.
goto CHECK_FFMPEG

:CHECK_FFMPEG
echo [5/5] Kiểm tra bộ giải mã FFmpeg...
"venv\Scripts\python.exe" -c "from app.utils import utils; utils.check_ffmpeg_ready()" 2>nul
if errorlevel 1 (
    echo [THÔNG TIN] Sẽ sử dụng bộ FFmpeg tích hợp trong thư viện imageio-ffmpeg.
) else (
    echo [OK] FFmpeg đã sẵn sàng hoạt động!
)
echo.

echo ======================================================================
echo    CHÚC MỪNG: CÀI ĐẶT HOÀN TẤT THÀNH CÔNG!
echo ======================================================================
echo Bạn có thể khởi động ứng dụng ngay bây giờ:
echo    - Nhấp đúp vào file: khoi_dong.bat (hoặc run.bat)
echo    - Hoặc chạy: start.bat để mở trung tâm điều khiển
echo ======================================================================
echo.
pause
