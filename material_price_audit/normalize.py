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


def build_queries(name: str, spec: str, brand: str, tokens: list[str]) -> list[str]:
    """
    人肉 RPA 怎么搜，这里就怎么生成——原始完整名称优先，再逐步放宽。
    顺序：
      1) Excel 原始完整名称
      2) 去装饰前后缀后的核心品名
      3) 核心品名 + 关键规格

    原始名称必须排第一，不能被末尾的 ``out[:3]`` 截掉。例如人工能搜到的
    “8端口分控器”，不能只实际搜索“分控器”。
    """
    name, spec, brand = _s(name), _s(spec), _s(brand)
    # 名称截短：去掉括号说明
    name_short = re.split(r"[（(【\[]", name)[0].strip() or name
    name_short = name_short[:24]
    name_core = name_search_core(name_short) or name_short

    def is_measurement(t: str) -> bool:
        return bool(
            re.fullmatch(
                r"(?i)(?:(?:AC|DC)?\d+(?:\.\d+)?(?:V|W|W/m|K)|IP\d{2})",
                re.sub(r"\s+", "", t or ""),
            )
        )

    model = next(
        (
            t
            for t in tokens
            if not is_measurement(t)
            and (
                re.match(r"^(?:DS-|RG-|ST|iDS-|HM-|JB-|MS-|LRS-|GTYQ-|ZN-|WDZN-)", t, re.I)
                or re.match(r"^[A-Z]{1,5}\d{3,}[A-Z0-9\-]*$", t, re.I)
            )
        ),
        None,
    )
    # 规格里再挖一次型号
    if not model and spec:
        m = re.search(
            r"((?:DS-|RG-|iDS-|HM-|JB-|MS-|LRS-)[A-Z0-9/\-\.]+|[A-Z]{1,4}\d{3,}[A-Z0-9\-]*)",
            spec,
            re.I,
        )
        if m:
            found = m.group(1)
            if not is_measurement(found):
                model = found

    sizes = [
        t
        for t in tokens
        if t.upper().startswith("DN") or t.startswith("φ") or t.startswith("Φ")
    ]
    if not sizes and spec:
        m = re.search(r"(?:DN|φ|Φ)\s*\d{2,3}", spec, re.I)
        if m:
            sizes.append(re.sub(r"\s+", "", m.group(0)))

    queries: list[str] = []
    if name_short:
        queries.append(name_short)
    if name_core and name_core.lower() != name_short.lower():
        queries.append(name_core)

    # 精准搜索优先放“会改变产品身份”的字段，再放普通数值。
    # 例如“脱机 8端口”比只带“512通道”更能排除错误控制器。
    search_params: list[str] = []
    for word in (
        "脱机", "联机", "无线", "有线", "户外", "户内", "室外", "室内",
        "防水", "防雨", "防爆", "阻燃", "耐火", "明装", "暗装",
    ):
        if word in spec:
            search_params.append(word)
    if "灯" not in name_core:
        for pat in (r"\d+\s*端口", r"\d+\s*通道"):
            m = re.search(pat, spec, re.I)
            if m:
                search_params.append(re.sub(r"\s+", "", m.group(0)))
    pats = (
        r"\d+(?:\.\d+)?\s*W\s*(?:[/／]\s*(?:m|米))?",
        r"\d{3,5}\s*K",
        r"(?:AC|DC)\s*\d+(?:\.\d+)?\s*V",
        r"IP\s*\d{2}",
    )
    for pat in pats:
        m = re.search(pat, spec, re.I)
        if m:
            search_params.append(re.sub(r"\s+", "", m.group(0)))
    if name_core and model:
        queries.append(f"{name_core} {model}")
    if name_core and search_params:
        queries.append(f"{name_core} {' '.join(search_params[:3])}")
    if name_core and len(search_params) > 3:
        queries.append(f"{name_core} {' '.join(search_params[3:6])}")
    if name_core and brand:
        queries.append(f"{name_core} {brand}")
    if brand and model:
        queries.append(f"{brand} {model}")
    if model:
        # 型号前补常见品牌
        if model.upper().startswith("DS-") or model.upper().startswith("IDS-"):
            queries.append(f"海康威视 {model}")
        if model.upper().startswith("RG-"):
            queries.append(f"锐捷 {model}")
        queries.append(model)
    if name_core and sizes:
        queries.append(f"{name_core} {sizes[0]}")

    seen = set()
    out = []
    for q in queries:
        q = re.sub(r"\s+", " ", q).strip()
        if len(q) < 2 or len(q) > 40:  # 超过 40 字的词人也不这么搜
            continue
        k = q.lower()
        if k not in seen:
            seen.add(k)
            out.append(q)
    return out[:6]


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
    # Chinese name chunks
    for m in re.finditer(r"[\u4e00-\u9fff]{2,6}", name or ""):
        w = m.group(0)
        if w not in ("材料", "设备", "名称", "规格", "型号", "不含税", "单价"):
            must.append(w)
            break
    for m in re.finditer(r"[\u4e00-\u9fff]{2,6}", spec or ""):
        must.append(m.group(0))
        break
    seen = set()
    out = []
    for x in must:
        k = x.lower()
        if k not in seen and len(x) >= 2:
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
