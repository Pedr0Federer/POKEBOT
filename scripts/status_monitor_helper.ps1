<#
KSP Pokemon Monitor status report. Non-elevated, read-only: never modifies the
scheduled task. Invoked by STATUS_MONITOR.bat and by START_MONITOR.bat.

Clearly separates two independent things:
  - "Autostart at Logon" : ENABLED/DISABLED, read from the task's LOGON TRIGGER
  - "Process"            : RUNNING/NOT RUNNING, the live pythonw.exe monitor
#>
$ErrorActionPreference = "Stop"

$TaskName    = "KSP Pokemon Monitor"
$ScriptMatch = "ksp_monitor_loop.py"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogFile     = Join-Path $ProjectRoot "logs\monitor.log"

Write-Host ""
Write-Host "===== KSP Pokemon Monitor - Status =====" -ForegroundColor Cyan
Write-Host ""

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Scheduled task     : NOT REGISTERED" -ForegroundColor Red
    Write-Host "                     run setup_task_scheduler.ps1 to create it."
    Write-Host ""
    Write-Host "Autostart at Logon : UNKNOWN (task not registered)" -ForegroundColor Yellow
} else {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName

    # Autostart is governed by the LOGON TRIGGER, not by the task's enabled
    # state. The task stays enabled (Ready) so on-demand start always works.
    $logon       = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskLogonTrigger" })
    $autostartOn = [bool]($logon | Where-Object { $_.Enabled } | Select-Object -First 1)
    $taskUsable  = [bool]$task.Settings.Enabled

    Write-Host "Scheduled task     : registered" -ForegroundColor Green
    Write-Host ("  State            : " + $task.State + $(if ($taskUsable) { "  (on-demand start OK)" } else { "  (DISABLED - START_MONITOR.bat will re-enable it)" }))
    Write-Host ("  Last Run         : " + $info.LastRunTime)
    Write-Host ("  Last Result      : 0x{0:X} ({0})" -f $info.LastTaskResult)
    Write-Host ("  Next Run         : " + $info.NextRunTime)

    Write-Host ""
    if (-not $logon) {
        Write-Host "Autostart at Logon : NO LOGON TRIGGER (re-run setup_task_scheduler.ps1)" -ForegroundColor Yellow
    } elseif ($autostartOn) {
        Write-Host "Autostart at Logon : ENABLED" -ForegroundColor Green
        Write-Host "                     monitor starts automatically at next logon"
    } else {
        Write-Host "Autostart at Logon : DISABLED" -ForegroundColor Yellow
        Write-Host "                     start manually with START_MONITOR.bat"
    }
}

Write-Host ""

$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
         Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($ScriptMatch) }
if ($procs) {
    foreach ($p in $procs) {
        Write-Host ("Process            : RUNNING (PID {0})" -f $p.ProcessId) -ForegroundColor Green
    }
} else {
    Write-Host "Process            : NOT RUNNING" -ForegroundColor Yellow
}

Write-Host ""

if (Test-Path $LogFile) {
    Write-Host "----- last 5 log lines -----" -ForegroundColor Cyan
    Get-Content -Path $LogFile -Tail 5 | ForEach-Object {
        $color = switch -Regex ($_) {
            'ERROR|CRITICAL|Traceback' { 'Red' }
            'WARN'                     { 'Yellow' }
            default                    { 'Gray' }
        }
        Write-Host $_ -ForegroundColor $color
    }
} else {
    Write-Host ("Log file not found: " + $LogFile) -ForegroundColor Yellow
}
Write-Host ""
