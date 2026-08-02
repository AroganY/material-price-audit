"""任务历史：写入 / 读取 / 删除 / 清空。"""

from __future__ import annotations

from pathlib import Path

from material_price_audit.models import Quote, QuoteSet
from material_price_audit.runtime import save_evidence

from material_price_audit.webapp.job_history import (
    append_job,
    clear_history,
    delete_job,
    delete_jobs,
    get_job,
    load_history,
)


def test_append_and_load(tmp_path: Path):
    append_job(tmp_path, {"id": "job-a", "message": "一", "items_done": 1})
    append_job(tmp_path, {"id": "job-b", "message": "二", "items_done": 2})
    rows = load_history(tmp_path, limit=10)
    assert len(rows) == 2
    assert rows[0]["id"] == "job-b"  # 最新在前
    assert get_job(tmp_path, "job-a")["message"] == "一"


def test_delete_single(tmp_path: Path):
    append_job(tmp_path, {"id": "job-1", "message": "x"})
    append_job(tmp_path, {"id": "job-2", "message": "y"})
    r = delete_job(tmp_path, "job-1")
    assert r["ok"] is True
    assert r["remaining"] == 1
    assert get_job(tmp_path, "job-1") is None
    assert get_job(tmp_path, "job-2") is not None


def test_delete_missing(tmp_path: Path):
    r = delete_job(tmp_path, "nope")
    assert r["ok"] is False


def test_delete_batch(tmp_path: Path):
    for i in range(3):
        append_job(tmp_path, {"id": f"j{i}", "message": str(i)})
    r = delete_jobs(tmp_path, ["j0", "j2"])
    assert r["ok"] is True
    assert set(r["deleted"]) == {"j0", "j2"}
    assert r["remaining"] == 1
    assert get_job(tmp_path, "j1") is not None


def test_clear_history(tmp_path: Path):
    append_job(tmp_path, {"id": "a", "message": "1"})
    append_job(tmp_path, {"id": "b", "message": "2"})
    r = clear_history(tmp_path)
    assert r["ok"] is True
    assert r["cleared"] == 2
    assert load_history(tmp_path) == []


def test_delete_files_only_under_data(tmp_path: Path):
    data = tmp_path / "data" / "output"
    data.mkdir(parents=True)
    f = data / "result-hist.xlsx"
    f.write_bytes(b"PK\x03\x04fake")
    outside = tmp_path / "outside.xlsx"
    outside.write_text("nope", encoding="utf-8")

    append_job(
        tmp_path,
        {
            "id": "with-file",
            "result_path": str(f),
            "rfq_path": str(outside),  # 不在 data/ 下，不应被删
        },
    )
    r = delete_job(tmp_path, "with-file", delete_files=True)
    assert r["ok"] is True
    assert not f.exists()
    assert outside.exists()  # 安全：项目外路径不删


def test_append_overwrite_same_id(tmp_path: Path):
    append_job(tmp_path, {"id": "same", "message": "old", "items_done": 1})
    append_job(tmp_path, {"id": "same", "message": "new", "items_done": 9})
    rows = load_history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["message"] == "new"
    assert rows[0]["items_done"] == 9


def test_get_job_hydrates_old_review_rows_from_evidence(tmp_path: Path):
    # 专属 evidence 文件名需含任务 id，才允许 hydrate
    evidence_path = tmp_path / "data" / "output" / "evidence-old-review.json"
    review = Quote(
        rank=1,
        price=17.08,
        platform="guangcai",
        title="不锈钢卡箍",
        url="https://example.com/search",
        detail_url="https://example.com/exact-quote.pdf",
        spec_seen="规格(mm):150",
        unit="个",
        supplier="某供应商",
        price_text="17.08",
        price_context="第2条厂家报价",
        evidence_scope="exact_quote_row",
    )
    save_evidence(
        evidence_path,
        {
            "询价|2": {
                **QuoteSet(
                    item_id="询价|2",
                    review_candidates=[review],
                    status="need_review",
                ).to_dict(),
                "sheet": "询价",
                "row": 2,
                "name": "不锈钢卡箍(含胶圈)",
                "spec": "DN150",
            }
        },
    )
    append_job(
        tmp_path,
        {
            "id": "old-review",
            "run_id": "old-review",
            "evidence_path": str(evidence_path),
            "item_results": [
                {
                    "id": "询价|2",
                    "status": "need_review",
                    "price": 17.08,
                    # 缺字段的旧快照，需从专属 evidence 补全
                    "review_list": [],
                }
            ],
        },
    )

    job = get_job(tmp_path, "old-review")
    assert job is not None
    row = job["item_results"][0]
    assert row["price"] is None
    assert row["audit"] is None
    assert row["name"] == "不锈钢卡箍(含胶圈)"
    assert row["review_list"][0]["spec_seen"] == "规格(mm):150"
    assert row["review_list"][0]["detail_url"] == review.detail_url
    assert row["review_list"][0]["price_context"] == "第2条厂家报价"
    assert row["review_list"][0]["price_role"] == "review_candidate"


def test_shared_evidence_json_does_not_overwrite_other_job_snapshot(tmp_path: Path):
    """公共 evidence.json 被后一次覆盖时，旧历史必须仍显示自己的材料。"""
    evidence = tmp_path / "data" / "output" / "evidence.json"
    save_evidence(
        evidence,
        {
            "A|1": {
                **QuoteSet(
                    item_id="A|1",
                    quotes=[
                        Quote(
                            rank=1,
                            price=99,
                            platform="jd",
                            title="最新任务材料",
                            url="https://x/1",
                        )
                    ],
                    status="full_k",
                ).to_dict(),
                "name": "最新任务材料",
                "spec": "NEW",
            }
        },
    )
    append_job(
        tmp_path,
        {
            "id": "job-old",
            "run_id": "job-old",
            "evidence_path": str(evidence),  # 误指向公共文件
            "item_results": [
                {
                    "id": "B|2",
                    "name": "旧任务材料",
                    "spec": "OLD-SPEC",
                    "status": "no_match",
                    "quotes": 0,
                    "quote_list": [],
                    "review_list": [],
                }
            ],
        },
    )
    job = get_job(tmp_path, "job-old")
    assert job is not None
    row = job["item_results"][0]
    assert row["name"] == "旧任务材料"
    assert row["spec"] == "OLD-SPEC"
    assert row["status"] == "no_match"


def test_rich_snapshot_not_replaced_by_evidence(tmp_path: Path):
    evidence_path = tmp_path / "data" / "output" / "evidence-run-rich.json"
    save_evidence(
        evidence_path,
        {
            "X|1": {
                **QuoteSet(
                    item_id="X|1",
                    quotes=[
                        Quote(
                            rank=1,
                            price=1,
                            platform="z",
                            title="evidence里的",
                            url="https://e",
                        )
                    ],
                    status="full_k",
                ).to_dict(),
                "name": "evidence里的",
            }
        },
    )
    append_job(
        tmp_path,
        {
            "id": "run-rich",
            "run_id": "run-rich",
            "evidence_path": str(evidence_path),
            "item_results": [
                {
                    "id": "X|1",
                    "name": "快照里的名称",
                    "spec": "快照规格",
                    "status": "partial",
                    "quotes": 1,
                    "quote_list": [
                        {
                            "price": 88,
                            "title": "快照报价",
                            "url": "https://snap",
                            "platform": "gc",
                        }
                    ],
                }
            ],
        },
    )
    job = get_job(tmp_path, "run-rich")
    row = job["item_results"][0]
    assert row["name"] == "快照里的名称"
    assert row["quote_list"][0]["title"] == "快照报价"
    assert row["quote_list"][0]["price"] == 88


def test_get_job_filters_legacy_empty_baidu_leads(tmp_path: Path):
    append_job(
        tmp_path,
        {
            "id": "legacy-baidu",
            "item_results": [
                {
                    "id": "X|2",
                    "name": "薄壁不锈钢管",
                    "spec": "DN200",
                    "status": "partial",
                    "message": "部分合格价 1/3；供应商线索2条",
                    "quote_list": [{"price": 325.22, "platform": "lingcai"}],
                    "supplier_list": [
                        {
                            "price": 0,
                            "title": "百度爱采购",
                            "supplier": "",
                            "phone": "",
                            "url": "https://passport.baidu.com/v2/?login",
                        },
                        {
                            "price": 0,
                            "title": "真实厂家",
                            "supplier": "华通管材有限公司",
                            "phone": "13800138000",
                            "url": "https://maker.example.com/product/1",
                        },
                    ],
                }
            ],
        },
    )
    job = get_job(tmp_path, "legacy-baidu")
    assert job is not None
    row = job["item_results"][0]
    assert len(row["supplier_list"]) == 1
    assert row["supplier_list"][0]["supplier"] == "华通管材有限公司"
    assert "供应商线索1条" in row["message"]
