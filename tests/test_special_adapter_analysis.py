import json

from scripts.refresh_special_adapter_analysis import _special_metrics


def test_special_metrics_separates_explicit_targets_from_adjacent_ai():
    report = {
        "state": "completed", "fixed": 1, "added_jobs": 2,
        "by_adapter": {
            "MokahrPublicPortal": {"added_jobs": 0},
            "HotjobPublicPortal": {"added_jobs": 2},
        },
        "items": [{"company": "药企", "source_url": "https://example.test/jobs"}],
    }
    store = {
        "https://example.test/jobs": {"result": {"jobs": [
            {"title": "计算机视觉算法工程师", "location": "上海", "url": "https://example.test/1"},
            {"title": "AI医药研发高级专员", "location": "上海", "url": "https://example.test/2"},
        ]}}
    }

    result = _special_metrics(report, store)

    assert result["target_match_count"] == 1
    assert result["target_matches"][0]["priority"] == 1
    assert result["adjacent_ai_count"] == 1
    assert result["hotjob_added_jobs"] == 2


def test_subagent2_dashboard_links_special_reports():
    from scripts.monitor_batch_analysis import render_status

    html = render_status({"status": "completed", "processed": 40, "summary": {
        "special_adapter": {"mokahr_added_jobs": 50, "hotjob_added_jobs": 252},
    }})

    assert "/view/data/audits/special_adapter_report.json" in html
    assert "/view/data/audits/special_adapter_report.csv" in html
    assert "Mokahr 新增岗位" in html
