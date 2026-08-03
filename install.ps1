# Windows 首次安装
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> Material Price Audit 安装"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
  Write-Host "未找到 python。请安装 Python 3.10+ 并勾选 Add to PATH: https://www.python.org/downloads/"
  exit 1
}

Write-Host "Python:" (python --version)

if (-not (Test-Path ".venv")) {
  Write-Host "==> 创建虚拟环境 .venv"
  python -m venv .venv
}

& .\.venv\Scripts\Activate.ps1
python -m pip install -U pip setuptools wheel

Write-Host "==> 安装本项目"
if (Test-Path "pyproject.toml") {
  pip install -e .
} else {
  $whl = Get-ChildItem -Filter "material*price*audit*.whl" | Select-Object -First 1
  if ($whl) { pip install $whl.FullName } else { pip install -r requirements.txt }
}

Write-Host "==> 安装 Playwright Chromium"
python -m playwright install chromium

Write-Host ""
Write-Host "安装完成。启动： .\start.ps1"
