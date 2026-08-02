"""名称空格折叠、地名剥离、名称优先检索。"""

from __future__ import annotations

from material_price_audit.matching import (
    collapse_cjk_spaces,
    name_core_words,
    name_search_core,
    normalize_material_name,
    soft_product_name_equivalent,
    strict_name_spec_match,
    strip_geo_noise,
)
from material_price_audit.models import CanonicalItem
from material_price_audit.name_aliases import normalize_name_key
from material_price_audit.normalize import (
    build_cost_site_queries,
    normalize_search_query,
)


def test_collapse_cjk_internal_spaces():
    assert collapse_cjk_spaces("薄 壁 不 锈 钢 管") == "薄壁不锈钢管"
    assert collapse_cjk_spaces("薄 壁 不锈钢管 DN100") == "薄壁不锈钢管 DN100"
    assert "薄壁不锈钢管" in normalize_material_name("薄 壁 不 锈 钢 管")


def test_name_search_core_with_spaces():
    core = name_search_core("薄 壁 不 锈 钢 管")
    assert "不锈钢" in core or core == "薄壁不锈钢管"
    words = name_core_words("薄 壁 不 锈 钢 管")
    assert words
    assert any("管" in w or "不锈钢" in w for w in words)


def test_spaced_name_matches_compact_title():
    item = CanonicalItem(
        id="1", sheet="s", row=1, name="薄 壁 不 锈 钢 管", spec="DN100"
    )
    mr = strict_name_spec_match(
        item,
        "薄壁不锈钢管 DN100",
        "规格 DN100 单价 63",
    )
    assert mr.ok is True or "名称未命中" not in (mr.detail or "")


def test_soft_equiv_ignores_spaces():
    assert soft_product_name_equivalent("地 埋 灯", "埋地灯")


def test_strip_geo_from_name_and_query():
    assert "成都" not in strip_geo_noise("成都市薄壁不锈钢管")
    assert "信息价" not in strip_geo_noise("薄壁不锈钢管 信息价")
    q = normalize_search_query("成都 薄 壁 不锈钢管")
    assert "成都" not in q
    assert "不锈钢" in q or "薄壁" in q


def test_cost_queries_no_geo_name_first():
    qs = build_cost_site_queries("成都市 薄壁不锈钢管", "DN100", "", [])
    assert qs
    assert "DN" not in qs[0].upper()
    blob = " ".join(qs)
    assert "成都" not in blob
    assert "信息价" not in blob
    # 禁止「不锈钢」被裁成「不锈管」
    assert "不锈管" not in blob


def test_score_title_partial_cn():
    from material_price_audit.scraper import score_title

    # 询价全名 vs 标题短名：人能看见的结果程序也要能进候选
    assert score_title("不锈钢管 DN100", ["薄壁不锈钢管"]) >= 1
    assert score_title("薄壁不锈钢管", ["薄壁不锈钢管"]) >= 1


def test_normalize_name_key_spaces_and_geo():
    a = normalize_name_key("薄 壁 不锈钢管")
    b = normalize_name_key("薄壁不锈钢管")
    assert a == b
    c = normalize_name_key("成都薄壁不锈钢管")
    assert "成都" not in c
    assert "不锈钢" in c or "薄壁" in c
