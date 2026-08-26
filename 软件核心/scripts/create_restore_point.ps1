param(
    [string]$Label = "611稳定版",
    [string]$PackagePath = ""
)

$ErrorActionPreference = "Stop"

function Assert-ChildPath($Path, $ParentPath) {
    $FullPath = [IO.Path]::GetFullPath($Path).TrimEnd("\")
    $FullParent = [IO.Path]::GetFullPath($ParentPath).TrimEnd("\")
    if (-not $FullPath.StartsWith($FullParent + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe path outside expected directory: $FullPath"
    }
}

function New-CleanDir($Path) {
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Copy-DirMirror($Source, $Target) {
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    & robocopy $Source $Target /MIR /NFL /NDL /NJH /NJS /NP /R:1 /W:1 /XD "__pycache__" | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "Copy failed: $Source -> $Target"
    }
}

$ScriptDir = (Resolve-Path $PSScriptRoot).Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$RestoreBase = Join-Path $ProjectRoot "用户工作区\版本还原点"
$RestoreRoot = Join-Path $RestoreBase $Label
$SnapshotRoot = Join-Path $RestoreRoot "源码快照"
$RestorePackage = Join-Path $RestoreRoot "611一键还原包"

if ([string]::IsNullOrWhiteSpace($PackagePath)) {
    $PackagePath = Join-Path $ProjectRoot "用户工作区\打包输出\客户覆盖包"
}
$PackagePath = [IO.Path]::GetFullPath($PackagePath)

if (-not (Test-Path -LiteralPath (Join-Path $PackagePath "一键更新.exe"))) {
    throw "Stable update package is missing: $PackagePath"
}
if (-not (Test-Path -LiteralPath (Join-Path $PackagePath "更新文件\牛爷爷电商视频工具箱.exe"))) {
    throw "Stable app payload is incomplete: $PackagePath"
}

Assert-ChildPath $RestoreRoot $RestoreBase
New-CleanDir $RestoreRoot
New-Item -ItemType Directory -Force -Path $SnapshotRoot | Out-Null

$RootFiles = @("main.py", "ui.py", "engine.py", "paths.py", "utils.py", "requirements.txt")
foreach ($Name in $RootFiles) {
    $Source = Join-Path $ProjectRoot $Name
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Source file missing: $Name"
    }
    Copy-Item -LiteralPath $Source -Destination $SnapshotRoot -Force
}
Copy-DirMirror (Join-Path $ProjectRoot "软件核心") (Join-Path $SnapshotRoot "软件核心")

Copy-DirMirror $PackagePath $RestorePackage
$UpdaterSource = Join-Path $RestorePackage "一键更新.exe"
$UpdaterTarget = Join-Path $RestorePackage "611一键还原.exe"
Copy-Item -LiteralPath $UpdaterSource -Destination $UpdaterTarget -Force
Remove-Item -LiteralPath $UpdaterSource -Force

$PayloadRoot = Join-Path $RestorePackage "更新文件"
$PayloadWorkspace = Join-Path $PayloadRoot "用户工作区"
if (Test-Path -LiteralPath $PayloadWorkspace) {
    Assert-ChildPath $PayloadWorkspace $PayloadRoot
    Remove-Item -LiteralPath $PayloadWorkspace -Recurse -Force
}

$VersionPath = Join-Path $PayloadRoot "软件核心依赖\_package_version.json"
$VersionInfo = [ordered]@{
    package_type = "restore_point"
    package_version = "611-stable-2026.06.11"
    built_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss zzz")
    app_name = "牛爷爷电商视频工具箱"
    format = "offline_restore_with_full_payload"
    updater = "611一键还原.exe"
    payload_directory = "更新文件"
    preserves = @("用户工作区", "网盘资源包")
}
$VersionInfo | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $VersionPath -Encoding UTF8

$PayloadHashPath = Join-Path $PayloadRoot "软件核心依赖\_package_sha256.txt"
$PayloadTargets = Get-ChildItem -LiteralPath $PayloadRoot -Recurse -File |
    Where-Object { $_.FullName -ne $PayloadHashPath } |
    Select-Object -ExpandProperty FullName
$PayloadHashes = foreach ($Target in $PayloadTargets) {
    $Hash = Get-FileHash -LiteralPath $Target -Algorithm SHA256
    $Relative = [IO.Path]::GetRelativePath($PayloadRoot, $Target)
    "{0}  {1}" -f $Hash.Hash, $Relative
}
$PayloadHashes | Set-Content -LiteralPath $PayloadHashPath -Encoding UTF8

$SourceHashPath = Join-Path $RestoreRoot "源码_SHA256.txt"
$SourceTargets = Get-ChildItem -LiteralPath $SnapshotRoot -Recurse -File |
    Select-Object -ExpandProperty FullName
$SourceHashes = foreach ($Target in $SourceTargets) {
    $Hash = Get-FileHash -LiteralPath $Target -Algorithm SHA256
    $Relative = [IO.Path]::GetRelativePath($SnapshotRoot, $Target)
    "{0}  {1}" -f $Hash.Hash, $Relative
}
$SourceHashes | Set-Content -LiteralPath $SourceHashPath -Encoding UTF8

Copy-Item -LiteralPath (Join-Path $ScriptDir "restore_source_snapshot.ps1") `
    -Destination (Join-Path $RestoreRoot "开发源码还原.ps1") -Force

$Metadata = [ordered]@{
    label = $Label
    frozen_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss zzz")
    source_files = $SourceTargets.Count
    payload_files = $PayloadTargets.Count
    restore_package_mb = [math]::Round(
        ((Get-ChildItem -LiteralPath $RestorePackage -Recurse -File | Measure-Object Length -Sum).Sum / 1MB),
        2
    )
}
$Metadata | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $RestoreRoot "还原点信息.json") -Encoding UTF8

@"
611稳定版还原点

客户软件还原：
1. 进入“611一键还原包”。
2. 双击“611一键还原.exe”。
3. 确认客户的软件目录后开始还原。
4. 用户工作区、素材、配置、输出和原有网盘资源包都会保留。

开发源码还原：
在 PowerShell 中运行：
  .\开发源码还原.ps1 -ConfirmRestore

源码还原前会自动备份当前源码。不要直接删除本还原点。
"@ | Set-Content -LiteralPath (Join-Path $RestoreRoot "使用说明.txt") -Encoding UTF8

Write-Host "Restore point created:"
Write-Host $RestoreRoot
