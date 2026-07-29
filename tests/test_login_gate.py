from material_price_audit.login_gate import verify_logged_in


class _Locator:
    def __init__(self, count=0):
        self._count = count

    def count(self):
        return self._count


class _Page:
    def __init__(self, url, title, body, passwords=0):
        self.url = url
        self._title = title
        self._body = body
        self._passwords = passwords

    def title(self):
        return self._title

    def inner_text(self, _selector):
        return self._body

    def locator(self, _selector):
        return _Locator(self._passwords)


def test_huixun_qr_login_is_not_mistaken_for_logged_in():
    page = _Page(
        "https://services.iccchina.com/products",
        "慧讯网",
        "微信扫码登录 免费注册",
    )
    ok, reason = verify_logged_in(page, "huixun", user_confirmed=True)
    assert not ok
    assert "未登录" in reason


def test_member_site_needs_positive_identity_evidence():
    page = _Page(
        "https://www.hylcw.cn/marketPrice/so.html",
        "领材网市场价",
        "建筑材料市场价格",
    )
    ok, reason = verify_logged_in(page, "lingcai", user_confirmed=True)
    assert not ok
    assert "会员身份" in reason


def test_member_site_positive_logout_link_passes():
    page = _Page(
        "https://www.gldjc.com/scj/so.html",
        "广材网",
        "个人中心 退出登录 材料价格",
    )
    ok, _reason = verify_logged_in(page, "guangcai", user_confirmed=False)
    assert ok
