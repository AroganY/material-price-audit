"""Background parse / inquiry jobs for the web wizard."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from ..export_quotes import export_rfq_from_quotes, write_quote_result_workbook
from ..inquiry import quote_map_to_evidence, quote_to_result_row, run_inquiry
from ..normalize import load_canonical_items, save_canonical_json
from ..platforms import load_platform_registry, normalize_platform_id
from ..run_analytics import (
    build_funnel,
    build_platform_stats,
    build_run_meta,
    load_existing_for_continue,
    new_run_id,
)
from ..runtime import (
    get_user_settings,
    load_config,
    load_evidence_document,
    load_quote_map,
    project_root,
    save_evidence,
)
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
    if "max_match_review_calls_per_item" in data:
        settings.llm_max_match_review_calls_per_item = max(
            1, min(5, int(data.get("max_match_review_calls_per_item") or 2))
        )
    if "max_calls_per_run" in data:
        settings.llm_max_calls_per_run = max(
            1, min(200, int(data.get("max_calls_per_run") or 30))
        )
    if "max_tokens_per_run" in data:
        settings.llm_max_tokens_per_run = max(
            2_000, min(500_000, int(data.get("max_tokens_per_run") or 24_000))
        )
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
    baidu_fallback_enabled: bool | None = None,
    default_region: dict | None = None,
    region_strategy: str | None = None,
    region_required: bool | None = None,
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
    if baidu_fallback_enabled is not None:
        settings.baidu_fallback_enabled = bool(baidu_fallback_enabled)
        settings.baidu_fallback_confirmed = True
    if default_region is not None and isinstance(default_region, dict):
        settings.default_region = {
            "province": str(default_region.get("province") or "").strip(),
            "city": str(default_region.get("city") or "").strip(),
            "district": str(default_region.get("district") or "").strip(),
            "source": str(default_region.get("source") or "task"),
        }
        # 尝试补全城市码（弱）
        try:
            from ..region_gate import parse_region_text

            blob = (
                settings.default_region.get("city")
                or settings.default_region.get("province")
                or ""
            )
            if blob:
                rt = parse_region_text(blob, source="task")
                if rt.city_code:
                    settings.default_region["city_code"] = rt.city_code
                if rt.province_code:
                    settings.default_region["province_code"] = rt.province_code
                if rt.province and not settings.default_region.get("province"):
                    settings.default_region["province"] = rt.province
                if rt.city and not settings.default_region.get("city"):
                    settings.default_region["city"] = rt.city
        except Exception:
            pass
    if region_strategy is not None:
        rs = str(region_strategy or "strict_city").strip().lower()
        if rs not in ("strict_city", "allow_province", "national_reference"):
            rs = "strict_city"
        settings.region_strategy = rs
    if region_required is not None:
        settings.region_required = bool(region_required)

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
        if "max_match_review_calls_per_item" in llm:
            settings.llm_max_match_review_calls_per_item = max(
                1,
                min(5, int(llm.get("max_match_review_calls_per_item") or 2)),
            )
        if "max_calls_per_run" in llm:
            settings.llm_max_calls_per_run = max(
                1, min(200, int(llm.get("max_calls_per_run") or 30))
            )
        if "max_tokens_per_run" in llm:
            settings.llm_max_tokens_per_run = max(
                2_000,
                min(500_000, int(llm.get("max_tokens_per_run") or 24_000)),
            )
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
    baidu = "百度兜底开" if settings.baidu_fallback_enabled else "百度兜底关"
    STATE.match_mode = settings.match_mode
    STATE.log(
        f"已保存设置：仅登录/询价这些站 → {', '.join(plats) or '（未选）'}；"
        f"每条 {STATE.quotes_per_item} 个价；匹配={settings.match_mode}；{ai}；{baidu}"
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
        from ..platforms import is_ecommerce_platform

        ecom = [p for p in plats if is_ecommerce_platform(p)]
        cost = [p for p in plats if not is_ecommerce_platform(p)]
        if settings.llm_enabled:
            use_for = list(settings.llm_use_for or [])
            STATE.log(
                f"[AI·配置] 已开启 model={settings.llm_model or '?'} "
                f"用途={','.join(use_for)} "
                f"Key={'已配置' if resolve_llm_api_key(settings) else '未配置'} · "
                f"只有 [AI·API] 才消耗 Token"
            )
            # 本局 AI 会真正干什么（避免用户以为「开了就一定搜词改价」）
            plan_bits = []
            if "schema" in use_for:
                plan_bits.append("表头难识别时用 AI 识表")
            if "search_agent" in use_for:
                if ecom:
                    plan_bits.append(
                        f"电商站({','.join(ecom)})：强制 AI 改写检索词；"
                        f"候选≥3 时 AI 排序；空结果 AI 改词"
                    )
                if cost:
                    plan_bits.append(
                        f"造价站({','.join(cost) or '无'})：默认规则词；"
                        f"空结果/候选模糊才 AI"
                    )
            if "match_review" in use_for:
                plan_bits.append("规格语义灰区才 AI 复核（可缓存，缓存不计 Token）")
            plan_bits.append("京东/1688 价只进「电商参考」，不进合格价")
            STATE.log("[AI·本局会做什么] " + "；".join(plan_bits))
            if not ecom and "search_agent" in use_for:
                STATE.log(
                    "[AI·预期] 本局无电商站：多数条目可能只有规则检索，"
                    "Token 仍可能为 0（不代表 AI 坏了）"
                )
            if ecom and "search_agent" in use_for:
                STATE.log(
                    "[AI·预期] 本局含电商：应陆续出现 [AI·API] search_agent 与 Token>0"
                )
        else:
            STATE.log("[AI·配置] 未开启（规则模式，不请求大模型）")
        for sh in schema.sheets:
            src = getattr(sh, "source", "rule") or "rule"
            if src == "llm":
                STATE.log(
                    f"[AI·说明] 表「{sh.sheet}」表头标记来源=llm"
                    f"（可能是历史缓存；本任务是否新请求看 [AI·API]）"
                )
            else:
                STATE.log(f"[AI·说明] 表「{sh.sheet}」表头来源={src}（规则/缓存，无大模型请求）")

        out_dir = root / "data" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        # 最新别名（下载兼容）+ 每 run 独立文件（历史隔离）
        output_latest = out_dir / "result.xlsx"
        evidence_latest = out_dir / "evidence.json"
        rfq_latest = out_dir / "rfq.xlsx"
        profile = root / ".browser-profile"
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
        # 任务 run_id：新任务全新 id；继续询价沿用上次同簿 run_id（若可）
        cont = bool(getattr(STATE, "continue_mode", False))
        # 断点续跑先读「最新」evidence 元数据
        prev_doc = load_evidence_document(evidence_latest)
        prev_meta = dict(prev_doc.get("meta") or {})
        if cont and prev_meta.get("run_id") and str(
            prev_meta.get("input_path") or ""
        ) == str(STATE.input_path or path):
            run_id = str(prev_meta.get("run_id"))
            STATE.log(f"继续询价：沿用 run_id={run_id}")
        else:
            run_id = new_run_id()
            if cont:
                STATE.log(
                    "继续询价：工作簿与历史不一致或无 run_id → 新建任务 "
                    f"{run_id}"
                )
            else:
                STATE.log(f"本任务 run_id={run_id}（统计与历史按此隔离）")
        STATE.run_id = run_id

        safe_run = "".join(
            c if (c.isalnum() or c in "-_") else "-" for c in str(run_id)
        )[:48] or "run"
        output = out_dir / f"result-{safe_run}.xlsx"
        evidence = out_dir / f"evidence-{safe_run}.json"
        rfq = out_dir / f"rfq-{safe_run}.xlsx"
        STATE.result_path = str(output)
        STATE.evidence_path = str(evidence)
        STATE.rfq_path = str(rfq)
        STATE.log(f"本任务结果文件：{output.name} / {evidence.name}")

        STATE.log(
            f"开始浏览器询价：平台={','.join(plats)}；仅收录名称+规格完全匹配"
        )

        # 续跑：优先本 run 专属 evidence，否则回落最新
        raw_existing = load_quote_map(
            evidence if evidence.exists() else evidence_latest
        )
        if cont:
            existing = load_existing_for_continue(
                raw_existing,
                items,
                meta=prev_meta,
                input_path=str(STATE.input_path or path),
            )
            STATE.log(
                f"继续询价：复用本簿同材料历史 {len(existing)} 条"
                f"（跳过已 full_k/partial）"
            )
        else:
            # 新任务：不混入以前任务的结果做统计/跳过
            existing = {}

        def on_event(ev: dict) -> None:
            STATE.push_event(ev)

        def on_progress(qm) -> None:
            # 只写当前材料范围，带 run_id
            scoped = {k: v for k, v in qm.items() if any(it.id == k for it in items)}
            ev = quote_map_to_evidence(
                scoped, items, k=settings.quotes_per_item, run_id=run_id
            )
            funnel = build_funnel(items, scoped, k=settings.quotes_per_item)
            pstats = build_platform_stats(scoped, item_ids={it.id for it in items})
            save_evidence(
                evidence,
                ev,
                meta=build_run_meta(
                    run_id=run_id,
                    input_path=str(STATE.input_path or path),
                    platforms=plats,
                    k=settings.quotes_per_item,
                    match_mode=settings.match_mode or "practical",
                    funnel=funnel,
                    platform_stats=pstats,
                    extra={
                        "login_verified": list(verified),
                        "partial_save": True,
                    },
                ),
            )
            # 实时漏斗写入 job_stats（不混历史）
            stats = dict(STATE.job_stats or {})
            stats["run_id"] = run_id
            stats["funnel"] = funnel
            stats["platform_stats"] = pstats
            stats["fail_reason_counts"] = funnel.get("fail_reason_counts") or {}
            STATE.job_stats = stats

        # 若有登录面板结果：把已验证的站注入 inquiry 的 session_login_done
        # 通过 skip_login + 在 run 前只对未验证站登录——用 skip_login 仅当全部验证
        # 范围已在 filter_items_by_scope 处理，这里不再二次 limit 截断
        STATE.reset_job_control(llm_default=bool(settings.llm_enabled))
        # 保留 run_id（reset 不应清掉）
        STATE.run_id = run_id
        js0 = dict(STATE.job_stats or {})
        js0["run_id"] = run_id
        STATE.job_stats = js0
        STATE.control = "run"
        STATE.phase = "running"

        def _control_check() -> str:
            return STATE.get_control()

        def _llm_enabled_check() -> bool:
            return STATE.llm_enabled_now(bool(settings.llm_enabled))

        # 继续询价：跳过已有合格价；重跑 need_review / no_match
        skip_statuses = ("full_k", "partial") if cont else ("full_k",)
        STATE.continue_mode = False

        # 汇总 API 返回的 token 用量 + 请求前硬预算。热关 AI 对当前材料立即生效，
        # 不再等到下一条材料才停止请求。
        try:
            from ..schema_map import set_llm_call_guard, set_llm_usage_hook

            def _on_usage(usage: dict) -> None:
                STATE.record_llm_usage(usage)

            budget_tripped = {"logged": False}

            def _allow_llm_call(req: dict) -> dict[str, Any]:
                if not STATE.llm_enabled_now(bool(settings.llm_enabled)):
                    return {"allowed": False, "reason": "本轮 AI 已关闭"}
                stats = dict(STATE.job_stats or {})
                calls = int(stats.get("llm_calls_session") or 0)
                used_tokens = int(stats.get("total_tokens") or 0)
                max_calls = int(settings.llm_max_calls_per_run or 30)
                max_tokens = int(settings.llm_max_tokens_per_run or 24_000)
                role = str(req.get("role") or "")
                output_reserve = {
                    "match_review": 220,
                    "search_agent": 400,
                    "schema": 900,
                }.get(role, 700)
                next_estimate = int(req.get("estimated_prompt_tokens") or 0) + output_reserve
                reason = ""
                if calls >= max_calls:
                    reason = f"本轮 AI 调用已达 {max_calls} 次上限"
                elif used_tokens + next_estimate > max_tokens:
                    reason = (
                        f"本轮 Token 预算 {max_tokens} 即将超限"
                        f"（已用 {used_tokens}）"
                    )
                if not reason:
                    return {"allowed": True}
                if not budget_tripped["logged"]:
                    budget_tripped["logged"] = True
                    STATE.set_llm_runtime(False)
                    STATE.log(f"[AI·硬熔断] {reason}；AI 已自动关闭，规则询价继续")
                return {"allowed": False, "reason": reason}

            set_llm_usage_hook(_on_usage)
            set_llm_call_guard(_allow_llm_call)
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
                run_id=run_id,
                input_path=str(STATE.input_path or path),
            )
        finally:
            try:
                from ..schema_map import set_llm_call_guard, set_llm_usage_hook

                set_llm_usage_hook(None)
                set_llm_call_guard(None)
            except Exception:
                pass

        work_items = items
        # 只保存本任务材料行
        quote_map = {k: v for k, v in quote_map.items() if any(it.id == k for it in work_items)}
        funnel = build_funnel(work_items, quote_map, k=settings.quotes_per_item)
        pstats = build_platform_stats(
            quote_map, item_ids={it.id for it in work_items}
        )
        evidence_map = quote_map_to_evidence(
            quote_map, work_items, k=settings.quotes_per_item, run_id=run_id
        )
        save_evidence(
            evidence,
            evidence_map,
            meta=build_run_meta(
                run_id=run_id,
                input_path=str(STATE.input_path or path),
                platforms=list(settings.platforms_enabled or plats),
                k=settings.quotes_per_item,
                match_mode=settings.match_mode or "practical",
                funnel=funnel,
                platform_stats=pstats,
                extra={
                    "stopped": STATE.get_control() == "stop"
                    or STATE.phase == "stopped",
                    "tokens": (STATE.job_stats or {}).get("total_tokens"),
                    "fail_reason_counts": funnel.get("fail_reason_counts") or {},
                },
            ),
        )
        js = dict(STATE.job_stats or {})
        js["run_id"] = run_id
        js["funnel"] = funnel
        js["platform_stats"] = pstats
        js["fail_reason_counts"] = funnel.get("fail_reason_counts") or {}
        STATE.job_stats = js
        STATE.funnel = funnel
        STATE.platform_stats = pstats
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
        # 同步最新别名，兼容 /api/download/* 默认路径
        try:
            import shutil

            if output.exists():
                shutil.copy2(output, output_latest)
            if evidence.exists():
                shutil.copy2(evidence, evidence_latest)
            if rfq.exists():
                shutil.copy2(rfq, rfq_latest)
        except Exception as e:
            STATE.log(f"同步最新结果别名失败: {e}")
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
                            "unit": it.unit,
                            "qty": it.qty,
                            "submit": it.submit,
                            "region_raw": it.region_raw,
                            "status": qset.status,
                            "quotes": len(qset.quotes),
                            "message": qset.error or "",
                            "platform": (q0 or r0).platform if (q0 or r0) else "",
                            "title": ((q0 or r0).title if (q0 or r0) else "")[:120],
                            "url": (q0.url if q0 else (r0.url if r0 else "")) or "",
                            # 候选价只能出现在 review_list，不得冒充材料主价格。
                            "price": q0.price if q0 else None,
                            "audit": d.get("audit"),
                            "quote_list": [
                                quote_to_result_row(q, role="formal")
                                for q in qset.quotes[:8]
                            ],
                            "review_list": [
                                quote_to_result_row(q, role="review_candidate")
                                for q in qset.review_candidates[:5]
                            ],
                            "market_list": [
                                quote_to_result_row(q, role="market_ref")
                                for q in (qset.market_refs or [])[:5]
                            ],
                            "web_list": [
                                quote_to_result_row(q, role="web_reference")
                                for q in (qset.web_refs or [])[:5]
                            ],
                            "supplier_list": [
                                quote_to_result_row(q, role="supplier_lead")
                                for q in (qset.supplier_leads or [])[:5]
                            ],
                        }
                    )
                if rows:
                    STATE.item_results = rows
        except Exception as e:
            STATE.log(f"汇总停止结果列表时: {e}")

        # 写入本机任务历史（可回看成功条目）
        try:
            from .job_history import append_job

            n_items = len(STATE.item_results or [])
            append_job(
                root,
                {
                    "id": run_id,
                    "run_id": run_id,
                    "phase": "stopped" if was_stop else "done",
                    "platforms": list(plats),
                    "input_path": str(STATE.input_path or path),
                    "full_k": STATE.full_k,
                    "partial": STATE.partial,
                    "need_review": STATE.need_review,
                    "no_match": STATE.no_match,
                    "tokens": tok,
                    "items_done": n_items,
                    "result_path": str(output),
                    "rfq_path": str(rfq),
                    "evidence_path": str(evidence),
                    "funnel": dict(STATE.funnel or funnel or {}),
                    "platform_stats": dict(STATE.platform_stats or pstats or {}),
                    "fail_reason_counts": dict(
                        (STATE.job_stats or {}).get("fail_reason_counts")
                        or funnel.get("fail_reason_counts")
                        or {}
                    ),
                    "item_results": list(STATE.item_results or [])[:200],
                    "message": (
                        f"{'已停止' if was_stop else '完成'} [{run_id[-12:]}] "
                        f"待核={STATE.need_review} 没查到={STATE.no_match} token≈{tok}"
                    ),
                },
            )
            # 历史已写入；结果页继续保留本轮明细，直到用户「再询一批/开始新任务」
            STATE.log(
                f"结果明细已写入历史并保留在结果页（{n_items} 条）；"
                f"可下载 Excel 或在本页核对"
            )
        except Exception as e:
            STATE.log(f"写任务历史失败: {e}")

        if was_stop:
            STATE.phase = "stopped"
            STATE.finished_at = __import__("time").time()
            stats_j = dict(STATE.job_stats or {})
            stats_j["ended_ts"] = STATE.finished_at
            STATE.job_stats = stats_j
            STATE.log(
                f"已停止：满额={STATE.full_k} 部分={STATE.partial} "
                f"候选待核={STATE.need_review} 没查到={STATE.no_match}；"
                f"已出结果 {len(STATE.item_results or [])} 条；本轮 token≈{tok}；可「继续询价」"
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
                f"RFQ={n} 条；结果页 {len(STATE.item_results or [])} 条；token≈{tok}"
            )
        STATE.log(f"结果文件：{output}")
    except Exception as e:
        try:
            from ..schema_map import set_llm_usage_hook

            set_llm_usage_hook(None)
        except Exception:
            pass
        # 出错也尽量保留已完成结果，并记入历史
        try:
            from .job_history import append_job

            tok = int((STATE.job_stats or {}).get("total_tokens") or 0)
            n_items = len(STATE.item_results or [])
            append_job(
                root,
                {
                    "id": getattr(STATE, "run_id", "") or f"err-{int(__import__('time').time())}",
                    "run_id": getattr(STATE, "run_id", "") or "",
                    "phase": "error",
                    "platforms": list(STATE.platforms or []),
                    "input_path": str(STATE.input_path or ""),
                    "full_k": STATE.full_k,
                    "partial": STATE.partial,
                    "need_review": STATE.need_review,
                    "no_match": STATE.no_match,
                    "tokens": tok,
                    "items_done": n_items,
                    "result_path": STATE.result_path or "",
                    "funnel": dict(getattr(STATE, "funnel", None) or {}),
                    "platform_stats": dict(getattr(STATE, "platform_stats", None) or {}),
                    "item_results": list(STATE.item_results or [])[:200],
                    "message": f"异常中断：{e}",
                    "error": str(e),
                },
            )
            # 异常中断：保留已完成条目在结果页，便于用户核对
        except Exception:
            pass
        STATE.phase = "error"
        STATE.error = str(e)
        STATE.log(f"运行失败：{e}")
        STATE.log(traceback.format_exc()[-500:])
        STATE.log(
            f"已尽量保留完成条目 {len(STATE.item_results or [])} 条，"
            f"可在结果页查看或「继续询价」"
        )
