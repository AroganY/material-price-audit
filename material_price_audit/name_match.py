"""
名称实体匹配流水线（Phase 4）。

顺序：
  用户确认别名 / 本地库
  → 规则同物
  → 规则预筛（明显不同）
  → 灰区批量 LLM（整轮同名只判一次）

输出决策：same | possible | different | unknown
禁止：每个候选单独调 LLM；possible 不得当正式价（由 inquiry 门禁）。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .name_aliases import normalize_name_key
from .semantic_review import (
    MatchReviewLimiter,
    prepare_item_name_decisions,
    resolve_name_without_ai,
)


def title_key(title: str) -> str:
    return normalize_name_key(title or "") or re.sub(
        r"\s+", "", (title or "").strip()
    ).lower()


def pair_cache_key(inquiry_name: str, candidate_name: str) -> str:
    a = normalize_name_key(inquiry_name)
    b = title_key(candidate_name)
    raw = f"{a}||{b}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:28]


@dataclass
class NameDecision:
    decision: str  # same|possible|different|unknown
    source: str = ""
    note: str = ""
    confidence: float = 0.0
    candidate_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "source": self.source,
            "note": self.note,
            "confidence": self.confidence,
            "candidate_name": self.candidate_name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "NameDecision":
        d = d or {}
        dec = str(d.get("decision") or "unknown").lower().strip()
        if dec not in ("same", "possible", "different", "unknown"):
            dec = "unknown"
        return cls(
            decision=dec,
            source=str(d.get("source") or ""),
            note=str(d.get("note") or ""),
            confidence=float(d.get("confidence") or 0),
            candidate_name=str(d.get("candidate_name") or ""),
        )


class NameDecisionCache:
    """
    整轮任务名称判决缓存：同一 (询价品名, 候选名) 只判一次。
    可选落盘到 data/mapping-cache/name-decisions/。
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        use_disk: bool = True,
    ) -> None:
        self.root = Path(root) if root else None
        self.use_disk = bool(use_disk and self.root)
        self._mem: dict[str, NameDecision] = {}
        self._lock = threading.RLock()
        # 并行平台可能同时遇到同一品名；批量判决做 single-flight。
        self._prepare_lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _disk_path(self, key: str) -> Path | None:
        if not self.use_disk or not self.root:
            return None
        return (
            self.root
            / "data"
            / "mapping-cache"
            / "name-decisions"
            / f"{key}.json"
        )

    def get(self, inquiry_name: str, candidate_name: str) -> NameDecision | None:
        key = pair_cache_key(inquiry_name, candidate_name)
        with self._lock:
            if key in self._mem:
                cached = self._mem[key]
                if cached.decision != "unknown":
                    self.hits += 1
                    return cached
            path = self._disk_path(key)
            if path and path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    dec = NameDecision.from_dict(data)
                    if dec.decision != "unknown":
                        self._mem[key] = dec
                        self.hits += 1
                        return dec
                except Exception:
                    pass
            self.misses += 1
            return None

    def put(
        self, inquiry_name: str, candidate_name: str, decision: NameDecision
    ) -> None:
        key = pair_cache_key(inquiry_name, candidate_name)
        with self._lock:
            self._mem[key] = decision
            path = self._disk_path(key)
            if path:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        json.dumps(decision.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

    def prepare(
        self,
        *,
        inquiry_name: str,
        candidate_titles: list[str],
        settings: Any = None,
        root: Path | None = None,
        limiter: MatchReviewLimiter | None = None,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        with self._prepare_lock:
            return self._prepare_unlocked(
                inquiry_name=inquiry_name,
                candidate_titles=candidate_titles,
                settings=settings,
                root=root,
                limiter=limiter,
                log=log,
            )

    def _prepare_unlocked(
        self,
        *,
        inquiry_name: str,
        candidate_titles: list[str],
        settings: Any = None,
        root: Path | None = None,
        limiter: MatchReviewLimiter | None = None,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """
        对标题列表判决，优先整轮缓存；仅未缓存的送 prepare_item_name_decisions。
        返回与 semantic_review 相同的 {norm_key: dict}。
        """
        out: dict[str, dict[str, Any]] = {}
        need: list[str] = []
        seen: set[str] = set()
        for t in candidate_titles or []:
            tt = re.sub(r"\s+", " ", str(t or "")).strip()
            if len(tt) < 2:
                continue
            k = title_key(tt)
            if not k or k in seen:
                continue
            seen.add(k)
            cached = self.get(inquiry_name, tt)
            if cached is not None:
                out[k] = cached.to_dict()
                continue
            need.append(tt)

        if need:
            judged = prepare_item_name_decisions(
                inquiry_name=inquiry_name,
                candidate_titles=need,
                settings=settings,
                root=root or self.root,
                limiter=limiter,
                log=log,
            )
            for t in need:
                k = title_key(t)
                row = judged.get(k)
                if not row:
                    # 再试 normalize 键
                    row = judged.get(normalize_name_key(t)) or {
                        "decision": "unknown",
                        "source": "miss",
                        "note": "",
                        "confidence": 0.0,
                        "candidate_name": t,
                    }
                dec = NameDecision.from_dict(row)
                if not dec.candidate_name:
                    dec.candidate_name = t
                # AI 关闭/预算用尽得到的 unknown 不是结论，不得永久缓存。
                if dec.decision != "unknown":
                    self.put(inquiry_name, t, dec)
                out[k] = dec.to_dict()
        return out

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "size": len(self._mem)}


def decide_name_quick(
    inquiry_name: str,
    candidate_name: str,
    root: Path | None = None,
) -> NameDecision:
    """无 LLM 快速判决（规则+本地库）。"""
    dec, src, note = resolve_name_without_ai(
        inquiry_name, candidate_name, root=root
    )
    if dec == "same":
        return NameDecision("same", src, note, 1.0, candidate_name)
    if dec == "different":
        return NameDecision("different", src, note, 1.0, candidate_name)
    return NameDecision("unknown", src, note, 0.0, candidate_name)


def allows_formal_quote(name_decision: str) -> bool:
    """only same 可进正式价；possible/unknown/different 均不可。"""
    return (name_decision or "").lower().strip() == "same"


def allows_spec_extract(name_decision: str) -> bool:
    """different 禁止进规格抽取；same/possible/unknown 可以。"""
    return (name_decision or "").lower().strip() != "different"
