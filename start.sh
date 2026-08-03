#!/usr/bin/env bash
# 启动本地询价向导
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "尚未安装。请先执行：./install.sh"
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

HOST="${MPA_HOST:-127.0.0.1}"
PORT="${MPA_PORT:-8765}"

echo "启动询价向导：http://${HOST}:${PORT}/"
echo "按 Ctrl+C 结束"
exec python -m material_price_audit serve --host "$HOST" --port "$PORT"
