"""Playwright scrapers — only accept title-matched candidates with prices."""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    text = str(text).replace(",", "").replace("￥", "¥")
    m = re.search(r"(\d+\.?\d*)", text)
    if not m:
        return None
    try:
        v = float(m.group(1))
        if 0.01 < v < 5_000_000:
            return v
    except Exception:
        return None
    return None


def score_title(title: str, must: list[str]) -> int:
    t = (title or "").lower()
    return sum(1 for m in must if m and m.lower() in t)


def launch_context(profile_dir: Path, channel: str = "chrome", headless: bool = False):
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    kwargs = dict(
        user_data_dir=str(profile_dir),
        headless=headless,
        viewport={"width": 1400, "height": 900},
        locale="zh-CN",
        args=["--disable-blink-features=AutomationControlled"],
    )
    try:
        context = pw.chromium.launch_persistent_context(channel=channel, **kwargs)
    except Exception:
        context = pw.chromium.launch_persistent_context(**kwargs)
    page = context.pages[0] if context.pages else context.new_page()
    return pw, context, page


def wait_user(msg: str, seconds: int, non_interactive: bool) -> None:
    """Legacy helper. Prefer wait_until_logged_in for platform logins."""
    print(msg)
    if non_interactive:
        # 不再默认傻等 90s：seconds<=0 则几乎不等
        if seconds and seconds > 0:
            print(f"[wait] {seconds}s（建议改用自动检测登录）…")
            time.sleep(seconds)
        else:
            time.sleep(0.3)
    else:
        input("完成后按回车 Continue > ")


# URL/标题里这些信号 ≈ 还在登录页
_LOGIN_URL_MARKERS = (
    "/login",
    "/signin",
    "/sign-in",
    "/passport",
    "/sso",
    "/oauth",
    "login.",
    "passport.",
    "accounts.",
    "auth.",
    "apply_trial",
)
_LOGIN_TITLE_MARKERS = ("登录", "登陆", "sign in", "log in", "账号登录", "会员登录")


def _url_title(page) -> tuple[str, str]:
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    return url, title


def page_looks_like_login(page) -> bool:
    """Heuristic: still on a login / SSO / password form page."""
    url, title = _url_title(page)
    title_l = title.lower()
    if any(m in url for m in _LOGIN_URL_MARKERS):
        # 用户中心有时 URL 含 login 片段以外的；纯 /login 明确
        if "/login" in url or "passport" in url or "signin" in url or "sso" in url:
            return True
        if "apply_trial" in url:
            return True
    if any(m in title or m in title_l for m in _LOGIN_TITLE_MARKERS):
        # 「登录-广材网」类标题
        if "管理" not in title and "控制台" not in title:
            return True
    try:
        # 可见密码框 = 几乎肯定要登录
        n = page.locator('input[type="password"]:visible').count()
        if n and n > 0:
            return True
    except Exception:
        pass
    try:
        # 常见登录按钮文案
        if page.get_by_role("button", name=re.compile(r"^(登录|登陆|立即登录)$")).count() > 0:
            # 同时有账号输入才算
            if page.locator('input[type="password"], input[type="text"], input[type="tel"]').count() > 0:
                return True
    except Exception:
        pass
    return False


def page_looks_logged_in(page, platform_id: str = "", login_url: str = "") -> bool:
    """
    Heuristic: session already usable.
    - 不在登录页，且 URL/标题已离开登录态
    - 或平台特征 cookie / 用户入口出现
    """
    if page_looks_like_login(page):
        return False

    url, title = _url_title(page)
    login_l = (login_url or "").lower()

    # 从明确登录 URL 跳走了
    if login_l and any(m in login_l for m in ("/login", "passport", "signin", "sso")):
        if not any(m in url for m in ("/login", "passport", "signin", "sso", "apply_trial")):
            return True

    pid = (platform_id or "").lower()
    # 平台特判（轻量）
    if pid == "jd":
        if "passport.jd.com" not in url and "login" not in url:
            return True
        try:
            cookies = page.context.cookies()
            names = {c.get("name") for c in cookies if "jd" in (c.get("domain") or "")}
            if names & {"pin", "pt_key", "pwdt_id", "thor"}:
                return not page_looks_like_login(page)
        except Exception:
            pass
    if pid == "1688":
        if "login.taobao.com" not in url and "login.1688.com" not in url and "passport" not in url:
            return True
    if pid in ("guangcai", "gldjc_hangqing", "gldjc_xunjia"):
        if "gldjc.com" in url and "/login" not in url:
            return True
    if pid == "huixun":
        if "iccchina.com" in url and "/login" not in url:
            return True
    if pid == "lingcai":
        # 用户中心：无密码框且页面已加载 → 多半已登录
        if "hylcw.cn" in url and not page_looks_like_login(page):
            return True

    # 通用：有「退出/我的/用户中心」且无密码框
    try:
        if page.get_by_text(re.compile(r"退出|注销|个人中心|我的账户|用户中心")).count() > 0:
            return True
    except Exception:
        pass

    # 仍说不清：不在 login 页就算过（避免误杀）—— 仅当 login_url 是登录页时
    if login_l and "/login" in login_l and "/login" not in url and not page_looks_like_login(page):
        return True

    return False


def wait_until_logged_in(
    page,
    *,
    platform_id: str,
    name: str,
    login_url: str,
    timeout_s: int = 180,
    poll_ms: int = 800,
) -> str:
    """
    智能等登录：轮询页面状态，登录成功立刻返回。
    返回: already_ok | logged_in | timeout
    绝不固定 sleep 90 秒。
    """
    try:
        page.wait_for_timeout(800)
    except Exception:
        time.sleep(0.8)

    if page_looks_logged_in(page, platform_id, login_url):
        print(f"  [{name}] ✓ 已登录，跳过等待")
        return "already_ok"

    if not page_looks_like_login(page):
        # 打开的不是登录页，也可能 session 有效
        print(f"  [{name}] ✓ 当前页无需登录，继续")
        return "already_ok"

    print(f"  [{name}] 请在浏览器登录（自动检测成功，无需傻等/回车）…")
    print(f"           超时上限 {timeout_s}s；登录成功会立刻下一站")
    deadline = time.time() + max(15, int(timeout_s))
    last_log = 0.0
    while time.time() < deadline:
        try:
            if page_looks_logged_in(page, platform_id, login_url):
                print(f"  [{name}] ✓ 检测到登录成功")
                return "logged_in"
            # 用户可能手动跳到首页
            if not page_looks_like_login(page):
                # 双检：再等一小会防闪跳
                page.wait_for_timeout(600)
                if not page_looks_like_login(page) or page_looks_logged_in(page, platform_id, login_url):
                    print(f"  [{name}] ✓ 已离开登录页，视为成功")
                    return "logged_in"
        except Exception:
            pass
        now = time.time()
        if now - last_log >= 6:
            left = int(deadline - now)
            try:
                cur = (page.url or "")[:80]
            except Exception:
                cur = "?"
            print(f"  [{name}] …等待中 剩余~{left}s  url={cur}")
            last_log = now
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            time.sleep(poll_ms / 1000.0)

    print(f"  [{name}] ⚠ 超时未确认登录，继续流程（抓取时再探测）")
    return "timeout"


def jd_search(page, query: str, must: list[str], timeout_ms: int, min_score: int = 1):
    url = f"https://search.jd.com/Search?keyword={quote(query)}&enc=utf-8"
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(2500)
    if "登录" in (page.title() or ""):
        return None, "need_login_jd"

    goods = page.eval_on_selector_all(
        "li.gl-item",
        """els => els.slice(0, 15).map(el => {
            const sku = el.getAttribute('data-sku') || '';
            const a = el.querySelector('.p-name a, a[href*="item.jd.com"]');
            const priceEl = el.querySelector('.p-price i, .p-price em');
            const nameEl = el.querySelector('.p-name em, .p-name a');
            return {
              sku,
              href: a ? a.href : (sku ? ('https://item.jd.com/' + sku + '.html') : ''),
              priceText: priceEl ? priceEl.innerText : '',
              name: nameEl ? nameEl.innerText.replace(/\\s+/g,' ').trim() : ''
            };
        })""",
    )
    cands = []
    for g in goods or []:
        name = g.get("name") or ""
        price = parse_price(g.get("priceText"))
        href = g.get("href") or ""
        sc = score_title(name + " " + href, must)
        if price and href and "item.jd.com" in href and sc >= min_score:
            cands.append(
                {
                    "title": name[:160],
                    "price_tax": price,
                    "url": href.split("?")[0],
                    "sku": g.get("sku") or "",
                    "score": sc,
                    "platform": "jd",
                }
            )
    cands.sort(key=lambda x: (-x["score"], x["price_tax"]))
    return (cands[0] if cands else None), "ok"


def s1688_search(page, query: str, must: list[str], timeout_ms: int, min_score: int = 1):
    url = f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote(query)}"
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(3000)
    if "login" in page.url.lower() or "登录" in (page.title() or ""):
        return None, "need_login_1688"

    cards = page.eval_on_selector_all(
        'a[href*="detail.1688.com"]',
        """els => {
          const out=[], seen=new Set();
          for (const a of els.slice(0, 40)) {
            const href = a.href || '';
            if (!href.includes('detail.1688.com') || seen.has(href)) continue;
            seen.add(href);
            let root = a;
            for (let i=0;i<6;i++){ if(root.parentElement) root=root.parentElement; }
            const text=(root.innerText||a.innerText||'').replace(/\\s+/g,' ').trim().slice(0,220);
            out.push({href, text});
          }
          return out.slice(0, 20);
        }""",
    )
    cands = []
    for c in cards or []:
        text = c.get("text") or ""
        href = (c.get("href") or "").split("?")[0]
        sc = score_title(text, must)
        prices = re.findall(r"[¥￥]\s*(\d+\.?\d*)", text)
        price = None
        for p in prices:
            v = float(p)
            if 0.05 < v < 500000:
                price = v
                break
        if price and href and sc >= min_score:
            cands.append(
                {
                    "title": text[:160],
                    "price_tax": price,
                    "url": href,
                    "sku": "",
                    "score": sc,
                    "platform": "1688",
                }
            )
    cands.sort(key=lambda x: (-x["score"], x["price_tax"]))
    return (cands[0] if cands else None), "ok"


def open_detail(
    page,
    cand: dict,
    timeout_ms: int,
    extra_price_selectors: list[str] | None = None,
) -> dict:
    try:
        page.goto(cand["url"], wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2500)
        title = page.title() or ""
        price = None
        selectors = list(extra_price_selectors or []) + [
            ".p-price .price",
            ".summary-price-wrap .p-price span.price",
            "#jd-price",
            ".price-text",
            ".tm-price",
            ".tb-rmb-num",
            "#mainPrice",
            "[class*='price']",
        ]
        # de-dup preserve order
        seen = set()
        ordered = []
        for s in selectors:
            if s and s not in seen:
                seen.add(s)
                ordered.append(s)
        for sel in ordered:
            try:
                el = page.query_selector(sel)
                if el:
                    price = parse_price(el.inner_text())
                    if price:
                        break
            except Exception:
                pass
        if not price:
            body = page.inner_text("body")[:4000]
            m = re.search(r"[¥￥]\s*(\d+\.?\d*)", body)
            if m:
                price = parse_price(m.group(1))
        if price:
            cand["price_tax"] = price
            cand["detail_confirmed"] = True
        else:
            cand["detail_confirmed"] = False
        cand["detail_title"] = title[:120]
        cand["final_url"] = page.url
        cand["captured_at"] = datetime.now().isoformat(timespec="seconds")
    except Exception as e:
        cand["detail_error"] = str(e)
        cand["detail_confirmed"] = False
        cand["captured_at"] = datetime.now().isoformat(timespec="seconds")
    return cand


def pick_manual(cands: list[dict], query: str) -> dict | None:
    if not cands:
        return None
    print(f"\n候选 / Candidates for: {query}")
    for i, c in enumerate(cands[:10], 1):
        plat = c.get("platform", "?")
        print(f"  {i}. [{plat}] ¥{c['price_tax']} score={c.get('score')} | {c['title'][:60]}")
        print(f"     {c['url']}")
    print("  0. skip")
    sel = input("选择序号 Select > ").strip()
    if sel == "0":
        return None
    idx = int(sel) - 1 if sel.isdigit() else 0
    return cands[idx] if 0 <= idx < len(cands) else cands[0]


def to_evidence(
    item_key: str,
    item: Any,
    cand: dict,
    tax_divisor: float,
    never_exceed: bool,
) -> dict:
    from .excel_io import r2

    ex = r2(float(cand["price_tax"]) / tax_divisor)
    submit = float(item.submit)
    audit = min(ex, submit) if never_exceed else ex
    return {
        "key": item_key,
        "status": "verified",
        "sheet": item.sheet,
        "row": item.row,
        "name": item.name,
        "spec": item.spec[:100],
        "submit": submit,
        "qty": item.qty,
        "platform": cand.get("platform"),
        "title": cand.get("detail_title") or cand.get("title"),
        "url": cand.get("final_url") or cand.get("url"),
        "price_tax": cand["price_tax"],
        "price_ex_tax": ex,
        "audit": audit,
        "detail_confirmed": bool(cand.get("detail_confirmed")),
        "captured_at": cand.get("captured_at"),
        "sku": cand.get("sku"),
        "match_score": cand.get("score"),
    }
