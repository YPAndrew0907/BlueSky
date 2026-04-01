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

function Get-UnixEpoch {
  return [int][Math]::Floor(((Get-Date).ToUniversalTime() - [datetime]"1970-01-01T00:00:00Z").TotalSeconds)
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
$SourceOutBase = Get-ConfigValue -Name "SOURCE_OUT_BASE" -Default (Join-Path $Root "data_v2_full")
$EnvPath = Get-ConfigValue -Name "ENV_PATH" -Default (Join-Path $Root "auth.env")
$PythonBin = Get-ConfigValue -Name "PYTHON_BIN" -Default (Join-Path $Root ".venv-win\Scripts\python.exe")
$StateWriterTcp = Get-ConfigValue -Name "STATE_WRITER_TCP" -Default "127.0.0.1:9921"
$LoopSleepSeconds = [int](Get-ConfigValue -Name "LOOP_SLEEP_S" -Default "30")
$ForceFullCollectionOnStart = Get-ConfigValue -Name "FORCE_FULL_COLLECTION_ON_START" -Default "0"

$AcceptLabelers = Get-ConfigValue -Name "LABELEREXP_ACCEPT_LABELERS" -Default "did:plc:ar7c4by46qjdydhdevvrndac,did:plc:uac6er53o2pvr5y2qmvaf7hw,did:plc:l624mewisyr6hymexmrjkprc,did:plc:d2mkddsbmnrgr3domzg5qexf,did:plc:cnn3jrtucivembf66xe6fdfs"

$IntervalPanelSeconds = [int](Get-ConfigValue -Name "INTERVAL_PANEL_S" -Default "21600")
$IntervalSnapshotSeconds = [int](Get-ConfigValue -Name "INTERVAL_SNAPSHOT_S" -Default "3600")

$ControlDir = Join-Path $OutBase "control"
$StateDir = Join-Path $ControlDir "daemon_state_win"
$LogDir = Join-Path $OutBase "logs\manual_runs"
$LaunchdLogDir = Join-Path $OutBase "logs\launchd"
$DaemonPidFile = Join-Path $ControlDir "collector_daemon_labelerexp_win.pid"
$StateWriterPidFile = Join-Path $ControlDir "state_writer_labelerexp_win.pid"
$StateWriterSocketFile = Join-Path $ControlDir "state_writer_labelerexp_win.socket"
$StateWriterLogPathFile = Join-Path $ControlDir "state_writer_labelerexp_win.logpath"
$DaemonLogFile = Join-Path $LaunchdLogDir "collector-daemon-labelerexp-win.log"

New-Item -ItemType Directory -Force -Path $ControlDir | Out-Null
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $LaunchdLogDir | Out-Null

if (-not (Test-Path $PythonBin)) {
  throw "Python executable not found: $PythonBin"
}
if (-not (Test-Path $EnvPath)) {
  throw "Auth env not found: $EnvPath"
}

Set-Content -Path $DaemonPidFile -Value "$PID"

function Write-Log {
  param([Parameter(Mandatory = $true)][string]$Message)
  $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  Add-Content -Path $DaemonLogFile -Value "$ts $Message"
}

function Get-JobPidFile {
  param([Parameter(Mandatory = $true)][string]$JobName)
  return (Join-Path $ControlDir "$JobName.pid")
}

function Get-StampFile {
  param([Parameter(Mandatory = $true)][string]$JobName)
  return (Join-Path $StateDir "$JobName.last_start_epoch")
}

function Is-Due {
  param(
    [Parameter(Mandatory = $true)][string]$JobName,
    [Parameter(Mandatory = $true)][int]$IntervalSeconds
  )
  $stampFile = Get-StampFile -JobName $JobName
  $now = Get-UnixEpoch
  if (-not (Test-Path $stampFile)) {
    return $true
  }
  $lastRaw = (Get-Content $stampFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ([string]::IsNullOrWhiteSpace($lastRaw)) {
    return $true
  }
  $last = 0
  if (-not [int]::TryParse($lastRaw.Trim(), [ref]$last)) {
    return $true
  }
  return (($now - $last) -ge $IntervalSeconds)
}

function Mark-Started {
  param([Parameter(Mandatory = $true)][string]$JobName)
  $stampFile = Get-StampFile -JobName $JobName
  Set-Content -Path $stampFile -Value (Get-UnixEpoch)
}

function Clear-StalePid {
  param([Parameter(Mandatory = $true)][string]$JobName)
  $pidFile = Get-JobPidFile -JobName $JobName
  $pidValue = Read-PidFile -Path $pidFile
  if ($null -eq $pidValue) {
    return
  }
  if (-not (Test-PidAlive -PidValue $pidValue)) {
    Remove-Item -Force $pidFile -ErrorAction SilentlyContinue
    Write-Log "stale pid removed job=$JobName pid=$pidValue"
  }
}

function Ensure-StateWriter {
  $existingPid = Read-PidFile -Path $StateWriterPidFile
  if ($null -ne $existingPid -and (Test-PidAlive -PidValue $existingPid)) {
    return
  }

  Remove-Item -Force $StateWriterPidFile -ErrorAction SilentlyContinue
  $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
  $stdoutPath = Join-Path $LogDir "state-writer_labelerexp_$ts.stdout.log"
  $stderrPath = Join-Path $LogDir "state-writer_labelerexp_$ts.stderr.log"
  $args = @(
    "-m", "bsky_collector_v2",
    "state-writer",
    "--out-base", $OutBase,
    "--tcp", $StateWriterTcp,
    "--log-level", "info"
  )
  $proc = Start-Process -FilePath $PythonBin -ArgumentList $args -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
  Set-Content -Path $StateWriterPidFile -Value "$($proc.Id)"
  Set-Content -Path $StateWriterSocketFile -Value "tcp://$StateWriterTcp"
  Set-Content -Path $StateWriterLogPathFile -Value $stdoutPath
  Start-Sleep -Seconds 1
  if (Test-PidAlive -PidValue $proc.Id) {
    Write-Log "state-writer started pid=$($proc.Id) tcp=$StateWriterTcp log=$stdoutPath"
  }
  else {
    Write-Log "state-writer failed_to_start log=$stdoutPath"
  }
}

function Run-JobSync {
  param(
    [Parameter(Mandatory = $true)][string]$JobName,
    [Parameter(Mandatory = $true)][int]$IntervalSeconds,
    [Parameter(Mandatory = $true)][string[]]$Args
  )
  $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
  $stdoutPath = Join-Path $LogDir ("{0}_{1}.stdout.log" -f $JobName, $ts)
  $stderrPath = Join-Path $LogDir ("{0}_{1}.stderr.log" -f $JobName, $ts)
  $env:BSKY_STATE_WRITER_SOCKET = "tcp://$StateWriterTcp"
  $proc = Start-Process -FilePath $PythonBin -ArgumentList $Args -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -Wait -PassThru
  Mark-Started -JobName $JobName
  if ($proc.ExitCode -eq 0) {
    Write-Log "job_finished_sync job=$JobName exit=0 log=$stdoutPath"
  }
  else {
    $stampFile = Get-StampFile -JobName $JobName
    $backdate = $IntervalSeconds - 300
    if ($backdate -lt 0) {
      $backdate = 0
    }
    $retryEpoch = (Get-UnixEpoch) - $backdate
    if ($retryEpoch -lt 0) {
      $retryEpoch = 0
    }
    Set-Content -Path $stampFile -Value $retryEpoch
    Write-Log "job_failed_sync job=$JobName exit=$($proc.ExitCode) log=$stdoutPath"
  }
}

function Start-JobNow {
  param(
    [Parameter(Mandatory = $true)][string]$JobName,
    [Parameter(Mandatory = $true)][int]$IntervalSeconds,
    [Parameter(Mandatory = $true)][string[]]$Args
  )
  $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
  $stdoutPath = Join-Path $LogDir ("{0}_{1}.stdout.log" -f $JobName, $ts)
  $stderrPath = Join-Path $LogDir ("{0}_{1}.stderr.log" -f $JobName, $ts)
  $env:BSKY_STATE_WRITER_SOCKET = "tcp://$StateWriterTcp"
  $proc = Start-Process -FilePath $PythonBin -ArgumentList $Args -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
  $pidFile = Get-JobPidFile -JobName $JobName
  Set-Content -Path $pidFile -Value "$($proc.Id)"
  Mark-Started -JobName $JobName

  Start-Sleep -Seconds 1
  if (Test-PidAlive -PidValue $proc.Id) {
    Write-Log "job_started job=$JobName pid=$($proc.Id) log=$stdoutPath"
  }
  else {
    # Some jobs can legitimately finish in <1s (e.g., panel builds). Treat a fast exit
    # with exit code 0 as success instead of forcing a retry loop.
    $hasExited = $false
    $exitCode = $null
    try {
      $proc.WaitForExit()
      $proc.Refresh()
      $hasExited = $proc.HasExited
      if ($hasExited) {
        $exitCode = $proc.ExitCode
      }
    }
    catch { }

    if ($hasExited -and $exitCode -eq 0) {
      Remove-Item -Force $pidFile -ErrorAction SilentlyContinue
      Write-Log "job_finished_fast job=$JobName exit=0 log=$stdoutPath"
      return
    }

    $stampFile = Get-StampFile -JobName $JobName
    $backdate = $IntervalSeconds - 300
    if ($backdate -lt 0) {
      $backdate = 0
    }
    $retryEpoch = (Get-UnixEpoch) - $backdate
    if ($retryEpoch -lt 0) {
      $retryEpoch = 0
    }
    Set-Content -Path $stampFile -Value $retryEpoch
    Remove-Item -Force $pidFile -ErrorAction SilentlyContinue
    Write-Log "job_failed_fast job=$JobName log=$stdoutPath"
  }
}

function Ensure-IntervalJob {
  param(
    [Parameter(Mandatory = $true)][string]$JobName,
    [Parameter(Mandatory = $true)][int]$IntervalSeconds,
    [Parameter(Mandatory = $true)][string[]]$Args
  )
  Clear-StalePid -JobName $JobName
  $pidFile = Get-JobPidFile -JobName $JobName
  $existingPid = Read-PidFile -Path $pidFile
  if ($null -ne $existingPid -and (Test-PidAlive -PidValue $existingPid)) {
    return
  }
  if (-not (Is-Due -JobName $JobName -IntervalSeconds $IntervalSeconds)) {
    return
  }
  Start-JobNow -JobName $JobName -IntervalSeconds $IntervalSeconds -Args $Args
}

function Ensure-IntervalJobSync {
  param(
    [Parameter(Mandatory = $true)][string]$JobName,
    [Parameter(Mandatory = $true)][int]$IntervalSeconds,
    [Parameter(Mandatory = $true)][string[]]$Args
  )
  # Run synchronously so we can reliably observe exit codes even when a job finishes in <1s
  # (Start-Process + redirection in Windows PowerShell doesn't expose ExitCode without -Wait).
  Clear-StalePid -JobName $JobName
  $pidFile = Get-JobPidFile -JobName $JobName
  $existingPid = Read-PidFile -Path $pidFile
  if ($null -ne $existingPid -and (Test-PidAlive -PidValue $existingPid)) {
    return
  }
  if (-not (Is-Due -JobName $JobName -IntervalSeconds $IntervalSeconds)) {
    return
  }
  Run-JobSync -JobName $JobName -IntervalSeconds $IntervalSeconds -Args $Args
}

function Reset-ScheduleStamps {
  if ($ForceFullCollectionOnStart -ne "1") {
    Write-Log "startup stamp reset disabled FORCE_FULL_COLLECTION_ON_START=$ForceFullCollectionOnStart"
    return
  }
  $jobs = @(
    "build-labelerexp-panel",
    "snapshot-panel"
  )
  $cleared = 0
  foreach ($jobName in $jobs) {
    $stampFile = Get-StampFile -JobName $jobName
    if (Test-Path $stampFile) {
      Remove-Item -Force $stampFile -ErrorAction SilentlyContinue
      $cleared += 1
    }
  }
  Write-Log "startup stamp reset enabled cleared=$cleared"
}

$jobConfigs = @(
  [PSCustomObject]@{
    Name = "build-labelerexp-panel"
    Interval = $IntervalPanelSeconds
    Args = @(
      "-m", "bsky_collector_v2",
      "build-labelerexp-panel",
      "--out-base", $OutBase,
      "--source-out-base", $SourceOutBase,
      "--bucket", "suggested_labelerexp",
      "--concurrency", "4",
      "--rps", "2"
    )
  },
  [PSCustomObject]@{
    Name = "snapshot-panel"
    Interval = $IntervalSnapshotSeconds
    Args = @(
      "-m", "bsky_collector_v2",
      "snapshot-panel",
      "--out-base", $OutBase,
      "--appview-host", "https://bsky.social",
      "--env-path", $EnvPath,
      "--accept-language", "en-US",
      "--accept-labelers", $AcceptLabelers,
      "--vantage-id-unauth", "unauth_enUS_labelerexp",
      "--vantage-id-auth", "auth_enUS_labelerexp",
      "--viewer-modes", "auth",
      "--include-author-labels",
      "--posts-per-feed", "50",
      "--concurrency", "8",
      "--rps", "10",
      "--feed-time-budget-s", "20",
      "--time-budget-minutes", "55",
      "--resume"
    )
  }
)

Write-Log "collector-daemon-labelerexp-win starting pid=$PID root=$Root out_base=$OutBase source_out_base=$SourceOutBase"
Reset-ScheduleStamps

try {
  Ensure-StateWriter

  # Bootstrap: ensure panel exists before we ever attempt snapshot-panel.
  $panelPath = Join-Path $OutBase "panel\\panel_v1.csv"
  if (-not (Test-Path $panelPath)) {
    Write-Log "panel missing; bootstrapping build-labelerexp-panel"
    $cfg = $jobConfigs | Where-Object { $_.Name -eq "build-labelerexp-panel" } | Select-Object -First 1
    if ($null -ne $cfg) {
      Run-JobSync -JobName $cfg.Name -IntervalSeconds ([int]$cfg.Interval) -Args ([string[]]$cfg.Args)
    }
  }

  while ($true) {
    Ensure-StateWriter
    foreach ($jobCfg in $jobConfigs) {
      if ($jobCfg.Name -eq "build-labelerexp-panel") {
        Ensure-IntervalJobSync -JobName $jobCfg.Name -IntervalSeconds ([int]$jobCfg.Interval) -Args ([string[]]$jobCfg.Args)
      }
      else {
        Ensure-IntervalJob -JobName $jobCfg.Name -IntervalSeconds ([int]$jobCfg.Interval) -Args ([string[]]$jobCfg.Args)
      }
    }
    Start-Sleep -Seconds $LoopSleepSeconds
  }
}
finally {
  $current = (Get-Content $DaemonPidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($current -eq "$PID") {
    Remove-Item -Force $DaemonPidFile -ErrorAction SilentlyContinue
  }
}
