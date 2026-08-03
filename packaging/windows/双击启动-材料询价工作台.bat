@echo off
chcp 65001 >nul
title 材料询价工作台
cd /d "%~dp0app"

where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo 未检测到 Python。
    echo 请安装 Python 3.10+ ：https://www.python.org/downloads/
    echo 安装时务必勾选 "Add python.exe to PATH"
    pause
    exit /b 1
  )
  set "PY=py -3"
) else (
  set "PY=python"
)

set MPA_DESKTOP_NOTIFY=1
set PYTHONUNBUFFERED=1
echo 正在启动材料询价工作台…
echo 首次运行会自动下载依赖与浏览器组件，请耐心等待。
echo.
%PY% desktop_launch.py
echo.
echo 服务已结束。
pause
