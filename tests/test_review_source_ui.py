from pathlib import Path

from material_price_audit.inquiry import quote_to_result_row
from material_price_audit.models import Quote


def test_review_ui_prefers_detail_url_and_does_not_promote_candidate_price():
    html = (
        Path(__file__).parents[1]
        / "material_price_audit"
        / "webapp"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert "q.detail_url || q.url" in html
    assert "待核候选不是正式报价" in html
    assert 'if (r.status === "need_review" && !hasFormal) return "—";' in html
    assert "平台结果逐条核对" in html
    assert "打开供应商报价附件" in html
    assert "预览报价图片" in html
    assert 'class="evidence-preview"' in html
    assert 'id="evidencePreviewImg"' in html
    assert "img.src = url" in html
    assert "btn-preview-evidence" in html
    assert "页面定位：" in html
    assert "按页面定位核对" in html
    assert "无价供应商线索只进入 RFQ" in html
    assert '(r.supplier_list || []).forEach' not in html
    assert 'kind === "supplier"' in html
    assert '"无公开价"' in html
    assert '"价空"' not in html


def test_result_panel_makes_excel_baseline_and_match_conclusions_explicit():
    html = (
        Path(__file__).parents[1]
        / "material_price_audit"
        / "webapp"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert "Excel 报送基准" in html
    assert "报送单价" in html
    assert "识别型号" in html
    assert "Excel 原始规格型号" in html
    assert "系统参考审定价" in html
    assert "只由正式报价生成，待核候选不计入" in html
    assert "Excel 报送基准</div>" in html
    assert "平台来源证据</div>" in html
    assert "核对结论</div>" in html
    assert '_compareRow("材料名称"' in html
    assert '"型号",' in html
    assert '_compareRow("规格参数"' in html
    assert '_compareRow("计价单位"' in html
    assert '_compareRow("价格"' in html
    assert "价格高低只做对比，不替代名称、型号和规格判断" in html
    assert "名称符合" in html
    assert "型号符合" in html
    assert "规格符合" in html
    assert "明确冲突" in html


def test_result_quote_serializer_keeps_price_comparison_evidence():
    quote = Quote(
        rank=1,
        price=80.0,
        platform="guangcai",
        title="网络摄像机 DS-2CD3T46WDV3-I3",
        url="https://example.com/detail",
        vs_submit="below_submit",
        price_anomaly="明显低于报送·请核对单位",
        source_record_id="quote-row-8",
    )

    row = quote_to_result_row(quote, role="formal")

    assert row["vs_submit"] == "below_submit"
    assert row["price_anomaly"] == "明显低于报送·请核对单位"
    assert row["source_record_id"] == "quote-row-8"
