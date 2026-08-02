"""
多价询价编排（对齐人工）：

对每条材料：
  for 平台 in 用户勾选顺序:
    搜索 → 详情 → **名称+规格完全匹配** 才收价
    本站失败 / 关浏览器 / 无匹配 → **必须继续下一站**（如广材没有就去领材）
  凑满 K 条 → 成功
  所有平台试完都没有 → 留空「没查到」
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
import re
from typing import Any, Callable

from .excel_io import r2
from .matching import (
    decide_quote_bucket,
    name_missed,
    strict_name_spec_match,
    unit_compatibility,
)
from .models import CanonicalItem, Quote, QuoteSet
from .llm_agent import (
    collect_match_fail_reasons,
    plan_search_queries,
    rank_candidates,
    suggest_requery,
)
from .name_aliases import expand_queries_with_aliases
from .normalize import (
    build_platform_queries,
    normalize_search_query,
    platform_query_budget,
)
from .platforms import (
    is_ecommerce_platform,
    load_platform_registry,
    login_urls_for,
    restore_platform_workspace,
    search_on_platform,
)
from .login_gate import (
    check_url_for,
    ensure_logged_in_or_resume,
    try_resume_huixun_session,
    try_resume_zaojiatong_session,
    verify_logged_in,
)
from .scraper import (
    agent_login_signal_path,
    launch_context,
    open_detail,
    wait_for_login_agent,
)
from .settings_store import UserSettings
from . import perf as perf_mod


def quote_to_result_row(q: Quote, *, role: str = "") -> dict[str, Any]:
    """Serialize one source quote for the web review panel without losing evidence fields."""
    return {
        "rank": q.rank,
        "price": q.price,
        "price_ex_tax": getattr(q, "price_ex_tax", None),
        "tax_mode": getattr(q, "tax_mode", "") or "",
        "platform": q.platform,
        "title": (q.title or "")[:500],
        "spec_seen": (getattr(q, "spec_seen", None) or "")[:800],
        # url may be a search page; detail_url is the exact material/quote evidence.
        "url": q.url or "",
        "detail_url": getattr(q, "detail_url", "") or "",
        "match_level": getattr(q, "match_level", "") or "",
        "match_detail": (q.match_detail or "")[:400],
        "sku": getattr(q, "sku", "") or "",
        "supplier": q.supplier or "",
        "phone": getattr(q, "phone", "") or "",
        "contact": getattr(q, "contact", "") or "",
        "unit": getattr(q, "unit", "") or "",
        "moq": getattr(q, "moq", "") or "",
        "price_text": getattr(q, "price_text", "") or "",
        "price_context": getattr(q, "price_context", "") or "",
        "evidence_scope": getattr(q, "evidence_scope", "") or "",
        "source_group_index": getattr(q, "source_group_index", None),
        "source_quote_index": getattr(q, "source_quote_index", None),
        "source_row_index": getattr(q, "source_row_index", None),
        "source_row_label": getattr(q, "source_row_label", "") or "",
        "source_quality": getattr(q, "source_quality", "") or "",
        "price_role": role or getattr(q, "price_role", "") or "",
        "requested_region": getattr(q, "requested_region", "") or "",
        "platform_selected_region": getattr(q, "platform_selected_region", "") or "",
        "source_price_region": getattr(q, "source_price_region", "") or "",
        "supplier_region": getattr(q, "supplier_region", "") or "",
        "region_match": getattr(q, "region_match", "") or "",
        "name_decision": getattr(q, "name_decision", "") or "",
        "vs_submit": getattr(q, "vs_submit", "") or "unknown",
        "price_anomaly": getattr(q, "price_anomaly", "") or "",
        "source_record_id": getattr(q, "source_record_id", "") or "",
    }


def _recover_price_from_attempt(a: dict) -> float | None:
    """
    从 attempt 的正文/价文字里补抽真实数字价。
    造价通列表常把「市场价：￥1143.17」写在 spec_seen，但 price_tax 曾被写成 0.01 占位。
    """
    try:
        raw = a.get("price_tax")
        if raw not in (None, ""):
            v = float(raw)
            if v > 0.05 and v < 5_000_000 and v not in (-1000.0, 0.01):
                return v
    except Exception:
        pass
    blob = " ".join(
        str(a.get(k) or "")
        for k in (
            "price_text",
            "price_context",
            "spec_seen",
            "title",
            "match_detail",
        )
    )
    # 优先走造价通专用解析（含 市场价/建议价 / 拒绝 -1000）
    if str(a.get("platform") or "") == "zaojiatong" or "造价通" in blob or "市场价" in blob:
        try:
            from .adapters.zaojiatong import extract_visible_price

            p, _, _ = extract_visible_price(blob)
            if p is not None:
                return float(p)
        except Exception:
            pass
    m = re.search(
        r"(?:市场价|建议价|除税|含税)[^0-9¥￥]{0,8}[¥￥]?\s*(\d+(?:\.\d+)?)",
        blob,
    )
    if m:
        try:
            v = float(m.group(1))
            if 0.05 < v < 5_000_000:
                return v
        except Exception:
            pass
    m = re.search(r"[¥￥]\s*(\d+(?:\.\d+)?)", blob)
    if m:
        try:
            v = float(m.group(1))
            if 0.05 < v < 5_000_000:
                return v
        except Exception:
            pass
    return None
from .runtime import load_config, project_root


ProgressCb = Callable[[dict[str, Any]], None]


def _ecommerce_cfg() -> dict[str, Any]:
    """Load ecommerce + inquiry search budget knobs from config (safe defaults)."""
    try:
        cfg = load_config(project_root() / "config.yaml")
    except Exception:
        try:
            from pathlib import Path

            cfg = load_config(Path("config.example.yaml"))
        except Exception:
            cfg = {}
    e = (cfg or {}).get("ecommerce") if isinstance(cfg, dict) else {}
    if not isinstance(e, dict):
        e = {}
    inq = (cfg or {}).get("inquiry") if isinstance(cfg, dict) else {}
    if not isinstance(inq, dict):
        inq = {}
    return {
        "treat_as_market_ref": bool(e.get("treat_as_market_ref", True)),
        "between_query_sleep_min": float(e.get("between_query_sleep_min", 2.5)),
        "between_query_sleep_max": float(e.get("between_query_sleep_max", 5.0)),
        "captcha_wait_seconds": int(e.get("captcha_wait_seconds", 180)),
        "max_queries_per_item": int(e.get("max_queries_per_item", 2)),
        "fallback_only_when_no_formal": bool(
            e.get("fallback_only_when_no_formal", False)
        ),
        # 造价站检索词预算（建议 4～6）
        "cost_max_queries_per_item": int(inq.get("cost_max_queries_per_item", 6)),
    }


def _ecommerce_throttle(pid: str, e_cfg: dict[str, Any], log: Callable[[str], None] | None) -> None:
    if not is_ecommerce_platform(pid):
        return
    import random

    lo = max(0.5, float(e_cfg.get("between_query_sleep_min") or 2.5))
    hi = max(lo, float(e_cfg.get("between_query_sleep_max") or 5.0))
    sec = random.uniform(lo, hi)
    if log:
        log(f"   [{pid}] 电商限速 sleep {sec:.1f}s …")
    time.sleep(sec)


def _review_norm(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


# 相对报送价：仅作标记，绝不决定是否 match / 是否收录市场报价
# below_submit:   ≤报送×1.02
# near_submit:    报送×1.02～1.10
# above_submit:   >报送×1.10
# suspicious_low: <报送×0.25（异常提示，仍收录）
# unknown:        无报送或无法估算
SUBMIT_BELOW_RATIO = 1.02
SUBMIT_NEAR_RATIO = 1.10
SUBMIT_SUSPICIOUS_LOW_RATIO = 0.25

# 兼容旧名
SUBMIT_UNDER_RATIO = SUBMIT_BELOW_RATIO
SUBMIT_FLOOR_RATIO = SUBMIT_SUSPICIOUS_LOW_RATIO
SUBMIT_FORMAL_MAX_RATIO = SUBMIT_BELOW_RATIO


def _estimate_ex_tax_simple(
    price: float,
    tax_mode: str,
    *,
    platform: str = "",
    tax_divisor: float = 1.13,
) -> float | None:
    if price is None or price <= 0.05:
        return None
    mode = (tax_mode or "unknown").lower()
    if mode == "tax_excl":
        return float(price)
    if mode == "tax_incl":
        return float(price) / max(1.01, float(tax_divisor))
    # 造价站列表多按不含税/市场价展示
    if platform in ("guangcai", "lingcai", "huixun", "yize", "zaojiatong"):
        return float(price)
    return float(price) / max(1.01, float(tax_divisor))


def vs_submit_relation(
    price: float | None,
    *,
    tax_mode: str = "unknown",
    platform: str = "",
    submit: float | None,
    tax_divisor: float = 1.13,
) -> str:
    """
    市场价 vs 报送价关系标记（**不改变 match_ok，不丢弃报价**）。
    返回: below_submit | near_submit | above_submit | suspicious_low | unknown
    """
    if submit is None:
        return "unknown"
    try:
        sub = float(submit)
    except Exception:
        return "unknown"
    if sub <= 0:
        return "unknown"
    if price is None:
        return "unknown"
    try:
        p = float(price)
    except Exception:
        return "unknown"
    if p <= 0.05:
        return "unknown"
    ex = _estimate_ex_tax_simple(p, tax_mode, platform=platform, tax_divisor=tax_divisor)
    if ex is None:
        return "unknown"
    if ex < sub * SUBMIT_SUSPICIOUS_LOW_RATIO:
        return "suspicious_low"
    if ex <= sub * SUBMIT_BELOW_RATIO:
        return "below_submit"
    if ex <= sub * SUBMIT_NEAR_RATIO:
        return "near_submit"
    return "above_submit"


def submit_price_band(
    price: float | None,
    *,
    tax_mode: str = "unknown",
    platform: str = "",
    submit: float | None,
    tax_divisor: float = 1.13,
) -> str:
    """兼容旧 API；新代码请用 vs_submit_relation。"""
    return vs_submit_relation(
        price,
        tax_mode=tax_mode,
        platform=platform,
        submit=submit,
        tax_divisor=tax_divisor,
    )


def price_anomaly_hint(vs: str, *, submit: float | None = None) -> str:
    """由 vs_submit 生成异常提示（空=无异常）。"""
    if vs == "suspicious_low":
        sub = f"（报送{float(submit):.2f}）" if submit else ""
        return f"远低于报送{sub}·请核对规格/单位/是否错品"
    if vs == "above_submit":
        sub = f"（报送{float(submit):.2f}）" if submit else ""
        return f"高于报送{sub}·市场价如实收录，审定请人工取舍"
    return ""


_VS_LABEL_CN = {
    "below_submit": "≤报送",
    "near_submit": "接近报送",
    "above_submit": "高于报送",
    "suspicious_low": "异常偏低",
    "unknown": "",
    # 旧别名
    "under": "≤报送",
    "near": "接近报送",
    "over": "高于报送",
    "low": "异常偏低",
}


def build_review_candidates(
    item: CanonicalItem,
    attempts: list[dict],
    *,
    limit: int = 5,
    match_mode: str = "practical",
    tax_divisor: float = 1.13,
) -> list[Quote]:
    """
    候选待核：有价有链接、名称大致对，但规格未过硬门槛。

    报送价**不**决定是否进待核，只打 vs_submit 标记。
    practical：硬规格冲突（功率/电压/尺寸…）不进待核。
    """
    from .matching import has_hard_spec_conflict, name_search_core, peel_name_dimension_noise

    mode = (match_mode or "practical").lower()
    exact_name = _review_norm(item.name)
    core_name = _review_norm(name_search_core(peel_name_dimension_noise(item.name) or item.name))
    model = ""
    m = re.search(r"[A-Za-z]{1,8}\d{2,}[A-Za-z0-9\-]*", item.name or "", re.I)
    if m:
        model = m.group(0).lower()
    try:
        submit = float(item.submit) if item.submit is not None else None
    except Exception:
        submit = None

    ranked: list[tuple[tuple, dict]] = []
    seen: set[str] = set()
    for a in attempts:
        # 已进正式价的跳过
        if a.get("match_ok") is True or a.get("bucket") == "formal":
            continue
        if a.get("bucket") == "discard":
            continue
        # 硬规格冲突：practical/strict 不进待核（loose 仍可看）
        detail0 = str(a.get("match_detail") or "")
        if mode != "loose" and has_hard_spec_conflict(
            conflicts=a.get("conflicts") or [],
            detail=detail0,
            match_outcome=str(a.get("match_outcome") or ""),
        ):
            continue
        if mode == "strict" and a.get("match_outcome") == "reject":
            continue

        try:
            price = float(a.get("price_tax") or 0)
        except Exception:
            price = 0.0
        # 无有效价：先从正文补抽；仍无价则仅「见价需会员」可保留链接（有报送时不收 0 价）
        if price <= 0.05:
            recovered = _recover_price_from_attempt(a)
            if recovered is not None:
                a = dict(a)
                a["price_tax"] = recovered
                price = recovered
            elif a.get("price_hidden_ok") and submit is None:
                price = 0.0
                a = dict(a)
                a["price_tax"] = None
                a["_price_unknown"] = True
            else:
                continue

        title = str(a.get("title") or "")
        blob = " ".join(
            str(a.get(k) or "") for k in ("title", "spec_seen", "match_detail")
        )
        blob_n = _review_norm(blob)
        detail = str(a.get("match_detail") or "")
        pure_name_miss = "名称未命中" in detail and not a.get("name_hit")
        if pure_name_miss and mode != "loose":
            continue

        exact_hit = bool(exact_name and exact_name in blob_n)
        core_hit = bool(core_name and core_name in blob_n)
        model_hit = bool(model and model in (title + blob).lower().replace("型", ""))
        name_hit = bool(a.get("name_hit") or exact_hit or core_hit or model_hit)
        if not name_hit and mode != "loose":
            continue

        vs = vs_submit_relation(
            price,
            tax_mode=str(a.get("tax_mode") or "unknown"),
            platform=str(a.get("platform") or ""),
            submit=submit,
            tax_divisor=tax_divisor,
        )

        score = float(a.get("match_score") or 0)
        key = "|".join(
            str(a.get(k) or "")
            for k in ("platform", "price_tax", "title", "supplier", "spec_seen")
        )
        if key in seen:
            continue
        seen.add(key)

        a = dict(a)
        a["_vs_submit"] = vs
        # 排序：名称/规格分优先；同档再按价低（价格不决定是否进待核）
        ex = _estimate_ex_tax_simple(
            price,
            str(a.get("tax_mode") or "unknown"),
            platform=str(a.get("platform") or ""),
            tax_divisor=tax_divisor,
        )
        price_key = float(ex) if ex is not None else float(price or 1e18)
        ranked.append(
            (
                (
                    1 if name_hit else 0,
                    1 if model_hit else 0,
                    float(score),
                    -price_key,  # reverse=True 时价低优先
                ),
                a,
            )
        )

    ranked.sort(key=lambda x: x[0], reverse=True)
    out: list[Quote] = []
    for _, a in ranked[: max(1, limit)]:
        try:
            price = float(a.get("price_tax")) if a.get("price_tax") not in (None, "") else 0.0
        except Exception:
            price = 0.0
        if price <= 0.05:
            recovered = _recover_price_from_attempt(a)
            if recovered is not None:
                price = recovered
        page_url = str(a.get("url") or "")
        detail_url = str(a.get("detail_url") or a.get("quotation_url") or page_url)
        detail = str(a.get("match_detail") or "规格证据不足，请人工确认后采用")
        vs = str(a.get("_vs_submit") or "unknown")
        anomaly = price_anomaly_hint(vs, submit=submit)
        lab = _VS_LABEL_CN.get(vs, "")
        if lab and lab not in detail:
            detail = f"[{lab}]{detail}"
        if price <= 0.05:
            if "无数字价" not in detail and "见价需会员" not in detail:
                detail = f"[无数字价·勿当0元采用]{detail}"
            price = 0.0
            price_ex = None
        else:
            price_ex = _estimate_ex_tax_simple(
                price,
                str(a.get("tax_mode") or "unknown"),
                platform=str(a.get("platform") or ""),
                tax_divisor=tax_divisor,
            )
            if price_ex is not None:
                price_ex = r2(price_ex)
        out.append(
            Quote(
                rank=len(out) + 1,
                price=price,
                platform=str(a.get("platform") or ""),
                title=str(a.get("title") or "")[:160],
                url=page_url or detail_url,
                detail_url=detail_url,
                match_level="need_review",
                match_score=float(a.get("match_score") or 0),
                match_detail=detail,
                tax_mode=str(a.get("tax_mode") or "unknown"),
                price_ex_tax=price_ex,
                spec_seen=str(a.get("spec_seen") or ""),
                supplier=str(a.get("supplier") or ""),
                contact=str(a.get("contact") or ""),
                phone=str(a.get("phone") or ""),
                captured_at=datetime.now().isoformat(timespec="seconds"),
                unit=str(a.get("unit") or ""),
                moq=str(a.get("moq") or ""),
                price_text=str(a.get("price_text") or ""),
                price_context=str(a.get("price_context") or ""),
                evidence_scope=str(a.get("sku_scope") or ""),
                source_group_index=a.get("source_group_index"),
                source_quote_index=a.get("source_quote_index"),
                source_row_index=a.get("source_row_index"),
                source_row_label=str(a.get("source_row_label") or ""),
                price_role="review_candidate",
                vs_submit=vs if vs in (
                    "below_submit", "near_submit", "above_submit",
                    "suspicious_low", "unknown",
                ) else "unknown",
                price_anomaly=anomaly,
            )
        )
    return out


def _is_browser_dead_error(exc: BaseException) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    keys = (
        "target closed",
        "targetclosed",
        "browser has been closed",
        "browserclosed",
        "connection closed",
        "context closed",
        "page closed",
        "session closed",
        "protocol error",
        "execution context was destroyed",
        "most likely because of a navigation",
    )
    return any(k in msg for k in keys)


class BrowserSession:
    """可崩溃恢复的浏览器会话：用户关掉窗口后自动再开，继续下一站。

    两种模式：
      - profile 持久化（登录面板/串行询价）：同一 .browser-profile
      - storage_state 临时浏览器（平台 Worker 并发）：注入 Cookie，不占 profile 锁
    """

    def __init__(
        self,
        profile: Path | None,
        channel: str,
        headless: bool,
        *,
        storage_state: dict | str | Path | None = None,
    ):
        self.profile = Path(profile) if profile is not None else None
        self.channel = channel
        self.headless = headless
        self.storage_state = storage_state
        self.pw = None
        self.ctx = None
        self.page = None
        self.browser = None  # ephemeral 模式下的 Browser 句柄
        self._open()

    def _open(self) -> None:
        self.close_quiet()
        if self.storage_state is not None or self.profile is None:
            from .scraper import launch_ephemeral_with_state

            self.pw, self.ctx, self.page, self.browser = launch_ephemeral_with_state(
                self.storage_state,
                channel=self.channel,
                headless=self.headless,
            )
        else:
            self.pw, self.ctx, self.page = launch_context(
                self.profile, channel=self.channel, headless=self.headless
            )
            self.browser = None

    def close_quiet(self) -> None:
        from .scraper import graceful_close_browser

        # 先关 browser（ephemeral），再关 context/pw
        try:
            if self.browser is not None:
                try:
                    self.browser.close()
                except Exception:
                    pass
        except Exception:
            pass
        self.browser = None
        graceful_close_browser(
            self.pw,
            self.ctx,
            self.profile,  # ephemeral 时为 None，只 stop pw
            force_kill=False,
            flush_wait_s=0.8 if self.profile else 0.2,
        )
        self.pw = self.ctx = self.page = None

    def export_storage_state(self) -> dict | None:
        """导出 Cookie 供平台 Worker 注入。"""
        try:
            if self.ctx is not None:
                return self.ctx.storage_state()
        except Exception:
            return None
        return None

    def alive(self) -> bool:
        try:
            if not self.page:
                return False
            _ = self.page.url
            return True
        except Exception:
            return False

    def ensure(self, log: Callable[[str], None] | None = None) -> Any:
        if self.alive():
            return self.page
        if log:
            log("   ⚠ 浏览器已关闭或崩溃 → 自动重新打开，继续询价（不会整单放弃）")
        print("   ⚠ 浏览器已关闭 → 重新启动浏览器，继续下一站")
        self._open()
        return self.page

    def recover(self, log: Callable[[str], None] | None = None) -> Any:
        if log:
            log("   ⚠ 页面异常，重建浏览器会话…")
        self._open()
        return self.page


def _login_one(
    session: BrowserSession,
    pid: str,
    name: str,
    url: str,
    root: Path,
    timeout_s: int,
    timeout_ms: int,
    log: Callable[[str], None] | None = None,
    *,
    prefer_session: bool = True,
) -> str:
    """
    登录单个站并 **真校验**。
    返回: verified | timeout | error

    关键：先打开校验页探测 Cookie 会话，已登录则绝不重开登录页。
    只有会话失效时才 goto login URL 等人手登录。
    """
    def _log(m: str) -> None:
        print(m)
        if log:
            log(m)

    try:
        page = session.ensure(_log)
        check = check_url_for(pid, url)

        # —— 优先复用已有登录态（登录面板 / 上一条材料）——
        if prefer_session:
            # 慧讯/造价通：若浏览器已在产品库/市场价工作台，别再 goto 校验页（会打断会话、诱发互踢）
            if pid in ("huixun", "zaojiatong"):
                try:
                    cur = (page.url or "").lower()
                except Exception:
                    cur = ""
                on_ws = (
                    (pid == "huixun" and "iccchina.com" in cur and "/products" in cur and "login" not in cur)
                    or (
                        pid == "zaojiatong"
                        and "zjtcn.com" in cur
                        and "shichangjia" in cur
                        and "login" not in cur
                        and "member.zjtcn" not in cur
                    )
                )
                if on_ws:
                    ok_ws, reason_ws = ensure_logged_in_or_resume(
                        page, pid, url, user_confirmed=True
                    )
                    if ok_ws:
                        _log(f"    ✓ [{pid}] 已在搜价页，复用会话: {reason_ws}")
                        return "verified"
            _log(f"  · 预检会话 [{pid}] {name} → {check[:70]}")
            try:
                page.goto(check, wait_until="domcontentloaded", timeout=min(timeout_ms, 30000))
                page.wait_for_timeout(700)
            except Exception as e:
                if _is_browser_dead_error(e):
                    page = session.recover(_log)
                    try:
                        page.goto(
                            check, wait_until="domcontentloaded", timeout=min(timeout_ms, 30000)
                        )
                        page.wait_for_timeout(700)
                    except Exception as e2:
                        _log(f"    打开校验页失败: {e2}")
                else:
                    _log(f"    打开校验页失败: {e}")
            ok, reason = ensure_logged_in_or_resume(
                page, pid, url, user_confirmed=False
            )
            if ok:
                _log(f"    ✓ [{pid}] 会话有效，无需重登: {reason}")
                return "verified"
            ok2, reason2 = ensure_logged_in_or_resume(
                page, pid, url, user_confirmed=True
            )
            if ok2:
                _log(f"    ✓ [{pid}] 会话有效（确认/一键登录）: {reason2}")
                return "verified"
            # 慧讯：专开登录页点「一键登录」
            if pid == "huixun" and url:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_timeout(800)
                    ok_h, reason_h = try_resume_huixun_session(page)
                    if ok_h:
                        _log(f"    ✓ [{pid}] {reason_h}")
                        return "verified"
                    reason = reason_h or reason2 or reason
                except Exception as e:
                    _log(f"    慧讯一键登录尝试失败: {e}")
            # 造价通：登录在 member，必须落到 gd 分站，否则每条新链接再要登录
            if pid == "zaojiatong":
                try:
                    ok_z, reason_z = try_resume_zaojiatong_session(page, timeout_ms=timeout_ms)
                    if ok_z:
                        _log(f"    ✓ [{pid}] {reason_z}")
                        return "verified"
                    reason = reason_z or reason2 or reason
                except Exception as e:
                    _log(f"    造价通分站落地失败: {e}")
            _log(f"    [{pid}] 会话无效（{reason}）→ 需要登录")
        else:
            reason = "跳过会话预检"

        # —— 真未登录：打开登录页等用户 ——
        _log(f"  · 打开登录页 [{pid}] {name}  {url}")
        try:
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(800)
        except Exception as e:
            if _is_browser_dead_error(e):
                page = session.recover(_log)
                try:
                    if url:
                        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as e2:
                    _log(f"    打开登录页失败: {e2} → 跳过本站")
                    return "error"
            else:
                _log(f"    打开登录页失败: {e} → 跳过本站")
                return "error"

        # 打开登录页后先尝试慧讯一键登录（有账号缓存时无需人点）
        if pid == "huixun":
            try:
                ok_h, reason_h = try_resume_huixun_session(page)
                if ok_h:
                    _log(f"    ✓ [{pid}] {reason_h}")
                    return "verified"
                _log(f"    慧讯未自动一键登录：{reason_h}，等待手动…")
            except Exception as e:
                _log(f"    慧讯一键登录异常: {e}")
        if pid == "zaojiatong":
            _log(
                "    造价通请在此页登录；登录成功后应自动跳回广东市场价。"
                "若仍停在登录页，登录后点向导「本站已登录」。"
            )

        def _on_wait_start(info: dict) -> None:
            plat = str((info or {}).get("platform") or pid)
            nm = str((info or {}).get("name") or name)
            _log(
                f"⏳ 等待 [{plat}] {nm} 登录：请在弹出浏览器完成登录后，"
                f"点向导「我已登录，继续」（最长约 {int((info or {}).get('timeout_s') or timeout_s)}s）"
            )

        st = wait_for_login_agent(
            page,
            platform_id=pid,
            name=name,
            login_url=url,
            package_root=root,
            timeout_s=timeout_s,
            on_wait_start=_on_wait_start,
        )
        _log(f"    → 等待结束: {st}")
        if not session.alive():
            _log(f"    ⚠ 登录过程中浏览器被关闭 → [{pid}] 未验证，跳过")
            session.recover(_log)
            return "timeout"

        page = session.ensure(_log)
        # 登录后去首页校验（用户确认模式）；慧讯再尝试一键登录
        # agent_continue：用户明确点了继续，用 user_confirmed=True 放宽
        try:
            page.goto(check, wait_until="domcontentloaded", timeout=min(timeout_ms, 30000))
            page.wait_for_timeout(800)
        except Exception:
            pass
        ok3, reason3 = ensure_logged_in_or_resume(
            page, pid, url, user_confirmed=True
        )
        if ok3:
            _log(f"    ✓ [{pid}] 登录校验通过: {reason3}")
            return "verified"
        # 用户已点「继续」但校验页仍无 Cookie：再等一轮短暂确认（避免刚登完 Cookie 未落盘）
        if st in ("agent_continue", "logged_in"):
            _log(f"    [{pid}] 登录信号已收到但校验未过（{reason3}），再等 Cookie 落盘…")
            try:
                page.wait_for_timeout(1500)
                page.goto(check, wait_until="domcontentloaded", timeout=min(timeout_ms, 30000))
                page.wait_for_timeout(1000)
            except Exception:
                pass
            ok3b, reason3b = ensure_logged_in_or_resume(
                page, pid, url, user_confirmed=True
            )
            if ok3b:
                _log(f"    ✓ [{pid}] 二次校验通过: {reason3b}")
                return "verified"
            reason3 = reason3b or reason3
        if pid == "huixun":
            try:
                ok4, reason4 = try_resume_huixun_session(page)
                if ok4:
                    _log(f"    ✓ [{pid}] {reason4}")
                    return "verified"
                reason3 = reason4 or reason3
            except Exception:
                pass
        if pid == "zaojiatong":
            try:
                ok4, reason4 = try_resume_zaojiatong_session(page, timeout_ms=timeout_ms)
                if ok4:
                    _log(f"    ✓ [{pid}] {reason4}")
                    return "verified"
                reason3 = reason4 or reason3
            except Exception:
                pass
        _log(f"    ✗ [{pid}] 登录校验失败: {reason3} → 本站跳过")
        return "timeout"
    except Exception as e:
        _log(f"    登录异常 [{pid}]: {e} → 跳过本站")
        if _is_browser_dead_error(e) or not session.alive():
            try:
                session.recover(_log)
            except Exception:
                pass
        return "error"


def collect_quotes_for_item(
    session: BrowserSession,
    item: CanonicalItem,
    platforms: list[str],
    reg: dict,
    *,
    k: int,
    min_title_score: int,
    timeout_ms: int,
    tax_divisor: float,
    session_skip: set[str],
    session_login_done: set[str],
    root: Path,
    login_timeout: int,
    log: Callable[[str], None] | None = None,
    llm_settings: UserSettings | None = None,
    on_llm: Callable[[str, str, bool], None] | None = None,
    shared_pool: Any | None = None,
    pool_region_code: str = "UNSPECIFIED",
    family_queries: list[str] | None = None,
    name_cache: Any | None = None,
    cancel_token: Any | None = None,
    match_review_limiter: Any | None = None,
    allow_baidu_fallback: bool = True,
) -> QuoteSet:
    """
    严格完全匹配收价。
    **每个平台独立 try**：关窗/报错/无结果 → 只跳过该站，必须继续领材等下一站。
    """
    def _log(msg: str) -> None:
        print(msg)
        if log:
            log(msg)

    def _cancelled() -> bool:
        return bool(
            cancel_token is not None
            and getattr(cancel_token, "is_cancelled", lambda: False)()
        )

    quotes: list[Quote] = []
    market_refs: list[Quote] = []
    attempts: list[dict] = []
    seen_urls: set[str] = set()
    seen_candidate_keys: set[str] = set()
    tried_platforms: list[str] = []
    e_cfg = _ecommerce_cfg()
    treat_ecom_ref = bool(e_cfg.get("treat_as_market_ref", True))
    ecom_max_q = max(1, min(2, int(e_cfg.get("max_queries_per_item") or 2)))
    # 造价站：名称优先，默认 2 词（纯品名 → 品名+硬规格），再多就太慢
    cost_max_q = max(2, min(3, int(e_cfg.get("cost_max_queries_per_item") or 2)))
    captcha_wait = max(30, int(e_cfg.get("captcha_wait_seconds") or 180))
    fallback_only = bool(e_cfg.get("fallback_only_when_no_formal", False))
    # Phase0 性能桶键：材料 id（默认关闭，不影响业务）
    _perf_key = f"item:{getattr(item, 'id', '') or getattr(item, 'name', '') or '_'}"

    # 单条材料共用一个熔断器：跨平台、跨检索词累计，绝不按候选无限复核。
    from .semantic_review import MatchReviewLimiter

    if match_review_limiter is None:
        match_review_limiter = MatchReviewLimiter(
            max_api_calls=max(
                1,
                min(
                    5,
                    int(
                        getattr(
                            llm_settings,
                            "llm_max_match_review_calls_per_item",
                            1,
                        )
                        or 1
                    ),
                ),
            )
        )
    name_prefilter_skips = 0
    match_budget_logged = False

    must = item.must_match or name_must(item)
    tokens = list(item.spec_tokens or [])

    for pid in platforms:
        if _cancelled():
            _log(f"   [cancel] 任务已取消（{getattr(cancel_token, 'reason', '') or 'cancelled'}）→ 停止后续平台")
            attempts.append(
                {
                    "platform": pid,
                    "status": "cancelled",
                    "detail": getattr(cancel_token, "reason", "") or "cancelled",
                }
            )
            break
        if len(quotes) >= k:
            # 正式价已满：电商仅作补充时仍可收集参考；默认跳过以提速
            if not is_ecommerce_platform(pid):
                break
            if fallback_only or treat_ecom_ref:
                _log(f"   [{pid}] 正式价已凑满 → 跳过电商站")
                continue
        if pid in session_skip:
            _log(f"   [{pid}] 本会话未通过登录校验/无会员/风控 → 跳过，换下一站")
            continue
        # 仅没查到再跑电商：已有正式价则跳过京东/1688
        if (
            fallback_only
            and is_ecommerce_platform(pid)
            and len(quotes) > 0
        ):
            _log(f"   [{pid}] 已有合格价且开启「仅没查到跑电商」→ 跳过")
            continue

        tried_platforms.append(pid)
        platform_got = 0
        ecom = is_ecommerce_platform(pid)
        spec = reg.get(pid)
        require_login = bool(spec.require_login_hint) if spec else True

        # 每站独立检索词：造价站「纯品名优先」；电商可 AI 改写
        seed_queries = build_platform_queries(
            pid, item.name, item.spec, item.brand, tokens
        )
        if not seed_queries and item.name:
            from .matching import name_search_core, normalize_material_name

            seed_queries = [
                name_search_core(normalize_material_name(item.name))
                or normalize_material_name(item.name)[:24]
            ]
        # 本地已确认同义名称：追加最多 1 个检索词（不编造规格）
        try:
            seed_queries = expand_queries_with_aliases(
                list(seed_queries or []),
                item.name,
                root,
                max_alias_queries=1,
            )
        except Exception:
            pass
        # 造价站禁止 search_agent 改词（防 LLM 夹地名/乱加词）；仅电商 force
        force_plan_llm = bool(
            ecom
            and llm_settings
            and getattr(llm_settings, "llm_enabled", False)
            and "search_agent" in (getattr(llm_settings, "llm_use_for", None) or [])
            and getattr(llm_settings, "llm_force_search_plan", False)
        )
        platform_queries, q_note = plan_search_queries(
            item=item,
            platform_id=pid,
            seed_queries=seed_queries,
            settings=llm_settings,
            force_llm=force_plan_llm,
        )
        # 分平台检索词预算：造价站默认 3；电商 2
        q_budget = platform_query_budget(
            pid, cost_max=cost_max_q, ecom_max=ecom_max_q
        )
        platform_queries = [
            normalize_search_query(q) for q in (platform_queries or []) if q
        ]
        platform_queries = [q for q in platform_queries if len(q) >= 2][:q_budget]
        # Phase3：材料族 — 主搜纯品名必须排第一，再补硬规格
        if family_queries:
            merged_q: list[str] = []
            for q in list(family_queries) + list(platform_queries):
                qq = normalize_search_query(q)
                if not qq:
                    continue
                # 禁止「DN100 DN100」式重复
                qq = re.sub(r"(?i)\b(DN\d+)\s+\1\b", r"\1", qq)
                if qq.lower() not in {x.lower() for x in merged_q}:
                    merged_q.append(qq)
            platform_queries = merged_q[:q_budget]
            q_note = (q_note or "") + "+family"
        _log(
            f"   [{pid}] 检索词({len(platform_queries)}/{q_budget}) [{q_note}]: "
            + " | ".join(platform_queries[:6])
        )
        if force_plan_llm:
            _log(
                f"   [{pid}] [AI·策略] 电商站启用「AI 改写检索词」"
                + ("（本次有 API 请求，见 Token）" if "规则" not in (q_note or "") else "（回退规则词）")
            )
        if on_llm and q_note and "规则" not in q_note:
            try:
                on_llm("search_agent", f"plan_queries:{q_note[:40]}", True)
            except Exception:
                pass

        # Phase2：平台地区插槽（默认关闭；MPA_REGION_SWITCH=1 或 region_required 时尝试）
        # 顺序：登录后 → 设地区 → 读取 → 校验 → 再搜索
        try:
            region_hook_on = bool(
                getattr(llm_settings, "region_required", False)
            ) if llm_settings else False
            import os as _os

            if (region_hook_on or (_os.environ.get("MPA_REGION_SWITCH") or "").strip() in (
                "1", "true", "yes", "on"
            )):
                from .region_models import RegionTarget
                from .region_platform import ensure_platform_region
                from . import perf as _perf

                tgt_dict = getattr(item, "region", None) or {}
                if isinstance(tgt_dict, dict) and tgt_dict:
                    tgt = RegionTarget.from_dict(tgt_dict)
                else:
                    dreg = getattr(llm_settings, "default_region", None) or {}
                    tgt = RegionTarget.from_dict(dreg if isinstance(dreg, dict) else {})
                    if llm_settings and getattr(llm_settings, "region_strategy", None):
                        tgt.strategy = str(llm_settings.region_strategy)
                if tgt.is_specified():
                    page_r = session.ensure(_log)
                    _perf.inc("region_switch_count", 1, key=_perf_key)
                    ok_r, why_r, meta_r = ensure_platform_region(
                        page_r,
                        pid,
                        tgt,
                        force=True,
                        require_exact=False,
                        log=_log,
                    )
                    attempts.append(
                        {
                            "platform": pid,
                            "status": "region_ok" if ok_r else "region_fail",
                            "detail": why_r,
                            "region_meta": meta_r,
                        }
                    )
                    if ok_r:
                        _perf.inc("region_verify_ok", 1, key=_perf_key)
                    else:
                        _perf.inc("region_verify_fail", 1, key=_perf_key)
                        _log(f"   [{pid}] 地区控制失败 → 本站跳过：{why_r}")
                        session_skip.add(pid)
                        continue
        except Exception as e:
            _log(f"   [{pid}] 地区插槽异常（忽略，继续搜索）: {e}")

        # 已在 session_login_done（skip_login 或面板验证）→ 直接搜
        # 未验证且 require_login → 先登；电商也可搜（失败再标 need_login）
        if require_login and pid not in session_login_done:
            login_url = (spec.login_url if spec else "") or ""
            _log(f"   [{pid}] 尚未验证登录 → 先登录再搜")
            st_login = _login_one(
                session,
                pid,
                spec.name if spec else pid,
                login_url,
                root,
                min(login_timeout, 180),
                timeout_ms,
                log=_log,
            )
            if st_login == "verified":
                session_login_done.add(pid)
            else:
                # 京东等：登录失败仍可尝试公开搜
                if pid in ("jd", "1688"):
                    _log(f"   [{pid}] 登录未过，仍尝试公开搜索…")
                    session_login_done.add(pid)
                elif pid == "zaojiatong":
                    # 造价通：登录失败也继续，但必须先打开市场价页（用户要看见浏览器在干活）
                    _log(
                        "   [zaojiatong] 登录未完整验证 → 仍打开市场价页并按专用规则搜索；"
                        "若列表无数字价请先在弹出窗口登录会员"
                    )
                    session_login_done.add(pid)
                    try:
                        from .adapters import zaojiatong as zjt

                        page = session.ensure(_log)
                        zjt.allow_login_navigation(page, False)
                        ok_ws, why = zjt.open_workspace(page, timeout_ms)
                        _log(f"   [zaojiatong] 工作台: {why}")
                    except Exception as e:
                        _log(f"   [zaojiatong] 打开工作台异常: {e}")
                else:
                    session_skip.add(pid)
                    attempts.append({"platform": pid, "status": f"not_logged_in:{st_login}"})
                    _log(f"   [{pid}] 登录未验证 → **不搜索，直接换下一站**")
                    continue

        _log(f"   → 正在 [{pid}] 搜索（名称+规格完全匹配）…")

        try:
            page = session.ensure(_log)
        except Exception as e:
            _log(f"   [{pid}] 无法打开浏览器: {e} → 试下一站")
            attempts.append({"platform": pid, "status": f"browser_dead:{e}"})
            continue

        # 造价通：进入该站时先强制打开市场价页（避免“页面没开就结束”）
        if pid == "zaojiatong":
            try:
                from .adapters import zaojiatong as zjt

                zjt.allow_login_navigation(page, False)
                ok_ws, why = zjt.open_workspace(page, timeout_ms)
                _log(f"   [zaojiatong] 预开工作台 → {why}")
                if not ok_ws and why == "need_login":
                    _log("   [zaojiatong] 需要登录：请在浏览器完成登录后点继续")
                    # 允许导航到登录页，走一次登录
                    zjt.allow_login_navigation(page, True)
                    st_login = _login_one(
                        session,
                        pid,
                        spec.name if spec else pid,
                        (spec.login_url if spec else "") or "",
                        root,
                        min(login_timeout, 180),
                        timeout_ms,
                        log=_log,
                        prefer_session=False,
                    )
                    zjt.allow_login_navigation(page, False)
                    page = session.ensure(_log)
                    if st_login == "verified":
                        session_login_done.add(pid)
                        zjt.open_workspace(page, timeout_ms)
                    else:
                        _log("   [zaojiatong] 登录仍未通过，继续尝试搜列表（可能无数字价）")
                        zjt.open_workspace(page, timeout_ms)
            except Exception as e:
                _log(f"   [zaojiatong] 预开工作台失败: {e}")

        empty_streak = 0
        tried_queries: list[str] = []
        requery_done = False  # 空结果 / 全规格失败 共用一次改词额度
        seen_result_fps: set[tuple] = set()  # 相同候选集合不重复询价
        # 本站品名批量判决（每条材料每站最多 1 次批量 API，跨查询词复用）
        name_decisions: dict[str, dict] = {}
        name_batch_done = False
        qi = 0
        while qi < len(platform_queries):
            if _cancelled():
                attempts.append(
                    {
                        "platform": pid,
                        "status": "cancelled",
                        "detail": getattr(cancel_token, "reason", "") or "cancelled",
                    }
                )
                break
            query = platform_queries[qi]
            qi += 1
            if len(quotes) >= k:
                break
            tried_queries.append(query)
            useful_this_query = 0  # formal / candidate / 电商参考
            try:
                page = session.ensure(_log)
                perf_mod.inc("query_count", 1, key=_perf_key)
                cands: list = []
                st = "ok"
                pool_key = ""
                used_pool = False
                if shared_pool is not None:
                    try:
                        pool_key = shared_pool.make_key(
                            pid, pool_region_code or "UNSPECIFIED", query
                        )
                        cached = shared_pool.get(pool_key)
                        if cached is not None:
                            cands = list(cached)
                            st = "ok"
                            used_pool = True
                            perf_mod.inc("cache_hits", 1, key=_perf_key)
                            _log(
                                f"   [{pid}] 候选池命中「{query[:28]}」"
                                f" region={pool_region_code} n={len(cands)}"
                            )
                    except Exception:
                        used_pool = False
                if not used_pool:
                    with perf_mod.span("search_ms", key=_perf_key):
                        cands, st = search_on_platform(
                            page,
                            pid,
                            query,
                            must,
                            timeout_ms,
                            min_title_score,
                            reg,
                        )
                    if shared_pool is not None and pool_key and cands is not None:
                        try:
                            # 仅缓存有列表的成功/空结果，避免缓存登录失败态
                            if st in (
                                "ok",
                                "empty_page",
                                "no_list",
                                "",
                            ) or (cands and st not in (
                                "need_login",
                                "captcha",
                                "rate_limited",
                                "no_membership",
                            )):
                                shared_pool.put(
                                    pool_key,
                                    list(cands or []),
                                    meta={
                                        "platform": pid,
                                        "region": pool_region_code,
                                        "query": query,
                                        "status": st,
                                    },
                                )
                        except Exception:
                            pass
                if cands:
                    perf_mod.inc("candidate_count", len(cands), key=_perf_key)
            except Exception as e:
                if _is_browser_dead_error(e):
                    _log(f"   [{pid}] 搜索时浏览器被关 → 恢复后换下一站")
                    try:
                        session.recover(_log)
                    except Exception:
                        pass
                    attempts.append({"platform": pid, "status": "browser_closed", "query": query})
                    break
                _log(f"   [{pid}] 搜索异常: {e}")
                attempts.append({"platform": pid, "status": f"error:{e}", "query": query})
                break  # 不再用下一关键词空刷

            if st == "rate_limited":
                session_skip.add(pid)
                attempts.append({"platform": pid, "status": "rate_limited", "query": query})
                _log(
                    f"   [{pid}] ⚠ 访问过于频繁被平台限流 → 本会话熔断该站，"
                    f"请过 10～30 分钟再试；造价站可继续"
                )
                break
            if st == "captcha":
                attempts.append({"platform": pid, "status": "captcha", "query": query})
                _log(
                    f"   [{pid}] 需要验证码/安全验证 → 请在浏览器完成验证后，"
                    f"向导点继续或 touch data/output/LOGIN_CONTINUE（最多等 {captcha_wait}s）"
                )
                try:
                    cur_url = ""
                    try:
                        cur_url = str(page.url or "")
                    except Exception:
                        cur_url = ""
                    wait_st = wait_for_login_agent(
                        page,
                        platform_id=pid,
                        name=spec.name if spec else pid,
                        login_url=cur_url
                        or ((spec.login_url if spec else "") or ""),
                        package_root=root,
                        timeout_s=captcha_wait,
                        cancel_check=_cancelled,
                    )
                except Exception as e:
                    wait_st = f"error:{e}"
                if wait_st in ("logged_in", "agent_continue", "already_ok", "verified"):
                    _log(f"   [{pid}] 验证后继续，重试当前检索词一次…")
                    try:
                        page = session.ensure(_log)
                        cands, st = search_on_platform(
                            page, pid, query, must, timeout_ms, min_title_score, reg
                        )
                    except Exception as e:
                        _log(f"   [{pid}] 验证后搜索仍失败: {e}")
                        session_skip.add(pid)
                        break
                    if st in ("captcha", "rate_limited"):
                        session_skip.add(pid)
                        _log(f"   [{pid}] 验证后仍 {st} → 本会话跳过该站")
                        break
                else:
                    session_skip.add(pid)
                    _log(f"   [{pid}] 验证等待结束({wait_st}) → 本会话跳过该站")
                    break
            if st == "no_membership":
                session_skip.add(pid)
                attempts.append({"platform": pid, "status": "no_membership", "query": query})
                _log(f"   [{pid}] 无会员 → 换下一站")
                break
            if st == "need_login":
                # 搜索页报未登录：先静默复检；真失效才打开登录等人
                # 造价通：必须让用户在已打开的浏览器里登完，禁止「没打开就结束」
                login_url = (spec.login_url if spec else "") or ""
                _log(f"   [{pid}] 搜索返回 need_login → 请在浏览器完成登录")
                if pid == "zaojiatong":
                    try:
                        from .adapters import zaojiatong as zjt

                        zjt.allow_login_navigation(page, True)
                    except Exception:
                        pass
                st_login = _login_one(
                    session,
                    pid,
                    spec.name if spec else pid,
                    login_url,
                    root,
                    min(login_timeout, 180),
                    timeout_ms,
                    log=_log,
                    prefer_session=True,
                )
                if pid == "zaojiatong":
                    try:
                        from .adapters import zaojiatong as zjt

                        zjt.allow_login_navigation(page, False)
                        page = session.ensure(_log)
                        zjt.open_workspace(page, timeout_ms)
                    except Exception:
                        pass
                if st_login != "verified":
                    # 造价通：登录失败仍可继续用 HTTP 搜名称/规格，不整站放弃
                    if pid == "zaojiatong":
                        _log(
                            "   [zaojiatong] 登录未确认 → 仍用 HTTP 搜列表"
                            "（可能无数字价）；请尽量完成登录"
                        )
                        session_login_done.add(pid)
                        try:
                            page = session.ensure(_log)
                            cands, st = search_on_platform(
                                page, pid, query, must, timeout_ms, min_title_score, reg
                            )
                        except Exception as e:
                            _log(f"   [zaojiatong] 登录后搜索失败: {e}")
                            break
                        if st == "need_login":
                            st = "empty_page"
                            cands = []
                    else:
                        session_login_done.discard(pid)
                        session_skip.add(pid)
                        attempts.append({"platform": pid, "status": "need_login_fail"})
                        _log(f"   [{pid}] 搜索时发现未登录且校验失败 → 换站")
                        break
                else:
                    session_login_done.add(pid)
                    try:
                        page = session.ensure(_log)
                        cands, st = search_on_platform(
                            page, pid, query, must, timeout_ms, min_title_score, reg
                        )
                    except Exception as e:
                        _log(f"   [{pid}] 登录后搜索失败: {e}")
                        break
                    if st in ("need_login", "no_membership") and pid != "zaojiatong":
                        session_skip.add(pid)
                        _log(f"   [{pid}] 登录后仍不可用 → 换站")
                        break
                    if st == "need_login" and pid == "zaojiatong":
                        # 已登录仍报 need_login：当空结果继续，别死循环
                        _log("   [zaojiatong] 已登录仍提示 need_login → 按空结果继续")
                        st = "empty_page"
                        cands = []

            if st and str(st).startswith("error"):
                attempts.append({"platform": pid, "status": st, "query": query})
                _log(f"   [{pid}] {st} → 换站（不重复刷新）")
                break

            if not cands:
                empty_streak += 1
                attempts.append(
                    {"platform": pid, "status": st or "no_list", "query": query, "n": 0}
                )
                _log(f"   [{pid}] 无列表结果: {query[:40]}")
                # 不再因「连续两个空结果」提前终止本站——继续用完预算内剩余检索词
                # 仅在预算将尽且仍空时，触发一次原因感知改词（规则必可用；AI 开启则增强）
                budget_left = len(platform_queries) - qi
                should_try_requery = (
                    not requery_done
                    and (
                        budget_left <= 0
                        or empty_streak >= (1 if ecom else 2)
                    )
                )
                if should_try_requery:
                    try:
                        # 空列表改词：只用规则，不读整页 body、不调 LLM（太慢）
                        reasons = collect_match_fail_reasons(
                            attempts, platform_id=pid
                        ) or ["列表为空"]
                        from .normalize import rule_requery_from_failures

                        extra = rule_requery_from_failures(
                            item.name or "",
                            item.spec or "",
                            item.brand or "",
                            tried_queries,
                            reasons,
                            list(item.spec_tokens or []),
                            max_n=2,
                        )
                        rnote = "规则空结果改词"
                        if extra:
                            requery_done = True
                            added = 0
                            for eq in extra:
                                if eq not in platform_queries and eq not in tried_queries:
                                    # 总查询数不得突破平台预算。
                                    if added >= 2 or len(platform_queries) >= q_budget:
                                        break
                                    platform_queries.append(eq)
                                    added += 1
                            if added:
                                empty_streak = 0
                                _log(
                                    f"   [{pid}] 空结果改词重搜: {rnote} → "
                                    f"{' | '.join(extra[:added])}"
                                )
                                if on_llm and "AI" in (rnote or ""):
                                    try:
                                        on_llm(
                                            "search_agent",
                                            f"requery:{rnote[:40]}",
                                            True,
                                        )
                                    except Exception:
                                        pass
                                continue
                        _log(f"   [{pid}] 改词未给出新词（{rnote}）")
                    except Exception as e:
                        _log(f"   [{pid}] 改词失败（回退继续预算内词）: {e}")
                # 预算内还有词 → 继续下一检索词；用尽才换站
                if qi >= len(platform_queries):
                    _log(f"   [{pid}] 检索词预算已用尽且无列表 → 换下一站")
                continue

            # 有列表结果：清空「连续空」计数
            empty_streak = 0

            # 相同候选集合（按 url/sku）已处理过则跳过，避免同义词重复询价
            try:
                fp = tuple(
                    sorted(
                        {
                            str(c.get("sku") or c.get("url") or c.get("title") or "")[:120]
                            for c in (cands or [])[:30]
                            if c
                        }
                    )
                )
                if fp and fp in seen_result_fps:
                    _log(f"   [{pid}] 候选集合与已查结果重复，跳过本检索词")
                    continue
                if fp:
                    seen_result_fps.add(fp)
            except Exception:
                pass

            # 排序：规格优先（名称/型号口径/硬规格），价格最后
            try:
                ranked, rnote = rank_candidates(
                    item=item,
                    platform_id=pid,
                    candidates=list(cands),
                    settings=llm_settings,
                    top_n=16,
                    tax_divisor=tax_divisor,
                    force_llm=bool(
                        ecom
                        and llm_settings
                        and getattr(llm_settings, "llm_enabled", False)
                        and "search_agent" in (
                            getattr(llm_settings, "llm_use_for", None) or []
                        )
                        and getattr(llm_settings, "llm_force_candidate_rank", False)
                    ),
                )
                if ranked:
                    cands = ranked
                    under_n = sum(
                        1 for c in cands[:8] if c.get("_under_submit") == "under"
                    )
                    _log(
                        f"   [{pid}] 候选排序: {rnote}"
                        + (f"（≤报送约 {under_n} 条）" if under_n else "")
                    )
                    if on_llm and ("AI" in (rnote or "") or "AI+" in (rnote or "")):
                        try:
                            on_llm("search_agent", f"rank:{rnote[:40]}", True)
                        except Exception:
                            pass
            except Exception as e:
                _log(f"   [{pid}] 排序跳过: {e}")

            # 截面/尺寸数字在标题里的优先点（消声器同型号多截面）
            try:
                from .normalize import peel_dims_into_spec

                _n, _s = peel_dims_into_spec(item.name, item.spec)
                dim_nums: list[str] = []
                for m in re.finditer(
                    r"(?<!\d)(\d{2,5})\s*[xX×*]\s*(\d{2,5})", f"{_n} {_s}"
                ):
                    dim_nums.extend([m.group(1), m.group(2)])
                    break
                if dim_nums:
                    def _dim_rank(c: dict) -> tuple:
                        blob = f"{c.get('title') or ''} {c.get('spec_seen') or ''} {c.get('detail_text') or ''}"
                        hit = sum(1 for n in dim_nums if n in blob)
                        return (-hit, -float(c.get("score") or c.get("title_score") or 0))

                    cands = sorted(list(cands), key=_dim_rank)
                    _log(
                        f"   [{pid}] 尺寸优先：标题含 {'×'.join(dim_nums)} 的候选靠前"
                    )
            except Exception:
                pass

            _log(f"   [{pid}] 列表候选 {len(cands)} 条，详情匹配+抽厂家电话…")
            # 品名三级判决：规则→本地库→批量AI；整轮 name_cache 去重
            if not name_batch_done and cands:
                try:
                    titles = [
                        str(c.get("title") or c.get("detail_title") or "")
                        for c in cands[:40]
                    ]
                    if name_cache is not None:
                        name_decisions = name_cache.prepare(
                            inquiry_name=item.name,
                            candidate_titles=titles,
                            settings=llm_settings,
                            root=root,
                            limiter=match_review_limiter,
                            log=_log,
                        )
                    else:
                        from .semantic_review import prepare_item_name_decisions

                        name_decisions = prepare_item_name_decisions(
                            inquiry_name=item.name,
                            candidate_titles=titles,
                            settings=llm_settings,
                            root=root,
                            limiter=match_review_limiter,
                            log=_log,
                        )
                    name_batch_done = True
                except Exception as e:
                    _log(f"   [{pid}] 品名批量判决跳过: {e}")
                    name_batch_done = True
            # 详情页是最大耗时：优先用列表/inline 价，严格限制打开次数
            has_section = bool(
                re.search(r"\d{2,5}\s*[xX×*]\s*\d{2,5}", f"{item.name} {item.spec}")
            )
            n_inline = sum(1 for c in cands if c.get("inline_detail"))
            if n_inline >= 3:
                # 列表已带价/规格：多看几条 inline，几乎不 goto 详情
                cand_limit = 8 if has_section else 6
            else:
                # 每词最多开 2～3 个详情；已有正式价再收紧
                if has_section:
                    cand_limit = 3 if not quotes else 2
                else:
                    cand_limit = 2 if not quotes else 1
            for cand in cands[:cand_limit]:
                if _cancelled():
                    break
                if len(quotes) >= k:
                    break
                url = cand.get("url") or ""
                cand_key = str(cand.get("sku") or url)
                if not url or cand_key in seen_urls or cand_key in seen_candidate_keys:
                    continue
                # Phase4：名称 different 不开详情、不抽规格
                list_title = str(cand.get("title") or cand.get("detail_title") or "")
                if name_decisions and list_title:
                    from .name_aliases import normalize_name_key as _nnk
                    from .name_match import allows_spec_extract

                    _nd0 = name_decisions.get(_nnk(list_title)) or {}
                    if str(_nd0.get("decision") or "") == "different":
                        perf_mod.inc("rejected", 1, key=_perf_key)
                        attempts.append(
                            {
                                "platform": pid,
                                "query": query,
                                "url": url,
                                "title": list_title[:80],
                                "status": "name_different",
                                "match_ok": False,
                                "bucket": "discard",
                                "match_detail": f"[名称·different]{_nd0.get('note') or '不同物'}",
                            }
                        )
                        continue
                    if not allows_spec_extract(str(_nd0.get("decision") or "unknown")):
                        continue
                seen_candidate_keys.add(cand_key)
                try:
                    if cand.get("inline_detail"):
                        title = cand.get("title") or ""
                        body = cand.get("detail_text") or ""
                        cand["final_url"] = url
                        cand["detail_title"] = title
                        cand["detail_confirmed"] = True
                        # 造价通等：列表行 inline 时也要从正文补抽真实价，禁止残留 0.01 占位
                        try:
                            pt = float(cand.get("price_tax") or 0)
                        except Exception:
                            pt = 0.0
                        if pt <= 0.05 or cand.get("needs_detail_price"):
                            recovered = _recover_price_from_attempt(
                                {
                                    "platform": pid,
                                    "price_tax": cand.get("price_tax"),
                                    "price_text": cand.get("price_text"),
                                    "price_context": cand.get("price_context"),
                                    "spec_seen": cand.get("spec_seen") or body,
                                    "title": title,
                                }
                            )
                            if recovered is not None:
                                cand["price_tax"] = recovered
                                cand["needs_detail_price"] = False
                                cand["price_source"] = cand.get("price_source") or "inline_text"
                            elif pid == "zaojiatong":
                                # 再走 HTTP 详情补价（不 page.goto）
                                try:
                                    from .adapters import zaojiatong as zjt

                                    page = session.ensure(_log)
                                    cand = zjt.enrich_detail(page, cand, timeout_ms)
                                    title = cand.get("detail_title") or title
                                    body = str(cand.get("detail_text") or body)[:6000]
                                except Exception:
                                    cand["price_tax"] = None
                    else:
                        page = session.ensure(_log)
                        plat_spec = reg.get(pid)
                        extra = list(plat_spec.detail_price_selectors) if plat_spec else []
                        perf_mod.inc("detail_open_count", 1, key=_perf_key)
                        with perf_mod.span("detail_ms", key=_perf_key):
                            cand = open_detail(
                                page, cand, timeout_ms, extra_price_selectors=extra
                            )
                        # 造价通：详情无价/登录墙时不整站跳过——SSR 列表仍可用，换下一条或换站由匹配结果决定
                        if pid == "zaojiatong" and cand.get("login_wall"):
                            _log(
                                f"   [{pid}] 详情需会员价不可见，跳过本条（不重复弹登录）"
                            )
                            try:
                                restore_platform_workspace(page, pid, reg, timeout_ms)
                            except Exception:
                                pass
                            continue
                        title = cand.get("detail_title") or cand.get("title") or ""
                        # 只使用商品主区/规格区证据，禁止把推荐商品全文混入匹配。
                        body = str(cand.get("detail_text") or "")[:6000]
                        # 慧讯/造价通：详情后立刻回到搜价工作台，下一条只改搜索框
                        if pid in ("huixun", "zaojiatong", "yize", "lingcai"):
                            try:
                                restore_platform_workspace(page, pid, reg, timeout_ms)
                            except Exception:
                                pass
                except Exception as e:
                    if _is_browser_dead_error(e):
                        _log(f"   [{pid}] 详情时浏览器被关 → 换站")
                        try:
                            session.recover(_log)
                        except Exception:
                            pass
                        platform_got = -1
                        break
                    _log(f"   [{pid}] 详情失败: {e}")
                    continue

                # 规格门禁：优先列表/详情规格字段，避免电话/价格噪声误命中 DN
                with perf_mod.span("spec_match_ms", key=_perf_key):
                    mr = strict_name_spec_match(
                        item,
                        title,
                        body,
                        match_spec_text=str(
                            cand.get("match_spec_text") or cand.get("spec_seen") or ""
                        ),
                        match_name_text=str(
                            cand.get("match_name_text") or title or ""
                        ),
                        spec_seen=str(cand.get("spec_seen") or ""),
                    )
                match_mode = (
                    str(getattr(llm_settings, "match_mode", None) or "practical")
                    if llm_settings
                    else "practical"
                )
                # 名称三级链路：批量判决结果 → 同物则重跑规格门禁；不逐候选 AI
                from .matching import has_hard_spec_conflict
                from .name_aliases import normalize_name_key
                from .semantic_review import apply_name_same_then_spec

                hard_block = has_hard_spec_conflict(mr)
                name_is_miss = name_missed(mr)
                name_possible = False  # AI possible → 强制待核
                if (
                    name_is_miss
                    and not hard_block
                    and match_mode in ("strict", "practical")
                ):
                    nd = name_decisions.get(normalize_name_key(title or "")) or {}
                    n_dec = str(nd.get("decision") or "unknown")
                    n_src = str(nd.get("source") or "")
                    n_note = str(nd.get("note") or "")
                    if n_dec == "same":
                        mr = apply_name_same_then_spec(
                            item=item,
                            title=title,
                            evidence_text=body,
                            note=n_note or f"名称同物({n_src})",
                            source_tag=n_src or "name_tier",
                        )
                        name_is_miss = name_missed(mr)
                    elif n_dec == "possible":
                        name_possible = True
                        from .matching import MatchResult as _MR

                        mr = _MR(
                            False,
                            float(nd.get("confidence") or 0.5),
                            mr.required_hit,
                            mr.required_total,
                            f"[名称·待核]可能同物：{n_note or title}"
                            f"；{mr.detail}",
                            "review",
                            "review",
                            missing=mr.missing or (f"品名：{item.name}",),
                            conflicts=(),
                            evidence=mr.evidence + ("name_possible",),
                            llm_decision="possible",
                        )
                    elif n_dec == "different":
                        # 保持名称未命中 reject
                        pass
                    elif not name_decisions:
                        # 无批量结果时：单条语义路径兜底（限流器保护）
                        if match_review_limiter.allow_api():
                            try:
                                from .semantic_review import review_semantic_gray_area

                                mr = review_semantic_gray_area(
                                    item=item,
                                    title=title,
                                    evidence_text=body,
                                    rule_result=mr,
                                    settings=llm_settings,
                                    root=root,
                                    limiter=match_review_limiter,
                                )
                            except Exception as e:
                                _log(f"   [{pid}] 品名兜底复核跳过: {e}")
                elif (
                    not name_is_miss
                    and mr.outcome == "review"
                    and match_mode in ("strict", "practical")
                    and not hard_block
                ):
                    # 规格灰区（非品名）：仍允许单条 match_review，受预算限制
                    if match_review_limiter.allow_api():
                        try:
                            from .semantic_review import review_semantic_gray_area

                            mr = review_semantic_gray_area(
                                item=item,
                                title=title,
                                evidence_text=body,
                                rule_result=mr,
                                settings=llm_settings,
                                root=root,
                                limiter=match_review_limiter,
                            )
                        except Exception as e:
                            _log(f"   [{pid}] 规格灰区复核跳过: {e}")
                    elif not match_budget_logged:
                        _log(
                            f"   [AI·预算] {match_review_limiter.stopped_reason or '达上限'}；"
                            "规则继续"
                        )
                        match_budget_logged = True

                final_url = cand.get("final_url") or url
                unit_ok, unit_reason = unit_compatibility(item.unit, cand.get("unit"))
                price_ambiguous = bool(cand.get("price_ambiguous"))
                bucket, match_outcome, match_detail = decide_quote_bucket(
                    mr,
                    unit_ok=unit_ok,
                    price_ambiguous=price_ambiguous,
                    match_mode=match_mode,
                )
                # 名称 possible：不得正式价，强制候选待核
                if name_possible and bucket == "formal":
                    bucket = "candidate"
                    match_outcome = "review"
                    match_detail = f"[名称·待核·需人工确认]{match_detail}"
                # Phase4：无有效数字价不得进 formal / 有效结果
                try:
                    from .spec_match import has_valid_numeric_price

                    _raw_p = cand.get("price_tax")
                    if bucket == "formal" and not has_valid_numeric_price(_raw_p):
                        if pid == "zaojiatong":
                            # 名称+规格证据齐，但会员价未展示：只能待核，不得正式价。
                            bucket = "candidate"
                            match_outcome = "review"
                            match_detail = f"[无数字价·需会员见价]{match_detail}"
                        else:
                            bucket = "discard"
                            match_outcome = "reject"
                            match_detail = f"[无数字价]{match_detail}"
                except Exception:
                    pass
                # Phase6：地区门禁（region_required=false 且无目标时 passthrough）
                region_ev_dict: dict = {}
                try:
                    from .region_gate import (
                        apply_gate_to_bucket,
                        classify_region_match,
                        decide_region_gate,
                        resolve_target_region,
                    )

                    _tgt = resolve_target_region(
                        item_region=getattr(item, "region", None),
                        item_region_raw=str(getattr(item, "region_raw", "") or ""),
                        task_region=getattr(llm_settings, "default_region", None)
                        if llm_settings
                        else None,
                        user_default=getattr(llm_settings, "default_region", None)
                        if llm_settings
                        else None,
                        strategy=str(
                            getattr(llm_settings, "region_strategy", None)
                            or "strict_city"
                        ),
                    )
                    _blob = f"{title}\n{body}\n{cand.get('spec_seen') or ''}"
                    _ev = classify_region_match(
                        _tgt,
                        source_price_region=str(
                            cand.get("source_price_region") or ""
                        ),
                        platform_selected_region=str(
                            cand.get("platform_selected_region") or ""
                        ),
                        supplier_region=str(cand.get("supplier_region") or ""),
                        raw_text=_blob,
                    )
                    # 禁止供应商地冒充价格地
                    if (
                        not _ev.source_price_region
                        and _ev.supplier_region
                        and "适用" not in _blob
                    ):
                        pass  # 保持空价格地
                    _gate = decide_region_gate(
                        _tgt,
                        _ev,
                        strategy=_tgt.strategy,
                        region_required=bool(
                            getattr(llm_settings, "region_required", False)
                        )
                        if llm_settings
                        else False,
                    )
                    region_ev_dict = _ev.to_dict()
                    new_b, pref = apply_gate_to_bucket(bucket, _gate)
                    if pref:
                        match_detail = f"{pref}；{match_detail}"
                    if new_b == "market_ref" and bucket == "formal":
                        # 走电商参考通道
                        bucket = "candidate"
                        match_outcome = "review"
                        cand["_force_market_ref"] = True
                    else:
                        bucket = new_b
                        if new_b == "discard":
                            match_outcome = "reject"
                        elif new_b == "candidate" and match_outcome == "accept":
                            match_outcome = "review"
                except Exception:
                    region_ev_dict = {}
                if unit_ok is False and "单位" not in match_detail:
                    match_detail = f"{match_detail}；{unit_reason}"
                match_ok = bucket == "formal"
                is_name_hit = not name_missed(mr)
                if bucket == "formal":
                    perf_mod.inc("accepted", 1, key=_perf_key)
                elif bucket == "candidate":
                    perf_mod.inc("review", 1, key=_perf_key)
                elif bucket == "discard":
                    perf_mod.inc("rejected", 1, key=_perf_key)
                attempts.append(
                    {
                        "platform": pid,
                        "query": query,
                        "url": final_url,
                        "price_tax": cand.get("price_tax"),
                        "match_ok": match_ok,
                        "match_outcome": match_outcome,
                        "match_score": mr.score,
                        "match_detail": match_detail,
                        "bucket": bucket,
                        "name_hit": is_name_hit,
                        "missing": list(mr.missing),
                        "conflicts": list(mr.conflicts),
                        "evidence": list(mr.evidence),
                        "title": (title or "")[:80],
                        "supplier": cand.get("supplier") or "",
                        "contact": cand.get("contact") or "",
                        "phone": cand.get("phone") or "",
                        "spec_seen": str(cand.get("spec_seen") or "")[:500],
                        "quotation_url": cand.get("quotation_url") or "",
                        "detail_url": cand.get("quotation_url") or final_url,
                        "unit": cand.get("unit") or "",
                        "unit_check": unit_reason,
                        "price_text": cand.get("price_text") or "",
                        "price_context": cand.get("price_context") or "",
                        "source_group_index": cand.get("source_group_index"),
                        "source_quote_index": cand.get("source_quote_index"),
                        "source_row_index": cand.get("source_row_index"),
                        "source_row_label": cand.get("source_row_label") or "",
                        "moq": cand.get("moq") or "",
                        "sku_scope": cand.get("sku_scope") or "",
                        "tax_mode": cand.get("tax_mode") or "unknown",
                        "llm_invoked": bool(getattr(mr, "llm_invoked", False)),
                        "region_match": region_ev_dict.get("region_match") or "",
                        "source_price_region": region_ev_dict.get("source_price_region")
                        or "",
                        "supplier_region": region_ev_dict.get("supplier_region") or "",
                        "requested_region": region_ev_dict.get("requested_region") or "",
                    }
                )
                if bucket == "discard":
                    _log(f"   [{pid}] × 丢弃：{match_detail}")
                    continue
                raw_price = cand.get("price_tax")
                try:
                    price = float(raw_price) if raw_price not in (None, "") else 0.0
                except Exception:
                    price = 0.0
                # 再兜底：从正文补价（防止 0.01 占位 / 解析漏网）
                if price <= 0.05:
                    recovered = _recover_price_from_attempt(
                        {
                            "platform": pid,
                            "price_tax": raw_price,
                            "price_text": cand.get("price_text"),
                            "price_context": cand.get("price_context"),
                            "spec_seen": cand.get("spec_seen") or body,
                            "title": title,
                        }
                    )
                    if recovered is not None:
                        price = recovered
                        cand["price_tax"] = recovered
                if price <= 0.05:
                    # 造价通：名称规格已对上，但列表/详情真无数字价（会员「查看价格」）
                    # 进待核带链接，价格字段保持空（None），禁止写 0 当报价
                    if pid == "zaojiatong" and bucket in ("formal", "candidate"):
                        _log(
                            f"   [{pid}] ◐ 规格已匹配但无数字价（需会员见价）| "
                            f"{(title or '')[:28]} | {match_detail[:50]}"
                        )
                        attempts.append(
                            {
                                "platform": pid,
                                "query": query,
                                "url": final_url,
                                "price_tax": None,
                                "match_ok": False,
                                "match_outcome": "review",
                                "match_score": float(mr.score),
                                "match_detail": f"[造价通·见价需会员·无数字价]{match_detail}"[:300],
                                "bucket": "candidate",
                                "name_hit": is_name_hit,
                                "missing": list(mr.missing),
                                "conflicts": list(mr.conflicts),
                                "evidence": list(mr.evidence),
                                "title": (title or "")[:80],
                                "supplier": cand.get("supplier") or "",
                                "spec_seen": str(cand.get("spec_seen") or body or "")[:500],
                                "detail_url": final_url,
                                "unit": cand.get("unit") or "",
                                "price_text": cand.get("price_text") or "",
                                "price_hidden_ok": True,
                            }
                        )
                        seen_urls.add(cand_key)
                        continue
                    _log(f"   [{pid}] × 无有效价格：{match_detail}")
                    continue
                tax_mode = str(cand.get("tax_mode") or "unknown")
                price_ex_tax = (
                    r2(price)
                    if tax_mode == "tax_excl"
                    else r2(price / tax_divisor)
                    if tax_mode == "tax_incl"
                    else None
                )
                # 报送价关系：只标记，不改变 match_ok，不丢弃真实市场价
                submit_f = None
                try:
                    submit_f = (
                        float(item.submit) if item.submit is not None else None
                    )
                except Exception:
                    submit_f = None
                vs_rel = vs_submit_relation(
                    price,
                    tax_mode=tax_mode,
                    platform=pid,
                    submit=submit_f,
                    tax_divisor=tax_divisor,
                )
                anomaly = price_anomaly_hint(vs_rel, submit=submit_f)
                vs_lab = _VS_LABEL_CN.get(vs_rel, "")

                # 电商：永不进正式合格价 → 市场参考
                force_market = (ecom and treat_ecom_ref) or bool(
                    cand.get("_force_market_ref")
                )
                if bucket == "candidate" or force_market:
                    if force_market and bucket == "formal":
                        match_detail = f"[电商参考]{match_detail}"
                        _log(
                            f"   [{pid}] ◎ 市场参考 ¥{price} | {(title or '')[:28]} | "
                            f"不进合格价 | {match_detail[:50]}"
                        )
                    else:
                        _log(
                            f"   [{pid}] ◐ 候选待核 ¥{price}"
                            f"{f'({vs_lab})' if vs_lab else ''} | "
                            f"{(title or '')[:28]} | {match_detail[:60]}"
                        )
                    if force_market and price > 0.05:
                        market_refs.append(
                            Quote(
                                rank=len(market_refs) + 1,
                                price=price,
                                platform=pid,
                                title=title[:160],
                                url=final_url,
                                match_level="market_ref",
                                match_score=float(mr.score),
                                match_detail=match_detail,
                                tax_mode=tax_mode,
                                price_ex_tax=price_ex_tax,
                                sku=str(cand.get("sku") or ""),
                                captured_at=datetime.now().isoformat(timespec="seconds"),
                                supplier=str(cand.get("supplier") or ""),
                                contact=str(cand.get("contact") or ""),
                                phone=str(cand.get("phone") or ""),
                                detail_url=str(cand.get("quotation_url") or final_url),
                                spec_seen=str(cand.get("spec_seen") or ""),
                                unit=str(cand.get("unit") or ""),
                                moq=str(cand.get("moq") or ""),
                                price_text=str(cand.get("price_text") or ""),
                                price_context=str(cand.get("price_context") or ""),
                                evidence_scope=str(cand.get("sku_scope") or "product_detail"),
                                source_group_index=cand.get("source_group_index"),
                                source_quote_index=cand.get("source_quote_index"),
                                source_row_index=cand.get("source_row_index"),
                                source_row_label=str(cand.get("source_row_label") or ""),
                                price_role="market_ref",
                                vs_submit=vs_rel,
                                price_anomaly=anomaly,
                            )
                        )
                        platform_got += 1
                        useful_this_query += 1
                    elif bucket == "candidate":
                        useful_this_query += 1
                    if attempts:
                        attempts[-1]["vs_submit"] = vs_rel
                        attempts[-1]["price_anomaly"] = anomaly
                        attempts[-1]["price_tax"] = price
                    seen_urls.add(cand_key)
                    _ecommerce_throttle(pid, e_cfg, _log)
                    continue

                # formal 市场报价：名称+规格匹配且页面真价 → 收录（与报送无关）
                seen_urls.add(cand_key)
                detail_out = match_detail
                if vs_lab and vs_lab not in detail_out:
                    detail_out = f"[{vs_lab}]{detail_out}"
                quotes.append(
                    Quote(
                        rank=len(quotes) + 1,
                        price=price,
                        platform=pid,
                        title=title[:160],
                        url=final_url,
                        match_level="strict" if match_mode == "strict" else "practical",
                        match_score=float(mr.score),
                        match_detail=detail_out,
                        tax_mode=tax_mode,
                        price_ex_tax=price_ex_tax,
                        sku=str(cand.get("sku") or ""),
                        captured_at=datetime.now().isoformat(timespec="seconds"),
                        supplier=str(cand.get("supplier") or ""),
                        contact=str(cand.get("contact") or ""),
                        phone=str(cand.get("phone") or ""),
                        detail_url=str(cand.get("quotation_url") or final_url),
                        spec_seen=str(cand.get("spec_seen") or ""),
                        unit=str(cand.get("unit") or ""),
                        moq=str(cand.get("moq") or ""),
                        price_text=str(cand.get("price_text") or ""),
                        price_context=str(cand.get("price_context") or ""),
                        evidence_scope=str(cand.get("sku_scope") or "product_detail"),
                        source_group_index=cand.get("source_group_index"),
                        source_quote_index=cand.get("source_quote_index"),
                        source_row_index=cand.get("source_row_index"),
                        source_row_label=str(cand.get("source_row_label") or ""),
                        price_role="formal",
                        vs_submit=vs_rel,
                        price_anomaly=anomaly,
                    )
                )
                if attempts:
                    attempts[-1]["vs_submit"] = vs_rel
                    attempts[-1]["price_anomaly"] = anomaly
                    attempts[-1]["match_ok"] = True
                    attempts[-1]["bucket"] = "formal"
                    attempts[-1]["price_tax"] = price
                platform_got += 1
                useful_this_query += 1
                _log(
                    f"   [{pid}] ✓ 市场报价{len(quotes)}/{k} ¥{price}"
                    f"{f'(不含税≈{price_ex_tax})' if price_ex_tax is not None else ''}"
                    f"{f' {vs_lab}' if vs_lab else ''}"
                    f"{f' ⚠{anomaly[:24]}' if anomaly else ''} | {title[:28]} | "
                    f"厂家={cand.get('supplier') or '-'} 电话={cand.get('phone') or '-'}"
                )
                _ecommerce_throttle(pid, e_cfg, _log)
                # 已凑够条数则少开详情提速（与是否超报送无关）
                if len(quotes) >= k:
                    break

            # 有列表但全部规格不匹配 → 一次原因感知改词（规则可用；AI 增强）
            if (
                cands
                and useful_this_query == 0
                and not requery_done
                and len(quotes) < k
            ):
                try:
                    reasons = collect_match_fail_reasons(attempts, platform_id=pid)
                    if not reasons:
                        reasons = ["规格不匹配"]
                    extra, rnote = suggest_requery(
                        item=item,
                        platform_id=pid,
                        tried_queries=tried_queries,
                        page_hint="",
                        settings=llm_settings,
                        fail_reasons=reasons,
                    )
                    if extra:
                        requery_done = True
                        added = 0
                        for eq in extra:
                            if eq in platform_queries or eq in tried_queries:
                                continue
                            if added >= 2 or len(platform_queries) >= q_budget:
                                break
                            platform_queries.append(eq)
                            added += 1
                        if added:
                            _log(
                                f"   [{pid}] 列表有结果但规格全不匹配 → 改词: {rnote} | "
                                f"原因={reasons[:4]} → {' | '.join(extra[:added])}"
                            )
                            if on_llm and "AI" in (rnote or ""):
                                try:
                                    on_llm(
                                        "search_agent",
                                        f"mismatch_requery:{rnote[:40]}",
                                        True,
                                    )
                                except Exception:
                                    pass
                except Exception as e:
                    _log(f"   [{pid}] 规格失败改词异常: {e}")

            if platform_got < 0:
                break
            # 每个检索词之间限速（电商）
            _ecommerce_throttle(pid, e_cfg, _log)

        if ecom:
            if platform_got == 0:
                _log(f"   [{pid}] 本站无市场参考结果 → **换下一站**")
            elif platform_got > 0:
                _log(f"   [{pid}] 本站 {platform_got} 条市场参考（不进合格价）")
        elif len(quotes) < k:
            if platform_got == 0:
                _log(f"   [{pid}] 本站无完全匹配 → **换下一站**")
            elif platform_got > 0:
                _log(f"   [{pid}] 本站 {platform_got} 条，未满 {k} → 继续下一站")

    if llm_settings and getattr(llm_settings, "llm_enabled", False):
        _log(
            f"   [AI·保护汇总] match_review 实际请求 "
            f"{match_review_limiter.api_calls}/{match_review_limiter.max_api_calls}；"
            f"明显不同候选规则直拒 {name_prefilter_skips} 条"
        )

    # —— 百度全网兜底：仅正式价不足 K、本条未做过时触发一次 ——
    web_refs: list[Quote] = []
    supplier_leads: list[Quote] = []
    baidu_enabled = bool(allow_baidu_fallback) and not _cancelled()
    if llm_settings is not None:
        baidu_enabled = bool(
            getattr(llm_settings, "baidu_fallback_enabled", True)
        )
    baidu_already_done = any(
        str(a.get("platform") or "") == "baidu"
        and str(a.get("status") or "") not in ("",)
        for a in attempts
    )
    try:
        from .adapters.baidu_fallback import (
            extract_hard_spec_tokens,
            run_baidu_fallback,
            should_trigger_baidu,
        )

        if should_trigger_baidu(
            formal_quote_count=len(quotes),
            k=k,
            baidu_already_done=baidu_already_done,
            baidu_enabled=baidu_enabled,
        ):
            try:
                page_b = session.ensure(_log)
            except Exception:
                page_b = None
            try:
                bres = run_baidu_fallback(
                    item,
                    page_b,
                    root=root,
                    timeout_ms=min(int(timeout_ms or 20000), 25000),
                    log=_log,
                    baidu_enabled=True,
                    formal_quote_count=len(quotes),
                    k=k,
                    already_done=False,
                )
                web_refs = list(bres.web_refs or [])
                supplier_leads = list(bres.supplier_leads or [])
                attempts.extend(list(bres.attempts or []))
                if bres.skipped_reason:
                    attempts.append(
                        {
                            "platform": "baidu",
                            "status": f"skipped:{bres.skipped_reason}",
                        }
                    )
                # alias_clue → 回原造价平台补搜（最多 +2 查询词，不编造规格）
                if bres.alias_clues and len(quotes) < k:
                    hard = " ".join(
                        extract_hard_spec_tokens(item.name, item.spec)
                    )
                    extra_qs: list[str] = []
                    for al in bres.alias_clues:
                        q = normalize_search_query(f"{al} {hard}".strip())[:60]
                        if q and q.lower() not in {
                            x.lower() for x in extra_qs
                        }:
                            extra_qs.append(q)
                        if len(extra_qs) >= 2:
                            break
                    cost_pids = [
                        p
                        for p in platforms
                        if not is_ecommerce_platform(p)
                        and p not in session_skip
                    ]
                    if extra_qs and cost_pids:
                        _log(
                            f"   [百度·别名补搜] +{len(extra_qs)} 词回造价站: "
                            + " | ".join(extra_qs)
                        )
                        match_mode_alias = (
                            str(
                                getattr(llm_settings, "match_mode", None)
                                or "practical"
                            )
                            if llm_settings
                            else "practical"
                        )
                        for pid in cost_pids:
                            if len(quotes) >= k:
                                break
                            for query in extra_qs:
                                if len(quotes) >= k:
                                    break
                                try:
                                    page = session.ensure(_log)
                                    cands, st = search_on_platform(
                                        page,
                                        pid,
                                        query,
                                        must,
                                        timeout_ms,
                                        min_title_score,
                                        reg,
                                    )
                                except Exception as e:
                                    attempts.append(
                                        {
                                            "platform": pid,
                                            "query": query,
                                            "status": f"alias_reseach_err:{e}",
                                            "via": "baidu_alias",
                                        }
                                    )
                                    if _is_browser_dead_error(e):
                                        try:
                                            session.recover(_log)
                                        except Exception:
                                            pass
                                    break
                                attempts.append(
                                    {
                                        "platform": pid,
                                        "query": query,
                                        "status": st or "ok",
                                        "n": len(cands or []),
                                        "via": "baidu_alias",
                                    }
                                )
                                if not cands or st in (
                                    "captcha",
                                    "rate_limited",
                                    "need_login",
                                    "no_membership",
                                ):
                                    continue
                                for cand in (cands or [])[:8]:
                                    if len(quotes) >= k:
                                        break
                                    url = str(
                                        cand.get("url")
                                        or cand.get("detail_url")
                                        or ""
                                    )
                                    title = str(cand.get("title") or "")
                                    body = str(
                                        cand.get("spec_seen")
                                        or cand.get("body")
                                        or ""
                                    )
                                    cand_key = re.sub(
                                        r"[#?].*$", "", url
                                    ).rstrip("/").lower() or (
                                        f"{pid}|{title[:40]}"
                                    )
                                    if cand_key in seen_urls:
                                        continue
                                    mr = strict_name_spec_match(
                                        item, title, f"{title}\n{body}"
                                    )
                                    unit_ok, _ur = unit_compatibility(
                                        item.unit, cand.get("unit")
                                    )
                                    bucket, mo, md = decide_quote_bucket(
                                        mr,
                                        unit_ok=unit_ok,
                                        price_ambiguous=bool(
                                            cand.get("price_ambiguous")
                                        ),
                                        match_mode=match_mode_alias,
                                    )
                                    if bucket != "formal":
                                        continue
                                    try:
                                        price = float(
                                            cand.get("price_tax") or 0
                                        )
                                    except Exception:
                                        price = 0.0
                                    if price <= 0.05:
                                        recovered = _recover_price_from_attempt(
                                            {
                                                "price_tax": cand.get(
                                                    "price_tax"
                                                ),
                                                "price_text": cand.get(
                                                    "price_text"
                                                ),
                                                "price_context": cand.get(
                                                    "price_context"
                                                ),
                                                "spec_seen": body,
                                                "title": title,
                                            }
                                        )
                                        if recovered is not None:
                                            price = recovered
                                    if price <= 0.05:
                                        continue
                                    tax_mode = str(
                                        cand.get("tax_mode") or "unknown"
                                    )
                                    price_ex_tax = (
                                        r2(price)
                                        if tax_mode == "tax_excl"
                                        else r2(price / tax_divisor)
                                        if tax_mode == "tax_incl"
                                        else None
                                    )
                                    final_url = (
                                        cand.get("final_url")
                                        or cand.get("quotation_url")
                                        or url
                                    )
                                    seen_urls.add(cand_key)
                                    quotes.append(
                                        Quote(
                                            rank=len(quotes) + 1,
                                            price=price,
                                            platform=pid,
                                            title=title[:160],
                                            url=str(url or final_url or ""),
                                            match_level="practical",
                                            match_score=float(mr.score or 0),
                                            match_detail=(
                                                f"[百度别名补搜]{md}"
                                            )[:300],
                                            tax_mode=tax_mode,
                                            price_ex_tax=price_ex_tax,
                                            spec_seen=str(cand.get("spec_seen") or ""),
                                            sku=str(cand.get("sku") or ""),
                                            supplier=str(
                                                cand.get("supplier") or ""
                                            ),
                                            contact=str(
                                                cand.get("contact") or ""
                                            ),
                                            phone=str(
                                                cand.get("phone") or ""
                                            ),
                                            detail_url=str(
                                                cand.get("quotation_url")
                                                or final_url
                                                or url
                                                or ""
                                            ),
                                            unit=str(cand.get("unit") or ""),
                                            moq=str(cand.get("moq") or ""),
                                            price_text=str(
                                                cand.get("price_text") or ""
                                            ),
                                            price_context=str(
                                                cand.get("price_context") or ""
                                            ),
                                            evidence_scope=str(
                                                cand.get("sku_scope") or "product_detail"
                                            ),
                                            source_group_index=cand.get(
                                                "source_group_index"
                                            ),
                                            source_quote_index=cand.get(
                                                "source_quote_index"
                                            ),
                                            source_row_index=cand.get(
                                                "source_row_index"
                                            ),
                                            source_row_label=str(
                                                cand.get("source_row_label") or ""
                                            ),
                                            price_role="formal",
                                            captured_at=datetime.now().isoformat(
                                                timespec="seconds"
                                            ),
                                        )
                                    )
                                    _log(
                                        f"   [{pid}] ✓ 别名补搜正式价 ¥{price} | "
                                        f"{title[:28]}"
                                    )
            except Exception as e:
                _log(f"   [百度兜底] 失败不影响主任务: {e}")
                attempts.append(
                    {
                        "platform": "baidu",
                        "status": f"error:{type(e).__name__}:{e}",
                    }
                )
    except Exception as e:
        _log(f"   [百度兜底] 模块不可用: {e}")

    # 同规格多报价：仅按市场价格排序（低→高）；报送不参与是否匹配
    def _quote_sort_key(q: Quote) -> tuple:
        ex = q.price_ex_tax
        if ex is None and q.price:
            try:
                ex = (
                    float(q.price) / tax_divisor
                    if q.tax_mode == "tax_incl"
                    else float(q.price)
                )
            except Exception:
                ex = None
        return (ex if ex is not None else 1e18, float(q.price or 1e18))

    quotes.sort(key=_quote_sort_key)
    for i, q in enumerate(quotes, 1):
        q.rank = i

    match_mode = (
        str(getattr(llm_settings, "match_mode", None) or "practical")
        if llm_settings
        else "practical"
    )
    # 始终尝试生成候选（无合格价时必用；有合格价也可附带备选）
    review_candidates = build_review_candidates(
        item, attempts, limit=max(k, 5), match_mode=match_mode
    )
    # 去重：market_refs 已单独列出的平台价，不重复塞进 review
    ref_urls = {(m.detail_url or m.url) for m in market_refs}
    if ref_urls:
        review_candidates = [
            r
            for r in review_candidates
            if (r.detail_url or r.url) not in ref_urls
        ]
    for i, m in enumerate(market_refs, 1):
        m.rank = i

    def _extra_clue_note() -> str:
        bits: list[str] = []
        if market_refs:
            bits.append(f"电商参考{len(market_refs)}条")
        if web_refs:
            bits.append(f"全网参考{len(web_refs)}条")
        if supplier_leads:
            bits.append(f"供应商线索{len(supplier_leads)}条")
        return ("；" + "、".join(bits)) if bits else ""

    if len(quotes) >= k:
        status = "full_k"
        msg = f"已凑满 {k} 个合格价（模式={match_mode}；试过: {','.join(tried_platforms)}）"
        msg += _extra_clue_note()
        review_candidates = []  # 已满额不展示造价站待核
    elif quotes:
        status = "partial"
        msg = (
            f"部分合格价 {len(quotes)}/{k}（模式={match_mode}；"
            f"平台: {','.join(tried_platforms)}）"
        )
        msg += _extra_clue_note()
        review_candidates = []
    elif review_candidates:
        status = "need_review"
        best = review_candidates[0]
        price_part = (
            f"¥{best.price:.2f}"
            if best.price and best.price > 0.05
            else "价未展示"
        )
        msg = (
            f"【候选待核】{price_part}（{best.platform} · "
            f"{(best.title or best.supplier or '来源页')[:36]}）—"
            f"{best.match_detail}；请在结果表核对链接后人工采用"
        )
        msg += _extra_clue_note()
        _log(f"   → {msg}")
    elif market_refs:
        status = "need_review"
        best = market_refs[0]
        msg = (
            f"【电商参考】¥{best.price:.2f}（{best.platform}）—"
            f"不作合格价，请人工核对后采用；共 {len(market_refs)} 条"
        )
        msg += _extra_clue_note()
        _log(f"   → {msg}")
    elif web_refs or supplier_leads:
        status = "need_review"
        msg = (
            f"【全网线索】正式价未满；"
            f"全网参考{len(web_refs)}条、供应商线索{len(supplier_leads)}条"
            f"（不作合格价）"
        )
        _log(f"   → {msg}")
    else:
        status = "no_match"
        msg = (
            f"没查到可用候选：已试 [{', '.join(tried_platforms) or '无'}]"
            f"（模式={match_mode}）"
        )

    qset = QuoteSet(
        item_id=item.id,
        quotes=quotes,
        review_candidates=review_candidates,
        market_refs=market_refs,
        web_refs=web_refs,
        supplier_leads=supplier_leads,
        status=status,
        attempts=attempts,
    )
    qset.error = msg
    return qset


def name_must(item: CanonicalItem) -> list[str]:
    from .matching import name_core_words, spec_required_tokens

    must = name_core_words(item.name or "")
    must.extend(spec_required_tokens(item.spec or "", item.name or "")[:4])
    return must[:8] or [((item.name or "")[:4])]


def collect_quotes_via_scheduler(
    *,
    item: CanonicalItem,
    platforms: list[str],
    reg: dict,
    k: int,
    min_title_score: int,
    timeout_ms: int,
    tax_divisor: float,
    session_skip: set[str],
    session_login_done: set[str],
    root: Path,
    login_timeout: int,
    profile: Path,
    channel: str,
    headless: bool,
    log: Callable[[str], None] | None = None,
    llm_settings: UserSettings | None = None,
    on_llm: Callable[[str, str, bool], None] | None = None,
    shared_pool: Any | None = None,
    pool_region_code: str = "UNSPECIFIED",
    family_queries: list[str] | None = None,
    name_cache: Any | None = None,
    max_platforms: int = 3,
    platform_session_pool: Any | None = None,
    storage_state: dict | None = None,
    control_check: Callable[[], str] | None = None,
) -> QuoteSet:
    """
    Phase5：多平台有界并发收集。

    - 优先复用任务级 PlatformSessionPool（一平台一长期 Worker + storage_state 登录态）
    - 无池时：每平台临时浏览器 + 注入 storage_state（不抢主 profile 锁）
    - 满 K 正式价后 cancel 其余平台任务
    """
    from .scheduler import (
        BoundedPlatformScheduler,
        CancelToken,
        merge_platform_quote_sets,
    )

    def _log(msg: str) -> None:
        print(msg)
        if log:
            log(msg)

    plats = [p for p in platforms if p and p not in session_skip]
    if not plats:
        return QuoteSet(item_id=item.id, status="no_match", error="无可用平台")

    def _user_stopped() -> bool:
        if control_check is None:
            return False
        try:
            return str(control_check() or "").strip().lower() in (
                "stop",
                "stopped",
                "stopping",
            )
        except Exception:
            return False

    token = CancelToken(cancel_check=_user_stopped)
    formal_total = {"n": 0}
    lock = __import__("threading").Lock()
    parts: list[QuoteSet] = []

    # 单条材料跨平台共用一个语义复核预算；并行也最多 1 次。
    from .semantic_review import MatchReviewLimiter

    shared_match_limiter = MatchReviewLimiter(
        max_api_calls=max(
            1,
            min(
                5,
                int(
                    getattr(
                        llm_settings,
                        "llm_max_match_review_calls_per_item",
                        1,
                    )
                    or 1
                ),
            ),
        )
    )

    def should_stop() -> bool:
        return formal_total["n"] >= max(1, int(k or 1)) or token.is_cancelled()

    def on_result(r) -> None:
        if not r.ok or r.payload is None or r.cancelled:
            return
        with lock:
            parts.append(r.payload)
            formal_total["n"] = sum(len(getattr(p, "quotes", None) or []) for p in parts)
            if formal_total["n"] >= max(1, int(k or 1)):
                token.cancel("full_k")
                _log(
                    f"   [scheduler] 已满 {k} 正式价 → 取消其余平台任务"
                )

    def worker(pid: str, ctok: CancelToken) -> QuoteSet:
        if ctok.is_cancelled():
            return QuoteSet(item_id=item.id, status="no_match", error="cancelled")
        mode = "pool" if platform_session_pool is not None else "ephemeral"
        _log(f"   [scheduler·{pid}] Worker 启动 mode={mode}")

        def _collect(sess: BrowserSession) -> QuoteSet:
            return collect_quotes_for_item(
                sess,
                item,
                [pid],
                reg,
                k=k,
                min_title_score=min_title_score,
                timeout_ms=timeout_ms,
                tax_divisor=tax_divisor,
                session_skip=set(session_skip),
                session_login_done=set(session_login_done),
                root=root,
                login_timeout=login_timeout,
                log=log,
                llm_settings=llm_settings,
                on_llm=on_llm,
                shared_pool=shared_pool,
                pool_region_code=pool_region_code,
                family_queries=family_queries,
                name_cache=name_cache,
                cancel_token=ctok,
                match_review_limiter=shared_match_limiter,
                # 平台 Worker 只处理本平台；百度由协调器合并后最多执行一次。
                allow_baidu_fallback=False,
            )

        if platform_session_pool is not None:
            return platform_session_pool.run(pid, _collect, cancel_token=ctok)

        # 兼容直接调用：临时会话也在当前 Worker 线程内创建/关闭。
        sess = BrowserSession(
            None,
            channel,
            headless,
            storage_state=storage_state,
        )
        try:
            return _collect(sess)
        finally:
            sess.close_quiet()

    breaker = (
        getattr(platform_session_pool, "breaker", None)
        if platform_session_pool is not None
        else None
    )
    sched = BoundedPlatformScheduler(max_platforms=max_platforms, breaker=breaker)
    _log(
        f"   [scheduler] 一平台一Worker 并发 max_platforms={sched.max_platforms} "
        f"平台={','.join(plats)} "
        f"pool={'yes' if platform_session_pool else 'no'} "
        f"cookies={'yes' if storage_state else 'no'}"
    )
    job_results = sched.submit_platform_jobs(
        plats,
        worker,
        cancel_token=token,
        should_stop=should_stop,
        on_result=on_result,
    )
    for jr in job_results:
        if jr.cancelled:
            _log(f"   [scheduler·{jr.platform_id}] 已取消")
        elif not jr.ok:
            _log(f"   [scheduler·{jr.platform_id}] 失败: {jr.error}")
        else:
            nq = len(getattr(jr.payload, "quotes", None) or [])
            _log(
                f"   [scheduler·{jr.platform_id}] 完成 quotes={nq} "
                f"{jr.elapsed_ms:.0f}ms"
            )

    with lock:
        merged_parts = list(parts)
    # 补上未进 on_result 的成功 payload
    for jr in job_results:
        if jr.ok and jr.payload is not None and jr.payload not in merged_parts:
            merged_parts.append(jr.payload)

    qset = merge_platform_quote_sets(item.id, merged_parts, k=k)

    # 百度只在所有平台完成后由协调器执行一次，且只作全网参考/供应商线索。
    baidu_enabled = bool(
        llm_settings is not None
        and getattr(llm_settings, "baidu_fallback_enabled", False)
    )
    if baidu_enabled and len(qset.quotes) < max(1, int(k or 1)) and not _user_stopped():
        try:
            from .adapters.baidu_fallback import run_baidu_fallback

            def _run_baidu(sess: BrowserSession):
                page = sess.ensure(_log)
                return run_baidu_fallback(
                    item,
                    page,
                    root=root,
                    timeout_ms=min(int(timeout_ms or 20000), 25000),
                    log=_log,
                    baidu_enabled=True,
                    formal_quote_count=len(qset.quotes),
                    k=k,
                    already_done=False,
                )

            if platform_session_pool is not None:
                bres = platform_session_pool.run(plats[0], _run_baidu)
            else:
                temp = BrowserSession(
                    None,
                    channel,
                    headless,
                    storage_state=storage_state,
                )
                try:
                    bres = _run_baidu(temp)
                finally:
                    temp.close_quiet()
            qset.web_refs.extend(list(bres.web_refs or []))
            qset.supplier_leads.extend(list(bres.supplier_leads or []))
            qset.attempts.extend(list(bres.attempts or []))
            if not qset.quotes and (qset.web_refs or qset.supplier_leads):
                qset.status = "need_review"
        except Exception as e:
            qset.attempts.append(
                {
                    "platform": "baidu",
                    "status": f"error:{type(e).__name__}:{e}",
                }
            )
            _log(f"   [百度线索] 单次协调器查询失败（不影响主任务）: {e}")
    if not qset.error:
        qset.error = (
            f"scheduler:完成平台{len(merged_parts)}/"
            f"{len(plats)} max_inflight={sched.stats.max_inflight}"
        )
    _log(
        f"   [scheduler] 合并 status={qset.status} formal={len(qset.quotes)} "
        f"stats={sched.stats.to_dict()}"
    )
    return qset


def run_inquiry(
    *,
    items: list[CanonicalItem],
    platforms: list[str],
    settings: UserSettings,
    cfg: dict,
    root: Path,
    profile: Path,
    skip_login: bool = False,
    login_timeout: int = 180,
    skip_existing: bool = True,
    existing: dict[str, QuoteSet] | None = None,
    limit: int = 0,
    on_progress: Callable[[dict[str, QuoteSet]], None] | None = None,
    on_event: ProgressCb | None = None,
    pre_verified_platforms: list[str] | None = None,
    control_check: Callable[[], str] | None = None,
    # 返回当前是否启用 LLM（询价中可热切换）
    llm_enabled_check: Callable[[], bool] | None = None,
    # continue：跳过已有合格价的材料
    skip_statuses: tuple[str, ...] = ("full_k",),
    run_id: str = "",
    input_path: str = "",
) -> dict[str, QuoteSet]:
    from .run_analytics import (
        build_funnel,
        build_platform_stats,
        classify_fail_reason,
        filter_quote_map_for_items,
        new_run_id,
        normalize_status,
    )

    reg = load_platform_registry(cfg)
    k = max(1, int(settings.quotes_per_item))
    tax = settings.tax_divisor
    min_title = settings.min_title_score
    raw_timeout_ms = int((cfg.get("browser") or {}).get("page_timeout_ms") or 30000)
    # 单次平台操作不得卡一分钟；失败应快速换站。
    timeout_ms = max(8000, min(raw_timeout_ms, 30000))
    # 电商默认更慢，降低「访问频繁」
    sleep_s = float((cfg.get("browser") or {}).get("between_items_sleep") or 1.2)
    if any(p in ("jd", "1688") for p in platforms):
        sleep_s = max(sleep_s, 3.5)
    channel = (cfg.get("browser") or {}).get("channel") or "chrome"
    headless = bool((cfg.get("browser") or {}).get("headless"))

    run_id = (run_id or "").strip() or new_run_id()
    # Phase0：仅当 MPA_PERF=1 时累计；默认关闭不改业务
    if perf_mod.enabled():
        perf_mod.reset()
    # 只保留当前材料范围内的历史（继续询价），禁止混入其它簿/其它任务行
    results: dict[str, QuoteSet] = filter_quote_map_for_items(
        dict(existing or {}), items
    )
    work = items[:limit] if limit and limit > 0 else list(items)
    platforms = [p for p in platforms if p]
    skip_st = set(skip_statuses or ("full_k",))

    def emit(ev: dict[str, Any]) -> None:
        if on_event:
            on_event(ev)

    def _control() -> str:
        if not control_check:
            return "run"
        try:
            c = (control_check() or "run").strip().lower()
        except Exception:
            c = "run"
        if c in ("pause", "paused", "pausing"):
            return "pause"
        if c in ("stop", "stopped", "stopping"):
            return "stop"
        return "run"

    def _wait_control() -> str:
        """处理暂停；返回 run 或 stop。"""
        paused_emitted = False
        while True:
            c = _control()
            if c == "stop":
                return "stop"
            if c == "pause":
                if not paused_emitted:
                    emit(
                        {
                            "type": "paused",
                            "message": "询价已暂停。可点「继续」或「停止」。",
                        }
                    )
                    print("[control] 已暂停，等待继续/停止…")
                    paused_emitted = True
                time.sleep(0.5)
                continue
            if paused_emitted:
                emit({"type": "resumed", "message": "已继续询价"})
                print("[control] 已继续")
            return "run"

    def _settings_for_llm() -> UserSettings:
        """按运行中开关返回 settings（可热关 LLM）。"""
        use = True
        if llm_enabled_check is not None:
            try:
                use = bool(llm_enabled_check())
            except Exception:
                use = bool(settings.llm_enabled)
        else:
            use = bool(settings.llm_enabled)
        if use == bool(settings.llm_enabled):
            return settings
        # 浅拷贝开关，不写回磁盘
        from dataclasses import replace

        return replace(settings, llm_enabled=use)

    print("=== 询价（实用/严格可配；支持暂停停止）===")
    print(f"items={len(work)}  K={k}  platforms={','.join(platforms)}")
    print(f"登录确认: touch {agent_login_signal_path(root)}")
    emit(
        {
            "type": "start",
            "total": len(work),
            "k": k,
            "platforms": platforms,
            "message": (
                f"开始询价：共 {len(work)} 条，每条目标 {k} 个价；"
                f"平台顺序 {' → '.join(platforms)}（前站没有就自动后站）"
            ),
        }
    )

    stopped_early = False
    session = BrowserSession(profile, channel=channel, headless=headless)
    pre_ok = set(pre_verified_platforms or [])
    session_login_done: set[str] = set(pre_ok)
    session_skip: set[str] = set()
    platform_session_pool = None
    storage_state_blob: dict | None = None
    try:
        if pre_ok:
            print(f"[login] 登录面板已验证：{', '.join(pre_ok)}")
            # 关键：面板「强制确认」可能没有真实 Cookie（领材曾中招）。
            # 启动 Worker 前用 profile 浏览器做一次轻量探针，无会话的站移出
            # session_login_done，强制走登录，避免搜价时再卡死。
            from .login_gate import MEMBERSHIP_PLATFORMS, ensure_logged_in_or_resume

            try:
                page0 = session.ensure()
            except Exception as e:
                print(f"[login] 无法打开浏览器做会话复检: {e}")
                page0 = None
            still_ok: set[str] = set()
            need_relogin: list[str] = []
            for pid in list(pre_ok):
                if pid not in platforms:
                    continue
                if page0 is None:
                    need_relogin.append(pid)
                    continue
                if pid not in MEMBERSHIP_PLATFORMS:
                    still_ok.add(pid)
                    continue
                try:
                    check = check_url_for(
                        pid, (reg.get(pid).login_url if reg.get(pid) else "") or ""
                    )
                    page0.goto(
                        check, wait_until="domcontentloaded", timeout=min(timeout_ms, 25000)
                    )
                    page0.wait_for_timeout(600)
                    ok_c, reason_c = ensure_logged_in_or_resume(
                        page0, pid, "", user_confirmed=True
                    )
                    if ok_c:
                        still_ok.add(pid)
                        print(f"  ✓ [{pid}] 面板验证可复用: {reason_c}")
                    else:
                        need_relogin.append(pid)
                        print(
                            f"  ✗ [{pid}] 面板标已登录但会话无效 → 需重登: {reason_c}"
                        )
                except Exception as e:
                    need_relogin.append(pid)
                    print(f"  ✗ [{pid}] 会话复检异常 → 需重登: {e}")
            pre_ok = still_ok
            session_login_done = set(pre_ok)
            if need_relogin:
                emit(
                    {
                        "type": "login",
                        "message": (
                            "以下站登录态已失效，请在弹出浏览器重新登录："
                            + "、".join(need_relogin)
                        ),
                        "platforms": list(need_relogin),
                    }
                )
                for pid in need_relogin:
                    sp = reg.get(pid)
                    name = sp.name if sp else pid
                    url = (sp.login_url if sp else "") or ""
                    st = _login_one(
                        session,
                        pid,
                        name,
                        url,
                        root,
                        login_timeout,
                        timeout_ms,
                        log=lambda m: emit({"type": "log", "message": m}),
                    )
                    if st == "verified":
                        session_login_done.add(pid)
                        emit(
                            {
                                "type": "login_ok",
                                "platform": pid,
                                "message": f"{name} 重新登录校验通过",
                            }
                        )
                    else:
                        session_skip.add(pid)
                        emit(
                            {
                                "type": "login_fail",
                                "platform": pid,
                                "message": f"{name} 重新登录失败，将跳过",
                            }
                        )

        if skip_login:
            # CLI/任务 --skip-login：用现有 cookie 直接搜，不要把站全 skip 掉
            # 若同时有登录面板结果（含已失效被复检摘掉的）：只信任最终 session_login_done
            # 禁止：面板全失效时误落入「允许全部平台」
            had_panel = bool(pre_verified_platforms)
            if had_panel or session_login_done or session_skip:
                session_login_done = set(session_login_done) | set(pre_ok)
                for p in platforms:
                    if p not in session_login_done and p not in session_skip:
                        session_skip.add(p)
                        print(f"  [{p}] 未在登录面板验证 → 本次不搜索")
                print(
                    f"[login] skip_login + 面板验证：可搜={list(session_login_done)} "
                    f"跳过={list(session_skip)}"
                )
            else:
                session_login_done = set(platforms)
                print(
                    f"[login] skip_login：允许直接搜索全部平台 "
                    f"{', '.join(platforms)}（不强制登录面板）"
                )
        else:
            # 只对「未在登录面板验证」的站再走预检
            need = [p for p in platforms if p not in session_login_done and p not in session_skip]
            urls = login_urls_for(list(need), reg)
            if urls:
                names = "、".join(f"{b}({a})" for a, b, _ in urls) or "（无）"
                emit(
                    {
                        "type": "login",
                        "message": f"以下站尚未登录：{names}",
                        "urls": [{"id": a, "name": b, "url": c} for a, b, c in urls],
                        "platforms": list(need),
                    }
                )
                print(f"登录预检（仅未验证站）：{', '.join(need)}")
                for pid, name, url in urls:
                    st = _login_one(
                        session, pid, name, url, root, login_timeout, timeout_ms
                    )
                    if st == "verified":
                        session_login_done.add(pid)
                        print(f"  ✓ [{pid}] 已验证登录")
                        emit(
                            {
                                "type": "login_ok",
                                "platform": pid,
                                "message": f"{name} 登录校验通过",
                            }
                        )
                    else:
                        session_skip.add(pid)
                        print(f"  ✗ [{pid}] 未验证登录 → 搜索跳过")
                        emit(
                            {
                                "type": "login_fail",
                                "platform": pid,
                                "message": f"{name} 未登录成功，将跳过",
                            }
                        )
                    try:
                        session.ensure()
                    except Exception:
                        try:
                            session.recover()
                        except Exception as e:
                            print(f"  无法恢复浏览器: {e}")
                            break
            print(
                f"登录结果：已验证={list(session_login_done)} 跳过={list(session_skip)}"
            )

        # 多平台并行：必须先导出有效 Cookie；否则 Worker 无登录态 → 全站查不到
        from .scheduler import PlatformSessionPool, scheduler_enabled

        usable_plats = [p for p in platforms if p not in session_skip]
        use_sched = scheduler_enabled(settings) and len(usable_plats) > 1
        if use_sched:
            # 轻量唤醒：打开一个已验证站，确保 Cookie 已从 profile 载入再导出
            try:
                page0 = session.ensure()
                warm_pid = next(
                    (p for p in usable_plats if p in session_login_done),
                    usable_plats[0],
                )
                warm_url = ""
                try:
                    sp0 = reg.get(warm_pid)
                    warm_url = (sp0.login_url if sp0 else "") or ""
                    if sp0 and getattr(sp0, "home_url", None):
                        warm_url = sp0.home_url or warm_url
                except Exception:
                    pass
                if warm_url:
                    try:
                        page0.goto(warm_url, wait_until="domcontentloaded", timeout=20000)
                        page0.wait_for_timeout(400)
                    except Exception:
                        pass
            except Exception as e:
                print(f"[scheduler] 预热主浏览器失败: {e}")
            storage_state_blob = session.export_storage_state()
            n_cookies = 0
            try:
                n_cookies = len((storage_state_blob or {}).get("cookies") or [])
            except Exception:
                n_cookies = 0
            if n_cookies < 2:
                # Cookie 没导出成功 → 并行 Worker 必挂；强制改回主浏览器串行
                use_sched = False
                storage_state_blob = None
                print(
                    "[scheduler] Cookie 导出不足，禁用平台并行，改用主浏览器串行"
                    f"（cookies={n_cookies}）"
                )
                emit(
                    {
                        "type": "log",
                        "message": "登录 Cookie 未就绪，改用单浏览器串行询价（保证能查到）",
                    }
                )
            else:
                # 关闭主 profile 浏览器，释放锁；Worker 用 ephemeral + Cookie
                try:
                    session.close_quiet()
                except Exception:
                    pass
                n_plat = len(usable_plats)
                max_p = max(2, min(4, n_plat))
                platform_session_pool = PlatformSessionPool(
                    storage_state=storage_state_blob,
                    channel=channel,
                    headless=headless,
                    max_platforms=max_p,
                )
                print(
                    f"[scheduler] 已启用平台 Worker 池：max_platforms={max_p} "
                    f"cookies={n_cookies} （一平台一浏览器，跨材料复用）"
                )
                emit(
                    {
                        "type": "log",
                        "message": (
                            f"平台并行：最多 {max_p} 站同时查价，"
                            f"已注入 {n_cookies} 条 Cookie"
                        ),
                    }
                )

        done = 0
        # Phase3：材料族共享候选池（flag / MPA_FAMILY_POOL）
        from .family import (
            build_families,
            family_pool_enabled,
            region_code_for_item,
        )
        from .candidate_pool import CandidatePool

        use_family = family_pool_enabled(settings)
        shared_pool: CandidatePool | None = None
        family_by_item: dict[str, Any] = {}
        if use_family:
            shared_pool = CandidatePool(root, use_disk=False)
            fams = build_families(
                work, getattr(settings, "default_region", None)
            )
            for fam in fams:
                for it in fam.items:
                    family_by_item[it.id] = fam
            print(
                f"[family] 启用材料族共享池：{len(fams)} 族 / {len(work)} 条材料"
            )
            for fam in fams[:12]:
                print(
                    f"  · 族「{fam.core_name}」region={fam.region_code} "
                    f"×{len(fam.items)} 主搜={fam.main_query()}"
                )

        # Phase4：整轮名称判决缓存（同品名×候选名只判一次）
        from .name_match import NameDecisionCache

        name_cache = NameDecisionCache(root, use_disk=True)

        for idx, item in enumerate(work, 1):
            # 暂停 / 停止（在每条材料边界）
            if _wait_control() == "stop":
                stopped_early = True
                emit(
                    {
                        "type": "stopped",
                        "index": idx,
                        "total": len(work),
                        "message": (
                            f"已停止询价（完成到第 {done}/{len(work)} 条）。"
                            "可点「继续询价」从断点接着跑未完成材料。"
                        ),
                    }
                )
                print(f"[control] 停止于 {idx}/{len(work)}")
                break

            if skip_existing:
                prev = results.get(item.id)
                skip_it = False
                if prev:
                    if prev.status == "full_k" and len(prev.quotes) >= k:
                        skip_it = True
                    elif prev.status == "partial" and prev.quotes and "partial" in skip_st:
                        skip_it = True
                    elif prev.status in skip_st and prev.status not in (
                        "full_k",
                        "partial",
                    ):
                        skip_it = True
                if skip_it:
                    # 保留原 status，计入本 run 统计（不重跑）
                    if prev and item.id not in results:
                        results[item.id] = prev
                    emit(
                        {
                            "type": "item_skip",
                            "index": idx,
                            "total": len(work),
                            "name": item.name,
                            "status": prev.status if prev else "skipped",
                            "run_id": run_id,
                            "message": f"跳过已完成({prev.status if prev else ''})：{item.name[:40]}",
                        }
                    )
                    continue

            emit(
                {
                    "type": "item_start",
                    "index": idx,
                    "total": len(work),
                    "id": item.id,
                    "name": item.name or "",
                    "spec": item.spec or "",
                    "message": (
                        f"[{idx}/{len(work)}] {item.name or ''}｜"
                        f"规格：{item.spec or '—'}｜平台 {'→'.join(platforms)}"
                    ),
                }
            )
            print(
                f"→ [{item.sheet}] R{item.row} {item.name or ''} | "
                f"{item.spec or ''}"
            )
            print(f"   将依次尝试: {' → '.join(platforms)}")
            logs: list[str] = []

            def _log(msg: str, _logs=logs) -> None:
                _logs.append(msg)

            # 每条材料创建独立设置快照；所有平台 Worker 共享最多 1 次真实 API。
            from copy import copy as _copy
            from .schema_map import LLMItemCallBudget

            item_llm_settings = _copy(_settings_for_llm())
            setattr(
                item_llm_settings,
                "_llm_item_call_budget",
                LLMItemCallBudget(max_calls=1),
            )

            try:
                # 调度模式的主 profile 浏览器已主动关闭；不得在每条材料前又 ensure()
                # 重开一个无用主浏览器。Worker actor 会自行确保各平台会话。
                if not (use_sched and platform_session_pool is not None):
                    try:
                        session.ensure(_log)
                    except Exception as e:
                        _log(f"浏览器无法启动: {e}")
                        results[item.id] = QuoteSet(
                            item_id=item.id,
                            status="error",
                            error=f"浏览器无法启动: {e}",
                        )
                        emit(
                            {
                                "type": "item_done",
                                "index": idx,
                                "total": len(work),
                                "id": item.id,
                                "name": item.name,
                                "status": "error",
                                "message": str(e),
                            }
                        )
                        continue

                if item.parse_status == "fail":
                    qset = QuoteSet(
                        item_id=item.id,
                        status="no_match",
                        error="材料解析失败，无法询价",
                    )
                else:
                    def _on_llm(role: str, decision: str, ok: bool) -> None:
                        emit(
                            {
                                "type": "llm",
                                "role": role,
                                "ok": ok,
                                "detail": f"{item.name[:40]} · {decision}",
                                "model": getattr(settings, "llm_model", "") or "",
                            }
                        )

                    fam = family_by_item.get(item.id) if use_family else None
                    fam_qs = fam.queries_for_item(item) if fam else None
                    reg_code = (
                        fam.region_code
                        if fam
                        else region_code_for_item(
                            item, getattr(settings, "default_region", None)
                        )
                    )
                    if use_sched and platform_session_pool is not None:
                        qset = collect_quotes_via_scheduler(
                            item=item,
                            platforms=platforms,
                            reg=reg,
                            k=k,
                            min_title_score=min_title,
                            timeout_ms=timeout_ms,
                            tax_divisor=tax,
                            session_skip=session_skip,
                            session_login_done=session_login_done,
                            root=root,
                            login_timeout=login_timeout,
                            profile=profile,
                            channel=channel,
                            headless=headless,
                            log=_log,
                            llm_settings=item_llm_settings,
                            on_llm=_on_llm,
                            shared_pool=shared_pool,
                            pool_region_code=reg_code,
                            family_queries=fam_qs,
                            name_cache=name_cache,
                            max_platforms=platform_session_pool.max_platforms,
                            platform_session_pool=platform_session_pool,
                            storage_state=storage_state_blob,
                            control_check=_control,
                        )
                    else:
                        # 串行：主会话可能已关闭（调度路径），必要时重建
                        try:
                            session.ensure()
                        except Exception:
                            try:
                                session = BrowserSession(
                                    profile, channel=channel, headless=headless
                                )
                            except Exception:
                                pass
                        qset = collect_quotes_for_item(
                            session,
                            item,
                            platforms,
                            reg,
                            k=k,
                            min_title_score=min_title,
                            timeout_ms=timeout_ms,
                            tax_divisor=tax,
                            session_skip=session_skip,
                            session_login_done=session_login_done,
                            root=root,
                            login_timeout=login_timeout,
                            log=_log,
                            llm_settings=item_llm_settings,
                            on_llm=_on_llm,
                            shared_pool=shared_pool,
                            pool_region_code=reg_code,
                            family_queries=fam_qs,
                            name_cache=name_cache,
                        )
                results[item.id] = qset
                done += 1
                fail_reason = classify_fail_reason(qset, k=k)
                tip = qset.error or (
                    f"{qset.status}"
                    + (f" · {fail_reason}" if fail_reason else "")
                )
                print(f"   => {qset.status} quotes={len(qset.quotes)} | {tip}")
                q0 = qset.quotes[0] if qset.quotes else None
                def _ai_touched(text: str) -> bool:
                    t = text or ""
                    keys = ("AI ", "AI+", "LLM", "语义复核", "语义", "大模型", "search_agent")
                    return any(k in t for k in keys)

                match_via_llm = any(
                    _ai_touched(q.match_detail or "")
                    for q in (
                        list(qset.quotes)
                        + list(qset.review_candidates)
                        + list(getattr(qset, "market_refs", None) or [])
                    )
                )
                # 本条 attempts 里若走过 AI 灰区，也标
                if not match_via_llm:
                    match_via_llm = any(
                        _ai_touched(str(a.get("match_detail") or ""))
                        or a.get("llm_invoked")
                        for a in (qset.attempts or [])
                    )
                audit = None
                if q0 and q0.price_ex_tax is not None and item.submit is not None:
                    try:
                        audit = min(float(q0.price_ex_tax), float(item.submit))
                    except Exception:
                        audit = q0.price_ex_tax
                elif q0:
                    audit = q0.price_ex_tax if q0.price_ex_tax is not None else q0.price
                emit(
                    {
                        "type": "item_done",
                        "index": idx,
                        "total": len(work),
                        "id": item.id,
                        "name": item.name or "",
                        "sheet": item.sheet,
                        "row": item.row,
                        # 面板必须完整展示询价名称+规格，禁止截断
                        "spec": item.spec or "",
                        "brand": item.brand or "",
                        "unit": item.unit or "",
                        "qty": item.qty,
                        "submit": item.submit,
                        "region_raw": item.region_raw or "",
                        "status": normalize_status(qset.status),
                        "fail_reason": fail_reason,
                        "quotes": len(qset.quotes),
                        "k": k,
                        "message": tip,
                        "run_id": run_id,
                        "match_via_llm": match_via_llm,
                        "platform": q0.platform if q0 else "",
                        "title": (q0.title if q0 else "")[:500],
                        "url": (q0.url if q0 else "") or "",
                        "price": q0.price if q0 else None,
                        "audit": audit,
                        "quote_list": [
                            quote_to_result_row(q, role="formal")
                            for q in qset.quotes[:8]
                        ],
                        "review_list": [
                            quote_to_result_row(q, role="review_candidate")
                            for q in qset.review_candidates[:5]
                        ],
                        "market_list": [
                            quote_to_result_row(q, role="market_ref")
                            for q in (getattr(qset, "market_refs", None) or [])[:5]
                        ],
                        "web_list": [
                            quote_to_result_row(q, role="web_reference")
                            for q in (getattr(qset, "web_refs", None) or [])[:5]
                        ],
                        "supplier_list": [
                            quote_to_result_row(q, role="supplier_lead")
                            for q in (getattr(qset, "supplier_leads", None) or [])[:5]
                        ],
                        "logs": logs[-12:],
                    }
                )
            except Exception as e:
                print(f"   ERROR {type(e).__name__}: {e}")
                # 单条异常也要恢复浏览器，继续下一条/不阻断
                if (
                    _is_browser_dead_error(e)
                    and not (use_sched and platform_session_pool is not None)
                ):
                    try:
                        session.recover()
                    except Exception:
                        pass
                results[item.id] = QuoteSet(item_id=item.id, status="error", error=str(e))
                emit(
                    {
                        "type": "item_done",
                        "index": idx,
                        "total": len(work),
                        "id": item.id,
                        "name": item.name,
                        "status": "error",
                        "message": str(e),
                    }
                )

            if on_progress and done and done % 2 == 0:
                on_progress(results)

            # 每条材料后也响应一次停止（更快）
            if _control() == "stop":
                stopped_early = True
                emit(
                    {
                        "type": "stopped",
                        "index": idx,
                        "total": len(work),
                        "message": (
                            f"已停止询价（完成 {done}/{len(work)} 条）。"
                            "可点「继续询价」从断点接着跑。"
                        ),
                    }
                )
                break
            time.sleep(sleep_s)
    finally:
        if platform_session_pool is not None:
            try:
                close_errors = platform_session_pool.close_all()
                if close_errors:
                    msg = "平台浏览器未完全关净: " + " | ".join(close_errors)
                    print(f"[scheduler] {msg}")
                    emit({"type": "log", "message": msg})
            except Exception as e:
                print(f"[scheduler] 关闭平台 Worker 池失败: {e}")
        try:
            session.close_quiet()
        except Exception:
            pass

    # 只统计本次 work（按 run 隔离，不混入其它任务 evidence）
    scoped_map = {i.id: results.get(i.id) for i in work if results.get(i.id)}
    full = sum(1 for q in scoped_map.values() if q and normalize_status(q.status) == "full_k")
    partial = sum(
        1 for q in scoped_map.values() if q and normalize_status(q.status) == "partial"
    )
    review = sum(
        1 for q in scoped_map.values() if q and normalize_status(q.status) == "need_review"
    )
    none = sum(
        1 for q in scoped_map.values() if q and normalize_status(q.status) == "no_match"
    )
    funnel = build_funnel(work, results, k=k)
    plat_stats = build_platform_stats(results, item_ids={i.id for i in work})

    # 组装按 sheet 分组的完整结果（供前端结果页）
    evidence_rows = quote_map_to_evidence(results, work, k=k, run_id=run_id)
    item_results_full = []
    by_sheet: dict[str, list] = {}
    for it in work:
        d = evidence_rows.get(it.id) or {}
        qset = results.get(it.id)
        q0 = qset.quotes[0] if qset and qset.quotes else None
        st = normalize_status(qset.status if qset else d.get("status") or "no_match")
        fail_reason = (d.get("fail_reason") if d else "") or (
            classify_fail_reason(qset, k=k) if qset else "平台没有结果"
        )
        row = {
            "id": it.id,
            "sheet": it.sheet,
            "row": it.row,
            "name": it.name,
            "spec": it.spec,
            "brand": it.brand,
            "unit": it.unit,
            "qty": it.qty,
            "submit": it.submit,
            "region_raw": it.region_raw,
            "status": st,
            "fail_reason": fail_reason,
            "quotes": len(qset.quotes) if qset else 0,
            "message": (qset.error if qset else "")
            or d.get("hint")
            or d.get("message")
            or (f"没查到：{fail_reason}" if st == "no_match" and fail_reason else ""),
            "platform": q0.platform if q0 else d.get("platform") or "",
            "title": (q0.title if q0 else d.get("title") or "")[:120],
            "url": (q0.url if q0 else d.get("url") or ""),
            "price": q0.price if q0 else d.get("price_tax"),
            "price_ex_tax": q0.price_ex_tax if q0 else d.get("price_ex_tax"),
            "audit": d.get("audit"),
            "run_id": run_id,
            "quote_list": [
                quote_to_result_row(q, role="formal")
                for q in (qset.quotes if qset else [])[:8]
            ],
            "review_list": [
                quote_to_result_row(q, role="review_candidate")
                for q in (qset.review_candidates if qset else [])[:5]
            ],
            "market_list": [
                quote_to_result_row(q, role="market_ref")
                for q in (getattr(qset, "market_refs", None) or [])[:5]
            ],
            "web_list": [
                quote_to_result_row(q, role="web_reference")
                for q in (getattr(qset, "web_refs", None) or [])[:5]
            ],
            "supplier_list": [
                quote_to_result_row(q, role="supplier_lead")
                for q in (getattr(qset, "supplier_leads", None) or [])[:5]
            ],
            "match_via_llm": any(
                "AI " in (q.match_detail or "") or "LLM" in (q.match_detail or "")
                for q in ((qset.quotes + qset.review_candidates) if qset else [])
            ),
        }
        item_results_full.append(row)
        by_sheet.setdefault(it.sheet or "（未命名表）", []).append(row)
    result_by_sheet = [
        {
            "sheet": sheet,
            "count": len(rows),
            "full_k": sum(1 for r in rows if r["status"] == "full_k"),
            "partial": sum(1 for r in rows if r["status"] == "partial"),
            "need_review": sum(1 for r in rows if r["status"] == "need_review"),
            "no_match": sum(
                1 for r in rows if r["status"] in ("no_match", "error", "skipped")
            ),
            "items": sorted(rows, key=lambda x: int(x.get("row") or 0)),
        }
        for sheet, rows in by_sheet.items()
    ]

    done_payload = {
        "full_k": full,
        "partial": partial,
        "need_review": review,
        "no_match": none,
        "run_id": run_id,
        "funnel": funnel,
        "platform_stats": plat_stats,
        "fail_reason_counts": funnel.get("fail_reason_counts") or {},
        "result_by_sheet": result_by_sheet,
        "item_results": item_results_full,
    }
    if not stopped_early:
        emit(
            {
                "type": "done",
                **done_payload,
                "message": (
                    f"完成[{run_id[-12:]}]：满{k}价={full}，部分={partial}，"
                    f"候选待核={review}，没查到={none}"
                ),
            }
        )
    else:
        emit(
            {
                "type": "stopped",
                **done_payload,
                "message": (
                    f"已停止[{run_id[-12:]}]：满{k}价={full}，部分={partial}，"
                    f"候选待核={review}，没查到={none}；可「继续询价」"
                ),
            }
        )
    # Phase0：性能快照仅在开启时发出，不写业务结果
    if perf_mod.enabled():
        try:
            emit({"type": "perf", "run_id": run_id, "perf": perf_mod.snapshot()})
        except Exception:
            pass
    return results


def quote_map_to_evidence(
    quote_map: dict[str, QuoteSet],
    items: list[CanonicalItem],
    *,
    k: int = 3,
    run_id: str = "",
) -> dict[str, dict]:
    """
    QuoteSet → evidence 行。
    状态统一为 full_k|partial|need_review|no_match|error|skipped，
    **不再**把 full_k/partial 覆盖成 verified（旧字段 legacy_verified 仅兼容）。
    """
    from .run_analytics import classify_fail_reason, item_diagnostics, normalize_status

    by_id = {i.id: i for i in items}
    out: dict[str, dict] = {}
    for iid, qset in quote_map.items():
        # 仅输出当前 items 中的行，避免混入其它任务
        it = by_id.get(iid)
        if it is None and items:
            continue
        d = qset.to_dict()
        st = normalize_status(qset.status)
        d["status"] = st
        d["multi_status"] = st
        if run_id:
            d["run_id"] = run_id
        if it:
            d.update(
                {
                    "sheet": it.sheet,
                    "row": it.row,
                    "name": it.name,
                    "spec": it.spec,
                    "submit": it.submit,
                    "qty": it.qty,
                    "brand": it.brand,
                    "unit": it.unit,
                    "region_raw": it.region_raw,
                }
            )
            diag = item_diagnostics(qset, k=k)
            d["fail_reason"] = diag.get("fail_reason") or ""
            if qset.quotes:
                # 兼容旧 UI：曾用 verified 表示「有正式价」
                d["legacy_verified"] = st in ("full_k", "partial")
                q0 = qset.quotes[0]
                d["platform"] = q0.platform
                d["title"] = q0.title
                d["url"] = q0.url
                d["price_tax"] = q0.price
                d["price_ex_tax"] = q0.price_ex_tax
                if it.submit is not None and q0.price_ex_tax is not None:
                    d["audit"] = min(q0.price_ex_tax, float(it.submit))
                else:
                    d["audit"] = q0.price_ex_tax
                d["detail_confirmed"] = True
                d["hint"] = qset.error or ""
                d["message"] = d["hint"]
            elif qset.review_candidates:
                d["hint"] = qset.error or (
                    f"候选待核：{d['fail_reason'] or '规格证据不足'}"
                )
                d["message"] = d["hint"]
            else:
                reason = d["fail_reason"] or classify_fail_reason(qset, k=k)
                d["fail_reason"] = reason
                d["hint"] = qset.error or (
                    f"没查到：{reason}" if reason else "没查到"
                )
                d["message"] = d["hint"]
        out[iid] = d
    return out
