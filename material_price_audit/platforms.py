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
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .scraper import parse_price, score_title


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
        search_url_template="https://services.iccchina.com/iccHome",
        handler="generic",
        item_link_contains="iccchina.com",
        item_link_selector='a[href*="iccchina.com"]',
        detail_price_selectors=[".price", "[class*='price']", "[class*='Price']"],
        same_login_as="",
        notes="登录 https://services.iccchina.com/login · 首页 iccHome · RCC瑞达恒，非广材网",
    ),
    "lingcai": PlatformSpec(
        id="lingcai",
        name="领材网",
        # 用户核实登录/用户中心：/userInfo/index.html（不是 lcIndex 首页）
        login_url="https://www.hylcw.cn/userInfo/index.html",
        search_url_template="https://www.hylcw.cn/lcIndex.html?keyword={query}",
        handler="generic",
        item_link_contains="hylcw.cn",
        item_link_selector='a[href*="hylcw.cn"]',
        detail_price_selectors=[".price", "[class*='price']", "[class*='Price']"],
        requires_config=False,
        notes="登录 https://www.hylcw.cn/userInfo/index.html · 首页 lcIndex · 域名 hylcw.cn · 非广材网",
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
        notes="实测可打开 hangqing.gldjc.com · 钢材等行情",
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
        notes="实测可打开 xunjia.gldjc.com · 人工询价入口",
        require_login_hint=True,
    ),
    "jcnet": PlatformSpec(
        id="jcnet",
        name="建材在线",
        login_url="https://www.jc.net.cn/",
        search_url_template="https://www.jc.net.cn/",
        handler="generic",
        item_link_contains="jc.net.cn",
        item_link_selector='a[href*="jc.net.cn"]',
        detail_price_selectors=["[class*='price']", ".price"],
        notes="实测标题含「建材在线-建材信息价格服务」· jc.net.cn",
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
        "iccchina": "huixun",
        "rcc": "huixun",
        "瑞达恒": "huixun",
        "领材": "lingcai",
        "领材网": "lingcai",
        "领财网": "lingcai",
        "lingcaiwang": "lingcai",
        "hylcw": "lingcai",
        "广材行情": "gldjc_hangqing",
        "广材询价": "gldjc_xunjia",
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


def resolve_enabled_platforms(
    cfg: dict | None,
    cli_platforms: str | None,
    *,
    allow_default: bool = False,
) -> list[str]:
    """
    Priority:
      1) CLI --platforms a,b,c
      2) config platforms.enabled list
      3) config platforms: [jd, 1688]  (legacy list form)
      4) 默认空列表 —— 必须由用户选择，禁止偷偷全站登录
         allow_default=True 时仅给 platforms 列表展示用，不给 run/scrape 当默认
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
    if allow_default:
        return ["guangcai", "huixun", "lingcai", "jd", "1688"]
    return []


# 终端/网页勾选时展示的推荐顺序（不自动启用）
SELECTABLE_PLATFORM_IDS = [
    "guangcai",
    "huixun",
    "lingcai",
    "jd",
    "1688",
    "zkh",
    "taobao",
    "tmall",
    "jcnet",
    "gldjc_hangqing",
    "gldjc_xunjia",
    "suning",
    "mysteel",
]


def save_platforms_selected(path: Path, platform_ids: list[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(normalize_platform_id(p) for p in platform_ids if str(p).strip())
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def pick_platforms_interactive(
    registry: dict[str, PlatformSpec] | None = None,
    *,
    preselected: list[str] | None = None,
    prefer_dialog: bool = True,
) -> list[str]:
    """
    优先弹窗勾选；弹窗失败再退回终端输入编号。
    """
    reg = registry or load_platform_registry({})
    if prefer_dialog:
        try:
            from .platform_dialog import can_show_dialog, pick_platforms_dialog

            if can_show_dialog():
                print("[platforms] 打开选择平台弹窗…")
                picked = pick_platforms_dialog(reg, preselected=preselected)
                return picked  # [] = 用户取消
            print("[platforms] 当前环境无法弹窗，改用终端选择")
        except Exception as e:
            print(f"[platforms] 弹窗失败，改用终端选择: {e}")

    choices: list[tuple[str, PlatformSpec]] = []
    for pid in SELECTABLE_PLATFORM_IDS:
        if pid in reg:
            choices.append((pid, reg[pid]))
    for pid, spec in reg.items():
        if pid not in {c[0] for c in choices}:
            choices.append((pid, spec))

    print("")
    print("========== 选择要比价的平台（必选，只登录你勾的） ==========")
    print("输入编号（逗号分隔）如 1,4,5   或 id 如 guangcai,jd")
    print("没广材会员就不要选 guangcai；只选你能用的站。")
    print("-" * 60)
    for i, (pid, spec) in enumerate(choices, 1):
        mark = "*" if preselected and pid in preselected else " "
        print(f" {mark}{i:>2}. {spec.name:<10}  {pid:<14}  {spec.login_url}")
    print("-" * 60)
    try:
        raw = input("你的选择 > ").strip()
    except EOFError:
        return []
    if not raw:
        return []

    selected: list[str] = []
    for part in raw.replace("，", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            idx = int(part)
            if 1 <= idx <= len(choices):
                selected.append(choices[idx - 1][0])
            continue
        pid = normalize_platform_id(part)
        if pid in reg:
            selected.append(pid)
        else:
            print(f"  [忽略未知] {part}")
    seen = set()
    out = []
    for p in selected:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


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
    link_hint = spec.item_link_contains or "gldjc.com"
    cands = _filter_cands(goods, must, min_score, spec.id, link_hint)
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


def _search_generic(page, query: str, must: list[str], timeout_ms: int, min_score: int, spec: PlatformSpec):
    """
    通用搜索：慧讯(iccchina)、领材(hylcw) 等非广材站。
    - 模板含 {query} → 直接拼 URL
    - 否则打开入口页，尝试页面内搜索框
    - 无会员/权限 → no_membership（跳过整站）
    """
    if not spec.search_url_template:
        return None, "bad_config"

    if "{query}" in spec.search_url_template:
        url = spec.search_url_template.format(query=quote(query))
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2500)
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

    if _page_membership_blocked(page):
        return None, "no_membership"
    title = page.title() or ""
    if "登录" in title and spec.require_login_hint and "慧讯" not in title and "领材" not in title:
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
    elif pid in ("huixun", "lingcai"):
        handler_name = "generic"
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


def pick_best_candidate(cands: list[dict]) -> dict | None:
    if not cands:
        return None
    # highest score, then lowest price
    return sorted(cands, key=lambda x: (-x.get("score", 0), x.get("price_tax", 1e18)))[0]
