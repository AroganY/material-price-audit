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
from .matching import strict_name_spec_match, unit_compatibility
from .models import CanonicalItem, Quote, QuoteSet
from .platforms import load_platform_registry, login_urls_for, search_on_platform
from .login_gate import check_url_for, verify_logged_in
from .scraper import (
    agent_login_signal_path,
    launch_context,
    open_detail,
    wait_for_login_agent,
)
from .settings_store import UserSettings


ProgressCb = Callable[[dict[str, Any]], None]


def _review_norm(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def build_review_candidates(
    item: CanonicalItem,
    attempts: list[dict],
    *,
    limit: int = 3,
) -> list[Quote]:
    """把“确实搜到、但规格证据不足”的结果单列待核，绝不混入正式合格价。"""
    from .matching import name_search_core

    exact_name = _review_norm(item.name)
    core_name = _review_norm(name_search_core(item.name))
    ranked: list[tuple[tuple[int, float, float], dict]] = []
    seen: set[str] = set()
    for a in attempts:
        if a.get("match_ok") is not False:
            continue
        # 明确型号/规格冲突的候选连“待核”也不能进，避免误导人工。
        if a.get("match_outcome") == "reject":
            continue
        try:
            price = float(a.get("price_tax") or 0)
        except Exception:
            price = 0.0
        if price <= 0.05:
            continue
        blob = " ".join(
            str(a.get(k) or "") for k in ("title", "spec_seen", "match_detail")
        )
        blob_n = _review_norm(blob)
        exact_hit = bool(exact_name and exact_name in blob_n)
        core_hit = bool(core_name and core_name in blob_n)
        score = float(a.get("match_score") or 0)
        # 至少是完整名称明确出现；或核心品名已中且大部分规格已中。
        if not exact_hit and not (core_hit and score >= 0.6):
            continue
        key = "|".join(
            str(a.get(k) or "")
            for k in ("platform", "price_tax", "title", "supplier", "spec_seen")
        )
        if key in seen:
            continue
        seen.add(key)
        ranked.append(((1 if exact_hit else 0, score, -price), a))

    ranked.sort(key=lambda x: x[0], reverse=True)
    out: list[Quote] = []
    for _, a in ranked[: max(1, limit)]:
        price = float(a.get("price_tax") or 0)
        page_url = str(a.get("url") or "")
        detail_url = str(a.get("detail_url") or a.get("quotation_url") or page_url)
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
                match_detail=str(a.get("match_detail") or "规格证据不足"),
                tax_mode=str(a.get("tax_mode") or "unknown"),
                price_ex_tax=(
                    price
                    if a.get("tax_mode") == "tax_excl"
                    else r2(price / 1.13)
                    if a.get("tax_mode") == "tax_incl"
                    else None
                ),
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
    """可崩溃恢复的浏览器会话：用户关掉窗口后自动再开，继续下一站。"""

    def __init__(self, profile: Path, channel: str, headless: bool):
        self.profile = profile
        self.channel = channel
        self.headless = headless
        self.pw = None
        self.ctx = None
        self.page = None
        self._open()

    def _open(self) -> None:
        self.close_quiet()
        self.pw, self.ctx, self.page = launch_context(
            self.profile, channel=self.channel, headless=self.headless
        )

    def close_quiet(self) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.pw = self.ctx = self.page = None

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
) -> str:
    """
    登录单个站并 **真校验**。
    返回: verified | timeout | error
    - 只有 verify_logged_in 通过才算 verified
    - 点「我已登录」后仍会校验；不通过则本站不算登录成功
    """
    def _log(m: str) -> None:
        print(m)
        if log:
            log(m)

    try:
        page = session.ensure(_log)
        _log(f"  · 打开登录 [{pid}] {name}  {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e:
            if _is_browser_dead_error(e):
                page = session.recover(_log)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as e2:
                    _log(f"    打开登录页失败: {e2} → 跳过本站")
                    return "error"
            else:
                _log(f"    打开登录页失败: {e} → 跳过本站")
                return "error"

        # 先打开首页/搜索页快速自检（不要用 login URL 判断）
        check = check_url_for(pid, url)
        try:
            page.goto(check, wait_until="domcontentloaded", timeout=min(timeout_ms, 30000))
            page.wait_for_timeout(600)
        except Exception:
            pass
        ok, reason = verify_logged_in(page, pid, url, user_confirmed=False)
        if ok:
            _log(f"    ✓ [{pid}] 已检测到登录: {reason}")
            return "verified"

        # 未登录：打开登录页等用户
        _log(f"    [{pid}] 未登录（{reason}）→ 打开登录页，请登录后点面板「校验」或等 URL 变化")
        try:
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        st = wait_for_login_agent(
            page,
            platform_id=pid,
            name=name,
            login_url=url,
            package_root=root,
            timeout_s=timeout_s,
        )
        _log(f"    → 等待结束: {st}")
        if not session.alive():
            _log(f"    ⚠ 登录过程中浏览器被关闭 → [{pid}] 未验证，跳过")
            session.recover(_log)
            return "timeout"

        page = session.ensure(_log)
        # 登录后去首页校验（用户确认模式）
        try:
            page.goto(check, wait_until="domcontentloaded", timeout=min(timeout_ms, 30000))
            page.wait_for_timeout(800)
        except Exception:
            pass
        ok2, reason2 = verify_logged_in(page, pid, url, user_confirmed=True)
        if ok2:
            _log(f"    ✓ [{pid}] 登录校验通过: {reason2}")
            return "verified"
        _log(f"    ✗ [{pid}] 登录校验失败: {reason2} → 本站跳过")
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
) -> QuoteSet:
    """
    严格完全匹配收价。
    **每个平台独立 try**：关窗/报错/无结果 → 只跳过该站，必须继续领材等下一站。
    """
    def _log(msg: str) -> None:
        print(msg)
        if log:
            log(msg)

    quotes: list[Quote] = []
    attempts: list[dict] = []
    seen_urls: set[str] = set()
    seen_candidate_keys: set[str] = set()
    tried_platforms: list[str] = []

    # normalize 已按“原名→核心名→带关键规格”排好；行业站最多 5 组，
    # 京东/1688 在下面限制为 2 组，避免频繁刷新触发风控。
    queries = list(item.search_queries or [])[:5]
    if not queries and item.name:
        queries = [item.name[:24]]
    must = item.must_match or name_must(item)

    for pid in platforms:
        if len(quotes) >= k:
            break
        if pid in session_skip:
            _log(f"   [{pid}] 本会话未通过登录校验/无会员 → 跳过，换下一站")
            continue

        tried_platforms.append(pid)
        platform_got = 0
        spec = reg.get(pid)
        require_login = bool(spec.require_login_hint) if spec else True

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
                # 京东等：登录失败仍尝试搜索（列表常可不登录看价）
                if pid in ("jd", "1688"):
                    _log(f"   [{pid}] 登录未过，仍尝试公开搜索…")
                    session_login_done.add(pid)
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

        empty_streak = 0
        platform_queries = queries[:2] if pid in ("jd", "1688") else queries
        for query in platform_queries:
            if len(quotes) >= k:
                break
            try:
                page = session.ensure(_log)
                cands, st = search_on_platform(
                    page, pid, query, must, timeout_ms, min_title_score, reg
                )
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
                    f"   [{pid}] ⚠ 访问过于频繁被平台限流 → 本会话不再搜该站，"
                    f"请过 10～30 分钟再试，或先用广材/领材"
                )
                break
            if st == "captcha":
                session_skip.add(pid)
                attempts.append({"platform": pid, "status": "captcha", "query": query})
                _log(f"   [{pid}] 需要验证码/安全验证 → 本会话跳过该站")
                break
            if st == "no_membership":
                session_skip.add(pid)
                attempts.append({"platform": pid, "status": "no_membership", "query": query})
                _log(f"   [{pid}] 无会员 → 换下一站")
                break
            if st == "need_login":
                session_login_done.discard(pid)
                login_url = (spec.login_url if spec else "") or ""
                st_login = _login_one(
                    session,
                    pid,
                    spec.name if spec else pid,
                    login_url,
                    root,
                    min(login_timeout, 120),
                    timeout_ms,
                    log=_log,
                )
                if st_login != "verified":
                    session_skip.add(pid)
                    attempts.append({"platform": pid, "status": "need_login_fail"})
                    _log(f"   [{pid}] 搜索时发现未登录且校验失败 → 换站")
                    break
                session_login_done.add(pid)
                try:
                    page = session.ensure(_log)
                    cands, st = search_on_platform(
                        page, pid, query, must, timeout_ms, min_title_score, reg
                    )
                except Exception as e:
                    _log(f"   [{pid}] 登录后搜索失败: {e}")
                    break
                if st in ("need_login", "no_membership"):
                    session_skip.add(pid)
                    _log(f"   [{pid}] 登录后仍不可用 → 换站")
                    break

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
                # 第一个词可能过窄；至少尝试下一个核心词/规格词。
                if empty_streak >= 2:
                    _log(f"   [{pid}] 列表为空，停止本站继续刷词 → 换下一站")
                    break
                continue

            _log(f"   [{pid}] 列表候选 {len(cands)} 条，详情匹配+抽厂家电话…")
            cand_limit = 60 if any(c.get("inline_detail") for c in cands) else 8
            for cand in cands[:cand_limit]:
                if len(quotes) >= k:
                    break
                url = cand.get("url") or ""
                cand_key = str(cand.get("sku") or url)
                if not url or cand_key in seen_urls or cand_key in seen_candidate_keys:
                    continue
                seen_candidate_keys.add(cand_key)
                try:
                    if cand.get("inline_detail"):
                        title = cand.get("title") or ""
                        body = cand.get("detail_text") or ""
                        cand["final_url"] = url
                        cand["detail_title"] = title
                        cand["detail_confirmed"] = True
                    else:
                        page = session.ensure(_log)
                        plat_spec = reg.get(pid)
                        extra = list(plat_spec.detail_price_selectors) if plat_spec else []
                        cand = open_detail(page, cand, timeout_ms, extra_price_selectors=extra)
                        title = cand.get("detail_title") or cand.get("title") or ""
                        # 只使用商品主区/规格区证据，禁止把推荐商品全文混入匹配。
                        body = str(cand.get("detail_text") or "")[:6000]
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

                mr = strict_name_spec_match(item, title, body)
                if mr.outcome == "review" and not mr.conflicts:
                    try:
                        from .semantic_review import review_semantic_gray_area

                        mr = review_semantic_gray_area(
                            item=item,
                            title=title,
                            evidence_text=body,
                            rule_result=mr,
                            settings=llm_settings,
                            root=root,
                        )
                    except Exception as e:
                        _log(f"   [{pid}] 语义复核不可用，保留程序判定: {e}")
                final_url = cand.get("final_url") or url
                unit_ok, unit_reason = unit_compatibility(item.unit, cand.get("unit"))
                price_ambiguous = bool(cand.get("price_ambiguous"))
                match_ok = bool(mr.ok and unit_ok is not False and not price_ambiguous)
                match_outcome = mr.outcome
                match_detail = mr.detail
                if unit_ok is False:
                    match_ok = False
                    match_outcome = "reject"
                    match_detail = f"{match_detail}；{unit_reason}"
                elif price_ambiguous:
                    match_ok = False
                    if match_outcome != "reject":
                        match_outcome = "review"
                    match_detail = f"{match_detail}；价格为区间/多档，未绑定明确数量"
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
                        "moq": cand.get("moq") or "",
                        "sku_scope": cand.get("sku_scope") or "",
                        "tax_mode": cand.get("tax_mode") or "unknown",
                    }
                )
                if not match_ok:
                    marker = "冲突拒绝" if match_outcome == "reject" else "证据不足"
                    _log(f"   [{pid}] × {marker}：{match_detail}")
                    continue
                raw_price = cand.get("price_tax")
                try:
                    price = float(raw_price) if raw_price not in (None, "") else 0.0
                except Exception:
                    price = 0.0
                # 占位价 0.01 视为详情未抽到真价
                if price <= 0.05:
                    _log(f"   [{pid}] × 匹配但详情无有效价格")
                    continue
                seen_urls.add(cand_key)
                tax_mode = str(cand.get("tax_mode") or "unknown")
                price_ex_tax = (
                    r2(price)
                    if tax_mode == "tax_excl"
                    else r2(price / tax_divisor)
                    if tax_mode == "tax_incl"
                    else None
                )
                quotes.append(
                    Quote(
                        rank=len(quotes) + 1,
                        price=price,
                        platform=pid,
                        title=title[:160],
                        url=final_url,
                        match_level="strict",
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
                    )
                )
                platform_got += 1
                _log(
                    f"   [{pid}] ✓ 价{len(quotes)}/{k} ¥{price} | {title[:28]} | "
                    f"厂家={cand.get('supplier') or '-'} 电话={cand.get('phone') or '-'}"
                )

            if platform_got < 0:
                break

        if len(quotes) < k:
            if platform_got == 0:
                _log(f"   [{pid}] 本站无完全匹配 → **换下一站**")
            elif platform_got > 0:
                _log(f"   [{pid}] 本站 {platform_got} 条，未满 {k} → 继续下一站")

    for i, q in enumerate(quotes, 1):
        q.rank = i

    if len(quotes) >= k:
        status = "full_k"
        msg = f"已凑满 {k} 个完全匹配价（试过: {','.join(tried_platforms)}）"
    elif quotes:
        status = "partial"
        msg = (
            f"仅找到 {len(quotes)}/{k} 个（已试平台: {','.join(tried_platforms)}；"
            f"其余站无完全匹配）"
        )
    else:
        review_candidates = build_review_candidates(item, attempts, limit=k)
        if review_candidates:
            status = "need_review"
            best = review_candidates[0]
            msg = (
                f"已找到价格候选 ¥{best.price:.2f}（{best.platform}，"
                f"{best.supplier or best.title or '来源页'}），但来源规格证据不足："
                f"{best.match_detail}；正式合格价留空，候选价和链接已写入结果表待复核"
            )
        else:
            status = "no_match"
            msg = (
                f"没查到：已依次尝试 [{', '.join(tried_platforms) or '无'}]，"
                f"名称+规格均无完全匹配（不是只试了第一站就停）"
            )

    qset = QuoteSet(
        item_id=item.id,
        quotes=quotes,
        review_candidates=review_candidates if not quotes else [],
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
) -> dict[str, QuoteSet]:
    reg = load_platform_registry(cfg)
    k = max(1, int(settings.quotes_per_item))
    tax = settings.tax_divisor
    min_title = settings.min_title_score
    timeout_ms = int((cfg.get("browser") or {}).get("page_timeout_ms") or 60000)
    # 电商默认更慢，降低「访问频繁」
    sleep_s = float((cfg.get("browser") or {}).get("between_items_sleep") or 1.2)
    if any(p in ("jd", "1688") for p in platforms):
        sleep_s = max(sleep_s, 3.5)
    channel = (cfg.get("browser") or {}).get("channel") or "chrome"
    headless = bool((cfg.get("browser") or {}).get("headless"))

    results: dict[str, QuoteSet] = dict(existing or {})
    work = items[:limit] if limit and limit > 0 else list(items)
    platforms = [p for p in platforms if p]

    def emit(ev: dict[str, Any]) -> None:
        if on_event:
            on_event(ev)

    print("=== 询价（完全匹配；单站失败必换下一站）===")
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

    session = BrowserSession(profile, channel=channel, headless=headless)
    pre_ok = set(pre_verified_platforms or [])
    session_login_done: set[str] = set(pre_ok)
    session_skip: set[str] = set()
    try:
        if pre_ok:
            print(f"[login] 登录面板已验证：{', '.join(pre_ok)}")

        if skip_login:
            # CLI/任务 --skip-login：用现有 cookie 直接搜，不要把站全 skip 掉
            # 若同时有登录面板结果：只信任已验证站；否则允许全部平台
            if pre_ok:
                session_login_done = set(pre_ok)
                for p in platforms:
                    if p not in session_login_done:
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
            need = [p for p in platforms if p not in session_login_done]
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

        done = 0
        for idx, item in enumerate(work, 1):
            if skip_existing:
                prev = results.get(item.id)
                if prev and prev.status == "full_k" and len(prev.quotes) >= k:
                    emit(
                        {
                            "type": "item_skip",
                            "index": idx,
                            "total": len(work),
                            "name": item.name,
                            "message": f"跳过已完成：{item.name[:40]}",
                        }
                    )
                    continue

            emit(
                {
                    "type": "item_start",
                    "index": idx,
                    "total": len(work),
                    "id": item.id,
                    "name": item.name,
                    "spec": item.spec,
                    "message": f"[{idx}/{len(work)}] {item.name[:40]} | 平台 {'→'.join(platforms)}",
                }
            )
            print(f"→ [{item.sheet}] R{item.row} {item.name[:40]} | {item.spec[:30]}")
            print(f"   将依次尝试: {' → '.join(platforms)}")
            logs: list[str] = []

            def _log(msg: str, _logs=logs) -> None:
                _logs.append(msg)

            try:
                # 每条材料前确保浏览器活着
                try:
                    session.ensure(_log)
                except Exception as e:
                    _log(f"浏览器无法启动: {e}")
                    results[item.id] = QuoteSet(
                        item_id=item.id, status="error", error=f"浏览器无法启动: {e}"
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
                        llm_settings=settings,
                    )
                results[item.id] = qset
                done += 1
                tip = qset.error or qset.status
                print(f"   => {qset.status} quotes={len(qset.quotes)} | {tip}")
                emit(
                    {
                        "type": "item_done",
                        "index": idx,
                        "total": len(work),
                        "id": item.id,
                        "name": item.name,
                        "status": qset.status,
                        "quotes": len(qset.quotes),
                        "k": k,
                        "message": tip,
                        "logs": logs[-12:],
                    }
                )
            except Exception as e:
                print(f"   ERROR {type(e).__name__}: {e}")
                # 单条异常也要恢复浏览器，继续下一条/不阻断
                if _is_browser_dead_error(e):
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
            time.sleep(sleep_s)
    finally:
        session.close_quiet()

    # 试跑 limit 时只统计本次 work，不能把 evidence 里旧的其它行混进完成日志。
    scoped = [results.get(i.id) for i in work]
    full = sum(1 for q in scoped if q and q.status == "full_k")
    partial = sum(1 for q in scoped if q and q.status == "partial")
    review = sum(1 for q in scoped if q and q.status == "need_review")
    none = sum(1 for q in scoped if q and q.status == "no_match")
    emit(
        {
            "type": "done",
            "full_k": full,
            "partial": partial,
            "need_review": review,
            "no_match": none,
            "message": (
                f"完成：满{k}价={full}，部分={partial}，"
                f"候选待核={review}，没查到={none}"
            ),
        }
    )
    return results


def quote_map_to_evidence(quote_map: dict[str, QuoteSet], items: list[CanonicalItem]) -> dict[str, dict]:
    by_id = {i.id: i for i in items}
    out: dict[str, dict] = {}
    for iid, qset in quote_map.items():
        it = by_id.get(iid)
        d = qset.to_dict()
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
                }
            )
            if qset.quotes:
                d["status"] = "verified" if qset.status in ("full_k", "partial") else qset.status
                d["multi_status"] = qset.status
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
            elif qset.review_candidates:
                d["status"] = "need_review"
                d["multi_status"] = "need_review"
                d["hint"] = qset.error or "已找到价格候选，但规格证据不足"
                d["message"] = d["hint"]
            else:
                d["status"] = qset.status or "no_match"
                d["multi_status"] = d["status"]
                d["hint"] = qset.error or "没查到（名称+规格完全匹配）"
                d["message"] = d["hint"]
        out[iid] = d
    return out
