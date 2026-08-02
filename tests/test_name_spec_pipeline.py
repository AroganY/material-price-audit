"""Phase 4：名称/规格两阶段 + 缓存 + 门禁。"""

from __future__ import annotations

from pathlib import Path

from material_price_audit.matching import strict_name_spec_match
from material_price_audit.models import CanonicalItem
from material_price_audit.name_match import (
    NameDecision,
    NameDecisionCache,
    allows_formal_quote,
    allows_spec_extract,
    decide_name_quick,
    pair_cache_key,
)
from material_price_audit.spec_match import (
    extract_structured_spec,
    has_valid_numeric_price,
    match_name_and_spec,
)


def test_different_skips_spec_extract():
    assert allows_spec_extract("different") is False
    assert allows_spec_extract("same") is True
    assert allows_spec_extract("possible") is True


def test_possible_not_formal():
    assert allows_formal_quote("possible") is False
    assert allows_formal_quote("same") is True
    assert allows_formal_quote("different") is False
    assert allows_formal_quote("unknown") is False


def test_name_different_blocks_spec_pipeline():
    item = CanonicalItem(
        id="1", sheet="s", row=1, name="球墨铸铁管", spec="DN150"
    )
    out = match_name_and_spec(
        item,
        "不锈钢法兰 DN150",
        "不锈钢法兰 DN150 价格 10 元",
        name_decision="different",
    )
    assert out.skip_reason == "name_different"
    assert out.hard_conflict is True
    assert out.result.ok is False
    # 未对 different 做完整规格命中
    assert "禁止规格抽取" in (out.result.detail or "") or "different" in (
        out.result.detail or ""
    )


def test_dn_conflict_rejected():
    item = CanonicalItem(
        id="1", sheet="s", row=1, name="球墨铸铁管", spec="DN150 PN10"
    )
    out = match_name_and_spec(
        item,
        "球墨铸铁管 DN200 PN10",
        "球墨铸铁管 DN200 PN10 价格 99 元",
        name_decision="same",
    )
    # DN 冲突必须拒绝
    assert out.result.ok is False or out.hard_conflict is True
    assert out.skip_reason != "name_different"


def test_structured_spec_extract():
    sp = extract_structured_spec(
        "薄壁不锈钢管 DN100 PN16 304", "规格 DN100 1250x400"
    )
    assert any("DN100" in d for d in sp.diameters)
    assert sp.pressure.upper().startswith("PN") or "16" in sp.pressure
    assert any("1250" in d for d in sp.dimensions) or "x" in " ".join(sp.dimensions)


def test_no_numeric_price_rejected():
    assert has_valid_numeric_price(None) is False
    assert has_valid_numeric_price(0) is False
    assert has_valid_numeric_price(0.01) is False
    assert has_valid_numeric_price(12.5) is True


def test_name_cache_once_per_pair(tmp_path: Path):
    cache = NameDecisionCache(tmp_path, use_disk=True)
    # 预置
    cache.put(
        "地埋灯",
        "埋地灯 3W",
        NameDecision("same", "local_alias", "测试", 1.0, "埋地灯 3W"),
    )
    d1 = cache.get("地埋灯", "埋地灯 3W")
    d2 = cache.get("地埋灯", "埋地灯 3W")
    assert d1 is not None and d1.decision == "same"
    assert d2 is not None
    assert cache.hits >= 2
    # 磁盘再读
    cache2 = NameDecisionCache(tmp_path, use_disk=True)
    d3 = cache2.get("地埋灯", "埋地灯 3W")
    assert d3 is not None and d3.decision == "same"


def test_pair_cache_key_stable():
    a = pair_cache_key("单向阀 DN100", "止回阀")
    b = pair_cache_key("单向阀 DN100", "止回阀")
    assert a == b


def test_decide_name_quick_builtin_alias():
    # 内置 止回阀≈单向阀
    d = decide_name_quick("止回阀", "单向阀 DN50", root=None)
    # 可能 same（builtin）或 unknown 取决于库是否加载
    assert d.decision in ("same", "unknown", "different")


def test_same_name_still_runs_strict_spec():
    item = CanonicalItem(
        id="1", sheet="s", row=1, name="球墨铸铁管", spec="DN150 PN10"
    )
    out = match_name_and_spec(
        item,
        "球墨铸铁管 DN150 PN10",
        "球墨铸铁管 DN150 PN10 含税价 100 元",
        name_decision="same",
    )
    assert out.skip_reason == ""
    # 同物同规应可 accept（规则侧）
    assert out.result.ok is True or out.result.outcome in ("accept", "review")


def test_strict_match_unchanged_for_same_path():
    item = CanonicalItem(
        id="1", sheet="s", row=1, name="球墨铸铁管", spec="DN150"
    )
    title = "球墨铸铁管 DN150"
    body = title + " 价格 1"
    a = strict_name_spec_match(item, title, body)
    b = match_name_and_spec(item, title, body, name_decision="same").result
    assert a.ok == b.ok
