<#
KSP Pokemon Monitor - one-click connection & health diagnostic.

READ-ONLY. This never writes state.json, never starts or stops the monitor
process, and never modifies the scheduled task. It only inspects:

  1. KSP IP / Endpoint Status : a live probe against KSP's category API using
                                the monitor's own headers / TLS profile
                                (scripts\test_connection_probe.py).
  2. Monitor Process          : is the pythonw.exe monitor loop alive (+ PID).
  3. Autostart at Logon       : is the task's LOGON trigger enabled.
  4. Scan Loop                : health + next-run ETA from logs\monitor.log.

Invoked by TEST_CONNECTION.bat. Non-elevated.
#>
$ErrorActionPreference = "Stop"

$TaskName    = "KSP Pokemon Monitor"
$ScriptMatch = "ksp_monitor_loop.py"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProbeScript = Join-Path $PSScriptRoot "test_connection_probe.py"

function Resolve-Python {
    foreach ($name in @("python.exe", "python3.exe", "py.exe")) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    return $null
}

Write-Host ""
Write-Host "===== KSP Pokemon Monitor - Connection Test =====" -ForegroundColor Cyan
Write-Host "(read-only diagnostic - nothing is started, stopped, or changed)" -ForegroundColor DarkGray
Write-Host ""

# --- 1. Live network probe (Python, read-only) ---------------------------------
$ipStatus = "UNKNOWN"
$ipDetail = ""
$scanHealth = "Unknown"
$scanDetail = ""
$logTail = @()

$python = Resolve-Python
if (-not $python) {
    $ipDetail = "python not found on PATH - cannot run the network probe"
} elseif (-not (Test-Path $ProbeScript)) {
    $ipDetail = "probe script missing: $ProbeScript"
} else {
    Write-Host "Running live network probe against KSP (usually a few seconds)..." -ForegroundColor Gray
    $errFile = Join-Path $env:TEMP "ksp_test_connection_probe.err"
    try {
        $raw = & $python $ProbeScript 2>$errFile
        $line = $raw | Where-Object { $_ -match '^\s*\{.*\}\s*$' } | Select-Object -Last 1
        if (-not $line) { throw "probe produced no JSON output" }
        $probe = $line | ConvertFrom-Json
        $ipStatus   = "$($probe.ip_status)"
        $ipDetail   = "$($probe.ip_detail)"
        $scanHealth = "$($probe.scan_health)"
        $scanDetail = "$($probe.scan_detail)"
        $logTail    = @($probe.log_tail)
    } catch {
        $ipDetail = "network probe failed: $($_.Exception.Message)"
        if (Test-Path $errFile) {
            $err = (Get-Content $errFile -Tail 3) -join " | "
            if ($err) { $ipDetail += " [$err]" }
        }
    }
}

$scanLine = if ($scanHealth -and $scanDetail) { "$scanHealth - $scanDetail" }
            elseif ($scanHealth)              { $scanHealth }
            else                              { "Unknown" }

# --- 2. Monitor process -------------------------------------------------------
$procLine = "NOT RUNNING"
$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($ScriptMatch) }
if (-not $procs) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($ScriptMatch) }
}
if ($procs) {
    $pidList = ($procs | ForEach-Object { $_.ProcessId }) -join ', '
    $procLine = "RUNNING (PID $pidList)"
}

# --- 3. Autostart at logon (task's LOGON trigger only) -----------------------
$autoLine = "UNKNOWN"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    $autoLine = "DISABLED (task not registered)"
} else {
    $logon = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskLogonTrigger" })
    if ($logon.Count -eq 0) {
        $autoLine = "DISABLED (no logon trigger)"
    } elseif ($logon | Where-Object { $_.Enabled } | Select-Object -First 1) {
        $autoLine = "ENABLED"
    } else {
        $autoLine = "DISABLED"
    }
}

# --- Report -----------------------------------------------------------------
function Write-Field($label, $value, $color) {
    Write-Host $label -NoNewline
    Write-Host $value -ForegroundColor $color
}

$ipColor   = switch -Regex ($ipStatus) { '^CLEAN'      { 'Green' } '^BLOCKED' { 'Red' } default { 'Yellow' } }
$procColor = if ($procLine -like 'RUNNING*') { 'Green' } else { 'Yellow' }
$autoColor = if ($autoLine -like 'ENABLED*') { 'Green' } else { 'Yellow' }
$scanColor = switch -Regex ($scanLine) { '^Healthy' { 'Green' } '^Backoff' { 'Yellow' } default { 'Yellow' } }

Write-Host ""
Write-Host "-----------------------------------------------------------" -ForegroundColor DarkGray
Write-Field "KSP IP / Endpoint Status : " $ipStatus  $ipColor
Write-Field "Monitor Process          : " $procLine  $procColor
Write-Field "Autostart at Logon       : " $autoLine  $autoColor
Write-Field "Scan Loop                : " $scanLine  $scanColor
Write-Host "-----------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""

if ($ipDetail) {
    Write-Host ("  endpoint : " + $ipDetail) -ForegroundColor Gray
}
Write-Host ("  task     : " + $(if ($task) { "'$TaskName' registered, state=$($task.State)" } else { "'$TaskName' not registered" })) -ForegroundColor Gray
Write-Host ""

if ($logTail.Count -gt 0) {
    Write-Host "----- last $($logTail.Count) log lines (logs\monitor.log) -----" -ForegroundColor Cyan
    foreach ($ln in $logTail) {
        $c = switch -Regex ($ln) {
            'ERROR|CRITICAL|Traceback' { 'Red' }
            'WARN'                     { 'Yellow' }
            default                    { 'Gray' }
        }
        Write-Host $ln -ForegroundColor $c
    }
    Write-Host ""
}

Write-Host "Done. Nothing was started, stopped, or modified." -ForegroundColor DarkGray
Write-Host ""
