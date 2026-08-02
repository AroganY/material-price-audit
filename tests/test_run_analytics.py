"""任务 run_id 隔离、漏斗、失败归因。"""

from material_price_audit.models import CanonicalItem, Quote, QuoteSet
from material_price_audit.run_analytics import (
    build_funnel,
    build_platform_stats,
    classify_fail_reason,
    filter_quote_map_for_items,
    load_existing_for_continue,
    new_run_id,
    normalize_status,
    resolve_item_status,
)
from material_price_audit.inquiry import quote_map_to_evidence


def _item(i: int, name: str = "闸阀") -> CanonicalItem:
    return CanonicalItem(id=f"s|{i}", sheet="s", row=i, name=name, spec="DN100")


def test_new_run_id_unique():
    a, b = new_run_id(), new_run_id()
    assert a != b
    assert a.startswith("run-")


def test_normalize_status_no_verified_collapse():
    assert normalize_status("full_k") == "full_k"
    assert normalize_status("partial") == "partial"
    assert normalize_status("need_review") == "need_review"
    assert normalize_status("verified") == "full_k"  # 旧值兼容映射


def test_quote_map_to_evidence_keeps_full_k_not_verified():
    it = _item(1)
    qset = QuoteSet(
        item_id=it.id,
        status="full_k",
        quotes=[
            Quote(
                rank=1,
                price=100,
                platform="guangcai",
                title="闸阀 DN100",
                url="http://x",
                price_ex_tax=100,
            )
        ],
    )
    rows = quote_map_to_evidence({it.id: qset}, [it], k=3, run_id="run-test")
    d = rows[it.id]
    assert d["status"] == "full_k"
    assert d["multi_status"] == "full_k"
    assert d.get("legacy_verified") is True
    assert d.get("run_id") == "run-test"


def test_classify_fail_reasons():
    empty = QuoteSet(
        item_id="a",
        status="no_match",
        attempts=[{"platform": "guangcai", "status": "empty_page", "query": "闸阀", "n": 0}],
    )
    assert classify_fail_reason(empty) == "平台没有结果"

    name_miss = QuoteSet(
        item_id="b",
        status="no_match",
        attempts=[
            {
                "platform": "guangcai",
                "query": "闸阀",
                "match_detail": "名称未命中 need=['闸阀']",
                "title": "蝶阀",
            }
        ],
    )
    assert classify_fail_reason(name_miss) == "名称不匹配"

    conflict = QuoteSet(
        item_id="c",
        status="no_match",
        attempts=[
            {
                "platform": "guangcai",
                "match_detail": "规格冲突：尺寸 DN100，页面尺寸为 DN40",
                "name_hit": True,
                "title": "闸阀",
            }
        ],
    )
    assert classify_fail_reason(conflict) == "规格冲突"

    login = QuoteSet(
        item_id="d",
        status="no_match",
        attempts=[{"platform": "huixun", "status": "no_membership", "query": "x"}],
    )
    assert classify_fail_reason(login) == "未登录/无会员"

    rate = QuoteSet(
        item_id="e",
        status="no_match",
        attempts=[{"platform": "jd", "status": "rate_limited", "query": "x"}],
    )
    assert classify_fail_reason(rate) == "限流"

    # 有正式价 → 无失败原因
    ok = QuoteSet(
        item_id="f",
        status="partial",
        quotes=[
            Quote(rank=1, price=10, platform="g", title="t", url="u")
        ],
    )
    assert classify_fail_reason(ok) == ""


def test_funnel_and_platform_stats_isolated_by_items():
    items = [_item(1), _item(2), _item(3, "蝶阀")]
    qm = {
        items[0].id: QuoteSet(
            item_id=items[0].id,
            status="full_k",
            quotes=[
                Quote(rank=1, price=100, platform="guangcai", title="闸阀", url="u1")
            ],
            attempts=[
                {
                    "platform": "guangcai",
                    "query": "闸阀 DN100",
                    "match_detail": "名称+规格全部命中",
                    "name_hit": True,
                    "price_tax": 100,
                }
            ],
        ),
        items[1].id: QuoteSet(
            item_id=items[1].id,
            status="no_match",
            attempts=[
                {
                    "platform": "guangcai",
                    "query": "闸阀",
                    "status": "empty_page",
                    "n": 0,
                },
                {
                    "platform": "zaojiatong",
                    "query": "闸阀 DN100",
                    "match_detail": "名称未命中",
                    "title": "截止阀",
                },
            ],
        ),
        items[2].id: QuoteSet(
            item_id=items[2].id,
            status="need_review",
            review_candidates=[
                Quote(
                    rank=1,
                    price=50,
                    platform="guangcai",
                    title="蝶阀",
                    url="u3",
                    match_level="need_review",
                )
            ],
            attempts=[
                {
                    "platform": "guangcai",
                    "query": "蝶阀",
                    "match_detail": "名称命中；规格缺少：DN100",
                    "name_hit": True,
                    "price_tax": 50,
                }
            ],
        ),
        # 另一任务残留行 —— 不应进 funnel（items 不含）
        "other|9": QuoteSet(
            item_id="other|9",
            status="full_k",
            quotes=[Quote(rank=1, price=1, platform="jd", title="x", url="u")],
        ),
    }
    funnel = build_funnel(items, qm, k=3)
    assert funnel["items_total"] == 3
    assert funnel["full_k"] == 1
    assert funnel["need_review"] == 1
    assert funnel["no_match"] == 1
    assert funnel["spec_full_match"] == 1
    # 失败原因
    assert funnel["fail_reason_counts"].get("平台没有结果") or funnel[
        "fail_reason_counts"
    ].get("名称不匹配")

    pstats = build_platform_stats(qm, item_ids={it.id for it in items})
    assert "guangcai" in pstats
    assert pstats["guangcai"]["formal_hits"] == 1
    assert "jd" not in pstats  # 被 item_ids 过滤


def test_continue_only_same_workbook_and_rows():
    items = [_item(1), _item(2)]
    qm = {
        items[0].id: QuoteSet(item_id=items[0].id, status="full_k", quotes=[]),
        "other|99": QuoteSet(item_id="other|99", status="full_k", quotes=[]),
    }
    filtered = filter_quote_map_for_items(qm, items)
    assert items[0].id in filtered
    assert "other|99" not in filtered

    # 不同工作簿
    got = load_existing_for_continue(
        qm,
        items,
        meta={"input_path": "/tmp/a.xlsx", "run_id": "run-old"},
        input_path="/tmp/b.xlsx",
    )
    assert got == {}

    # 相同工作簿
    got2 = load_existing_for_continue(
        qm,
        items,
        meta={"input_path": "/tmp/a.xlsx", "run_id": "run-old"},
        input_path="/tmp/a.xlsx",
    )
    assert items[0].id in got2
    assert "other|99" not in got2


def test_resolve_legacy_verified():
    row = {"status": "verified", "multi_status": "partial", "quotes": [{}, {}]}
    assert resolve_item_status(row) == "partial"
    row2 = {"status": "verified", "quotes": 3}
    assert resolve_item_status(row2) in ("full_k", "partial")
