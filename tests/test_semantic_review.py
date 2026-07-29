from pathlib import Path

import material_price_audit.semantic_review as semantic_review
from material_price_audit.matching import strict_name_spec_match
from material_price_audit.models import CanonicalItem
from material_price_audit.settings_store import UserSettings


def test_llm_cannot_override_missing_numeric_or_model_requirement(tmp_path: Path):
    item = CanonicalItem(
        id="x",
        sheet="s",
        row=1,
        name="8端口分控器",
        spec="脱机 AC220V 8端口 512通道",
    )
    rule = strict_name_spec_match(
        item,
        "8端口分控器",
        "脱机 8端口 DMX512",
    )
    assert rule.outcome == "review"
    assert any("电压" in x for x in rule.missing)

    original = semantic_review._llm_chat_json
    semantic_review._llm_chat_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("hard requirements must not call LLM")
    )
    try:
        result = semantic_review.review_semantic_gray_area(
            item=item,
            title="8端口分控器",
            evidence_text="脱机 8端口 DMX512",
            rule_result=rule,
            settings=UserSettings(llm_enabled=True, llm_use_for=["match_review"]),
            root=tmp_path,
        )
    finally:
        semantic_review._llm_chat_json = original
    assert result is rule
