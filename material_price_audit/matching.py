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

    # required: model-like tokens
    required = [
        t
        for t in tokens
        if re.search(r"[A-Za-z]", t) or t.upper().startswith("DN") or t.startswith("φ") or t.startswith("Φ")
    ]
    optional = [t for t in tokens if t not in required]

    def hit(tok: str) -> bool:
        return tok.lower() in blob_l or tok in blob

    req_hits = sum(1 for t in required if hit(t))
    opt_hits = sum(1 for t in optional if hit(t))

    if required:
        # all model tokens ideally; allow miss 0 if only 1 required, else >= ceil(0.8)
        need = max(1, int(round(len(required) * 0.8)))
        if req_hits < need:
            return MatchResult(
                ok=False,
                score=req_hits / max(len(required), 1),
                required_hit=req_hits,
                required_total=len(required),
                detail=f"model tokens {req_hits}/{len(required)} < {need}",
            )
        score = 0.6 * (req_hits / len(required)) + 0.4 * (opt_hits / max(len(optional), 1) if optional else 1.0)
        return MatchResult(
            ok=score >= min_score,
            score=score,
            required_hit=req_hits,
            required_total=len(required),
            detail=f"model ok score={score:.2f}",
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
