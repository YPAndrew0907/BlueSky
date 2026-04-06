param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = "",
    [string]$DataRoot = "",
    [string]$StudyId = "micro10_full_live_20260319",
    [string]$OutDir = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $PythonExe) {
    $PythonExe = Join-Path $RepoRoot ".venv-win\Scripts\python.exe"
}
if (-not $DataRoot) {
    $DataRoot = Join-Path $RepoRoot "data_v2_full"
}
if (-not $OutDir) {
    $OutDir = Join-Path $RepoRoot "output\analysis_demo_20260405_fullrun"
}

$OutDir = [System.IO.Path]::GetFullPath($OutDir)
$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$PythonExe = [System.IO.Path]::GetFullPath($PythonExe)
$DataRoot = [System.IO.Path]::GetFullPath($DataRoot)

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}
if (-not (Test-Path -LiteralPath $DataRoot)) {
    throw "Data root not found: $DataRoot"
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$LogDir = Join-Path $OutDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$PipelineLog = Join-Path $OutDir "pipeline.log"
$StatusPath = Join-Path $OutDir "pipeline_status.json"
$SignatureCachePath = Join-Path $OutDir "content_signature_cache_micro10_full_live_20260319.json"

if (Test-Path -LiteralPath $PipelineLog) {
    Remove-Item -LiteralPath $PipelineLog -Force
}
if (Test-Path -LiteralPath $StatusPath) {
    Remove-Item -LiteralPath $StatusPath -Force
}

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    $line = "[$timestamp] $Message"
    Add-Content -LiteralPath $PipelineLog -Value $line
    Write-Host $line
}

function Write-Status {
    param(
        [string]$State,
        [string]$CurrentStep,
        [array]$StepStates,
        [string]$StartedAt,
        [string]$FinishedAt = ""
    )

    $payload = [ordered]@{
        state = $State
        repo_root = $RepoRoot
        python_exe = $PythonExe
        data_root = $DataRoot
        study_id = $StudyId
        out_dir = $OutDir
        started_at = $StartedAt
        finished_at = $FinishedAt
        current_step = $CurrentStep
        steps = $StepStates
    }
    $payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $StatusPath -Encoding UTF8
}

function New-Step {
    param(
        [string]$Name,
        [string[]]$StepArgs
    )
    return [ordered]@{
        name = $Name
        arguments = @($StepArgs)
        status = "pending"
        started_at = ""
        finished_at = ""
        duration_seconds = $null
        exit_code = $null
        stdout_log = (Join-Path $LogDir "$Name.stdout.log")
        stderr_log = (Join-Path $LogDir "$Name.stderr.log")
    }
}

$steps = @(
    (New-Step "first_pass" @(
        "scripts\run_dced_first_pass.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_first_pass_micro10_full.json")
    )),
    (New-Step "gap_metrics_24h" @(
        "scripts\run_dced_gap_metrics.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_gap_metrics_micro10_full_24h.json"),
        "--out-csv", (Join-Path $OutDir "dced_gap_metrics_top_contexts_micro10_full_24h.csv"),
        "--max-age-hours", "24"
    )),
    (New-Step "timing_upgrade_24h_age_window" @(
        "scripts\run_dced_timing_upgrade.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_timing_upgrade_micro10_full_24h.json"),
        "--max-age-hours", "24",
        "--riskset-mode", "age_window"
    )),
    (New-Step "trajectory_24h_1h" @(
        "scripts\run_dced_trajectory_gap_metrics.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_trajectory_gap_metrics_micro10_full_24h_1h.json"),
        "--max-age-hours", "24",
        "--riskset-mode", "age_window",
        "--early-window-hours", "1"
    )),
    (New-Step "gatekeeping_audit_24h_1h" @(
        "scripts\run_dced_gatekeeping_audit.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--model-json", (Join-Path $OutDir "dced_trajectory_gap_metrics_micro10_full_24h_1h.json"),
        "--out-json", (Join-Path $OutDir "dced_gatekeeping_audit_micro10_full_24h_1h.json"),
        "--max-age-hours", "24",
        "--early-window-hours", "1"
    )),
    (New-Step "trajectory_24h_1h_availability_strict10m" @(
        "scripts\run_dced_trajectory_gap_metrics.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_trajectory_gap_metrics_micro10_full_24h_1h_availability_strict10m.json"),
        "--max-age-hours", "24",
        "--riskset-mode", "age_window",
        "--early-window-hours", "1",
        "--availability-anchor", "availability_time",
        "--max-first-monitor-delay-minutes", "10",
        "--strict-cohort-mode", "row"
    )),
    (New-Step "trajectory_24h_1h_availability_strict20m" @(
        "scripts\run_dced_trajectory_gap_metrics.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_trajectory_gap_metrics_micro10_full_24h_1h_availability_strict20m.json"),
        "--max-age-hours", "24",
        "--riskset-mode", "age_window",
        "--early-window-hours", "1",
        "--availability-anchor", "availability_time",
        "--max-first-monitor-delay-minutes", "20",
        "--strict-cohort-mode", "row"
    )),
    (New-Step "trajectory_24h_1h_availability_strict30m" @(
        "scripts\run_dced_trajectory_gap_metrics.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_trajectory_gap_metrics_micro10_full_24h_1h_availability_strict30m.json"),
        "--max-age-hours", "24",
        "--riskset-mode", "age_window",
        "--early-window-hours", "1",
        "--availability-anchor", "availability_time",
        "--max-first-monitor-delay-minutes", "30",
        "--strict-cohort-mode", "row"
    )),
    (New-Step "trajectory_24h_1h_availability_strict20m_context" @(
        "scripts\run_dced_trajectory_gap_metrics.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_trajectory_gap_metrics_micro10_full_24h_1h_availability_strict20m_context.json"),
        "--max-age-hours", "24",
        "--riskset-mode", "age_window",
        "--early-window-hours", "1",
        "--availability-anchor", "availability_time",
        "--max-first-monitor-delay-minutes", "20",
        "--strict-cohort-mode", "context"
    )),
    (New-Step "trajectory_24h_1h_availability_strict20m_context_ever_seen" @(
        "scripts\run_dced_trajectory_gap_metrics.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_trajectory_gap_metrics_micro10_full_24h_1h_availability_strict20m_context_ever_seen.json"),
        "--max-age-hours", "24",
        "--riskset-mode", "ever_seen_in_feed",
        "--early-window-hours", "1",
        "--availability-anchor", "availability_time",
        "--max-first-monitor-delay-minutes", "20",
        "--strict-cohort-mode", "context"
    )),
    (New-Step "cluster_purity_audit_24h_1h_availability_strict20m" @(
        "scripts\run_dced_cluster_purity_audit.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_cluster_purity_audit_micro10_full_24h_1h_availability_strict20m.json"),
        "--out-csv", (Join-Path $OutDir "dced_cluster_purity_audit_micro10_full_24h_1h_availability_strict20m.csv"),
        "--max-age-hours", "24",
        "--riskset-mode", "age_window",
        "--early-window-hours", "1",
        "--availability-anchor", "availability_time",
        "--max-first-monitor-delay-minutes", "20",
        "--strict-cohort-mode", "row"
    )),
    (New-Step "cluster_purity_audit_24h_1h_availability_strict20m_context_ever_seen" @(
        "scripts\run_dced_cluster_purity_audit.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_cluster_purity_audit_micro10_full_24h_1h_availability_strict20m_context_ever_seen.json"),
        "--out-csv", (Join-Path $OutDir "dced_cluster_purity_audit_micro10_full_24h_1h_availability_strict20m_context_ever_seen.csv"),
        "--max-age-hours", "24",
        "--riskset-mode", "ever_seen_in_feed",
        "--early-window-hours", "1",
        "--availability-anchor", "availability_time",
        "--max-first-monitor-delay-minutes", "20",
        "--strict-cohort-mode", "context"
    )),
    (New-Step "trajectory_24h_1h_availability_strict20m_context_ever_seen_content_same" @(
        "scripts\run_dced_trajectory_gap_metrics_content_same.py",
        "--root", $DataRoot,
        "--study-id", $StudyId,
        "--out-json", (Join-Path $OutDir "dced_trajectory_gap_metrics_micro10_full_24h_1h_availability_strict20m_context_ever_seen_content_same.json"),
        "--signature-cache-json", $SignatureCachePath,
        "--max-age-hours", "24",
        "--riskset-mode", "ever_seen_in_feed",
        "--early-window-hours", "1",
        "--availability-anchor", "availability_time",
        "--max-first-monitor-delay-minutes", "20",
        "--strict-cohort-mode", "context",
        "--request-pause-seconds", "0.05"
    ))
)

$startedAt = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
Write-Status -State "running" -CurrentStep "" -StepStates $steps -StartedAt $startedAt
Write-Log "RQ1 full pipeline starting for study '$StudyId'"
Write-Log "Repo root: $RepoRoot"
Write-Log "Data root: $DataRoot"
Write-Log "Output dir: $OutDir"

foreach ($step in $steps) {
    $step.status = "running"
    $step.started_at = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Write-Status -State "running" -CurrentStep $step.name -StepStates $steps -StartedAt $startedAt
    Write-Log "Starting step '$($step.name)'"

    if (Test-Path -LiteralPath $step.stdout_log) {
        Remove-Item -LiteralPath $step.stdout_log -Force
    }
    if (Test-Path -LiteralPath $step.stderr_log) {
        Remove-Item -LiteralPath $step.stderr_log -Force
    }

    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $process = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList $step.arguments `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $step.stdout_log `
        -RedirectStandardError $step.stderr_log `
        -NoNewWindow `
        -PassThru `
        -Wait
    $timer.Stop()

    $step.finished_at = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    $step.duration_seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 2)
    $step.exit_code = $process.ExitCode

    if ($process.ExitCode -ne 0) {
        $step.status = "failed"
        Write-Status -State "failed" -CurrentStep $step.name -StepStates $steps -StartedAt $startedAt -FinishedAt $step.finished_at
        Write-Log "Step '$($step.name)' failed with exit code $($process.ExitCode)"
        throw "Pipeline aborted at step '$($step.name)'"
    }

    $step.status = "completed"
    Write-Status -State "running" -CurrentStep "" -StepStates $steps -StartedAt $startedAt
    Write-Log "Completed step '$($step.name)' in $($step.duration_seconds)s"
}

$finishedAt = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
Write-Status -State "completed" -CurrentStep "" -StepStates $steps -StartedAt $startedAt -FinishedAt $finishedAt
Write-Log "RQ1 full pipeline completed"
