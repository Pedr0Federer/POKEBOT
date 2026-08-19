@echo off
setlocal
cd /d "%~dp0"

rem Per-device: point this at pythonw.exe on this machine if it lives elsewhere.
set "PYTHONW=C:\Users\ilaym\AppData\Local\Programs\Python\Python314\pythonw.exe"

start "" /B "%PYTHONW%" ksp_monitor_loop.py
