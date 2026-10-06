from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPANY_FILE = "applicable_companies.csv"
JOB_FILE = "job_targets.csv"
VERIFIED_COMPANY_FILE = "verified_companies.csv"
VERIFIED_JOB_FILE = "verified_job_targets.csv"


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def stable_id(*parts: str) -> str:
    value = "\x1f".join(clean(part).casefold() for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def normalize_url(value: str) -> str:
    return clean(value).rstrip("/")


def load_store_jobs(database: Path) -> dict[str, dict[str, Any]]:
    import sqlite3

    jobs: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(database, timeout=30) as connection:
        for (raw,) in connection.execute("SELECT result_json FROM tasks WHERE result_json IS NOT NULL"):
            try:
                result = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            for job in result.get("jobs", []) if isinstance(result, dict) else []:
                if not isinstance(job, dict):
                    continue
                url = normalize_url(job.get("url", ""))
                if url:
                    jobs[url] = job
    return jobs


def company_row(
    candidate: dict[str, str],
    delta_jobs: list[dict[str, Any]],
    existing: dict[str, str] | None,
) -> dict[str, Any]:
    row = dict(existing or {})
    company = clean(candidate["company"])
    row.update(
        company_id=stable_id(company),
        company=company,
        enterprise_nature=clean(candidate.get("enterprise_nature")),
        ownership_status=clean(candidate.get("ownership_status")) or "非国企（CSV标注）",
        ownership_confidence="medium",
        ownership_evidence=(
            f"企业性质={clean(candidate.get('enterprise_nature')) or '未注明'}; "
            f"预筛判断={clean(candidate.get('ownership_status')) or '非国企（CSV标注）'}"
        ),
        industry=clean(candidate.get("industry")),
        best_priority=min(int(job["priority"]) for job in delta_jobs),
        matched_role_count=len(delta_jobs),
        graduation_year=clean(candidate.get("graduation_year")),
        location=clean(candidate.get("location")),
        deadline=clean(candidate.get("deadline")),
        application_url=clean(candidate.get("application_url")),
        announcement_url=clean(candidate.get("announcement_url")),
        acquisition_url=clean(candidate.get("acquisition_url")),
        source_pool=clean(candidate.get("source_pool")),
        source_row=clean(candidate.get("source_row")),
        target_status="可投候选",
        evidence_level="audit_confirmed",
        verification_status="独立审核已确认",
        fetch_status=clean((existing or {}).get("fetch_status")) or "partial",
        fetch_updated_at=clean((existing or {}).get("fetch_updated_at")),
        structured_jobs_count=clean((existing or {}).get("structured_jobs_count")),
        evidence_summary=(
            f"recovered_jobs_target_delta; CSV第{clean(candidate.get('source_row')) or '?'}行; "
            f"delta确认岗位={len(delta_jobs)}"
        ),
        audit_evidence=(
            "source=data/audits/recovered_jobs_target_delta.json; "
            "screening_skill=.codex/skills/medical-ai-job-screening/SKILL.md; "
            "generic_image_processing_requires_affirmative_ai_evidence"
        ),
        uncertainty="",
    )
    return row


def job_row(
    item: dict[str, Any],
    candidate: dict[str, str],
    store_job: dict[str, Any],
) -> dict[str, Any]:
    company = clean(item["company"])
    title = clean(item["job_title"])
    url = clean(item["job_url"])
    evidence = clean(item.get("role_evidence"))
    return {
        "jd_id": stable_id(company, title, url),
        "company_id": stable_id(company),
        "company": company,
        "priority": int(item["priority"]),
        "priority_label": clean(item["category"]),
        "role_title": title,
        "jd_status": "独立审核已确认",
        "details_verified": "true",
        "evidence_level": "audit_confirmed",
        "verification_status": "独立审核已确认",
        "structured_job_title": clean(store_job.get("title")) or title,
        "structured_job_url": url,
        "description": clean(store_job.get("description")) or evidence,
        "requirements": clean(store_job.get("requirements")),
        "location": clean(item.get("location")),
        "graduation_year": clean(candidate.get("graduation_year")),
        "deadline": clean(candidate.get("deadline")),
        "application_url": clean(candidate.get("application_url")),
        "source_url": url,
        "source_pool": clean(candidate.get("source_pool")),
        "source_row": clean(candidate.get("source_row")),
        "fetch_status": "partial",
        "fetch_updated_at": "",
        "evidence": (
            f"evidence_level=audit_confirmed; role_origin=recovered_structured_job; "
            f"structured_job_id={clean(item.get('structured_job_id'))}; role_evidence={evidence}"
        ),
        "audit_evidence": (
            f"source=data/audits/recovered_jobs_target_delta.json; "
            f"source_task_id={clean(item.get('source_task_id'))}; batch={clean(item.get('batch'))}; "
            f"decision={clean(item.get('decision_reason'))}"
        ),
        "uncertainty": "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge the audited recovered-job target delta locally.")
    parser.add_argument("--delta", type=Path, default=ROOT / "data/audits/recovered_jobs_target_delta.json")
    parser.add_argument("--candidates", type=Path, default=ROOT / "data/medical_candidates/first_batch_candidates.csv")
    parser.add_argument("--database", type=Path, default=ROOT / "data/workstation.sqlite3")
    parser.add_argument("--output", type=Path, default=ROOT / "exports/targets")
    args = parser.parse_args()

    delta = json.loads(args.delta.read_text(encoding="utf-8"))
    included = [item for item in delta.get("included_jobs", []) if item.get("should_include") is True]
    if len(included) != 5:
        raise RuntimeError(f"Expected exactly 5 included delta jobs, found {len(included)}")
    if any(clean(item.get("exclusion_code")) == "image_processing_without_ai_evidence" for item in included):
        raise RuntimeError("Generic image-processing exclusion cannot be merged as a target")

    candidates = read_csv(args.candidates)
    candidate_by_company = {clean(row.get("company")): row for row in candidates}
    missing = sorted({clean(item["company"]) for item in included} - candidate_by_company.keys())
    if missing:
        raise RuntimeError(f"Missing candidate metadata: {missing}")

    company_fields = list(read_csv(args.output / COMPANY_FILE)[0].keys())
    job_fields = list(read_csv(args.output / JOB_FILE)[0].keys())
    old_companies = read_csv(args.output / COMPANY_FILE)
    old_jobs = read_csv(args.output / JOB_FILE)
    store_jobs = load_store_jobs(args.database)

    delta_urls = {normalize_url(item["job_url"]) for item in included}
    retained_jobs: list[dict[str, Any]] = []
    removed_duplicate_rows = 0
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()
    for row in old_jobs:
        # Entry-page source URLs are legitimately shared by several CSV-declared roles.
        # Only a concrete structured-job detail URL is a global JD identity.
        url = normalize_url(row.get("structured_job_url") or "")
        if url in delta_urls:
            removed_duplicate_rows += 1
            continue
        if (url and url in seen_urls) or row.get("jd_id") in seen_ids:
            removed_duplicate_rows += 1
            continue
        retained_jobs.append(row)
        if url:
            seen_urls.add(url)
        seen_ids.add(row.get("jd_id", ""))

    additions = [
        job_row(item, candidate_by_company[clean(item["company"])], store_jobs.get(normalize_url(item["job_url"]), {}))
        for item in included
    ]
    merged_jobs = retained_jobs + additions
    merged_jobs.sort(key=lambda row: (int(row.get("priority") or 99), row.get("company", ""), row.get("role_title", "")))

    delta_by_company: dict[str, list[dict[str, Any]]] = {}
    for item in included:
        delta_by_company.setdefault(clean(item["company"]), []).append(item)
    existing_by_name = {clean(row.get("company")): row for row in old_companies}
    updated_by_id = {row["company_id"]: dict(row) for row in old_companies}
    for company, items in delta_by_company.items():
        candidate = candidate_by_company[company]
        updated = company_row(candidate, items, existing_by_name.get(company))
        updated_by_id[updated["company_id"]] = updated

    job_company_ids = {row["company_id"] for row in merged_jobs}
    merged_companies = [row for key, row in updated_by_id.items() if key in job_company_ids]
    for row in merged_companies:
        company_jobs = [job for job in merged_jobs if job["company_id"] == row["company_id"]]
        row["matched_role_count"] = len(company_jobs)
        row["best_priority"] = min(int(job["priority"]) for job in company_jobs)
    merged_companies.sort(key=lambda row: (int(row.get("best_priority") or 99), row.get("company", "")))

    verified_jobs = [row for row in merged_jobs if row.get("evidence_level") != "csv_declared"]
    verified_company_ids = {row["company_id"] for row in verified_jobs}
    verified_companies = [row for row in merged_companies if row["company_id"] in verified_company_ids]

    write_csv(args.output / COMPANY_FILE, company_fields, merged_companies)
    write_csv(args.output / JOB_FILE, job_fields, merged_jobs)
    write_csv(args.output / VERIFIED_COMPANY_FILE, company_fields, verified_companies)
    write_csv(args.output / VERIFIED_JOB_FILE, job_fields, verified_jobs)

    summary_path = args.output / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(
        generated_at=datetime.now(timezone.utc).isoformat(),
        trigger="recovered_jobs_target_delta",
        applicable_companies=len(merged_companies),
        verified_companies=len(verified_companies),
        jd_rows=len(merged_jobs),
        verified_jd_rows=len(verified_jobs),
        evidence_backed_jds=len(merged_jobs),
        verified_structured_jds=sum(row.get("details_verified") == "true" for row in merged_jobs),
        by_evidence_level={
            level: sum(row.get("evidence_level") == level for row in merged_jobs)
            for level in ("csv_declared", "page_text", "structured_job", "audit_confirmed")
        },
        by_priority={
            str(priority): sum(str(row.get("priority")) == str(priority) for row in merged_jobs)
            for priority in (1, 2, 3)
        },
    )
    summary["recovered_jobs_delta"] = {
        "state": "merged",
        "source_json": str(args.delta.resolve()),
        "source_csv": str(args.delta.with_suffix(".csv").resolve()),
        "screening_skill": str((ROOT / ".codex/skills/medical-ai-job-screening/SKILL.md").resolve()),
        "declared_included_jobs": len(included),
        "represented_jobs": sum(normalize_url(row.get("structured_job_url", "")) in delta_urls for row in merged_jobs),
        "net_new_job_rows": len(merged_jobs) - len(old_jobs),
        "replaced_existing_delta_rows": sum(
            normalize_url(row.get("structured_job_url") or "") in delta_urls
            for row in old_jobs
        ),
        "duplicate_rows_removed": removed_duplicate_rows,
        "companies": sorted(delta_by_company),
        "network_requests": 0,
        "ocr_calls": 0,
        "generic_image_processing_rule": "普通图像处理不作为视觉AI证据，必须有模型或算法肯定证据。",
        "merged_at": datetime.now(timezone.utc).isoformat(),
    }
    notes = summary.setdefault("notes", [])
    note = "recovered_jobs_target_delta 的 should_include=true 岗位已按 URL 严格去重并合入核验子集。"
    if note not in notes:
        notes.append(note)
    write_json(summary_path, summary)

    represented = {
        normalize_url(row.get("structured_job_url", "")) for row in merged_jobs
    } & delta_urls
    if represented != delta_urls:
        raise RuntimeError("Not all five delta URLs are represented after merge")
    structured_urls = [normalize_url(row.get("structured_job_url") or "") for row in merged_jobs]
    structured_urls = [url for url in structured_urls if url]
    if len(set(structured_urls)) != len(structured_urls):
        raise RuntimeError("Duplicate job URLs remain after merge")
    if len({row.get("jd_id") for row in merged_jobs}) != len(merged_jobs):
        raise RuntimeError("Duplicate jd_id values remain after merge")
    if summary.get("processed_companies") != 171 or summary.get("runner_state") != "circuit_open":
        raise RuntimeError("batch_009 processing state was not preserved")

    print(json.dumps(summary["recovered_jobs_delta"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
