# Web 向导完整教程

本文只讲 **浏览器向导**（推荐用法）。截图来自 Playwright 对 `http://127.0.0.1:8765/` 的真实抓取，见 [images/](./images/)。

## 1. 安装与启动

```bash
cd material-price-audit
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m playwright install chromium

python -m material_price_audit serve --host 127.0.0.1 --port 8765
```

打开：http://127.0.0.1:8765/

> 不要把 `--host` 改成 `0.0.0.0` 暴露到公网（无登录认证）。

## 2. 六步流程

### 步骤 ① 平台与模式

![01](./images/01-step1-platforms.png)

1. 勾选平台（顺序 = 搜不到时的切换优先级）。  
2. **每条材料询几个价**：建议试跑用 `1～2`，全量再用 `2～3`。  
3. **匹配模式**：  
   - **实用·候选工作台（推荐）**：名字对上就保留候选价+链接  
   - **严格**：硬规格全过才收正式价  
   - **宽松**：名称像就进候选  
4. （可选）AI：配置 Base / Key / 用途；**默认可关**。  
5. 点 **保存并下一步**。

### 步骤 ② 询价表

![02](./images/02-step2-upload.png)

- 拖入 `.xlsx` / `.xlsm`，或选择 `data/input/` 里已有文件。  
- 表头可含「名称 / 规格型号 / 报送不含税单价」等常见列，无需固定模板。  
- 点 **识别表结构**。

### 步骤 ③ 识表确认

![03](./images/03-step3-schema.png)

- 抽查名称、规格、报送价是否串列。  
- 可用 Sheet 页签切换专业。  
- 确认后进入登录面板。

### 步骤 ④ 登录

![04](./images/04-step4-login.png)

对每个站：

1. **打开登录页**（弹出 Chromium，profile 在 `.browser-profile/`）  
2. 在浏览器中完成登录 / 验证码  
3. 回到向导点 **本站已登录，校验**  

全部通过后再询价。慧讯关窗重开若出现「一键登录 / 账号已被登录」，程序会尝试自动点「继续登录」。

### 步骤 ⑤ 执行询价

![05](./images/05-step5-run.png)

**询价范围：**

![范围](./images/05b-scope-box.png)

| 选项 | 含义 |
|------|------|
| 全部材料 | 识表全部行 |
| 前 N 条 | 按识表顺序试跑 |
| 按 Sheet | 只跑某些专业表 |
| 勾选材料 | 逐条勾选 |

**过程控制：**

| 按钮 | 行为 |
|------|------|
| 暂停 | 当前材料结束后暂停（计时冻结） |
| 继续 | 从暂停处接着跑 |
| 停止询价 | 当前材料结束后停止，保存已完成结果并进入结果页 |
| 继续询价（断点） | 跳过已有合格价，只补没查到/候选 |

**AI 与用量：**

![用量](./images/05c-usage-panel.png)

- 可随时开关「询价中使用大模型」  
- 显示 **总 Token / 输入 / 输出** 与请求次数  

### 步骤 ⑥ 结果

![06](./images/06-step6-results.png)

| 颜色 | 状态 | 建议 |
|------|------|------|
| 绿 | 合格价 | 可作审价参考 |
| 黄 | 候选待核 | 点链接核对规格后采用 |
| 灰 | 没查到 | 进 RFQ 或换站再跑 |

下载：

- **结果 Excel** → `data/output/result.xlsx`  
- **RFQ** → `data/output/rfq.xlsx`  

## 3. 推荐试跑策略

1. 先只开 **1 个你有会员的站**（如广材）。  
2. 匹配模式用 **实用**。  
3. 范围选 **前 5～10 条** 或单 Sheet。  
4. 确认登录与匹配正常后，再扩大范围 / 加站。  

## 4. 常见问题

**Q: 页面上能看到商品，为什么仍是候选/没查到？**  
A: 名称对上但截面/型号与询价表不一致时，实用模式会进「黄·候选」而不是直接当合格价。点链接人工确认即可。

**Q: 停止后秒数还在涨？**  
A: 新版本会在停止/完成时冻结计时；请刷新页面。

**Q: 隐私会不会被提交到 Git？**  
A: `data/input`、`data/output`、`data/user`、`.browser-profile`、`config.yaml` 均在 `.gitignore`。发 PR 前用 `git status` 再确认一次。

## 5. 重截文档图

```bash
# 终端 1
python -m material_price_audit serve --host 127.0.0.1 --port 8765

# 终端 2（建议先用演示表、清空隐私输出后再截）
python scripts/capture_screenshots.py
```
