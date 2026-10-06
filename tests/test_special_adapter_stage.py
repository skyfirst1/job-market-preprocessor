import csv
import json

from scripts.run_special_adapter_stage import (REQUIRED_COMPANIES, assert_clean_report,
                                               company_map_from_raw, finalize_files,
                                               finalize_report, looks_mojibake)


def test_finalize_report_restores_names_and_marks_51job_blocked():
    report = finalize_report({
        "state": "running",
        "items": [{"task_id": "2a6fcbd3ddf304830ef04123", "company": "bad",
                   "adapter": "MokahrPublicPortal", "outcome": "fixed", "added_jobs": 45}],
        "blocked": [{"task_id": "52107deea3d314934348d5b0", "company": "bad",
                     "source_url": "https://campus.51job.com/Innovent2027/index2.html"}],
    })
    assert report["state"] == "completed"
    assert report["items"][0]["company"] == "九州通医药"
    assert report["blocked"][0]["company"] == "信达生物"
    assert report["blocked"][0]["reason"] == "adapter_not_implemented_in_stage"
    assert "innoventbio.zhiye.com" in report["blocked"][0]["next_interface_direction"]
    assert report["by_adapter"]["MokahrPublicPortal"]["added_jobs"] == 45


def test_finalize_files_are_utf8_and_completed(tmp_path):
    json_path = tmp_path / "report.json"
    csv_path = tmp_path / "report.csv"
    items = [{"task_id": task_id, "company": "bad", "source_url": f"https://example.test/{index}",
              "adapter": "HotjobPublicPortal", "outcome": "fixed", "added_jobs": 1}
             for index, task_id in enumerate((
                 "aa47b5f48ec21d18c0ff0adb", "2a6fcbd3ddf304830ef04123",
                 "d7cf6df6791ef0e582847414", "366d752a69814f11922e6c60"))]
    json_path.write_text(json.dumps({"state": "running", "items": items, "blocked": []}),
                         encoding="utf-8")
    report = finalize_files(json_path, csv_path)
    assert report["state"] == "completed"
    decoded = json_path.read_text(encoding="utf-8")
    assert "先声药业集团" in decoded
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        assert {row["company"] for row in csv.DictReader(handle)} == REQUIRED_COMPANIES


def test_finalize_marks_unrecovered_hotjob_root_blocked():
    report = finalize_report({"state": "running", "items": [{
        "task_id": "aa47b5f48ec21d18c0ff0adb", "company": "bad",
        "adapter": "HotjobPublicPortal", "outcome": "unchanged", "added_jobs": 0,
    }], "blocked": []})
    item = report["items"][0]
    assert item["company"] == "信立泰药业" and item["outcome"] == "blocked"
    assert item["reason"] == "hotjob_suite_route_not_stable_from_root"


def test_company_map_uses_canonical_utf8_csv_and_mojibake_checks(tmp_path):
    path = tmp_path / "companies.csv"
    path.write_text("公司名称,公告链接,投递链接\n"
                    "九州通医药,,https://example.test/jobs\n", encoding="utf-8-sig")
    mapping = company_map_from_raw(path)
    assert mapping["https://example.test/jobs"] == "九州通医药"
    assert not looks_mojibake("仲景宛西制药")
    assert looks_mojibake("����")
    assert looks_mojibake("ä¿¡ç«‹æ³°è¯ä¸š")
