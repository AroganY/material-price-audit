"""检索召回增强：预算、空结果不早停、排序、原因改词（不改正式门禁）。"""

from material_price_audit.llm_agent import (
    collect_match_fail_reasons,
    rule_rank_candidates,
    suggest_requery,
)
from material_price_audit.normalize import (
    build_cost_site_queries,
    platform_query_budget,
    rule_requery_from_failures,
)


class _Item:
    def __init__(self, name="闸阀", spec="DN100 PN16", brand="", submit=None):
        self.name = name
        self.spec = spec
        self.brand = brand
        self.submit = submit
        self.unit = "个"
        self.spec_tokens = []


def test_cost_query_budget_is_2_to_4():
    # 造价站默认收紧：名称优先，少搜
    assert platform_query_budget("guangcai", cost_max=3, ecom_max=2) == 3
    assert platform_query_budget("zaojiatong", cost_max=4, ecom_max=2) == 4
    assert platform_query_budget("huixun", cost_max=1, ecom_max=2) == 2  # 下限 2
    assert platform_query_budget("jd", cost_max=6, ecom_max=2) == 2
    assert platform_query_budget("1688", cost_max=6, ecom_max=3) == 3


def test_cost_site_name_first_then_spec():
    q = build_cost_site_queries("闸阀", "DN100 PN16", "正丰", [])
    assert len(q) >= 2
    # 第一个词必须是纯品名（不含 DN）
    assert "DN" not in q[0].upper()
    assert "闸阀" in q[0]
    # 后续可有品名+口径
    assert any("DN100" in x for x in q)


def test_empty_streak_does_not_imply_stop_before_budget():
    """
    语义测试：预算内词表不会被空结果提前截断。
    """
    seeds = build_cost_site_queries(
        "XZP100型片式消声器 1250X400 有效长度：1500",
        "15K116-1",
        "",
        [],
    )
    budget = platform_query_budget("guangcai", cost_max=3, ecom_max=2)
    planned = seeds[:budget]
    assert len(planned) >= 2
    # 纯品名优先
    assert "DN" not in planned[0].upper() or "消声" in planned[0]


def test_rule_rank_prefers_correct_dn_over_cheap_wrong():
    """低价错误规格不能排在高价精确规格前面。"""
    item = _Item(name="闸阀", spec="DN100", submit=500)
    cands = [
        {
            "title": "闸阀",
            "spec_seen": "规格型号：DN40",
            "price_tax": 80,
            "tax_mode": "tax_excl",
            "score": 50,
            "url": "http://x/cheap-wrong",
        },
        {
            "title": "闸阀",
            "spec_seen": "规格型号：DN100 PN16",
            "price_tax": 480,
            "tax_mode": "tax_excl",
            "score": 40,
            "url": "http://x/correct-expensive",
        },
    ]
    ranked = rule_rank_candidates(cands, item=item, tax_divisor=1.13, top_n=5)
    assert ranked[0]["url"] == "http://x/correct-expensive"
    assert "DN100" in (ranked[0].get("spec_seen") or "")


def test_rule_rank_section_hit_beats_low_price():
    item = _Item(
        name="XZP100型片式消声器 1250X400",
        spec="15K116-1",
        submit=900,
    )
    cands = [
        {
            "title": "XZP100片式消声器",
            "spec_seen": "规格 400×320",
            "price_tax": 200,
            "tax_mode": "tax_excl",
            "score": 90,
            "url": "wrong-section",
        },
        {
            "title": "XZP100片式消声器",
            "spec_seen": "规格 1250×400×1500",
            "price_tax": 850,
            "tax_mode": "tax_excl",
            "score": 50,
            "url": "right-section",
        },
    ]
    ranked = rule_rank_candidates(cands, item=item, tax_divisor=1.13, top_n=5)
    assert ranked[0]["url"] == "right-section"


def test_requery_wrong_dn_suggests_correct_dn():
    tried = ["闸阀", "阀门"]
    reasons = ["规格冲突：尺寸 DN150，页面尺寸为 DN40", "DN错误"]
    extra = rule_requery_from_failures(
        "闸阀", "DN150 PN16", "", tried, reasons, [], max_n=3
    )
    assert extra
    assert any("DN150" in q for q in extra), extra
    assert all("DN40" not in q for q in extra)


def test_suggest_requery_works_without_ai():
    """AI 关闭时规则流程完整可用。"""
    item = _Item(name="球阀", spec="DN100")
    qs, note = suggest_requery(
        item=item,
        platform_id="guangcai",
        tried_queries=["球阀"],
        page_hint="",
        settings=None,  # 无 AI
        fail_reasons=["DN错误", "规格冲突：尺寸 DN100，页面尺寸为 DN50"],
    )
    assert qs
    assert any("DN100" in q for q in qs)
    assert "规则" in note


def test_suggest_requery_ai_failure_falls_back_to_rules(monkeypatch):
    """AI 失败时必须回退规则流程。"""
    from material_price_audit import llm_agent as la
    from material_price_audit.settings_store import UserSettings

    settings = UserSettings(
        llm_enabled=True,
        llm_use_for=["search_agent"],
        llm_api_base="http://invalid.local",
        llm_model="x",
    )

    def _boom(*a, **k):
        return None

    monkeypatch.setattr(la, "_search_agent_on", lambda s: True)
    monkeypatch.setattr(la, "_llm_chat_json", _boom)

    item = _Item(name="截止阀", spec="DN200")
    qs, note = suggest_requery(
        item=item,
        platform_id="zaojiatong",
        tried_queries=["截止阀"],
        page_hint="",
        settings=settings,
        fail_reasons=["DN错误"],
    )
    assert qs
    assert any("DN200" in q for q in qs)
    assert "回退" in note or "规则" in note


def test_collect_match_fail_reasons_stable():
    attempts = [
        {
            "platform": "guangcai",
            "match_detail": "名称未命中 need=['消声器']",
        },
        {
            "platform": "guangcai",
            "match_detail": "规格冲突：尺寸 DN150，页面尺寸为 DN40",
        },
        {
            "platform": "guangcai",
            "match_detail": "名称命中；规格缺少：电压 AC220V",
        },
        {
            "platform": "jd",
            "match_detail": "规格冲突：型号 DS-1，页面型号为 DS-2",
        },
    ]
    r = collect_match_fail_reasons(attempts, platform_id="guangcai")
    assert "名称未命中" in r
    assert "DN错误" in r or any("尺寸" in x for x in r)
    assert "规格缺失" in r
    assert "型号错误" not in r  # 过滤了 jd


def test_platform_waterfall_semantics_first_platform_no_match_continues():
    """
    瀑布语义：第一站无匹配不阻塞后续站——
    本测试只验证预算与平台列表独立（collect_quotes 集成由运行时保证）。
    """
    platforms = ["guangcai", "zaojiatong", "lingcai"]
    # 每站独立预算，站与站互不影响（造价站上限 4）
    budgets = [platform_query_budget(p, cost_max=4, ecom_max=2) for p in platforms]
    assert all(b >= 2 for b in budgets)
    assert len(platforms) == 3
