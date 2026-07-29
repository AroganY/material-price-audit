"""
规格/名称匹配：人工标准 = 名称对上 + 规格对上才算命中。
不匹配绝不采用；当前平台无结果则换下一平台。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_STOP_CN = frozenset(
    {
        "不含税", "报送", "审定", "规格", "型号", "材料", "名称", "产地", "品牌",
        "设备", "及", "的", "和", "或", "等", "用", "型", "式", "专业", "项目",
        "特殊要求", "特殊", "要求", "国产", "进口",
    }
)


def _norm(s: str) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", "", s)
    s = s.replace("×", "x").replace("Ｘ", "x").replace("＊", "x").replace("*", "x")
    s = s.replace("Φ", "φ").replace("∅", "φ")
    return s.lower()


def extract_tokens(text: str) -> list[str]:
    """Extract model-like and meaningful tokens from name+spec."""
    text = (text or "").replace("\n", " ")
    tokens: list[str] = []
    for m in re.finditer(
        r"(?:DS-|RG-|ST|iDS-|HM-|JB-|MS-|LRS-|GTYQ-|ZN-|WDZN-)[A-Z0-9/\-\.\(\)]+",
        text,
        re.I,
    ):
        tokens.append(m.group(0))
    for m in re.finditer(r"(?:DN|φ|Φ)\s*\d{2,3}(?:\s*[×xX\*]\s*\d+(?:\.\d+)?)?", text, re.I):
        tokens.append(re.sub(r"\s+", "", m.group(0)))
    for m in re.finditer(r"\d+(?:\.\d+)?\s*(?:kW|KW|W|V|mm|MPa|Mpa)", text, re.I):
        tokens.append(re.sub(r"\s+", "", m.group(0)))
    for m in re.finditer(r"[A-Z]{1,6}[-_]?\d{2,}[A-Z0-9\-_/\.]*", text, re.I):
        t = m.group(0)
        if len(t) >= 4:
            tokens.append(t)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,8}", text):
        w = m.group(0)
        if w not in _STOP_CN:
            tokens.append(w)
    seen = set()
    out = []
    for t in tokens:
        k = t.lower()
        if k not in seen and len(t) >= 2:
            seen.add(k)
            out.append(t)
    return out[:28]


def name_core_words(name: str) -> list[str]:
    """核心品名词：去掉过短/停用词，用于「名称必须命中」。"""
    name = (name or "").strip()
    core = name_search_core(name)
    words: list[str] = []
    if core:
        words.append(core)
        # 行业站常混用“线型/线形”。这只是品名同义，不放宽规格。
        if "线型" in core:
            words.append(core.replace("线型", "线形"))
        elif "线形" in core:
            words.append(core.replace("线形", "线型"))
    for m in re.finditer(r"[\u4e00-\u9fff]{2,10}", name):
        w = m.group(0)
        if w in _STOP_CN:
            continue
        if core and core in w:
            continue
        words.append(w)
    # 纯英文材料名才使用英文片段；LED/O1 这类装饰前后缀不能当品名命中。
    if not core:
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9\-]{1,}", name):
            words.append(m.group(0))
    # 去重，优先长词
    seen = set()
    out = []
    for w in sorted(words, key=len, reverse=True):
        k = w.lower()
        if k in seen:
            continue
        # 被更长词包含则跳过（避免「钢管」「镀锌钢管」双计过严）
        if any(k in s for s in seen if len(s) > len(k)):
            continue
        seen.add(k)
        out.append(w)
    # 最多取前 4 个核心词，但至少要能覆盖名称
    return out[:4] if out else ([name[:6]] if name else [])


def name_search_core(name: str) -> str:
    """把 Excel 中的装饰编号去掉，得到人会拿去搜索的核心材料名。"""
    s = (name or "").strip()
    s = re.sub(r"(?i)LED", "", s)
    s = re.sub(r"^[\s\-_]*\d+\s*(?:端口|路)", "", s)
    s = re.sub(r"^成品", "", s)
    s = re.sub(r"(?i)(?:[A-Z]\d+|\d+)$", "", s).strip(" -_（）()")
    chunks = re.findall(r"[\u4e00-\u9fff]{2,12}", s)
    if chunks:
        return max(chunks, key=len)
    return s[:24]


def _num_text(value: str) -> str:
    try:
        n = float(value)
        return str(int(n)) if n.is_integer() else str(n).rstrip("0").rstrip(".")
    except Exception:
        return value


def spec_requirement_groups(spec: str) -> list[dict[str, Any]]:
    """抽取必须逐项核对的规格参数；每组都命中才算严格匹配。"""
    s = (spec or "").strip()
    reqs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, label: str, value: Any, **extra: Any) -> None:
        key = (kind, str(value).lower())
        if key in seen:
            return
        seen.add(key)
        reqs.append({"kind": kind, "label": label, "value": value, **extra})

    for m in re.finditer(
        r"(?i)(?<![A-Za-z0-9])(AC|DC)\s*[-:]?\s*(\d+(?:\.\d+)?)\s*V(?![A-Za-z0-9])",
        s,
    ):
        prefix, value = m.group(1).upper(), _num_text(m.group(2))
        add("voltage", f"电压 {prefix}{value}V", value, prefix=prefix)

    for m in re.finditer(
        r"(?i)(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*W\s*(?:[/／]\s*(m|米))?(?![A-Za-z0-9])",
        s,
    ):
        value = _num_text(m.group(1))
        per_m = bool(m.group(2))
        add("power", f"功率 {value}W{'/m' if per_m else ''}", value, per_m=per_m)

    for m in re.finditer(r"(?i)(?<![A-Za-z0-9])(\d{3,5})\s*K(?![A-Za-z0-9])", s):
        add("kelvin", f"色温 {m.group(1)}K", m.group(1))

    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*°", s):
        value = _num_text(m.group(1))
        add("angle", f"角度 {value}°", value)

    for m in re.finditer(r"(?i)(≥|>=|不低于)?\s*IP\s*(\d{2})", s):
        level = int(m.group(2))
        at_least = bool(m.group(1))
        add("ip", f"防护等级 {'≥' if at_least else ''}IP{level}", level, at_least=at_least)

    if re.search(r"(?i)ON\s*[/／-]\s*OFF|\bONOFF\b", s):
        add("onoff", "控制方式 ON/OFF", "onoff")

    for m in re.finditer(r"(\d+)\s*端口", s):
        add("ports", f"{m.group(1)}端口", int(m.group(1)))
    for m in re.finditer(r"(\d+)\s*通道", s):
        add("channels", f"{m.group(1)}通道", int(m.group(1)))

    for word in (
        "脱机", "联机", "无线", "有线", "防水", "防雨", "户外", "户内",
        "室内", "室外", "明装", "暗装", "阻燃", "耐火", "防爆",
    ):
        if word in s:
            add("text", word, word)

    # 常见型号、口径和尺寸必须核对。
    for m in re.finditer(r"(?:DN|φ|Φ)\s*\d{2,3}(?:\s*[×xX*]\s*\d+(?:\.\d+)?)?", s, re.I):
        add("dimension", f"尺寸 {m.group(0)}", m.group(0))
    for m in re.finditer(
        r"(?<!\d)(\d+(?:\.\d+)?(?:\s*[×xX*]\s*\d+(?:\.\d+)?){1,3})\s*(mm|cm|m)(?![A-Za-z])",
        s,
        re.I,
    ):
        value = f"{m.group(1)}{m.group(2)}"
        add("dimension", f"尺寸 {value}", value)
    for m in re.finditer(r"(?i)(\d+(?:\.\d+)?)\s*(MPa|kPa|Pa)(?![A-Za-z])", s):
        value = f"{m.group(1)}{m.group(2)}"
        add("pressure", f"压力 {value}", value)
    for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9._/-]{3,}", s):
        token = m.group(0).strip("./-")
        if not (re.search(r"[A-Za-z]", token) and re.search(r"\d", token)):
            continue
        if re.match(r"(?i)^(?:AC|DC)\d+(?:\.\d+)?V(?:/|$)", token):
            continue
        if re.fullmatch(r"(?i)(?:AC|DC)?\d+(?:\.\d+)?(?:V|W|W/m|K)", token):
            continue
        if re.fullmatch(r"(?i)IP\d{2}", token):
            continue
        add("model", f"型号 {token}", token)
    return reqs


def _requirement_hit(req: dict[str, Any], blob_raw: str) -> bool:
    kind = req.get("kind")
    value = str(req.get("value"))
    b = _norm(blob_raw).replace("／", "/")
    if kind == "voltage":
        prefix = str(req.get("prefix") or "").lower()
        if f"{prefix}{value}v" in b or f"{value}v{prefix}" in b:
            return True
        if re.search(rf"(?:额定|工作)?电压\(v\)[:：]?{prefix}{re.escape(value)}(?!\d)", b, re.I):
            return True
        target = float(value)
        for lo, hi in re.findall(rf"{prefix}\s*(\d+(?:\.\d+)?)\s*[-~～至]\s*(\d+(?:\.\d+)?)\s*v", blob_raw, re.I):
            if float(lo) <= target <= float(hi):
                return True
        return False
    if kind == "power":
        per_m = bool(req.get("per_m"))
        if per_m:
            patterns = (
                rf"{re.escape(value)}w/(?:m|米)",
                rf"功率\(w/(?:m|米)\)[:：]?{re.escape(value)}(?!\d)",
                rf"功率[:：]?{re.escape(value)}w/(?:m|米)",
            )
        else:
            patterns = (
                rf"{re.escape(value)}w(?![/\w])",
                rf"功率\(w\)[:：]?{re.escape(value)}(?!\d)",
                rf"功率[:：]?{re.escape(value)}w?(?![/\d])",
            )
        if any(re.search(p, b, re.I) for p in patterns):
            return True
        if per_m:
            value_hit = bool(re.search(rf"功率\(w\)[:：]?{re.escape(value)}(?!\d)", b, re.I))
            unit_m = bool(re.search(r"单位[:：]?(?:m|米)(?![a-z])", b, re.I))
            return value_hit and unit_m
        return False
    if kind == "kelvin":
        if re.search(rf"(?:{re.escape(value)}k|色温\(k\)[:：]?{re.escape(value)}(?!\d)|色温[:：]?{re.escape(value)}k?)", b, re.I):
            return True
        target = float(value)
        for field in re.findall(r"色温(?:\(k\))?[:：]?([^|；;]+)", blob_raw, re.I):
            for lo, hi in re.findall(r"(\d{3,5})\s*[-~～至]\s*(\d{3,5})", field):
                if float(lo) <= target <= float(hi):
                    return True
            if any(float(x) == target for x in re.findall(r"\d{3,5}", field)):
                return True
        return False
    if kind == "angle":
        if re.search(rf"(?:{re.escape(value)}°|(?:角度|光束角)(?:\(°\))?[:：]?{re.escape(value)}(?!\d))", b, re.I):
            return True
        target = float(value)
        for field in re.findall(r"(?:角度|光束角)(?:\(°\))?[:：]?([^|；;]+)", blob_raw, re.I):
            for lo, hi in re.findall(r"(\d+(?:\.\d+)?)\s*°?\s*[-~～至]\s*(\d+(?:\.\d+)?)\s*°?", field):
                if float(lo) <= target <= float(hi):
                    return True
            if any(float(x) == target for x in re.findall(r"\d+(?:\.\d+)?", field)):
                return True
        return False
    if kind == "ip":
        levels = [int(x) for x in re.findall(r"(?i)IP\s*(\d{2})", blob_raw)]
        target = int(req.get("value") or 0)
        return any(x >= target if req.get("at_least") else x == target for x in levels)
    if kind == "onoff":
        return bool(re.search(r"(?i)ON\s*[/／-]?\s*OFF|\bONOFF\b|开关控制|开/关", blob_raw))
    if kind == "ports":
        n = re.escape(value)
        return bool(re.search(rf"{n}\s*端口|{n}\s*路(?:独立)?(?:(?:信号|数据){{0,2}})?输出", blob_raw))
    if kind == "channels":
        n = re.escape(value)
        return bool(re.search(rf"{n}\s*通道|DMX\s*{n}(?!\d)", blob_raw, re.I))
    if kind == "text":
        aliases = {
            "脱机": ("脱机", "离线式", "无需联网"),
            "联机": ("联机", "在线式", "需联网"),
            "户外": ("户外", "室外"),
            "户内": ("户内", "室内"),
            "室外": ("室外", "户外"),
            "室内": ("室内", "户内"),
        }
        return any(x in blob_raw for x in aliases.get(value, (value,)))
    if kind == "model":
        return _model_hit(value, blob_raw)
    if kind in ("dimension", "pressure"):
        return _norm(value) in _norm(blob_raw)
    return _hit(blob_raw.lower(), blob_raw, value)


def spec_required_tokens(spec: str, name: str = "") -> list[str]:
    """规格侧硬条件：型号/尺寸/关键参数必须全部出现在页面上。"""
    blob = f"{spec or ''} {name or ''}"
    toks: list[str] = []
    for m in re.finditer(
        r"(?:DS-|RG-|ST|iDS-|HM-|JB-|MS-|LRS-|GTYQ-|ZN-|WDZN-)[A-Z0-9/\-\.\(\)]+",
        blob,
        re.I,
    ):
        toks.append(m.group(0))
    for m in re.finditer(r"(?:DN|φ|Φ)\s*\d{2,3}(?:\s*[×xX\*]\s*\d+(?:\.\d+)?)?", blob, re.I):
        toks.append(re.sub(r"\s+", "", m.group(0)))
    for m in re.finditer(r"\d+(?:\.\d+)?\s*(?:kW|KW|W|V|mm|MPa|Mpa|T|TB)", blob, re.I):
        toks.append(re.sub(r"\s+", "", m.group(0)))
    for m in re.finditer(r"[A-Z]{1,5}\d{3,}[A-Z0-9\-]*", blob, re.I):
        toks.append(m.group(0))
    # 规格里剩余中文材质关键词
    for m in re.finditer(r"[\u4e00-\u9fff]{2,6}", spec or ""):
        w = m.group(0)
        if w not in _STOP_CN and w not in "".join(name_core_words(name)):
            toks.append(w)
    # 若几乎抽不出 token，用规范化后的整段规格（长度合理时）
    seen = set()
    out = []
    for t in toks:
        k = _norm(t)
        if k and k not in seen and len(k) >= 2:
            seen.add(k)
            out.append(t)
    if not out and (spec or "").strip():
        compact = re.sub(r"\s+", "", spec.strip())
        if 2 <= len(compact) <= 40:
            out.append(compact)
    return out[:16]


@dataclass
class MatchResult:
    ok: bool
    score: float
    required_hit: int
    required_total: int
    detail: str
    level: str = "none"  # strict | review | reject | none
    outcome: str = "review"  # accept | review | reject
    missing: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


_OPPOSITE_TEXT: dict[str, tuple[str, ...]] = {
    "脱机": ("联机", "在线式", "需联网"),
    "联机": ("脱机", "离线式", "无需联网"),
    "无线": ("有线",),
    "有线": ("无线",),
    "户外": ("户内", "室内"),
    "户内": ("户外", "室外"),
    "室外": ("户内", "室内"),
    "室内": ("户外", "室外"),
    "防水": ("不防水", "非防水"),
    "防雨": ("不防雨", "非防雨"),
}


def _normalized_model(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (value or "").lower())


def _model_hit(value: str, blob_raw: str) -> bool:
    """型号必须完整一致，ABC-123 不能误命中 ABC-1234。"""
    wanted = _normalized_model(value)
    if not wanted:
        return False
    candidates = re.findall(
        r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9._/\-]{2,}(?![A-Za-z0-9])",
        blob_raw or "",
    )
    return any(_normalized_model(x) == wanted for x in candidates)


def _numeric_values(blob_raw: str, suffix: str) -> list[float]:
    values: list[float] = []
    pat = rf"(?i)(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*{suffix}(?![A-Za-z0-9])"
    for raw in re.findall(pat, blob_raw or ""):
        try:
            values.append(float(raw))
        except Exception:
            pass
    return values


def _requirement_conflicts(req: dict[str, Any], blob_raw: str) -> list[str]:
    """只报告明确的反向证据；未展示不算冲突。"""
    kind = str(req.get("kind") or "")
    value = str(req.get("value") or "")
    label = str(req.get("label") or value)
    blob = blob_raw or ""
    conflicts: list[str] = []

    if kind == "model":
        wanted = _normalized_model(value)
        models = []
        for token in re.findall(
            r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9._/\-]{2,}(?![A-Za-z0-9])",
            blob,
        ):
            normalized = _normalized_model(token)
            if normalized and re.search(r"[a-z]", normalized) and re.search(r"\d", normalized):
                models.append((token, normalized))
        # 只有页面明确标注“型号”或标题中唯一型号时才把不同型号当硬冲突。
        marked = re.findall(
            r"(?i)(?:规格型号|产品型号|型号|model)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/\-]{2,})",
            blob,
        )
        marked_norm = [(_normalized_model(x), x) for x in marked]
        if marked_norm and all(x[0] != wanted for x in marked_norm):
            conflicts.append(f"{label}，页面型号为 {marked_norm[0][1]}")
        return conflicts

    if kind == "text":
        for opposite in _OPPOSITE_TEXT.get(value, ()):
            if opposite in blob and value not in blob:
                conflicts.append(f"{label}，页面明确为“{opposite}”")
        return conflicts

    if kind == "voltage":
        target = float(value)
        prefix = str(req.get("prefix") or "").upper()
        volts = [
            (p.upper(), float(v))
            for p, v in re.findall(r"(?i)(AC|DC)\s*[-:]?\s*(\d+(?:\.\d+)?)\s*V", blob)
        ]
        if volts and not any(p == prefix and v == target for p, v in volts):
            rendered = "/".join(f"{p}{_num_text(str(v))}V" for p, v in volts[:3])
            conflicts.append(f"{label}，页面电压为 {rendered}")
        return conflicts

    unit_suffix = {"power": "W", "kelvin": "K"}.get(kind)
    if unit_suffix:
        target = float(value)
        vals = _numeric_values(blob, unit_suffix)
        if vals and target not in vals:
            conflicts.append(
                f"{label}，页面为 {'/'.join(_num_text(str(x)) + unit_suffix for x in vals[:3])}"
            )
        return conflicts

    if kind == "ip":
        target = int(req.get("value") or 0)
        vals = [int(x) for x in re.findall(r"(?i)IP\s*(\d{2})", blob)]
        valid = any(x >= target if req.get("at_least") else x == target for x in vals)
        if vals and not valid:
            conflicts.append(f"{label}，页面为 IP{vals[0]}")
        return conflicts

    if kind == "angle":
        target = float(value)
        vals = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*°", blob)]
        if vals and target not in vals:
            conflicts.append(f"{label}，页面为 {_num_text(str(vals[0]))}°")
        return conflicts

    if kind == "onoff":
        controls = re.findall(r"(?:控制方式|调光方式)\s*[:：]\s*([^|，,；;\n]{2,30})", blob, re.I)
        if controls and not any(re.search(r"(?i)ON\s*[/／-]?\s*OFF|开关控制|开/关", x) for x in controls):
            conflicts.append(f"{label}，页面控制方式为 {controls[0].strip()}")
        return conflicts

    if kind == "dimension":
        wanted = _norm(value)
        dims = re.findall(
            r"(?i)(?:DN|φ|Φ)\s*\d{2,3}(?:\s*[×xX*]\s*\d+(?:\.\d+)?)?"
            r"|(?<!\d)\d+(?:\.\d+)?(?:\s*[×xX*]\s*\d+(?:\.\d+)?){1,3}\s*(?:mm|cm|m)",
            blob,
        )
        if dims and all(_norm(x) != wanted for x in dims):
            conflicts.append(f"{label}，页面尺寸为 {dims[0]}")
        return conflicts

    if kind == "pressure":
        wanted = _norm(value)
        vals = re.findall(r"(?i)(\d+(?:\.\d+)?\s*(?:MPa|kPa|Pa))(?![A-Za-z])", blob)
        if vals and all(_norm(x) != wanted for x in vals):
            conflicts.append(f"{label}，页面压力为 {vals[0]}")
        return conflicts

    if kind in ("ports", "channels"):
        target = int(req.get("value") or 0)
        word = "端口" if kind == "ports" else "通道"
        vals = [int(x) for x in re.findall(rf"(\d+)\s*{word}", blob)]
        if vals and target not in vals:
            conflicts.append(f"{label}，页面为 {vals[0]}{word}")
        return conflicts
    return conflicts


def normalize_unit(value: Any) -> str:
    raw = _norm(str(value or ""))
    aliases = {
        "米": "m", "延米": "m", "m": "m",
        "平方米": "m2", "㎡": "m2", "m2": "m2",
        "立方米": "m3", "m³": "m3", "m3": "m3",
        "千克": "kg", "公斤": "kg", "kg": "kg",
        "吨": "t", "t": "t",
        "个": "piece", "只": "piece", "件": "piece",
        "台": "set", "套": "set", "组": "set",
    }
    return aliases.get(raw, raw)


def unit_compatibility(requested: Any, offered: Any) -> tuple[bool | None, str]:
    """None=来源未展示单位；False=明确冲突；True=一致/同类。"""
    req = normalize_unit(requested)
    got = normalize_unit(offered)
    if not req or not got:
        return None, "来源未展示计价单位"
    if req == got:
        return True, f"计价单位一致：{offered}"
    return False, f"计价单位冲突：询价表={requested}，来源={offered}"


def _hit(blob_l: str, blob_raw: str, tok: str) -> bool:
    if not tok:
        return False
    t = tok.lower()
    if t in blob_l or tok in blob_raw:
        return True
    tn = _norm(tok)
    bn = _norm(blob_raw)
    if tn and tn in bn:
        return True
    # 型号去横杠
    if re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", tok):
        if re.sub(r"[\s\-/]", "", t) in re.sub(r"[\s\-/]", "", blob_l):
            return True
    return False


def strict_name_spec_match(
    item: Any,
    page_title: str,
    page_text: str = "",
) -> MatchResult:
    """名称命中，并且可抽取的规格硬参数逐项全部命中。"""
    blob_raw = f"{page_title or ''} {page_text or ''}"
    blob_l = blob_raw.lower()
    name = (getattr(item, "name", None) or "").strip()
    spec = (getattr(item, "spec", None) or "").strip()

    if not name:
        return MatchResult(
            False, 0.0, 0, 1, "无材料名称", "reject", "reject",
            conflicts=("无材料名称",),
        )

    name_words = name_core_words(name)
    if not name_words:
        name_words = [name[:6]]
    # 只取最长的 1～2 个核心词（避免「可视对讲分机室内」拆太碎）
    name_words = sorted(name_words, key=len, reverse=True)[:2]

    name_hits = [w for w in name_words if _hit(blob_l, blob_raw, w)]
    name_ok = len(name_hits) >= 1

    if not name_ok:
        return MatchResult(
            False, 0.0, 0, 1, f"名称未命中 need={name_words}", "reject", "reject",
            conflicts=(f"名称未命中：{name_words}",),
        )

    reqs = spec_requirement_groups(spec)
    if not reqs:
        if not spec or spec.strip() in ("/", "-", "无"):
            return MatchResult(
                True, 1.0, 1, 1, f"名称命中 {name_hits}；询价表无规格", "strict", "accept",
                evidence=tuple(name_hits),
            )
        # 无法结构化的规格只接受整段明确出现，宁可留空也不误填。
        if _norm(spec) not in _norm(blob_raw):
            return MatchResult(
                False, 0.5, 1, 2, "名称命中，但规格原文未命中", "review", "review",
                missing=(f"规格原文：{spec}",), evidence=tuple(name_hits),
            )
        return MatchResult(
            True, 1.0, 2, 2, f"名称+规格原文命中 {name_hits}", "strict", "accept",
            evidence=tuple(name_hits) + (spec,),
        )

    hits = [r for r in reqs if _requirement_hit(r, blob_raw)]
    missing = [str(r.get("label") or r.get("value")) for r in reqs if r not in hits]
    conflicts: list[str] = []
    for req in reqs:
        if req in hits:
            continue
        conflicts.extend(_requirement_conflicts(req, blob_raw))
    total = 1 + len(reqs)
    hit_count = 1 + len(hits)
    if conflicts:
        return MatchResult(
            False,
            hit_count / total,
            hit_count,
            total,
            f"规格冲突：{'; '.join(conflicts[:6])}",
            "reject",
            "reject",
            missing=tuple(missing),
            conflicts=tuple(conflicts),
            evidence=tuple(name_hits) + tuple(str(r.get("label") or r.get("value")) for r in hits),
        )
    if missing:
        return MatchResult(
            False,
            hit_count / total,
            hit_count,
            total,
            f"名称命中；规格缺少：{', '.join(missing[:8])}",
            "review",
            "review",
            missing=tuple(missing),
            evidence=tuple(name_hits) + tuple(str(r.get("label") or r.get("value")) for r in hits),
        )
    return MatchResult(
        True,
        1.0,
        total,
        total,
        f"名称+规格全部命中（{len(reqs)}项）",
        "strict",
        "accept",
        evidence=tuple(name_hits) + tuple(str(r.get("label") or r.get("value")) for r in hits),
    )
