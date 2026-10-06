import json
from pathlib import Path
import pytest
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import urlopen

from scripts.serve_operations_dashboard import (
    DashboardHandler,
    DashboardServer,
    _csv_preview,
    _column_kind,
    _html_page,
    build_catalog,
)


def test_dashboard_preview_links_use_current_tab():
    html = Path("exports/operations_dashboard/index.html").read_text(encoding="utf-8")
    assert 'target="_blank"' not in html
    assert '总候选 <strong id="total-count">176</strong>' in html
    assert '累计已处理 <strong id="processed-count">171</strong>' in html
    assert 'Deferred <strong id="deferred-count">5</strong>' in html
    assert "/raw/exports/targets/summary.json" in html


def test_delivery_tree_only_contains_canonical_results_status_and_final_audits():
    catalog, groups = build_catalog()
    assert [group["name"] for group in groups] == ["核心目标结果", "当前运行状态", "最终审计"]
    visible = {
        child["name"]
        for group in groups
        for child in group["children"]
    }
    assert visible == {
        "summary.json",
        "applicable_companies.csv",
        "job_targets.csv",
        "verified_companies.csv",
        "verified_job_targets.csv",
        "pending_discovery_companies.csv",
        "error_companies.csv",
        "subagent1.json",
        "subagent2.json",
        "final_acceptance_fix.json",
        "batch009_target_analysis.md",
        "batch009_target_consistency.json",
    }
    assert "data/audits/continuous_adapter_report.json" in catalog
    assert "continuous_adapter_report.json" not in visible


def test_csv_preview_has_search_sort_counts_expansion_and_safe_links(tmp_path):
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text(
        "name,url,notes\n"
        "测试,https://example.com/job?id=1,"
        + "很长的岗位说明" * 40
        + "\n危险,javascript:alert(1),<script>alert(1)</script> https://safe.example/x\n",
        encoding="utf-8",
    )
    preview = _csv_preview(csv_path, "data/sample.csv")
    assert 'id="csv-search"' in preview
    assert "显示 2 / 2 行" in preview
    assert 'data-column="0"' in preview
    assert "sortColumn" in preview
    assert "<details>" in preview
    assert 'href="https://example.com/job?id=1" target="_blank" rel="noopener noreferrer"' in preview
    assert 'href="https://safe.example/x" target="_blank" rel="noopener noreferrer"' in preview
    assert 'href="javascript:' not in preview
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in preview
    assert 'href="/raw/data/sample.csv"' in preview


def test_csv_preview_hides_detail_and_machine_id_columns_without_removing_search_data(tmp_path):
    csv_path = tmp_path / "targets.csv"
    csv_path.write_text(
        "company,role_title,location,company_id,jd_id,evidence,requirements,source_url,updated_at\n"
        "测试公司,大模型工程师,北京,company-123,jd-456,模型证据,熟悉深度学习,https://example.com/job,2026-10-06\n",
        encoding="utf-8",
    )

    preview = _csv_preview(csv_path, "exports/targets/job_targets.csv")

    assert 'id="show-detail-columns"' in preview
    assert "显示详情列（6）" in preview
    assert '<th scope="col" data-column="0" class="column-company"' in preview
    assert '<th scope="col" data-column="1" class="column-role"' in preview
    assert '<th scope="col" data-column="2" class="column-compact"' in preview
    assert '<th scope="col" data-column="3" class="detail-column"' in preview
    assert '<th scope="col" data-column="4" class="detail-column"' in preview
    assert 'class="detail-column" data-value="company-123"' in preview
    assert 'data-search="测试公司 大模型工程师 北京 company-123 jd-456 模型证据 熟悉深度学习 https://example.com/job 2026-10-06"' in preview
    assert "table.classList.toggle('show-details', showDetails.checked)" in preview


def test_csv_preview_styles_keep_readable_type_and_narrow_screen_layout():
    page = _html_page("preview", '<table id="csv-table"></table>').decode("utf-8")

    assert "table{border-collapse:separate" in page
    assert "font-size:14px" in page
    assert ".detail-column{display:none}" in page
    assert "table.show-details .detail-column{display:table-cell}" in page
    assert "@media (max-width:700px)" in page


def test_target_column_policy_keeps_action_links_visible_and_source_details_hidden():
    for name in ("company_id", "jd_id", "source_pool", "source_row", "source_url", "page_title", "fetch_status"):
        assert _column_kind(name) == "detail-column"
    assert _column_kind("structured_job_title") == "detail-column"
    assert _column_kind("application_url") == ""
    assert _column_kind("structured_job_url") == ""


def test_target_preview_hides_applied_companies_by_default(tmp_path, monkeypatch):
    history = tmp_path / "工作.md"
    history.write_text(
        "| company | 状态 |\n| --- | --- |\n| 开立 | DL |\n| 贝壳 | 还没投 |\n",
        encoding="utf-8",
    )
    csv_path = tmp_path / "targets.csv"
    csv_path.write_text("company,role\n开立医疗,算法工程师\n贝壳找房,Agent工程师\n", encoding="utf-8")
    monkeypatch.setattr("jobprep.runner.dashboard_server.APPLICATION_HISTORY", history)

    preview = _csv_preview(csv_path, "exports/targets/applicable_companies.csv")

    assert 'id="show-applied"' in preview
    assert "显示已投（1）" in preview
    assert 'data-applied="true"' in preview
    assert 'data-applied="false"' in preview
    assert "显示 1 / 2 行" in preview


def test_preview_page_and_raw_endpoint_are_inline():
    catalog, groups = build_catalog()
    server = DashboardServer(("127.0.0.1", 0), DashboardHandler)
    server.catalog, server.groups = catalog, groups
    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        relative = "exports/targets/summary.json"
        with urlopen(f"http://127.0.0.1:{server.server_port}/view/{quote(relative, safe='/')}") as response:
            body = response.read().decode("utf-8")
            assert response.headers.get_content_type() == "text/html"
            assert "返回看板" in body
            assert "<pre>" in body
        with urlopen(f"http://127.0.0.1:{server.server_port}/raw/{quote(relative, safe='/')}") as response:
            assert response.headers.get_content_type() == "application/json"
            assert isinstance(json.loads(response.read()), dict)

        csv_relative = "exports/targets/job_targets.csv"
        with urlopen(f"http://127.0.0.1:{server.server_port}/view/{quote(csv_relative, safe='/')}") as response:
            csv_body = response.read().decode("utf-8")
            assert response.headers.get_content_type() == "text/html"
            assert 'id="csv-search"' in csv_body
            assert 'id="csv-table"' in csv_body
            assert 'target="_blank" rel="noopener noreferrer"' in csv_body

        hidden_relative = "data/audits/continuous_adapter_report.json"
        with urlopen(f"http://127.0.0.1:{server.server_port}/view/{quote(hidden_relative, safe='/')}") as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_summary_remediation_company_names_round_trip_strict_utf8():
    raw = Path("exports/targets/summary.json").read_bytes()
    decoded = raw.decode("utf-8", errors="strict")
    summary = json.loads(decoded)

    assert summary["remediation"]["fixed_companies"] == [
        "巨鲨医疗",
        "优宁维",
        "中润医药(集团)",
        "勃林格殷格翰",
        "爱迪特",
    ]
    assert "\ufffd" not in decoded


def test_preview_rejects_path_traversal():
    catalog, groups = build_catalog()
    server = DashboardServer(("127.0.0.1", 0), DashboardHandler)
    server.catalog, server.groups = catalog, groups
    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in ("/view/../.env", "/view/%2e%2e/.env", "/view/data%5c..%5c.env"):
            with pytest.raises(HTTPError) as error:
                urlopen(f"http://127.0.0.1:{server.server_port}{path}")
            assert error.value.code in {403, 404}
    finally:
        server.shutdown()
        server.server_close()
