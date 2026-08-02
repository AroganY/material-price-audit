from material_price_audit.inquiry import build_review_candidates, submit_price_band
from material_price_audit.matching import decide_quote_bucket, strict_name_spec_match
from material_price_audit.models import CanonicalItem


def _item():
    return CanonicalItem(
        id="x",
        sheet="s",
        row=3,
        name="XZP100型片式消声器 1250X400 有效长度：1500",
        spec="15K116-1",
        unit="节",
        submit=877.0,
    )


def test_practical_discards_hard_dimension_conflict():
    """功率/尺寸等硬冲突不得进待核（否则询价结果不可信）。"""
    it = _item()
    mr = strict_name_spec_match(it, "XZP100片式消声器", "规格 400×320×1000")
    bucket, outcome, detail = decide_quote_bucket(
        mr, unit_ok=True, price_ambiguous=False, match_mode="practical"
    )
    assert bucket == "discard"
    assert outcome == "reject"
    assert "冲突" in detail or "不完全一致" in detail or "硬规格" in detail


def test_strict_discards_dimension_conflict():
    it = _item()
    mr = strict_name_spec_match(it, "XZP100片式消声器", "规格 400×320×1000")
    bucket, _, _ = decide_quote_bucket(
        mr, unit_ok=True, price_ambiguous=False, match_mode="strict"
    )
    assert bucket == "discard"


def test_loose_keeps_dimension_conflict_as_candidate():
    it = _item()
    mr = strict_name_spec_match(it, "XZP100片式消声器", "规格 400×320×1000")
    bucket, outcome, _ = decide_quote_bucket(
        mr, unit_ok=True, price_ambiguous=False, match_mode="loose"
    )
    assert bucket == "candidate"
    assert outcome == "review"


def test_build_review_keeps_above_submit_drops_hard_conflict():
    """高于报送仍可待核；硬冲突仍丢。"""
    it = _item()  # submit=877
    attempts = [
        {
            "match_ok": False,
            "bucket": "candidate",
            "name_hit": True,
            "price_tax": 1500.0,  # 远超报送 — 仍保留
            "title": "XZP100片式消声器",
            "platform": "guangcai",
            "match_outcome": "review",
            "match_detail": "名称命中；规格缺少：有效长度",
            "url": "https://example.com/over",
            "tax_mode": "tax_excl",
        },
        {
            "match_ok": False,
            "bucket": "candidate",
            "name_hit": True,
            "price_tax": 917.4,  # 接近报送但硬冲突
            "title": "XZP100片式消声器",
            "platform": "guangcai",
            "match_outcome": "reject",
            "match_detail": "规格冲突：尺寸 1250x400，页面 400×320",
            "conflicts": ["尺寸 1250x400，页面 400×320"],
            "url": "https://example.com/conflict",
            "tax_mode": "tax_excl",
        },
        {
            "match_ok": False,
            "bucket": "candidate",
            "name_hit": True,
            "price_tax": 820.0,
            "title": "XZP100片式消声器",
            "platform": "guangcai",
            "match_outcome": "review",
            "match_detail": "名称命中；规格缺少：有效长度：1500",
            "url": "https://example.com/ok",
            "tax_mode": "tax_excl",
        },
    ]
    cands = build_review_candidates(it, attempts, match_mode="practical")
    prices = {c.price for c in cands}
    assert 820.0 in prices
    assert 1500.0 in prices  # 高于报送不丢
    assert all(c.url != "https://example.com/conflict" for c in cands)


def test_submit_price_band():
    # 新标签；submit_price_band 兼容别名
    assert submit_price_band(800, tax_mode="tax_excl", submit=877) == "below_submit"
    assert submit_price_band(900, tax_mode="tax_excl", submit=877) == "near_submit"
    assert submit_price_band(1200, tax_mode="tax_excl", submit=877) == "above_submit"
    assert submit_price_band(50, tax_mode="tax_excl", submit=877) == "suspicious_low"
    assert submit_price_band(800, tax_mode="tax_excl", submit=None) == "unknown"
