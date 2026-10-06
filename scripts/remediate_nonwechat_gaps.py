from __future__ import annotations

import csv
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.app.wjx import _is_wjx_host, parse_form
from jobprep.pipeline import Workstation
from jobprep.settings import load_settings
from scripts.report_agent_status import update as update_dashboard


AUDIT = ROOT / "data" / "audits" / "continuous_incomplete_review.csv"
ARTIFACTS = ROOT / "data" / "artifacts" / "wjx-live-validation"
REPORT_JSON = ROOT / "data" / "audits" / "nonwechat_gap_remediation.json"
REPORT_CSV = ROOT / "data" / "audits" / "nonwechat_gap_remediation.csv"
COLUMNS = (
    "batch", "company", "task_id", "host", "adapter", "url", "before_status",
    "after_status", "before_attempts", "after_attempts", "retry_count", "network_requests",
    "jobs_recovered", "text_chars", "list_complete", "jd_complete", "complete",
    "stop_reason", "artifact", "preserved_prior", "failure_reason",
)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def result_of(task: dict) -> dict:
    try:
        value = json.loads(task.get("result_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def artifact_index() -> dict[str, Path]:
    index = {}
    for path in ARTIFACTS.glob("*.html"):
        try:
            soup = BeautifulSoup(path.read_bytes(), "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
        except OSError:
            continue
        index[title] = path.resolve()
    return index


def select_artifact(company: str, index: dict[str, Path]) -> Path | None:
    return next((path for title, path in index.items() if company in title), None)


def quality(status: str, result: dict) -> tuple[int, int, int, int, int]:
    coverage = result.get("coverage") or {}
    rank = {"ok": 4, "partial": 3, "error": 0, "blocked": 0, "deleted": 0}.get(status, 0)
    return (rank, int(coverage.get("complete") is True),
            int(coverage.get("list_complete") is True), len(result.get("jobs") or []),
            len(result.get("text") or ""))


def platform(host: str) -> str:
    host = host.lower()
    for suffix, name in (("wjx.cn", "WJX"), ("wjx.top", "WJX"),
                         ("tupu360.com", "Tupu360"),
                         ("xinrenxinshi.com", "Xinrenxinshi"),
                         ("hotjob.cn", "Hotjob"), ("51job.com", "51job"),
                         ("moseeker.com", "Moseeker")):
        if host == suffix or host.endswith("." + suffix):
            return name
    return host


def run() -> dict:
    with AUDIT.open(encoding="utf-8-sig", newline="") as handle:
        audit_rows = list(csv.DictReader(handle))
    true_gaps = [row for row in audit_rows if row.get("review_code") == "true_list_or_pagination_gap"]
    eligible = [row for row in true_gaps if row.get("host") != "mp.weixin.qq.com"
                and ".zhiye.com" not in row.get("host", "")
                and "mokahr" not in row.get("host", "").lower()
                and row.get("review_code") != "auth_or_verification_blocked"]
    selected = [row for row in eligible if _is_wjx_host(row.get("host", ""))]
    selected.sort(key=lambda row: (row.get("batch", ""), row.get("company", "")))
    cluster_counts = {}
    for row in eligible:
        name = platform(row.get("host", ""))
        cluster_counts[name] = cluster_counts.get(name, 0) + 1
    if len(selected) != 5:
        raise RuntimeError(f"WJX remediation scope changed: expected 5 rows, found {len(selected)}")

    workstation = Workstation(load_settings(ROOT))
    artifacts = artifact_index()
    rows = []
    for audit in selected:
        task = workstation.store.task(audit["task_id"])
        if task is None:
            raise RuntimeError(f"Missing task {audit['task_id']}")
        before_result = result_of(task)
        before_status = task["status"]
        if before_status not in {"partial", "error"}:
            rows.append({
                "batch": audit["batch"], "company": audit["company"], "task_id": task["id"],
                "host": audit["host"], "adapter": "WjxPublicForm", "url": task["url"],
                "before_status": before_status, "after_status": before_status,
                "before_attempts": task["attempts"], "after_attempts": task["attempts"],
                "retry_count": 0, "network_requests": 0, "jobs_recovered": 0,
                "text_chars": len(before_result.get("text") or ""),
                "list_complete": bool((before_result.get("coverage") or {}).get("list_complete")),
                "jd_complete": bool((before_result.get("coverage") or {}).get("jd_complete")),
                "complete": bool((before_result.get("coverage") or {}).get("complete")),
                "stop_reason": (before_result.get("coverage") or {}).get("stop_reason"),
                "artifact": "", "preserved_prior": True, "failure_reason": "status_not_retryable",
            })
            continue
        if (before_result.get("remediation") or {}).get("source") == "nonwechat_gap_remediation":
            raise RuntimeError(f"Task already received its one allowed retry: {task['id']}")
        artifact = select_artifact(audit["company"], artifacts)
        if artifact is None:
            raise RuntimeError(f"Validated WJX artifact missing for {audit['company']}")
        html = artifact.read_bytes().decode("utf-8", errors="replace")
        candidate = parse_form(html, task["url"], str(artifact))
        candidate.update(artifact_dir=str(ARTIFACTS.resolve()), fetched_at=datetime.now().astimezone().isoformat())
        candidate["source_references"] = before_result.get("source_references") or workstation.store.source_references(task["id"])
        candidate["acquisition"] = {
            "adapter": "WjxPublicForm", "selection": "rules",
            "attempts": [{"tool": "wjx_form", "url": task["url"], "ordinal": 1,
                          "status": candidate["status"]}],
            "provenance": [{"rule": "wjx_static_full_form", "browser_used": False,
                            "ocr_used": False, "network_requests": 1}],
        }
        candidate["remediation"] = {
            "source": "nonwechat_gap_remediation", "retry_count": 1,
            "network_requests": 1, "reused_validated_artifact": True,
        }
        preserved = quality(candidate["status"], candidate) < quality(before_status, before_result)
        if preserved:
            final_status, final_result = before_status, before_result
        else:
            final_status, final_result = candidate["status"], candidate
            # The preceding adapter validation was this task's single network retry.
            with workstation.store.connect() as db:
                db.execute("UPDATE tasks SET attempts=attempts+1 WHERE id=?", (task["id"],))
            workstation.store.finish(task["id"], final_status, final_result)
            workstation.store.event(task["id"], "nonwechat_gap_remediation",
                                    "WjxPublicForm one-request validated artifact applied")
        final_task = workstation.store.task(task["id"])
        coverage = final_result.get("coverage") or {}
        rows.append({
            "batch": audit["batch"], "company": audit["company"], "task_id": task["id"],
            "host": audit["host"], "adapter": "WjxPublicForm", "url": task["url"],
            "before_status": before_status, "after_status": final_status,
            "before_attempts": task["attempts"], "after_attempts": final_task["attempts"],
            "retry_count": 1, "network_requests": 1,
            "jobs_recovered": max(0, len(final_result.get("jobs") or []) - len(before_result.get("jobs") or [])),
            "text_chars": len(final_result.get("text") or ""),
            "list_complete": coverage.get("list_complete") is True,
            "jd_complete": coverage.get("jd_complete") is True,
            "complete": coverage.get("complete") is True,
            "stop_reason": coverage.get("stop_reason"), "artifact": str(artifact),
            "preserved_prior": preserved, "failure_reason": "" if not preserved else "no_quality_gain",
        })

    report = {
        "state": "completed", "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": {
            "audit_true_gaps": len(true_gaps), "eligible_after_exclusions": len(eligible),
            "platform_clusters": dict(sorted(cluster_counts.items(),
                                              key=lambda item: (-item[1], item[0]))),
            "selected_platform": "WJX", "selected_hosts": sorted({row["host"] for row in selected}),
            "selected": len(selected), "excluded": ["mp.weixin.qq.com", "Zhiye", "Mokahr",
                                                     "login_or_verification_blocked"],
            "statuses": ["partial", "error"], "max_retry_per_task": 1,
            "ok_urls_rerun": 0, "ocr_calls": 0, "browser_calls": 0,
            "exports_targets_modified": False, "runner_stopped": False,
        },
        "summary": {
            "processed": len(rows), "network_requests": sum(row["network_requests"] for row in rows),
            "ok": sum(row["after_status"] == "ok" for row in rows),
            "partial": sum(row["after_status"] == "partial" for row in rows),
            "error": sum(row["after_status"] == "error" for row in rows),
            "list_complete": sum(bool(row["list_complete"]) for row in rows),
            "jd_complete": sum(bool(row["jd_complete"]) for row in rows),
            "jobs_recovered": sum(int(row["jobs_recovered"]) for row in rows),
        },
        "items": rows,
    }
    atomic_json(REPORT_JSON, report)
    write_csv(REPORT_CSV, rows)
    update_dashboard("subagent1", {
        "role": "非微信真实缺口专站修复", "phase": "nonwechat_gap_remediation",
        "status": "completed", "batch": "batch_003..batch_006", "candidates": len(selected),
        "processed": len(rows), "success": report["summary"]["ok"],
        "partial": report["summary"]["partial"], "failed": report["summary"]["error"],
        "errors": report["summary"]["error"],
        "artifacts": [str(REPORT_JSON), str(REPORT_CSV)],
        "next_step": "WJX 单平台缺口已闭环；其他平台保持待后续聚类修复",
    })
    return report


if __name__ == "__main__":
    print(json.dumps(run()["summary"], ensure_ascii=False, indent=2))
