"""Shared completion rules for the bounded adapter pool.

The acquisition layer tracks list coverage separately from job-description
coverage. A missing JD is useful metadata, but it does not make an enumerated
job list incomplete.
"""

from copy import deepcopy
from pathlib import Path

from jobprep.html_extract import extract_html


AUDITABLE_TERMINALS = frozenset({
    "terminal_pagination",
    "last_page",
    "reported_last_page",
    "reported_total_reached",
})


def _artifact_refs(result):
    refs = []
    for key in ("html_path", "raw_html_path"):
        value = result.get(key)
        if isinstance(value, str) and value:
            refs.append(value)
    for key in ("html_paths", "json_paths"):
        values = result.get(key)
        if isinstance(values, list):
            refs.extend(value for value in values if isinstance(value, str) and value)
    for item in result.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        for key in ("raw_ref", "html_path", "path"):
            value = item.get(key)
            if isinstance(value, str) and value:
                refs.append(value)
    return list(dict.fromkeys(refs))


def _last_page_evidence(coverage, jobs):
    expected_total = coverage.get("expected_total")
    expected_pages = coverage.get("expected_total_pages")
    pages_received = coverage.get("pages_received")
    pages = coverage.get("pages")
    if (type(expected_total) is not int or expected_total < 0
            or expected_total != len(jobs)
            or type(expected_pages) is not int or expected_pages < 1
            or expected_pages != pages_received
            or not isinstance(pages, list) or len(pages) != pages_received):
        return None
    accepted = [item for item in pages if isinstance(item, dict) and item.get("accepted") is True]
    if len(accepted) != pages_received or any(not item.get("raw_ref") for item in accepted):
        return None
    actual_pages = [item.get("actual_page") for item in accepted]
    if actual_pages != list(range(1, expected_pages + 1)):
        return None
    return {
        "kind": "reported_last_page",
        "expected_total": expected_total,
        "expected_total_pages": expected_pages,
        "pages_received": pages_received,
        "raw_refs": [item["raw_ref"] for item in accepted],
    }


def _browser_terminal_evidence(result, coverage, jobs):
    pages_seen = coverage.get("pages_seen")
    refs = _artifact_refs(result)
    if type(pages_seen) is not int or pages_seen < 1 or not refs:
        return None
    return {
        "kind": "disabled_or_absent_next_control",
        "pages_seen": pages_seen,
        "job_count": len(jobs),
        "artifact_refs": refs,
        "final_url": result.get("final_url") or result.get("url"),
    }


def accept_complete_list(result, *, allowed=AUDITABLE_TERMINALS):
    """Return a copy promoted to ``ok`` only with auditable list evidence.

    ``coverage.complete`` intentionally remains independent: it may stay false
    when JD bodies or incidental images were not collected.
    """
    result = deepcopy(result)
    coverage = result.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("list_complete") is not True:
        return result
    reason = coverage.get("stop_reason")
    if reason not in allowed:
        return result
    jobs = result.get("jobs")
    if not isinstance(jobs, list):
        return result
    if reason in {"last_page", "reported_last_page", "reported_total_reached"}:
        evidence = _last_page_evidence(coverage, jobs)
    else:
        evidence = _browser_terminal_evidence(result, coverage, jobs)
    if evidence is None:
        return result
    coverage["list_completion_evidence"] = evidence
    coverage.setdefault("jd_complete", False)
    coverage.setdefault("needs_details_count", sum(bool(job.get("needs_details"))
                                                   for job in jobs if isinstance(job, dict)))
    if result.get("status") not in {"blocked", "deleted", "error"}:
        result["status"] = "ok"
    return result


def _job_identity(job):
    if not isinstance(job, dict):
        return None
    for key in ("id", "url", "title"):
        value = job.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def accept_stable_mokahr_snapshots(result, source_url, *, max_files=100,
                                    max_bytes=5 * 1024**2):
    """Replay saved Mokahr HTML and accept only a stable terminal identity set.

    This is deliberately narrower than generic ``unknown_pagination`` repair:
    two final snapshots must expose the same non-empty job identities, and the
    union across all snapshots must exactly match the canonical result jobs.
    """
    result = deepcopy(result)
    coverage = result.get("coverage")
    if (not isinstance(coverage, dict)
            or coverage.get("stop_reason") != "unknown_pagination"
            or coverage.get("list_complete") is True):
        return result
    raw_paths = result.get("html_paths") or []
    if not isinstance(raw_paths, list) or not 2 <= len(raw_paths) <= max_files:
        return result
    snapshots = []
    accepted_paths = []
    for raw_path in raw_paths:
        if not isinstance(raw_path, str):
            return result
        path = Path(raw_path)
        try:
            if path.suffix.lower() not in {".html", ".htm"} or not path.is_file():
                return result
            body = path.read_bytes()
        except OSError:
            return result
        if not body or len(body) > max_bytes:
            return result
        parsed = extract_html(body.decode("utf-8", errors="replace"), source_url)
        identities = {_job_identity(job) for job in parsed.get("jobs") or []}
        identities.discard(None)
        snapshots.append(identities)
        accepted_paths.append(str(path.resolve()))
    canonical = {_job_identity(job) for job in result.get("jobs") or []}
    canonical.discard(None)
    observed = set().union(*snapshots)
    if not canonical or snapshots[-1] != snapshots[-2] or observed != canonical:
        return result
    coverage.update(
        list_complete=True,
        stop_reason="stable_terminal_snapshots",
        list_completion_evidence={
            "kind": "stable_final_job_identity_set",
            "snapshot_count": len(snapshots),
            "job_count": len(canonical),
            "final_snapshot_refs": accepted_paths[-2:],
            "all_snapshot_refs": accepted_paths,
        },
    )
    coverage.setdefault("jd_complete", False)
    coverage.setdefault("needs_details_count", sum(bool(job.get("needs_details"))
                                                   for job in result.get("jobs") or []
                                                   if isinstance(job, dict)))
    if result.get("status") not in {"blocked", "deleted", "error"}:
        result["status"] = "ok"
    return result


__all__ = ["AUDITABLE_TERMINALS", "accept_complete_list",
           "accept_stable_mokahr_snapshots"]
