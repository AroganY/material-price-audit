# material-price-audit · 使用教程

面向：**造价人员 / 开发者 / 让 AI Agent 代跑**。

更易读的网页版：**[index.html](./index.html)**（浏览器打开）。

---

## 1. 安装

```bash
cd material-price-audit
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
python -m material_price_audit check
```

看到 `状态 : OK` 即可。

---

## 2. 初始化（人类或 Agent 都从这里开始）

```bash
python -m material_price_audit init
```

指定平台（推荐造价三站 + 电商）：

```bash
python -m material_price_audit init --platforms guangcai,huixun,lingcai,jd,1688 --force
```

生成：

- `config.yaml`
- `data/input/`、`data/output/`
- `.browser-profile/`
- `data/output/AGENT_NEXT.md`（Agent 下一步）
- `data/output/agent_state.json`

查看下一步：

```bash
python -m material_price_audit guide
```

---

## 3. 准备询价单（入参）

把 Excel 放到 `data/input/` 即可，**文件名随意**（不必叫 `inquiry.xlsx`）：

```text
data/input/安装专业询价材料设备.xlsx
data/input/项目A材料询价.xlsx
```

`run` / `scrape` 不传 `--input` 时会自动扫描该目录：优先表头含「报送不含税单价」的文件。

### 表头要求

| 列 | 是否必须 |
|----|----------|
| 材料名称 | 建议 |
| 规格、型号 | 建议 |
| 单位 / 数量 | 建议 |
| **报送不含税单价** | **必须**（审定上限） |
| 审定不含税单价 | 可空，工具填写 |
| 品牌 | 可选 |

列名可在 `config.yaml` → `excel` 配置别名。

---

## 4. 选择平台并登录

### 内置平台（节选）

| ID | 名称 | 登录入口 |
|----|------|----------|
| guangcai | 广材网 | https://www.gldjc.com/login |
| huixun | 慧讯网（RCC） | https://services.iccchina.com/login |
| lingcai | 领材网 | https://www.hylcw.cn/userInfo/index.html |
| jd / 1688 | 京东 / 批发 | 各自官网 |
| zkh / taobao / tmall / suning | 工业品与电商 | 各自官网 |

三家造价信息站域名不同，登录会**分别**打开，不会串成广材。详见 [PLATFORMS.md](./PLATFORMS.md)。

```bash
python -m material_price_audit platforms

python -m material_price_audit login \
  --profile .browser-profile \
  --platforms guangcai,huixun,lingcai,jd,1688
```

浏览器会按平台打开登录页。**广材/慧讯/领材通常要会员账号**。

---

## 5. 抓取核价

### 试跑（务必先做）

```bash
python -m material_price_audit scrape \
  --input  data/input/inquiry.xlsx \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile \
  --platforms guangcai,huixun,lingcai,jd,1688 \
  --limit 8
```

### 全量

```bash
python -m material_price_audit scrape \
  --input  data/input/inquiry.xlsx \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile \
  --platforms guangcai,huixun,lingcai,jd,1688
```

### 人工挑选候选（最准）

```bash
python -m material_price_audit scrape ... --manual --limit 20
```

---

## 6. 看结果

打开 `data/output/result.xlsx`：

1. Sheet **「实抓汇总」** — 仅 verified 行  
2. 点 **「打开详情页」** 核对型号与价格  
3. 各专业原表：有证据的已填 **审定不含税单价**；无证据为 pending  

证据明细：`data/output/evidence.json`

### 计价规则

```text
不含税参考价 ≈ 挂牌含税 ÷ 1.13   （可在 config 改 tax_divisor）
审定不含税   = min(不含税参考价, 报送不含税单价)
```

---

## 7. 未命中 → 供应商询价

```bash
python -m material_price_audit rfq \
  --input data/input/inquiry.xlsx \
  --evidence data/output/evidence.json \
  --output data/output/rfq.xlsx
```

把 `rfq.xlsx` 发给供应商，收回盖章报价后再人工或脚本回填。

---

## 8. 让 AI Agent 代跑

### 你对 Agent 说

```text
请按 AGENTS.md 执行 material-price-audit：
1) init
2) 读 AGENT_NEXT.md，按 questions 一项项问我
3) 执行 next_command
4) 每步后 guide
```

### Agent 命令

```bash
python -m material_price_audit init
python -m material_price_audit guide
# … 按 phase 继续 login / scrape …
```

详见 [AGENTS.md](../AGENTS.md)。

---

## 9. 路径约定（开源协作统一）

| 角色 | 路径 |
|------|------|
| 入参询价单 | `data/input/*.xlsx`（任意文件名，自动识别） |
| 出参结果 | `data/output/result.xlsx` |
| 出参证据 | `data/output/evidence.json` |
| 出参 RFQ | `data/output/rfq.xlsx` |
| 配置 | `config.yaml` |
| 登录态 | `.browser-profile/`（勿提交） |

也可用绝对路径：

```bash
python -m material_price_audit scrape \
  --input /path/to/inquiry.xlsx \
  --output /path/to/result.xlsx \
  --evidence /path/to/evidence.json \
  --profile /path/to/.browser-profile
```

---

## 10. 常见问题

**Q: 广材一直跳登录？**  
A: 正常。先 `login` 用会员账号登录，再 scrape。

**Q: 很多材料没有审定价？**  
A: 正常。无公开价/定制设备不会编造，请用 `rfq.xlsx` 询供应商。

**Q: 1688 在 config 里变奇怪数字？**  
A: 写成 `"1688"`（带引号），YAML 才会当字符串。

**Q: 领材网 / 慧讯网是不是广材？**  
A: 不是。领材 = `hylcw.cn`，慧讯 = `iccchina.com`（RCC），广材 = `gldjc.com`。内置 URL 已写死；官网变更时再在 `platforms.definitions` 覆盖。

**Q: 询价表必须叫 inquiry.xlsx 吗？**  
A: 不必。丢进 `data/input/` 任意名字即可；多文件时按表头自动选。

**Q: 能 headless 吗？**  
A: 登录场景不推荐。默认有界面，便于人工登录与风控验证。

---

## 11. 命令速查

| 命令 | 作用 |
|------|------|
| `check` | 环境检查 |
| `init` | 初始化 + Agent 引导 |
| `guide` | 刷新下一步 |
| `platforms` | 列出平台 |
| `login` | 登录指定平台 |
| `scrape` | 抓价并出结果 |
| `merge` | 仅合并 evidence→Excel |
| `rfq` | 导出待询价 |
| `status` | 状态 |
