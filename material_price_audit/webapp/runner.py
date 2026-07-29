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


def apply_llm_settings(llm: dict[str, Any] | None) -> dict[str, Any]:
    """
    保存向导里的 AI 配置。api_key 为空字符串表示「不改动已有 Key」；
    传 clear_api_key=true 可清空。
    """
    root = _root()
    cfg = load_config(root / "config.yaml" if (root / "config.yaml").exists() else None)
    settings = get_user_settings(root, cfg)
    data = llm if isinstance(llm, dict) else {}
    if "enabled" in data:
        settings.llm_enabled = bool(data.get("enabled"))
    if "api_base" in data:
        settings.llm_api_base = str(data.get("api_base") or "").strip()
    if "api_key_env" in data:
        settings.llm_api_key_env = str(data.get("api_key_env") or "OPENAI_API_KEY").strip() or "OPENAI_API_KEY"
    if "model" in data:
        settings.llm_model = str(data.get("model") or "gpt-4o-mini").strip() or "gpt-4o-mini"
    if "use_for" in data:
        use = data.get("use_for") or []
        if isinstance(use, str):
            use = [x.strip() for x in use.split(",") if x.strip()]
        allowed = {"schema", "match_review", "search_agent"}
        settings.llm_use_for = [str(x) for x in use if str(x) in allowed] or [
            "schema",
            "match_review",
            "search_agent",
        ]
    if data.get("clear_api_key"):
        settings.llm_api_key = ""
    elif "api_key" in data:
        raw = data.get("api_key")
        # 空 = 保留原 Key；非空 = 更新
        if raw is not None and str(raw).strip() != "":
            settings.llm_api_key = str(raw).strip()
    save_settings(root, settings)
    STATE.log(
        "AI 配置已保存："
        + ("已开启" if settings.llm_enabled else "已关闭")
        + f" · model={settings.llm_model}"
        + (" · Key已配置" if settings.llm_api_key else " · Key未写入(可用环境变量)")
        + f" · 用途={','.join(settings.llm_use_for or [])}"
    )
    return settings.public_llm_dict()


def apply_settings(
    platforms: list[str],
    quotes: int,
    limit: int = 0,
    skip_login: bool = False,
    llm: dict[str, Any] | None = None,
    match_mode: str | None = None,
) -> None:
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
    # 注意：不能用 `quotes or 3`，否则用户合法设置会被误伤；0 已在上层 bound 掉
    try:
        k = int(quotes)
    except (TypeError, ValueError):
        k = 3
    settings.quotes_per_item = max(1, min(10, k if k > 0 else 3))
    if match_mode is not None:
        mm = str(match_mode or "practical").strip().lower()
        if mm not in ("strict", "practical", "loose"):
            mm = "practical"
        settings.match_mode = mm

    # 可选：同一次保存里带上 AI 配置
    if isinstance(llm, dict) and llm:
        if "enabled" in llm:
            settings.llm_enabled = bool(llm.get("enabled"))
        if "api_base" in llm:
            settings.llm_api_base = str(llm.get("api_base") or "").strip()
        if "api_key_env" in llm:
            settings.llm_api_key_env = (
                str(llm.get("api_key_env") or "OPENAI_API_KEY").strip() or "OPENAI_API_KEY"
            )
        if "model" in llm:
            settings.llm_model = str(llm.get("model") or "gpt-4o-mini").strip() or "gpt-4o-mini"
        if "use_for" in llm:
            use = llm.get("use_for") or []
            if isinstance(use, str):
                use = [x.strip() for x in use.split(",") if x.strip()]
            allowed = {"schema", "match_review", "search_agent"}
            settings.llm_use_for = [str(x) for x in use if str(x) in allowed] or [
                "schema",
                "match_review",
                "search_agent",
            ]
        if llm.get("clear_api_key"):
            settings.llm_api_key = ""
        elif llm.get("api_key") not in (None, ""):
            settings.llm_api_key = str(llm.get("api_key")).strip()

    save_settings(root, settings)
    STATE.platforms = list(plats)
    STATE.quotes_per_item = settings.quotes_per_item
    try:
        STATE.limit = max(0, int(limit or 0))
    except (TypeError, ValueError):
        STATE.limit = 0
    STATE.skip_login = bool(skip_login)
    # 兼容旧「试跑条数」：仅当用户未设置更细范围时，limit>0 视为 first_n
    if STATE.limit > 0 and (STATE.item_scope_mode or "all") == "all":
        STATE.item_scope_mode = "first_n"
        STATE.item_scope_n = STATE.limit
    try:
        from .login_panel import LOGIN_PANEL

        LOGIN_PANEL.reset_for_platforms(plats)
    except Exception:
        pass
    ai = "AI开" if settings.llm_enabled else "AI关"
    STATE.match_mode = settings.match_mode
    STATE.log(
        f"已保存设置：仅登录/询价这些站 → {', '.join(plats) or '（未选）'}；"
        f"每条 {STATE.quotes_per_item} 个价；匹配={settings.match_mode}；{ai}"
    )


def apply_item_scope(
    mode: str = "all",
    n: int = 0,
    sheets: list[str] | None = None,
    ids: list[str] | None = None,
) -> dict[str, Any]:
    """设置本次询价材料范围（全部 / 前 N / 按 sheet / 勾选 id）。"""
    m = (mode or "all").strip().lower()
    if m not in ("all", "first_n", "sheets", "ids"):
        m = "all"
    STATE.item_scope_mode = m
    try:
        STATE.item_scope_n = max(0, int(n or 0))
    except (TypeError, ValueError):
        STATE.item_scope_n = 0
    STATE.item_scope_sheets = [str(s) for s in (sheets or []) if str(s).strip()]
    STATE.item_scope_ids = [str(i) for i in (ids or []) if str(i).strip()]
    # 与 limit 同步：first_n 时 limit=n，其它模式 limit=0（由过滤后列表决定）
    if m == "first_n" and STATE.item_scope_n > 0:
        STATE.limit = STATE.item_scope_n
    elif m == "all":
        STATE.limit = 0
    else:
        STATE.limit = 0
    with STATE.lock:
        selected = STATE._selected_count_unlocked()
    STATE.log(
        f"询价范围：mode={m}"
        + (f" n={STATE.item_scope_n}" if m == "first_n" else "")
        + (f" sheets={STATE.item_scope_sheets}" if m == "sheets" else "")
        + (f" 勾选{len(STATE.item_scope_ids)}条" if m == "ids" else "")
        + f" → 预计 {selected} 条"
    )
    return {
        "mode": m,
        "n": STATE.item_scope_n,
        "sheets": list(STATE.item_scope_sheets),
        "ids": list(STATE.item_scope_ids),
        "selected_count": selected,
    }


def filter_items_by_scope(items: list) -> list:
    """按 STATE 中的询价范围过滤材料列表。"""
    mode = (STATE.item_scope_mode or "all").lower()
    if not items:
        return []
    if mode == "first_n":
        n = int(STATE.item_scope_n or STATE.limit or 0)
        if n <= 0:
            return list(items)
        return list(items[:n])
    if mode == "sheets":
        want = set(STATE.item_scope_sheets or [])
        if not want:
            return []
        return [it for it in items if getattr(it, "sheet", "") in want]
    if mode == "ids":
        want = set(STATE.item_scope_ids or [])
        if not want:
            return []
        return [it for it in items if getattr(it, "id", "") in want]
    return list(items)


def run_parse(input_path: Path | None = None) -> dict[str, Any]:
    root = _root()
    cfg = load_config(root / "config.yaml" if (root / "config.yaml").exists() else None)
    settings = get_user_settings(root, cfg)
    STATE.phase = "parsing"
    STATE.log("正在识别表头并标准化材料…")
    try:
        from ..schema_map import set_llm_usage_hook

        set_llm_usage_hook(lambda u: STATE.record_llm_usage(u))
    except Exception:
        pass
    try:
        from ..excel_io import EXCEL_SUFFIXES

        path: Path | None = None
        if input_path:
            path = Path(input_path).expanduser()
        elif STATE.input_path:
            path = Path(STATE.input_path).expanduser()
        else:
            # 仅当 data/input 只有 1 个表时自动选用；多文件必须用户先选
            folder = root / "data" / "input"
            files = []
            if folder.is_dir():
                files = sorted(
                    p
                    for suf in EXCEL_SUFFIXES
                    for p in folder.glob(f"*{suf}")
                    if p.is_file() and not p.name.startswith(("~$", "."))
                )
            if len(files) == 1:
                path = files[0]
            elif not files:
                STATE.phase = "error"
                STATE.error = "请先上传或选择询价 Excel（data/input 为空）"
                STATE.log(STATE.error)
                return {"ok": False, "error": STATE.error}
            else:
                names = "、".join(p.name for p in files[:8])
                STATE.phase = "error"
                STATE.error = f"data/input 有多份表，请先在下拉框选定：{names}"
                STATE.log(STATE.error)
                return {"ok": False, "error": STATE.error}

        if path is None or not path.is_file():
            STATE.phase = "error"
            STATE.error = f"询价表不存在：{path}"
            STATE.log(STATE.error)
            return {"ok": False, "error": STATE.error}

        path = path.resolve()
        STATE.input_path = str(path)
        STATE.log(f"识表文件：{path.name}")
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
        # 识表阶段 AI 可见性
        from ..schema_map import resolve_llm_api_key

        STATE.llm_status = {
            "enabled": bool(settings.llm_enabled),
            "model": settings.llm_model or "",
            "use_for": list(settings.llm_use_for or []),
            "key_ready": bool(resolve_llm_api_key(settings)),
            "counts": dict((STATE.llm_status or {}).get("counts") or {}),
            "calls": list((STATE.llm_status or {}).get("calls") or []),
        }
        llm_sheets = [s.sheet for s in schema.sheets if getattr(s, "source", "") == "llm"]
        rule_sheets = [s.sheet for s in schema.sheets if getattr(s, "source", "") != "llm"]
        if settings.llm_enabled:
            STATE.log(
                f"[AI] 识表：开启中 · Key={'有' if resolve_llm_api_key(settings) else '无'} · "
                f"AI识别表={llm_sheets or '无'} · 规则识别表={rule_sheets or '无'}"
            )
            for sn in llm_sheets:
                STATE.record_llm(
                    "schema",
                    ok=True,
                    detail=f"表「{sn}」由 AI 识别",
                    model=settings.llm_model or "",
                )
        else:
            STATE.log("[AI] 识表：未开启，全部走规则识别")
        # 新表默认范围=全部
        STATE.item_scope_mode = "all"
        STATE.item_scope_n = 0
        STATE.item_scope_sheets = []
        STATE.item_scope_ids = []
        STATE.limit = 0
        STATE.log(f"识别完成：{len(schema.sheets)} 个表，{len(items)} 条材料")
        # 完整列表给前端勾选（过大时截断，仍够用）
        items_for_ui = STATE.items_preview[:3000]
        with STATE.lock:
            sheet_counts = STATE._sheet_counts_unlocked()
        return {
            "ok": True,
            "items": len(items),
            "sheets": len(schema.sheets),
            "input_path": str(path),
            "input_name": path.name,
            "items_preview": items_for_ui,
            "items_all": items_for_ui,
            "sheet_counts": sheet_counts,
            "schema_preview": STATE.schema_preview,
            "llm_status": dict(STATE.llm_status or {}),
            "schema_ai_sheets": llm_sheets,
            "item_scope": {
                "mode": "all",
                "n": 0,
                "sheets": [],
                "ids": [],
                "selected_count": len(items),
            },
        }
    except Exception as e:
        STATE.phase = "error"
        STATE.error = str(e)
        STATE.log(f"识表失败：{e}")
        return {"ok": False, "error": str(e)}
    finally:
        try:
            from ..schema_map import set_llm_usage_hook

            set_llm_usage_hook(None)
        except Exception:
            pass


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

        # 按用户选择的范围过滤（全部 / 前N / 某几个 sheet / 勾选行）
        before_n = len(items)
        items = filter_items_by_scope(items)
        if not items:
            STATE.phase = "error"
            STATE.error = (
                "询价范围内没有材料：请在「开始询价」页选择全部、前 N 条、"
                "某个专业表(Sheet)，或勾选具体材料"
            )
            STATE.log(STATE.error)
            return
        STATE.total = len(items)
        if len(items) < before_n:
            STATE.log(
                f"询价范围已过滤：{before_n} → {len(items)} 条"
                f"（mode={STATE.item_scope_mode}）"
            )
        else:
            STATE.log(f"询价范围：全部 {len(items)} 条材料")

        # AI 状态：配置 + 识表是否用了模型
        from ..schema_map import resolve_llm_api_key

        STATE.llm_status = {
            "enabled": bool(settings.llm_enabled),
            "model": settings.llm_model or "",
            "use_for": list(settings.llm_use_for or []),
            "key_ready": bool(resolve_llm_api_key(settings)),
            "counts": {},
            "calls": [],
        }
        if settings.llm_enabled:
            STATE.log(
                f"[AI] 已开启 model={settings.llm_model or '?'} "
                f"用途={','.join(settings.llm_use_for or [])} "
                f"Key={'已配置' if resolve_llm_api_key(settings) else '未配置'}"
            )
        else:
            STATE.log("[AI] 未开启（规则模式：硬规格匹配，不调用大模型）")
        for sh in schema.sheets:
            if getattr(sh, "source", "") == "llm":
                STATE.record_llm(
                    "schema",
                    ok=True,
                    detail=f"表「{sh.sheet}」由 AI 识别表头",
                    model=settings.llm_model or "",
                )
            else:
                STATE.log(f"[AI] 表「{sh.sheet}」表头来源={getattr(sh, 'source', 'rule')}")

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
        from ..scraper import (
            clean_profile_locks,
            kill_stale_profile_browsers,
            profile_lock_present,
        )

        verified = set(LOGIN_PANEL.verified_ids())
        # 必须先关掉登录面板浏览器，再启动询价（同一 .browser-profile 只能一个实例）。
        # 关键：优雅关闭并等待 Cookie 落盘；不要立刻 SIGKILL，否则登录态会丢。
        try:
            LOGIN_PANEL.close_browser()
        except Exception as e:
            STATE.log(f"关闭登录浏览器时: {e}")
        import time as _t

        _t.sleep(1.0)

        n = 0
        removed: list[str] = []
        if profile_lock_present(profile):
            n = kill_stale_profile_browsers(profile)
            removed = clean_profile_locks(profile)
            _t.sleep(0.4)
        else:
            removed = clean_profile_locks(profile)
        if n or removed:
            STATE.log(f"已释放浏览器 profile：killed≈{n}，锁={removed or '无'}")
        else:
            STATE.log("登录浏览器已优雅退出，Cookie profile 可复用")

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
        # 范围已在 filter_items_by_scope 处理，这里不再二次 limit 截断
        STATE.reset_job_control(llm_default=bool(settings.llm_enabled))
        STATE.control = "run"
        STATE.phase = "running"

        def _control_check() -> str:
            return STATE.get_control()

        def _llm_enabled_check() -> bool:
            return STATE.llm_enabled_now(bool(settings.llm_enabled))

        # 继续询价：跳过已有合格价；重跑 need_review / no_match
        cont = bool(getattr(STATE, "continue_mode", False))
        skip_statuses = ("full_k", "partial") if cont else ("full_k",)
        STATE.continue_mode = False

        # 汇总 API 返回的 token 用量
        try:
            from ..schema_map import set_llm_usage_hook

            def _on_usage(usage: dict) -> None:
                STATE.record_llm_usage(usage)

            set_llm_usage_hook(_on_usage)
        except Exception:
            pass

        quote_map: dict = dict(existing or {})
        try:
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
                limit=0,
                on_progress=on_progress,
                on_event=on_event,
                pre_verified_platforms=list(verified),
                control_check=_control_check,
                llm_enabled_check=_llm_enabled_check,
                skip_statuses=skip_statuses,
            )
        finally:
            try:
                from ..schema_map import set_llm_usage_hook

                set_llm_usage_hook(None)
            except Exception:
                pass

        work_items = items
        evidence_map = quote_map_to_evidence(quote_map, items)
        save_evidence(
            evidence,
            evidence_map,
            meta={
                "platforms": settings.platforms_enabled,
                "k": settings.quotes_per_item,
                "mode": settings.match_mode or "practical",
                "stopped": STATE.get_control() == "stop" or STATE.phase == "stopped",
                "tokens": (STATE.job_stats or {}).get("total_tokens"),
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
        tok = int((STATE.job_stats or {}).get("total_tokens") or 0)
        was_stop = STATE.get_control() == "stop" or STATE.phase == "stopped"
        # 保证停止/完成后结果列表可用（若 inquiry 事件未带全量）
        # 注意：勿在本函数内再 from ..inquiry import quote_map_to_evidence，
        # 否则会遮蔽顶部 import，导致 on_progress 闭包 NameError。
        try:
            ev_rows = quote_map_to_evidence(quote_map, work_items)
            if not STATE.item_results and ev_rows:
                rows = []
                for it in work_items:
                    d = ev_rows.get(it.id) or {}
                    qset = quote_map.get(it.id)
                    if not qset:
                        continue
                    q0 = qset.quotes[0] if qset.quotes else None
                    r0 = qset.review_candidates[0] if qset.review_candidates else None
                    rows.append(
                        {
                            "id": it.id,
                            "sheet": it.sheet,
                            "row": it.row,
                            "name": it.name,
                            "spec": it.spec,
                            "brand": it.brand,
                            "submit": it.submit,
                            "status": qset.status,
                            "quotes": len(qset.quotes),
                            "message": qset.error or "",
                            "platform": (q0 or r0).platform if (q0 or r0) else "",
                            "title": ((q0 or r0).title if (q0 or r0) else "")[:120],
                            "url": (q0.url if q0 else (r0.url if r0 else "")) or "",
                            "price": q0.price if q0 else (r0.price if r0 else None),
                            "audit": d.get("audit"),
                            "quote_list": [
                                {
                                    "price": q.price,
                                    "platform": q.platform,
                                    "title": (q.title or "")[:100],
                                    "url": q.url or "",
                                }
                                for q in qset.quotes[:8]
                            ],
                            "review_list": [
                                {
                                    "price": q.price,
                                    "platform": q.platform,
                                    "title": (q.title or "")[:100],
                                    "url": q.url or "",
                                    "match_detail": (q.match_detail or "")[:160],
                                }
                                for q in qset.review_candidates[:5]
                            ],
                        }
                    )
                if rows:
                    STATE.item_results = rows
        except Exception as e:
            STATE.log(f"汇总停止结果列表时: {e}")

        if was_stop:
            STATE.phase = "stopped"
            STATE.finished_at = __import__("time").time()
            stats_j = dict(STATE.job_stats or {})
            stats_j["ended_ts"] = STATE.finished_at
            STATE.job_stats = stats_j
            STATE.log(
                f"已停止：满额={STATE.full_k} 部分={STATE.partial} "
                f"候选待核={STATE.need_review} 没查到={STATE.no_match}；"
                f"已出结果 {len(STATE.item_results)} 条；本轮 token≈{tok}；可「继续询价」"
            )
        else:
            STATE.phase = "done"
            STATE.control = "run"
            STATE.finished_at = __import__("time").time()
            stats_j = dict(STATE.job_stats or {})
            stats_j["ended_ts"] = STATE.finished_at
            STATE.job_stats = stats_j
            STATE.log(
                f"完成：满额={STATE.full_k} 部分={STATE.partial} "
                f"候选待核={STATE.need_review} 没查到={STATE.no_match}；"
                f"RFQ={n} 条；本轮 token≈{tok}"
            )
        STATE.log(f"结果文件：{output}")
    except Exception as e:
        try:
            from ..schema_map import set_llm_usage_hook

            set_llm_usage_hook(None)
        except Exception:
            pass
        STATE.phase = "error"
        STATE.error = str(e)
        STATE.log(f"运行失败：{e}")
        STATE.log(traceback.format_exc()[-500:])
