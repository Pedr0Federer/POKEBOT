@echo off
setlocal
cd /d "%~dp0"

echo ===============================
echo   KSP Pokemon Monitor - Start
echo ===============================
echo.

REM Starts the monitor on demand. Works whether "Autostart at Logon" is
REM ENABLED or DISABLED - the scheduled task stays enabled either way, and the
REM helper falls back to a direct pythonw background launch if needed. No-op if
REM the monitor is already running.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_monitor_helper.ps1"
if errorlevel 1 (
    echo.
    echo [KSP] ERROR: the monitor could not be started. See messages above.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\status_monitor_helper.ps1"

echo.
pause
exit /b 0
