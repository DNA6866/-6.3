param(
    [string]$OutputName = ""
)

$ErrorActionPreference = "Stop"

function Decode-Utf8($Value) {
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}

function Assert-ChildPath($Path, $ParentPath) {
    $FullPath = [IO.Path]::GetFullPath($Path).TrimEnd("\")
    $FullParent = [IO.Path]::GetFullPath($ParentPath).TrimEnd("\")
    if (-not $FullPath.StartsWith($FullParent + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe path outside workspace: $FullPath"
    }
}

function Get-RelativePathCompat($BasePath, $TargetPath) {
    $FullBase = [IO.Path]::GetFullPath($BasePath)
    $FullTarget = [IO.Path]::GetFullPath($TargetPath)
    try {
        return [IO.Path]::GetRelativePath($FullBase, $FullTarget)
    }
    catch {
        if (-not $FullBase.EndsWith("\")) {
            $FullBase += "\"
        }
        $BaseUri = New-Object System.Uri($FullBase)
        $TargetUri = New-Object System.Uri($FullTarget)
        return [Uri]::UnescapeDataString($BaseUri.MakeRelativeUri($TargetUri).ToString()).Replace("/", "\")
    }
}

function New-CleanDir($Path) {
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Copy-DirMirror($Source, $Target) {
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Directory not found: $Source"
    }
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    & robocopy $Source $Target /MIR /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "Copy failed: $Source -> $Target"
    }
}

function Copy-FileIfExists($Source, $TargetDir) {
    if (Test-Path -LiteralPath $Source) {
        New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
        Copy-Item -LiteralPath $Source -Destination $TargetDir -Force
    }
}

function Remove-PyCache($Root) {
    if (-not (Test-Path -LiteralPath $Root)) {
        return
    }
    Get-ChildItem -Force -Recurse -LiteralPath $Root -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
    Get-ChildItem -Force -Recurse -LiteralPath $Root -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
}

$ScriptDir = (Resolve-Path $PSScriptRoot).Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path

$DefaultOutputName = Decode-Utf8 "5a6i5oi36KaG55uW5YyF"
if ([string]::IsNullOrWhiteSpace($OutputName)) {
    $OutputName = $DefaultOutputName
}

$OutputRel = Decode-Utf8 "55So5oi35bel5L2c5Yy6XOaJk+WMhei+k+WHug=="
$TempRel = Decode-Utf8 "55So5oi35bel5L2c5Yy6XOe8k+WtmFzmiZPljIXkuLTml7Y="
$AppName = Decode-Utf8 "54mb54i354i355S15ZWG6KeG6aKR5bel5YW3566x"
$CoreDep = Decode-Utf8 "6L2v5Lu25qC45b+D5L6d6LWW"
$CoreDir = Decode-Utf8 "6L2v5Lu25qC45b+D"
$ComfyInputRel = Decode-Utf8 "572R55uY6LWE5rqQ5YyFXGVuZ2luZXNcQ29tZnlVSVxDb21meVVJXGNvbWZ5X2FwaVxpbnB1dA=="

$OutputRoot = Join-Path $ProjectRoot $OutputRel
$CoverRoot = Join-Path $OutputRoot $OutputName
$PackageRoot = $CoverRoot
$PayloadName = Decode-Utf8 "5pu05paw5paH5Lu2"
$UpdaterName = Decode-Utf8 "5LiA6ZSu5pu05paw"
$PayloadRoot = Join-Path $PackageRoot $PayloadName
$PayloadZip = Join-Path $PackageRoot ($PayloadName + ".zip")
$TempRoot = Join-Path $ProjectRoot $TempRel
$DistPath = Join-Path $TempRoot "dist"
$WorkPath = Join-Path $TempRoot "build"
$SpecPath = Join-Path $TempRoot "spec"

Assert-ChildPath $OutputRoot $ProjectRoot
Assert-ChildPath $CoverRoot $OutputRoot
Assert-ChildPath $PayloadRoot $PackageRoot
Assert-ChildPath $TempRoot $ProjectRoot

New-CleanDir $OutputRoot
New-CleanDir $TempRoot
New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
New-Item -ItemType Directory -Force -Path $PayloadRoot | Out-Null

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

$PyInstallerArgs = @(
    "-m", "PyInstaller",
    "--clean",
    "-y",
    "--onedir",
    "--windowed",
    "--name", $AppName,
    "--contents-directory", $CoreDep,
    "--distpath", $DistPath,
    "--workpath", $WorkPath,
    "--specpath", $SpecPath,
    "--paths", ".",
    "--paths", $CoreDir,
    "--hidden-import", "uuid",
    "--hidden-import", "urllib.request",
    "--hidden-import", "urllib.parse",
    "--hidden-import", "urllib.error",
    "--hidden-import", "http.client",
    "--hidden-import", "html",
    "--hidden-import", "requests",
    "--hidden-import", "urllib3",
    "--hidden-import", "certifi",
    "--hidden-import", "charset_normalizer",
    "--hidden-import", "idna",
    "--hidden-import", "socket",
    "--hidden-import", "yt_dlp",
    "--hidden-import", "psutil",
    "--hidden-import", "PyQt5.QtMultimedia",
    "--hidden-import", "ui_components",
    "--hidden-import", "services.media_service",
    "--hidden-import", "services.asr_service",
    "--hidden-import", "services.cache_service",
    "--hidden-import", "services.glm_service",
    "--hidden-import", "modules.short_video.task_worker",
    "--hidden-import", "modules.voice_clone.voice_clone_widget",
    "--hidden-import", "modules.image_generate.image_generate_widget",
    "--hidden-import", "modules.image_generate.comfy_shutdown_helper",
    "--hidden-import", "modules.live_downloader.live_downloader_widget",
    "--hidden-import", "modules.roi_calculator.roi_calculator_widget",
    "--hidden-import", "modules.mixer_v2.mixer_v2_widget",
    "--hidden-import", "modules.quick_video_trim.quick_video_trim_widget",
    "--hidden-import", "modules.quick_video_trim.quick_video_trim_engine",
    "--hidden-import", "modules.ai_copywriter.ai_copywriter_widget",
    "--exclude-module", "torch",
    "--exclude-module", "torchaudio",
    "--exclude-module", "transformers",
    "--exclude-module", "modelscope",
    "--exclude-module", "librosa",
    "--exclude-module", "scipy",
    "--exclude-module", "pandas",
    "--exclude-module", "matplotlib",
    "--exclude-module", "sklearn",
    "--exclude-module", "gradio",
    "--exclude-module", "voxcpm",
    "main.py"
)

Push-Location $ProjectRoot
try {
    & $Python @PyInstallerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed. Exit code: $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$BuiltApp = Join-Path $DistPath $AppName
Copy-DirMirror $BuiltApp $PayloadRoot

$CoreSource = Join-Path $ProjectRoot $CoreDir
$CoreTarget = Join-Path $PayloadRoot (Join-Path $CoreDep $CoreDir)
Copy-DirMirror $CoreSource $CoreTarget
Remove-PyCache $CoreTarget

$ComfyInput = Join-Path $ProjectRoot $ComfyInputRel
$ComfyInputTarget = Join-Path $PayloadRoot $ComfyInputRel
if (Test-Path -LiteralPath $ComfyInput) {
    New-Item -ItemType Directory -Force -Path $ComfyInputTarget | Out-Null
    foreach ($Name in @("__init__.py", "basic_types.py", "video_types.py")) {
        Copy-FileIfExists (Join-Path $ComfyInput $Name) $ComfyInputTarget
    }
}

# 注意：voice_clone / ComfyUI python 均属「网盘资源包」（主包）内容，
# 客户首次安装单独下载主包；覆盖包只含主程序 + 软件核心，避免重复携带大体积运行时。

$PayloadWorkspace = Join-Path $PayloadRoot (Decode-Utf8 "55So5oi35bel5L2c5Yy6")
if (Test-Path -LiteralPath $PayloadWorkspace) {
    Assert-ChildPath $PayloadWorkspace $PayloadRoot
    Remove-Item -LiteralPath $PayloadWorkspace -Recurse -Force
}
foreach ($RuntimeArtifact in @("crash_log.txt", "更新器错误日志.txt")) {
    $ArtifactPath = Join-Path $PayloadRoot $RuntimeArtifact
    if (Test-Path -LiteralPath $ArtifactPath) {
        Remove-Item -LiteralPath $ArtifactPath -Force
    }
}

$VersionInfo = [ordered]@{
    package_type = "full_update"
    package_version = (Get-Date -Format "yyyy.MM.dd.HHmm")
    built_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss zzz")
    app_name = $AppName
    format = "offline_updater_with_full_payload"
    updater = ($UpdaterName + ".exe")
    payload_directory = $PayloadName
    payload_archive = ($PayloadName + ".zip")
    preserves = @(
        (Decode-Utf8 "55So5oi35bel5L2c5Yy6"),
        (Decode-Utf8 "572R55uY6LWE5rqQ5YyF")
    )
}
$VersionPath = Join-Path $PayloadRoot (Join-Path $CoreDep "_package_version.json")
$VersionInfo | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $VersionPath -Encoding UTF8

$HashPath = Join-Path $PayloadRoot (Join-Path $CoreDep "_package_sha256.txt")
$HashTargets = Get-ChildItem -LiteralPath $PayloadRoot -Recurse -File |
    Where-Object { $_.FullName -ne $HashPath } |
    Select-Object -ExpandProperty FullName
$HashLines = foreach ($Target in $HashTargets) {
    $Hash = Get-FileHash -LiteralPath $Target -Algorithm SHA256
    $RelativePath = Get-RelativePathCompat $PayloadRoot $Target
    "{0}  {1}" -f $Hash.Hash, $RelativePath
}
$HashLines | Set-Content -LiteralPath $HashPath -Encoding UTF8

$UpdaterSource = Join-Path $CoreSource "scripts\offline_updater.py"
$UpdaterDist = Join-Path $TempRoot "updater_dist"
$UpdaterWork = Join-Path $TempRoot "updater_build"
$UpdaterSpec = Join-Path $TempRoot "updater_spec"
$UpdaterArgs = @(
    "-m", "PyInstaller",
    "--clean",
    "-y",
    "--onefile",
    "--windowed",
    "--name", $UpdaterName,
    "--distpath", $UpdaterDist,
    "--workpath", $UpdaterWork,
    "--specpath", $UpdaterSpec,
    "--hidden-import", "tkinter",
    "--hidden-import", "tkinter.ttk",
    "--hidden-import", "tkinter.filedialog",
    "--hidden-import", "tkinter.messagebox",
    $UpdaterSource
)

& $Python @UpdaterArgs
if ($LASTEXITCODE -ne 0) {
    throw "Updater build failed. Exit code: $LASTEXITCODE"
}
Copy-Item -LiteralPath (Join-Path $UpdaterDist ($UpdaterName + ".exe")) -Destination $PackageRoot -Force

if (Test-Path -LiteralPath $PayloadZip) {
    Remove-Item -LiteralPath $PayloadZip -Force
}
$ZipBuilder = Join-Path $ScriptDir "create_update_zip.py"
& $Python $ZipBuilder --source $PayloadRoot --output $PayloadZip
if ($LASTEXITCODE -ne 0) {
    throw "Update ZIP creation failed. Exit code: $LASTEXITCODE"
}
Remove-Item -LiteralPath $PayloadRoot -Recurse -Force

Remove-Item -LiteralPath $TempRoot -Recurse -Force

$Size = (Get-ChildItem -Force -Recurse -LiteralPath $CoverRoot -File | Measure-Object Length -Sum).Sum
Write-Host "Update package created:"
Write-Host $CoverRoot
Write-Host ("Size: {0:N2} MB" -f ($Size / 1MB))
