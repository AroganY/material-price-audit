# AGENTS.md — AI 自动化协议（少问话、多执行）

> **不要一步一步碎问。**  
> 用户要的是：环境自检 → 一次选好平台 → 自动登录等待 → 瀑布匹配抓价 → 出结果。

---

## 0. 默认行为（必须）

用户一说「核价 / 询价 / 跑工具」：

```bash
cd <package-root>   # material-price-audit 目录

# 1) 环境：只快检 import；缺包才 --auto-install。禁止升级 Python / 禁止反复 check
#    日常 run 已内置轻量检查，不要先刷一遍 check

# 2) 平台：必须用户选。禁止默认全站登录。
#    问一句：「要比哪几个站？」→ 例如 guangcai,jd
#    或：select-platforms / docs/platform-select.html

# 3) 只跑用户选的站（询价表 data/input/ 任意文件名）
python3 -m material_price_audit run --platforms guangcai,jd --limit 8
```

**只在这些情况停下来问用户：**

1. **缺包装失败** → 给安装命令（只装缺失，不升级 Python）  
2. **没有询价单** → 「把任意文件名 Excel 放到 data/input/」  
3. **没选平台** → 「要比哪些网站？例如 guangcai,jd / huixun / lingcai」  
4. **登录** → 程序每个站只 `goto` 一次，然后**被动等**（不刷新页面）。  
   用户说「登完了」后 Agent 执行：
   ```bash
   touch data/output/LOGIN_CONTINUE
   ```
   或分两步：`login` 完成后再 `run --skip-login`。

不要默认全站挨个登录；不要循环刷新登录页；不要固定 sleep 90 秒。

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
| 询价单 | `data/input/*.xlsx`（任意名；表头含「报送不含税单价」优先） |
| 结果 | `data/output/result.xlsx` |
| 证据 | `data/output/evidence.json` |
| RFQ | `data/output/rfq.xlsx` |
| 勾选平台文件 | `data/output/platforms.selected` |
| 登录态 | `.browser-profile/` |

用户表不在 `data/input/` 时，**一次**用 `--input` 指定完整路径，不要反复确认。

---

## 6. 对用户话术（短）

### 开场

```text
我来自动跑核价：先检查环境，再用广材/慧讯(iccchina)/领材(hylcw)（可改）按顺序匹配详情价。
请把询价表丢进 data/input/（文件名随意）；浏览器弹出时登录一下各网站即可。
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
