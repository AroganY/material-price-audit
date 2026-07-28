# AGENTS.md — 给所有 AI Agent 的操作协议

> 本文件是 **material-price-audit** 的机器可读流程。  
> **任何模型（Grok / Codex / Claude / Cursor）接入时：先 `init`，再读引导，再问用户。**

---

## 0. 一句话

用户说「询价核价 / 安装材料审价 / 帮我抓价」时：

```text
1) cd 到本包根目录
2) python -m material_price_audit init
3) 阅读终端 AGENT_GUIDE 块 + data/output/AGENT_NEXT.md
4) 按 questions 逐条问用户
5) 用户答完后执行 next_command
6) 每完成一步再跑 python -m material_price_audit guide
```

---

## 1. 包根目录

```text
/Users/arogan/myMvp/AI搞定造价IDEA/AI询价/material-price-audit
```

（开源克隆后 = 含 `pyproject.toml` 与 `material_price_audit/` 的目录）

---

## 2. 硬规则（违反即错误）

| # | 规则 |
|---|------|
| R1 | **无 `status=verified` 证据 → 禁止填审定不含税单价** |
| R2 | 禁止用搜索列表页冒充详情证据 |
| R3 | `审定 = min(挂牌含税÷tax_divisor, 报送不含税)` |
| R4 | 环境 `check` 失败时，先装依赖，不要 scrape |
| R5 | `--input --output --evidence --profile` 路径必须显式 |
| R6 | **平台由用户指定**，禁止默认写死「只有京东/阿里」而不询问 |
| R7 | 先 `--limit 8` 试跑，成功再全量 |
| R8 | 登录必须用户在浏览器完成；Agent 只负责打开 login 流程 |

---

## 3. 标准路径

| 用途 | 相对路径 |
|------|----------|
| 配置 | `config.yaml`（`init` 生成） |
| 询价单入参 | `data/input/inquiry.xlsx` |
| 结果出参 | `data/output/result.xlsx` |
| 证据 | `data/output/evidence.json` |
| RFQ | `data/output/rfq.xlsx` |
| Agent 状态 | `data/output/agent_state.json` |
| Agent 下一步 | `data/output/AGENT_NEXT.md` |
| 浏览器登录态 | `.browser-profile/`（勿提交 git） |

---

## 4. 阶段机 phase

`init` / `guide` 会输出 `phase`：

| phase | 你要做的事 |
|-------|------------|
| `need_env` | 展示安装命令，征得同意后执行 pip / playwright install，再 `check` |
| `need_config` | 已 `init` 或再 `init --platforms ...` |
| `need_inquiry` | 请用户放置询价单到 `data/input/inquiry.xlsx`，确认表头 |
| `need_login` | 确认平台列表 → `login --platforms ...` → 等用户说「登录完成」 |
| `ready_scrape` | `scrape --limit 8` → 核对 → 全量 scrape → `rfq` |
| `done` | 汇报结果路径与 verified 数量 |

**每完成一个用户动作后执行：**

```bash
python -m material_price_audit guide
```

---

## 5. 初始化（唯一推荐入口）

```bash
cd "<package-root>"
python -m material_price_audit init
```

若用户已说平台：

```bash
python -m material_price_audit init --platforms guangcai,huixun,lingcai,jd,1688 --tax 1.13
```

终端会出现：

```text
========== AGENT_GUIDE_BEGIN ==========
phase: ...
questions:
  - ...
next_command: ...
========== AGENT_GUIDE_END ==========
```

**Agent 必须：**

1. 解析 `AGENT_GUIDE` 或读 `data/output/AGENT_NEXT.md`
2. 用中文向用户提问 `questions`（建议一次 1 个问题）
3. 用户回答后执行 `next_command`（可补全路径）
4. 不要跳过 phase 乱 scrape

---

## 6. 向用户收集的配置项

初始化对话中按需收集（有默认值可跳过）：

| 配置项 | 问法示例 | 默认 |
|--------|----------|------|
| 平台列表 | 要在哪些网站比价？广材/慧讯/领材 + 京东/1688…？ | guangcai,huixun,lingcai,jd,1688 |
| 自定义站 | 若有内部商城，请给：名称、登录页URL、搜索URL（含`{query}`） | 无 |
| 询价单 | 请把 Excel 放到 `data/input/inquiry.xlsx` | — |
| 税率折算 | 含税÷多少当不含税？ | 1.13 |
| 试跑条数 | 先试跑几条？ | 8 |

收集平台后：

```bash
python -m material_price_audit init --platforms guangcai,huixun,lingcai,jd,1688 --force
```

---

## 7. 命令速查

```bash
# 环境
python -m material_price_audit check

# 初始化 + 引导（首选）
python -m material_price_audit init --platforms guangcai,huixun,lingcai,jd,1688
python -m material_price_audit guide

# 平台
python -m material_price_audit platforms

# 登录（用户操作浏览器）
python -m material_price_audit login --profile .browser-profile \
  --platforms guangcai,huixun,lingcai,jd,1688

# 试跑
python -m material_price_audit scrape \
  --input data/input/inquiry.xlsx \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile \
  --platforms guangcai,huixun,lingcai,jd,1688 \
  --limit 8

# 全量
python -m material_price_audit scrape \
  --input data/input/inquiry.xlsx \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile \
  --platforms guangcai,huixun,lingcai,jd,1688

# 未命中
python -m material_price_audit rfq \
  --input data/input/inquiry.xlsx \
  --evidence data/output/evidence.json \
  --output data/output/rfq.xlsx
```

非交互等待登录（用户离开键盘时）：

```bash
... login --yes --login-wait 120
... scrape --yes --login-wait 90 --limit 8
```

---

## 8. 对用户的标准话术

### 开场（init 后）

```text
我已经初始化「材料询价核价」工具。
接下来需要你做几项配置：
1）选择要比价的网站
2）把询价单放到指定文件夹
3）在浏览器登录这些网站
我不会在没有商品证据时乱填审定价格。
```

### 要平台时

```text
请告诉我启用哪些平台（可多选）：

【造价材料信息站 · 推荐】
- guangcai  广材网（gldjc.com，通常需登录）
- huixun    慧讯网（广联达材料价，与广材同体系）
- lingcai   领材网（默认同检索入口；独立域名可配置）

【电商补充】
- jd / 1688 / zkh / taobao / tmall / suning / mysteel

或你们自己的网站（需登录地址 + 搜索地址）
直接回复例如：guangcai,huixun,lingcai,jd,1688
```

### 要询价单时

```text
请把询价 Excel 放到：
  <root>/data/input/inquiry.xlsx
表头需要有「报送不含税单价」。放好后回复：已放好
```

### 登录时

```text
我现在打开浏览器，请依次登录：<平台列表>。
全部登录完成后回复：登录完成
```

### 试跑后

```text
试跑结束：命中 N 条。
请打开 data/output/result.xlsx →「实抓汇总」，点开蓝色链接核对型号。
确认没问题后回复：全量 或 导出询价单
```

---

## 9. 完成汇报模板

```text
- 启用平台：...
- 材料总数 / 可匹配 / verified：...
- 结果：data/output/result.xlsx
- 证据：data/output/evidence.json
- 未命中 RFQ：data/output/rfq.xlsx
- 提醒：请人工抽查详情页链接与型号一致性
```

---

## 10. 禁止行为

- 不要读取旧的「审定核价完成 / 带可点击URL」假结果当最终依据  
- 不要在 `phase=need_env` 时强行 scrape  
- 不要替用户编造商品详情 URL  
- 不要一次问 10 个问题；跟随 `AGENT_NEXT.md` 的 questions  

---

## 11. 相关文件

| 文件 | 读者 |
|------|------|
| `AGENTS.md` | AI Agent（本文） |
| `data/output/AGENT_NEXT.md` | 运行时动态下一步 |
| `data/output/agent_state.json` | 运行时状态 JSON |
| `README.md` | 人类技术文档 |
| `教程-用Grok或Codex执行核价.md` | 人类小白 + 提示词 |
| `config.example.yaml` | 配置模板 |
