"""
Table structure understanding:
  L0 rule header mapping (no fixed single header names)
  L1 optional LLM schema mapping
  Cache by fingerprint for repeat templates
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import openpyxl

from .models import ColumnMap, SheetSchema, WorkbookSchema
from .settings_store import UserSettings

# role -> many possible header fragments (not exclusive fixed headers)
ROLE_HINTS: dict[str, list[str]] = {
    "name": [
        "材料名称", "设备名称", "材料设备名称", "材料及设备名称", "名称", "品名",
        "项目名称", "材料设备", "物资名称", "商品名称", "清单名称", "工程材料",
    ],
    "spec": [
        "规格、型号", "规格型号", "规格及型号", "规格", "型号", "技术参数",
        "规格参数", "特征描述", "项目特征", "材质规格",
    ],
    "brand": [
        "产地、品牌及特殊要求", "产地品牌及特殊要求", "产地、品牌", "品牌产地",
        "品牌", "产地", "厂家", "生产厂家", "厂商", "特殊要求",
    ],
    "unit": ["计量单位", "单位", "单位名称"],
    "qty": ["工程量", "数量", "清单工程量", "申报数量", "合同数量"],
    "submit_price": [
        "报送不含税单价", "报送单价", "投标单价", "承包人报价", "承包人申报单价",
        "报审单价", "申报单价", "报出单价", "不含税单价", "含税单价", "单价",
        "综合单价", "材料单价", "设备单价", "市场单价", "信息价",
    ],
    "audit_price": [
        "审定不含税单价", "审定单价", "核定单价", "审核单价", "批准单价", "认定单价",
    ],
    "sum_price": ["合价", "报送合价", "审定合价", "金额", "总价"],
    "remark": ["备注", "说明", "附注", "注释"],
}

# higher = more specific when multiple match
ROLE_PRIORITY = {
    "submit_price": 50,
    "audit_price": 45,
    "name": 40,
    "spec": 38,
    "brand": 30,
    "qty": 28,
    "unit": 26,
    "sum_price": 20,
    "remark": 10,
    "ignore": 0,
    "unknown": 0,
}


def _cell_text(v: Any) -> str:
    if v is None:
        return ""
    return str(v).replace("\n", "").replace("\r", "").strip()


def fingerprint_workbook(path: Path, max_rows: int = 12, max_cols: int = 22) -> str:
    """Structure fingerprint from header-ish rows (stable across data edits)."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts: list[str] = []
    try:
        for sn in wb.sheetnames[:12]:
            if sn.strip() in ("实抓汇总", "询价比价结果", "核价汇总", "说明", "README"):
                continue
            ws = wb[sn]
            parts.append(f"SHEET:{sn}")
            for i, row in enumerate(ws.iter_rows(max_row=max_rows, max_col=max_cols, values_only=True)):
                cells = [_cell_text(c) for c in row]
                if any(cells):
                    parts.append(f"R{i+1}:" + "|".join(cells))
    finally:
        wb.close()
    raw = "\n".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def cache_dir(root: Path) -> Path:
    d = root / "data" / "mapping-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_schema_cache(root: Path, fp: str) -> WorkbookSchema | None:
    p = cache_dir(root) / f"{fp}.json"
    if not p.exists():
        return None
    try:
        return WorkbookSchema.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def save_schema_cache(root: Path, schema: WorkbookSchema) -> Path:
    p = cache_dir(root) / f"{schema.file_fingerprint}.json"
    schema.created_at = schema.created_at or datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(schema.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _score_header_text(text: str) -> tuple[str | None, float]:
    """Return best role + confidence for a header cell."""
    t = _cell_text(text)
    if not t or len(t) > 40:
        return None, 0.0
    best_role = None
    best_score = 0.0
    for role, hints in ROLE_HINTS.items():
        for h in hints:
            if h == t:
                sc = 1.0
            elif h in t or t in h:
                sc = 0.85 if len(h) >= 2 else 0.5
            else:
                continue
            # penalize bare「单价」if longer more specific already
            if h == "单价" and t != "单价":
                sc = 0.55
            if h == "名称" and t != "名称" and "材料" not in t and "设备" not in t and "项目" not in t:
                sc = min(sc, 0.7)
            pri = ROLE_PRIORITY.get(role, 0) / 100.0
            sc = sc + pri * 0.05
            if sc > best_score:
                best_score = sc
                best_role = role
    return best_role, min(1.0, best_score)


def _row_header_score(cells: list[str]) -> float:
    score = 0.0
    roles = set()
    for t in cells:
        role, sc = _score_header_text(t)
        if role and sc >= 0.55:
            score += sc
            roles.add(role)
    # bonus if looks like a real header row
    if "name" in roles:
        score += 1.5
    if "spec" in roles:
        score += 0.8
    if "submit_price" in roles or "unit" in roles:
        score += 0.8
    return score


def map_sheet_by_rules(ws, sheet_name: str, max_scan: int = 15, max_cols: int = 24) -> SheetSchema | None:
    grid: list[list[str]] = []
    for r in range(1, max_scan + 1):
        row = []
        for c in range(1, max_cols + 1):
            row.append(_cell_text(ws.cell(r, c).value))
        grid.append(row)

    best_r = None
    best_score = 0.0
    for i, row in enumerate(grid):
        sc = _row_header_score(row)
        if sc > best_score:
            best_score = sc
            best_r = i + 1  # 1-based

    if best_r is None or best_score < 1.2:
        return None

    header = grid[best_r - 1]
    columns: list[ColumnMap] = []
    used_roles: set[str] = set()
    # rank candidates then assign unique roles
    cands: list[tuple[float, int, str, str]] = []
    for c, text in enumerate(header, 1):
        if not text:
            continue
        role, conf = _score_header_text(text)
        if role and conf >= 0.55:
            cands.append((conf + ROLE_PRIORITY.get(role, 0) / 1000.0, c, role, text))
        else:
            columns.append(ColumnMap(col=c, role="unknown", header_text=text, confidence=0.3))

    cands.sort(reverse=True)
    assigned_cols: set[int] = set()
    for conf, c, role, text in cands:
        if c in assigned_cols:
            continue
        # allow only one of each primary role (except sum_price can be multi — take first)
        if role in used_roles and role != "sum_price":
            # secondary: if name already taken and text looks like remark
            columns.append(ColumnMap(col=c, role="ignore", header_text=text, confidence=conf * 0.5))
            assigned_cols.add(c)
            continue
        used_roles.add(role)
        assigned_cols.add(c)
        columns.append(ColumnMap(col=c, role=role, header_text=text, confidence=conf))

    columns.sort(key=lambda x: x.col)
    roles = {c.role for c in columns}
    if "name" not in roles:
        # try: first long text column as name
        for c, text in enumerate(header, 1):
            if text and c not in assigned_cols:
                columns.append(ColumnMap(col=c, role="name", header_text=text, confidence=0.4))
                roles.add("name")
                break
        if "name" not in roles:
            # column 1 or 2 often name
            for guess in (2, 1, 3):
                if guess <= max_cols:
                    columns.append(ColumnMap(col=guess, role="name", header_text=header[guess - 1], confidence=0.35))
                    break

    conf = min(1.0, best_score / 6.0)
    return SheetSchema(
        sheet=sheet_name.strip(),
        header_row=best_r,
        data_start_row=best_r + 1,
        columns=columns,
        layout_notes="rule-mapped",
        source="rule",
        confidence=conf,
    )


def _sheet_preview(ws, max_rows: int = 18, max_cols: int = 18) -> list[list[str]]:
    out = []
    for r in range(1, max_rows + 1):
        row = [_cell_text(ws.cell(r, c).value)[:40] for c in range(1, max_cols + 1)]
        if any(row):
            out.append(row)
    return out


def _llm_chat_json(settings: UserSettings, system: str, user: str) -> dict | None:
    if not settings.llm_enabled:
        return None
    key = os.environ.get(settings.llm_api_key_env) or os.environ.get("MATERIAL_PRICE_AUDIT_LLM_KEY")
    # also common alternatives
    if not key:
        for env in ("OPENAI_API_KEY", "SPACEXAI_API_KEY", "LLM_API_KEY"):
            key = os.environ.get(env)
            if key:
                break
    if not key:
        return None
    base = (settings.llm_api_base or os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/chat/completions"
    body = {
        "model": settings.llm_model or "gpt-4o-mini",
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        content = raw["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                (x.get("text") if isinstance(x, dict) else str(x)) for x in content
            )
        # strip markdown fences if any
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        return json.loads(content)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError, TimeoutError) as e:
        print(f"[schema] LLM 调用失败，回退规则: {type(e).__name__}: {e}")
        return None
    except Exception as e:
        print(f"[schema] LLM 异常，回退规则: {e}")
        return None


def map_sheet_by_llm(ws, sheet_name: str, settings: UserSettings) -> SheetSchema | None:
    if "schema" not in (settings.llm_use_for or ["schema"]):
        return None
    preview = _sheet_preview(ws)
    if not preview:
        return None
    system = (
        "你是工程造价询价表结构分析器。根据 Excel 网格识别表头行与列语义。"
        "只输出 JSON，不要定价。role 只能是: "
        "name,spec,brand,unit,qty,submit_price,audit_price,sum_price,remark,ignore,unknown。"
        "JSON 字段: header_row(int), data_start_row(int), columns:[{col,role,header_text,confidence}], "
        "layout_notes(string), confidence(0-1)。col 从 1 开始。"
    )
    user = json.dumps(
        {"sheet": sheet_name, "grid_rows": preview},
        ensure_ascii=False,
    )
    data = _llm_chat_json(settings, system, user)
    if not data:
        return None
    cols = []
    for x in data.get("columns") or []:
        try:
            role = str(x.get("role") or "unknown")
            if role not in ROLE_HINTS and role not in ("ignore", "unknown"):
                role = "unknown"
            cols.append(
                ColumnMap(
                    col=int(x["col"]),
                    role=role,
                    header_text=str(x.get("header_text") or ""),
                    confidence=float(x.get("confidence") or 0.7),
                )
            )
        except Exception:
            continue
    if not cols:
        return None
    hr = int(data.get("header_row") or 1)
    return SheetSchema(
        sheet=sheet_name.strip(),
        header_row=hr,
        data_start_row=int(data.get("data_start_row") or (hr + 1)),
        columns=cols,
        layout_notes=str(data.get("layout_notes") or "llm"),
        source="llm",
        confidence=float(data.get("confidence") or 0.75),
    )


def detect_workbook_schema(
    path: Path,
    root: Path,
    settings: UserSettings | None = None,
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> WorkbookSchema:
    settings = settings or UserSettings()
    fp = fingerprint_workbook(path)
    if use_cache and not force_refresh:
        cached = load_schema_cache(root, fp)
        if cached and cached.sheets:
            print(f"[schema] 命中映射缓存 fingerprint={fp}")
            return cached

    wb = openpyxl.load_workbook(path, data_only=True)
    sheets: list[SheetSchema] = []
    skip = {"实抓汇总", "询价比价结果", "核价汇总", "说明", "README", "待询价"}
    try:
        for sn in wb.sheetnames:
            if sn.strip() in skip:
                continue
            ws = wb[sn]
            schema = None
            # L0 rules first
            schema = map_sheet_by_rules(ws, sn)
            # L1 LLM if weak / no name
            need_llm = (
                schema is None
                or schema.confidence < 0.55
                or schema.role_col("name") is None
            )
            if need_llm and settings.llm_enabled:
                llm_schema = map_sheet_by_llm(ws, sn, settings)
                if llm_schema and (
                    schema is None
                    or llm_schema.confidence >= schema.confidence
                    or (schema.role_col("name") is None and llm_schema.role_col("name"))
                ):
                    schema = llm_schema
            if schema and schema.role_col("name"):
                sheets.append(schema)
                print(
                    f"[schema] sheet={sn!r} source={schema.source} "
                    f"header_row={schema.header_row} conf={schema.confidence:.2f} "
                    f"roles={list(schema.roles().keys())}"
                )
            else:
                print(f"[schema] sheet={sn!r} 无法识别有效材料列，跳过")
    finally:
        wb.close()

    result = WorkbookSchema(file_fingerprint=fp, sheets=sheets, created_at=datetime.now().isoformat(timespec="seconds"))
    if sheets:
        save_schema_cache(root, result)
    return result


def dump_schema_preview(schema: WorkbookSchema, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
