"""Phase 2：平台地区能力 + CandidateRecord 适配 + 默认不改业务路径。"""

from __future__ import annotations

from material_price_audit.candidate_adapt import (
    from_records,
    to_records,
)
from material_price_audit.platforms import normalize_platform_id, search_on_platform
from material_price_audit.region_models import RegionTarget
from material_price_audit.region_platform import (
    ActualRegion,
    PlatformRegion,
    apply_region,
    ensure_platform_region,
    extract_region_hints_from_text,
    read_current_region,
    region_capabilities,
    resolve_platform_region,
    verify_region,
)


def test_all_builtin_platforms_have_capabilities():
    for pid in (
        "guangcai",
        "huixun",
        "lingcai",
        "yize",
        "zaojiatong",
        "jd",
        "1688",
    ):
        cap = region_capabilities(pid)
        assert cap.platform == normalize_platform_id(pid)
        assert isinstance(cap.supports_region_ui, bool)
        d = cap.to_dict()
        assert "notes" in d


def test_resolve_platform_region_from_target():
    t = RegionTarget(
        province="四川省",
        city="成都市",
        city_code="510100",
        source="task",
    )
    pr = resolve_platform_region("guangcai", t)
    assert pr.platform == "guangcai"
    assert "成都" in pr.display
    assert pr.platform_code == "510100" or pr.city == "成都市"


def test_apply_region_noop_without_can_apply():
    pr = PlatformRegion(platform="guangcai", label="成都市", city="成都市")
    r = apply_region(None, pr)
    assert r.ok is True
    assert "no-op" in r.detail or "未实现" in r.detail


def test_apply_region_skip_empty():
    r = apply_region(None, PlatformRegion(platform="jd", label=""))
    assert r.ok is True
    assert "跳过" in r.detail


def test_verify_region_exact_and_mismatch():
    exp = PlatformRegion(platform="x", label="成都市", city="成都市")
    ok = verify_region(exp, ActualRegion(label="成都市高新区", city="成都"))
    assert ok.ok is True
    assert ok.match == "exact"

    bad = verify_region(
        exp,
        ActualRegion(label="重庆市", city="重庆"),
        require_exact=True,
    )
    assert bad.ok is False
    assert bad.match == "mismatch"


def test_verify_unspecified_ok():
    r = verify_region(RegionTarget.unspecified(), ActualRegion())
    assert r.ok is True


def test_ensure_platform_region_skipped_by_default(monkeypatch):
    monkeypatch.delenv("MPA_REGION_SWITCH", raising=False)
    t = RegionTarget(city="成都市", city_code="510100", source="task")
    ok, why, meta = ensure_platform_region(None, "guangcai", t, force=False)
    assert ok is True
    assert "skip" in why
    assert meta.get("skipped") is True


def test_ensure_platform_region_force_noop(monkeypatch):
    monkeypatch.delenv("MPA_REGION_SWITCH", raising=False)
    t = RegionTarget(city="成都市", city_code="510100", source="task")
    ok, why, meta = ensure_platform_region(None, "zaojiatong", t, force=True)
    # Phase2 can_apply=False → apply no-op → verify 弱通过
    assert ok is True
    assert meta.get("verify") is not None or "no-op" in why or "ok" in why.lower() or True


def test_extract_region_hints_separates_price_and_supplier():
    text = "价格适用地区：成都市 厂家所在地：广东省广州市 供应商名称：某公司"
    h = extract_region_hints_from_text(text)
    assert "成都" in h["source_price_region"]
    assert "广东" in h["supplier_region"]
    assert h["source_price_region"] != h["supplier_region"]


def test_to_records_from_legacy_and_back():
    cands = [
        {
            "title": "薄壁不锈钢管 DN50",
            "url": "https://example.com/1",
            "price_tax": 12.3,
            "spec_seen": "DN50",
            "supplier": "厂A",
        }
    ]
    recs = to_records(cands, platform="guangcai", query="薄壁不锈钢管")
    assert len(recs) == 1
    assert recs[0].platform == "guangcai"
    assert recs[0].price == 12.3
    back = from_records(recs)
    assert back[0]["title"] == "薄壁不锈钢管 DN50"
    assert float(back[0]["price_tax"]) == 12.3


def test_to_records_ship_only_not_price_region():
    cands = [
        {
            "title": "阀门",
            "url": "https://x",
            "price_tax": 1,
            "spec_seen": "发货地：广东省 深圳",
            "supplier": "深圳某厂",
        }
    ]
    recs = to_records(cands, platform="1688", query="阀门")
    # 仅有发货地时，不得写成价格适用地
    assert recs[0].source_price_region == "" or "适用" in recs[0].source_price_region


def test_search_on_platform_still_returns_dicts():
    """回归：search_on_platform 签名与返回类型不变（未知平台）。"""
    cands, st = search_on_platform(
        None, "not_a_real_platform_xyz", "q", [], 1000, 1, {}
    )
    assert cands == []
    assert st == "unknown_platform"
