from material_price_audit.llm_agent import (
    estimate_ex_tax,
    rule_rank_candidates,
    under_submit_flag,
)


class _Item:
    def __init__(self, submit=None, name="阀门", spec="DN50"):
        self.submit = submit
        self.name = name
        self.spec = spec
        self.brand = ""
        self.unit = "个"


def test_under_submit_flag():
    c = {"price_tax": 100, "tax_mode": "tax_excl"}
    assert under_submit_flag(c, 120) == "under"
    assert under_submit_flag(c, 80) == "over"  # 100/80 = 1.25 > 15%
    assert under_submit_flag(c, 95) == "near"  # 100/95 ≈ 1.05 within 15%


def test_rule_rank_spec_before_price_when_same_name():
    """同名候选：规格一致优先；价格仅作末位 tie-break。"""
    item = _Item(submit=100, name="闸阀", spec="DN50")
    cands = [
        {
            "title": "闸阀",
            "spec_seen": "DN50",
            "price_tax": 200,
            "tax_mode": "tax_excl",
            "score": 50,
            "url": "hi-correct",
        },
        {
            "title": "闸阀",
            "spec_seen": "DN50",
            "price_tax": 80,
            "tax_mode": "tax_excl",
            "score": 50,
            "url": "lo-correct",
        },
        {
            "title": "闸阀",
            "spec_seen": "DN25",
            "price_tax": 50,
            "tax_mode": "tax_excl",
            "score": 90,
            "url": "cheap-wrong",
        },
    ]
    ranked = rule_rank_candidates(cands, item=item, tax_divisor=1.13, top_n=3)
    # 错 DN 不得排第一
    assert ranked[0]["url"] != "cheap-wrong"
    # 同规格下低价优先
    assert ranked[0]["url"] == "lo-correct"
    assert ranked[-1]["url"] == "cheap-wrong"


def test_estimate_ex_tax_incl():
    c = {"price_tax": 113, "tax_mode": "tax_incl"}
    assert abs(estimate_ex_tax(c, 1.13) - 100) < 0.01
