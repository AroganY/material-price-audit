"""
平台级有界调度（Phase 5）。

规则：
  - 不同平台最多 max_platforms 并发（默认 2）
  - 同一域名并发固定 1（每平台一 Worker 自然满足）
  - 禁止多线程共享 Page/Context
  - 达到 K 个正式价后 cancel 剩余排队任务
  - 429/403/captcha/login 熔断平台

编排使用线程 + 信号量（每 Worker 内仍为 sync Playwright）。
不使用「多线程共 Page」；不使用验证码绕过/代理轮换/stealth。

开关：
  默认开启（settings.use_platform_scheduler=True）
  MPA_SCHEDULER=0/1 可强制关/开
"""

from __future__ import annotations

import os
import queue
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

from .platforms import normalize_platform_id

T = TypeVar("T")

# 平台 → 主域名（同域串行）
_PLATFORM_DOMAIN: dict[str, str] = {
    "guangcai": "gldjc.com",
    "huixun": "iccchina.com",
    "lingcai": "hylcw.cn",
    "yize": "easybii.com",
    "zaojiatong": "zjtcn.com",
    "jd": "jd.com",
    "1688": "1688.com",
    "baidu_web": "baidu.com",
}

# 熔断状态
_TRIP_STATUSES = frozenset(
    {
        "rate_limited",
        "captcha",
        "need_login",
        "need_login_fail",
        "no_membership",
        "403",
        "429",
    }
)


def platform_domain(platform_id: str) -> str:
    pid = normalize_platform_id(platform_id)
    return _PLATFORM_DOMAIN.get(pid, pid or "unknown")


def scheduler_enabled(settings: Any = None, *, env: dict | None = None) -> bool:
    """默认 True（多平台一 Worker 并发）；MPA_SCHEDULER 可强制覆盖。"""
    e = env if env is not None else os.environ
    v = (e.get("MPA_SCHEDULER") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    if settings is not None and hasattr(settings, "use_platform_scheduler"):
        return bool(getattr(settings, "use_platform_scheduler"))
    # 无 settings 时默认开启（与 UserSettings 默认一致）
    return True


class PlatformSessionPool:
    """
    任务级「一平台一 BrowserSession」池。

    - 各平台独立 Playwright Context（禁止多线程共 Page）
    - 用主会话导出的 storage_state 注入登录 Cookie
    - 跨材料复用，避免每条材料反复启浏览器
    """

    def __init__(
        self,
        *,
        storage_state: dict | None,
        channel: str,
        headless: bool,
        max_platforms: int = 3,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.storage_state = storage_state
        self.channel = channel
        self.headless = headless
        self.max_platforms = max(1, min(4, int(max_platforms or 3)))
        self._session_factory = session_factory
        self._workers: dict[str, _PlatformWorker] = {}
        self._lock = threading.RLock()
        self._closed = False
        # 跨材料复用熔断状态；不再每条材料重置。
        self.breaker = CircuitBreaker()

    def _new_worker(self, platform_id: str) -> "_PlatformWorker":
        return _PlatformWorker(
            platform_id=platform_id,
            storage_state=self.storage_state,
            channel=self.channel,
            headless=self.headless,
            session_factory=self._session_factory,
        )

    def run(
        self,
        platform_id: str,
        fn: Callable[[Any], T],
        *,
        cancel_token: "CancelToken | None" = None,
    ) -> T:
        """在平台的固定长期线程内执行 ``fn(session)``。

        Playwright sync API 将创建、使用和关闭绑定在同一线程，
        避免跨材料时出现 ``cannot switch to a different thread``。
        """
        pid = normalize_platform_id(platform_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("平台 Worker 池已关闭")
            worker = self._workers.get(pid)
            if worker is None or not worker.is_alive():
                worker = self._new_worker(pid)
                self._workers[pid] = worker
        return worker.run(fn, cancel_token=cancel_token)

    def worker_thread_ids(self) -> dict[str, int | None]:
        """调试/验收：返回每平台固定线程 id。"""
        with self._lock:
            return {pid: worker.thread_id for pid, worker in self._workers.items()}

    def close_all(self) -> list[str]:
        """在各自 Worker 线程内关闭 Playwright，返回未关净的错误。"""
        with self._lock:
            if self._closed:
                return []
            self._closed = True
            items = list(self._workers.items())
            self._workers.clear()
        errors: list[str] = []
        for pid, worker in items:
            err = worker.close()
            if err:
                errors.append(f"{pid}:{err}")
        return errors


@dataclass
class _WorkerCall:
    fn: Callable[[Any], Any]
    cancel_token: "CancelToken | None" = None
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


_WORKER_STOP = object()


class _PlatformWorker:
    """单平台 actor：一个固定线程 + 一个 BrowserSession。"""

    def __init__(
        self,
        *,
        platform_id: str,
        storage_state: dict | None,
        channel: str,
        headless: bool,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.platform_id = platform_id
        self.storage_state = storage_state
        self.channel = channel
        self.headless = headless
        self.session_factory = session_factory
        self._queue: queue.Queue[Any] = queue.Queue()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._startup_error: BaseException | None = None
        self._close_error = ""
        self._thread = threading.Thread(
            target=self._loop,
            name=f"mpa-platform-{platform_id}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError(f"[{platform_id}] Worker 启动超时")
        if self._startup_error is not None:
            raise RuntimeError(
                f"[{platform_id}] Worker 启动失败: {self._startup_error}"
            ) from self._startup_error

    @property
    def thread_id(self) -> int | None:
        return self._thread.ident

    def is_alive(self) -> bool:
        return self._thread.is_alive() and not self._closed.is_set()

    def _make_session(self) -> Any:
        factory = self.session_factory
        if factory is None:
            from .inquiry import BrowserSession

            factory = BrowserSession
        return factory(
            None,
            self.channel,
            self.headless,
            storage_state=self.storage_state,
        )

    def _loop(self) -> None:
        session = None
        try:
            try:
                session = self._make_session()
            except BaseException as exc:
                self._startup_error = exc
                return
            finally:
                self._ready.set()

            while True:
                call = self._queue.get()
                if call is _WORKER_STOP:
                    break
                if not isinstance(call, _WorkerCall):
                    continue
                try:
                    if call.cancel_token is not None and call.cancel_token.is_cancelled():
                        raise RuntimeError(
                            call.cancel_token.reason or "cancelled_before_worker"
                        )
                    call.result = call.fn(session)
                except BaseException as exc:
                    call.error = exc
                finally:
                    call.done.set()
        finally:
            # 必须在创建 Playwright 的这个线程内关闭。
            if session is not None:
                try:
                    session.close_quiet()
                except BaseException as exc:
                    self._close_error = f"{type(exc).__name__}:{exc}"
            self._closed.set()
            self._ready.set()

    def run(
        self,
        fn: Callable[[Any], T],
        *,
        cancel_token: "CancelToken | None" = None,
    ) -> T:
        if not self.is_alive():
            raise RuntimeError(f"[{self.platform_id}] Worker 已停止")
        call = _WorkerCall(fn=fn, cancel_token=cancel_token)
        self._queue.put(call)
        # 不在等待线程强关 Playwright；只等 actor 在安全边界返回。
        while not call.done.wait(timeout=0.25):
            if not self._thread.is_alive():
                raise RuntimeError(f"[{self.platform_id}] Worker 异常退出")
        if call.error is not None:
            raise call.error
        return call.result

    def close(self, timeout_s: float = 30.0) -> str:
        if self._thread.is_alive():
            self._queue.put(_WORKER_STOP)
            self._thread.join(timeout=max(1.0, float(timeout_s)))
        if self._thread.is_alive():
            return "关闭超时（为避免跨线程损坏，未强行关闭）"
        return self._close_error


class CancelToken:
    """协作式取消：满 K 后置位，Worker 在边界检查。"""

    def __init__(self, cancel_check: Callable[[], bool] | None = None) -> None:
        self._ev = threading.Event()
        self._reason = ""
        self._cancel_check = cancel_check

    def cancel(self, reason: str = "cancelled") -> None:
        self._reason = reason or "cancelled"
        self._ev.set()

    def is_cancelled(self) -> bool:
        if not self._ev.is_set() and self._cancel_check is not None:
            try:
                if self._cancel_check():
                    self.cancel("user_stop")
            except Exception:
                pass
        return self._ev.is_set()

    @property
    def reason(self) -> str:
        return self._reason


class CircuitBreaker:
    """平台熔断：命中限流/验证码/登录失效后本会话跳过。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tripped: dict[str, str] = {}

    def trip(self, platform_id: str, reason: str) -> None:
        pid = normalize_platform_id(platform_id)
        with self._lock:
            self._tripped[pid] = reason or "tripped"

    def is_tripped(self, platform_id: str) -> bool:
        pid = normalize_platform_id(platform_id)
        with self._lock:
            return pid in self._tripped

    def reason(self, platform_id: str) -> str:
        pid = normalize_platform_id(platform_id)
        with self._lock:
            return self._tripped.get(pid, "")

    def tripped_set(self) -> set[str]:
        with self._lock:
            return set(self._tripped.keys())

    def note_status(self, platform_id: str, status: str) -> bool:
        """若 status 属于熔断类则 trip，返回是否新熔断。"""
        st = (status or "").lower()
        for marker in _TRIP_STATUSES:
            if marker in st:
                if not self.is_tripped(platform_id):
                    self.trip(platform_id, st)
                    return True
                return False
        return False


@dataclass
class PlatformJobResult:
    platform_id: str
    ok: bool
    payload: Any = None
    error: str = ""
    cancelled: bool = False
    elapsed_ms: float = 0.0
    trip_status: str = ""


@dataclass
class SchedulerStats:
    submitted: int = 0
    completed: int = 0
    cancelled: int = 0
    tripped: int = 0
    max_inflight: int = 0
    domain_waits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "completed": self.completed,
            "cancelled": self.cancelled,
            "tripped": self.tripped,
            "max_inflight": self.max_inflight,
            "domain_waits": self.domain_waits,
        }


class BoundedPlatformScheduler:
    """
    有界平台调度器。

    - max_platforms：同时执行的平台任务上限（默认 2）
    - 每域名一把锁（同域串行）
    - 每任务在独立线程执行，禁止共享 page
    """

    def __init__(
        self,
        *,
        max_platforms: int = 2,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.max_platforms = max(1, min(4, int(max_platforms or 2)))
        self.breaker = breaker or CircuitBreaker()
        self._global_sem = threading.Semaphore(self.max_platforms)
        self._domain_locks: dict[str, threading.Lock] = {}
        self._domain_guard = threading.Lock()
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self.stats = SchedulerStats()
        self._pool: ThreadPoolExecutor | None = None

    def _domain_lock(self, platform_id: str) -> threading.Lock:
        dom = platform_domain(platform_id)
        with self._domain_guard:
            lk = self._domain_locks.get(dom)
            if lk is None:
                lk = threading.Lock()
                self._domain_locks[dom] = lk
            return lk

    def _track_inflight(self, delta: int) -> None:
        with self._inflight_lock:
            self._inflight += delta
            if self._inflight > self.stats.max_inflight:
                self.stats.max_inflight = self._inflight

    def submit_platform_jobs(
        self,
        platform_ids: list[str],
        worker_fn: Callable[[str, CancelToken], Any],
        *,
        cancel_token: CancelToken | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_result: Callable[[PlatformJobResult], None] | None = None,
    ) -> list[PlatformJobResult]:
        """
        对每个 platform_id 提交 worker_fn(pid, token)。
        should_stop：如「已满 K」返回 True → 取消未开始任务。
        """
        token = cancel_token or CancelToken()
        plats = [normalize_platform_id(p) for p in platform_ids if p]
        # 去重保序
        seen: set[str] = set()
        ordered: list[str] = []
        for p in plats:
            if p not in seen:
                seen.add(p)
                ordered.append(p)

        results: list[PlatformJobResult] = []
        results_lock = threading.Lock()

        def _run(pid: str) -> PlatformJobResult:
            t0 = time.perf_counter()
            if token.is_cancelled() or (should_stop and should_stop()):
                self.stats.cancelled += 1
                return PlatformJobResult(
                    platform_id=pid,
                    ok=False,
                    cancelled=True,
                    error=token.reason or "cancelled_before_start",
                    elapsed_ms=0.0,
                )
            if self.breaker.is_tripped(pid):
                self.stats.tripped += 1
                return PlatformJobResult(
                    platform_id=pid,
                    ok=False,
                    error=f"circuit_open:{self.breaker.reason(pid)}",
                    trip_status=self.breaker.reason(pid),
                    elapsed_ms=0.0,
                )

            dom_lock = self._domain_lock(pid)
            got_dom = dom_lock.acquire(blocking=False)
            if not got_dom:
                self.stats.domain_waits += 1
                dom_lock.acquire(blocking=True)
            self._global_sem.acquire()
            self._track_inflight(1)
            self.stats.submitted += 1
            try:
                if token.is_cancelled() or (should_stop and should_stop()):
                    self.stats.cancelled += 1
                    return PlatformJobResult(
                        platform_id=pid,
                        ok=False,
                        cancelled=True,
                        error=token.reason or "cancelled",
                        elapsed_ms=(time.perf_counter() - t0) * 1000,
                    )
                payload = worker_fn(pid, token)
                # 从 QuoteSet.attempts 嗅探熔断
                trip = ""
                try:
                    for a in getattr(payload, "attempts", None) or []:
                        st = str(a.get("status") or "")
                        if self.breaker.note_status(pid, st):
                            trip = st
                            self.stats.tripped += 1
                            break
                except Exception:
                    pass
                res = PlatformJobResult(
                    platform_id=pid,
                    ok=True,
                    payload=payload,
                    trip_status=trip,
                    elapsed_ms=(time.perf_counter() - t0) * 1000,
                )
                self.stats.completed += 1
                return res
            except Exception as e:
                self.stats.completed += 1
                return PlatformJobResult(
                    platform_id=pid,
                    ok=False,
                    error=f"{type(e).__name__}:{e}",
                    elapsed_ms=(time.perf_counter() - t0) * 1000,
                )
            finally:
                self._track_inflight(-1)
                self._global_sem.release()
                dom_lock.release()

        # 线程池大小 = 平台数，真正并发由 semaphore 限制为 max_platforms
        with ThreadPoolExecutor(max_workers=max(1, len(ordered))) as ex:
            futs: dict[Future, str] = {
                ex.submit(_run, pid): pid for pid in ordered
            }
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception as e:
                    r = PlatformJobResult(
                        platform_id=futs[fut],
                        ok=False,
                        error=str(e),
                    )
                with results_lock:
                    results.append(r)
                if on_result:
                    try:
                        on_result(r)
                    except Exception:
                        pass
                if should_stop and should_stop():
                    token.cancel("full_k_or_stop")
        return results


def merge_platform_quote_sets(
    item_id: str,
    parts: list[Any],
    *,
    k: int,
) -> Any:
    """合并各平台 QuoteSet → 单条（正式价截断到 K）。"""
    from .models import Quote, QuoteSet

    quotes: list[Quote] = []
    reviews: list[Quote] = []
    market: list[Quote] = []
    web: list[Quote] = []
    leads: list[Quote] = []
    attempts: list[dict] = []
    errors: list[str] = []
    for qs in parts:
        if qs is None:
            continue
        for q in getattr(qs, "quotes", None) or []:
            quotes.append(q)
        for q in getattr(qs, "review_candidates", None) or []:
            reviews.append(q)
        for q in getattr(qs, "market_refs", None) or []:
            market.append(q)
        for q in getattr(qs, "web_refs", None) or []:
            web.append(q)
        for q in getattr(qs, "supplier_leads", None) or []:
            leads.append(q)
        attempts.extend(list(getattr(qs, "attempts", None) or []))
        if getattr(qs, "error", None):
            errors.append(str(qs.error))

    def _dedupe(rows: list[Quote]) -> list[Quote]:
        """去掉同一报价行被同义检索词重复抓取的副本。"""
        seen: set[tuple[str, str, str, str]] = set()
        out: list[Quote] = []
        for q in rows:
            raw_url = str(getattr(q, "detail_url", "") or q.url or "")
            clean_url = raw_url.split("#", 1)[0].rstrip("/").lower()
            sku = str(getattr(q, "sku", "") or "").strip().lower()
            supplier = str(getattr(q, "supplier", "") or "").strip().lower()
            try:
                price = f"{float(q.price or 0):.4f}"
            except Exception:
                price = "0"
            identity = sku or clean_url or str(q.title or "").strip().lower()
            key = (str(q.platform or "").lower(), identity, supplier, price)
            if key in seen:
                continue
            seen.add(key)
            out.append(q)
        return out

    quotes = _dedupe(quotes)
    reviews = _dedupe(reviews)
    market = _dedupe(market)
    web = _dedupe(web)
    leads = _dedupe(leads)

    # 正式价：按价格排序截断 K
    def _pkey(q: Quote) -> float:
        try:
            return float(q.price or 1e18)
        except Exception:
            return 1e18

    quotes.sort(key=_pkey)
    quotes = quotes[: max(1, int(k or 1))]
    for i, q in enumerate(quotes, 1):
        q.rank = i

    if len(quotes) >= k:
        status = "full_k"
    elif quotes:
        status = "partial"
    elif reviews or market or web or leads:
        status = "need_review"
    else:
        status = "no_match"

    return QuoteSet(
        item_id=item_id,
        quotes=quotes,
        review_candidates=reviews[:8],
        market_refs=market[:5],
        web_refs=web[:5],
        supplier_leads=leads[:5],
        status=status,
        attempts=attempts,
        error=" | ".join(errors[:3]) if errors else "",
    )


def profile_dir_for_platform(base_profile: Path, platform_id: str) -> Path:
    """每平台独立 profile，禁止多线程共 user-data-dir。"""
    pid = normalize_platform_id(platform_id) or "unknown"
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in pid)
    return Path(base_profile) / f"sched-{safe}"
