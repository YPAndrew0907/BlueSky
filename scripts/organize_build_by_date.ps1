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

function Move-IntoDateBucket {
    param(
        [System.IO.FileSystemInfo]$Item,
        [string]$RelativeTail
    )

    $dateKey = $Item.LastWriteTime.ToString("yyyy-MM-dd")
    $targetDir = Join-Path $script:BuildRootResolved (Join-Path $dateKey $RelativeTail)
    Ensure-Dir -Path $targetDir
    $targetPath = Resolve-DestinationPath -TargetDir $targetDir -Name $Item.Name

    if ($DryRun) {
        Write-Host "[dry-run] $($Item.FullName) -> $targetPath"
        return
    }

    Move-Item -LiteralPath $Item.FullName -Destination $targetPath
    Write-Host "$($Item.FullName) -> $targetPath"
}

function Move-Children-Of {
    param(
        [string]$SourceDir,
        [string]$RelativeTail
    )

    if (-not (Test-Path -LiteralPath $SourceDir)) {
        return
    }

    Get-ChildItem -LiteralPath $SourceDir -Force | ForEach-Object {
        Move-IntoDateBucket -Item $_ -RelativeTail $RelativeTail
    }
}

function Remove-IfEmpty {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $remaining = Get-ChildItem -LiteralPath $Path -Force
    if ($remaining.Count -eq 0) {
        if ($DryRun) {
            Write-Host "[dry-run] remove empty $Path"
        } else {
            Remove-Item -LiteralPath $Path -Force
            Write-Host "removed empty $Path"
        }
    }
}

$script:BuildRootResolved = (Resolve-Path -LiteralPath $BuildRoot).Path

Move-Children-Of -SourceDir (Join-Path $script:BuildRootResolved "decks\\final") -RelativeTail "decks\\final"
Move-Children-Of -SourceDir (Join-Path $script:BuildRootResolved "decks\\working") -RelativeTail "decks\\working"
Move-Children-Of -SourceDir (Join-Path $script:BuildRootResolved "decks\\archive") -RelativeTail "decks\\archive"

Move-Children-Of -SourceDir (Join-Path $script:BuildRootResolved "previews") -RelativeTail "previews"

Move-Children-Of -SourceDir (Join-Path $script:BuildRootResolved "analysis\\csv") -RelativeTail "analysis\\csv"
Move-Children-Of -SourceDir (Join-Path $script:BuildRootResolved "analysis\\notes") -RelativeTail "analysis\\notes"

Move-Children-Of -SourceDir (Join-Path $script:BuildRootResolved "temp\\office-lock") -RelativeTail "temp\\office-lock"
Move-Children-Of -SourceDir (Join-Path $script:BuildRootResolved "temp\\thumbnails") -RelativeTail "temp\\thumbnails"
Move-Children-Of -SourceDir (Join-Path $script:BuildRootResolved "temp\\workdirs") -RelativeTail "temp\\workdirs"

Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "decks\\final")
Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "decks\\working")
Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "decks\\archive")
Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "decks")

Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "previews")

Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "analysis\\csv")
Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "analysis\\notes")
Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "analysis")

Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "temp\\office-lock")
Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "temp\\thumbnails")
Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "temp\\workdirs")
Remove-IfEmpty -Path (Join-Path $script:BuildRootResolved "temp")
