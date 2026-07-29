"""Optional LLM review for semantic-only gray areas; never sources or invents prices."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .matching import MatchResult
from .schema_map import _llm_chat_json
from .settings_store import UserSettings


def _cache_path(root: Path, payload: dict[str, Any]) -> Path:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    key = hashlib.sha256(raw).hexdigest()[:28]
    return root / "data" / "mapping-cache" / "match-review" / f"{key}.json"


def _semantic_only(missing: tuple[str, ...]) -> bool:
    """型号、数字、单位等硬字段禁止由 LLM 覆盖。"""
    if not missing:
        return False
    hard = re.compile(
        r"\d|型号|尺寸|口径|电压|功率|色温|角度|防护等级|端口|通道|压力|流量|容量|单位",
        re.I,
    )
    return all(not hard.search(x or "") for x in missing)


def review_semantic_gray_area(
    *,
    item: Any,
    title: str,
    evidence_text: str,
    rule_result: MatchResult,
    settings: UserSettings | None,
    root: Path | None,
) -> MatchResult:
    """
    Promote only semantic synonyms with quoted same-page evidence.
    Explicit conflict and any numeric/model missing condition always remain deterministic.
    """
    if not settings or not root:
        return rule_result
    if rule_result.outcome != "review" or rule_result.conflicts:
        return rule_result
    if not _semantic_only(rule_result.missing):
        return rule_result
    if not settings.llm_enabled or "match_review" not in (settings.llm_use_for or []):
        return rule_result

    payload = {
        "name": str(getattr(item, "name", "")),
        "spec": str(getattr(item, "spec", "")),
        "brand": str(getattr(item, "brand", "")),
        "title": title[:300],
        "evidence": evidence_text[:6000],
        "missing": list(rule_result.missing),
    }
    cache = _cache_path(Path(root), payload)
    data: dict[str, Any] | None = None
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            data = None
    if data is None:
        system = (
            "你是工程材料规格复核器，只判断名称/规格语义是否等价，不提供也不推断价格。"
            "只能根据给出的同一商品证据判断。任何型号、数值、单位不同都必须 conflict；"
            "未展示则 insufficient。输出 JSON：decision 为 equivalent/insufficient/conflict，"
            "confidence 为 0-1，covered_requirements 为已覆盖的 missing 原文数组，"
            "evidence_quotes 为页面逐字引用数组，reason 为简短中文。"
        )
        data = _llm_chat_json(settings, system, json.dumps(payload, ensure_ascii=False))
        if data:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
    if not data:
        return rule_result

    decision = str(data.get("decision") or "").lower()
    confidence = float(data.get("confidence") or 0)
    quotes = [str(x).strip() for x in (data.get("evidence_quotes") or []) if str(x).strip()]
    covered = {str(x) for x in (data.get("covered_requirements") or [])}
    quotes_valid = bool(quotes) and all(q in evidence_text for q in quotes)
    all_covered = set(rule_result.missing).issubset(covered)
    reason = str(data.get("reason") or "")[:240]

    if decision == "conflict" and confidence >= 0.9 and quotes_valid:
        conflict = f"语义复核发现冲突：{reason or quotes[0]}"
        return MatchResult(
            False, rule_result.score, rule_result.required_hit, rule_result.required_total,
            conflict, "reject", "reject", missing=rule_result.missing,
            conflicts=(conflict,), evidence=rule_result.evidence + tuple(quotes),
        )
    if decision == "equivalent" and confidence >= 0.95 and quotes_valid and all_covered:
        return MatchResult(
            True, 1.0, rule_result.required_total, rule_result.required_total,
            f"名称+规格语义等价（LLM 灰区复核；证据：{'；'.join(quotes[:3])}）",
            "strict", "accept", evidence=rule_result.evidence + tuple(quotes),
        )
    return rule_result
