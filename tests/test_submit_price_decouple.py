"""市场匹配与报送价策略解耦。"""

from material_price_audit.export_quotes import audit_from_quotes
from material_price_audit.inquiry import (
    build_review_candidates,
    price_anomaly_hint,
    vs_submit_relation,
)
from material_price_audit.models import CanonicalItem, Quote, QuoteSet


def _item(submit: float = 100.0, name: str = "闸阀", spec: str = "DN100") -> CanonicalItem:
    return CanonicalItem(
        id="t1",
        sheet="s",
        row=2,
        name=name,
        spec=spec,
        unit="个",
        submit=submit,
    )


def test_vs_submit_relation_labels():
    assert vs_submit_relation(100, tax_mode="tax_excl", submit=100) == "below_submit"
    assert vs_submit_relation(105, tax_mode="tax_excl", submit=100) == "near_submit"
    assert vs_submit_relation(150, tax_mode="tax_excl", submit=100) == "above_submit"
    assert vs_submit_relation(20, tax_mode="tax_excl", submit=100) == "suspicious_low"
    assert vs_submit_relation(100, tax_mode="tax_excl", submit=None) == "unknown"


def test_exact_match_above_submit_50pct_still_formal_semantics():
    """
    精确匹配但高于报送 50%：应能作为正式市场报价（由 inquiry 收录），
    并标记 above_submit；本测试验证关系标记与 anomaly，不挡 match。
    """
    vs = vs_submit_relation(150, tax_mode="tax_excl", submit=100)
    assert vs == "above_submit"
    hint = price_anomaly_hint(vs, submit=100)
    assert "高于报送" in hint
    # Quote 可携带该标记
    q = Quote(
        rank=1,
        price=150,
        platform="guangcai",
        title="闸阀 DN100",
        url="http://x",
        match_level="practical",
        match_score=1.0,
        match_detail="名称+规格全部命中",
        tax_mode="tax_excl",
        price_ex_tax=150,
        price_role="formal",
        vs_submit=vs,
        price_anomaly=hint,
    )
    assert q.vs_submit == "above_submit"
    assert q.price_role == "formal"
    d = q.to_dict()
    q2 = Quote.from_dict(d)
    assert q2.vs_submit == "above_submit"


def test_exact_match_suspicious_low_still_kept_with_flag():
    vs = vs_submit_relation(10, tax_mode="tax_excl", submit=100)
    assert vs == "suspicious_low"
    hint = price_anomaly_hint(vs, submit=100)
    assert "远低于报送" in hint
    q = Quote(
        rank=1,
        price=10,
        platform="zaojiatong",
        title="闸阀 DN100",
        url="http://y",
        match_level="practical",
        price_ex_tax=10,
        price_role="formal",
        vs_submit=vs,
        price_anomaly=hint,
    )
    assert q.price_role == "formal"
    assert q.vs_submit == "suspicious_low"


def test_wrong_spec_near_submit_not_in_formal_quotes_via_review_only():
    """规格错误但价格接近报送 → 不得进正式 quotes（仅可能待核，且硬冲突 practical 丢）。"""
    item = _item(submit=100, name="闸阀", spec="DN100")
    attempts = [
        {
            "platform": "guangcai",
            "price_tax": 102.0,
            "tax_mode": "tax_excl",
            "match_ok": False,
            "bucket": "discard",
            "match_outcome": "reject",
            "match_detail": "规格冲突：尺寸 DN100，页面尺寸为 DN40",
            "conflicts": ["尺寸 DN100，页面尺寸为 DN40"],
            "title": "闸阀",
            "name_hit": True,
            "url": "http://wrong",
        }
    ]
    # 正式 quotes 为空（测试侧模拟）
    qset = QuoteSet(item_id=item.id, quotes=[], status="no_match", attempts=attempts)
    assert qset.quotes == []
    # practical 硬冲突不进待核
    revs = build_review_candidates(item, attempts, match_mode="practical")
    assert revs == []


def test_never_exceed_only_affects_audit_not_market_quote():
    item = _item(submit=100)
    qset = QuoteSet(
        item_id=item.id,
        quotes=[
            Quote(
                rank=1,
                price=180,
                platform="guangcai",
                title="闸阀 DN100",
                url="http://x",
                tax_mode="tax_excl",
                price_ex_tax=180,
                price_role="formal",
                vs_submit="above_submit",
            ),
            Quote(
                rank=2,
                price=120,
                platform="huixun",
                title="闸阀 DN100",
                url="http://y",
                tax_mode="tax_excl",
                price_ex_tax=120,
                price_role="formal",
                vs_submit="above_submit",
            ),
        ],
        status="full_k",
    )
    # 市场报价仍完整保留
    assert len(qset.quotes) == 2
    assert all(q.price_role == "formal" for q in qset.quotes)
    # 审定：开启 never_exceed → 封顶报送
    audit_cap = audit_from_quotes(item, qset, 1.13, never_exceed=True)
    assert audit_cap == 100.0
    # 关闭 never_exceed → 取最低市场不含税
    audit_raw = audit_from_quotes(item, qset, 1.13, never_exceed=False)
    assert audit_raw == 120.0


def test_ecommerce_still_market_ref_role():
    q = Quote(
        rank=1,
        price=99,
        platform="jd",
        title="闸阀",
        url="http://jd",
        price_role="market_ref",
        match_level="market_ref",
        vs_submit="below_submit",
    )
    assert q.price_role == "market_ref"
    assert q.price_role != "formal"


def test_review_keeps_above_submit_price():
    """待核也不再因高于报送丢弃。"""
    item = _item(submit=100, name="分控器", spec="8端口")
    attempts = [
        {
            "platform": "guangcai",
            "price_tax": 3000.0,
            "tax_mode": "tax_excl",
            "match_ok": False,
            "bucket": "candidate",
            "match_outcome": "review",
            "match_detail": "名称命中；规格缺少：脱机",
            "title": "分控器",
            "name_hit": True,
            "url": "http://hi",
            "spec_seen": "8端口分控器",
        }
    ]
    revs = build_review_candidates(item, attempts, match_mode="practical")
    assert len(revs) == 1
    assert revs[0].price == 3000.0
    assert revs[0].vs_submit == "above_submit"
