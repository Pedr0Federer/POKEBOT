# Registers a Windows Scheduled Task that can launch the KSP fast-poll monitor
# loop (ksp_monitor_loop.py) and let it run indefinitely, checking KSP roughly
# every 15-30 seconds.
#
# Decoupled architecture (manual on-demand vs. autostart-at-logon):
#   - The TASK itself is always registered ENABLED (State = Ready) with
#     AllowStartOnDemand = true, so 'schtasks /Run' / START_MONITOR.bat can
#     start the monitor on demand at ANY time, regardless of the autostart
#     setting.
#   - The LOGON TRIGGER is what controls autostart. It is registered DISABLED
#     by default. ENABLE_AUTOSTART.bat / DISABLE_AUTOSTART.bat toggle ONLY that
#     trigger's Enabled flag (via scripts\set_autostart_helper.ps1 /
#     Set-ScheduledTask); they never disable the task, so on-demand start keeps
#     working either way.
#
# This uses an XML task definition (via schtasks /Create /XML) rather than
# schtasks' basic flags, because two settings the loop needs aren't reachable
# from the basic flags:
#   - ExecutionTimeLimit must be disabled (PT0S / unlimited). Task Scheduler's
#     default 72-hour limit would silently kill a `while True` loop.
#   - MultipleInstancesPolicy = IgnoreNew, so if the task fires again at a
#     later logon while the loop is already running, Task Scheduler itself
#     won't start a second copy (the loop also self-enforces this with a
#     named mutex, but this is a second layer of protection).
#
# Also note: on this machine the ScheduledTasks PowerShell module
# (Register-ScheduledTask) fails with "Access is denied" for this user's
# token; schtasks.exe uses a different (RPC-based) path that works fine.
# Read-only / modify calls from the module (Get-ScheduledTask,
# Set-ScheduledTask, Enable-ScheduledTask, Stop-ScheduledTask) DO work
# non-elevated for the task once it has been created by this user.
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
  <RegistrationInfo>
    <Description>KSP Pokemon fast-poll monitor loop. Task stays enabled for on-demand start; the logon trigger (autostart) is toggled by ENABLE/DISABLE_AUTOSTART.bat.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>false</Enabled>
      <UserId>$UserId</UserId>
    </LogonTrigger>
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
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
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

Write-Host ""
Write-Host "Registered scheduled task '$TaskName':"
Write-Host "  - Task state        : ENABLED / Ready  (on-demand start via START_MONITOR.bat works now)"
Write-Host "  - Autostart at logon : DISABLED         (enable with ENABLE_AUTOSTART.bat)"
Write-Host ""
Write-Host "The monitor is NOT started by this script. Use START_MONITOR.bat to start it now,"
Write-Host "or ENABLE_AUTOSTART.bat to have it start automatically at your next logon."
