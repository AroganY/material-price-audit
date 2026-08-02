"""In-memory job state for the guided inquiry UI."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobState:
    phase: str = "idle"  # idle|ready|parsing|login|running|paused|done|error|stopped
    message: str = "等待开始"
    platforms: list[str] = field(default_factory=list)
    quotes_per_item: int = 3
    match_mode: str = "practical"  # strict | practical | loose
    limit: int = 0
    skip_login: bool = False
    # 询价范围：all | first_n | sheets | ids
    item_scope_mode: str = "all"
    item_scope_n: int = 0
    item_scope_sheets: list[str] = field(default_factory=list)
    item_scope_ids: list[str] = field(default_factory=list)
    input_path: str = ""
    total: int = 0
    current: int = 0
    current_name: str = ""
    full_k: int = 0
    partial: int = 0
    need_review: int = 0
    no_match: int = 0
    error: str = ""
    logs: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    result_path: str = ""
    evidence_path: str = ""
    rfq_path: str = ""
    schema_preview: list[dict] = field(default_factory=list)
    items_preview: list[dict] = field(default_factory=list)
    item_results: list[dict] = field(default_factory=list)
    # 按原表 sheet 分组的完整结果（前端结果页用）
    result_by_sheet: list[dict] = field(default_factory=list)
    # AI 参与情况（配置是否开 + 实际调用次数）
    llm_status: dict = field(default_factory=dict)
    # 任务控制：run | pause | stop
    control: str = "run"
    # 运行中 LLM 开关（None=跟随 settings；True/False=临时覆盖）
    llm_runtime_enabled: bool | None = None
    # 继续询价：跳过已有 full_k/partial
    continue_mode: bool = False
    # 任务用量/进度统计
    job_stats: dict = field(default_factory=dict)
    # 本任务唯一 id + 漏斗/平台统计（与历史隔离）
    run_id: str = ""
    funnel: dict = field(default_factory=dict)
    platform_stats: dict = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reset_job_control(self, *, llm_default: bool | None = None) -> None:
        with self.lock:
            self.control = "run"
            if llm_default is not None:
                self.llm_runtime_enabled = bool(llm_default)
            self.job_stats = {
                "items_done": 0,
                "items_skipped": 0,
                "pause_count": 0,
                "llm_calls_session": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "started_ts": time.time(),
            }

    def set_control(self, action: str) -> dict[str, Any]:
        """pause | resume | stop"""
        action = (action or "").strip().lower()
        with self.lock:
            if action == "pause":
                if self.phase not in ("running", "login"):
                    return {"ok": False, "error": "当前没有运行中的询价"}
                self.control = "pause"
                self.message = "已请求暂停（当前材料结束后暂停）"
                self.logs.append(
                    f"[{time.strftime('%H:%M:%S')}] 用户请求暂停询价"
                )
                return {"ok": True, "control": "pause", "phase": self.phase}
            if action == "resume":
                if self.control not in ("pause", "paused") and self.phase != "paused":
                    return {"ok": False, "error": "当前不在暂停状态"}
                self.control = "run"
                self.phase = "running"
                self.message = "继续询价…"
                stats = dict(self.job_stats or {})
                stats["pause_count"] = int(stats.get("pause_count") or 0)
                self.job_stats = stats
                self.logs.append(
                    f"[{time.strftime('%H:%M:%S')}] 用户继续询价"
                )
                return {"ok": True, "control": "run", "phase": self.phase}
            if action == "stop":
                if self.phase not in ("running", "paused", "login"):
                    return {"ok": False, "error": "当前没有可停止的询价"}
                self.control = "stop"
                self.message = "已请求停止（当前材料结束后停止）"
                self.logs.append(
                    f"[{time.strftime('%H:%M:%S')}] 用户请求停止询价"
                )
                return {"ok": True, "control": "stop", "phase": self.phase}
            return {"ok": False, "error": f"未知控制动作: {action}"}

    def set_llm_runtime(self, enabled: bool) -> dict[str, Any]:
        with self.lock:
            self.llm_runtime_enabled = bool(enabled)
            st = dict(self.llm_status or {})
            st["runtime_enabled"] = bool(enabled)
            st["runtime_note"] = "询价中临时" + ("开启" if enabled else "关闭")
            self.llm_status = st
            self.logs.append(
                f"[{time.strftime('%H:%M:%S')}] 询价中 AI 已"
                + ("开启" if enabled else "关闭")
            )
            return {
                "ok": True,
                "llm_runtime_enabled": self.llm_runtime_enabled,
                "llm_status": dict(self.llm_status),
            }

    def get_control(self) -> str:
        with self.lock:
            return self.control or "run"

    def llm_enabled_now(self, settings_enabled: bool) -> bool:
        with self.lock:
            if self.llm_runtime_enabled is not None:
                return bool(self.llm_runtime_enabled)
            return bool(settings_enabled)

    def _record_llm_unlocked(
        self,
        role: str,
        *,
        ok: bool,
        detail: str = "",
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        """记录一次 AI 实际调用（调用方已持有 self.lock）。"""
        st = dict(self.llm_status or {})
        st.setdefault("enabled", True)
        st.setdefault("model", model or st.get("model") or "")
        pt = int(prompt_tokens or 0)
        ct = int(completion_tokens or 0)
        tt = int(total_tokens or 0) or (pt + ct)
        tokens = dict(st.get("tokens") or {})
        tokens["prompt"] = int(tokens.get("prompt") or 0) + pt
        tokens["completion"] = int(tokens.get("completion") or 0) + ct
        tokens["total"] = int(tokens.get("total") or 0) + tt
        st["tokens"] = tokens
        calls = list(st.get("calls") or [])
        calls.append(
            {
                "role": role,
                "ok": bool(ok),
                "detail": (detail or "")[:200],
                "ts": time.strftime("%H:%M:%S"),
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
            }
        )
        st["calls"] = calls[-80:]
        counts = dict(st.get("counts") or {})
        key = f"{role}_{'ok' if ok else 'fail'}"
        counts[key] = int(counts.get(key) or 0) + 1
        counts["total"] = int(counts.get("total") or 0) + 1
        st["counts"] = counts
        st["last"] = calls[-1] if calls else None
        acts = list(st.get("activity") or [])
        acts.append(
            {
                "ts": time.strftime("%H:%M:%S"),
                "kind": "api",
                "role": role,
                "ok": bool(ok),
                "detail": (detail or "API 调用")[:200],
                "tokens": tt,
                "prompt_tokens": pt,
                "completion_tokens": ct,
            }
        )
        st["activity"] = acts[-40:]
        self.llm_status = st
        # session job_stats
        stats = dict(self.job_stats or {})
        stats["llm_calls_session"] = int(stats.get("llm_calls_session") or 0) + 1
        stats["prompt_tokens"] = int(stats.get("prompt_tokens") or 0) + pt
        stats["completion_tokens"] = int(stats.get("completion_tokens") or 0) + ct
        stats["total_tokens"] = int(stats.get("total_tokens") or 0) + tt
        self.job_stats = stats
        tag = "✓" if ok else "×"
        line = f"[{time.strftime('%H:%M:%S')}] [AI·API] {tag} {role} · {tt} tok"
        if detail:
            line += f" — {detail[:100]}"
        self.logs.append(line)
        self.logs = self.logs[-200:]

    def record_llm(
        self,
        role: str,
        *,
        ok: bool,
        detail: str = "",
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        """记录一次 AI 实际调用（识表 / 语义复核 / token）。"""
        with self.lock:
            self._record_llm_unlocked(
                role,
                ok=ok,
                detail=detail,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

    def record_llm_usage(self, usage: dict[str, Any]) -> None:
        """从 API usage 回写（schema_map hook）。"""
        usage = usage or {}
        role = str(usage.get("role") or "chat").strip() or "chat"
        pt = int(
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or 0
        )
        ct = int(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0
        )
        tt = int(usage.get("total_tokens") or 0) or (pt + ct)
        with self.lock:
            self._record_llm_unlocked(
                role,
                ok=bool(usage.get("ok", True)),
                detail=str(usage.get("detail") or "API 调用"),
                model=str(usage.get("model") or ""),
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=tt,
            )

    def log(self, msg: str) -> None:
        with self.lock:
            ts = time.strftime("%H:%M:%S")
            line = f"[{ts}] {msg}"
            self.logs.append(line)
            self.logs = self.logs[-200:]
            self.message = msg
            # 询价中进入登录等待：切 phase=login，向导显示「我已登录，继续」
            text = str(msg or "")
            if self.phase in ("running", "login") and any(
                k in text
                for k in (
                    "等待登录",
                    "等待 [",
                    "LOGIN_WAIT",
                    "需重登",
                    "重新登录",
                    "请在弹出浏览器",
                    "请在浏览器完成登录",
                )
            ):
                self.phase = "login"
            elif self.phase == "login" and any(
                k in text
                for k in (
                    "登录校验通过",
                    "会话有效",
                    "二次校验通过",
                    "正在搜索",
                    "→ 正在",
                )
            ):
                self.phase = "running"

    def push_event(self, ev: dict[str, Any]) -> None:
        with self.lock:
            self.events.append(ev)
            self.events = self.events[-300:]
            t = ev.get("type")
            if t == "item_start":
                self.current = int(ev.get("index") or self.current)
                self.total = int(ev.get("total") or self.total)
                name = str(ev.get("name") or "")
                spec = str(ev.get("spec") or "")
                # 进度条旁展示完整名称+规格
                self.current_name = (
                    f"{name}｜规格：{spec}" if (name and spec) else (name or spec)
                )
                self.message = str(ev.get("message") or self.message)
            elif t == "item_done":
                st = ev.get("status")
                if st == "full_k":
                    self.full_k += 1
                elif st == "partial":
                    self.partial += 1
                elif st == "need_review":
                    self.need_review += 1
                elif st == "no_match":
                    self.no_match += 1
                stats = dict(self.job_stats or {})
                stats["items_done"] = int(stats.get("items_done") or 0) + 1
                self.job_stats = stats
                row = {
                    "id": ev.get("id"),
                    "name": ev.get("name") or "",
                    "status": st,
                    "fail_reason": ev.get("fail_reason") or "",
                    "quotes": ev.get("quotes"),
                    "message": ev.get("message"),
                    "sheet": ev.get("sheet") or "",
                    "row": ev.get("row"),
                    # 名称/规格完整保留，供结果面板完整展示
                    "spec": ev.get("spec") if ev.get("spec") is not None else "",
                    "brand": ev.get("brand") or "",
                    "unit": ev.get("unit") or "",
                    "qty": ev.get("qty") or 0,
                    "submit": ev.get("submit"),
                    "region_raw": ev.get("region_raw") or "",
                    "quote_list": list(ev.get("quote_list") or []),
                    "review_list": list(ev.get("review_list") or []),
                    "market_list": list(ev.get("market_list") or []),
                    "web_list": list(ev.get("web_list") or []),
                    "supplier_list": list(ev.get("supplier_list") or []),
                    "match_via_llm": bool(ev.get("match_via_llm")),
                    "audit": ev.get("audit"),
                    "platform": ev.get("platform") or "",
                    "url": ev.get("url") or "",
                    "price": ev.get("price"),
                    "title": ev.get("title") or "",
                    "run_id": ev.get("run_id") or self.run_id or "",
                }
                # 无合格价时，展示电商参考价到主价字段，避免「成功了却像没结果」
                if (
                    not row.get("price")
                    and row.get("market_list")
                    and row["market_list"][0].get("price") is not None
                ):
                    m0 = row["market_list"][0]
                    row["price"] = m0.get("price")
                    row["platform"] = m0.get("platform") or row.get("platform")
                    row["title"] = m0.get("title") or row.get("title")
                    row["url"] = m0.get("url") or row.get("url")
                # 同 id 更新，否则追加
                replaced = False
                for i, old in enumerate(self.item_results):
                    if old.get("id") and old.get("id") == row.get("id"):
                        self.item_results[i] = row
                        replaced = True
                        break
                if not replaced:
                    self.item_results.append(row)
                self.item_results = self.item_results[-2000:]
                self.message = str(ev.get("message") or self.message)
            elif t == "login":
                self.phase = "login"
                self.message = str(ev.get("message") or "请登录")
            elif t in ("done", "stopped"):
                if t == "done":
                    self.phase = "done"
                elif self.phase not in ("stopped",):
                    self.phase = "stopped" if t == "stopped" else self.phase
                self.full_k = int(ev.get("full_k") or self.full_k)
                self.partial = int(ev.get("partial") or self.partial)
                self.need_review = int(ev.get("need_review") or self.need_review)
                self.no_match = int(ev.get("no_match") or self.no_match)
                self.message = str(ev.get("message") or ("完成" if t == "done" else "已停止"))
                self.finished_at = time.time()
                stats = dict(self.job_stats or {})
                stats["ended_ts"] = self.finished_at
                if ev.get("run_id"):
                    self.run_id = str(ev.get("run_id"))
                    stats["run_id"] = self.run_id
                if ev.get("funnel"):
                    self.funnel = dict(ev.get("funnel") or {})
                    stats["funnel"] = self.funnel
                if ev.get("platform_stats"):
                    self.platform_stats = dict(ev.get("platform_stats") or {})
                    stats["platform_stats"] = self.platform_stats
                if ev.get("fail_reason_counts"):
                    stats["fail_reason_counts"] = dict(ev.get("fail_reason_counts") or {})
                self.job_stats = stats
                if ev.get("result_by_sheet"):
                    self.result_by_sheet = list(ev.get("result_by_sheet") or [])
                if ev.get("item_results"):
                    self.item_results = list(ev.get("item_results") or [])
            elif t == "start":
                self.phase = "running"
                self.control = "run"
                self.total = int(ev.get("total") or 0)
                self.full_k = self.partial = self.need_review = self.no_match = 0
                self.item_results = []
                self.result_by_sheet = []
                self.funnel = {}
                self.platform_stats = {}
                self.finished_at = 0.0
                self.message = str(ev.get("message") or "运行中")
                if ev.get("run_id"):
                    self.run_id = str(ev.get("run_id"))
            elif t == "paused":
                self.phase = "paused"
                self.control = "paused"
                self.message = str(ev.get("message") or "已暂停")
                stats = dict(self.job_stats or {})
                stats["pause_count"] = int(stats.get("pause_count") or 0) + 1
                # 暂停时冻结计时（继续后从现在重算累计显示用 pause 不涨秒）
                stats["paused_ts"] = time.time()
                self.job_stats = stats
            elif t == "resumed":
                self.phase = "running"
                self.control = "run"
                self.message = str(ev.get("message") or "已继续")
                stats = dict(self.job_stats or {})
                # 把暂停时长加到 started_ts 上，使已用时不包含暂停
                paused_ts = float(stats.get("paused_ts") or 0)
                if paused_ts > 0:
                    started = float(stats.get("started_ts") or 0)
                    if started > 0:
                        stats["started_ts"] = started + (time.time() - paused_ts)
                    stats.pop("paused_ts", None)
                self.job_stats = stats
            elif t == "stopped":
                self.phase = "stopped"
                self.control = "stop"
                self.message = str(ev.get("message") or "已停止")
                self.finished_at = time.time()
                stats = dict(self.job_stats or {})
                stats["ended_ts"] = self.finished_at
                stats.pop("paused_ts", None)
                self.job_stats = stats
                if ev.get("full_k") is not None:
                    self.full_k = int(ev.get("full_k") or self.full_k)
                    self.partial = int(ev.get("partial") or self.partial)
                    self.need_review = int(ev.get("need_review") or self.need_review)
                    self.no_match = int(ev.get("no_match") or self.no_match)
                if ev.get("result_by_sheet"):
                    self.result_by_sheet = list(ev.get("result_by_sheet") or [])
                if ev.get("item_results"):
                    self.item_results = list(ev.get("item_results") or [])
            elif t == "llm":
                # 业务层用途说明；Token/API 次数只由 usage hook 统计（真请求）
                pt = int(ev.get("prompt_tokens") or 0)
                ct = int(ev.get("completion_tokens") or 0)
                tt = int(ev.get("total_tokens") or 0)
                if pt or ct or tt:
                    self._record_llm_unlocked(
                        str(ev.get("role") or "unknown"),
                        ok=bool(ev.get("ok")),
                        detail=str(ev.get("detail") or ""),
                        model=str(ev.get("model") or ""),
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        total_tokens=tt,
                    )
                else:
                    role = str(ev.get("role") or "unknown")
                    ok = bool(ev.get("ok"))
                    detail = str(ev.get("detail") or "")
                    is_cache = "缓存" in detail
                    tag = "✓" if ok else "×"
                    # 明确标注：这不是 Token 消耗
                    kind = "缓存" if is_cache else "用途"
                    line = (
                        f"[{time.strftime('%H:%M:%S')}] [AI·{kind}] {tag} {role}"
                    )
                    if detail:
                        line += f" — {detail[:120]}"
                    if not is_cache:
                        line += " ·（本条未附带Token；若无 [AI·API] 则可能未真请求）"
                    else:
                        line += " · 不计Token"
                    self.logs.append(line)
                    self.logs = self.logs[-200:]
                    st = dict(self.llm_status or {})
                    st["last_role"] = role
                    st["last_detail"] = detail[:200]
                    acts = list(st.get("activity") or [])
                    acts.append(
                        {
                            "ts": time.strftime("%H:%M:%S"),
                            "kind": "cache" if is_cache else "note",
                            "role": role,
                            "ok": ok,
                            "detail": detail[:200],
                            "tokens": 0,
                        }
                    )
                    st["activity"] = acts[-40:]
                    self.llm_status = st
            msg = ev.get("message")
            if msg and t != "llm":
                ts = time.strftime("%H:%M:%S")
                self.logs.append(f"[{ts}] {msg}")
                self.logs = self.logs[-200:]

    def _sheet_counts_unlocked(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for it in self.items_preview or []:
            sh = str(it.get("sheet") or "(未命名)")
            counts[sh] = counts.get(sh, 0) + 1
        return [{"sheet": k, "count": v} for k, v in counts.items()]

    def _selected_count_unlocked(self) -> int:
        mode = (self.item_scope_mode or "all").lower()
        n_all = len(self.items_preview or [])
        if mode == "first_n":
            n = int(self.item_scope_n or self.limit or 0)
            if n <= 0:
                return n_all
            return min(n, n_all)
        if mode == "sheets":
            want = set(self.item_scope_sheets or [])
            if not want:
                return 0
            return sum(
                1 for it in (self.items_preview or []) if str(it.get("sheet") or "") in want
            )
        if mode == "ids":
            want = set(self.item_scope_ids or [])
            return sum(1 for it in (self.items_preview or []) if str(it.get("id") or "") in want)
        return n_all

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            base = {
                "phase": self.phase,
                "message": self.message,
                "platforms": list(self.platforms),
                "quotes_per_item": self.quotes_per_item,
                "match_mode": self.match_mode or "practical",
                "limit": self.limit,
                "skip_login": self.skip_login,
                "control": self.control or "run",
                "llm_runtime_enabled": self.llm_runtime_enabled,
                "job_stats": dict(self.job_stats or {}),
                "run_id": self.run_id or (self.job_stats or {}).get("run_id") or "",
                "funnel": dict(self.funnel or (self.job_stats or {}).get("funnel") or {}),
                "platform_stats": dict(
                    self.platform_stats
                    or (self.job_stats or {}).get("platform_stats")
                    or {}
                ),
                "fail_reason_counts": dict(
                    (self.job_stats or {}).get("fail_reason_counts")
                    or (self.funnel or {}).get("fail_reason_counts")
                    or {}
                ),
                "item_scope": {
                    "mode": self.item_scope_mode or "all",
                    "n": int(self.item_scope_n or 0),
                    "sheets": list(self.item_scope_sheets or []),
                    "ids": list(self.item_scope_ids or []),
                    "selected_count": self._selected_count_unlocked(),
                },
                "input_path": self.input_path,
                "total": self.total,
                "current": self.current,
                "current_name": self.current_name,
                "full_k": self.full_k,
                "partial": self.partial,
                "need_review": self.need_review,
                "no_match": self.no_match,
                "error": self.error,
                "logs": list(self.logs[-80:]),
                "schema_preview": list(self.schema_preview),
                # 轮询只带少量预览；完整列表见 /api/parse 与 /api/items
                "items_preview": list(self.items_preview[:40]),
                "items_count": len(self.items_preview) if self.items_preview else self.total,
                "sheet_counts": self._sheet_counts_unlocked(),
                "item_results": list(self.item_results),
                "result_by_sheet": list(self.result_by_sheet),
                "llm_status": dict(self.llm_status or {}),
                "result_path": self.result_path,
                "evidence_path": self.evidence_path,
                "rfq_path": self.rfq_path,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "progress_pct": (
                    int(100 * self.current / self.total) if self.total else 0
                ),
            }
        # 登录面板状态（独立锁，勿嵌套 self.lock）
        try:
            from .login_panel import LOGIN_PANEL

            base["login_panel"] = LOGIN_PANEL.snapshot()
        except Exception:
            base["login_panel"] = {"platforms": [], "verified": [], "pending": []}
        return base


STATE = JobState()
