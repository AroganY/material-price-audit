#!/usr/bin/env bash
# 首次安装：venv + 依赖 + Chromium
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Material Price Audit 安装"
if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。请先安装 Python 3.10+：https://www.python.org/downloads/"
  exit 1
fi

PY=python3
echo "Python: $($PY --version)"

if [[ ! -d .venv ]]; then
  echo "==> 创建虚拟环境 .venv"
  $PY -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> 升级 pip"
python -m pip install -U pip setuptools wheel

echo "==> 安装本项目"
if [[ -f pyproject.toml ]]; then
  pip install -e .
else
  # 仅 wheel 的发布布局
  whl=$(ls -1 material_price_audit-*.whl material-price-audit-*.whl 2>/dev/null | head -1 || true)
  if [[ -n "${whl:-}" ]]; then
    pip install "$whl"
  else
    pip install -r requirements.txt
    pip install -e . 2>/dev/null || true
  fi
fi

echo "==> 安装 Playwright Chromium（首次较慢）"
python -m playwright install chromium

echo ""
echo "安装完成。启动向导："
echo "  ./start.sh"
echo "或："
echo "  source .venv/bin/activate && python -m material_price_audit serve"
