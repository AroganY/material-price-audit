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

from ..login_gate import (
    check_url_for,
    ensure_logged_in_or_resume,
    install_zaojiatong_dialog_auto_accept,
    page_shows_session_conflict,
    probe_zaojiatong_market_session,
    try_handle_zaojiatong_session_conflict,
    try_resume_huixun_session,
    try_resume_zaojiatong_session,
    verify_logged_in,
)
from ..platforms import BUILTIN, load_platform_registry, normalize_platform_id
from ..runtime import load_config, project_root
from ..scraper import (
    clean_profile_locks,
    graceful_close_browser,
    kill_stale_profile_browsers,
    launch_context,
    profile_lock_present,
)
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

    @staticmethod
    def _is_target_closed_error(exc: BaseException) -> bool:
        msg = f"{type(exc).__name__}: {exc}".lower()
        return any(
            x in msg
            for x in (
                "targetclosed",
                "target page, context or browser has been closed",
                "browser has been closed",
                "context has been closed",
                "connection closed",
                "protocol error",
            )
        )

    def _alive_unlocked(self) -> bool:
        """页面/上下文仍可用才算活着；用户点 X 关窗后必须判死。"""
        try:
            if not self.page or not self.ctx:
                return False
            # Playwright: Page.is_closed()
            try:
                if bool(getattr(self.page, "is_closed", lambda: False)()):
                    return False
            except Exception:
                return False
            try:
                browser = getattr(self.ctx, "browser", None)
                if browser is not None and not browser.is_connected():
                    return False
            except Exception:
                pass
            # 读 url；已关闭会抛
            _ = self.page.url
            return True
        except Exception:
            return False

    def _invalidate_browser_refs(self) -> None:
        """不关进程，只丢掉已死句柄（用户手动关窗时用）。"""
        self.pw = None
        self.ctx = None
        self.page = None

    def _ensure_browser(self, *, force_new: bool = False) -> Any:
        root = project_root()
        profile = root / ".browser-profile"
        cfg = load_config(root / "config.yaml" if (root / "config.yaml").exists() else None)
        channel = (cfg.get("browser") or {}).get("channel") or "chrome"

        if not force_new and self._alive_unlocked():
            return self.page

        # 旧实例已死或强制重建
        if self.pw or self.ctx or self.page:
            try:
                self._close_quiet(force_kill=False)
            except Exception:
                self._invalidate_browser_refs()
        else:
            self._invalidate_browser_refs()

        # 仅当锁还在才杀残留，避免无意义地打掉刚写入的登录态
        if profile_lock_present(profile):
            kill_stale_profile_browsers(profile)
            clean_profile_locks(profile)
            time.sleep(0.4)
        else:
            clean_profile_locks(profile)

        last_err: Exception | None = None
        for attempt in range(2):
            try:
                self.pw, self.ctx, self.page = launch_context(
                    profile, channel=channel, headless=False
                )
                if self._alive_unlocked():
                    STATE.log("[登录面板] 已启动/重建浏览器")
                    return self.page
            except Exception as e:
                last_err = e
                STATE.log(f"[登录面板] 启动浏览器失败({attempt+1}/2): {e}")
                self._invalidate_browser_refs()
                kill_stale_profile_browsers(profile)
                clean_profile_locks(profile)
                time.sleep(0.6)
        raise RuntimeError(f"无法启动登录浏览器: {last_err}")

    def _goto(self, url: str, *, timeout: int = 30000, wait_ms: int = 600) -> Any:
        """
        带自动恢复的 goto：关窗 / TargetClosed 后重建浏览器再试一次。
        解决「校验完关浏览器 → 下一站打开报 Target page has been closed」。
        """
        page = self._ensure_browser()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if wait_ms:
                page.wait_for_timeout(wait_ms)
            return page
        except Exception as e:
            if not self._is_target_closed_error(e):
                raise
            STATE.log(f"[登录面板] 页面已关闭，自动重建浏览器后重试… ({e})")
            # 句柄作废，强制新开（Cookie 仍在 profile）
            try:
                self._close_quiet(force_kill=False)
            except Exception:
                self._invalidate_browser_refs()
            page = self._ensure_browser(force_new=True)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if wait_ms:
                page.wait_for_timeout(wait_ms)
            return page

    def _close_quiet(self, *, force_kill: bool = False) -> None:
        profile = project_root() / ".browser-profile"
        try:
            graceful_close_browser(
                self.pw,
                self.ctx,
                profile,
                force_kill=force_kill,
                flush_wait_s=1.0,
            )
        finally:
            self.pw = self.ctx = self.page = None

    def _start_zaojiatong_conflict_watcher(self) -> None:
        """登录页打开后轮询「账号使用中」弹窗并自动点继续登录。"""
        if getattr(self, "_zjt_watch_started", False):
            return
        self._zjt_watch_started = True

        def _loop() -> None:
            try:
                for _ in range(90):  # ~3 分钟，每 2 秒
                    time.sleep(2)
                    with self.lock:
                        page = self.page
                        alive = bool(page) and self._alive_unlocked()
                        active = self.active_platform == "zaojiatong"
                    if not alive or not active:
                        break
                    try:
                        if page_shows_session_conflict(page):
                            ok, lab = try_handle_zaojiatong_session_conflict(page)
                            if ok:
                                STATE.log(
                                    f"[登录面板] 造价通互踢弹窗已自动「{lab}」"
                                    "（会接管本机会话，其它端同账号会被挤下线）"
                                )
                    except Exception:
                        pass
            finally:
                self._zjt_watch_started = False

        threading.Thread(target=_loop, name="zjt-conflict-watch", daemon=True).start()

    def close_browser(self) -> dict[str, Any]:
        with self.lock:
            # 正常交接给询价：优雅关窗，尽量保留 Cookie；勿强杀
            self._close_quiet(force_kill=False)
            self.active_platform = ""
            self.busy = False
            self.last_error = ""
            self._zjt_watch_started = False
            STATE.log("[登录面板] 已关闭浏览器（登录 Cookie 保留在 .browser-profile；下一站会自动重开）")
            return self.snapshot()

    def open_url(self, url: str) -> dict[str, Any]:
        """
        在脚本浏览器（.browser-profile，已登录 Cookie）中打开链接。
        用于证据链跳转：不走系统默认浏览器，避免重新登录。
        """
        url = (url or "").strip()
        if not url:
            return {"ok": False, "error": "链接为空", **self.snapshot()}
        if not (url.startswith("http://") or url.startswith("https://")):
            return {"ok": False, "error": "仅支持 http(s) 链接", **self.snapshot()}
        with self.lock:
            if self.busy:
                return {
                    "ok": False,
                    "error": "浏览器正忙（登录/校验中），请稍后再试",
                    **self.snapshot(),
                }
            self.busy = True
            self.last_error = ""
            try:
                page = self._ensure_browser()
                # 新标签打开，保留当前登录页
                try:
                    if self.ctx is not None:
                        new_page = self.ctx.new_page()
                        new_page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        try:
                            new_page.bring_to_front()
                        except Exception:
                            pass
                        STATE.log(f"[证据链] 已在脚本浏览器新标签打开：{url[:120]}")
                        return {
                            "ok": True,
                            "url": url,
                            "message": "已在脚本浏览器中打开（复用登录态）",
                            **self.snapshot(),
                        }
                except Exception as e:
                    if not self._is_target_closed_error(e):
                        # 回退：当前页跳转
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=45000)
                            STATE.log(f"[证据链] 当前页打开：{url[:120]}")
                            return {
                                "ok": True,
                                "url": url,
                                "message": "已在脚本浏览器中打开",
                                **self.snapshot(),
                            }
                        except Exception as e2:
                            self.last_error = str(e2)
                            return {
                                "ok": False,
                                "error": f"打开失败：{e2}",
                                **self.snapshot(),
                            }
                    # TargetClosed：重建后再开
                    page = self._ensure_browser(force_new=True)
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    STATE.log(f"[证据链] 重建浏览器后打开：{url[:120]}")
                    return {
                        "ok": True,
                        "url": url,
                        "message": "已在脚本浏览器中打开（复用登录态）",
                        **self.snapshot(),
                    }
                # 无 ctx 时当前页打开
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                return {
                    "ok": True,
                    "url": url,
                    "message": "已在脚本浏览器中打开",
                    **self.snapshot(),
                }
            except Exception as e:
                self.last_error = str(e)
                STATE.log(f"[证据链] 打开失败：{e}")
                return {"ok": False, "error": f"打开失败：{e}", **self.snapshot()}
            finally:
                self.busy = False

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
            # 先去校验页探测会话：已登录则不要再把用户扔回登录页
            check = check_url_for(pid, login_url)
            already = False
            reason = ""
            try:
                page = self._goto(check, timeout=30000, wait_ms=800)
                # 慧讯：可能被踢回登录页但有「一键登录」
                already, reason = ensure_logged_in_or_resume(
                    page, pid, login_url, user_confirmed=False
                )
            except Exception as e:
                STATE.log(f"[登录面板] 预检会话失败 {pid}: {e}，打开登录页")
                already = False

            if already:
                with self.lock:
                    row = self.rows.get(pid)
                    if row:
                        row.status = "verified"
                        row.message = f"✓ 会话仍有效：{reason}"
                        row.checked_at = time.strftime("%H:%M:%S")
                    self.busy = False
                STATE.log(f"[登录面板] ✓ {name} 无需重登 — {reason}")
                self._save_persist()
                return {**self.snapshot(), "ok": True, "verified": True, "reused": True}

            # 会话无效：打开登录页（同样自动恢复关窗）
            page = self._goto(login_url, timeout=45000, wait_ms=800)
            # 慧讯：关窗重开常只需点「一键登录」
            if pid == "huixun":
                try:
                    ok_h, reason_h = try_resume_huixun_session(page)
                    if ok_h:
                        with self.lock:
                            row = self.rows.get(pid)
                            if row:
                                row.status = "verified"
                                row.message = f"✓ {reason_h}"
                                row.checked_at = time.strftime("%H:%M:%S")
                            self.busy = False
                        STATE.log(f"[登录面板] ✓ {name} — {reason_h}")
                        self._save_persist()
                        return {
                            **self.snapshot(),
                            "ok": True,
                            "verified": True,
                            "reused": True,
                        }
                    STATE.log(f"[登录面板] 慧讯一键登录未自动完成：{reason_h}")
                except Exception as e:
                    STATE.log(f"[登录面板] 慧讯一键登录异常: {e}")
            # 造价通：允许跳登录页 + 互踢弹窗自动「继续登录」
            if pid == "zaojiatong":
                try:
                    from ..adapters import zaojiatong as zjt

                    zjt.allow_login_navigation(page, True)
                    install_zaojiatong_dialog_auto_accept(page)
                    if page_shows_session_conflict(page):
                        ok_c, lab = try_handle_zaojiatong_session_conflict(page)
                        if ok_c:
                            STATE.log(f"[登录面板] 造价通已自动点「{lab}」")
                    STATE.log(
                        "[登录面板] 造价通：请在本窗口登录；若提示「账号正在使用中」点「继续登录」。"
                        "登完应跳到市场价列表，再点校验。"
                    )
                except Exception as e:
                    STATE.log(f"[登录面板] 造价通登录准备失败: {e}")

            with self.lock:
                row = self.rows.get(pid)
                if row:
                    # 重新打开登录页后必须重新校验，不能沿用旧通过状态。
                    row.status = "opened"
                    if pid == "huixun":
                        row.message = (
                            "已打开慧讯登录页。若已有账号信息，可点页面「一键登录」；"
                            "或点下方「本站已登录，校验」（程序也会尝试自动点）"
                        )
                    elif pid == "zaojiatong":
                        row.message = (
                            "已打开造价通登录页。若提示「账号正在使用中」，"
                            "点「继续登录」即可（表示踢掉其它端的同账号，不是没登过）。"
                            "程序也会自动点「继续登录」。登完后点「本站已登录，校验」。"
                        )
                    else:
                        row.message = "已打开登录页，请在浏览器登录，然后点「本站已登录，校验」"
                    row.checked_at = time.strftime("%H:%M:%S")
                self.busy = False
            # 造价通：后台盯 3 分钟，用户密码登录后若弹出互踢框则自动点「继续登录」
            if pid == "zaojiatong":
                self._start_zaojiatong_conflict_watcher()
            STATE.log(f"[登录面板] 已打开 {name}({pid}) 登录页")
            return {**self.snapshot(), "ok": True}
        except Exception as e:
            with self.lock:
                self.busy = False
                self.last_error = str(e)
                row = self.rows.get(pid)
                if row:
                    row.status = "failed"
                    row.message = f"打开失败: {e}"
            # 失败后清掉死句柄，避免下一站继续踩雷
            if self._is_target_closed_error(e):
                try:
                    self._close_quiet(force_kill=False)
                except Exception:
                    self._invalidate_browser_refs()
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
            # 关键：去首页/搜索页校验；关窗后自动重开再 goto
            check = check_url_for(pid, login_url)
            page = self._goto(check, timeout=30000, wait_ms=1200)
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            try:
                page.wait_for_timeout(400)
            except Exception:
                pass

            # 造价通：SPA 会把未登录会话踢回登录页，必须等鉴权完成
            if pid == "zaojiatong":
                try:
                    install_zaojiatong_dialog_auto_accept(page)
                    if page_shows_session_conflict(page):
                        ok_c, lab = try_handle_zaojiatong_session_conflict(page)
                        if ok_c:
                            STATE.log(f"[登录面板] 校验前已自动「{lab}」")
                            page.wait_for_timeout(1500)
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                ok, reason = try_resume_zaojiatong_session(page, timeout_ms=30000)
            else:
                # 慧讯等：产品页若跳回登录，自动点「一键登录」
                ok, reason = ensure_logged_in_or_resume(
                    page, pid, login_url, user_confirmed=True
                )
            if not ok and pid == "huixun":
                try:
                    page = self._goto(login_url, timeout=30000, wait_ms=800)
                    ok, reason = try_resume_huixun_session(page)
                except Exception as e:
                    reason = f"{reason}；重试一键登录失败: {e}"
            if not ok and pid == "zaojiatong":
                try:
                    # 仍失败时回到带 url 回跳的登录页，方便用户重登
                    page = self._goto(login_url, timeout=30000, wait_ms=800)
                    reason = (
                        f"{reason}。请在此页登录；成功后应自动跳回市场价，再点一次校验"
                    )
                except Exception as e:
                    reason = f"{reason}；打开登录页失败: {e}"
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
            if self._is_target_closed_error(e):
                try:
                    self._close_quiet(force_kill=False)
                except Exception:
                    self._invalidate_browser_refs()
            STATE.log(f"[登录面板] 校验异常 {pid}: {e}")
            return {**self.snapshot(), "ok": False, "verified": False, "error": str(e)}

    def force_verify(self, platform_id: str) -> dict[str, Any]:
        """
        用户强制标记已登录（最后手段）。

        会员站（广材/领材/慧讯/易择/造价通）**禁止**空标 verified：
        必须打开校验页/市场价探针，确认 Cookie 或正向文案，否则拒绝。
        否则 Worker 会当成已登录直接搜 → need_login → 再弹登录，且任务难继续。

        电商（京东/1688）允许在正确域名、无硬登录页时用户确认放行。
        """
        from ..login_gate import MEMBERSHIP_PLATFORMS

        pid = normalize_platform_id(platform_id)
        with self.lock:
            if pid not in self.rows:
                return {**self.snapshot(), "ok": False, "error": f"未知平台 {pid}"}
            row = self.rows[pid]
            name = row.name
            login_url = row.login_url

        # 造价通：市场价 SPA 专用探针
        if pid == "zaojiatong":
            try:
                with self.lock:
                    self.busy = True
                page = self._goto(
                    check_url_for(pid, login_url), timeout=30000, wait_ms=800
                )
                ok, reason = probe_zaojiatong_market_session(page, timeout_ms=30000)
                with self.lock:
                    row = self.rows.get(pid)
                    if row:
                        row.checked_at = time.strftime("%H:%M:%S")
                        if ok:
                            row.status = "verified"
                            row.message = f"✓ 市场价探针通过：{reason}"
                        else:
                            row.status = "failed"
                            row.message = f"强制确认无效：{reason}"
                    self.busy = False
                if ok:
                    STATE.log(f"[登录面板] ✓ {name} 强制确认经市场价探针通过 — {reason}")
                    self._save_persist()
                    return {**self.snapshot(), "ok": True, "verified": True, "reason": reason}
                STATE.log(f"[登录面板] ✗ {name} 强制确认被拒绝 — {reason}")
                return {
                    **self.snapshot(),
                    "ok": False,
                    "verified": False,
                    "error": reason,
                    "reason": reason,
                }
            except Exception as e:
                with self.lock:
                    self.busy = False
                    row = self.rows.get(pid)
                    if row:
                        row.status = "failed"
                        row.message = f"强制确认探针失败: {e}"
                return {**self.snapshot(), "ok": False, "verified": False, "error": str(e)}

        # 其它会员站：打开校验页 + 真检会话（Cookie/正向文案）
        if pid in MEMBERSHIP_PLATFORMS:
            try:
                with self.lock:
                    self.busy = True
                page = self._goto(
                    check_url_for(pid, login_url), timeout=30000, wait_ms=1000
                )
                ok, reason = ensure_logged_in_or_resume(
                    page, pid, login_url, user_confirmed=True
                )
                with self.lock:
                    row = self.rows.get(pid)
                    if row:
                        row.checked_at = time.strftime("%H:%M:%S")
                        if ok:
                            row.status = "verified"
                            row.message = f"✓ 强制确认经会话探针通过：{reason}"
                        else:
                            row.status = "failed"
                            row.message = (
                                f"强制确认无效：{reason}。"
                                "请在本工具弹出的浏览器完成登录后再点校验"
                                "（不要只点强制确认）"
                            )
                    self.busy = False
                if ok:
                    STATE.log(f"[登录面板] ✓ {name} 强制确认经探针通过 — {reason}")
                    self._save_persist()
                    return {
                        **self.snapshot(),
                        "ok": True,
                        "verified": True,
                        "reason": reason,
                    }
                STATE.log(f"[登录面板] ✗ {name} 强制确认被拒绝 — {reason}")
                return {
                    **self.snapshot(),
                    "ok": False,
                    "verified": False,
                    "error": reason,
                    "reason": reason,
                }
            except Exception as e:
                with self.lock:
                    self.busy = False
                    row = self.rows.get(pid)
                    if row:
                        row.status = "failed"
                        row.message = f"强制确认探针失败: {e}"
                return {
                    **self.snapshot(),
                    "ok": False,
                    "verified": False,
                    "error": str(e),
                }

        # 电商：用户确认 + 非硬登录页可放行
        with self.lock:
            row = self.rows[pid]
            row.status = "verified"
            row.message = "✓ 用户强制确认已登录"
            row.checked_at = time.strftime("%H:%M:%S")
            name = row.name
        STATE.log(f"[登录面板] ✓ {name} 用户强制确认已登录（电商）")
        self._save_persist()
        return {**self.snapshot(), "ok": True, "verified": True}

    def verified_ids(self) -> list[str]:
        with self.lock:
            return [r.id for r in self.rows.values() if r.status == "verified"]

    def pending_ids(self) -> list[str]:
        with self.lock:
            return [r.id for r in self.rows.values() if r.status != "verified"]


LOGIN_PANEL = LoginPanel()
