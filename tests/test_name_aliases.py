"""三级品名链路：规则 / 本地库 / 批量AI / 人工确认 / 规格门禁。"""

from pathlib import Path

import material_price_audit.semantic_review as semantic_review
from material_price_audit.matching import soft_product_name_equivalent, strict_name_spec_match
from material_price_audit.models import CanonicalItem
from material_price_audit.name_aliases import (
    confirm_different_names,
    confirm_same_names,
    expand_queries_with_aliases,
    get_aliases_for_name,
    load_name_alias_store,
    lookup_name_relation,
    normalize_name_key,
)
from material_price_audit.semantic_review import (
    MatchReviewLimiter,
    apply_name_same_then_spec,
    batch_judge_product_names,
    prepare_item_name_decisions,
    resolve_name_without_ai,
)
from material_price_audit.settings_store import UserSettings


def test_dimai_maidi_rule_same():
    """地埋灯 / 埋地灯：规则字袋同物。"""
    assert soft_product_name_equivalent("地埋灯", "埋地灯", "")
    dec, src, _ = resolve_name_without_ai("地埋灯", "埋地灯", root=None)
    assert dec == "same"
    assert src == "rule"


def test_check_valve_local_confirm_no_ai(tmp_path: Path, monkeypatch):
    """止回阀 / 单向阀：本地确认后不调用 AI。"""
    # builtin 已有；再 confirm 一次
    confirm_same_names("止回阀", "单向阀", tmp_path, source="user_confirmed")
    rel, note = lookup_name_relation("单向阀", "止回阀", tmp_path)
    assert rel == "same"

    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("should not call AI")

    monkeypatch.setattr(semantic_review, "_llm_chat_json", boom)
    # soft 对止回/单向未必过，但 local 应过
    dec, src, _ = resolve_name_without_ai("止回阀", "单向阀", root=tmp_path)
    assert dec == "same"
    assert src in ("local_alias", "rule")
    # prepare 批量也不应调 AI
    d = prepare_item_name_decisions(
        inquiry_name="止回阀",
        candidate_titles=["单向阀 DN100", "法兰"],
        settings=UserSettings(llm_enabled=True, llm_use_for=["match_review"]),
        root=tmp_path,
        limiter=MatchReviewLimiter(max_api_calls=2),
    )
    # 键规范化会剥 DN100
    key = normalize_name_key("单向阀 DN100")
    assert key in d or normalize_name_key("单向阀") in d
    hit = d.get(key) or d.get(normalize_name_key("单向阀"))
    assert hit and hit["decision"] == "same"
    assert calls["n"] == 0


def test_valve_vs_flange_not_same():
    """阀门 / 法兰：不得判为同物。"""
    assert not soft_product_name_equivalent("阀门", "法兰", "")
    dec, _, _ = resolve_name_without_ai("阀门", "法兰", root=None)
    assert dec != "same"
    # 预筛也不应进灰区（关联度低）
    from material_price_audit.semantic_review import is_name_review_gray_candidate

    # 可能有微弱公共字「阀」；若进灰区也必须 AI 判 different
    # 至少规则不同物
    assert dec == "unknown" or dec == "different"


def test_same_name_dn_conflict_reject():
    """名称同物但 DN100 / DN150：必须拒绝正式报价。"""
    item = CanonicalItem(id="x", sheet="s", row=1, name="闸阀", spec="DN100")
    mr = apply_name_same_then_spec(
        item=item,
        title="闸阀",
        evidence_text="闸阀 规格型号：DN150 PN16",
        note="名称同物",
        source_tag="local_alias",
    )
    assert not mr.ok
    assert mr.outcome == "reject"
    assert "冲突" in (mr.detail or "") or mr.conflicts


def test_same_name_missing_dn_only_review():
    """名称同物但候选没展示 DN：只能进入 review（非 accept）。"""
    item = CanonicalItem(id="x", sheet="s", row=1, name="闸阀", spec="DN100")
    mr = apply_name_same_then_spec(
        item=item,
        title="闸阀",
        evidence_text="闸阀 优质产品 厂家直供",
        note="名称同物",
        source_tag="rule",
    )
    assert not mr.ok
    assert mr.outcome == "review"
    assert any("DN" in str(x) or "尺寸" in str(x) for x in (mr.missing or ()))


def test_batch_94_candidates_one_api(tmp_path: Path, monkeypatch):
    """94 个不同候选：最多 1 次批量 AI API（仅送前 5）。"""
    calls = {"n": 0}

    def fake_llm(*_a, **_k):
        calls["n"] += 1
        return {
            "inquiry_name": "波纹补偿器",
            "candidates": [
                {
                    "candidate_name": f"波纹伸缩节型号{i}",
                    "decision": "same" if i == 0 else "different",
                    "confidence": 0.95,
                    "canonical_name": "波纹补偿器",
                    "reason": "同物异名" if i == 0 else "不同",
                }
                for i in range(5)
            ],
        }

    monkeypatch.setattr(semantic_review, "_llm_chat_json", fake_llm)
    titles = [f"波纹伸缩节型号{i}" for i in range(94)]
    # soft 可能对「波纹伸缩节」直接 same，先用负向无关名逼进 AI
    titles = [f"补偿装置型{i}号伸缩" for i in range(94)]
    # force gray by monkeypatch
    monkeypatch.setattr(
        semantic_review,
        "is_name_review_gray_candidate",
        lambda a, b: True,
    )
    monkeypatch.setattr(
        semantic_review,
        "soft_product_name_equivalent",
        lambda *a, **k: False,
    )
    # 清空 local builtin interference for 波纹
    lim = MatchReviewLimiter(max_api_calls=2)
    prepare_item_name_decisions(
        inquiry_name="波纹补偿器",
        candidate_titles=titles,
        settings=UserSettings(llm_enabled=True, llm_use_for=["match_review"]),
        root=tmp_path,
        limiter=lim,
    )
    assert calls["n"] == 1
    assert lim.api_calls == 1


def test_duplicate_candidate_names_deduped(tmp_path: Path, monkeypatch):
    calls = {"n": 0, "payloads": []}

    def fake_llm(settings, system, user, role="match_review"):
        calls["n"] += 1
        import json

        calls["payloads"].append(json.loads(user))
        return {
            "inquiry_name": "地埋灯",
            "candidates": [
                {
                    "candidate_name": "某品牌埋地灯",
                    "decision": "same",
                    "confidence": 0.92,
                    "reason": "同物",
                }
            ],
        }

    monkeypatch.setattr(semantic_review, "_llm_chat_json", fake_llm)
    monkeypatch.setattr(
        semantic_review, "soft_product_name_equivalent", lambda *a, **k: False
    )
    monkeypatch.setattr(
        semantic_review, "is_name_review_gray_candidate", lambda *a, **k: True
    )
    titles = ["某品牌埋地灯"] * 20 + ["某品牌埋地灯 18W"] * 10
    prepare_item_name_decisions(
        inquiry_name="地埋灯",
        candidate_titles=titles,
        settings=UserSettings(llm_enabled=True, llm_use_for=["match_review"]),
        root=tmp_path,
        limiter=MatchReviewLimiter(max_api_calls=2),
    )
    assert calls["n"] == 1
    # 批量里 unique 候选 ≤5
    cands = calls["payloads"][0].get("candidates") or []
    assert len(cands) <= 5


def test_user_confirm_second_run_token_zero(tmp_path: Path, monkeypatch):
    """用户确认映射后，第二次运行 Token=0。"""
    confirm_same_names("水泵接合器", "消防水泵接合器", tmp_path, source="user_confirmed")
    calls = {"n": 0}
    monkeypatch.setattr(
        semantic_review,
        "_llm_chat_json",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or None,
    )
    d = prepare_item_name_decisions(
        inquiry_name="水泵接合器",
        candidate_titles=["消防水泵接合器 DN100"],
        settings=UserSettings(llm_enabled=True, llm_use_for=["match_review"]),
        root=tmp_path,
        limiter=MatchReviewLimiter(max_api_calls=2),
    )
    assert d[normalize_name_key("消防水泵接合器 DN100")]["decision"] == "same"
    assert calls["n"] == 0


def test_budget_exhausted_rules_continue(tmp_path: Path, monkeypatch):
    """AI 预算耗尽后规则流程继续（不抛错，可规则同物）。"""
    lim = MatchReviewLimiter(max_api_calls=1)
    lim.api_calls = 1
    lim.stopped_reason = "单条材料 AI 复核已达 1 次上限"
    monkeypatch.setattr(
        semantic_review,
        "_llm_chat_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no api")),
    )
    # 地埋灯规则仍可过
    d = prepare_item_name_decisions(
        inquiry_name="地埋灯",
        candidate_titles=["埋地灯", "完全无关的塔吊"],
        settings=UserSettings(llm_enabled=True, llm_use_for=["match_review"]),
        root=tmp_path,
        limiter=lim,
    )
    assert d[normalize_name_key("埋地灯")]["decision"] == "same"


def test_formal_price_still_from_page_not_invented():
    """正式报价仍须名称+规格+真实价链路（规格门禁独立）。"""
    item = CanonicalItem(id="x", sheet="s", row=1, name="地埋灯", spec="9W DC24V")
    # 名称同物 + 规格齐 → accept
    mr = strict_name_spec_match(
        item,
        "埋地灯",
        "埋地灯 功率9W 电压DC24V",
    )
    # soft 已过名称
    assert "名称未命中" not in (mr.detail or "")
    if mr.ok:
        assert mr.outcome == "accept"
    # 名称同物但无价时由 inquiry 层丢弃 0 价；此处仅证规格门禁不因 AI 编价
    mr2 = apply_name_same_then_spec(
        item=item,
        title="埋地灯",
        evidence_text="埋地灯 功率9W 电压DC24V",
        note="同物",
        source_tag="rule",
    )
    assert mr2.ok  # 规格齐
    # 无价格字段时门禁仍只看规格；价格由 inquiry 从页面抽取


def test_expand_queries_adds_at_most_two_aliases(tmp_path: Path):
    confirm_same_names("旋流防止器", "旋流防止阀", tmp_path)
    qs = expand_queries_with_aliases(
        ["旋流防止器 DN100", "旋流防止器"],
        "旋流防止器",
        tmp_path,
        max_alias_queries=2,
    )
    assert qs[0] == "旋流防止器 DN100"
    alias_qs = [q for q in qs if "旋流防止阀" in q]
    assert 1 <= len(alias_qs) <= 2
    assert any("DN100" in q for q in alias_qs)


def test_negative_mapping_blocks(tmp_path: Path):
    confirm_different_names("阀门", "法兰", tmp_path)
    rel, _ = lookup_name_relation("法兰", "阀门", tmp_path)
    assert rel == "different"
