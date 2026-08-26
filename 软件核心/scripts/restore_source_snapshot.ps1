param(
    [switch]$ConfirmRestore
)

$ErrorActionPreference = "Stop"

function Assert-ChildPath($Path, $ParentPath) {
    $FullPath = [IO.Path]::GetFullPath($Path).TrimEnd("\")
    $FullParent = [IO.Path]::GetFullPath($ParentPath).TrimEnd("\")
    if (-not $FullPath.StartsWith($FullParent + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe path outside expected directory: $FullPath"
    }
}

function Copy-DirMirror($Source, $Target) {
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    & robocopy $Source $Target /MIR /NFL /NDL /NJH /NJS /NP /R:1 /W:1 /XD "__pycache__" | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "Directory restore failed: $Source -> $Target"
    }
}

$RestorePointRoot = (Resolve-Path $PSScriptRoot).Path
$SnapshotRoot = Join-Path $RestorePointRoot "源码快照"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $RestorePointRoot "..\..\.."))
$CoreTarget = Join-Path $ProjectRoot "软件核心"
$BackupRoot = Join-Path $ProjectRoot (
    "用户工作区\版本还原点\还原前自动备份\" + (Get-Date -Format "yyyyMMdd_HHmmss")
)

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "main.py"))) {
    throw "Project root validation failed: $ProjectRoot"
}
if (-not (Test-Path -LiteralPath (Join-Path $SnapshotRoot "ui.py"))) {
    throw "Source snapshot is incomplete: $SnapshotRoot"
}
if (-not $ConfirmRestore) {
    throw "This operation replaces current source. Run with -ConfirmRestore after confirming the restore point."
}

Assert-ChildPath $CoreTarget $ProjectRoot
Assert-ChildPath $BackupRoot $ProjectRoot
New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null

$RootFiles = @("main.py", "ui.py", "engine.py", "paths.py", "utils.py", "requirements.txt")
foreach ($Name in $RootFiles) {
    $Current = Join-Path $ProjectRoot $Name
    if (Test-Path -LiteralPath $Current) {
        Copy-Item -LiteralPath $Current -Destination $BackupRoot -Force
    }
}
if (Test-Path -LiteralPath $CoreTarget) {
    Copy-DirMirror $CoreTarget (Join-Path $BackupRoot "软件核心")
}

foreach ($Name in $RootFiles) {
    $Source = Join-Path $SnapshotRoot $Name
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Snapshot file missing: $Name"
    }
    Copy-Item -LiteralPath $Source -Destination (Join-Path $ProjectRoot $Name) -Force
}
Copy-DirMirror (Join-Path $SnapshotRoot "软件核心") $CoreTarget

Write-Host "611 source restore completed."
Write-Host "Pre-restore backup: $BackupRoot"
