# 文档截图

本目录截图由 Playwright 对本地 `python -m material_price_audit serve`  
（`http://127.0.0.1:8765/`）**真实页面**抓取，非 AI 生成图。

- ①～⑤ 与特写：`scripts/capture_screenshots.py`（内置演示数据，不读本机询价结果）
- ⑥ 结果工作台：可用真实询价后的界面截图（左侧材料 + 右侧规格/链接核对），便于展示「自行核对」

| 文件 | 内容 |
|------|------|
| `01-step1-platforms.png` | ① 选择平台 / 匹配模式 / AI 配置 |
| `02-step2-upload.png` | ② 上传或选择询价 Excel |
| `03-step3-schema.png` | ③ 识表预览（演示行） |
| `04-step4-login.png` | ④ 分站登录校验 |
| `05-step5-run.png` | ⑤ 询价范围、暂停/停止、**我已登录继续**、用量 |
| `05b-scope-box.png` | 询价范围面板特写 |
| `05c-usage-panel.png` | Token 用量面板特写 |
| `06-step6-results.png` | ⑥ 结果工作台（材料列表 + 规格/链接核对区） |

重新截取 ①～⑤（需服务已启动；默认**不会覆盖** `06`）：

```bash
python -m material_price_audit serve --host 127.0.0.1 --port 8765
python scripts/capture_screenshots.py
# 强制重截 ⑥ 演示页：MPA_SHOT_STEP6=1 python scripts/capture_screenshots.py
```

**注意：** 发版截图勿含 API Key、登录 Cookie 或敏感客户合同信息。
