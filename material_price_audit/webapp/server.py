"""
本地向导服务：Agent 启动后用户只在浏览器操作，不敲命令。

  python -m material_price_audit serve
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import threading
import time
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
SERVER_PROCESS_STARTED_AT = time.time()
_CORE_SOURCE_FILES = (
    Path(__file__),
    Path(__file__).with_name("runner.py"),
    Path(__file__).parents[1] / "inquiry.py",
    Path(__file__).parents[1] / "scheduler.py",
    Path(__file__).parents[1] / "adapters" / "zaojiatong.py",
    STATIC_DIR / "index.html",
)


def _source_fingerprint() -> str:
    """轻量源码指纹：用来发现“页面进程没重启，还在跑旧内存代码”。"""
    parts: list[str] = []
    for path in _CORE_SOURCE_FILES:
        try:
            st = path.stat()
            parts.append(f"{path.name}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{path.name}:missing")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


SOURCE_FINGERPRINT_AT_START = _source_fingerprint()


def _service_identity() -> dict[str, Any]:
    current = _source_fingerprint()
    return {
        "pid": os.getpid(),
        "process_started_at": SERVER_PROCESS_STARTED_AT,
        "source_fingerprint": SOURCE_FINGERPRINT_AT_START,
        "current_source_fingerprint": current,
        "restart_required": current != SOURCE_FINGERPRINT_AT_START,
    }


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
            "cost": platform_id in ("guangcai", "huixun", "lingcai", "yize", "zaojiatong"),
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

        if path == "/api/health":
            return _json_response(
                self,
                200,
                {"ok": True, "version": __version__, **_service_identity()},
            )

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
            snap["service"] = _service_identity()
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
            snap["baidu_fallback_enabled"] = bool(
                getattr(settings, "baidu_fallback_enabled", False)
            )
            snap["default_region"] = dict(
                getattr(settings, "default_region", None) or {}
            )
            snap["region_strategy"] = (
                getattr(settings, "region_strategy", None) or "strict_city"
            )
            snap["region_required"] = bool(
                getattr(settings, "region_required", False)
            )
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
        if path == "/api/history":
            from .job_history import load_history

            jobs = load_history(root, limit=50)
            lite = [
                {
                    "id": j.get("id"),
                    "ts": j.get("ts"),
                    "phase": j.get("phase"),
                    "platforms": j.get("platforms"),
                    "full_k": j.get("full_k"),
                    "partial": j.get("partial"),
                    "need_review": j.get("need_review"),
                    "no_match": j.get("no_match"),
                    "tokens": j.get("tokens"),
                    "items_done": j.get("items_done"),
                    "message": j.get("message"),
                    "result_path": j.get("result_path"),
                    "rfq_path": j.get("rfq_path"),
                    "has_files": bool(j.get("result_path") or j.get("rfq_path")),
                }
                for j in jobs
            ]
            return _json_response(
                self, 200, {"ok": True, "jobs": lite, "total": len(lite)}
            )

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
            baidu_fb = data.get("baidu_fallback_enabled")
            if baidu_fb is None:
                baidu_fb_arg = None
            else:
                baidu_fb_arg = bool(baidu_fb)
            def_reg = data.get("default_region")
            if not isinstance(def_reg, dict):
                def_reg = None
            reg_strat = data.get("region_strategy")
            reg_req = data.get("region_required")
            runner.apply_settings(
                platforms,
                quotes,
                limit,
                skip_login,
                llm=llm_payload,
                match_mode=match_mode,
                baidu_fallback_enabled=baidu_fb_arg,
                default_region=def_reg,
                region_strategy=str(reg_strat) if reg_strat is not None else None,
                region_required=bool(reg_req) if reg_req is not None else None,
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
                    "baidu_fallback_enabled": bool(
                        getattr(settings, "baidu_fallback_enabled", False)
                    ),
                    "default_region": dict(
                        getattr(settings, "default_region", None) or {}
                    ),
                    "region_strategy": getattr(
                        settings, "region_strategy", None
                    )
                    or "strict_city",
                    "region_required": bool(
                        getattr(settings, "region_required", False)
                    ),
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

        if path == "/api/history/load":
            import copy

            from .job_history import get_job

            job_id = str((data or {}).get("id") or "")
            job = get_job(root, job_id) if job_id else None
            if not job:
                return _json_response(self, 404, {"ok": False, "error": "任务不存在"})
            # 深拷贝；不写回 STATE，避免历史结果污染「当前任务」面板
            item_rows = copy.deepcopy(list(job.get("item_results") or []))
            payload = {
                "ok": True,
                "viewing_history": True,
                "phase": "history",
                "run_id": str(job.get("run_id") or job.get("id") or ""),
                "full_k": int(job.get("full_k") or 0),
                "partial": int(job.get("partial") or 0),
                "need_review": int(job.get("need_review") or 0),
                "no_match": int(job.get("no_match") or 0),
                "message": str(job.get("message") or "历史任务"),
                "result_path": str(job.get("result_path") or ""),
                "rfq_path": str(job.get("rfq_path") or ""),
                "evidence_path": str(job.get("evidence_path") or ""),
                "item_results": item_rows,
                "result_by_sheet": copy.deepcopy(list(job.get("result_by_sheet") or [])),
                "job": {
                    "id": job.get("id"),
                    "ts": job.get("ts"),
                    "message": job.get("message"),
                    "run_id": job.get("run_id") or job.get("id"),
                    "items_done": len(item_rows),
                    "phase": job.get("phase") or "done",
                },
            }
            return _json_response(self, 200, payload)

        if path == "/api/open-url":
            # 用脚本浏览器（.browser-profile 登录态）打开证据链，避免系统浏览器重登
            url = str((data or {}).get("url") or "").strip()
            if not url or not (url.startswith("http://") or url.startswith("https://")):
                return _json_response(
                    self, 400, {"ok": False, "error": "请提供 http(s) 链接"}
                )
            if STATE.phase in ("running", "login", "paused"):
                return _json_response(
                    self,
                    409,
                    {
                        "ok": False,
                        "error": "询价进行中，请结束后再打开证据（避免抢浏览器）",
                    },
                )
            result = LOGIN_PANEL.open_url(url)
            code = 200 if result.get("ok") else 400
            return _json_response(self, code, result)

        if path == "/api/history/delete":
            from .job_history import delete_job, delete_jobs

            payload = data if isinstance(data, dict) else {}
            delete_files = bool(payload.get("delete_files"))
            # 单条 id 或批量 ids
            ids = payload.get("ids")
            if isinstance(ids, list) and ids:
                result = delete_jobs(
                    root, [str(x) for x in ids], delete_files=delete_files
                )
                return _json_response(self, 200, result)
            job_id = str(payload.get("id") or "")
            if not job_id:
                return _json_response(
                    self, 400, {"ok": False, "error": "请提供 id 或 ids"}
                )
            result = delete_job(root, job_id, delete_files=delete_files)
            code = 200 if result.get("ok") else 404
            return _json_response(self, code, result)

        if path == "/api/history/clear":
            from .job_history import clear_history

            payload = data if isinstance(data, dict) else {}
            delete_files = bool(payload.get("delete_files"))
            result = clear_history(root, delete_files=delete_files)
            return _json_response(self, 200, result)

        if path == "/api/llm/ready":
            # 运行前 AI 是否真的能用（可选 probe 真连通）
            from ..schema_map import check_llm_readiness

            cfg = load_config(root / "config.yaml" if (root / "config.yaml").exists() else None)
            settings = get_user_settings(root, cfg)
            probe = False
            if isinstance(data, dict):
                probe = bool(data.get("probe"))
                if "enabled" in data:
                    settings.llm_enabled = bool(data.get("enabled"))
                if data.get("api_base") is not None:
                    settings.llm_api_base = str(data.get("api_base") or "").strip()
                if data.get("model") is not None:
                    settings.llm_model = str(data.get("model") or "gpt-4o-mini").strip()
                if data.get("api_key_env") is not None:
                    settings.llm_api_key_env = str(data.get("api_key_env") or "OPENAI_API_KEY")
                if data.get("api_key"):
                    settings.llm_api_key = str(data.get("api_key")).strip()
                if data.get("use_for") is not None and isinstance(data.get("use_for"), list):
                    settings.llm_use_for = [str(x) for x in data["use_for"]]
            # 运行时开关：询价页关掉 AI 则视为不想用
            if isinstance(data, dict) and "runtime_enabled" in data:
                if not bool(data.get("runtime_enabled")):
                    settings.llm_enabled = False
            result = check_llm_readiness(settings, probe=probe)
            return _json_response(self, 200, {"ok": True, **result})

        if path == "/api/llm/test":
            # 可用请求体临时覆盖 Key（不落盘）做连通性测试
            from ..schema_map import test_llm_connection

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
            # 成功则记入本会话，供开始询价时复用（免重复探测）
            if result.get("ok"):
                try:
                    STATE.llm_status = dict(STATE.llm_status or {})
                    STATE.llm_status["last_probe_ok"] = True
                    STATE.llm_status["last_probe_ts"] = __import__("time").time()
                    STATE.llm_status["last_probe_model"] = settings.llm_model or ""
                    STATE.log(
                        f"[AI·探测] 连接成功 model={settings.llm_model or '?'} — "
                        f"可以开着 AI 跑询价"
                    )
                except Exception:
                    pass
            else:
                try:
                    STATE.llm_status = dict(STATE.llm_status or {})
                    STATE.llm_status["last_probe_ok"] = False
                    STATE.llm_status["last_probe_error"] = result.get("error") or ""
                    STATE.log(
                        f"[AI·探测] 失败 — {result.get('error') or '未知错误'}；"
                        f"请勿在未修好前依赖 AI"
                    )
                except Exception:
                    pass
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

        if path == "/api/name-alias/confirm":
            # 人工确认品名同义 / 负向映射 → 本地库 Token=0 学习
            from ..name_aliases import confirm_different_names, confirm_same_names

            same = bool((data or {}).get("same", True))
            a = str((data or {}).get("inquiry_name") or (data or {}).get("a") or "").strip()
            b = str(
                (data or {}).get("candidate_name") or (data or {}).get("b") or ""
            ).strip()
            if not a or not b:
                return _json_response(
                    self, 400, {"ok": False, "error": "需要 inquiry_name 与 candidate_name"}
                )
            try:
                if same:
                    result = confirm_same_names(
                        a, b, root, source="user_confirmed", confidence=1.0
                    )
                else:
                    result = confirm_different_names(a, b, root, source="user_confirmed")
                return _json_response(self, 200, result)
            except Exception as e:
                return _json_response(self, 500, {"ok": False, "error": str(e)})

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

            # —— AI 运行前硬检查：开了却不能用必须拦住 ——
            try:
                from ..schema_map import check_llm_readiness

                cfg_ai = load_config(
                    root / "config.yaml" if (root / "config.yaml").exists() else None
                )
                st_ai = get_user_settings(root, cfg_ai)
                runtime_ai = (data or {}).get("llm_enabled")
                if runtime_ai is not None:
                    want_ai = bool(runtime_ai)
                else:
                    want_ai = bool(st_ai.llm_enabled)
                st_ai.llm_enabled = want_ai
                force_rules = bool((data or {}).get("llm_force_rules"))
                skip_probe = bool((data or {}).get("llm_skip_probe"))
                last_ok = bool((STATE.llm_status or {}).get("last_probe_ok"))
                last_ts = float((STATE.llm_status or {}).get("last_probe_ts") or 0)
                recent_ok = last_ok and (time.time() - last_ts < 600)
                need_probe = (
                    want_ai and not force_rules and not skip_probe and not recent_ok
                )
                ready = check_llm_readiness(st_ai, probe=need_probe)
                if want_ai and not ready.get("ok"):
                    if force_rules:
                        with STATE.lock:
                            STATE.llm_runtime_enabled = False
                        STATE.log(
                            f"[AI·拦截] 用户选择纯规则继续 — {ready.get('summary')}"
                        )
                    else:
                        return _json_response(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": ready.get("summary")
                                or "AI 已开启但当前不可用",
                                "llm_ready": ready,
                                "hint": ready.get("hint")
                                or "请修好 AI，或确认后用纯规则启动",
                                "code": "llm_not_ready",
                            },
                        )
                elif want_ai and ready.get("ok"):
                    STATE.log(f"[AI·就绪] {ready.get('summary')}")
                    if need_probe and ready.get("live_ok"):
                        with STATE.lock:
                            st = dict(STATE.llm_status or {})
                            st["last_probe_ok"] = True
                            st["last_probe_ts"] = time.time()
                            STATE.llm_status = st
                elif not want_ai:
                    STATE.log("[AI·就绪] 本任务不使用 AI（纯规则）")
            except Exception as e:
                STATE.log(f"[AI·就绪] 检查异常，将按配置继续: {e}")

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
    print(f"  源码指纹: {SOURCE_FINGERPRINT_AT_START}  pid={os.getpid()}")
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
