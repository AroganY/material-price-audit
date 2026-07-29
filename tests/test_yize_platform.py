"""易择网（easybii.com）适配离线回归。"""

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
    parse_yize_result_rows,
)


def test_yize_is_core_builtin_with_handler():
    assert "yize" in CORE_PLATFORM_IDS
    assert "yize" in BUILTIN
    spec = BUILTIN["yize"]
    assert spec.name == "易择网"
    assert "easybii.com" in spec.login_url
    assert "P4-3-info-price-home" in spec.search_url_template
    assert "{query}" not in spec.search_url_template  # 页面填词，不拼 URL
    assert spec.handler == "yize"
    assert "yize" in HANDLERS


def test_yize_aliases():
    for alias in ("易择", "易择网", "易泽网", "easybii", "YIZE", "yizewang"):
        assert normalize_platform_id(alias) == "yize"


def test_yize_login_gate_config():
    assert "yize" in MEMBERSHIP_PLATFORMS
    assert "easybii.com" in CHECK_URLS["yize"]
    assert "info-price" in CHECK_URLS["yize"]


def test_yize_positive_text_passes_login():
    class _Loc:
        def count(self):
            return 0

    class Page:
        url = "https://www.easybii.com/P4-3-info-price-home.html"
        def title(self):
            return "信息价首页-易择网"
        def inner_text(self, _):
            return "我的易择 服务有效期: 2026-12-25 系统消息 收藏夹 信息价"
        def locator(self, _):
            return _Loc()
        def cookies(self):
            return []

    ok, reason = verify_logged_in(Page(), "yize", user_confirmed=True)
    assert ok, reason


def test_yize_login_page_rejected():
    class _Loc:
        def count(self):
            return 1

    class Page:
        url = "https://www.easybii.com/"
        def title(self):
            return "易择网-询价，我们更专业"
        def inner_text(self, _):
            return "密码登录 免密登录 账号： 密码： 立即登录 申请试用"
        def locator(self, _):
            return _Loc()
        def cookies(self):
            return []

    ok, reason = verify_logged_in(Page(), "yize", user_confirmed=True)
    assert not ok
    assert "登录" in reason or "表单" in reason or "会话" in reason


def test_yize_session_cookie_passes():
    class _Loc:
        def count(self):
            return 0

    class Page:
        url = "https://www.easybii.com/P4-3-info-price-home.html"
        def title(self):
            return "信息价首页-易择网"
        def inner_text(self, _):
            return "产品信息 企业信息 信息价 搜索 名称 规格型号"
        def locator(self, _):
            return _Loc()
        def cookies(self):
            return [
                {"name": "userId", "value": "u-1001", "domain": ".easybii.com"},
                {"name": "token", "value": "sess-abc", "domain": "www.easybii.com"},
            ]

    hits = auth_cookie_hits(Page(), "yize")
    assert hits
    ok, reason = verify_logged_in(Page(), "yize", user_confirmed=True)
    assert ok, reason


def test_parse_yize_info_price_row():
    rows = parse_yize_result_rows(
        [
            {
                "index": 0,
                "name": "闸阀",
                "text": "闸阀 DN100 PN16 单位:个 含税价:128.5 除税价:113.7 税率:13%",
                "priceText": "128.5",
                "unit": "个",
                "href": "",
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0]["name"].startswith("闸阀")
    assert rows[0]["price"] == 113.7 or rows[0]["price"] == 128.5
    # 优先除税价
    assert rows[0]["price"] == 113.7
    assert rows[0]["tax_mode"] == "tax_excl"


def test_parse_yize_product_market_price_row():
    rows = parse_yize_result_rows(
        [
            {
                "index": 1,
                "name": "冷却塔",
                "text": "冷却塔 逆流式 品牌:良机 某某机电设备有限公司 市场价:18600 工程价:17200",
                "priceText": "18600",
                "supplier": "某某机电设备有限公司",
                "brand": "良机",
                "href": "https://www.easybii.com/detail/1",
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0]["price"] == 18600
    assert rows[0]["tax_mode"] == "tax_incl"
    assert "机电" in rows[0]["supplier"]


def test_parse_yize_skips_header_only_rows():
    rows = parse_yize_result_rows(
        [
            {"index": 0, "name": "名称", "text": "名称 规格型号 单位 含税价 除税价"},
            {
                "index": 1,
                "name": "铜芯电缆",
                "text": "铜芯电缆 YJV-3*95+1*50 米 含税价:42.3",
                "priceText": "42.3",
            },
        ]
    )
    assert len(rows) == 1
    assert "电缆" in rows[0]["name"]
