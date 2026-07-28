# AGENTS.md — AI 自动化协议（少问话、多执行）

> **不要一步一步碎问。**  
> 用户要的是：环境自检 → 一次选好平台 → 自动登录等待 → 瀑布匹配抓价 → 出结果。

---

## 0. 默认行为（必须）

用户一说「核价 / 询价 / 跑工具」：

```bash
cd <package-root>   # material-price-audit 目录

# 1) 环境自检；失败则自动装依赖
python3 -m material_price_audit check --auto-install

# 2) 若用户还没给平台：打开/告知勾选页（一次多选），或直接用默认
#    默认：guangcai,huixun,lingcai,jd,1688
#    勾选页：docs/platform-select.html  → 生成 platforms 列表

# 3) 一键跑（询价单默认 data/input/inquiry.xlsx）
python3 -m material_price_audit run \
  --platforms guangcai,huixun,lingcai,jd,1688 \
  --auto-install \
  --login-wait 90
```

**只在这两种情况停下来问用户：**

1. **没有 Python / 自动安装失败** → 把安装命令甩给用户（或帮装）  
2. **没有询价单文件** → 一句：「请把表放到 `data/input/inquiry.xlsx`」  
3. **登录** → 浏览器已打开，提示「请在 90 秒内登录各平台（已登录可忽略）」——**不要每个平台单独问一遍**

不要：先问平台、再问路径、再问是否登录、再问是否试跑……那种傻逼流程。

---

## 1. 环境自检（Python 都没有也要提示）

```bash
python3 -m material_price_audit check --auto-install
```

| 情况 | Agent 动作 |
|------|------------|
| 无 `python3`/`python` | 提示安装 Python 3.10+（brew / python.org），装完重跑 |
| 缺 pip 包 / playwright | `--auto-install` 已尝试；仍失败则打印 hints 给用户 |
| OK | 继续 `run` |

检测逻辑在 `env_check.py`，会输出 `AGENT_ENV_FAIL` 块。

---

## 2. 平台：一次多选，不要连问

### 方式 A — 默认（最快）

直接：

```bash
--platforms guangcai,huixun,lingcai,jd,1688
```

### 方式 B — 用户勾选（HTML）

1. 让用户打开 `docs/platform-select.html`  
2. 勾选平台 → 点「生成命令并复制」  
3. Agent 执行复制出的 `run` 命令  

### 方式 C — 用户一句话

用户说「广材+京东+1688」→ 转成：

```bash
--platforms guangcai,jd,1688
```

中文别名已支持：`广材网,慧讯网,领材网`。

**优先级 = 列表从左到右（A→B→C）。**

---

## 3. 瀑布匹配规则（核心）

对每一条材料：

```text
平台A：搜索 → 打开详情
  ├─ 规格/型号（及关键中文词）匹配 → 采用 A 的价，停止
  └─ 不匹配 / 无结果 → 自动试平台B → C → …
全部失败 → status=no_match（审定留空），进 RFQ
```

实现：`platform_strategy: waterfall` + `matching.detail_matches_item`  
详情页必须对上型号/规格倾向的 token，**不是随便搜到第一个价就用**。

---

## 4. 一键命令 `run`

```bash
python3 -m material_price_audit run \
  --platforms guangcai,huixun,lingcai,jd,1688 \
  --auto-install \
  --login-wait 90
```

内部顺序：

1. `check`（可选 auto-install）  
2. `init` 写 config  
3. 打开各平台登录页，**每个只等 login-wait 秒**（非交互）  
4. 瀑布 scrape  
5. 自动导出 `rfq.xlsx`  

试跑 8 条：

```bash
python3 -m material_price_audit run --platforms guangcai,huixun,lingcai --limit 8 --login-wait 90 --auto-install
```

已登录过可：

```bash
python3 -m material_price_audit run --skip-login --platforms ...
```

---

## 5. 路径（固定，别问）

| 用途 | 默认路径 |
|------|----------|
| 询价单 | `data/input/inquiry.xlsx` |
| 结果 | `data/output/result.xlsx` |
| 证据 | `data/output/evidence.json` |
| RFQ | `data/output/rfq.xlsx` |
| 勾选平台文件 | `data/output/platforms.selected` |
| 登录态 | `.browser-profile/` |

用户表不在默认路径时，**一次**用 `--input` 指定，不要反复确认。

---

## 6. 对用户话术（短）

### 开场

```text
我来自动跑核价：先检查环境，再用广材/慧讯/领材（可改）按顺序匹配详情价。
请确认询价表在 data/input/inquiry.xlsx；浏览器弹出时登录一下各网站即可。
```

### 只要平台时（可选一句）

```text
默认平台顺序：广材→慧讯→领材→京东→1688。
要改的话直接回：例如 广材,京东,1688
或打开 docs/platform-select.html 勾选。
不回则用默认，我直接跑。
```

### 结束

```text
完成：verified=N。结果 data/output/result.xlsx，未命中 RFQ=data/output/rfq.xlsx。
请打开「实抓汇总」抽查几条详情链接。
```

---

## 7. 禁止

- 禁止逐步：问平台 → 问路径 → 问登录 → 问是否试跑 → 问是否全量  
- 禁止无详情匹配就采用列表价（waterfall 模式）  
- 禁止编造审定价  
- 禁止提交 `.browser-profile` 与真实报价表  

---

## 8. 人类文档

- 造价白话：`给造价人员-怎么用.md`  
- 网页：`docs/index.html`、`docs/platform-select.html`  
