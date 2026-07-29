"""
独立登录面板：打开登录页 / 校验 / 持久化已通过状态。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..login_gate import check_url_for, verify_logged_in
from ..platforms import BUILTIN, load_platform_registry, normalize_platform_id
from ..runtime import load_config, project_root
from ..scraper import clean_profile_locks, kill_stale_profile_browsers, launch_context
from .job_state import STATE


def _persist_path() -> Path:
    return project_root() / "data" / "user" / "login_verified.json"


@dataclass
class PlatformLoginRow:
    id: str
    name: str
    login_url: str
    status: str = "pending"  # pending | remembered | opened | verified | failed
    message: str = "未登录"
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "login_url": self.login_url,
            "status": self.status,
            "message": self.message,
            "checked_at": self.checked_at,
        }


class LoginPanel:
    def __init__(self) -> None:
        # 可重入：snapshot 可能在已持锁路径被调用
        self.lock = threading.RLock()
        self.rows: dict[str, PlatformLoginRow] = {}
        self.active_platform: str = ""
        self.pw = None
        self.ctx = None
        self.page = None
        self.busy: bool = False
        self.last_error: str = ""
        self._load_persist()

    def _load_persist(self) -> dict[str, dict]:
        p = _persist_path()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_persist(self) -> None:
        """把 verified 站写入磁盘，避免刷新/init 丢状态。"""
        p = _persist_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {}
        with self.lock:
            for pid, row in self.rows.items():
                if row.status == "verified":
                    payload[pid] = {
                        "status": "verified",
                        "message": row.message,
                        "checked_at": row.checked_at,
                        "name": row.name,
                    }
        # merge with existing file for platforms not in current rows
        old = self._load_persist()
        old.update(payload)
        # drop non-verified of current session platforms only if we explicitly failed? keep old verified
        try:
            p.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def reset_for_platforms(self, platform_ids: list[str]) -> None:
        reg = load_platform_registry(
            load_config(
                project_root() / "config.yaml"
                if (project_root() / "config.yaml").exists()
                else None
            )
        )
        persisted = self._load_persist()
        with self.lock:
            old_verified = {
                k: v for k, v in self.rows.items() if v.status == "verified"
            }
            self.rows = {}
            for raw in platform_ids:
                pid = normalize_platform_id(raw)
                spec = reg.get(pid) or BUILTIN.get(pid)
                if not spec:
                    continue
                if pid in old_verified:
                    self.rows[pid] = old_verified[pid]
                elif pid in persisted and persisted[pid].get("status") == "verified":
                    self.rows[pid] = PlatformLoginRow(
                        id=pid,
                        name=spec.name,
                        login_url=spec.login_url or "",
                        status="remembered",
                        message="上次登录过，本次需要重新校验",
                        checked_at=persisted[pid].get("checked_at") or "",
                    )
                else:
                    self.rows[pid] = PlatformLoginRow(
                        id=pid,
                        name=spec.name,
                        login_url=spec.login_url or "",
                    )
            self.active_platform = ""
            self.last_error = ""
            self.busy = False

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            rows = [r.to_dict() for r in self.rows.values()]
            verified = [r.id for r in self.rows.values() if r.status == "verified"]
            pending = [r.id for r in self.rows.values() if r.status != "verified"]
            return {
                "platforms": rows,
                "verified": verified,
                "pending": pending,
                "active_platform": self.active_platform,
                "browser_alive": self._alive_unlocked(),
                "busy": self.busy,
                "all_verified": bool(rows) and len(pending) == 0,
                "last_error": self.last_error,
                "message": self._summary_unlocked(rows, verified, pending),
            }

    def _summary_unlocked(self, rows, verified, pending) -> str:
        if not rows:
            return "请先在第①步勾选平台"
        if not pending:
            return f"✓ 全部已登录（{len(verified)} 站），可以开始询价"
        names = []
        for r in rows:
            if r["id"] in pending:
                names.append(r["name"] or r["id"])
        return f"已通过 {len(verified)}/{len(rows)}，待登录：{'、'.join(names)}"

    def _alive_unlocked(self) -> bool:
        try:
            if not self.page:
                return False
            _ = self.page.url
            return True
        except Exception:
            return False

    def _ensure_browser(self) -> Any:
        root = project_root()
        profile = root / ".browser-profile"
        cfg = load_config(root / "config.yaml" if (root / "config.yaml").exists() else None)
        channel = (cfg.get("browser") or {}).get("channel") or "chrome"
        if self._alive_unlocked():
            return self.page
        self._close_quiet()
        # 避免「profile already in use」
        kill_stale_profile_browsers(profile)
        clean_profile_locks(profile)
        self.pw, self.ctx, self.page = launch_context(
            profile, channel=channel, headless=False
        )
        return self.page

    def _close_quiet(self) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.pw = self.ctx = self.page = None
        # 关闭后清锁，方便询价进程立刻接管同一 profile
        try:
            profile = project_root() / ".browser-profile"
            clean_profile_locks(profile)
        except Exception:
            pass

    def close_browser(self) -> dict[str, Any]:
        with self.lock:
            self._close_quiet()
            try:
                profile = project_root() / ".browser-profile"
                kill_stale_profile_browsers(profile)
                clean_profile_locks(profile)
            except Exception:
                pass
            self.active_platform = ""
            self.busy = False
            STATE.log("[登录面板] 已关闭浏览器（已通过状态仍保留）")
            return self.snapshot()

    def open_platform(self, platform_id: str) -> dict[str, Any]:
        pid = normalize_platform_id(platform_id)
        with self.lock:
            if pid not in self.rows:
                return {
                    **self.snapshot(),
                    "ok": False,
                    "error": f"未知平台 {pid}，请先保存勾选",
                }
            row = self.rows[pid]
            if not row.login_url:
                return {
                    **self.snapshot(),
                    "ok": False,
                    "error": f"{row.name} 无登录地址",
                }
            if self.busy:
                return {
                    **self.snapshot(),
                    "ok": False,
                    "error": "正在处理中，请稍候",
                }
            self.busy = True
            self.active_platform = pid
            self.last_error = ""
            login_url = row.login_url
            name = row.name
        try:
            page = self._ensure_browser()
            page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(600)
            with self.lock:
                row = self.rows.get(pid)
                if row:
                    # 重新打开登录页后必须重新校验，不能沿用旧通过状态。
                    row.status = "opened"
                    row.message = "已打开登录页，请在浏览器登录，然后点「本站已登录，校验」"
                    row.checked_at = time.strftime("%H:%M:%S")
                self.busy = False
            STATE.log(f"[登录面板] 已打开 {name}({pid})")
            return {**self.snapshot(), "ok": True}
        except Exception as e:
            with self.lock:
                self.busy = False
                self.last_error = str(e)
                row = self.rows.get(pid)
                if row:
                    row.status = "failed"
                    row.message = f"打开失败: {e}"
            STATE.log(f"[登录面板] 打开 {pid} 失败: {e}")
            return {**self.snapshot(), "ok": False, "error": str(e)}

    def verify_platform(self, platform_id: str) -> dict[str, Any]:
        """
        用户点「本站已登录，校验」：
        - 打开「首页/搜索页」而非 login URL
        - 用户确认模式：无登录表单即通过
        """
        pid = normalize_platform_id(platform_id)
        with self.lock:
            if pid not in self.rows:
                return {**self.snapshot(), "ok": False, "error": f"未知平台 {pid}"}
            if self.busy:
                return {**self.snapshot(), "ok": False, "error": "正在校验中，请稍候…"}
            row = self.rows[pid]
            self.busy = True
            self.active_platform = pid
            login_url = row.login_url
            name = row.name
        try:
            page = self._ensure_browser()
            # 关键：去首页/搜索页校验，不要再 goto /login（会误判卡住）
            check = check_url_for(pid, login_url)
            try:
                page.goto(check, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(800)
            except Exception as e:
                STATE.log(f"[登录面板] 打开校验页失败 {pid}: {e}，用当前页判断")

            ok, reason = verify_logged_in(
                page, pid, login_url, user_confirmed=True
            )
            with self.lock:
                row = self.rows.get(pid)
                if not row:
                    self.busy = False
                    return {**self.snapshot(), "ok": False, "error": "平台已变更"}
                row.checked_at = time.strftime("%H:%M:%S")
                if ok:
                    row.status = "verified"
                    row.message = f"✓ 已通过：{reason}"
                else:
                    row.status = "failed"
                    row.message = f"未通过：{reason}"
                self.busy = False
            if ok:
                STATE.log(f"[登录面板] ✓ {name} 已通过 — {reason}")
                self._save_persist()
            else:
                STATE.log(f"[登录面板] ✗ {name} — {reason}")
            return {
                **self.snapshot(),
                "ok": True,
                "verified": bool(ok),
                "reason": reason,
            }
        except Exception as e:
            with self.lock:
                self.busy = False
                self.last_error = str(e)
                row = self.rows.get(pid)
                if row:
                    row.status = "failed"
                    row.message = f"校验异常: {e}"
                    row.checked_at = time.strftime("%H:%M:%S")
            STATE.log(f"[登录面板] 校验异常 {pid}: {e}")
            return {**self.snapshot(), "ok": False, "verified": False, "error": str(e)}

    def force_verify(self, platform_id: str) -> dict[str, Any]:
        """用户强制标记已登录（最后手段）。"""
        pid = normalize_platform_id(platform_id)
        with self.lock:
            if pid not in self.rows:
                return {**self.snapshot(), "ok": False, "error": f"未知平台 {pid}"}
            row = self.rows[pid]
            row.status = "verified"
            row.message = "✓ 用户强制确认已登录"
            row.checked_at = time.strftime("%H:%M:%S")
            name = row.name
        STATE.log(f"[登录面板] ✓ {name} 用户强制确认已登录")
        self._save_persist()
        return {**self.snapshot(), "ok": True, "verified": True}

    def verified_ids(self) -> list[str]:
        with self.lock:
            return [r.id for r in self.rows.values() if r.status == "verified"]

    def pending_ids(self) -> list[str]:
        with self.lock:
            return [r.id for r in self.rows.values() if r.status != "verified"]


LOGIN_PANEL = LoginPanel()
