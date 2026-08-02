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

# 常见城市（无「市」后缀也要剥）：搜索/品名里的地名噪声
_COMMON_CITIES = (
    "北京|上海|天津|重庆|成都|广州|深圳|杭州|武汉|西安|南京|苏州|青岛|大连|"
    "厦门|福州|长沙|郑州|济南|合肥|昆明|贵阳|南宁|海口|哈尔滨|长春|沈阳|"
    "石家庄|太原|南昌|兰州|宁波|无锡|东莞|佛山|珠海|中山|惠州|泉州|温州|"
    "嘉兴|金华|绍兴|台州|嘉峪关|乌鲁木齐|呼和浩特|银川|西宁|拉萨"
)


def collapse_cjk_spaces(text: str) -> str:
    """
    折叠中文内部空格：Excel/OCR 常见「薄 壁 不 锈 钢 管」→「薄壁不锈钢管」。
    保留「品名 DN100」这类品名与规格之间的空格。
    """
    s = (text or "").replace("\u3000", " ").replace("\n", " ").replace("\r", " ")
    # 中文与中文之间的空白全部去掉
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
    # 中文与数字/字母紧贴处去空白：「管 DN」保留；「管 100」→「管100」过激，
    # 仅去掉中文与紧随其后的中文之间；型号字母前保留一个空格即可
    s = re.sub(r"\s+", " ", s).strip()
    return s


def strip_geo_noise(text: str) -> str:
    """
    从品名/检索词中剥离地名与信息价字样。
    禁止「成都 薄壁不锈钢管」「成都市信息价」进搜索框。
    """
    s = collapse_cjk_spaces(text or "")
    if not s:
        return ""
    # 省 / 自治区
    s = re.sub(
        r"[\u4e00-\u9fff]{1,8}(?:省|自治区|特别行政区)",
        " ",
        s,
    )
    # 市/州/盟/地区/区/县（避免误伤「市场」：市后不得接「场」）
    s = re.sub(
        r"[\u4e00-\u9fff]{2,8}(?:市(?!场)|州|盟|地区|(?<!市)区(?!域)|县)",
        " ",
        s,
    )
    # 常见城市名（可无「市」）
    s = re.sub(rf"(?:{_COMMON_CITIES})(?:市)?", " ", s)
    # 价源字样
    s = re.sub(
        r"(?:全国|本市|当地|当地价|信息价|市场价|指导价|除税价|含税价|除税|含税)",
        " ",
        s,
    )
    s = re.sub(r"\s+", " ", s).strip(" ，,;；/-")
    return s


def normalize_material_name(name: str) -> str:
    """入库/匹配/搜前统一：折中文空格 + 去地名噪声 + 剥尺寸噪声。"""
    s = strip_geo_noise(collapse_cjk_spaces(name or ""))
    s = peel_name_dimension_noise(s) or s
    return re.sub(r"\s+", " ", s).strip()


def _norm(s: str) -> str:
    s = collapse_cjk_spaces(s or "")
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


def _cn_chars(s: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fff]", s or "")


def _strip_name_decorations(s: str) -> str:
    """去掉 LED/成品/型 等装饰前缀，留下可比的核心品名。"""
    t = peel_name_dimension_noise(s or "") or (s or "")
    t = re.sub(r"(?i)^(?:LED|成品|新型|进口|国产)+", "", t)
    t = re.sub(r"^(?:型)", "", t)
    return t.strip()


def soft_product_name_equivalent(query_name: str, page_title: str, page_text: str = "") -> bool:
    """
    通用品名软等价（**不写死同义词表**）：
      1) 去装饰后子串包含（镀锌管 ∈ 热镀锌钢管）
      2) 中文字符多重集相同（地埋灯 ↔ 埋地灯，同字不同序）
      3) 页面标题/正文中 2～8 字中文片段与询价品名字袋相同或互相包含
    不碰规格数字；仅解决「同物异名/异序」名称问题。
    """
    q0 = _strip_name_decorations(query_name)
    t0 = _strip_name_decorations(page_title)
    if not q0:
        return False
    qn, tn = _norm(q0), _norm(t0)
    if qn and (qn in tn or tn in qn):
        return True
    qc, tc = _cn_chars(q0), _cn_chars(t0)
    if len(qc) >= 2 and sorted(qc) == sorted(tc):
        return True
    # 从标题+正文抽中文片段，与询价品名字袋比对
    pool = f"{page_title or ''} {page_text or ''}"
    qbag = "".join(sorted(qc)) if qc else ""
    for m in re.finditer(r"[\u4e00-\u9fff]{2,8}", pool):
        frag = m.group(0)
        if frag in _STOP_CN or frag in _NAME_NOISE_CN:
            continue
        fc = _cn_chars(frag)
        if len(fc) < 2:
            continue
        if qbag and "".join(sorted(fc)) == qbag:
            return True
        # 互相包含（去一字装饰：钢管/管）
        if len(qc) >= 2 and len(fc) >= 2:
            if set(qc).issubset(set(fc)) or set(fc).issubset(set(qc)):
                # 防止过宽：长度差太大不认（冷却塔 vs 塔）
                if abs(len(qc) - len(fc)) <= 2:
                    return True
    return False


def name_token_matches_blob(word: str, blob_l: str, blob_raw: str) -> bool:
    """品名词命中：子串 + 字序无关/软包含（通用，非词表）。"""
    if _hit(blob_l, blob_raw, word):
        return True
    # 字袋相同：word 与 blob 中等长片段
    wc = _cn_chars(word)
    if len(wc) >= 2:
        wbag = "".join(sorted(wc))
        for m in re.finditer(r"[\u4e00-\u9fff]{2,8}", blob_raw or ""):
            frag = m.group(0)
            if "".join(sorted(_cn_chars(frag))) == wbag:
                return True
    # 线型/线形 形近（同一字形族，仍属规则变换不是行业词表）
    if "线型" in word and _hit(blob_l, blob_raw, word.replace("线型", "线形")):
        return True
    if "线形" in word and _hit(blob_l, blob_raw, word.replace("线形", "线型")):
        return True
    return False


def name_core_words(name: str) -> list[str]:
    """核心品名词：去掉过短/停用词，用于「名称必须命中」。"""
    # 先折中文空格 + 去地名，否则「薄 壁 管」抽不出连续中文词
    name = normalize_material_name(name or "")
    # 再剥尺寸（normalize 已 peel 一次，幂等）
    name_for_words = peel_name_dimension_noise(name) or name
    core = name_search_core(name_for_words or name)
    words: list[str] = []
    if core:
        words.append(core)
        # 型号后残留的「型xxx」→ 同时尝试无「型」前缀
        if core.startswith("型") and len(core) >= 3:
            words.append(core[1:])
        # 行业站常混用“线型/线形”。这只是字形变换，不是枚举行业词表。
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


def required_name_inclusions(name: str) -> list[str]:
    """Extract material inclusions such as “(含胶圈)” that must not be dropped."""
    out: list[str] = []
    for group in re.findall(r"[（(]([^）)]{1,30})[）)]", name or ""):
        for part in re.split(r"[、,，/；;+]", group):
            text = part.strip()
            match = re.match(r"^(?:含|带|配|包含|附带)\s*(.{1,20})$", text)
            if not match:
                continue
            component = match.group(1).strip()
            if component and component not in out:
                out.append(component)
    return out


def peel_name_dimension_noise(name: str) -> str:
    """从名称里去掉尺寸、有效长度等参数，只留品名+型号。"""
    s = collapse_cjk_spaces(name or "")
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
    # 关键：先折中文空格，否则「薄 壁 管」正则抽不出连续中文
    s = collapse_cjk_spaces(name or "")
    s = strip_geo_noise(s)
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


def _is_electrical_attr_token(token: str) -> bool:
    """AC220V / 400W / IP68 / 3500K 等电气参数片段，不得当型号。"""
    t = (token or "").strip()
    if not t:
        return False
    if re.fullmatch(r"(?i)(?:AC|DC)\s*\d+(?:\.\d+)?\s*V", t):
        return True
    if re.fullmatch(r"(?i)\d+(?:\.\d+)?\s*W(?:\s*[/／]\s*(?:m|米))?", t):
        return True
    if re.fullmatch(r"(?i)IP\s*\d{2}", t):
        return True
    if re.fullmatch(r"(?i)\d{3,5}\s*K", t):
        return True
    if re.fullmatch(r"(?i)(?:AC|DC)?\d+(?:\.\d+)?(?:V|W|A|K)", t):
        return True
    return False


def _is_bore_or_pressure_token(token: str) -> bool:
    """DN100 / φ12 / PN16 已是口径/压力，不得再当型号。"""
    t = (token or "").strip()
    return bool(
        re.fullmatch(r"(?i)(?:DN|φ|Φ|ф|ø)\s*\d+(?:\.\d+)?(?:\s*[×xX*]\s*\d+(?:\.\d+)?)?", t)
        or re.fullmatch(r"(?i)PN\s*\d+(?:\.\d+)?", t)
    )


def _is_attr_chain_model(token: str) -> bool:
    """`400W/AC220V/DC24V/IP68` 整串由电气属性拼接 → 不当型号。"""
    t = (token or "").strip()
    if not t or ("/" not in t and "／" not in t):
        return False
    parts = [p for p in re.split(r"[/／]", t) if p.strip()]
    if len(parts) < 2:
        return False
    return all(_is_electrical_attr_token(p) or _is_bore_or_pressure_token(p) for p in parts)


# 规格匹配时剔除的非规格噪声（电话/价格/供应商/说明），避免 150 出现在手机号里误命中 DN150
_NON_SPEC_NOISE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:运费说明|税金说明|报价说明|备注说明)\s*[:：]?[^。；;\n]{0,200}[。；;\n]?",
        re.I,
    ),
    re.compile(
        r"(?:市场价|建议价|除税市场价|含税市场价|除税建议价|含税建议价|除税价|含税价)"
        r"\s*[:：]?\s*[¥￥]?\s*-?\d+(?:\.\d+)?",
        re.I,
    ),
    re.compile(r"[¥￥]\s*-?\d+(?:\.\d+)?"),
    re.compile(
        r"(?:手机号码|固定电话|联系电话|电话号码|联系方式|Tel|手机|电话)\s*[:：]?\s*[\d\-\s]{6,20}",
        re.I,
    ),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),  # 大陆手机号
    re.compile(r"(?<!\d)0\d{2,3}[-\s]?\d{7,8}(?!\d)"),  # 固话
    re.compile(
        r"(?:供应商名称|供应商|厂家名称|生产厂家|经营模式|所在地区|联系人|联系地址|地址)\s*[:：][^\n|]{0,100}",
        re.I,
    ),
    re.compile(r"(?:查看价格|查看联系方式|查看报价单)", re.I),
)


def scrub_non_spec_noise(text: str) -> str:
    """去掉价格/电话/供应商/运费等非规格文本，供尺寸与硬条件匹配使用。"""
    s = text or ""
    for pat in _NON_SPEC_NOISE_RES:
        s = pat.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_match_page_text(
    page_title: str = "",
    page_text: str = "",
    *,
    spec_seen: str = "",
    match_spec_text: str = "",
    match_name_text: str = "",
) -> str:
    """
    组装用于 strict_name_spec_match 的证据正文。
    优先：显式规格字段 / match_spec_text / spec_seen / 标题；再并入清洗后的页面正文。
    """
    parts: list[str] = []
    for p in (
        match_name_text,
        page_title,
        match_spec_text,
        spec_seen,
    ):
        t = (p or "").strip()
        if t and t not in parts:
            parts.append(t)
    body = scrub_non_spec_noise(page_text or "")
    if body and body not in parts:
        parts.append(body)
    # 标题/规格字段本身也去一次噪声（列表行常把价和电话拼进 spec_seen）
    joined = scrub_non_spec_noise(" ".join(parts))
    return joined


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

    # 口径/直径：只进 dimension，绝不进 model
    for m in re.finditer(r"(?:DN|φ|Φ)\s*\d{2,3}(?:\s*[×xX*]\s*\d+(?:\.\d+)?)?", s, re.I):
        raw = re.sub(r"\s+", "", m.group(0))
        add("dimension", f"尺寸 {raw}", raw)
    # 询价表「直径12 / 直径(mm)12」→ 与 DN/φ 同一硬条件
    for m in re.finditer(
        r"直径\s*(?:\(mm\)|mm)?\s*[:：]?\s*(\d+(?:\.\d+)?)", s, re.I
    ):
        n = _num_text(m.group(1))
        add("dimension", f"尺寸 φ{n}", f"φ{n}")
    # 公称压力 PN16：只进 pressure
    for m in re.finditer(r"(?i)\bPN\s*(\d+(?:\.\d+)?)\b", s):
        add("pressure", f"压力 PN{m.group(1)}", f"PN{m.group(1)}")
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
        if _is_bore_or_pressure_token(token):
            continue
        if _is_electrical_attr_token(token) or _is_attr_chain_model(token):
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
        vn = _norm(value)
        if vn in _norm(blob_raw):
            return True
        # PN16 ↔ 公称压力16 / pn 16
        m = re.match(r"pn\s*(\d+(?:\.\d+)?)", vn, re.I)
        if m:
            n = m.group(1)
            return bool(
                re.search(rf"pn\s*{re.escape(n)}(?!\d)", _norm(blob_raw), re.I)
                or re.search(rf"公称压力\s*[:：]?\s*{re.escape(n)}(?!\d)", blob_raw or "")
            )
        return False
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
    llm_invoked: bool = False  # 是否走了 AI 语义路径（含缓存）
    llm_decision: str = ""  # equivalent | insufficient | conflict | ""
    llm_from_cache: bool = False  # True=读本地缓存，无新 Token
    llm_api_called: bool = False  # True=本回合真正请求了 API


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
    """只抽有规格语义的尺寸串；不从裸数字/电话中猜 DN。"""
    text = scrub_non_spec_noise(blob or "")
    dims = re.findall(
        r"(?i)(?:DN|φ|Φ|ф|ø)\s*\d{2,3}(?:\s*[×xX*]\s*\d+(?:\.\d+)?)?"
        r"|直径\s*(?:\(mm\)|mm)?\s*[:：]?\s*\d+(?:\.\d+)?"
        r"|(?:口径|管径|公称通径)\s*[:：]?\s*(?:DN|φ|Φ)?\s*\d+(?:\.\d+)?"
        r"|(?<!\d)\d+(?:\.\d+)?(?:\s*[×xX*]\s*\d+(?:\.\d+)?){1,3}\s*(?:mm|cm|m)"
        r"|(?<!\d)\d{2,5}\s*[×xX*]\s*\d{2,5}(?:\s*[×xX*]\s*\d{2,5})?",
        text,
    )
    out: list[str] = []
    for x in dims:
        if not x:
            continue
        # 直径(mm)：12 → 归一成 φ12，便于比对
        m = re.search(r"直径\s*(?:\(mm\)|mm)?\s*[:：]?\s*(\d+(?:\.\d+)?)", x, re.I)
        if m and not re.search(r"(?i)dn|φ|φ", x):
            out.append(f"φ{m.group(1)}")
            continue
        m2 = re.search(
            r"(?:口径|管径|公称通径)\s*[:：]?\s*(?:DN|φ|Φ)?\s*(\d+(?:\.\d+)?)", x, re.I
        )
        if m2 and not re.search(r"(?i)(?:DN|φ|Φ)\s*\d", x):
            out.append(f"DN{m2.group(1)}")
            continue
        out.append(re.sub(r"\s+", "", x))
    return out


def _dim_nums(value: str) -> list[str]:
    return re.findall(r"\d+", value or "")


def _is_bore_dimension(wanted: str) -> bool:
    wn = _norm(wanted)
    return bool(re.match(r"^(?:dn|φ|ф|ø)\s*\d", wn, re.I))


def _dimension_hit(wanted: str, blob_raw: str) -> bool:
    """
    截面/尺寸命中：
      - 1250x400 命中 1250×400 / 1250*400
      - 1250x400 命中 1250×400×1500（第三维常为有效长度）
      - DN100 / φ12 与造价通「直径(mm)：12」「直径12」互认
      - **禁止**用电话/价格/地址里的裸数字冒充 DN/φ
    """
    wanted_n = _norm(wanted)
    if not wanted_n:
        return False
    # 匹配前清洗非规格噪声（手机号 150、市场价 100 等）
    raw = scrub_non_spec_noise(blob_raw or "")
    bn = _norm(raw)

    # —— 口径/直径：只认带规格语义的写法，绝不回落「正文任意出现该数字」——
    m_cal = re.match(r"^(?:dn|φ|ф|ø)\s*(\d+(?:\.\d+)?)(?:[x×*].*)?$", wanted_n, re.I)
    if m_cal:
        num_i = _num_text(m_cal.group(1))
        patterns = (
            rf"(?:dn|φ|ф|ø)\s*{re.escape(num_i)}(?!\d)",
            rf"直径\s*(?:\(mm\)|mm)?\s*[:：]?\s*{re.escape(num_i)}(?!\d)",
            rf"直径\s*{re.escape(num_i)}(?!\d)",
            rf"(?:口径|管径|公称通径)\s*[:：]?\s*(?:dn|φ|ф|ø)?\s*{re.escape(num_i)}(?!\d)",
            # 造价平台常把管件口径写成「规格(mm):150 / 规格尺寸(mm):150」。
            # 只接受明确字段和值，且拒绝 150×150，避免把截面误当 DN150。
            rf"(?:规格尺寸|规格)\s*(?:\(mm\)|mm)?\s*[:：]\s*(?:dn\s*)?{re.escape(num_i)}(?!\d)(?!\s*[x×*]\s*\d)",
            # 仅当数字紧贴 mm 且前后有口径语义词时才认，禁止单独 100mm 来自无关描述
            rf"(?:直径|口径|管径|公称通径|dn|φ).{{0,8}}(?<!\d){re.escape(num_i)}\s*mm(?![a-z0-9])",
        )
        if any(re.search(p, bn, re.I) or re.search(p, raw, re.I) for p in patterns):
            return True
        return False  # 口径条件：禁止再走裸数字 fallback

    if wanted_n in bn:
        return True
    compact = re.sub(r"[^0-9x]", "", wanted_n)
    bcompact = re.sub(r"[^0-9x×\*]", "", bn).replace("×", "x").replace("*", "x")
    if compact and len(compact) >= 3 and "x" in compact and compact in bcompact:
        return True

    wnums = _dim_nums(wanted)
    if len(wnums) < 2:
        # 单数字非口径尺寸：必须有单位/尺寸语义，禁止裸数字
        if not wnums:
            return False
        n = wnums[0]
        return bool(
            re.search(
                rf"(?:尺寸|规格|长度|宽度|高度|厚度|截面).{{0,12}}(?<!\d){re.escape(n)}(?!\d)"
                rf"|(?<!\d){re.escape(n)}\s*(?:mm|cm|m)(?![a-z])",
                raw,
                re.I,
            )
        )
    # 页面任一尺寸串包含所需截面数字（顺序一致优先）
    for d in _extract_page_dims(raw):
        pnums = _dim_nums(d)
        if len(pnums) < 2:
            continue
        if pnums[: len(wnums)] == wnums:
            return True
        if set(wnums).issubset(set(pnums)) and len(wnums) == 2:
            return True
    # 宽松：两个关键数字都在规格语义上下文出现（宽/高 或 x 连接）
    if all(n in bn for n in wnums[:2]):
        if re.search(r"[x×*]", bn) or ("宽" in raw and "高" in raw):
            # 再确认不是价格噪声残留
            if re.search(
                rf"(?<!\d){re.escape(wnums[0])}(?!\d).{{0,24}}(?<!\d){re.escape(wnums[1])}(?!\d)",
                bn,
            ) or re.search(
                rf"(?:宽|高|截面|规格).{{0,16}}{re.escape(wnums[0])}.{{0,16}}{re.escape(wnums[1])}",
                raw,
                re.I,
            ):
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
        clean = scrub_non_spec_noise(blob)
        if _dimension_hit(value, clean):
            return conflicts
        dims = _extract_page_dims(clean)
        if dims:
            # 页面明确有其它 DN/φ/截面 → 冲突；无尺寸展示不算硬冲突
            conflicts.append(f"{label}，页面尺寸为 {dims[0]}")
            return conflicts
        # 口径：页面写了不同 DN/φ 也算冲突（_extract 已覆盖；再兜一层）
        if _is_bore_dimension(value):
            other = re.findall(r"(?i)(?:DN|φ|Φ|ф|ø)\s*(\d{2,3})", clean)
            want_n = _dim_nums(value)
            if other and want_n and all(_num_text(x) != want_n[0] for x in other):
                conflicts.append(f"{label}，页面尺寸为 DN{other[0]}")
        return conflicts

    if kind == "pressure":
        clean = scrub_non_spec_noise(blob)
        wanted = _norm(value)
        if wanted and wanted in _norm(clean):
            return conflicts
        # PN16 明确写了其它 PN
        m_want = re.match(r"pn\s*(\d+(?:\.\d+)?)", wanted, re.I)
        if m_want:
            page_pns = re.findall(r"(?i)\bPN\s*(\d+(?:\.\d+)?)\b", clean)
            if page_pns and all(_num_text(x) != _num_text(m_want.group(1)) for x in page_pns):
                conflicts.append(f"{label}，页面压力为 PN{page_pns[0]}")
                return conflicts
        vals = re.findall(r"(?i)(\d+(?:\.\d+)?\s*(?:MPa|kPa|Pa))(?![A-Za-z])", clean)
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


# 硬规格冲突关键字：功率/电压/口径等对不上时，practical 也不得进待核（会污染结果）
_HARD_CONFLICT_MARKERS = (
    "功率",
    "电压",
    "直径",
    "通径",
    "DN",
    "φ",
    "Φ",
    "防护",
    "IP",
    "色温",
    "型号",
    "压力",
    "PN",
    "尺寸",
    "截面",
    "规格冲突",
)


def has_hard_spec_conflict(
    mr: MatchResult | None = None,
    *,
    conflicts: list | tuple | None = None,
    detail: str = "",
    match_outcome: str = "",
) -> bool:
    """
    功率/电压/尺寸等硬条件 **冲突** → True（不可当可用询价）。

    注意：仅「规格缺少：电压 …」不算冲突——缺证据可进待核，数值对不上才丢。
    """
    conflict_items: list[str] = []
    if mr is not None:
        conflict_items.extend(str(c) for c in (mr.conflicts or ()))
        det = str(mr.detail or "")
        if (mr.outcome or "") == "reject" and "规格冲突" in det:
            return True
        # 详情里「规格冲突：…」整段也算
        if "规格冲突" in det:
            conflict_items.append(det)
    conflict_items.extend(str(c) for c in (conflicts or ()))
    det2 = detail or ""
    if (match_outcome or "") == "reject" and "规格冲突" in det2:
        return True
    if "规格冲突" in det2:
        conflict_items.append(det2)

    if not conflict_items:
        return False

    blob = "；".join(conflict_items)
    # 只要声明了规格冲突，即硬冲突（detail 已含功率/尺寸等说明）
    if "规格冲突" in blob:
        return True
    # 单条 conflict 文本含硬字段（如「功率 9W，页面为 6W」）
    return any(k in blob for k in _HARD_CONFLICT_MARKERS)


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

    # 名称已中：规格缺 → 候选待核；硬规格冲突 → practical 直接丢（9W≠15W 不能当待审价）
    if mr.outcome == "review":
        return "candidate", "review", detail
    if mr.outcome == "reject":
        if mode == "loose":
            return (
                "candidate",
                "review",
                f"{detail}（规格与询价表不完全一致，请人工确认）",
            )
        # practical：硬冲突丢弃；非硬 reject 也丢（避免错规格污染待核）
        if has_hard_spec_conflict(mr):
            return "discard", "reject", f"{detail}（硬规格冲突，不进待核）"
        return "discard", "reject", detail

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
    *,
    match_spec_text: str = "",
    match_name_text: str = "",
    spec_seen: str = "",
) -> MatchResult:
    """
    名称命中，并且可抽取的规格硬参数逐项全部命中。

    正式报价唯一门禁。规格数字只在清洗后的规格语义文本中匹配；
    电话/价格/供应商等噪声不得参与尺寸命中。
    可选 match_spec_text / spec_seen / match_name_text 优先作为证据。
    """
    # 名称匹配可用较宽文本（含标题）；规格硬条件只用清洗后的证据
    name_blob = build_match_page_text(
        page_title,
        page_text,
        match_name_text=match_name_text,
        # 名称侧也带上规格字段，避免标题过短
        spec_seen=spec_seen,
        match_spec_text=match_spec_text,
    )
    # 规格侧：优先显式规格区，并强制去噪声
    spec_blob = build_match_page_text(
        page_title,
        page_text,
        match_name_text=match_name_text,
        match_spec_text=match_spec_text,
        spec_seen=spec_seen,
    )
    blob_raw = spec_blob or name_blob
    blob_l = name_blob.lower()
    # 必须用原始名称抽尺寸再剥，否则 1250X400 会在 normalize 时丢掉导致硬冲突失效
    original_name = collapse_cjk_spaces(getattr(item, "name", None) or "")
    original_name = strip_geo_noise(original_name) or original_name
    spec = collapse_cjk_spaces(getattr(item, "spec", None) or "").strip()
    # 名称里若还粘着尺寸/有效长度，匹配时并入规格侧，不要求标题写「有效长度」四字
    name_clean = peel_name_dimension_noise(original_name)
    name = name_clean or original_name
    if name_clean and name_clean != original_name:
        moved: list[str] = []
        for part in re.findall(
            r"\d+(?:\.\d+)?\s*[xX×*]\s*\d+(?:\.\d+)?(?:\s*[xX×*]\s*\d+(?:\.\d+)?){0,2}"
            r"|(?:有效)?(?:长度|宽度|高度|厚度)\s*[：:为]?\s*\d+(?:\.\d+)?",
            original_name,
        ):
            p = part.strip()
            if p and p not in moved:
                moved.append(p)
        if moved:
            extra = " ".join(moved)
            if extra not in spec:
                spec = f"{spec} {extra}".strip()
    # 品名侧再去地名/折空格（尺寸已进 spec）
    name = strip_geo_noise(collapse_cjk_spaces(name)) or name

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

    # 品名命中：子串 + 字序无关软等价（地埋灯↔埋地灯），非写死词表
    name_hits = [w for w in name_words if name_token_matches_blob(w, blob_l, name_blob)]
    cn_hits = [w for w in name_hits if re.search(r"[\u4e00-\u9fff]", w)]
    model_hits = [w for w in name_hits if re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", w)]
    name_ok = bool(cn_hits) or (bool(model_hits) and any(
        name_token_matches_blob(w, blob_l, name_blob)
        for w in name_words
        if re.search(r"[\u4e00-\u9fff]{2,}", w)
    ))
    if not name_ok and name_words:
        name_ok = len(name_hits) >= 1
    # 整名软等价（询价品名 vs 标题/正文）
    if not name_ok and soft_product_name_equivalent(name, page_title, page_text):
        name_ok = True
        name_hits = list(name_hits) + [f"软等价:{_strip_name_decorations(name)}"]
    # 整串互含：询价「薄壁不锈钢管」⊂ 标题 或 标题 ⊂ 询价（去空格后）
    if not name_ok:
        q_ns = re.sub(r"\s+", "", name or "")
        t_ns = re.sub(r"\s+", "", (page_title or "") + (match_name_text or ""))
        if len(q_ns) >= 2 and t_ns and (q_ns in t_ns or t_ns in q_ns):
            name_ok = True
            name_hits = list(name_hits) + [f"整串互含:{q_ns[:12]}"]
        elif len(q_ns) >= 4:
            # 标题含询价核心 2 字块 ≥2 个（薄壁/不锈钢/钢管）
            frags = re.findall(r"[\u4e00-\u9fff]{2}", q_ns)
            hit_n = sum(1 for f in frags if f in t_ns)
            if hit_n >= min(2, len(frags)) and hit_n >= 1:
                name_ok = True
                name_hits = list(name_hits) + [f"字块命中:{hit_n}"]

    if not name_ok:
        return MatchResult(
            False,
            0.0,
            0,
            1,
            f"名称未命中 need={name_words[:5]}",
            "reject",
            "reject",
            conflicts=(f"名称未命中：{name_words}",),
            # missing 用「品名：」前缀 → AI 可判同义（硬规格缺项仍禁止 AI 改判）
            missing=(f"品名：{name}",),
        )

    # “(含胶圈)/(带底座)”是材料组成要求，不是可随意剥掉的名称装饰。
    # 来源未明确展示时只能待核；明确写“不含”则直接冲突。
    inclusions = required_name_inclusions(original_name)
    if inclusions:
        inclusion_blob = " ".join(
            str(x or "")
            for x in (page_title, match_name_text, spec_seen, match_spec_text, page_text)
        )
        missing_inclusions: list[str] = []
        for component in inclusions:
            if re.search(rf"不\s*(?:含|带|配)\s*{re.escape(component)}", inclusion_blob):
                return MatchResult(
                    False,
                    0.0,
                    len(name_hits),
                    len(name_words) + len(inclusions),
                    f"名称附加条件冲突：来源明确不含{component}",
                    "reject",
                    "reject",
                    conflicts=(f"不含{component}",),
                    evidence=tuple(name_hits),
                )
            if not re.search(
                rf"(?:含|带|配|包含|附带)\s*{re.escape(component)}",
                inclusion_blob,
            ):
                missing_inclusions.append(f"含{component}")
        if missing_inclusions:
            return MatchResult(
                False,
                0.6,
                len(name_hits),
                len(name_words) + len(inclusions),
                "名称附加条件缺少：" + ", ".join(missing_inclusions),
                "review",
                "review",
                missing=tuple(f"名称附加条件：{x}" for x in missing_inclusions),
                evidence=tuple(name_hits),
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
