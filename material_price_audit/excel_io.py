"""Generic inquiry Excel loader / writer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter


def r2(x: float) -> float:
    return round(float(x) + 1e-12, 2)


@dataclass
class LineItem:
    sheet: str
    row: int
    is_rain_layout: bool
    name: str
    spec: str
    unit: Any
    qty: float
    submit: float
    brand: str
    cols: dict = field(default_factory=dict)  # logical -> col index

    @property
    def key(self) -> str:
        return f"{self.sheet}|{self.row}"

    @property
    def text(self) -> str:
        return f"{self.name} {self.spec} {self.brand}"


def _find_header_row(ws, max_scan: int = 8) -> int | None:
    for r in range(1, max_scan + 1):
        for c in range(1, 20):
            v = ws.cell(r, c).value
            if v and any(k in str(v) for k in ("报送不含税单价", "报送单价", "投标单价")):
                return r
    return None


def _map_headers(ws, header_row: int, aliases: dict) -> dict[str, int]:
    """Map logical names to column indexes using aliases from config."""
    found: dict[str, int] = {}
    cells = {}
    for c in range(1, 25):
        v = ws.cell(header_row, c).value
        if v:
            cells[c] = str(v).strip().replace("\n", "")

    def pick(logical: str, defaults: list[str]) -> int | None:
        names = aliases.get(logical) or defaults
        for c, text in cells.items():
            for n in names:
                if n == text or n in text:
                    return c
        return None

    mapping = {
        "name": pick("name_headers", ["材料名称", "名称", "设备名称"]),
        "spec": pick("spec_headers", ["规格、型号", "规格型号", "规格", "型号"]),
        "unit": pick("unit_headers", ["单位"]),
        "qty": pick("qty_headers", ["数量"]),
        "submit": pick("submit_price_headers", ["报送不含税单价", "报送单价", "投标单价"]),
        "audit": pick("audit_price_headers", ["审定不含税单价", "审定单价"]),
        "brand": pick("brand_headers", ["产地、品牌及特殊要求", "品牌"]),
        "sum_submit": None,
        "sum_audit": None,
    }
    # 合价 columns: first after submit often 报送合价, after audit 审定合价
    if mapping["submit"]:
        # next numeric header 合价
        for c, text in cells.items():
            if "合价" in text and c > mapping["submit"]:
                if mapping["sum_submit"] is None:
                    mapping["sum_submit"] = c
                elif mapping["audit"] and c > mapping["audit"] and mapping["sum_audit"] is None:
                    mapping["sum_audit"] = c
    if mapping["audit"] and mapping["sum_audit"] is None:
        for c, text in cells.items():
            if "合价" in text and c > mapping["audit"]:
                mapping["sum_audit"] = c
                break
    return {k: v for k, v in mapping.items() if v is not None}


def load_inquiry(path: Path, excel_cfg: dict | None = None) -> list[LineItem]:
    excel_cfg = excel_cfg or {}
    wb = openpyxl.load_workbook(path, data_only=True)
    items: list[LineItem] = []
    for sn in wb.sheetnames:
        ws = wb[sn]
        # skip utility sheets
        if sn.strip() in ("实抓汇总", "核价汇总", "说明", "README"):
            continue
        hr = _find_header_row(ws)
        if not hr:
            continue
        is_rain = "雨水" in sn or (
            ws.cell(hr, 1).value and "名称" in str(ws.cell(hr, 1).value) and "材料名称" in str(ws.cell(hr, 3).value or "")
        )
        cols = _map_headers(ws, hr, excel_cfg)
        if "submit" not in cols:
            continue
        # rain layout fallback
        if is_rain and "name" not in cols:
            cols.update({"name": 3, "spec": 4, "unit": 5, "qty": 6, "submit": 7, "audit": 9, "sum_audit": 10, "brand": 11})

        for r in range(hr + 1, ws.max_row + 1):
            submit_v = ws.cell(r, cols["submit"]).value
            if submit_v in (None, ""):
                continue
            try:
                submit = float(submit_v)
            except Exception:
                continue
            name_c = cols.get("name", 2)
            name = ws.cell(r, name_c).value
            if name is None and submit == 0:
                continue
            try:
                qty = float(ws.cell(r, cols["qty"]).value or 0) if "qty" in cols else 0
            except Exception:
                qty = 0
            spec = ws.cell(r, cols["spec"]).value if "spec" in cols else ""
            unit = ws.cell(r, cols["unit"]).value if "unit" in cols else ""
            brand = ws.cell(r, cols["brand"]).value if "brand" in cols else ""
            items.append(
                LineItem(
                    sheet=sn.strip(),
                    row=r,
                    is_rain_layout=is_rain,
                    name=str(name or "").replace("\n", " ").strip(),
                    spec=str(spec or "").replace("\n", " ").strip(),
                    unit=unit,
                    qty=qty,
                    submit=submit,
                    brand=str(brand or "").strip(),
                    cols=cols,
                )
            )
    return items


def write_result_workbook(
    source_path: Path,
    output_path: Path,
    items: list[LineItem],
    evidence: dict[str, dict],
    tax_divisor: float = 1.13,
) -> int:
    """Copy source, fill audit columns + evidence, add 实抓汇总 sheet. Returns verified count."""
    wb = openpyxl.load_workbook(source_path)
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF")
    ok_fill = PatternFill("solid", fgColor="C6EFCE")
    wait_fill = PatternFill("solid", fgColor="FFF2CC")
    link_font = Font(color="0563C1", underline="single")

    if "实抓汇总" in wb.sheetnames:
        del wb["实抓汇总"]
    summary = wb.create_sheet("实抓汇总", 0)
    summary["A1"] = "实抓核价汇总（仅 verified 详情/列表证据）"
    summary["A1"].font = Font(bold=True, size=14, color="C00000")
    summary["A2"] = (
        f"审定不含税 = min(挂牌含税÷{tax_divisor}, 报送不含税) | 无证据不填 | 请点开详情URL复核型号"
    )
    headers = [
        "专业", "材料", "规格", "报送不含税", "挂牌含税", "折算不含税", "审定不含税",
        "数量", "审定合价", "平台", "商品标题", "详情URL", "抓取时间", "详情确认",
    ]
    for c, h in enumerate(headers, 1):
        cell = summary.cell(4, c, h)
        cell.fill = header_fill
        cell.font = header_font

    # index items by sheet for column layout
    by_sheet: dict[str, list[LineItem]] = {}
    for it in items:
        by_sheet.setdefault(it.sheet, []).append(it)

    # prepare evidence cols on each sheet
    sheet_meta = {}
    for sn in wb.sheetnames:
        if sn == "实抓汇总":
            continue
        ws = wb[sn]
        hr = _find_header_row(ws)
        if not hr:
            continue
        # find free col after used
        real_max = 1
        for c in range(1, 20):
            if ws.cell(hr, c).value is not None:
                real_max = max(real_max, c)
        evid_c = real_max + 1
        for i, h in enumerate(["证据状态", "详情URL", "挂牌含税", "审定说明"]):
            cell = ws.cell(hr, evid_c + i, h)
            cell.fill = header_fill
            cell.font = header_font
        # resolve audit col
        aliases = {}
        cols = _map_headers(ws, hr, aliases)
        is_rain = "雨水" in sn
        if is_rain:
            audit_c, sum_c = cols.get("audit", 9), cols.get("sum_audit", 10)
        else:
            audit_c = cols.get("audit", 8)
            sum_c = cols.get("sum_audit", 9)
        sheet_meta[sn.strip()] = {
            "hr": hr,
            "audit_c": audit_c,
            "sum_c": sum_c,
            "evid_c": evid_c,
            "ws": ws,
        }

    hit = 0
    srow = 5
    for it in items:
        ev = evidence.get(it.key)
        meta = sheet_meta.get(it.sheet)
        if not meta:
            # try fuzzy
            for k, v in sheet_meta.items():
                if k.strip() == it.sheet or it.sheet in k:
                    meta = v
                    break
        if not meta:
            continue
        ws = meta["ws"]
        r = it.row
        if ev and ev.get("status") == "verified":
            hit += 1
            audit = float(ev["audit"])
            ws.cell(r, meta["audit_c"]).value = audit
            ws.cell(r, meta["audit_c"]).fill = ok_fill
            if it.qty:
                ws.cell(r, meta["sum_c"]).value = r2(audit * it.qty)
            ws.cell(r, meta["evid_c"]).value = "verified"
            cell = ws.cell(r, meta["evid_c"] + 1, "打开详情")
            cell.hyperlink = ev.get("url")
            cell.font = link_font
            ws.cell(r, meta["evid_c"] + 2).value = ev.get("price_tax")
            ws.cell(r, meta["evid_c"] + 3).value = (
                f"含税{ev.get('price_tax')}→不含税{ev.get('price_ex_tax')}；"
                f"审定{audit}≤报送{it.submit}；{(ev.get('title') or '')[:48]}"
            )
            # summary
            vals = [
                it.sheet, it.name[:40], it.spec[:50], it.submit,
                ev.get("price_tax"), ev.get("price_ex_tax"), audit, it.qty,
                r2(audit * it.qty) if it.qty else None,
                ev.get("platform"), (ev.get("title") or "")[:80], ev.get("url"),
                ev.get("captured_at"), "是" if ev.get("detail_confirmed") else "列表价",
            ]
            for c, v in enumerate(vals, 1):
                summary.cell(srow, c, v).fill = ok_fill
            sc = summary.cell(srow, 12)
            if ev.get("url"):
                sc.hyperlink = ev["url"]
                sc.value = "打开详情页"
                sc.font = link_font
            srow += 1
        else:
            # clear audit if we own the column? keep empty for accuracy
            if ws.cell(r, meta["evid_c"]).value in (None, ""):
                ws.cell(r, meta["evid_c"]).value = "pending"
                ws.cell(r, meta["evid_c"]).fill = wait_fill
                ws.cell(r, meta["evid_c"] + 3).value = "无已核验详情价，审定留空"

    summary.cell(srow + 1, 1, f"verified={hit} / items={len(items)}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return hit


def export_rfq(items: list[LineItem], evidence: dict[str, dict], output_path: Path) -> int:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "待询价"
    headers = [
        "询价编号", "专业", "材料名称", "规格型号", "单位", "数量", "品牌",
        "报送不含税单价(上限)", "请报不含税", "请报含税", "商品详情页URL", "供应商", "备注",
    ]
    fill = PatternFill("solid", fgColor="C65911")
    font = Font(bold=True, color="FFFFFF")
    wait = PatternFill("solid", fgColor="FFF2CC")
    for c, h in enumerate(headers, 1):
        cell = ws.cell(1, c, h)
        cell.fill = fill
        cell.font = font
    r = 2
    n = 0
    for it in items:
        ev = evidence.get(it.key)
        if ev and ev.get("status") == "verified":
            continue
        n += 1
        ws.cell(r, 1, f"RFQ-{n:04d}")
        ws.cell(r, 2, it.sheet)
        ws.cell(r, 3, it.name)
        ws.cell(r, 4, it.spec[:150])
        ws.cell(r, 5, it.unit)
        ws.cell(r, 6, it.qty)
        ws.cell(r, 7, it.brand)
        ws.cell(r, 8, it.submit)
        for c in range(9, 13):
            ws.cell(r, c).fill = wait
        ws.cell(r, 13, "报价≤报送不含税；型号须一致；附详情页或盖章件")
        r += 1
    ws.cell(r + 1, 1, f"共 {n} 项待询价")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return n
