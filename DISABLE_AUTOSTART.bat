@echo off
setlocal
cd /d "%~dp0"

REM Toggles ONLY the logon trigger of the "KSP Pokemon Monitor" task OFF. The
REM task itself stays enabled, so START_MONITOR.bat still starts the monitor on
REM demand and STOP_MONITOR.bat / STATUS_MONITOR.bat are unaffected. This does
REM NOT stop a monitor that is already running - use STOP_MONITOR.bat for that.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\set_autostart_helper.ps1" -Mode off
set "RC=%ERRORLEVEL%"

echo.
pause
exit /b %RC%
