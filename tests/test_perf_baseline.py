"""Phase 0：性能埋点基线 — 默认关闭、开启可计数、不影响匹配结果。"""

from __future__ import annotations

import os

from material_price_audit import perf as perf_mod
from material_price_audit.matching import strict_name_spec_match
from material_price_audit.models import CanonicalItem, Quote, QuoteSet


def test_perf_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MPA_PERF", raising=False)
    perf_mod.disable()
    perf_mod.reset()
    assert perf_mod.enabled() is False
    perf_mod.inc("query_count", 5)
    snap = perf_mod.snapshot()
    assert snap["aggregate"]["query_count"] == 0
    assert snap["counters"] == {}


def test_perf_enable_counts(monkeypatch):
    monkeypatch.setenv("MPA_PERF", "1")
    with perf_mod.scoped_enable(True) as rec:
        assert rec.enabled is True
        perf_mod.inc("query_count", 2, key="item:a")
        perf_mod.inc("detail_open_count", 1, key="item:a")
        with perf_mod.span("search_ms", key="item:a"):
            pass
        perf_mod.inc("accepted", 1, key="item:a")
        snap = perf_mod.snapshot()
    assert snap["aggregate"]["query_count"] == 2
    assert snap["aggregate"]["detail_open_count"] == 1
    assert snap["aggregate"]["accepted"] == 1
    assert "item:a" in snap["buckets"]
    assert snap["buckets"]["item:a"]["query_count"] == 2


def test_perf_env_flag(monkeypatch):
    monkeypatch.setenv("MPA_PERF", "true")
    perf_mod.disable()  # 显式关本地 flag，仅 env
    perf_mod.reset()
    # enabled 读 env
    assert perf_mod.enabled() is True
    perf_mod.inc("candidate_count", 3)
    snap = perf_mod.snapshot()
    # env 开时 inc 生效
    assert snap["aggregate"]["candidate_count"] == 3
    monkeypatch.delenv("MPA_PERF", raising=False)
    perf_mod.disable()
    perf_mod.reset()


def test_matching_unchanged_with_perf_on(monkeypatch):
    """开启埋点不得改变 strict_name_spec_match 结果。"""
    item = CanonicalItem(
        id="t1",
        sheet="s",
        row=2,
        name="球墨铸铁管",
        spec="DN150 PN10",
        unit="m",
    )
    title = "球墨铸铁管 DN150 PN10"
    body = "球墨铸铁管 DN150 PN10 价格 100 元"
    with perf_mod.scoped_enable(False):
        a = strict_name_spec_match(item, title, body)
    with perf_mod.scoped_enable(True):
        b = strict_name_spec_match(item, title, body)
        perf_mod.inc("query_count", 1)
    assert a.ok == b.ok
    assert a.outcome == b.outcome
    assert a.detail == b.detail


def test_quoteset_structure_baseline():
    """业务结果结构基线：四分法字段仍在。"""
    qs = QuoteSet(
        item_id="x",
        quotes=[
            Quote(
                rank=1,
                price=10.0,
                platform="guangcai",
                title="t",
                url="https://a",
                price_role="formal",
            )
        ],
        review_candidates=[],
        market_refs=[],
        web_refs=[],
        supplier_leads=[],
        status="partial",
    )
    d = qs.to_dict()
    assert "quotes" in d
    assert "review_candidates" in d
    assert "market_refs" in d
    assert "web_refs" in d
    assert "supplier_leads" in d
    qs2 = QuoteSet.from_dict(d)
    assert len(qs2.quotes) == 1
    assert qs2.quotes[0].price == 10.0


def test_span_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("MPA_PERF", raising=False)
    perf_mod.disable()
    perf_mod.reset()
    with perf_mod.span("search_ms"):
        x = 1 + 1
    assert x == 2
    assert perf_mod.snapshot()["aggregate"]["search_ms"] == 0
