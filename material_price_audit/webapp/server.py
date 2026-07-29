"""
本地向导服务：Agent 启动后用户只在浏览器操作，不敲命令。

  python -m material_price_audit serve
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
import zipfile
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from .. import __version__
from ..excel_io import EXCEL_SUFFIXES
from ..platforms import BUILTIN, CORE_PLATFORM_IDS, load_platform_registry
from ..runtime import get_user_settings, load_config, project_root
from ..scraper import agent_login_signal_path, clear_agent_login_signal
from ..settings_store import load_settings
from . import runner
from .job_state import STATE
from .login_panel import LOGIN_PANEL


STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_JSON_BYTES = 1 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class RequestBodyError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _platform_catalog(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if config is None:
        root = project_root()
        config_path = root / "config.yaml"
        config = load_config(config_path if config_path.exists() else None)
    registry = load_platform_registry(config)
    platform_ids = list(CORE_PLATFORM_IDS)
    platform_ids.extend(sorted(pid for pid in registry if pid not in CORE_PLATFORM_IDS))
    return [
        {
            "id": platform_id,
            "name": registry[platform_id].name,
            "login_url": registry[platform_id].login_url,
            "cost": platform_id in ("guangcai", "huixun", "lingcai", "yize"),
            "custom": platform_id not in BUILTIN,
        }
        for platform_id in platform_ids
        if platform_id in registry
    ]


def _json_response(handler: BaseHTTPRequestHandler, code: int, data: Any) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler: BaseHTTPRequestHandler, max_bytes: int) -> bytes:
    value = handler.headers.get("Content-Length")
    if value in (None, ""):
        return b""
    try:
        length = int(value)
    except (TypeError, ValueError) as exc:
        raise RequestBodyError("Content-Length 无效") from exc
    if length < 0:
        raise RequestBodyError("Content-Length 无效")
    if length > max_bytes:
        raise RequestBodyError(f"请求体不能超过 {max_bytes // 1024 // 1024} MB", 413)
    body = handler.rfile.read(length)
    if len(body) != length:
        raise RequestBodyError("请求体不完整")
    return body


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    raw = _read_body(handler, MAX_JSON_BYTES)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestBodyError("请求 JSON 无效") from exc
    if not isinstance(data, dict):
        raise RequestBodyError("请求 JSON 顶层必须是对象")
    return data


def _safe_child(base: Path, relative: str) -> Path | None:
    """Resolve a URL path below *base* and reject traversal attempts."""
    try:
        base = base.resolve()
        candidate = (base / unquote(relative)).resolve()
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_relative_to(base) else None


def _excel_filename(value: str) -> str:
    name = unquote(value or "").strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or name.startswith("~$")
    ):
        raise RequestBodyError("Excel 文件名无效")
    if Path(name).suffix.lower() not in EXCEL_SUFFIXES:
        raise RequestBodyError("仅支持 .xlsx 和 .xlsm；旧 .xls 请先另存为 .xlsx")
    return name


def _is_ooxml_workbook(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
        return "[Content_Types].xml" in names and "xl/workbook.xml" in names
    except (OSError, zipfile.BadZipFile):
        return False


def _bounded_int(
    value: Any,
    *,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RequestBodyError(f"{field} 必须是整数") from exc
    if not minimum <= parsed <= maximum:
        raise RequestBodyError(f"{field} 必须在 {minimum}～{maximum} 之间")
    return parsed


def _serve_file(
    handler: BaseHTTPRequestHandler,
    path: Path,
    *,
    download_name: str = "",
) -> None:
    if not path.exists() or not path.is_file():
        handler.send_error(404)
        return
    data = path.read_bytes()
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    if download_name:
        handler.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{quote(download_name)}",
        )
    if path.suffix.lower() in (".html", ".css", ".js"):
        handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    server_version = f"MaterialInquiry/{__version__}"

    def log_message(self, fmt: str, *args) -> None:
        # quieter
        if "/api/state" in str(args[0] if args else ""):
            return
        print(f"[web] {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        root = project_root()

        if path in ("/", "/index.html"):
            return _serve_file(self, STATIC_DIR / "index.html")
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            static_path = _safe_child(STATIC_DIR, rel)
            if static_path is None:
                return self.send_error(404)
            return _serve_file(self, static_path)

        if path == "/api/state":
            cfg = load_config(root / "config.yaml" if (root / "config.yaml").exists() else None)
            settings = get_user_settings(root, cfg)
            snap = STATE.snapshot()
            # 优先内存 STATE（界面刚保存）；否则 settings.json（用户偏好）
            # 禁止用 config 默认三站偷偷加回未勾选的慧讯
            if snap.get("platforms"):
                pass
            elif settings.platforms_enabled:
                snap["platforms"] = list(settings.platforms_enabled)
                STATE.platforms = list(settings.platforms_enabled)
            # quotes_per_item：内存 STATE 优先；仅在未初始化时回落 settings
            if not snap.get("quotes_per_item"):
                snap["quotes_per_item"] = settings.quotes_per_item or 3
                STATE.quotes_per_item = int(snap["quotes_per_item"])
            # platforms catalog
            snap["catalog"] = _platform_catalog(cfg)
            snap["version"] = __version__
            snap["login_signal"] = str(agent_login_signal_path(root))
            # list input files
            inp = root / "data" / "input"
            files = []
            if inp.exists():
                for p in sorted(inp.iterdir()):
                    if not p.is_file() or p.suffix.lower() not in EXCEL_SUFFIXES:
                        continue
                    if p.name.startswith(("~$", ".")):
                        continue
                    files.append({"name": p.name, "path": str(p)})
            snap["input_files"] = files
            # AI / LLM 配置（Key 不回传明文）
            snap["llm"] = settings.public_llm_dict()
            # 登录面板独立状态（snapshot 内已带 login_panel，此处保证有 platforms 时初始化）
            if snap.get("platforms") and not (snap.get("login_panel") or {}).get("platforms"):
                LOGIN_PANEL.reset_for_platforms(snap["platforms"])
                snap["login_panel"] = LOGIN_PANEL.snapshot()
            return _json_response(self, 200, snap)

        if path == "/api/login/status":
            if STATE.platforms and not LOGIN_PANEL.rows:
                LOGIN_PANEL.reset_for_platforms(STATE.platforms)
            return _json_response(self, 200, {"ok": True, **LOGIN_PANEL.snapshot()})

        if path == "/api/download/result":
            p = Path(STATE.result_path or (root / "data/output/result.xlsx"))
            return _serve_file(self, p, download_name="result.xlsx")
        if path == "/api/download/rfq":
            p = Path(STATE.rfq_path or (root / "data/output/rfq.xlsx"))
            return _serve_file(self, p, download_name="rfq.xlsx")

        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        root = project_root()

        if path == "/api/upload":
            return _json_response(self, 410, {"ok": False, "error": "请使用新版上传接口"})

        if path == "/api/upload-file":
            try:
                raw = _read_body(self, MAX_UPLOAD_BYTES)
                fname = _excel_filename(self.headers.get("X-Filename") or "inquiry.xlsx")
                if not raw:
                    raise RequestBodyError("上传文件为空")
                if not _is_ooxml_workbook(raw):
                    raise RequestBodyError("文件内容不是有效的 .xlsx/.xlsm 工作簿")
            except RequestBodyError as exc:
                return _json_response(self, exc.status, {"ok": False, "error": str(exc)})

            dest_dir = root / "data" / "input"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / fname
            temporary = dest.with_name(f".{dest.name}.uploading")
            try:
                temporary.write_bytes(raw)
                temporary.replace(dest)
            finally:
                temporary.unlink(missing_ok=True)
            STATE.input_path = str(dest)
            STATE.log(f"已上传询价表：{fname}")
            return _json_response(self, 200, {"ok": True, "path": str(dest), "name": fname})

        try:
            data = _read_json(self)
        except RequestBodyError as exc:
            return _json_response(self, exc.status, {"ok": False, "error": str(exc)})

        if path == "/api/settings":
            platforms = data.get("platforms") or []
            if isinstance(platforms, str):
                platforms = [x.strip() for x in platforms.split(",") if x.strip()]
            try:
                quotes = _bounded_int(
                    data.get("quotes_per_item")
                    if data.get("quotes_per_item") is not None
                    else data.get("quotes"),
                    field="每条价格数",
                    default=3,
                    minimum=1,
                    maximum=10,
                )
                limit = _bounded_int(
                    data.get("limit"),
                    field="试跑条数",
                    default=0,
                    minimum=0,
                    maximum=100_000,
                )
            except RequestBodyError as exc:
                return _json_response(self, exc.status, {"ok": False, "error": str(exc)})
            skip_login = bool(data.get("skip_login"))
            llm_payload = data.get("llm") if isinstance(data.get("llm"), dict) else None
            match_mode = str(data.get("match_mode") or "").strip().lower() or None
            runner.apply_settings(
                platforms, quotes, limit, skip_login, llm=llm_payload, match_mode=match_mode
            )
            snap = STATE.snapshot()
            cfg = load_config(root / "config.yaml" if (root / "config.yaml").exists() else None)
            settings = get_user_settings(root, cfg)
            # 确保前端立刻能拿到 catalog 重绘勾选；显式回写价数/试跑/AI，便于前端锁表
            return _json_response(
                self,
                200,
                {
                    "ok": True,
                    **snap,
                    "quotes_per_item": STATE.quotes_per_item,
                    "match_mode": getattr(STATE, "match_mode", None) or settings.match_mode,
                    "limit": STATE.limit,
                    "platforms": list(STATE.platforms),
                    "llm": settings.public_llm_dict(),
                    "catalog": _platform_catalog(),
                    "login_panel": LOGIN_PANEL.snapshot(),
                },
            )

        if path == "/api/llm":
            # 仅更新 AI 配置（不要求同时改平台）
            llm_payload = data if isinstance(data, dict) else {}
            if "llm" in llm_payload and isinstance(llm_payload.get("llm"), dict):
                llm_payload = llm_payload["llm"]
            public = runner.apply_llm_settings(llm_payload)
            return _json_response(self, 200, {"ok": True, "llm": public})

        if path == "/api/llm/test":
            # 可用请求体临时覆盖 Key（不落盘）做连通性测试
            from ..schema_map import test_llm_connection
            from ..settings_store import UserSettings

            cfg = load_config(root / "config.yaml" if (root / "config.yaml").exists() else None)
            settings = get_user_settings(root, cfg)
            if isinstance(data, dict):
                if "enabled" in data:
                    settings.llm_enabled = bool(data.get("enabled"))
                else:
                    settings.llm_enabled = True  # 测试时视为开启
                if data.get("api_base") is not None:
                    settings.llm_api_base = str(data.get("api_base") or "").strip()
                if data.get("model") is not None:
                    settings.llm_model = str(data.get("model") or "gpt-4o-mini").strip()
                if data.get("api_key_env") is not None:
                    settings.llm_api_key_env = str(data.get("api_key_env") or "OPENAI_API_KEY")
                if data.get("api_key"):
                    settings.llm_api_key = str(data.get("api_key")).strip()
            result = test_llm_connection(settings)
            return _json_response(self, 200 if result.get("ok") else 400, result)

        if path == "/api/login/init":
            plats = data.get("platforms") or STATE.platforms
            if isinstance(plats, str):
                plats = [x.strip() for x in plats.split(",") if x.strip()]
            plats = runner._norm_platforms(plats)
            if not plats:
                return _json_response(self, 400, {"ok": False, "error": "请先勾选平台"})
            LOGIN_PANEL.reset_for_platforms(plats)
            STATE.platforms = plats
            STATE.log(f"[登录面板] 初始化：{', '.join(plats)}")
            return _json_response(self, 200, {"ok": True, **LOGIN_PANEL.snapshot()})

        if path == "/api/login/open":
            pid = (data.get("platform") or data.get("id") or "").strip()
            if not pid:
                return _json_response(self, 400, {"ok": False, "error": "缺少 platform"})
            result = LOGIN_PANEL.open_platform(pid)
            return _json_response(self, 200 if result.get("ok") else 400, result)

        if path == "/api/login/verify":
            pid = (data.get("platform") or data.get("id") or "").strip()
            if not pid:
                return _json_response(self, 400, {"ok": False, "error": "缺少 platform"})
            # force=true：用户强制确认已登录
            if data.get("force"):
                result = LOGIN_PANEL.force_verify(pid)
            else:
                result = LOGIN_PANEL.verify_platform(pid)
            return _json_response(self, 200, result)

        if path == "/api/login/close":
            result = LOGIN_PANEL.close_browser()
            return _json_response(self, 200, {"ok": True, **result})

        if path == "/api/select-input":
            try:
                name = _excel_filename(str(data.get("name") or ""))
            except RequestBodyError as exc:
                return _json_response(self, exc.status, {"ok": False, "error": str(exc)})
            p = root / "data" / "input" / name
            if not p.is_file():
                return _json_response(self, 404, {"ok": False, "error": "文件不存在"})
            STATE.input_path = str(p)
            STATE.log(f"已选择：{name}")
            return _json_response(self, 200, {"ok": True, "path": str(p)})

        if path == "/api/parse":
            # 优先请求体 name → 已选 STATE.input_path → 单文件自动
            p: Path | None = None
            name = ""
            try:
                if data.get("name"):
                    name = _excel_filename(str(data.get("name") or ""))
                    p = root / "data" / "input" / name
                    if not p.is_file():
                        return _json_response(
                            self, 404, {"ok": False, "error": f"文件不存在：{name}"}
                        )
                    STATE.input_path = str(p.resolve())
            except RequestBodyError as exc:
                return _json_response(self, exc.status, {"ok": False, "error": str(exc)})
            if p is None and STATE.input_path:
                p = Path(STATE.input_path)
            result = runner.run_parse(p)
            return _json_response(self, 200, {**result, **STATE.snapshot()})

        if path == "/api/login-done":
            sig = agent_login_signal_path(root)
            sig.parent.mkdir(parents=True, exist_ok=True)
            sig.write_text("ok", encoding="utf-8")
            STATE.log("已确认登录完成（LOGIN_CONTINUE）")
            return _json_response(self, 200, {"ok": True})

        if path == "/api/items":
            # 完整材料列表（供勾选范围），可按 sheet 过滤
            sheet = str((data or {}).get("sheet") or "").strip()
            items = list(STATE.items_preview or [])
            if sheet:
                items = [it for it in items if str(it.get("sheet") or "") == sheet]
            with STATE.lock:
                sheet_counts = STATE._sheet_counts_unlocked()
            return _json_response(
                self,
                200,
                {
                    "ok": True,
                    "items": items[:3000],
                    "items_count": len(STATE.items_preview or []),
                    "filtered_count": len(items),
                    "sheet_counts": sheet_counts,
                    "item_scope": {
                        "mode": STATE.item_scope_mode,
                        "n": STATE.item_scope_n,
                        "sheets": list(STATE.item_scope_sheets),
                        "ids": list(STATE.item_scope_ids),
                    },
                },
            )

        if path == "/api/job/control":
            action = str((data or {}).get("action") or "").strip().lower()
            result = STATE.set_control(action)
            code = 200 if result.get("ok") else 400
            return _json_response(self, code, {**result, **STATE.snapshot()})

        if path == "/api/job/llm":
            # 询价过程中热切换 AI
            if "enabled" not in (data or {}):
                return _json_response(
                    self, 400, {"ok": False, "error": "需要 enabled: true/false"}
                )
            result = STATE.set_llm_runtime(bool(data.get("enabled")))
            return _json_response(self, 200, {**result, **STATE.snapshot()})

        if path == "/api/start":
            if STATE.phase in ("running", "paused"):
                return _json_response(
                    self,
                    409,
                    {
                        "ok": False,
                        "error": "已在运行/暂停中，请先停止或继续当前任务",
                        "phase": STATE.phase,
                    },
                )
            # 必须用请求体里的勾选覆盖，避免旧 settings 里的慧讯混入
            platforms = data.get("platforms") if data else None
            if isinstance(platforms, str):
                platforms = [x.strip() for x in platforms.split(",") if x.strip()]
            if not platforms:
                platforms = list(STATE.platforms) or list(load_settings(root).platforms_enabled)
            platforms = runner._norm_platforms(platforms)
            if not platforms:
                return _json_response(self, 400, {"ok": False, "error": "请先勾选平台"})
            try:
                # 缺省用当前 STATE（用户第①步已保存的值），不要静默掉回 3
                quotes = _bounded_int(
                    data.get("quotes_per_item")
                    if data.get("quotes_per_item") is not None
                    else data.get("quotes"),
                    field="每条价格数",
                    default=STATE.quotes_per_item or 3,
                    minimum=1,
                    maximum=10,
                )
                limit = _bounded_int(
                    data.get("limit"),
                    field="试跑条数",
                    default=STATE.limit if STATE.limit is not None else 0,
                    minimum=0,
                    maximum=100_000,
                )
            except RequestBodyError as exc:
                return _json_response(self, exc.status, {"ok": False, "error": str(exc)})
            # 询价范围：全部 / 前N / 按 sheet / 勾选 id
            scope = (data or {}).get("item_scope") if isinstance(data, dict) else None
            if not isinstance(scope, dict):
                scope = {}
            mode = str(
                scope.get("mode")
                or (data or {}).get("item_scope_mode")
                or STATE.item_scope_mode
                or "all"
            ).lower()
            # 兼容旧字段 limit：无 item_scope 时 limit>0 → first_n
            if not scope.get("mode") and not (data or {}).get("item_scope_mode"):
                if limit > 0:
                    mode = "first_n"
            try:
                scope_n = _bounded_int(
                    scope.get("n")
                    if scope.get("n") is not None
                    else (data or {}).get("item_scope_n", limit),
                    field="前N条",
                    default=limit or 0,
                    minimum=0,
                    maximum=100_000,
                )
            except RequestBodyError as exc:
                return _json_response(self, exc.status, {"ok": False, "error": str(exc)})
            scope_sheets = scope.get("sheets") or (data or {}).get("item_scope_sheets") or []
            if isinstance(scope_sheets, str):
                scope_sheets = [x.strip() for x in scope_sheets.split(",") if x.strip()]
            scope_ids = scope.get("ids") or (data or {}).get("item_scope_ids") or []
            if isinstance(scope_ids, str):
                scope_ids = [x.strip() for x in scope_ids.split(",") if x.strip()]
            runner.apply_item_scope(mode, scope_n, list(scope_sheets), list(scope_ids))
            if mode == "first_n":
                limit = scope_n
            elif mode in ("sheets", "ids"):
                limit = 0
            # 优先使用登录面板已验证的站；可强制 require_login_panel
            verified = LOGIN_PANEL.verified_ids()
            require_all = bool((data or {}).get("require_all_login", True))
            if require_all and platforms:
                missing = [p for p in platforms if p not in verified]
                if missing and not (data or {}).get("force"):
                    return _json_response(
                        self,
                        400,
                        {
                            "ok": False,
                            "error": f"请先在「登录面板」完成这些站：{', '.join(missing)}",
                            "pending": missing,
                            "verified": verified,
                        },
                    )
            skip_login = bool((data or {}).get("skip_login", False)) or (
                bool(verified) and set(platforms).issubset(set(verified))
            )
            match_mode = str((data or {}).get("match_mode") or "").strip().lower() or None
            runner.apply_settings(
                platforms, quotes, limit, skip_login, match_mode=match_mode
            )
            # apply_settings 可能把 first_n 又写一遍；再应用一次 scope 保证 sheets/ids 不丢
            runner.apply_item_scope(mode, scope_n, list(scope_sheets), list(scope_ids))
            with STATE.lock:
                sel_n = STATE._selected_count_unlocked()
            STATE.log(
                f"启动参数：K={STATE.quotes_per_item}  匹配={STATE.match_mode}"
                f"  范围={STATE.item_scope_mode} 预计{sel_n}条"
            )
            # apply_settings 会 reset 登录面板行，需保留 verified 状态
            if verified:
                LOGIN_PANEL.reset_for_platforms(platforms)
                for pid in verified:
                    if pid in LOGIN_PANEL.rows:
                        LOGIN_PANEL.rows[pid].status = "verified"
                        LOGIN_PANEL.rows[pid].message = "询价前已验证"
            # 登录浏览器由当前 HTTPServer 线程创建，也必须在同一线程关闭。
            # 后台询价随后会用同一 profile 启动自己的浏览器；提前释放可避免
            # Playwright 跨线程关闭异常和 profile already in use。
            try:
                LOGIN_PANEL.close_browser()
            except Exception as e:
                STATE.log(f"[登录面板] 启动询价前关闭浏览器失败: {e}")
            clear_agent_login_signal(root)
            # 继续询价：保留 evidence 跳过已完成；全新开始则清空进度计数
            continue_mode = bool((data or {}).get("continue"))
            STATE.continue_mode = continue_mode
            STATE.phase = "running"
            STATE.control = "run"
            STATE.error = ""
            if not continue_mode:
                STATE.full_k = STATE.partial = STATE.need_review = STATE.no_match = 0
                STATE.item_results = []
                STATE.result_by_sheet = []
            STATE.logs = []
            try:
                cfg0 = load_config(
                    root / "config.yaml" if (root / "config.yaml").exists() else None
                )
                st0 = get_user_settings(root, cfg0)
                STATE.reset_job_control(llm_default=bool(st0.llm_enabled))
                if (data or {}).get("llm_enabled") is not None:
                    STATE.llm_runtime_enabled = bool(data.get("llm_enabled"))
            except Exception:
                STATE.reset_job_control(llm_default=None)
            STATE.log(
                "继续询价（跳过已有合格价）…"
                if continue_mode
                else "开始新询价任务…"
            )
            STATE.log(
                f"任务启动…平台={', '.join(platforms)}；"
                f"登录面板已验证={', '.join(verified) or '无'}"
            )
            t = threading.Thread(target=runner.run_job_background, daemon=True)
            t.start()
            return _json_response(
                self,
                200,
                {
                    "ok": True,
                    "message": "已开始",
                    "platforms": platforms,
                    "verified": verified,
                },
            )

        self.send_error(404)


def run_server(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    root = project_root()
    (root / "data" / "input").mkdir(parents=True, exist_ok=True)
    (root / "data" / "output").mkdir(parents=True, exist_ok=True)
    (root / "data" / "user").mkdir(parents=True, exist_ok=True)

    # seed state from settings
    settings = load_settings(root)
    STATE.platforms = list(settings.platforms_enabled)
    STATE.quotes_per_item = settings.quotes_per_item or 3
    if STATE.platforms:
        LOGIN_PANEL.reset_for_platforms(STATE.platforms)

    # Playwright sync API 与创建它的线程绑定。登录面板的“打开”和“校验”是
    # 两个独立 HTTP 请求；ThreadingHTTPServer 会把它们分配到不同线程，导致
    # cannot switch to a different thread。向导请求量很小，单线程 HTTPServer
    # 能保证登录浏览器始终在同一线程操作；耗时询价仍由后台线程执行。
    httpd = HTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print("")
    print("=" * 56)
    print("  材料询价向导已启动（用户在浏览器操作，无需敲命令）")
    print(f"  打开: {url}")
    print("  登录：向导内「登录面板」分站打开/校验，全部通过后再询价")
    print("  停止: Ctrl+C")
    print("=" * 56)
    print("")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止向导服务")
    finally:
        httpd.server_close()
