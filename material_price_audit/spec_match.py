"""
规格结构化匹配（Phase 4）。

只对名称 same/possible 的候选抽取规格 JSON。
硬规格冲突（DN/型号/截面等）必须拒绝。
不产生、不推测价格。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .matching import (
    MatchResult,
    has_hard_spec_conflict,
    strict_name_spec_match,
)
from .name_match import allows_spec_extract


@dataclass
class StructuredSpec:
    model: str = ""
    diameters: list[str] = field(default_factory=list)  # DN*
    dimensions: list[str] = field(default_factory=list)  # 1250x400
    material: str = ""
    pressure: str = ""  # PN*
    voltage: str = ""
    power: str = ""
    brand: str = ""
    unit: str = ""
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "StructuredSpec":
        d = d or {}
        return cls(
            model=str(d.get("model") or ""),
            diameters=list(d.get("diameters") or []),
            dimensions=list(d.get("dimensions") or []),
            material=str(d.get("material") or ""),
            pressure=str(d.get("pressure") or ""),
            voltage=str(d.get("voltage") or ""),
            power=str(d.get("power") or ""),
            brand=str(d.get("brand") or ""),
            unit=str(d.get("unit") or ""),
            raw_text=str(d.get("raw_text") or ""),
        )


def extract_structured_spec(
    *texts: str,
    brand: str = "",
    unit: str = "",
) -> StructuredSpec:
    """从标题/规格/正文规则抽取规格字段（无 LLM）。"""
    blob = " ".join(str(t or "") for t in texts)
    blob = re.sub(r"\s+", " ", blob).strip()
    diameters: list[str] = []
    for m in re.finditer(r"(?i)(?:DN|φ|Φ)\s*(\d{2,4})", blob):
        t = f"DN{m.group(1)}"
        if t not in diameters:
            diameters.append(t)
    pressures: list[str] = []
    for m in re.finditer(r"(?i)PN\s*(\d+(?:\.\d+)?)", blob):
        t = f"PN{m.group(1)}"
        if t not in pressures:
            pressures.append(t)
    dimensions: list[str] = []
    for m in re.finditer(
        r"(?<!\d)(\d{2,5})\s*[xX×*]\s*(\d{2,5})(?:\s*[xX×*]\s*(\d{2,5}))?",
        blob,
    ):
        parts = [m.group(1), m.group(2)] + ([m.group(3)] if m.group(3) else [])
        t = "x".join(parts)
        if t not in dimensions:
            dimensions.append(t)
    model = ""
    m = re.search(
        r"(?i)(?:DS-|RG-|iDS-|HM-|JB-|MS-|LRS-|XZP)[A-Z0-9/\-\.]+",
        blob,
    )
    if m:
        model = m.group(0)
    else:
        m = re.search(r"(?i)\b([A-Z]{1,6}\d{2,}[A-Z0-9\-_/\.]*)\b", blob)
        if m and len(m.group(1)) >= 4:
            model = m.group(1)
    voltage = ""
    m = re.search(r"(?i)(\d+(?:\.\d+)?)\s*V\b", blob)
    if m:
        voltage = f"{m.group(1)}V"
    power = ""
    m = re.search(r"(?i)(\d+(?:\.\d+)?)\s*(?:kW|KW|W)\b", blob)
    if m:
        power = re.sub(r"\s+", "", m.group(0))
    material = ""
    for mat in ("304", "316L", "316", "Q235", "球墨铸铁", "不锈钢", "碳钢", "铜"):
        if mat in blob:
            material = mat
            break
    return StructuredSpec(
        model=model,
        diameters=diameters,
        dimensions=dimensions,
        material=material,
        pressure=pressures[0] if pressures else "",
        voltage=voltage,
        power=power,
        brand=brand or "",
        unit=unit or "",
        raw_text=blob[:800],
    )


@dataclass
class SpecMatchOutcome:
    result: MatchResult
    structured: StructuredSpec
    hard_conflict: bool
    skip_reason: str = ""  # e.g. name_different

    @property
    def ok(self) -> bool:
        return bool(self.result.ok) and not self.hard_conflict


def match_name_and_spec(
    item: Any,
    title: str,
    body: str,
    *,
    name_decision: str = "same",
    match_spec_text: str = "",
    match_name_text: str = "",
    spec_seen: str = "",
    brand: str = "",
    unit: str = "",
) -> SpecMatchOutcome:
    """
    名称 different → 不抽规格、直接拒绝。
    否则 strict_name_spec_match + 结构化抽取 + 硬冲突检测。
    """
    structured = StructuredSpec()
    if not allows_spec_extract(name_decision):
        from .matching import MatchResult as MR

        mr = MR(
            False,
            0.0,
            0,
            0,
            f"[名称·different]禁止规格抽取：{title[:60]}",
            "reject",
            "reject",
            missing=(),
            conflicts=("名称不同物",),
            evidence=("name_different",),
        )
        return SpecMatchOutcome(
            result=mr,
            structured=structured,
            hard_conflict=True,
            skip_reason="name_different",
        )

    structured = extract_structured_spec(
        title,
        match_spec_text or spec_seen,
        body,
        brand=brand,
        unit=unit,
    )
    mr = strict_name_spec_match(
        item,
        title,
        body,
        match_spec_text=match_spec_text or spec_seen or "",
        match_name_text=match_name_text or title or "",
        spec_seen=spec_seen or "",
    )
    hard = has_hard_spec_conflict(mr)
    if hard:
        # 确保 outcome 为 reject
        if mr.outcome != "reject":
            from .matching import MatchResult as MR

            mr = MR(
                False,
                mr.score,
                mr.required_hit,
                mr.required_total,
                f"[硬规格冲突]{mr.detail}",
                "reject",
                "reject",
                missing=mr.missing,
                conflicts=mr.conflicts,
                evidence=mr.evidence + ("hard_spec_conflict",),
            )
    return SpecMatchOutcome(
        result=mr, structured=structured, hard_conflict=hard, skip_reason=""
    )


def has_valid_numeric_price(price: Any) -> bool:
    """无有效数字价不得进询价正式/参考结果。"""
    try:
        p = float(price)
    except Exception:
        return False
    return 0.05 < p < 5_000_000
