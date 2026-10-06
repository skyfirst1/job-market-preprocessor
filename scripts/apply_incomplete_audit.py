from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.analysis.application_targets import (
    COMPANY_COLUMNS,
    JOB_COLUMNS,
    _read_csv,
    _write_csv,
    build_rows,
    load_audit_confirmations,
    load_store_results,
)
from scripts.monitor_batch_analysis import atomic_write, render_status


def audit_evidence(row: dict[str, str]) -> str:
    return "; ".join((
        "review_category=B",
        f"task_id={row.get('task_id', '')}",
        f"rationale={row.get('review_rationale', '')}",
        f"root_cause={row.get('root_cause', '')}",
        f"evidence={row.get('evidence', '')}",
        f"source={row.get('source_url', '')}",
    ))


def merge_rows(existing: list[dict[str, str]], additions: list[dict], key: str) -> tuple[list[dict], int]:
    merged = {row[key]: dict(row) for row in existing}
    new_count = 0
    for row in additions:
        if row[key] not in merged:
            new_count += 1
            merged[row[key]] = dict(row)
        else:
            merged[row[key]].update({name: value for name, value in row.items() if value not in (None, "")})
    return list(merged.values()), new_count


def main() -> None:
    parser = argparse.ArgumentParser(description="仅合并独立审核 B 类且满足既有严格规则的公司与 JD。")
    parser.add_argument("--audit", type=Path, default=Path("data/audits/incomplete_review.csv"))
    parser.add_argument("--master", type=Path, default=Path("data/medical_candidates/first_batch_candidates.csv"))
    parser.add_argument("--database", type=Path, default=Path("data/workstation.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("exports/targets"))
    args = parser.parse_args()

    audit_rows = _read_csv(args.audit)
    b_rows = [row for row in audit_rows if row.get("review_category") == "B"]
    master = _read_csv(args.master)
    selected: list[dict[str, str]] = []
    evidence_by_company: dict[str, str] = {}
    seen_sources: set[tuple[str, str]] = set()
    for audit in b_rows:
        source_rows = {value.strip() for value in (audit.get("source_rows") or "").split(",") if value.strip()}
        for candidate in master:
            if candidate.get("company", "").strip() != audit.get("company", "").strip():
                continue
            if source_rows and candidate.get("source_row", "").strip() not in source_rows:
                continue
            key = (candidate.get("company", ""), candidate.get("source_row", ""))
            if key not in seen_sources:
                selected.append(candidate)
                seen_sources.add(key)
            evidence_by_company[candidate.get("company", "").strip()] = audit_evidence(audit)

    companies, jobs = build_rows(
        selected, load_store_results(args.database), load_audit_confirmations(args.audit)
    )
    eligible = [row for row in companies if row["target_status"] == "可投候选"]
    eligible_ids = {row["company_id"] for row in eligible}
    jobs = [row for row in jobs if row["company_id"] in eligible_ids]
    for row in eligible:
        row["audit_evidence"] = evidence_by_company.get(row["company"], "")
    for row in jobs:
        row["audit_evidence"] = evidence_by_company.get(row["company"], "")

    output = args.output
    existing_companies = _read_csv(output / "applicable_companies.csv")
    existing_jobs = _read_csv(output / "job_targets.csv")
    merged_companies, new_companies = merge_rows(existing_companies, eligible, "company_id")
    merged_jobs, new_jobs = merge_rows(existing_jobs, jobs, "jd_id")
    merged_companies.sort(key=lambda row: (int(row.get("best_priority") or 99), row.get("company", "")))
    merged_jobs.sort(key=lambda row: (int(row.get("priority") or 99), row.get("company", ""), row.get("role_title", "")))
    _write_csv(output / "applicable_companies.csv", COMPANY_COLUMNS, merged_companies)
    _write_csv(output / "job_targets.csv", JOB_COLUMNS, merged_jobs)
    verified_companies = [row for row in merged_companies if row.get("evidence_level") != "csv_declared"]
    verified_jobs = [row for row in merged_jobs if row.get("evidence_level") != "csv_declared"]
    _write_csv(output / "verified_companies.csv", COMPANY_COLUMNS, verified_companies)
    _write_csv(output / "verified_job_targets.csv", JOB_COLUMNS, verified_jobs)

    summary_path = output / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(
        generated_at=datetime.now(timezone.utc).isoformat(),
        applicable_companies=len(merged_companies),
        verified_companies=len(verified_companies),
        jd_rows=len(merged_jobs),
        verified_jd_rows=len(verified_jobs),
        evidence_backed_jds=len(merged_jobs),
    )
    summary["audit"] = {
        "tasks_reviewed": len(audit_rows),
        "b_reviewed": len(b_rows),
        "mapped_b_companies": len({row.get('company', '') for row in selected}),
        "eligible_companies": len(eligible),
        "eligible_jds": len(jobs),
        "new_applicable_companies": new_companies,
        "new_jds": new_jobs,
        "evidence_enriched_companies": len(eligible),
        "evidence_enriched_jds": len(jobs),
        "excluded_non_b": len(audit_rows) - len(b_rows),
        "excluded_categories": {category: sum(row.get("review_category") == category for row in audit_rows) for category in ("A", "C", "D")},
        "sources": [str((ROOT / "data" / "audits" / name).resolve()) for name in (
            "incomplete_review.csv", "incomplete_review.json", "summary.json", "summary.md"
        )],
    }
    atomic_write(summary_path, json.dumps(summary, ensure_ascii=False, indent=2))

    state_path = ROOT / "data" / "agent_reports" / "subagent2.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(status="completed", phase="独立审核 B 类严格增量合并完成", summary=summary,
                 updated_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2))
    atomic_write(ROOT / "exports" / "operations_dashboard" / "subagent2.html", render_status(state))
    print(json.dumps(summary["audit"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
