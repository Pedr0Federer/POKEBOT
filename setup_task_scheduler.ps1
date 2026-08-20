# Registers a Windows Scheduled Task that launches the KSP fast-poll monitor
# loop (ksp_monitor_loop.py) at logon and lets it run indefinitely, checking
# KSP roughly every 15-30 seconds.
#
# A second trigger re-attempts a launch every 15 minutes as a watchdog, in
# case the logon-triggered process dies for some unexpected reason (there's
# otherwise no supervisor to bring it back until the next logon/reboot).
# This is safe to fire at that cadence while the loop is healthy: it never
# creates a duplicate running instance, because both Task Scheduler's own
# MultipleInstancesPolicy=IgnoreNew and the loop's own named mutex
# (ksp_monitor_loop.py) reject the relaunch attempt while the earlier
# process still holds the lock. The loop's own retry loop around Cloudflare
# session bootstrap means a transient block no longer crashes the process,
# so a watchdog relaunch -- and the one Telegram "started" message that
# comes with it -- should now only happen on a genuine, rare process death.
#
# This uses an XML task definition (via schtasks /Create /XML) rather than
# schtasks' basic flags, because two settings the loop needs aren't reachable
# from the basic flags:
#   - ExecutionTimeLimit must be disabled (PT0S / unlimited). Task Scheduler's
#     default 72-hour limit would silently kill a `while True` loop.
#   - MultipleInstancesPolicy = IgnoreNew, so if a trigger fires again while
#     the loop is already running, Task Scheduler itself won't start a
#     second copy (the loop also self-enforces this with a named mutex, but
#     this is a second layer of protection).
#
# Also note: on this machine the ScheduledTasks PowerShell module
# (Register-ScheduledTask) fails with "Access is denied" for this user's
# token; schtasks.exe uses a different (RPC-based) path that works fine.
#
# Run this once, from an ordinary (non-admin) PowerShell prompt:
#   powershell -ExecutionPolicy Bypass -File .\setup_task_scheduler.ps1

$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
$ScriptPath = Join-Path $ProjectDir "ksp_monitor_loop.py"
$TaskName = "KSP Pokemon Monitor"

$PythonwCmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $PythonwCmd) {
    $PythonwCmd = Get-Command python.exe -ErrorAction Stop
    Write-Warning "pythonw.exe not found; falling back to python.exe (a console window may briefly flash)."
}
$PythonPath = $PythonwCmd.Source
$UserId = "$env:USERDOMAIN\$env:USERNAME"

$XmlContent = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$UserId</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Repetition>
        <Interval>PT15M</Interval>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$UserId</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$PythonPath</Command>
      <Arguments>"$ScriptPath"</Arguments>
      <WorkingDirectory>$ProjectDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$XmlPath = Join-Path $env:TEMP "ksp_monitor_task.xml"
Set-Content -Path $XmlPath -Value $XmlContent -Encoding Unicode

schtasks /Create /TN $TaskName /XML $XmlPath /F
if ($LASTEXITCODE -ne 0) {
    throw "schtasks /Create failed with exit code $LASTEXITCODE"
}
Remove-Item $XmlPath -Force

Write-Host "Registered scheduled task '$TaskName' (starts the fast-poll loop at logon, runs indefinitely)."
Write-Host "Starting it once now so you don't have to log out/in to see it working..."
schtasks /Run /TN $TaskName
Start-Sleep -Seconds 3
schtasks /Query /TN $TaskName /V /FO LIST | Select-String -Pattern "Last Run Time|Last Result|Status"
Write-Host "Check logs\monitor.log to confirm it's running (you should see 'Startup: running an initial full check')."
