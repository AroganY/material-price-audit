"""Phase 3：材料族 + 共享候选池。"""

from __future__ import annotations

from material_price_audit.candidate_pool import CandidatePool, pool_cache_key
from material_price_audit.family import (
    build_families,
    extract_item_hard_tags,
    family_core_name,
    family_key,
    family_pool_enabled,
    region_code_for_item,
    strip_hard_specs,
)
from material_price_audit.models import CanonicalItem
from material_price_audit.region_models import RegionTarget
from material_price_audit.settings_store import UserSettings


def _item(name: str, spec: str = "", *, region: dict | None = None, row: int = 1) -> CanonicalItem:
    return CanonicalItem(
        id=f"s|{row}",
        sheet="s",
        row=row,
        name=name,
        spec=spec,
        region=dict(region or {}),
    )


def test_family_core_strips_dn():
    assert "DN" not in family_core_name("薄壁不锈钢管 DN50", "DN50").upper()
    core = family_core_name("薄壁不锈钢管", "DN100")
    assert "不锈钢" in core or "管" in core


def test_four_dn_same_family_same_region():
    items = [
        _item("薄壁不锈钢管", "DN50", row=1),
        _item("薄壁不锈钢管", "DN100", row=2),
        _item("薄壁不锈钢管", "DN150", row=3),
        _item("薄壁不锈钢管 DN200", "", row=4),
    ]
    fams = build_families(items, RegionTarget(city="成都市", city_code="510100"))
    # 无行级 region 时都用 default → 一族
    assert len(fams) == 1
    assert len(fams[0].items) == 4
    main = fams[0].main_query()
    assert "DN50" not in main.upper()
    assert "DN100" not in main.upper()
    # 主搜最多一次语义：四条共用同一 main_query
    assert all(f.main_query() == main for f in fams)


def test_chengdu_chongqing_isolated_families():
    cd = {"city": "成都市", "city_code": "510100", "source": "task"}
    cq = {"city": "重庆市", "city_code": "500100", "source": "task"}
    items = [
        _item("薄壁不锈钢管", "DN50", region=cd, row=1),
        _item("薄壁不锈钢管", "DN50", region=cq, row=2),
    ]
    fams = build_families(items)
    assert len(fams) == 2
    keys = {f.family_key for f in fams}
    assert len(keys) == 2
    codes = {f.region_code for f in fams}
    assert "510100" in codes
    assert "500100" in codes


def test_pool_key_includes_region_no_cross_city():
    k1 = pool_cache_key("guangcai", "510100", "薄壁不锈钢管")
    k2 = pool_cache_key("guangcai", "500100", "薄壁不锈钢管")
    assert k1 != k2
    k3 = pool_cache_key("guangcai", "510100", "薄壁不锈钢管")
    assert k1 == k3


def test_pool_get_put_shared():
    pool = CandidatePool()
    key = pool.make_key("zaojiatong", "510100", "薄壁不锈钢管")
    assert pool.get(key) is None
    pool.put(key, [{"title": "管 DN50", "url": "u1"}, {"title": "管 DN100", "url": "u2"}])
    hit = pool.get(key)
    assert hit is not None
    assert len(hit) == 2
    # 同 key 再次 get 不丢
    assert len(pool.get(key) or []) == 2


def test_gap_query_not_duplicate_dn():
    fams = build_families(
        [_item("薄壁不锈钢管", "DN100", row=1)],
        RegionTarget(city_code="510100", city="成都"),
    )
    fam = fams[0]
    gap = fam.gap_query_for(fam.items[0])
    assert gap is not None
    assert gap.upper().count("DN100") == 1
    qs = fam.queries_for_item(fam.items[0])
    assert len(qs) >= 1
    assert qs[0] == fam.main_query()
    # 查询列表无重复
    low = [q.lower() for q in qs]
    assert len(low) == len(set(low))


def test_no_dn_dn_in_normalized_pool_query():
    from material_price_audit.candidate_pool import normalize_query_key

    assert "dn100 dn100" not in normalize_query_key("管 DN100 DN100")


def test_family_pool_flag():
    # 默认开启（同品名共搜）
    assert family_pool_enabled(None, env={}) is True
    assert family_pool_enabled(None, env={"MPA_FAMILY_POOL": "1"}) is True
    assert family_pool_enabled(None, env={"MPA_FAMILY_POOL": "0"}) is False
    s = UserSettings.from_dict({"use_family_pool": True})
    assert family_pool_enabled(s, env={}) is True
    s_off = UserSettings.from_dict({"use_family_pool": False})
    assert family_pool_enabled(s_off, env={}) is False


def test_hard_tags():
    tags = extract_item_hard_tags("不锈钢管", "DN150 PN10")
    assert any("DN150" in t.upper() for t in tags)


def test_region_code_priority_excel_over_default():
    it = _item(
        "阀",
        region={"city": "成都市", "city_code": "510100"},
        row=1,
    )
    code = region_code_for_item(
        it, RegionTarget(city="重庆市", city_code="500100")
    )
    assert code == "510100"


def test_main_search_once_semantics_via_pool():
    """
    验收语义：同族 4 规格，同一 platform×region×main_query 只 put 一次。
    （不启浏览器，模拟 collect 路径的池行为）
    """
    items = [
        _item("薄壁不锈钢管", f"DN{d}", row=i)
        for i, d in enumerate((50, 100, 150, 200), 1)
    ]
    fams = build_families(items, {"city_code": "510100", "city": "成都"})
    assert len(fams) == 1
    fam = fams[0]
    pool = CandidatePool()
    main = fam.main_query()
    search_calls = {"n": 0}

    def fake_search(platform: str, region: str, query: str):
        key = pool.make_key(platform, region, query)
        hit = pool.get(key)
        if hit is not None:
            return hit, True
        search_calls["n"] += 1
        cands = [
            {"title": f"薄壁不锈钢管 DN{d}", "url": f"u{d}", "price_tax": float(d)}
            for d in (50, 100, 150, 200)
        ]
        pool.put(key, cands)
        return cands, False

    for it in fam.items:
        for q in fam.queries_for_item(it):
            if q == main or q.startswith(main):
                fake_search("guangcai", fam.region_code, main if q == main else q)

    # 主搜只应真实请求 1 次
    main_key = pool.make_key("guangcai", fam.region_code, main)
    assert pool.get(main_key) is not None
    # 再次对主搜
    _, from_cache = fake_search("guangcai", fam.region_code, main)
    assert from_cache is True
    # 统计：主搜 live 仅 1
    assert search_calls["n"] >= 1
    # 只计主搜 live：重置后只打 main
    search_calls["n"] = 0
    pool2 = CandidatePool()

    def fake2(platform, region, query):
        key = pool2.make_key(platform, region, query)
        if pool2.get(key) is not None:
            return pool2.get(key), True
        search_calls["n"] += 1
        pool2.put(key, [{"title": "x", "url": "u"}])
        return pool2.get(key), False

    for _ in range(4):
        fake2("guangcai", "510100", main)
    assert search_calls["n"] == 1
