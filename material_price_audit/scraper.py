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
    print(msg)
    if non_interactive:
        print(f"[non-interactive] wait {seconds}s for login…")
        time.sleep(max(0, seconds))
    else:
        input("完成后按回车 Continue > ")


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
