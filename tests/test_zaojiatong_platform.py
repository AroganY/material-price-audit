"""造价通（zjtcn.com）适配离线回归。"""

from material_price_audit.login_gate import (
    CHECK_URLS,
    MEMBERSHIP_PLATFORMS,
    auth_cookie_hits,
    verify_logged_in,
)
from material_price_audit.platforms import (
    BUILTIN,
    CORE_PLATFORM_IDS,
    HANDLERS,
    normalize_platform_id,
    parse_zaojiatong_result_rows,
)


def test_zaojiatong_is_core_builtin_with_handler():
    assert "zaojiatong" in CORE_PLATFORM_IDS
    assert "zaojiatong" in BUILTIN
    spec = BUILTIN["zaojiatong"]
    assert spec.name == "造价通"
    assert "member.zjtcn.com" in spec.login_url
    # 登录必须带 url 回跳分站，否则登完仍停在 member，每条新链接再要登录
    assert "url=" in spec.login_url
    assert "gd.zjtcn.com" in spec.login_url or "shichangjia" in spec.login_url
    assert "shichangjia/list" in spec.search_url_template
    assert "{query}" in spec.search_url_template
    assert "zjtcn.com" in spec.item_link_contains
    assert spec.handler == "zaojiatong"
    assert "zaojiatong" in HANDLERS


def test_zaojiatong_aliases():
    for alias in ("造价通", "zjtcn", "zjt", "ZJT", "zaojia", "中建普联"):
        assert normalize_platform_id(alias) == "zaojiatong"


def test_zaojiatong_login_gate_config():
    assert "zaojiatong" in MEMBERSHIP_PLATFORMS
    assert "zjtcn.com" in CHECK_URLS["zaojiatong"]
    assert "shichangjia" in CHECK_URLS["zaojiatong"]


def test_zaojiatong_positive_text_passes_login():
    class _Loc:
        def count(self):
            return 0

    class Page:
        url = "https://gd.zjtcn.com/shichangjia/list/c_t_d_k.html"

        def title(self):
            return "2026年03月市场价_广东2026年03月厂家报价 - 广东造价通"

        def inner_text(self, _):
            return "退出登录 我的造价通 会员中心 市场价 材料名称 规格型号"

        def locator(self, _):
            return _Loc()

        def cookies(self):
            return []

    ok, reason = verify_logged_in(Page(), "zaojiatong", user_confirmed=True)
    assert ok, reason


def test_zaojiatong_login_page_rejected():
    class _Loc:
        def count(self):
            return 1

    class Page:
        url = "https://member.zjtcn.com/common/login.html?url=https://gd.zjtcn.com/"

        def title(self):
            return "会员登录_造价通"

        def inner_text(self, _):
            return "会员登录 请输入手机号/账号 请输入密码 扫码登录 登录"

        def locator(self, _):
            return _Loc()

        def cookies(self):
            return []

    ok, reason = verify_logged_in(Page(), "zaojiatong", user_confirmed=True)
    assert not ok
    assert "登录" in reason or "表单" in reason or "会话" in reason


def test_zaojiatong_session_cookie_passes():
    class _Loc:
        def count(self):
            return 0

    class Page:
        url = "https://gd.zjtcn.com/shichangjia/list/c_t_d_k_%E9%95%82%E9%94%8C%E7%AE%A1.html"

        def title(self):
            return "市场价 - 广东造价通"

        def inner_text(self, _):
            return "市场价 材料名称搜索 规格型号 供应商"

        def locator(self, _):
            return _Loc()

        def cookies(self):
            return [
                # jsid 匿名也有，不能单独当登录证据
                {"name": "jsid", "value": "24b53f08-4081-4242-97fe", "domain": ".zjtcn.com"},
                {"name": "uid", "value": "user-1001", "domain": ".zjtcn.com"},
                {"name": "token", "value": "sess-zjt-1", "domain": ".zjtcn.com"},
            ]

    hits = auth_cookie_hits(Page(), "zaojiatong")
    assert hits
    assert "jsid" not in [h.lower() for h in hits] or "uid" in [h.lower() for h in hits]
    ok, reason = verify_logged_in(Page(), "zaojiatong", user_confirmed=True)
    assert ok, reason


def test_zaojiatong_anonymous_jsid_alone_is_not_login():
    """匿名访客也会带 jsid，不能据此判已登录。"""
    class _Loc:
        def count(self):
            return 0

    class Page:
        url = "https://gd.zjtcn.com/shichangjia/list/c_t_d_k.html"

        def title(self):
            return "市场价 - 广东造价通"

        def inner_text(self, _):
            return "市场价 登录 注册 材料名称"

        def locator(self, _):
            return _Loc()

        def cookies(self):
            return [
                {"name": "jsid", "value": "anon-only", "domain": ".zjtcn.com"},
            ]

    hits = auth_cookie_hits(Page(), "zaojiatong")
    assert not hits
    ok, reason = verify_logged_in(Page(), "zaojiatong", user_confirmed=True)
    assert not ok


def test_probe_zaojiatong_detects_spa_login_kick():
    """模拟 SPA 把市场价踢到登录页。"""
    from material_price_audit.login_gate import probe_zaojiatong_market_session

    class Loc:
        def count(self):
            return 0

        def first(self):
            return self

        def is_visible(self, timeout=0):
            return False

    class Page:
        def __init__(self):
            self._n = 0
            self.url = "https://gd.zjtcn.com/shichangjia/list/c_t_d_k.html"

        def goto(self, url, **kwargs):
            self.url = url

        def wait_for_timeout(self, _ms):
            self._n += 1
            # 第 2 次等待后模拟 SPA 踢登录
            if self._n >= 2:
                self.url = (
                    "https://member.zjtcn.com/common/login.html"
                    "?url=https://gd.zjtcn.com/shichangjia/list/c_t_d_k.html"
                )

        def title(self):
            if "login" in self.url:
                return "会员登录_造价通"
            return "市场价"

        def inner_text(self, _):
            if "login" in self.url:
                return "会员登录 请输入密码"
            return "材料名称 查看价格"

        def locator(self, _):
            return Loc()

        def cookies(self):
            return [{"name": "jsid", "value": "x", "domain": ".zjtcn.com"}]

        @property
        def context(self):
            return self

    ok, reason = probe_zaojiatong_market_session(Page(), timeout_ms=5000)
    assert not ok
    assert "登录" in reason


def test_zaojiatong_conflict_dialog_clicks_continue():
    """账号使用中弹窗 → 自动点「继续登录」。"""
    from material_price_audit.login_gate import (
        page_shows_session_conflict,
        try_handle_zaojiatong_session_conflict,
    )

    class Btn:
        def __init__(self):
            self.clicked = False

        def count(self):
            return 1

        @property
        def first(self):
            return self

        def is_visible(self, timeout=0):
            return True

        def click(self, **kwargs):
            self.clicked = True

        def scroll_into_view_if_needed(self, **kwargs):
            pass

    class Page:
        def __init__(self):
            self.btn = Btn()
            self.handlers = []

        def inner_text(self, _):
            return (
                "账号【nw046380】正在登录使用中，强行登录会导致当前使用者无法正常使用，是否继续？"
                "确认当前使用者是否仍需使用账号 更换其他账号登录"
            )

        def locator(self, sel):
            if "mb_btn_ok" in sel or "继续登录" in sel:
                return self.btn
            return Btn()  # empty-ish

        def wait_for_timeout(self, _):
            pass

        def on(self, *_a, **_k):
            pass

        def get_by_role(self, *_a, **_k):
            class Empty:
                def count(self):
                    return 0

            return Empty()

        def get_by_text(self, *_a, **_k):
            class Empty:
                def count(self):
                    return 0

                @property
                def first(self):
                    return self

                def is_visible(self, timeout=0):
                    return False

            return Empty()

    p = Page()
    assert page_shows_session_conflict(p)
    ok, lab = try_handle_zaojiatong_session_conflict(p)
    assert ok
    assert lab == "继续登录"
    assert p.btn.clicked


def test_parse_zaojiatong_row_with_visible_price():
    rows = parse_zaojiatong_result_rows(
        [
            {
                "index": 0,
                "name": "镀锌圆钢",
                "text": (
                    "原始名称：镀锌圆钢 规格型号：直径(mm)：12 牌号：HPB300 "
                    "除税市场价：3850 含税市场价：4350.5 13% t 2026-03 "
                    "供应商名称：佛山市管鸿钢业有限公司"
                ),
                "priceText": "3850",
                "href": "https://gd.zjtcn.com/shichangjia/info_0191430100014.html",
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "镀锌圆钢"
    assert rows[0]["price"] == 3850
    assert rows[0]["tax_mode"] == "tax_excl"
    assert "管鸿" in rows[0]["supplier"]
    assert "12" in rows[0]["spec"] or "12" in rows[0]["text"]


def test_parse_zaojiatong_view_price_placeholder():
    """未登录时价列为「查看价格」，不得编造数字。"""
    rows = parse_zaojiatong_result_rows(
        [
            {
                "index": 1,
                "name": "镀锌锚环(U型)",
                "text": (
                    "原始名称：镀锌锚环(U型) 规格型号：直径80mm圆钢,长度5.4m/根 "
                    "查看价格 查看价格 13% 个 2026-03 "
                    "供应商名称：深圳市天勤建材有限公司"
                ),
                "priceText": "查看价格",
                "href": "/shichangjia/info_0194226100001.html?priceType=2",
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0]["price"] is None
    assert "天勤" in rows[0]["supplier"]
    assert "锚环" in rows[0]["name"]


def test_parse_zaojiatong_skips_header_row():
    rows = parse_zaojiatong_result_rows(
        [
            {
                "index": 0,
                "name": "材料名称及规格型号",
                "text": "材料名称及规格型号 除税市场价 除税建议价 税率 单位 操作",
            },
            {
                "index": 1,
                "name": "刚性系杆XG1",
                "text": "原始名称：刚性系杆XG1 规格型号：Q235B圆钢管89×2.5 除税市场价：6200 13% t",
                "priceText": "6200",
            },
        ]
    )
    assert len(rows) == 1
    assert "系杆" in rows[0]["name"]
    assert rows[0]["price"] == 6200


def test_parse_zaojiatong_ssr_html_extracts_rows_without_login():
    """SSR 列表在未登录时也能解析名称/规格；价格可为占位。"""
    from material_price_audit.platforms import parse_zaojiatong_ssr_html

    html = """
    <table><tbody>
    <tr class="flex flex-row"><th>材料名称及规格型号</th><th>除税市场价</th></tr>
    <tr class="flex flex-row">
      <td><a href="/shichangjia/info_123.html?priceType=2" class="material-title">镀锌圆钢</a>
      <div>原始名称：</div><span>镀锌圆钢</span>
      <div>规格型号：</div><span>直径(mm)：12 牌号：HPB300</span>
      <span>查看价格</span><span>查看价格</span>
      <span>13%</span><span>t</span>
      <div>供应商名称：</div><span>佛山市管鸿钢业有限公司</span></td>
    </tr>
    <tr class="flex flex-row">
      <td><a href="/shichangjia/info_456.html" class="material-title">刚性系杆XG1</a>
      原始名称：刚性系杆XG1 规格型号：Q235B圆钢管89×2.5 除税市场价：6200
      供应商名称：深圳万达膜结构有限公司</td>
    </tr>
    </tbody></table>
    """
    rows = parse_zaojiatong_ssr_html(html)
    assert len(rows) >= 2
    assert any("镀锌" in (r.get("name") or "") or "圆钢" in (r.get("name") or "") for r in rows)
    priced = [r for r in rows if r.get("price")]
    assert any(r.get("price") == 6200 for r in priced)
