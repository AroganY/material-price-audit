"""
任务级统计与失败归因（按 run_id 隔离）。

不参与定价；只根据 QuoteSet.attempts / status 汇总「匹配率低在哪里」。
兼容旧 evidence：无 run_id / funnel 时也能从 results 推导。
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from .models import CanonicalItem, QuoteSet

# 统一状态（不再把 full_k/partial 覆盖成 verified）
CANONICAL_STATUSES = frozenset(
    {"full_k", "partial", "need_review", "no_match", "error", "skipped"}
)

# Web「为什么没查到」归因标签
FAIL_REASONS = (
    "平台没有结果",
    "名称不匹配",
    "规格冲突",
    "规格证据缺失",
    "没有数字价",
    "未登录/无会员",
    "限流",
    "验证码",
    "其它",
)


def new_run_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"run-{ts}-{uuid.uuid4().hex[:8]}"


def normalize_status(status: str | None) -> str:
    """旧 verified → 尽量还原；未知归 no_match。"""
    st = str(status or "no_match").strip().lower()
    if st == "verified":
        # 旧 evidence 把 full_k/partial 都写成 verified；调用方应再看 multi_status
        return "full_k"
    if st in CANONICAL_STATUSES:
        return st
    if st in ("fail", "failed"):
        return "error"
    return "no_match"


def resolve_item_status(row: dict[str, Any]) -> str:
    """从 evidence 行解析规范状态（优先 multi_status）。"""
    multi = str(row.get("multi_status") or "").strip().lower()
    if multi in CANONICAL_STATUSES:
        return multi
    st = str(row.get("status") or "").strip().lower()
    if st == "verified":
        # 有 quotes 条数暗示
        nq = 0
        try:
            nq = len(row.get("quotes") or [])
            if isinstance(row.get("quotes"), list) and row["quotes"] and isinstance(
                row["quotes"][0], dict
            ):
                nq = len(row["quotes"])
            elif isinstance(row.get("quotes"), int):
                nq = int(row["quotes"])
        except Exception:
            nq = 0
        if nq >= 3:
            return "full_k"
        if nq >= 1:
            return "partial"
        return "full_k"
    return normalize_status(st)


def classify_fail_reason(qset: QuoteSet | None, *, k: int = 3) -> str:
    """
    单条材料主失败原因（有正式价时返回空串）。
    优先级：登录/会员/限流/验证码 → 无列表 → 名称 → 规格冲突 → 缺证据 → 无数字价。
    """
    if qset is None:
        return "其它"
    st = normalize_status(qset.status)
    if st in ("full_k", "partial") and qset.quotes:
        return ""
    if st == "skipped":
        return ""
    if st == "error":
        return "其它"

    attempts = list(qset.attempts or [])
    statuses = [str(a.get("status") or "") for a in attempts]
    details = [str(a.get("match_detail") or "") for a in attempts]
    blob = " ".join(statuses + details + [qset.error or ""])

    if any("rate_limited" in s for s in statuses) or "限流" in blob:
        return "限流"
    if any("captcha" in s for s in statuses) or "验证码" in blob:
        return "验证码"
    if any(
        x in blob
        for x in (
            "need_login",
            "no_membership",
            "not_logged_in",
            "无会员",
            "未登录",
        )
    ):
        return "未登录/无会员"

    # 有 match 尝试记录
    has_match_attempt = any(
        a.get("match_detail") or a.get("match_outcome") or a.get("bucket")
        for a in attempts
    )
    empty_only = attempts and all(
        (a.get("status") in ("empty_page", "no_list", "ok") and not a.get("match_detail"))
        or str(a.get("status") or "") in ("empty_page", "no_list")
        or (a.get("n") == 0 and not a.get("match_detail"))
        for a in attempts
    )
    # 简化：所有带 n 的都是 0 且无 match_detail
    if attempts and not has_match_attempt:
        if any(
            s in ("empty_page", "no_list", "") or s.startswith("error")
            for s in statuses
        ) or empty_only:
            return "平台没有结果"

    name_n = sum(1 for d in details if "名称未命中" in d)
    conflict_n = sum(
        1
        for d in details
        if "规格冲突" in d or ("冲突" in d and "名称未命中" not in d)
    )
    missing_n = sum(1 for d in details if "规格缺少" in d or "缺少" in d)
    no_price_n = sum(
        1
        for a in attempts
        if (
            "无数字价" in str(a.get("match_detail") or "")
            or "见价需会员" in str(a.get("match_detail") or "")
            or a.get("price_hidden_ok")
            or (
                a.get("match_detail")
                and (
                    a.get("price_tax") in (None, 0, 0.0, 0.01)
                    or (
                        isinstance(a.get("price_tax"), (int, float))
                        and float(a.get("price_tax") or 0) <= 0.05
                    )
                )
            )
        )
    )

    if name_n and name_n >= max(conflict_n, missing_n, 1):
        return "名称不匹配"
    if conflict_n:
        return "规格冲突"
    if missing_n and st == "need_review":
        return "规格证据缺失"
    if missing_n and not qset.quotes:
        return "规格证据缺失"
    if no_price_n or "无数字价" in blob or "见价需会员" in blob:
        return "没有数字价"
    if not has_match_attempt:
        return "平台没有结果"
    if st == "need_review":
        return "规格证据缺失"
    return "其它"


def _attempt_has_candidates(a: dict) -> bool:
    if a.get("match_detail") or a.get("bucket") or a.get("title"):
        return True
    try:
        n = int(a.get("n") or 0)
        if n > 0:
            return True
    except Exception:
        pass
    st = str(a.get("status") or "")
    return st == "ok" and bool(a.get("url") or a.get("price_tax"))


def build_platform_stats(
    quote_map: dict[str, QuoteSet],
    *,
    item_ids: set[str] | None = None,
) -> dict[str, dict[str, int]]:
    """
    每平台：查询词数、返回候选、名称拒绝、规格拒绝、价格隐藏、正式命中。
    """
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "queries": 0,
            "candidates": 0,
            "name_reject": 0,
            "spec_reject": 0,
            "price_hidden": 0,
            "formal_hits": 0,
        }
    )
    seen_query: dict[str, set[str]] = defaultdict(set)

    for iid, qset in (quote_map or {}).items():
        if item_ids is not None and iid not in item_ids:
            continue
        for q in qset.quotes or []:
            pid = str(q.platform or "") or "?"
            stats[pid]["formal_hits"] += 1
        for a in qset.attempts or []:
            pid = str(a.get("platform") or "") or "?"
            qtext = str(a.get("query") or "").strip()
            if qtext:
                k = qtext.lower()
                if k not in seen_query[pid]:
                    seen_query[pid].add(k)
                    stats[pid]["queries"] += 1
            if _attempt_has_candidates(a):
                stats[pid]["candidates"] += 1
            detail = str(a.get("match_detail") or "")
            if "名称未命中" in detail:
                stats[pid]["name_reject"] += 1
            if "规格冲突" in detail or (
                "冲突" in detail and "名称未命中" not in detail
            ):
                stats[pid]["spec_reject"] += 1
            if (
                "无数字价" in detail
                or "见价需会员" in detail
                or a.get("price_hidden_ok")
            ):
                stats[pid]["price_hidden"] += 1
            # 列表空
            st = str(a.get("status") or "")
            if st in ("empty_page", "no_list") or a.get("n") == 0:
                pass  # 计入 queries 即可

    return {pid: dict(v) for pid, v in sorted(stats.items())}


def build_funnel(
    items: list[CanonicalItem],
    quote_map: dict[str, QuoteSet],
    *,
    k: int = 3,
) -> dict[str, Any]:
    """询价漏斗 + 失败原因分布（仅统计给定 items）。"""
    total = len(items)
    searched = 0
    has_candidates = 0
    name_hit = 0
    spec_full = 0
    has_price = 0
    full_k = 0
    partial = 0
    need_review = 0
    no_match = 0
    error = 0
    skipped = 0
    fail_counts: Counter[str] = Counter()

    for it in items:
        qset = quote_map.get(it.id)
        st = normalize_status(qset.status if qset else "no_match")
        if st == "skipped" or (
            qset
            and any(
                str(a.get("status") or "").startswith("skip")
                for a in (qset.attempts or [])
            )
            and not qset.quotes
            and st not in ("full_k", "partial", "need_review")
        ):
            # 明确 skipped 状态
            if st == "skipped":
                skipped += 1
                continue

        if qset and (qset.attempts or qset.quotes or qset.review_candidates):
            searched += 1
        elif qset and st not in ("no_match",):
            searched += 1

        if not qset:
            no_match += 1
            fail_counts["平台没有结果"] += 1
            continue

        attempts = qset.attempts or []
        if any(_attempt_has_candidates(a) for a in attempts) or qset.quotes or qset.review_candidates:
            has_candidates += 1

        if any(
            a.get("name_hit")
            or (a.get("match_detail") and "名称未命中" not in str(a.get("match_detail")))
            and a.get("match_detail")
            for a in attempts
        ) or qset.quotes or qset.review_candidates:
            # 有正式价/待核 或 attempt 名称命中
            if (
                qset.quotes
                or qset.review_candidates
                or any(a.get("name_hit") for a in attempts)
                or any(
                    "名称命中" in str(a.get("match_detail") or "")
                    for a in attempts
                )
            ):
                name_hit += 1

        if qset.quotes:
            spec_full += 1
            if any(
                q.price is not None and float(q.price) > 0.05 for q in qset.quotes
            ):
                has_price += 1

        if st == "full_k":
            full_k += 1
        elif st == "partial":
            partial += 1
        elif st == "need_review":
            need_review += 1
            reason = classify_fail_reason(qset, k=k) or "规格证据缺失"
            fail_counts[reason] += 1
        elif st == "error":
            error += 1
            fail_counts["其它"] += 1
        elif st == "skipped":
            skipped += 1
        else:
            no_match += 1
            reason = classify_fail_reason(qset, k=k) or "其它"
            fail_counts[reason] += 1

    return {
        "items_total": total,
        "searched": searched,
        "has_candidates": has_candidates,
        "name_matched": name_hit,
        "spec_full_match": spec_full,
        "has_real_price": has_price,
        "full_k": full_k,
        "partial": partial,
        "need_review": need_review,
        "no_match": no_match,
        "error": error,
        "skipped": skipped,
        "fail_reason_counts": dict(fail_counts),
        "k": k,
    }


def item_diagnostics(qset: QuoteSet | None, *, k: int = 3) -> dict[str, Any]:
    """单条材料诊断：状态 + 主因 + 次要提示。"""
    if qset is None:
        return {
            "status": "no_match",
            "fail_reason": "平台没有结果",
            "hint": "未执行或无结果",
        }
    st = normalize_status(qset.status)
    reason = classify_fail_reason(qset, k=k)
    return {
        "status": st,
        "fail_reason": reason,
        "hint": qset.error or reason or "",
        "n_quotes": len(qset.quotes or []),
        "n_review": len(qset.review_candidates or []),
        "n_attempts": len(qset.attempts or []),
    }


def filter_quote_map_for_items(
    quote_map: dict[str, QuoteSet],
    items: list[CanonicalItem],
) -> dict[str, QuoteSet]:
    """只保留当前材料行（相同工作簿本次范围），避免混入其它任务。"""
    ids = {it.id for it in items}
    return {k: v for k, v in (quote_map or {}).items() if k in ids}


def load_existing_for_continue(
    quote_map: dict[str, QuoteSet],
    items: list[CanonicalItem],
    *,
    meta: dict[str, Any] | None,
    input_path: str,
) -> dict[str, QuoteSet]:
    """
    继续询价：仅读取相同工作簿 + 相同材料 id 的历史。
    工作簿路径不一致则返回空（不复用错簿结果）。
    """
    meta = meta or {}
    prev_input = str(meta.get("input_path") or meta.get("workbook") or "").strip()
    cur = str(input_path or "").strip()
    if prev_input and cur:
        # 规范化比较文件名+父目录尾段
        try:
            from pathlib import Path

            if Path(prev_input).resolve() != Path(cur).resolve():
                # 允许仅文件名相同且在 data/input 下
                if Path(prev_input).name != Path(cur).name:
                    return {}
        except Exception:
            if prev_input != cur:
                return {}
    return filter_quote_map_for_items(quote_map, items)


def build_run_meta(
    *,
    run_id: str,
    input_path: str,
    platforms: list[str],
    k: int,
    match_mode: str,
    funnel: dict[str, Any] | None = None,
    platform_stats: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "run_id": run_id,
        "input_path": input_path or "",
        "platforms": list(platforms or []),
        "k": int(k or 3),
        "mode": match_mode or "practical",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if funnel:
        meta["funnel"] = funnel
    if platform_stats:
        meta["platform_stats"] = platform_stats
    if extra:
        meta.update(extra)
    return meta
