"""分平台检索词策略：造价站 ≠ 电商站。"""

from material_price_audit.matching import extract_tokens
from material_price_audit.normalize import (
    build_cost_site_queries,
    build_ecommerce_queries,
    build_must,
    build_platform_queries,
    build_queries,
)


def _tok(name: str, spec: str, brand: str = "") -> list[str]:
    return extract_tokens(f"{name} {spec} {brand}")


def test_cost_site_prefers_material_core_not_shop_title():
    name = "LED成品线型灯O1"
    spec = "电源:DC24V功率:18W/m色温:3500K角度:180°防护等级:≥IP65控制方式:ON/OFF"
    brand = "蓝狐"
    q = build_cost_site_queries(name, spec, brand, _tok(name, spec, brand))
    assert q
    # 造价站：核心品名优先（线型灯 / LED线型灯），不是电商标题堆砌
    assert any("线型灯" in x for x in q[:3])
    assert any("18W" in x or "DC24V" in x or "IP65" in x for x in q)
    # 营销编号名不能排第一
    assert q[0] in ("线型灯", "LED线型灯")
    assert "线型灯" in q[0]


def test_ecommerce_prefers_brand_and_params():
    name = "LED地埋灯"
    spec = "电源:DC24V功率:9W色温:3500K角度:15°防护等级:≥IP67控制方式:ON/OFF"
    brand = "蓝狐"
    q = build_ecommerce_queries(name, spec, brand, _tok(name, spec, brand))
    assert q
    # 电商：品牌+品名+参数
    assert any("蓝狐" in x for x in q[:3])
    assert any("9W" in x or "DC24V" in x or "IP67" in x for x in q)
    joined = " ".join(q)
    assert "地埋灯" in joined


def test_controller_keeps_8_port_identity_on_both():
    name = "8端口分控器"
    spec = "脱机分控器，AC220V/8端口分控制器,各端口标准512通道"
    tokens = _tok(name, spec)
    cost = build_cost_site_queries(name, spec, "", tokens)
    ecom = build_ecommerce_queries(name, spec, "", tokens)
    assert any("8端口" in x or "分控器" in x for x in cost[:2])
    assert any("8端口分控器" in x or ("脱机" in x and "分控" in x) for x in ecom + cost)
    # 电商也不能只剩光杆「分控器」排第一
    assert ecom[0] != "分控器"


def test_platform_dispatch():
    name, spec, brand = "闸阀", "DN100 PN16", "正丰"
    tokens = _tok(name, spec, brand)
    for pid in ("guangcai", "lingcai", "huixun", "yize"):
        q = build_platform_queries(pid, name, spec, brand, tokens)
        assert q
        assert any("闸阀" in x for x in q)
        assert any("DN100" in x for x in q) or any("闸阀" == x for x in q)
    for pid in ("jd", "1688"):
        q = build_platform_queries(pid, name, spec, brand, tokens)
        assert q
        # 电商应倾向 品牌+品名 或 品名+DN
        assert any("正丰" in x or "DN100" in x or "闸阀" in x for x in q[:3])


def test_must_does_not_include_spec_field_labels():
    name = "LED筒灯01"
    spec = "电源:DC24V功率:18W色温:3500K"
    must = build_must(name, spec, _tok(name, spec))
    assert "电源" not in must
    assert "功率" not in must
    assert any("筒灯" in m for m in must)


def test_default_build_queries_still_works():
    name = "8端口分控器"
    spec = "脱机分控器，AC220V/8端口分控制器"
    q = build_queries(name, spec, "", _tok(name, spec))
    assert q
    assert q[0] == "8端口分控器" or "8端口" in q[0]
