from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts.merge_target_scopes import merge_target_scopes


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _scope_dir(root: Path, company: str, industry: str) -> Path:
    company_row = {"company_id": "same-company-id", "company": company, "industry": industry}
    job_row = {
        "jd_id": "same-job-id",
        "company_id": "same-company-id",
        "company": company,
        "role_title": "AI算法工程师",
        "industry": "",
    }
    _write(root / "applicable_companies.csv", [company_row])
    _write(root / "verified_companies.csv", [company_row])
    _write(root / "pending_discovery_companies.csv", [])
    _write(root / "error_companies.csv", [])
    _write(root / "job_targets.csv", [job_row])
    _write(root / "verified_job_targets.csv", [job_row])
    return root


def test_merge_keeps_same_stable_ids_distinct_by_scope_and_verified_subsets(tmp_path: Path):
    medical = _scope_dir(tmp_path / "medical", "同名集团", "医疗器械")
    manufacturing = _scope_dir(tmp_path / "manufacturing", "同名集团", "装备制造")
    output = tmp_path / "combined"
    input_snapshots = {
        path: path.read_bytes()
        for directory in (medical, manufacturing)
        for path in directory.iterdir()
    }

    summary = merge_target_scopes(
        {"medical": medical, "manufacturing": manufacturing},
        output,
    )

    companies = list(csv.DictReader((output / "applicable_companies.csv").open(encoding="utf-8-sig")))
    jobs = list(csv.DictReader((output / "job_targets.csv").open(encoding="utf-8-sig")))
    verified_companies = list(csv.DictReader((output / "verified_companies.csv").open(encoding="utf-8-sig")))
    verified_jobs = list(csv.DictReader((output / "verified_job_targets.csv").open(encoding="utf-8-sig")))

    assert {(row["screening_scope"], row["company_id"]) for row in companies} == {
        ("medical", "same-company-id"),
        ("manufacturing", "same-company-id"),
    }
    assert {(row["screening_scope"], row["industry"]) for row in jobs} == {
        ("medical", "医疗器械"),
        ("manufacturing", "装备制造"),
    }
    assert {(row["screening_scope"], row["company_id"]) for row in verified_companies} <= {
        (row["screening_scope"], row["company_id"]) for row in companies
    }
    assert {(row["screening_scope"], row["jd_id"]) for row in verified_jobs} <= {
        (row["screening_scope"], row["jd_id"]) for row in jobs
    }
    assert summary["by_scope"]["medical"]["jd_rows"] == 1
    assert summary["by_scope"]["manufacturing"]["jd_rows"] == 1
    assert json.loads((output / "summary.json").read_text(encoding="utf-8"))["screening_scope"] == "multiple"
    assert all(path.read_bytes() == content for path, content in input_snapshots.items())


def test_merge_refuses_to_write_inside_an_input_directory(tmp_path: Path):
    medical = _scope_dir(tmp_path / "medical", "甲医疗", "医疗")

    with pytest.raises(ValueError, match="independent"):
        merge_target_scopes({"medical": medical}, medical / "combined")


def test_merge_rejects_verified_job_not_in_full_job_table(tmp_path: Path):
    medical = _scope_dir(tmp_path / "medical", "甲医疗", "医疗")
    _write(medical / "job_targets.csv", [{
        "jd_id": "different-job-id", "company_id": "same-company-id", "company": "甲医疗"
    }])

    with pytest.raises(ValueError, match="verified_job_targets"):
        merge_target_scopes({"medical": medical}, tmp_path / "combined")
