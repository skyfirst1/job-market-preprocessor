"""Bounded collector for public Zhiye white-label job-list APIs."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from jobprep import crawler


def _has_complete_job_text(job: dict) -> bool:
    """Require both duties and requirements before skipping a detail fetch."""
    raw = job.get("raw")
    if not isinstance(raw, dict):
        return False
    duty = raw.get("Duty")
    requirement = raw.get("Require")
    return (
        isinstance(job.get("title"), str)
        and bool(job["title"].strip())
        and isinstance(duty, str)
        and len(duty.strip()) >= 10
        and isinstance(requirement, str)
        and len(requirement.strip()) >= 10
        and isinstance(job.get("text"), str)
        and len(job["text"].strip()) >= 40
    )


def finalize_public_list(
    result: dict,
    total: int | None,
    *,
    reported_items_seen: int | None = None,
) -> dict:
    """Apply auditable list/JD completion semantics to a Zhiye result."""
    jobs = result.get("jobs") or []
    detail_urls = []
    for job in jobs:
        complete = _has_complete_job_text(job)
        job["needs_details"] = not complete
        if not complete and job.get("url"):
            detail_urls.append(job["url"])

    coverage = result.setdefault("coverage", {})
    counted = len(jobs) if reported_items_seen is None else reported_items_seen
    list_complete = total is not None and counted >= total
    jd_complete = bool(jobs) and not detail_urls
    coverage.update(
        total_reported=total,
        api_items_seen=reported_items_seen,
        detail_urls=list(dict.fromkeys(detail_urls)),
        needs_details_count=sum(job.get("needs_details") is True for job in jobs),
        list_complete=list_complete,
        jd_complete=jd_complete,
        complete=list_complete and jd_complete,
    )
    if not list_complete:
        coverage["stop_reason"] = "reported_total_mismatch"
    elif not jd_complete:
        coverage["stop_reason"] = "zhiye_details_required"
    else:
        coverage.update(
            stop_reason="zhiye_public_api_complete",
            completion_basis="reported_total_and_structured_duty_require_fields",
        )
    result["status"] = "ok" if coverage["complete"] else "partial"
    return result


async def collect(url: str, artifact_dir: Path, options: dict | None = None) -> dict:
    opts = dict(options or {})
    max_pages = opts.get("max_pages", 10)
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise ValueError("Zhiye max_pages must be 1..100")
    opts.update(browser=True, max_images=0, max_json_responses=max(40, int(opts.get("max_json_responses", 40))))
    result = await crawler.collect(url, artifact_dir, opts)
    endpoint = portal_id = None
    for raw_path in result.get("json_paths") or []:
        try:
            record = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        observed = record.get("url", "")
        if observed.lower().endswith("/api/jobad/getjobadpagelist"):
            endpoint = observed
        if "/api/PortalAgent/GetPortalAgentConfig" in observed:
            portal_id = (parse_qs(urlsplit(observed).query).get("portalId") or [None])[0]
    if not endpoint or not portal_id:
        return result

    payload = {"PageIndex": 0, "PageSize": 100, "KeyWords": "", "SpecialType": 0,
               "PortalId": portal_id, "DisplayFields": ["Category"]}
    try:
        async with httpx.AsyncClient(timeout=min(35, float(opts.get("timeout", 20))),
                                     follow_redirects=False) as client:
            existing = {item["id"] for item in result.get("jobs", [])}
            total = None
            api_ids = set()
            for page_index in range(max_pages):
                page_payload = {**payload, "PageIndex": page_index}
                response = await client.post(endpoint, json=page_payload,
                                             headers={"Referer": url})
                response.raise_for_status()
                body = response.json()
                record = {"url": endpoint, "status": response.status_code, "body": body,
                          "request": {"PageIndex": page_index, "PageSize": 100,
                                      "PortalId": portal_id}}
                path = crawler._save(
                    Path(artifact_dir),
                    json.dumps(record, ensure_ascii=False).encode(),
                    ".json",
                )
                if path not in result.setdefault("json_paths", []):
                    result["json_paths"].append(path)
                jobs, reported = crawler._json_jobs(body, endpoint)
                api_ids.update(job["id"] for job in jobs)
                if reported is not None:
                    total = reported if total is None else max(total, reported)
                added = 0
                for job in jobs:
                    if job["id"] not in existing:
                        result.setdefault("jobs", []).append(job)
                        existing.add(job["id"])
                        added += 1
                if total is not None and len(api_ids) >= total:
                    break
                if not jobs or added == 0:
                    break
        finalize_public_list(result, total, reported_items_seen=len(api_ids))
        result.pop("error", None)
        result.setdefault("warnings", []).append(
            f"Expanded public Zhiye list API across up to {max_pages} pages of 100"
        )
    except Exception as exc:
        result.setdefault("warnings", []).append(f"Zhiye public-list expansion failed: {type(exc).__name__}")
    return result
