"""
登录校验（实用版）：

1) 不要跳回 /login 再判（很多站登录后仍能打开登录 URL，会误判）
2) 去「首页/搜索页」看是否还在硬登录表单
3) 优先用 Cookie 会话证据 + 页面正向文案；文案缺失时 Cookie 仍可判定已登录
4) 用户已点「本站已登录」时：无硬登录页 + 同域名 + Cookie/文案证据 → 通过
"""

from __future__ import annotations

import re
from typing import Any


# 仅硬登录路径
_HARD_LOGIN_URL = (
    "/login",
    "/signin",
    "/sign-in",
    "/passport",
    "passport.",
    "/sso",
    "apply_trial",
    "user/login",
    "account/login",
)

# 校验用落地页（不要用 login_url）
CHECK_URLS: dict[str, str] = {
    "guangcai": "https://www.gldjc.com/scj/so.html?l=1&keyword=%E9%98%80%E9%97%A8",
    # 领材前端会连续 decodeURI 两次；单次编码会把“阀门”显示成 UTF-8 乱码。
    "lingcai": "https://www.hylcw.cn/marketPrice/so.html?index=0&type=1&gjz=%25E9%2598%2580%25E9%2597%25A8",
    "huixun": "https://services.iccchina.com/products",
    # 易择：登录后进信息价首页；有「我的易择/服务有效期」等线索
    "yize": "https://www.easybii.com/P4-3-info-price-home.html",
    # 造价通：登录后进广东分站市场价列表
    "zaojiatong": "https://gd.zjtcn.com/shichangjia/list/c_t_d_k.html",
    "jd": "https://www.jd.com/",
    "1688": "https://www.1688.com/",
}

MEMBERSHIP_PLATFORMS = frozenset({"guangcai", "lingcai", "huixun", "yize", "zaojiatong"})

# 页面可见文案（SPA 常延迟渲染，不能作为唯一证据）
_POSITIVE_HINTS: dict[str, tuple[str, ...]] = {
    "guangcai": (
        "退出登录",
        "退出",
        "会员中心",
        "我的广材",
        "个人中心",
        "账户中心",
        "我的消息",
        "充值",
    ),
    "lingcai": (
        "退出登录",
        "退出",
        "我的领材",
        "会员中心",
        "账户设置",
        "个人中心",
        "用户中心",
        "我的收藏",
    ),
    "huixun": (
        "退出登录",
        "退出",
        "我的慧讯",
        "用户中心",
        "会员中心",
        "个人中心",
        "账户中心",
        "我的收藏",
        "已登录",
    ),
    "yize": (
        "退出登录",
        "退出",
        "我的易择",
        "服务有效期",
        "我的收藏",
        "系统消息",
        "子账号管理",
        "收藏夹",
        "个人中心",
        "提交人工询价",
    ),
    "zaojiatong": (
        "退出登录",
        "退出",
        "我的造价通",
        "会员中心",
        "个人中心",
        "账户中心",
        "我的收藏",
        "云造价",
        "退出账号",
    ),
    "jd": ("退出登录", "退出", "我的京东", "我的订单", "你好，"),
    "1688": ("退出登录", "退出", "我的阿里", "买家工作台", "卖家工作台", "已登录"),
}

_NEGATIVE_HINTS: dict[str, tuple[str, ...]] = {
    "guangcai": ("扫码登录", "密码登录", "验证码登录", "登录后查看"),
    "lingcai": ("账号登录", "手机验证码登录", "登录后查看", "请先登录"),
    "huixun": ("微信扫码登录", "账号密码登录", "免费注册", "登录后可见"),
    "yize": ("密码登录", "免密登录", "立即登录", "申请试用", "还没有账号"),
    "zaojiatong": ("会员登录", "请输入密码", "请输入手机号/账号", "扫码登录", "登录后查看", "请先登录"),
    "jd": ("扫码登录", "账户登录"),
    "1688": ("扫码登录", "密码登录", "免费注册"),
}

DOMAIN_HINT: dict[str, tuple[str, ...]] = {
    "guangcai": ("gldjc.com",),
    "lingcai": ("hylcw.cn",),
    "huixun": ("iccchina.com",),
    "yize": ("easybii.com",),
    "zaojiatong": ("zjtcn.com", "member.zjtcn.com"),
    "jd": ("jd.com",),
    "1688": ("1688.com", "taobao.com"),
}

# 登录会话 Cookie（比页面文案更稳）。名称按小写匹配；支持前缀。
_AUTH_COOKIE_NAMES: dict[str, tuple[str, ...]] = {
    "guangcai": ("token", "tokensx", "access_token", "usertoken", "authorization"),
    "lingcai": (
        "token",
        "access_token",
        "accesstoken",
        "usertoken",
        "userid",
        "user_id",
        "uid",
        "jsessionid",
        "authorization",
        "hy_token",
        "hy_user",
        "auth_token",
        "sessionid",
        "lc_token",
        "lc_user",
        "memberid",
        "member_id",
        "login_token",
        "userinfo",
        "username",
        "user_name",
        "phone",
        "mobile",
    ),
    "huixun": (
        "has_logined",
        "quick_login_token",
        "remember_user",
        "_icc_session_id",
        "last_active_login",
        "icc_last_login_timestamp",
    ),
    "yize": (
        "token",
        "access_token",
        "usertoken",
        "userid",
        "user_id",
        "sessionid",
        "jsessionid",
        "authorization",
        "easybii",
        "login",
    ),
    # 注意：匿名访客也会下发 jsid，不能当登录证据，否则「已登录」校验误过，
    # 随后每条搜索/详情被踢回登录页（用户感觉：每次新链接都要再登）。
    # 实测 .browser-profile 登录后常见：token / userId / username / user_uid / remUser / userLoginCookie
    # 注意：匿名访客也会下发 jsid，绝不能收录；仅 user_name 也不算已登录。
    "zaojiatong": (
        "token",
        "userid",
        "user_id",
        "user_uid",
        "username",
        "userlogincookie",
        "remuser",
        "remlogintime",
        "remme",
        "rememberme",
        "uid",
        "employeeid",
        "tenantid",
        "applicationid",
        "usertag",
        "access_token",
        "usertoken",
        "memberid",
        "member_id",
        "jsessionid",
        "sessionid",
        "authorization",
        "loginname",
        "curruser",
        "userinfo",
        "zjt_token",
        "zjt_uid",
    ),
    "jd": ("pin", "thor", "unick", "pt_key", "pt_pin"),
    "1688": ("__cn_logon__", "cookie2", "lid", "_m_h5_tk"),
}

_FALSEY_COOKIE_VALUES = frozenset(
    {"", "0", "false", "null", "undefined", "deleted", "none", "nil", "-"}
)


def _safe(page, attr: str, default: str = "") -> str:
    try:
        if attr == "url":
            return page.url or default
        if attr == "title":
            return page.title() or default
    except Exception:
        pass
    return default


def page_text_snippet(page, n: int = 4000) -> str:
    try:
        return (page.inner_text("body") or "")[:n]
    except Exception:
        return ""


def looks_like_hard_login_url(url: str) -> bool:
    u = (url or "").lower()
    return any(m in u for m in _HARD_LOGIN_URL)


def has_login_form(page) -> bool:
    """是否仍有明显登录表单（password + 登录按钮/文案）。"""
    try:
        # password 输入框
        n = page.locator('input[type="password"]').count()
        if n and n > 0:
            return True
    except Exception:
        pass
    try:
        body = page_text_snippet(page, 1200)
        # 同时出现「密码」和「登录」表单区更像登录页
        if ("密码" in body or "password" in body.lower()) and (
            "登录" in body or "验证码" in body
        ):
            # 排除页脚孤立「登录」
            if re.search(r"(手机号|账号|用户名).{0,20}(密码|验证码)", body):
                return True
            if "立即登录" in body or "密码登录" in body or "验证码登录" in body:
                return True
    except Exception:
        pass
    return False


def on_platform_domain(url: str, platform_id: str) -> bool:
    pid = (platform_id or "").lower()
    domains = DOMAIN_HINT.get(pid) or ()
    u = (url or "").lower()
    if not domains:
        return True
    return any(d in u for d in domains)


def _cookie_domain_matches(cookie_domain: str, platform_domains: tuple[str, ...]) -> bool:
    host = (cookie_domain or "").lower().lstrip(".")
    if not platform_domains:
        return True
    for d in platform_domains:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d) or d in host:
            return True
    return False


def _cookie_name_matches(cookie_name: str, patterns: tuple[str, ...]) -> bool:
    nlow = (cookie_name or "").lower()
    if not nlow:
        return False
    for key in patterns:
        k = key.lower()
        if nlow == k or nlow.startswith(k + "_") or nlow.startswith(k):
            return True
        # last_active_login_xxxx 一类动态名
        if k in nlow and (nlow.startswith(k) or f"_{k}" in f"_{nlow}"):
            return True
    return False


def _read_cookies(page) -> list[dict[str, Any]]:
    """从 Playwright page/context 读 cookie；测试 mock 可提供 cookies()。"""
    for getter in (
        lambda: page.context.cookies(),
        lambda: page.cookies(),
    ):
        try:
            raw = getter()
            if isinstance(raw, list):
                return [c for c in raw if isinstance(c, dict)]
        except Exception:
            continue
    return []


def auth_cookie_hits(page, platform_id: str) -> list[str]:
    """
    返回命中的登录会话 cookie 名称。
    慧讯 has_logined / quick_login_token、广材 token、京东 pin/thor 等。
    """
    pid = (platform_id or "").lower()
    patterns = _AUTH_COOKIE_NAMES.get(pid) or ()
    if not patterns:
        return []
    domains = DOMAIN_HINT.get(pid) or ()
    hits: list[str] = []
    for c in _read_cookies(page):
        name = str(c.get("name") or "")
        if not _cookie_name_matches(name, patterns):
            continue
        if not _cookie_domain_matches(str(c.get("domain") or ""), domains):
            continue
        val = str(c.get("value") or "").strip()
        if val.lower() in _FALSEY_COOKIE_VALUES:
            continue
        hits.append(name)
    # 保序去重
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def page_positive_hits(page, platform_id: str, body: str | None = None) -> list[str]:
    pid = (platform_id or "").lower()
    text = body if body is not None else page_text_snippet(page)
    ok_hints = _POSITIVE_HINTS.get(pid) or (
        "退出登录",
        "注销",
        "个人中心",
        "我的账户",
        "会员中心",
        "欢迎",
        "退出",
    )
    return [k for k in ok_hints if k in text]


def check_url_for(platform_id: str, login_url: str = "") -> str:
    pid = (platform_id or "").lower()
    if pid in CHECK_URLS:
        return CHECK_URLS[pid]
    # 从 login_url 推首页
    if login_url:
        m = re.match(r"(https?://[^/]+)", login_url)
        if m:
            return m.group(1) + "/"
    return login_url or "about:blank"


_ONE_CLICK_LABELS = ("一键登录", "一键登陆")

# 点「一键登录」后常见：账号多端占用弹窗
_SESSION_CONFLICT_HINTS = (
    "账号已被登录",
    "已被您登录",
    "将下线已登录人",
    "仅人工询价登录",
    # 造价通：账号已在别处在线（MsgBox.Confirm / ConfirmBoxSmall）
    "正在登录使用中",
    "强行登录",
    "是否继续",
    "确认当前使用者是否仍需使用账号",
    "更换其他账号登录",
    "请留意使用者是否正在发布询价",
)
# 优先「继续登录」（完整进产品库）；次选「仅人工询价登录」
_SESSION_CONFLICT_ACTIONS = ("继续登录", "仅人工询价登录", "继续操作")


def page_shows_one_click_login(page) -> bool:
    """是否出现「一键登录」类入口（慧讯关窗重开常见）。"""
    try:
        body = page_text_snippet(page, 1800)
    except Exception:
        body = ""
    if any(x in body for x in _ONE_CLICK_LABELS):
        return True
    # DOM 探测
    for label in _ONE_CLICK_LABELS:
        try:
            loc = page.get_by_text(label, exact=False)
            if loc.count() > 0 and loc.first.is_visible(timeout=400):
                return True
        except Exception:
            continue
        try:
            loc = page.locator(f"text={label}")
            if loc.count() > 0 and loc.first.is_visible(timeout=300):
                return True
        except Exception:
            continue
    return False


def page_shows_session_conflict(page) -> bool:
    """是否出现「账号已被登录」冲突弹窗。"""
    try:
        body = page_text_snippet(page, 2500)
    except Exception:
        body = ""
    if any(h in body for h in _SESSION_CONFLICT_HINTS):
        return True
    for label in _SESSION_CONFLICT_ACTIONS:
        try:
            loc = page.get_by_role("button", name=re.compile(re.escape(label)))
            if loc.count() > 0 and loc.first.is_visible(timeout=400):
                return True
        except Exception:
            continue
    return False


def _click_visible_label(page, label: str) -> bool:
    """点可见按钮/文案（精确优先）。"""
    # 慧讯弹窗按钮：.ivu-btn（勿用 newlogin-btn，那是一键登录主按钮）
    # 造价通 MsgBox：#mb_btn_ok value=继续登录
    selectors = [
        f"#mb_btn_ok[value='{label}']",
        f"#mb_btn_ok_BoxSmall[value='{label}']",
        f"#mb_btn_ok_Box[value='{label}']",
        f"input#mb_btn_ok[value='{label}']",
        f"input[type='button'][value='{label}']",
        f"input[type='submit'][value='{label}']",
        f".ivu-modal-footer button:has-text('{label}')",
        f".ivu-modal button:has-text('{label}')",
        f".ivu-modal-wrap button:has-text('{label}')",
        f"button:has-text('{label}')",
        f"//div[contains(@class,'ivu-modal')]//button[normalize-space(.)='{label}']",
        f"//div[contains(@class,'ivu-modal')]//button[contains(normalize-space(.), '{label}')]",
        f"//button[normalize-space(.)='{label}']",
        f"//button[contains(normalize-space(.), '{label}')]",
        f"//span[normalize-space(.)='{label}']/ancestor::button[1]",
        f"//input[@type='button' and @value='{label}']",
        f"//input[@type='submit' and @value='{label}']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=600):
                loc.scroll_into_view_if_needed(timeout=1500)
                loc.click(timeout=5000, force=True)
                return True
        except Exception:
            continue
    try:
        btn = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$"))
        if btn.count() > 0 and btn.first.is_visible(timeout=500):
            btn.first.click(timeout=5000, force=True)
            return True
    except Exception:
        pass
    try:
        loc = page.get_by_text(label, exact=True).first
        if loc.is_visible(timeout=500):
            loc.click(timeout=5000, force=True)
            return True
    except Exception:
        pass
    try:
        clicked = page.evaluate(
            """(label) => {
              const want = label.replace(/\\s+/g, '');
              const nodes = Array.from(document.querySelectorAll(
                'button, a, [role="button"], .ivu-btn, span, div'
              ));
              // 优先短文本精确匹配（避免点到大容器）
              const scored = [];
              for (const el of nodes) {
                const t = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                if (t !== want && !t.includes(want)) continue;
                if (t.length > want.length + 8) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 4 || r.height < 4) continue;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                scored.push({ el, exact: t === want, area: r.width * r.height });
              }
              scored.sort((a, b) => {
                if (a.exact !== b.exact) return a.exact ? -1 : 1;
                return a.area - b.area;
              });
              if (!scored.length) return false;
              scored[0].el.click();
              return true;
            }""",
            label,
        )
        return bool(clicked)
    except Exception:
        return False


def try_click_one_click_login(page) -> tuple[bool, str]:
    """点击页面上的「一键登录」按钮（慧讯 class=newlogin-btn）。"""
    # 优先官方按钮 class
    for sel in (
        "button.newlogin-btn",
        "button.newlogin-btn.ivu-btn",
        ".newlogin-btn",
    ):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.scroll_into_view_if_needed(timeout=1500)
                loc.click(timeout=5000, force=True)
                return True, "一键登录"
        except Exception:
            pass
    for label in _ONE_CLICK_LABELS:
        if _click_visible_label(page, label):
            return True, label
    return False, "未找到一键登录按钮"


def try_handle_huixun_session_conflict(page) -> tuple[bool, str]:
    """
    处理「账号已被登录」弹窗。
    优先点「继续登录」（下线其他端，完整进产品库）；
    否则点「仅人工询价登录」。
    绝不点「暂不登录」。
    """
    # 弹窗可能稍晚出现
    appeared = False
    for _ in range(12):
        if page_shows_session_conflict(page):
            appeared = True
            break
        try:
            page.wait_for_timeout(300)
        except Exception:
            break
    if not appeared:
        return False, ""

    for label in _SESSION_CONFLICT_ACTIONS:
        if _click_visible_label(page, label):
            try:
                page.wait_for_timeout(1200)
            except Exception:
                pass
            return True, label
    return False, "检测到账号冲突弹窗但未能点击确认"


def install_zaojiatong_dialog_auto_accept(page) -> None:
    """
    造价通登录链路里，服务端 state=islogin 时会走浏览器原生 confirm()。
    自动点「确定」= 同意踢掉其它端（isKick=true 再登一次）。
    幂等：重复安装无妨。
    """
    if getattr(page, "_zjt_dialog_hooked", False):
        return

    def _on_dialog(dialog) -> None:
        try:
            msg = dialog.message or ""
        except Exception:
            msg = ""
        # 仅自动接受「已在使用 / 强行登录」类；其它 alert 也 accept 以免卡住
        try:
            if dialog.type in ("confirm", "alert", "prompt"):
                dialog.accept()
            else:
                dialog.dismiss()
        except Exception:
            try:
                dialog.accept()
            except Exception:
                pass
        # 记在 page 上方便调试
        try:
            page._zjt_last_dialog = msg[:200]
        except Exception:
            pass

    try:
        page.on("dialog", _on_dialog)
        page._zjt_dialog_hooked = True
    except Exception:
        pass


def try_handle_zaojiatong_session_conflict(page) -> tuple[bool, str]:
    """
    造价通：账号【xxx】正在登录使用中 → 弹窗「取消 / 继续登录」。

    这不是「你没登录」，而是**同一账号已在别处在线**（工具浏览器上次会话、
    系统 Chrome、另一台电脑等）。点「继续登录」会踢掉旧端、在本浏览器接管。
    """
    install_zaojiatong_dialog_auto_accept(page)

    appeared = False
    for _ in range(16):
        if page_shows_session_conflict(page):
            appeared = True
            break
        # DOM id 探测（MsgBox）
        try:
            if page.locator("#mb_btn_ok, #mb_btn_ok_BoxSmall, #mb_con").count() > 0:
                appeared = True
                break
        except Exception:
            pass
        try:
            page.wait_for_timeout(250)
        except Exception:
            break
    if not appeared:
        return False, ""

    # 优先点造价通 MsgBox 的「继续登录」
    for sel in (
        "#mb_btn_ok",
        "#mb_btn_ok_BoxSmall",
        "#mb_btn_ok_Box",
        "input#mb_btn_ok",
        "input[value='继续登录']",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=500):
                loc.click(timeout=4000, force=True)
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                return True, "继续登录"
        except Exception:
            continue

    for label in ("继续登录", "继续操作"):
        if _click_visible_label(page, label):
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            return True, label

    return False, "检测到造价通「账号使用中」弹窗但未能点继续登录"


def _wait_left_login_page(page, *, timeout_ms: int = 12000) -> bool:
    """点完一键登录/继续登录后，等离开 /login 或进入 /products。"""
    deadline_steps = max(3, int(timeout_ms / 400))
    for _ in range(deadline_steps):
        # 中途若冒出冲突弹窗，先处理
        if page_shows_session_conflict(page):
            try_handle_huixun_session_conflict(page)
        try:
            cur = (page.url or "").lower()
        except Exception:
            cur = ""
        if "/products" in cur or ("iccchina.com" in cur and "/login" not in cur):
            # 产品库且无冲突弹窗
            if not page_shows_session_conflict(page) and not page_shows_one_click_login(
                page
            ):
                return True
            if "/products" in cur and not page_shows_session_conflict(page):
                return True
        try:
            page.wait_for_timeout(400)
        except Exception:
            break
    return False


def _huixun_click_login_flow(page) -> tuple[bool, str]:
    """
    完整一键登录链路：一键登录 →（若有）继续登录/仅人工询价登录。
    返回 (是否点过关键按钮, 描述文案)。
    """
    parts: list[str] = []
    ok_click, label = try_click_one_click_login(page)
    if ok_click:
        parts.append(label)
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
    # 冲突弹窗：无论是否点过一键登录都检查（可能上一步已弹出）
    handled, conflict_label = try_handle_huixun_session_conflict(page)
    if handled and conflict_label:
        parts.append(conflict_label)
    if not parts:
        return False, ""
    return True, " → ".join(parts)


def try_resume_huixun_session(page, *, timeout_ms: int = 20000) -> tuple[bool, str]:
    """
    慧讯网特殊流程：
    1) 关窗重开 → 登录页显示账号 +「一键登录」
    2) 点一键登录后常弹「账号已被登录」→ 再点「继续登录」
    注意：cookie（has_logined 等）往往还在，但会话未激活，不能仅凭 Cookie 判已登录。
    """
    products = CHECK_URLS.get("huixun") or "https://services.iccchina.com/products"
    login_url = "https://services.iccchina.com/login"

    # SPA 可能晚渲染按钮：多等一会儿
    has_btn = False
    for _ in range(8):
        try:
            body = page_text_snippet(page, 2000)
        except Exception:
            body = ""
        has_btn = page_shows_one_click_login(page) or any(
            x in body for x in _ONE_CLICK_LABELS
        )
        if has_btn or page_shows_session_conflict(page):
            break
        try:
            page.wait_for_timeout(350)
        except Exception:
            break

    cookie_hits = auth_cookie_hits(page, "huixun")
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""

    # 已在产品库且 verify 通过 → 无需点
    if (
        "/products" in url
        and not looks_like_hard_login_url(url)
        and not page_shows_one_click_login(page)
        and not page_shows_session_conflict(page)
    ):
        ok0, reason0 = verify_logged_in(
            page, "huixun", login_url, user_confirmed=False
        )
        if ok0:
            return True, f"慧讯已在产品库：{reason0}"

    if (
        not has_btn
        and not cookie_hits
        and not page_shows_session_conflict(page)
    ):
        return False, "慧讯：无会话也无一键登录入口"

    clicked_label = ""
    if has_btn or page_shows_session_conflict(page):
        ok_flow, clicked_label = _huixun_click_login_flow(page)
        if ok_flow:
            left = _wait_left_login_page(page, timeout_ms=min(timeout_ms, 12000))
            if not left:
                # 再试冲突弹窗
                handled, cl = try_handle_huixun_session_conflict(page)
                if handled:
                    if cl:
                        clicked_label = (
                            f"{clicked_label} → {cl}" if clicked_label else cl
                        )
                    _wait_left_login_page(page, timeout_ms=8000)
                else:
                    try:
                        page.wait_for_timeout(1200)
                    except Exception:
                        pass
        else:
            clicked_label = ""

    # 点完或仅有 cookie：进产品库复核（必须离开 login 才算成功）
    try:
        cur = (page.url or "").lower()
    except Exception:
        cur = url
    if "/products" not in cur or looks_like_hard_login_url(cur):
        try:
            page.goto(products, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1500)
        except Exception as e:
            return False, f"慧讯：打开产品库失败: {e}"

    # 产品库若又跳回登录 / 冲突弹窗 → 再走一遍
    try:
        cur2 = (page.url or "").lower()
    except Exception:
        cur2 = ""
    if (
        looks_like_hard_login_url(cur2)
        or page_shows_one_click_login(page)
        or page_shows_session_conflict(page)
    ):
        ok_flow2, label2 = _huixun_click_login_flow(page)
        if ok_flow2:
            clicked_label = label2 or clicked_label
            _wait_left_login_page(page, timeout_ms=10000)
            try:
                if looks_like_hard_login_url((page.url or "").lower()):
                    page.goto(
                        products, wait_until="domcontentloaded", timeout=timeout_ms
                    )
                    page.wait_for_timeout(1200)
            except Exception:
                pass

    # 成功条件：不在登录 URL，且 cookie/正向文案证明会话
    try:
        final_url = (page.url or "").lower()
    except Exception:
        final_url = ""
    if looks_like_hard_login_url(final_url) or page_shows_session_conflict(page):
        extra = ""
        if page_shows_session_conflict(page):
            extra = "；仍有「账号已被登录」弹窗未处理成功"
        return False, (
            f"已尝试一键登录但仍停在登录页（cookie={cookie_hits[:3] or '无'}）"
            f"{'；已点「' + clicked_label + '」' if clicked_label else ''}"
            f"{extra}"
        )

    ok, reason = verify_logged_in(page, "huixun", login_url, user_confirmed=True)
    if ok:
        if clicked_label:
            return True, f"已自动「{clicked_label}」并进入：{reason}"
        return True, f"慧讯会话已恢复：{reason}"

    # 仍失败：最后再完整走一遍
    if page_shows_one_click_login(page) or page_shows_session_conflict(page):
        ok_flow, clicked_label = _huixun_click_login_flow(page)
        if ok_flow:
            _wait_left_login_page(page, timeout_ms=10000)
            try:
                page.goto(products, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(1200)
            except Exception:
                pass
            if not looks_like_hard_login_url((page.url or "").lower()):
                ok2, reason2 = verify_logged_in(
                    page, "huixun", login_url, user_confirmed=True
                )
                if ok2:
                    return True, f"二次「{clicked_label}」成功：{reason2}"
                return False, f"已点「{clicked_label}」但仍未登录：{reason2}"
    return False, f"慧讯一键登录未成功：{reason}"


def probe_zaojiatong_market_session(page, timeout_ms: int = 30000) -> tuple[bool, str]:
    """
    造价通会话探针。

    实测：
    - SPA 会把未登录的 page.goto 踢到登录页（0.3s 内），不能单靠最终 URL 判死；
    - 搜价主路径已改为 HTTP 抓 SSR，**不依赖**页面是否被踢；
    - 要「能看见数字价」仍需登录 Cookie（token/userId 等）。

    通过条件（满足其一）：
    1) 有有效登录 Cookie（token/userId/…）；
    2) 浏览器打开市场价后 ≥3s 仍停在分站且列表可见（真·已登录未踢）。
    """
    hits = auth_cookie_hits(page, "zaojiatong")
    if hits:
        # 再用 SSR 确认站点可达
        check = CHECK_URLS.get("zaojiatong") or (
            "https://gd.zjtcn.com/shichangjia/list/c_t_d_k.html"
        )
        try:
            resp = page.context.request.get(check, timeout=timeout_ms)
            text = resp.text() if resp else ""
            if text and ("material-title" in text or "shichangjia" in text):
                return True, f"已检测到登录 Cookie {hits[:3]}，SSR 列表可达"
        except Exception:
            return True, f"已检测到登录 Cookie {hits[:3]}"
        return True, f"已检测到登录 Cookie {hits[:3]}"

    check = CHECK_URLS.get("zaojiatong") or (
        "https://gd.zjtcn.com/shichangjia/list/c_t_d_k.html"
    )
    # 无 Cookie：试一次页面是否「登完后不会被踢」（真登录态）
    try:
        page.goto(check, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as e:
        return False, (
            f"打开市场价失败: {e}。"
            "请在本工具弹出的浏览器内登录造价通（勾选「30天内自动登录」），再点校验"
        )

    kicked = False
    for _ in range(5):
        try:
            page.wait_for_timeout(700)
        except Exception:
            break
        url = _safe(page, "url").lower()
        title = _safe(page, "title")
        if "member.zjtcn.com" in url or "/common/login" in url or title.strip().startswith(
            "会员登录"
        ):
            kicked = True
            break

    hits2 = auth_cookie_hits(page, "zaojiatong")
    if hits2:
        return True, f"登录后已写入 Cookie {hits2[:3]}"

    if kicked:
        return False, (
            "市场价被踢回登录页，且无 token/userId 等登录 Cookie。"
            "请务必在【本工具弹出的浏览器】里登录（不要用系统自带 Chrome），"
            "登录页勾选「30天内自动登录」，成功后应跳到材料列表再点校验。"
            "说明：未登录时程序仍可用 SSR 搜名称规格，但价格需会员会话。"
        )

    # 没被踢且列表在
    try:
        n = page.locator("a.material-title, tbody tr").count()
    except Exception:
        n = 0
    if n and n > 0:
        return True, "市场价页保持打开且列表可见（未检测到典型 Cookie，请留意价格是否可见）"

    return False, (
        "未检测到造价通登录会话。请在工具弹出的浏览器完成登录后再校验。"
    )


def try_resume_zaojiatong_session(
    page, timeout_ms: int = 25000
) -> tuple[bool, str]:
    """
    造价通：登录在 member.zjtcn.com，搜价在 gd.zjtcn.com。
    1) 先处理「账号正在使用中 → 继续登录」弹窗（多端互踢）；
    2) 再以市场价探针确认会话。
    """
    install_zaojiatong_dialog_auto_accept(page)
    handled, label = try_handle_zaojiatong_session_conflict(page)
    if handled:
        # 点完继续登录后等跳转/写 Cookie
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass
        ok, reason = probe_zaojiatong_market_session(page, timeout_ms=timeout_ms)
        if ok:
            return True, f"已自动「{label}」并确认会话：{reason}"
        return False, f"已点「{label}」但会话仍未生效：{reason}"
    return probe_zaojiatong_market_session(page, timeout_ms=timeout_ms)


def ensure_logged_in_or_resume(
    page,
    platform_id: str,
    login_url: str = "",
    *,
    user_confirmed: bool = False,
) -> tuple[bool, str]:
    """
    通用：先 verify；慧讯若停在一键登录页则自动点入。

    慧讯特殊：关窗重开后 cookie 仍在，但页面卡在「一键登录」——
    此时不能仅凭 Cookie 判通过，必须先尝试点按钮并进入产品库。

    造价通特殊：登录在 member、搜价在 gd 分站，需把会话落到分站，避免每条新链接再登。
    """
    pid = (platform_id or "").lower()
    if pid == "huixun":
        url = _safe(page, "url")
        needs_one_click = (
            looks_like_hard_login_url(url)
            or page_shows_one_click_login(page)
            or page_shows_session_conflict(page)
        )
        if needs_one_click:
            resumed, r2 = try_resume_huixun_session(page)
            if resumed:
                return True, r2
            # 一键失败后再走常规判定（通常仍未登录）
            ok, reason = verify_logged_in(
                page, pid, login_url, user_confirmed=user_confirmed
            )
            if ok and not looks_like_hard_login_url(_safe(page, "url")):
                return True, reason
            return False, f"{r2}" if r2 else reason

    # 造价通：先处理多端互踢弹窗，再探针
    if pid == "zaojiatong":
        install_zaojiatong_dialog_auto_accept(page)
        # 登录页若正在弹「账号使用中」，先点继续登录
        if page_shows_session_conflict(page) or "login" in _safe(page, "url").lower():
            try_handle_zaojiatong_session_conflict(page)
        resumed, r2 = try_resume_zaojiatong_session(page)
        if resumed:
            return True, r2
        # 用户已点确认时仍失败：给出可操作原因，不要凭 Cookie 假通过
        return False, r2

    ok, reason = verify_logged_in(
        page, pid, login_url, user_confirmed=user_confirmed
    )
    if ok:
        return True, reason
    if pid == "huixun":
        resumed, r2 = try_resume_huixun_session(page)
        if resumed:
            return True, r2
        return False, f"{reason}；{r2}"
    return False, reason


def verify_logged_in(
    page,
    platform_id: str,
    login_url: str = "",
    *,
    user_confirmed: bool = False,
) -> tuple[bool, str]:
    """
    user_confirmed=True：用户点了「本站已登录，校验」

    判定顺序：
    1. 硬登录 URL / 登录标题 → 未登录
       （慧讯例外：关窗后 cookie 仍在但仍需点「一键登录」，绝不能凭 Cookie 放行）
    2. 登录表单 + 无 Cookie → 未登录
    3. 域名不对 → 未登录
    4. 强未登录文案 + 无 Cookie → 未登录
    5. Cookie 会话 或 正向文案 → 已登录
    6. 会员站仍无证据 → 提示强制确认；电商在用户确认时可放行
    """
    pid = (platform_id or "").lower()
    url = _safe(page, "url")
    title = _safe(page, "title")
    cookie_hits = auth_cookie_hits(page, pid)
    has_session = bool(cookie_hits)

    if looks_like_hard_login_url(url):
        # 慧讯：登录页即使有 has_logined/quick_login_token，也仍需一键登录激活
        if pid == "huixun":
            return False, (
                f"慧讯仍在登录页（需点「一键登录」或完整登录）: {url[:70]}"
                f"{'；已有账号缓存 Cookie ' + str(cookie_hits[:2]) if cookie_hits else ''}"
            )
        # 少数站登录后仍停留 /login 但已写会话 cookie；有 cookie 时放行
        if has_session and user_confirmed:
            return True, f"仍在登录 URL 但检测到会话 Cookie: {cookie_hits[:3]}"
        return False, f"仍在登录 URL，请先完成登录再点校验: {url[:70]}"

    # 慧讯产品页若弹出「一键登录」遮罩，也算未激活
    if pid == "huixun" and page_shows_one_click_login(page):
        return False, "慧讯页面出现「一键登录」，会话未激活"

    if (
        title.strip() in ("登录", "用户登录", "会员登录")
        or re.match(r"^登录[-_|]", title.strip())
        or title.strip().startswith("会员登录")
    ):
        if pid == "huixun":
            return False, f"慧讯标题仍是登录页: {title[:40]}"
        # 造价通：标题「会员登录_造价通」时，即使有弱 Cookie 也不放行，必须落到分站
        if pid == "zaojiatong":
            return False, f"造价通标题仍是登录页: {title[:40]}"
        if has_session and user_confirmed:
            return True, f"标题像登录页但检测到会话 Cookie: {cookie_hits[:3]}"
        return False, f"标题仍是登录页: {title[:40]}"

    form_present = has_login_form(page)
    if form_present and not has_session:
        return False, "页面仍有登录表单（密码框），请登录成功后再校验"
    # 有会话 Cookie 时，首页/产品页里偶发残留隐藏密码框或登录弹层，不据此否决

    if not on_platform_domain(url, pid):
        if user_confirmed:
            return False, f"当前不在 {pid} 域名（{url[:60]}），请先打开该站再校验"
        return False, f"当前不在目标站域名: {url[:60]}"

    body = page_text_snippet(page)
    text = f"{title}\n{body}"
    negative_hits = [k for k in _NEGATIVE_HINTS.get(pid, ()) if k in text]
    if negative_hits and not has_session:
        return False, f"检测到未登录页面：{negative_hits[:2]}"
    # 慧讯：同时有「一键登录」类未激活态时，负向文案 + cookie 也不算通过
    if pid == "huixun" and negative_hits and any(
        x in text for x in _ONE_CLICK_LABELS
    ):
        return False, f"慧讯登录页残留（{negative_hits[:2]}），请点一键登录"

    ok_hits = page_positive_hits(page, pid, body=body)
    if ok_hits:
        extra = f"；会话Cookie={cookie_hits[:2]}" if cookie_hits else ""
        return True, f"检测到已登录线索: {ok_hits[:3]}{extra}"

    if has_session:
        return True, f"检测到登录会话 Cookie: {cookie_hits[:4]}"

    if pid in MEMBERSHIP_PLATFORMS:
        if user_confirmed:
            return (
                False,
                "未检测到登录会话（页面文案与 Cookie 均无）；若页面确已登录，可使用人工强制确认",
            )
        return False, "未检测到登录会话，请登录后再校验"

    # 电商首页可能不渲染账号文案；用户主动确认时可通过，搜索阶段仍会二次判定。
    if user_confirmed:
        return True, "用户确认已登录，且未检测到登录页或登录表单"

    # 普通站在正确域名、无登录表单、非登录 URL → 视为可用。
    if (
        on_platform_domain(url, pid)
        and not form_present
        and not looks_like_hard_login_url(url)
    ):
        return True, f"已在 {pid} 站内且无登录表单（{url[:50]}）"

    return False, "未能确认登录，请登录后点「本站已登录，校验」"


def extract_contact_fields(page_text: str, title: str = "") -> dict[str, str]:
    """从详情页正文抽厂家/电话/联系人。"""
    text = f"{title}\n{page_text or ''}"
    out = {"supplier": "", "contact": "", "phone": "", "spec_seen": ""}

    phones = re.findall(
        r"(?:电话|手机|联系电话|联系方式|Tel|TEL|手机号)[：:\s]*([0-9\-–—\s]{7,18})",
        text,
    )
    if not phones:
        phones = re.findall(r"(?<!\d)(1[3-9]\d{9})(?!\d)", text)
    if not phones:
        phones = re.findall(r"(?<!\d)(0\d{2,3}[\-–—]?\d{7,8})(?!\d)", text)
    if phones:
        out["phone"] = re.sub(r"\s+", "", phones[0])[:20]

    for pat in (
        r"(?:生产厂家|厂家名称|供应商|厂商|公司名称|企业名称|出品单位)[：:\s]*([^\n\r，,。；;]{2,40})",
        r"(?:店铺|卖家|商家)[：:\s]*([^\n\r，,。；;]{2,40})",
    ):
        m = re.search(pat, text)
        if m:
            out["supplier"] = m.group(1).strip()[:60]
            break

    m = re.search(r"(?:联系人|业务员|销售)[：:\s]*([^\n\r，,。；;]{2,20})", text)
    if m:
        out["contact"] = m.group(1).strip()[:30]

    m = re.search(r"(?:规格|型号|规格型号)[：:\s]*([^\n\r]{2,80})", text)
    if m:
        out["spec_seen"] = m.group(1).strip()[:80]

    return out
