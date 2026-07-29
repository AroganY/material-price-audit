"""
Multi-platform registry.

Maintained built-ins: Guangcai, Lingcai, Huixun, JD, and 1688.
Additional sites can be registered in ``config.yaml`` with the generic adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote, quote_from_bytes

from .scraper import parse_price, score_title


def _quote_1688_query(query: str) -> str:
    """1688 搜索入口按 GBK 解码 keywords；UTF-8 会显示成“鍒嗘帶鍣�”。"""
    raw = (query or "").encode("gbk", errors="replace")
    return quote_from_bytes(raw, safe="")


def _quote_lingcai_query(query: str) -> str:
    """领材前端会连续 decode 两次，因此 gjz 必须双重 UTF-8 百分号编码。"""
    return quote(quote(query or "", safe=""), safe="")


def _normalize_1688_price_text(text: str | None) -> str:
    """Join a visually split decimal without joining price and minimum quantity."""
    raw = str(text or "").replace("\xa0", " ")
    return re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", raw)


def _page_1688_captcha(page) -> str | None:
    """识别阿里风控页，不能把验证码页当成正常的 0 条结果。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    try:
        body = (page.inner_text("body") or "")[:2000]
    except Exception:
        body = ""
    text = f"{title}\n{body}"
    if "/punish" in url or "_____tmd_____" in url or "x5secdata=" in url:
        return "1688 风控拦截"
    for marker in (
        "验证码拦截",
        "拖动下方滑块",
        "请按住滑块",
        "通过验证以确保正常访问",
        "安全验证",
    ):
        if marker in text:
            return marker
    return None


@dataclass
class PlatformSpec:
    id: str
    name: str
    login_url: str
    search_url_template: str  # must contain {query} when configured
    # built-in handler name or "generic"
    handler: str = "generic"
    require_login_hint: bool = True
    # generic selectors
    item_link_contains: str = ""  # substring that product URLs should contain
    item_link_selector: str = "a[href]"
    detail_price_selectors: list[str] = field(default_factory=list)
    notes: str = ""
    # if True, must set login_url+search_url in config before use (no fake defaults)
    requires_config: bool = False
    # if set, login_urls_for will skip when that platform already logged (same site)
    same_login_as: str = ""


# ---- built-in definitions ----
# 只写「实测可打开且标题匹配」的 URL；禁止把不同品牌网站指到广材网。

BUILTIN: dict[str, PlatformSpec] = {
    # ========== 造价材料信息站 ==========
    "guangcai": PlatformSpec(
        id="guangcai",
        name="广材网",
        # 实测：标题「广材网-建筑工程造价行业材料价格查询平台」
        login_url="https://www.gldjc.com/login",
        search_url_template="https://www.gldjc.com/scj/so.html?l=1&keyword={query}",
        handler="gldjc",
        item_link_contains="gldjc.com",
        item_link_selector='a[href*="gldjc.com"]',
        detail_price_selectors=[".price", "[class*='price']", "[class*='Price']"],
        notes="官网 https://www.gldjc.com/ · 登录 https://www.gldjc.com/login",
    ),
    "huixun": PlatformSpec(
        id="huixun",
        name="慧讯网",
        # 用户核实登录页：/login（不是 apply_trial）
        login_url="https://services.iccchina.com/login",
        search_url_template="https://services.iccchina.com/products",
        handler="huixun",
        item_link_contains="iccchina.com",
        item_link_selector='a[href*="iccchina.com"]',
        detail_price_selectors=[".price", "[class*='price']", "[class*='Price']"],
        same_login_as="",
        notes="登录 https://services.iccchina.com/login · 产品库 /products · RCC瑞达恒",
    ),
    "lingcai": PlatformSpec(
        id="lingcai",
        name="领材网",
        # 用户核实登录/用户中心：/userInfo/index.html（不是 lcIndex 首页）
        login_url="https://www.hylcw.cn/userInfo/index.html",
        search_url_template="https://www.hylcw.cn/marketPrice/so.html?index=0&type=1&gjz={query}",
        handler="lingcai",
        item_link_contains="hylcw.cn",
        item_link_selector='a[href*="hylcw.cn"]',
        detail_price_selectors=[".price", "[class*='price']", "[class*='Price']"],
        requires_config=False,
        notes="登录/用户中心 /userInfo/index.html · 市场价搜索 /marketPrice/so.html",
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
        # 真正登录页（首页 www.1688.com 未登录也会打开，不能当「已登录」）
        login_url="https://login.taobao.com/?redirect_url=https%3A%2F%2Fwww.1688.com%2F",
        search_url_template="https://s.1688.com/selloffer/offer_search.htm?keywords={query}",
        handler="1688",
        item_link_contains="detail.1688.com",
        detail_price_selectors=[".price-text", ".price"],
        notes="批发价，常需登录可见",
    ),
}

# Product UI order and the only built-ins covered by maintained adapters/tests.
CORE_PLATFORM_IDS = ("guangcai", "lingcai", "huixun", "jd", "1688")


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
        # 造价材料站
        "广材": "guangcai",
        "广材网": "guangcai",
        "gldjc": "guangcai",
        "guangcaiwang": "guangcai",
        "慧讯": "huixun",
        "慧讯网": "huixun",
        "huixunwang": "huixun",
        "iccchina": "huixun",
        "rcc": "huixun",
        "瑞达恒": "huixun",
        "领材": "lingcai",
        "领材网": "lingcai",
        "领财网": "lingcai",
        "lingcaiwang": "lingcai",
        "hylcw": "lingcai",
    }
    if p in aliases:
        return aliases[p]
    if p2 in aliases:
        return aliases[p2]
    return p


def load_platform_registry(cfg: dict | None = None) -> dict[str, PlatformSpec]:
    """Merge built-ins with config platforms.definitions (definitions fully override fields)."""
    reg = {k: PlatformSpec(**{**v.__dict__}) for k, v in BUILTIN.items()}
    cfg = cfg or {}
    plats = cfg.get("platforms") or {}
    definitions = {}
    if isinstance(plats, dict):
        definitions = plats.get("definitions") or {}
    for pid, raw in definitions.items():
        if not isinstance(raw, dict):
            continue
        pid = normalize_platform_id(str(pid))
        base = reg.get(pid)
        login = str(raw.get("login_url") or raw.get("home_url") or (base.login_url if base else "") or "")
        search = str(
            raw.get("search_url")
            or raw.get("search_url_template")
            or (base.search_url_template if base else "")
            or ""
        )
        reg[pid] = PlatformSpec(
            id=pid,
            name=str(raw.get("name") or (base.name if base else pid)),
            login_url=login,
            search_url_template=search,
            handler=str(raw.get("handler") or (base.handler if base else "generic")),
            require_login_hint=bool(raw.get("require_login", True if not base else base.require_login_hint)),
            item_link_contains=str(raw.get("item_link_contains") or (base.item_link_contains if base else "")),
            item_link_selector=str(raw.get("item_link_selector") or (base.item_link_selector if base else "a[href]")),
            detail_price_selectors=list(
                raw.get("detail_price_selectors")
                or raw.get("detail_price_selector")
                or (base.detail_price_selectors if base else [])
            ),
            notes=str(raw.get("notes") or (base.notes if base else "")),
            requires_config=False if (login and search) else bool(base.requires_config if base else True),
            same_login_as=str(raw.get("same_login_as") or (base.same_login_as if base else "")),
        )
    return reg


# ---- search handlers ----

def _jd_blocked_message(page) -> str | None:
    """检测京东限流/风控文案。"""
    try:
        body = (page.inner_text("body") or "")[:1500]
    except Exception:
        body = ""
    title = ""
    try:
        title = page.title() or ""
    except Exception:
        pass
    text = f"{title}\n{body}"
    keys = (
        "访问频繁",
        "无法搜索",
        "请稍后再试",
        "异常流量",
        "操作过于频繁",
        "访问太频繁",
        "系统繁忙",
        "验证码",
        "安全验证",
        "风险控制",
    )
    for k in keys:
        if k in text:
            return k
    return None


def _search_jd(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    """
    京东搜索。短词 + 等列表；命中「访问频繁」立即 rate_limited，禁止再刷。
    """
    q = (query or "").strip()[:40]
    url = spec.search_url_template.format(query=quote(q))
    goods = []
    # 只打开 1 次；被限流就返回，绝不二次 goto 加重封禁
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(4500)
    try:
        page.wait_for_selector("[data-sku], li.gl-item, .gl-i-wrap", timeout=6000)
    except Exception:
        pass

    cur = (page.url or "").lower()
    title = page.title() or ""
    if "passport" in cur or ("登录" in title and "商品搜索" not in title and "京东" not in title):
        return None, "need_login"

    blocked = _jd_blocked_message(page)
    if blocked:
        print(f"  [jd] 风控限流：页面提示含「{blocked}」→ 本会话停止搜京东")
        return None, "rate_limited"

    goods_n = page.evaluate(
        """() => {
          return document.querySelectorAll('[data-sku], li.gl-item').length;
        }"""
    )
    if not goods_n:
        # 再等一次异步渲染（不再重新 goto）
        page.wait_for_timeout(2500)
        blocked = _jd_blocked_message(page)
        if blocked:
            print(f"  [jd] 风控限流：{blocked}")
            return None, "rate_limited"
        goods_n = page.evaluate(
            """() => document.querySelectorAll('[data-sku], li.gl-item').length"""
        )
    if not goods_n:
        return [], "empty_page"

    # 新版：div[data-sku]；旧版：li.gl-item
    goods = page.evaluate(
        """() => {
          const out = [];
          const seen = new Set();
          const nodes = document.querySelectorAll('[data-sku], li.gl-item');
          for (const el of nodes) {
            const sku = el.getAttribute('data-sku') || el.getAttribute('data-spu') || '';
            if (!sku || seen.has(sku)) continue;
            seen.add(sku);
            let href = '';
            const a = el.querySelector('a[href*="item.jd.com"], a[href*="item.m.jd.com"]')
              || el.querySelector('a[href]');
            if (a && a.href && a.href.includes('item')) href = a.href;
            if (!href) href = 'https://item.jd.com/' + sku + '.html';
            const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            // 价格：¥ 452 . 42 / ¥452.42 / 到手价
            let priceText = '';
            const m1 = text.match(/[¥￥]\\s*(\\d+)\\s*[.\\u3002]?\\s*(\\d{0,2})/);
            if (m1) priceText = m1[2] ? (m1[1] + '.' + m1[2]) : m1[1];
            if (!priceText) {
              const m2 = text.match(/(\\d+\\.\\d{2})/);
              if (m2) priceText = m2[1];
            }
            // 名称：去掉广告前缀
            let name = text.replace(/^广告\\s*/, '').slice(0, 160);
            const nameEl = el.querySelector('.p-name em, .p-name a, [class*="name"]');
            if (nameEl && (nameEl.innerText || '').trim().length > 4) {
              name = nameEl.innerText.replace(/\\s+/g, ' ').trim().slice(0, 160);
            }
            out.push({ sku, href, priceText, name });
            if (out.length >= 20) break;
          }
          return out;
        }"""
    )
    cands = _filter_cands(goods or [], must, min_score, "jd", "item.jd.com")
    # 链接过滤过严时：允许我们构造的 item.jd.com/{sku}
    if not cands and goods:
        loose = []
        for g in goods:
            price = parse_price(g.get("priceText"))
            name = g.get("name") or ""
            href = g.get("href") or ""
            if not price:
                continue
            sc = score_title(name + " " + href, must)
            if sc < min_score:
                continue
            loose.append(
                {
                    "title": name[:160],
                    "price_tax": price,
                    "url": (href.split("?")[0] if href else f"https://item.jd.com/{g.get('sku')}.html"),
                    "sku": g.get("sku") or "",
                    "score": sc,
                    "platform": "jd",
                    "price_text": str(g.get("priceText") or ""),
                    "price_source": "search_list",
                    "tax_mode": "tax_incl",
                }
            )
        loose.sort(key=lambda x: (-x["score"], x["price_tax"]))
        return loose[:15], "ok"
    return cands, "ok"


def _search_1688(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    url = spec.search_url_template.format(query=_quote_1688_query(query))
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(3000)
    captcha = _page_1688_captcha(page)
    if captcha:
        print(f"  [1688] {captcha} → 等用户完成验证，本次不报空结果")
        return None, "captcha"
    if "login" in page.url.lower() or "登录" in (page.title() or ""):
        return None, "need_login"

    # 2026 新版列表的商品卡片本身是 a.search-offer-wrapper，链接改为
    # detail.m.1688.com/page/index.html?offerId=... 。保留旧版选择器作回退。
    card_selector = (
        'a.search-offer-wrapper, '
        'a[href*="detail.m.1688.com"], '
        'a[href*="detail.1688.com"]'
    )
    try:
        page.wait_for_selector(card_selector, timeout=min(8000, max(1500, timeout_ms // 3)))
    except Exception:
        # 真正的空结果/风控页会走下面的统一判定，不在这里报错。
        pass
    captcha = _page_1688_captcha(page)
    if captcha:
        print(f"  [1688] {captcha} → 等用户完成验证，本次不报空结果")
        return None, "captcha"
    cards = page.eval_on_selector_all(
        card_selector,
        """els => {
          const out=[], seen=new Set();
          for (const a of els.slice(0, 80)) {
            const href = a.href || '';
            if (!href.includes('detail.1688.com') && !href.includes('detail.m.1688.com')) continue;
            const offerMatch = href.match(/[?&]offerId=(\\d+)/i) || href.match(/\\/offer\\/(\\d+)\\.html/i);
            const offerId = offerMatch ? offerMatch[1] : '';
            const dedupeKey = offerId || href;
            if (seen.has(dedupeKey)) continue;
            seen.add(dedupeKey);

            let root = a.matches('.search-offer-wrapper')
              ? a
              : a.closest('.search-offer-wrapper, .search-offer-item, [data-tracker="offer"]');
            if (!root) {
              root = a;
              for (let i=0; i<6 && root.parentElement; i++) {
                root = root.parentElement;
                if ((root.innerText || '').includes('¥')) break;
              }
            }
            const text=(root.innerText||a.innerText||'').replace(/\\s+/g,' ').trim().slice(0,500);
            const titleEl = root.querySelector(
              '.offer-title-row .title-text, .offer-title-row, [class*="title"]'
            );
            const priceEl = root.querySelector(
              '.offer-price-row .price-item, .offer-price-row, [class*="price"]'
            );
            const supplierEl = root.querySelector(
              '.offer-shop-row .col-left .desc-text, .offer-shop-row .desc-text, [class*="shop"] [class*="desc"]'
            );
            const name=(titleEl?.innerText||a.innerText||text).replace(/\\s+/g,' ').trim().slice(0,180);
            const priceText=(priceEl?.innerText||text).replace(/\\s+/g,' ').trim();
            const supplier=(supplierEl?.innerText||'').replace(/\\s+/g,' ').trim().slice(0,100);
            out.push({href, text, name, priceText, supplier, offerId});
          }
          return out.slice(0, 40);
        }""",
    )
    cands = []
    for c in cards or []:
        text = c.get("text") or c.get("name") or ""
        name = c.get("name") or text
        # 1688 会把 2.09 拆成两个 DOM 节点，innerText 是“¥ 2 .09”。
        # 只移除价格里的空白，避免把 2.09 误读为 2。
        price_text = _normalize_1688_price_text(c.get("priceText"))
        price = parse_price(price_text)
        href = c.get("href") or ""
        sc = score_title(f"{name} {text}", must)
        if not price or not href or sc < min_score:
            continue
        offer_id = str(c.get("offerId") or "")
        detail_url = (
            f"https://detail.1688.com/offer/{offer_id}.html"
            if offer_id
            else href
        )
        cands.append(
            {
                "title": name[:160],
                "price_tax": price,
                "url": detail_url,
                "source_url": href,
                "sku": offer_id,
                "score": sc,
                "platform": "1688",
                "supplier": c.get("supplier") or "",
                "price_text": price_text,
                "price_source": "search_list",
                # 列表只负责找候选；正式收价前仍打开详情核对完整规格。
                # 桌面详情比 detail.m 移动页稳定，也方便 Excel 点击复核。
                "spec_seen": text,
            }
        )
    cands.sort(key=lambda x: (-x["score"], x["price_tax"]))
    return cands[:20], "ok"


def _page_membership_blocked(page) -> bool:
    """广材等站：无会员/权限不足/付费墙 → 应跳过整站，别死等。"""
    try:
        url = (page.url or "").lower()
        title = page.title() or ""
        body = ""
        try:
            body = (page.inner_text("body") or "")[:2000]
        except Exception:
            body = ""
        text = f"{title}\n{body}"
        keys = (
            "开通会员",
            "购买会员",
            "会员专享",
            "请开通",
            "权限不足",
            "无访问权限",
            "续费",
            "升级会员",
            "非会员",
            "成为会员",
            "充值",
            "套餐已过期",
            "vip",
            "VIP",
        )
        if any(k in text for k in keys):
            return True
        if "member" in url and ("buy" in url or "open" in url or "pay" in url):
            return True
    except Exception:
        pass
    return False


def _search_gldjc(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    """
    广材网（gldjc.com）材料搜索。
    搜索 URL: /scj/so.html?l=1&keyword=...
    未登录 → need_login；无会员 → no_membership（调用方应跳过整站）。
    """
    url = spec.search_url_template.format(query=quote(query))
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(2500)
    cur = page.url or ""
    title = page.title() or ""
    if "/login" in cur or "登录" in title:
        return None, "need_login"
    if _page_membership_blocked(page):
        return None, "no_membership"

    # 默认只展示每个材料的前几条厂家报价；逐个展开后再解析，避免漏掉精确规格。
    try:
        for _ in range(24):
            more = page.get_by_text("查看更多报价", exact=True)
            if more.count() <= 0:
                break
            btn = more.first
            if not btn.is_visible(timeout=300):
                break
            btn.click(force=True, timeout=1500)
            page.wait_for_timeout(120)
        page.wait_for_timeout(500)
    except Exception:
        pass

    # SSR 数据比可见 DOM 更完整：含联系人、电话、盖章报价单链接，以及折叠的厂家报价。
    rows = page.evaluate(
        """() => {
          const data = window.__NUXT__?.data?.[0] || {};
          const products = Array.isArray(data.searchResData) ? data.searchResData : [];
          const out = [];
          for (const [ri, p] of products.entries()) {
            const name = String(p?.name || p?.core_word_precise || '').trim();
            const specParts = [];
            for (const x of (p?.specDataArr || [])) {
              const label = String(x?.name || '').trim();
              const value = String(x?.desc || '').trim();
              if (label || value) specParts.push(`${label} : ${value}`.trim());
            }
            const base = specParts.join(' | ') || String(
              p?.specificationattr_str || p?.specificationattr_all_str || ''
            ).trim();
            for (const [qi, c] of (p?.companies || []).entries()) {
              const price = c?.market_price ?? c?.engineering_price ?? c?.market_price_te ?? '';
              if (!name || price === '' || price == null) continue;
              const quoteSpec = String(c?.resource_attrvalues || '').trim();
              const brand = String(c?.brand_name || '').trim();
              const supplier = String(c?.company_name || c?.name || '').trim();
              const unit = String(c?.unit || '').trim();
              const phone = String(c?.company_phone || '').trim();
              const contact = String(c?.company_contact_person || '').trim();
              const quotationUrl = String(c?.quotation_file_path || '').trim();
              const text = [name, base, quoteSpec, brand, unit ? `单位:${unit}` : '', supplier]
                .filter(Boolean).join(' ');
              out.push({
                name, base, quoteSpec, brand, supplier, unit,
                price: String(price), phone, contact, quotationUrl, text, ri, qi
              });
            }
          }
          return out.slice(0, 120);
        }"""
    )

    # 页面结构变化或无 SSR 数据时，再退回可见 DOM。
    if not rows:
        rows = page.eval_on_selector_all(
        ".tr",
        """rows => {
          const out = [];
          for (const [ri, row] of rows.entries()) {
            const nameEl = row.querySelector('.m-name');
            const quoteRows = [...row.querySelectorAll('.colspan-row')];
            if (!nameEl || !quoteRows.length) continue;
            const name = (nameEl.getAttribute('title') || nameEl.innerText || '').replace(/\\s+/g, ' ').trim();
            const base = (row.querySelector('.m-detail-content')?.innerText || '').replace(/\\s+/g, ' ').trim();
            for (const [qi, q] of quoteRows.entries()) {
              const quoteSpec = (q.querySelector('.resource-attrvalues-text')?.innerText || '').replace(/\\s+/g, ' ').trim();
              const brand = (q.querySelector('.brand-box')?.getAttribute('title') || '').trim();
              const supplier = (q.querySelector('.supplier-name')?.innerText || '').replace(/\\s+/g, ' ').trim();
              const price = (q.querySelector('.tax-price .change-point[title]')?.getAttribute('title') || q.querySelector('.price-block')?.innerText || '').trim();
              const unit = (q.querySelector('.width-56')?.innerText || '').trim();
              const text = [name, base, quoteSpec, brand, unit ? ('单位:' + unit) : '', supplier].filter(Boolean).join(' ');
              if (!name || !price) continue;
              out.push({name, base, quoteSpec, brand, supplier, unit, price, text, ri, qi});
            }
          }
          return out.slice(0, 80);
        }""",
        )
    cands = []
    for row in rows or []:
        price = parse_price(row.get("price"))
        if not price:
            continue
        text = row.get("text") or ""
        sc = max(1, score_title(text, must))
        sku = "|".join(
            str(row.get(k) or "")
            for k in ("name", "base", "quoteSpec", "brand", "supplier", "price")
        )[:500]
        cands.append(
            {
                "title": str(row.get("name") or "")[:160],
                "price_tax": price,
                "url": page.url,
                "sku": sku,
                "score": sc,
                "platform": spec.id,
                "inline_detail": True,
                "detail_text": text[:3000],
                "spec_seen": " | ".join(
                    x for x in (str(row.get("base") or ""), str(row.get("quoteSpec") or "")) if x
                )[:1000],
                "supplier": str(row.get("supplier") or ""),
                "contact": str(row.get("contact") or ""),
                "phone": str(row.get("phone") or ""),
                "quotation_url": str(row.get("quotationUrl") or ""),
                "unit": str(row.get("unit") or ""),
                "price_text": str(row.get("price") or ""),
                "price_context": "同一厂家报价行",
                "price_source": "platform_quote_row",
                "tax_mode": "tax_incl",
                "sku_scope": "exact_quote_row",
            }
        )
    cands.sort(key=lambda x: (-x["score"], x["price_tax"]))
    return cands[:60], "ok" if cands else "empty_page"


def _try_fill_site_search(page, query: str) -> bool:
    """Best-effort: type keyword into common search boxes and submit (慧讯/领材等 SPA 站)。"""
    selectors = [
        'input[type="search"]',
        'input[placeholder*="搜索"]',
        'input[placeholder*="查找"]',
        'input[placeholder*="材料"]',
        'input[placeholder*="关键字"]',
        'input[placeholder*="关键词"]',
        'input[name*="keyword" i]',
        'input[name*="search" i]',
        'input[id*="search" i]',
        'input[id*="keyword" i]',
        'input.el-input__inner',
        'input[type="text"]',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible(timeout=800):
                continue
            loc.click(timeout=1500)
            loc.fill("")
            loc.fill(query[:80])
            loc.press("Enter")
            page.wait_for_timeout(2500)
            return True
        except Exception:
            continue
    return False


def _member_page_state(page, platform_id: str) -> tuple[str, str]:
    """会员站公共状态：只做明确判断，不把空壳/登录页当 0 条结果。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    try:
        body = (page.inner_text("body") or "")[:2200]
    except Exception:
        body = ""
    text = f"{title}\n{body}"
    if "/login" in url or "passport" in url:
        return "need_login", body
    if platform_id == "lingcai" and "/userinfo/index.html" in url:
        # 搜索路由跳回用户中心是未登录/无权限，不是搜索无结果。
        return "need_login", body
    if any(k in text for k in ("账号登录", "扫码登录", "微信扫码登录", "登录后查看", "请先登录")):
        return "need_login", body
    if _page_membership_blocked(page):
        return "no_membership", body
    if any(k in body for k in ("暂无数据", "暂无结果", "没有找到", "未找到相关", "无搜索结果")):
        return "empty_page", body
    if len(body.strip()) < 40:
        return "empty_page", body
    return "ok", body


def _extract_member_rows(page, platform_id: str) -> list[dict[str, Any]]:
    """从同一结果行提取标题、规格、价格、单位，避免跨卡片串价。"""
    # 领材的每一条厂家报价就是一个 .list-item。只取这一层，避免再把内部
    # price-item 当成第二条记录，造成标题、规格和价格重复或错位。
    selectors = (
        ".list-item"
        if platform_id == "lingcai"
        else (
            ".el-table__row, .ant-table-row, tbody > tr, "
            "[class*='product-item'], [class*='material-item'], [class*='price-item']"
        )
    )
    try:
        rows = page.eval_on_selector_all(
            selectors,
            """(rows, platformId) => {
              const out = [], seen = new Set();
              for (const [index, row] of rows.entries()) {
                const text = (row.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!text || text.length < 4 || text.length > 1600) continue;
                if (/登录|注册|网站导航/.test(text) && text.length < 60) continue;
                const a = row.querySelector('a[href]');
                const href = a?.href || '';
                const titleEl = row.querySelector(
                  '[class*="name"], [class*="title"], [class*="material"], td:nth-child(1), td:nth-child(2)'
                );
                let name = (titleEl?.getAttribute('title') || titleEl?.innerText || a?.innerText || '')
                  .replace(/\\s+/g, ' ').trim().slice(0, 180);
                if (!name) name = text.slice(0, 180);
                const priceEl = row.querySelector(
                  '[class*="price"], [class*="amount"], [class*="market"]'
                );
                const priceText = (priceEl?.innerText || text).replace(/\\s+/g, ' ').trim();
                const key = `${href}|${name}|${priceText.slice(0, 80)}`;
                if (seen.has(key)) continue;
                seen.add(key);
                const dataId = row.querySelector('input[data-id]')?.getAttribute('data-id') || '';
                out.push({index, href, name, text, priceText, hasPriceNode: !!priceEl, dataId, platformId});
                if (out.length >= 80) break;
              }
              return out;
            }""",
            platform_id,
        )
        return list(rows or [])
    except Exception:
        return []


def _member_rows_to_candidates(
    page,
    rows: list[dict[str, Any]],
    query: str,
    must: list[str],
    min_score: int,
    spec: PlatformSpec,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    current_url = page.url or spec.search_url_template
    for row in rows:
        text = str(row.get("text") or "")
        name = str(row.get("name") or text)[:180]
        if spec.id == "lingcai" and "价格因子" in text:
            name = text.split("价格因子", 1)[0].strip()[:180] or name
        href = str(row.get("href") or "")
        if href and spec.item_link_contains and spec.item_link_contains not in href:
            href = ""
        sc = score_title(f"{name} {text}", must)
        # 会员站结果页已经按 query 做过服务端筛选。这里不能再用“所有必选规格
        # 命中数”砍候选：例如搜索“8端口分控器”时，领材标题只有“分控器”，
        # “8端口”在规格字段内，旧逻辑会把整页 29 条误报为 0 条。
        # 精度由后续 strict_name_spec_match 统一把关，缺一项也不会进入正式价。
        price_text = str(row.get("priceText") or "")
        labeled_price = re.search(
            r"(含税价|除税价|市场价|信息价|单价|价格)\s*[:：]\s*[¥￥]?\s*(\d+(?:\.\d+)?)"
            r"(?:\s*/\s*(m²|m³|㎡|米|m|个|件|套|台|组|kg|t|吨))?",
            text,
            re.I,
        )
        if labeled_price:
            price = parse_price(labeled_price.group(2))
            price_text = labeled_price.group(0)
            unit_value = labeled_price.group(3) or ""
            label = labeled_price.group(1)
            tax_mode = "tax_excl" if "除税" in label else "tax_incl" if "含税" in label else "unknown"
        else:
            has_money_marker = bool(re.search(r"[¥￥]|\d+(?:\.\d+)?\s*元", price_text))
            price = parse_price(price_text) if (row.get("hasPriceNode") and has_money_marker) else None
            unit_match = re.search(
                r"(?:单位|计价单位)\s*[:：]?\s*(m²|m³|㎡|米|m|个|件|套|台|组|kg|t|吨)",
                text,
                re.I,
            )
            unit_value = unit_match.group(1) if unit_match else ""
            tax_mode = "unknown"
        supplier_match = re.search(
            r"查看联系方式\s*(?:[\u4e00-\u9fff]{2,10})?\s*([^\s]{4,60}(?:公司|商行|经营部|厂))\s*报价时间",
            text,
        )
        cand = {
            "title": name,
            "price_tax": price or 0.01,
            "url": href or current_url,
            "sku": str(row.get("dataId") or f"{spec.id}:{row.get('index', '')}:{_norm_row_key(name, text)}"),
            "score": max(sc, 1),
            "platform": spec.id,
            "supplier": supplier_match.group(1) if supplier_match else "",
            "unit": unit_value,
            "tax_mode": tax_mode,
            "price_text": price_text[:300],
            "price_context": text[:500],
            "price_source": "platform_result_row" if price else "missing",
            "spec_seen": text[:1200],
        }
        if price:
            cand.update(
                inline_detail=True,
                detail_text=text[:4000],
                sku_scope="exact_result_row",
            )
        else:
            cand["needs_detail_price"] = True
        out.append(cand)
    out.sort(key=lambda x: (-x.get("score", 0), x.get("price_tax", 1e18)))
    return out[:40]


def _norm_row_key(name: str, text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", f"{name}|{text}".lower())[:120]


def _search_lingcai(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    url = spec.search_url_template.format(query=_quote_lingcai_query(query))
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(2600)
    state, _body = _member_page_state(page, "lingcai")
    if state != "ok":
        return ([] if state == "empty_page" else None), state
    rows = _extract_member_rows(page, "lingcai")
    cands = _member_rows_to_candidates(page, rows, query, must, min_score, spec)
    return cands, "ok" if cands else "empty_page"


def _search_huixun(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    # 慧讯是 SPA：进入产品库后用页面搜索框输入 Unicode，不在 URL 二次编码。
    current = (page.url or "").lower()
    if "iccchina.com/products" not in current:
        page.goto(spec.search_url_template, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1800)
    state, _body = _member_page_state(page, "huixun")
    if state not in ("ok", "empty_page"):
        return None, state
    if not _try_fill_site_search(page, query):
        return [], "search_control_missing"
    page.wait_for_timeout(1800)
    state, _body = _member_page_state(page, "huixun")
    if state != "ok":
        return ([] if state == "empty_page" else None), state
    rows = _extract_member_rows(page, "huixun")
    cands = _member_rows_to_candidates(page, rows, query, must, min_score, spec)
    return cands, "ok" if cands else "empty_page"


def _search_generic(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    """
    通用搜索：慧讯(iccchina)、领材(hylcw) 等非广材站。
    - 模板含 {query} → 直接拼 URL（只 goto 一次，禁止空结果连环刷新）
    - 未登录/空壳页 → need_login / empty_page，交给上层换站
    """
    if not spec.search_url_template:
        return None, "bad_config"

    if "{query}" in spec.search_url_template:
        url = spec.search_url_template.format(query=quote(query))
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2800)
    else:
        page.goto(spec.search_url_template, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2000)
        cur = page.url or ""
        title = page.title() or ""
        if "/login" in cur.lower() or ("登录" in title and "申请" not in title):
            if spec.require_login_hint:
                return None, "need_login"
        if _page_membership_blocked(page):
            return None, "no_membership"
        if not _try_fill_site_search(page, query):
            page.wait_for_timeout(500)

    cur = (page.url or "").lower()
    title = page.title() or ""
    try:
        body = (page.inner_text("body") or "")[:1500]
    except Exception:
        body = ""

    # 未登录硬判断（领材空壳页常见）
    if spec.require_login_hint:
        if "/login" in cur or "passport" in cur:
            return None, "need_login"
        if any(k in f"{title}\n{body}" for k in ("请登录", "立即登录", "用户登录", "登录后查看", "登录后可见")):
            return None, "need_login"
    if _page_membership_blocked(page):
        return None, "no_membership"

    # 明显空页：无结果话术或几乎无内容
    if any(k in body for k in ("暂无数据", "暂无结果", "没有找到", "未找到相关", "0条结果", "无搜索结果")):
        return [], "empty_page"
    if len(body.strip()) < 40:
        return [], "empty_page"

    link_sel = spec.item_link_selector or "a[href]"
    contains = spec.item_link_contains or ""
    cards = page.eval_on_selector_all(
        link_sel,
        """(els, contains) => {
          const out=[], seen=new Set();
          for (const a of els.slice(0, 100)) {
            let href = a.href || '';
            if (!href || href.startsWith('javascript')) continue;
            if (contains && !href.includes(contains)) continue;
            // 过滤导航/登录无用链接
            const low = href.toLowerCase();
            if (low.includes('/login') || low.includes('userinfo') || low.endsWith('.css')) continue;
            if (seen.has(href)) continue;
            seen.add(href);
            let root = a;
            for (let i=0;i<5;i++){ if(root.parentElement) root=root.parentElement; }
            const text=(root.innerText||a.innerText||'').replace(/\\s+/g,' ').trim().slice(0,240);
            const name=(a.innerText||text).replace(/\\s+/g,' ').trim().slice(0,160);
            if (!name || name.length < 2) continue;
            out.push({href, name, priceText: text, text});
          }
          return out.slice(0, 30);
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
    cands = _filter_cands(goods, must, min_score, spec.id, contains)
    # 领材/慧讯列表价常藏在表格，过滤过严时放宽：有名有链即可进详情抽价
    if not cands and goods and spec.id in ("lingcai", "huixun", "guangcai"):
        loose = []
        for g in goods:
            href = g.get("href") or ""
            name = g.get("name") or ""
            if not href or not name or len(name) < 4:
                continue
            price = parse_price(g.get("priceText"))
            sc = score_title(name + " " + href, must)
            if sc < 1 and must:
                # 至少命中 must 里一个字或直接放行进详情（造价站已按 keyword 过滤）
                if not any((m or "")[:2] in name for m in must if m):
                    continue
            loose.append(
                {
                    "title": name[:160],
                    "price_tax": price or 0.01,  # 占位，详情页再修正；0 会被详情覆盖
                    "url": href.split("?")[0],
                    "sku": "",
                    "score": max(sc, 1),
                    "platform": spec.id,
                    "needs_detail_price": not bool(price),
                }
            )
        loose.sort(key=lambda x: -x["score"])
        cands = loose[:12]
    if not cands:
        return [], "empty_page"
    return cands, "ok"


def _filter_cands(goods, must, min_score, platform_id, link_hint) -> list[dict] | None:
    cands = []
    for g in goods or []:
        name = g.get("name") or ""
        href = g.get("href") or ""
        price = parse_price(g.get("priceText"))
        sc = score_title(name + " " + href, must)
        if not price or not href:
            continue
        if link_hint and link_hint not in href and platform_id != "jd":
            # 京东新版常无 item 锚点，href 由 SKU 拼出。
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
                    "price_text": str(g.get("priceText") or ""),
                    "price_source": "search_list",
                    "tax_mode": "tax_incl" if platform_id == "jd" else "unknown",
                }
            )
    cands.sort(key=lambda x: (-x["score"], x["price_tax"]))
    return cands  # return full list for multi-pick; caller may take [0]


HANDLERS: dict[str, Callable] = {
    "jd": _search_jd,
    "1688": _search_1688,
    "gldjc": _search_gldjc,
    "lingcai": _search_lingcai,
    "huixun": _search_huixun,
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
    if getattr(spec, "requires_config", False) and (
        not spec.login_url or not spec.search_url_template or "{query}" not in spec.search_url_template
    ):
        print(
            f"  [{pid}] 未配置真实搜索地址，跳过（请配置 platforms.definitions.{pid}，勿误用广材网）"
        )
        return [], "not_configured"
    if not spec.search_url_template or (
        "{query}" not in spec.search_url_template and spec.handler not in ("generic",)
    ):
        if "{query}" not in (spec.search_url_template or "") and spec.handler == "gldjc":
            return [], "not_configured"
    handler_name = spec.handler if spec.handler in HANDLERS else "generic"
    # map built-in id to specialized handler（慧讯/领材各自域名，绝不能走 gldjc）
    if pid == "jd":
        handler_name = "jd"
    elif pid == "1688":
        handler_name = "1688"
    elif pid == "guangcai" or (handler_name == "gldjc" and "gldjc" in (spec.item_link_contains or "")):
        handler_name = "gldjc"
    elif pid == "huixun":
        handler_name = "huixun"
    elif pid == "lingcai":
        handler_name = "lingcai"
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
    """
    Return list of (id, name, login_url).
    - skip empty login_url
    - dedupe by login_url only（广材/慧讯/领材域名不同，会分别打开）
    """
    out = []
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()
    for pid in platform_ids:
        pid = normalize_platform_id(pid)
        spec = registry.get(pid)
        if not spec:
            print(f"  [skip] 未知平台 id={pid}")
            continue
        if getattr(spec, "requires_config", False) and (
            not spec.login_url or not spec.search_url_template
        ):
            print(
                f"  [skip] {spec.name}({pid}) 未配置真实官网。"
                f"请在 config.yaml → platforms.definitions.{pid} 填写 login_url / search_url"
            )
            continue
        if not spec.login_url:
            print(f"  [skip] {spec.name}({pid}) 无 login_url")
            continue
        # resolve same_login_as
        login_url = spec.login_url
        if spec.same_login_as:
            parent = registry.get(normalize_platform_id(spec.same_login_as))
            if parent and parent.login_url:
                login_url = parent.login_url
        key = login_url.rstrip("/")
        if key in seen_urls:
            print(f"  [dedupe] {spec.name} 与已打开站点共用登录页，跳过重复弹窗: {login_url}")
            continue
        if pid in seen_ids:
            continue
        seen_urls.add(key)
        seen_ids.add(pid)
        out.append((spec.id, spec.name, login_url))
    return out
