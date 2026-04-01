param(
    [string]$BuildRoot = (Join-Path $PSScriptRoot "..\\_build"),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Ensure-Dir {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Resolve-DestinationPath {
    param(
        [string]$TargetDir,
        [string]$Name
    )

    $candidate = Join-Path $TargetDir $Name
    if (-not (Test-Path -LiteralPath $candidate)) {
        return $candidate
    }

    $base = [System.IO.Path]::GetFileNameWithoutExtension($Name)
    $ext = [System.IO.Path]::GetExtension($Name)
    $i = 2
    do {
        $candidate = Join-Path $TargetDir ("{0}__{1}{2}" -f $base, $i, $ext)
        $i++
    } while (Test-Path -LiteralPath $candidate)
    return $candidate
}

function Move-Into {
    param(
        [System.IO.FileSystemInfo]$Item,
        [string]$RelativeDest
    )

    $targetDir = Join-Path $script:BuildRootResolved $RelativeDest
    Ensure-Dir -Path $targetDir
    $targetPath = Resolve-DestinationPath -TargetDir $targetDir -Name $Item.Name
    $displayFrom = $Item.FullName
    $displayTo = $targetPath

    if ($DryRun) {
        Write-Host "[dry-run] $displayFrom -> $displayTo"
        return
    }

    Move-Item -LiteralPath $Item.FullName -Destination $targetPath
    Write-Host "$displayFrom -> $displayTo"
}

function Is-WorkingDeckName {
    param([string]$LowerName)
    return (
        $LowerName -like "*preanim*" -or
        $LowerName -like "*polish_work*" -or
        $LowerName -like "*workcopy*" -or
        $LowerName -like "*source_for_*" -or
        $LowerName -like "*enhanced_svg*" -or
        $LowerName -like "*blackboard_work*" -or
        $LowerName -like "*before_matrix*" -or
        $LowerName -like "*pre_3b1b*" -or
        $LowerName -like "*pre_blackboard*"
    )
}

function Is-FinalDeckName {
    param([string]$LowerName)
    return (
        $LowerName -eq "ifx_poster_animated.pptx" -or
        $LowerName -eq "ifx_poster_animated_02.pptx" -or
        $LowerName -like "*_animated.pptx" -or
        $LowerName -like "*_raw.pptx" -or
        $LowerName -like "*_3b1b_morph.pptx" -or
        $LowerName -like "*_enhanced_2slides.pptx" -or
        $LowerName -like "*_blackboard.pptx" -or
        $LowerName -like "bluesky_meeting*.pptx"
    )
}

$script:BuildRootResolved = (Resolve-Path -LiteralPath $BuildRoot).Path

$stableDirs = @(
    "decks\\final",
    "decks\\working",
    "decks\\archive",
    "previews",
    "analysis\\csv",
    "analysis\\notes",
    "temp\\office-lock",
    "temp\\thumbnails",
    "temp\\workdirs"
)
foreach ($dir in $stableDirs) {
    Ensure-Dir -Path (Join-Path $script:BuildRootResolved $dir)
}

$topLevel = Get-ChildItem -LiteralPath $script:BuildRootResolved -Force

foreach ($item in $topLevel) {
    if ($item.Name -in @("decks", "previews", "analysis", "temp", "_tools", "README.md")) {
        continue
    }

    $name = $item.Name
    $lower = $name.ToLowerInvariant()

    if ($item.PSIsContainer) {
        if ($name -match "^(_tmp|_observed.*tmp|_ifx_svg_tmp$)") {
            Move-Into -Item $item -RelativeDest "temp\\workdirs"
            continue
        }
        if ($name -match "^_analysis_") {
            Move-Into -Item $item -RelativeDest "analysis\\notes"
            continue
        }
        if ($name -match "(_render$|_render_|_renders_|_composite$|_browser_png$)") {
            Move-Into -Item $item -RelativeDest "previews"
            continue
        }
        continue
    }

    if ($lower.StartsWith("~$")) {
        Move-Into -Item $item -RelativeDest "temp\\office-lock"
        continue
    }

    if ($lower.Contains("thumb") -or $lower.Contains("thumbnail") -or $lower.EndsWith(".jpeg") -or $lower.EndsWith(".jpg")) {
        Move-Into -Item $item -RelativeDest "temp\\thumbnails"
        continue
    }

    if ($lower.EndsWith(".csv") -or $lower.EndsWith(".gz")) {
        Move-Into -Item $item -RelativeDest "analysis\\csv"
        continue
    }

    if ($lower.Contains(".bak")) {
        Move-Into -Item $item -RelativeDest "decks\\archive"
        continue
    }

    if ($lower.EndsWith(".pptx")) {
        if (Is-WorkingDeckName -LowerName $lower) {
            Move-Into -Item $item -RelativeDest "decks\\working"
            continue
        }

        if (Is-FinalDeckName -LowerName $lower) {
            Move-Into -Item $item -RelativeDest "decks\\final"
            continue
        }

        Move-Into -Item $item -RelativeDest "decks\\working"
        continue
    }
}
