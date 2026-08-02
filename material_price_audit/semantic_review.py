"""Optional LLM review for semantic gray areas; never sources or invents prices.

两条路径：
  1) 品名同义（名称未命中）：任意材料，不靠写死词表
  2) 规格灰区（outcome=review 且 missing 无硬数字）：同义描述

硬规格（型号/功率/DN/尺寸数字）永远不由 LLM 改判。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .matching import (
    MatchResult,
    name_search_core,
    soft_product_name_equivalent,
    strict_name_spec_match,
)
from .schema_map import _llm_chat_json
from .settings_store import UserSettings


def _cache_path(root: Path, payload: dict[str, Any], *, prefix: str = "match-review") -> Path:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    key = hashlib.sha256(raw).hexdigest()[:28]
    return root / "data" / "mapping-cache" / prefix / f"{key}.json"


# AI 绝对不可改判/补全的硬规格关键字
_HARD_SPEC_MARKERS = re.compile(
    r"(?<![品])\d|型号|尺寸|口径|通径|电压|功率|色温|角度|防护等级|端口|通道|"
    r"压力|流量|容量|单位|DN|φ|Φ|PN|IP\s*\d",
    re.I,
)

# 单独一个这些字相同，不能算「品名语义灰区」；否则阀/管/器会把整个列表送给 AI。
_GENERIC_NAME_CHARS = frozenset("器阀管灯箱机泵门板套线件类型式")

_NAME_CATEGORY_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("flange", ("法兰",)),
    ("valve", ("阀门", "止回阀", "单向阀", "闸阀", "截止阀", "球阀", "蝶阀")),
    ("light", ("灯具", "线型灯", "线条灯", "地埋灯", "埋地灯", "射灯")),
    ("pump", ("水泵", "泵组", "离心泵")),
    ("pipe", ("钢管", "给水管", "排水管", "铸铁管", "管材")),
    ("rebar", ("钢筋", "圆钢", "螺纹钢", "盘螺")),
    ("cable", ("电缆", "电线", "线缆")),
    ("controller", ("控制器", "分控器", "主控器")),
)


def obvious_name_category_conflict(query_name: str, page_title: str) -> bool:
    """仅拦截高确定性跨品类；其它低字面相似仍交给批量语义判断。"""
    q = re.sub(r"\s+", "", query_name or "")
    t = re.sub(r"\s+", "", page_title or "")

    def _cat(text: str) -> str:
        for cat, terms in _NAME_CATEGORY_TERMS:
            if any(term in text for term in terms):
                return cat
        return ""

    cq, ct = _cat(q), _cat(t)
    return bool(cq and ct and cq != ct)


@dataclass
class MatchReviewLimiter:
    """单条材料的语义复核熔断器，只统计真实 API 请求，不统计缓存。"""

    max_api_calls: int = 2
    api_calls: int = 0
    consecutive_rejects: int = 0
    stopped_reason: str = ""
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False, compare=False
    )

    def allow_api(self) -> bool:
        with self._lock:
            if self.stopped_reason:
                return False
            if self.api_calls >= max(1, int(self.max_api_calls or 1)):
                self.stopped_reason = f"单条材料 AI 复核已达 {self.max_api_calls} 次上限"
                return False
            return True

    def reserve_api_call(self) -> bool:
        # 并行平台共用同一个 limiter，检查+预留必须原子。
        with self._lock:
            if self.stopped_reason:
                return False
            if self.api_calls >= max(1, int(self.max_api_calls or 1)):
                self.stopped_reason = f"单条材料 AI 复核已达 {self.max_api_calls} 次上限"
                return False
            self.api_calls += 1
            return True

    def record_decision(self, decision: str) -> None:
        with self._lock:
            d = (decision or "").lower().strip()
            if d in ("different", "uncertain", "no", "conflict", "insufficient", ""):
                self.consecutive_rejects += 1
            else:
                self.consecutive_rejects = 0
            # 连续无效复核与调用上限任一命中即熔断本条；规则匹配仍继续。
            if self.consecutive_rejects >= max(1, int(self.max_api_calls or 1)):
                self.stopped_reason = (
                    f"连续 {self.consecutive_rejects} 次非同义/不确定，停止本条 AI 复核"
                )


def is_name_review_gray_candidate(query_name: str, page_title: str) -> bool:
    """仅让名称足够接近的候选进入 AI；明显不同直接规则拒绝。"""
    q = name_search_core(query_name or "") or (query_name or "")
    t = name_search_core(page_title or "") or (page_title or "")
    q = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", q).lower()
    t = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", t).lower()
    if not q or not t:
        return False
    if q in t or t in q:
        return True

    q_cn = [c for c in q if "\u4e00" <= c <= "\u9fff"]
    t_cn = [c for c in t if "\u4e00" <= c <= "\u9fff"]
    if q_cn and t_cn:
        qset, tset = set(q_cn), set(t_cn)
        meaningful_common = (qset & tset) - _GENERIC_NAME_CHARS
        common_n = len(qset & tset)
        dice = (2.0 * common_n) / max(1, len(qset) + len(tset))
        q_bigrams = {"".join(q_cn[i : i + 2]) for i in range(len(q_cn) - 1)}
        t_bigrams = {"".join(t_cn[i : i + 2]) for i in range(len(t_cn) - 1)}
        # 至少有一个非泛化字，且整体字符或相邻二字有明显重合。
        return bool(meaningful_common) and (
            dice >= 0.45 or bool(q_bigrams & t_bigrams)
        )

    # 英文/型号品名只接受较强的共同片段；短 token 不足以触发模型。
    q_parts = {x for x in re.findall(r"[a-z0-9]{3,}", q) if len(x) >= 3}
    t_parts = {x for x in re.findall(r"[a-z0-9]{3,}", t) if len(x) >= 3}
    return bool(q_parts & t_parts)


def _semantic_only(missing: tuple[str, ...]) -> bool:
    """型号、数字、单位等硬字段禁止由 LLM 覆盖。品名：xxx 允许。"""
    if not missing:
        return True
    for x in missing:
        s = x or ""
        if re.match(r"^品名[：:]", s):
            continue
        if _HARD_SPEC_MARKERS.search(s):
            return False
    return True


def _hard_conflicts_present(mr: MatchResult) -> bool:
    """是否存在硬规格冲突（非纯名称未命中）。"""
    from .matching import has_hard_spec_conflict

    if has_hard_spec_conflict(mr):
        return True
    for c in mr.conflicts or ():
        s = str(c)
        if "名称未命中" in s:
            continue
        if _HARD_SPEC_MARKERS.search(s) or "冲突" in s:
            return True
    return False


def _llm_enabled_for_review(settings: UserSettings | None) -> bool:
    if not settings:
        return False
    if not getattr(settings, "llm_enabled", False):
        return False
    use = getattr(settings, "llm_use_for", None) or []
    return "match_review" in use


def _norm_title_key(title: str) -> str:
    from .name_aliases import normalize_name_key

    return normalize_name_key(title or "")


def resolve_name_without_ai(
    inquiry_name: str,
    candidate_name: str,
    *,
    root: Path | None,
    page_text: str = "",
) -> tuple[str, str, str]:
    """
    第一/二级：规则 + 本地库。
    返回 (decision, source, note)
      decision: same|different|unknown
      source: rule|local_alias|local_negative|""
    """
    from .name_aliases import lookup_name_relation

    if not (inquiry_name or "").strip() or not (candidate_name or "").strip():
        return "unknown", "", ""
    if soft_product_name_equivalent(inquiry_name, candidate_name, page_text):
        return "same", "rule", "规则同物（包含/字序/装饰）"
    rel, note = lookup_name_relation(inquiry_name, candidate_name, root)
    if rel == "same":
        return "same", "local_alias", note or "本地同义库"
    if rel == "different":
        return "different", "local_negative", note or "本地负向映射"
    return "unknown", "", ""


def apply_name_same_then_spec(
    *,
    item: Any,
    title: str,
    evidence_text: str,
    note: str,
    source_tag: str,
) -> MatchResult:
    """名称确认同物后必须重跑 strict_name_spec_match。"""
    inject = f"{getattr(item, 'name', '')} {title} {evidence_text}"
    rerun = strict_name_spec_match(item, title, inject)
    if _hard_conflicts_present(rerun) or (
        rerun.outcome == "reject" and rerun.conflicts
    ):
        return MatchResult(
            False,
            rerun.score,
            rerun.required_hit,
            rerun.required_total,
            f"{note}；规格仍冲突/不满足：{rerun.detail}",
            "reject",
            "reject",
            missing=rerun.missing,
            conflicts=rerun.conflicts,
            evidence=rerun.evidence + (note, source_tag),
            llm_invoked=False,
            llm_decision=source_tag,
        )
    return MatchResult(
        rerun.ok,
        max(rerun.score, 0.9) if rerun.ok else rerun.score,
        rerun.required_hit,
        rerun.required_total,
        f"{note}；规格侧：{rerun.detail}",
        "strict" if rerun.ok else rerun.level,
        "accept" if rerun.ok else rerun.outcome,
        missing=rerun.missing,
        conflicts=rerun.conflicts,
        evidence=rerun.evidence + (note, source_tag),
        llm_invoked=False,
        llm_decision=source_tag,
    )


def batch_judge_product_names(
    *,
    inquiry_name: str,
    candidate_names: list[str],
    settings: UserSettings | None,
    root: Path | None,
    limiter: MatchReviewLimiter | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """
    第三级：对去重后的候选名称批量 AI 判断（每条材料最多 1 次 API）。

    返回:
      decisions: {norm_key: {decision, confidence, reason, candidate_name, source}}
      meta: llm_* 与日志用字段
    """
    from .name_aliases import confirm_same_names, normalize_name_key

    meta: dict[str, Any] = {
        "llm_invoked": False,
        "llm_from_cache": False,
        "llm_api_called": False,
        "llm_budget_blocked": False,
        "batch_size": 0,
        "n_unique": 0,
    }
    decisions: dict[str, dict[str, Any]] = {}
    # 规范化去重，最多 5 个
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in candidate_names or []:
        t = re.sub(r"\s+", " ", str(raw or "")).strip()
        if len(t) < 2:
            continue
        k = normalize_name_key(t)
        if not k or k in seen:
            continue
        seen.add(k)
        ordered.append(t)
        if len(ordered) >= 5:
            break
    meta["n_unique"] = len(ordered)
    meta["batch_size"] = len(ordered)
    if not ordered:
        return decisions, meta
    if not _llm_enabled_for_review(settings) or not root:
        return decisions, meta

    cache_payload = {
        "task": "product_name_batch_v1",
        "inquiry_name": normalize_name_key(inquiry_name),
        "candidates": [normalize_name_key(x) for x in ordered],
    }
    cache = _cache_path(Path(root), cache_payload, prefix="name-batch")
    data: dict[str, Any] | None = None
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            meta["llm_from_cache"] = bool(data)
            meta["llm_invoked"] = True
        except Exception:
            data = None
    if data is None:
        if limiter is not None and not limiter.reserve_api_call():
            meta["llm_budget_blocked"] = True
            return decisions, meta
        system = (
            "你是工程造价/材料采购领域的品名同义判定器。"
            "只判断「是不是同一种材料实体」，不要管规格口径（DN/尺寸留给规则匹配）。"
            "规则："
            "1) 名称中的空格忽略：'薄 壁 不 锈 钢 管' = '薄壁不锈钢管'；"
            "2) 忽略地名/城市/信息价字样（成都/本市/信息价等与是否同物无关）；"
            "3) decision 只能 same/possible/different："
            "same=确定同物异名；possible=可能同物需人工；different=不同产品；"
            "4) 阀门≠法兰，管≠阀，泵≠接合器（除非名称明确同类）；"
            "5) 禁止编造规格/型号/价格。"
            "输出 JSON："
            '{"inquiry_name":"...","candidates":[{"candidate_name":"...","decision":"same|possible|different",'
            '"confidence":0.0,"canonical_name":"...","reason":"..."}]}'
        )
        # 送给模型前折叠空格，避免被空格拆字误导
        try:
            from .matching import collapse_cjk_spaces, strip_geo_noise

            inq_for_llm = strip_geo_noise(collapse_cjk_spaces(inquiry_name))
            cands_for_llm = [
                strip_geo_noise(collapse_cjk_spaces(x)) for x in ordered
            ]
        except Exception:
            inq_for_llm = inquiry_name
            cands_for_llm = list(ordered)
        user = json.dumps(
            {
                "inquiry_name": inq_for_llm,
                "candidates": [{"candidate_name": x} for x in cands_for_llm],
                "note": "ignore_spaces_and_geo=true; spec_not_for_name_decision=true",
            },
            ensure_ascii=False,
        )
        meta["llm_api_called"] = True
        meta["llm_invoked"] = True
        data = _llm_chat_json(settings, system, user, role="match_review")
        if data:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass
    if not data:
        return decisions, meta

    rows = data.get("candidates") or []
    if not isinstance(rows, list):
        rows = []
    by_norm: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cn = str(row.get("candidate_name") or "")
        by_norm[normalize_name_key(cn)] = row
    for t in ordered:
        k = normalize_name_key(t)
        row = by_norm.get(k) or {}
        dec = str(row.get("decision") or "different").lower().strip()
        if dec not in ("same", "possible", "different"):
            # 兼容旧 same/uncertain
            if dec in ("equivalent", "synonym", "yes"):
                dec = "same"
            elif dec in ("uncertain", "maybe"):
                dec = "possible"
            else:
                dec = "different"
        conf = float(row.get("confidence") or 0)
        reason = str(row.get("reason") or "")[:200]
        # same 需 conf>=0.90
        if dec == "same" and conf < 0.90:
            dec = "possible"
        decisions[k] = {
            "candidate_name": t,
            "decision": dec,
            "confidence": conf,
            "reason": reason,
            "canonical_name": str(row.get("canonical_name") or "")[:80],
            "source": "ai_batch",
        }
        if limiter is not None and meta.get("llm_api_called"):
            limiter.record_decision(dec if dec != "possible" else "uncertain")
        # AI same 高置信写入本地库，便于下次 Token=0
        if dec == "same" and conf >= 0.90 and root:
            try:
                confirm_same_names(
                    inquiry_name,
                    t,
                    Path(root),
                    source="ai_confirmed",
                    confidence=conf,
                )
            except Exception:
                pass
    return decisions, meta


def prepare_item_name_decisions(
    *,
    inquiry_name: str,
    candidate_titles: list[str],
    settings: UserSettings | None,
    root: Path | None,
    limiter: MatchReviewLimiter | None = None,
    log: Any = None,
) -> dict[str, dict[str, Any]]:
    """
    对一条材料的全部候选标题做三级品名判决（规则→本地→批量AI≤1次）。
    返回 {norm_title: {decision, source, note, confidence}}
    """
    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    out: dict[str, dict[str, Any]] = {}
    need_ai: list[str] = []
    weak_unknown: list[str] = []
    # 去重保序
    seen: set[str] = set()
    unique_titles: list[str] = []
    for t in candidate_titles or []:
        tt = re.sub(r"\s+", " ", str(t or "")).strip()
        if len(tt) < 2:
            continue
        k = _norm_title_key(tt)
        if not k or k in seen:
            continue
        seen.add(k)
        unique_titles.append(tt)

    for t in unique_titles:
        k = _norm_title_key(t)
        dec, src, note = resolve_name_without_ai(inquiry_name, t, root=root)
        if dec == "same":
            tag = "规则" if src == "rule" else "人工映射"
            _log(f"  [名称·{tag}] 「{inquiry_name}」≈「{t}」同物，Token=0")
            out[k] = {
                "decision": "same",
                "source": src,
                "note": note,
                "confidence": 1.0,
                "candidate_name": t,
            }
            continue
        if dec == "different":
            _log(f"  [名称·负向映射] 「{inquiry_name}」≠「{t}」直接拒绝")
            out[k] = {
                "decision": "different",
                "source": src,
                "note": note,
                "confidence": 1.0,
                "candidate_name": t,
            }
            continue
        # 灰区才进 AI；明显不同不浪费
        if is_name_review_gray_candidate(inquiry_name, t):
            need_ai.append(t)
        elif obvious_name_category_conflict(inquiry_name, t):
            out[k] = {
                "decision": "different",
                "source": "rule_category_conflict",
                "note": "规则预筛：明确跨产品类别",
                "confidence": 1.0,
                "candidate_name": t,
            }
        else:
            # 搜索结果前排候选可能是“完全不同字面的行业同义词”。
            # 不再一刀切 different，留少量名额给同一次批量 AI；仍不增加 API 次数。
            weak_unknown.append(t)

    # 强灰区优先，再用剩余批量槽位覆盖字面低相似同义词。
    remaining = max(0, 5 - len(need_ai))
    need_ai.extend(weak_unknown[:remaining])
    for t in weak_unknown[remaining:]:
        k = _norm_title_key(t)
        out[k] = {
            "decision": "different",
            "source": "rule_prefilter",
            "note": "未进入本条批量语义槽位（上限5）",
            "confidence": 1.0,
            "candidate_name": t,
        }

    if not need_ai:
        return out

    # 批量 AI：最多 5 个，1 次 API
    batch_in = need_ai[:5]
    _log(
        f"  [名称·AI批量] 本条共判断 {len(batch_in)} 个候选"
        + (f"（另有 {len(need_ai)-5} 个未送）" if len(need_ai) > 5 else "")
    )
    judged, meta = batch_judge_product_names(
        inquiry_name=inquiry_name,
        candidate_names=batch_in,
        settings=settings,
        root=root,
        limiter=limiter,
    )
    if meta.get("llm_budget_blocked"):
        _log(f"  [AI·预算] {meta.get('llm_budget_blocked') and '本条品名批量预算已用尽'}；规则继续")
    elif meta.get("llm_from_cache"):
        _log("  [名称·AI批量] 缓存命中，Token=0")
    elif meta.get("llm_api_called"):
        _log(f"  [名称·AI批量] 1次API · 候选{len(batch_in)}个")
    for t in need_ai:
        k = _norm_title_key(t)
        if k in judged:
            j = judged[k]
            dec = j.get("decision") or "different"
            if dec == "possible":
                _log(f"  [名称·待核] 「{inquiry_name}」?「{t}」可能同物，等待人工确认")
            out[k] = {
                "decision": dec,
                "source": "ai_batch",
                "note": j.get("reason") or "",
                "confidence": float(j.get("confidence") or 0),
                "candidate_name": t,
            }
        elif k not in out:
            # 超过 5 个未送 AI 的：保持 unknown → 名称未命中
            out[k] = {
                "decision": "unknown",
                "source": "skipped",
                "note": "未纳入本条批量AI（上限5）",
                "confidence": 0.0,
                "candidate_name": t,
            }
    return out


def review_product_name_with_llm(
    *,
    item: Any,
    title: str,
    evidence_text: str,
    settings: UserSettings | None,
    root: Path | None,
    limiter: MatchReviewLimiter | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    通用品名同义判定（不写死词表）。
    返回 (是否同物, 说明, 元数据 llm_* )。
    """
    meta: dict[str, Any] = {
        "llm_invoked": False,
        "llm_from_cache": False,
        "llm_api_called": False,
        "llm_decision": "",
        "llm_budget_blocked": False,
    }
    if not _llm_enabled_for_review(settings) or not root:
        return False, "AI未启用或未配置match_review", meta

    name = str(getattr(item, "name", "") or "")
    request_payload = {
        "task": "product_name_synonym",
        "inquiry_name": name,
        "page_title": (title or "")[:300],
        # 品名判断只需主区短摘要；不再把几千字详情重复发送。
        "page_evidence": (evidence_text or "")[:1200],
    }
    # 品名同义缓存按「询价品名+规范化标题」复用；正文/价格/电话变化不应导致重复调用。
    cache_payload = {
        "task": "product_name_synonym_v2",
        "inquiry_name": re.sub(r"\s+", "", name).lower(),
        "page_title": re.sub(r"\s+", "", (title or "")[:300]).lower(),
    }
    cache = _cache_path(Path(root), cache_payload, prefix="name-synonym")
    data: dict[str, Any] | None = None
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            meta["llm_from_cache"] = bool(data)
            meta["llm_invoked"] = True
        except Exception:
            data = None
    if data is None:
        if limiter is not None and not limiter.reserve_api_call():
            meta["llm_budget_blocked"] = True
            return False, limiter.stopped_reason or "单条材料 AI 复核预算已用尽", meta
        system = (
            "你是工程造价/材料采购领域的品名同义判定器。"
            "判断「询价材料名称」与「商品标题/页面」是否指同一类产品（同物异名、字序颠倒、"
            "前后缀 LED/成品/型 可忽略）。"
            "不同品类（如钢管 vs 圆钢、阀门 vs 法兰）必须判 different。"
            "只根据给定标题和证据，不编造。"
            "输出 JSON：decision=same|different|uncertain，confidence=0~1，"
            "reason=简短中文，evidence_quote=页面中支持判断的短引文（可空）。"
        )
        meta["llm_api_called"] = True
        meta["llm_invoked"] = True
        data = _llm_chat_json(
            settings,
            system,
            json.dumps(request_payload, ensure_ascii=False),
            role="match_review",
        )
        if data:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass
    if not data:
        return False, "AI品名同义无返回", meta

    decision = str(data.get("decision") or "").lower().strip()
    if limiter is not None and meta.get("llm_api_called"):
        limiter.record_decision(decision)
    conf = float(data.get("confidence") or 0)
    reason = str(data.get("reason") or "")[:200]
    meta["llm_decision"] = decision
    # 正式「同物」门槛 conf>=0.90；0.75~0.90 视为 possible 不自动过门
    if decision in ("same", "equivalent", "synonym", "yes") and conf >= 0.90:
        return True, f"AI品名同义 conf={conf:.2f}：{reason}", meta
    if decision in ("same", "possible", "uncertain", "maybe") and conf >= 0.75:
        meta["llm_decision"] = "possible"
        return False, f"AI品名可能同物 conf={conf:.2f}：{reason}", meta
    return False, f"AI品名非同义 decision={decision} conf={conf:.2f}：{reason}", meta


def review_semantic_gray_area(
    *,
    item: Any,
    title: str,
    evidence_text: str,
    rule_result: MatchResult,
    settings: UserSettings | None,
    root: Path | None,
    limiter: MatchReviewLimiter | None = None,
) -> MatchResult:
    """
    1) 名称未命中 → 先软规则，再 AI 品名同义；同义则带着询价品名重跑规格匹配
    2) 规格 review 灰区 → 原逻辑（硬字段不改判）
    """
    if not settings or not root:
        return rule_result

    name_miss = any("名称未命中" in str(c) for c in (rule_result.conflicts or ())) or (
        "名称未命中" in (rule_result.detail or "")
    )

    # 硬规格冲突在：型号/DN/尺寸/电压/功率/色温/IP/压力/单位 — AI 不得改判
    if _hard_conflicts_present(rule_result) and not name_miss:
        return rule_result

    # —— 路径 A：品名同义（规则 → 本地库 → 单条AI兜底；批量优先在 inquiry 侧）——
    if name_miss or (
        rule_result.outcome == "review"
        and rule_result.missing
        and all(str(x).startswith("品名") for x in rule_result.missing)
    ):
        qname = str(getattr(item, "name", "") or "")
        dec, src, note = resolve_name_without_ai(
            qname, title, root=root, page_text=evidence_text
        )
        if dec == "same":
            tag = "rule" if src == "rule" else "local_alias"
            return apply_name_same_then_spec(
                item=item,
                title=title,
                evidence_text=evidence_text,
                note=note or "名称同物",
                source_tag=tag,
            )
        if dec == "different":
            return MatchResult(
                False,
                rule_result.score,
                rule_result.required_hit,
                rule_result.required_total,
                f"{rule_result.detail}（{note or '本地负向映射'}）",
                "reject",
                "reject",
                missing=rule_result.missing,
                conflicts=rule_result.conflicts,
                evidence=rule_result.evidence + (note or "负向映射",),
                llm_invoked=False,
                llm_decision="local_negative",
            )

        # 规则已能确认明显不同的名称，不允许为了「碰碰运气」逐候选烧 Token。
        if not is_name_review_gray_candidate(qname, title):
            return rule_result

        # 单条 AI 兜底（inquiry 批量已判过的标题应不再走进这里）
        ok_ai, note_ai, meta = review_product_name_with_llm(
            item=item,
            title=title,
            evidence_text=evidence_text,
            settings=settings,
            root=root,
            limiter=limiter,
        )
        if ok_ai:
            rerun = apply_name_same_then_spec(
                item=item,
                title=title,
                evidence_text=evidence_text,
                note=note_ai,
                source_tag="ai_single",
            )
            return MatchResult(
                rerun.ok,
                rerun.score,
                rerun.required_hit,
                rerun.required_total,
                rerun.detail,
                rerun.level,
                rerun.outcome,
                missing=rerun.missing,
                conflicts=rerun.conflicts,
                evidence=rerun.evidence,
                llm_invoked=bool(meta.get("llm_invoked")),
                llm_decision=str(meta.get("llm_decision") or "same"),
                llm_from_cache=bool(meta.get("llm_from_cache")),
                llm_api_called=bool(meta.get("llm_api_called")),
            )
        if meta.get("llm_invoked"):
            return MatchResult(
                False,
                rule_result.score,
                rule_result.required_hit,
                rule_result.required_total,
                f"{rule_result.detail}（{note_ai}）",
                "reject",
                "reject",
                missing=rule_result.missing,
                conflicts=rule_result.conflicts,
                evidence=rule_result.evidence,
                llm_invoked=True,
                llm_decision=str(meta.get("llm_decision") or "different"),
                llm_from_cache=bool(meta.get("llm_from_cache")),
                llm_api_called=bool(meta.get("llm_api_called")),
            )
        return rule_result

    # —— 路径 B：规格灰区（仅软缺失；硬字段缺失禁止 AI 补全）——
    if rule_result.outcome != "review":
        return rule_result
    if _hard_conflicts_present(rule_result):
        return rule_result
    if not _semantic_only(rule_result.missing):
        return rule_result
    if not _llm_enabled_for_review(settings):
        return rule_result

    payload = {
        "name": str(getattr(item, "name", "")),
        "spec": str(getattr(item, "spec", "")),
        "brand": str(getattr(item, "brand", "")),
        "title": title[:300],
        "evidence": evidence_text[:1800],
        "missing": list(rule_result.missing),
    }
    cache = _cache_path(Path(root), payload, prefix="match-review")
    data: dict[str, Any] | None = None
    from_cache = False
    api_called = False
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            from_cache = bool(data)
        except Exception:
            data = None
            from_cache = False
    if data is None:
        if limiter is not None and not limiter.reserve_api_call():
            return rule_result
        system = (
            "你是工程材料规格复核器，只判断名称/规格语义是否等价，不提供也不推断价格。"
            "只能根据给出的同一商品证据判断。任何型号、数值、单位不同都必须 conflict；"
            "未展示则 insufficient。输出 JSON：decision 为 equivalent/insufficient/conflict，"
            "confidence 为 0-1，covered_requirements 为已覆盖的 missing 原文数组，"
            "evidence_quotes 为页面逐字引用数组，reason 为简短中文。"
        )
        api_called = True
        data = _llm_chat_json(
            settings,
            system,
            json.dumps(payload, ensure_ascii=False),
            role="match_review",
        )
        if data:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass
    if not data:
        return MatchResult(
            rule_result.ok,
            rule_result.score,
            rule_result.required_hit,
            rule_result.required_total,
            rule_result.detail + "（AI 语义复核未返回结果，保留规则判定）",
            rule_result.level,
            rule_result.outcome,
            missing=rule_result.missing,
            conflicts=rule_result.conflicts,
            evidence=rule_result.evidence,
            llm_invoked=True,
            llm_decision="fail",
            llm_from_cache=False,
            llm_api_called=api_called,
        )

    decision = str(data.get("decision") or "").lower()
    if limiter is not None and api_called:
        limiter.record_decision(decision)
    confidence = float(data.get("confidence") or 0)
    quotes = [str(x).strip() for x in (data.get("evidence_quotes") or []) if str(x).strip()]
    covered = {str(x) for x in (data.get("covered_requirements") or [])}
    quotes_valid = bool(quotes) and all(q in evidence_text for q in quotes)
    all_covered = set(rule_result.missing).issubset(covered)
    reason = str(data.get("reason") or "")[:240]
    cache_note = "缓存·不计Token" if from_cache else "实时API"

    def _pack(**kw: Any) -> MatchResult:
        kw.setdefault("llm_invoked", True)
        kw.setdefault("llm_from_cache", from_cache)
        kw.setdefault("llm_api_called", api_called)
        return MatchResult(**kw)

    if decision == "conflict" and confidence >= 0.9 and quotes_valid:
        conflict = f"语义复核发现冲突：{reason or quotes[0]}"
        return _pack(
            ok=False,
            score=rule_result.score,
            required_hit=rule_result.required_hit,
            required_total=rule_result.required_total,
            detail=conflict,
            level="reject",
            outcome="reject",
            missing=rule_result.missing,
            conflicts=(conflict,),
            evidence=rule_result.evidence + tuple(quotes),
            llm_decision="conflict",
        )
    if decision == "equivalent" and confidence >= 0.85 and (quotes_valid or not quotes):
        # 无 quotes 时也允许（模型有时不引原文）；有 quotes 则必须落在证据里
        if quotes and not quotes_valid:
            pass
        elif all_covered or not rule_result.missing:
            return _pack(
                ok=True,
                score=1.0,
                required_hit=rule_result.required_total,
                required_total=rule_result.required_total,
                detail=f"名称+规格语义等价（AI {cache_note}复核；{reason}）",
                level="strict",
                outcome="accept",
                evidence=rule_result.evidence + tuple(quotes),
                llm_decision="equivalent",
            )
    return _pack(
        ok=rule_result.ok,
        score=rule_result.score,
        required_hit=rule_result.required_hit,
        required_total=rule_result.required_total,
        detail=f"{rule_result.detail}（AI {cache_note}：{decision or 'insufficient'}，保留规则判定）",
        level=rule_result.level,
        outcome=rule_result.outcome,
        missing=rule_result.missing,
        conflicts=rule_result.conflicts,
        evidence=rule_result.evidence,
        llm_decision=decision or "insufficient",
    )
