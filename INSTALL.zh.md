# 安装与使用（普通用户）

适合：会装 Python、想快速用浏览器向导询价的造价员。

## 环境要求

- **Python 3.10+**（终端执行 `python3 --version` 查看）
- 本机 **Chrome / Chromium**（推荐）
- 磁盘约 500MB+（含浏览器驱动）

## 方式 A：macOS / Windows 双击启动包（最省事）

从 [GitHub Releases](https://github.com/AroganY/material-price-audit/releases) 下载对应系统的 zip：

| 文件 | 系统 |
|------|------|
| **MaterialPriceAudit-macOS-v\*.zip** | Mac |
| **MaterialPriceAudit-Windows-v\*.zip** | Windows |

**macOS**
1. 解压后，把 **「材料询价工作台.app」拖到「应用程序」**（不要在「下载」里直接开）
2. 若无法打开：右键 → 打开；或终端：`xattr -cr /Applications/材料询价工作台.app`
3. 双击 App；首次会自动装依赖 + Chromium（需联网，几分钟）
4. 完成后**自动弹出浏览器**进入向导 `http://127.0.0.1:8765/`
5. 若曾在下载区打开失败（只读/AppTranslocation），新版本会把环境写到  
   `~/Library/Application Support/MaterialPriceAudit/`

**Windows**
1. 解压 → 双击 **「双击启动-材料询价工作台.bat」**
2. 首次自动装依赖 + Chromium（需联网）
3. 完成后**自动弹出浏览器**进入向导

> 仍需本机已安装 **Python 3.10+**（不是完全零依赖安装包；Chromium 首次在线下载）。  
> 真正的「单文件免 Python」体积很大，后续版本可再做。

## 方式 B：命令行便携包

1. 下载 **`material-price-audit-*-portable.zip`**
2. 解压到任意目录（路径尽量不要有奇怪空格）
3. **macOS / Linux** 终端进入解压目录后执行：

```bash
chmod +x install.sh start.sh
./install.sh          # 首次：创建虚拟环境、装依赖、装 Chromium
./start.sh            # 启动向导 → 浏览器打开 http://127.0.0.1:8765/
```

4. **Windows**（PowerShell）进入解压目录：

```powershell
.\install.ps1
.\start.ps1
```

## 方式 C：pip 安装 wheel

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install material_price_audit-*.whl
python -m playwright install chromium
material-price-audit serve
```

## 方式 D：从源码（开发者）

```bash
git clone https://github.com/AroganY/material-price-audit.git
cd material-price-audit
pip install -e .
python -m playwright install chromium
python -m material_price_audit serve
```

## 使用提醒

1. 向导地址：**http://127.0.0.1:8765/**（仅本机）
2. 登录会员站请在**本工具弹出的浏览器**里操作
3. 询价结果请**自行核对**原站链接；开启 AI 可提高检索成功率，但**不会编造价格**
4. 数据与 Cookie 只在本机：`data/`、`.browser-profile/`，勿提交到 Git

## 常见问题

| 现象 | 处理 |
|------|------|
| 找不到 python3 | 安装 Python 3.10+，并勾选加入 PATH |
| playwright 浏览器失败 | 再执行：`python -m playwright install chromium` |
| 端口被占用 | `python -m material_price_audit serve --port 8766` |
| 登录后又要登 | 务必在工具弹出的窗口登录，不要只用系统 Chrome |

更多说明见 [README.md](./README.md)。

## 价联通客户端

若需要若亘 **价联通** 桌面客户端（非本开源本地向导），请访问：

**https://www.scjcio.site/tools/download**
