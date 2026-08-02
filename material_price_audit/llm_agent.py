"""
LLM 检索 Agent（不编价）：

职责边界：
  - Playwright：真正打开页面、输入、点击、抽价
  - LLM：空结果改词；候选模糊时辅助排序（默认少调用）
  - 规则：型号/数值冲突否决；优先不超报送不含税单价；价格数字只来自页面

速度原则：
  - 默认规则优先，LLM 只在「值得问」时调用
  - 检索词默认用规则，search_agent 仅在改词/模糊排序时介入
  - 同条材料检索词结果做进程内缓存
"""

from __future__ import annotations

import json
import re
from typing import Any

from .normalize import build_platform_queries, normalize_search_query
from .schema_map import _llm_chat_json
from .settings_store import UserSettings

# 进程内缓存：同材料+平台不重复问「搜什么词」
_PLAN_CACHE: dict[str, tuple[list[str], str]] = {}


def _search_agent_on(settings: UserSettings | None) -> bool:
    if not settings or not settings.llm_enabled:
        return False
    return "search_agent" in (settings.llm_use_for or [])


def _item_payload(item: Any, platform_id: str) -> dict[str, Any]:
    submit = getattr(item, "submit", None)
    try:
        submit_f = float(submit) if submit is not None else None
    except Exception:
        submit_f = None
    return {
        "platform": platform_id,
        "name": str(getattr(item, "name", "") or ""),
        "spec": str(getattr(item, "spec", "") or "")[:500],
        "brand": str(getattr(item, "brand", "") or ""),
        "unit": str(getattr(item, "unit", "") or ""),
        "submit_ex_tax": submit_f,
    }


def estimate_ex_tax(
    cand: dict[str, Any],
    tax_divisor: float = 1.13,
) -> float | None:
    """粗算候选不含税单价（列表价可能是含税）。"""
    raw = cand.get("price_tax")
    if raw in (None, ""):
        return None
    try:
        p = float(raw)
    except Exception:
        return None
    if p <= 0:
        return None
    mode = str(cand.get("tax_mode") or "unknown").lower()
    div = tax_divisor if tax_divisor and tax_divisor > 1 else 1.13
    if mode in ("tax_excl", "excl", "不含税"):
        return p
    if mode in ("tax_incl", "incl", "含税"):
        return p / div
    # 未知：造价站多按不含税展示，电商多含税——用略保守的「含税折算」作排序参考
    # 若站点本身已是不含税，排序仍合理（只是略低估相对报送的空间）
    pid = str(cand.get("platform") or "")
    if pid in ("guangcai", "lingcai", "huixun", "yize", "zaojiatong"):
        return p
    return p / div


def under_submit_flag(
    cand: dict[str, Any],
    submit: float | None,
    tax_divisor: float = 1.13,
) -> str:
    """
    under  — 不超报送（含 2% 容差）
    near   — 超报送但 ≤15%
    over   — 明显超报送
    unknown
    """
    if submit is None or submit <= 0:
        return "unknown"
    ex = estimate_ex_tax(cand, tax_divisor)
    if ex is None:
        return "unknown"
    if ex <= submit * 1.02:
        return "under"
    if ex <= submit * 1.15:
        return "near"
    return "over"


def rule_rank_candidates(
    candidates: list[dict[str, Any]],
    *,
    item: Any = None,
    tax_divisor: float = 1.13,
    top_n: int = 12,
) -> list[dict[str, Any]]:
    """
    规则排序（零 LLM 成本）——**规格优先，价格最后**：
      1) 名称命中
      2) 型号 / 口径(DN) / 截面命中
      3) 硬规格覆盖数量（电压/功率/IP 等）
      4) 证据完整度（spec_seen / 链接 / 价）
      5) 列表分
      6) 最后才参考价格（同规格时价低优先；**禁止**低价错规格压过精确候选）
    """
    if not candidates:
        return []
    submit = None
    try:
        s = getattr(item, "submit", None) if item is not None else None
        submit = float(s) if s is not None else None
    except Exception:
        submit = None

    name = str(getattr(item, "name", "") or "") if item is not None else ""
    spec = str(getattr(item, "spec", "") or "") if item is not None else ""
    name_core = ""
    try:
        from .matching import name_search_core, peel_name_dimension_noise

        name_core = name_search_core(peel_name_dimension_noise(name) or name) or ""
    except Exception:
        name_core = re.sub(r"(?i)^(?:LED|成品)+", "", name)[:12]

    dim_nums: list[str] = []
    try:
        m = re.search(
            r"(?<!\d)(\d{2,5})\s*[xX×*]\s*(\d{2,5})", f"{name} {spec}"
        )
        if m:
            dim_nums = [m.group(1), m.group(2)]
    except Exception:
        dim_nums = []

    model = ""
    try:
        m2 = re.search(
            r"[A-Za-z]{1,8}\d{2,}[A-Za-z0-9\-]*",
            f"{name} {spec}",
            re.I,
        )
        if m2:
            model = m2.group(0)
    except Exception:
        model = ""

    dn_wanted = ""
    m_dn = re.search(r"(?i)(?:DN|φ|Φ)\s*(\d{2,3})", f"{name} {spec}")
    if m_dn:
        dn_wanted = re.sub(r"\s+", "", m_dn.group(0))

    hard_tokens: list[str] = []
    for pat in (
        r"(?i)(?:AC|DC)\s*\d+(?:\.\d+)?\s*V",
        r"(?i)\d+(?:\.\d+)?\s*W(?:\s*[/／]\s*(?:m|米))?",
        r"(?i)IP\s*\d{2}",
        r"(?i)\d{3,5}\s*K",
        r"(?i)PN\s*\d+",
    ):
        for m in re.finditer(pat, spec or ""):
            hard_tokens.append(re.sub(r"\s+", "", m.group(0)).lower())

    def sort_key(c: dict[str, Any]) -> tuple:
        title = str(c.get("title") or "")
        blob = (
            f"{title} {c.get('spec_seen') or ''} "
            f"{c.get('detail_text') or ''} {c.get('price_context') or ''}"
        )
        blob_l = blob.lower()
        blob_n = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", blob_l)

        # 1) 名称
        name_hit = 0
        if name_core and (
            name_core in title
            or name_core in blob
            or re.sub(r"\s+", "", name_core) in blob_n
        ):
            name_hit = 1
        elif name and any(
            w in title for w in re.findall(r"[\u4e00-\u9fff]{2,}", name)[:3]
        ):
            name_hit = 1

        # 2) 型号 / 口径 / 截面
        id_hit = 0
        model_hit = 0
        if model:
            ml = model.lower()
            if ml in blob_l or re.sub(r"型$", "", model, flags=re.I).lower() in blob_l:
                model_hit = 1
                id_hit += 1
        dn_hit = 0
        wrong_dn = 0
        if dn_wanted:
            dn_num = re.search(r"(\d+)", dn_wanted)
            num = dn_num.group(1) if dn_num else ""
            if re.search(rf"(?i)(?:DN|φ|Φ)\s*{re.escape(num)}(?!\d)", blob):
                dn_hit = 1
                id_hit += 1
            else:
                other = re.findall(r"(?i)(?:DN|φ|Φ)\s*(\d{2,3})", blob)
                if other and all(str(x) != num for x in other):
                    wrong_dn = 1  # 明确其它口径 → 压到后面
        section_hit = 0
        if dim_nums:
            section_hit = 1 if all(n in blob for n in dim_nums) else 0
            if section_hit:
                id_hit += 1
            elif re.search(r"\d{2,5}\s*[xX×*]\s*\d{2,5}", blob):
                # 页面有其它截面
                wrong_dn = max(wrong_dn, 1)

        # 3) 硬规格覆盖
        hard_cover = 0
        for tok in hard_tokens:
            if tok and tok.lower() in blob_l.replace(" ", ""):
                hard_cover += 1

        # 4) 证据完整度
        evidence = 0
        if (c.get("spec_seen") or c.get("detail_text") or "").strip():
            evidence += 2
        if c.get("url") or c.get("quotation_url"):
            evidence += 1
        try:
            p = float(c.get("price_tax") or 0)
            if p > 0.05:
                evidence += 1
        except Exception:
            pass

        score = float(c.get("score") or c.get("title_score") or c.get("match_score") or 0)
        flag = under_submit_flag(c, submit, tax_divisor)
        # 价格最后；同规格时 under 略优先，但绝不能压过 wrong_dn
        tier = {"under": 0, "near": 1, "unknown": 2, "over": 3}
        ex = estimate_ex_tax(c, tax_divisor)
        price_key = ex if ex is not None else 1e18

        # reverse=False：更小更好
        return (
            0 if name_hit else 1,
            wrong_dn,  # 错口径/错截面靠后
            -id_hit,
            -hard_cover,
            -evidence,
            -score,
            tier.get(flag, 2),
            price_key,
        )

    ranked = [dict(c) for c in candidates]
    ranked.sort(key=sort_key)
    for i, c in enumerate(ranked):
        c["_rule_rank"] = i + 1
        c["_under_submit"] = under_submit_flag(c, submit, tax_divisor)
    return ranked[:top_n]


def plan_search_queries(
    *,
    item: Any,
    platform_id: str,
    seed_queries: list[str],
    settings: UserSettings | None,
    force_llm: bool = False,
) -> tuple[list[str], str]:
    """
    生成检索词。默认规则词（快）；
    仅当 force_llm 或配置了 search_agent 且显式需要时才问 LLM。
    为提速：search_agent 默认也不改写检索词，改词交给空结果后的 suggest_requery。
    """
    seeds: list[str] = []
    seed_seen: set[str] = set()
    for raw in seed_queries or []:
        q = normalize_search_query(str(raw or ""))
        key = q.lower()
        if len(q) < 2 or key in seed_seen:
            continue
        seed_seen.add(key)
        seeds.append(q)
    if not seeds:
        seeds = build_platform_queries(
            platform_id,
            getattr(item, "name", "") or "",
            getattr(item, "spec", "") or "",
            getattr(item, "brand", "") or "",
            list(getattr(item, "spec_tokens", None) or []),
        )
    # 默认：规则足够快且稳；LLM 改词成本高、收益有限
    # 返回完整规则词列表，由 inquiry 按平台预算截断（造价站 4～6）
    if not force_llm:
        return seeds[:8], "规则检索词"

    if not _search_agent_on(settings):
        return seeds[:8], "规则检索词"

    cache_key = "|".join(
        [
            platform_id,
            str(getattr(item, "name", "") or "")[:80],
            str(getattr(item, "spec", "") or "")[:120],
            str(getattr(item, "brand", "") or "")[:40],
        ]
    )
    if cache_key in _PLAN_CACHE:
        return _PLAN_CACHE[cache_key]

    system = (
        "你是工程造价材料询价员的搜索助理。根据材料名称/规格/品牌，为指定平台生成搜索关键词。"
        "要求：像真人在该平台搜索框输入；短而准；不要编造不存在的型号。"
        f"平台={platform_id}。"
        "硬规则：①第一个词必须是纯材料品名（不要夹 DN/尺寸/城市/信息价）；"
        "②忽略名称中的空格；③禁止把地名（成都/本市等）写进检索词；"
        "④造价站最多 3 词：纯品名 → 品名+口径/型号；电商优先 品牌+型号。"
        "输出 JSON：{\"queries\":[\"词1\",\"词2\"],\"reason\":\"简短中文\"}，queries 最多 3 个。"
    )
    user = json.dumps(
        {**_item_payload(item, platform_id), "seed_queries": seeds[:5]},
        ensure_ascii=False,
    )
    data = _llm_chat_json(settings, system, user, role="search_agent")
    if not data:
        out = seeds[:5], "AI 检索词失败，用规则词"
        _PLAN_CACHE[cache_key] = out
        return out
    raw = data.get("queries") or []
    out_q: list[str] = []
    seen: set[str] = set()
    for q in list(raw) + seeds:
        s = normalize_search_query(str(q or ""))
        if len(s) < 2 or len(s) > 40:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out_q.append(s)
        if len(out_q) >= 6:
            break
    reason = str(data.get("reason") or "AI 改写检索词")[:120]
    # 规则词垫底，保证 AI 失败/词少时预算内仍有兜底
    result = (out_q or seeds[:8], reason)
    _PLAN_CACHE[cache_key] = result
    return result


def rank_candidates(
    *,
    item: Any,
    platform_id: str,
    candidates: list[dict[str, Any]],
    settings: UserSettings | None,
    top_n: int = 8,
    tax_divisor: float = 1.13,
    force_llm: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """
    对列表候选排序。不改价格，只改尝试顺序。
    默认：规则排序（不超报送优先）——零延迟。
    仅 search_agent 且候选 ≥4、规则前两名分差小 时才问 LLM。
    force_llm：电商等场景放宽门槛（候选≥3 即可）。
    """
    if not candidates:
        return [], "无候选"

    rule_ranked = rule_rank_candidates(
        candidates, item=item, tax_divisor=tax_divisor, top_n=max(top_n, 12)
    )

    # 造价站列表交给确定性规则排序；否则每个平台都会各烧一次 Token。
    # 只有调用方明确 force_llm 的电商站才允许 AI 排序。
    use_llm = bool(
        force_llm and len(candidates) >= 3 and _search_agent_on(settings)
    )
    if not use_llm:
        note = "规则排序（名称/型号口径/硬规格优先，价格最后）"
        under_n = sum(1 for c in rule_ranked if c.get("_under_submit") == "under")
        if under_n:
            note = f"规则排序（规格优先；其中≤报送约 {under_n} 条）"
        return rule_ranked[:top_n], note

    slim = []
    for i, c in enumerate(rule_ranked[:12]):
        slim.append(
            {
                "i": i,
                "title": str(c.get("title") or "")[:140],
                "price": c.get("price_tax"),
                "ex_tax_est": estimate_ex_tax(c, tax_divisor),
                "vs_submit": c.get("_under_submit"),
                "unit": str(c.get("unit") or "")[:20],
                "spec_seen": str(c.get("spec_seen") or c.get("detail_text") or "")[:180],
            }
        )
    submit = _item_payload(item, platform_id).get("submit_ex_tax")
    system = (
        "你是工程材料询价核对员。对搜索候选排序，优先规格一致且不含税价不超过报送单价。"
        "禁止编造/改写价格。硬冲突（型号/DN/功率不符）必须低分。"
        f"报送不含税单价={submit}。"
        "同规格时优先 vs_submit=under，其次 near，避免 over。"
        "输出 JSON：{\"order\":[候选i从好到差],\"reject\":[应跳过的i],\"reason\":\"简短中文\"}。"
    )
    user = json.dumps(
        {
            **_item_payload(item, platform_id),
            "candidates": slim,
        },
        ensure_ascii=False,
    )
    data = _llm_chat_json(settings, system, user, role="search_agent")
    if not data:
        return rule_ranked[:top_n], "AI 排序失败，回退规则排序（规格优先）"

    order = data.get("order") or data.get("best") or []
    reject: set[int] = set()
    for x in data.get("reject") or []:
        try:
            reject.add(int(x))
        except Exception:
            pass
    ranked: list[dict[str, Any]] = []
    seen_i: set[int] = set()
    for x in order:
        try:
            i = int(x)
        except Exception:
            continue
        if i in seen_i or i in reject or i < 0 or i >= len(rule_ranked):
            continue
        seen_i.add(i)
        c = dict(rule_ranked[i])
        c["_llm_rank"] = len(ranked) + 1
        ranked.append(c)
    for i, c in enumerate(rule_ranked):
        if i in seen_i or i in reject:
            continue
        ranked.append(c)
    reason = str(data.get("reason") or "AI 候选排序")[:160]
    return ranked[:top_n], f"AI+规则：{reason}"


def _needs_llm_rank(rule_ranked: list[dict[str, Any]]) -> bool:
    """前两名区分度够则不必调 LLM。"""
    if len(rule_ranked) < 4:
        return False
    # 已有明确 under 且规格分不差 → 规则够用
    tops = rule_ranked[:3]
    under_tops = [c for c in tops if c.get("_under_submit") == "under"]
    if len(under_tops) >= 1:
        return False
    scores = [float(c.get("score") or c.get("title_score") or 0) for c in tops]
    if scores and max(scores) - min(scores) >= 15:
        return False
    return True


def _query_anchors(item: Any) -> list[str]:
    """询价表中的关键锚点：型号 / DN / 截面 — AI 改词不得删除。"""
    name = str(getattr(item, "name", "") or "")
    spec = str(getattr(item, "spec", "") or "")
    blob = f"{name} {spec}"
    out: list[str] = []
    for m in re.finditer(r"(?i)(?:DN|φ|Φ)\s*\d{2,3}", blob):
        t = re.sub(r"\s+", "", m.group(0))
        if t not in out:
            out.append(t)
    for m in re.finditer(
        r"(?:DS-|RG-|iDS-|HM-|JB-|MS-|LRS-|GTYQ-|ZN-|WDZN-)[A-Z0-9/\-\.]+"
        r"|[A-Z]{1,6}\d{2,}[A-Z0-9\-_]*",
        blob,
        re.I,
    ):
        t = m.group(0)
        if t not in out and not re.fullmatch(r"(?i)(?:AC|DC)?\d+V?", t):
            out.append(t)
    for m in re.finditer(
        r"(?<!\d)(\d{2,5})\s*[xX×*]\s*(\d{2,5})", blob
    ):
        t = f"{m.group(1)}x{m.group(2)}"
        if t not in out:
            out.append(t)
    return out[:6]


def _sanitize_ai_queries(
    raw: list[str],
    *,
    item: Any,
    tried: list[str],
    max_n: int = 2,
) -> list[str]:
    """
    AI 新检索词约束：
      - 最多 max_n 个
      - 与已尝试词去重
      - 不编造型号（新出现的字母数字型号串必须来自询价表）
      - 不删除关键 DN/型号（若询价表有，尽量补回）
    """
    anchors = _query_anchors(item)
    allowed_models = {a.lower() for a in anchors}
    name = str(getattr(item, "name", "") or "")
    spec = str(getattr(item, "spec", "") or "")
    allowed_blob = f"{name} {spec}".lower()
    tried_l = {normalize_search_query(q).lower() for q in (tried or [])}
    out: list[str] = []
    for q in raw or []:
        s = normalize_search_query(str(q or ""))
        if len(s) < 2 or len(s) > 40:
            continue
        # 拒绝编造：抽出疑似型号，必须在询价表或锚点中出现
        invented = False
        for m in re.finditer(
            r"(?:DS-|RG-|iDS-)[A-Z0-9/\-\.]+|[A-Z]{2,6}\d{2,}[A-Z0-9\-_]*",
            s,
            re.I,
        ):
            tok = m.group(0)
            if tok.lower() not in allowed_blob and tok.lower() not in allowed_models:
                invented = True
                break
        if invented:
            continue
        # 补回被删掉的关键 DN/型号（最多补 1 个锚点，避免词过长）
        for a in anchors[:2]:
            if a.lower() not in s.lower() and a.lower() in allowed_blob:
                # 名称未命中类改词仍应保留口径/型号
                if re.search(r"(?i)DN|φ|Φ|\d{2,5}x\d{2,5}|[A-Z]{2,}\d", a):
                    cand = f"{s} {a}".strip()
                    if len(cand) <= 40:
                        s = normalize_search_query(cand)
                    break
        k = s.lower()
        if k in tried_l:
            continue
        tried_l.add(k)
        out.append(s)
        if len(out) >= max_n:
            break
    return out


def suggest_requery(
    *,
    item: Any,
    platform_id: str,
    tried_queries: list[str],
    page_hint: str,
    settings: UserSettings | None,
    fail_reasons: list[str] | None = None,
) -> tuple[list[str], str]:
    """
    列表为空、全部名称不匹配或规格全错时的改词。
    - 始终先算规则原因感知词（AI 关闭/失败可独立工作）
    - search_agent 开启时再问 LLM（造价站/电商均可用），失败回退规则
    - AI 最多 2 个新词；不编造型号；不删 DN/型号
    """
    from .normalize import rule_requery_from_failures

    name = str(getattr(item, "name", "") or "")
    spec = str(getattr(item, "spec", "") or "")
    brand = str(getattr(item, "brand", "") or "")
    tokens = list(getattr(item, "spec_tokens", None) or [])
    reasons = [str(x) for x in (fail_reasons or []) if x][:12]
    rule_q = rule_requery_from_failures(
        name, spec, brand, list(tried_queries or []), reasons, tokens, max_n=3
    )
    # 规则词也做去重/锚点清洗，最多留给后续合并
    rule_q = _sanitize_ai_queries(
        rule_q, item=item, tried=list(tried_queries or []), max_n=3
    ) or rule_q

    ecommerce = (platform_id or "").strip().lower() in {
        "jd",
        "1688",
        "taobao",
        "tmall",
        "zkh",
        "suning",
    }
    if not ecommerce or not _search_agent_on(settings):
        if rule_q:
            return rule_q[:2], "规则改词（原因感知）"
        return [], "无新检索词（规则路径）"

    anchors = _query_anchors(item)
    system = (
        "你是工程造价材料询价的搜索助理（search_agent）。"
        "上一轮搜索无合适结果：可能列表为空、品名不匹配、或规格全错。"
        "请给出最多 2 个新检索词。"
        "硬约束："
        "1) 禁止编造询价表中不存在的型号；"
        "2) 询价表若有 DN/φ/截面/型号，新词不得删除这些关键身份；"
        "3) 禁止编造或改写价格；"
        "4) 可缩短装饰词、换行业常用品名叫法；"
        "5) 不要与 tried_queries 重复。"
        f"关键锚点（必须保留在至少一个新词中）：{anchors or '无'}。"
        "输出 JSON：{\"queries\":[\"词1\",\"词2\"],\"reason\":\"简短中文\"}。"
    )
    user = json.dumps(
        {
            **_item_payload(item, platform_id),
            "tried_queries": (tried_queries or [])[:8],
            "fail_reasons": reasons[:8],
            "must_keep_anchors": anchors,
            "page_hint": (page_hint or "")[:500],
            "rule_suggestions": rule_q[:3],
        },
        ensure_ascii=False,
    )
    data = _llm_chat_json(settings, system, user, role="search_agent")
    if not data:
        if rule_q:
            return rule_q[:2], "AI改词失败，回退规则改词"
        return [], "AI改词失败"

    ai_raw = [str(q) for q in (data.get("queries") or [])]
    out = _sanitize_ai_queries(
        ai_raw, item=item, tried=list(tried_queries or []), max_n=2
    )
    # 规则词垫底补足（仍最多 2）
    if len(out) < 2:
        for q in rule_q:
            if len(out) >= 2:
                break
            if q.lower() not in {x.lower() for x in out} and q.lower() not in {
                t.lower() for t in (tried_queries or [])
            }:
                out.append(q)
    if not out and rule_q:
        return rule_q[:2], "AI无合规新词，回退规则改词"
    reason = str(data.get("reason") or "AI+规则改词")[:120]
    return out[:2], f"search_agent：{reason}"


def collect_match_fail_reasons(attempts: list[dict[str, Any]], *, platform_id: str = "") -> list[str]:
    """从 attempts 抽取稳定的失败原因标签，供 requery 使用。"""
    reasons: list[str] = []
    for a in attempts or []:
        if platform_id and str(a.get("platform") or "") not in ("", platform_id):
            continue
        detail = str(a.get("match_detail") or a.get("status") or "")
        if not detail:
            continue
        if "名称未命中" in detail:
            reasons.append("名称未命中")
        if "型号" in detail and ("冲突" in detail or "页面型号" in detail):
            reasons.append("型号错误")
        if re.search(r"(?i)DN|口径|通径|φ|直径", detail) and (
            "冲突" in detail or "页面" in detail
        ):
            reasons.append("DN错误")
        if "尺寸" in detail and ("冲突" in detail or "页面" in detail):
            reasons.append("尺寸错误")
        if "规格缺少" in detail or "缺少" in detail:
            reasons.append("规格缺失")
        if "规格冲突" in detail and "规格冲突" not in reasons:
            reasons.append(detail[:80])
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out[:10]
