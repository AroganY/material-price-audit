from material_price_audit.inquiry import build_review_candidates
from material_price_audit.models import CanonicalItem, QuoteSet


def test_exact_name_candidate_is_kept_for_review_not_as_strict_quote():
    item = CanonicalItem(
        id="x",
        sheet="询价",
        row=3,
        name="8端口分控器",
        spec="脱机分控器，AC220V/8端口分控制器,各端口标准512通道",
    )
    attempts = [
        {
            "platform": "guangcai",
            "url": "https://www.gldjc.com/scj/so.html?keyword=8端口分控器",
            "quotation_url": "https://example.com/quote.jpg",
            "price_tax": 3747.08,
            "match_ok": False,
            "match_score": 0.4,
            "match_detail": "名称命中；规格缺少：电压 AC220V, 512通道, 脱机",
            "title": "控制器",
            "spec_seen": "品种 : 分控器 | 产品描述 : 8端口分控器",
            "supplier": "安徽富晟兴智能科技有限公司",
        },
        {
            "platform": "guangcai",
            "url": "https://www.gldjc.com/scj/so.html?keyword=分控器",
            "price_tax": 1100,
            "match_ok": False,
            "match_score": 0.6,
            "match_detail": "名称命中；规格缺少：电压 AC220V, 脱机",
            "title": "分控器",
            "spec_seen": "8路独立信号数据输出，可同时带载DMX512",
        },
    ]

    review = build_review_candidates(item, attempts, limit=1)
    assert len(review) == 1
    assert review[0].price == 3747.08
    assert review[0].detail_url == "https://example.com/quote.jpg"
    assert review[0].match_level == "need_review"

    qset = QuoteSet(item_id=item.id, review_candidates=review, status="need_review")
    restored = QuoteSet.from_dict(qset.to_dict())
    assert restored.status == "need_review"
    assert restored.quotes == []
    assert restored.review_candidates[0].price == 3747.08
