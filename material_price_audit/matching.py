"""Detail-page / title match rules for waterfall platform selection."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .excel_io import LineItem


def extract_tokens(text: str) -> list[str]:
    """Extract model-like and meaningful tokens from name+spec."""
    text = (text or "").replace("\n", " ")
    tokens: list[str] = []
    # model patterns
    for m in re.finditer(
        r"(?:DS-|RG-|ST|iDS-|HM-|JB-|MS-|LRS-|GTYQ-|ZN-|WDZN-)[A-Z0-9/\-\.\(\)]+",
        text,
        re.I,
    ):
        tokens.append(m.group(0))
    # DN / phi sizes
    for m in re.finditer(r"(?:DN|φ|Φ)\s*\d{2,3}(?:\s*[×xX\*]\s*\d+(?:\.\d+)?)?", text, re.I):
        tokens.append(re.sub(r"\s+", "", m.group(0)))
    # power / voltage
    for m in re.finditer(r"\d+(?:\.\d+)?\s*(?:kW|KW|W|V|mm|MPa)", text, re.I):
        tokens.append(re.sub(r"\s+", "", m.group(0)))
    # Chinese material keywords (length>=2)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,8}", text):
        w = m.group(0)
        if w not in ("不含税", "报送", "审定", "规格", "型号", "材料", "名称", "产地", "品牌"):
            tokens.append(w)
    # unique preserve order
    seen = set()
    out = []
    for t in tokens:
        k = t.lower()
        if k not in seen and len(t) >= 2:
            seen.add(k)
            out.append(t)
    return out[:24]


@dataclass
class MatchResult:
    ok: bool
    score: float
    required_hit: int
    required_total: int
    detail: str


def detail_matches_item(
    item: LineItem,
    page_title: str,
    page_text: str = "",
    min_score: float = 0.55,
) -> MatchResult:
    """
    Strict-ish match for waterfall:
    - Prefer model tokens (DS-xxx, DN100) must hit if present
    - Else require enough Chinese/spec tokens in title+text
    """
    blob = f"{page_title or ''} {page_text or ''}"
    blob_l = blob.lower()
    source = f"{item.name} {item.spec} {item.brand}"
    tokens = extract_tokens(source)
    if not tokens:
        # fallback: any 2-char overlap of name
        name = (item.name or "").strip()
        ok = bool(name) and (name[:4] in blob or name in blob)
        return MatchResult(ok=ok, score=1.0 if ok else 0.0, required_hit=1 if ok else 0, required_total=1, detail="name-fallback")

    # 型号核心：DS-/RG-/ST… 整段；不要把 AC220V、零碎字母全当成「必须全中」
    model_full = [
        t
        for t in tokens
        if re.match(r"^(?:DS-|RG-|ST|iDS-|HM-|JB-|MS-|LRS-|GTYQ-|ZN-|WDZN-)", t, re.I)
    ]
    size_toks = [
        t
        for t in tokens
        if t.upper().startswith("DN") or t.startswith("φ") or t.startswith("Φ")
    ]
    # 从整型号拆出关键片段（KH6320 / 6320-C1），命中任一段也算型号对上
    model_keys: list[str] = []
    for m in model_full:
        model_keys.append(m)
        # DS-KH6320-C1 → KH6320-C1, KH6320, 6320
        parts = re.split(r"[-/]", m)
        for p in parts:
            if len(p) >= 4 and re.search(r"[A-Za-z0-9]", p):
                model_keys.append(p)
        m2 = re.search(r"([A-Z]{0,4}\d{3,}[A-Z0-9]*)", m, re.I)
        if m2:
            model_keys.append(m2.group(1))

    # 去重
    seen_k = set()
    model_keys_u = []
    for k in model_keys:
        lk = k.lower()
        if lk not in seen_k:
            seen_k.add(lk)
            model_keys_u.append(k)
    model_keys = model_keys_u

    optional = [
        t
        for t in tokens
        if t not in model_full and t not in size_toks
    ]

    def hit(tok: str) -> bool:
        if not tok:
            return False
        return tok.lower() in blob_l or tok in blob

    # 型号：任一核心片段命中即可（京东标题常写 DS-KH6320-C1A1 变体）
    model_hit = any(hit(k) for k in model_keys) if model_keys else False
    # 宽松：去横杠再比
    if not model_hit and model_full:
        compact_blob = re.sub(r"[\s\-/]", "", blob_l)
        for m in model_full:
            if re.sub(r"[\s\-/]", "", m.lower()) in compact_blob:
                model_hit = True
                break
            # 核心数字段
            num = re.search(r"(\d{4,})", m)
            if num and num.group(1) in blob:
                # 还要一点字母前缀防误伤
                prefix = re.search(r"([A-Za-z]{1,4})\d", m)
                if not prefix or prefix.group(1).lower() in blob_l:
                    model_hit = True
                    break

    size_hits = sum(1 for t in size_toks if hit(t))
    opt_hits = sum(1 for t in optional if hit(t))

    if model_keys or size_toks:
        if model_keys and not model_hit:
            return MatchResult(
                ok=False,
                score=0.0,
                required_hit=0,
                required_total=1,
                detail=f"model miss (need one of {model_keys[:4]})",
            )
        if size_toks and size_hits < max(1, len(size_toks) // 2):
            return MatchResult(
                ok=False,
                score=size_hits / max(len(size_toks), 1),
                required_hit=size_hits,
                required_total=len(size_toks),
                detail=f"size tokens {size_hits}/{len(size_toks)}",
            )
        # 型号已中：给高分；中文词加分
        score = 0.72 if model_hit or not model_keys else 0.0
        if optional:
            score += 0.28 * (opt_hits / max(len(optional), 1))
        else:
            score = max(score, 0.85 if model_hit else score)
        score = min(1.0, score)
        return MatchResult(
            ok=score >= min_score or model_hit,
            score=score,
            required_hit=1 if model_hit else size_hits,
            required_total=1,
            detail=f"model hit={model_hit} score={score:.2f}",
        )

    # no model tokens: need enough keyword hits
    total = len(optional) or len(tokens)
    hits = opt_hits if optional else sum(1 for t in tokens if hit(t))
    score = hits / max(total, 1)
    # require at least 2 hits if enough tokens
    min_hits = 2 if total >= 3 else 1
    ok = hits >= min_hits and score >= min_score
    return MatchResult(
        ok=ok,
        score=score,
        required_hit=hits,
        required_total=total,
        detail=f"keywords {hits}/{total} score={score:.2f}",
    )
