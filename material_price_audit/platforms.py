"""
Multi-platform registry.

Maintained built-ins: Guangcai, Lingcai, Huixun, Yize (EasyBii), Zaojiatong,
JD, and 1688.
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
    "yize": PlatformSpec(
        id="yize",
        name="易择网",
        # 首页即登录页（密码/扫码/免密）；登录后顶栏搜索产品信息/信息价
        login_url="https://www.easybii.com/",
        # 信息价首页带全局搜索框；查询靠页面交互填入，不拼 {query}
        search_url_template="https://www.easybii.com/P4-3-info-price-home.html",
        handler="yize",
        item_link_contains="easybii.com",
        item_link_selector='a[href*="easybii.com"]',
        detail_price_selectors=[
            ".price",
            "[class*='price']",
            "[class*='market']",
            "td",
        ],
        notes="官网 https://www.easybii.com/ · 产品信息/信息价双通道搜索",
    ),
    "zaojiatong": PlatformSpec(
        id="zaojiatong",
        name="造价通",
        # 必须带 url= 回跳参数：登录成功后跳回分站市场价，才能写齐 .zjtcn.com 会话
        # （只停在 member 登录页时，后续每开一条 gd 链接都会再被踢去登录）
        login_url=(
            "https://member.zjtcn.com/common/login.html"
            "?url=https%3A%2F%2Fgd.zjtcn.com%2Fshichangjia%2Flist%2Fc_t_d_k.html"
        ),
        # 默认广东分站市场价；{query} 拼入路径（实测 c_t_d_k_关键词.html）
        search_url_template="https://gd.zjtcn.com/shichangjia/list/c_t_d_k_{query}.html",
        handler="zaojiatong",
        item_link_contains="zjtcn.com",
        item_link_selector='a[href*="shichangjia/info_"], a[href*="zjtcn.com"]',
        detail_price_selectors=[
            ".text-orange-color",
            "[class*='price']",
            "[class*='Price']",
            "td",
        ],
        notes="官网 https://www.zjtcn.com/ · 市场价 gd.zjtcn.com/shichangjia · 登录须带 url 回跳分站",
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
CORE_PLATFORM_IDS = ("guangcai", "lingcai", "huixun", "yize", "zaojiatong", "jd", "1688")

# 零售/批发电商：价只作市场参考，不得写入正式合格价（见 docs/ECOMMERCE_POLICY.md）
ECOMMERCE_PLATFORM_IDS = frozenset({"jd", "1688", "taobao", "tmall", "jingdong"})


def is_ecommerce_platform(pid: str | None) -> bool:
    p = normalize_platform_id(pid) if pid is not None else ""
    if p in ECOMMERCE_PLATFORM_IDS:
        return True
    # normalize may map 京东→jd already; keep bare checks
    return str(pid or "").strip().lower() in ECOMMERCE_PLATFORM_IDS


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
        "易择": "yize",
        "易择网": "yize",
        "易泽": "yize",
        "易泽网": "yize",
        "easybii": "yize",
        "yizewang": "yize",
        "造价通": "zaojiatong",
        "zjtcn": "zaojiatong",
        "zjt": "zaojiatong",
        "zaojia": "zaojiatong",
        "中建普联": "zaojiatong",
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
                "price_context": (
                    f"广材搜索结果第{int(row.get('ri') or 0) + 1}个材料组 / "
                    f"第{int(row.get('qi') or 0) + 1}条厂家报价；"
                    f"页面价格={str(row.get('price') or '').strip()}"
                ),
                "source_group_index": int(row.get("ri") or 0) + 1,
                "source_quote_index": int(row.get("qi") or 0) + 1,
                "source_row_label": (
                    f"广材搜索结果第{int(row.get('ri') or 0) + 1}个材料组 / "
                    f"第{int(row.get('qi') or 0) + 1}条厂家报价"
                ),
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
            "price_context": (
                f"领材搜索结果第{int(row.get('index') or 0) + 1}条厂家报价；"
                f"{price_text[:180]}；报价ID={str(row.get('dataId') or '-')}"
            ),
            "source_row_index": int(row.get("index") or 0) + 1,
            "source_row_label": (
                f"领材搜索结果第{int(row.get('index') or 0) + 1}条厂家报价"
                + (
                    f"（报价ID {str(row.get('dataId'))}）"
                    if row.get("dataId")
                    else ""
                )
            ),
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


def _huixun_try_resume_if_needed(page, timeout_ms: int) -> bool:
    """关窗重开后若卡在登录/一键登录页，自动点入；返回是否可用产品库。"""
    from .login_gate import (
        looks_like_hard_login_url,
        page_shows_one_click_login,
        try_resume_huixun_session,
    )

    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if (
        looks_like_hard_login_url(url)
        or page_shows_one_click_login(page)
        or ("login" in url and "iccchina" in url)
    ):
        ok, _ = try_resume_huixun_session(page, timeout_ms=timeout_ms)
        return ok
    return True


def _huixun_on_products(page) -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "login" in url or "passport" in url:
        return False
    return "iccchina.com" in url and "/products" in url


def ensure_platform_workspace(
    page,
    platform_id: str,
    spec: PlatformSpec,
    timeout_ms: int,
) -> tuple[bool, str]:
    """
    保证浏览器停在该站「搜价工作台」页，而不是每条材料都重新打开登录/新链接。

    返回 (是否可用, 原因)。造价通/慧讯等会员站：工作台一旦就绪，后续只改搜索框。
    """
    pid = (platform_id or "").lower()
    if pid == "huixun":
        from .login_gate import looks_like_hard_login_url, page_shows_one_click_login

        if _huixun_on_products(page):
            return True, "已在慧讯产品库"
        if not _huixun_try_resume_if_needed(page, timeout_ms):
            if looks_like_hard_login_url((page.url or "").lower()) or page_shows_one_click_login(
                page
            ):
                return False, "need_login"
        if not _huixun_on_products(page):
            try:
                page.goto(
                    spec.search_url_template or "https://services.iccchina.com/products",
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                page.wait_for_timeout(1200)
            except Exception as e:
                return False, f"error:{e}"
            if not _huixun_try_resume_if_needed(page, timeout_ms):
                return False, "need_login"
        return (True, "慧讯工作台就绪") if _huixun_on_products(page) else (False, "need_login")

    if pid == "zaojiatong":
        from .login_gate import (
            install_zaojiatong_dialog_auto_accept,
            try_handle_zaojiatong_session_conflict,
        )

        install_zaojiatong_dialog_auto_accept(page)
        try_handle_zaojiatong_session_conflict(page)
        if _zaojiatong_on_market_page(page):
            # 确认搜索框还在
            try:
                if page.locator("#indexKey2, input[placeholder*='关键词'], input[placeholder*='材料名称']").count() > 0:
                    return True, "已在造价通市场价页"
            except Exception:
                return True, "已在造价通市场价页"
        landing = "https://gd.zjtcn.com/shichangjia/list/c_t_d_k.html"
        try:
            page.goto(landing, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(600)  # 短等：尽快用框，别等 SPA 踢登录太久
        except Exception as e:
            return False, f"error:{e}"
        try_handle_zaojiatong_session_conflict(page)
        if _zaojiatong_on_market_page(page):
            return True, "造价通工作台已打开"
        # 被踢登录页
        state, _ = _zaojiatong_page_state(page)
        if state == "need_login":
            return False, "need_login"
        return False, "empty_page"

    # 其它站：有 search 模板则打开一次
    tpl = spec.search_url_template or ""
    if tpl and "{query}" not in tpl:
        try:
            cur = (page.url or "").lower()
        except Exception:
            cur = ""
        if tpl.split("?")[0].split("/")[-1] and tpl.split("/")[2] in cur:
            return True, "已在目标站"
        try:
            page.goto(tpl, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1000)
            return True, "已打开目标站"
        except Exception as e:
            return False, f"error:{e}"
    return True, "ok"


def restore_platform_workspace(
    page,
    platform_id: str,
    registry: dict[str, PlatformSpec] | None = None,
    timeout_ms: int = 30000,
) -> None:
    """详情页看完后回到搜价工作台，避免下一条又 goto 登录。"""
    pid = normalize_platform_id(platform_id)
    reg = registry or BUILTIN
    spec = reg.get(pid) or BUILTIN.get(pid)
    if not spec:
        return
    try:
        ensure_platform_workspace(page, pid, spec, timeout_ms)
    except Exception:
        pass


def _search_huixun(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    """
    慧讯：登录一次进产品库后，**只改搜索框重搜**，禁止每条材料重新 goto 登录/产品库。
    """
    ok_ws, ws_reason = ensure_platform_workspace(page, "huixun", spec, timeout_ms)
    if not ok_ws:
        return None, ws_reason if ws_reason.startswith("need_login") or ws_reason == "need_login" else (
            "need_login" if "login" in ws_reason else ws_reason
        )

    state, _body = _member_page_state(page, "huixun")
    if state == "need_login":
        if not _huixun_try_resume_if_needed(page, timeout_ms):
            return None, "need_login"
        # 一键登录后回到产品库，仍不重新 goto 除非丢了
        if not _huixun_on_products(page):
            ok_ws, ws_reason = ensure_platform_workspace(page, "huixun", spec, timeout_ms)
            if not ok_ws:
                return None, "need_login"
        state, _body = _member_page_state(page, "huixun")
    if state not in ("ok", "empty_page"):
        return None, state

    # 核心：同页改词
    if not _try_fill_site_search(page, query):
        return [], "search_control_missing"
    page.wait_for_timeout(1800)
    # 若填词后被踢登录，再试一次恢复，仍失败才 need_login
    state, _body = _member_page_state(page, "huixun")
    if state == "need_login":
        if _huixun_try_resume_if_needed(page, timeout_ms) and _try_fill_site_search(page, query):
            page.wait_for_timeout(1800)
            state, _body = _member_page_state(page, "huixun")
        else:
            return None, "need_login"
    if state != "ok":
        return ([] if state == "empty_page" else None), state
    rows = _extract_member_rows(page, "huixun")
    cands = _member_rows_to_candidates(page, rows, query, must, min_score, spec)
    return cands, "ok" if cands else "empty_page"


def _yize_page_state(page) -> tuple[str, str]:
    """易择网登录/会员/空结果状态。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    try:
        body = (page.inner_text("body") or "")[:2500]
    except Exception:
        body = ""
    text = f"{title}\n{body}"

    # 首页/登录页
    if any(
        k in text
        for k in (
            "密码登录",
            "免密登录",
            "立即登录",
            "申请试用",
            "微信扫码登录",
            "账号：",
            "还没有账号",
        )
    ) and not any(k in text for k in ("我的易择", "服务有效期", "系统消息", "收藏夹")):
        return "need_login", body
    if "login" in url and "easybii.com" in url and "p4-" not in url:
        # 根路径登录页
        if "密码" in body and "登录" in body and "我的易择" not in body:
            return "need_login", body

    # 会员/服务到期（信息站常见）
    if any(
        k in text
        for k in (
            "服务已到期",
            "套餐已过期",
            "开通会员",
            "请开通服务",
            "无访问权限",
            "权限不足",
            "续费后使用",
        )
    ):
        return "no_membership", body

    if any(k in body for k in ("暂无数据", "暂无结果", "没有找到", "未找到相关", "无搜索结果", "没有相关数据")):
        return "empty_page", body
    if len(body.strip()) < 30:
        return "empty_page", body
    return "ok", body


def _yize_set_search_type(page, objid: str = "0") -> None:
    """顶栏搜索类型：0=产品信息 1=企业信息 2=信息价。"""
    try:
        # 展开下拉再点类型
        page.locator("#searchText").click(timeout=1500)
        page.wait_for_timeout(200)
    except Exception:
        pass
    try:
        loc = page.locator(f'.searchType[objid="{objid}"]')
        if loc.count() > 0:
            loc.first.click(timeout=2000)
            page.wait_for_timeout(200)
    except Exception:
        pass


def _yize_header_search(page, query: str, *, search_type_objid: str = "0") -> bool:
    """
    使用顶栏全局搜索：input[name=paramLike] + #seachInput。
    返回是否成功触发搜索。
    """
    _yize_set_search_type(page, search_type_objid)
    selectors = (
        'input.search-input[name="paramLike"]',
        'input[name="paramLike"]',
        "input.search-input",
    )
    filled = False
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0 or not loc.is_visible(timeout=800):
                continue
            loc.click(timeout=1500)
            loc.fill("")
            loc.fill((query or "")[:80])
            filled = True
            break
        except Exception:
            continue
    if not filled:
        return False
    try:
        btn = page.locator("#seachInput, p.do-search").first
        if btn.count() > 0 and btn.is_visible(timeout=800):
            btn.click(timeout=2000)
            return True
    except Exception:
        pass
    try:
        page.locator('input[name="paramLike"]').first.press("Enter")
        return True
    except Exception:
        return False


def _yize_info_price_search(page, query: str) -> bool:
    """信息价页本地表单：#info-name-like / #info-specifications-like / #info-search。"""
    try:
        name_box = page.locator("#info-name-like")
        if name_box.count() == 0 or not name_box.first.is_visible(timeout=1000):
            return False
        # 名称框放完整搜索词；规格框尽量塞型号/规格片段
        name_box.first.fill("")
        name_box.first.fill((query or "")[:60])
        spec_part = ""
        # 粗提：数字+字母型号、DN、mm 等
        m = re.search(
            r"([A-Za-z]{1,6}[\-]?\d[\w\-\./]{1,20}|\d+(?:\.\d+)?\s*(?:mm|cm|m|DN|kW|W|V))",
            query or "",
            re.I,
        )
        if m:
            spec_part = m.group(1).strip()
        try:
            spec_box = page.locator("#info-specifications-like")
            if spec_box.count() > 0 and spec_box.first.is_visible(timeout=500):
                spec_box.first.fill(spec_part[:40] if spec_part else "")
        except Exception:
            pass
        # 类型尽量切到「材料价」
        try:
            page.locator("#select-info-type").click(timeout=800)
            page.wait_for_timeout(150)
            page.get_by_text("材料价", exact=True).first.click(timeout=800)
        except Exception:
            pass
        page.locator("#info-search").click(timeout=2000)
        return True
    except Exception:
        return False


def parse_yize_result_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    将易择网表格/列表行规范化为候选中间结构（可单测）。
    支持：产品信息（市场价/工程价）与信息价（含税价/除税价）。
    """
    out: list[dict[str, Any]] = []
    for row in raw_rows or []:
        text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
        name = re.sub(r"\s+", " ", str(row.get("name") or "")).strip()
        if not text and not name:
            continue
        if len(text) < 4:
            continue
        # 跳过表头
        if re.fullmatch(r"(名称|规格型号|单位|含税价|除税价|市场价|工程价|品牌|企业名称).*", text):
            continue
        if not name:
            name = text[:120]

        price = None
        price_text = ""
        tax_mode = "unknown"
        unit = str(row.get("unit") or "")

        # 优先明确价签
        for label, mode in (
            ("除税价", "tax_excl"),
            ("含税价", "tax_incl"),
            ("市场价", "tax_incl"),
            ("工程价", "tax_incl"),
            ("信息价", "unknown"),
            ("单价", "unknown"),
        ):
            m = re.search(
                rf"{label}\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d+)?)",
                text,
            )
            if m:
                price = parse_price(m.group(1))
                price_text = m.group(0)
                tax_mode = mode
                break
        if price is None:
            # 表格单元格价
            cell_price = str(row.get("priceText") or "")
            if cell_price:
                price = parse_price(cell_price)
                price_text = cell_price
                if "除税" in cell_price:
                    tax_mode = "tax_excl"
                elif "含税" in cell_price:
                    tax_mode = "tax_incl"

        if not unit:
            um = re.search(
                r"(?:单位|计价单位)\s*[:：]?\s*(m²|m³|㎡|米|m|个|件|套|台|组|kg|t|吨|项)",
                text,
                re.I,
            )
            if um:
                unit = um.group(1)

        supplier = str(row.get("supplier") or "")
        if not supplier:
            sm = re.search(
                r"([^\s]{2,40}(?:公司|厂|商行|经营部|集团))",
                text,
            )
            if sm:
                supplier = sm.group(1)

        brand = str(row.get("brand") or "")
        href = str(row.get("href") or "")
        out.append(
            {
                "name": name[:180],
                "text": text[:2000],
                "price": price,
                "price_text": price_text[:300],
                "tax_mode": tax_mode,
                "unit": unit,
                "supplier": supplier[:80],
                "brand": brand[:60],
                "href": href,
                "index": row.get("index", len(out)),
            }
        )
    return out


def _yize_extract_dom_rows(page) -> list[dict[str, Any]]:
    """从易择结果页 DOM 抽同行标题/规格/价格，避免跨行串价。"""
    try:
        rows = page.evaluate(
            """() => {
              const out = [];
              const seen = new Set();
              const pushRow = (el, index) => {
                const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!text || text.length < 6 || text.length > 1800) return;
                if (/^(名称|规格型号|单位|含税价|除税价|市场价|工程价)/.test(text) && text.length < 40) return;
                if (/登录|注册|申请试用|密码登录/.test(text) && text.length < 80) return;
                const cells = [...el.querySelectorAll('td')].map(
                  td => (td.innerText || '').replace(/\\s+/g, ' ').trim()
                ).filter(Boolean);
                let name = '';
                let priceText = '';
                let unit = '';
                let brand = '';
                let supplier = '';
                if (cells.length >= 3) {
                  name = cells[0] || '';
                  // 信息价：名称 规格 单位 含税 除税 ...
                  // 产品收藏/列表：产品信息 品牌 企业 市场价 工程价 ...
                  const moneyCells = cells.filter(c => /\\d/.test(c) && /(元|¥|￥|\\d\\.\\d)|\\d{2,}/.test(c));
                  priceText = moneyCells[0] || cells.find(c => /^\\d+(\\.\\d+)?$/.test(c)) || '';
                  unit = cells.find(c => /^(m²|m³|㎡|米|m|个|件|套|台|组|kg|t|吨|项)$/i.test(c)) || '';
                  brand = cells[1] && cells[1].length < 30 ? cells[1] : '';
                  supplier = cells.find(c => /公司|厂|商行|集团/.test(c)) || '';
                  // 若首列太短、第二列像规格，拼进 name 文本
                  if (cells[1] && /[0-9A-Za-z]/.test(cells[1]) && cells[1].length < 80) {
                    name = `${name} ${cells[1]}`.trim();
                  }
                } else {
                  const titleEl = el.querySelector(
                    '[class*="name"], [class*="title"], [class*="product"], a'
                  );
                  name = (titleEl?.getAttribute('title') || titleEl?.innerText || '').replace(/\\s+/g, ' ').trim();
                  const priceEl = el.querySelector('[class*="price"], [class*="market"], [class*="amount"]');
                  priceText = (priceEl?.innerText || '').replace(/\\s+/g, ' ').trim();
                }
                if (!name) name = text.slice(0, 120);
                const a = el.querySelector('a[href]');
                const href = a?.href || '';
                const key = `${name}|${priceText}|${text.slice(0, 60)}`;
                if (seen.has(key)) return;
                seen.add(key);
                out.push({index, href, name, text, priceText, unit, brand, supplier});
              };

              const selectors = [
                'table tr',
                '.fav-table tr',
                '.man-table tr',
                '[class*="list-item"]',
                '[class*="product-item"]',
                '[class*="price-item"]',
                '.item',
              ];
              let idx = 0;
              for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) {
                  // 跳过嵌套过深重复
                  if (el.closest('thead')) continue;
                  pushRow(el, idx++);
                  if (out.length >= 80) return out;
                }
                if (out.length >= 8) break;
              }
              return out;
            }"""
        )
        return list(rows or [])
    except Exception:
        return []


def _yize_rows_to_candidates(
    page,
    rows: list[dict[str, Any]],
    query: str,
    must: list[str],
    min_score: int,
    spec: PlatformSpec,
) -> list[dict[str, Any]]:
    parsed = parse_yize_result_rows(rows)
    current_url = page.url or spec.search_url_template
    out: list[dict[str, Any]] = []
    for row in parsed:
        name = row["name"]
        text = row["text"]
        sc = score_title(f"{name} {text}", must)
        # 服务端已按关键词筛；正式精度交给 strict_name_spec_match
        price = row.get("price")
        href = row.get("href") or ""
        if href and spec.item_link_contains and spec.item_link_contains not in href:
            href = ""
        cand = {
            "title": name[:160],
            "price_tax": price or 0.01,
            "url": href or current_url,
            "sku": f"yize:{row.get('index', '')}:{_norm_row_key(name, text)}",
            "score": max(sc, 1),
            "platform": spec.id,
            "supplier": row.get("supplier") or "",
            "unit": row.get("unit") or "",
            "tax_mode": row.get("tax_mode") or "unknown",
            "price_text": row.get("price_text") or "",
            "price_context": text[:500],
            "price_source": "platform_result_row" if price else "missing",
            "spec_seen": text[:1200],
            "brand": row.get("brand") or "",
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


def _search_yize(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    """
    易择网（easybii.com）：
    1) 进入信息价首页（带全局搜索）
    2) 优先「产品信息」搜索（市场价/工程价）
    3) 不足时退回本页「信息价」名称/规格搜索（含税/除税）
    """
    landing = spec.search_url_template or "https://www.easybii.com/P4-3-info-price-home.html"
    current = (page.url or "").lower()
    if "easybii.com" not in current or "login" in current:
        page.goto(landing, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1800)
    elif "p4-" not in current:
        # 可能停在首页登录后仍是 /，跳到搜索页
        page.goto(landing, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1500)

    state, _body = _yize_page_state(page)
    if state not in ("ok", "empty_page"):
        return None, state

    # —— 通道 A：顶栏产品信息 ——
    if _yize_header_search(page, query, search_type_objid="0"):
        page.wait_for_timeout(2200)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(600)
        state, _body = _yize_page_state(page)
        if state not in ("ok", "empty_page"):
            return ([] if state == "empty_page" else None), state
        rows = _yize_extract_dom_rows(page)
        cands = _yize_rows_to_candidates(page, rows, query, must, min_score, spec)
        priced = [c for c in cands if c.get("price_tax") and c.get("price_tax") > 0.01]
        if priced:
            return priced, "ok"
        if cands:
            return cands, "ok"

    # —— 通道 B：信息价本地表单 ——
    # 若已跳离信息价页，先回去
    try:
        if "info-price" not in (page.url or "").lower():
            page.goto(landing, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1200)
    except Exception:
        pass

    if _yize_info_price_search(page, query):
        page.wait_for_timeout(2200)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        state, _body = _yize_page_state(page)
        if state not in ("ok", "empty_page"):
            return ([] if state == "empty_page" else None), state
        rows = _yize_extract_dom_rows(page)
        cands = _yize_rows_to_candidates(page, rows, query, must, min_score, spec)
        priced = [c for c in cands if c.get("price_tax") and c.get("price_tax") > 0.01]
        if priced:
            return priced, "ok"
        return cands, "ok" if cands else "empty_page"

    # —— 通道 C：顶栏信息价类型 ——
    if _yize_header_search(page, query, search_type_objid="2"):
        page.wait_for_timeout(2200)
        rows = _yize_extract_dom_rows(page)
        cands = _yize_rows_to_candidates(page, rows, query, must, min_score, spec)
        priced = [c for c in cands if c.get("price_tax") and c.get("price_tax") > 0.01]
        if priced:
            return priced, "ok"
        return cands, "ok" if cands else "empty_page"

    return [], "search_control_missing"


def _zaojiatong_page_state(page) -> tuple[str, str]:
    """造价通登录/会员/空结果状态（仅用于「整页 goto 后」的 DOM 判断）。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    try:
        body = (page.inner_text("body") or "")[:2500]
    except Exception:
        body = ""
    text = f"{title}\n{body}"

    if "member.zjtcn.com" in url and ("login" in url or "登录" in title):
        return "need_login", body
    if "/common/login" in url or ("passport" in url and "zjtcn" in url):
        return "need_login", body
    if title.strip().startswith("会员登录"):
        return "need_login", body
    if any(
        k in text
        for k in (
            "会员登录",
            "请输入手机号/账号",
            "请输入密码",
            "扫码登录",
            "登录后查看",
            "请先登录",
            "账号密码登录",
        )
    ) and not any(k in text for k in ("退出登录", "我的造价通", "退出", "会员中心", "个人中心")):
        # 列表页未登录时价列也显示「查看价格」，不能单凭它判登录；硬登录页才 need_login
        if "请输入密码" in text or "会员登录" in title or "common/login" in url:
            return "need_login", body

    if any(
        k in text
        for k in (
            "服务已到期",
            "套餐已过期",
            "开通会员",
            "请开通服务",
            "无访问权限",
            "权限不足",
            "续费后使用",
            "开通VIP",
            "成为会员后",
        )
    ):
        return "no_membership", body

    if any(k in body for k in ("暂无数据", "暂无结果", "没有找到", "未找到相关", "无搜索结果", "没有相关数据")):
        return "empty_page", body
    if len(body.strip()) < 40:
        return "empty_page", body
    return "ok", body


def _zaojiatong_has_auth_cookies(page) -> list[str]:
    """是否有登录态 Cookie（token/userId 等；jsid 不算）。"""
    try:
        from .login_gate import auth_cookie_hits

        return list(auth_cookie_hits(page, "zaojiatong") or [])
    except Exception:
        return []


def _zaojiatong_list_url(spec: PlatformSpec, query: str) -> str:
    q = (query or "").strip()[:40]
    if "{query}" in (spec.search_url_template or ""):
        return spec.search_url_template.format(query=quote(q))
    return f"https://gd.zjtcn.com/shichangjia/list/c_t_d_k_{quote(q)}.html"


def _zaojiatong_fetch_list_html(page, query: str, spec: PlatformSpec, timeout_ms: int) -> str:
    """
    用不执行 JS 的 HTTP 请求抓市场价 SSR HTML。

    实测：page.goto 后 SPA 约 0.3s 内会踢到会员登录页，列表 DOM 被清空；
    但同一 URL 的 SSR 响应始终含材料行（价格列可能是「查看价格」）。
    用 context.request 可避开 SPA 拦截，且自动带上浏览器 Cookie。
    """
    url = _zaojiatong_list_url(spec, query)
    # 优先走 Playwright 上下文请求（共享登录 Cookie）
    for getter in (
        lambda: page.context.request.get(url, timeout=timeout_ms),
        lambda: page.request.get(url, timeout=timeout_ms),
    ):
        try:
            resp = getter()
            if resp is None:
                continue
            status = int(getattr(resp, "status", 0) or 0)
            ok = bool(getattr(resp, "ok", False)) or status == 200
            if not ok:
                continue
            text = resp.text()
            if text and len(text) > 500 and (
                "material-title" in text or "shichangjia/info_" in text or "原始名称" in text
            ):
                return text
        except Exception:
            continue
    # 回退：短 goto + 立即抽 outerHTML（在 SPA 跳转前）
    try:
        page.goto(url, wait_until="commit", timeout=timeout_ms)
        # 尽快取 HTML，勿等 networkidle
        html = page.content()
        if html and "material-title" in html:
            return html
        page.wait_for_timeout(200)
        html = page.content()
        if html:
            return html
    except Exception:
        pass
    return ""


def parse_zaojiatong_ssr_html(html: str) -> list[dict[str, Any]]:
    """
    从市场价列表 SSR HTML 抽行（可单测、无需浏览器 DOM）。
    未登录时价格多为「查看价格」/占位，price 为 None。
    """
    if not html:
        return []
    import html as html_lib

    out: list[dict[str, Any]] = []
    # 行块
    blocks = re.findall(r'<tr class="flex flex-row"[^>]*>(.*?)</tr>', html, re.S | re.I)
    if not blocks:
        # 兼容无 class 的 tr
        blocks = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
    for index, block in enumerate(blocks):
        if "材料名称及规格型号" in block and "除税市场价" in block:
            continue
        if "material-title" not in block and "/shichangjia/info_" not in block:
            continue
        href_m = re.search(r'href="(/shichangjia/info_[^"]+)"', block)
        href = html_lib.unescape(href_m.group(1)) if href_m else ""
        title_m = re.search(
            r'class="material-title[^"]*"[^>]*>(.*?)</a>', block, re.S | re.I
        )
        title = ""
        if title_m:
            title = re.sub(r"<[^>]+>", "", title_m.group(1))
            title = html_lib.unescape(title).strip()
        name_m = re.search(r"原始名称：\s*</span>\s*<span[^>]*>([^<]+)", block)
        if not name_m:
            name_m = re.search(r"原始名称：\s*([^<\s][^<]{0,80})", block)
        name = (name_m.group(1).strip() if name_m else title) or title
        # 规格：优先标签；否则拼「品种/牌号/直径」等片段
        spec = ""
        sm = re.search(r"规格型号：\s*</span>\s*<span[^>]*>([^<]+)", block)
        if sm:
            spec = sm.group(1).strip()
        texts = [t.strip() for t in re.findall(r">([^<>]{1,80})<", block) if t.strip()]
        if not spec:
            bits = [
                t
                for t in texts
                if any(
                    k in t
                    for k in (
                        "品种",
                        "牌号",
                        "直径",
                        "规格",
                        "DN",
                        "Φ",
                        "Ф",
                        "mm",
                        "×",
                        "x",
                    )
                )
                and "原始名称" not in t
                and "规格型号" not in t
            ]
            spec = " ".join(bits[:6])
        supplier = ""
        sup = re.search(r"供应商名称：\s*</span>\s*<span[^>]*>([^<]+)", block)
        if sup:
            supplier = sup.group(1).strip()
        else:
            for t in texts:
                if re.search(r"(公司|厂|商行|经营部|集团)$", t) and len(t) >= 4:
                    supplier = t
                    break
        unit = ""
        for t in texts:
            if re.fullmatch(r"(m²|m³|㎡|米|m|个|件|套|台|组|kg|t|吨|项|张|根)", t, re.I):
                unit = t
                break
        # 价格：跳过「查看价格」
        price = None
        price_text = ""
        tax_mode = "unknown"
        plain = re.sub(r"<[^>]+>", " ", block)
        plain = re.sub(r"\s+", " ", plain)
        for label, mode in (
            ("除税市场价", "tax_excl"),
            ("含税市场价", "tax_incl"),
            ("除税建议价", "tax_excl"),
            ("含税建议价", "tax_incl"),
            ("除税价", "tax_excl"),
            ("含税价", "tax_incl"),
        ):
            m = re.search(rf"{label}\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d+)?)", plain)
            if m:
                price = parse_price(m.group(1))
                price_text = m.group(0)
                tax_mode = mode
                break
        if price is None:
            # NUXT 压缩字段 noTaxPrice:123.4
            m = re.search(r"noTaxPrice:(\d+(?:\.\d+)?)", block)
            if m:
                price = parse_price(m.group(1))
                price_text = m.group(0)
                tax_mode = "tax_excl"
            else:
                m = re.search(r"taxPrice:(\d+(?:\.\d+)?)", block)
                if m:
                    price = parse_price(m.group(1))
                    price_text = m.group(0)
                    tax_mode = "tax_incl"
        if not name and not title:
            continue
        out.append(
            {
                "index": index,
                "name": (name or title)[:180],
                "text": plain[:2000],
                "spec": spec[:400],
                "price": price,
                "price_text": price_text[:300],
                "tax_mode": tax_mode,
                "unit": unit,
                "supplier": supplier[:80],
                "brand": "",
                "href": href,
            }
        )
    return out


def parse_zaojiatong_result_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    将造价通市场价表格行规范化（可单测）。
    未登录时价格常为「查看价格」——price 为 None，由上层标 needs_detail_price。
    """
    out: list[dict[str, Any]] = []
    for row in raw_rows or []:
        text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
        name = re.sub(r"\s+", " ", str(row.get("name") or "")).strip()
        if not text and not name:
            continue
        if len(text) < 4:
            continue
        # 表头
        if re.search(r"材料名称及规格型号|除税市场价|除税建议价", text) and "原始名称" not in text:
            continue
        if re.fullmatch(r"(名称|规格型号|单位|市场价|建议价|税率|供应商|操作).*", name or text):
            continue

        # 优先「原始名称」，截到下一字段标签
        m_name = re.search(
            r"原始名称\s*[:：]\s*(.+?)(?=\s*(?:规格型号|查看价格|除税|含税|税率|单位|供应商|档次|品牌|$))",
            text,
        )
        if m_name:
            name = m_name.group(1).strip()
        if not name:
            name = text[:120]

        # 规格
        spec_seen = str(row.get("spec") or "")
        m_spec = re.search(
            r"规格型号\s*[:：]\s*(.+?)(?=\s*(?:查看价格|原始名称|除税|含税|税率|单位|供应商|档次|品牌|$))",
            text,
        )
        if m_spec:
            spec_seen = m_spec.group(1).strip()
        if not spec_seen:
            # 行内常有「名称 + 规格」两段
            pass

        price = None
        price_text = ""
        tax_mode = "unknown"
        for label, mode in (
            ("除税市场价", "tax_excl"),
            ("含税市场价", "tax_incl"),
            ("除税建议价", "tax_excl"),
            ("含税建议价", "tax_incl"),
            ("除税价", "tax_excl"),
            ("含税价", "tax_incl"),
            ("市场价", "tax_incl"),
            ("建议价", "tax_incl"),
        ):
            m = re.search(rf"{label}\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d+)?)", text)
            if m:
                price = parse_price(m.group(1))
                price_text = m.group(0)
                tax_mode = mode
                break
        if price is None:
            cell = str(row.get("priceText") or "")
            if cell and "查看" not in cell and re.search(r"\d", cell):
                price = parse_price(cell)
                price_text = cell[:80]
                if "除税" in cell:
                    tax_mode = "tax_excl"
                elif "含税" in cell:
                    tax_mode = "tax_incl"

        unit = str(row.get("unit") or "")
        if not unit:
            um = re.search(
                r"(?:单位|计价单位)\s*[:：]?\s*(m²|m³|㎡|米|m|个|件|套|台|组|kg|t|吨|项|张|根)",
                text,
                re.I,
            )
            if um:
                unit = um.group(1)
            else:
                # 税率后常接单位：13% 个 / 13% t
                um2 = re.search(r"\d+%\s*(m²|m³|㎡|米|m|个|件|套|台|组|kg|t|吨|项|张|根)\b", text, re.I)
                if um2:
                    unit = um2.group(1)

        supplier = str(row.get("supplier") or "")
        if not supplier:
            sm = re.search(r"供应商名称\s*[:：]\s*([^\s]{2,60})", text)
            if sm:
                supplier = sm.group(1).strip()
            else:
                sm = re.search(r"([^\s]{2,40}(?:公司|厂|商行|经营部|集团|有限公司))", text)
                if sm:
                    supplier = sm.group(1)

        brand = str(row.get("brand") or "")
        href = str(row.get("href") or "")
        combined = f"{name} {spec_seen}".strip()
        out.append(
            {
                "name": name[:180],
                "text": (combined + " " + text)[:2000],
                "spec": spec_seen[:400],
                "price": price,
                "price_text": price_text[:300],
                "tax_mode": tax_mode,
                "unit": unit,
                "supplier": supplier[:80],
                "brand": brand[:60],
                "href": href,
                "index": row.get("index", len(out)),
            }
        )
    return out


def _zaojiatong_extract_dom_rows(page) -> list[dict[str, Any]]:
    """从造价通市场价表 tbody 抽同行材料名/规格/价/供应商。"""
    try:
        rows = page.evaluate(
            """() => {
              const out = [];
              const seen = new Set();
              const rows = document.querySelectorAll(
                'tbody tr, tr.flex.flex-row, .el-table__row, [class*="table-row"]'
              );
              for (const [index, row] of rows.entries()) {
                const text = (row.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!text || text.length < 8 || text.length > 2200) continue;
                if (/材料名称及规格型号/.test(text) && /除税市场价|操作/.test(text)) continue;
                if (/登录|注册|网站导航/.test(text) && text.length < 80) continue;

                const a = row.querySelector('a.material-title, a[href*="shichangjia/info_"], a[href*="/info_"]');
                let href = a?.href || '';
                let name = (a?.innerText || '').replace(/\\s+/g, ' ').trim();
                const rawName = text.match(/原始名称\\s*[:：]\\s*([^\\s][^|]{1,80})/);
                if (rawName) name = rawName[1].trim();
                if (!name) {
                  const titleEl = row.querySelector('[class*="material-title"], [class*="name"]');
                  name = (titleEl?.innerText || '').replace(/\\s+/g, ' ').trim();
                }
                if (!name) continue;

                let spec = '';
                const sm = text.match(/规格型号\\s*[:：]\\s*(.+?)(?=\\s*(?:查看价格|原始名称|税率|单位|供应商|$))/);
                if (sm) spec = sm[1].trim();

                // 价格列：跳过「查看价格」占位
                let priceText = '';
                const priceCells = row.querySelectorAll(
                  '[class*="price"], .text-orange-color, td'
                );
                for (const cell of priceCells) {
                  const t = (cell.innerText || '').replace(/\\s+/g, ' ').trim();
                  if (!t || /查看价格|查看联系|查看报价/.test(t)) continue;
                  if (/\\d/.test(t) && t.length < 40) {
                    priceText = t;
                    break;
                  }
                }
                if (!priceText) {
                  const m = text.match(/(?:除税市场价|含税市场价|市场价|建议价)\\s*[:：]?\\s*[¥￥]?\\s*(\\d+(?:\\.\\d+)?)/);
                  if (m) priceText = m[0];
                }

                let unit = '';
                const um = text.match(/(\\d+%\\s*)(m²|m³|㎡|米|m|个|件|套|台|组|kg|t|吨|项|张|根)\\b/i);
                if (um) unit = um[2];

                let supplier = '';
                const sup = text.match(/供应商名称\\s*[:：]\\s*([^\\s]{2,60})/);
                if (sup) supplier = sup[1];
                else {
                  const sc = text.match(/([\\u4e00-\\u9fffA-Za-z0-9（）()]{2,40}(?:公司|厂|商行|经营部|集团))/);
                  if (sc) supplier = sc[1];
                }

                let brand = '';
                // 档次/品牌列常是短中文词
                const brandCand = Array.from(row.querySelectorAll('td')).map(
                  td => (td.innerText || '').replace(/\\s+/g, ' ').trim()
                ).filter(t => t && t.length <= 12 && !/查看|%|\\d{4}-\\d{2}/.test(t));

                const key = `${href}|${name}|${spec.slice(0, 40)}|${priceText}`;
                if (seen.has(key)) continue;
                seen.add(key);
                out.push({
                  index,
                  href,
                  name: name.slice(0, 180),
                  text: text.slice(0, 1800),
                  spec: spec.slice(0, 400),
                  priceText: priceText.slice(0, 80),
                  unit,
                  supplier: supplier.slice(0, 80),
                  brand: (brand || brandCand[0] || '').slice(0, 40),
                });
                if (out.length >= 60) break;
              }
              return out;
            }"""
        )
        return list(rows or [])
    except Exception:
        return []


def _zaojiatong_rows_to_candidates(
    page,
    rows: list[dict[str, Any]],
    query: str,
    must: list[str],
    min_score: int,
    spec: PlatformSpec,
) -> list[dict[str, Any]]:
    parsed = parse_zaojiatong_result_rows(rows)
    current_url = page.url or spec.search_url_template
    out: list[dict[str, Any]] = []
    for row in parsed:
        name = str(row.get("name") or "")
        text = str(row.get("text") or "")
        spec_seen = str(row.get("spec") or "")
        blob = f"{name} {spec_seen} {text}"
        sc = score_title(blob, must)
        href = str(row.get("href") or "")
        if href and href.startswith("/"):
            # 相对路径补全域名
            base = "https://gd.zjtcn.com"
            try:
                from urllib.parse import urlparse

                p = urlparse(current_url)
                if p.scheme and p.netloc:
                    base = f"{p.scheme}://{p.netloc}"
            except Exception:
                pass
            href = base + href
        if href and spec.item_link_contains and spec.item_link_contains not in href:
            # 仍保留，造价通分站域名可能变化
            pass
        price = row.get("price")
        cand: dict[str, Any] = {
            "title": name[:180] if name else blob[:160],
            "price_tax": float(price) if price else 0.01,
            "url": href or current_url,
            "sku": f"zaojiatong:{row.get('index', '')}:{_norm_row_key(name, text)}",
            "score": max(sc, 1),
            "platform": spec.id,
            "supplier": str(row.get("supplier") or ""),
            "unit": str(row.get("unit") or ""),
            "tax_mode": str(row.get("tax_mode") or "unknown"),
            "price_text": str(row.get("price_text") or "")[:300],
            "price_context": text[:500],
            "spec_seen": (spec_seen or text)[:1200],
            "brand": str(row.get("brand") or ""),
        }
        if price:
            cand.update(
                price_source="platform_result_row",
                inline_detail=True,
                detail_text=text[:4000],
                sku_scope="exact_result_row",
            )
        else:
            cand["price_source"] = "missing"
            cand["needs_detail_price"] = True
        out.append(cand)
    out.sort(key=lambda x: (-x.get("score", 0), x.get("price_tax", 1e18)))
    return out[:40]


def _zaojiatong_on_market_page(page) -> bool:
    """是否已在分站市场价/信息价页（非会员登录页）。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "member.zjtcn.com" in url or "login" in url:
        return False
    if "zjtcn.com" not in url:
        return False
    return any(x in url for x in ("shichangjia", "xinxijia", "ration", "facx"))


def _zaojiatong_fill_search(page, query: str) -> bool:
    """在已打开的市场价页用搜索框改词，避免每条材料整页 goto 新链接触发再登录。"""
    q = (query or "").strip()[:40]
    if not q:
        return False
    # 优先顶栏关键词，其次列表上方「材料名称搜索」
    for sel in (
        "#indexKey2",
        'input[placeholder*="关键词"]',
        'input[placeholder*="快速查找"]',
        'input[placeholder*="材料名称"]',
        ".search-keywords input",
        ".cailiao-keywords input",
        "input.el-input__inner",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() == 0 or not loc.is_visible(timeout=600):
                continue
            loc.click(timeout=1200)
            try:
                loc.fill("")
            except Exception:
                loc.press("Control+a")
                loc.press("Backspace")
            loc.fill(q)
            submitted = False
            for bsel in (
                ".search-btn",
                ".module-search .search-btn",
                "div.search-btn",
                ".search-icon",
                "button:has-text('搜索')",
            ):
                try:
                    btn = page.locator(bsel).first
                    if btn.count() > 0 and btn.is_visible(timeout=400):
                        btn.click(timeout=1500)
                        submitted = True
                        break
                except Exception:
                    continue
            if not submitted:
                loc.press("Enter")
            # 短等结果刷新即可，不要 2s+（SPA 可能跳登录）
            page.wait_for_timeout(1200)
            return True
        except Exception:
            continue
    return False


def _zaojiatong_try_reveal_list_prices(page, max_clicks: int = 6) -> None:
    """
    列表价列「查看价格」：登录后点击可就地展开数字，减少逐条打开详情链接。
    若点击触发登录墙则立即停止。
    """
    try:
        nodes = page.locator("text=查看价格")
        n = min(int(nodes.count() or 0), max_clicks)
    except Exception:
        return
    for i in range(n):
        try:
            el = nodes.nth(i)
            if not el.is_visible(timeout=400):
                continue
            el.click(timeout=1200)
            page.wait_for_timeout(600)
            state, _ = _zaojiatong_page_state(page)
            if state == "need_login":
                return
            # 点开后可能变成弹层，Esc 关掉以免挡下一行
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        except Exception:
            continue


def _zaojiatong_extract_after_inpage_search(
    page, query: str, must: list[str], min_score: int, spec: PlatformSpec
) -> list[dict[str, Any]]:
    """同页搜完立刻抽结果（优先 DOM，失败再用当前 HTML SSR 解析）。"""
    page.wait_for_timeout(900)
    rows = _zaojiatong_extract_dom_rows(page)
    if rows:
        cands = _zaojiatong_rows_to_candidates(page, rows, query, must, min_score, spec)
        if cands:
            return cands
    try:
        html = page.content()
        parsed = parse_zaojiatong_ssr_html(html)
        if parsed:
            return _zaojiatong_rows_to_candidates(page, parsed, query, must, min_score, spec)
    except Exception:
        pass
    return []


def _search_zaojiatong(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    """
    造价通专用：委托 adapters.zaojiatong（纯 HTTP SSR，禁止 page.goto 搜价）。

    规则见 adapters/zaojiatong.py 文件头 R1–R7。
    """
    from .adapters import zaojiatong as zjt

    # 可选：装上路由守卫，防止其它逻辑误把页面带去登录
    try:
        zjt.install_browser_guards(page)
    except Exception:
        pass
    return zjt.search(page, query, must, timeout_ms, min_score, spec)


# 兼容旧测试 / 外部引用：SSR 解析入口指向专用适配器
def parse_zaojiatong_ssr_html(html: str) -> list[dict[str, Any]]:  # type: ignore[no-redef]
    from .adapters import zaojiatong as zjt

    return zjt.parse_list_html(html)


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
    if not cands and goods and spec.id in ("lingcai", "huixun", "guangcai", "yize", "zaojiatong"):
        loose = []
        for g in goods:
            href = g.get("href") or ""
            name = g.get("name") or ""
            if not href or not name or len(name) < 4:
                continue
            price = parse_price(g.get("priceText"))
            sc = score_title(name + " " + href, must)
            if sc < 1 and must:
                # 造价站列表本身已按关键词过滤：名称 ≥2 字有链接就放行，
                # 正式精度交给 strict_name_spec_match（避免「人能搜到程序 0 条」）
                name_ns = re.sub(r"\s+", "", name)
                if len(name_ns) < 2:
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
    "yize": _search_yize,
    "zaojiatong": _search_zaojiatong,
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

    返回值保持 list[dict] 以兼容现有 inquiry。
    需要 CandidateRecord 时请用 candidate_adapt.search_as_records。
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
    elif pid == "yize":
        handler_name = "yize"
    elif pid == "zaojiatong":
        handler_name = "zaojiatong"
    try:
        fn = HANDLERS[handler_name]
        result, status = fn(page, query, must, timeout_ms, min_score, spec)
        if result is None:
            return [], status
        # specialized handlers may return single best historically — normalize to list
        if isinstance(result, dict):
            cands = [result]
        else:
            cands = list(result)
        # Phase2：为每条候选补 platform 字段（不改其它结构）
        for c in cands:
            if isinstance(c, dict) and not c.get("platform"):
                c["platform"] = pid
        return cands, status
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
