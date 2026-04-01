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

function Parse-TcpTarget {
  param([Parameter(Mandatory = $true)][string]$Raw)
  $value = $Raw.Trim()
  if (-not $value) {
    throw "empty tcp target"
  }
  if (-not $value.Contains("://")) {
    $value = "tcp://$value"
  }
  $uri = [Uri]$value
  if ([string]::IsNullOrWhiteSpace($uri.Host) -or $uri.Port -le 0) {
    throw "invalid tcp target: $Raw"
  }
  return [PSCustomObject]@{
    Host = $uri.Host
    Port = [int]$uri.Port
  }
}

$Root = Get-ConfigValue -Name "ROOT" -Default "D:\BlueSky"
$OutBase = Get-ConfigValue -Name "OUT_BASE" -Default (Join-Path $Root "data_v2_full")
$EnvPath = Get-ConfigValue -Name "ENV_PATH" -Default (Join-Path $Root "auth.env")
$PythonBin = Get-ConfigValue -Name "PYTHON_BIN" -Default (Join-Path $Root ".venv-win\Scripts\python.exe")
$CollectorMode = Get-ConfigValue -Name "COLLECTOR_MODE" -Default "micro5"
$DefaultStudyId = Get-ConfigValue -Name "DEFAULT_STUDY_ID" -Default "micro10_full_live_20260319"
$StudyId = [Environment]::GetEnvironmentVariable("STUDY_ID", "Process")
$StateWriterTcp = Get-ConfigValue -Name "STATE_WRITER_TCP" -Default "127.0.0.1:9911"
$LoopSleepSeconds = [int](Get-ConfigValue -Name "LOOP_SLEEP_S" -Default "30")
$ForceFullCollectionOnStart = Get-ConfigValue -Name "FORCE_FULL_COLLECTION_ON_START" -Default "1"

$IntervalMicroSeconds = [int](Get-ConfigValue -Name "INTERVAL_MICRO_S" -Default "600")
$IntervalSnapshotSeconds = [int](Get-ConfigValue -Name "INTERVAL_SNAPSHOT_S" -Default "3600")
$IntervalIndexSeconds = [int](Get-ConfigValue -Name "INTERVAL_INDEX_S" -Default "3600")
$IntervalHydrateSeconds = [int](Get-ConfigValue -Name "INTERVAL_HYDRATE_S" -Default "10800")
$IntervalRefreshSeconds = [int](Get-ConfigValue -Name "INTERVAL_REFRESH_S" -Default "86400")
$IntervalBuildPanelSeconds = [int](Get-ConfigValue -Name "INTERVAL_BUILD_PANEL_S" -Default "86400")
$IntervalWideSeconds = [int](Get-ConfigValue -Name "INTERVAL_WIDE_S" -Default "86400")

$EnableIndexFeedGenerators = Get-ConfigValue -Name "ENABLE_INDEX_FEED_GENERATORS" -Default "1"
$EnableHydrateAuthors = Get-ConfigValue -Name "ENABLE_HYDRATE_AUTHORS" -Default "1"
$EnableRefreshDiscovery = Get-ConfigValue -Name "ENABLE_REFRESH_DISCOVERY" -Default "1"
$EnableBuildPanel = Get-ConfigValue -Name "ENABLE_BUILD_PANEL" -Default "0"
$EnableWideSweep = Get-ConfigValue -Name "ENABLE_WIDE_SWEEP" -Default "1"

$ControlDir = Join-Path $OutBase "control"
$StateDir = Join-Path $ControlDir "daemon_state_win"
$LogDir = Join-Path $OutBase "logs\manual_runs"
$LaunchdLogDir = Join-Path $OutBase "logs\launchd"
$DaemonPidFile = Join-Path $ControlDir "collector_daemon_win.pid"
$StateWriterPidFile = Join-Path $ControlDir "state_writer_win.pid"
$StateWriterSocketFile = Join-Path $ControlDir "state_writer_win.socket"
$StateWriterLogPathFile = Join-Path $ControlDir "state_writer_win.logpath"
$StateWriterProbeScript = Join-Path $ControlDir "_state_writer_probe.py"
$DaemonLogFile = Join-Path $LaunchdLogDir "collector-daemon-win.log"
$StateWriterTarget = Parse-TcpTarget -Raw $StateWriterTcp

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

Set-Content -Path $StateWriterProbeScript -Encoding UTF8 -Value @'
import json
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
request = {
    "method": "list_feed_catalog_uris",
    "args": [],
    "kwargs": {"limit": 1},
}
payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")

with socket.create_connection((host, port), timeout=2.0) as conn:
    conn.sendall(payload)
    data = conn.recv(65536)

if not data:
    raise SystemExit(1)

line = data.split(b"\n", 1)[0]
resp = json.loads(line.decode("utf-8"))
if not isinstance(resp, dict) or not bool(resp.get("ok")):
    raise SystemExit(1)
'@

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

function Remove-StateWriterMetadata {
  Remove-Item -Force $StateWriterPidFile -ErrorAction SilentlyContinue
  Remove-Item -Force $StateWriterSocketFile -ErrorAction SilentlyContinue
  Remove-Item -Force $StateWriterLogPathFile -ErrorAction SilentlyContinue
}

function Stop-TrackedProcess {
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
  Set-Content -Path (Get-StampFile -JobName $JobName) -Value (Get-UnixEpoch)
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

function Get-DefaultStudyId {
  $preferredManifest = Join-Path $OutBase "studies\$DefaultStudyId\study_manifest.json"
  if (-not [string]::IsNullOrWhiteSpace($DefaultStudyId) -and (Test-Path $preferredManifest)) {
    return $DefaultStudyId
  }

  $studiesRoot = Join-Path $OutBase "studies"
  if (-not (Test-Path $studiesRoot)) {
    return $null
  }

  $coreCandidates = @()
  $allCandidates = @()
  $manifestFiles = Get-ChildItem -Path $studiesRoot -Filter "study_manifest.json" -Recurse -File -ErrorAction SilentlyContinue
  foreach ($manifestFile in $manifestFiles) {
    try {
      $data = Get-Content $manifestFile.FullName -Raw | ConvertFrom-Json
    }
    catch {
      continue
    }
    $candidateId = if ($null -ne $data.study_id -and -not [string]::IsNullOrWhiteSpace([string]$data.study_id)) {
      [string]$data.study_id
    }
    else {
      $manifestFile.Directory.Name
    }
    $createdAtUtc = ""
    if ($null -ne $data.created_at_utc) {
      $createdAtUtc = [string]$data.created_at_utc
    }
    $sampleFamily = ""
    if ($null -ne $data.sample_family) {
      $sampleFamily = [string]$data.sample_family
    }
    $candidate = [PSCustomObject]@{
      CreatedAtUtc = $createdAtUtc
      StudyId      = $candidateId
      SampleFamily = $sampleFamily
    }
    $allCandidates += $candidate
    if ($candidate.SampleFamily -eq "micro5_core_full") {
      $coreCandidates += $candidate
    }
  }

  $candidates = if ($coreCandidates.Count -gt 0) { $coreCandidates } else { $allCandidates }
  if ($candidates.Count -eq 0) {
    return $null
  }

  return ($candidates | Sort-Object CreatedAtUtc, StudyId | Select-Object -Last 1).StudyId
}

function Require-Micro5Config {
  if ([string]::IsNullOrWhiteSpace($script:StudyId)) {
    $script:StudyId = Get-DefaultStudyId
  }
  if ([string]::IsNullOrWhiteSpace($script:StudyId)) {
    Write-Log "micro5 config missing no study manifest found under $OutBase\studies and STUDY_ID is unset"
    throw "micro5 config missing: no study manifest found and STUDY_ID is unset"
  }
  $manifestPath = Join-Path $OutBase "studies\$script:StudyId\study_manifest.json"
  if (-not (Test-Path $manifestPath)) {
    Write-Log "micro5 config missing study_id=$script:StudyId manifest=$manifestPath"
    throw "micro5 config missing: $manifestPath"
  }
}

function Test-StateWriterResponding {
  & $PythonBin $StateWriterProbeScript $StateWriterTarget.Host "$($StateWriterTarget.Port)" *> $null
  return ($LASTEXITCODE -eq 0)
}

function Ensure-StateWriter {
  $existingPid = Read-PidFile -Path $StateWriterPidFile
  if ($null -ne $existingPid -and (Test-PidAlive -PidValue $existingPid) -and (Test-StateWriterResponding)) {
    return $true
  }

  if ($null -ne $existingPid) {
    Write-Log "state-writer unhealthy pid=$existingPid tcp=$StateWriterTcp"
    Stop-TrackedProcess -PidValue $existingPid
  }
  Remove-StateWriterMetadata

  $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
  $stdoutPath = Join-Path $LogDir "state-writer_$ts.stdout.log"
  $stderrPath = Join-Path $LogDir "state-writer_$ts.stderr.log"
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

  $ready = $false
  for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ((Test-PidAlive -PidValue $proc.Id) -and (Test-StateWriterResponding)) {
      $ready = $true
      break
    }
  }

  if ($ready) {
    Write-Log "state-writer started pid=$($proc.Id) tcp=$StateWriterTcp log=$stdoutPath"
    return $true
  }

  Stop-TrackedProcess -PidValue $proc.Id
  Remove-StateWriterMetadata
  Write-Log "state-writer failed_to_start log=$stdoutPath"
  return $false
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
    return
  }

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

function Ensure-IntervalJob {
  param(
    [Parameter(Mandatory = $true)][string]$JobName,
    [Parameter(Mandatory = $true)][int]$IntervalSeconds,
    [Parameter(Mandatory = $true)][string[]]$Args
  )
  if ($JobName -eq "build-panel") {
    $depPidFile = Get-JobPidFile -JobName "refresh-discovery"
    $depPid = Read-PidFile -Path $depPidFile
    if ($null -ne $depPid -and (Test-PidAlive -PidValue $depPid)) {
      return
    }
  }

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

function Reset-ScheduleStamps {
  param([Parameter(Mandatory = $true)][string[]]$JobNames)
  if ($ForceFullCollectionOnStart -ne "1") {
    Write-Log "startup stamp reset disabled FORCE_FULL_COLLECTION_ON_START=$ForceFullCollectionOnStart"
    return
  }
  $cleared = 0
  foreach ($jobName in $JobNames) {
    $stampFile = Get-StampFile -JobName $jobName
    if (Test-Path $stampFile) {
      Remove-Item -Force $stampFile -ErrorAction SilentlyContinue
      $cleared += 1
    }
  }
  Write-Log "startup stamp reset enabled cleared=$cleared"
}

if ($CollectorMode -eq "micro5") {
  Require-Micro5Config
}
elseif ($CollectorMode -ne "legacy_hourly") {
  Write-Log "invalid collector mode mode=$CollectorMode expected=micro5|legacy_hourly"
  throw "invalid collector mode: $CollectorMode"
}

$jobConfigs = @()
if ($CollectorMode -eq "micro5") {
  $jobConfigs += [PSCustomObject]@{
    Name = "micro-snapshot-study"
    Interval = $IntervalMicroSeconds
    Args = @(
      "-m", "bsky_collector_v2",
      "micro-snapshot-study",
      "--out-base", $OutBase,
      "--env-path", $EnvPath,
      "--study-id", $StudyId,
      "--sleep-until-window",
      "--concurrency", "16",
      "--rps", "20",
      "--feed-time-budget-s", "20",
      "--resume"
    )
  }
}
else {
  $jobConfigs += [PSCustomObject]@{
    Name = "snapshot-panel"
    Interval = $IntervalSnapshotSeconds
    Args = @(
      "-m", "bsky_collector_v2",
      "snapshot-panel",
      "--out-base", $OutBase,
      "--env-path", $EnvPath,
      "--accept-language", "en-US",
      "--vantage-id-unauth", "unauth_enUS",
      "--vantage-id-auth", "auth_enUS",
      "--viewer-modes", "unauth,auth",
      "--posts-per-feed", "50",
      "--concurrency", "16",
      "--rps", "20",
      "--feed-time-budget-s", "20",
      "--time-budget-minutes", "55",
      "--resume"
    )
  }
}

if ($EnableIndexFeedGenerators -eq "1") {
  $jobConfigs += [PSCustomObject]@{
    Name = "index-feed-generators"
    Interval = $IntervalIndexSeconds
    Args = @(
      "-m", "bsky_collector_v2",
      "index-feed-generators",
      "--out-base", $OutBase,
      "--env-path", $EnvPath,
      "--relay-host", "https://bsky.network",
      "--pds-host", "https://bsky.social",
      "--rps", "20",
      "--time-budget-minutes", "55",
      "--resume"
    )
  }
}

if ($EnableHydrateAuthors -eq "1") {
  $jobConfigs += [PSCustomObject]@{
    Name = "hydrate-authors"
    Interval = $IntervalHydrateSeconds
    Args = @(
      "-m", "bsky_collector_v2",
      "hydrate-authors",
      "--out-base", $OutBase,
      "--env-path", $EnvPath,
      "--accept-language", "en-US",
      "--vantage-id-unauth", "unauth_enUS",
      "--max-authors", "50000",
      "--batch-size", "25",
      "--concurrency", "8",
      "--rps", "20",
      "--resume"
    )
  }
}

if ($EnableRefreshDiscovery -eq "1") {
  $jobConfigs += [PSCustomObject]@{
    Name = "refresh-discovery"
    Interval = $IntervalRefreshSeconds
    Args = @(
      "-m", "bsky_collector_v2",
      "refresh-discovery",
      "--out-base", $OutBase,
      "--env-path", $EnvPath,
      "--accept-language", "en-US",
      "--vantage-id-unauth", "unauth_enUS",
      "--vantage-id-auth", "auth_enUS",
      "--concurrency", "16",
      "--rps", "20",
      "--resume"
    )
  }
}

if ($EnableBuildPanel -eq "1") {
  $jobConfigs += [PSCustomObject]@{
    Name = "build-panel"
    Interval = $IntervalBuildPanelSeconds
    Args = @(
      "-m", "bsky_collector_v2",
      "build-panel",
      "--out-base", $OutBase,
      "--env-path", $EnvPath,
      "--concurrency", "16",
      "--rps", "20"
    )
  }
}

if ($EnableWideSweep -eq "1") {
  $jobConfigs += [PSCustomObject]@{
    Name = "wide-sweep"
    Interval = $IntervalWideSeconds
    Args = @(
      "-m", "bsky_collector_v2",
      "wide-sweep",
      "--out-base", $OutBase,
      "--env-path", $EnvPath,
      "--accept-language", "en-US",
      "--vantage-id-unauth", "unauth_enUS",
      "--posts-per-feed", "20",
      "--n-feeds", "10000",
      "--concurrency", "16",
      "--rps", "20",
      "--feed-time-budget-s", "20",
      "--time-budget-minutes", "55",
      "--resume"
    )
  }
}

$jobNames = @($jobConfigs | ForEach-Object { $_.Name })
Write-Log "collector-daemon-win starting pid=$PID mode=$CollectorMode root=$Root out_base=$OutBase study_id=$StudyId"
Reset-ScheduleStamps -JobNames $jobNames

try {
  while ($true) {
    if (-not (Ensure-StateWriter)) {
      Start-Sleep -Seconds $LoopSleepSeconds
      continue
    }
    foreach ($jobCfg in $jobConfigs) {
      Ensure-IntervalJob -JobName $jobCfg.Name -IntervalSeconds ([int]$jobCfg.Interval) -Args ([string[]]$jobCfg.Args)
    }
    Start-Sleep -Seconds $LoopSleepSeconds
  }
}
catch {
  Write-Log ("fatal_error type={0} message={1}" -f $_.Exception.GetType().FullName, $_.Exception.Message)
  throw
}
finally {
  $current = (Get-Content $DaemonPidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($current -eq "$PID") {
    Remove-Item -Force $DaemonPidFile -ErrorAction SilentlyContinue
  }
}
