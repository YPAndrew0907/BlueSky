param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet("start", "stop", "restart", "status", "logs")]
  [string]$Action
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-ConfigValue {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Default
  )
  $value = [Environment]::GetEnvironmentVariable($Name, "Process")
  if ([string]::IsNullOrWhiteSpace($value)) {
    return $Default
  }
  return $value
}

function Read-PidFile {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not (Test-Path $Path)) {
    return $null
  }
  $raw = (Get-Content $Path -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ([string]::IsNullOrWhiteSpace($raw)) {
    return $null
  }
  $pidValue = 0
  if ([int]::TryParse($raw.Trim(), [ref]$pidValue)) {
    return $pidValue
  }
  return $null
}

function Test-PidAlive {
  param([Parameter(Mandatory = $true)][int]$PidValue)
  $proc = Get-Process -Id $PidValue -ErrorAction SilentlyContinue
  return ($null -ne $proc)
}

$Root = Get-ConfigValue -Name "ROOT" -Default "D:\BlueSky"
$OutBase = Get-ConfigValue -Name "OUT_BASE" -Default (Join-Path $Root "data_v2_full")
$ControlDir = Join-Path $OutBase "control"
$LaunchdLogDir = Join-Path $OutBase "logs\launchd"
$DaemonPidFile = Join-Path $ControlDir "collector_daemon_win.pid"
$DaemonLogFile = Join-Path $LaunchdLogDir "collector-daemon-win.log"
$ScriptPath = Join-Path $Root "scripts\collector_daemon_windows.ps1"
$Pwsh = "$PSHOME\powershell.exe"
$KnownJobs = @(
  "state-writer",
  "micro-snapshot-study",
  "snapshot-panel",
  "wide-sweep",
  "hydrate-authors",
  "index-feed-generators",
  "refresh-discovery",
  "build-panel"
)

function Get-JobPidFile {
  param([Parameter(Mandatory = $true)][string]$JobName)
  if ($JobName -eq "state-writer") {
    return (Join-Path $ControlDir "state_writer_win.pid")
  }
  return (Join-Path $ControlDir "$JobName.pid")
}

function Stop-TrackedPid {
  param([int]$PidValue)
  if ($PidValue -le 0) {
    return
  }
  if (-not (Test-PidAlive -PidValue $PidValue)) {
    return
  }
  Stop-Process -Id $PidValue -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
  if (Test-PidAlive -PidValue $PidValue) {
    Stop-Process -Id $PidValue -Force -ErrorAction SilentlyContinue
  }
}

function Remove-TrackedPidFiles {
  foreach ($jobName in $KnownJobs) {
    Remove-Item -Force (Get-JobPidFile -JobName $jobName) -ErrorAction SilentlyContinue
  }
}

function Get-TrackedJobs {
  $jobs = @()
  foreach ($jobName in $KnownJobs) {
    $pidFile = Get-JobPidFile -JobName $jobName
    $pidValue = Read-PidFile -Path $pidFile
    if ($null -eq $pidValue) {
      continue
    }

    $alive = Test-PidAlive -PidValue $pidValue
    $startTime = $null
    $processName = $null
    if ($alive) {
      $proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
      if ($null -ne $proc) {
        $startTime = $proc.StartTime
        $processName = $proc.ProcessName
      }
    }

    $jobs += [PSCustomObject]@{
      JobName     = $jobName
      ProcessId   = $pidValue
      Alive       = $alive
      ProcessName = $processName
      StartTime   = $startTime
      PidFile     = $pidFile
    }
  }
  return $jobs
}

function Start-Daemon {
  $existingPid = Read-PidFile -Path $DaemonPidFile
  if ($null -ne $existingPid -and (Test-PidAlive -PidValue $existingPid)) {
    Write-Output "collector daemon already running pid=$existingPid"
    return
  }
  if (-not (Test-Path $ScriptPath)) {
    throw "daemon script not found: $ScriptPath"
  }

  Remove-Item -Force $DaemonPidFile -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $ControlDir | Out-Null
  New-Item -ItemType Directory -Force -Path $LaunchdLogDir | Out-Null

  $proc = Start-Process -FilePath $Pwsh -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ScriptPath) -WindowStyle Hidden -PassThru
  Start-Sleep -Seconds 2
  if (Test-PidAlive -PidValue $proc.Id) {
    Write-Output "collector daemon started pid=$($proc.Id)"
  }
  else {
    throw "collector daemon failed to start"
  }
}

function Stop-Daemon {
  $daemonPid = Read-PidFile -Path $DaemonPidFile
  if ($null -ne $daemonPid) {
    Stop-TrackedPid -PidValue $daemonPid
  }

  foreach ($job in Get-TrackedJobs) {
    Stop-TrackedPid -PidValue ([int]$job.ProcessId)
  }

  Remove-Item -Force $DaemonPidFile -ErrorAction SilentlyContinue
  Remove-TrackedPidFiles
  Write-Output "collector daemon stopped"
}

function Status-Daemon {
  $daemonPid = Read-PidFile -Path $DaemonPidFile
  if ($null -ne $daemonPid -and (Test-PidAlive -PidValue $daemonPid)) {
    Write-Output "daemon: running pid=$daemonPid"
  }
  else {
    Write-Output "daemon: not running"
  }

  $tracked = @(Get-TrackedJobs)
  Write-Output ""
  Write-Output "tracked jobs:"
  if ($tracked.Count -eq 0) {
    Write-Output "(none)"
    return
  }

  $tracked | Sort-Object JobName | Select-Object JobName, ProcessId, Alive, ProcessName, StartTime
}

function Logs-Daemon {
  if (Test-Path $DaemonLogFile) {
    Get-Content $DaemonLogFile -Tail 120
  }
  else {
    Write-Output "log not found: $DaemonLogFile"
  }
}

switch ($Action) {
  "start" { Start-Daemon }
  "stop" { Stop-Daemon }
  "restart" { Stop-Daemon; Start-Daemon }
  "status" { Status-Daemon }
  "logs" { Logs-Daemon }
}
