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


def test_char_bag_name_equivalence_not_hardcoded_list():
    """
    同字异序品名应互认（通用算法，非词表）：
    地埋灯↔埋地灯；不依赖写死同义词。
    """
    from material_price_audit.matching import soft_product_name_equivalent

    assert soft_product_name_equivalent("地埋灯", "埋地灯", "")
    assert soft_product_name_equivalent("埋地灯", "地埋灯", "")
    assert soft_product_name_equivalent("LED地埋灯", "埋地灯", "埋地灯 18W")
    # 不同品类不能因有共同字就过
    assert not soft_product_name_equivalent("冷却塔", "塔吊", "")

    a = strict_name_spec_match(item("地埋灯", "9W"), "埋地灯", "埋地灯 功率9W IP67")
    assert "名称未命中" not in (a.detail or ""), a.detail
    b = strict_name_spec_match(
        item("LED地埋灯", "DC24V 9W"),
        "埋地灯",
        "埋地灯 功率(W):9 电压DC24V",
    )
    assert "名称未命中" not in (b.detail or ""), b.detail
    c = strict_name_spec_match(item("埋地灯", ""), "地埋灯", "地埋灯 LED")
    assert c.ok, (c.outcome, c.detail)


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


# ─── P0：规格匹配准确性（DN/电话/价格噪声/条件去重）────────────────────


def test_dn150_rejects_dn40_when_phone_contains_150():
    """目标 DN150，页面 DN40 + 手机号含 150 → 必须 reject，不得因电话数字误命中。"""
    target = item("闸阀", "DN150")
    page = (
        "闸阀 规格型号：DN40 PN16 "
        "供应商名称：某某阀门有限公司 "
        "手机号码：13800150123 联系人：张三"
    )
    mr = strict_name_spec_match(target, "闸阀", page)
    assert not mr.ok
    assert mr.outcome == "reject", (mr.outcome, mr.detail)
    assert "DN150" in mr.detail or "尺寸" in mr.detail or "规格冲突" in mr.detail


def test_dn100_not_hit_by_market_price_100():
    """正文只有市场价 100、无 DN100 → 不得命中口径。"""
    target = item("闸阀", "DN100")
    page = "闸阀 规格型号：DN40 市场价： ￥100 建议价： ￥100 运费说明：本报价不含运费"
    mr = strict_name_spec_match(target, "闸阀", page)
    assert not mr.ok
    # 不得因 ￥100 判定 DN100 已命中
    assert mr.outcome in ("reject", "review"), mr.detail
    if mr.outcome == "review":
        assert "DN100" in mr.detail or "尺寸" in mr.detail
    # 页面有 DN40 时应是硬冲突 reject
    assert mr.outcome == "reject" or "缺少" in mr.detail


def test_dn200_not_hit_by_address_or_phone_200():
    """地址/电话出现 200 不得命中 DN200。"""
    target = item("截止阀", "DN200")
    page = (
        "截止阀 规格型号：DN50 "
        "固定电话：020-12342008 联系地址：广州市某路200号 "
        "手机号码：13900000200"
    )
    mr = strict_name_spec_match(target, "截止阀", page)
    assert not mr.ok
    assert mr.outcome == "reject", (mr.outcome, mr.detail)


def test_dn150_hits_when_spec_field_explicit():
    """规格字段明确写 DN150 → 必须命中 accept。"""
    target = item("闸阀", "DN150")
    page = "闸阀 原始名称：闸阀 规格型号：DN150 PN16 除税市场价：128.5 供应商名称：某某公司"
    mr = strict_name_spec_match(target, "闸阀", page)
    assert mr.ok, (mr.outcome, mr.detail)
    assert mr.outcome == "accept"


def test_dn150_hits_explicit_platform_spec_mm_field():
    """造价平台「规格(mm):150」是明确规格字段，可等价于 DN150。"""
    target = item("不锈钢卡箍", "DN150")
    page = "品种 : 卡箍 | 材质 : 不锈钢 | 规格(mm) : 150 | 类型 : 铸铁管用"
    mr = strict_name_spec_match(
        target,
        "不锈钢卡箍",
        page,
        match_spec_text=page,
        spec_seen=page,
    )
    assert mr.ok, (mr.outcome, mr.detail)
    assert mr.outcome == "accept"


def test_dn150_does_not_accept_cross_section_150x150_as_bore():
    """规格(mm):150×150 是截面，不得冒充单口径 DN150。"""
    target = item("不锈钢卡箍", "DN150")
    page = "品种 : 卡箍 | 材质 : 不锈钢 | 规格(mm) : 150×150 | 类型 : 钢管用"
    mr = strict_name_spec_match(
        target,
        "不锈钢卡箍",
        page,
        match_spec_text=page,
        spec_seen=page,
    )
    assert not mr.ok
    assert mr.outcome in ("review", "reject")


def test_name_parenthetical_inclusion_must_be_shown_by_source():
    """“含胶圈”是组成要求，不能因核心品名和 DN 对上就自动收价。"""
    target = item("不锈钢卡箍(含胶圈)", "DN150")
    source = "品种 : 卡箍 | 材质 : 不锈钢 | 规格(mm) : 150 | 类型 : 铸铁管用"
    mr = strict_name_spec_match(
        target,
        "不锈钢卡箍",
        source,
        match_spec_text=source,
        spec_seen=source,
    )
    assert not mr.ok
    assert mr.outcome == "review"
    assert "含胶圈" in mr.detail


def test_name_parenthetical_inclusion_accepts_explicit_source_evidence():
    target = item("不锈钢卡箍(含胶圈)", "DN150")
    source = "品种 : 不锈钢卡箍 | 规格(mm) : 150 | 套装 : 含胶圈"
    mr = strict_name_spec_match(
        target,
        "不锈钢卡箍（含胶圈）",
        source,
        match_spec_text=source,
        spec_seen=source,
    )
    assert mr.ok, (mr.outcome, mr.detail)


def test_name_parenthetical_inclusion_rejects_explicit_exclusion():
    target = item("不锈钢卡箍(含胶圈)", "DN150")
    source = "品种 : 不锈钢卡箍 | 规格(mm) : 150 | 包装 : 不含胶圈"
    mr = strict_name_spec_match(
        target,
        "不锈钢卡箍",
        source,
        match_spec_text=source,
        spec_seen=source,
    )
    assert not mr.ok
    assert mr.outcome == "reject"
    assert "不含胶圈" in mr.detail


def test_dn100_forms_only_dimension_not_model():
    """DN100 只能形成一个口径硬条件，不能同时形成型号条件。"""
    from material_price_audit.matching import spec_requirement_groups

    reqs = spec_requirement_groups("DN100")
    kinds = [r["kind"] for r in reqs]
    assert kinds.count("dimension") == 1
    assert "model" not in kinds
    assert all("DN100" in str(r.get("value") or r.get("label")) for r in reqs if r["kind"] == "dimension")


def test_pn16_dn150_only_pressure_and_bore():
    """PN16 DN150 只能形成压力 PN16 和口径 DN150。"""
    from material_price_audit.matching import spec_requirement_groups

    reqs = spec_requirement_groups("PN16 DN150")
    kinds = sorted(r["kind"] for r in reqs)
    assert kinds == ["dimension", "pressure"], reqs
    labels = {r["kind"]: r["label"] for r in reqs}
    assert "PN16" in labels["pressure"]
    assert "DN150" in labels["dimension"] or "150" in labels["dimension"]
    assert not any(r["kind"] == "model" for r in reqs)


def test_electrical_attrs_not_one_model_token():
    """AC220V/DC24V/400W/IP68 分别作为独立属性，不能整串作为型号。"""
    from material_price_audit.matching import spec_requirement_groups

    reqs = spec_requirement_groups("400W/AC220V/DC24V/IP68")
    kinds = [r["kind"] for r in reqs]
    assert "voltage" in kinds
    assert "power" in kinds
    assert "ip" in kinds
    assert "model" not in kinds, reqs
    # 两个电压各自独立
    volts = [r for r in reqs if r["kind"] == "voltage"]
    assert len(volts) == 2
    prefixes = {r.get("prefix") for r in volts}
    assert prefixes == {"AC", "DC"}


def test_name_and_spec_exact_must_accept():
    """名称和规格完全一致时必须 accept。"""
    target = item("球阀", "PN16 DN100")
    page = "球阀 规格型号：PN16 DN100 材质：铸钢"
    mr = strict_name_spec_match(target, "球阀", page)
    assert mr.ok
    assert mr.outcome == "accept", mr.detail


def test_name_ok_spec_absent_only_review():
    """名称一致但规格未展示时只能 review（缺证据，不是冲突）。"""
    target = item("球阀", "PN16 DN100")
    page = "球阀 优质阀门 厂家直供 欢迎询价"
    mr = strict_name_spec_match(target, "球阀", page)
    assert not mr.ok
    assert mr.outcome == "review", (mr.outcome, mr.detail)
    assert "缺少" in mr.detail or "规格" in mr.detail
    assert "冲突" not in mr.detail


def test_name_ok_other_explicit_spec_must_reject():
    """页面明确出现其它规格时必须 reject。"""
    target = item("球阀", "DN100")
    page = "球阀 规格型号：DN50 公称通径DN50"
    mr = strict_name_spec_match(target, "球阀", page)
    assert not mr.ok
    assert mr.outcome == "reject", (mr.outcome, mr.detail)
    assert "冲突" in mr.detail or "页面" in mr.detail
