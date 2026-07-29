"""Load rows via schema → CanonicalItem + search queries + spec tokens."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import openpyxl

from .matching import extract_tokens, name_search_core
from .models import CanonicalItem, SheetSchema, WorkbookSchema


def _cell(ws, r: int, c: int | None) -> Any:
    if not c:
        return None
    return ws.cell(r, c).value


def _s(v: Any) -> str:
    if v is None:
        return ""
    return str(v).replace("\n", " ").replace("\r", " ").strip()


def _f(v: Any) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("￥", "").replace("¥", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def split_name_spec(blob: str) -> tuple[str, str]:
    """If name/spec glued, try to split."""
    blob = _s(blob)
    if not blob:
        return "", ""
    # common separators
    for sep in ("|", "／", "/", "；", ";", "，"):
        if sep in blob:
            a, b = blob.split(sep, 1)
            if len(a.strip()) >= 2 and len(b.strip()) >= 1:
                return a.strip(), b.strip()
    # model-like tail
    m = re.search(
        r"(.+?)\s+((?:DN|φ|Φ)\s*\d{2,3}.*|(?:DS-|RG-|ST|HM-|JB-)[A-Za-z0-9/\-\.]+.*)$",
        blob,
        re.I,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return blob, ""


def peel_dims_into_spec(name: str, spec: str) -> tuple[str, str]:
    """
    名称栏常把截面/有效长度粘在一起：
      XZP100型片式消声器 1250X400 有效长度：1500
    → name=XZP100型片式消声器，spec 追加尺寸参数。
    """
    name, spec = _s(name), _s(spec)
    if not name:
        return name, spec
    moved: list[str] = []
    rest = name
    for pat in (
        r"\d+(?:\.\d+)?\s*[xX×*]\s*\d+(?:\.\d+)?(?:\s*[xX×*]\s*\d+(?:\.\d+)?){0,2}",
        r"(?:有效)?(?:长度|宽度|高度|厚度|深度)\s*[：:为]?\s*\d+(?:\.\d+)?\s*(?:mm|cm|m)?",
    ):
        for m in re.finditer(pat, rest, flags=re.I):
            moved.append(m.group(0).strip())
        rest = re.sub(pat, " ", rest, flags=re.I)
    rest = re.sub(r"\s+", " ", rest).strip(" ，,;；/-")
    if not moved:
        return name, spec
    extra = " ".join(moved)
    new_spec = f"{spec} {extra}".strip() if spec else extra
    return rest or name, new_spec


def _is_measurement_token(t: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?i)(?:(?:AC|DC)?\d+(?:\.\d+)?(?:V|W|W/m|K)|IP\d{2})",
            re.sub(r"\s+", "", t or ""),
        )
    )


def _query_features(
    name: str, spec: str, brand: str, tokens: list[str] | None = None
) -> dict[str, Any]:
    """从名称/规格抽出各站共用的检索特征（型号、口径、关键参数）。"""
    name, spec, brand = _s(name), _s(spec), _s(brand)
    tokens = list(tokens or extract_tokens(f"{name} {spec} {brand}"))
    name_short = re.split(r"[（(【\[]", name)[0].strip() or name
    name_short = name_short[:28]
    name_core = name_search_core(name_short) or name_short
    # 保留名称里的「N端口/N路」身份（name_search_core 会剥掉，电商站反而需要）
    port_in_name = ""
    m_port = re.search(r"(\d+\s*(?:端口|路))", name_short)
    if m_port:
        port_in_name = re.sub(r"\s+", "", m_port.group(1))

    model = next(
        (
            t
            for t in tokens
            if not _is_measurement_token(t)
            and (
                re.match(
                    r"^(?:DS-|RG-|ST|iDS-|HM-|JB-|MS-|LRS-|GTYQ-|ZN-|WDZN-)", t, re.I
                )
                or re.match(r"^[A-Z]{1,5}\d{3,}[A-Z0-9\-]*$", t, re.I)
            )
        ),
        None,
    )
    if not model and spec:
        m = re.search(
            r"((?:DS-|RG-|iDS-|HM-|JB-|MS-|LRS-)[A-Z0-9/\-\.]+|[A-Z]{1,4}\d{3,}[A-Z0-9\-]*)",
            spec,
            re.I,
        )
        if m and not _is_measurement_token(m.group(1)):
            model = m.group(1)

    sizes = [
        t
        for t in tokens
        if t.upper().startswith("DN") or t.startswith("φ") or t.startswith("Φ")
    ]
    if not sizes and spec:
        m = re.search(r"(?:DN|φ|Φ)\s*\d{2,3}", spec, re.I)
        if m:
            sizes.append(re.sub(r"\s+", "", m.group(0)))
    # 截面尺寸 1250X400 / 630x400（消声器、风口等）；优先放进检索词
    blob_ns = f"{name} {spec}"
    for m in re.finditer(
        r"(?<!\d)(\d{2,5})\s*[xX×*]\s*(\d{2,5})(?:\s*[xX×*]\s*(\d{2,5}))?(?!\d)",
        blob_ns,
    ):
        parts = [m.group(1), m.group(2)] + ([m.group(3)] if m.group(3) else [])
        sz = "x".join(parts)
        if sz not in sizes and f"{parts[0]}×{parts[1]}" not in sizes:
            sizes.append(sz)
    # 有效长度单独作为检索辅助（不替代截面）
    m_len = re.search(r"有效长度\s*[：:为]?\s*(\d{3,5})", blob_ns)
    length_hint = m_len.group(1) if m_len else ""

    identity: list[str] = []  # 决定产品身份的词
    for word in (
        "脱机",
        "联机",
        "无线",
        "有线",
        "户外",
        "户内",
        "室外",
        "室内",
        "防水",
        "防雨",
        "防爆",
        "阻燃",
        "耐火",
        "明装",
        "暗装",
    ):
        if word in spec or word in name:
            identity.append(word)
    for pat in (r"\d+\s*端口", r"\d+\s*通道", r"\d+\s*路"):
        m = re.search(pat, f"{name} {spec}", re.I)
        if m:
            identity.append(re.sub(r"\s+", "", m.group(0)))

    electrical: list[str] = []
    for pat in (
        r"(?:AC|DC)\s*\d+(?:\.\d+)?\s*V",
        r"\d+(?:\.\d+)?\s*W\s*(?:[/／]\s*(?:m|米))?",
        r"\d{3,5}\s*K",
        r"IP\s*\d{2}",
    ):
        m = re.search(pat, spec, re.I)
        if m:
            electrical.append(re.sub(r"\s+", "", m.group(0)))

    # 名称里若带 LED/成品 等前缀，造价站常用「LED地埋灯」也能搜到
    name_led = ""
    if re.search(r"(?i)LED", name) and name_core and "LED" not in name_core.upper():
        name_led = f"LED{name_core}"

    brand_hint = brand
    if model:
        mu = model.upper()
        if (mu.startswith("DS-") or mu.startswith("IDS-")) and not brand_hint:
            brand_hint = "海康威视"
        if mu.startswith("RG-") and not brand_hint:
            brand_hint = "锐捷"

    return {
        "name": name,
        "spec": spec,
        "brand": brand,
        "brand_hint": brand_hint,
        "name_short": name_short,
        "name_core": name_core,
        "name_led": name_led,
        "model": model or "",
        "sizes": sizes,
        "length_hint": length_hint,
        "identity": identity,
        "electrical": electrical,
        "port_in_name": port_in_name,
        "tokens": tokens,
    }


def _dedupe_queries(queries: list[str], *, max_n: int = 6, max_len: int = 40) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        q = re.sub(r"\s+", " ", (q or "")).strip()
        if len(q) < 2 or len(q) > max_len:
            continue
        k = q.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(q)
        if len(out) >= max_n:
            break
    return out


# 造价信息站：按「材料品名库」检索，短词+口径/关键规格
_COST_PLATFORM_IDS = frozenset({"guangcai", "lingcai", "huixun", "yize"})
# 电商：按「商品标题」检索，品牌+型号+参数
_ECOM_PLATFORM_IDS = frozenset({"jd", "1688", "taobao", "tmall", "zkh", "suning"})


def build_cost_site_queries(name: str, spec: str, brand: str, tokens: list[str] | None = None) -> list[str]:
    """
    广材 / 领材 / 慧讯 / 易择 搜法（信息价/材料库）：
      1) 核心品名（线型灯、地埋灯、分控器…）
      2) 品名 + 口径/型号
      3) 品名 + 身份词（脱机、8端口…）
      4) 品名 + 电气参数（功率/电压/IP）
      5) 原始短名 / LED+品名
      6) 品牌 + 品名
    不靠超长商品文案；信息站索引的是材料名而非电商标题。
    """
    # 名称粘尺寸时先剥开，保证 sizes/model 能抽到
    name2, spec2 = peel_dims_into_spec(name, spec)
    f = _query_features(name2, spec2, brand, tokens)
    core = f["name_core"]
    short = f["name_short"]
    model = f.get("model") or ""
    sizes = list(f.get("sizes") or [])
    length_hint = str(f.get("length_hint") or "")
    queries: list[str] = []

    # 有型号+截面时：最像人搜「XZP100 1250x400」——必须靠前，否则会点到同型号其它截面
    if model and sizes:
        queries.append(f"{model} {sizes[0]}")
        queries.append(f"{model} {sizes[0].replace('x', '*')}")
    if core and sizes:
        queries.append(f"{core} {sizes[0]}")
    if model and core:
        queries.append(f"{core} {model}")
    if model and sizes and length_hint:
        queries.append(f"{model} {sizes[0]} {length_hint}")

    # 品名库习惯：短品名靠后作兜底（纯「片式消声器」结果太杂）
    if core:
        queries.append(core)
    if f["name_led"] and f["name_led"].lower() != (core or "").lower():
        queries.append(f["name_led"])

    if core and f["identity"]:
        queries.append(f"{core} {' '.join(f['identity'][:3])}")
    if core and f["electrical"]:
        elec = f["electrical"][:2]
        queries.append(f"{core} {' '.join(elec)}")
    if core and f["brand"]:
        queries.append(f"{core} {f['brand']}")
    if model:
        queries.append(model)

    if short and short.lower() not in {q.lower() for q in queries}:
        industrial_whole = bool(
            f["port_in_name"]
            or re.search(r"(?i)(?:DN|φ|Φ)\s*\d", short)
            or (
                len(short) <= 12
                and not re.search(r"(?i)LED|成品|[A-Z]\d+$", short)
            )
        )
        if industrial_whole and not sizes:
            queries.insert(0, short)
        else:
            queries.append(short)

    return _dedupe_queries(queries, max_n=6, max_len=40)


def build_ecommerce_queries(name: str, spec: str, brand: str, tokens: list[str] | None = None) -> list[str]:
    """
    京东 / 1688 搜法（商品标题）：
      1) 品牌 + 型号
      2) 型号
      3) 品牌 + 品名 + 关键参数
      4) 完整短名（8端口分控器 / LED地埋灯）
      5) 品名 + 身份词 + 电气参数
    电商标题靠品牌型号和卖点参数，不靠纯行业库短词。
    """
    f = _query_features(name, spec, brand, tokens)
    core = f["name_core"]
    short = f["name_short"]
    brand_h = f["brand_hint"] or f["brand"]
    queries: list[str] = []

    if brand_h and f["model"]:
        queries.append(f"{brand_h} {f['model']}")
    if f["model"]:
        queries.append(f["model"])
    if brand_h and core:
        tail = " ".join((f["identity"][:1] + f["electrical"][:2])[:3])
        queries.append(f"{brand_h} {core} {tail}".strip())
    if short:
        queries.append(short)
    if f["name_led"] and f["name_led"] != short:
        queries.append(f["name_led"])
    # 紧凑参数串：人在京东常搜「地埋灯 9W 24V IP67」
    if core:
        compact = " ".join(
            x for x in ([core] + f["identity"][:1] + f["electrical"][:3]) if x
        )
        queries.append(compact)
    if core and f["sizes"]:
        queries.append(f"{core} {f['sizes'][0]}")
    # 过宽的光杆品名放最后，仅作兜底
    if core and core.lower() not in {q.lower() for q in queries}:
        queries.append(core)

    return _dedupe_queries(queries, max_n=5, max_len=40)


def build_platform_queries(
    platform_id: str,
    name: str,
    spec: str,
    brand: str,
    tokens: list[str] | None = None,
) -> list[str]:
    """按平台检索习惯生成搜索词；禁止全站同一套 query。"""
    pid = (platform_id or "").strip().lower()
    if pid in _ECOM_PLATFORM_IDS:
        return build_ecommerce_queries(name, spec, brand, tokens)
    if pid in _COST_PLATFORM_IDS:
        return build_cost_site_queries(name, spec, brand, tokens)
    # 未知站：偏保守，用造价站策略（短词）
    return build_cost_site_queries(name, spec, brand, tokens)


def build_queries(name: str, spec: str, brand: str, tokens: list[str]) -> list[str]:
    """
    默认/预览用搜索词（跨站并集，供 Excel 预览与兼容旧逻辑）。
    真正抓取时请用 ``build_platform_queries(platform_id, ...)``。
    """
    # 合并两套策略，保持原始短名靠前
    f = _query_features(name, spec, brand, tokens)
    merged = []
    if f["name_short"]:
        merged.append(f["name_short"])
    merged.extend(build_cost_site_queries(name, spec, brand, tokens))
    merged.extend(build_ecommerce_queries(name, spec, brand, tokens))
    return _dedupe_queries(merged, max_n=6, max_len=40)


# 规格字段标签，不能当 must 命中词（否则「电源:DC24V」会强制要标题含「电源」）
_SPEC_LABEL_STOP = frozenset(
    {
        "电源",
        "功率",
        "色温",
        "角度",
        "防护",
        "防护等级",
        "控制",
        "控制方式",
        "规格",
        "型号",
        "品牌",
        "单位",
        "备注",
        "电压",
        "电流",
        "材质",
        "接口",
        "尺寸",
        "长度",
        "宽度",
        "高度",
        "厚度",
        "产地",
        "要求",
    }
)


def build_must(name: str, spec: str, tokens: list[str]) -> list[str]:
    must: list[str] = []
    for t in tokens:
        if re.match(r"^(?:DS-|RG-|ST|iDS-|HM-|JB-|MS-|LRS-)", t, re.I):
            must.append(t)
            parts = re.split(r"[-/]", t)
            for p in parts:
                if len(p) >= 4:
                    must.append(p)
        if t.upper().startswith("DN") or t.startswith("φ") or t.startswith("Φ"):
            must.append(t)
    # Chinese name chunks — 只用名称，不用规格里的字段标签
    for m in re.finditer(r"[\u4e00-\u9fff]{2,8}", name or ""):
        w = m.group(0)
        if w in ("材料", "设备", "名称", "规格", "型号", "不含税", "单价", "成品"):
            continue
        if w in _SPEC_LABEL_STOP:
            continue
        must.append(w)
        break
    # 规格侧只取身份词，不取「电源/功率」标签
    for word in ("脱机", "联机", "防水", "防爆", "阻燃", "耐火", "明装", "暗装"):
        if word in (spec or ""):
            must.append(word)
            break
    seen = set()
    out = []
    for x in must:
        k = x.lower()
        if k not in seen and len(x) >= 2 and x not in _SPEC_LABEL_STOP:
            seen.add(k)
            out.append(x)
    return out[:12]


def row_to_item(ws, schema: SheetSchema, r: int) -> CanonicalItem | None:
    roles = schema.roles()
    name_c = roles.get("name")
    if not name_c:
        return None
    name = _s(_cell(ws, r, name_c))
    spec = _s(_cell(ws, r, roles.get("spec")))
    brand = _s(_cell(ws, r, roles.get("brand")))
    unit = _cell(ws, r, roles.get("unit"))
    qty = _f(_cell(ws, r, roles.get("qty"))) or 0.0
    submit = _f(_cell(ws, r, roles.get("submit_price")))
    remark = _s(_cell(ws, r, roles.get("remark")))

    # skip empty / section headers
    if not name and submit is None:
        return None
    if not name and spec:
        name, spec2 = split_name_spec(spec)
        if spec2:
            spec = spec2
    if name and not spec:
        n2, s2 = split_name_spec(name)
        if s2:
            name, spec = n2, s2
    # 名称粘尺寸/有效长度 → 挪到规格（消声器、风管附件极常见）
    name, spec = peel_dims_into_spec(name, spec)
    if not name:
        return None
    # skip pure section titles (no digits, very short, no unit/qty/price)
    if (
        submit is None
        and qty == 0
        and not spec
        and len(name) <= 6
        and not re.search(r"\d", name)
    ):
        # still allow if looks like material
        if name.endswith(("工程", "分部", "合计", "小计", "说明", "专业")):
            return None

    issues: list[str] = []
    conf = 1.0
    status = "ok"
    if not spec:
        issues.append("无规格")
        conf -= 0.25
        status = "weak"
    if submit is None:
        issues.append("无报送单价")
        conf -= 0.05

    tokens = extract_tokens(f"{name} {spec} {brand}")
    queries = build_queries(name, spec, brand, tokens)
    must = build_must(name, spec, tokens)
    if not queries:
        issues.append("无法生成搜索词")
        status = "fail"
        conf = min(conf, 0.2)

    item_id = f"{schema.sheet}|{r}"
    return CanonicalItem(
        id=item_id,
        sheet=schema.sheet,
        row=r,
        name=name,
        spec=spec,
        brand=brand,
        unit=unit if unit is not None else "",
        qty=qty,
        submit=submit,
        remark=remark,
        cols=roles,
        spec_tokens=tokens,
        search_queries=queries,
        must_match=must,
        parse_confidence=max(0.0, conf),
        parse_status=status,
        parse_issues=issues,
    )


def load_canonical_items(
    path: Path,
    schema: WorkbookSchema,
    *,
    max_row_scan: int = 5000,
) -> list[CanonicalItem]:
    wb = openpyxl.load_workbook(path, data_only=True)
    items: list[CanonicalItem] = []
    try:
        by_name = {s.sheet: s for s in schema.sheets}
        for sn in wb.sheetnames:
            sch = by_name.get(sn.strip()) or by_name.get(sn)
            if not sch:
                continue
            ws = wb[sn]
            # 写回原表必须用真实 sheet 名（可能带尾部空格）
            real_sheet = sn
            start = sch.data_start_row
            end = min(ws.max_row or start, start + max_row_scan - 1)
            for r in range(start, end + 1):
                it = row_to_item(ws, sch, r)
                if it and it.parse_status != "fail":
                    it.sheet = real_sheet
                    it.id = f"{real_sheet}|{r}"
                    items.append(it)
    finally:
        wb.close()
    return items


def save_canonical_json(items: list[CanonicalItem], path: Path) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([i.to_dict() for i in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
