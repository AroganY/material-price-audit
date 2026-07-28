# 输入目录 Input

把**询价单 Excel** 放在这里即可，**文件名随意**：

```text
data/input/安装专业询价材料设备.xlsx   ✅
data/input/某某项目材料询价表.xlsx     ✅
data/input/inquiry.xlsx                ✅（也可以，但不强制）
```

工具会自动：

1. 扫描本目录所有 `.xlsx` / `.xlsm` / `.xls`
2. 优先选用**表头含「报送不含税单价」**的表
3. 多个候选时按表头匹配度 + 文件名关键词（询价/材料/…）排序
4. 也可用 `--input /完整路径/你的表.xlsx` 强制指定

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
# 不传 --input：自动识别 data/input/ 下的询价表
python -m material_price_audit run \
  --platforms guangcai,huixun,lingcai,jd,1688 \
  --auto-install --login-wait 90

# 或显式指定任意文件名
python -m material_price_audit scrape \
  --input "data/input/安装专业询价材料设备.xlsx" \
  --output data/output/result.xlsx \
  --evidence data/output/evidence.json \
  --profile .browser-profile \
  --limit 8
```
