"""AI 真实调用路径：品名同义、硬规格不可覆盖、search_agent、缓存。"""

from pathlib import Path
import threading

import material_price_audit.semantic_review as semantic_review
from material_price_audit.inquiry import name_missed
from material_price_audit.llm_agent import (
    _sanitize_ai_queries,
    _search_agent_on,
    suggest_requery,
)
from material_price_audit.matching import MatchResult, strict_name_spec_match
from material_price_audit.models import CanonicalItem
from material_price_audit.settings_store import UserSettings
from material_price_audit.schema_map import LLMItemCallBudget


def _item(name="地埋灯", spec="9W DC24V"):
    return CanonicalItem(id="x", sheet="s", row=1, name=name, spec=spec)


def test_item_llm_budget_is_shared_atomically_across_platform_workers():
    budget = LLMItemCallBudget(max_calls=1)
    barrier = threading.Barrier(4)
    allowed: list[bool] = []
    lock = threading.Lock()

    def reserve():
        barrier.wait()
        verdict = budget.reserve({"role": "match_review"})
        with lock:
            allowed.append(bool(verdict["allowed"]))

    threads = [threading.Thread(target=reserve) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert allowed.count(True) == 1
    assert budget.calls == 1


def test_name_miss_conflicts_still_allows_ai_path_entry_logic():
    """名称未命中会有 conflicts，但不应被当成硬规格冲突挡 AI。"""
    from material_price_audit.matching import has_hard_spec_conflict, name_missed

    item = _item("地埋灯", "9W")
    mr = strict_name_spec_match(item, "埋地灯XX型号", "埋地灯 功率9W")
    # 可能 soft 已过；强制构造名称未命中结果
    if not name_missed(mr):
        mr = MatchResult(
            False,
            0.0,
            0,
            1,
            "名称未命中 need=['地埋灯']",
            "reject",
            "reject",
            conflicts=("名称未命中：['地埋灯']",),
            missing=("品名：地埋灯",),
        )
    assert name_missed(mr)
    assert mr.conflicts  # 有 conflicts
    assert not has_hard_spec_conflict(mr)  # 但不是硬规格冲突 → 可进 AI


def test_ai_name_synonym_then_spec_conflict_still_reject(tmp_path: Path, monkeypatch):
    """名称同物（含本地/规则）后，规格冲突仍然 reject。"""
    item = _item("地埋灯", "DN100")  # 用 DN 制造硬冲突
    rule = MatchResult(
        False,
        0.0,
        0,
        1,
        "名称未命中 need=['地埋灯']",
        "reject",
        "reject",
        conflicts=("名称未命中：['地埋灯']",),
        missing=("品名：地埋灯",),
    )

    result = semantic_review.review_semantic_gray_area(
        item=item,
        title="埋地灯",
        evidence_text="埋地灯 规格型号：DN40 功率9W",
        rule_result=rule,
        settings=UserSettings(llm_enabled=True, llm_use_for=["match_review"]),
        root=tmp_path,
    )
    # 品名过了（规则/本地）但 DN 冲突 → 不得 accept
    assert not result.ok
    assert result.outcome == "reject"
    assert result.conflicts or "冲突" in (result.detail or "") or "DN" in (
        result.detail or ""
    )


def test_ai_cannot_fill_hard_missing(tmp_path: Path, monkeypatch):
    """硬规格缺失不能被 AI 补全（路径 B 不得调用 API）。"""
    item = CanonicalItem(
        id="x",
        sheet="s",
        row=1,
        name="8端口分控器",
        spec="脱机 AC220V 8端口 512通道",
    )
    rule = strict_name_spec_match(
        item, "8端口分控器", "脱机 8端口 DMX512"
    )
    assert rule.outcome == "review"
    assert any("电压" in x for x in rule.missing)

    def boom(*_a, **_k):
        raise AssertionError("hard missing must not call LLM")

    monkeypatch.setattr(semantic_review, "_llm_chat_json", boom)
    result = semantic_review.review_semantic_gray_area(
        item=item,
        title="8端口分控器",
        evidence_text="脱机 8端口 DMX512",
        rule_result=rule,
        settings=UserSettings(llm_enabled=True, llm_use_for=["match_review"]),
        root=tmp_path,
    )
    assert result is rule or not result.ok


def test_search_agent_off_no_api(monkeypatch):
    settings = UserSettings(
        llm_enabled=True, llm_use_for=["match_review"]  # 无 search_agent
    )
    assert not _search_agent_on(settings)

    def boom(*_a, **_k):
        raise AssertionError("search_agent off must not call API")

    monkeypatch.setattr(
        "material_price_audit.llm_agent._llm_chat_json", boom
    )
    qs, note = suggest_requery(
        item=_item("闸阀", "DN100"),
        platform_id="guangcai",
        tried_queries=["闸阀"],
        page_hint="",
        settings=settings,
        fail_reasons=["列表为空"],
    )
    assert "search_agent关闭" in note or "规则" in note
    # 规则词可有可无，但绝不能调 API


def test_search_agent_api_fail_falls_back_to_rules(monkeypatch):
    settings = UserSettings(
        llm_enabled=True, llm_use_for=["search_agent", "match_review"]
    )
    monkeypatch.setattr(
        "material_price_audit.llm_agent._llm_chat_json", lambda *a, **k: None
    )
    qs, note = suggest_requery(
        item=_item("闸阀", "DN150"),
        platform_id="zaojiatong",
        tried_queries=["闸阀"],
        page_hint="",
        settings=settings,
        fail_reasons=["DN错误", "名称未命中"],
    )
    assert "回退" in note or "规则" in note
    if qs:
        assert any("DN150" in q for q in qs)


def test_ai_queries_max_2_keep_dn_no_invent_model():
    item = _item("闸阀", "DN100 PN16")
    raw = [
        "阀门 DN100",
        "闸阀 DN100 国标",
        "第三词不该出现",
        "闸阀 FAKE999MODEL",  # 编造型号
    ]
    out = _sanitize_ai_queries(raw, item=item, tried=["闸阀"], max_n=2)
    assert len(out) <= 2
    assert all("FAKE999MODEL" not in q for q in out)
    # 至少一个词带 DN100
    assert any("DN100" in q for q in out) or not out


def test_name_synonym_cache_no_repeat_api(tmp_path: Path, monkeypatch):
    """批量品名缓存命中不重复消耗 Token。"""
    from material_price_audit.semantic_review import (
        MatchReviewLimiter,
        batch_judge_product_names,
    )

    calls = {"n": 0}

    def fake_llm(*_a, **_k):
        calls["n"] += 1
        return {
            "inquiry_name": "旋流防止器",
            "candidates": [
                {
                    "candidate_name": "旋流阻旋装置",
                    "decision": "same",
                    "confidence": 0.95,
                    "reason": "同物",
                }
            ],
        }

    monkeypatch.setattr(semantic_review, "_llm_chat_json", fake_llm)
    settings = UserSettings(llm_enabled=True, llm_use_for=["match_review"])
    lim = MatchReviewLimiter(max_api_calls=3)
    batch_judge_product_names(
        inquiry_name="旋流防止器",
        candidate_names=["旋流阻旋装置"],
        settings=settings,
        root=tmp_path,
        limiter=lim,
    )
    assert calls["n"] == 1
    batch_judge_product_names(
        inquiry_name="旋流防止器",
        candidate_names=["旋流阻旋装置"],
        settings=settings,
        root=tmp_path,
        limiter=lim,
    )
    assert calls["n"] == 1  # 缓存，无第二次 API


def test_obviously_different_names_never_call_match_review(tmp_path: Path, monkeypatch):
    """灭火器/电缆等明显不同候选不得为了碰运气调用 AI。"""
    item = _item("旋流防止器", "DN100")
    rule = MatchResult(
        False,
        0.0,
        0,
        1,
        "名称未命中",
        "reject",
        "reject",
        conflicts=("名称未命中：旋流防止器",),
        missing=("品名：旋流防止器",),
    )

    def boom(*_a, **_k):
        raise AssertionError("明显不同候选不得调用 LLM")

    monkeypatch.setattr(semantic_review, "_llm_chat_json", boom)
    monkeypatch.setattr(
        semantic_review, "soft_product_name_equivalent", lambda *a, **k: False
    )
    result = semantic_review.review_semantic_gray_area(
        item=item,
        title="手提式干粉灭火器",
        evidence_text="MFZ/ABC4 4kg",
        rule_result=rule,
        settings=UserSettings(llm_enabled=True, llm_use_for=["match_review"]),
        root=tmp_path,
    )
    assert result is rule


def test_94_gray_candidates_cannot_exceed_per_item_budget(tmp_path: Path, monkeypatch):
    """回归 Token 风暴：94 个候选最多只允许 2 次真实品名复核。"""
    item = _item("旋流防止器", "DN100")
    rule = MatchResult(
        False,
        0.0,
        0,
        1,
        "名称未命中",
        "reject",
        "reject",
        conflicts=("名称未命中：旋流防止器",),
        missing=("品名：旋流防止器",),
    )
    calls = {"n": 0}

    def fake_llm(*_a, **_k):
        calls["n"] += 1
        return {
            "decision": "different",
            "confidence": 0.99,
            "reason": "不是同一材料",
            "evidence_quote": "",
        }

    monkeypatch.setattr(semantic_review, "_llm_chat_json", fake_llm)
    monkeypatch.setattr(
        semantic_review, "soft_product_name_equivalent", lambda *a, **k: False
    )
    limiter = semantic_review.MatchReviewLimiter(max_api_calls=2)
    settings = UserSettings(llm_enabled=True, llm_use_for=["match_review"])
    for i in range(94):
        semantic_review.review_semantic_gray_area(
            item=item,
            title=f"低阻力倒流防止器 第{i}款",
            evidence_text=f"候选{i} DN100",
            rule_result=rule,
            settings=settings,
            root=tmp_path,
            limiter=limiter,
        )

    assert calls["n"] == 2
    assert limiter.api_calls == 2
    assert limiter.stopped_reason


def test_global_call_guard_blocks_network_without_usage(monkeypatch):
    """整轮预算命中时必须在网络请求前拦截。"""
    from material_price_audit import schema_map

    monkeypatch.setattr(
        schema_map.urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("预算拦截后不得发网络请求")
        ),
    )
    schema_map.set_llm_call_guard(
        lambda _req: {"allowed": False, "reason": "测试预算已满"}
    )
    try:
        result = schema_map._llm_chat_json(
            UserSettings(llm_enabled=True, llm_api_key="test-key"),
            "system",
            "user",
            role="match_review",
        )
    finally:
        schema_map.set_llm_call_guard(None)
    assert result is None
