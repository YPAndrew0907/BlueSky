param(
  [string]$RepoRoot = "D:\BlueSky",
  [int]$DurationHours = 4,
  [int]$IntervalSeconds = 300,
  [string]$JsonlPath = "",
  [string]$TextPath = "",
  [string]$LatestJsonPath = "",
  [string]$LatestTextPath = "",
  [switch]$RunOnce
)

$ErrorActionPreference = "Stop"

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

function Read-FirstLine {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not (Test-Path $Path)) {
    return $null
  }
  return (Get-Content $Path -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Test-PidAlive {
  param([Parameter(Mandatory = $true)][string]$PidPath)
  $raw = Read-FirstLine -Path $PidPath
  if ([string]::IsNullOrWhiteSpace($raw)) {
    return $false
  }
  $pidValue = 0
  if (-not [int]::TryParse($raw.Trim(), [ref]$pidValue)) {
    return $false
  }
  return ($null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue))
}

function Sum-IntProperties {
  param([object]$Obj)
  if ($null -eq $Obj) {
    return 0
  }
  $sum = 0
  foreach ($property in $Obj.PSObject.Properties) {
    try {
      $sum += [int]$property.Value
    }
    catch {
    }
  }
  return $sum
}

function Safe-Value {
  param($Value)
  if ($null -eq $Value) {
    return "-"
  }
  if ($Value -is [System.Array]) {
    if ($Value.Count -eq 0) {
      return "-"
    }
    return ($Value -join ",")
  }
  return [string]$Value
}

function Get-LatestPublicLog {
  param([Parameter(Mandatory = $true)][string]$RepoRoot)
  $manualDir = Join-Path $RepoRoot "data_v2_full\logs\manual_runs"
  return Get-ChildItem $manualDir -Filter "public_omnivore_*.log" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime |
    Select-Object -Last 1
}

function Get-PublicBackfillHealthy {
  param(
    [bool]$PublicPidAlive,
    [object]$PublicRun,
    [object]$LatestPublicLog
  )

  if ($PublicPidAlive) {
    return $true
  }
  if ($null -ne $LatestPublicLog -and $LatestPublicLog.LastWriteTime -ge (Get-Date).AddMinutes(-15)) {
    return $true
  }
  if ($null -eq $PublicRun) {
    return $false
  }
  if ($PublicRun.started_at_utc -and -not $PublicRun.finished_at_utc) {
    return $true
  }
  if ($PublicRun.finished_at_utc) {
    try {
      $finishedUtc = [DateTime]::Parse($PublicRun.finished_at_utc).ToUniversalTime()
      if ($finishedUtc -ge (Get-Date).ToUniversalTime().AddMinutes(-15)) {
        return $true
      }
    }
    catch {
    }
  }
  return $false
}

function New-Sample {
  param([Parameter(Mandatory = $true)][string]$RepoRoot)

  $controlDir = Join-Path $RepoRoot "data_v2_full\control"
  $nowUtc = (Get-Date).ToUniversalTime()
  $dateUtc = $nowUtc.ToString("yyyy-MM-dd")

  $public = Read-JsonFile -Path (Join-Path $controlDir "public_omnibus_last_run.json")
  $interactionsProgress = Read-JsonFile -Path (Join-Path $RepoRoot "data_v2_full\interactions\$dateUtc\progress.json")
  $interactionsRun = Read-JsonFile -Path (Join-Path $RepoRoot "data_v2_full\interactions\$dateUtc\run_manifest.json")
  $rq1Progress = Read-JsonFile -Path (Join-Path $RepoRoot "data_v2_full\rq1_factors\$dateUtc\shard_000\progress.json")
  $rq1Run = Read-JsonFile -Path (Join-Path $RepoRoot "data_v2_full\rq1_factors\$dateUtc\shard_000\run_manifest.json")
  $latestPublicLog = Get-LatestPublicLog -RepoRoot $RepoRoot
  $publicPidAlive = Test-PidAlive -PidPath (Join-Path $controlDir "collector_public_omnivore_daemon.pid")
  $stateWriterAlive = Test-PidAlive -PidPath (Join-Path $controlDir "state_writer.pid")
  $publicDaemonAlive = Get-PublicBackfillHealthy -PublicPidAlive $publicPidAlive -PublicRun $public -LatestPublicLog $latestPublicLog

  $issues = New-Object System.Collections.Generic.List[string]
  if (-not $publicDaemonAlive) {
    $issues.Add("public_daemon_down")
  }
  if (-not $stateWriterAlive) {
    $issues.Add("state_writer_down")
  }
  if ($null -eq $interactionsProgress) {
    $issues.Add("interactions_progress_missing")
  }
  if ($null -eq $rq1Progress) {
    $issues.Add("rq1_progress_missing")
  }
  if ($null -ne $public -and $public.PSObject.Properties.Name -contains "success" -and $public.success -eq $false) {
    $issues.Add("last_public_run_failed")
  }
  if ($null -ne $latestPublicLog -and $latestPublicLog.LastWriteTime -lt (Get-Date).AddMinutes(-15)) {
    $issues.Add("public_log_stale_gt_15m")
  }

  return [ordered]@{
    observed_at_utc = $nowUtc.ToString("yyyy-MM-ddTHH:mm:ssZ")
    public_daemon_alive = $publicDaemonAlive
    public_pid_alive = $publicPidAlive
    state_writer_alive = $stateWriterAlive
    latest_public_log = if ($latestPublicLog) { $latestPublicLog.FullName } else { $null }
    public_run = if ($public) {
      [ordered]@{
        run_id = $public.run_id
        started_at_utc = $public.started_at_utc
        finished_at_utc = $public.finished_at_utc
        success = $public.success
        step_results = $public.step_results
      }
    }
    else {
      $null
    }
    interactions = if ($interactionsProgress) {
      [ordered]@{
        started_at_utc = $interactionsProgress.started_at_utc
        updated_at_utc = $interactionsProgress.updated_at_utc
        phase = $interactionsProgress.details.phase
        selected_posts = $interactionsProgress.details.selected_posts
        feeds_total = $interactionsProgress.feeds_total
        feeds_done = $interactionsProgress.feeds_done
        feeds_failed = $interactionsProgress.feeds_failed
        total_rows_written = Sum-IntProperties -Obj $interactionsProgress.rows_written
        run_success = if ($interactionsRun) { $interactionsRun.success } else { $null }
      }
    }
    else {
      $null
    }
    rq1 = if ($rq1Progress) {
      [ordered]@{
        started_at_utc = $rq1Progress.started_at_utc
        updated_at_utc = $rq1Progress.updated_at_utc
        phase = $rq1Progress.details.phase
        selected_posts = $rq1Progress.details.selected_posts
        feeds_total = $rq1Progress.feeds_total
        feeds_done = $rq1Progress.feeds_done
        feeds_failed = $rq1Progress.feeds_failed
        total_rows_written = Sum-IntProperties -Obj $rq1Progress.rows_written
        run_success = if ($rq1Run) { $rq1Run.success } else { $null }
      }
    }
    else {
      $null
    }
    issues = $issues
  }
}

if (-not $JsonlPath -or -not $TextPath) {
  $observerDir = Join-Path $RepoRoot "data_v2_full\logs\observer"
  New-Item -ItemType Directory -Force -Path $observerDir | Out-Null
  $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
  if (-not $JsonlPath) {
    $JsonlPath = Join-Path $observerDir ("backfill-watch_{0}.jsonl" -f $stamp)
  }
  if (-not $TextPath) {
    $TextPath = Join-Path $observerDir ("backfill-watch_{0}.txt" -f $stamp)
  }
}

if (-not $LatestJsonPath) {
  $LatestJsonPath = Join-Path $RepoRoot "data_v2_full\control\backfill_watch.latest.json"
}
if (-not $LatestTextPath) {
  $LatestTextPath = Join-Path $RepoRoot "data_v2_full\control\backfill_watch.latest.txt"
}

New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($JsonlPath)) | Out-Null
New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($TextPath)) | Out-Null
New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($LatestJsonPath)) | Out-Null
New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($LatestTextPath)) | Out-Null

$watchStartUtc = (Get-Date).ToUniversalTime()
$watchEndUtc = $watchStartUtc.AddHours($DurationHours)
$header = "watch_start_utc={0} watch_end_utc={1} interval_s={2} jsonl={3}" -f `
  $watchStartUtc.ToString("yyyy-MM-ddTHH:mm:ssZ"), `
  $watchEndUtc.ToString("yyyy-MM-ddTHH:mm:ssZ"), `
  $IntervalSeconds, `
  $JsonlPath

Set-Content -Path $TextPath -Value $header -Encoding utf8
Set-Content -Path $LatestTextPath -Value $header -Encoding utf8

do {
  try {
    $sample = New-Sample -RepoRoot $RepoRoot
    $json = $sample | ConvertTo-Json -Depth 8 -Compress
    Add-Content -Path $JsonlPath -Value $json -Encoding utf8
    Set-Content -Path $LatestJsonPath -Value ($sample | ConvertTo-Json -Depth 8) -Encoding utf8

    $summary = "[{0}] public_daemon={1} writer={2} public_started={3} public_finished={4} public_success={5} interactions_phase={6} interactions_selected={7} interactions_rows={8} rq1_phase={9} rq1_selected={10} rq1_rows={11} issues={12}" -f `
      (Safe-Value $sample.observed_at_utc), `
      (Safe-Value $sample.public_daemon_alive), `
      (Safe-Value $sample.state_writer_alive), `
      (Safe-Value $sample.public_run.started_at_utc), `
      (Safe-Value $sample.public_run.finished_at_utc), `
      (Safe-Value $sample.public_run.success), `
      (Safe-Value $sample.interactions.phase), `
      (Safe-Value $sample.interactions.selected_posts), `
      (Safe-Value $sample.interactions.total_rows_written), `
      (Safe-Value $sample.rq1.phase), `
      (Safe-Value $sample.rq1.selected_posts), `
      (Safe-Value $sample.rq1.total_rows_written), `
      (Safe-Value $sample.issues)
    Add-Content -Path $TextPath -Value $summary -Encoding utf8
    Set-Content -Path $LatestTextPath -Value $summary -Encoding utf8
  }
  catch {
    $errLine = "[{0}] watcher_error={1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"), $_
    Add-Content -Path $TextPath -Value $errLine -Encoding utf8
    Set-Content -Path $LatestTextPath -Value $errLine -Encoding utf8
  }

  if ($RunOnce) {
    break
  }
  Start-Sleep -Seconds $IntervalSeconds
}
while ((Get-Date).ToUniversalTime() -lt $watchEndUtc)
