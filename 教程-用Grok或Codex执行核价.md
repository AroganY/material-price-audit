# 小白教程：用 Grok / Codex 帮你执行「材料询价核价」

这份文档只讲一件事：  
**怎么让 AI（Grok 或 Codex）帮你跑核价工具**，你负责登录网页和检查结果。

---

## 0. 先搞懂分工（很重要）

| 谁 | 负责什么 |
|----|----------|
| **你** | 安装一次环境；在弹出的浏览器里登录京东/1688；最后点开结果检查 |
| **Grok / Codex** | 在项目目录执行命令、看报错、生成结果表、告诉你文件在哪 |
| **工具本身** | 按规则抓商品价格；**没证据不会乱填审定价** |

> 一句话：AI 帮你敲命令；**登录必须你自己完成**（安全需要）。

---

## 1. 工具在哪个文件夹

请认准这个目录（后面所有操作都在这里）：

```text
/Users/arogan/myMvp/AI搞定造价IDEA/AI询价/material-price-audit
```

用访达（Finder）打开也可以：

1. 打开访达  
2. 按 `Shift + Command + G`  
3. 粘贴上面的路径，回车  

你会看到大致结构：

```text
material-price-audit/
├── data/
│   ├── input/          ← 把询价单放这里
│   └── output/         ← 结果会出现在这里
├── material_price_audit/
├── README.md
└── 教程-用Grok或Codex执行核价.md   ← 就是本文
```

### 文件放哪、出来在哪（记死）

| 用途 | 路径（相对 material-price-audit） |
|------|-----------------------------------|
| **你的询价单（入参）** | `data/input/*.xlsx`（任意文件名，自动识别） |
| **核价结果（出参）** | `data/output/result.xlsx` |
| **证据记录** | `data/output/evidence.json` |
| **没抓到的待询价单** | `data/output/rfq.xlsx` |

把你的 Excel 询价单 **复制/另存为** 到：

```text
data/input/   # 任意 .xlsx，如 安装专业询价材料设备.xlsx
```

表里要有列：**报送不含税单价**（这是上限，审定不能超过它）。

---

## 2. 第一次使用：检查电脑环境（只需做一次）

打开「终端」（Terminal），复制粘贴下面整段，回车：

```bash
cd "/Users/arogan/myMvp/AI搞定造价IDEA/AI询价/material-price-audit"

python3 -m pip install -r requirements.txt
python3 -m playwright install chromium

python3 -m material_price_audit check
```

### 怎样算成功？

终端里出现类似：

```text
状态   : OK — 可运行 scrape / login
```

### 如果失败？

- 提示缺少 `playwright` → 再执行一次上面的 `pip install` 和 `playwright install`
- 提示 Python 版本太旧 → 需要 Python 3.10 或以上  
- 把终端红色报错 **整段复制给 Grok/Codex**，让它帮你看

---

## 3. 用 Grok 帮你执行（推荐）

### 3.1 打开方式

1. 打开 **Grok Build / Grok 编程助手**（你平时让 Grok 改代码的那个）  
2. **工作区 / 项目目录** 选到：

   ```text
   /Users/arogan/myMvp/AI搞定造价IDEA/AI询价
   ```

   或直接打开：

   ```text
   .../AI询价/material-price-audit
   ```

3. 在对话框里 **复制粘贴下面「提示词」** 即可。

### 3.2 给 Grok 的提示词（直接复制）

#### A. 第一次：只说这一句（推荐）

```text
请按 material-price-audit 的 Agent 协议执行。

工作目录：
/Users/arogan/myMvp/AI搞定造价IDEA/AI询价/material-price-audit

1. 先运行：python -m material_price_audit init
2. 阅读终端 AGENT_GUIDE 和 data/output/AGENT_NEXT.md
3. 按里面的 questions 一项一项问我，不要一次问完
4. 我回答后执行 next_command，每步结束后再 guide
5. 遵守：无证据不填审定；平台由我指定；先试跑 8 条
```

（可选）你已经想好平台时：

```text
请 init 并启用平台 jd,1688,zkh，然后引导我放询价单和登录。
```

#### B. 选平台并登录（会弹出浏览器，你自己登录）

平台**不限京东/阿里**。先让 AI 列出，再指定你要的：

```text
请在 material-price-audit 目录执行：

python -m material_price_audit platforms

然后用我指定的平台登录（示例可改）：
python -m material_price_audit login \
  --profile .browser-profile \
  --platforms jd,1688,zkh

注意：会按平台依次弹出页面。
请在每个平台提示时暂停，等我登录完成后再继续。
可用平台包括：jd, 1688, taobao, tmall, zkh, suning, mysteel，以及 config 里自定义的站。
```

> **你要做的：**  
> 浏览器弹出来 → 按提示登录**你指定的每一个网站** → 回车继续（或告诉 AI「这个登好了」）。

#### C. 试跑 8 条（强烈建议先做这个）

```text
询价单已放在：
material-price-audit/data/input/inquiry.xlsx

我要使用的平台：jd,1688,zkh
（可改成 taobao,tmall 等）

请执行试跑（只要 8 条），每个材料在上述平台交叉搜索，取匹配最好且更低的价：

cd "/Users/arogan/myMvp/AI搞定造价IDEA/AI询价/material-price-audit"

python -m material_price_audit scrape \
  --input  data/input/inquiry.xlsx \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile \
  --platforms jd,1688,zkh \
  --limit 8

规则：
- 没有商品证据不要编造审定单价
- 跑完告诉我：verified 命中几条、用了哪些平台、结果文件路径
- 若需要登录，提醒我去浏览器操作
```

#### D. 试跑成功后：全量抓取

```text
试跑已经成功。请全量执行 scrape（不要 --limit）：

python -m material_price_audit scrape \
  --input  data/input/inquiry.xlsx \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile

然后导出未命中项：

python -m material_price_audit rfq \
  --input data/input/inquiry.xlsx \
  --evidence data/output/evidence.json \
  --output data/output/rfq.xlsx

最后用中文汇总：命中数、结果路径、我接下来要人工检查什么。
```

#### E. 只看进度

```text
请在 material-price-audit 目录执行 status，并解释给我听：

python -m material_price_audit status \
  --input data/input/inquiry.xlsx \
  --evidence data/output/evidence.json \
  --output data/output/result.xlsx
```

### 3.3 Grok 技能（如果菜单里有）

若 Grok 支持 Skills，可输入：

```text
/install-material-pricing
```

或直接说：

```text
按 install-material-pricing 技能，帮我做材料询价核价，先 check 再试跑 8 条。
```

---

## 4. 用 Codex 帮你执行

### 4.1 打开方式

1. 打开 **Codex**（CLI 或 VS Code 插件均可）  
2. 把当前目录切到工具目录，例如 CLI：

```bash
cd "/Users/arogan/myMvp/AI搞定造价IDEA/AI询价/material-price-audit"
```

3. 启动 Codex 后，把下面提示词贴进去。

### 4.2 给 Codex 的提示词（直接复制）

#### 一键说明 + 试跑

```text
You are helping me run the open-source tool in this folder: material-price-audit.

Rules (must follow):
1. Accuracy first: never invent audit prices without verified product evidence.
2. Always use explicit paths for --input --output --evidence --profile.
3. Run `python -m material_price_audit check` first; if env fails, show install commands and stop.
4. Prefer --limit 8 smoke test before full scrape.
5. I will manually log in to JD/1688 when the browser opens.

Do this now:
1. check env
2. status with:
   --input data/input/inquiry.xlsx
   --evidence data/output/evidence.json
   --output data/output/result.xlsx
3. Ask me whether to run login, then scrape --limit 8
4. Report verified count and output file paths in Chinese
```

#### 只跑全量（你已登录成功后）

```text
In material-price-audit folder, run full scrape then rfq:

python -m material_price_audit scrape \
  --input data/input/inquiry.xlsx \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile

python -m material_price_audit rfq \
  --input data/input/inquiry.xlsx \
  --evidence data/output/evidence.json \
  --output data/output/rfq.xlsx

Do not fabricate prices. Summarize results in Chinese.
```

### 4.3 Codex CLI 自己敲命令也可以

不懂 AI 时，你也可以不用 AI，自己在终端执行第 2、6 节命令——效果一样。

---

## 5. 完整小白流程（照着勾）

把下面当清单打勾：

- [ ] **1.** 询价单放到 `data/input/`（任意文件名，不必叫 inquiry.xlsx）  
- [ ] **2.** 终端执行 `check`，状态为 OK  
- [ ] **3.** 运行 `platforms` 看可选网站，决定 `--platforms`（如 jd,1688,zkh）  
- [ ] **4.** 让 Grok/Codex 执行 `login --platforms ...`，你在浏览器登录**指定的每一个站**  
- [ ] **5.** 执行 `scrape --platforms ... --limit 8` 试跑  
- [ ] **5.** 打开 `data/output/result.xlsx` → 看 Sheet「实抓汇总」  
- [ ] **6.** 点蓝色「打开详情页」，核对型号对不对、价格靠不靠谱  
- [ ] **7.** 没问题再全量 `scrape`（不加 limit）  
- [ ] **8.** 导出 `rfq.xlsx`，把没抓到的发给供应商询价  

---

## 6. 不用 AI 时：自己复制的命令

```bash
cd "/Users/arogan/myMvp/AI搞定造价IDEA/AI询价/material-price-audit"

# 检查
python3 -m material_price_audit check

# 登录（弹窗，你来登）
python3 -m material_price_audit login --profile .browser-profile

# 试跑 8 条
python3 -m material_price_audit scrape \
  --input  data/input/inquiry.xlsx \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile \
  --limit 8

# 全量
python3 -m material_price_audit scrape \
  --input  data/input/inquiry.xlsx \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile

# 没抓到的 → 供应商询价单
python3 -m material_price_audit rfq \
  --input data/input/inquiry.xlsx \
  --evidence data/output/evidence.json \
  --output data/output/rfq.xlsx
```

---

## 7. 结果怎么看

1. 打开：`data/output/result.xlsx`  
2. 先看工作表 **「实抓汇总」**  
3. 只有这里的行，才是 **抓到证据的审定价**  
4. 点 **「打开详情页」** → 浏览器打开商品页 → 核对：  
   - 型号是否一致  
   - 价格是否差不多  
5. 其他没出现在汇总里的材料 = **还没证据**，审定应为空或待询价，去看 `rfq.xlsx`

### 价格口径（不用深究，知道就行）

- 网上挂牌价多是 **含税**  
- 表里审定是 **不含税** ≈ 含税价 ÷ 1.13  
- 审定 **不会高于** 表里的「报送不含税单价」

---

## 8. 常见问题

### Q1：浏览器没弹出来？

- 看终端有没有报错  
- 再跑一次 `python -m material_price_audit check`  
- 让 AI 帮你看报错原文  

### Q2：一直要登录 / 抓到 0 条？

- 登录没成功，或登录过期  
- 再执行一次 `login`  
- 登录后马上 `scrape --limit 8`  

### Q3：为什么很多材料没有审定价？

这是 **正常且正确** 的：  
没有公开商品页、定制设备、人防专用等，工具 **宁可不填**，不会瞎编。  
用 `rfq.xlsx` 找供应商要盖章报价。

### Q4：Grok/Codex 填了一堆没有链接的价格？

告诉它：

```text
停止。必须遵守 material-price-audit 规则：
没有 verified 详情证据不得填写审定单价。
请只使用 scrape 产出的 result.xlsx / evidence.json。
```

### Q5：换一份新的询价单？

1. 新文件放到 `data/input/`（任意文件名即可，无需改名 inquiry.xlsx）  
2. 建议换输出文件名，避免和旧结果混：  

```bash
python3 -m material_price_audit scrape \
  --input  data/input/inquiry.xlsx \
  --output data/output/result_项目B.xlsx \
  --evidence data/output/evidence_项目B.json \
  --profile .browser-profile \
  --limit 8
```

---

## 9. 一句话记忆

1. **表放进** `data/input/`  
2. **让 AI 跑** `check → login → scrape --limit 8`  
3. **你登录浏览器**  
4. **打开** `data/output/result.xlsx` 核对详情页  
5. **没抓到的** 用 `rfq.xlsx` 去问供应商  

---

## 10. 相关文件

| 文件 | 说明 |
|------|------|
| `README.md` | 完整技术说明 |
| `config.example.yaml` | 税点、浏览器等配置示例 |
| `data/input/README.md` | 询价单格式要求 |
| 本文 | 给小白 + Grok/Codex 的操作教程 |

有问题：把 **终端完整报错** 复制给 Grok 或 Codex，并写上「按 material-price-audit 教程排查」。
