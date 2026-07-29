"""
登录校验（实用版）：

1) 不要跳回 /login 再判（很多站登录后仍能打开登录 URL，会误判）
2) 去「首页/搜索页」看是否还在硬登录表单
3) 用户已点「本站已登录」时：无硬登录表单 + 同域名 → 信任通过
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
    "jd": "https://www.jd.com/",
    "1688": "https://www.1688.com/",
}

MEMBERSHIP_PLATFORMS = frozenset({"guangcai", "lingcai", "huixun"})

_POSITIVE_HINTS: dict[str, tuple[str, ...]] = {
    "guangcai": ("退出登录", "会员中心", "我的广材", "个人中心"),
    "lingcai": ("退出登录", "我的领材", "会员中心", "账户设置", "个人中心"),
    "huixun": ("退出登录", "我的慧讯", "用户中心", "会员中心", "个人中心"),
    "jd": ("退出登录", "我的京东", "我的订单"),
    "1688": ("退出登录", "我的阿里", "买家工作台", "卖家工作台"),
}

_NEGATIVE_HINTS: dict[str, tuple[str, ...]] = {
    "guangcai": ("扫码登录", "密码登录", "验证码登录", "登录后查看"),
    "lingcai": ("账号登录", "手机验证码登录", "登录后查看", "请先登录"),
    "huixun": ("微信扫码登录", "账号密码登录", "免费注册", "登录后可见"),
    "jd": ("扫码登录", "账户登录"),
    "1688": ("扫码登录", "密码登录", "免费注册"),
}

DOMAIN_HINT: dict[str, tuple[str, ...]] = {
    "guangcai": ("gldjc.com",),
    "lingcai": ("hylcw.cn",),
    "huixun": ("iccchina.com",),
    "jd": ("jd.com",),
    "1688": ("1688.com", "taobao.com"),
}


def _safe(page, attr: str, default: str = "") -> str:
    try:
        if attr == "url":
            return page.url or default
        if attr == "title":
            return page.title() or default
    except Exception:
        pass
    return default


def page_text_snippet(page, n: int = 2500) -> str:
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


def verify_logged_in(
    page,
    platform_id: str,
    login_url: str = "",
    *,
    user_confirmed: bool = False,
) -> tuple[bool, str]:
    """
    user_confirmed=True：用户点了「本站已登录，校验」
      → 只要不在硬登录 URL、无密码表单、域名正确，就通过
    """
    pid = (platform_id or "").lower()
    url = _safe(page, "url")
    title = _safe(page, "title")

    if looks_like_hard_login_url(url):
        return False, f"仍在登录 URL，请先完成登录再点校验: {url[:70]}"

    if title.strip() in ("登录", "用户登录", "会员登录") or re.match(r"^登录[-_|]", title.strip()):
        return False, f"标题仍是登录页: {title[:40]}"

    if has_login_form(page):
        return False, "页面仍有登录表单（密码框），请登录成功后再校验"

    if not on_platform_domain(url, pid):
        # 用户可能还在别的站
        if user_confirmed:
            return False, f"当前不在 {pid} 域名（{url[:60]}），请先打开该站再校验"
        return False, f"当前不在目标站域名: {url[:60]}"

    body = page_text_snippet(page)
    text = f"{title}\n{body}"
    negative_hits = [k for k in _NEGATIVE_HINTS.get(pid, ()) if k in text]
    if negative_hits:
        return False, f"检测到未登录页面：{negative_hits[:2]}"

    # 正向线索。会员材料站必须有正向证据，不能只凭“同域名且无密码框”误判。
    ok_hints = _POSITIVE_HINTS.get(pid) or (
        "退出登录", "注销", "个人中心", "我的账户", "会员中心", "欢迎"
    )
    ok_hits = [k for k in ok_hints if k in body]
    if ok_hits:
        return True, f"检测到已登录线索: {ok_hits[:3]}"

    if pid in MEMBERSHIP_PLATFORMS:
        if user_confirmed:
            return False, "未检测到会员身份；若页面确已登录，可使用人工强制确认"
        return False, "未检测到会员身份，请登录后再校验"

    # 电商首页可能不渲染账号文案；用户主动确认时可通过，搜索阶段仍会二次判定。
    if user_confirmed:
        return True, "用户确认已登录，且未检测到登录页或登录表单"

    # 普通站在正确域名、无登录表单、非登录 URL → 视为可用。
    if on_platform_domain(url, pid) and not has_login_form(page) and not looks_like_hard_login_url(url):
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
