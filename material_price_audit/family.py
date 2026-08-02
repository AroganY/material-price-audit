"""
材料族：标准品名 + 目标地区（Phase 3）。

同族共享主搜索词（不含 DN/硬规格），避免
「薄壁不锈钢管 DN50/100/150」各搜一遍主名。
成都与重庆 region_code 不同，禁止共池。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .matching import name_search_core, peel_name_dimension_noise
from .models import CanonicalItem
from .name_aliases import normalize_name_key
from .region_models import RegionTarget


_HARD_SPEC_RE = re.compile(
    r"(?i)(?:DN|φ|Φ|PN)\s*\d+(?:\.\d+)?"
    r"|\d+(?:\.\d+)?\s*(?:W(?:\s*[/／]\s*m)?|V|K|mm|MPa|kPa|A)"
    r"|IP\s*\d{2}"
    r"|(?:DS-|RG-|iDS-|HM-|JB-|MS-|LRS-)[A-Z0-9/\-\.]+"
    r"|[A-Z]{1,8}\d{2,}[A-Z0-9\-_/\.]*"
    r"|(?<!\d)(\d{2,5})\s*[xX×*]\s*(\d{2,5})(?:\s*[xX×*]\s*(\d{2,5}))?"
)


def strip_hard_specs(text: str) -> str:
    """去掉 DN/尺寸/型号等硬规格，得到可共享的主搜品名。"""
    s = text or ""
    s = peel_name_dimension_noise(s) or s
    s = _HARD_SPEC_RE.sub(" ", s)
    s = re.sub(r"[\(\)（）\[\]【】]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def family_core_name(name: str, spec: str = "") -> str:
    """材料族标准品名（不含 DN、不含地名、中文空格已折）。"""
    from .matching import collapse_cjk_spaces, strip_geo_noise

    raw = strip_geo_noise(collapse_cjk_spaces(name or ""))
    base = strip_hard_specs(raw)
    # 规格栏若整段都是硬参数，不并入 core
    core = name_search_core(base) or base
    core = re.sub(r"\s+", "", core or "")
    if not core:
        # 兜底：名称去空白截断
        core = re.sub(r"\s+", "", (raw or name or ""))[:24]
    return core[:40]


def extract_item_hard_tags(name: str, spec: str = "") -> list[str]:
    """本条材料必须保留的硬规格 token（用于缺规格补搜）。"""
    blob = f"{name or ''} {spec or ''}"
    tags: list[str] = []
    for m in re.finditer(
        r"(?i)(?:DN|φ|Φ|PN)\s*\d+(?:\.\d+)?",
        blob,
    ):
        t = re.sub(r"\s+", "", m.group(0)).upper()
        t = t.replace("Φ", "DN").replace("φ", "DN")
        if t.startswith("DN") or t.startswith("PN"):
            if t not in tags:
                tags.append(t)
    for m in re.finditer(
        r"(?<!\d)(\d{2,5})\s*[xX×*]\s*(\d{2,5})(?:\s*[xX×*]\s*(\d{2,5}))?",
        blob,
    ):
        parts = [m.group(1), m.group(2)] + ([m.group(3)] if m.group(3) else [])
        t = "x".join(parts)
        if t not in tags:
            tags.append(t)
    for m in re.finditer(
        r"(?i)(?:DS-|RG-|iDS-|HM-|JB-|MS-|LRS-)[A-Z0-9/\-\.]+",
        blob,
    ):
        t = m.group(0)
        if t not in tags:
            tags.append(t)
    return tags[:8]


def region_code_for_item(
    item: CanonicalItem,
    default_region: dict[str, Any] | RegionTarget | None = None,
) -> str:
    """目标地区码：Excel 行 region > 默认 region > UNSPECIFIED。"""
    reg = getattr(item, "region", None) or {}
    if isinstance(reg, dict) and reg:
        rt = RegionTarget.from_dict(reg)
        if rt.is_specified():
            return rt.code_key
    raw = str(getattr(item, "region_raw", "") or "").strip()
    if raw:
        return raw  # 原文作键，避免成都重庆混淆
    if isinstance(default_region, RegionTarget):
        if default_region.is_specified():
            return default_region.code_key
    elif isinstance(default_region, dict) and default_region:
        rt = RegionTarget.from_dict(default_region)
        if rt.is_specified():
            return rt.code_key
    return "UNSPECIFIED"


def family_key(core_name: str, region_code: str) -> str:
    nk = normalize_name_key(core_name) or re.sub(r"\s+", "", core_name or "").lower()
    rc = (region_code or "UNSPECIFIED").strip() or "UNSPECIFIED"
    return f"{nk}|{rc}"


@dataclass
class MaterialFamily:
    family_key: str
    core_name: str
    region_code: str
    items: list[CanonicalItem] = field(default_factory=list)

    def main_query(self) -> str:
        """族级主搜词：仅标准品名，禁止夹带 DN。"""
        q = re.sub(r"\s+", "", self.core_name or "")
        # 再次剥离可能残留的 DN
        q = re.sub(r"(?i)(?:DN|PN)\d+", "", q)
        q = q.strip() or (self.core_name or "")[:24]
        return q[:40]

    def gap_query_for(self, item: CanonicalItem) -> str | None:
        """缺规格补搜：品名 + 首个硬规格；禁止「DN100 DN100」。"""
        main = self.main_query()
        tags = extract_item_hard_tags(item.name, item.spec)
        if not tags:
            return None
        tag = tags[0]
        # 主词已含该 tag 则不再拼
        if tag.upper() in main.upper():
            return None
        q = f"{main} {tag}".strip()
        # 去重空格
        q = re.sub(r"\s+", " ", q)
        if q == main:
            return None
        return q[:60]

    def queries_for_item(self, item: CanonicalItem, *, max_n: int = 3) -> list[str]:
        """
        单条在族模式下的检索词：
          1) 主搜品名
          2) 品名+硬规格（补搜）
        最多 max_n，且互不重复。
        """
        out: list[str] = []
        main = self.main_query()
        if main:
            out.append(main)
        gap = self.gap_query_for(item)
        if gap and gap.lower() not in {x.lower() for x in out}:
            out.append(gap)
        return out[: max(1, max_n)]


def build_families(
    items: Iterable[CanonicalItem],
    default_region: dict[str, Any] | RegionTarget | None = None,
) -> list[MaterialFamily]:
    """
    按「标准品名 + 目标地区」分族，保持首次出现顺序。
    """
    order: list[str] = []
    buckets: dict[str, MaterialFamily] = {}
    for it in items or []:
        core = family_core_name(it.name, it.spec)
        rc = region_code_for_item(it, default_region)
        fk = family_key(core, rc)
        if fk not in buckets:
            buckets[fk] = MaterialFamily(
                family_key=fk,
                core_name=core,
                region_code=rc,
                items=[],
            )
            order.append(fk)
        buckets[fk].items.append(it)
    return [buckets[k] for k in order]


def family_pool_enabled(
    settings: Any = None,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """Feature flag：默认开；MPA_FAMILY_POOL=0 或 settings.use_family_pool=False 可关。"""
    import os

    e = env if env is not None else os.environ
    v = (e.get("MPA_FAMILY_POOL") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    if settings is not None and hasattr(settings, "use_family_pool"):
        return bool(getattr(settings, "use_family_pool"))
    # 默认开启：同品名只搜一次主词，省时间+Token
    return True
