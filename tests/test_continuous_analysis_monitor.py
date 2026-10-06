import csv
import json
from pathlib import Path

import scripts.monitor_continuous_analysis as monitor
from scripts.monitor_continuous_analysis import analyze_batch, complete_batches, merge_candidates, publish, render_status


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_complete_batches_starts_at_three_and_requires_marker(tmp_path: Path):
    for number in (2, 3, 4):
        (tmp_path / f"batch_{number:03d}").mkdir()
    (tmp_path / "batch_002" / "complete.json").write_text("{}", encoding="utf-8")
    (tmp_path / "batch_003" / "complete.json").write_text("{}", encoding="utf-8")
    assert [path.name for path in complete_batches(3, tmp_path)] == ["batch_003"]


def test_merge_candidates_strictly_deduplicates(tmp_path: Path):
    row = {"company": "甲医疗", "acquisition_url": "https://example.com/jobs", "source_row": "1"}
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    output = tmp_path / "merged.csv"
    _write_csv(first, [row])
    _write_csv(second, [row, {**row, "company": "乙医药", "source_row": "2"}])
    rows, companies = merge_candidates([first, second], output)
    assert (rows, companies) == (2, 2)
    assert len(list(csv.DictReader(output.open(encoding="utf-8-sig")))) == 2


def test_dashboard_uses_view_links_for_each_batch(tmp_path: Path):
    batch = tmp_path / "batch_003"
    batch.mkdir()
    for name in ("complete.json", "candidates.csv", "web_report.json"):
        (batch / name).write_text("{}", encoding="utf-8")
    html = render_status({"processed_batches": [3], "next_batch": 4, "summary": {}}, tmp_path)
    assert '/view/data/batches/batch_003/complete.json' in html
    assert '/view/data/batches/batch_003/candidates.csv' in html
    assert "run_wechat" not in html


def test_dashboard_result_links_are_view_routes():
    html = render_status({"processed_batches": [], "next_batch": 3, "summary": {}})
    assert '/view/exports/targets/applicable_companies.csv' in html
    assert '/view/exports/targets/summary.json' in html


def test_publish_mirrors_state_into_dashboard_json(tmp_path: Path):
    state_path = tmp_path / "data" / "subagent4.json"
    slot_state = tmp_path / "data" / "subagent2.json"
    slot_html = tmp_path / "dashboard" / "subagent2.html"
    board_state = tmp_path / "dashboard" / "subagent2.json"
    publish(
        {"status": "waiting", "processed_batches": [3]},
        state_path, slot_state, slot_html, tmp_path / "batches", board_state,
    )
    assert json.loads(board_state.read_text(encoding="utf-8"))["processed_batches"] == [3]


def test_circuit_batch_is_analyzed_with_processed_and_deferred_counts(tmp_path: Path, monkeypatch):
    first = tmp_path / "data" / "first_batch" / "first_batch_candidates.csv"
    batch = tmp_path / "data" / "batches" / "batch_009"
    rows = [
        {"company": "已处理医疗", "acquisition_url": "https://example.com/a", "source_row": "1"},
        {"company": "待续跑医疗", "acquisition_url": "https://mp.weixin.qq.com/s/b", "source_row": "2"},
    ]
    _write_csv(first, rows[:1])
    _write_csv(batch / "candidates.csv", rows[1:])
    (batch / "complete.json").write_text(json.dumps({
        "state": "circuit_open", "source_end_one_based": 2,
        "processed_count": 1, "deferred_count": 1,
    }), encoding="utf-8")

    output = tmp_path / "exports" / "targets"
    output.mkdir(parents=True)
    for name in monitor.OUTPUT_NAMES:
        if name.endswith(".csv"):
            (output / name).write_text("company_id,jd_id\n", encoding="utf-8")
        else:
            (output / name).write_text("{}", encoding="utf-8")

    def fake_update(candidates, complete, database, target, **kwargs):
        merged = list(csv.DictReader(candidates.open(encoding="utf-8-sig")))
        target.mkdir(parents=True)
        for name in monitor.OUTPUT_NAMES:
            if name.endswith(".csv"):
                (target / name).write_text("company_id,jd_id\n", encoding="utf-8")
        summary = {
            "unique_input_companies": len(merged), "analyzed_companies": len(merged),
            "applicable_companies": 0, "verified_companies": 0, "jd_rows": 0,
            "verified_jd_rows": 0, "pending_discovery_companies": 2, "error_companies": 0,
        }
        (target / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return summary

    monkeypatch.setattr(monitor, "update_application_targets", fake_update)
    result = analyze_batch(9, {"processed_batches": [], "total_companies": 2}, root=tmp_path)
    assert result["reason"] == "runner_circuit_open"
    assert result["summary"]["analysis_snapshot_companies"] == 2
    assert result["summary"]["processed_companies"] == 1
    assert result["summary"]["deferred_companies"] == 1
    assert result["summary"]["remaining_companies"] == 1
    assert result["summary"]["processed_batches"] == [9]
