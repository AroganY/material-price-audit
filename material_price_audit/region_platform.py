"""
平台地区能力协议（Phase 2）。

统一接口：capabilities → resolve → apply → read → verify。
默认实现为安全 no-op / unknown，不改变未开启地区控制时的询价结果。

开启地区切换（后续 Phase 接线）：
  - 环境变量 MPA_REGION_SWITCH=1
  - 或 ensure_platform_region(..., force=True)
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .region_models import RegionTarget


def _env_switch_on() -> bool:
    v = (os.environ.get("MPA_REGION_SWITCH") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


@dataclass
class PlatformRegion:
    """平台侧可提交的地区选择（可能与行政区码不同）。"""

    platform: str = ""
    label: str = ""  # 展示名，如「成都市」
    province: str = ""
    city: str = ""
    district: str = ""
    platform_code: str = ""  # 平台内部码（若有）
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "PlatformRegion":
        d = d or {}
        return cls(
            platform=str(d.get("platform") or ""),
            label=str(d.get("label") or ""),
            province=str(d.get("province") or ""),
            city=str(d.get("city") or ""),
            district=str(d.get("district") or ""),
            platform_code=str(d.get("platform_code") or ""),
            raw=dict(d.get("raw") or {}),
        )

    @property
    def display(self) -> str:
        if self.label:
            return self.label
        return "".join(p for p in (self.province, self.city, self.district) if p) or ""


@dataclass
class RegionCapabilities:
    platform: str
    supports_region_ui: bool = False
    levels: list[str] = field(default_factory=lambda: ["city"])
    can_read_current: bool = False
    can_apply: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplyRegionResult:
    ok: bool
    detail: str = ""
    applied: PlatformRegion | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "applied": self.applied.to_dict() if self.applied else None,
        }


@dataclass
class ActualRegion:
    label: str = ""
    province: str = ""
    city: str = ""
    district: str = ""
    raw_text: str = ""
    source: str = "unknown"  # ui | body | none

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def display(self) -> str:
        if self.label:
            return self.label
        return "".join(p for p in (self.province, self.city, self.district) if p) or ""


@dataclass
class VerifyRegionResult:
    ok: bool
    match: str = "unknown"  # exact | province | mismatch | unknown | unsupported
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# —— 平台能力表（Phase2 声明式；后续按实测改 can_apply/can_read）——
_CAPABILITIES: dict[str, RegionCapabilities] = {
    "guangcai": RegionCapabilities(
        platform="guangcai",
        supports_region_ui=True,
        levels=["province", "city"],
        can_read_current=False,
        can_apply=False,
        notes="广材常有城市筛选；Phase2 未接 DOM，apply 为 no-op",
    ),
    "huixun": RegionCapabilities(
        platform="huixun",
        supports_region_ui=True,
        levels=["province", "city"],
        can_read_current=False,
        can_apply=False,
        notes="慧讯会员站可能有项目地/地区；Phase2 no-op",
    ),
    "lingcai": RegionCapabilities(
        platform="lingcai",
        supports_region_ui=True,
        levels=["province", "city"],
        can_read_current=False,
        can_apply=False,
        notes="领材类似造价信息站；Phase2 no-op",
    ),
    "yize": RegionCapabilities(
        platform="yize",
        supports_region_ui=False,
        levels=["city"],
        can_read_current=False,
        can_apply=False,
        notes="易择地区能力待实测",
    ),
    "zaojiatong": RegionCapabilities(
        platform="zaojiatong",
        supports_region_ui=True,
        levels=["province", "city"],
        can_read_current=False,
        can_apply=False,
        notes="造价通市场价常绑地区；Phase2 no-op，列表可抽文本",
    ),
    "jd": RegionCapabilities(
        platform="jd",
        supports_region_ui=True,
        levels=["province", "city", "district"],
        can_read_current=False,
        can_apply=False,
        notes="京东收货地影响价；Phase2 no-op",
    ),
    "1688": RegionCapabilities(
        platform="1688",
        supports_region_ui=False,
        levels=["province", "city"],
        can_read_current=False,
        can_apply=False,
        notes="商家发货地≠价格适用地，禁止把 supplier 当地当价格地",
    ),
    "baidu_web": RegionCapabilities(
        platform="baidu_web",
        supports_region_ui=False,
        can_apply=False,
        can_read_current=False,
        notes="全网兜底无可靠地区选择器",
    ),
}


def region_capabilities(platform_id: str) -> RegionCapabilities:
    from .platforms import normalize_platform_id

    pid = normalize_platform_id(platform_id)
    return _CAPABILITIES.get(
        pid,
        RegionCapabilities(
            platform=pid,
            supports_region_ui=False,
            can_apply=False,
            can_read_current=False,
            notes="未声明能力，默认不切换地区",
        ),
    )


def resolve_platform_region(
    platform_id: str, target: RegionTarget | None
) -> PlatformRegion:
    """把用户 RegionTarget 映射为平台侧选择（Phase2：直接用行政区名称）。"""
    from .platforms import normalize_platform_id

    pid = normalize_platform_id(platform_id)
    t = target or RegionTarget.unspecified()
    label = t.display if t.is_specified() else ""
    return PlatformRegion(
        platform=pid,
        label=label,
        province=t.province,
        city=t.city,
        district=t.district,
        platform_code=t.city_code or t.province_code or t.district_code,
        raw={"target": t.to_dict()},
    )


def apply_region(page, platform_region: PlatformRegion) -> ApplyRegionResult:
    """
    在页面上设置地区。
    Phase2：默认 no-op 成功（不操作 DOM），避免误改业务。
    真正 DOM 切换在后续 Phase 按平台实现。
    """
    cap = region_capabilities(platform_region.platform)
    if not platform_region.display:
        return ApplyRegionResult(
            ok=True,
            detail="未指定目标地区，跳过 apply",
            applied=platform_region,
        )
    if not cap.can_apply:
        return ApplyRegionResult(
            ok=True,
            detail=f"[{platform_region.platform}] Phase2 未实现 DOM 切换（no-op）：目标={platform_region.display}",
            applied=platform_region,
        )
    # 预留：各平台 apply 实现
    apply_fn = _APPLY_HANDLERS.get(platform_region.platform)
    if apply_fn is None:
        return ApplyRegionResult(
            ok=False,
            detail=f"[{platform_region.platform}] 声明 can_apply 但无 handler",
            applied=platform_region,
        )
    try:
        return apply_fn(page, platform_region)
    except Exception as e:
        return ApplyRegionResult(
            ok=False,
            detail=f"apply_region 异常: {type(e).__name__}:{e}",
            applied=platform_region,
        )


def read_current_region(page, platform_id: str) -> ActualRegion:
    """读取页面当前地区显示。Phase2：尽量从 body 弱提取，失败则 unknown。"""
    from .platforms import normalize_platform_id

    pid = normalize_platform_id(platform_id)
    cap = region_capabilities(pid)
    if page is None:
        return ActualRegion(source="none", raw_text="")
    read_fn = _READ_HANDLERS.get(pid)
    if read_fn is not None:
        try:
            return read_fn(page)
        except Exception:
            pass
    if not cap.can_read_current:
        # 弱提取：不声称成功
        text = _safe_page_text(page, limit=2500)
        hit = _guess_city_from_text(text)
        if hit:
            return ActualRegion(label=hit, city=hit, raw_text=hit, source="body")
        return ActualRegion(source="none", raw_text="")
    return ActualRegion(source="none")


def verify_region(
    expected: PlatformRegion | RegionTarget | None,
    actual: ActualRegion | None,
    *,
    require_exact: bool = False,
) -> VerifyRegionResult:
    """
    校验地区是否切换成功。
    - 未指定 expected → ok, unsupported/unknown
    - actual 空且 require_exact → fail
    - Phase2 no-op 路径：不强制 require_exact 时，未知可读性也 ok
    """
    exp_label = ""
    exp_city = ""
    exp_province = ""
    if isinstance(expected, RegionTarget):
        exp_label = expected.display
        exp_city = expected.city
        exp_province = expected.province
    elif isinstance(expected, PlatformRegion):
        exp_label = expected.display
        exp_city = expected.city
        exp_province = expected.province
    elif isinstance(expected, dict):
        exp_label = str(
            expected.get("label")
            or expected.get("city")
            or expected.get("province")
            or ""
        )
        exp_city = str(expected.get("city") or "")
        exp_province = str(expected.get("province") or "")

    if not exp_label and not exp_city and not exp_province:
        return VerifyRegionResult(
            ok=True, match="unknown", detail="未指定期望地区，跳过校验"
        )

    act = actual or ActualRegion()
    act_blob = f"{act.display} {act.city} {act.province} {act.raw_text}"

    if not act.display and not act.city and not act.province and not act.raw_text:
        if require_exact:
            return VerifyRegionResult(
                ok=False,
                match="unknown",
                detail="无法读取页面当前地区，且 require_exact=true",
            )
        return VerifyRegionResult(
            ok=True,
            match="unknown",
            detail="无法读取页面当前地区（Phase2 弱校验通过，未声明切换成功）",
        )

    # exact：城市名互相包含
    for token in (exp_city, exp_label):
        t = (token or "").replace("市", "").replace("省", "").strip()
        if t and t in act_blob.replace("市", "").replace("省", ""):
            return VerifyRegionResult(
                ok=True, match="exact", detail=f"命中地区「{token}」实际「{act.display or act_blob[:40]}」"
            )
    # province
    p = (exp_province or "").replace("省", "").replace("市", "").strip()
    if p and p in act_blob.replace("省", "").replace("市", ""):
        return VerifyRegionResult(
            ok=True,
            match="province",
            detail=f"省级命中「{exp_province}」实际「{act.display or act_blob[:40]}」",
        )

    if require_exact:
        return VerifyRegionResult(
            ok=False,
            match="mismatch",
            detail=f"期望「{exp_label or exp_city}」实际「{act.display or act_blob[:40]}」",
        )
    return VerifyRegionResult(
        ok=False,
        match="mismatch",
        detail=f"地区不一致：期望「{exp_label or exp_city}」实际「{act.display or act_blob[:40]}」",
    )


def ensure_platform_region(
    page,
    platform_id: str,
    target: RegionTarget | None,
    *,
    force: bool = False,
    require_exact: bool = False,
    log: Callable[[str], None] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    恢复会话后的标准顺序：
      resolve → apply → read → verify

    默认：
      - 目标未指定 → 跳过，ok
      - force=False 且未设 MPA_REGION_SWITCH → 跳过，ok（不改变现网）
      - can_apply=False 时 apply 为 no-op，verify 不强制 exact
    """
    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    t = target or RegionTarget.unspecified()
    if not t.is_specified():
        return True, "skip:no_target", {"skipped": True}

    if not force and not _env_switch_on():
        return True, "skip:region_switch_off", {"skipped": True, "target": t.to_dict()}

    pref = resolve_platform_region(platform_id, t)
    _log(f"   [{platform_id}] 地区 resolve → {pref.display or '(空)'}")
    applied = apply_region(page, pref)
    _log(f"   [{platform_id}] 地区 apply → ok={applied.ok} {applied.detail}")
    if not applied.ok:
        return False, f"apply_fail:{applied.detail}", applied.to_dict()

    actual = read_current_region(page, platform_id)
    # Phase2：未实现 DOM 时不强制 exact，避免误杀
    cap = region_capabilities(platform_id)
    hard = require_exact and cap.can_apply and cap.can_read_current
    verified = verify_region(pref, actual, require_exact=hard)
    _log(
        f"   [{platform_id}] 地区 verify → ok={verified.ok} match={verified.match} {verified.detail}"
    )
    meta = {
        "expected": pref.to_dict(),
        "actual": actual.to_dict(),
        "apply": applied.to_dict(),
        "verify": verified.to_dict(),
    }
    if not verified.ok and hard:
        return False, f"verify_fail:{verified.detail}", meta
    return True, verified.detail or "ok", meta


# —— 可选平台 handler 注册表（Phase2 空）——
_APPLY_HANDLERS: dict[str, Callable[[Any, PlatformRegion], ApplyRegionResult]] = {}
_READ_HANDLERS: dict[str, Callable[[Any], ActualRegion]] = {}


def register_apply_handler(
    platform_id: str, fn: Callable[[Any, PlatformRegion], ApplyRegionResult]
) -> None:
    from .platforms import normalize_platform_id

    _APPLY_HANDLERS[normalize_platform_id(platform_id)] = fn


def register_read_handler(platform_id: str, fn: Callable[[Any], ActualRegion]) -> None:
    from .platforms import normalize_platform_id

    _READ_HANDLERS[normalize_platform_id(platform_id)] = fn


def _safe_page_text(page, limit: int = 2500) -> str:
    try:
        return (page.inner_text("body") or "")[:limit]
    except Exception:
        try:
            return (page.content() or "")[:limit]
        except Exception:
            return ""


_CITY_HINTS = (
    "成都市",
    "成都",
    "重庆市",
    "重庆",
    "北京市",
    "北京",
    "上海市",
    "上海",
    "广州市",
    "深圳市",
    "杭州市",
    "南京市",
    "武汉市",
    "西安市",
    "成华区",
    "武侯区",
    "高新区",
)


def _guess_city_from_text(text: str) -> str:
    if not text:
        return ""
    for c in _CITY_HINTS:
        if c in text:
            return c if c.endswith("市") or c.endswith("区") else c
    m = re.search(r"([\u4e00-\u9fff]{2,8}(?:市|地区|州))", text)
    if m:
        return m.group(1)
    return ""


def extract_region_hints_from_text(text: str) -> dict[str, str]:
    """
    从列表/详情文本弱抽取地区线索（不推断供应商地=价格地）。
    返回 source_price_region / supplier_region 可能字段。
    """
    out = {"source_price_region": "", "supplier_region": ""}
    if not text:
        return out
    for pat, key in (
        (r"(?:价格适用|适用地区|报价地区|地区价格|价格地区)\s*[:：]?\s*([^\n，,；;]{2,20})", "source_price_region"),
        (r"(?:交货地|到货地|收货城市)\s*[:：]?\s*([^\n，,；;]{2,20})", "source_price_region"),
        (r"(?:厂家所在地|供应商所在地|所在地区|发货地|商所在地)\s*[:：]?\s*([^\n，,；;]{2,20})", "supplier_region"),
    ):
        m = re.search(pat, text)
        if m and not out[key]:
            out[key] = m.group(1).strip()[:40]
    # 禁止：仅「所在地」同时填两个字段
    return out
