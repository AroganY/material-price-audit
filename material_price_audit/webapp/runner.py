"""Background parse / inquiry jobs for the web wizard."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from ..export_quotes import export_rfq_from_quotes, write_quote_result_workbook
from ..inquiry import quote_map_to_evidence, run_inquiry
from ..normalize import load_canonical_items, save_canonical_json
from ..platforms import load_platform_registry, normalize_platform_id
from ..runtime import get_user_settings, load_config, load_quote_map, project_root, save_evidence
from ..schema_map import detect_workbook_schema, dump_schema_preview
from ..settings_store import save_settings
from .job_state import STATE


def _root() -> Path:
    return project_root()


def _norm_platforms(platforms: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    root = _root()
    config_path = root / "config.yaml"
    registry = load_platform_registry(load_config(config_path if config_path.exists() else None))
    for p in platforms or []:
        pid = normalize_platform_id(str(p).strip())
        if not pid or pid in seen or pid not in registry:
            continue
        seen.add(pid)
        out.append(pid)
    return out


def apply_settings(platforms: list[str], quotes: int, limit: int = 0, skip_login: bool = False) -> None:
    """
    用户勾选是唯一真相：同时写入
      - data/user/settings.json
      - STATE.platforms
    禁止残留未勾选的站（如慧讯）进入登录列表。
    """
    root = _root()
    cfg = load_config(root / "config.yaml" if (root / "config.yaml").exists() else None)
    settings = get_user_settings(root, cfg)
    plats = _norm_platforms(platforms)
    # 显式传入列表时一律覆盖（包括只勾两个站）；禁止 merge 旧设置
    settings.platforms_enabled = plats
    settings.quotes_per_item = max(1, min(10, int(quotes or 3)))
    save_settings(root, settings)
    STATE.platforms = list(plats)
    STATE.quotes_per_item = settings.quotes_per_item
    STATE.limit = int(limit or 0)
    STATE.skip_login = bool(skip_login)
    try:
        from .login_panel import LOGIN_PANEL

        LOGIN_PANEL.reset_for_platforms(plats)
    except Exception:
        pass
    STATE.log(
        f"已保存设置：仅登录/询价这些站 → {', '.join(plats) or '（未选）'}；每条 {STATE.quotes_per_item} 个价"
    )


def run_parse(input_path: Path | None = None) -> dict[str, Any]:
    root = _root()
    cfg = load_config(root / "config.yaml" if (root / "config.yaml").exists() else None)
    settings = get_user_settings(root, cfg)
    STATE.phase = "parsing"
    STATE.log("正在识别表头并标准化材料…")
    try:
        from ..excel_io import resolve_inquiry_path

        path = input_path or resolve_inquiry_path(None, default_dir=root / "data/input")
        STATE.input_path = str(path)
        schema = detect_workbook_schema(path, root, settings, use_cache=True)
        if not schema.sheets:
            STATE.phase = "error"
            STATE.error = "无法识别表结构，请检查是否为材料询价表"
            STATE.log(STATE.error)
            return {"ok": False, "error": STATE.error}
        dump_schema_preview(schema, root / "data/output/schema.json")
        items = load_canonical_items(path, schema)
        save_canonical_json(items, root / "data/output/canonical_items.json")
        STATE.schema_preview = [
            {
                "sheet": s.sheet,
                "header_row": s.header_row,
                "roles": list(s.roles().keys()),
                "confidence": s.confidence,
                "source": s.source,
            }
            for s in schema.sheets
        ]
        STATE.items_preview = [
            {
                "id": i.id,
                "sheet": i.sheet,
                "row": i.row,
                "name": i.name,
                "spec": i.spec,
                "brand": i.brand,
                "submit": i.submit,
            }
            for i in items
        ]
        STATE.total = len(items)
        STATE.phase = "ready"
        STATE.log(f"识别完成：{len(schema.sheets)} 个表，{len(items)} 条材料")
        return {
            "ok": True,
            "items": len(items),
            "sheets": len(schema.sheets),
            "items_preview": STATE.items_preview[:40],
            "schema_preview": STATE.schema_preview,
        }
    except Exception as e:
        STATE.phase = "error"
        STATE.error = str(e)
        STATE.log(f"识表失败：{e}")
        return {"ok": False, "error": str(e)}


def run_job_background() -> None:
    root = _root()
    try:
        cfg = load_config(root / "config.yaml" if (root / "config.yaml").exists() else None)
        settings = get_user_settings(root, cfg)
        # 登录列表只信 STATE（界面刚保存的勾选），绝不回落掺入旧 settings 里的慧讯等
        plats = _norm_platforms(STATE.platforms)
        if not plats:
            STATE.phase = "error"
            STATE.error = "请先勾选平台（仅登录你勾选的站）"
            STATE.log(STATE.error)
            return
        settings.platforms_enabled = plats
        settings.quotes_per_item = STATE.quotes_per_item or settings.quotes_per_item
        save_settings(root, settings)
        STATE.log(f"本次仅登录/询价：{', '.join(plats)}（不会打开未勾选网站）")

        from ..excel_io import resolve_inquiry_path

        path = Path(STATE.input_path) if STATE.input_path else resolve_inquiry_path(
            None, default_dir=root / "data/input"
        )
        STATE.input_path = str(path)

        # ensure parsed
        if not STATE.items_preview:
            pr = run_parse(path)
            if not pr.get("ok"):
                return

        schema = detect_workbook_schema(path, root, settings, use_cache=True)
        items = load_canonical_items(path, schema)
        if not items:
            STATE.phase = "error"
            STATE.error = "没有可询价的材料行"
            return

        output = root / "data/output/result.xlsx"
        evidence = root / "data/output/evidence.json"
        rfq = root / "data/output/rfq.xlsx"
        profile = root / ".browser-profile"
        STATE.result_path = str(output)
        STATE.evidence_path = str(evidence)
        STATE.rfq_path = str(rfq)
        STATE.phase = "running"
        STATE.started_at = __import__("time").time()

        # 登录面板已验证的站：询价可跳过预检；未验证的站仍会尝试登录
        from .login_panel import LOGIN_PANEL
        from ..scraper import clean_profile_locks, kill_stale_profile_browsers

        verified = set(LOGIN_PANEL.verified_ids())
        # 必须先关掉登录面板浏览器，再启动询价（同一 .browser-profile 只能一个实例）
        try:
            LOGIN_PANEL.close_browser()
        except Exception as e:
            STATE.log(f"关闭登录浏览器时: {e}")
        n = kill_stale_profile_browsers(profile)
        removed = clean_profile_locks(profile)
        if n or removed:
            STATE.log(f"已释放浏览器 profile：killed≈{n}，锁={removed or '无'}")
        import time as _t

        _t.sleep(0.5)

        # 登录面板通过的站 → 询价时直接当已登录，别再卡「请先登录」
        skip_login = bool(STATE.skip_login) or bool(verified)
        if verified:
            STATE.log(
                f"登录面板已验证：{', '.join(verified)} → 询价直接搜这些站"
            )
            # 只搜已验证站（没验证的本来也搜不动）
            only_ok = [p for p in plats if p in verified]
            if only_ok:
                plats = only_ok
                settings.platforms_enabled = plats
        STATE.log(
            f"开始浏览器询价：平台={','.join(plats)}；仅收录名称+规格完全匹配"
        )

        existing = load_quote_map(evidence)

        def on_event(ev: dict) -> None:
            STATE.push_event(ev)

        def on_progress(qm) -> None:
            ev = quote_map_to_evidence(qm, items)
            save_evidence(
                evidence,
                ev,
                meta={
                    "platforms": plats,
                    "k": settings.quotes_per_item,
                    "mode": "strict_full_match",
                    "login_verified": list(verified),
                },
            )

        # 若有登录面板结果：把已验证的站注入 inquiry 的 session_login_done
        # 通过 skip_login + 在 run 前只对未验证站登录——用 skip_login 仅当全部验证
        quote_map = run_inquiry(
            items=items,
            platforms=list(plats),
            settings=settings,
            cfg=cfg,
            root=root,
            profile=profile,
            skip_login=skip_login,
            login_timeout=int((cfg.get("browser") or {}).get("login_timeout_seconds") or 180),
            skip_existing=True,
            existing=existing,
            limit=STATE.limit or 0,
            on_progress=on_progress,
            on_event=on_event,
            pre_verified_platforms=list(verified),
        )

        work_items = items[: STATE.limit] if STATE.limit else items
        evidence_map = quote_map_to_evidence(quote_map, items)
        save_evidence(
            evidence,
            evidence_map,
            meta={
                "platforms": settings.platforms_enabled,
                "k": settings.quotes_per_item,
                "mode": "strict_full_match",
            },
        )
        stats = write_quote_result_workbook(
            path,
            output,
            work_items,
            quote_map,
            tax_divisor=settings.tax_divisor,
            never_exceed=settings.never_exceed_submit,
            k=settings.quotes_per_item,
            write_back_mode=settings.write_back_mode,
        )
        n = export_rfq_from_quotes(
            work_items, quote_map, rfq, k=settings.quotes_per_item
        )
        STATE.full_k = stats.get("full_k", 0)
        STATE.partial = stats.get("partial", 0)
        STATE.need_review = stats.get("need_review", 0)
        STATE.no_match = stats.get("no_match", 0)
        STATE.phase = "done"
        STATE.log(
            f"完成：满额={STATE.full_k} 部分={STATE.partial} "
            f"候选待核={STATE.need_review} 没查到={STATE.no_match}；RFQ={n} 条"
        )
        STATE.log(f"结果文件：{output}")
    except Exception as e:
        STATE.phase = "error"
        STATE.error = str(e)
        STATE.log(f"运行失败：{e}")
        STATE.log(traceback.format_exc()[-500:])
