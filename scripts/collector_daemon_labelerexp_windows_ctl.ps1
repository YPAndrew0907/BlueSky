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
$OutBase = Get-ConfigValue -Name "OUT_BASE" -Default (Join-Path (Join-Path $Root "data_v2_full") "labelerexp")
$ControlDir = Join-Path $OutBase "control"
$LaunchdLogDir = Join-Path $OutBase "logs\launchd"
$DaemonPidFile = Join-Path $ControlDir "collector_daemon_labelerexp_win.pid"
$DaemonLogFile = Join-Path $LaunchdLogDir "collector-daemon-labelerexp-win.log"
$ScriptPath = Join-Path $Root "scripts\collector_daemon_labelerexp_windows.ps1"
$Pwsh = "$PSHOME\powershell.exe"

function Start-Daemon {
  $existingPid = Read-PidFile -Path $DaemonPidFile
  if ($null -ne $existingPid -and (Test-PidAlive -PidValue $existingPid)) {
    Write-Output "collector labelerexp daemon already running pid=$existingPid"
    return
  }
  if (-not (Test-Path $ScriptPath)) {
    throw "daemon script not found: $ScriptPath"
  }

  New-Item -ItemType Directory -Force -Path $ControlDir | Out-Null
  New-Item -ItemType Directory -Force -Path $LaunchdLogDir | Out-Null

  $proc = Start-Process -FilePath $Pwsh -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ScriptPath) -WindowStyle Hidden -PassThru
  Start-Sleep -Seconds 2
  if (Test-PidAlive -PidValue $proc.Id) {
    Write-Output "collector labelerexp daemon started pid=$($proc.Id)"
  }
  else {
    throw "collector labelerexp daemon failed to start"
  }
}

function Stop-Daemon {
  $daemonPid = Read-PidFile -Path $DaemonPidFile
  if ($null -ne $daemonPid -and (Test-PidAlive -PidValue $daemonPid)) {
    Stop-Process -Id $daemonPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
  }

  $outEsc = [regex]::Escape($OutBase)
  Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match "python|powershell" -and $_.CommandLine -match "bsky_collector_v2" -and $_.CommandLine -match $outEsc
  } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }

  Remove-Item -Force $DaemonPidFile -ErrorAction SilentlyContinue
  Write-Output "collector labelerexp daemon stopped"
}

function Status-Daemon {
  $daemonPid = Read-PidFile -Path $DaemonPidFile
  if ($null -ne $daemonPid -and (Test-PidAlive -PidValue $daemonPid)) {
    Write-Output "daemon: running pid=$daemonPid"
  }
  else {
    Write-Output "daemon: not running"
  }
  Write-Output ""
  Write-Output "collector processes (labelerexp):"

  $outEsc = [regex]::Escape($OutBase)
  Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match "python|powershell" -and $_.CommandLine -match "bsky_collector_v2" -and $_.CommandLine -match $outEsc
  } | Select-Object ProcessId, Name, CommandLine
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
