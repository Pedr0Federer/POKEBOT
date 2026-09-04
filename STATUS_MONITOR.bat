@echo off
setlocal
cd /d "%~dp0"

REM Read-only status: "Autostart at Logon" (from the task's logon trigger) and
REM "Process" (the live pythonw.exe monitor) are reported separately.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\status_monitor_helper.ps1"

echo.
pause
exit /b 0
