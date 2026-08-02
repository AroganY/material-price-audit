"""
造价通（zjtcn.com）专用规则 —— 与广材/慧讯等完全独立。

═══════════════════════════════════════════════════════════════
  为什么单独做一套？
═══════════════════════════════════════════════════════════════
造价通市场价是 Nuxt SSR + 前端 SPA：
  1) 列表 URL 的 **HTTP SSR 永远 200**，未登录也能拿到材料名/规格/供应商；
  2) 浏览器 **page.goto 后约 0.3s**，SPA 把页面踢到 member…/login；
  3) 数字价在「查看价格」后，需会员 Cookie；SSR 里常是占位；
  4) 同一账号多端会弹「正在登录使用中 → 继续登录」。

旧逻辑每条材料 page.goto / 填框等 SPA → 必撞登录 / 互踢。
本适配器 **禁止** 用浏览器导航搜价，**只走 HTTP SSR**。

═══════════════════════════════════════════════════════════════
  专用规则（硬约束）
═══════════════════════════════════════════════════════════════
R1  搜价：只用 context.request.get(列表URL)，禁止 page.goto 带关键词的新链接。
R2  详情：只用 request.get(info_URL)，禁止把主页面跳到详情再跳回来。
R3  登录：整个任务最多要求一次（由询价层 session_login_done 管）；
      搜价结果 **永不** 因 SPA 踢登录返回 need_login（有 SSR 行就 ok）。
R4  无会员价：返回候选 + needs_detail_price / 无数字价，由匹配层判「没查到」，
      不循环弹登录。
R5  互踢弹窗：登录阶段自动点「继续登录」；搜价阶段不碰登录页。
R6  默认分站：gd.zjtcn.com（广东）；可用 PlatformSpec.search_url_template 覆盖。
R7  浏览器页面仅用于：用户手工登录一次；登录后 Cookie 自动带进 request。

═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from ..scraper import parse_price, score_title

# 默认广东分站
DEFAULT_HOST = "https://gd.zjtcn.com"
LIST_PATH_TMPL = "/shichangjia/list/c_t_d_k_{query}.html"
LANDING_PATH = "/shichangjia/list/c_t_d_k.html"
LOGIN_URL = (
    "https://member.zjtcn.com/common/login.html"
    f"?url={quote(DEFAULT_HOST + LANDING_PATH, safe='')}"
)

_AUTH_COOKIE_NAMES = (
    "token",
    "userid",
    "user_id",
    "user_uid",
    "username",
    "userlogincookie",
    "remuser",
    "employeeid",
    "tenantid",
)

# 造价通锁价/占位：会员未解锁时常写 -1000；本适配器旧版用 0.01 占位 —— 一律不算有效价
_INVALID_PRICE_SENTINELS = {-1000.0, -1.0, 0.0, 0.01}


def is_valid_price(value: Any) -> bool:
    """只接受真实可核价数字；0 / 0.01 / -1000 / 空 一律无效。"""
    if value is None or value is False:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if v != v:  # NaN
        return False
    if v in _INVALID_PRICE_SENTINELS:
        return False
    # 与 scraper.parse_price 一致：排除过小/过大
    return 0.05 < v < 5_000_000


def coerce_valid_price(value: Any) -> float | None:
    """转成有效价，否则 None（绝不返回 0 当占位）。"""
    if value is None or value is False:
        return None
    try:
        if isinstance(value, str):
            s = value.strip().replace(",", "").replace("￥", "").replace("¥", "")
            # 先认纯数字（含负号）：避免 parse_price 把 -1000 吃成 1000
            if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
                v = float(s)
            else:
                p = parse_price(value)
                v = float(p) if p is not None else None
                if v is None:
                    return None
        else:
            v = float(value)
    except (TypeError, ValueError):
        return None
    return float(v) if is_valid_price(v) else None


def extract_visible_price(text: str) -> tuple[float | None, str, str]:
    """
    从列表行/详情正文抽数字价。

    造价通实际 SSR 常见写法（登录后）：
      「1143.17 市场价： ￥1143.17 建议价： ￥1143.17」
    未登录则是「查看价格」，无数字。

    返回 (price, price_text, tax_mode)。无效时 price=None。
    """
    if not text:
        return None, "", "unknown"
    plain = re.sub(r"<[^>]+>", " ", str(text))
    plain = re.sub(r"\s+", " ", plain)
    # 纯「查看价格」且无数字标价 → 无价
    if "查看价格" in plain and not re.search(
        r"(?:市场价|建议价|除税|含税)[^0-9]{0,12}[¥￥]?\s*\d", plain
    ):
        # 仍可能有 NUXT 字段，继续往下试
        pass

    # 长标签优先，避免「除税市场价」被短「市场价」抢先匹配到错误数字
    label_modes: list[tuple[str, str]] = [
        ("除税市场价", "tax_excl"),
        ("含税市场价", "tax_incl"),
        ("除税建议价", "tax_excl"),
        ("含税建议价", "tax_incl"),
        ("除税价", "tax_excl"),
        ("含税价", "tax_incl"),
        ("市场价", "tax_incl"),
        ("建议价", "tax_incl"),
    ]
    for label, mode in label_modes:
        # 标签后的价格必须有币种符号，或有明确冒号。
        # 否则「圆钢管89×2.5 市场价」「4级钢筋 市场价」会把规格 2.5/4 当价。
        m = re.search(
            rf"{re.escape(label)}\s*(?:[:：]\s*[¥￥]?|[¥￥]\s*)"
            rf"(-?\d+(?:\.\d+)?)",
            plain,
        )
        if m:
            price = coerce_valid_price(m.group(1))
            if price is not None:
                return price, m.group(0)[:300], mode

    # NUXT / API 压缩字段；-1000 为锁价占位，coerce 会丢掉
    # 注意：不用 re.I 匹配 taxPrice，否则会误命中 noTaxPrice 中间的 TaxPrice
    for pat, mode in (
        (r"(?<![A-Za-z])noTaxPrice\s*[:=]\s*(-?\d+(?:\.\d+)?)", "tax_excl"),
        (r"(?<![A-Za-z])taxPrice\s*[:=]\s*(-?\d+(?:\.\d+)?)", "tax_incl"),
        (r"(?<![A-Za-z])suggestNoTaxPrice\s*[:=]\s*(-?\d+(?:\.\d+)?)", "tax_excl"),
        (r"(?<![A-Za-z])suggestTaxPrice\s*[:=]\s*(-?\d+(?:\.\d+)?)", "tax_incl"),
        # 预测价仅作最后手段，仍须过 is_valid_price
        (r"(?<![A-Za-z])priceyucem\s*[:=]\s*(-?\d+(?:\.\d+)?)", "tax_incl"),
        (r"(?<![A-Za-z])priceyucejy\s*[:=]\s*(-?\d+(?:\.\d+)?)", "tax_excl"),
    ):
        m = re.search(pat, plain)
        if not m:
            m = re.search(pat, str(text))
        if m:
            price = coerce_valid_price(m.group(1))
            if price is not None:
                return price, m.group(0)[:300], mode

    # ￥ 金额（避免把税率 13% 当价）
    m = re.search(r"[¥￥]\s*(-?\d+(?:\.\d+)?)", plain)
    if m:
        price = coerce_valid_price(m.group(1))
        if price is not None:
            return price, m.group(0)[:300], "tax_incl"

    return None, "", "unknown"


def list_url(query: str, *, host: str = DEFAULT_HOST) -> str:
    q = (query or "").strip()[:40]
    base = (host or DEFAULT_HOST).rstrip("/")
    return base + LIST_PATH_TMPL.format(query=quote(q))


def host_from_spec(spec: Any) -> str:
    tpl = str(getattr(spec, "search_url_template", "") or "")
    if tpl.startswith("http"):
        p = urlparse(tpl)
        return f"{p.scheme}://{p.netloc}"
    return DEFAULT_HOST


def _request_get(page, url: str, timeout_ms: int) -> str:
    """从 Playwright page/context 发 HTTP GET，带上浏览器 Cookie，不执行页面 JS。"""
    last_err = ""
    for getter in (
        lambda: page.context.request.get(url, timeout=timeout_ms),
        lambda: page.request.get(url, timeout=timeout_ms),
    ):
        try:
            resp = getter()
            if resp is None:
                continue
            status = int(getattr(resp, "status", 0) or 0)
            if status != 200 and not getattr(resp, "ok", False):
                last_err = f"status={status}"
                continue
            text = resp.text() or ""
            # 有正文就返回（含「暂无」短页）；勿再 fallback 到无 Cookie 的 urllib
            if text.strip():
                return text
            last_err = "empty body"
        except Exception as e:
            last_err = str(e)
            continue
    # 禁止退回无 Cookie urllib。那会把站点默认钢材列表当成查询结果。
    if last_err:
        print(f"  [zaojiatong] 带 Cookie HTTP 请求失败: {last_err}")
    return ""


def auth_cookie_names(page) -> list[str]:
    """有效登录 Cookie 名（jsid 匿名不算）。"""
    try:
        cookies = page.context.cookies()
    except Exception:
        try:
            cookies = page.cookies()
        except Exception:
            return []
    hits: list[str] = []
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain") or "").lower()
        if "zjtcn" not in domain:
            continue
        name = str(c.get("name") or "").lower()
        val = str(c.get("value") or "").strip()
        if not val or val.lower() in {"0", "false", "null", "undefined", "deleted"}:
            continue
        for key in _AUTH_COOKIE_NAMES:
            if name == key or name.startswith(key):
                hits.append(str(c.get("name") or name))
                break
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def parse_list_html(html: str, *, host: str = DEFAULT_HOST) -> list[dict[str, Any]]:
    """解析市场价列表 SSR。"""
    if not html:
        return []
    import html as html_lib

    out: list[dict[str, Any]] = []
    blocks = re.findall(r'<tr class="flex flex-row"[^>]*>(.*?)</tr>', html, re.S | re.I)
    if not blocks:
        blocks = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)

    base = (host or DEFAULT_HOST).rstrip("/") + "/"

    for index, block in enumerate(blocks):
        if "材料名称及规格型号" in block and "除税市场价" in block:
            continue
        if "material-title" not in block and "/shichangjia/info_" not in block:
            continue

        href_m = re.search(r'href="(/shichangjia/info_[^"]+)"', block)
        href = html_lib.unescape(href_m.group(1)) if href_m else ""
        if href and href.startswith("/"):
            href = urljoin(base, href.lstrip("/"))

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

        texts = [t.strip() for t in re.findall(r">([^<>]{1,100})<", block) if t.strip()]
        spec = ""
        sm = re.search(r"规格型号：\s*</span>\s*<span[^>]*>([^<]+)", block)
        if sm:
            spec = sm.group(1).strip()
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
                        "型号",
                    )
                )
                and "原始名称" not in t
                and t != "规格型号："
            ]
            spec = " ".join(bits[:8])

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
            if re.fullmatch(
                r"(m²|m³|㎡|米|m|个|件|套|台|组|kg|t|吨|项|张|根)", t, re.I
            ):
                unit = t
                break

        plain = re.sub(r"<[^>]+>", " ", block)
        plain = re.sub(r"\s+", " ", plain)
        # 列表块 + 原始 HTML 一起抽价（登录后常见「市场价：￥xxx」）
        price, price_text, tax_mode = extract_visible_price(plain + "\n" + block)

        if not name and not title:
            continue

        blob = f"{name} {spec} {plain}"[:2000]
        out.append(
            {
                "index": index,
                "name": (name or title)[:180],
                "title": (name or title)[:180],
                "text": blob,
                "spec": spec[:400],
                "price": price,  # None 表示无有效价，绝不写 0
                "price_text": (price_text or "")[:300],
                "tax_mode": tax_mode,
                "unit": unit,
                "supplier": supplier[:80],
                "brand": "",
                "href": href,
                "url": href,
            }
        )
    return out


def parse_detail_html(html: str, fallback_title: str = "") -> dict[str, Any]:
    """解析材料详情 SSR。"""
    if not html:
        return {"title": fallback_title, "price": None, "text": ""}
    tm = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = (tm.group(1).strip() if tm else "") or fallback_title
    body = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    body = re.sub(r"<style[\s\S]*?</style>", " ", body, flags=re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    price, price_text, tax_mode = extract_visible_price(body + "\n" + html)
    return {
        "title": title[:180],
        "price": price,
        "price_text": (price_text or "")[:300],
        "tax_mode": tax_mode,
        "text": body[:6000],
    }


def rows_to_candidates(
    rows: list[dict[str, Any]],
    query: str,
    must: list[str],
    *,
    host: str = DEFAULT_HOST,
) -> list[dict[str, Any]]:
    """SSR 行 → 询价候选。有价则 inline_detail；无价也带上规格证据，避免再开详情页。

    **硬约束**：price_tax 只写有效数字；缺失用 None，禁止 0 / 0.01 占位。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or row.get("title") or "")
        text = str(row.get("text") or "")
        spec = str(row.get("spec") or "")
        blob = f"{name} {spec} {text}"
        sc = score_title(blob, must)
        if sc <= 0:
            try:
                from ..matching import soft_product_name_equivalent

                if soft_product_name_equivalent(query, name, blob):
                    sc = 1
            except Exception:
                pass
        # 搜「线型灯」却返回「圆钢/4级钢筋」是失真响应，不得强制保留。
        if sc <= 0:
            continue
        href = str(row.get("href") or row.get("url") or "")
        if href and href.startswith("/"):
            href = urljoin(host.rstrip("/") + "/", href.lstrip("/"))
        # 行上没价时，再从正文扫一遍（兼容「市场价：￥」写在 text 里）
        price = coerce_valid_price(row.get("price"))
        price_text = str(row.get("price_text") or "")
        tax_mode = str(row.get("tax_mode") or "unknown")
        if price is None:
            p2, t2, m2 = extract_visible_price(blob)
            if p2 is not None:
                price, price_text, tax_mode = p2, t2, m2
        # 无价时用行内规格作证据，尽量不触发 open_detail 跳转
        detail_blob = f"{name}\n规格：{spec}\n{text}"[:4000]
        cand: dict[str, Any] = {
            "title": name[:180],
            # 有效价写 float；无效写 None —— 禁止 0.01 占位（会导出成 0）
            "price_tax": price,
            "url": href or host,
            "sku": f"zaojiatong:{row.get('index', '')}:{re.sub(r'[^0-9a-z\\u4e00-\\u9fff]+', '', (name + spec).lower())[:80]}",
            "score": sc,
            "platform": "zaojiatong",
            "supplier": str(row.get("supplier") or ""),
            "unit": str(row.get("unit") or ""),
            "tax_mode": tax_mode,
            "price_text": (price_text or "")[:300],
            "price_context": text[:500],
            "spec_seen": (spec or text)[:1200],
            "brand": str(row.get("brand") or ""),
            # 关键：列表行已带规格证据，默认 inline，减少详情 goto
            "inline_detail": True,
            "detail_text": detail_blob,
            "detail_title": name[:180],
            "detail_confirmed": True,
            "sku_scope": "exact_result_row",
        }
        if price is not None:
            cand["price_source"] = "platform_result_row"
            cand["needs_detail_price"] = False
        else:
            cand["price_source"] = "missing"
            cand["needs_detail_price"] = True
        out.append(cand)
    # 有价优先；无价排后（None 当极大）
    def _sort_key(x: dict[str, Any]) -> tuple:
        p = x.get("price_tax")
        pv = float(p) if is_valid_price(p) else 1e18
        return (-x.get("score", 0), pv)

    out.sort(key=_sort_key)
    return out[:40]


def open_workspace(page, timeout_ms: int = 60000, *, host: str = DEFAULT_HOST) -> tuple[bool, str]:
    """
    必须打开造价通市场价页（用户能看见浏览器），并尽量在 SPA 踢登录前站住。

    返回 (是否打开成功, 说明)。
    """
    # 装 dialog 自动点「继续登录」；不要用 route.abort 拦登录（会变成 chrome-error 白屏）
    try:
        from ..login_gate import (
            install_zaojiatong_dialog_auto_accept,
            try_handle_zaojiatong_session_conflict,
        )

        install_zaojiatong_dialog_auto_accept(page)
        try_handle_zaojiatong_session_conflict(page)
    except Exception:
        pass

    landing = (host or DEFAULT_HOST).rstrip("/") + LANDING_PATH
    try:
        cur = (page.url or "").lower()
    except Exception:
        cur = ""

    # 已在市场价页则不重开
    if "shichangjia" in cur and "zjtcn.com" in cur and "login" not in cur and "member.zjtcn" not in cur:
        print(f"  [zaojiatong] 已在市场价页: {cur[:80]}")
        return True, "already_on_market"

    try:
        print(f"  [zaojiatong] 打开市场价工作台: {landing}")
        # commit 更快，尽早拿到 SSR
        page.goto(landing, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(400)
    except Exception as e:
        return False, f"打开失败: {e}"

    try:
        try_handle_zaojiatong_session_conflict(page)
    except Exception:
        pass

    try:
        url = (page.url or "").lower()
        title = page.title() or ""
    except Exception:
        url, title = "", ""

    if "chrome-error" in url or url.startswith("chrome-error"):
        # 上次 abort 残留：强制重开
        try:
            page.goto(landing, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(500)
            url = (page.url or "").lower()
            title = page.title() or ""
        except Exception as e:
            return False, f"从错误页恢复失败: {e}"

    if "member.zjtcn" in url or "/common/login" in url or title.strip().startswith("会员登录"):
        print("  [zaojiatong] 当前在登录页 — 请在本窗口登录（账号使用中请点「继续登录」）")
        return False, "need_login"

    n = 0
    try:
        n = int(page.locator("a.material-title").count() or 0)
    except Exception:
        n = 0
    if n <= 0:
        html = _request_get(page, landing, timeout_ms)
        n_ssr = (html.count("material-title") // 2) if html else 0
        if n_ssr > 0:
            print(
                f"  [zaojiatong] 页面已打开（DOM 可能被 SPA 刷新），"
                f"SSR 仍有约 {n_ssr} 条，可继续搜价"
            )
            return True, f"opened_ssr n≈{n_ssr}"
        print(f"  [zaojiatong] 页面 URL={url[:80]} 但未见列表")
        return True, "opened_no_list"  # 仍算打开了，别直接结束

    print(f"  [zaojiatong] 工作台已打开，列表可见约 {n} 条  url={url[:70]}")
    return True, f"opened n={n}"


def _visual_type_search(page, query: str) -> None:
    """在已打开的页面上尽量填搜索框（给用户看），失败不影响 HTTP 搜价。"""
    q = (query or "").strip()[:40]
    if not q:
        return
    for sel in ("#indexKey2", "input[placeholder*='关键词']", ".search-keywords input"):
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.click(timeout=1500, force=True)
            loc.fill(q, force=True, timeout=2000)
            try:
                page.locator(".search-btn").first.click(timeout=1500, force=True)
            except Exception:
                try:
                    loc.press("Enter")
                except Exception:
                    pass
            page.wait_for_timeout(600)
            print(f"  [zaojiatong] 已在页面搜索框填入: {q}")
            return
        except Exception:
            continue


def search(
    page,
    query: str,
    must: list[str],
    timeout_ms: int,
    min_score: int,
    spec: Any = None,
) -> tuple[list[dict[str, Any]] | None, str]:
    """
    造价通专用搜索入口。

    流程：
      1) 先打开并稳住市场价页（用户必须能看见浏览器在干活）
      2) 页面上填一次搜索词（可视化）
      3) HTTP 抓 SSR 出候选（可靠，不依赖 SPA 是否踢登录）
    """
    q = (query or "").strip()[:40]
    if not q:
        return [], "empty_page"

    host = host_from_spec(spec) if spec is not None else DEFAULT_HOST

    # —— 1) 尽量打开页面给用户看（失败不阻断 HTTP 搜价）——
    opened, why = open_workspace(page, timeout_ms, host=host)
    if not opened:
        print(f"  [zaojiatong] 工作台: {why}（继续 HTTP 拉列表，不中断）")
    else:
        print(f"  [zaojiatong] 工作台: {why}")

    # —— 2) 可视化填词（失败无所谓）——
    try:
        if opened or why not in ("need_login",):
            _visual_type_search(page, q)
    except Exception:
        pass

    # —— 3) HTTP SSR 拿数据（主路径，不依赖页面是否登录）——
    url = list_url(q, host=host)
    print(f"  [zaojiatong] HTTP 拉取列表: {url[:90]}…")
    html = _request_get(page, url, timeout_ms)
    if not html:
        print("  [zaojiatong] HTTP 拉取失败")
        # 仅当页面也在登录且完全没数据时才 need_login
        if why == "need_login":
            return None, "need_login"
        return [], "empty_page"

    if "会员登录" in html[:800] and "material-title" not in html and "shichangjia/info_" not in html:
        print("  [zaojiatong] HTTP 也只有登录页 → need_login")
        return None, "need_login"

    rows = parse_list_html(html, host=host)
    print(f"  [zaojiatong] 解析到 {len(rows)} 行")
    if not rows:
        return [], "empty_page"

    cands = rows_to_candidates(rows, q, must, host=host)
    if not cands:
        return [], "empty_page"

    # 规格同义展开写入 detail_text，便于 DN/φ/直径 互认
    for c in cands:
        c["detail_text"] = expand_spec_aliases_in_text(
            str(c.get("detail_text") or ""), str(c.get("spec_seen") or "")
        )
        c["spec_seen"] = expand_spec_aliases_in_text(
            str(c.get("spec_seen") or ""), ""
        )

    priced = [c for c in cands if is_valid_price(c.get("price_tax"))]
    print(
        f"  [zaojiatong] 候选 {len(cands)} 条，其中有数字价 {len(priced)} 条"
        + ("（其余需会员才见价）" if not priced else "")
    )
    # 有价优先，但不得因为列表里存在任意有价行，就丢掉真正相关的无价候选。
    # 无价候选保留 None，只能进待核，绝不会变成正式价。
    return cands, "ok"


def expand_spec_aliases_in_text(text: str, extra: str = "") -> str:
    """
    把造价通站常见写法展开成匹配引擎能认的 DN/φ/直径 同义串。
    例如：直径(mm)：12 → 附加 φ12 DN12 直径12
    """
    blob = f"{text or ''} {extra or ''}"
    if not blob.strip():
        return text or ""
    add: list[str] = []
    for m in re.finditer(
        r"直径\s*(?:\(mm\)|mm)?\s*[:：]?\s*(\d+(?:\.\d+)?)", blob, re.I
    ):
        n = m.group(1)
        add.extend([f"φ{n}", f"Φ{n}", f"DN{n}", f"直径{n}", f"{n}mm"])
    for m in re.finditer(r"(?:DN|φ|Φ|ф)\s*(\d+(?:\.\d+)?)", blob, re.I):
        n = m.group(1)
        add.extend([f"φ{n}", f"DN{n}", f"直径{n}", f"直径(mm)：{n}", f"{n}mm"])
    for m in re.finditer(r"牌号\s*[:：]?\s*([A-Z0-9]+)", blob, re.I):
        add.append(m.group(1))
    # 去重
    seen = set()
    extras = []
    for a in add:
        k = a.lower()
        if k not in seen:
            seen.add(k)
            extras.append(a)
    if not extras:
        return text or ""
    return f"{text or ''} " + " ".join(extras)


def enrich_detail(page, cand: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    """
    详情补价：只 HTTP，不移动主页面。
    已有 inline 规格时，仅当需要价才请求。
    """
    out = dict(cand)
    url = str(cand.get("url") or "")
    if not url or "shichangjia/info_" not in url:
        # 仍可从已有正文补抽价
        if not is_valid_price(out.get("price_tax")):
            p, t, m = extract_visible_price(
                f"{out.get('detail_text') or ''} {out.get('spec_seen') or ''} {out.get('price_text') or ''}"
            )
            if p is not None:
                out["price_tax"] = p
                out["price_text"] = t
                out["tax_mode"] = m
                out["needs_detail_price"] = False
                out["price_source"] = "inline_text"
            else:
                out["price_tax"] = None
                out["needs_detail_price"] = True
        return out
    # 已有真实价则不动
    if is_valid_price(cand.get("price_tax")) and cand.get("price_source") != "missing":
        return out

    # 先从 inline 正文补抽（列表其实已有「市场价：￥」时常可命中，免再 HTTP）
    p0, t0, m0 = extract_visible_price(
        f"{out.get('detail_text') or ''} {out.get('spec_seen') or ''} {out.get('price_text') or ''}"
    )
    if p0 is not None:
        out["price_tax"] = p0
        out["price_text"] = t0
        out["tax_mode"] = m0
        out["needs_detail_price"] = False
        out["price_source"] = "inline_text"
        out["detail_confirmed"] = True
        return out

    html = _request_get(page, url, timeout_ms)
    if not html:
        out["price_tax"] = None if not is_valid_price(out.get("price_tax")) else out.get("price_tax")
        return out
    det = parse_detail_html(html, fallback_title=str(cand.get("title") or ""))
    out["detail_title"] = det["title"] or out.get("title")
    # 合并证据
    prev = str(out.get("detail_text") or "")
    out["detail_text"] = (prev + "\n" + det["text"])[:6000]
    out["detail_confirmed"] = True
    out["final_url"] = url
    if is_valid_price(det.get("price")):
        out["price_tax"] = float(det["price"])
        out["price_text"] = det.get("price_text") or ""
        out["tax_mode"] = det.get("tax_mode") or out.get("tax_mode") or "unknown"
        out["needs_detail_price"] = False
        out["price_source"] = "detail_ssr"
    else:
        # 详情也无有效价：清空占位，禁止残留 0.01
        out["price_tax"] = None
        out["needs_detail_price"] = True
        out["price_source"] = "missing"
    return out


def install_browser_guards(page) -> None:
    """只装 dialog 自动「继续登录」；不再 route.abort 登录导航（会白屏 chrome-error）。"""
    try:
        from ..login_gate import (
            install_zaojiatong_dialog_auto_accept,
            try_handle_zaojiatong_session_conflict,
        )

        install_zaojiatong_dialog_auto_accept(page)
        try_handle_zaojiatong_session_conflict(page)
    except Exception:
        pass


def allow_login_navigation(page, allow: bool = True) -> None:
    """兼容旧调用；现已不再用 route 拦登录，仅作标记。"""
    try:
        page._zjt_allow_login_nav = bool(allow)
    except Exception:
        pass
