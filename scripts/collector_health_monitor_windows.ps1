param(
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

function Read-JsonFile {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not (Test-Path $Path)) {
    return $null
  }
  try {
    return (Get-Content $Path -Raw | ConvertFrom-Json)
  }
  catch {
    return $null
  }
}

$Root = Get-ConfigValue -Name "ROOT" -Default "D:\BlueSky"
$OutBase = Get-ConfigValue -Name "OUT_BASE" -Default (Join-Path $Root "data_v2_full")
$ControlDir = Join-Path $OutBase "control"
$MonitorDir = Join-Path $OutBase "logs\monitor"
$LatestJsonPath = Join-Path $MonitorDir "collector_health_latest.json"
$MonitorPidFile = Join-Path $ControlDir "collector_health_monitor_win.pid"
$MonitorLogPathFile = Join-Path $ControlDir "collector_health_monitor_win.logpath"
$DaemonPidFile = Join-Path $ControlDir "collector_daemon_win.pid"
$StateWriterPidFile = Join-Path $ControlDir "state_writer_win.pid"
$DaemonCtlScript = Join-Path $Root "scripts\collector_daemon_windows_ctl.ps1"

New-Item -ItemType Directory -Force -Path $ControlDir | Out-Null
New-Item -ItemType Directory -Force -Path $MonitorDir | Out-Null

$startUtc = (Get-Date).ToUniversalTime()
$endUtc = $startUtc.AddHours($DurationHours)
$stamp = $startUtc.ToString("yyyyMMddTHHmmssZ")
$logPath = Join-Path $MonitorDir ("collector-health-monitor_{0}.log" -f $stamp)

Set-Content -Path $MonitorPidFile -Value "$PID"
Set-Content -Path $MonitorLogPathFile -Value $logPath

function Write-MonitorLog {
  param([Parameter(Mandatory = $true)][string]$Message)
  $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  Add-Content -Path $logPath -Value "$ts $Message"
}

function Latest-DayDir {
  param([Parameter(Mandatory = $true)][string]$BasePath)
  if (-not (Test-Path $BasePath)) {
    return $null
  }
  $dir = Get-ChildItem $BasePath -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
  if ($null -eq $dir) {
    return $null
  }
  return $dir.FullName
}

function Latest-HourDir {
  param([Parameter(Mandatory = $true)][string]$DayPath)
  if (-not (Test-Path $DayPath)) {
    return $null
  }
  $dir = Get-ChildItem $DayPath -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
  if ($null -eq $dir) {
    return $null
  }
  return $dir.FullName
}

function Ensure-DaemonRunning {
  $daemonPid = Read-PidFile -Path $DaemonPidFile
  if ($null -ne $daemonPid -and (Test-PidAlive -PidValue $daemonPid)) {
    return
  }
  if (-not (Test-Path $DaemonCtlScript)) {
    Write-MonitorLog "daemon ctl script missing path=$DaemonCtlScript"
    return
  }
  Write-MonitorLog "daemon not alive; attempting restart"
  try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $DaemonCtlScript start | Out-Null
    Start-Sleep -Seconds 3
    $newPid = Read-PidFile -Path $DaemonPidFile
    if ($null -ne $newPid -and (Test-PidAlive -PidValue $newPid)) {
      Write-MonitorLog "daemon restart success pid=$newPid"
    }
    else {
      Write-MonitorLog "daemon restart attempted but not running"
    }
  }
  catch {
    Write-MonitorLog ("daemon restart error err={0}" -f $_.Exception.Message)
  }
}

Write-MonitorLog ("monitor start duration_h={0} interval_s={1} pid={2}" -f $DurationHours, $IntervalSeconds, $PID)

try {
  while ($true) {
    $nowUtc = (Get-Date).ToUniversalTime()
    if ($nowUtc -ge $endUtc) {
      break
    }

    Ensure-DaemonRunning

    $daemonPid = Read-PidFile -Path $DaemonPidFile
    $daemonAlive = $false
    if ($null -ne $daemonPid) {
      $daemonAlive = Test-PidAlive -PidValue $daemonPid
    }

    $writerPid = Read-PidFile -Path $StateWriterPidFile
    $writerAlive = $false
    if ($null -ne $writerPid) {
      $writerAlive = Test-PidAlive -PidValue $writerPid
    }

    $activeJobs = (Get-CimInstance Win32_Process | Where-Object {
      $_.Name -match "python" -and $_.CommandLine -match "bsky_collector_v2 (wide-sweep|index-feed-generators|snapshot-panel|hydrate-authors|refresh-discovery|build-panel)"
    } | Measure-Object).Count

    $wideObj = $null
    $wideDay = Latest-DayDir -BasePath (Join-Path $OutBase "wide")
    if ($null -ne $wideDay) {
      $wideProgressPath = Join-Path $wideDay "progress.json"
      $wideProgress = Read-JsonFile -Path $wideProgressPath
      if ($null -ne $wideProgress) {
        $wideObj = [PSCustomObject]@{
          date_utc = (Split-Path $wideDay -Leaf)
          feeds_done = $wideProgress.feeds_done
          feeds_failed = $wideProgress.feeds_failed
          feeds_pending = $wideProgress.feeds_pending
          feeds_total = $wideProgress.feeds_total
          updated_at_utc = $wideProgress.updated_at_utc
        }
      }
    }

    $hourlyObj = $null
    $hourlyDay = Latest-DayDir -BasePath (Join-Path $OutBase "hourly")
    if ($null -ne $hourlyDay) {
      $hourDir = Latest-HourDir -DayPath $hourlyDay
      if ($null -ne $hourDir) {
        $hourProgressPath = Join-Path $hourDir "progress.json"
        $hourProgress = Read-JsonFile -Path $hourProgressPath
        if ($null -ne $hourProgress) {
          $hourlyObj = [PSCustomObject]@{
            date_utc = (Split-Path $hourlyDay -Leaf)
            hour_utc = (Split-Path $hourDir -Leaf)
            feeds_done = $hourProgress.feeds_done
            feeds_failed = $hourProgress.feeds_failed
            feeds_pending = $hourProgress.feeds_pending
            feeds_total = $hourProgress.feeds_total
            updated_at_utc = $hourProgress.updated_at_utc
          }
        }
      }
    }

    $snapshot = [PSCustomObject]@{
      time_utc = $nowUtc.ToString("yyyy-MM-ddTHH:mm:ssZ")
      monitor_pid = "$PID"
      daemon_pid = $daemonPid
      daemon_alive = $daemonAlive
      state_writer_pid = $writerPid
      state_writer_alive = $writerAlive
      active_job_processes = $activeJobs
      wide = $wideObj
      hourly = $hourlyObj
    }

    $json = $snapshot | ConvertTo-Json -Depth 8
    Set-Content -Path $LatestJsonPath -Value $json

    $wideMsg = "wide=n/a"
    if ($null -ne $wideObj) {
      $wideMsg = ("wide done={0} failed={1} pending={2} total={3} updated={4}" -f $wideObj.feeds_done, $wideObj.feeds_failed, $wideObj.feeds_pending, $wideObj.feeds_total, $wideObj.updated_at_utc)
    }

    $hourlyMsg = "hourly=n/a"
    if ($null -ne $hourlyObj) {
      $hourlyMsg = ("hourly date={0} hour={1} done={2} failed={3} pending={4} total={5} updated={6}" -f $hourlyObj.date_utc, $hourlyObj.hour_utc, $hourlyObj.feeds_done, $hourlyObj.feeds_failed, $hourlyObj.feeds_pending, $hourlyObj.feeds_total, $hourlyObj.updated_at_utc)
    }

    Write-MonitorLog ("heartbeat daemon_alive={0} writer_alive={1} active_jobs={2} {3} {4}" -f $daemonAlive, $writerAlive, $activeJobs, $wideMsg, $hourlyMsg)

    Start-Sleep -Seconds $IntervalSeconds
  }
}
finally {
  Write-MonitorLog "monitor finished"
  $cur = (Get-Content $MonitorPidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($cur -eq "$PID") {
    Remove-Item -Force $MonitorPidFile -ErrorAction SilentlyContinue
  }
}
