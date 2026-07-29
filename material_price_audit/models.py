"""Canonical data models: schema map, materials, multi-quotes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


STANDARD_ROLES = (
    "name",
    "spec",
    "brand",
    "unit",
    "qty",
    "submit_price",
    "audit_price",
    "sum_price",
    "remark",
    "ignore",
    "unknown",
)


@dataclass
class ColumnMap:
    col: int  # 1-based Excel column
    role: str
    header_text: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ColumnMap":
        return cls(
            col=int(d["col"]),
            role=str(d.get("role") or "unknown"),
            header_text=str(d.get("header_text") or ""),
            confidence=float(d.get("confidence") or 0.0),
        )


@dataclass
class SheetSchema:
    sheet: str
    header_row: int
    data_start_row: int
    columns: list[ColumnMap] = field(default_factory=list)
    layout_notes: str = ""
    source: str = "rule"  # rule | llm | cache | manual
    confidence: float = 0.0

    def role_col(self, role: str) -> int | None:
        for c in self.columns:
            if c.role == role:
                return c.col
        return None

    def roles(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.columns:
            if c.role in STANDARD_ROLES and c.role not in ("ignore", "unknown", "remark"):
                # first wins
                out.setdefault(c.role, c.col)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "header_row": self.header_row,
            "data_start_row": self.data_start_row,
            "columns": [c.to_dict() for c in self.columns],
            "layout_notes": self.layout_notes,
            "source": self.source,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SheetSchema":
        return cls(
            sheet=str(d.get("sheet") or ""),
            header_row=int(d.get("header_row") or 1),
            data_start_row=int(d.get("data_start_row") or 2),
            columns=[ColumnMap.from_dict(x) for x in (d.get("columns") or [])],
            layout_notes=str(d.get("layout_notes") or ""),
            source=str(d.get("source") or "rule"),
            confidence=float(d.get("confidence") or 0.0),
        )


@dataclass
class WorkbookSchema:
    file_fingerprint: str
    sheets: list[SheetSchema] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_fingerprint": self.file_fingerprint,
            "sheets": [s.to_dict() for s in self.sheets],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkbookSchema":
        return cls(
            file_fingerprint=str(d.get("file_fingerprint") or ""),
            sheets=[SheetSchema.from_dict(x) for x in (d.get("sheets") or [])],
            created_at=str(d.get("created_at") or ""),
        )


@dataclass
class CanonicalItem:
    """Internal standardized material row — inquiry engine only speaks this."""

    id: str
    sheet: str
    row: int
    name: str
    spec: str = ""
    brand: str = ""
    unit: Any = ""
    qty: float = 0.0
    submit: float | None = None  # 报送/投标单价；可无
    remark: str = ""
    cols: dict[str, int] = field(default_factory=dict)  # role -> col
    spec_tokens: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    must_match: list[str] = field(default_factory=list)
    parse_confidence: float = 1.0
    parse_status: str = "ok"  # ok | weak | fail
    parse_issues: list[str] = field(default_factory=list)
    category: str = ""

    @property
    def key(self) -> str:
        return self.id

    @property
    def text(self) -> str:
        return f"{self.name} {self.spec} {self.brand} {self.remark}".strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CanonicalItem":
        submit = d.get("submit")
        try:
            submit_f = float(submit) if submit not in (None, "") else None
        except Exception:
            submit_f = None
        try:
            qty = float(d.get("qty") or 0)
        except Exception:
            qty = 0.0
        return cls(
            id=str(d.get("id") or d.get("key") or ""),
            sheet=str(d.get("sheet") or ""),
            row=int(d.get("row") or 0),
            name=str(d.get("name") or ""),
            spec=str(d.get("spec") or ""),
            brand=str(d.get("brand") or ""),
            unit=d.get("unit") or "",
            qty=qty,
            submit=submit_f,
            remark=str(d.get("remark") or ""),
            cols=dict(d.get("cols") or {}),
            spec_tokens=list(d.get("spec_tokens") or []),
            search_queries=list(d.get("search_queries") or []),
            must_match=list(d.get("must_match") or []),
            parse_confidence=float(d.get("parse_confidence") or 0),
            parse_status=str(d.get("parse_status") or "ok"),
            parse_issues=list(d.get("parse_issues") or []),
            category=str(d.get("category") or ""),
        )


@dataclass
class Quote:
    rank: int
    price: float
    platform: str
    title: str
    url: str
    match_level: str = "approximate"  # strict | approximate | weak
    match_score: float = 0.0
    match_detail: str = ""
    tax_mode: str = "unknown"  # tax_incl | tax_excl | unknown
    price_ex_tax: float | None = None
    spec_seen: str = ""
    sku: str = ""
    captured_at: str = ""
    # 详情扩展：厂家 / 联系人 / 电话
    supplier: str = ""
    contact: str = ""
    phone: str = ""
    detail_url: str = ""  # 与 url 同义，导出用
    unit: str = ""
    moq: str = ""
    price_text: str = ""
    price_context: str = ""
    evidence_scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Quote":
        url = str(d.get("url") or d.get("detail_url") or "")
        return cls(
            rank=int(d.get("rank") or 0),
            price=float(d.get("price") or d.get("price_tax") or 0),
            platform=str(d.get("platform") or ""),
            title=str(d.get("title") or ""),
            url=url,
            match_level=str(d.get("match_level") or "approximate"),
            match_score=float(d.get("match_score") or 0),
            match_detail=str(d.get("match_detail") or ""),
            tax_mode=str(d.get("tax_mode") or "unknown"),
            price_ex_tax=(
                float(d["price_ex_tax"])
                if d.get("price_ex_tax") not in (None, "")
                else None
            ),
            spec_seen=str(d.get("spec_seen") or ""),
            sku=str(d.get("sku") or ""),
            captured_at=str(d.get("captured_at") or ""),
            supplier=str(d.get("supplier") or ""),
            contact=str(d.get("contact") or ""),
            phone=str(d.get("phone") or ""),
            detail_url=str(d.get("detail_url") or url),
            unit=str(d.get("unit") or ""),
            moq=str(d.get("moq") or ""),
            price_text=str(d.get("price_text") or ""),
            price_context=str(d.get("price_context") or ""),
            evidence_scope=str(d.get("evidence_scope") or ""),
        )


@dataclass
class QuoteSet:
    item_id: str
    quotes: list[Quote] = field(default_factory=list)
    review_candidates: list[Quote] = field(default_factory=list)
    status: str = "no_match"  # full_k | partial | need_review | no_match | skipped | error
    attempts: list[dict] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "key": self.item_id,  # evidence compat
            "quotes": [q.to_dict() for q in self.quotes],
            "review_candidates": [q.to_dict() for q in self.review_candidates],
            "status": self.status,
            "attempts": self.attempts,
            "error": self.error,
            # legacy single-quote fields for old merge path
            "platform": self.quotes[0].platform if self.quotes else "",
            "title": self.quotes[0].title if self.quotes else "",
            "url": self.quotes[0].url if self.quotes else "",
            "price_tax": self.quotes[0].price if self.quotes else None,
            "price_ex_tax": self.quotes[0].price_ex_tax if self.quotes else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QuoteSet":
        quotes = [Quote.from_dict(x) for x in (d.get("quotes") or [])]
        review_candidates = [
            Quote.from_dict(x) for x in (d.get("review_candidates") or [])
        ]
        # migrate legacy single evidence
        if not quotes and d.get("price_tax") and d.get("status") in ("verified", "full_k", "partial"):
            quotes = [
                Quote(
                    rank=1,
                    price=float(d["price_tax"]),
                    platform=str(d.get("platform") or ""),
                    title=str(d.get("title") or ""),
                    url=str(d.get("url") or ""),
                    match_score=float(d.get("match_score") or 0),
                    price_ex_tax=(
                        float(d["price_ex_tax"]) if d.get("price_ex_tax") is not None else None
                    ),
                    sku=str(d.get("sku") or ""),
                    captured_at=str(d.get("captured_at") or ""),
                )
            ]
        return cls(
            item_id=str(d.get("item_id") or d.get("key") or ""),
            quotes=quotes,
            review_candidates=review_candidates,
            status=str(d.get("status") or "no_match"),
            attempts=list(d.get("attempts") or []),
            error=str(d.get("error") or ""),
        )
