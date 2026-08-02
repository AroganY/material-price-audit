"""
性能基线埋点（Phase 0）。

默认关闭，对业务路径零副作用。
开启方式：
  - 环境变量 MPA_PERF=1
  - 或 perf.enable() / perf.scoped_enable()

只累计计数与耗时，不参与匹配/定价，不写网络。
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


def _env_enabled() -> bool:
    v = (os.environ.get("MPA_PERF") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


@dataclass
class PerfBucket:
    """单个「材料族×地区×平台」或通用桶。"""

    key: str = ""
    query_count: int = 0
    candidate_count: int = 0
    detail_open_count: int = 0
    cache_hits: int = 0
    region_switch_count: int = 0
    region_verify_ok: int = 0
    region_verify_fail: int = 0
    name_match_ms: float = 0.0
    spec_match_ms: float = 0.0
    search_ms: float = 0.0
    detail_ms: float = 0.0
    llm_calls: int = 0
    tokens: int = 0
    accepted: int = 0
    review: int = 0
    rejected: int = 0
    total_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "query_count": self.query_count,
            "candidate_count": self.candidate_count,
            "detail_open_count": self.detail_open_count,
            "cache_hits": self.cache_hits,
            "region_switch_count": self.region_switch_count,
            "region_verify_ok": self.region_verify_ok,
            "region_verify_fail": self.region_verify_fail,
            "name_match_ms": round(self.name_match_ms, 3),
            "spec_match_ms": round(self.spec_match_ms, 3),
            "search_ms": round(self.search_ms, 3),
            "detail_ms": round(self.detail_ms, 3),
            "llm_calls": self.llm_calls,
            "tokens": self.tokens,
            "accepted": self.accepted,
            "review": self.review,
            "rejected": self.rejected,
            "total_ms": round(self.total_ms, 3),
        }


class PerfRecorder:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._enabled = False
        self._buckets: dict[str, PerfBucket] = {}
        self._run_started: float | None = None
        self._counters: dict[str, int] = {}

    def enable(self) -> None:
        with self._lock:
            self._enabled = True
            if self._run_started is None:
                self._run_started = time.perf_counter()

    def disable(self) -> None:
        with self._lock:
            self._enabled = False

    @property
    def enabled(self) -> bool:
        if self._enabled:
            return True
        return _env_enabled()

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._counters.clear()
            self._run_started = time.perf_counter() if self.enabled else None

    def _bucket(self, key: str) -> PerfBucket:
        k = key or "_global"
        b = self._buckets.get(k)
        if b is None:
            b = PerfBucket(key=k)
            self._buckets[k] = b
        return b

    def inc(self, field: str, n: int = 1, *, key: str = "_global") -> None:
        if not self.enabled:
            return
        with self._lock:
            b = self._bucket(key)
            if hasattr(b, field):
                cur = getattr(b, field)
                if isinstance(cur, int):
                    setattr(b, field, cur + int(n))
            self._counters[field] = int(self._counters.get(field, 0)) + int(n)

    def add_ms(self, field: str, ms: float, *, key: str = "_global") -> None:
        if not self.enabled:
            return
        with self._lock:
            b = self._bucket(key)
            if hasattr(b, field):
                cur = getattr(b, field)
                if isinstance(cur, (int, float)):
                    setattr(b, field, float(cur) + float(ms))

    @contextmanager
    def span(self, field: str, *, key: str = "_global") -> Iterator[None]:
        if not self.enabled:
            yield
            return
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.add_ms(field, (time.perf_counter() - t0) * 1000.0, key=key)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total_ms = 0.0
            if self._run_started is not None:
                total_ms = (time.perf_counter() - self._run_started) * 1000.0
            buckets = {k: v.to_dict() for k, v in self._buckets.items()}
            # 汇总
            agg = PerfBucket(key="_aggregate")
            for b in self._buckets.values():
                for f in (
                    "query_count",
                    "candidate_count",
                    "detail_open_count",
                    "cache_hits",
                    "region_switch_count",
                    "region_verify_ok",
                    "region_verify_fail",
                    "llm_calls",
                    "tokens",
                    "accepted",
                    "review",
                    "rejected",
                ):
                    setattr(agg, f, getattr(agg, f) + getattr(b, f))
                for f in (
                    "name_match_ms",
                    "spec_match_ms",
                    "search_ms",
                    "detail_ms",
                    "total_ms",
                ):
                    setattr(agg, f, getattr(agg, f) + getattr(b, f))
            return {
                "enabled": self.enabled,
                "run_total_ms": round(total_ms, 3),
                "counters": dict(self._counters),
                "buckets": buckets,
                "aggregate": agg.to_dict(),
            }


_RECORDER = PerfRecorder()


def get_recorder() -> PerfRecorder:
    return _RECORDER


def enable() -> None:
    _RECORDER.enable()


def disable() -> None:
    _RECORDER.disable()


def reset() -> None:
    _RECORDER.reset()


def enabled() -> bool:
    return _RECORDER.enabled


def snapshot() -> dict[str, Any]:
    return _RECORDER.snapshot()


def inc(field: str, n: int = 1, *, key: str = "_global") -> None:
    _RECORDER.inc(field, n, key=key)


def add_ms(field: str, ms: float, *, key: str = "_global") -> None:
    _RECORDER.add_ms(field, ms, key=key)


@contextmanager
def span(field: str, *, key: str = "_global") -> Iterator[None]:
    with _RECORDER.span(field, key=key):
        yield


@contextmanager
def scoped_enable(on: bool = True) -> Iterator[PerfRecorder]:
    """测试用：临时开启并在退出时 reset+restore。"""
    prev = _RECORDER._enabled
    if on:
        _RECORDER.enable()
        _RECORDER.reset()
    else:
        _RECORDER.disable()
    try:
        yield _RECORDER
    finally:
        _RECORDER.disable()
        _RECORDER.reset()
        _RECORDER._enabled = prev
