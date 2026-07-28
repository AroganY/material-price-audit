# material-price-audit · 开源介绍

**Accuracy-first construction material inquiry price audit.**  
造价场景下的**材料询价核价**工具：以「有证据才填审定单价」为第一原则，支持多平台（广材网 / 慧讯网 / 领材网 / 京东 / 1688 等）登录比价，并输出可审计的 Excel + JSON。

---

## 为什么做这个

工程造价审核里，施工单位报送材料单价往往偏高。审价需要：

1. **上限明确**：报送不含税单价不可突破  
2. **依据可查**：价格应对应商品/材料详情或信息站挂牌，而不是“感觉核减”  
3. **路径清晰**：询价单进、结果出，方便开源协作与 AI Agent 自动跑流程  

本工具用 Playwright 在用户登录后的真实浏览器会话里检索材料价，把 **含税挂牌 → 折算不含税 → 与报送价取低** 的过程固化成命令行与 Agent 协议。

---

## 核心原则（Accuracy First）

| 原则 | 说明 |
|------|------|
| 无证据不填审定 | 没有 `verified` 详情/列表证据，**审定不含税单价留空** |
| 不超报送 | `审定 = min(挂牌含税 ÷ 税点, 报送不含税)` |
| 禁止搜索页冒充 | 搜索列表不能单独当最终依据；尽量进详情二次确认 |
| 平台用户指定 | 默认推荐造价站，但不锁死；可扩展任意网站 |
| Agent 可引导 | `init` / `guide` 输出阶段与要问用户的问题 |

---

## 功能一览

- **Excel 询价单入参**：自动识别「报送不含税单价 / 材料名称 / 规格」等列  
- **多平台抓取**：广材网、慧讯网、领材网（gldjc 体系）、京东、1688、淘宝、天猫、震坤行等  
- **自定义平台**：`config.yaml` 里配置登录页、搜索 URL、选择器  
- **证据链**：`evidence.json` 记录 URL、挂牌价、平台、备选结果  
- **结果 Excel**：回填审定列 +「实抓汇总」可点击详情  
- **RFQ 导出**：未命中项生成供应商询价单  
- **AI Agent 协议**：`AGENTS.md` + `init`/`guide` 阶段机  

---

## 技术栈

- Python 3.10+  
- [Playwright](https://playwright.dev/)（有界面浏览器，保留登录态）  
- openpyxl（读写 xlsx）  
- PyYAML（配置）  

---

## 仓库结构（开源边界）

```text
material-price-audit/
├── README.md                 # 入口
├── LICENSE                   # MIT
├── AGENTS.md                 # AI Agent 协议
├── requirements.txt
├── pyproject.toml
├── config.example.yaml
├── material_price_audit/     # 源码
├── docs/
│   ├── OPEN_SOURCE.md        # 本文
│   ├── USAGE.md              # 使用教程
│   └── index.html            # 浏览器打开的操作引导
├── examples/
├── data/input|output/        # 运行时数据（默认不提交成果）
└── tests/
```

**请勿提交：**

- `.browser-profile/`（登录 Cookie）  
- `data/output/*` 中的真实项目报价  
- 含敏感项目名称的询价单（示例请脱敏）  

---

## 快速开始（30 秒）

```bash
git clone <your-repo-url> material-price-audit
cd material-price-audit
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

python -m material_price_audit init
python -m material_price_audit check
```

浏览器打开操作引导：

```bash
open docs/index.html   # macOS
# 或直接双击 docs/index.html
```

完整步骤见 [USAGE.md](./USAGE.md)。

---

## 适用与边界

**适合**

- 安装/装饰/市政等材料询价表核价  
- 有型号的设备与通用建材  
- 需要可追溯链接的内部审核底稿  

**不适合 / 需人工**

- 独家定制、人防专用无公开价设备  
- 信息站会员价未授权抓取的场景（请遵守网站服务条款与当地法律）  
- 把零售价直接当合同结算价（仍需结合合同、品牌锁定、运费与税率）  

---

## 合规声明

1. 请仅在你有权访问的账号下登录信息站/电商。  
2. 自动访问频率请保持克制，遵守 robots 与网站用户协议。  
3. 本工具输出为**审核参考**，不构成造价咨询执业意见。  
4. 开源协议 MIT：见根目录 [LICENSE](../LICENSE)。  

---

## 贡献

欢迎 PR：

- 新平台适配（`platforms.py` / `definitions`）  
- 更稳的选择器与登录检测  
- 信息价 PDF/信息价表解析  
- 多语言文档  

提交前请运行：

```bash
python -m material_price_audit check
python -m material_price_audit platforms
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [USAGE.md](./USAGE.md) | 完整使用教程 |
| [index.html](./index.html) | 可视化操作引导 |
| [../AGENTS.md](../AGENTS.md) | AI Agent 初始化协议 |
| [../教程-用Grok或Codex执行核价.md](../教程-用Grok或Codex执行核价.md) | 小白 + 提示词 |
| [../README.md](../README.md) | 仓库主说明 |
