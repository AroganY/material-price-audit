"""
Multi-platform registry.

Users can:
  - pick built-in platforms: jd / 1688 / taobao / tmall / zkh / suning
  - add custom sites in config.yaml under platforms.definitions
  - login to only the platforms they choose
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

from .scraper import parse_price, score_title


@dataclass
class PlatformSpec:
    id: str
    name: str
    login_url: str
    search_url_template: str  # must contain {query}
    # built-in handler name or "generic"
    handler: str = "generic"
    require_login_hint: bool = True
    # generic selectors
    item_link_contains: str = ""  # substring that product URLs should contain
    item_link_selector: str = "a[href]"
    # optional: evaluate script returns list[{href,name,priceText}]
    # kept simple: use link selector + parent text for price
    detail_price_selectors: list[str] = field(default_factory=list)
    notes: str = ""


# ---- built-in definitions ----
# 造价材料信息站（优先）+ 电商/工业品（补充）

BUILTIN: dict[str, PlatformSpec] = {
    # ========== 造价材料信息站（内置重点）==========
    "guangcai": PlatformSpec(
        id="guangcai",
        name="广材网",
        login_url="https://www.gldjc.com/login",
        # 实测搜索落地：/scj/so.html?l=1&keyword=...（未登录会跳转登录页）
        search_url_template="https://www.gldjc.com/scj/so.html?l=1&keyword={query}",
        handler="gldjc",
        item_link_contains="gldjc.com",
        item_link_selector='a[href*="gldjc.com"]',
        detail_price_selectors=[
            ".price",
            "[class*='price']",
            "[class*='Price']",
            ".material-price",
        ],
        notes="广联达广材网 gldjc.com · 建工材料价格查询，通常需登录会员",
    ),
    "huixun": PlatformSpec(
        id="huixun",
        name="慧讯网",
        # 慧讯为广联达材料价格产品线常用称呼，现入口与广材网同一体系
        login_url="https://www.gldjc.com/login",
        search_url_template="https://www.gldjc.com/scj/so.html?l=1&keyword={query}",
        handler="gldjc",
        item_link_contains="gldjc.com",
        item_link_selector='a[href*="gldjc.com"]',
        detail_price_selectors=[
            ".price",
            "[class*='price']",
            "[class*='Price']",
        ],
        notes="广联达慧讯/材料价 · 与广材网同属 gldjc 体系，需登录；若贵司有独立慧讯域名可在 definitions 覆盖",
    ),
    "lingcai": PlatformSpec(
        id="lingcai",
        name="领材网",
        # 领材网常与广联达材料询价生态相关；默认走广材询价/材料检索入口
        # 若实际账号在独立域名，请用 platforms.definitions.lingcai 覆盖 URL
        login_url="https://www.gldjc.com/login",
        search_url_template="https://www.gldjc.com/scj/so.html?l=1&keyword={query}",
        handler="gldjc",
        item_link_contains="gldjc.com",
        item_link_selector='a[href*="gldjc.com"]',
        detail_price_selectors=[".price", "[class*='price']"],
        notes="领材网 · 默认对接广材材料检索；独立部署域名请在 config definitions 覆盖 login_url/search_url",
    ),
    "gldjc_hangqing": PlatformSpec(
        id="gldjc_hangqing",
        name="广材行情",
        login_url="https://hangqing.gldjc.com/",
        search_url_template="https://hangqing.gldjc.com/",
        handler="generic",
        item_link_contains="hangqing.gldjc.com",
        item_link_selector='a[href*="hangqing.gldjc.com"]',
        detail_price_selectors=["[class*='price']"],
        notes="钢材等每日行情（资讯向，需人工判断是否可作审定）",
        require_login_hint=False,
    ),
    "gldjc_xunjia": PlatformSpec(
        id="gldjc_xunjia",
        name="广材询价",
        login_url="https://xunjia.gldjc.com/",
        search_url_template="https://xunjia.gldjc.com/",
        handler="generic",
        item_link_contains="xunjia.gldjc.com",
        item_link_selector='a[href*="xunjia"]',
        detail_price_selectors=["[class*='price']"],
        notes="人工询价入口（偏流程，非自动挂牌价）",
        require_login_hint=True,
    ),
    # ========== 电商 / 工业品 ==========
    "jd": PlatformSpec(
        id="jd",
        name="京东",
        login_url="https://www.jd.com/",
        search_url_template="https://search.jd.com/Search?keyword={query}&enc=utf-8",
        handler="jd",
        item_link_contains="item.jd.com",
        detail_price_selectors=[
            ".p-price .price",
            ".summary-price-wrap .p-price span.price",
            "#jd-price",
        ],
        notes="零售挂牌；工程价可能更低",
    ),
    "1688": PlatformSpec(
        id="1688",
        name="1688批发",
        login_url="https://www.1688.com/",
        search_url_template="https://s.1688.com/selloffer/offer_search.htm?keywords={query}",
        handler="1688",
        item_link_contains="detail.1688.com",
        detail_price_selectors=[".price-text", ".price"],
        notes="批发价，常需登录可见",
    ),
    "taobao": PlatformSpec(
        id="taobao",
        name="淘宝",
        login_url="https://www.taobao.com/",
        search_url_template="https://s.taobao.com/search?q={query}",
        handler="generic",
        item_link_contains="item.taobao.com",
        item_link_selector='a[href*="item.taobao.com"]',
        detail_price_selectors=[".tb-rmb-num", "[class*='Price']"],
        notes="需登录；反爬较强",
    ),
    "tmall": PlatformSpec(
        id="tmall",
        name="天猫",
        login_url="https://www.tmall.com/",
        search_url_template="https://list.tmall.com/search_product.htm?q={query}",
        handler="generic",
        item_link_contains="detail.tmall.com",
        item_link_selector='a[href*="detail.tmall.com"]',
        detail_price_selectors=[".tm-price", "[class*='Price']"],
        notes="需登录；反爬较强",
    ),
    "zkh": PlatformSpec(
        id="zkh",
        name="震坤行工业品",
        login_url="https://www.zkh.com/",
        search_url_template="https://www.zkh.com/search?keyword={query}",
        handler="generic",
        item_link_contains="zkh.com",
        item_link_selector='a[href*="/product"], a[href*="item"]',
        detail_price_selectors=["[class*='price']", ".price"],
        notes="工业品超市，阀门/辅材常见",
    ),
    "suning": PlatformSpec(
        id="suning",
        name="苏宁易购",
        login_url="https://www.suning.com/",
        search_url_template="https://search.suning.com/{query}/",
        handler="generic",
        item_link_contains="product.suning.com",
        item_link_selector='a[href*="product.suning.com"]',
        detail_price_selectors=["#mainPrice", ".mainprice", "[class*='price']"],
        notes="零售",
    ),
    "mysteel": PlatformSpec(
        id="mysteel",
        name="我的钢铁网",
        login_url="https://www.mysteel.com/",
        search_url_template="https://search.mysteel.com/search.html?searchKey={query}",
        handler="generic",
        item_link_contains="mysteel.com",
        item_link_selector='a[href*="mysteel.com"]',
        detail_price_selectors=["[class*='price']"],
        notes="钢材行情入口（多为资讯价，需人工判断）",
        require_login_hint=False,
    ),
}


def normalize_platform_id(pid) -> str:
    # YAML may parse bare 1688 as int — always stringify
    p = str(pid if pid is not None else "").strip().lower()
    # strip common suffix 网
    p2 = p.replace("网", "")
    aliases = {
        "jingdong": "jd",
        "京东": "jd",
        "alibaba": "1688",
        "ali": "1688",
        "阿里": "1688",
        "淘宝": "taobao",
        "天猫": "tmall",
        "震坤行": "zkh",
        "苏宁": "suning",
        "钢材": "mysteel",
        "我的钢铁网": "mysteel",
        # 造价材料站
        "广材": "guangcai",
        "广材网": "guangcai",
        "gldjc": "guangcai",
        "guangcaiwang": "guangcai",
        "慧讯": "huixun",
        "慧讯网": "huixun",
        "huixunwang": "huixun",
        "广联达慧讯": "huixun",
        "领材": "lingcai",
        "领材网": "lingcai",
        "lingcaiwang": "lingcai",
        "广材行情": "gldjc_hangqing",
        "广材询价": "gldjc_xunjia",
    }
    if p in aliases:
        return aliases[p]
    if p2 in aliases:
        return aliases[p2]
    return p


def load_platform_registry(cfg: dict | None = None) -> dict[str, PlatformSpec]:
    """Merge built-ins with config platforms.definitions."""
    reg = dict(BUILTIN)
    cfg = cfg or {}
    plats = cfg.get("platforms") or {}
    # support both list form and dict form
    definitions = {}
    if isinstance(plats, dict):
        definitions = plats.get("definitions") or {}
    for pid, raw in definitions.items():
        if not isinstance(raw, dict):
            continue
        pid = normalize_platform_id(str(pid))
        reg[pid] = PlatformSpec(
            id=pid,
            name=str(raw.get("name") or pid),
            login_url=str(raw.get("login_url") or raw.get("home_url") or ""),
            search_url_template=str(
                raw.get("search_url") or raw.get("search_url_template") or ""
            ),
            handler=str(raw.get("handler") or "generic"),
            require_login_hint=bool(raw.get("require_login", True)),
            item_link_contains=str(raw.get("item_link_contains") or ""),
            item_link_selector=str(raw.get("item_link_selector") or "a[href]"),
            detail_price_selectors=list(
                raw.get("detail_price_selectors")
                or raw.get("detail_price_selector")
                or []
            ),
            notes=str(raw.get("notes") or ""),
        )
    return reg


def resolve_enabled_platforms(cfg: dict | None, cli_platforms: str | None) -> list[str]:
    """
    Priority:
      1) CLI --platforms a,b,c
      2) config platforms.enabled list
      3) config platforms: [jd, 1688]  (legacy list form)
      4) default jd,1688
    """
    if cli_platforms:
        ids = [normalize_platform_id(x) for x in cli_platforms.split(",") if x.strip()]
        return ids

    cfg = cfg or {}
    plats = cfg.get("platforms")
    if isinstance(plats, list):
        return [normalize_platform_id(x) for x in plats]
    if isinstance(plats, dict):
        enabled = plats.get("enabled") or plats.get("list") or []
        if enabled:
            return [normalize_platform_id(x) for x in enabled]
    return ["guangcai", "huixun", "lingcai", "jd", "1688"]


def list_platforms(cfg: dict | None = None) -> list[PlatformSpec]:
    reg = load_platform_registry(cfg)
    enabled = set(resolve_enabled_platforms(cfg, None))
    # show all known, mark enabled in notes via order: enabled first
    out = []
    for pid in resolve_enabled_platforms(cfg, None):
        if pid in reg:
            out.append(reg[pid])
    for pid, spec in reg.items():
        if pid not in enabled:
            out.append(spec)
    return out


# ---- search handlers ----

def _search_jd(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    url = spec.search_url_template.format(query=quote(query))
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(2500)
    if "登录" in (page.title() or ""):
        return None, "need_login"
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
    return _filter_cands(goods or [], must, min_score, "jd", "item.jd.com"), "ok"


def _search_1688(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    url = spec.search_url_template.format(query=quote(query))
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(3000)
    if "login" in page.url.lower() or "登录" in (page.title() or ""):
        return None, "need_login"
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
            out.push({href, text, name: text, priceText: text});
          }
          return out.slice(0, 20);
        }""",
    )
    # extract price from text
    goods = []
    for c in cards or []:
        text = c.get("text") or c.get("name") or ""
        prices = re.findall(r"[¥￥]\s*(\d+\.?\d*)", text)
        price_text = prices[0] if prices else ""
        goods.append(
            {
                "href": c.get("href"),
                "name": text[:160],
                "priceText": price_text,
                "sku": "",
            }
        )
    return _filter_cands(goods, must, min_score, "1688", "detail.1688.com"), "ok"


def _search_gldjc(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    """
    广材网 / 慧讯网 / 领材网（gldjc 体系）材料搜索。
    搜索 URL: /scj/so.html?l=1&keyword=...
    未登录会跳到 /login?hostUrl=...
    """
    url = spec.search_url_template.format(query=quote(query))
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(3500)
    cur = page.url or ""
    title = page.title() or ""
    if "/login" in cur or "登录" in title:
        return None, "need_login"

    # 结果页：尽量从列表卡片提取 名称/价格/链接
    cards = page.eval_on_selector_all(
        "a, tr, .list-item, .material-item, li, .el-table__row",
        """els => {
          const out = [];
          const seen = new Set();
          for (const el of els.slice(0, 120)) {
            const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!text || text.length < 4 || text.length > 300) continue;
            // must look price-like
            if (!/[¥￥]|\\d+(\\.\\d+)?\\s*元/.test(text) && !/\\d{2,6}(\\.\\d+)?/.test(text)) continue;
            let href = '';
            if (el.tagName === 'A') href = el.href || '';
            else {
              const a = el.querySelector && el.querySelector('a[href]');
              href = a ? a.href : '';
            }
            if (href && seen.has(href + text.slice(0,40))) continue;
            seen.add((href||'') + text.slice(0,40));
            out.push({ href, name: text.slice(0,160), priceText: text, text });
          }
          return out.slice(0, 40);
        }""",
    )
    goods = []
    for c in cards or []:
        text = c.get("text") or c.get("priceText") or ""
        # 优先 ¥ 或 元
        prices = re.findall(r"[¥￥]\s*(\d+\.?\d*)", text)
        if not prices:
            prices = re.findall(r"(\d+\.?\d*)\s*元", text)
        # 广材列表有时纯数字价
        if not prices:
            prices = re.findall(r"(?:含税|除税|单价)[^\d]{0,6}(\d+\.?\d*)", text)
        price_text = prices[0] if prices else ""
        href = c.get("href") or page.url
        goods.append(
            {
                "href": href,
                "name": (c.get("name") or text)[:160],
                "priceText": price_text,
                "sku": "",
            }
        )
    cands = _filter_cands(goods, must, min_score, spec.id, "gldjc.com")
    # 若关键词匹配过严导致 0 条，放宽：只要有价格就收（造价站结果页通常已按 keyword 过滤）
    if not cands and goods:
        loose = []
        for g in goods:
            price = parse_price(g.get("priceText"))
            if not price:
                continue
            loose.append(
                {
                    "title": (g.get("name") or "")[:160],
                    "price_tax": price,
                    "url": (g.get("href") or page.url).split("?")[0]
                    if g.get("href")
                    else page.url,
                    "sku": "",
                    "score": max(1, score_title(g.get("name") or "", must)),
                    "platform": spec.id,
                }
            )
        loose.sort(key=lambda x: (-x["score"], x["price_tax"]))
        return loose[:15], "ok"
    return cands, "ok"


def _search_generic(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    if not spec.search_url_template or "{query}" not in spec.search_url_template:
        return None, "bad_config"
    url = spec.search_url_template.format(query=quote(query))
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(3000)
    title = page.title() or ""
    if "登录" in title and spec.require_login_hint:
        # soft signal — still try parse
        pass

    link_sel = spec.item_link_selector or "a[href]"
    contains = spec.item_link_contains or ""
    # Pull anchors + nearby text
    cards = page.eval_on_selector_all(
        link_sel,
        """(els, contains) => {
          const out=[], seen=new Set();
          for (const a of els.slice(0, 80)) {
            let href = a.href || '';
            if (!href || href.startsWith('javascript')) continue;
            if (contains && !href.includes(contains)) continue;
            if (seen.has(href)) continue;
            seen.add(href);
            let root = a;
            for (let i=0;i<5;i++){ if(root.parentElement) root=root.parentElement; }
            const text=(root.innerText||a.innerText||'').replace(/\\s+/g,' ').trim().slice(0,240);
            const name=(a.innerText||text).replace(/\\s+/g,' ').trim().slice(0,160);
            out.push({href, name, priceText: text, text});
          }
          return out.slice(0, 25);
        }""",
        contains,
    )
    goods = []
    for c in cards or []:
        text = c.get("text") or c.get("priceText") or ""
        prices = re.findall(r"[¥￥]\s*(\d+\.?\d*)", text)
        if not prices:
            prices = re.findall(r"(\d+\.?\d*)\s*元", text)
        price_text = prices[0] if prices else ""
        goods.append(
            {
                "href": c.get("href"),
                "name": (c.get("name") or text)[:160],
                "priceText": price_text,
                "sku": "",
            }
        )
    return _filter_cands(goods, must, min_score, spec.id, contains), "ok"


def _filter_cands(goods, must, min_score, platform_id, link_hint) -> list[dict] | None:
    cands = []
    for g in goods or []:
        name = g.get("name") or ""
        href = g.get("href") or ""
        price = parse_price(g.get("priceText"))
        sc = score_title(name + " " + href, must)
        if not price or not href:
            continue
        if link_hint and link_hint not in href and platform_id not in ("zkh", "mysteel"):
            # allow zkh loose links
            if platform_id not in ("zkh", "suning", "mysteel"):
                continue
        if sc >= min_score:
            cands.append(
                {
                    "title": name[:160],
                    "price_tax": price,
                    "url": href.split("?")[0],
                    "sku": g.get("sku") or "",
                    "score": sc,
                    "platform": platform_id,
                }
            )
    cands.sort(key=lambda x: (-x["score"], x["price_tax"]))
    return cands  # return full list for multi-pick; caller may take [0]


HANDLERS: dict[str, Callable] = {
    "jd": _search_jd,
    "1688": _search_1688,
    "gldjc": _search_gldjc,
    "generic": _search_generic,
}


def search_on_platform(
    page,
    platform_id: str,
    query: str,
    must: list[str],
    timeout_ms: int,
    min_score: int,
    registry: dict[str, PlatformSpec],
) -> tuple[list[dict], str]:
    """
    Returns (candidates_sorted, status)
    status: ok | need_login | unknown_platform | bad_config | error:...
    """
    pid = normalize_platform_id(platform_id)
    spec = registry.get(pid)
    if not spec:
        return [], "unknown_platform"
    handler_name = spec.handler if spec.handler in HANDLERS else "generic"
    # map built-in id to specialized handler
    if pid == "jd":
        handler_name = "jd"
    elif pid == "1688":
        handler_name = "1688"
    elif pid in ("guangcai", "huixun", "lingcai") or handler_name == "gldjc":
        handler_name = "gldjc"
    try:
        fn = HANDLERS[handler_name]
        result, status = fn(page, query, must, timeout_ms, min_score, spec)
        if result is None:
            return [], status
        # specialized handlers may return single best historically — normalize to list
        if isinstance(result, dict):
            return [result], status
        return list(result), status
    except Exception as e:
        return [], f"error:{type(e).__name__}:{e}"


def login_urls_for(platform_ids: list[str], registry: dict[str, PlatformSpec]) -> list[tuple[str, str, str]]:
    """Return list of (id, name, login_url)."""
    out = []
    for pid in platform_ids:
        pid = normalize_platform_id(pid)
        spec = registry.get(pid)
        if not spec:
            continue
        if not spec.login_url:
            continue
        out.append((spec.id, spec.name, spec.login_url))
    return out


def pick_best_candidate(cands: list[dict]) -> dict | None:
    if not cands:
        return None
    # highest score, then lowest price
    return sorted(cands, key=lambda x: (-x.get("score", 0), x.get("price_tax", 1e18)))[0]
