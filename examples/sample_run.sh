#!/usr/bin/env bash
# 示例：把询价单放到 data/input/inquiry.xlsx 后执行
set -euo pipefail
cd "$(dirname "$0")/.."

INPUT="${1:-data/input/inquiry.xlsx}"
OUT_DIR="data/output"
PROFILE=".browser-profile"

python -m material_price_audit check
python -m material_price_audit status --input "$INPUT"

echo ""
echo ">>> login（浏览器弹出后请登录京东/1688）"
python -m material_price_audit login --profile "$PROFILE"

echo ""
echo ">>> scrape 试跑 8 条"
python -m material_price_audit scrape \
  --input "$INPUT" \
  --output "$OUT_DIR/result.xlsx" \
  --evidence "$OUT_DIR/evidence.json" \
  --profile "$PROFILE" \
  --limit 8

echo ""
echo ">>> rfq 未命中项"
python -m material_price_audit rfq \
  --input "$INPUT" \
  --evidence "$OUT_DIR/evidence.json" \
  --output "$OUT_DIR/rfq.xlsx"

echo "完成。查看: $OUT_DIR/result.xlsx"
