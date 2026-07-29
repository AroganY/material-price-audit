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
from .llm_agent import plan_search_queries, rank_candidates, suggest_requery
from .normalize import build_platform_queries
from .platforms import load_platform_registry, login_urls_for, search_on_platform
from .login_gate import check_url_for, ensure_logged_in_or_resume, try_resume_huixun_session, verify_logged_in
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
    limit: int = 5,
    match_mode: str = "practical",
) -> list[Quote]:
    """
    候选待核：有价有链接、名称大致对，但规格未过硬门槛。
    practical/loose：规格冲突也可进待核（标明原因），供人点选采用。
    strict：仍排除明确 reject。
    """
    from .matching import name_search_core, peel_name_dimension_noise

    mode = (match_mode or "practical").lower()
    exact_name = _review_norm(item.name)
    core_name = _review_norm(name_search_core(peel_name_dimension_noise(item.name) or item.name))
    model = ""
    m = re.search(r"[A-Za-z]{1,8}\d{2,}[A-Za-z0-9\-]*", item.name or "", re.I)
    if m:
        model = m.group(0).lower()
    ranked: list[tuple[tuple[int, int, float, float], dict]] = []
    seen: set[str] = set()
    for a in attempts:
        # 已进正式价的跳过
        if a.get("match_ok") is True or a.get("bucket") == "formal":
            continue
        if a.get("bucket") == "discard" and mode == "strict":
            continue
        try:
            price = float(a.get("price_tax") or 0)
        except Exception:
            price = 0.0
        if price <= 0.05:
            continue
        title = str(a.get("title") or "")
        blob = " ".join(
            str(a.get(k) or "") for k in ("title", "spec_seen", "match_detail")
        )
        blob_n = _review_norm(blob)
        detail = str(a.get("match_detail") or "")
        # 纯名称未命中：strict/practical 不进；loose 可进
        pure_name_miss = "名称未命中" in detail and not a.get("name_hit")
        if pure_name_miss and mode != "loose":
            continue
        if mode == "strict" and a.get("match_outcome") == "reject":
            continue

        exact_hit = bool(exact_name and exact_name in blob_n)
        core_hit = bool(core_name and core_name in blob_n)
        model_hit = bool(model and model in (title + blob).lower().replace("型", ""))
        name_hit = bool(a.get("name_hit") or exact_hit or core_hit or model_hit)
        if not name_hit and mode != "loose":
            continue
        score = float(a.get("match_score") or 0)
        key = "|".join(
            str(a.get(k) or "")
            for k in ("platform", "price_tax", "title", "supplier", "spec_seen")
        )
        if key in seen:
            continue
        seen.add(key)
        # 优先：名称强命中、分高、价接近报送
        submit = float(item.submit) if item.submit is not None else None
        price_gap = abs(price - submit) if submit else 0.0
        ranked.append(
            (
                (
                    1 if name_hit else 0,
                    1 if model_hit else 0,
                    score,
                    -price_gap if submit else -price,
                ),
                a,
            )
        )

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
                match_detail=str(a.get("match_detail") or "规格证据不足，请人工确认后采用"),
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
        from .scraper import graceful_close_browser

        graceful_close_browser(
            self.pw,
            self.ctx,
            self.profile,
            force_kill=False,
            flush_wait_s=0.8,
        )
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
        # 登录后去首页校验（用户确认模式）；慧讯再尝试一键登录
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
        if pid == "huixun":
            try:
                ok4, reason4 = try_resume_huixun_session(page)
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

    must = item.must_match or name_must(item)
    tokens = list(item.spec_tokens or [])

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

        # 每站独立检索词：规则生成 +（可选）AI 按平台改写
        seed_queries = build_platform_queries(
            pid, item.name, item.spec, item.brand, tokens
        )
        if not seed_queries and item.name:
            seed_queries = [item.name[:24]]
        # 默认规则检索词（快）；LLM 改词只在空结果后 suggest_requery
        platform_queries, q_note = plan_search_queries(
            item=item,
            platform_id=pid,
            seed_queries=seed_queries,
            settings=llm_settings,
            force_llm=False,
        )
        # 少刷词：速度优先。电商 2 组，造价站 3 组；空了再 AI 补
        if pid in ("jd", "1688", "taobao", "tmall"):
            platform_queries = platform_queries[:2]
        else:
            platform_queries = platform_queries[:3]
        _log(
            f"   [{pid}] 检索词({len(platform_queries)}) [{q_note}]: "
            + " | ".join(platform_queries[:4])
        )
        if on_llm and q_note and "规则" not in q_note:
            try:
                on_llm("search_agent", f"plan_queries:{q_note[:40]}", True)
            except Exception:
                pass

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
        tried_queries: list[str] = []
        requery_done = False
        qi = 0
        while qi < len(platform_queries):
            query = platform_queries[qi]
            qi += 1
            if len(quotes) >= k:
                break
            tried_queries.append(query)
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
                # 搜索页报未登录：先静默复检 Cookie（登录面板登过时常是误判），
                # 会话仍有效则不弹登录页；真失效才走登录。
                login_url = (spec.login_url if spec else "") or ""
                _log(f"   [{pid}] 搜索返回 need_login → 先复检会话，避免重复登录")
                st_login = _login_one(
                    session,
                    pid,
                    spec.name if spec else pid,
                    login_url,
                    root,
                    min(login_timeout, 120),
                    timeout_ms,
                    log=_log,
                    prefer_session=True,
                )
                if st_login != "verified":
                    session_login_done.discard(pid)
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
                # AI：空结果时允许改词重搜一轮
                if empty_streak >= 2 and not requery_done and llm_settings:
                    try:
                        page_hint = ""
                        try:
                            page_hint = (session.ensure(_log).inner_text("body") or "")[:600]
                        except Exception:
                            pass
                        extra, rnote = suggest_requery(
                            item=item,
                            platform_id=pid,
                            tried_queries=tried_queries,
                            page_hint=page_hint,
                            settings=llm_settings,
                        )
                        if extra:
                            requery_done = True
                            _log(f"   [{pid}] [AI] 改词重搜: {rnote} → {' | '.join(extra)}")
                            if on_llm:
                                try:
                                    on_llm("search_agent", f"requery:{rnote[:40]}", True)
                                except Exception:
                                    pass
                            for eq in extra:
                                if eq not in platform_queries:
                                    platform_queries.append(eq)
                            empty_streak = 0
                            continue
                    except Exception as e:
                        _log(f"   [{pid}] [AI] 改词失败: {e}")
                if empty_streak >= 2:
                    _log(f"   [{pid}] 列表为空，停止本站继续刷词 → 换下一站")
                    break
                continue

            # 排序：默认规则（不超报送优先）；仅模糊时才 LLM
            try:
                ranked, rnote = rank_candidates(
                    item=item,
                    platform_id=pid,
                    candidates=list(cands),
                    settings=llm_settings,
                    top_n=16,
                    tax_divisor=tax_divisor,
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
                    if on_llm and "AI" in (rnote or "") and "规则" not in (rnote or ""):
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
            # 有截面要求时多开几条详情，否则易点到同型号其它尺寸
            has_section = bool(
                re.search(r"\d{2,5}\s*[xX×*]\s*\d{2,5}", f"{item.name} {item.spec}")
            )
            if any(c.get("inline_detail") for c in cands):
                cand_limit = 50 if has_section else 40
            else:
                if has_section:
                    cand_limit = 12 if not quotes else 8
                else:
                    cand_limit = 4 if quotes else 6
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
                match_mode = (
                    str(getattr(llm_settings, "match_mode", None) or "practical")
                    if llm_settings
                    else "practical"
                )
                # 灰区才调 LLM；strict 更依赖模型，practical 以候选为主
                if (
                    mr.outcome == "review"
                    and not mr.conflicts
                    and match_mode in ("strict", "practical")
                ):
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
                        if getattr(mr, "llm_invoked", False):
                            dec = mr.llm_decision or "?"
                            _log(
                                f"   [{pid}] [AI] 语义复核 → {dec} "
                                f"{'(通过)' if mr.ok else '(未改判/拒绝)'}"
                            )
                            if on_llm:
                                try:
                                    on_llm("match_review", dec, bool(mr.ok and dec == "equivalent"))
                                except Exception:
                                    pass
                    except Exception as e:
                        _log(f"   [{pid}] 语义复核不可用，保留程序判定: {e}")
                final_url = cand.get("final_url") or url
                unit_ok, unit_reason = unit_compatibility(item.unit, cand.get("unit"))
                price_ambiguous = bool(cand.get("price_ambiguous"))
                bucket, match_outcome, match_detail = decide_quote_bucket(
                    mr,
                    unit_ok=unit_ok,
                    price_ambiguous=price_ambiguous,
                    match_mode=match_mode,
                )
                if unit_ok is False and "单位" not in match_detail:
                    match_detail = f"{match_detail}；{unit_reason}"
                match_ok = bucket == "formal"
                is_name_hit = not name_missed(mr)
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
                        "moq": cand.get("moq") or "",
                        "sku_scope": cand.get("sku_scope") or "",
                        "tax_mode": cand.get("tax_mode") or "unknown",
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
                if price <= 0.05:
                    _log(f"   [{pid}] × 无有效价格：{match_detail}")
                    continue
                if bucket == "candidate":
                    # 有价有名 → 留给 build_review_candidates；不进正式 quotes
                    _log(
                        f"   [{pid}] ◐ 候选待核 ¥{price} | {(title or '')[:28]} | {match_detail[:60]}"
                    )
                    seen_urls.add(cand_key)
                    continue

                # formal 合格价
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
                        match_level="strict" if match_mode == "strict" else "practical",
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
                vs = ""
                if item.submit is not None and price_ex_tax is not None:
                    try:
                        if price_ex_tax <= float(item.submit) * 1.02:
                            vs = " ≤报送"
                        else:
                            vs = f" 超报送(报送{float(item.submit):.2f})"
                    except Exception:
                        vs = ""
                _log(
                    f"   [{pid}] ✓ 合格价{len(quotes)}/{k} ¥{price}"
                    f"{f'(不含税≈{price_ex_tax})' if price_ex_tax is not None else ''}"
                    f"{vs} | {title[:28]} | "
                    f"厂家={cand.get('supplier') or '-'} 电话={cand.get('phone') or '-'}"
                )
                if (
                    item.submit is not None
                    and price_ex_tax is not None
                    and price_ex_tax <= float(item.submit) * 1.02
                    and len(quotes) >= min(k, 2)
                ):
                    _log(f"   [{pid}] 已有不超报送合格价，本站少开详情以提速")
                    break

            if platform_got < 0:
                break

        if len(quotes) < k:
            if platform_got == 0:
                _log(f"   [{pid}] 本站无完全匹配 → **换下一站**")
            elif platform_got > 0:
                _log(f"   [{pid}] 本站 {platform_got} 条，未满 {k} → 继续下一站")

    # 同条材料多报价：不超报送的排前面，便于审定取 min
    def _quote_sort_key(q: Quote) -> tuple:
        submit = None
        try:
            submit = float(item.submit) if item.submit is not None else None
        except Exception:
            submit = None
        ex = q.price_ex_tax
        if ex is None and q.price:
            try:
                ex = float(q.price) / tax_divisor if q.tax_mode == "tax_incl" else float(q.price)
            except Exception:
                ex = None
        under = 1
        if submit is not None and ex is not None:
            under = 0 if ex <= submit * 1.02 else 1
        return (under, ex if ex is not None else 1e18)

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
    if len(quotes) >= k:
        status = "full_k"
        msg = f"已凑满 {k} 个合格价（模式={match_mode}；试过: {','.join(tried_platforms)}）"
        review_candidates = []  # 已满额不展示待核
    elif quotes:
        status = "partial"
        msg = (
            f"部分合格价 {len(quotes)}/{k}（模式={match_mode}；"
            f"平台: {','.join(tried_platforms)}）"
        )
        review_candidates = []
    elif review_candidates:
        status = "need_review"
        best = review_candidates[0]
        msg = (
            f"【候选待核】¥{best.price:.2f}（{best.platform} · "
            f"{(best.title or best.supplier or '来源页')[:36]}）—"
            f"{best.match_detail}；请在结果表核对链接后人工采用"
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
    control_check: Callable[[], str] | None = None,
    # 返回当前是否启用 LLM（询价中可热切换）
    llm_enabled_check: Callable[[], bool] | None = None,
    # continue：跳过已有合格价的材料
    skip_statuses: tuple[str, ...] = ("full_k",),
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
                    emit(
                        {
                            "type": "item_skip",
                            "index": idx,
                            "total": len(work),
                            "name": item.name,
                            "status": prev.status if prev else "",
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
                        llm_settings=_settings_for_llm(),
                        on_llm=_on_llm,
                    )
                results[item.id] = qset
                done += 1
                tip = qset.error or qset.status
                print(f"   => {qset.status} quotes={len(qset.quotes)} | {tip}")
                q0 = qset.quotes[0] if qset.quotes else None
                match_via_llm = any(
                    "AI " in (q.match_detail or "") or "LLM" in (q.match_detail or "")
                    for q in (qset.quotes + qset.review_candidates)
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
                        "name": item.name,
                        "sheet": item.sheet,
                        "row": item.row,
                        "spec": (item.spec or "")[:200],
                        "brand": item.brand or "",
                        "submit": item.submit,
                        "status": qset.status,
                        "quotes": len(qset.quotes),
                        "k": k,
                        "message": tip,
                        "match_via_llm": match_via_llm,
                        "platform": q0.platform if q0 else "",
                        "title": (q0.title if q0 else "")[:120],
                        "url": (q0.url if q0 else "") or "",
                        "price": q0.price if q0 else None,
                        "audit": audit,
                        "quote_list": [
                            {
                                "rank": q.rank,
                                "price": q.price,
                                "price_ex_tax": q.price_ex_tax,
                                "platform": q.platform,
                                "title": (q.title or "")[:100],
                                "url": q.url or q.detail_url or "",
                                "match_level": q.match_level,
                                "match_detail": (q.match_detail or "")[:160],
                                "supplier": q.supplier or "",
                                "unit": q.unit or "",
                            }
                            for q in qset.quotes[:8]
                        ],
                        "review_list": [
                            {
                                "rank": q.rank,
                                "price": q.price,
                                "platform": q.platform,
                                "title": (q.title or "")[:100],
                                "url": q.url or "",
                                "match_detail": (q.match_detail or "")[:160],
                            }
                            for q in qset.review_candidates[:5]
                        ],
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
        session.close_quiet()

    # 试跑 limit 时只统计本次 work，不能把 evidence 里旧的其它行混进完成日志。
    scoped = [results.get(i.id) for i in work]
    full = sum(1 for q in scoped if q and q.status == "full_k")
    partial = sum(1 for q in scoped if q and q.status == "partial")
    review = sum(1 for q in scoped if q and q.status == "need_review")
    none = sum(1 for q in scoped if q and q.status == "no_match")

    # 组装按 sheet 分组的完整结果（供前端结果页）
    evidence_rows = quote_map_to_evidence(results, work)
    item_results_full = []
    by_sheet: dict[str, list] = {}
    for it in work:
        d = evidence_rows.get(it.id) or {}
        qset = results.get(it.id)
        q0 = qset.quotes[0] if qset and qset.quotes else None
        row = {
            "id": it.id,
            "sheet": it.sheet,
            "row": it.row,
            "name": it.name,
            "spec": it.spec,
            "brand": it.brand,
            "submit": it.submit,
            "status": (qset.status if qset else d.get("status") or "no_match"),
            "quotes": len(qset.quotes) if qset else 0,
            "message": (qset.error if qset else "") or d.get("hint") or d.get("message") or "",
            "platform": q0.platform if q0 else d.get("platform") or "",
            "title": (q0.title if q0 else d.get("title") or "")[:120],
            "url": (q0.url if q0 else d.get("url") or ""),
            "price": q0.price if q0 else d.get("price_tax"),
            "price_ex_tax": q0.price_ex_tax if q0 else d.get("price_ex_tax"),
            "audit": d.get("audit"),
            "quote_list": [
                {
                    "rank": q.rank,
                    "price": q.price,
                    "price_ex_tax": q.price_ex_tax,
                    "platform": q.platform,
                    "title": (q.title or "")[:100],
                    "url": q.url or "",
                    "match_detail": (q.match_detail or "")[:160],
                    "supplier": q.supplier or "",
                }
                for q in (qset.quotes if qset else [])[:8]
            ],
            "review_list": [
                {
                    "price": q.price,
                    "platform": q.platform,
                    "title": (q.title or "")[:100],
                    "url": q.url or "",
                    "match_detail": (q.match_detail or "")[:160],
                }
                for q in (qset.review_candidates if qset else [])[:5]
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
            "no_match": sum(1 for r in rows if r["status"] in ("no_match", "error", "skipped")),
            "items": sorted(rows, key=lambda x: int(x.get("row") or 0)),
        }
        for sheet, rows in by_sheet.items()
    ]

    if not stopped_early:
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
                "result_by_sheet": result_by_sheet,
                "item_results": item_results_full,
            }
        )
    else:
        # 停止时也推送当前结果，便于结果页查看
        emit(
            {
                "type": "stopped",
                "full_k": full,
                "partial": partial,
                "need_review": review,
                "no_match": none,
                "message": (
                    f"已停止：满{k}价={full}，部分={partial}，"
                    f"候选待核={review}，没查到={none}；可「继续询价」"
                ),
                "result_by_sheet": result_by_sheet,
                "item_results": item_results_full,
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
