#!/usr/bin/env bash
# 构建 macOS / Windows「双击启动」分发包（输出到 dist/）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION=$(python3 -c "from material_price_audit import __version__; print(__version__)")
echo "==> 构建桌面启动包 v${VERSION}"

STAGE_MAC="dist/MaterialPriceAudit-macOS-v${VERSION}"
STAGE_WIN="dist/MaterialPriceAudit-Windows-v${VERSION}"
rm -rf "$STAGE_MAC" "$STAGE_WIN"
mkdir -p "$STAGE_MAC" "$STAGE_WIN"

# --- 公共 app 树（干净源码，无 .venv）---
fill_app() {
  local dest="$1"
  mkdir -p "$dest"
  # 用 git archive 导出当前提交
  git archive --format=tar HEAD | tar -x -C "$dest"
  # 确保启动器在
  cp -f "$ROOT/desktop_launch.py" "$dest/"
  rm -rf "$dest/.browser-profile" "$dest/data/output" "$dest/data/user" "$dest/data/mapping-cache" 2>/dev/null || true
  mkdir -p "$dest/data/output" "$dest/data/input" "$dest/data/user"
  touch "$dest/data/output/.gitkeep"
  # 不把 release venv 打进去
  rm -rf "$dest/.release-venv" "$dest/.venv" 2>/dev/null || true
}

# ========== macOS .app ==========
APP="$STAGE_MAC/材料询价工作台.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"
fill_app "$APP/Contents/Resources/app"
cp -f "$ROOT/packaging/macos/Info.plist" "$APP/Contents/"
cp -f "$ROOT/packaging/macos/MaterialPriceAudit" "$APP/Contents/MacOS/"
chmod +x "$APP/Contents/MacOS/MaterialPriceAudit"
# 若有图标可拷贝；无则跳过
if [[ -f "$ROOT/packaging/macos/AppIcon.icns" ]]; then
  cp -f "$ROOT/packaging/macos/AppIcon.icns" "$APP/Contents/Resources/"
fi
cp -f "$ROOT/packaging/macos/启动说明.txt" "$STAGE_MAC/"
# 备用：双击 .command
cat > "$STAGE_MAC/若无法打开App则双击这里.command" <<'EOF'
#!/bin/bash
cd "$(dirname "$0")/材料询价工作台.app/Contents/Resources/app"
exec python3 desktop_launch.py
EOF
chmod +x "$STAGE_MAC/若无法打开App则双击这里.command"

(cd dist && zip -r -y "MaterialPriceAudit-macOS-v${VERSION}.zip" "MaterialPriceAudit-macOS-v${VERSION}")
echo "OK dist/MaterialPriceAudit-macOS-v${VERSION}.zip"

# ========== Windows ==========
mkdir -p "$STAGE_WIN/app"
fill_app "$STAGE_WIN/app"
cp -f "$ROOT/packaging/windows/双击启动-材料询价工作台.bat" "$STAGE_WIN/"
cp -f "$ROOT/packaging/windows/启动说明.txt" "$STAGE_WIN/"
# 再拷一份 install.ps1 备用
cp -f "$ROOT/install.ps1" "$STAGE_WIN/app/" 2>/dev/null || true

(cd dist && zip -r "MaterialPriceAudit-Windows-v${VERSION}.zip" "MaterialPriceAudit-Windows-v${VERSION}")
echo "OK dist/MaterialPriceAudit-Windows-v${VERSION}.zip"

ls -lh dist/MaterialPriceAudit-*-v${VERSION}.zip
echo "完成。用户解压后双击即可（需本机已装 Python 3.10+）。"
