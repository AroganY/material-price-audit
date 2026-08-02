"""京东/1688 市场参考策略：不进正式合格价。"""

from __future__ import annotations

from material_price_audit.models import Quote, QuoteSet
from material_price_audit.platforms import is_ecommerce_platform


def test_is_ecommerce_platform():
    assert is_ecommerce_platform("jd") is True
    assert is_ecommerce_platform("1688") is True
    assert is_ecommerce_platform("京东") is True
    assert is_ecommerce_platform("guangcai") is False
    assert is_ecommerce_platform("huixun") is False
    assert is_ecommerce_platform("zaojiatong") is False
    assert is_ecommerce_platform("造价通") is False


def test_quote_price_role_roundtrip():
    q = Quote(
        rank=1,
        price=12.5,
        platform="jd",
        title="demo",
        url="https://item.jd.com/1.html",
        price_role="market_ref",
    )
    d = q.to_dict()
    q2 = Quote.from_dict(d)
    assert q2.price_role == "market_ref"
    assert q2.platform == "jd"


def test_quoteset_market_refs_roundtrip():
    qs = QuoteSet(
        item_id="x1",
        quotes=[],
        market_refs=[
            Quote(
                rank=1,
                price=9.9,
                platform="1688",
                title="ref",
                url="https://detail.1688.com/1.html",
                price_role="market_ref",
            )
        ],
        status="need_review",
    )
    d = qs.to_dict()
    assert "market_refs" in d
    qs2 = QuoteSet.from_dict(d)
    assert len(qs2.market_refs) == 1
    assert qs2.market_refs[0].price_role == "market_ref"
    assert qs2.quotes == []
