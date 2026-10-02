@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python。请安装 Python 3.12 或更高版本后重试。
  pause
  exit /b 1
)
python -c "import pypdf" >nul 2>nul
if errorlevel 1 (
  echo 正在安装本项目的轻量依赖...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo 依赖安装失败，请查看上方提示。
    pause
    exit /b 1
  )
)
python -c "import sys;sys.path.insert(0,'.runtime');import fitz" >nul 2>nul
if errorlevel 1 (
  echo 正在安装本地 PDF 渲染依赖...
  if not exist .runtime mkdir .runtime
  python -m pip install --target .runtime PyMuPDF
  if errorlevel 1 (
    echo PDF 渲染依赖安装失败，请查看上方提示。
    pause
    exit /b 1
  )
)
start "论文双语阅读器" http://127.0.0.1:8765
python run.py
endlocal
