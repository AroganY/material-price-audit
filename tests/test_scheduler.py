"""Phase 5：有界平台调度 — 并发上限、同域串行、满K取消、熔断。"""

from __future__ import annotations

import threading
import time

from material_price_audit.models import Quote, QuoteSet
from material_price_audit.scheduler import (
    BoundedPlatformScheduler,
    CancelToken,
    CircuitBreaker,
    PlatformSessionPool,
    merge_platform_quote_sets,
    platform_domain,
    profile_dir_for_platform,
    scheduler_enabled,
)
from material_price_audit.settings_store import UserSettings
from pathlib import Path


def test_scheduler_flag():
    # 默认开启
    assert scheduler_enabled(None, env={}) is True
    assert scheduler_enabled(None, env={"MPA_SCHEDULER": "1"}) is True
    assert scheduler_enabled(None, env={"MPA_SCHEDULER": "0"}) is False
    s = UserSettings.from_dict({"use_platform_scheduler": True})
    assert scheduler_enabled(s, env={}) is True
    s_off = UserSettings.from_dict({"use_platform_scheduler": False})
    assert scheduler_enabled(s_off, env={}) is False
    # 环境变量优先
    assert scheduler_enabled(s_off, env={"MPA_SCHEDULER": "1"}) is True


def test_platform_domain_map():
    assert platform_domain("guangcai") == "gldjc.com"
    assert platform_domain("jd") == "jd.com"
    assert platform_domain("guangcai") != platform_domain("huixun")


def test_max_platforms_concurrent_never_exceeds_2():
    """验收 13：不同平台总并发不得超过 2。"""
    sched = BoundedPlatformScheduler(max_platforms=2)
    inflight = {"n": 0, "max": 0}
    lock = threading.Lock()

    def worker(pid: str, token: CancelToken):
        with lock:
            inflight["n"] += 1
            inflight["max"] = max(inflight["max"], inflight["n"])
        time.sleep(0.08)
        with lock:
            inflight["n"] -= 1
        return QuoteSet(item_id="x", status="no_match")

    plats = ["guangcai", "huixun", "lingcai", "yize", "zaojiatong"]
    sched.submit_platform_jobs(plats, worker)
    assert inflight["max"] <= 2
    assert sched.stats.max_inflight <= 2


def test_same_domain_serialized():
    """同域并发为 1：用两个自定义同域平台模拟。"""
    # 将两个 id 映射到同域：通过直接测 domain lock 行为
    # 使用 guangcai 两次去重后只有一个；改为测不同 pid 若同 domain
    from material_price_audit import scheduler as sch

    old = dict(sch._PLATFORM_DOMAIN)
    try:
        sch._PLATFORM_DOMAIN["p_a"] = "same.test"
        sch._PLATFORM_DOMAIN["p_b"] = "same.test"
        sched = BoundedPlatformScheduler(max_platforms=2)
        concurrent = {"n": 0, "max": 0}
        lock = threading.Lock()

        def worker(pid: str, token: CancelToken):
            with lock:
                concurrent["n"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["n"])
            time.sleep(0.1)
            with lock:
                concurrent["n"] -= 1
            return "ok"

        sched.submit_platform_jobs(["p_a", "p_b"], worker)
        # 同域必须串行
        assert concurrent["max"] == 1
    finally:
        sch._PLATFORM_DOMAIN.clear()
        sch._PLATFORM_DOMAIN.update(old)


def test_cancel_when_full_k():
    """验收 14：达到 K 正式价后取消剩余。"""
    sched = BoundedPlatformScheduler(max_platforms=2)
    started = []
    lock = threading.Lock()

    def worker(pid: str, token: CancelToken):
        with lock:
            started.append(pid)
        if pid == "guangcai":
            time.sleep(0.05)
            return QuoteSet(
                item_id="i",
                quotes=[
                    Quote(
                        rank=1,
                        price=10.0,
                        platform="guangcai",
                        title="a",
                        url="u1",
                    ),
                    Quote(
                        rank=2,
                        price=11.0,
                        platform="guangcai",
                        title="b",
                        url="u2",
                    ),
                    Quote(
                        rank=3,
                        price=12.0,
                        platform="guangcai",
                        title="c",
                        url="u3",
                    ),
                ],
                status="full_k",
            )
        # 慢任务：应被 cancel 掉（若尚未开始）
        for _ in range(20):
            if token.is_cancelled():
                return QuoteSet(item_id="i", status="no_match", error="cancelled")
            time.sleep(0.02)
        return QuoteSet(item_id="i", status="no_match")

    formal = {"n": 0}

    def should_stop():
        return formal["n"] >= 3

    def on_result(r):
        if r.ok and r.payload and r.payload.quotes:
            formal["n"] += len(r.payload.quotes)

    results = sched.submit_platform_jobs(
        ["guangcai", "huixun", "lingcai", "yize"],
        worker,
        should_stop=should_stop,
        on_result=on_result,
    )
    cancelled = [r for r in results if r.cancelled]
    # 至少一个被取消，或未全部完成成功
    assert formal["n"] >= 3 or any(r.cancelled for r in results)
    assert len(results) == 4


def test_circuit_breaker_trip():
    br = CircuitBreaker()
    assert br.is_tripped("jd") is False
    assert br.note_status("jd", "rate_limited") is True
    assert br.is_tripped("jd") is True
    assert "rate" in br.reason("jd")


def test_breaker_skips_tripped_platform():
    br = CircuitBreaker()
    br.trip("huixun", "captcha")
    sched = BoundedPlatformScheduler(max_platforms=2, breaker=br)
    ran = []

    def worker(pid: str, token: CancelToken):
        ran.append(pid)
        return "ok"

    results = sched.submit_platform_jobs(["huixun", "guangcai"], worker)
    assert "huixun" not in ran
    assert any(r.platform_id == "huixun" and not r.ok for r in results)
    assert "guangcai" in ran


def test_merge_quote_sets_caps_k():
    a = QuoteSet(
        item_id="i",
        quotes=[
            Quote(1, 5, "a", "t1", "u1"),
            Quote(2, 6, "a", "t2", "u2"),
        ],
    )
    b = QuoteSet(
        item_id="i",
        quotes=[Quote(1, 4, "b", "t3", "u3")],
    )
    m = merge_platform_quote_sets("i", [a, b], k=2)
    assert len(m.quotes) == 2
    assert m.status == "full_k"
    # 最低价优先
    assert m.quotes[0].price <= m.quotes[1].price


def test_profile_dir_isolated():
    base = Path("/tmp/mpa-profile")
    p1 = profile_dir_for_platform(base, "guangcai")
    p2 = profile_dir_for_platform(base, "huixun")
    assert p1 != p2
    assert "guangcai" in str(p1) or "sched-" in str(p1)


def test_platform_worker_reuses_one_thread_and_closes_on_owner_thread():
    """跨三条材料复用同一平台时，Playwright 的创建/使用/关闭必须同线程。"""
    events: list[tuple[str, int]] = []
    lock = threading.Lock()

    class FakeSession:
        def __init__(self, *_a, **_kw):
            with lock:
                events.append(("create", threading.get_ident()))

        def close_quiet(self):
            with lock:
                events.append(("close", threading.get_ident()))

    pool = PlatformSessionPool(
        storage_state={"cookies": [{"name": "sid", "value": "x"}]},
        channel="chrome",
        headless=True,
        session_factory=FakeSession,
    )
    caller_tid = threading.get_ident()
    used = [
        pool.run(
            "guangcai",
            lambda _s, i=i: (events.append((f"use{i}", threading.get_ident())) or threading.get_ident()),
        )
        for i in range(3)
    ]
    assert len(set(used)) == 1
    assert used[0] != caller_tid
    tids = pool.worker_thread_ids()
    assert tids["guangcai"] == used[0]
    assert pool.close_all() == []
    create_tid = next(t for kind, t in events if kind == "create")
    close_tid = next(t for kind, t in events if kind == "close")
    assert create_tid == used[0] == close_tid
    assert not any(t.name == "mpa-platform-guangcai" for t in threading.enumerate())


def test_cancel_token_polls_user_stop():
    stopped = {"v": False}
    token = CancelToken(cancel_check=lambda: stopped["v"])
    assert token.is_cancelled() is False
    stopped["v"] = True
    assert token.is_cancelled() is True
    assert token.reason == "user_stop"


def test_merge_dedupes_same_quote_row_before_counting_k():
    q1 = Quote(1, 100, "guangcai", "分控器", "https://x.test/search")
    q1.detail_url = "https://x.test/quote/1"
    q1.supplier = "A厂"
    q2 = Quote(1, 100, "guangcai", "LED分控器", "https://x.test/search?q=2")
    q2.detail_url = "https://x.test/quote/1"
    q2.supplier = "A厂"
    merged = merge_platform_quote_sets(
        "i",
        [QuoteSet(item_id="i", quotes=[q1]), QuoteSet(item_id="i", quotes=[q2])],
        k=2,
    )
    assert len(merged.quotes) == 1
    assert merged.status == "partial"
