$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "正在准备项目内 BabelDOC 及完整依赖。首次安装可能需要下载较多 Python 包。"
$python311 = $env:PAPER_TRANSLATOR_PYTHON311
$usePyLauncher = $false

if (-not ($python311 -and (Test-Path $python311))) {
    $knownPython = "E:\miniconda\envs\EXO\python.exe"
    if (Test-Path $knownPython) {
        $python311 = $knownPython
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.11 -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 11))"
        if ($LASTEXITCODE -eq 0) { $usePyLauncher = $true }
    }
}

if (-not $usePyLauncher -and -not ($python311 -and (Test-Path $python311))) {
    throw "找不到 Python 3.11。请设置 PAPER_TRANSLATOR_PYTHON311，或安装 Python Launcher 的 3.11。"
}

$pipArgs = @("-m", "pip", "install", "--target", ".runtime311", "--prefer-binary", "BabelDOC==0.6.4", "pyzstd")
if ($usePyLauncher) { & py -3.11 @pipArgs } else { & $python311 @pipArgs }
if ($LASTEXITCODE -ne 0) { throw "BabelDOC 依赖安装失败。请保留上方错误信息后重试。" }

Write-Host "安装检查："
if ($usePyLauncher) { & py -3.11 babeldoc_local.py --help } else { & $python311 babeldoc_local.py --help }
if ($LASTEXITCODE -ne 0) { throw "BabelDOC 帮助检查失败，真实翻译尚未可用。" }
Write-Host "BabelDOC 已可用。现在可以运行 启动阅读器.bat。"
