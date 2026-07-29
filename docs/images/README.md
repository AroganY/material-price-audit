# 文档截图

本目录截图由 Playwright 对本地 `python -m material_price_audit serve`  
（`http://127.0.0.1:8765/`）**真实页面**抓取，非 AI 生成图。

| 文件 | 内容 |
|------|------|
| `01-step1-platforms.png` | ① 选择平台 / 匹配模式 / AI 配置 |
| `02-step2-upload.png` | ② 上传或选择询价 Excel |
| `03-step3-schema.png` | ③ 识表预览 |
| `04-step4-login.png` | ④ 分站登录校验 |
| `05-step5-run.png` | ⑤ 询价范围、暂停/停止、用量 |
| `05b-scope-box.png` | 询价范围面板特写 |
| `05c-usage-panel.png` | Token 用量面板特写 |
| `06-step6-results.png` | ⑥ 结果（绿/黄/灰） |

重新截取（需服务已启动）：

```bash
# 见仓库 scripts/capture_screenshots.py（若有）或：
python3 - <<'PY'
# 使用 Playwright 打开 http://127.0.0.1:8765/ 截取各步骤
PY
```

**注意：** 截图中可能含你本地上次任务的材料名/价。发版前请用**演示数据**重跑再截，或打码后再提交。
