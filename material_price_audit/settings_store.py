"""User preferences: platforms, quote count K, LLM, write-back mode."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_SETTINGS_REL = Path("data/user/settings.json")


@dataclass
class UserSettings:
    platforms_enabled: list[str] = field(default_factory=list)
    quotes_per_item: int = 3
    tax_divisor: float = 1.13
    never_exceed_submit: bool = True
    write_back_mode: str = "side_sheet"  # side_sheet | append_cols | both
    min_title_score: int = 1
    # 匹配档位：strict 全硬规格 | practical 候选工作台（默认）| loose 名称像就进候选
    match_mode: str = "practical"
    # LLM for schema/normalization/semantic gray review — never pricing
    llm_enabled: bool = False
    llm_api_base: str = ""
    llm_api_key_env: str = "OPENAI_API_KEY"
    # 可选：向导页直接填写的 Key（仅存本机 data/user/settings.json，不上传）
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_use_for: list[str] = field(
        # search_agent: 指挥检索词 + 列表排序 + 空结果改词（Playwright 仍负责真打开页面）
        default_factory=lambda: ["schema", "match_review", "search_agent"]
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_llm_dict(self) -> dict[str, Any]:
        """前端展示用：不回传完整 API Key。"""
        return {
            "enabled": bool(self.llm_enabled),
            "api_base": self.llm_api_base or "",
            "api_key_env": self.llm_api_key_env or "OPENAI_API_KEY",
            "api_key_set": bool((self.llm_api_key or "").strip()),
            "model": self.llm_model or "gpt-4o-mini",
            "use_for": list(
                self.llm_use_for or ["schema", "match_review", "search_agent"]
            ),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "UserSettings":
        d = d or {}
        plats = d.get("platforms_enabled") or d.get("platforms") or []
        if isinstance(plats, str):
            plats = [p.strip() for p in plats.split(",") if p.strip()]
        use_for = d.get("llm_use_for")
        if not use_for:
            use_for = ["schema", "match_review", "search_agent"]
        # 兼容嵌套 llm: {...}
        nested = d.get("llm") if isinstance(d.get("llm"), dict) else {}
        enabled = d.get("llm_enabled")
        if enabled is None:
            enabled = nested.get("enabled", False)
        api_base = d.get("llm_api_base")
        if api_base in (None, ""):
            api_base = nested.get("api_base") or ""
        api_key_env = d.get("llm_api_key_env")
        if api_key_env in (None, ""):
            api_key_env = nested.get("api_key_env") or "OPENAI_API_KEY"
        api_key = d.get("llm_api_key")
        if api_key in (None, ""):
            api_key = nested.get("api_key") or ""
        model = d.get("llm_model")
        if model in (None, ""):
            model = nested.get("model") or "gpt-4o-mini"
        if nested.get("use_for") and not d.get("llm_use_for"):
            use_for = nested.get("use_for") or use_for
        allowed = {"schema", "match_review", "search_agent"}
        use_for = [str(x) for x in use_for if str(x) in allowed] or [
            "schema",
            "match_review",
            "search_agent",
        ]
        mode = str(d.get("match_mode") or "practical").strip().lower()
        if mode not in ("strict", "practical", "loose"):
            mode = "practical"
        return cls(
            platforms_enabled=[str(p).strip() for p in plats if str(p).strip()],
            quotes_per_item=max(1, min(10, int(d.get("quotes_per_item") or 3))),
            tax_divisor=float(d.get("tax_divisor") or 1.13),
            never_exceed_submit=bool(d.get("never_exceed_submit", True)),
            write_back_mode=str(d.get("write_back_mode") or "side_sheet"),
            min_title_score=int(d.get("min_title_score") or 1),
            match_mode=mode,
            llm_enabled=bool(enabled),
            llm_api_base=str(api_base or ""),
            llm_api_key_env=str(api_key_env or "OPENAI_API_KEY"),
            llm_api_key=str(api_key or ""),
            llm_model=str(model or "gpt-4o-mini"),
            llm_use_for=list(use_for),
        )


def settings_path(root: Path) -> Path:
    return root / DEFAULT_SETTINGS_REL


def load_settings(root: Path) -> UserSettings:
    path = settings_path(root)
    if not path.exists():
        return UserSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return UserSettings.from_dict(data)
    except Exception:
        return UserSettings()


def save_settings(root: Path, settings: UserSettings) -> Path:
    path = settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def merge_settings_from_config(settings: UserSettings, cfg: dict | None) -> UserSettings:
    """Overlay config.yaml fields when settings file is sparse."""
    cfg = cfg or {}
    pricing = cfg.get("pricing") or {}
    plats = cfg.get("platforms") or {}
    llm = cfg.get("llm") or {}
    inquiry = cfg.get("inquiry") or {}

    if not settings.platforms_enabled and isinstance(plats, dict):
        en = plats.get("enabled") or []
        if en:
            settings.platforms_enabled = [str(p) for p in en]
    if inquiry.get("quotes_per_item"):
        settings.quotes_per_item = max(1, min(10, int(inquiry["quotes_per_item"])))
    if inquiry.get("write_back_mode"):
        settings.write_back_mode = str(inquiry["write_back_mode"])
    if pricing.get("tax_divisor") is not None:
        settings.tax_divisor = float(pricing["tax_divisor"])
    if "never_exceed_submit" in pricing:
        settings.never_exceed_submit = bool(pricing["never_exceed_submit"])
    if "min_title_score" in pricing:
        settings.min_title_score = int(pricing["min_title_score"])
    if inquiry.get("match_mode") or pricing.get("match_mode"):
        mode = str(inquiry.get("match_mode") or pricing.get("match_mode") or "").lower()
        if mode in ("strict", "practical", "loose"):
            settings.match_mode = mode
    if llm:
        if "enabled" in llm:
            settings.llm_enabled = bool(llm["enabled"])
        if llm.get("api_base"):
            settings.llm_api_base = str(llm["api_base"])
        if llm.get("api_key_env"):
            settings.llm_api_key_env = str(llm["api_key_env"])
        if llm.get("model"):
            settings.llm_model = str(llm["model"])
        if llm.get("use_for"):
            settings.llm_use_for = list(llm["use_for"])
    return settings
