from __future__ import annotations

from pathlib import Path

from material_price_audit.platforms import CORE_PLATFORM_IDS
from material_price_audit.runtime import get_user_settings, load_config, project_root
from material_price_audit.settings_store import UserSettings, save_settings


def test_config_is_deep_merged_over_defaults(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "browser:\n  page_timeout_ms: 12345\nllm:\n  model: test-model\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config["browser"]["page_timeout_ms"] == 12345
    assert config["browser"]["channel"] == "chrome"
    assert config["llm"]["model"] == "test-model"
    assert config["llm"]["enabled"] is False


def test_wizard_platforms_and_quote_count_win_over_config(tmp_path: Path):
    save_settings(
        tmp_path,
        UserSettings(platforms_enabled=["jd", "1688"], quotes_per_item=2),
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
platforms:
  enabled: [guangcai, huixun, lingcai]
inquiry:
  quotes_per_item: 5
  write_back_mode: both
pricing:
  tax_divisor: 1.09
""".strip(),
        encoding="utf-8",
    )

    settings = get_user_settings(tmp_path, load_config(config_path))

    assert settings.platforms_enabled == ["jd", "1688"]
    assert settings.quotes_per_item == 2
    assert settings.write_back_mode == "both"
    assert settings.tax_divisor == 1.09


def test_only_five_sites_are_builtin_maintained_platforms():
    assert CORE_PLATFORM_IDS == ("guangcai", "lingcai", "huixun", "jd", "1688")


def test_project_home_environment_override_is_respected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MATERIAL_PRICE_AUDIT_HOME", str(tmp_path))
    assert project_root() == tmp_path.resolve()
