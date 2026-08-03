#!/usr/bin/env bash
# 构建 wheel / sdist / 便携 zip，输出到 dist/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION=$(python3 -c "from material_price_audit import __version__; print(__version__)")
NAME="material-price-audit-${VERSION}"
echo "==> 版本 ${VERSION}"

python3 -m pip install -q -U build setuptools wheel
rm -rf dist build *.egg-info
python3 -m build

# 便携包：源码 + 安装脚本 + 说明（不含 .venv / data 隐私）
STAGE="dist/${NAME}-portable"
rm -rf "$STAGE" "dist/${NAME}-portable.zip"
mkdir -p "$STAGE"

# 用 git archive 保证干净树
git archive --format=tar HEAD | tar -x -C "$STAGE"

# 确保安装脚本可执行说明与 wheel 副本
cp -f dist/material_price_audit-*.whl "$STAGE/" 2>/dev/null || \
  cp -f dist/material_price_audit-*.whl "$STAGE/" 2>/dev/null || true
# setuptools 可能用连字符名
cp -f dist/*.whl "$STAGE/" 2>/dev/null || true
cp -f dist/*.tar.gz "$STAGE/" 2>/dev/null || true

# 清理便携包内不需要的重型/隐私路径
rm -rf "$STAGE/.browser-profile" "$STAGE/data/output"/* "$STAGE/data/user"/* \
  "$STAGE/data/mapping-cache" "$STAGE/.git" 2>/dev/null || true
mkdir -p "$STAGE/data/output" "$STAGE/data/input" "$STAGE/data/user"
touch "$STAGE/data/output/.gitkeep" 2>/dev/null || true

(
  cd dist
  zip -r -q "${NAME}-portable.zip" "${NAME}-portable"
)

echo "==> 产物："
ls -lh dist/*."${VERSION}"* dist/*portable* dist/*.whl dist/*.tar.gz 2>/dev/null || ls -lh dist/
echo "OK: dist/${NAME}-portable.zip"
