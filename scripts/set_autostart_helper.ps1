<#
Toggles ONLY the logon (autostart) trigger of the "KSP Pokemon Monitor" task.

    powershell -File .\scripts\set_autostart_helper.ps1 -Mode on
    powershell -File .\scripts\set_autostart_helper.ps1 -Mode off

Non-elevated. The task was registered by the current user (via
setup_task_scheduler.ps1 / schtasks), so this user already has full control
over it and Set-ScheduledTask works without elevation.

The task itself is left ENABLED (State = Ready) no matter what, so on-demand
start via START_MONITOR.bat / 'schtasks /Run' keeps working with autostart
either on or off. This script never starts or stops a running monitor.
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("on", "off")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$TaskName = "KSP Pokemon Monitor"
$want     = ($Mode -eq "on")

Write-Host ""
Write-Host "===== KSP Pokemon Monitor - Autostart $($Mode.ToUpper()) =====" -ForegroundColor Cyan
Write-Host ""

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "[KSP] ERROR: task '$TaskName' is not registered." -ForegroundColor Red
    Write-Host "[KSP] Run once: powershell -ExecutionPolicy Bypass -File `"$PSScriptRoot\..\setup_task_scheduler.ps1`""
    exit 1
}

$triggers = @($task.Triggers)
$logon    = $triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskLogonTrigger" }
if (-not $logon) {
    Write-Host "[KSP] ERROR: the task has no logon trigger to toggle." -ForegroundColor Red
    Write-Host "[KSP] Re-create it: powershell -ExecutionPolicy Bypass -File `"$PSScriptRoot\..\setup_task_scheduler.ps1`""
    exit 1
}

try {
    foreach ($t in $logon) { $t.Enabled = $want }

    # Belt and braces: keep the task itself enabled so on-demand start works.
    $settings = $task.Settings
    $settings.Enabled = $true

    Set-ScheduledTask -TaskName $TaskName -Trigger $triggers -Settings $settings -ErrorAction Stop | Out-Null
}
catch {
    Write-Host "[KSP] ERROR: could not update the task: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# --- Verify end state ---
$after       = Get-ScheduledTask -TaskName $TaskName
$logonAfter  = @($after.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskLogonTrigger" })
$autostartOn = [bool]($logonAfter | Where-Object { $_.Enabled } | Select-Object -First 1)

Write-Host ("[KSP] Task state          : {0}" -f $after.State)
if ($autostartOn) {
    Write-Host "[KSP] Autostart at logon  : ENABLED" -ForegroundColor Green
    Write-Host "[KSP] The monitor will start automatically at your next logon."
} else {
    Write-Host "[KSP] Autostart at logon  : DISABLED" -ForegroundColor Yellow
    Write-Host "[KSP] The monitor will NOT start at logon. Start it manually with START_MONITOR.bat."
}
Write-Host "[KSP] This did not start or stop a running monitor."
Write-Host ""

if ($autostartOn -ne $want) {
    Write-Host "[KSP] ERROR: trigger state did not change as requested." -ForegroundColor Red
    exit 1
}
