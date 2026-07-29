# Material Price Audit · 材料询价助手

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-22C55E.svg)](./LICENSE)

把造价员逐条执行的“搜索材料 → 核对名称规格 → 抄价 → 写 Excel”，变成一个浏览器向导。

上传无需固定模板的询价 Excel，选择平台并登录后，程序会逐条搜索并导出多价比价结果。名称或任一关键规格对不上时，正式价格保持为空；找到相似结果但证据不足时，只进入“规格待核”，不会冒充合格报价。

## 当前能力

- 自动识别名称、规格、品牌、单位、数量、报送价等常见列，不要求固定模板。
- 只打开用户勾选的平台，勾选顺序就是询价优先级。
- 每条材料可收集 1～10 个报价；当前站不足会继续下一个站。
- 严格校验型号、尺寸、电压、功率、色温、防护等级、端口、联机/脱机和计价单位。
- 输出来源价格、税口径、不含税参考价、商品标题、供应商和可点击证据链接。
- 不完全匹配的候选单列待核；所有平台都无合格结果时标记“没查到”。
- 生成 `rfq.xlsx`，用于继续向供应商询价。
- 可选 LLM 只处理陌生表头和语义灰区，不能越过数值、型号、尺寸等硬规则。

## 快速开始

要求 Python 3.10+，建议使用 Chrome 或 Chromium。

```bash
git clone https://github.com/AroganY/material-price-audit.git
cd material-price-audit
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
python -m playwright install chromium
python -m material_price_audit serve
```

浏览器会自动打开 `http://127.0.0.1:8765/`。之后只在页面操作：

1. 勾选平台并调整优先级。
2. 设置每条材料需要几个价格。
3. 上传或选择 Excel，确认识表结果。
4. 分站打开登录窗口并完成校验。
5. 开始询价，完成后下载结果 Excel 和 RFQ。

不需要把表头改成固定模板，也不需要为每次任务拼一长串命令。

## 维护中的平台

| ID | 平台 | 专用适配 | 说明 |
|---|---|---:|---|
| `guangcai` | 广材网 | 是 | 会员市场价与厂家报价行 |
| `lingcai` | 领材网 | 是 | 市场价结果行；已处理双重 URL 解码 |
| `huixun` | 慧讯网 | 是 | SPA 产品库，使用页面搜索框 |
| `jd` | 京东 | 是 | 商品列表与限流检测 |
| `1688` | 1688 | 是 | GBK 搜索编码、详情价与风控检测 |

网站会改版，也可能要求验证码或限制访问。适配器遇到登录失效、验证码或访问频繁时会停止当前站，不会把异常页当成“0 条结果”连续刷新。平台细节见 [docs/PLATFORMS.md](./docs/PLATFORMS.md)。

## 匹配和收价规则

```text
Excel 材料行
  → 生成“原始名称优先”的搜索词
  → 按用户勾选顺序搜索平台
  → 从同一商品/厂家报价行提取名称、规格、价格和单位
  → strict_name_spec_match 校验全部硬规格
      ├─ 全部满足：写入正式价
      ├─ 无冲突但证据不足：只写入待核候选
      └─ 型号/数值/语义冲突：拒绝
  → 未凑满 K 个价格时继续下一平台
```

程序不会根据报送价“猜”一个市场价，也不会让 LLM 放行硬规格冲突。详情见 [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)。

## 输出

运行数据默认写入：

| 文件 | 内容 |
|---|---|
| `data/output/result.xlsx` | Sheet `询价比价结果`，包含价 1～K、税口径、来源、供应商和链接 |
| `data/output/rfq.xlsx` | 未凑满 K 个合格价的材料 |
| `data/output/evidence.json` | 每次搜索、拒绝原因和证据的机器可读记录 |
| `data/user/settings.json` | 用户勾选的平台和每条目标价数 |

真实询价表、结果、登录 Cookie 和用户设置均已在 `.gitignore` 中排除。

## 可选配置

程序无需 `config.yaml` 也能运行。需要自定义税率、浏览器或 LLM 时：

```bash
cp config.example.yaml config.yaml
```

LLM 默认关闭。开启前配置 `OPENAI_API_KEY`，也可通过兼容 OpenAI Chat Completions 的 `api_base` 接入其它服务。启用后，表头预览或待核材料证据会发送到该接口；涉及保密项目时请保持关闭。即使 LLM 不可用，规则识表和严格匹配仍能运行。

## 开发

```bash
pip install -e ".[dev]"
pytest
python -m material_price_audit parse --no-llm
```

核心模块：

- `schema_map.py` / `normalize.py`：识表与标准材料行
- `matching.py`：严格名称规格判定
- `platforms.py`：平台注册和搜索适配器
- `inquiry.py`：跨平台瀑布询价编排
- `export_quotes.py`：结果与 RFQ 导出
- `webapp/`：浏览器向导、登录面板和后台任务

贡献指南见 [CONTRIBUTING.md](./CONTRIBUTING.md)，安全问题见 [SECURITY.md](./SECURITY.md)。

## 合规与边界

- 只使用你有权访问的账号，并遵守平台服务协议、robots 规则和当地法律。
- 保持正常访问节奏，不绕过验证码、付费权限或其它访问控制。
- 输出是询价与审核底稿，不构成最终造价咨询执业意见。
- 零售价、批发价、地区价、税费和运费口径不同，正式采用前必须点击证据链接复核。

本项目采用 [MIT License](./LICENSE)。
