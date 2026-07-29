"""Export multi-quote inquiry results (side sheet + optional append cols)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .excel_io import r2
from .models import CanonicalItem, QuoteSet


def audit_from_quotes(
    item: CanonicalItem,
    qset: QuoteSet,
    tax_divisor: float,
    never_exceed: bool,
) -> float | None:
    if not qset.quotes:
        return None
    # prefer lowest ex-tax among quotes
    prices = []
    for q in qset.quotes:
        ex = q.price_ex_tax
        if ex is None and q.price and q.tax_mode == "tax_incl":
            ex = r2(q.price / tax_divisor)
        if ex is not None:
            prices.append(ex)
    if not prices:
        return None
    audit = min(prices)
    if never_exceed and item.submit is not None:
        audit = min(audit, float(item.submit))
    return r2(audit)


def write_quote_result_workbook(
    source_path: Path,
    output_path: Path,
    items: list[CanonicalItem],
    quote_map: dict[str, QuoteSet],
    *,
    tax_divisor: float = 1.13,
    never_exceed: bool = True,
    k: int = 3,
    write_back_mode: str = "side_sheet",
) -> dict[str, int]:
    """
    write_back_mode:
      side_sheet — only 询价比价结果
      append_cols — also append 价1..K on source sheets
      both
    """
    wb = openpyxl.load_workbook(source_path)
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF")
    ok_fill = PatternFill("solid", fgColor="C6EFCE")
    partial_fill = PatternFill("solid", fgColor="FFF2CC")
    bad_fill = PatternFill("solid", fgColor="FCE4EC")
    link_font = Font(color="0563C1", underline="single")

    for dead in ("询价比价结果", "实抓汇总"):
        if dead in wb.sheetnames:
            del wb[dead]

    summary = wb.create_sheet("询价比价结果", 0)
    summary["A1"] = f"询价比价结果（目标 {k} 个同名同规市场价；无合格匹配不编造）"
    summary["A1"].font = Font(bold=True, size=13, color="C00000")
    summary["A2"] = (
        f"不含税粗算=含税÷{tax_divisor}；若有报送价且开启不超报送，参考审定=min(最低不含税,报送)"
    )

    headers = [
        "专业/Sheet",
        "行号",
        "材料名称",
        "规格型号",
        "品牌",
        "单位",
        "数量",
        "报送单价",
        "状态",
        "合格价条数",
        "参考审定不含税",
    ]
    for i in range(1, k + 1):
        headers.extend(
            [
                f"价{i}来源价",
                f"价{i}不含税",
                f"价{i}税口径",
                f"价{i}平台",
                f"价{i}标题",
                f"价{i}厂家",
                f"价{i}联系人",
                f"价{i}电话",
                f"价{i}计价单位",
                f"价{i}起订量",
                f"价{i}价格证据",
                f"价{i}匹配",
                f"价{i}详情链接",
            ]
        )
    headers.extend(
        [
            "待核候选价",
            "候选平台",
            "候选标题",
            "候选厂家",
            "未满足规格",
            "候选证据链接",
        ]
    )
    headers.append("解析备注")

    for c, h in enumerate(headers, 1):
        cell = summary.cell(4, c, h)
        cell.fill = header_fill
        cell.font = header_font

    stats = {
        "full_k": 0,
        "partial": 0,
        "need_review": 0,
        "no_match": 0,
        "other": 0,
        "items": len(items),
    }
    srow = 5
    for it in items:
        qset = quote_map.get(it.id) or quote_map.get(it.key) or QuoteSet(item_id=it.id, status="no_match")
        st = qset.status
        if st == "full_k":
            stats["full_k"] += 1
            fill = ok_fill
            st_label = f"已凑满{k}价"
        elif st == "partial":
            stats["partial"] += 1
            fill = partial_fill
            st_label = f"部分命中({len(qset.quotes)}/{k})"
        elif st == "need_review":
            stats["need_review"] += 1
            fill = partial_fill
            st_label = "找到候选·规格待核"
        elif st in ("no_match", "skipped"):
            stats["no_match"] += 1
            fill = bad_fill if st == "no_match" else partial_fill
            st_label = "没查到" if st == "no_match" else st
        else:
            stats["other"] += 1
            fill = partial_fill
            st_label = st

        audit = audit_from_quotes(it, qset, tax_divisor, never_exceed)
        n_q = len(qset.quotes)
        hint = qset.error or ("" if n_q else "用户勾选平台中均无名称+规格完全匹配")
        row_vals: list[Any] = [
            it.sheet,
            it.row,
            it.name[:60],
            it.spec[:80],
            it.brand[:40],
            it.unit,
            it.qty or None,
            it.submit,
            st_label,
            n_q,
            audit,
        ]
        for i in range(k):
            if i < n_q:
                q = qset.quotes[i]
                ex = q.price_ex_tax
                if ex is None and q.tax_mode == "tax_incl":
                    ex = r2(q.price / tax_divisor)
                detail = q.detail_url or q.url
                row_vals.extend(
                    [
                        q.price,
                        ex,
                        q.tax_mode,
                        q.platform,
                        (q.title or "")[:80],
                        (q.supplier or "")[:40],
                        (q.contact or "")[:20],
                        (q.phone or "")[:20],
                        (q.unit or "")[:12],
                        (q.moq or "")[:20],
                        (q.price_context or q.price_text or "")[:120],
                        f"{q.match_level}:{q.match_score:.2f}",
                        detail,
                    ]
                )
            else:
                row_vals.extend([None] * 13)
        review = qset.review_candidates[0] if qset.review_candidates else None
        if review:
            row_vals.extend(
                [
                    review.price,
                    review.platform,
                    (review.title or "")[:80],
                    (review.supplier or "")[:40],
                    (review.match_detail or "")[:160],
                    review.detail_url or review.url,
                ]
            )
        else:
            row_vals.extend([None] * 6)
        note = "; ".join(it.parse_issues) if it.parse_issues else ""
        if hint:
            note = (note + " | " if note else "") + hint
        row_vals.append(note)

        for c, v in enumerate(row_vals, 1):
            cell = summary.cell(srow, c, v)
            cell.fill = fill
        # hyperlinks：价i详情链接（每价 13 列，链接在第 13 列）
        # 前 11 列是材料信息，从第 12 列开始是价块
        for i in range(k):
            url_col = 12 + i * 13 + 12
            if i < n_q and (qset.quotes[i].detail_url or qset.quotes[i].url):
                cell = summary.cell(srow, url_col)
                link = qset.quotes[i].detail_url or qset.quotes[i].url
                cell.hyperlink = link
                cell.value = "打开详情"
                cell.font = link_font
        if review and (review.detail_url or review.url):
            review_url_col = 12 + k * 13 + 5
            cell = summary.cell(srow, review_url_col)
            cell.hyperlink = review.detail_url or review.url
            cell.value = "打开候选证据"
            cell.font = link_font
        srow += 1

    summary.cell(
        srow + 1,
        1,
        f"full_k={stats['full_k']} partial={stats['partial']} "
        f"need_review={stats['need_review']} no_match={stats['no_match']} items={stats['items']}",
    )

    if write_back_mode in ("append_cols", "both"):
        _append_quote_cols(wb, items, quote_map, k=k, tax_divisor=tax_divisor)

    # widths
    for c in range(1, min(len(headers), 20) + 1):
        summary.column_dimensions[get_column_letter(c)].width = 12
    summary.column_dimensions["C"].width = 28
    summary.column_dimensions["D"].width = 24

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return stats


def _append_quote_cols(
    wb,
    items: list[CanonicalItem],
    quote_map: dict[str, QuoteSet],
    *,
    k: int,
    tax_divisor: float,
) -> None:
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF")
    link_font = Font(color="0563C1", underline="single")
    by_sheet: dict[str, list[CanonicalItem]] = {}
    for it in items:
        by_sheet.setdefault(it.sheet, []).append(it)

    for sn, sheet_items in by_sheet.items():
        if sn not in wb.sheetnames:
            continue
        ws = wb[sn]
        # find header row from first item cols or scan
        hr = None
        for it in sheet_items:
            # use row-1 if looks like header? better: min row - 1
            hr = max(1, min(x.row for x in sheet_items) - 1)
            break
        if not hr:
            continue
        real_max = 1
        for c in range(1, 40):
            if ws.cell(hr, c).value is not None:
                real_max = max(real_max, c)
        base = real_max + 1
        labels = ["询价状态", "参考审定"]
        for i in range(1, k + 1):
            labels.extend([f"询价价{i}", f"询价平台{i}", f"询价链接{i}"])
        for i, lab in enumerate(labels):
            cell = ws.cell(hr, base + i, lab)
            cell.fill = header_fill
            cell.font = header_font

        for it in sheet_items:
            qset = quote_map.get(it.id) or QuoteSet(item_id=it.id)
            audit = audit_from_quotes(it, qset, tax_divisor, True)
            ws.cell(it.row, base, qset.status)
            ws.cell(it.row, base + 1, audit)
            for i in range(k):
                col = base + 2 + i * 3
                if i < len(qset.quotes):
                    q = qset.quotes[i]
                    ws.cell(it.row, col, q.price)
                    ws.cell(it.row, col + 1, q.platform)
                    cell = ws.cell(it.row, col + 2, "打开")
                    if q.url:
                        cell.hyperlink = q.url
                        cell.font = link_font


def export_rfq_from_quotes(
    items: list[CanonicalItem],
    quote_map: dict[str, QuoteSet],
    output_path: Path,
    *,
    k: int = 3,
) -> int:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "待询价"
    headers = ["专业", "行号", "材料名称", "规格型号", "品牌", "单位", "数量", "报送单价", "状态", "已有价条数", "备注"]
    for c, h in enumerate(headers, 1):
        ws.cell(1, c, h)
    n = 0
    r = 2
    for it in items:
        qset = quote_map.get(it.id) or QuoteSet(item_id=it.id, status="no_match")
        if len(qset.quotes) >= k and qset.status == "full_k":
            continue
        n += 1
        ws.cell(r, 1, it.sheet)
        ws.cell(r, 2, it.row)
        ws.cell(r, 3, it.name)
        ws.cell(r, 4, it.spec)
        ws.cell(r, 5, it.brand)
        ws.cell(r, 6, it.unit)
        ws.cell(r, 7, it.qty)
        ws.cell(r, 8, it.submit)
        ws.cell(r, 9, qset.status)
        ws.cell(r, 10, len(qset.quotes))
        note = "; ".join(it.parse_issues)
        if qset.error:
            note = (note + " | " if note else "") + qset.error
        ws.cell(r, 11, note)
        r += 1
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return n
