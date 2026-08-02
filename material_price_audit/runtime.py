"""Project paths, configuration, and evidence persistence.

This module is deliberately independent from the CLI and web layer.  Both
entry points use the same configuration and evidence format, which keeps the
core workflow importable in tests and by third-party integrations.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # ``check --auto-install`` must still be able to start.
    yaml = None

from . import __version__
from .models import QuoteSet
from .settings_store import (
    UserSettings,
    load_settings,
    merge_settings_from_config,
    settings_path,
)


DEFAULT_CONFIG: dict[str, Any] = {
    "pricing": {
        "tax_divisor": 1.13,
        "never_exceed_submit": True,
        "min_title_score": 1,
    },
    "inquiry": {
        "quotes_per_item": 3,
        "write_back_mode": "side_sheet",
        # 造价站每条材料检索词预算（建议 4～6）
        "cost_max_queries_per_item": 6,
    },
    "llm": {
        "enabled": False,
        "api_base": "",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
        "use_for": ["schema", "match_review", "search_agent"],
        # 任何一个上限命中都自动关闭本轮 AI，规则询价继续。
        "max_match_review_calls_per_item": 2,
        "max_calls_per_run": 30,
        "max_tokens_per_run": 24_000,
    },
    "browser": {
        "channel": "chrome",
        "headless": False,
        "login_timeout_seconds": 180,
        "page_timeout_ms": 60_000,
        "between_items_sleep": 1.2,
    },
    "ecommerce": {
        "treat_as_market_ref": True,
        "between_query_sleep_min": 2.5,
        "between_query_sleep_max": 5.0,
        "captcha_wait_seconds": 180,
        "max_queries_per_item": 2,
        "fallback_only_when_no_formal": False,
    },
    "platforms": {"enabled": [], "definitions": {}},
}


def project_root() -> Path:
    """Return the writable project directory used for config and runtime data."""
    override = os.environ.get("MATERIAL_PRICE_AUDIT_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    cwd = Path.cwd().resolve()
    if (cwd / "data").is_dir() or (cwd / "config.yaml").exists():
        return cwd

    source_root = Path(__file__).resolve().parents[1]
    if (source_root / "pyproject.toml").is_file():
        return source_root

    # A wheel installed into site-packages must never write runtime data back
    # into site-packages.  Use the launch directory unless an explicit home
    # was supplied through MATERIAL_PRICE_AUDIT_HOME.
    return cwd


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load ``config.yaml`` over safe defaults without mutating globals."""
    config = deepcopy(DEFAULT_CONFIG)
    if not path or not path.exists():
        return config
    if yaml is None:
        raise RuntimeError("缺少 PyYAML，请先运行 check --auto-install")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml 顶层必须是 YAML 对象")
    # Compatibility with the early ``platforms: [jd, 1688]`` format.
    if isinstance(raw.get("platforms"), list):
        raw["platforms"] = {"enabled": raw["platforms"], "definitions": {}}
    return _deep_merge(config, raw)


def load_evidence(path: Path) -> dict[str, dict]:
    """返回 {item_id: row}；兼容旧文件。完整 meta 见 load_evidence_document。"""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row.get("key") or row.get("item_id")): row
        for row in data.get("results", [])
        if row.get("key") or row.get("item_id")
    }


def load_evidence_document(path: Path) -> dict[str, Any]:
    """读取完整 evidence 文档（含 run_id / meta / results）。"""
    if not path.exists():
        return {"version": __version__, "results": [], "meta": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": __version__, "results": [], "meta": {}}
        data.setdefault("meta", {})
        data.setdefault("results", [])
        return data
    except Exception:
        return {"version": __version__, "results": [], "meta": {}}


def save_evidence(
    path: Path,
    evidence: dict[str, dict],
    meta: dict[str, Any] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_id = ""
    if meta:
        run_id = str(meta.get("run_id") or "")
    payload: dict[str, Any] = {
        "version": __version__,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "results": list(evidence.values()),
    }
    if meta:
        payload["meta"] = meta
        if run_id:
            payload["meta"] = dict(meta)
            payload["meta"]["run_id"] = run_id
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # 按 run_id 另存一份，便于历史隔离与回看
    if run_id:
        try:
            run_dir = path.parent / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "evidence.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass


def get_user_settings(root: Path, config: dict | None = None) -> UserSettings:
    settings = load_settings(root)
    if settings_path(root).exists():
        # The browser wizard owns user choices such as platforms, K and LLM.
        # Advanced config may still supply pricing options, but must not
        # silently overwrite a choice persisted by the UI.
        config = deepcopy(config or {})
        platforms = config.get("platforms")
        if isinstance(platforms, dict):
            platforms.pop("enabled", None)
        inquiry = config.get("inquiry")
        if isinstance(inquiry, dict):
            inquiry.pop("quotes_per_item", None)
        # LLM 以向导/settings.json 为准，避免 config.yaml 把前端关掉的 AI 又打开
        config.pop("llm", None)
    return merge_settings_from_config(settings, config)


def load_quote_map(path: Path) -> dict[str, QuoteSet]:
    return {key: QuoteSet.from_dict(value) for key, value in load_evidence(path).items()}
