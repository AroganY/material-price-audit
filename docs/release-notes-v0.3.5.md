## 材料询价工作台 v0.3.5（推荐使用）

修复 macOS 从「下载」直接打开时，因系统隔离（App Translocation）只读路径无法创建虚拟环境的问题。

### 推荐下载

| 文件 | 说明 |
|------|------|
| MaterialPriceAudit-macOS-v0.3.5.zip | Mac：解压后建议拖到「应用程序」再双击 |
| MaterialPriceAudit-Windows-v0.3.5.zip | Windows：解压后双击 bat 启动 |
| material-price-audit-0.3.5-portable.zip | 命令行便携包 |
| material_price_audit-0.3.5-py3-none-any.whl | pip 安装 |
| material_price_audit-0.3.5.tar.gz | 源码包 |

### 使用提示（macOS）

1. 将 App 拖到「应用程序」
2. 终端执行：`xattr -cr /Applications/材料询价工作台.app`
3. 双击打开；首次自动装依赖并打开 http://127.0.0.1:8765/
4. 数据目录：`~/Library/Application Support/MaterialPriceAudit/`

需要本机已安装 **Python 3.10+**。

### 相关链接

- 价联通客户端：https://www.scjcio.site/tools/download
- 若亘官网：https://www.rogan.asia/
- 安装说明：仓库内 INSTALL.zh.md
