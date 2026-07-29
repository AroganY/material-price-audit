from material_price_audit.matching import strict_name_spec_match, unit_compatibility
from material_price_audit.models import CanonicalItem


def item(name: str, spec: str) -> CanonicalItem:
    return CanonicalItem(id="x", sheet="s", row=1, name=name, spec=spec)


def test_led_ground_light_requires_every_hard_parameter():
    target = item(
        "LED地埋灯",
        "电源:DC24V功率:9W色温:3500K角度:15°防护等级:≥IP67控制方式:ON/OFF",
    )
    exact = (
        "地埋灯 光源:LED灯 功率(W):9 额定电压:DC24V "
        "色温(K):3500 光束角(°):15 防护等级:IP68 控制方式:ON/OFF"
    )
    wrong = "地埋灯 光源:LED灯 功率(W):9 额定电压(V):24 控制方式:DMX512"
    assert strict_name_spec_match(target, "地埋灯", exact).ok
    result = strict_name_spec_match(target, "地埋灯", wrong)
    assert not result.ok
    assert "规格缺少" in result.detail or "规格冲突" in result.detail


def test_controller_accepts_port_and_dmx512_synonyms_but_still_requires_voltage():
    target = item(
        "8端口分控器",
        "脱机分控器，AC220V/8端口分控制器,各端口标准512通道",
    )
    exact = "分控器 脱机 工作电压AC220V 8路独立数据输出 支持DMX512"
    missing_voltage = "分控器 脱机 8路独立数据输出 支持DMX512"
    assert strict_name_spec_match(target, "分控器", exact).ok
    assert not strict_name_spec_match(target, "分控器", missing_voltage).ok


def test_controller_voltage_range_can_cover_requested_voltage():
    target = item(
        "8端口分控器",
        "脱机分控器，AC220V/8端口分控制器,各端口标准512通道",
    )
    text = "分控器 脱机 工作电压AC92-264V 8路独立信号数据输出 支持DMX512"
    assert strict_name_spec_match(target, "分控器", text).ok


def test_controller_rejects_online_when_offline_is_required():
    target = item(
        "8端口分控器",
        "脱机分控器，AC220V/8端口分控制器,各端口标准512通道",
    )
    wrong = "联机工作 DMX512 8端口分控器 AC220V 512通道"
    result = strict_name_spec_match(target, "8端口分控器", wrong)
    assert not result.ok
    assert result.outcome == "reject"
    assert "联机" in result.detail


def test_model_suffix_must_be_exact():
    target = item("网络摄像机", "型号：DS-2CD3T46WDV3-I3")
    exact = strict_name_spec_match(
        target, "网络摄像机", "产品型号：DS-2CD3T46WDV3-I3"
    )
    wrong = strict_name_spec_match(
        target, "网络摄像机", "产品型号：DS-2CD3T46WDV3-I5"
    )
    assert exact.ok
    assert not wrong.ok
    assert wrong.outcome == "reject"
    assert "页面型号" in wrong.detail


def test_explicit_price_unit_conflict_is_rejected():
    assert unit_compatibility("m", "米")[0] is True
    ok, detail = unit_compatibility("m", "套")
    assert ok is False
    assert "单位冲突" in detail


def test_piece_like_units_are_compatible():
    """消声器：询价表「节」 vs 广材「台/个」应视为同类，不能硬拒。"""
    assert unit_compatibility("节", "台")[0] is True
    assert unit_compatibility("节", "个")[0] is True
    assert unit_compatibility("台", "套")[0] is True


def test_xzp100_silencer_matches_guangcai_title():
    """
    真实踩坑：名称粘了尺寸+有效长度，匹配词变成「型片式消声器/有效长度」，
    页面标题「XZP100片式消声器」被整表 reject；图集号 15K116-1 页面常不写。
    """
    target = item(
        "XZP100型片式消声器 1250X400 有效长度：1500",
        "15K116-1",
    )
    # 广材常见：标题无「型」、详情有截面/长度，可不写图集号
    title = "XZP100片式消声器"
    detail = "XZP100 片式消声器 截面1250×400 有效长度1500"
    mr = strict_name_spec_match(target, title, detail)
    assert mr.ok, mr.detail
    # 仅标题无尺寸：名称应能过，规格可 review（绝不能名称 reject）
    mr2 = strict_name_spec_match(target, title, "XZP100片式消声器 通风消声")
    assert "名称未命中" not in mr2.detail
    assert mr2.outcome != "reject"


def test_name_core_drops_effective_length_noise():
    from material_price_audit.matching import name_core_words, name_search_core

    core = name_search_core("XZP100型片式消声器 1250X400 有效长度：1500")
    assert "有效长度" not in core
    assert "片式消声器" in core or "消声器" in core
    words = name_core_words("XZP100型片式消声器 1250X400 有效长度：1500")
    assert "有效长度" not in words
    assert any("片式消声器" in w or w == "片式消声器" for w in words)


def test_xzp100_type_char_is_same_model():
    """XZP100型 ≡ XZP100，标题无「型」也能名称命中。"""
    target = item("XZP100型片式消声器", "15K116-1")
    mr = strict_name_spec_match(target, "XZP100片式消声器", "XZP100片式消声器")
    assert mr.ok or "名称未命中" not in mr.detail, mr.detail
    assert mr.outcome != "reject" or mr.ok


def test_section_matches_three_number_size():
    """询价 1250x400 应命中页面 1250×400×1500（第三维有效长）。"""
    target = item(
        "XZP100型片式消声器 1250X400 有效长度：1500",
        "15K116-1",
    )
    mr = strict_name_spec_match(
        target,
        "XZP100片式消声器",
        "型号XZP100 规格1250×400×1500",
    )
    assert mr.ok, mr.detail


def test_cost_queries_include_section_size():
    from material_price_audit.normalize import build_cost_site_queries, peel_dims_into_spec

    name = "XZP100型片式消声器 1250X400 有效长度：1500"
    spec = "15K116-1"
    n, s = peel_dims_into_spec(name, spec)
    qs = build_cost_site_queries(n, s, "", None)
    joined = " ".join(qs).lower()
    assert "1250" in joined and "400" in joined, qs
    assert any("xzp100" in q.lower() and "1250" in q.lower() for q in qs), qs


def test_line_light_name_drops_decorative_led_and_o1():
    target = item(
        "LED成品线型灯O1",
        "电源:DC24V功率:18W/m色温:3500K角度:180°防护等级:≥IP65控制方式:ON/OFF",
    )
    exact = (
        "线形灯 工作电压DC24V 功率(W/m):18 色温(K):3500 "
        "角度:180° 防护等级:IP65 控制方式:ON/OFF"
    )
    assert strict_name_spec_match(target, "线形灯", exact).ok


def test_concatenated_chinese_spec_still_requires_voltage_and_kelvin():
    target = item(
        "LED成品线型灯O1",
        "电源:DC24V功率:18W/m色温:3500K角度:180°防护等级:≥IP65控制方式:ON/OFF",
    )
    missing_voltage_and_color = (
        "线型灯 功率(W/m):18 角度:180° 防护等级:IP65 控制方式:ON/OFF"
    )
    result = strict_name_spec_match(target, "线型灯", missing_voltage_and_color)
    assert not result.ok
    assert "电压 DC24V" in result.detail
    assert "色温 3500K" in result.detail


def test_selectable_ranges_and_meter_unit_count_as_exact_coverage():
    ground = item(
        "LED地埋灯",
        "DC24V、9W、3500K、15°、IP67、ON/OFF",
    )
    ground_text = (
        "地埋灯 功率(W):9 输入电压DC24V 防护等级:IP67 控制方式:ON/OFF "
        "色温(K):2200-6500 光束角(°):10°-60°"
    )
    line = item(
        "LED成品线型灯O1",
        "DC24V、18W/m、3500K、180°、IP65、ON/OFF",
    )
    line_text = (
        "线型灯 功率(W):18 单位:m 输入电压DC24V 色温(K):3500 "
        "角度:180° 防护等级:IP65 控制方式:ON/OFF"
    )
    assert strict_name_spec_match(ground, "地埋灯", ground_text).ok
    assert strict_name_spec_match(line, "线型灯", line_text).ok
