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


# 不能当品名命中的参数/标签词（常被 Excel 拼进名称栏）
_NAME_NOISE_CN = frozenset(
    {
        "有效长度",
        "长度",
        "宽度",
        "高度",
        "厚度",
        "口径",
        "截面",
        "外形尺寸",
        "外形",
        "尺寸",
        "规格",
        "型号",
        "材质",
        "壁厚",
        "额定",
        "工作",
        "电源",
        "功率",
        "电压",
        "电流",
        "单位",
        "备注",
        "不含税",
        "单价",
    }
)


def name_core_words(name: str) -> list[str]:
    """核心品名词：去掉过短/停用词，用于「名称必须命中」。"""
    name = (name or "").strip()
    # 先剥掉尺寸/有效长度等，避免污染品名词
    name_for_words = peel_name_dimension_noise(name)
    core = name_search_core(name_for_words or name)
    words: list[str] = []
    if core:
        words.append(core)
        # 型号后残留的「型xxx」→ 同时尝试无「型」前缀
        if core.startswith("型") and len(core) >= 3:
            words.append(core[1:])
        # 行业站常混用“线型/线形”。这只是品名同义，不放宽规格。
        if "线型" in core:
            words.append(core.replace("线型", "线形"))
        elif "线形" in core:
            words.append(core.replace("线形", "线型"))
    for m in re.finditer(r"[\u4e00-\u9fff]{2,10}", name_for_words or name):
        w = m.group(0)
        if w in _STOP_CN or w in _NAME_NOISE_CN:
            continue
        if core and core in w:
            continue
        # 跳过纯参数标签
        if w.endswith(("长度", "宽度", "高度", "厚度", "尺寸")):
            continue
        words.append(w)
        if w.startswith("型") and len(w) >= 3:
            words.append(w[1:])
    # 型号字母数字（如 XZP100）也作为名称侧命中线索
    for m in re.finditer(r"[A-Za-z]{1,6}\d{2,}[A-Za-z0-9\-]*", name_for_words or name, re.I):
        t = m.group(0)
        if len(t) >= 4:
            words.append(t)
    # 纯英文材料名才使用英文片段；LED/O1 这类装饰前后缀不能当品名命中。
    if not core:
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9\-]{1,}", name):
            words.append(m.group(0))
    # 去重，优先长词；噪声词剔除
    seen = set()
    out = []
    for w in sorted(words, key=len, reverse=True):
        k = w.lower()
        if k in seen:
            continue
        if w in _NAME_NOISE_CN or w in _STOP_CN:
            continue
        # 被更长词包含则跳过（避免「钢管」「镀锌钢管」双计过严）
        if any(k in s for s in seen if len(s) > len(k)):
            continue
        seen.add(k)
        out.append(w)
    # 最多取前 4 个核心词，但至少要能覆盖名称
    return out[:4] if out else ([name[:6]] if name else [])


def peel_name_dimension_noise(name: str) -> str:
    """从名称里去掉尺寸、有效长度等参数，只留品名+型号。"""
    s = (name or "").strip()
    if not s:
        return ""
    # 1250X400 / 630x400 / 1000×500×80
    s = re.sub(
        r"\d+(?:\.\d+)?\s*[xX×*]\s*\d+(?:\.\d+)?(?:\s*[xX×*]\s*\d+(?:\.\d+)?){0,2}",
        " ",
        s,
    )
    # 有效长度：1500 / 长度1500mm
    s = re.sub(
        r"(?:有效)?(?:长度|宽度|高度|厚度|深度)\s*[：:为]?\s*\d+(?:\.\d+)?\s*(?:mm|cm|m)?",
        " ",
        s,
        flags=re.I,
    )
    s = re.sub(r"[：:]\s*\d+(?:\.\d+)?\s*(?:mm|cm|m)?(?=\s|$)", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ，,;；/-")


def name_search_core(name: str) -> str:
    """把 Excel 中的装饰编号去掉，得到人会拿去搜索的核心材料名。"""
    s = (name or "").strip()
    s = peel_name_dimension_noise(s)
    s = re.sub(r"(?i)LED", "", s)
    s = re.sub(r"^[\s\-_]*\d+\s*(?:端口|路)", "", s)
    s = re.sub(r"^成品", "", s)
    # 型号前缀 XZP100型 / ABC-12型 粘在中文品名前
    s = re.sub(r"(?i)^[A-Z]{1,8}\d+[A-Z0-9\-]*型?", "", s)
    s = re.sub(r"(?i)(?:[A-Z]\d+|\d+)$", "", s).strip(" -_（）()")
    # 连续中文；去掉以「型」开头的粘连（型号残留）
    chunks = re.findall(r"[\u4e00-\u9fff]{2,12}", s)
    cleaned: list[str] = []
    for c in chunks:
        if c in _NAME_NOISE_CN or c in _STOP_CN:
            continue
        if c.startswith("型") and len(c) >= 3:
            cleaned.append(c[1:])
        cleaned.append(c)
    chunks = cleaned or chunks
    if chunks:
        # 优先真正的品名（含「阀/器/灯/泵/管…」），否则取最长
        productish = [
            c
            for c in chunks
            if re.search(
                r"(阀|器|灯|泵|管|箱|柜|门|窗|板|扇|机|仪|表|盘|架|座|盖|罩|网|消声|开关|插座)",
                c,
            )
        ]
        pool = productish or chunks
        return max(pool, key=len)
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
    # 无单位截面 1250X400 / 630x400（暖通消声器常见）
    for m in re.finditer(
        r"(?<!\d)(\d{2,5})\s*[×xX*]\s*(\d{2,5})(?:\s*[×xX*]\s*(\d{2,5}))?(?!\d)",
        s,
    ):
        parts = [m.group(1), m.group(2)] + ([m.group(3)] if m.group(3) else [])
        value = "x".join(parts)
        add("dimension", f"尺寸 {value}", value)
    # 有效长度：1500
    for m in re.finditer(
        r"有效长度\s*[：:为]?\s*(\d+(?:\.\d+)?)\s*(mm|cm|m)?",
        s,
        re.I,
    ):
        value = _num_text(m.group(1))
        add("length", f"有效长度 {value}", value)
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
        # 国标图集号（15K116-1、02S403）页面上常不写，不能当硬型号
        if re.fullmatch(r"\d{2}[A-Za-z]\d{2,5}(?:-\d+)?", token):
            add("atlas", f"图集 {token}", token)
            continue
        # 纯尺寸串 1250X400 已作为 dimension，勿再当型号
        if re.fullmatch(r"\d+(?:[xX×*]\d+){1,3}", token):
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
    if kind == "atlas":
        # 软条件：有则加分，无也不否决（在 strict 主流程里会跳过缺失）
        return _model_hit(value, blob_raw) or _norm(value) in _norm(blob_raw)
    if kind == "dimension":
        return _dimension_hit(str(req.get("value") or ""), blob_raw)
    if kind == "length":
        v = re.escape(str(req.get("value")))
        return bool(
            re.search(
                rf"(?:有效)?长度\s*[：:为]?\s*{v}|{v}\s*(?:mm|cm|m)?\s*(?:长|有效)",
                blob_raw,
                re.I,
            )
            or re.search(rf"(?<!\d){v}(?!\d)", _norm(blob_raw))
        )
    if kind == "pressure":
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
    llm_invoked: bool = False  # 是否实际调用了大模型
    llm_decision: str = ""  # equivalent | insufficient | conflict | ""


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
    # XZP100型 ↔ XZP100（型号后的「型」不参与比对）
    s = re.sub(r"型$", "", (value or "").strip())
    return re.sub(r"[^0-9a-z]", "", s.lower())


def _extract_page_dims(blob: str) -> list[str]:
    dims = re.findall(
        r"(?i)(?:DN|φ|Φ)\s*\d{2,3}(?:\s*[×xX*]\s*\d+(?:\.\d+)?)?"
        r"|(?<!\d)\d+(?:\.\d+)?(?:\s*[×xX*]\s*\d+(?:\.\d+)?){1,3}\s*(?:mm|cm|m)?"
        r"|(?<!\d)\d{2,5}\s*[×xX*]\s*\d{2,5}(?:\s*[×xX*]\s*\d{2,5})?",
        blob or "",
    )
    return [re.sub(r"\s+", "", x) for x in dims if x]


def _dim_nums(value: str) -> list[str]:
    return re.findall(r"\d+", value or "")


def _dimension_hit(wanted: str, blob_raw: str) -> bool:
    """
    截面/尺寸命中：
      - 1250x400 命中 1250×400 / 1250*400
      - 1250x400 命中 1250×400×1500（第三维常为有效长度）
      - 两侧数字均出现且页面有 x 连接
    """
    wanted_n = _norm(wanted)
    bn = _norm(blob_raw)
    if not wanted_n:
        return False
    if wanted_n in bn:
        return True
    compact = re.sub(r"[^0-9x]", "", wanted_n)
    bcompact = re.sub(r"[^0-9x×\*]", "", bn).replace("×", "x").replace("*", "x")
    if compact and compact in bcompact:
        return True
    wnums = _dim_nums(wanted)
    if len(wnums) < 2:
        return all(n in bn for n in wnums) if wnums else False
    # 页面任一尺寸串包含所需截面数字（顺序一致优先）
    for d in _extract_page_dims(blob_raw):
        pnums = _dim_nums(d)
        if len(pnums) < 2:
            continue
        # 完整相等或页面是 截面+长度
        if pnums[: len(wnums)] == wnums:
            return True
        if set(wnums).issubset(set(pnums)) and len(wnums) == 2:
            # 两维截面在三维串中
            return True
    # 宽松：两个关键数字都在文中出现（防漏抓「宽1250 高400」）
    if all(n in bn for n in wnums[:2]):
        if re.search(r"[x×*]", bn) or ("宽" in (blob_raw or "") and "高" in (blob_raw or "")):
            return True
    return False


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
        if _dimension_hit(value, blob):
            return conflicts
        dims = _extract_page_dims(blob)
        if dims:
            # 页面只有其它截面 → 冲突；无截面数字不算硬冲突
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
        # 件/台/节 在设备类材料上常混用（消声器一节=一台）
        "个": "piece", "只": "piece", "件": "piece",
        "台": "piece", "套": "piece", "组": "piece", "节": "piece",
        "根": "piece", "支": "piece", "块": "piece", "片": "piece",
        "set": "piece", "pcs": "piece", "pc": "piece",
    }
    return aliases.get(raw, raw)


# 明确互斥的单位族（长度 vs 面积 vs 件 等）
_UNIT_FAMILIES = (
    frozenset({"m", "延米"}),
    frozenset({"m2"}),
    frozenset({"m3"}),
    frozenset({"kg", "t"}),
    frozenset({"piece"}),
)


def unit_compatibility(requested: Any, offered: Any) -> tuple[bool | None, str]:
    """None=来源未展示单位；False=明确冲突；True=一致/同类。"""
    req = normalize_unit(requested)
    got = normalize_unit(offered)
    if not req or not got:
        return None, "来源未展示计价单位"
    if req == got:
        return True, f"计价单位一致/同类：询价表={requested}，来源={offered}"
    # 同族放行
    for fam in _UNIT_FAMILIES:
        if req in fam and got in fam:
            return True, f"计价单位同类：询价表={requested}，来源={offered}"
    return False, f"计价单位冲突：询价表={requested}，来源={offered}"


def name_missed(mr: MatchResult) -> bool:
    if any("名称未命中" in str(c) for c in (mr.conflicts or ())):
        return True
    return "名称未命中" in (mr.detail or "")


def decide_quote_bucket(
    mr: MatchResult,
    *,
    unit_ok: bool | None,
    price_ambiguous: bool,
    match_mode: str = "practical",
) -> tuple[str, str, str]:
    """
    决定这条候选如何入账。
    返回 (bucket, outcome, detail)
      bucket: formal | candidate | discard
        formal   — 写入正式合格价
        candidate — 写入「候选待核」（有价有链接，等人拍板）
        discard  — 丢掉
    """
    mode = (match_mode or "practical").strip().lower()
    if mode not in ("strict", "practical", "loose"):
        mode = "practical"
    detail = mr.detail or ""
    missed = name_missed(mr)

    if price_ambiguous:
        if missed and mode != "loose":
            return "discard", "reject", f"{detail}；价格区间不明"
        if mode == "strict":
            return "discard", "review", f"{detail}；价格区间不明"
        return "candidate", "review", f"{detail}；价格区间不明，待人工确认"

    # 严格单位冲突：非件类互斥
    if unit_ok is False:
        if mode == "strict":
            return "discard", "reject", detail
        if missed:
            return "discard", "reject", detail
        return "candidate", "review", f"{detail}；单位待核"

    if mr.ok:
        return "formal", "accept", detail

    if mode == "strict":
        # 原行为：只有 accept 进正式；review 留给上层 LLM；reject 丢
        if mr.outcome == "review":
            return "candidate", "review", detail
        return "discard", mr.outcome or "reject", detail

    # —— practical / loose ——
    if missed and mode == "practical":
        return "discard", "reject", detail

    # loose：名称没中也尽量留候选（标题像）
    if missed and mode == "loose":
        return "candidate", "review", f"{detail}；宽松模式名称弱匹配"

    # 名称已中：规格缺/冲突 → 候选待核（不再整条 no_match）
    if mr.outcome == "review":
        return "candidate", "review", detail
    if mr.outcome == "reject":
        # 规格冲突（截面不同）仍给人看，标红原因
        return "candidate", "review", f"{detail}（规格与询价表不完全一致，请人工确认）"

    return "candidate", "review", detail


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
    # 「型片式消声器」→「片式消声器」
    if tok.startswith("型") and len(tok) >= 3:
        return _hit(blob_l, blob_raw, tok[1:])
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
    # 名称里若还粘着尺寸/有效长度，匹配时并入规格侧，不要求标题写「有效长度」四字
    name_clean = peel_name_dimension_noise(name)
    if name_clean and name_clean != name:
        moved: list[str] = []
        for part in re.findall(
            r"\d+(?:\.\d+)?\s*[xX×*]\s*\d+(?:\.\d+)?(?:\s*[xX×*]\s*\d+(?:\.\d+)?){0,2}"
            r"|(?:有效)?(?:长度|宽度|高度|厚度)\s*[：:为]?\s*\d+(?:\.\d+)?",
            name,
        ):
            p = part.strip()
            if p and p not in moved:
                moved.append(p)
        if moved:
            extra = " ".join(moved)
            if extra not in spec:
                spec = f"{spec} {extra}".strip()
        name = name_clean or name

    if not name:
        return MatchResult(
            False, 0.0, 0, 1, "无材料名称", "reject", "reject",
            conflicts=("无材料名称",),
        )

    name_words = name_core_words(name)
    if not name_words:
        name_words = [name[:6]]
    # 优先中文品名，其次型号；最多 3 个
    name_words = sorted(
        name_words,
        key=lambda w: (0 if re.search(r"[\u4e00-\u9fff]", w) else 1, -len(w)),
    )[:3]

    name_hits = [w for w in name_words if _hit(blob_l, blob_raw, w)]
    # 品名：至少命中 1 个中文核心词，或「型号+品名片段」
    cn_hits = [w for w in name_hits if re.search(r"[\u4e00-\u9fff]", w)]
    model_hits = [w for w in name_hits if re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", w)]
    name_ok = bool(cn_hits) or (bool(model_hits) and any(
        _hit(blob_l, blob_raw, w)
        for w in name_words
        if re.search(r"[\u4e00-\u9fff]{2,}", w)
    ))
    # 再放宽：核心品名被页面「加字」包含（片式消声器 ∈ 片式阻性消声器 已由子串覆盖；
    # 页面 片式消声器 vs 需要 片式消声器 OK）
    if not name_ok and name_words:
        # 任一词命中即可（含型号 XZP100）
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

    # 图集号为软条件：缺失不否决；硬条件（型号/尺寸/功率等）必须齐
    hard_reqs = [r for r in reqs if r.get("kind") != "atlas"]
    soft_reqs = [r for r in reqs if r.get("kind") == "atlas"]
    hard_hits = [r for r in hard_reqs if _requirement_hit(r, blob_raw)]
    soft_hits = [r for r in soft_reqs if _requirement_hit(r, blob_raw)]
    hits = hard_hits + soft_hits
    missing = [
        str(r.get("label") or r.get("value")) for r in hard_reqs if r not in hard_hits
    ]
    soft_missing = [
        str(r.get("label") or r.get("value")) for r in soft_reqs if r not in soft_hits
    ]
    conflicts: list[str] = []
    for req in hard_reqs:
        if req in hard_hits:
            continue
        conflicts.extend(_requirement_conflicts(req, blob_raw))
    total = 1 + max(len(hard_reqs), 1)
    hit_count = 1 + len(hard_hits)
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
            missing=tuple(missing + soft_missing),
            evidence=tuple(name_hits) + tuple(str(r.get("label") or r.get("value")) for r in hits),
        )
    note = f"名称+规格全部命中（硬条件{len(hard_reqs)}项）"
    if soft_hits:
        note += f"；图集命中{len(soft_hits)}"
    elif soft_missing:
        note += f"；图集未在页面展示（不否决）：{','.join(soft_missing[:2])}"
    return MatchResult(
        True,
        1.0,
        total,
        total,
        note,
        "strict",
        "accept",
        evidence=tuple(name_hits) + tuple(str(r.get("label") or r.get("value")) for r in hits),
    )
