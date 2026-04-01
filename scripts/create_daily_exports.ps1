param(
    [string]$Root = 'D:\BlueSky\data_v2_full',
    [string]$OutputSubdir = 'exports\daily_exports_realzip',
    [switch]$RebuildExisting,
    [switch]$IncludeEffectiveCsv,
    [int64]$MaxZipBytes = 500MB,
    [string[]]$OnlyDates = @()
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Add-DateDirs {
    param(
        [string]$RootPath,
        [string]$BaseRelativePath,
        [System.Collections.Generic.HashSet[string]]$Dates
    )

    $base = Join-Path $RootPath $BaseRelativePath
    if (-not (Test-Path -LiteralPath $base)) {
        return
    }

    Get-ChildItem -LiteralPath $base -Directory -Force |
        Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' } |
        ForEach-Object { [void]$Dates.Add($_.Name) }
}

function Add-DateFromFiles {
    param(
        [string]$RootPath,
        [string]$BaseRelativePath,
        [System.Collections.Generic.HashSet[string]]$Dates
    )

    $base = Join-Path $RootPath $BaseRelativePath
    if (-not (Test-Path -LiteralPath $base)) {
        return
    }

    Get-ChildItem -LiteralPath $base -File -Force | ForEach-Object {
        if ($_.Name -match '(\d{4}-\d{2}-\d{2})') {
            [void]$Dates.Add($matches[1])
        }
    }
}

function Get-DateScopedRoots {
    param(
        [string]$RootPath,
        [string]$Date,
        [bool]$IncludeEffectiveCsvRoots
    )

    $roots = New-Object System.Collections.Generic.List[string]

    foreach ($rel in @(
        "hourly\$Date",
        "wide\$Date",
        "metadata\$Date",
        "authors\$Date",
        "labelerexp\hourly\$Date",
        "panel\panel_versions\panel_v1_$Date.csv",
        "labelerexp\panel\panel_versions\panel_v1_$Date.csv"
    )) {
        if (Test-Path -LiteralPath (Join-Path $RootPath $rel)) {
            [void]$roots.Add($rel)
        }
    }

    if ($IncludeEffectiveCsvRoots) {
        foreach ($rel in @(
            "effective_csv\timeseries\metadata\$Date",
            "effective_csv\timeseries\hourly\$Date",
            "effective_csv\timeseries\wide\$Date",
            "effective_csv\timeseries\authors\$Date",
            "labelerexp\effective_csv\timeseries\hourly\$Date",
            "effective_csv\timeseries\panel\panel_v1_$Date.csv",
            "labelerexp\effective_csv\timeseries\panel\panel_v1_$Date.csv"
        )) {
            if (Test-Path -LiteralPath (Join-Path $RootPath $rel)) {
                [void]$roots.Add($rel)
            }
        }
    }

    return $roots
}

function Get-DateScopedFiles {
    param(
        [string]$RootPath,
        [System.Collections.Generic.List[string]]$SourceRoots
    )

    $result = [ordered]@{
        SourceFiles = New-Object System.Collections.Generic.List[string]
        SourceFileCount = 0
        SourceBytes = [int64]0
    }

    foreach ($rel in $SourceRoots) {
        $full = Join-Path $RootPath $rel
        if (Test-Path -LiteralPath $full -PathType Container) {
            Get-ChildItem -LiteralPath $full -File -Recurse -Force |
                Where-Object { $_.Name -notlike '._*' } |
                ForEach-Object {
                    $relative = $_.FullName.Substring($RootPath.Length + 1)
                    [void]$result.SourceFiles.Add($relative)
                    $result.SourceFileCount += 1
                    $result.SourceBytes += $_.Length
                }
        }
        elseif (Test-Path -LiteralPath $full -PathType Leaf) {
            $item = Get-Item -LiteralPath $full -Force
            if ($item.Name -notlike '._*') {
                [void]$result.SourceFiles.Add($rel)
                $result.SourceFileCount += 1
                $result.SourceBytes += $item.Length
            }
        }
    }

    return $result
}

function Test-ArchiveFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }

    $quotedPath = '"' + $Path.Replace('"', '""') + '"'
    & cmd.exe /c "tar.exe -tf $quotedPath >nul 2>nul"
    return ($LASTEXITCODE -eq 0)
}

function Test-ZipFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }

    try {
        $zip = [System.IO.Compression.ZipFile]::OpenRead($Path)
        $zip.Dispose()
        return $true
    }
    catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $Root)) {
    throw "Root path not found: $Root"
}

$outDir = Join-Path $Root $OutputSubdir
$manifestPath = Join-Path $outDir 'manifest.csv'
$summaryPath = Join-Path $outDir 'summary.txt'
$listFile = Join-Path $env:TEMP ("bsky_daily_exports_" + [guid]::NewGuid().ToString('N') + '.txt')
$dates = [System.Collections.Generic.HashSet[string]]::new()
$results = New-Object System.Collections.Generic.List[object]
$buildStarted = Get-Date

New-Item -ItemType Directory -Force -Path $outDir | Out-Null

foreach ($rel in @(
    'hourly',
    'wide',
    'metadata',
    'authors',
    'labelerexp\hourly'
)) {
    Add-DateDirs -RootPath $Root -BaseRelativePath $rel -Dates $dates
}

foreach ($rel in @(
    'panel\panel_versions',
    'labelerexp\panel\panel_versions'
)) {
    Add-DateFromFiles -RootPath $Root -BaseRelativePath $rel -Dates $dates
}

if ($IncludeEffectiveCsv) {
    foreach ($rel in @(
        'effective_csv\timeseries\metadata',
        'effective_csv\timeseries\hourly',
        'effective_csv\timeseries\wide',
        'effective_csv\timeseries\authors',
        'labelerexp\effective_csv\timeseries\hourly'
    )) {
        Add-DateDirs -RootPath $Root -BaseRelativePath $rel -Dates $dates
    }

    foreach ($rel in @(
        'effective_csv\timeseries\panel',
        'labelerexp\effective_csv\timeseries\panel'
    )) {
        Add-DateFromFiles -RootPath $Root -BaseRelativePath $rel -Dates $dates
    }
}

$allDates = @($dates | Sort-Object)
if ($OnlyDates.Count -gt 0) {
    $wanted = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($d in $OnlyDates) {
        [void]$wanted.Add($d)
    }
    $allDates = @($allDates | Where-Object { $wanted.Contains($_) })
}
$index = 0

foreach ($date in $allDates) {
    $index += 1
    $sourceRoots = Get-DateScopedRoots -RootPath $Root -Date $date -IncludeEffectiveCsvRoots:$IncludeEffectiveCsv
    $sourceInfo = Get-DateScopedFiles -RootPath $Root -SourceRoots $sourceRoots
    $sourceGiB = [math]::Round($sourceInfo.SourceBytes / 1GB, 2)
    $zipName = "data_v2_full_$date.zip"
    $zipPath = Join-Path $outDir $zipName
    # tar -a chooses the archive format from the output suffix.
    # Keep ".zip" on the temp name so the temp file is a real ZIP too.
    $tmpZipPath = $zipPath -replace '\.zip$', '.building.zip'

    if ((-not $RebuildExisting) -and (Test-ZipFile -Path $zipPath)) {
        $existing = Get-Item -LiteralPath $zipPath -Force
        Write-Output ("[{0}/{1}] SKIP {2} -> {3} ({4} files, {5} GiB source, {6} GiB zip)" -f $index, $allDates.Count, $date, $zipName, $sourceInfo.SourceFileCount, $sourceGiB, [math]::Round($existing.Length / 1GB, 2))
        $results.Add([pscustomobject]@{
            Date = $date
            ZipName = $zipName
            ZipPath = $zipPath
            EffectiveCsvIncluded = [bool]$IncludeEffectiveCsv
            MaxZipBytes = $MaxZipBytes
            SourceRoots = ($sourceRoots -join '; ')
            SourceFiles = $sourceInfo.SourceFileCount
            SourceBytes = $sourceInfo.SourceBytes
            SourceGiB = $sourceGiB
            ZipBytes = $existing.Length
            ZipGiB = [math]::Round($existing.Length / 1GB, 2)
            Status = 'skipped_existing'
            BuiltAt = $existing.LastWriteTime.ToString('s')
        }) | Out-Null
        $results | Export-Csv -LiteralPath $manifestPath -NoTypeInformation
        continue
    }

    if (Test-Path -LiteralPath $tmpZipPath) {
        if (Test-ZipFile -Path $tmpZipPath) {
            Move-Item -LiteralPath $tmpZipPath -Destination $zipPath -Force
        }
        else {
            Remove-Item -LiteralPath $tmpZipPath -Force
        }
    }

    if ((-not $RebuildExisting) -and (Test-ZipFile -Path $zipPath)) {
        $existing = Get-Item -LiteralPath $zipPath -Force
        Write-Output ("[{0}/{1}] RECOVER {2} -> {3} ({4} GiB zip)" -f $index, $allDates.Count, $date, $zipName, [math]::Round($existing.Length / 1GB, 2))
        $results.Add([pscustomobject]@{
            Date = $date
            ZipName = $zipName
            ZipPath = $zipPath
            EffectiveCsvIncluded = [bool]$IncludeEffectiveCsv
            MaxZipBytes = $MaxZipBytes
            SourceRoots = ($sourceRoots -join '; ')
            SourceFiles = $sourceInfo.SourceFileCount
            SourceBytes = $sourceInfo.SourceBytes
            SourceGiB = $sourceGiB
            ZipBytes = $existing.Length
            ZipGiB = [math]::Round($existing.Length / 1GB, 2)
            Status = 'recovered_tmp'
            BuiltAt = $existing.LastWriteTime.ToString('s')
        }) | Out-Null
        $results | Export-Csv -LiteralPath $manifestPath -NoTypeInformation
        continue
    }

    if ((-not $RebuildExisting) -and (Test-Path -LiteralPath $zipPath)) {
        Write-Output ("[{0}/{1}] REBUILD {2} -> {3} (existing file is not a real ZIP)" -f $index, $allDates.Count, $date, $zipName)
        Remove-Item -LiteralPath $zipPath -Force
    }

    ($sourceInfo.SourceFiles | Sort-Object) | Set-Content -LiteralPath $listFile -Encoding ascii
    Write-Output ("[{0}/{1}] CREATE {2} -> {3} ({4} files, {5} GiB source)" -f $index, $allDates.Count, $date, $zipName, $sourceInfo.SourceFileCount, $sourceGiB)
    & tar.exe -a -c -f $tmpZipPath -C $Root -T $listFile
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed for $date with exit code $LASTEXITCODE"
    }

    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Move-Item -LiteralPath $tmpZipPath -Destination $zipPath -Force
    $zipItem = Get-Item -LiteralPath $zipPath -Force
    if ($zipItem.Length -gt $MaxZipBytes) {
        throw ("Archive {0} exceeds MaxZipBytes: {1} > {2}" -f $zipName, $zipItem.Length, $MaxZipBytes)
    }
    Write-Output ("[{0}/{1}] DONE   {2} -> {3} GiB zip" -f $index, $allDates.Count, $date, [math]::Round($zipItem.Length / 1GB, 2))

    $results.Add([pscustomobject]@{
        Date = $date
        ZipName = $zipName
        ZipPath = $zipPath
        EffectiveCsvIncluded = [bool]$IncludeEffectiveCsv
        MaxZipBytes = $MaxZipBytes
        SourceRoots = ($sourceRoots -join '; ')
        SourceFiles = $sourceInfo.SourceFileCount
        SourceBytes = $sourceInfo.SourceBytes
        SourceGiB = $sourceGiB
        ZipBytes = $zipItem.Length
        ZipGiB = [math]::Round($zipItem.Length / 1GB, 2)
        Status = 'created'
        BuiltAt = (Get-Date).ToString('s')
    }) | Out-Null
    $results | Export-Csv -LiteralPath $manifestPath -NoTypeInformation
}

$totSourceBytes = ($results | Measure-Object -Property SourceBytes -Sum).Sum
$totZipBytes = ($results | Measure-Object -Property ZipBytes -Sum).Sum
$summary = @(
    "OutputDir=$outDir",
    "BuildStarted=$($buildStarted.ToString('s'))",
    "BuildFinished=$((Get-Date).ToString('s'))",
    "IncludeEffectiveCsv=$([bool]$IncludeEffectiveCsv)",
    "MaxZipBytes=$MaxZipBytes",
    "ArchiveCount=$($results.Count)",
    "TotalSourceGiB=$([math]::Round($totSourceBytes / 1GB, 2))",
    "TotalZipGiB=$([math]::Round($totZipBytes / 1GB, 2))",
    "Manifest=$manifestPath"
)

$results | Export-Csv -LiteralPath $manifestPath -NoTypeInformation
$summary | Set-Content -LiteralPath $summaryPath -Encoding ascii

try {
    Remove-Item -LiteralPath $listFile -Force
}
catch {
}

Write-Output ''
Write-Output 'SUMMARY'
$summary | ForEach-Object { Write-Output $_ }
