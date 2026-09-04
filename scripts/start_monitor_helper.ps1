<#
Starts the KSP Pokemon Monitor on demand. Non-elevated.

Independent of the autostart (logon) trigger: the scheduled task is kept
ENABLED (State = Ready) at all times, so 'Start-ScheduledTask' works whether
autostart is ON or OFF.

Order of preference:
  1. If a monitor is already running          -> do nothing (no-op).
  2. If the task is registered                -> Start-ScheduledTask
     (re-enabling the task first if something disabled it - the autostart
      trigger is NOT touched).
  3. Otherwise / if the task run fails         -> launch
     pythonw.exe ksp_monitor_loop.py directly, in the background.

STOP_MONITOR.bat and STATUS_MONITOR.bat work with every path above: both key
off the running 'pythonw.exe ... ksp_monitor_loop.py' process, and STOP also
calls Stop-ScheduledTask.
#>
$ErrorActionPreference = "Stop"

$TaskName    = "KSP Pokemon Monitor"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ScriptPath  = Join-Path $ProjectRoot "ksp_monitor_loop.py"
$ScriptMatch = "ksp_monitor_loop.py"

function Get-MonitorProcs {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($ScriptMatch) }
}

function Resolve-Pythonw {
    $c = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $c) { $c = Get-Command python.exe -ErrorAction SilentlyContinue }
    if ($c) { return $c.Source }
    return $null
}

Write-Host ""
Write-Host "===== KSP Pokemon Monitor - Start =====" -ForegroundColor Cyan
Write-Host ""

$running = Get-MonitorProcs
if ($running) {
    Write-Host ("[KSP] Monitor already running (PID {0}) - nothing to do." -f ($running.ProcessId -join ', ')) -ForegroundColor Green
    Write-Host ""
    return
}

$task    = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$started = $false

if ($task) {
    if (-not $task.Settings.Enabled) {
        Write-Host "[KSP] Task was disabled - re-enabling it (autostart trigger left untouched)..."
        try {
            Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        } catch {
            Write-Host "[KSP] Could not re-enable the task: $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    try {
        Write-Host "[KSP] Starting via scheduled task '$TaskName'..."
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Start-Sleep -Seconds 3
        $p = Get-MonitorProcs
        if ($p) {
            Write-Host ("[KSP] Started (PID {0})." -f ($p.ProcessId -join ', ')) -ForegroundColor Green
            $started = $true
        } else {
            Write-Host "[KSP] Task ran but no monitor process appeared - will try a direct launch." -ForegroundColor Yellow
        }
    }
    catch {
        Write-Host "[KSP] Could not run the task ($($_.Exception.Message)) - will try a direct launch." -ForegroundColor Yellow
    }
}
else {
    Write-Host "[KSP] Scheduled task not registered - using a direct background launch." -ForegroundColor Yellow
    Write-Host "[KSP] (Register it once with setup_task_scheduler.ps1 to get autostart support.)"
}

if (-not $started) {
    $pythonw = Resolve-Pythonw
    if (-not $pythonw) {
        Write-Host "[KSP] ERROR: neither pythonw.exe nor python.exe found on PATH." -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path $ScriptPath)) {
        Write-Host "[KSP] ERROR: $ScriptPath not found." -ForegroundColor Red
        exit 1
    }
    Write-Host "[KSP] Launching $pythonw ksp_monitor_loop.py in the background..."
    Start-Process -FilePath $pythonw -ArgumentList "`"$ScriptPath`"" `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden
    Start-Sleep -Seconds 3
    $p = Get-MonitorProcs
    if ($p) {
        Write-Host ("[KSP] Started (PID {0})." -f ($p.ProcessId -join ', ')) -ForegroundColor Green
        $started = $true
    } else {
        Write-Host "[KSP] ERROR: monitor did not start - check logs\monitor.log" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
