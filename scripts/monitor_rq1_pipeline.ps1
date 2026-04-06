param(
    [string]$StatusPath = "",
    [string]$OutDir = "",
    [int]$DurationMinutes = 185,
    [int]$PollSeconds = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutDir) {
    $OutDir = Join-Path $RepoRoot "output\analysis_demo_20260405_fullrun"
}
$OutDir = [System.IO.Path]::GetFullPath($OutDir)

if (-not $StatusPath) {
    $StatusPath = Join-Path $OutDir "pipeline_status.json"
}
$StatusPath = [System.IO.Path]::GetFullPath($StatusPath)

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$MonitorLog = Join-Path $OutDir "monitor.log"
$LatestText = Join-Path $OutDir "latest_monitor_update.txt"
$MonitorStatus = Join-Path $OutDir "monitor_status.json"

function Write-MonitorLine {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    $line = "[$timestamp] $Message"
    Add-Content -LiteralPath $MonitorLog -Value $line
    Write-Host $line
}

function Safe-JsonLoad {
    param([string]$Path)
    try {
        if (-not (Test-Path -LiteralPath $Path)) {
            return $null
        }
        $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
        if (-not $raw.Trim()) {
            return $null
        }
        return $raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $null
    }
}

function Read-LastNonEmptyLine {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    $lines = Get-Content -LiteralPath $Path -Tail 20 -ErrorAction SilentlyContinue
    if (-not $lines) {
        return ""
    }
    $last = ""
    foreach ($line in $lines) {
        if ($line -and $line.Trim()) {
            $last = $line.Trim()
        }
    }
    return $last
}

$startedAt = Get-Date
$deadline = $startedAt.AddMinutes($DurationMinutes)
$lastFingerprint = ""
$lastPipelineLine = ""

Write-MonitorLine "RQ1 monitor starting; duration=$DurationMinutes min, poll=$PollSeconds s"
Write-MonitorLine "Watching status file: $StatusPath"

while ((Get-Date) -lt $deadline) {
    $now = Get-Date
    $status = Safe-JsonLoad -Path $StatusPath

    if ($null -eq $status) {
        $summary = "status file not ready yet"
        $fingerprint = "waiting"
        $currentStep = ""
        $state = "waiting"
        $completed = 0
        $total = 0
        $stepElapsedSeconds = $null
        $stdoutSize = $null
        $stderrSize = $null
    } else {
        $steps = @($status.steps)
        $total = $steps.Count
        $completed = @($steps | Where-Object { $_.status -eq "completed" }).Count
        $failed = @($steps | Where-Object { $_.status -eq "failed" })
        $running = @($steps | Where-Object { $_.status -eq "running" })
        $active = $null
        if ($running.Count -gt 0) {
            $active = $running[0]
        } elseif ($status.current_step) {
            $active = $steps | Where-Object { $_.name -eq $status.current_step } | Select-Object -First 1
        }

        $state = [string]$status.state
        $currentStep = if ($null -ne $active) { [string]$active.name } else { "" }

        $stepElapsedSeconds = $null
        if ($null -ne $active -and $active.started_at) {
            try {
                $stepElapsedSeconds = [math]::Round((New-TimeSpan -Start ([datetimeoffset]::Parse([string]$active.started_at)) -End $now).TotalSeconds, 0)
            } catch {
                $stepElapsedSeconds = $null
            }
        }

        $stdoutSize = $null
        $stderrSize = $null
        if ($null -ne $active) {
            if ($active.stdout_log -and (Test-Path -LiteralPath ([string]$active.stdout_log))) {
                $stdoutSize = (Get-Item -LiteralPath ([string]$active.stdout_log)).Length
            }
            if ($active.stderr_log -and (Test-Path -LiteralPath ([string]$active.stderr_log))) {
                $stderrSize = (Get-Item -LiteralPath ([string]$active.stderr_log)).Length
            }
        }

        $summary = "state=$state step=$currentStep completed=$completed/$total"
        if ($null -ne $stepElapsedSeconds) {
            $summary += " step_elapsed_s=$stepElapsedSeconds"
        }
        if ($failed.Count -gt 0) {
            $summary += " failed_step=$($failed[0].name)"
        }
        if ($null -ne $stdoutSize) {
            $summary += " stdout_bytes=$stdoutSize"
        }
        if ($null -ne $stderrSize) {
            $summary += " stderr_bytes=$stderrSize"
        }

        $fingerprint = @(
            $state,
            $currentStep,
            $completed,
            $total,
            $stdoutSize,
            $stderrSize
        ) -join "|"
    }

    $latestPipelineLine = Read-LastNonEmptyLine -Path (Join-Path $OutDir "pipeline.log")
    if ($latestPipelineLine -and $latestPipelineLine -ne $lastPipelineLine) {
        Write-MonitorLine "pipeline: $latestPipelineLine"
        $lastPipelineLine = $latestPipelineLine
    }

    $latestTextBody = @(
        "updated_at=$($now.ToString("yyyy-MM-ddTHH:mm:ssK"))"
        "summary=$summary"
        "status_path=$StatusPath"
        "monitor_log=$MonitorLog"
    ) -join [Environment]::NewLine
    Set-Content -LiteralPath $LatestText -Value $latestTextBody -Encoding UTF8

    $monitorPayload = [ordered]@{
        updated_at = $now.ToString("yyyy-MM-ddTHH:mm:ssK")
        summary = $summary
        state = $state
        current_step = $currentStep
        completed_steps = $completed
        total_steps = $total
        step_elapsed_seconds = $stepElapsedSeconds
        stdout_bytes = $stdoutSize
        stderr_bytes = $stderrSize
        latest_pipeline_line = $latestPipelineLine
        deadline = $deadline.ToString("yyyy-MM-ddTHH:mm:ssK")
    }
    $monitorPayload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $MonitorStatus -Encoding UTF8

    if ($fingerprint -ne $lastFingerprint) {
        Write-MonitorLine $summary
        $lastFingerprint = $fingerprint
    }

    if ($state -eq "completed" -or $state -eq "failed") {
        break
    }

    Start-Sleep -Seconds $PollSeconds
}

$endedAt = Get-Date
$finalStatus = Safe-JsonLoad -Path $StatusPath
$finalState = if ($null -ne $finalStatus) { [string]$finalStatus.state } else { "unknown" }
Write-MonitorLine "RQ1 monitor exiting at state=$finalState"
