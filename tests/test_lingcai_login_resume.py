"""领材登录等待 / 假通过修复。"""

from __future__ import annotations

from material_price_audit.scraper import (
    url_left_login,
    url_looks_like_login,
    wait_for_login_agent,
)


def test_lingcai_userinfo_is_not_left_login():
    """打开 userInfo 后立刻把同页当成已离开 → 会导致 0 秒假通过。"""
    start = "https://www.hylcw.cn/userInfo/index.html"
    cur = "https://www.hylcw.cn/userInfo/index.html"
    assert not url_left_login(start, cur, "lingcai")


def test_lingcai_market_page_counts_as_left_login():
    start = "https://www.hylcw.cn/userInfo/index.html"
    cur = "https://www.hylcw.cn/marketPrice/so.html?index=0&type=1&gjz=x"
    assert url_left_login(start, cur, "lingcai")


def test_lingcai_hard_login_to_userinfo_not_enough():
    """从 /login 到 userInfo 仍是登录入口，不能算业务页离开。"""
    start = "https://www.hylcw.cn/login"
    cur = "https://www.hylcw.cn/userInfo/index.html"
    # URL 变了且无 /login 字样，但 userInfo 仍是登录入口
    assert not url_left_login(start, cur, "lingcai")


def test_wait_for_login_lingcai_does_not_instant_pass(monkeypatch):
    """领材停在 userInfo 时不能 already_ok / 立刻 logged_in。"""
    class Page:
        url = "https://www.hylcw.cn/userInfo/index.html"

        def context(self):
            return self

        def cookies(self):
            return []

    page = Page()
    page.context = page  # type: ignore

    # 缩短等待，避免测试慢
    st = wait_for_login_agent(
        page,
        platform_id="lingcai",
        name="领材网",
        login_url="https://www.hylcw.cn/userInfo/index.html",
        package_root=None,
        timeout_s=2,
        poll_s=0.2,
        allow_stdin=False,
    )
    assert st == "timeout"


def test_wait_for_login_lingcai_cookie_resume(monkeypatch, tmp_path):
    class Page:
        url = "https://www.hylcw.cn/userInfo/index.html"

        def cookies(self):
            return [
                {
                    "name": "token",
                    "value": "sess-lingcai-1",
                    "domain": ".hylcw.cn",
                }
            ]

        @property
        def context(self):
            return self

    page = Page()
    st = wait_for_login_agent(
        page,
        platform_id="lingcai",
        name="领材网",
        login_url="https://www.hylcw.cn/userInfo/index.html",
        package_root=tmp_path,
        timeout_s=5,
        poll_s=0.2,
        allow_stdin=False,
    )
    assert st == "logged_in"


def test_url_looks_like_login_userinfo_not_hard_marker():
    # userInfo 本身不含 /login 标记（由 lingcai 专用逻辑处理）
    assert not url_looks_like_login("https://www.hylcw.cn/userInfo/index.html")
    assert url_looks_like_login("https://www.hylcw.cn/user/login")
