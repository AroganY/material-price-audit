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
