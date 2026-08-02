"""
共享候选池（Phase 3）。

键必须含：platform、target_region_code、query、（可选）price_type/tax_mode。
禁止跨地区复用。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any


def normalize_query_key(query: str) -> str:
    q = re.sub(r"\s+", " ", (query or "").strip().lower())
    # 压缩重复 DN 片段：dn100 dn100 → dn100
    q = re.sub(r"\b(dn\d+)\s+\1\b", r"\1", q, flags=re.I)
    return q


def pool_cache_key(
    platform: str,
    region_code: str,
    query: str,
    *,
    price_type: str = "market",
    price_date: str = "",
    tax_mode: str = "",
) -> str:
    parts = [
        (platform or "").strip().lower(),
        (region_code or "UNSPECIFIED").strip(),
        normalize_query_key(query),
        (price_type or "market").strip().lower(),
        (price_date or "").strip(),
        (tax_mode or "").strip().lower(),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class CandidatePool:
    """
    进程内共享池 + 可选磁盘缓存。
    值：list[dict] 候选（旧 platforms 结构）。
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        use_disk: bool = False,
        ttl_seconds: int = 7 * 24 * 3600,
    ) -> None:
        self._mem: dict[str, list[dict[str, Any]]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self.root = Path(root) if root else None
        self.use_disk = bool(use_disk and self.root)
        self.ttl_seconds = int(ttl_seconds)

    def _disk_path(self, key: str) -> Path | None:
        if not self.use_disk or not self.root:
            return None
        d = self.root / "data" / "mapping-cache" / "candidate-pool"
        return d / f"{key}.json"

    def make_key(
        self,
        platform: str,
        region_code: str,
        query: str,
        **kwargs: Any,
    ) -> str:
        return pool_cache_key(platform, region_code, query, **kwargs)

    def get(self, key: str) -> list[dict[str, Any]] | None:
        if not key:
            return None
        if key in self._mem:
            return list(self._mem[key])
        path = self._disk_path(key)
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                ts = float(data.get("ts") or 0)
                if self.ttl_seconds > 0 and ts > 0:
                    if time.time() - ts > self.ttl_seconds:
                        return None
                hits = data.get("candidates")
                if isinstance(hits, list):
                    self._mem[key] = [x for x in hits if isinstance(x, dict)]
                    return list(self._mem[key])
            except Exception:
                return None
        return None

    def put(
        self,
        key: str,
        candidates: list[dict[str, Any]] | None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not key:
            return
        cands = [dict(x) for x in (candidates or []) if isinstance(x, dict)]
        self._mem[key] = cands
        if meta:
            self._meta[key] = dict(meta)
        path = self._disk_path(key)
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "meta": meta or {},
                            "candidates": cands,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except Exception:
                pass

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def stats(self) -> dict[str, Any]:
        return {
            "memory_keys": len(self._mem),
            "keys": list(self._mem.keys())[:50],
        }

    def clear(self) -> None:
        self._mem.clear()
        self._meta.clear()
