@echo off
setlocal
cd /d "%~dp0"

REM Toggles ONLY the logon trigger of the "KSP Pokemon Monitor" task ON. The
REM task itself stays enabled, so START_MONITOR.bat / STOP_MONITOR.bat /
REM STATUS_MONITOR.bat keep working exactly the same. This does NOT start the
REM monitor now - use START_MONITOR.bat for that.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\set_autostart_helper.ps1" -Mode on
set "RC=%ERRORLEVEL%"

echo.
pause
exit /b %RC%
