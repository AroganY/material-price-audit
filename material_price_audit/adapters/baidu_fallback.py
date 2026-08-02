"""
百度全网兜底（非正式价格平台）。

仅在用户勾选的造价/电商平台全部查完且正式价仍不足 K 时触发一次。
用途：
  - alias_clue：发现别名 → 回原造价站补搜（最多 +2 词）
  - web_reference：来源页验证后的公开数字价（不进正式合格价）
  - supplier_lead：厂家/电话线索，价格留空

不调用大模型也能完成基础 SERP 解析与页面抽取。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from ..matching import strict_name_spec_match
from ..models import Quote
from ..name_aliases import get_aliases_for_name, normalize_name_key
from ..normalize import normalize_search_query
from ..scraper import parse_price

# —— 预算 ——
MAX_BAIDU_QUERIES = 2
MAX_SOURCE_PAGES = 5
CACHE_DAYS = 7

# 低质/排除域名
_BLOCK_DOMAINS = frozenset(
    {
        "zhidao.baidu.com",
        "wenku.baidu.com",
        "baijiahao.baidu.com",
        "mbd.baidu.com",
        "baike.baidu.com",
        "tieba.baidu.com",
        "jingyan.baidu.com",
        "image.baidu.com",
        "map.baidu.com",
        "passport.baidu.com",
    }
)
_NON_SOURCE_DOMAINS = frozenset({"baidu.com", "passport.baidu.com", "m.baidu.com"})
_LOW_QUALITY_HINTS = (
    "采集",
    "转载",
    "seo",
    "知道",
    "文库",
    "百家号",
    "贴吧",
)

# 优先域名片段（厂家/B2B/招投标）
_HIGH_QUALITY_HINTS = (
    ".gov.cn",
    "ccgp",
    "ggzy",
    "zfcg",
    "1688.com",
    "made-in-china",
    "hc360",
    "globalsources",
    "manufacturer",
)


@dataclass
class SerpHit:
    title: str
    url: str
    snippet: str = ""
    rank: int = 0


@dataclass
class BaiduFallbackResult:
    web_refs: list[Quote] = field(default_factory=list)
    supplier_leads: list[Quote] = field(default_factory=list)
    alias_clues: list[str] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    queries_used: list[str] = field(default_factory=list)
    skipped_reason: str = ""
    captcha: bool = False


def _cache_dir(root: Path | None) -> Path | None:
    if root is None:
        return None
    return Path(root) / "data" / "mapping-cache" / "baidu-fallback"


def _cache_key(query: str) -> str:
    raw = re.sub(r"\s+", " ", (query or "").strip().lower()).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:28]


def load_baidu_cache(root: Path | None, query: str) -> list[dict[str, Any]] | None:
    d = _cache_dir(root)
    if not d:
        return None
    path = d / f"{_cache_key(query)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = str(data.get("cached_at") or "")
        if ts:
            cached = datetime.fromisoformat(ts)
            if datetime.now() - cached > timedelta(days=CACHE_DAYS):
                return None
        hits = data.get("hits")
        return list(hits) if isinstance(hits, list) else None
    except Exception:
        return None


def save_baidu_cache(root: Path | None, query: str, hits: list[dict[str, Any]]) -> None:
    d = _cache_dir(root)
    if not d:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_cache_key(query)}.json"
        path.write_text(
            json.dumps(
                {
                    "query": query,
                    "cached_at": datetime.now().isoformat(timespec="seconds"),
                    "hits": hits,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def extract_hard_spec_tokens(name: str, spec: str) -> list[str]:
    """从名称+规格抽取必须原样保留的硬规格 token。"""
    blob = f"{name or ''} {spec or ''}"
    toks: list[str] = []
    for m in re.finditer(
        r"(?i)(?:DN|φ|Φ|PN)\s*\d+(?:\.\d+)?"
        r"|\d+(?:\.\d+)?\s*(?:W(?:\s*[/／]\s*m)?|V|K|mm|MPa|kPa|A)"
        r"|IP\s*\d{2}"
        r"|(?:DS-|RG-|iDS-|HM-|JB-|MS-|LRS-)[A-Z0-9/\-\.]+"
        r"|[A-Z]{1,8}\d{2,}[A-Z0-9\-_/\.]*",
        blob,
    ):
        t = re.sub(r"\s+", "", m.group(0))
        if t and t not in toks:
            toks.append(t)
    # 截面 1250x400
    for m in re.finditer(
        r"(?<!\d)(\d{2,5})\s*[xX×*]\s*(\d{2,5})(?:\s*[xX×*]\s*(\d{2,5}))?",
        blob,
    ):
        parts = [m.group(1), m.group(2)] + ([m.group(3)] if m.group(3) else [])
        t = "x".join(parts)
        if t not in toks:
            toks.append(t)
    return toks[:8]


def build_baidu_queries(
    name: str,
    spec: str,
    root: Path | None = None,
    *,
    max_n: int = MAX_BAIDU_QUERIES,
) -> list[str]:
    """
    1) 完整名称 + 核心规格
    2) 已确认别名 + 核心规格
    最多 max_n 个；硬规格原样保留。
    """
    from ..matching import name_search_core, peel_name_dimension_noise

    hard = extract_hard_spec_tokens(name, spec)
    hard_s = " ".join(hard)
    core = name_search_core(peel_name_dimension_noise(name) or name) or (name or "")[:24]
    short = re.split(r"[（(【\[]", name or "")[0].strip() or core
    q1 = normalize_search_query(f"{short} {hard_s}".strip())[:60]
    queries: list[str] = []
    if q1:
        queries.append(q1)
    # 别名
    try:
        aliases = get_aliases_for_name(name, root, max_n=2)
    except Exception:
        aliases = []
    for al in aliases:
        if len(queries) >= max_n:
            break
        q = normalize_search_query(f"{al} {hard_s}".strip())[:60]
        if q and q.lower() not in {x.lower() for x in queries}:
            queries.append(q)
    # 无别名时用 core + hard 作第二词
    if len(queries) < max_n and core and core != short:
        q2 = normalize_search_query(f"{core} {hard_s}".strip())[:60]
        if q2 and q2.lower() not in {x.lower() for x in queries}:
            queries.append(q2)
    return queries[:max_n]


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def source_quality_for_url(url: str, title: str = "", snippet: str = "") -> str:
    """high | medium | low | block"""
    dom = domain_of(url)
    blob = f"{dom} {title} {snippet}".lower()
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        path = ""
    auth_or_redirect = (
        dom in _NON_SOURCE_DOMAINS
        or dom.endswith(".baidu.com")
        or "passport" in dom
        or any(
            marker in path
            for marker in ("/login", "/signin", "/passport", "/auth/", "/baidu.php")
        )
    )
    if not dom or auth_or_redirect or any(b in dom for b in _BLOCK_DOMAINS):
        return "block"
    if any(h in blob for h in _LOW_QUALITY_HINTS) and not any(
        h in dom for h in (".gov.cn", "ccgp", "ggzy")
    ):
        return "low"
    if any(h in dom or h in blob for h in _HIGH_QUALITY_HINTS):
        return "high"
    if dom.endswith(".com.cn") or dom.endswith(".com") or dom.endswith(".cn"):
        return "medium"
    return "low"


def unwrap_baidu_url(href: str) -> str:
    """解析百度跳转链接中的真实 URL。"""
    if not href:
        return ""
    href = href.strip()
    if "baidu.com/link" in href or "www.baidu.com/link" in href:
        try:
            qs = parse_qs(urlparse(href).query)
            if "url" in qs and qs["url"]:
                return unquote(qs["url"][0])
        except Exception:
            pass
    if href.startswith("//"):
        href = "https:" + href
    return href


def parse_baidu_serp_html(html: str) -> list[SerpHit]:
    """从百度 SERP HTML 抽取结果（无 JS 也能拿到 SSR 块）。"""
    if not html:
        return []
    if "验证码" in html[:2000] and ("安全验证" in html or "captcha" in html.lower()):
        return []  # 调用方再看 captcha 标志
    out: list[SerpHit] = []
    # 常见结果块
    blocks = re.findall(
        r'<div[^>]+class="[^"]*(?:result|c-container)[^"]*"[^>]*>(.*?)</div>\s*(?=<div[^>]+class="[^"]*(?:result|c-container)|$)',
        html,
        re.S | re.I,
    )
    if not blocks:
        # 退化：找带 mu= 的结果
        for m in re.finditer(
            r'mu="(https?://[^"]+)"[^>]*>.*?<a[^>]+>(.*?)</a>',
            html,
            re.S | re.I,
        ):
            url = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2))
            title = re.sub(r"\s+", " ", title).strip()
            if url and title:
                out.append(SerpHit(title=title[:200], url=url, rank=len(out) + 1))
            if len(out) >= 15:
                break
        return out

    for i, block in enumerate(blocks[:20]):
        href_m = re.search(r'href="(https?://[^"]+)"', block)
        if not href_m:
            href_m = re.search(r'mu="(https?://[^"]+)"', block)
        if not href_m:
            continue
        url = unwrap_baidu_url(href_m.group(1))
        title_m = re.search(r"<h3[^>]*>\s*<a[^>]*>(.*?)</a>", block, re.S | re.I)
        if not title_m:
            title_m = re.search(r"<a[^>]+>(.*?)</a>", block, re.S | re.I)
        title = re.sub(r"<[^>]+>", "", title_m.group(1) if title_m else "")
        title = re.sub(r"\s+", " ", title).strip()
        snip_m = re.search(
            r'class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</(?:span|div|p)>',
            block,
            re.S | re.I,
        )
        snippet = re.sub(r"<[^>]+>", " ", snip_m.group(1) if snip_m else "")
        snippet = re.sub(r"\s+", " ", snippet).strip()[:400]
        if not title or not url:
            continue
        if "baidu.com" in domain_of(url) and "baidu.com/link" not in url:
            # 仍是百度站内
            if source_quality_for_url(url) == "block":
                continue
        out.append(
            SerpHit(title=title[:200], url=url, snippet=snippet, rank=len(out) + 1)
        )
    return out


def is_baidu_captcha_html(html: str) -> bool:
    if not html:
        return False
    head = html[:3000].lower()
    return ("安全验证" in html[:3000] or "captcha" in head) and (
        "验证码" in html[:3000] or "wappass" in head or "passport" in head
    )


def filter_and_rank_hits(hits: list[SerpHit], *, max_n: int = MAX_SOURCE_PAGES) -> list[SerpHit]:
    """去重 + 质量过滤，取前 max_n 个有效来源。"""
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    scored: list[tuple[int, SerpHit]] = []
    for h in hits:
        url = unwrap_baidu_url(h.url)
        if not url or not url.startswith("http"):
            continue
        q = source_quality_for_url(url, h.title, h.snippet)
        if q == "block":
            continue
        uk = re.sub(r"[#?].*$", "", url).rstrip("/").lower()
        tk = re.sub(r"\s+", "", h.title.lower())[:40]
        if uk in seen_url or (tk and tk in seen_title):
            continue
        seen_url.add(uk)
        if tk:
            seen_title.add(tk)
        score = {"high": 0, "medium": 1, "low": 2}.get(q, 3)
        scored.append((score, SerpHit(h.title, url, h.snippet, h.rank)))
    scored.sort(key=lambda x: (x[0], x[1].rank))
    return [h for _, h in scored[:max_n]]


def extract_alias_clues(
    inquiry_name: str,
    title: str,
    body: str,
) -> list[str]:
    """从标题/正文中发现可能的别名（不含自身）。"""
    from ..matching import name_search_core, peel_name_dimension_noise

    core = name_search_core(peel_name_dimension_noise(inquiry_name) or inquiry_name) or ""
    core_k = normalize_name_key(core or inquiry_name)
    clues: list[str] = []
    pool = f"{title} {body[:1500]}"
    for m in re.finditer(r"[\u4e00-\u9fff]{2,12}", pool):
        frag = m.group(0)
        if frag in ("产品", "价格", "规格", "型号", "厂家", "供应商", "公司"):
            continue
        fk = normalize_name_key(frag)
        if not fk or fk == core_k:
            continue
        # 与询价品名有交集且不是过宽单字
        if core and len(set(core) & set(frag)) >= 2 and abs(len(frag) - len(core)) <= 4:
            if frag not in clues:
                clues.append(frag)
        if len(clues) >= 3:
            break
    return clues


def extract_page_contact(text: str) -> dict[str, str]:
    out = {"supplier": "", "phone": "", "contact": ""}
    if not text:
        return out
    for pat in (
        r"(?:公司名称|厂家名称|供应商|生产厂家|单位名称)\s*[:：]?\s*([^\n]{2,40})",
        r"([\u4e00-\u9fff]{2,20}(?:有限公司|股份有限公司|集团|厂|商行))",
    ):
        m = re.search(pat, text)
        if m:
            out["supplier"] = m.group(1).strip()[:60]
            break
    m = re.search(r"1[3-9]\d{9}", text)
    if m:
        out["phone"] = m.group(0)
    else:
        m = re.search(r"0\d{2,3}[-\s]?\d{7,8}", text)
        if m:
            out["phone"] = m.group(0)
    m = re.search(r"(?:联系人|经理|业务)\s*[:：]?\s*([\u4e00-\u9fff]{2,4})", text)
    if m:
        out["contact"] = m.group(1)
    return out


def extract_visible_prices_from_page(text: str) -> list[tuple[float, str]]:
    """
    从来源页正文抽数字价。
    不采用「摘要」；要求有 价格/单价/元 等上下文。
    """
    if not text:
        return []
    found: list[tuple[float, str]] = []
    for m in re.finditer(
        r"(?:价格|单价|报价|售价|市场价|含税价|除税价|参考价)\s*[:：]?\s*"
        r"[¥￥]?\s*(\d+(?:\.\d+)?)\s*(?:元|万元)?",
        text,
    ):
        raw = m.group(0)
        p = parse_price(m.group(1))
        if p is not None and 0.05 < p < 5_000_000:
            # 万元
            if "万元" in raw:
                p = p * 10000
            found.append((float(p), raw[:80]))
    for m in re.finditer(r"[¥￥]\s*(\d+(?:\.\d+)?)", text):
        p = parse_price(m.group(1))
        if p is not None and 0.05 < p < 5_000_000:
            found.append((float(p), m.group(0)[:80]))
    # 去重
    seen: set[float] = set()
    out: list[tuple[float, str]] = []
    for p, t in found:
        if p in seen:
            continue
        seen.add(p)
        out.append((p, t))
    return out[:5]


def should_trigger_baidu(
    *,
    formal_quote_count: int,
    k: int,
    baidu_already_done: bool,
    baidu_enabled: bool,
) -> bool:
    """原平台全部查完后，由调用方确认；此处只判数量与一次性。"""
    if not baidu_enabled:
        return False
    if baidu_already_done:
        return False
    if formal_quote_count >= max(1, int(k or 1)):
        return False
    return True


def classify_source_match(
    item: Any,
    title: str,
    body: str,
    prices: list[tuple[float, str]],
    contact: dict[str, str],
    url: str,
    quality: str,
) -> tuple[str, Quote | None, list[str]]:
    """
    返回 (kind, quote_or_none, alias_clues)
    kind: web_reference | supplier_lead | reject | alias_only
    """
    aliases = extract_alias_clues(
        str(getattr(item, "name", "") or ""), title, body
    )
    # 登录页、搜索跳转页、百度内部页不能作为可审核来源。
    if quality == "block" or source_quality_for_url(url, title, body[:300]) == "block":
        return ("alias_only", None, aliases) if aliases else ("reject", None, [])
    # 注入标题+正文做规格门禁
    mr = strict_name_spec_match(item, title, f"{title}\n{body[:5000]}")
    if "名称未命中" in (mr.detail or "") or any(
        "名称未命中" in str(c) for c in (mr.conflicts or ())
    ):
        # 仅别名线索
        if aliases:
            return "alias_only", None, aliases
        return "reject", None, []
    if mr.outcome == "reject" and mr.conflicts:
        return "reject", None, aliases

    domain = domain_of(url)
    now = datetime.now().isoformat(timespec="seconds")
    base_kw = dict(
        platform="baidu_web",
        title=(title or "")[:160],
        url=url,
        detail_url=url,
        captured_at=now,
        supplier=contact.get("supplier") or "",
        contact=contact.get("contact") or "",
        phone=contact.get("phone") or "",
        source_quality=quality,
        spec_seen=(body or "")[:800],
        evidence_scope="baidu_source_page",
    )

    if mr.ok and prices:
        p, ptxt = prices[0]
        q = Quote(
            rank=1,
            price=float(p),
            match_level="web_reference",
            match_score=float(mr.score or 0),
            match_detail=f"[全网参考·{domain}]{mr.detail}",
            price_text=ptxt,
            price_context=ptxt,
            price_role="web_reference",
            tax_mode="unknown",
            **base_kw,
        )
        return "web_reference", q, aliases

    # 名称过但规格缺失 / 无价
    has_actionable_contact = bool(
        (contact.get("supplier") or "").strip()
        or (contact.get("phone") or "").strip()
        or (contact.get("contact") or "").strip()
    )
    if (
        has_actionable_contact
        and (mr.outcome in ("review", "accept") or (not mr.ok and not mr.conflicts))
    ):
        q = Quote(
            rank=1,
            price=0.0,
            match_level="supplier_lead",
            match_score=float(mr.score or 0),
            match_detail=(
                f"[供应商线索·{domain}]"
                + (mr.detail if not mr.ok else "名称规格可参考，无可靠公开价")
            ),
            price_role="supplier_lead",
            **base_kw,
        )
        return "supplier_lead", q, aliases

    if aliases:
        return "alias_only", None, aliases
    return "reject", None, []


def _http_get(page, url: str, timeout_ms: int = 20000) -> tuple[str, str]:
    """优先 Playwright request；失败返回空。返回 (final_url, html)。"""
    if page is not None:
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
                    continue
                text = resp.text() or ""
                final = str(getattr(resp, "url", None) or url)
                if text.strip():
                    return final, text
            except Exception:
                continue
    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=max(10, timeout_ms // 1000)) as r:
            return str(r.geturl() or url), r.read().decode("utf-8", "replace")
    except Exception:
        return url, ""


def fetch_baidu_serp(
    page,
    query: str,
    *,
    root: Path | None = None,
    timeout_ms: int = 20000,
    use_cache: bool = True,
) -> tuple[list[SerpHit], bool, str]:
    """
    返回 (hits, captcha, source)
    source: cache|live|empty
    """
    if use_cache:
        cached = load_baidu_cache(root, query)
        if cached is not None:
            hits = [
                SerpHit(
                    title=str(h.get("title") or ""),
                    url=str(h.get("url") or ""),
                    snippet=str(h.get("snippet") or ""),
                    rank=int(h.get("rank") or 0),
                )
                for h in cached
                if h.get("url")
            ]
            return hits, False, "cache"

    url = f"https://www.baidu.com/s?wd={quote(query)}&rn=10"
    _, html = _http_get(page, url, timeout_ms=timeout_ms)
    if is_baidu_captcha_html(html):
        return [], True, "captcha"
    hits = parse_baidu_serp_html(html)
    if hits and use_cache:
        save_baidu_cache(
            root,
            query,
            [
                {
                    "title": h.title,
                    "url": h.url,
                    "snippet": h.snippet,
                    "rank": h.rank,
                }
                for h in hits
            ],
        )
    return hits, False, "live" if hits else "empty"


def run_baidu_fallback(
    item: Any,
    page=None,
    *,
    root: Path | None = None,
    timeout_ms: int = 20000,
    max_queries: int = MAX_BAIDU_QUERIES,
    max_pages: int = MAX_SOURCE_PAGES,
    log: Callable[[str], None] | None = None,
    baidu_enabled: bool = True,
    formal_quote_count: int = 0,
    k: int = 3,
    already_done: bool = False,
) -> BaiduFallbackResult:
    """
    执行百度兜底（单材料最多一次调用本函数）。
    不抛异常影响主任务。
    """
    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    out = BaiduFallbackResult()
    if not should_trigger_baidu(
        formal_quote_count=formal_quote_count,
        k=k,
        baidu_already_done=already_done,
        baidu_enabled=baidu_enabled,
    ):
        out.skipped_reason = (
            "already_done"
            if already_done
            else ("disabled" if not baidu_enabled else "full_k")
        )
        return out

    name = str(getattr(item, "name", "") or "")
    spec = str(getattr(item, "spec", "") or "")
    queries = build_baidu_queries(name, spec, root, max_n=max_queries)
    out.queries_used = list(queries)
    if not queries:
        out.skipped_reason = "no_query"
        return out

    _log(f"   [百度兜底] 正式价 {formal_quote_count}/{k} 不足 → 全网线索（最多{max_queries}词/{max_pages}页）")
    opened_urls: set[str] = set()
    pages_opened = 0
    all_aliases: list[str] = []

    for q in queries:
        try:
            hits, captcha, src = fetch_baidu_serp(
                page, q, root=root, timeout_ms=timeout_ms
            )
        except Exception as e:
            out.attempts.append(
                {"platform": "baidu", "query": q, "status": f"error:{e}"}
            )
            _log(f"   [百度] 搜索失败: {e}")
            continue
        if captcha:
            out.captcha = True
            out.attempts.append(
                {"platform": "baidu", "query": q, "status": "captcha"}
            )
            _log("   [百度] 出现验证码 → 停止本站，不循环刷新")
            break
        out.attempts.append(
            {
                "platform": "baidu",
                "query": q,
                "status": "ok" if hits else "empty",
                "n": len(hits),
                "source": src,
            }
        )
        ranked = filter_and_rank_hits(hits, max_n=max_pages)
        _log(f"   [百度] 「{q[:36]}」→ SERP {len(hits)} 条，有效来源 {len(ranked)}（{src}）")

        for hit in ranked:
            if pages_opened >= max_pages:
                break
            uk = re.sub(r"[#?].*$", "", hit.url).rstrip("/").lower()
            if uk in opened_urls:
                continue
            opened_urls.add(uk)
            quality = source_quality_for_url(hit.url, hit.title, hit.snippet)
            if quality == "block":
                continue
            # 禁止采用摘要价：必须打开原页
            try:
                final_url, html = _http_get(page, hit.url, timeout_ms=timeout_ms)
            except Exception as e:
                out.attempts.append(
                    {
                        "platform": "baidu",
                        "query": q,
                        "url": hit.url,
                        "status": f"open_fail:{e}",
                    }
                )
                continue
            if not html or len(html) < 200:
                continue
            final_url = final_url or hit.url
            final_quality = source_quality_for_url(
                final_url, hit.title, hit.snippet
            )
            if final_quality == "block":
                out.attempts.append(
                    {
                        "platform": "baidu",
                        "query": q,
                        "url": final_url,
                        "status": "reject_non_source_page",
                        "title": hit.title[:80],
                    }
                )
                continue
            pages_opened += 1
            # 抽正文
            body = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
            body = re.sub(r"<style[\s\S]*?</style>", " ", body, flags=re.I)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", body).strip()
            # 摘要价故意不传入 extract（只用 body）
            prices = extract_visible_prices_from_page(body)
            contact = extract_page_contact(body + " " + hit.title)
            kind, quote, aliases = classify_source_match(
                item,
                hit.title,
                body,
                prices,
                contact,
                final_url,
                final_quality,
            )
            for a in aliases:
                if a not in all_aliases:
                    all_aliases.append(a)
            if kind == "web_reference" and quote is not None:
                out.web_refs.append(quote)
                _log(
                    f"   [百度·全网参考] ¥{quote.price} · {domain_of(quote.url)} · "
                    f"{(quote.title or '')[:28]}"
                )
            elif kind == "supplier_lead" and quote is not None:
                out.supplier_leads.append(quote)
                _log(
                    f"   [百度·供应商线索] {quote.supplier or domain_of(quote.url)} "
                    f"电话={quote.phone or '-'} · 无可靠公开价"
                )
            elif kind == "reject":
                out.attempts.append(
                    {
                        "platform": "baidu",
                        "query": q,
                        "url": hit.url,
                        "status": "reject_match",
                        "title": hit.title[:80],
                    }
                )
        if pages_opened >= max_pages:
            _log(f"   [百度] 已达打开页数上限 {max_pages}")
            break

    out.alias_clues = all_aliases[:5]
    # 去重 web/supplier by url
    def _dedupe(qs: list[Quote]) -> list[Quote]:
        seen: set[str] = set()
        res: list[Quote] = []
        for q in qs:
            u = (q.detail_url or q.url or "").lower()
            if u in seen:
                continue
            seen.add(u)
            res.append(q)
        return res

    out.web_refs = _dedupe(out.web_refs)
    out.supplier_leads = _dedupe(out.supplier_leads)
    for i, q in enumerate(out.web_refs, 1):
        q.rank = i
    for i, q in enumerate(out.supplier_leads, 1):
        q.rank = i
    _log(
        f"   [百度兜底结束] 全网参考={len(out.web_refs)} "
        f"供应商线索={len(out.supplier_leads)} 别名线索={out.alias_clues}"
    )
    return out
