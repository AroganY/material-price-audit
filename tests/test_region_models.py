"""Phase 1：RegionTarget / RegionEvidence / CandidateRecord 与旧字段兼容。"""

from __future__ import annotations

from material_price_audit.models import CanonicalItem, Quote, QuoteSet
from material_price_audit.region_models import (
    CandidateRecord,
    RegionEvidence,
    RegionTarget,
)
from material_price_audit.settings_store import UserSettings


def test_region_target_code_key_and_display():
    t = RegionTarget(
        province="四川省",
        province_code="510000",
        city="成都市",
        city_code="510100",
        source="task",
        strategy="strict_city",
    )
    assert t.code_key == "510100"
    assert "成都" in t.display
    assert t.is_specified() is True
    assert RegionTarget.unspecified().is_specified() is False
    assert RegionTarget.unspecified().code_key == "UNSPECIFIED"


def test_region_target_roundtrip():
    t = RegionTarget(
        province="重庆市",
        province_code="500000",
        city="重庆市",
        city_code="500100",
        source="excel_row",
        strategy="allow_province",
    )
    t2 = RegionTarget.from_dict(t.to_dict())
    assert t2.city_code == "500100"
    assert t2.strategy == "allow_province"
    assert t2.source == "excel_row"


def test_chengdu_chongqing_different_keys():
    cd = RegionTarget(city="成都市", city_code="510100")
    cq = RegionTarget(city="重庆市", city_code="500100")
    assert cd.code_key != cq.code_key


def test_region_evidence_separates_supplier_and_price():
    ev = RegionEvidence(
        requested_region="成都市",
        source_price_region="成都市",
        supplier_region="广东省广州市",
        region_match="exact",
        region_evidence="价格适用地区：成都",
    )
    d = ev.to_dict()
    assert d["source_price_region"] == "成都市"
    assert d["supplier_region"] == "广东省广州市"
    assert d["source_price_region"] != d["supplier_region"]
    ev2 = RegionEvidence.from_dict(d)
    assert ev2.region_match == "exact"


def test_candidate_record_legacy_roundtrip():
    legacy = {
        "title": "薄壁不锈钢管 DN100",
        "url": "https://example.com/p/1",
        "price_tax": 88.5,
        "spec_seen": "DN100 304",
        "supplier": "某厂",
        "unit": "m",
        "tax_mode": "tax_incl",
        "inline_detail": True,
    }
    rec = CandidateRecord.from_legacy_cand(
        legacy, platform="zaojiatong", query="薄壁不锈钢管", requested_region="成都"
    )
    assert rec.platform == "zaojiatong"
    assert rec.price == 88.5
    assert rec.detail_url.endswith("/1")
    assert rec.requested_region == "成都"
    assert rec.raw_payload.get("inline_detail") is True
    back = rec.to_legacy_cand()
    assert back["title"] == "薄壁不锈钢管 DN100"
    assert float(back["price_tax"]) == 88.5
    assert back["source_price_region"] == ""
    # 供应商地不得自动填入价格地
    assert back.get("supplier_region") == ""


def test_candidate_name_decision_normalize():
    c = CandidateRecord(name_decision="SAME")
    assert c.name_decision == "same"
    c2 = CandidateRecord.from_dict({"name_decision": "bogus"})
    assert c2.name_decision == "pending"


def test_canonical_item_region_fields_compat():
    it = CanonicalItem(
        id="a|1",
        sheet="a",
        row=1,
        name="管",
        region={"city": "成都市", "city_code": "510100", "source": "task"},
        region_raw="成都市",
    )
    d = it.to_dict()
    it2 = CanonicalItem.from_dict(d)
    assert it2.region.get("city_code") == "510100"
    assert it2.region_raw == "成都市"
    # 旧 dict 无 region 也能加载
    it3 = CanonicalItem.from_dict(
        {"id": "x", "sheet": "s", "row": 2, "name": "阀"}
    )
    assert it3.region == {}
    assert it3.region_raw == ""


def test_quote_region_fields_roundtrip():
    q = Quote(
        rank=1,
        price=12.0,
        platform="guangcai",
        title="t",
        url="https://u",
        requested_region="成都市",
        source_price_region="成都市",
        supplier_region="广东",
        region_match="exact",
        name_decision="same",
        source_record_id="row-3",
    )
    q2 = Quote.from_dict(q.to_dict())
    assert q2.requested_region == "成都市"
    assert q2.supplier_region == "广东"
    assert q2.source_price_region == "成都市"
    assert q2.region_match == "exact"
    assert q2.name_decision == "same"
    # 旧 Quote 无新字段
    q3 = Quote.from_dict(
        {
            "rank": 1,
            "price": 1,
            "platform": "jd",
            "title": "x",
            "url": "https://j",
        }
    )
    assert q3.region_match == ""
    assert q3.name_decision == ""


def test_settings_region_defaults():
    s = UserSettings.from_dict({})
    assert s.region_strategy == "strict_city"
    assert s.region_required is False
    assert s.default_region == {}
    s2 = UserSettings.from_dict(
        {
            "region_strategy": "allow_province",
            "region_required": True,
            "default_region": {
                "province": "四川省",
                "city": "成都市",
                "city_code": "510100",
            },
        }
    )
    assert s2.region_strategy == "allow_province"
    assert s2.region_required is True
    assert s2.default_region["city_code"] == "510100"


def test_quoteset_still_works_with_extended_quote():
    qs = QuoteSet(
        item_id="i1",
        quotes=[
            Quote(
                rank=1,
                price=9,
                platform="p",
                title="t",
                url="u",
                region_match="unknown",
            )
        ],
        status="partial",
    )
    qs2 = QuoteSet.from_dict(qs.to_dict())
    assert qs2.quotes[0].price == 9
    assert qs2.status == "partial"
