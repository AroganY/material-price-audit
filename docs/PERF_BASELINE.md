# 性能基线（Phase 0）

## 开启方式

```bash
export MPA_PERF=1
python3 -m material_price_audit serve
# 或在代码中：
# from material_price_audit import perf
# with perf.scoped_enable():
#     run_inquiry(...)
```

默认 **关闭**，对询价结果零影响。

## 埋点位置（inquiry）

| 指标 | 位置 |
|------|------|
| `query_count` / `search_ms` | 每次 `search_on_platform` |
| `candidate_count` | 列表返回条数 |
| `detail_open_count` / `detail_ms` | 每次 `open_detail`（非 inline） |
| `spec_match_ms` | `strict_name_spec_match` |
| `accepted` / `review` / `rejected` | `decide_quote_bucket` 结果 |

任务结束若开启 perf，会 emit `type=perf` 事件（不写 Excel）。

## 读取快照

```python
from material_price_audit.perf import snapshot
print(snapshot()["aggregate"])
```

## 后续 Phase

- Phase 3 后对比：同品名多 DN 的 `query_count` 应显著下降  
- Phase 5 后对比：墙钟 `run_total_ms` 与跨平台并行  

## 基线记录（人工填写）

| 场景 | 材料数 | 平台 | query_count | detail_open | run_total_ms | 日期 |
|------|--------|------|--------------|-------------|--------------|------|
| （待实跑） | | | | | | |
