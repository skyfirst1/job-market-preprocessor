from pathlib import Path

from jobprep.app.fileio import read_json_object, write_csv_atomic, write_json_atomic


def test_atomic_artifact_helpers_round_trip(tmp_path: Path):
    json_path = tmp_path / "nested" / "report.json"
    csv_path = tmp_path / "nested" / "report.csv"
    write_json_atomic(json_path, {"status": "ok"})
    write_csv_atomic(csv_path, [{"name": "甲", "url": "https://example.com"}], ("name", "url"))

    assert read_json_object(json_path) == {"status": "ok"}
    assert csv_path.read_text(encoding="utf-8-sig").splitlines() == [
        "name,url",
        "甲,https://example.com",
    ]
    assert not list((tmp_path / "nested").glob(".*.tmp"))


def test_legacy_script_imports_alias_production_modules():
    import scripts.analyze_batch009_targets as legacy_batch009_analysis
    import scripts.monitor_continuous_analysis as legacy_analysis
    import scripts.recover_batch009_image_gaps as legacy_recovery
    import scripts.report_agent_status as legacy_status
    import scripts.run_continuous_medical_batches as legacy_batches
    import scripts.serve_operations_dashboard as legacy_dashboard

    assert legacy_batch009_analysis.__name__ == "jobprep.analysis.batch009_targets"
    assert legacy_analysis.__name__ == "jobprep.runner.continuous_analysis"
    assert legacy_recovery.__name__ == "jobprep.app.batch009_image_recovery"
    assert legacy_status.__name__ == "jobprep.runner.dashboard_status"
    assert legacy_batches.__name__ == "jobprep.runner.continuous_batches"
    assert legacy_dashboard.__name__ == "jobprep.runner.dashboard_server"


def test_production_modules_do_not_import_scripts_package():
    root = Path(__file__).resolve().parents[1] / "jobprep"
    migrated = (
        root / "analysis" / "batch009_targets.py",
        root / "app" / "batch009_image_recovery.py",
        root / "runner" / "continuous_analysis.py",
        root / "runner" / "continuous_batches.py",
        root / "runner" / "dashboard_server.py",
        root / "runner" / "dashboard_status.py",
    )
    for path in migrated:
        source = path.read_text(encoding="utf-8")
        assert "from scripts." not in source
        assert "import scripts." not in source
