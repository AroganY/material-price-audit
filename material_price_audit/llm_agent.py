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

from .normalize import build_platform_queries
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
    if pid in ("guangcai", "lingcai", "huixun", "yize"):
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
    规则排序（零 LLM 成本）：
    1) 标题含截面尺寸数字优先（同型号多规格）
    2) 不超报送不含税优先
    3) 列表分/标题分高者优先
    4) 同档位价低优先（审价友好）
    """
    if not candidates:
        return []
    submit = None
    try:
        s = getattr(item, "submit", None) if item is not None else None
        submit = float(s) if s is not None else None
    except Exception:
        submit = None

    dim_nums: list[str] = []
    try:
        name = str(getattr(item, "name", "") or "")
        spec = str(getattr(item, "spec", "") or "")
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
            str(getattr(item, "name", "") or ""),
            re.I,
        )
        if m2:
            model = m2.group(0)
    except Exception:
        model = ""

    tier = {"under": 0, "near": 1, "unknown": 2, "over": 3}

    def sort_key(c: dict[str, Any]) -> tuple:
        blob = f"{c.get('title') or ''} {c.get('spec_seen') or ''} {c.get('detail_text') or ''}"
        dim_hit = sum(1 for n in dim_nums if n and n in blob)
        model_hit = 1 if model and model.lower() in blob.lower() else 0
        # 型号后「型」可忽略
        if not model_hit and model:
            mcore = re.sub(r"型$", "", model, flags=re.I)
            if mcore and mcore.lower() in blob.lower():
                model_hit = 1
        flag = under_submit_flag(c, submit, tax_divisor)
        score = float(c.get("score") or c.get("title_score") or c.get("match_score") or 0)
        ex = estimate_ex_tax(c, tax_divisor)
        price_key = ex if ex is not None else 1e18
        # 尺寸命中最优先，再型号，再报送，再列表分
        return (-dim_hit, -model_hit, tier.get(flag, 2), -score, price_key)

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
    seeds = [q for q in (seed_queries or []) if q and str(q).strip()]
    if not seeds:
        seeds = build_platform_queries(
            platform_id,
            getattr(item, "name", "") or "",
            getattr(item, "spec", "") or "",
            getattr(item, "brand", "") or "",
            list(getattr(item, "spec_tokens", None) or []),
        )
    # 默认：规则足够快且稳；LLM 改词成本高、收益有限
    if not force_llm:
        return seeds[:5], "规则检索词"

    if not _search_agent_on(settings):
        return seeds[:5], "规则检索词"

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
        "造价信息站优先品名+关键规格；电商优先 品牌+型号。"
        "输出 JSON：{\"queries\":[\"词1\",\"词2\"],\"reason\":\"简短中文\"}，queries 最多 3 个。"
    )
    user = json.dumps(
        {**_item_payload(item, platform_id), "seed_queries": seeds[:5]},
        ensure_ascii=False,
    )
    data = _llm_chat_json(settings, system, user)
    if not data:
        out = seeds[:5], "AI 检索词失败，用规则词"
        _PLAN_CACHE[cache_key] = out
        return out
    raw = data.get("queries") or []
    out_q: list[str] = []
    seen: set[str] = set()
    for q in list(raw) + seeds:
        s = re.sub(r"\s+", " ", str(q or "")).strip()
        if len(s) < 2 or len(s) > 40:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out_q.append(s)
        if len(out_q) >= 3:
            break
    reason = str(data.get("reason") or "AI 改写检索词")[:120]
    result = (out_q or seeds[:5], reason)
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
) -> tuple[list[dict[str, Any]], str]:
    """
    对列表候选排序。不改价格，只改尝试顺序。
    默认：规则排序（不超报送优先）——零延迟。
    仅 search_agent 且候选 ≥4、规则前两名分差小 时才问 LLM。
    """
    if not candidates:
        return [], "无候选"

    rule_ranked = rule_rank_candidates(
        candidates, item=item, tax_divisor=tax_divisor, top_n=max(top_n, 12)
    )

    use_llm = (
        _search_agent_on(settings)
        and len(candidates) >= 4
        and _needs_llm_rank(rule_ranked)
    )
    if not use_llm:
        note = "规则排序（优先不超报送）"
        under_n = sum(1 for c in rule_ranked if c.get("_under_submit") == "under")
        if under_n:
            note = f"规则排序（不超报送优先，{under_n} 条≤报送）"
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
    data = _llm_chat_json(settings, system, user)
    if not data:
        return rule_ranked[:top_n], "AI 排序失败，用规则（含不超报送）"

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


def suggest_requery(
    *,
    item: Any,
    platform_id: str,
    tried_queries: list[str],
    page_hint: str,
    settings: UserSettings | None,
) -> tuple[list[str], str]:
    """列表为空或全不匹配时，让模型给下一组搜索词（最多 2 个，控延迟）。"""
    if not _search_agent_on(settings):
        return [], "未启用 search_agent"
    system = (
        "你是材料询价搜索专家。上一轮搜索无合适结果，请换更可能命中的关键词。"
        "可缩短品名、去掉装饰编号、换成行业常用叫法；不要编造型号。"
        "输出 JSON：{\"queries\":[\"...\"],\"reason\":\"...\"}，最多 2 个词。"
    )
    user = json.dumps(
        {
            **_item_payload(item, platform_id),
            "tried_queries": tried_queries[:6],
            "page_hint": (page_hint or "")[:500],
        },
        ensure_ascii=False,
    )
    data = _llm_chat_json(settings, system, user)
    if not data:
        return [], "AI 改词失败"
    out = []
    seen = set(q.lower() for q in tried_queries)
    for q in data.get("queries") or []:
        s = re.sub(r"\s+", " ", str(q or "")).strip()
        if len(s) < 2 or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
        if len(out) >= 2:
            break
    return out, str(data.get("reason") or "AI 建议改词")[:120]
