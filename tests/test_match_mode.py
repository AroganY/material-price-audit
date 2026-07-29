from material_price_audit.inquiry import build_review_candidates
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


def test_practical_keeps_dimension_conflict_as_candidate():
    it = _item()
    mr = strict_name_spec_match(it, "XZP100片式消声器", "规格 400×320×1000")
    bucket, outcome, detail = decide_quote_bucket(
        mr, unit_ok=True, price_ambiguous=False, match_mode="practical"
    )
    assert bucket == "candidate"
    assert outcome == "review"
    assert "冲突" in detail or "不完全一致" in detail


def test_strict_discards_dimension_conflict():
    it = _item()
    mr = strict_name_spec_match(it, "XZP100片式消声器", "规格 400×320×1000")
    bucket, _, _ = decide_quote_bucket(
        mr, unit_ok=True, price_ambiguous=False, match_mode="strict"
    )
    assert bucket == "discard"


def test_build_review_includes_named_conflict_in_practical():
    it = _item()
    attempts = [
        {
            "match_ok": False,
            "bucket": "candidate",
            "name_hit": True,
            "price_tax": 917.4,
            "title": "XZP100片式消声器",
            "platform": "guangcai",
            "match_outcome": "review",
            "match_detail": "规格冲突：尺寸 1250x400，页面 400×320",
            "url": "https://example.com/p",
            "tax_mode": "tax_excl",
        }
    ]
    cands = build_review_candidates(it, attempts, match_mode="practical")
    assert len(cands) == 1
    assert cands[0].price == 917.4
    assert cands[0].match_level == "need_review"
