param(
    [switch]$UseExistingEngine,
    [string]$BuildPython = "python"
)

$ErrorActionPreference = "Stop"
$packaging = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $packaging
$cache = Join-Path $packaging "cache"
$stage = Join-Path $packaging "stage"
$tools = Join-Path $packaging "tools"
$output = Join-Path $packaging "output"
$venvPython = Join-Path $packaging ".venv\Scripts\python.exe"
$version = (Get-Content -LiteralPath (Join-Path $repo "VERSION") -Raw -Encoding UTF8).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "VERSION 格式无效：$version" }
foreach ($directory in @($cache, $tools, $output)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $BuildPython -m venv (Join-Path $packaging ".venv")
    if ($LASTEXITCODE -ne 0) { throw "无法创建 Python 3.11 构建环境。" }
}
& $venvPython -c "import sys; assert sys.version_info[:2] == (3, 11), 'Build Python must be 3.11'"
if ($LASTEXITCODE -ne 0) { throw "构建环境必须使用 Python 3.11。" }
& $venvPython -m pip install -r (Join-Path $repo "requirements.txt") "pywebview==6.2.1" "pyinstaller==6.21.0" "Pillow==11.3.0"
if ($LASTEXITCODE -ne 0) { throw "构建依赖安装失败。" }
& $venvPython (Join-Path $packaging "generate_icon.py")
if ($LASTEXITCODE -ne 0) { throw "图标生成失败。" }

# The stage is disposable build output. Never touch data/, cached models, or a previous installation.
$stageFull = [IO.Path]::GetFullPath($stage)
$packagingFull = [IO.Path]::GetFullPath($packaging).TrimEnd('\') + '\'
if (-not $stageFull.StartsWith($packagingFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw "拒绝清理工作区之外的目录：$stageFull"
}
if (Test-Path -LiteralPath $stageFull) { Remove-Item -LiteralPath $stageFull -Recurse -Force }
New-Item -ItemType Directory -Path $stageFull | Out-Null

& $venvPython -m PyInstaller --noconfirm --clean --distpath $stageFull --workpath (Join-Path $packaging "build") (Join-Path $packaging "paper2zh.spec")
if ($LASTEXITCODE -ne 0) { throw "桌面程序打包失败。" }
$appDir = Join-Path $stageFull "paper2zh"
$internal = Join-Path $appDir "_internal"
$runtimePath = Join-Path $internal ".runtime311"
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
if ($UseExistingEngine) {
    $sourceEngine = Join-Path $repo ".runtime311"
    if (-not (Test-Path -LiteralPath (Join-Path $sourceEngine "babeldoc\main.py"))) {
        throw "本地 BabelDOC 运行时不存在。"
    }
    & robocopy $sourceEngine $runtimePath /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "复制 BabelDOC 依赖失败。" }
} else {
    & $venvPython -m pip install --disable-pip-version-check --no-compile --target $runtimePath "babeldoc==0.6.4" "pyzstd>=0.18,<1"
    if ($LASTEXITCODE -ne 0) { throw "BabelDOC 依赖安装失败。" }
}

$embedZip = Join-Path $cache "python-3.11.9-embed-amd64.zip"
if (-not (Test-Path -LiteralPath $embedZip)) {
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip" -OutFile $embedZip
}
$actualMd5 = (Get-FileHash -LiteralPath $embedZip -Algorithm MD5).Hash
if ($actualMd5 -ne "6D9AA08531D48FCC261BA667E2DF17C4") { throw "官方 Python 3.11.9 嵌入版校验失败。" }
$engineDir = Join-Path $internal "engine"
Expand-Archive -LiteralPath $embedZip -DestinationPath $engineDir -Force
[IO.File]::WriteAllText((Join-Path $engineDir "python311._pth"), "python311.zip`r`n.`r`n..`r`n..\.runtime311`r`nimport site`r`n", [Text.Encoding]::ASCII)
$previousDataDir = $env:PAPER_TRANSLATOR_DATA_DIR
$env:PAPER_TRANSLATOR_DATA_DIR = Join-Path $cache "engine-probe"
try {
    & (Join-Path $engineDir "python.exe") (Join-Path $internal "babeldoc_local.py") --help | Out-Null
} finally {
    $env:PAPER_TRANSLATOR_DATA_DIR = $previousDataDir
}
if ($LASTEXITCODE -ne 0) { throw "独立 BabelDOC 引擎自检失败。" }

if (Test-Path -LiteralPath (Join-Path $appDir "data")) { throw "安装包中意外包含用户 data 目录。" }
if (Test-Path -LiteralPath (Join-Path $internal "data")) { throw "安装包中意外包含用户 data 目录。" }
$iscc = Join-Path $tools "Inno\ISCC.exe"
if (-not (Test-Path -LiteralPath $iscc)) {
    $installer = Join-Path $cache "innosetup-7.1.0-x64.exe"
    if (-not (Test-Path -LiteralPath $installer)) {
        Invoke-WebRequest -Uri "https://github.com/jrsoftware/issrc/releases/download/is-7_1_0/innosetup-7.1.0-x64.exe" -OutFile $installer
    }
    if ((Get-AuthenticodeSignature -LiteralPath $installer).Status -ne "Valid") { throw "Inno Setup 安装程序签名无效。" }
    Start-Process -FilePath $installer -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/NOICONS", "/DIR=$(Join-Path $tools 'Inno')") -Wait -WindowStyle Hidden
}
if (-not (Test-Path -LiteralPath $iscc)) { throw "Inno Setup 编译器不可用。" }
& $iscc "/DProductVersion=$version" (Join-Path $packaging "paper2zh.iss")
if ($LASTEXITCODE -ne 0) { throw "安装包编译失败。" }
Write-Host "安装包已生成：$(Join-Path $output "paper2zh-Setup-$version-win64.exe")"
