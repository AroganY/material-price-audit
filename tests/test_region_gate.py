"""Phase 6：地区门禁与解析优先级。"""

from __future__ import annotations

from material_price_audit.region_gate import (
    apply_gate_to_bucket,
    classify_region_match,
    decide_region_gate,
    parse_region_text,
    resolve_target_region,
)
from material_price_audit.region_models import RegionEvidence, RegionTarget


def test_parse_chengdu_chongqing():
    cd = parse_region_text("成都市", source="task")
    cq = parse_region_text("重庆市", source="task")
    assert cd.city_code == "510100"
    assert cq.city_code == "500100"
    assert cd.code_key != cq.code_key


def test_priority_excel_over_task():
    t = resolve_target_region(
        item_region={"city": "成都市", "city_code": "510100", "source": "excel_row"},
        task_region={"city": "重庆市", "city_code": "500100"},
        user_default={"city": "北京市", "city_code": "110100"},
        strategy="strict_city",
    )
    assert t.city_code == "510100"
    assert t.source == "excel_row"


def test_priority_task_over_default():
    t = resolve_target_region(
        item_region={},
        task_region={"city": "杭州市", "city_code": "330100"},
        user_default={"city": "南京市", "city_code": "320100"},
    )
    assert "杭州" in t.city or t.city_code == "330100"


def test_chengdu_request_chongqing_price_conflict():
    req = RegionTarget(
        province="四川省",
        city="成都市",
        city_code="510100",
        strategy="strict_city",
    )
    ev = classify_region_match(
        req,
        source_price_region="重庆市",
        supplier_region="",
    )
    assert ev.region_match == "conflict"
    g = decide_region_gate(req, ev)
    assert g.action == "reject"
    b, _ = apply_gate_to_bucket("formal", g)
    assert b == "discard"


def test_province_strict_vs_allow():
    req = RegionTarget(
        province="四川省",
        city="成都市",
        city_code="510100",
        strategy="strict_city",
    )
    ev = classify_region_match(req, source_price_region="四川省")
    assert ev.region_match == "province"
    g1 = decide_region_gate(req, ev, strategy="strict_city")
    assert g1.action == "review"
    g2 = decide_region_gate(req, ev, strategy="allow_province")
    assert g2.action == "allow_formal"


def test_guangdong_supplier_chengdu_price_ok():
    """广东供应商 + 成都适用价 → 不得因供应商地拒绝。"""
    req = RegionTarget(city="成都市", city_code="510100", province="四川省")
    ev = classify_region_match(
        req,
        source_price_region="成都市",
        supplier_region="广东省广州市",
    )
    assert ev.region_match == "exact"
    # 供应商地不参与 conflict
    assert "广东" in ev.supplier_region
    g = decide_region_gate(req, ev)
    assert g.action == "allow_formal"


def test_national_is_market_ref():
    req = RegionTarget(city="成都市", city_code="510100")
    ev = classify_region_match(req, source_price_region="全国统一价")
    assert ev.region_match == "national"
    g = decide_region_gate(req, ev)
    assert g.action == "market_ref"
    b, pref = apply_gate_to_bucket("formal", g)
    assert b == "candidate" or b == "market_ref" or "参考" in pref or b == "candidate"


def test_unknown_passthrough_when_not_required():
    req = RegionTarget(city="成都市", city_code="510100")
    ev = RegionEvidence(
        requested_region="成都市",
        region_match="unknown",
    )
    g = decide_region_gate(req, ev, region_required=False)
    assert g.action == "passthrough"
    g2 = decide_region_gate(req, ev, region_required=True)
    assert g2.action == "review"


def test_unspecified_target_passthrough():
    req = RegionTarget.unspecified()
    ev = classify_region_match(req, source_price_region="重庆市")
    g = decide_region_gate(req, ev, region_required=False)
    assert g.action == "passthrough"
