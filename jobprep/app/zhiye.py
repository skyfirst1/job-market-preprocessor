"""Bounded collector for public Zhiye white-label job-list APIs."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from jobprep import crawler


async def collect(url: str, artifact_dir: Path, options: dict | None = None) -> dict:
    opts = dict(options or {})
    opts.update(browser=True, max_images=0, max_json_responses=max(40, int(opts.get("max_json_responses", 40))))
    result = await crawler.collect(url, artifact_dir, opts)
    endpoint = portal_id = None
    for raw_path in result.get("json_paths") or []:
        try:
            record = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        observed = record.get("url", "")
        if observed.endswith("/api/JobAd/GetJobAdPageList"):
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
            response = await client.post(endpoint, json=payload, headers={"Referer": url})
            response.raise_for_status()
            body = response.json()
        record = {"url": endpoint, "status": response.status_code, "body": body,
                  "request": {"PageIndex": 0, "PageSize": 100, "PortalId": portal_id}}
        path = crawler._save(Path(artifact_dir), json.dumps(record, ensure_ascii=False).encode(), ".json")
        if path not in result.setdefault("json_paths", []):
            result["json_paths"].append(path)
        jobs, total = crawler._json_jobs(body, endpoint)
        existing = {item["id"] for item in result.get("jobs", [])}
        for job in jobs:
            if job["id"] not in existing:
                result.setdefault("jobs", []).append(job)
                existing.add(job["id"])
        coverage = result.setdefault("coverage", {})
        coverage["total_reported"] = total
        coverage["detail_urls"] = list(dict.fromkeys(
            job["url"] for job in result["jobs"] if job.get("url")
        ))
        coverage["list_complete"] = total is not None and len(result["jobs"]) >= total
        coverage["complete"] = coverage["list_complete"] and not coverage["detail_urls"]
        coverage["stop_reason"] = "zhiye_public_api_complete" if coverage["list_complete"] else "reported_total_mismatch"
        result["status"] = "ok" if coverage["complete"] else "partial"
        result.pop("error", None)
        result.setdefault("warnings", []).append("Expanded public Zhiye list API to PageSize=100")
    except Exception as exc:
        result.setdefault("warnings", []).append(f"Zhiye public-list expansion failed: {type(exc).__name__}")
    return result
