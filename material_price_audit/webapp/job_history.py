"""本机任务历史：回看、删除单条、清空、可选清理关联文件。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


def _path(root: Path) -> Path:
    return root / "data" / "user" / "job_history.json"


def _read_all(root: Path) -> list[dict[str, Any]]:
    p = _path(root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("jobs") or []
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]
    except Exception:
        return []


def _write_all(root: Path, rows: list[dict[str, Any]]) -> None:
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"jobs": list(rows)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_history(root: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = _read_all(root)
    lim = max(1, int(limit or 50))
    return list(rows)[:lim]


def append_job(root: Path, job: dict[str, Any], *, keep: int = 50) -> dict[str, Any]:
    rows = _read_all(root)
    job = dict(job)
    job.setdefault("id", f"job-{int(time.time() * 1000)}")
    job.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    # 同 id 覆盖，避免重复追加
    jid = str(job.get("id") or "")
    rows = [r for r in rows if str(r.get("id")) != jid]
    rows.insert(0, job)
    rows = rows[: max(1, int(keep or 50))]
    _write_all(root, rows)
    return job


def get_job(root: Path, job_id: str) -> dict[str, Any] | None:
    jid = str(job_id or "").strip()
    if not jid:
        return None
    for j in _read_all(root):
        if str(j.get("id")) == jid:
            # 深拷贝，避免后续改 STATE 污染历史缓存
            import copy

            hydrated = _hydrate_job_from_evidence(root, copy.deepcopy(j))
            return _sanitize_job_sources(hydrated)
    return None


def _sanitize_job_sources(job: dict[str, Any]) -> dict[str, Any]:
    """读取历史时过滤旧版本写入的无价百度跳转页；不改写历史文件。"""
    try:
        from ..adapters.baidu_fallback import source_quality_for_url
    except Exception:
        source_quality_for_url = None

    def _has_positive_price(q: dict[str, Any]) -> bool:
        try:
            return float(q.get("price") or 0) > 0.05
        except Exception:
            return False

    rows: list[dict[str, Any]] = []
    for old in list(job.get("item_results") or []):
        row = dict(old)
        row["web_list"] = [
            q
            for q in (row.get("web_list") or [])
            if isinstance(q, dict)
            and _has_positive_price(q)
        ]
        clean_leads: list[dict[str, Any]] = []
        for q in row.get("supplier_list") or []:
            if not isinstance(q, dict):
                continue
            has_contact = bool(
                str(q.get("supplier") or "").strip()
                or str(q.get("phone") or "").strip()
                or str(q.get("contact") or "").strip()
            )
            if not has_contact:
                continue
            url = str(q.get("detail_url") or q.get("url") or "").strip()
            if url and source_quality_for_url is not None:
                if source_quality_for_url(
                    url,
                    str(q.get("title") or ""),
                    str(q.get("spec_seen") or "")[:300],
                ) == "block":
                    continue
            clean_leads.append(q)
        row["supplier_list"] = clean_leads
        message = str(row.get("message") or "")
        if clean_leads:
            row["message"] = re.sub(
                r"供应商线索\s*\d+\s*条",
                f"供应商线索{len(clean_leads)}条",
                message,
            ).strip()
        else:
            row["message"] = re.sub(
                r"[；;]?\s*供应商线索\s*\d+\s*条", "", message
            ).strip()
        rows.append(row)
    job["item_results"] = rows
    return job


def _evidence_is_job_specific(job: dict[str, Any], evidence_path: Path) -> bool:
    """
    是否允许用该 evidence 文件补全本任务。
    共用 data/output/evidence.json 会被后一次询价覆盖，禁止拿来「复活」旧任务。
    """
    name = evidence_path.name.lower()
    run_id = str(job.get("run_id") or job.get("id") or "").strip()
    if not run_id:
        return False
    # 文件名含任务 id / run_id 片段才视为本任务专属
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in run_id)
    tokens = [t for t in safe.replace("_", "-").split("-") if len(t) >= 6]
    if any(t.lower() in name for t in tokens):
        return True
    if run_id.lower() in name:
        return True
    # 明确禁止通用文件名
    if name in ("evidence.json", "result.xlsx", "rfq.xlsx"):
        return False
    return False


def _row_has_rich_sources(row: dict[str, Any]) -> bool:
    for key in ("quote_list", "review_list", "market_list", "web_list", "supplier_list"):
        if row.get(key):
            return True
    return False


def _hydrate_job_from_evidence(root: Path, job: dict[str, Any]) -> dict[str, Any]:
    """
    仅在「本任务专属 evidence」且条目缺来源列表时补全。
    禁止用被覆盖的公共 evidence.json 把所有历史变成同一次结果。
    """
    rows_in = list(job.get("item_results") or [])
    # 快照已完整：直接用历史写入时的 item_results
    if rows_in and sum(1 for r in rows_in if _row_has_rich_sources(r)) >= max(
        1, (len(rows_in) + 1) // 2
    ):
        return job

    evidence_path = str(job.get("evidence_path") or "").strip()
    if not evidence_path:
        return job
    try:
        path = Path(evidence_path).expanduser().resolve()
        path.relative_to((root / "data").resolve())
        if not path.is_file():
            return job
        if not _evidence_is_job_specific(job, path):
            # 公共路径：绝不覆盖，避免「点哪条历史都是最新一次结果」
            return job
        from ..inquiry import quote_to_result_row
        from ..runtime import load_evidence, load_quote_map

        quote_map = load_quote_map(path)
        evidence = load_evidence(path)
    except Exception:
        return job

    hydrated = dict(job)
    rows: list[dict[str, Any]] = []
    for old in rows_in:
        row = dict(old)
        # 已有来源列表的行不再用 evidence 覆盖
        if _row_has_rich_sources(row):
            rows.append(row)
            continue
        item_id = str(row.get("id") or "")
        qset = quote_map.get(item_id)
        raw = evidence.get(item_id) or {}
        if not qset:
            rows.append(row)
            continue
        q0 = qset.quotes[0] if qset.quotes else None
        r0 = qset.review_candidates[0] if qset.review_candidates else None
        for key in (
            "sheet", "row", "name", "spec", "brand", "unit", "qty",
            "submit", "region_raw",
        ):
            if raw.get(key) not in (None, ""):
                row[key] = raw.get(key)
        row.update(
            {
                "status": qset.status,
                "quotes": len(qset.quotes),
                "platform": (q0 or r0).platform if (q0 or r0) else "",
                "title": (q0 or r0).title if (q0 or r0) else "",
                "url": (q0.url if q0 else (r0.url if r0 else "")) or "",
                # Only formal quotes may populate the material-level price/audit.
                "price": q0.price if q0 else None,
                "price_ex_tax": q0.price_ex_tax if q0 else None,
                "audit": raw.get("audit") if q0 else None,
                "quote_list": [
                    quote_to_result_row(q, role="formal")
                    for q in qset.quotes[:8]
                ],
                "review_list": [
                    quote_to_result_row(q, role="review_candidate")
                    for q in qset.review_candidates[:5]
                ],
                "market_list": [
                    quote_to_result_row(q, role="market_ref")
                    for q in qset.market_refs[:5]
                ],
                "web_list": [
                    quote_to_result_row(q, role="web_reference")
                    for q in qset.web_refs[:5]
                ],
                "supplier_list": [
                    quote_to_result_row(q, role="supplier_lead")
                    for q in qset.supplier_leads[:5]
                ],
            }
        )
        rows.append(row)
    hydrated["item_results"] = rows
    return hydrated


def _safe_unlink(path_str: str, root: Path) -> bool:
    """仅删除项目 data/ 下的文件，避免误删。"""
    if not path_str:
        return False
    try:
        p = Path(path_str).expanduser().resolve()
        data_root = (root / "data").resolve()
        # 必须在 data/ 内
        p.relative_to(data_root)
        if p.is_file():
            p.unlink()
            return True
    except Exception:
        return False
    return False


def delete_job(
    root: Path,
    job_id: str,
    *,
    delete_files: bool = False,
) -> dict[str, Any]:
    """
    删除单条历史。
    delete_files=True 时尝试删除该任务关联的 result/rfq/evidence（仅限 data/ 下）。
    """
    jid = str(job_id or "").strip()
    if not jid:
        return {"ok": False, "error": "缺少任务 id"}
    rows = _read_all(root)
    found: dict[str, Any] | None = None
    kept: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("id")) == jid:
            found = r
        else:
            kept.append(r)
    if not found:
        return {"ok": False, "error": "任务不存在"}
    removed_files: list[str] = []
    if delete_files:
        for key in ("result_path", "rfq_path", "evidence_path"):
            p = str(found.get(key) or "")
            if p and _safe_unlink(p, root):
                removed_files.append(p)
    _write_all(root, kept)
    return {
        "ok": True,
        "deleted_id": jid,
        "remaining": len(kept),
        "removed_files": removed_files,
    }


def delete_jobs(
    root: Path,
    job_ids: list[str],
    *,
    delete_files: bool = False,
) -> dict[str, Any]:
    """批量删除。"""
    ids = [str(x).strip() for x in (job_ids or []) if str(x).strip()]
    if not ids:
        return {"ok": False, "error": "未指定任务", "deleted": [], "remaining": len(_read_all(root))}
    deleted: list[str] = []
    errors: list[str] = []
    for jid in ids:
        r = delete_job(root, jid, delete_files=delete_files)
        if r.get("ok"):
            deleted.append(jid)
        else:
            errors.append(f"{jid}: {r.get('error')}")
    return {
        "ok": True,
        "deleted": deleted,
        "errors": errors,
        "remaining": len(_read_all(root)),
    }


def clear_history(root: Path, *, delete_files: bool = False) -> dict[str, Any]:
    """清空全部历史。"""
    rows = _read_all(root)
    removed_files: list[str] = []
    if delete_files:
        for job in rows:
            for key in ("result_path", "rfq_path", "evidence_path"):
                p = str(job.get(key) or "")
                if p and _safe_unlink(p, root):
                    removed_files.append(p)
    n = len(rows)
    _write_all(root, [])
    return {"ok": True, "cleared": n, "removed_files": removed_files, "remaining": 0}
