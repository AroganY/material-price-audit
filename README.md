# material-price-audit · 材料询价核价工具

> **给造价审核用的「有依据核价」工具**  
> 施工单位报多少，就以报送价为上限；网上能查到的，自动给更低的审定参考价，并留下可点开的依据链接。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org/)

---

## 先看哪个文档？

| 你是谁 | 打开这个 |
|--------|----------|
| **造价员 / 审核员（推荐先看）** | **[给造价人员-怎么用.md](./给造价人员-怎么用.md)** |
| **想用网页跟着点** | **[docs/index.html](./docs/index.html)**（双击用浏览器打开） |
| 信息中心 / 开发 | [docs/USAGE.md](./docs/USAGE.md) |
| 开源说明与合规 | [docs/OPEN_SOURCE.md](./docs/OPEN_SOURCE.md) |
| 让 AI（Grok/Codex）带着做 | [AGENTS.md](./AGENTS.md) |

---

## 1. 解决什么问题？

造价审核里，安装/装饰材料经常遇到：

- 施工单位 **报送不含税单价** 偏高  
- 自己百度、电商搜，**型号对不齐、截图乱、说不清依据**  
- 经验「砍一刀」对方不服，**没有可追溯链接**

本工具做的事很具体：

```text
询价 Excel（含报送价）
    ↓
登录广材网 / 慧讯网 / 领材网（等）自动查价
    ↓
输出：审定不含税单价（≤报送）+ 可点击依据链接
    ↓
查不到的 → 待询价表，发给供应商盖章
```

### 和「信息价 / 定额」的区别

| 概念 | 干什么 |
|------|--------|
| 定额 / 信息价 | 计价依据、发布价、套用规则 |
| **本工具** | **认质认价 / 材料询价审核底稿**：针对表里每一条材料找公开挂牌或信息站价，形成可核对的审定建议 |

---

## 2. 审价规则（三句话）

1. **有依据才填审定**——查不到同型号公开价，**宁可不填**，不编造。  
2. **审定 ≤ 报送不含税单价**——施工单位报的是上限。  
3. **含税挂牌会折成不含税**（默认 ÷1.13，可配置），再和报送价取低。

---

## 3. 造价人怎么用（一键）

> 白话版：**[给造价人员-怎么用.md](./给造价人员-怎么用.md)**  
> 勾选平台页：**[docs/platform-select.html](./docs/platform-select.html)**

| 步骤 | 做什么 |
|------|--------|
| ① | 询价表丢进 `data/input/`（**任意文件名**，如 `安装专业询价材料设备.xlsx`） |
| ② | （可选）打开 `docs/platform-select.html` 勾选平台顺序 |
| ③ | 执行下面 **一条命令**；浏览器弹出时登录即可 |
| ④ | 打开 `data/output/result.xlsx` 核对 |

```bash
# 环境自检+自动装依赖+登录等待+瀑布抓价+导出RFQ
python -m material_price_audit run \
  --platforms guangcai,huixun,lingcai,jd,1688 \
  --auto-install \
  --login-wait 90

# 第一次建议先试 8 条
python -m material_price_audit run --platforms guangcai,huixun,lingcai --limit 8 --auto-install --login-wait 90
```

**匹配规则：** 平台 A 的**详情页规格/型号匹配**才用 A；否则自动 B→C→…；全失败则不填审定，进 `rfq.xlsx`。

---

## 4. 结果文件怎么用？

| 文件 | 造价怎么用 |
|------|------------|
| `data/output/result.xlsx` | 主成果。先看 **「实抓汇总」**，点开详情链接存档 |
| `data/output/rfq.xlsx` | 没查到的材料，发给厂家/经销商盖章回价 |
| `data/output/evidence.json` | 技术底稿，一般不用打开 |

和对方沟通时可说：

> 审定价不高于贵司报送价；依据见结果表中的链接。  
> 无公开价材料请按待询价表书面报价。

---

## 5. 支持哪些网站？（URL 已核对）

详见 **[docs/PLATFORMS.md](./docs/PLATFORMS.md)**。

| ID | 名称 | 官网 / 登录 |
|----|------|-------------|
| `guangcai` | **广材网** | https://www.gldjc.com/ · 登录 https://www.gldjc.com/login |
| `huixun` | **慧讯网**（RCC 瑞达恒） | 登录 https://services.iccchina.com/login · **不是**广材 |
| `lingcai` | **领材网** | 登录 https://www.hylcw.cn/userInfo/index.html · **不是**广材 |
| `jcnet` | 建材在线 | https://www.jc.net.cn/ |
| `jd` / `1688` 等 | 电商 | 各自官网 |

默认启用：`guangcai,huixun,lingcai,jd,1688`。

---

## 6. 不想敲命令？让 AI 带你

复制给 Grok / Codex / Claude：

```text
按 AGENTS.md 自动跑 material-price-audit：
1) check --auto-install
2) 询价表丢进 data/input/ 即可（任意文件名，不必叫 inquiry.xlsx；没有就说一声）
3) run --platforms guangcai,huixun,lingcai,jd,1688 --auto-install --login-wait 90
不要一步步碎问。瀑布匹配：详情规格对上才用该平台，否则自动下一站。
```

```bash
bash examples/agent_bootstrap.sh guangcai,huixun,lingcai,jd,1688
# 试跑 8 条：
bash examples/agent_bootstrap.sh guangcai,huixun,lingcai 8
```

---

## 7. 第一次安装（信息中心代劳）

```bash
cd material-price-audit
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python -m material_price_audit check
python -m material_price_audit init --platforms guangcai,huixun,lingcai
```

网页引导：浏览器打开 `docs/index.html`。

---

## 8. 开源说明

- 协议：**MIT**（[LICENSE](./LICENSE)）  
- 介绍与合规：[docs/OPEN_SOURCE.md](./docs/OPEN_SOURCE.md)  
- **请勿上传**：登录目录 `.browser-profile/`、真实项目询价表与核价结果  

---

## 一句话总结

**把「报送材料价」变成「带链接的审定参考价」；查不到的，自动列出给供应商询价——专为造价审核底稿服务。**
