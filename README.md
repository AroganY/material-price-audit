# material-price-audit

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org/)
[![Accuracy First](https://img.shields.io/badge/accuracy-first-teal.svg)](./docs/OPEN_SOURCE.md)

**造价材料询价核价工具（准确性优先）**

- 询价单 Excel **入参** → 多平台登录比价 → 审定结果 / 证据 JSON **出参**
- 内置 **广材网 · 慧讯网 · 领材网**（广联达 gldjc 体系）+ 京东 / 1688 / 震坤行等
- **无证据不填审定单价**；`审定 = min(挂牌含税÷1.13, 报送不含税)`
- **AI Agent 可初始化引导**：`init` / `guide` + [AGENTS.md](./AGENTS.md)

---

## 30 秒上手

```bash
git clone <your-repo-url> material-price-audit
cd material-price-audit
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

# 初始化（人类或 Agent 统一入口）
python -m material_price_audit init --platforms guangcai,huixun,lingcai,jd,1688

# 浏览器打开操作引导
open docs/index.html   # macOS；Windows 可双击该文件
```

| 文档 | 说明 |
|------|------|
| **[docs/index.html](./docs/index.html)** | 网页操作引导（推荐用户打开） |
| **[docs/USAGE.md](./docs/USAGE.md)** | 完整使用教程 |
| **[docs/OPEN_SOURCE.md](./docs/OPEN_SOURCE.md)** | 开源介绍 / 原则 / 合规 |
| **[AGENTS.md](./AGENTS.md)** | AI Agent 协议（先 init） |
| **[教程-用Grok或Codex执行核价.md](./教程-用Grok或Codex执行核价.md)** | 小白提示词 |

---

## 路径约定

| 用途 | 路径 |
|------|------|
| 入参询价单 | `data/input/inquiry.xlsx` |
| 出参结果 | `data/output/result.xlsx` |
| 出参证据 | `data/output/evidence.json` |
| 出参 RFQ | `data/output/rfq.xlsx` |
| Agent 下一步 | `data/output/AGENT_NEXT.md` |
| 登录态（**勿提交 git**） | `.browser-profile/` |

---

## 标准流程

```bash
# 1) 环境
python -m material_price_audit check

# 2) 初始化 + 引导
python -m material_price_audit init --platforms guangcai,huixun,lingcai,jd,1688
python -m material_price_audit guide

# 3) 询价单放到 data/input/inquiry.xlsx

# 4) 登录（浏览器弹出，你自己登）
python -m material_price_audit login \
  --profile .browser-profile \
  --platforms guangcai,huixun,lingcai,jd,1688

# 5) 试跑 8 条
python -m material_price_audit scrape \
  --input  data/input/inquiry.xlsx \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile \
  --platforms guangcai,huixun,lingcai,jd,1688 \
  --limit 8

# 6) 全量 + 未命中询价单
python -m material_price_audit scrape ...   # 去掉 --limit
python -m material_price_audit rfq \
  --input data/input/inquiry.xlsx \
  --evidence data/output/evidence.json \
  --output data/output/rfq.xlsx
```

### Agent 一键引导

```bash
bash examples/agent_bootstrap.sh
# 或指定平台：
bash examples/agent_bootstrap.sh guangcai,huixun,lingcai,jd,1688
```

对 Grok / Codex / Claude 说：

```text
请按 AGENTS.md 执行：先 init，读 AGENT_NEXT.md，按 questions 一项项问我。
```

---

## 内置平台

| ID | 名称 | 备注 |
|----|------|------|
| `guangcai` | 广材网 | gldjc.com，需登录 |
| `huixun` | 慧讯网 | 广联达材料价，与广材同体系 |
| `lingcai` | 领材网 | 默认同检索；独立域名可覆盖 |
| `gldjc_hangqing` | 广材行情 | 钢材行情 |
| `gldjc_xunjia` | 广材询价 | 人工询价入口 |
| `jd` / `1688` / `zkh` / … | 电商 | 零售/批发补充 |

中文别名：`--platforms 广材网,慧讯网,领材网,京东`

自定义网站见 `config.example.yaml` → `platforms.definitions`。

---

## 原则（Accuracy First）

1. **无 verified 证据 → 不填审定单价**  
2. `审定 = min(挂牌含税 ÷ tax_divisor, 报送不含税)`  
3. 尽量详情页二次确认；禁止搜索页冒充  
4. 平台由用户指定；Agent 先问再跑  

---

## 开发

```bash
pip install -e .
python -m material_price_audit platforms
```

结构说明与合规见 [docs/OPEN_SOURCE.md](./docs/OPEN_SOURCE.md)。

---

## License

[MIT](./LICENSE)
