from urllib.parse import quote, unquote, unquote_to_bytes

from material_price_audit.login_gate import CHECK_URLS

from material_price_audit.platforms import (
    BUILTIN,
    _normalize_1688_price_text,
    _page_1688_captcha,
    _quote_1688_query,
    _quote_lingcai_query,
    _member_rows_to_candidates,
    _search_1688,
)


def test_1688_query_uses_gbk_without_affecting_other_platforms():
    query = "8端口分控器"
    encoded = _quote_1688_query(query)
    assert encoded == "8%B6%CB%BF%DA%B7%D6%BF%D8%C6%F7"
    assert unquote_to_bytes(encoded).decode("gbk") == query
    # 回归：不能再发 UTF-8 字节，否则 1688 会显示“鍒嗘帶鍣�”。
    assert "%E5%88%86%E6%8E%A7%E5%99%A8" not in encoded
    guangcai_url = BUILTIN["guangcai"].search_url_template.format(query=quote(query))
    assert "%E5%88%86%E6%8E%A7%E5%99%A8" in guangcai_url
    assert "%B7%D6%BF%D8%C6%F7" not in guangcai_url


def test_other_query_platforms_keep_single_utf8_encoding():
    query = "8端口分控器"
    utf8_query = quote(query)
    for platform_id in ("guangcai", "jd"):
        url = BUILTIN[platform_id].search_url_template.format(query=utf8_query)
        assert utf8_query in url
        assert "%B7%D6%BF%D8%C6%F7" not in url
    assert "enc=utf-8" in BUILTIN["jd"].search_url_template

    # 慧讯是 SPA：程序直接往搜索框填 Unicode，不在 URL 中二次转码。
    assert "{query}" not in BUILTIN["huixun"].search_url_template


def test_lingcai_query_is_double_utf8_encoded():
    query = "8端口分控器"
    encoded = _quote_lingcai_query(query)
    assert encoded.startswith("8%25E7%25AB%25AF")
    assert "%E7%AB%AF" not in encoded
    assert unquote(unquote(encoded)) == query

    search_url = BUILTIN["lingcai"].search_url_template.format(query=encoded)
    assert "gjz=8%25E7%25AB%25AF" in search_url
    assert unquote(unquote(search_url.split("gjz=", 1)[1])) == query


def test_lingcai_login_check_url_also_uses_double_encoding():
    encoded = CHECK_URLS["lingcai"].split("gjz=", 1)[1]
    assert "%25" in encoded
    assert unquote(unquote(encoded)) == "阀门"


def test_lingcai_rows_are_not_dropped_by_pre_match_score():
    class Page:
        url = "https://www.hylcw.cn/marketPrice/so.html?gjz=x"

    rows = [
        {
            "index": 0,
            "dataId": "quote-1",
            "name": "分控器",
            "text": (
                "分控器 价格因子 规格型号：分控器 DMX512 8端口 "
                "除税价：973.45/台 查看联系方式 成都市 "
                "四川骏涛照明科技有限公司 报价时间：2026-02-10"
            ),
            "priceText": "除税价：973.45/台",
            "hasPriceNode": True,
            "href": "",
        }
    ]
    cands = _member_rows_to_candidates(
        Page(),
        rows,
        "8端口分控器",
        ["分控器", "8端口", "AC220V", "脱机", "512通道"],
        5,
        BUILTIN["lingcai"],
    )

    assert len(cands) == 1
    assert cands[0]["price_tax"] == 973.45
    assert cands[0]["tax_mode"] == "tax_excl"
    assert cands[0]["unit"] == "台"
    assert cands[0]["supplier"] == "四川骏涛照明科技有限公司"


def test_member_platforms_use_real_product_search_routes_and_handlers():
    assert "/marketPrice/so.html" in BUILTIN["lingcai"].search_url_template
    assert "gjz={query}" in BUILTIN["lingcai"].search_url_template
    assert BUILTIN["lingcai"].handler == "lingcai"
    assert BUILTIN["huixun"].search_url_template.endswith("/products")
    assert BUILTIN["huixun"].handler == "huixun"


class _FakePage:
    def __init__(self, url: str, title: str = "", body: str = ""):
        self.url = url
        self._title = title
        self._body = body

    def title(self):
        return self._title

    def inner_text(self, _selector):
        return self._body


def test_1688_punish_page_is_captcha_not_empty_result():
    page = _FakePage(
        "https://s.1688.com/selloffer/offer_search.htm/_____tmd_____/punish?x5secdata=x",
        "验证码拦截",
        "亲，请拖动下方滑块完成验证",
    )
    assert _page_1688_captcha(page)


def test_normal_1688_page_is_not_marked_as_captcha():
    page = _FakePage(
        "https://s.1688.com/selloffer/offer_search.htm?keywords=8%B6%CB%BF%DA",
        "8端口分控器批发",
        "商品列表",
    )
    assert _page_1688_captcha(page) is None


class _Fake1688SearchPage(_FakePage):
    def __init__(self, cards):
        super().__init__("about:blank", "8端口分控器批发", "商品列表")
        self.cards = cards
        self.selector = ""

    def goto(self, url, **_kwargs):
        self.url = url

    def wait_for_timeout(self, _timeout):
        return None

    def wait_for_selector(self, selector, **_kwargs):
        self.selector = selector

    def eval_on_selector_all(self, selector, _script):
        self.selector = selector
        return self.cards


def test_1688_new_mobile_cards_are_parsed_with_title_price_and_supplier():
    page = _Fake1688SearchPage(
        [
            {
                "href": "http://detail.m.1688.com/page/index.html?offerId=607946799976",
                "name": "联机工作 DMX512 8端口分控器",
                "text": "联机工作 DMX512 8端口分控器 ¥ 980 深圳市康之视界实业有限公司",
                "priceText": "¥ 980",
                "supplier": "深圳市康之视界实业有限公司",
                "offerId": "607946799976",
            }
        ]
    )

    cands, status = _search_1688(
        page,
        "8端口分控器",
        ["8端口", "分控器"],
        30_000,
        2,
        BUILTIN["1688"],
    )

    assert status == "ok"
    assert "search-offer-wrapper" in page.selector
    assert len(cands) == 1
    assert cands[0]["price_tax"] == 980
    assert cands[0]["title"] == "联机工作 DMX512 8端口分控器"
    assert cands[0]["supplier"] == "深圳市康之视界实业有限公司"
    assert cands[0]["sku"] == "607946799976"
    assert cands[0]["url"] == "https://detail.1688.com/offer/607946799976.html"
    assert not cands[0].get("inline_detail")


def test_1688_split_decimal_price_is_not_truncated():
    page = _Fake1688SearchPage(
        [
            {
                "href": "http://detail.m.1688.com/page/index.html?offerId=123",
                "name": "智能控制器",
                "text": "智能控制器 ¥ 2 .09",
                "priceText": "¥ 2 .09",
                "supplier": "测试厂家",
                "offerId": "123",
            }
        ]
    )

    cands, status = _search_1688(
        page, "智能控制器", ["控制器"], 30_000, 1, BUILTIN["1688"]
    )

    assert status == "ok"
    assert cands[0]["price_tax"] == 2.09


def test_1688_price_does_not_join_minimum_quantity_into_decimal():
    assert _normalize_1688_price_text("¥ 78 .00 1件起批") == "¥ 78.00 1件起批"
