#!/usr/bin/env bash
# 一键：环境自检(+可选安装) → 全自动 run
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

PLATFORMS="${1:-guangcai,huixun,lingcai,jd,1688}"
LIMIT="${2:-}"   # 可选第二参数：试跑条数，如 8

echo "=============================================="
echo " material-price-audit · AUTO RUN"
echo " platforms(A→B→C): $PLATFORMS"
echo "=============================================="

python3 -m material_price_audit check --auto-install || true

ARGS=(run --platforms "$PLATFORMS" --auto-install --login-wait 90)
if [[ -n "$LIMIT" ]]; then
  ARGS+=(--limit "$LIMIT")
fi

python3 -m material_price_audit "${ARGS[@]}"

echo ""
echo "结果: data/output/result.xlsx"
echo "RFQ  : data/output/rfq.xlsx"
echo "平台勾选页: docs/platform-select.html"
