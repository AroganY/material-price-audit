"""In-memory job state for the guided inquiry UI."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobState:
    phase: str = "idle"  # idle|ready|parsing|login|running|done|error
    message: str = "等待开始"
    platforms: list[str] = field(default_factory=list)
    quotes_per_item: int = 3
    limit: int = 0
    skip_login: bool = False
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
    started_at: float = 0.0
    finished_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, msg: str) -> None:
        with self.lock:
            ts = time.strftime("%H:%M:%S")
            line = f"[{ts}] {msg}"
            self.logs.append(line)
            self.logs = self.logs[-200:]
            self.message = msg

    def push_event(self, ev: dict[str, Any]) -> None:
        with self.lock:
            self.events.append(ev)
            self.events = self.events[-300:]
            t = ev.get("type")
            if t == "item_start":
                self.current = int(ev.get("index") or self.current)
                self.total = int(ev.get("total") or self.total)
                self.current_name = str(ev.get("name") or "")
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
                self.item_results.append(
                    {
                        "name": ev.get("name"),
                        "status": st,
                        "quotes": ev.get("quotes"),
                        "message": ev.get("message"),
                    }
                )
                self.item_results = self.item_results[-100:]
                self.message = str(ev.get("message") or self.message)
            elif t == "login":
                self.phase = "login"
                self.message = str(ev.get("message") or "请登录")
            elif t == "done":
                self.phase = "done"
                self.full_k = int(ev.get("full_k") or self.full_k)
                self.partial = int(ev.get("partial") or self.partial)
                self.need_review = int(ev.get("need_review") or self.need_review)
                self.no_match = int(ev.get("no_match") or self.no_match)
                self.message = str(ev.get("message") or "完成")
                self.finished_at = time.time()
            elif t == "start":
                self.phase = "running"
                self.total = int(ev.get("total") or 0)
                self.full_k = self.partial = self.need_review = self.no_match = 0
                self.item_results = []
                self.message = str(ev.get("message") or "运行中")
            msg = ev.get("message")
            if msg:
                ts = time.strftime("%H:%M:%S")
                self.logs.append(f"[{ts}] {msg}")
                self.logs = self.logs[-200:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            base = {
                "phase": self.phase,
                "message": self.message,
                "platforms": list(self.platforms),
                "quotes_per_item": self.quotes_per_item,
                "limit": self.limit,
                "skip_login": self.skip_login,
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
                "items_preview": list(self.items_preview[:30]),
                "items_count": len(self.items_preview) if self.items_preview else self.total,
                "item_results": list(self.item_results[-40:]),
                "result_path": self.result_path,
                "evidence_path": self.evidence_path,
                "rfq_path": self.rfq_path,
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
