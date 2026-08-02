"""
CandidateRecord ↔ 旧 list[dict] 适配（Phase 2）。

默认询价仍使用 list[dict]；需要统一模型时调用 to_records / from_records。
"""

from __future__ import annotations

from typing import Any

from .region_models import CandidateRecord, RegionTarget
from .region_platform import extract_region_hints_from_text


def to_records(
    cands: list[dict] | None,
    *,
    platform: str = "",
    query: str = "",
    requested_region: str = "",
    target: RegionTarget | None = None,
) -> list[CandidateRecord]:
    req = requested_region
    if not req and target is not None:
        req = target.display
    out: list[CandidateRecord] = []
    for c in cands or []:
        if not isinstance(c, dict):
            continue
        rec = CandidateRecord.from_legacy_cand(
            c, platform=platform, query=query, requested_region=req
        )
        # 弱抽地区：仅填充空字段，不覆盖已有
        blob = " ".join(
            str(c.get(k) or "")
            for k in (
                "title",
                "spec_seen",
                "detail_text",
                "price_context",
                "supplier",
            )
        )
        hints = extract_region_hints_from_text(blob)
        if not rec.source_price_region and hints.get("source_price_region"):
            rec.source_price_region = hints["source_price_region"]
        if not rec.supplier_region and hints.get("supplier_region"):
            rec.supplier_region = hints["supplier_region"]
        # 安全：若两字段相同且来自「所在地」类，保留 supplier，清空 price 以免误用
        if (
            rec.supplier_region
            and rec.source_price_region
            and rec.supplier_region == rec.source_price_region
            and "适用" not in blob
            and "价格地区" not in blob
        ):
            # 仅有发货地/所在地时，不当作价格适用地
            if re_search_ship_only(blob):
                rec.source_price_region = ""
        out.append(rec)
    return out


def re_search_ship_only(blob: str) -> bool:
    import re

    if re.search(r"价格适用|适用地区|报价地区", blob):
        return False
    return bool(re.search(r"发货地|厂家所在|供应商所在|所在地区", blob))


def from_records(records: list[CandidateRecord] | None) -> list[dict[str, Any]]:
    return [r.to_legacy_cand() for r in (records or [])]


def search_as_records(
    page,
    platform_id: str,
    query: str,
    must: list[str],
    timeout_ms: int,
    min_score: int,
    registry: dict,
    *,
    target: RegionTarget | None = None,
    requested_region: str = "",
) -> tuple[list[CandidateRecord], str]:
    """调用 search_on_platform 并转为 CandidateRecord。"""
    from .platforms import search_on_platform

    cands, status = search_on_platform(
        page, platform_id, query, must, timeout_ms, min_score, registry
    )
    recs = to_records(
        cands,
        platform=platform_id,
        query=query,
        requested_region=requested_region,
        target=target,
    )
    return recs, status
