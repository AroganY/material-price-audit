from material_price_audit.matching import extract_tokens
from material_price_audit.normalize import build_queries


def test_three_validation_rows_search_by_exact_name_first():
    rows = [
        ("8端口分控器", "脱机分控器，AC220V/8端口分控制器,各端口标准512通道", "分控器"),
        ("LED成品线型灯O1", "DC24V、18W/m、3500K、180°、IP65、ON/OFF", "线型灯"),
        ("LED地埋灯", "DC24V、9W、3500K、15°、IP67、ON/OFF", "地埋灯"),
    ]
    for name, spec, core in rows:
        queries = build_queries(name, spec, "", extract_tokens(f"{name} {spec}"))
        assert queries
        assert queries[0] == name
        if name != core:
            assert core in queries


def test_exact_search_term_is_not_cut_off_for_8_port_controller():
    name = "8端口分控器"
    spec = "脱机分控器，AC220V/8端口分控制器,各端口标准512通道"
    queries = build_queries(name, spec, "", extract_tokens(f"{name} {spec}"))
    assert queries[:2] == ["8端口分控器", "分控器"]
    assert any("脱机" in q and "8端口" in q for q in queries)
    assert len(queries) <= 6
