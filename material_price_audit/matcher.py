"""Map inquiry lines to searchable queries. Accuracy: only high-confidence model/keyword matches."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .excel_io import LineItem


@dataclass
class SearchJob:
    item: LineItem
    query: str
    must: list[str]
    platform: str  # jd | 1688
    confidence: str  # high | medium


# High-confidence model patterns (construction / security / network common)
MODEL_RE = re.compile(
    r"(DS-[A-Z0-9][A-Z0-9/\-]+"
    r"|RG-[A-Z0-9][A-Z0-9/\-\(\)]+"
    r"|ST\d{4}VX\d+"
    r"|iDS-[A-Z0-9/\-]+"
    r"|HM-[A-Z0-9/\-]+"
    r"|MS-\d+-\d+"
    r"|LRS-\d+-\d+"
    r"|JB-[A-Z0-9/\-]+"
    r")",
    re.I,
)

# Explicit product rules: (all keys in text, query, must tokens, platform)
RULES: list[tuple[list[str], str, list[str], str]] = [
    (["DS-KH6320-C1"], "海康威视 DS-KH6320-C1", ["KH6320", "6320"], "jd"),
    (["DS-KAD608-P"], "海康威视 DS-KAD608-P", ["KAD608", "608"], "jd"),
    (["DS-K1T806M"], "海康威视 DS-K1T806M", ["K1T806", "806"], "jd"),
    (["DS-KD9513"], "海康威视 DS-KD9513", ["KD9513", "9513"], "jd"),
    (["DS-K4H"], "海康威视 磁力锁 280kg", ["磁力锁", "280"], "jd"),
    (["DS-K1F600U"], "海康威视 DS-K1F600U", ["K1F600", "F600"], "jd"),
    (["DS-8832N-R8"], "海康威视 DS-8832N-R8", ["8832", "R8"], "jd"),
    (["ST6000VX008"], "希捷 ST6000VX008", ["ST6000", "酷鹰", "6T", "6TB"], "jd"),
    (["RG-NBS3000-24GT2SFP"], "锐捷 RG-NBS3000-24GT2SFP", ["NBS3000", "24GT"], "jd"),
    (["RG-ES110GDS"], "锐捷 RG-ES110GDS", ["ES110"], "jd"),
    (["RG-RAP2200"], "锐捷 RG-RAP2200", ["RAP2200"], "jd"),
    (["RG-NBS3200"], "锐捷 RG-NBS3200", ["NBS3200"], "jd"),
    (["RG-NBC256"], "锐捷 RG-NBC256", ["NBC256"], "jd"),
    (["R18T"], "漫步者 R18T", ["R18T", "漫步者"], "jd"),
    (["瑞天900"], "联想 瑞天900 i3-12100", ["瑞天", "12100"], "jd"),
]


def match_job(item: LineItem) -> SearchJob | None:
    text = item.text
    for keys, query, must, platform in RULES:
        if any(k in text for k in keys):
            return SearchJob(item=item, query=query, must=must, platform=platform, confidence="high")

    m = MODEL_RE.search(text)
    if m:
        model = m.group(1)
        brand = item.brand or ""
        # brand hints
        if "海康" in text or model.upper().startswith("DS-") or model.upper().startswith("IDS-"):
            brand = brand or "海康威视"
        if "锐捷" in text or model.upper().startswith("RG-"):
            brand = brand or "锐捷"
        if model.upper().startswith("ST"):
            brand = brand or "希捷"
        must = [model, model.split("-")[-1][:4]]
        return SearchJob(
            item=item,
            query=f"{brand} {model}".strip(),
            must=must,
            platform="jd",
            confidence="high",
        )

    # Medium: only clear commodity + size (avoid noisy false matches)
    dn = re.search(r"DN\s*(\d{2,3})", text, re.I)
    if "不锈钢闸阀" in text and dn:
        return SearchJob(
            item=item,
            query=f"304不锈钢闸阀 DN{dn.group(1)} 法兰",
            must=["闸阀", dn.group(1)],
            platform="1688",
            confidence="medium",
        )
    if "PE给水管" in text and dn:
        return SearchJob(
            item=item,
            query=f"PE给水管 DN{dn.group(1)}",
            must=["PE", dn.group(1)],
            platform="1688",
            confidence="medium",
        )
    if "镀锌" in text and "钢管" in text and dn:
        return SearchJob(
            item=item,
            query=f"热镀锌钢管 DN{dn.group(1)}",
            must=["镀锌", dn.group(1)],
            platform="1688",
            confidence="medium",
        )
    if "塑料检查井" in text or ("检查井" in text and "700" in text):
        return SearchJob(
            item=item,
            query="塑料检查井 700",
            must=["检查井", "700"],
            platform="1688",
            confidence="medium",
        )

    return None


def build_jobs(items: list[LineItem], platforms: set[str] | None = None) -> list[SearchJob]:
    """
    platforms:
      - None: keep all matched jobs (scrape will search user-enabled platforms)
      - set: only keep jobs whose preferred platform is in the set
        (legacy; multi-platform scrape usually passes None)
    """
    jobs = []
    for it in items:
        job = match_job(it)
        if not job:
            continue
        if platforms is not None and job.platform not in platforms:
            continue
        jobs.append(job)
    return jobs
