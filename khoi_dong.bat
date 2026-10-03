@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title VietNamNewsVideo - WebUI Launcher

cd /d "%~dp0"
set "CURRENT_DIR=%CD%"
set "PYTHONPATH=%CURRENT_DIR%"

echo ======================================================================
echo    STARTING VIETNAMNEWSVIDEO
echo ======================================================================
echo.

rem 1. Check virtual environment
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
echo [WARNING] Virtual environment (venv) was not found.
echo.
set /p AUTO_INSTALL="Do you want to automatically run setup now? (Y/N, default Y): "
if /i "!AUTO_INSTALL!"=="N" (
    echo Startup canceled. Please run 'install.bat' before launching the tool.
    pause
    exit /b 1
)
echo Switching to automated setup...
echo.
call "%CURRENT_DIR%\cai_dat.bat"
if exist "%CURRENT_DIR%\venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CURRENT_DIR%\venv\Scripts\python.exe"
) else (
    echo [ERROR] Setup did not complete successfully.
    pause
    exit /b 1
)

:PROCEED_START
rem 2. Check config.toml
if not exist "%CURRENT_DIR%\config.toml" (
    if exist "%CURRENT_DIR%\config.example.toml" (
        copy "%CURRENT_DIR%\config.example.toml" "%CURRENT_DIR%\config.toml" >nul
        echo [OK] Automatically initialized 'config.toml' from template.
    )
)

rem 3. Configure Host and Port
if not defined MPT_WEBUI_HOST set "MPT_WEBUI_HOST=127.0.0.1"
if not defined MPT_WEBUI_PORT set "MPT_WEBUI_PORT=8501"

set "SELECTED_WEBUI_PORT="
for /f %%P in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$hostAddress=$null; foreach ($address in [Net.Dns]::GetHostAddresses($env:MPT_WEBUI_HOST)) { if ($address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) { $hostAddress=$address; break } }; if ($null -eq $hostAddress) { exit 1 }; $preferred=[int]$env:MPT_WEBUI_PORT; $candidates=New-Object System.Collections.Generic.List[int]; $candidates.Add($preferred); foreach ($candidate in 8502..8599) { if ($candidate -ne $preferred) { $candidates.Add($candidate) } }; foreach ($port in $candidates) { $socket=[Net.Sockets.Socket]::new([Net.Sockets.AddressFamily]::InterNetwork,[Net.Sockets.SocketType]::Stream,[Net.Sockets.ProtocolType]::Tcp); try { $socket.Bind([Net.IPEndPoint]::new($hostAddress,$port)); $socket.Close(); Write-Output $port; exit 0 } catch { try { $socket.Close() } catch {} } }; exit 1"') do set "SELECTED_WEBUI_PORT=%%P"

if defined SELECTED_WEBUI_PORT (
    set "MPT_WEBUI_PORT=%SELECTED_WEBUI_PORT%"
)

echo [OK] Python Environment: %PYTHON_EXE%
echo [OK] WebUI Address: http://%MPT_WEBUI_HOST%:%MPT_WEBUI_PORT%
echo.
echo ======================================================================
echo    Launching WebUI and opening browser...
echo    (To stop the application, press Ctrl + C in this window)
echo ======================================================================
echo.

rem Open browser after 2 seconds
start "" powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2; Start-Process 'http://%MPT_WEBUI_HOST%:%MPT_WEBUI_PORT%'"

rem Run Streamlit
"%PYTHON_EXE%" -m streamlit run .\webui\Main.py --server.address=%MPT_WEBUI_HOST% --server.port=%MPT_WEBUI_PORT% --browser.serverAddress=%MPT_WEBUI_HOST% --browser.gatherUsageStats=False --client.toolbarMode=minimal --logger.hideWelcomeMessage=True --server.showEmailPrompt=False --server.enableCORS=True

if errorlevel 1 (
    echo.
    echo [INFO] Application stopped.
    pause
)
