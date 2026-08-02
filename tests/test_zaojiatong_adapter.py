"""造价通专用适配器（纯 HTTP SSR）回归。"""

from material_price_audit.adapters import zaojiatong as zjt


SAMPLE_LIST = """
<table><tbody>
<tr class="flex flex-row"><th>材料名称及规格型号</th><th>除税市场价</th></tr>
<tr class="flex flex-row">
  <td><a href="/shichangjia/info_111.html?priceType=2" class="material-title">镀锌圆钢</a>
  <div>原始名称：</div><span>镀锌圆钢</span>
  <div>规格型号：</div><span>直径(mm)：12 牌号：HPB300</span>
  <span>查看价格</span><span>查看价格</span>
  <span>13%</span><span>t</span>
  <div>供应商名称：</div><span>佛山市管鸿钢业有限公司</span></td>
</tr>
<tr class="flex flex-row">
  <td><a href="/shichangjia/info_222.html" class="material-title">闸阀</a>
  原始名称：闸阀 规格型号：DN100 PN16 除税市场价：128.5
  供应商名称：某某阀门有限公司</td>
</tr>
<tr class="flex flex-row">
  <td><a href="/shichangjia/info_333.html?priceType=2" class="material-title">LED地埋灯</a>
  原始名称： LED地埋灯 规格型号： 6W LED 220V IP67
  113.86 市场价： ￥113.86 建议价： ￥113.86
  13% 套 供应商名称： 中山市横栏镇泊辉灯饰厂</td>
</tr>
</tbody></table>
"""


def test_parse_list_and_candidates():
    rows = zjt.parse_list_html(SAMPLE_LIST)
    assert len(rows) >= 2
    cands = zjt.rows_to_candidates(rows, "闸阀", ["闸阀", "DN100"])
    assert cands
    # 有价的闸阀
    priced = [c for c in cands if zjt.is_valid_price(c.get("price_tax"))]
    assert any(abs(c["price_tax"] - 128.5) < 0.01 for c in priced)
    # 无价也 inline，避免详情 goto；且 price_tax 不得是 0/0.01 占位
    for c in cands:
        assert c.get("inline_detail") is True
        assert c.get("platform") == "zaojiatong"
        if not zjt.is_valid_price(c.get("price_tax")):
            assert c.get("price_tax") is None
            assert c.get("needs_detail_price") is True


def test_market_price_label_not_zero():
    """登录后常见「市场价：￥xxx」必须抽到，绝不能变成 0。"""
    rows = zjt.parse_list_html(SAMPLE_LIST)
    dimai = [r for r in rows if "地埋灯" in (r.get("name") or "")]
    assert dimai, "should parse 地埋灯 row"
    assert dimai[0]["price"] is not None
    assert abs(float(dimai[0]["price"]) - 113.86) < 0.01
    cands = zjt.rows_to_candidates(rows, "LED地埋灯", ["地埋灯"])
    hit = [c for c in cands if "地埋灯" in (c.get("title") or "")]
    assert hit and zjt.is_valid_price(hit[0]["price_tax"])
    assert abs(hit[0]["price_tax"] - 113.86) < 0.01


def test_reject_sentinel_prices():
    assert zjt.is_valid_price(None) is False
    assert zjt.is_valid_price(0) is False
    assert zjt.is_valid_price(0.01) is False
    assert zjt.is_valid_price(-1000) is False
    assert zjt.is_valid_price(128.5) is True
    p, _, mode = zjt.extract_visible_price("noTaxPrice:-1000 taxPrice:-1000 查看价格")
    assert p is None
    p, txt, mode = zjt.extract_visible_price("市场价： ￥1143.17 建议价： ￥1143.17")
    assert p is not None and abs(p - 1143.17) < 0.01
    assert mode in ("tax_incl", "tax_excl")


def test_spec_numbers_are_never_parsed_as_market_price():
    """无币种/冒号价格证据时，规格数字必须保持无价。"""
    bad_rows = (
        "刚性系杆 XG1 Q235B圆钢管89×2.5 市场价 查看价格",
        "4级钢筋(Ф28-32)E 市场价 4 查看价格",
        "不锈钢圆钢 直径(mm):30 市场价 查看价格",
    )
    for text in bad_rows:
        price, price_text, _mode = zjt.extract_visible_price(text)
        assert price is None, (text, price, price_text)


def test_unrelated_default_list_rows_are_filtered():
    rows = zjt.parse_list_html(SAMPLE_LIST)
    cands = zjt.rows_to_candidates(rows, "线型灯", ["线型灯", "18W"])
    assert cands == []


def test_cookie_request_failure_does_not_fallback_to_urllib(monkeypatch):
    class BoomRequest:
        def get(self, *_a, **_kw):
            raise RuntimeError("cookie request failed")

    class FakePage:
        class Context:
            request = BoomRequest()

        context = Context()
        request = BoomRequest()

    called = {"n": 0}

    def fake_urlopen(*_a, **_kw):
        called["n"] += 1
        raise AssertionError("must not call urllib without cookies")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert zjt._request_get(FakePage(), "https://gd.zjtcn.com/x", 1000) == ""
    assert called["n"] == 0


def test_search_never_need_login_on_ssr(monkeypatch):
    class FakeResp:
        ok = True
        status = 200

        def text(self):
            return SAMPLE_LIST

    class FakeReq:
        def get(self, url, timeout=0):
            return FakeResp()

    class FakeCtx:
        request = FakeReq()

        def cookies(self):
            return []

    class FakePage:
        context = FakeCtx()
        request = FakeReq()

    cands, st = zjt.search(FakePage(), "闸阀", ["闸阀"], 10000, 1, None)
    assert st == "ok"
    assert st != "need_login"
    assert cands and len(cands) >= 1


def test_search_empty_is_empty_not_login(monkeypatch):
    class FakeResp:
        ok = True
        status = 200

        def text(self):
            return "<html><body>暂无数据</body></html>"

    class FakeReq:
        def get(self, url, timeout=0):
            return FakeResp()

    class FakePage:
        class C:
            request = FakeReq()

            def cookies(self):
                return []

        context = C()
        request = FakeReq()

    cands, st = zjt.search(FakePage(), "不存在材料xyz", ["xyz"], 10000, 1, None)
    assert st == "empty_page"
    assert cands == []


def test_dimension_aliases_dn_phi_vs_diameter():
    from material_price_audit.matching import _dimension_hit, strict_name_spec_match

    assert _dimension_hit("φ12", "直径(mm)：12 牌号：HPB300")
    assert _dimension_hit("DN100", "公称通径 DN100 闸阀")
    assert _dimension_hit("DN50", "直径50")

    class Item:
        name = "镀锌圆钢"
        spec = "φ12 HPB300"
        brand = ""
        unit = "t"

    blob = zjt.expand_spec_aliases_in_text(
        "镀锌圆钢 直径(mm)：12 牌号：HPB300 查看价格"
    )
    mr = strict_name_spec_match(Item(), "镀锌圆钢", blob)
    assert mr.ok, (mr.outcome, mr.detail, mr.missing)


def test_enrich_detail_http_only():
    class FakeResp:
        ok = True
        status = 200

        def text(self):
            return (
                "<html><title>闸阀 DN100</title>"
                "<body>除税市场价：99.5 规格 DN100</body></html>"
            )

    class FakeReq:
        def get(self, url, timeout=0):
            assert "info_" in url
            return FakeResp()

    class Ctx:
        def __init__(self):
            self.request = FakeReq()

        def cookies(self):
            return []

    class FakePage:
        def __init__(self):
            self.context = Ctx()
            self.request = FakeReq()

    cand = {
        "title": "闸阀",
        "url": "https://gd.zjtcn.com/shichangjia/info_1.html",
        "price_tax": None,
        "needs_detail_price": True,
        "platform": "zaojiatong",
    }
    out = zjt.enrich_detail(FakePage(), cand, 10000)
    assert out["price_tax"] == 99.5
    assert out.get("needs_detail_price") is False


def test_enrich_from_inline_text_without_http():
    """spec_seen 已有市场价时，enrich 不应依赖 HTTP，且不得写 0。"""
    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("should not HTTP")

    class FakePage:
        class C:
            request = Boom()

            def cookies(self):
                return []

        context = C()
        request = Boom()

    cand = {
        "title": "LED地埋灯",
        "url": "https://gd.zjtcn.com/shichangjia/info_x.html",
        "price_tax": None,
        "needs_detail_price": True,
        "platform": "zaojiatong",
        "spec_seen": "LED地埋灯 6W 113.86 市场价： ￥113.86 建议价： ￥113.86",
        "detail_text": "LED地埋灯 规格：6W 113.86 市场价： ￥113.86",
    }
    out = zjt.enrich_detail(FakePage(), cand, 10000)
    assert abs(out["price_tax"] - 113.86) < 0.01
    assert out.get("price_source") == "inline_text"


def test_recover_price_in_review_candidates():
    from material_price_audit.inquiry import build_review_candidates, _recover_price_from_attempt
    from material_price_audit.models import CanonicalItem

    a_conflict = {
        "platform": "zaojiatong",
        "price_tax": 0.01,
        "title": "LED地埋灯",
        "spec_seen": "LED地埋灯 6W 113.86 市场价： ￥113.86 建议价： ￥113.86",
        "match_detail": "[造价通·见价需会员]规格冲突：功率 9W，页面为 6W",
        "match_outcome": "reject",
        "conflicts": ["功率 9W，页面为 6W"],
        "match_score": 0.5,
        "name_hit": True,
        "bucket": "candidate",
        "price_hidden_ok": True,
        "url": "https://gd.zjtcn.com/shichangjia/info_x.html",
        "detail_url": "https://gd.zjtcn.com/shichangjia/info_x.html",
        "supplier": "某厂",
        "tax_mode": "tax_excl",
    }
    assert abs(_recover_price_from_attempt(a_conflict) - 113.86) < 0.01
    item = CanonicalItem(
        id="t1",
        sheet="s",
        row=2,
        name="LED地埋灯",
        spec="9W",
        brand="",
        unit="套",
        submit=258.0,
    )
    # 硬冲突不进待核
    assert build_review_candidates(item, [a_conflict], limit=3, match_mode="practical") == []

    a_ok = {
        "platform": "zaojiatong",
        "price_tax": 0.01,
        "title": "LED地埋灯",
        "spec_seen": "LED地埋灯 9W 113.86 市场价： ￥113.86",
        "match_detail": "名称命中；规格缺少：色温 3500K",
        "match_outcome": "review",
        "match_score": 0.6,
        "name_hit": True,
        "bucket": "candidate",
        "url": "https://gd.zjtcn.com/shichangjia/info_y.html",
        "tax_mode": "tax_excl",
    }
    revs = build_review_candidates(item, [a_ok], limit=3, match_mode="practical")
    assert revs and abs(revs[0].price - 113.86) < 0.01
    assert revs[0].price <= float(item.submit)
