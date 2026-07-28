#!/usr/bin/env bash
# Agent 引导入口：初始化 + 打印下一步（任何 AI 先跑这个）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PLATFORMS="${1:-guangcai,huixun,lingcai,jd,1688}"

echo "=============================================="
echo " material-price-audit · Agent Bootstrap"
echo " root: $ROOT"
echo " platforms: $PLATFORMS"
echo "=============================================="

if ! python3 -c "import material_price_audit" 2>/dev/null; then
  export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
fi

python3 -m material_price_audit check || true
python3 -m material_price_audit init --platforms "$PLATFORMS" --allow-broken-env
python3 -m material_price_audit guide

echo ""
echo ">>> Agent: 请阅读 data/output/AGENT_NEXT.md 并按 questions 引导用户"
echo ">>> HTML 教程: docs/index.html"
echo ">>> 协议: AGENTS.md"
