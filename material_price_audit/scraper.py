"""Playwright scrapers — only accept title-matched candidates with prices."""

from __future__ import annotations

import re
import sys
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
    """Legacy helper. Prefer wait_for_login_agent for platform logins."""
    print(msg)
    if non_interactive:
        if seconds and seconds > 0:
            time.sleep(seconds)
        else:
            time.sleep(0.2)
    else:
        input("完成后按回车 Continue > ")


# 仅 URL 判定登录态 —— 禁止狂扫 DOM（会触发 SPA 反复重绘/像刷新）
_LOGIN_URL_MARKERS = (
    "/login",
    "/signin",
    "/sign-in",
    "/passport",
    "passport.",
    "login.taobao",
    "login.1688",
    "login.jd",
    "/sso",
    "apply_trial",
)


def _safe_url(page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def _safe_title(page) -> str:
    try:
        return page.title() or ""
    except Exception:
        return ""


def url_looks_like_login(url: str) -> bool:
    u = (url or "").lower()
    if not u:
        return False
    return any(m in u for m in _LOGIN_URL_MARKERS)


def url_left_login(start_url: str, cur_url: str, platform_id: str = "") -> bool:
    """Passive: user navigated away from login URL → success."""
    s = (start_url or "").lower().split("?")[0]
    c = (cur_url or "").lower()
    if not c:
        return False
    # still on a hard login URL
    if url_looks_like_login(c) and "/userinfo" not in c:
        return False
    # URL changed from the page we opened
    c0 = c.split("?")[0]
    if s and c0 != s and not url_looks_like_login(c):
        return True
    # opened login, now not login
    if url_looks_like_login(s) and not url_looks_like_login(c):
        return True
    pid = (platform_id or "").lower()
    if pid == "lingcai":
        # 领材登录在 userInfo；已登录时往往仍在同域用户页，靠 cookie/不再跳转
        # 仅当从明确 login 子路径离开，或 title 不再含登录
        if "hylcw.cn" in c and "/login" not in c:
            return not url_looks_like_login(c)
    return False


def page_looks_like_login(page) -> bool:
    """URL-only（兼容旧调用）。禁止扫 password 控件。"""
    return url_looks_like_login(_safe_url(page))


def page_looks_logged_in(page, platform_id: str = "", login_url: str = "") -> bool:
    """URL-only 粗判，不碰 DOM。"""
    cur = _safe_url(page)
    if url_looks_like_login(cur) and "userinfo" not in cur.lower():
        return False
    if login_url and url_left_login(login_url, cur, platform_id):
        return True
    # 不在登录 URL 上 → 倾向已可用
    if not url_looks_like_login(cur):
        return True
    return False


def agent_login_signal_path(package_root: Path | None = None) -> Path:
    root = package_root or Path(__file__).resolve().parents[1]
    return root / "data" / "output" / "LOGIN_CONTINUE"


def clear_agent_login_signal(package_root: Path | None = None) -> None:
    p = agent_login_signal_path(package_root)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


def agent_login_signaled(package_root: Path | None = None) -> bool:
    return agent_login_signal_path(package_root).exists()


def wait_for_login_agent(
    page,
    *,
    platform_id: str,
    name: str,
    login_url: str,
    package_root: Path | None = None,
    timeout_s: int = 600,
    poll_s: float = 1.5,
    allow_stdin: bool = True,
) -> str:
    """
    Agent 友好登录等待：
    - **绝不** page.reload / 重复 goto / 扫 DOM
    - 只读 page.url（被动）
    - Agent 可 touch data/output/LOGIN_CONTINUE 立刻放行
    - TTY 下也可回车放行
    返回: already_ok | logged_in | agent_continue | timeout
    """
    root = package_root or Path(__file__).resolve().parents[1]
    clear_agent_login_signal(root)
    start_url = _safe_url(page) or login_url

    # 已不在登录 URL → 秒过，不折腾
    if page_looks_logged_in(page, platform_id, login_url):
        print(f"  [{name}] ✓ 已离开登录页 / 会话可用，不刷新、不等待")
        return "already_ok"
    if not url_looks_like_login(start_url) and not url_looks_like_login(login_url):
        print(f"  [{name}] ✓ 当前不是登录 URL，继续")
        return "already_ok"

    sig = agent_login_signal_path(root)
    print("")
    print("========== LOGIN_WAIT (agent) ==========")
    print(f"platform : {platform_id} / {name}")
    print(f"login_url: {login_url}")
    print(f"browser  : {_safe_url(page)[:100]}")
    print("请用户在【已打开的浏览器窗口】登录。")
    print("程序只被动看 URL，不会刷新页面。")
    print("Agent 在用户说「登完了」后任选：")
    print(f"  1) touch {sig}")
    print("  2) 终端回车（若有 TTY）")
    print("  3) URL 自动离开登录页也会继续")
    print("========================================")

    deadline = time.time() + max(30, int(timeout_s))
    last_log = 0.0
    stdin_ok = allow_stdin and sys.stdin.isatty()

    # 非阻塞读回车：用 select
    while time.time() < deadline:
        cur = _safe_url(page)
        if url_left_login(start_url, cur, platform_id) or (
            not url_looks_like_login(cur) and url_looks_like_login(start_url)
        ):
            print(f"  [{name}] ✓ URL 已离开登录页 → {_safe_url(page)[:80]}")
            clear_agent_login_signal(root)
            return "logged_in"

        if agent_login_signaled(root):
            print(f"  [{name}] ✓ Agent 信号 LOGIN_CONTINUE")
            clear_agent_login_signal(root)
            return "agent_continue"

        if stdin_ok:
            try:
                import select

                r, _, _ = select.select([sys.stdin], [], [], 0)
                if r:
                    try:
                        sys.stdin.readline()
                    except Exception:
                        pass
                    print(f"  [{name}] ✓ 终端回车确认")
                    clear_agent_login_signal(root)
                    return "agent_continue"
            except Exception:
                # Windows 等无 select：不读 stdin 循环，只靠 URL/文件
                stdin_ok = False

        now = time.time()
        if now - last_log >= 12:
            left = int(deadline - now)
            print(f"  [{name}] 等待登录中… 剩余~{left}s  url={cur[:70]}  (不刷新)")
            last_log = now

        # 纯 sleep，不碰 page 的 locator / reload
        time.sleep(poll_s)

    print(f"  [{name}] ⚠ 等待超时，不再刷新；由后续抓取结果判断")
    clear_agent_login_signal(root)
    return "timeout"


# 兼容旧名
def wait_until_logged_in(
    page,
    *,
    platform_id: str,
    name: str,
    login_url: str,
    timeout_s: int = 600,
    poll_ms: int = 1500,
    package_root: Path | None = None,
) -> str:
    return wait_for_login_agent(
        page,
        platform_id=platform_id,
        name=name,
        login_url=login_url,
        package_root=package_root,
        timeout_s=timeout_s,
        poll_s=max(0.5, poll_ms / 1000.0),
        allow_stdin=True,
    )


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
