@echo off
setlocal
cd /d "%~dp0"

REM Read-only one-click diagnostic. Runs a live KSP network probe using the
REM monitor's own headers/session logic, then reports:
REM   - KSP IP / Endpoint Status : CLEAN / BLOCKED
REM   - Monitor Process          : RUNNING (PID ...) / NOT RUNNING
REM   - Autostart at Logon       : ENABLED / DISABLED
REM   - Scan Loop                : Healthy / Backoff - next scan ~HH:MM
REM
REM This starts nothing, stops nothing, and modifies nothing (no DB writes,
REM no process changes, no scheduled-task changes).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\test_connection_helper.ps1"

echo.
pause
exit /b 0
