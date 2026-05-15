param(
    [string]$ReleaseRoot = "用户工作区\打包输出"
)

$ErrorActionPreference = "Stop"
$ScriptDir = (Resolve-Path $PSScriptRoot).Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "main.py"))) {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}
$ReleasePath = Join-Path $ProjectRoot $ReleaseRoot
$AppPack = Join-Path $ReleasePath "01_主程序打包输入"
$ResourcePack = Join-Path $ReleasePath "02_客户运行资源包_放到软件根目录"

function New-CleanDir($Path) {
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Copy-FileIfExists($Source, $TargetDir) {
    $full = Join-Path $ProjectRoot $Source
    if (Test-Path -LiteralPath $full) {
        New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
        Copy-Item -LiteralPath $full -Destination $TargetDir -Force
    }
}

function Copy-DirWithRobo($SourceRel, $TargetDir, [string[]]$ExcludeDirs = @()) {
    $source = Join-Path $ProjectRoot $SourceRel
    if (-not (Test-Path -LiteralPath $source)) {
        Write-Host "跳过不存在目录: $SourceRel"
        return
    }
    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
    $args = @($source, $TargetDir, "/E", "/R:1", "/W:1", "/NFL", "/NDL", "/NP")
    if ($ExcludeDirs.Count -gt 0) {
        $args += "/XD"
        $args += $ExcludeDirs
    }
    & robocopy @args | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "复制目录失败: $SourceRel"
    }
}

New-CleanDir $AppPack
New-CleanDir $ResourcePack

# 主程序打包输入：只放代码和轻量资源，给 PyInstaller 使用。
foreach ($file in @("main.py", "ui.py", "engine.py", "utils.py", "paths.py", "requirements.txt")) {
    Copy-FileIfExists $file $AppPack
}
foreach ($dir in @("modules", "services", "styles", "assets")) {
    Copy-DirWithRobo (Join-Path "软件核心" $dir) (Join-Path $AppPack "软件核心\$dir") @("__pycache__")
}

# 客户运行资源包：客户下载后解压到 EXE 同级根目录。
# 注意：大资源统一放在“网盘资源包”目录下，根目录保持清爽。
foreach ($dir in @("tools", "models", "engines", "workflows")) {
    Copy-DirWithRobo (Join-Path "网盘资源包" $dir) (Join-Path $ResourcePack "网盘资源包\$dir") @("__pycache__", ".git", "output", "outputs", "temp", "tmp", "logs", ".cache")
}

# 创建运行期目录，保留空目录结构，不复制你的本地素材/输出/缓存。
foreach ($dir in @(
    "用户工作区\素材\音频素材", "用户工作区\素材\封面图片", "用户工作区\素材\开头素材(段1)",
    "用户工作区\素材\承接素材(段2)", "用户工作区\素材\视频素材", "用户工作区\素材\背景音乐",
    "用户工作区\输出\保存视频", "用户工作区\输出\音视频处理输出", "用户工作区\输出\voice_clone",
    "用户工作区\输出\image_generate", "用户工作区\输出\live_downloader",
    "用户工作区\缓存", "用户工作区\配置", "用户工作区\日志"
)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ResourcePack $dir) | Out-Null
}

@"
【客户使用方法】

1. 将本资源包内所有文件夹复制到软件 EXE 所在目录。
2. 保持目录结构不变，例如：
   软件.exe
   网盘资源包/
     tools/
     models/
     engines/
     workflows/
   用户工作区/
3. 首次运行后，软件会自动检测 ffmpeg、SenseVoice、VoxCPM2、ComfyUI 等资源。

【不要删除】
- 网盘资源包
- 用户工作区

【可以清理】
- 用户工作区/输出
- 用户工作区/日志
"@ | Set-Content -LiteralPath (Join-Path $ResourcePack "客户资源包使用说明.txt") -Encoding UTF8

@"
【主程序打包输入说明】

这个目录只用于打包 EXE，不能直接作为完整软件交付。
大模型、ComfyUI、ffmpeg 等资源请使用旁边的“02_客户运行资源包_放到软件根目录”。

建议 PyInstaller 打包时只包含：
- main.py
- ui.py
- engine.py
- utils.py
- 软件核心/modules/
- 软件核心/services/
- 软件核心/styles/
- 软件核心/assets/
"@ | Set-Content -LiteralPath (Join-Path $AppPack "主程序打包输入说明.txt") -Encoding UTF8

Write-Host "整理完成:"
Write-Host "主程序打包输入: $AppPack"
Write-Host "客户运行资源包: $ResourcePack"
