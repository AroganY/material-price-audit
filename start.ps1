# Windows 启动向导
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
  Write-Host "尚未安装。请先执行：.\install.ps1"
  exit 1
}

& .\.venv\Scripts\Activate.ps1
$hostAddr = if ($env:MPA_HOST) { $env:MPA_HOST } else { "127.0.0.1" }
$port = if ($env:MPA_PORT) { $env:MPA_PORT } else { "8765" }

Write-Host "启动询价向导：http://${hostAddr}:${port}/"
Write-Host "按 Ctrl+C 结束"
python -m material_price_audit serve --host $hostAddr --port $port
