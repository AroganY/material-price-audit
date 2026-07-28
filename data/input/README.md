# 输入目录 Input

把**询价单 Excel**放在这里，例如：

```text
data/input/inquiry.xlsx
```

## 表头要求（自动识别）

每个 Sheet 需包含（名称可微调，见 `config.example.yaml`）：

| 列 | 含义 |
|---|---|
| 材料名称 | 必填语义 |
| 规格、型号 | 推荐 |
| 单位 | 推荐 |
| 数量 | 推荐 |
| **报送不含税单价** | **必填**（审定上限） |
| 审定不含税单价 | 可空，工具回填 |
| 品牌 | 可选 |

## 运行示例

```bash
python -m material_price_audit scrape \
  --input data/input/inquiry.xlsx \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile \
  --limit 8
```
