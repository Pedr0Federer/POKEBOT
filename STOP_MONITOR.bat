@echo off
setlocal
cd /d "%~dp0"

REM Stops the running monitor instance (task instance + any leftover
REM pythonw.exe). Does NOT change the autostart setting - use
REM DISABLE_AUTOSTART.bat to prevent it starting at the next logon.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_monitor_helper.ps1"

echo.
pause
exit /b 0
