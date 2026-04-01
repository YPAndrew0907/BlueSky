param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet("start", "stop", "restart", "status", "logs")]
  [string]$Action,
  [double]$DurationHours = 8,
  [int]$IntervalSeconds = 300
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
$MonitorDir = Join-Path $OutBase "logs\monitor"
$MonitorScript = Join-Path $Root "scripts\collector_health_monitor_windows.ps1"
$MonitorPidFile = Join-Path $ControlDir "collector_health_monitor_win.pid"
$MonitorLogPathFile = Join-Path $ControlDir "collector_health_monitor_win.logpath"
$LatestJsonPath = Join-Path $MonitorDir "collector_health_latest.json"
$Pwsh = "$PSHOME\powershell.exe"

function Start-Monitor {
  $existingPid = Read-PidFile -Path $MonitorPidFile
  if ($null -ne $existingPid -and (Test-PidAlive -PidValue $existingPid)) {
    Write-Output "collector health monitor already running pid=$existingPid"
    return
  }
  if (-not (Test-Path $MonitorScript)) {
    throw "monitor script not found: $MonitorScript"
  }
  New-Item -ItemType Directory -Force -Path $ControlDir | Out-Null
  New-Item -ItemType Directory -Force -Path $MonitorDir | Out-Null

  $proc = Start-Process -FilePath $Pwsh -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $MonitorScript,
    "-DurationHours", "$DurationHours",
    "-IntervalSeconds", "$IntervalSeconds"
  ) -WindowStyle Hidden -PassThru
  Start-Sleep -Seconds 2
  if (Test-PidAlive -PidValue $proc.Id) {
    Write-Output "collector health monitor started pid=$($proc.Id) duration_h=$DurationHours interval_s=$IntervalSeconds"
  }
  else {
    throw "collector health monitor failed to start"
  }
}

function Stop-Monitor {
  $pidValue = Read-PidFile -Path $MonitorPidFile
  if ($null -ne $pidValue -and (Test-PidAlive -PidValue $pidValue)) {
    Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
  }
  Remove-Item -Force $MonitorPidFile -ErrorAction SilentlyContinue
  Write-Output "collector health monitor stopped"
}

function Status-Monitor {
  $pidValue = Read-PidFile -Path $MonitorPidFile
  if ($null -ne $pidValue -and (Test-PidAlive -PidValue $pidValue)) {
    Write-Output "monitor: running pid=$pidValue"
  }
  else {
    Write-Output "monitor: not running"
  }
  Write-Output ""
  if (Test-Path $MonitorLogPathFile) {
    $logPath = (Get-Content $MonitorLogPathFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    Write-Output "log_path: $logPath"
  }
  else {
    Write-Output "log_path: (missing)"
  }
  Write-Output ""
  if (Test-Path $LatestJsonPath) {
    Get-Content $LatestJsonPath -Raw
  }
  else {
    Write-Output "latest_snapshot: (missing)"
  }
}

function Logs-Monitor {
  if (Test-Path $MonitorLogPathFile) {
    $logPath = (Get-Content $MonitorLogPathFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not [string]::IsNullOrWhiteSpace($logPath) -and (Test-Path $logPath)) {
      Get-Content $logPath -Tail 120
      return
    }
  }
  $latest = Get-ChildItem $MonitorDir -Filter "collector-health-monitor_*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($null -ne $latest) {
    Get-Content $latest.FullName -Tail 120
    return
  }
  Write-Output "monitor log not found"
}

switch ($Action) {
  "start" { Start-Monitor }
  "stop" { Stop-Monitor }
  "restart" { Stop-Monitor; Start-Monitor }
  "status" { Status-Monitor }
  "logs" { Logs-Monitor }
}
