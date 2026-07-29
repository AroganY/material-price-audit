from material_price_audit.login_gate import (
    auth_cookie_hits,
    ensure_logged_in_or_resume,
    page_shows_one_click_login,
    page_shows_session_conflict,
    try_click_one_click_login,
    try_handle_huixun_session_conflict,
    try_resume_huixun_session,
    verify_logged_in,
)


class _Locator:
    def __init__(self, count=0, visible=True, click_cb=None):
        self._count = count
        self._visible = visible
        self._click_cb = click_cb
        self.first = self

    def count(self):
        return self._count

    def is_visible(self, timeout=0):
        return bool(self._count and self._visible)

    def click(self, timeout=0, force=False):
        if self._click_cb:
            self._click_cb()

    def scroll_into_view_if_needed(self, timeout=0):
        return None


class _Page:
    def __init__(
        self,
        url,
        title,
        body,
        passwords=0,
        cookies=None,
        one_click=False,
        session_conflict=False,
    ):
        self.url = url
        self._title = title
        self._body = body
        self._passwords = passwords
        self._cookies = cookies or []
        self._one_click = one_click
        self._session_conflict = session_conflict
        self._clicked_one_click = False
        self._clicked_continue = False
        self.goto_calls = []

    def title(self):
        return self._title

    def inner_text(self, _selector):
        return self._body

    def locator(self, selector):
        s = str(selector)
        if "password" in s:
            return _Locator(self._passwords)
        # 冲突弹窗按钮优先（避免 newlogin-btn:has-text(继续登录) 误触一键登录）
        if self._session_conflict and "继续登录" in s and "一键" not in s:
            return _Locator(1, click_cb=self._on_continue)
        if self._session_conflict and "仅人工询价登录" in s:
            return _Locator(1, click_cb=self._on_continue)
        if self._one_click and (
            "newlogin-btn" in s or "一键登录" in s or "一键登陆" in s
        ):
            return _Locator(1, click_cb=self._on_one_click)
        return _Locator(0)

    def get_by_text(self, text, exact=False):
        if self._one_click and text in ("一键登录", "一键登陆"):
            return _Locator(1, click_cb=self._on_one_click)
        if self._session_conflict and text in ("继续登录", "仅人工询价登录"):
            return _Locator(1, click_cb=self._on_continue)
        if text and text in self._body:
            return _Locator(1)
        return _Locator(0)

    def get_by_role(self, role, name=None):
        # name may be a compiled regex
        name_s = getattr(name, "pattern", None) or str(name or "")
        if self._one_click and role in ("button", "link") and (
            "一键" in name_s or not name_s
        ):
            return _Locator(1, click_cb=self._on_one_click)
        if self._session_conflict and role == "button" and "继续登录" in name_s:
            return _Locator(1, click_cb=self._on_continue)
        if self._session_conflict and role == "button" and "仅人工" in name_s:
            return _Locator(1, click_cb=self._on_continue)
        return _Locator(0)

    def evaluate(self, _script, arg=None):
        if arg in ("一键登录", "一键登陆") and self._one_click:
            self._on_one_click()
            return True
        if arg in ("继续登录", "仅人工询价登录") and self._session_conflict:
            self._on_continue()
            return True
        if self._one_click and arg is None:
            self._on_one_click()
            return True
        return False

    def wait_for_timeout(self, _ms):
        return None

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)
        self.url = url
        # 模拟：点过一键登录（+ 若需继续登录）后再进产品库 → 真正进入
        activated = self._clicked_one_click and (
            self._clicked_continue or not self._needs_conflict_step()
        )
        # session_conflict starts after one-click only
        if "/products" in url and activated:
            self._enter_products()
        elif "/products" in url and not activated and (self._one_click or self._session_conflict):
            self.url = "https://services.iccchina.com/login"
            self._title = "登录"
            if self._session_conflict:
                self._body = (
                    "账号已被登录 继续登录 仅人工询价登录 暂不登录 一键登录"
                )
            else:
                self._body = "微信扫码登录 账号密码登录 一键登录 免费注册"
        return None

    def cookies(self):
        return list(self._cookies)

    def _needs_conflict_step(self) -> bool:
        # 若构造时要求冲突弹窗，则必须点「继续登录」
        return bool(getattr(self, "_require_continue", False))

    def _enter_products(self):
        self.url = "https://services.iccchina.com/products"
        self._title = "慧讯网产品库"
        self._body = "建筑材料 产品检索 规格 型号 退出登录"
        self._one_click = False
        self._session_conflict = False

    def _on_one_click(self):
        self._clicked_one_click = True
        # 真实站点：点一键登录后常弹「账号已被登录」
        if getattr(self, "_require_continue", False) or self._session_conflict:
            self._session_conflict = True
            self._one_click = False
            self._body = (
                "账号已被登录 当前账号已被您登录 继续登录 仅人工询价登录 暂不登录"
            )
            return
        self._enter_products()

    def _on_continue(self):
        self._clicked_continue = True
        self._enter_products()


def test_huixun_qr_login_is_not_mistaken_for_logged_in():
    page = _Page(
        "https://services.iccchina.com/products",
        "慧讯网",
        "微信扫码登录 免费注册",
    )
    ok, reason = verify_logged_in(page, "huixun", user_confirmed=True)
    assert not ok
    assert "未登录" in reason or "登录会话" in reason


def test_member_site_needs_positive_identity_evidence():
    page = _Page(
        "https://www.hylcw.cn/marketPrice/so.html",
        "领材网市场价",
        "建筑材料市场价格",
    )
    ok, reason = verify_logged_in(page, "lingcai", user_confirmed=True)
    assert not ok
    assert "登录会话" in reason or "会员" in reason or "强制确认" in reason


def test_member_site_positive_logout_link_passes():
    page = _Page(
        "https://www.gldjc.com/scj/so.html",
        "广材网",
        "个人中心 退出登录 材料价格",
    )
    ok, _reason = verify_logged_in(page, "guangcai", user_confirmed=False)
    assert ok


def test_huixun_session_cookie_passes_without_logout_text():
    """慧讯已登录后产品库常不渲染「退出登录」，但有 has_logined 等 cookie。"""
    page = _Page(
        "https://services.iccchina.com/products",
        "慧讯网产品库",
        "建筑材料 产品检索 规格 型号",
        cookies=[
            {
                "name": "has_logined",
                "value": "true",
                "domain": ".iccchina.com",
            },
            {
                "name": "quick_login_token",
                "value": "abc123token",
                "domain": ".iccchina.com",
            },
        ],
    )
    hits = auth_cookie_hits(page, "huixun")
    assert "has_logined" in hits
    ok, reason = verify_logged_in(page, "huixun", user_confirmed=True)
    assert ok, reason
    assert "Cookie" in reason or "cookie" in reason.lower() or "会话" in reason


def test_guangcai_token_cookie_passes():
    page = _Page(
        "https://www.gldjc.com/scj/so.html?keyword=x",
        "广材网",
        "市场信息价 搜索结果",
        cookies=[{"name": "token", "value": "sess-xyz", "domain": ".gldjc.com"}],
    )
    ok, reason = verify_logged_in(page, "guangcai", user_confirmed=True)
    assert ok, reason


def test_falsey_auth_cookie_does_not_pass():
    page = _Page(
        "https://services.iccchina.com/products",
        "慧讯网",
        "产品库",
        cookies=[
            {"name": "has_logined", "value": "false", "domain": ".iccchina.com"},
        ],
    )
    ok, _reason = verify_logged_in(page, "huixun", user_confirmed=True)
    assert not ok


def test_session_cookie_overrides_stray_password_input():
    """已登录会话下，页面偶发隐藏密码框不应再判未登录。"""
    page = _Page(
        "https://services.iccchina.com/products",
        "慧讯网产品库",
        "产品列表",
        passwords=1,
        cookies=[
            {
                "name": "_icc_session_id",
                "value": "sid-1",
                "domain": ".iccchina.com",
            }
        ],
    )
    ok, reason = verify_logged_in(page, "huixun", user_confirmed=True)
    assert ok, reason


def test_huixun_login_page_cookie_alone_is_not_logged_in():
    """关窗重开：登录页仍有 has_logined cookie，但未点一键登录 → 未登录。"""
    page = _Page(
        "https://services.iccchina.com/login",
        "登录",
        "微信扫码登录 账号密码登录 一键登录 免费注册 记住的账号",
        one_click=True,
        cookies=[
            {"name": "has_logined", "value": "true", "domain": ".iccchina.com"},
            {"name": "quick_login_token", "value": "tok", "domain": ".iccchina.com"},
            {"name": "remember_user", "value": "u1", "domain": ".iccchina.com"},
        ],
    )
    assert page_shows_one_click_login(page)
    ok, reason = verify_logged_in(page, "huixun", user_confirmed=True)
    assert not ok, reason
    assert "一键登录" in reason or "登录页" in reason


def test_huixun_auto_click_one_click_login_resumes_session():
    """有一键登录 + 账号缓存时，自动点击并进入产品库。"""
    page = _Page(
        "https://services.iccchina.com/login",
        "登录",
        "微信扫码登录 一键登录 账号信息",
        one_click=True,
        cookies=[
            {"name": "has_logined", "value": "true", "domain": ".iccchina.com"},
            {"name": "quick_login_token", "value": "tok", "domain": ".iccchina.com"},
        ],
    )
    ok, reason = try_resume_huixun_session(page)
    assert ok, reason
    assert page._clicked_one_click
    assert "products" in page.url
    assert "一键登录" in reason or "恢复" in reason or "进入" in reason


def test_ensure_logged_in_or_resume_prefers_one_click_over_cookie():
    """ensure 路径：不能因 cookie 误判通过，应自动点一键登录。"""
    page = _Page(
        "https://services.iccchina.com/login",
        "登录",
        "一键登录 微信扫码登录",
        one_click=True,
        cookies=[
            {"name": "has_logined", "value": "true", "domain": ".iccchina.com"},
        ],
    )
    ok, reason = ensure_logged_in_or_resume(
        page, "huixun", "https://services.iccchina.com/login", user_confirmed=True
    )
    assert ok, reason
    assert page._clicked_one_click
    assert "products" in (page.url or "")


def test_try_click_one_click_login_finds_button():
    page = _Page(
        "https://services.iccchina.com/login",
        "登录",
        "一键登录",
        one_click=True,
    )
    ok, label = try_click_one_click_login(page)
    assert ok
    assert label == "一键登录"
    assert page._clicked_one_click


def test_huixun_session_conflict_modal_continue_login():
    """点一键登录后弹出「账号已被登录」→ 自动点「继续登录」。"""
    page = _Page(
        "https://services.iccchina.com/login",
        "登录",
        "您已开启三天免登录 zhdj1 一键登录",
        one_click=True,
        cookies=[
            {"name": "has_logined", "value": "true", "domain": ".iccchina.com"},
            {"name": "quick_login_token", "value": "tok", "domain": ".iccchina.com"},
        ],
    )
    page._require_continue = True
    ok, reason = try_resume_huixun_session(page)
    assert ok, reason
    assert page._clicked_one_click
    assert page._clicked_continue
    assert "products" in page.url
    assert "继续登录" in reason or "一键登录" in reason


def test_page_shows_session_conflict():
    page = _Page(
        "https://services.iccchina.com/login",
        "登录",
        "账号已被登录 继续登录 仅人工询价登录 暂不登录",
        session_conflict=True,
    )
    assert page_shows_session_conflict(page)
    ok, label = try_handle_huixun_session_conflict(page)
    assert ok
    assert label == "继续登录"
    assert page._clicked_continue
