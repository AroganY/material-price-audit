"""
本地品名同义 / 负向映射库（持久化）。

三级链路中的第二级：规则不能确认时，先查本库（Token=0），
再才进入 AI 批量灰区。同义通过后仍须 strict_name_spec_match。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

Relation = Literal["same", "different", "unknown"]

_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}  # root_str -> store

# 仅少量确定性高的内置例子（非庞大行业词表）
_BUILTIN: list[dict[str, Any]] = [
    {
        "canonical_name": "地埋灯",
        "aliases": ["埋地灯"],
        "source": "builtin",
        "confidence": 1.0,
    },
    {
        "canonical_name": "止回阀",
        "aliases": ["单向阀"],
        "source": "builtin",
        "confidence": 1.0,
    },
    {
        "canonical_name": "波纹补偿器",
        "aliases": ["波纹伸缩节"],
        "source": "builtin",
        "confidence": 0.95,
    },
]


def _store_path(root: Path) -> Path:
    return Path(root) / "data" / "mapping-cache" / "name-aliases.json"


def normalize_name_key(name: str) -> str:
    """品名键：折中文空格/去地名/装饰前缀/尾部规格数字，小写。"""
    try:
        from .matching import collapse_cjk_spaces, strip_geo_noise

        s = strip_geo_noise(collapse_cjk_spaces(name or ""))
    except Exception:
        s = (name or "").strip()
    s = re.sub(r"(?i)^(?:LED|成品|新型|进口|国产)+", "", s)
    # 去掉常见规格尾缀，便于「单向阀 DN100」命中「单向阀」映射
    s = re.sub(
        r"(?i)(?:DN|φ|Φ|PN)\s*\d+(?:\.\d+)?"
        r"|\d+(?:\.\d+)?\s*(?:W(?:/m)?|V|K|mm|MPa|A)"
        r"|IP\s*\d{2}"
        r"|[A-Z]{1,8}\d{2,}[A-Z0-9\-_]*",
        " ",
        s,
    )
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[（(].*?[）)]", "", s)
    s = re.sub(r"(?:型号|规格).*$", "", s)
    return s.lower()


def _empty_store() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": "",
        "entries": [],
        "negatives": [],
    }


def load_name_alias_store(root: Path | None) -> dict[str, Any]:
    if root is None:
        return _empty_store()
    key = str(Path(root).resolve())
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        path = _store_path(Path(root))
        store = _empty_store()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    store["entries"] = list(raw.get("entries") or [])
                    store["negatives"] = list(raw.get("negatives") or [])
                    store["version"] = int(raw.get("version") or 1)
            except Exception:
                pass
        # 合并内置（不覆盖用户/AI 已有边）
        existing_pairs = _positive_pair_set(store)
        for b in _BUILTIN:
            can = str(b.get("canonical_name") or "")
            for al in b.get("aliases") or []:
                pk = _pair_key(can, str(al))
                if pk in existing_pairs:
                    continue
                store["entries"].append(
                    {
                        "canonical_name": can,
                        "aliases": [str(al)],
                        "source": "builtin",
                        "confidence": float(b.get("confidence") or 1.0),
                        "confirmed_at": "",
                    }
                )
        _CACHE[key] = store
        return store


def save_name_alias_store(root: Path, store: dict[str, Any]) -> None:
    path = _store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = dict(store)
    store["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with _LOCK:
        _CACHE[str(Path(root).resolve())] = store


def _pair_key(a: str, b: str) -> tuple[str, str]:
    ka, kb = normalize_name_key(a), normalize_name_key(b)
    return (ka, kb) if ka <= kb else (kb, ka)


def _positive_pair_set(store: dict[str, Any]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for e in store.get("entries") or []:
        can = str(e.get("canonical_name") or "")
        for al in e.get("aliases") or []:
            if can and al:
                out.add(_pair_key(can, str(al)))
        # aliases 彼此也可视为同义（同一 canonical 下）
        als = [str(x) for x in (e.get("aliases") or []) if x]
        for i in range(len(als)):
            for j in range(i + 1, len(als)):
                out.add(_pair_key(als[i], als[j]))
    return out


def _negative_pair_set(store: dict[str, Any]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for n in store.get("negatives") or []:
        a, b = str(n.get("a") or ""), str(n.get("b") or "")
        if a and b:
            out.add(_pair_key(a, b))
    return out


def lookup_name_relation(
    name_a: str,
    name_b: str,
    root: Path | None,
) -> tuple[Relation, str]:
    """
    查同义/负向。返回 (same|different|unknown, 说明)。
    双向可查。
    """
    if not name_a or not name_b:
        return "unknown", ""
    ka, kb = normalize_name_key(name_a), normalize_name_key(name_b)
    if not ka or not kb:
        return "unknown", ""
    if ka == kb:
        return "same", "名称规范化后相同"
    store = load_name_alias_store(root)
    pk = _pair_key(name_a, name_b)
    if pk in _negative_pair_set(store):
        return "different", "本地负向映射"
    if pk in _positive_pair_set(store):
        # 找 source
        src = "local"
        for e in store.get("entries") or []:
            names = {normalize_name_key(str(e.get("canonical_name") or ""))}
            names |= {
                normalize_name_key(str(x)) for x in (e.get("aliases") or [])
            }
            if ka in names and kb in names:
                src = str(e.get("source") or "local")
                break
        return "same", f"本地同义库({src})"
    return "unknown", ""


def get_aliases_for_name(name: str, root: Path | None, *, max_n: int = 4) -> list[str]:
    """返回与 name 同义的其它写法（不含自身），用于检索扩展。"""
    kn = normalize_name_key(name)
    if not kn:
        return []
    store = load_name_alias_store(root)
    found: list[str] = []
    seen = {kn}
    for e in store.get("entries") or []:
        can = str(e.get("canonical_name") or "")
        als = [str(x) for x in (e.get("aliases") or []) if x]
        cluster = [can] + als if can else als
        keys = {normalize_name_key(x) for x in cluster}
        if kn not in keys:
            continue
        for x in cluster:
            kx = normalize_name_key(x)
            if not kx or kx in seen:
                continue
            seen.add(kx)
            found.append(x.strip())
            if len(found) >= max_n:
                return found
    return found


def confirm_same_names(
    name_a: str,
    name_b: str,
    root: Path,
    *,
    source: str = "user_confirmed",
    confidence: float = 1.0,
) -> dict[str, Any]:
    """人工/AI 确认同义 → 写入正向映射。"""
    a, b = (name_a or "").strip(), (name_b or "").strip()
    if not a or not b:
        return {"ok": False, "error": "名称不能为空"}
    if normalize_name_key(a) == normalize_name_key(b):
        return {"ok": True, "message": "已是同一名称"}
    store = load_name_alias_store(root)
    # 去掉负向
    store["negatives"] = [
        n
        for n in (store.get("negatives") or [])
        if _pair_key(str(n.get("a") or ""), str(n.get("b") or ""))
        != _pair_key(a, b)
    ]
    # 并入已有 entry 或新建
    ka, kb = normalize_name_key(a), normalize_name_key(b)
    merged = False
    for e in store.get("entries") or []:
        names = {normalize_name_key(str(e.get("canonical_name") or ""))}
        names |= {normalize_name_key(str(x)) for x in (e.get("aliases") or [])}
        if ka in names or kb in names:
            can = str(e.get("canonical_name") or a)
            als = list(e.get("aliases") or [])
            for x in (a, b):
                if normalize_name_key(x) == normalize_name_key(can):
                    continue
                if not any(normalize_name_key(x) == normalize_name_key(y) for y in als):
                    als.append(x)
            e["aliases"] = als
            e["source"] = source
            e["confidence"] = float(confidence)
            e["confirmed_at"] = datetime.now().isoformat(timespec="seconds")
            merged = True
            break
    if not merged:
        store.setdefault("entries", []).append(
            {
                "canonical_name": a,
                "aliases": [b],
                "source": source,
                "confidence": float(confidence),
                "confirmed_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    save_name_alias_store(root, store)
    return {"ok": True, "relation": "same", "a": a, "b": b, "source": source}


def confirm_different_names(
    name_a: str,
    name_b: str,
    root: Path,
    *,
    source: str = "user_confirmed",
) -> dict[str, Any]:
    """人工确认不同 → 负向映射；并尽量拆掉正向边。"""
    a, b = (name_a or "").strip(), (name_b or "").strip()
    if not a or not b:
        return {"ok": False, "error": "名称不能为空"}
    store = load_name_alias_store(root)
    pk = _pair_key(a, b)
    # 从正向条目中移除
    new_entries = []
    for e in store.get("entries") or []:
        can = str(e.get("canonical_name") or "")
        als = [str(x) for x in (e.get("aliases") or []) if x]
        cluster = [can] + als
        if not any(_pair_key(x, y) == pk for x in cluster for y in cluster if x != y):
            new_entries.append(e)
            continue
        # 拆：去掉 b 若 can 是 a
        als2 = [
            x
            for x in als
            if _pair_key(can, x) != pk and normalize_name_key(x) != normalize_name_key(b)
            and normalize_name_key(x) != normalize_name_key(a)
        ]
        if can and normalize_name_key(can) not in (
            normalize_name_key(a),
            normalize_name_key(b),
        ):
            e2 = dict(e)
            e2["aliases"] = als2
            new_entries.append(e2)
        elif als2:
            new_entries.append(
                {
                    "canonical_name": als2[0],
                    "aliases": als2[1:],
                    "source": e.get("source") or source,
                    "confidence": e.get("confidence") or 1.0,
                    "confirmed_at": e.get("confirmed_at") or "",
                }
            )
    store["entries"] = new_entries
    negs = list(store.get("negatives") or [])
    if pk not in _negative_pair_set(store):
        negs.append(
            {
                "a": a,
                "b": b,
                "source": source,
                "confirmed_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    store["negatives"] = negs
    save_name_alias_store(root, store)
    return {"ok": True, "relation": "different", "a": a, "b": b, "source": source}


def expand_queries_with_aliases(
    queries: list[str],
    inquiry_name: str,
    root: Path | None,
    *,
    max_alias_queries: int = 2,
    max_len: int = 40,
) -> list[str]:
    """
    原 queries 保留顺序；在后方追加「同义品名 + 原词中的硬规格片段」最多 max_alias_queries 个。
    不编造型号；DN/PN/功率等从原查询词或询价名旁路保留。
    """
    if not queries:
        return queries
    aliases = get_aliases_for_name(inquiry_name, root, max_n=max_alias_queries)
    if not aliases:
        return queries
    # 从首个查询词抽硬规格 token
    head = queries[0] if queries else ""
    hard_bits = re.findall(
        r"(?i)(?:DN|φ|Φ|PN)\s*\d+(?:\.\d+)?"
        r"|\d+(?:\.\d+)?\s*(?:W(?:/m)?|V|K|mm|MPa)"
        r"|IP\s*\d{2}"
        r"|(?:DS-|RG-|iDS-)[A-Z0-9/\-\.]+"
        r"|[A-Z]{1,6}\d{2,}[A-Z0-9\-_]*",
        f"{head} {inquiry_name}",
    )
    hard = " ".join(dict.fromkeys(hard_bits))  # 去重保序
    seen = {re.sub(r"\s+", " ", q).strip().lower() for q in queries}
    out = list(queries)
    added = 0
    for al in aliases:
        if added >= max_alias_queries:
            break
        q = f"{al} {hard}".strip() if hard else al
        q = re.sub(r"\s+", " ", q).strip()
        if len(q) < 2 or len(q) > max_len:
            q = al[:max_len]
        k = q.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(q)
        added += 1
    return out
