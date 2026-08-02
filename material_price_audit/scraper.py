"""Playwright scrapers — only accept title-matched candidates with prices."""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    text = str(text).replace(",", "").replace("￥", "¥")
    m = re.search(r"(\d+\.?\d*)", text)
    if not m:
        return None
    try:
        v = float(m.group(1))
        if 0.01 < v < 5_000_000:
            return v
    except Exception:
        return None
    return None


def score_title(title: str, must: list[str]) -> int:
    """
    标题与 must 命中分。中文材料站标题常比询价名短/异序：
    「薄壁不锈钢管」vs 标题「不锈钢管」应至少得 1 分，不能整串硬比。
    """
    import re

    raw = (title or "")
    t = re.sub(r"\s+", "", raw).lower()
    if not t:
        return 0
    score = 0
    for m in must or []:
        if not m:
            continue
        mm = re.sub(r"\s+", "", str(m)).lower()
        if not mm:
            continue
        if mm in t or t in mm:
            score += 1
            continue
        # 中文：连续 2 字片段命中也算（薄壁/不锈钢/钢管）
        cn = re.findall(r"[\u4e00-\u9fff]{2,4}", mm)
        if cn and any(frag in t for frag in cn):
            score += 1
            continue
        # 型号/口径：DN100、XZP100
        alnum = re.findall(r"[a-z]{0,8}\d{2,}[a-z0-9\-_]*", mm, flags=re.I)
        if alnum and any(a.lower() in t for a in alnum):
            score += 1
    return score


def clean_profile_locks(profile_dir: Path) -> list[str]:
    """
    清理 Chromium 残留锁文件（SingletonLock 等）。
    上一实例未退出 / 登录面板与询价抢同一 profile 时会出现。
    注意：不要在浏览器尚未优雅退出时调用，否则可能截断 Cookie 落盘。
    """
    removed: list[str] = []
    profile_dir = Path(profile_dir)
    if not profile_dir.exists():
        return removed
    for name in (
        "SingletonLock",
        "SingletonSocket",
        "SingletonCookie",
        "lockfile",
        ".parentlock",
    ):
        p = profile_dir / name
        try:
            if p.exists() or p.is_symlink():
                p.unlink()
                removed.append(name)
        except Exception:
            pass
    return removed


def profile_lock_present(profile_dir: Path) -> bool:
    profile_dir = Path(profile_dir)
    for name in ("SingletonLock", "SingletonSocket", "lockfile", ".parentlock"):
        p = profile_dir / name
        if p.exists() or p.is_symlink():
            return True
    return False


def graceful_close_browser(
    pw,
    ctx,
    profile_dir: Path | None = None,
    *,
    force_kill: bool = False,
    flush_wait_s: float = 1.2,
) -> None:
    """
    优雅关闭持久化浏览器，尽量让 Cookie/LocalStorage 写回 user-data-dir。

    旧逻辑在 ctx.close() 后立刻 kill + 删锁，容易把尚未落盘的登录态冲掉，
    表现为：登录面板登完 → 询价又要再登。
    """
    import time as _t

    # 1) 触发存储序列化（有助于 Cookie 落盘）
    try:
        if ctx is not None:
            try:
                ctx.storage_state()
            except Exception:
                pass
            try:
                for page in list(getattr(ctx, "pages", []) or []):
                    try:
                        page.close()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass
    except Exception:
        pass

    # 2) 停 Playwright 驱动
    try:
        if pw is not None:
            pw.stop()
    except Exception:
        pass

    # 3) 等 Chromium 子进程自己写完磁盘
    _t.sleep(max(0.3, float(flush_wait_s)))

    if profile_dir is None:
        return
    profile_dir = Path(profile_dir)

    # 4) 仅在仍被占用 / 强制时才杀进程；正常关闭不要 SIGKILL
    if force_kill or profile_lock_present(profile_dir):
        n = kill_stale_profile_browsers(profile_dir)
        if n:
            _t.sleep(0.4)
    clean_profile_locks(profile_dir)


def kill_stale_profile_browsers(profile_dir: Path) -> int:
    """尽量结束占用该 user-data-dir 的 Chromium（仅匹配本项目 profile 路径）。"""
    import subprocess

    target = str(Path(profile_dir).resolve())
    killed = 0
    try:
        # macOS / Linux
        out = subprocess.check_output(["ps", "ax", "-o", "pid=,command="], text=True)
    except Exception:
        return 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if target not in line:
            continue
        if "chrome" not in line.lower() and "chromium" not in line.lower():
            continue
        # 避免误杀本 shell
        try:
            pid_s = line.split(None, 1)[0]
            pid = int(pid_s)
        except Exception:
            continue
        try:
            import os
            import signal

            os.kill(pid, signal.SIGTERM)
            killed += 1
        except Exception:
            pass
    if killed:
        import time as _t

        _t.sleep(0.8)
        # 仍活着的强制杀
        try:
            out2 = subprocess.check_output(["ps", "ax", "-o", "pid=,command="], text=True)
            for line in out2.splitlines():
                if target not in line:
                    continue
                if "chrome" not in line.lower() and "chromium" not in line.lower():
                    continue
                try:
                    pid = int(line.split(None, 1)[0])
                    import os
                    import signal

                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
        except Exception:
            pass
        _t.sleep(0.3)
    return killed


def launch_ephemeral_with_state(
    storage_state: dict | str | Path | None,
    channel: str = "chrome",
    headless: bool = False,
):
    """
    非持久化浏览器 + 注入 storage_state（Cookie/LocalStorage）。
    用于「一平台一 Worker」并发：各 Worker 独立 Browser，共享登录态，
    互不抢 .browser-profile 的 user-data-dir 锁。
    返回 (pw, context, page, browser)。
    """
    from playwright.sync_api import sync_playwright

    state = storage_state
    if isinstance(state, (str, Path)):
        p = Path(state)
        if p.is_file():
            import json

            state = json.loads(p.read_text(encoding="utf-8"))
        else:
            state = None

    pw = sync_playwright().start()
    try:
        launch_kwargs = dict(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            browser = pw.chromium.launch(channel=channel, **launch_kwargs)
        except Exception:
            browser = pw.chromium.launch(**launch_kwargs)
        ctx_kwargs = dict(
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
        )
        if state:
            ctx_kwargs["storage_state"] = state
        context = browser.new_context(**ctx_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        return pw, context, page, browser
    except Exception:
        try:
            pw.stop()
        except Exception:
            pass
        raise


def launch_context(profile_dir: Path, channel: str = "chrome", headless: bool = False):
    """
    启动持久化浏览器（同一 user-data-dir 复用登录 Cookie）。
    若 profile 被占用：先优雅等待 → 仍占用再杀残留 → 清锁 → 重试。
    """
    from playwright.sync_api import sync_playwright

    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    def _do_launch(pw):
        kwargs = dict(
            user_data_dir=str(profile_dir.resolve()),
            headless=headless,
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            return pw.chromium.launch_persistent_context(channel=channel, **kwargs)
        except Exception:
            return pw.chromium.launch_persistent_context(**kwargs)

    last_err: Exception | None = None
    for attempt in range(3):
        pw = sync_playwright().start()
        try:
            context = _do_launch(pw)
            page = context.pages[0] if context.pages else context.new_page()
            return pw, context, page
        except Exception as e:
            last_err = e
            try:
                pw.stop()
            except Exception:
                pass
            msg = str(e).lower()
            busy = (
                "singleton" in msg
                or "profile is already in use" in msg
                or "processsingleton" in msg
                or "singletonlock" in msg
            )
            print(f"[browser] 启动失败 (attempt {attempt+1}/3): {type(e).__name__}: {e}")
            if busy or attempt < 2:
                import time as _t

                # 先等一等，给上一实例（登录面板）时间把 Cookie 写完并退出
                _t.sleep(0.8 if attempt == 0 else 0.5)
                n = 0
                if profile_lock_present(profile_dir) or busy:
                    n = kill_stale_profile_browsers(profile_dir)
                removed = clean_profile_locks(profile_dir)
                print(
                    f"[browser] 释放 profile：killed≈{n} 进程，"
                    f"清理锁文件={removed or '无'}"
                )
                _t.sleep(0.5)
            else:
                break
    raise RuntimeError(
        f"无法启动浏览器（profile 可能仍被占用）: {last_err}\n"
        f"请关闭所有询价相关 Chrome 窗口后重试，或删除锁文件：\n"
        f"  rm -f '{profile_dir}/SingletonLock' '{profile_dir}/SingletonSocket'"
    ) from last_err


# 仅 URL 判定登录态 —— 禁止狂扫 DOM（会触发 SPA 反复重绘/像刷新）
_LOGIN_URL_MARKERS = (
    "/login",
    "/signin",
    "/sign-in",
    "/passport",
    "passport.",
    "login.taobao",
    "login.1688",
    "login.jd",
    "/sso",
    "apply_trial",
)


def _safe_url(page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def url_looks_like_login(url: str) -> bool:
    u = (url or "").lower()
    if not u:
        return False
    return any(m in u for m in _LOGIN_URL_MARKERS)


def url_left_login(start_url: str, cur_url: str, platform_id: str = "") -> bool:
    """Passive: user navigated away from login URL → success."""
    s = (start_url or "").lower().split("?")[0]
    c = (cur_url or "").lower()
    if not c:
        return False
    # still on a hard login URL
    if url_looks_like_login(c) and "/userinfo" not in c:
        return False
    # URL changed from the page we opened
    c0 = c.split("?")[0]
    if s and c0 != s and not url_looks_like_login(c):
        # 领材：userInfo 是登录入口本身，停在 userInfo 不算「已离开登录」
        pid0 = (platform_id or "").lower()
        if pid0 == "lingcai" and "hylcw.cn" in c and "userinfo" in c:
            return False
        return True
    # opened login, now not login
    if url_looks_like_login(s) and not url_looks_like_login(c):
        return True
    pid = (platform_id or "").lower()
    if pid == "lingcai":
        # 领材：只有从登录/用户中心进入市场价/业务页才算离开登录。
        # 禁止：打开 userInfo 时立刻把「同域非 /login」当成已登录（会 0 秒假通过，
        # 随后校验失败 → 用户感觉「登完任务却不继续」）。
        if "hylcw.cn" not in c or url_looks_like_login(c):
            return False
        if "userinfo" in c:
            return False
        # 到达市场价/首页等业务路径
        if any(
            x in c
            for x in (
                "/marketprice",
                "/market",
                "/so.html",
                "/lcindex",
                "/index",
            )
        ):
            return True
        # start 是登录/userInfo，当前已不在这些入口
        if ("/login" in s or "userinfo" in s) and "userinfo" not in c and "/login" not in c:
            return True
    return False


def page_looks_logged_in(page, platform_id: str = "", login_url: str = "") -> bool:
    """URL-only 粗判，不碰 DOM。"""
    cur = _safe_url(page)
    if url_looks_like_login(cur) and "userinfo" not in cur.lower():
        return False
    if login_url and url_left_login(login_url, cur, platform_id):
        return True
    # 不在登录 URL 上 → 倾向已可用
    if not url_looks_like_login(cur):
        return True
    return False


def agent_login_signal_path(package_root: Path | None = None) -> Path:
    root = package_root or Path(__file__).resolve().parents[1]
    return root / "data" / "output" / "LOGIN_CONTINUE"


def clear_agent_login_signal(package_root: Path | None = None) -> None:
    p = agent_login_signal_path(package_root)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


def agent_login_signaled(package_root: Path | None = None) -> bool:
    return agent_login_signal_path(package_root).exists()


def _lingcai_login_surface(url: str) -> bool:
    """领材登录入口：/login* 或用户中心 userInfo（非市场价业务页）。"""
    u = (url or "").lower()
    if not u or "hylcw.cn" not in u:
        return False
    if url_looks_like_login(u):
        return True
    return "userinfo" in u


def wait_for_login_agent(
    page,
    *,
    platform_id: str,
    name: str,
    login_url: str,
    package_root: Path | None = None,
    timeout_s: int = 600,
    poll_s: float = 1.5,
    allow_stdin: bool = True,
    cancel_check=None,
    on_wait_start=None,
) -> str:
    """
    Agent 友好登录等待：
    - **绝不** page.reload / 重复 goto / 扫 DOM（URL 轮询除外）
    - 只读 page.url（被动）+ 可选轻量 Cookie 探测
    - Agent / 向导可 touch data/output/LOGIN_CONTINUE 立刻放行
    - TTY 下也可回车放行
    返回: already_ok | logged_in | agent_continue | timeout | cancelled
    """
    root = package_root or Path(__file__).resolve().parents[1]
    clear_agent_login_signal(root)
    start_url = _safe_url(page) or login_url
    pid = (platform_id or "").lower()

    # 仅当「当前/目标」明确是登录页且已离开 → 秒过
    # 禁止：login_url 是首页时把「随便一个非 login URL」当成已登录（1688 曾中招）
    hard_login = url_looks_like_login(start_url) or url_looks_like_login(login_url)
    # 领材 userInfo 是真·登录入口，必须按「硬登录」等满时长，禁止 45s 假超时
    if pid == "lingcai" and (
        _lingcai_login_surface(start_url) or _lingcai_login_surface(login_url)
    ):
        hard_login = True
    # 会员站统一给足等待（用户可能要扫码/短信）
    membership = pid in ("guangcai", "lingcai", "huixun", "yize", "zaojiatong")

    if hard_login and page_looks_logged_in(page, platform_id, login_url):
        cur0 = _safe_url(page)
        if not url_looks_like_login(cur0) and not (
            pid == "lingcai" and _lingcai_login_surface(cur0)
        ):
            print(f"  [{name}] ✓ 已离开登录页，继续")
            return "already_ok"
    if (
        hard_login
        and url_looks_like_login(start_url)
        and not url_looks_like_login(_safe_url(page))
        and not (pid == "lingcai" and _lingcai_login_surface(_safe_url(page)))
    ):
        print(f"  [{name}] ✓ 当前不是登录 URL，继续")
        return "already_ok"
    # 首页类 login_url 且当前也不像登录页：不假定已登录，仍等 Agent/短超时
    # 会员站 / 领材 userInfo 不走短超时
    if (
        not hard_login
        and not membership
        and not url_looks_like_login(_safe_url(page))
    ):
        print(f"  [{name}] 目标不是登录页；请确认已登录或 touch LOGIN_CONTINUE 跳过")
        # 缩短：首页探测最多 45s，避免死等
        timeout_s = min(int(timeout_s), 45)
    elif membership and int(timeout_s) > 0:
        # 默认至少 3 分钟，避免扫码未完成就被跳过；
        # 调用方显式传入更短 timeout（如测试）时不强制抬高
        if int(timeout_s) >= 60:
            timeout_s = max(int(timeout_s), 180)

    sig = agent_login_signal_path(root)
    print("")
    print("========== LOGIN_WAIT (agent) ==========")
    print(f"platform : {platform_id} / {name}")
    print(f"login_url: {login_url}")
    print(f"browser  : {_safe_url(page)[:100]}")
    print("请用户在【已打开的浏览器窗口】登录。")
    print("程序只被动看 URL / Cookie，不会刷新页面。")
    print("登完后任选：")
    print("  1) 向导点「我已登录，继续」")
    print(f"  2) touch {sig}")
    print("  3) 终端回车（若有 TTY）")
    print("  4) URL 自动离开登录页也会继续")
    print("========================================")

    if on_wait_start is not None:
        try:
            on_wait_start(
                {
                    "platform": platform_id,
                    "name": name,
                    "login_url": login_url,
                    "timeout_s": int(timeout_s),
                    "signal": str(sig),
                }
            )
        except Exception:
            pass

    # 允许测试传入短 timeout；生产默认调用一般 ≥180s
    deadline = time.time() + max(1, int(timeout_s))
    last_log = 0.0
    stdin_ok = allow_stdin and sys.stdin.isatty()

    def _cookie_session_ready() -> bool:
        """轻量 Cookie 探测：有登录会话 Cookie 即视为可继续（不扫 DOM、不刷新）。"""
        try:
            from .login_gate import auth_cookie_hits

            hits = auth_cookie_hits(page, pid)
            return bool(hits)
        except Exception:
            return False

    # 非阻塞读回车：用 select
    while time.time() < deadline:
        if cancel_check is not None:
            try:
                if cancel_check():
                    print(f"  [{name}] 已取消登录/验证等待")
                    clear_agent_login_signal(root)
                    return "cancelled"
            except Exception:
                pass
        cur = _safe_url(page)
        if url_left_login(start_url, cur, platform_id) or (
            not url_looks_like_login(cur)
            and url_looks_like_login(start_url)
            and not (pid == "lingcai" and _lingcai_login_surface(cur))
        ):
            print(f"  [{name}] ✓ URL 已离开登录页 → {_safe_url(page)[:80]}")
            clear_agent_login_signal(root)
            return "logged_in"

        # 仍停在 userInfo/登录页时：若 Cookie 已写入，也可放行（领材常见）
        # 后续 _login_one 会再 goto 校验页真检
        if membership and _cookie_session_ready():
            print(f"  [{name}] ✓ 检测到登录 Cookie，继续校验")
            clear_agent_login_signal(root)
            return "logged_in"

        if agent_login_signaled(root):
            print(f"  [{name}] ✓ Agent 信号 LOGIN_CONTINUE")
            clear_agent_login_signal(root)
            return "agent_continue"

        if stdin_ok:
            try:
                import select

                r, _, _ = select.select([sys.stdin], [], [], 0)
                if r:
                    try:
                        sys.stdin.readline()
                    except Exception:
                        pass
                    print(f"  [{name}] ✓ 终端回车确认")
                    clear_agent_login_signal(root)
                    return "agent_continue"
            except Exception:
                # Windows 等无 select：不读 stdin 循环，只靠 URL/文件
                stdin_ok = False

        now = time.time()
        if now - last_log >= 12:
            left = int(deadline - now)
            print(f"  [{name}] 等待登录中… 剩余~{left}s  url={cur[:70]}  (不刷新)")
            last_log = now

        # 纯 sleep，不碰 page 的 locator / reload
        time.sleep(poll_s)

    print(f"  [{name}] ⚠ 等待超时，不再刷新；由后续抓取结果判断")
    clear_agent_login_signal(root)
    return "timeout"


_DETAIL_SCOPE_SELECTORS: dict[str, tuple[str, ...]] = {
    "jd": (
        ".sku-name", "#detail .p-parameter", "#detail .Ptable", ".product-detail",
        "[class*='parameter']", "[class*='specification']",
    ),
    "1688": (
        "h1", ".title-text", ".offer-title", ".od-pc-offer-title",
        "#mod-detail-attributes", ".offer-attr-list", ".detail-attributes",
        "[class*='attribute']", "[class*='sku-info']",
    ),
    "lingcai": (
        "h1", ".material-detail", ".product-detail", ".detail-content",
        "[class*='parameter']", "[class*='specification']",
    ),
    "huixun": (
        "h1", ".product-detail", ".detail-content", ".product-info",
        "[class*='parameter']", "[class*='specification']",
    ),
}

_DETAIL_PRICE_SELECTORS: dict[str, tuple[str, ...]] = {
    "jd": (".p-price .price", ".summary-price-wrap .p-price span.price", "#jd-price"),
    "1688": (
        ".module-od-main-price", ".od-price-container", ".price-component",
        ".price-comp", ".price-info", ".price-text", ".od-pc-offer-price", "[class*='offer-price']",
        "[class*='price-range']",
    ),
    "lingcai": (".material-price", ".product-price", ".price"),
    "huixun": (".product-price", ".unit-price", ".price"),
}


def _scoped_detail_text(page, platform: str) -> str:
    selectors = _DETAIL_SCOPE_SELECTORS.get(platform) or (
        "h1", ".product-detail", ".detail-content", ".product-info",
        "[class*='parameter']", "[class*='specification']",
    )
    try:
        return page.evaluate(
            """(selectors) => {
              const parts = [], seen = new Set();
              for (const selector of selectors) {
                for (const el of document.querySelectorAll(selector)) {
                  const text = (el.innerText || el.textContent || '')
                    .replace(/\\s+/g, ' ').trim();
                  if (!text || text.length < 2 || seen.has(text)) continue;
                  // 推荐/猜你喜欢不属于当前 SKU 的证据。
                  if (/猜你喜欢|相关推荐|看了又看|推荐商品/.test(text.slice(0, 20))) continue;
                  seen.add(text);
                  parts.push(text.slice(0, 1800));
                  if (parts.join(' ').length >= 6000) break;
                }
                if (parts.join(' ').length >= 6000) break;
              }
              return parts.join(' | ').slice(0, 6000);
            }""",
            list(selectors),
        ) or ""
    except Exception:
        return ""


def _product_title(page, fallback: str = "") -> str:
    try:
        titles = page.evaluate(
            """() => {
              const nodes = document.querySelectorAll(
                '.sku-name, .offer-title, .title-text, [class*="product-title"], [class*="offer-title"], h1'
              );
              const og = document.querySelector('meta[property="og:title"]');
              const out = [...nodes].map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim());
              if (og?.content) out.push(og.content.replace(/\\s+/g, ' ').trim());
              return out.filter(Boolean).slice(0, 30);
            }"""
        )
        choices = [str(x).strip() for x in (titles or []) if str(x).strip()]
        if fallback:
            choices.append(fallback.strip())
        # 商品标题通常比公司名/导航 h1 长；选最长且排除明显栏目名。
        choices = [
            x for x in choices
            if x not in ("商品详情", "商品属性", "产品详情", "详情") and len(x) <= 260
        ]
        if choices:
            return max(choices, key=len)[:180]
    except Exception:
        pass
    try:
        return (page.title() or fallback)[:180]
    except Exception:
        return fallback[:180]


def _price_numbers(text: str) -> list[float]:
    values: list[float] = []
    raw = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", str(text or ""))
    marked = re.findall(r"[¥￥]\s*(\d+(?:\.\d+)?)", raw)
    source = marked or re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?=\s*元)", raw)
    for value in source:
        try:
            number = float(value)
            if 0.01 < number < 5_000_000 and number not in values:
                values.append(number)
        except Exception:
            pass
    return values


def open_detail(
    page,
    cand: dict,
    timeout_ms: int,
    extra_price_selectors: list[str] | None = None,
) -> dict:
    platform = str(cand.get("platform") or "")
    # 造价通详情必须走带 Cookie 的 HTTP，禁止先 page.goto 再判断；
    # 否则 SPA 会踢到登录页，打断后续材料。
    if platform == "zaojiatong":
        try:
            from .adapters import zaojiatong as zjt

            return zjt.enrich_detail(page, cand, timeout_ms)
        except Exception as e:
            out = dict(cand)
            out["detail_error"] = str(e)
            out["needs_detail_price"] = True
            out["detail_text"] = str(
                cand.get("detail_text") or cand.get("spec_seen") or ""
            )
            out["detail_title"] = str(cand.get("title") or "")
            out["detail_confirmed"] = True
            return out
    try:
        page.goto(cand["url"], wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2500)
        title = _product_title(page, str(cand.get("title") or ""))
        price = None
        price_text = ""
        price_context = ""
        selectors = list(_DETAIL_PRICE_SELECTORS.get(platform) or ())
        # 只接受明确价格节点；[class*=price] 太宽，会抓到推荐商品或划线价。
        selectors.extend(
            s for s in (extra_price_selectors or [])
            if s and "[class*='price']" not in s and '[class*="price"]' not in s
        )
        # de-dup preserve order
        seen = set()
        ordered = []
        for s in selectors:
            if s and s not in seen:
                seen.add(s)
                ordered.append(s)
        for sel in ordered:
            try:
                el = page.query_selector(sel)
                if el:
                    price_text = re.sub(
                        r"(?<=\d)\s*\.\s*(?=\d)", ".", el.inner_text() or ""
                    ).strip()
                    price = parse_price(price_text)
                    if price:
                        try:
                            price_context = (el.evaluate(
                                "e => (e.parentElement?.innerText || e.innerText || '').replace(/\\s+/g, ' ').trim()"
                            ) or price_text)[:500]
                        except Exception:
                            price_context = price_text[:500]
                        break
            except Exception:
                pass
        body = _scoped_detail_text(page, platform)
        if price:
            cand["price_tax"] = price
            cand["detail_confirmed"] = True
            cand["price_source"] = "detail"
        else:
            cand["detail_confirmed"] = False
            # 保留搜索列表上的价，但明确标记来源，绝不从整页第一个 ¥ 乱猜。
            price = parse_price(str(cand.get("price_tax") or ""))
            cand["price_source"] = "search_list" if price else "missing"
        values = _price_numbers(price_context or price_text)
        cand["price_ambiguous"] = len(values) > 1
        cand["price_text"] = price_text or str(cand.get("price_tax") or "")
        cand["price_context"] = price_context
        moq = re.search(r"(\d+(?:\.\d+)?)\s*(件|个|套|台|米|m|kg|吨)\s*起", price_context, re.I)
        cand["moq"] = (moq.group(0) if moq else "")
        cand["detail_title"] = title[:180]
        cand["detail_text"] = body
        cand["final_url"] = page.url
        cand["captured_at"] = datetime.now().isoformat(timespec="seconds")
        # 厂家 / 电话 / 联系人
        try:
            from .login_gate import extract_contact_fields

            extra = extract_contact_fields(body, title)
            cand["supplier"] = extra.get("supplier") or cand.get("supplier") or ""
            cand["contact"] = extra.get("contact") or ""
            cand["phone"] = extra.get("phone") or ""
            cand["spec_seen"] = extra.get("spec_seen") or ""
            if not cand.get("unit"):
                unit = re.search(
                    r"(?:计价单位|单位)\s*[:：]\s*([^\s|，,；;]{1,8})", body
                )
                cand["unit"] = unit.group(1) if unit else ""
        except Exception:
            pass
    except Exception as e:
        cand["detail_error"] = str(e)
        cand["detail_confirmed"] = False
        cand["captured_at"] = datetime.now().isoformat(timespec="seconds")
    return cand
