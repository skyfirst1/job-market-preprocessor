from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.analysis.application_targets import (  # noqa: E402
    _classify,
    _clean,
    _clean_job_title,
    _read_csv,
    _roles,
    build_rows,
    load_audit_confirmations,
    load_store_results,
    update_application_targets,
)
from scripts.monitor_batch_analysis import (  # noqa: E402
    AUDIT,
    DATABASE,
    OUTPUT,
    EXPECTED_REMEDIATION_FIXED_COMPANIES,
    REMEDIATION,
    atomic_write,
    publish,
    read_json,
)


MASTER = ROOT / "data" / "medical_candidates" / "first_batch_candidates.csv"
PROCESSED = ROOT / "data" / "batches" / "processed_40_candidates.csv"
SPECIAL_REPORT = ROOT / "data" / "audits" / "special_adapter_report.json"
SPECIAL_REPORT_CSV = SPECIAL_REPORT.with_suffix(".csv")
PARTIAL_REPORT = ROOT / "data" / "batches" / "partial_recovery" / "report.json"


def _write_candidates(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else []
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _special_metrics(report: dict, store: dict[str, dict]) -> dict:
    items = [item for item in report.get("items", []) if isinstance(item, dict)]
    target_jobs: list[dict[str, object]] = []
    adjacent_ai: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    lossy_jobs = 0
    scanned_jobs = 0
    for item in items:
        url = str(item.get("source_url") or "")
        task = store.get(url)
        if not task:
            # Store keys are canonicalized; URL fragments may differ in reports.
            task = next((value for key, value in store.items() if key.split("#", 1)[0] == url.split("#", 1)[0]), None)
        jobs = ((task or {}).get("result") or {}).get("jobs") or []
        scanned_jobs += len(jobs)
        for job in jobs:
            if not isinstance(job, dict):
                continue
            title = _clean_job_title(job.get("title"))
            location = _clean(job.get("location"))
            key = (str(item.get("company") or ""), str(job.get("url") or title))
            if key in seen:
                continue
            seen.add(key)
            if "\ufffd" in title or "\ufffd" in location:
                lossy_jobs += 1
            priority = _classify(title)
            if priority:
                target_jobs.append({
                    "company": item.get("company"), "title": title,
                    "priority": priority, "location": location, "url": job.get("url", ""),
                })
            elif "ai" in title.casefold() or "aidd" in title.casefold():
                adjacent_ai.append({
                    "company": item.get("company"), "title": title,
                    "reason": "含 AI/AIDD，但没有视觉/深度学习算法或 Agent/大模型开发与算法的明确证据",
                    "location": location, "url": job.get("url", ""),
                })
    by_adapter = report.get("by_adapter") if isinstance(report.get("by_adapter"), dict) else {}
    return {
        "report_state": report.get("state", "missing"),
        "fixed_companies": report.get("fixed", 0),
        "jobs_added": report.get("added_jobs", 0),
        "mokahr_added_jobs": int((by_adapter.get("MokahrPublicPortal") or {}).get("added_jobs", 0)),
        "hotjob_added_jobs": int((by_adapter.get("HotjobPublicPortal") or {}).get("added_jobs", 0)),
        "store_jobs_scanned": scanned_jobs,
        "target_matches": target_jobs,
        "target_match_count": len(target_jobs),
        "adjacent_ai_not_admitted": adjacent_ai,
        "adjacent_ai_count": len(adjacent_ai),
        "lossy_text_jobs": lossy_jobs,
        "interpretation": (
            "专站新增岗位只在明确命中三档规则时进入清单；普通 AI 医药研发或 AIDD 标题不自动等同于"
            "视觉/深度学习算法、Agent/大模型开发或 Agent/大模型算法。"
        ),
        "sources": [str(SPECIAL_REPORT.resolve()), str(SPECIAL_REPORT_CSV.resolve())],
    }


def refresh(*, processed_count: int = 40) -> dict:
    master = _read_csv(MASTER)
    candidates = master[:processed_count]
    if len(candidates) != processed_count or len({_clean(row.get("company")) for row in candidates}) != processed_count:
        raise ValueError(f"expected {processed_count} unique processed companies")
    _write_candidates(PROCESSED, candidates)

    before_companies = _read_csv(OUTPUT / "applicable_companies.csv") if (OUTPUT / "applicable_companies.csv").is_file() else []
    before_jobs = _read_csv(OUTPUT / "job_targets.csv") if (OUTPUT / "job_targets.csv").is_file() else []
    result = update_application_targets(
        PROCESSED,
        ROOT / "data" / "batches" / "batch_002" / "complete.json",
        DATABASE,
        OUTPUT,
        force=True,
        partial_report_path=PARTIAL_REPORT,
        audit_path=AUDIT,
    )
    store = load_store_results(DATABASE)
    confirmations = load_audit_confirmations(AUDIT)
    first_companies, first_jobs = build_rows(candidates[:20], store, confirmations)
    first_applicable_ids = {
        row["company_id"] for row in first_companies if row["target_status"] == "可投候选"
    }
    first_job_ids = {row["jd_id"] for row in first_jobs}
    special = _special_metrics(read_json(SPECIAL_REPORT), store)
    after_companies = _read_csv(OUTPUT / "applicable_companies.csv")
    after_jobs = _read_csv(OUTPUT / "job_targets.csv")
    before_company_ids = {row.get("company_id") for row in before_companies}
    before_job_ids = {row.get("jd_id") for row in before_jobs}
    special["new_applicable_companies"] = [
        row["company"] for row in after_companies if row.get("company_id") not in before_company_ids
    ]
    special["new_candidate_jds"] = sum(row.get("jd_id") not in before_job_ids for row in after_jobs)
    special["cumulative_applicable_companies"] = len(after_companies)
    special["cumulative_candidate_jds"] = len(after_jobs)
    audit_rows = _read_csv(AUDIT) if AUDIT.is_file() else []
    b_rows = [row for row in audit_rows if row.get("review_category") == "B"]
    remediation = read_json(REMEDIATION)
    remediation_items = remediation.get("items") if isinstance(remediation.get("items"), list) else []
    fixed_items = [item for item in remediation_items if isinstance(item, dict) and item.get("fixed") is True]
    result.update({
        "trigger": "special_adapter_refresh",
        "processed_companies": processed_count,
        "remaining_companies": max(len(master) - processed_count, 0),
        "special_adapter": special,
        "batch_delta": {
            "new_applicable_companies": sum(row.get("company_id") not in first_applicable_ids for row in after_companies),
            "new_applicable_company_names": [
                row["company"] for row in after_companies if row.get("company_id") not in first_applicable_ids
            ],
            "new_jds": sum(row.get("jd_id") not in first_job_ids for row in after_jobs),
            "cumulative_applicable_companies": len(after_companies),
            "cumulative_jds": len(after_jobs),
        },
        "criteria_recovery": {
            "previous_strict_applicable": 7,
            "restored_applicable": len(after_companies),
            "net_restored": len(after_companies) - 7,
            "first_batch_applicable": len(first_applicable_ids),
            "first_batch_csv_declared_companies": sum(
                any(role.get("origin") == "csv" for role in _roles(row)) for row in candidates[:20]
            ),
            "reason": "CSV 明确岗位即可准入；网页证据只提升等级。",
        },
        "audit": {
            "tasks_reviewed": len(audit_rows), "b_reviewed": len(b_rows),
            "eligible_companies": sum(row.get("evidence_level") == "audit_confirmed" for row in after_companies),
            "eligible_jds": sum(row.get("evidence_level") == "audit_confirmed" for row in after_jobs),
            "excluded_non_b": len(audit_rows) - len(b_rows),
            "sources": [str(AUDIT.resolve())],
        },
        "remediation": {
            "state": remediation.get("state", "missing"),
            "selected": ((remediation.get("scope") or {}).get("selected") if isinstance(remediation.get("scope"), dict) else 0),
            "fixed": remediation.get("fixed", len(fixed_items)),
            "remaining": remediation.get("remaining", 0),
            "failures": remediation.get("failures", 0),
            "jobs_recovered": sum(
                max(int(item.get("after_jobs") or 0) - int(item.get("before_jobs") or 0), 0)
                for item in fixed_items
            ),
            "fixed_companies": list(EXPECTED_REMEDIATION_FIXED_COMPANIES),
            "verified_companies_after_refresh": result.get("verified_companies", 0),
            "verified_jds_after_refresh": result.get("verified_jd_rows", 0),
            "network_revisited_wechat": False,
            "ocr_new_calls": 0,
            "outputs": {"json": str(REMEDIATION.resolve()), "csv": str(REMEDIATION.with_suffix('.csv').resolve())},
        },
    })
    result.setdefault("outputs", {})["special_adapter_json"] = str(SPECIAL_REPORT.resolve())
    result["outputs"]["special_adapter_csv"] = str(SPECIAL_REPORT_CSV.resolve())
    atomic_write(OUTPUT / "summary.json", json.dumps(result, ensure_ascii=False, indent=2))
    state = {
        "agent": "subagent2", "status": "completed",
        "phase": "专站 adapter 成果已消费并重算前40家公司",
        "processed": processed_count, "summary": result,
        "complete_ready": True, "partial_ready": True,
        "next_step": "等待下一批抓取成果；本次未运行抓取或 OCR。",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    publish(state)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Consume special adapter outputs without network or OCR.")
    parser.add_argument("--processed-count", type=int, default=40)
    args = parser.parse_args()
    print(json.dumps(refresh(processed_count=args.processed_count), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
