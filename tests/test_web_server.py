from __future__ import annotations

import json
import threading
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from urllib.parse import quote

import openpyxl

from material_price_audit.webapp.server import (
    Handler,
    _excel_filename,
    _platform_catalog,
    _safe_child,
)
from material_price_audit.webapp.job_history import append_job


def test_static_paths_cannot_escape_the_static_directory(tmp_path: Path):
    static = tmp_path / "static"
    static.mkdir()
    assert _safe_child(static, "index.html") == static / "index.html"
    assert _safe_child(static, "../secret.txt") is None
    assert _safe_child(static, "%2e%2e/secret.txt") is None


def test_excel_filename_accepts_encoded_chinese_and_rejects_paths():
    assert _excel_filename(quote("安装询价表.xlsx")) == "安装询价表.xlsx"
    for value in ("../secret.xlsx", "folder/file.xlsx", "folder\\file.xlsx", "old.xls"):
        try:
            _excel_filename(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe filename accepted: {value}")


def test_configured_custom_platform_is_visible_in_catalog():
    config = {
        "platforms": {
            "definitions": {
                "example": {
                    "name": "示例平台",
                    "login_url": "https://example.com/login",
                    "search_url": "https://example.com/search?q={query}",
                }
            }
        }
    }
    catalog = _platform_catalog(config)
    custom = next(row for row in catalog if row["id"] == "example")
    assert custom["name"] == "示例平台"
    assert custom["custom"] is True


def test_binary_excel_upload_is_not_consumed_as_json(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MATERIAL_PRICE_AUDIT_HOME", str(tmp_path))
    workbook_path = tmp_path / "source.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "材料名称"
    workbook.save(workbook_path)
    payload = workbook_path.read_bytes()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        filename = "三条材料询价.xlsx"
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/upload-file",
            data=payload,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Filename": quote(filename),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["ok"] is True
    assert result["name"] == filename
    assert (tmp_path / "data" / "input" / filename).read_bytes() == payload


def test_history_load_returns_without_state_lock_deadlock(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MATERIAL_PRICE_AUDIT_HOME", str(tmp_path))
    append_job(
        tmp_path,
        {
            "id": "run-history-test",
            "run_id": "run-history-test",
            "phase": "done",
            "full_k": 1,
            "item_results": [
                {
                    "id": "sheet|2",
                    "name": "薄壁不锈钢管",
                    "spec": "DN100",
                    "status": "full_k",
                    "quote_list": [{"price": 63, "platform": "lingcai"}],
                }
            ],
        },
    )

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/history/load",
            data=json.dumps({"id": "run-history-test"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["ok"] is True
    assert result["run_id"] == "run-history-test"
    assert result["item_results"][0]["spec"] == "DN100"
    assert result.get("viewing_history") is True
    # 不应把历史写回全局 STATE.item_results（避免污染当前任务面板）
    from material_price_audit.webapp.job_state import STATE

    assert not STATE.item_results or all(
        r.get("id") != "sheet|2" for r in (STATE.item_results or [])
    )
