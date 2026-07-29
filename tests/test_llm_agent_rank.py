from material_price_audit.llm_agent import (
    estimate_ex_tax,
    rule_rank_candidates,
    under_submit_flag,
)


class _Item:
    def __init__(self, submit=None, name="阀门"):
        self.submit = submit
        self.name = name
        self.spec = "DN50"
        self.brand = ""
        self.unit = "个"


def test_under_submit_flag():
    c = {"price_tax": 100, "tax_mode": "tax_excl"}
    assert under_submit_flag(c, 120) == "under"
    assert under_submit_flag(c, 80) == "over"  # 100/80 = 1.25 > 15%
    assert under_submit_flag(c, 95) == "near"  # 100/95 ≈ 1.05 within 15%


def test_rule_rank_prefers_under_submit():
    item = _Item(submit=100)
    cands = [
        {"title": "A", "price_tax": 200, "tax_mode": "tax_excl", "score": 90, "url": "1"},
        {"title": "B", "price_tax": 80, "tax_mode": "tax_excl", "score": 70, "url": "2"},
        {"title": "C", "price_tax": 99, "tax_mode": "tax_excl", "score": 85, "url": "3"},
    ]
    ranked = rule_rank_candidates(cands, item=item, tax_divisor=1.13, top_n=3)
    # B(80) and C(99) under submit before A(200)
    assert ranked[0]["title"] in ("B", "C")
    assert ranked[0]["_under_submit"] == "under"
    assert ranked[-1]["title"] == "A"
    assert ranked[-1]["_under_submit"] == "over"


def test_estimate_ex_tax_incl():
    c = {"price_tax": 113, "tax_mode": "tax_incl"}
    assert abs(estimate_ex_tax(c, 1.13) - 100) < 0.01
