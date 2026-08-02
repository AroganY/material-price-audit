"""
地区门禁与目标地区解析（Phase 6）。

优先级：Excel 行级 > 任务选择 > 用户默认 > 未指定。

region_match:
  exact | province | national | conflict | unknown

处理：
  exact → 可进正式匹配后续
  province → allow_province 可 formal，否则待核
  national → 仅市场参考
  conflict → 拒绝
  unknown → region_required 时待核，否则放行（兼容旧表）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .region_models import RegionEvidence, RegionTarget
from .region_platform import extract_region_hints_from_text


# 常用城市速查（展示名 → 省/码/市/码）
_CITY_TABLE: dict[str, tuple[str, str, str, str]] = {
    "北京": ("北京市", "110000", "北京市", "110100"),
    "上海": ("上海市", "310000", "上海市", "310100"),
    "天津": ("天津市", "120000", "天津市", "120100"),
    "重庆": ("重庆市", "500000", "重庆市", "500100"),
    "成都": ("四川省", "510000", "成都市", "510100"),
    "广州": ("广东省", "440000", "广州市", "440100"),
    "深圳": ("广东省", "440000", "深圳市", "440300"),
    "杭州": ("浙江省", "330000", "杭州市", "330100"),
    "南京": ("江苏省", "320000", "南京市", "320100"),
    "武汉": ("湖北省", "420000", "武汉市", "420100"),
    "西安": ("陕西省", "610000", "西安市", "610100"),
    "苏州": ("江苏省", "320000", "苏州市", "320500"),
    "郑州": ("河南省", "410000", "郑州市", "410100"),
    "长沙": ("湖南省", "430000", "长沙市", "430100"),
    "青岛": ("山东省", "370000", "青岛市", "370200"),
    "大连": ("辽宁省", "210000", "大连市", "210200"),
    "厦门": ("福建省", "350000", "厦门市", "350200"),
    "昆明": ("云南省", "530000", "昆明市", "530100"),
    "贵阳": ("贵州省", "520000", "贵阳市", "520100"),
    "南宁": ("广西壮族自治区", "450000", "南宁市", "450100"),
    "合肥": ("安徽省", "340000", "合肥市", "340100"),
    "福州": ("福建省", "350000", "福州市", "350100"),
    "济南": ("山东省", "370000", "济南市", "370100"),
    "沈阳": ("辽宁省", "210000", "沈阳市", "210100"),
    "哈尔滨": ("黑龙江省", "230000", "哈尔滨市", "230100"),
    "长春": ("吉林省", "220000", "长春市", "220100"),
    "石家庄": ("河北省", "130000", "石家庄市", "130100"),
    "太原": ("山西省", "140000", "太原市", "140100"),
    "南昌": ("江西省", "360000", "南昌市", "360100"),
    "海口": ("海南省", "460000", "海口市", "460100"),
    "兰州": ("甘肃省", "620000", "兰州市", "620100"),
    "乌鲁木齐": ("新疆维吾尔自治区", "650000", "乌鲁木齐市", "650100"),
}


def parse_region_text(text: str, *, source: str = "unspecified") -> RegionTarget:
    """从自由文本解析 RegionTarget（弱规则）。"""
    s = (text or "").strip()
    if not s:
        return RegionTarget.unspecified()
    # 全国
    if re.search(r"全国|不限地区|通用价", s):
        t = RegionTarget(
            province="全国",
            province_code="NATIONAL",
            city="",
            city_code="NATIONAL",
            source=source if source in ("excel_row", "task", "user_default") else "unspecified",
            strategy="national_reference",
        )
        return t
    for key, (prov, pc, city, cc) in _CITY_TABLE.items():
        if key in s or city in s:
            return RegionTarget(
                province=prov,
                province_code=pc,
                city=city,
                city_code=cc,
                source=source if source != "unspecified" else "excel_row",
            )
    # 省级
    m = re.search(r"([\u4e00-\u9fff]{2,8}(?:省|自治区|壮族自治区|回族自治区|维吾尔自治区))", s)
    if m:
        return RegionTarget(
            province=m.group(1),
            province_code="",
            source=source if source != "unspecified" else "excel_row",
        )
    m = re.search(r"([\u4e00-\u9fff]{2,8}市)", s)
    if m:
        city = m.group(1)
        return RegionTarget(
            city=city,
            source=source if source != "unspecified" else "excel_row",
        )
    return RegionTarget(
        city=s[:20],
        source=source if source != "unspecified" else "excel_row",
    )


def resolve_target_region(
    *,
    item_region: dict[str, Any] | None = None,
    item_region_raw: str = "",
    task_region: dict[str, Any] | None = None,
    user_default: dict[str, Any] | None = None,
    strategy: str = "strict_city",
) -> RegionTarget:
    """
    优先级：Excel 行 > 任务 > 用户默认 > 未指定。
    """
    strat = (strategy or "strict_city").strip().lower()
    if strat not in ("strict_city", "allow_province", "national_reference"):
        strat = "strict_city"

    if isinstance(item_region, dict) and item_region:
        t = RegionTarget.from_dict(item_region)
        if t.is_specified():
            t.source = "excel_row"
            t.strategy = strat
            return t
    if (item_region_raw or "").strip():
        t = parse_region_text(item_region_raw, source="excel_row")
        t.strategy = strat
        if t.is_specified():
            return t
    if isinstance(task_region, dict) and task_region:
        t = RegionTarget.from_dict(task_region)
        if t.is_specified():
            t.source = "task"
            t.strategy = strat
            return t
    if isinstance(user_default, dict) and user_default:
        t = RegionTarget.from_dict(user_default)
        if t.is_specified():
            t.source = "user_default"
            t.strategy = strat
            return t
    t = RegionTarget.unspecified()
    t.strategy = strat
    return t


def _norm_place(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("省", "").replace("市", "").replace("区", "").replace("县", "")
    s = s.replace("壮族自治区", "").replace("回族自治区", "").replace("维吾尔自治区", "")
    s = s.replace("自治区", "")
    return s


def classify_region_match(
    requested: RegionTarget,
    *,
    source_price_region: str = "",
    platform_selected_region: str = "",
    supplier_region: str = "",
    raw_text: str = "",
) -> RegionEvidence:
    """
    只根据价格适用地 / 平台选择地区判定；
    **禁止**用 supplier_region 当价格地导致 conflict。
    """
    req_label = requested.display if requested and requested.is_specified() else ""
    # 补抽
    if raw_text and (not source_price_region or not supplier_region):
        hints = extract_region_hints_from_text(raw_text)
        source_price_region = source_price_region or hints.get("source_price_region") or ""
        supplier_region = supplier_region or hints.get("supplier_region") or ""

    price_blob = f"{source_price_region} {platform_selected_region}".strip()
    ev = RegionEvidence(
        requested_region=req_label,
        platform_selected_region=platform_selected_region or "",
        source_price_region=source_price_region or "",
        supplier_region=supplier_region or "",
        region_evidence=(source_price_region or platform_selected_region or "")[:120],
    )

    if not req_label:
        ev.region_match = "unknown"
        ev.region_scope = "unknown"
        return ev

    # 全国价
    if re.search(r"全国|不限", price_blob) or (
        requested.city_code == "NATIONAL" or requested.province_code == "NATIONAL"
    ):
        if re.search(r"全国|不限", price_blob):
            ev.region_match = "national"
            ev.region_scope = "national"
            return ev

    if not price_blob:
        # 无价格地区证据：unknown（不用供应商地补）
        ev.region_match = "unknown"
        ev.region_scope = "unknown"
        return ev

    req_n = _norm_place(req_label)
    price_n = _norm_place(price_blob)
    req_city = _norm_place(requested.city or "")
    req_prov = _norm_place(requested.province or "")

    # exact：城市互相包含
    if req_city and req_city in price_n:
        ev.region_match = "exact"
        ev.region_scope = "city"
        return ev
    if req_n and req_n in price_n:
        ev.region_match = "exact"
        ev.region_scope = "city"
        return ev
    # province：同省但未必同城
    if req_prov and req_prov in price_n:
        # 若价格地明确写了其他市
        other_city = False
        for key, (_, _, city, _) in _CITY_TABLE.items():
            cn = _norm_place(city)
            if cn and cn in price_n and req_city and cn != req_city:
                other_city = True
                break
        if other_city:
            ev.region_match = "conflict"
            ev.region_scope = "city"
            return ev
        ev.region_match = "province"
        ev.region_scope = "province"
        return ev

    # 明确异地：价格地含另一城市
    for key, (_, _, city, _) in _CITY_TABLE.items():
        cn = _norm_place(city)
        if not cn or not req_city:
            continue
        if cn in price_n and cn != req_city and req_city not in price_n:
            ev.region_match = "conflict"
            ev.region_scope = "city"
            return ev

    if re.search(r"全国|不限", price_blob):
        ev.region_match = "national"
        ev.region_scope = "national"
        return ev

    ev.region_match = "unknown"
    ev.region_scope = "unknown"
    return ev


@dataclass
class RegionGateDecision:
    """门禁动作。"""

    action: str  # allow_formal | review | market_ref | reject | passthrough
    region_match: str
    detail: str
    evidence: RegionEvidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "region_match": self.region_match,
            "detail": self.detail,
            "evidence": self.evidence.to_dict(),
        }


def decide_region_gate(
    requested: RegionTarget,
    evidence: RegionEvidence,
    *,
    strategy: str | None = None,
    region_required: bool = False,
) -> RegionGateDecision:
    """
    根据 region_match + 策略输出动作。
    """
    strat = (strategy or requested.strategy or "strict_city").strip().lower()
    if strat not in ("strict_city", "allow_province", "national_reference"):
        strat = "strict_city"
    rm = (evidence.region_match or "unknown").lower()

    if not requested.is_specified() and not region_required:
        return RegionGateDecision(
            action="passthrough",
            region_match=rm,
            detail="未指定目标地区且 region_required=false，放行",
            evidence=evidence,
        )

    if rm == "exact":
        return RegionGateDecision(
            "allow_formal", rm, "精确地区匹配", evidence
        )
    if rm == "province":
        if strat == "allow_province":
            return RegionGateDecision(
                "allow_formal", rm, "同省省级价（allow_province）", evidence
            )
        return RegionGateDecision(
            "review", rm, "同省省级价：strict_city 下待核", evidence
        )
    if rm == "national":
        if strat == "national_reference":
            return RegionGateDecision(
                "market_ref", rm, "全国价仅作参考", evidence
            )
        return RegionGateDecision(
            "market_ref", rm, "全国/异地通用价不作正式价", evidence
        )
    if rm == "conflict":
        return RegionGateDecision(
            "reject",
            rm,
            f"地区冲突：目标={evidence.requested_region} 价格地={evidence.source_price_region or evidence.platform_selected_region}",
            evidence,
        )
    # unknown
    if region_required:
        return RegionGateDecision(
            "review", rm, "无地区证据且 region_required → 待核", evidence
        )
    return RegionGateDecision(
        "passthrough", rm, "无地区证据，兼容放行", evidence
    )


def apply_gate_to_bucket(
    bucket: str,
    gate: RegionGateDecision,
) -> tuple[str, str]:
    """
    调整 decide_quote_bucket 结果。
    返回 (new_bucket, detail_prefix)
    """
    if gate.action == "passthrough" or gate.action == "allow_formal":
        return bucket, ""
    if gate.action == "reject":
        return "discard", f"[地区·拒绝·{gate.region_match}]{gate.detail}"
    if gate.action == "market_ref":
        if bucket == "formal":
            return "market_ref", f"[地区·参考·{gate.region_match}]{gate.detail}"
        if bucket == "candidate":
            return "candidate", f"[地区·{gate.region_match}]{gate.detail}"
        return bucket, f"[地区·{gate.region_match}]"
    if gate.action == "review":
        if bucket == "formal":
            return "candidate", f"[地区·待核·{gate.region_match}]{gate.detail}"
        return bucket if bucket != "discard" else "candidate", f"[地区·待核·{gate.region_match}]{gate.detail}"
    return bucket, ""
