# 架构与维护边界

## 数据流

```text
Web 向导
  → schema_map.detect_workbook_schema
  → normalize.load_canonical_items
  → inquiry.run_inquiry
      → platforms.search_on_platform
      → scraper.open_detail（需要详情时）
      → matching.strict_name_spec_match
  → export_quotes.write_quote_result_workbook
  → result.xlsx / rfq.xlsx / evidence.json
```

## 模块职责

| 模块 | 唯一职责 |
|---|---|
| `runtime.py` | 项目路径、配置合并、证据持久化 |
| `settings_store.py` | 页面用户设置 |
| `schema_map.py` | 表头识别；规则优先，可选 LLM |
| `normalize.py` | Excel 行标准化、搜索词生成 |
| `matching.py` | 名称与全部硬规格匹配 |
| `platforms.py` | 平台注册、搜索状态与候选抽取 |
| `scraper.py` | 浏览器生命周期、登录等待、详情证据 |
| `inquiry.py` | 平台顺序、凑 K 价、换站和状态编排 |
| `export_quotes.py` | Excel 结果与 RFQ |
| `webapp/` | HTTP 向导、登录面板和后台任务 |

Web 层不能从 `cli.py` 导入业务逻辑。CLI 只保留 `serve`、`check` 和 `parse` 三个薄入口。

## 配置归属

- `data/user/settings.json` 是页面选择的平台、优先级和 K 值的唯一来源。
- `config.yaml` 只承载税率、导出模式、浏览器、可选 LLM 和自定义平台等高级配置。
- 已有页面设置时，`config.yaml` 的默认平台和 K 值不能静默覆盖用户选择。
- `runtime.py` 负责配置深度合并，Web 与 CLI 不各自维护一套默认值。

向导默认只监听 `127.0.0.1`。上传接口限制为 50 MB 的有效 OOXML 工作簿，并对静态文件和输入文件路径执行目录边界检查。

## 安全不变量

1. `strict_name_spec_match` 是正式收价的唯一门禁。
2. 数值、型号、尺寸、计价单位或明确语义冲突不能被 LLM 覆盖。
3. 价格必须绑定同一商品或同一厂家报价行。
4. 未登录、无会员、验证码和限流是独立状态，不能等同空结果。
5. 只搜索用户本次选择并通过登录校验的平台。

## 新平台贡献要求

- 为平台编码、DOM 结构和风控页增加离线回归测试。
- 列表页只能生成候选，不能绕过统一严格匹配。
- 若价格来自列表，必须同时保存该行完整规格证据和计价单位。
- 遇到验证码或访问频繁立即停止当前站，不自动绕过。
- 不把登录 Cookie、真实账号或真实报价样本加入测试仓库。
