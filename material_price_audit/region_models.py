"""
地区与候选统一数据模型（Phase 1）。

不改变现有询价业务路径；供后续族共享池 / 地区门禁 / 平台 Worker 使用。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


REGION_STRATEGIES = frozenset(
    {"strict_city", "allow_province", "national_reference"}
)
REGION_MATCHES = frozenset(
    {"exact", "province", "national", "conflict", "unknown"}
)
NAME_DECISIONS = frozenset(
    {"same", "possible", "different", "pending", ""}
)
REGION_SOURCES = frozenset(
    {"excel_row", "task", "user_default", "unspecified"}
)


def _s(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


@dataclass
class RegionTarget:
    """用户/任务/Excel 的目标地区（项目侧）。"""

    province: str = ""
    province_code: str = ""
    city: str = ""
    city_code: str = ""
    district: str = ""
    district_code: str = ""
    # excel_row | task | user_default | unspecified
    source: str = "unspecified"
    # strict_city | allow_province | national_reference
    strategy: str = "strict_city"

    def __post_init__(self) -> None:
        src = _s(self.source).lower() or "unspecified"
        self.source = src if src in REGION_SOURCES else "unspecified"
        st = _s(self.strategy).lower() or "strict_city"
        self.strategy = st if st in REGION_STRATEGIES else "strict_city"

    @property
    def code_key(self) -> str:
        """缓存/材料族键用的地区码。"""
        if self.district_code:
            return self.district_code
        if self.city_code:
            return self.city_code
        if self.province_code:
            return self.province_code
        # 无国标码时用名称拼
        parts = [p for p in (self.province, self.city, self.district) if p]
        if parts:
            return "|".join(parts)
        return "UNSPECIFIED"

    @property
    def display(self) -> str:
        parts = [p for p in (self.province, self.city, self.district) if p]
        return "".join(parts) if parts else "未指定"

    def is_specified(self) -> bool:
        return bool(
            self.province
            or self.city
            or self.district
            or self.province_code
            or self.city_code
            or self.district_code
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "RegionTarget":
        d = d or {}
        return cls(
            province=_s(d.get("province")),
            province_code=_s(d.get("province_code")),
            city=_s(d.get("city")),
            city_code=_s(d.get("city_code")),
            district=_s(d.get("district")),
            district_code=_s(d.get("district_code")),
            source=_s(d.get("source") or "unspecified"),
            strategy=_s(d.get("strategy") or "strict_city"),
        )

    @classmethod
    def unspecified(cls) -> "RegionTarget":
        return cls(source="unspecified", strategy="strict_city")


@dataclass
class RegionEvidence:
    """
    地区证据三分离：
      requested_region     = 项目目标
      source_price_region  = 价格适用地
      supplier_region      = 厂家所在地（禁止当价格地）
    """

    requested_region: str = ""
    platform_selected_region: str = ""
    source_price_region: str = ""
    supplier_region: str = ""
    region_scope: str = "unknown"  # city|province|national|unknown
    # exact|province|national|conflict|unknown
    region_match: str = "unknown"
    region_evidence: str = ""

    def __post_init__(self) -> None:
        rm = _s(self.region_match).lower() or "unknown"
        self.region_match = rm if rm in REGION_MATCHES else "unknown"
        sc = _s(self.region_scope).lower() or "unknown"
        if sc not in ("city", "province", "national", "unknown"):
            sc = "unknown"
        self.region_scope = sc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "RegionEvidence":
        d = d or {}
        return cls(
            requested_region=_s(d.get("requested_region")),
            platform_selected_region=_s(d.get("platform_selected_region")),
            source_price_region=_s(d.get("source_price_region")),
            supplier_region=_s(d.get("supplier_region")),
            region_scope=_s(d.get("region_scope") or "unknown"),
            region_match=_s(d.get("region_match") or "unknown"),
            region_evidence=_s(d.get("region_evidence")),
        )

    @classmethod
    def empty(cls) -> "RegionEvidence":
        return cls()


@dataclass
class CandidateRecord:
    """平台列表/详情的统一候选中间态（Phase 1 定义，Phase 2+ 接入）。"""

    platform: str = ""
    query: str = ""
    requested_region: str = ""
    platform_selected_region: str = ""
    source_price_region: str = ""
    supplier_region: str = ""
    region_match: str = "unknown"
    source_title: str = ""
    normalized_name: str = ""
    # same | possible | different | pending
    name_decision: str = "pending"
    name_confidence: float = 0.0
    model: str = ""
    diameters: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    material: str = ""
    pressure: str = ""
    voltage: str = ""
    power: str = ""
    brand: str = ""
    unit: str = ""
    price: float | None = None
    tax_mode: str = "unknown"
    supplier: str = ""
    source_row_label: str = ""
    source_record_id: str = ""
    search_url: str = ""
    detail_url: str = ""
    raw_spec_text: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        nd = _s(self.name_decision).lower() or "pending"
        self.name_decision = nd if nd in NAME_DECISIONS else "pending"
        rm = _s(self.region_match).lower() or "unknown"
        self.region_match = rm if rm in REGION_MATCHES else "unknown"

    def region_evidence(self) -> RegionEvidence:
        return RegionEvidence(
            requested_region=self.requested_region,
            platform_selected_region=self.platform_selected_region,
            source_price_region=self.source_price_region,
            supplier_region=self.supplier_region,
            region_match=self.region_match,
            region_evidence=self.raw_spec_text[:200] if self.raw_spec_text else "",
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "CandidateRecord":
        d = d or {}
        price = d.get("price")
        try:
            price_f = float(price) if price not in (None, "") else None
        except Exception:
            price_f = None
        return cls(
            platform=_s(d.get("platform")),
            query=_s(d.get("query")),
            requested_region=_s(d.get("requested_region")),
            platform_selected_region=_s(d.get("platform_selected_region")),
            source_price_region=_s(d.get("source_price_region")),
            supplier_region=_s(d.get("supplier_region")),
            region_match=_s(d.get("region_match") or "unknown"),
            source_title=_s(d.get("source_title") or d.get("title")),
            normalized_name=_s(d.get("normalized_name")),
            name_decision=_s(d.get("name_decision") or "pending"),
            name_confidence=float(d.get("name_confidence") or 0),
            model=_s(d.get("model")),
            diameters=list(d.get("diameters") or []),
            dimensions=list(d.get("dimensions") or []),
            material=_s(d.get("material")),
            pressure=_s(d.get("pressure")),
            voltage=_s(d.get("voltage")),
            power=_s(d.get("power")),
            brand=_s(d.get("brand")),
            unit=_s(d.get("unit")),
            price=price_f,
            tax_mode=_s(d.get("tax_mode") or "unknown"),
            supplier=_s(d.get("supplier")),
            source_row_label=_s(d.get("source_row_label")),
            source_record_id=_s(d.get("source_record_id")),
            search_url=_s(d.get("search_url")),
            detail_url=_s(d.get("detail_url") or d.get("url")),
            raw_spec_text=_s(d.get("raw_spec_text") or d.get("spec_seen")),
            raw_payload=dict(d.get("raw_payload") or {}),
        )

    @classmethod
    def from_legacy_cand(
        cls,
        cand: dict[str, Any],
        *,
        platform: str = "",
        query: str = "",
        requested_region: str = "",
    ) -> "CandidateRecord":
        """从现有 platforms 列表 dict 适配（不丢字段，塞 raw_payload）。"""
        price = cand.get("price_tax")
        if price is None:
            price = cand.get("price")
        try:
            price_f = float(price) if price not in (None, "") else None
        except Exception:
            price_f = None
        url = _s(cand.get("url") or cand.get("detail_url") or cand.get("final_url"))
        return cls(
            platform=platform or _s(cand.get("platform")),
            query=query,
            requested_region=requested_region,
            platform_selected_region=_s(cand.get("platform_selected_region")),
            source_price_region=_s(
                cand.get("source_price_region") or cand.get("price_region")
            ),
            supplier_region=_s(
                cand.get("supplier_region") or cand.get("supplier_area")
            ),
            region_match=_s(cand.get("region_match") or "unknown"),
            source_title=_s(cand.get("title") or cand.get("detail_title")),
            normalized_name=_s(cand.get("normalized_name")),
            name_decision=_s(cand.get("name_decision") or "pending"),
            name_confidence=float(cand.get("name_confidence") or 0),
            model=_s(cand.get("model") or cand.get("sku")),
            brand=_s(cand.get("brand")),
            unit=_s(cand.get("unit")),
            price=price_f,
            tax_mode=_s(cand.get("tax_mode") or "unknown"),
            supplier=_s(cand.get("supplier")),
            source_row_label=_s(cand.get("source_row_label")),
            source_record_id=_s(
                cand.get("source_record_id") or cand.get("sku") or url
            ),
            search_url=_s(cand.get("search_url")),
            detail_url=url,
            raw_spec_text=_s(
                cand.get("spec_seen")
                or cand.get("match_spec_text")
                or cand.get("detail_text")
            )[:2000],
            raw_payload=dict(cand),
        )

    def to_legacy_cand(self) -> dict[str, Any]:
        """回退为 platforms/inquiry 仍认识的 dict。"""
        base = dict(self.raw_payload) if self.raw_payload else {}
        base.update(
            {
                "title": self.source_title or base.get("title") or "",
                "url": self.detail_url or base.get("url") or "",
                "detail_url": self.detail_url or base.get("detail_url") or "",
                "price_tax": self.price
                if self.price is not None
                else base.get("price_tax"),
                "spec_seen": self.raw_spec_text or base.get("spec_seen") or "",
                "supplier": self.supplier or base.get("supplier") or "",
                "unit": self.unit or base.get("unit") or "",
                "tax_mode": self.tax_mode or base.get("tax_mode") or "unknown",
                "platform": self.platform or base.get("platform") or "",
                "source_price_region": self.source_price_region,
                "supplier_region": self.supplier_region,
                "region_match": self.region_match,
                "name_decision": self.name_decision,
                "source_row_label": self.source_row_label,
                "source_record_id": self.source_record_id,
            }
        )
        return base
