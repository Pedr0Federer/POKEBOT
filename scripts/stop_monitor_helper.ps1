# stop_monitor_helper.ps1
# Stops the running instance of the existing "KSP Pokemon Monitor" scheduled
# task, then terminates any leftover monitor process
# (pythonw.exe running ksp_monitor_loop.py) if it is still alive.
#
# This does NOT disable autostart and does NOT modify the task definition -
# it only ends the currently running instance. Use DISABLE_AUTOSTART.bat to
# prevent it starting at the next logon.

$ErrorActionPreference = "Continue"
$TaskName    = "KSP Pokemon Monitor"
$ScriptMatch = "ksp_monitor_loop.py"

Write-Host "Stopping '$TaskName'..." -ForegroundColor Cyan

# --- End the running task instance ---
try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Write-Host "  Scheduled task instance ended." -ForegroundColor Green
} catch {
    Write-Host "  Could not end task via scheduler ($($_.Exception.Message))." -ForegroundColor Yellow
}

Start-Sleep -Seconds 2

# --- Kill any leftover monitor process ---
$procs = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($ScriptMatch) }

if ($procs) {
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-Host ("  Killed leftover process PID {0}." -f $p.ProcessId) -ForegroundColor Green
        } catch {
            Write-Host ("  Failed to kill PID {0}: {1}" -f $p.ProcessId, $_.Exception.Message) -ForegroundColor Red
        }
    }
} else {
    Write-Host "  No leftover monitor process found." -ForegroundColor Green
}

Write-Host "Done." -ForegroundColor Cyan
