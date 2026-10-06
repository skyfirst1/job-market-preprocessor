from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep import crawler
from jobprep.html_extract import extract_html
from jobprep.pipeline import Workstation
from jobprep.settings import load_settings, options_for_url
from jobprep.store import canonical_url
from scripts.report_agent_status import update as update_dashboard


TARGET_ADAPTERS = {"ZhiyeJobDetailPortal", "MokahrPublicPortal"}
REPORT_JSON = Path("data/audits/continuous_adapter_remediation.json")
REPORT_CSV = Path("data/audits/continuous_adapter_remediation.csv")
COLUMNS = (
    "batch", "company", "task_id", "adapter", "url", "before_status",
    "after_status", "before_jobs", "after_jobs", "added_jobs",
    "before_text_chars", "after_text_chars", "before_complete", "after_complete",
    "before_list_complete", "after_list_complete", "mode", "network_attempted",
    "network_calls", "recovered", "preserved_prior", "failure_reason",
)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


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


def task_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:24]


def result_of(task: dict) -> dict:
    try:
        value = json.loads(task.get("result_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def metrics(status: str, result: dict) -> dict:
    coverage = result.get("coverage") or {}
    return {
        "status": status,
        "jobs": len(result.get("jobs") or []),
        "text_chars": len(result.get("text") or ""),
        "complete": coverage.get("complete") is True,
        "list_complete": coverage.get("list_complete") is True,
        "stop_reason": coverage.get("stop_reason"),
    }


def quality(value: dict) -> tuple[int, int, int, int, int]:
    rank = {"ok": 4, "partial": 3, "pending_ocr": 2, "blocked": 1,
            "error": 0, "deleted": 0}.get(value["status"], 0)
    return (rank, int(value["complete"]), int(value["list_complete"]),
            value["jobs"], value["text_chars"])


def _decode_html(path: Path) -> str:
    body = path.read_bytes()
    for encoding in ("utf-8", "gb18030"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def normalize_zhiye_detail(result: dict, url: str) -> dict:
    """Turn one saved/rendered detail document into one canonical job."""
    candidate_paths = list(result.get("html_paths") or [])
    if result.get("html_path"):
        candidate_paths.insert(0, result["html_path"])
    best = None
    for raw_path in dict.fromkeys(candidate_paths):
        path = Path(raw_path)
        if not path.is_file():
            continue
        try:
            parsed = extract_html(_decode_html(path), url)
        except (OSError, ValueError):
            continue
        if parsed.get("status") in {"blocked", "deleted", "error"}:
            continue
        if best is None or len(parsed.get("text") or "") > len(best.get("text") or ""):
            best = parsed
            best["html_path"] = str(path.resolve())
    if best is None or len(best.get("text") or "".strip()) < 80:
        return result

    merged = json.loads(json.dumps(result))
    title = (best.get("title") or merged.get("title") or "招聘岗位详情").strip()
    job = {
        "id": hashlib.sha256(url.encode()).hexdigest()[:24],
        "title": title,
        "url": url,
        "text": best["text"],
        "raw": {"source": "offline_html_reparse", "html_path": best["html_path"]},
        "needs_details": False,
    }
    merged.update(title=title, text=best["text"], jobs=[job], status="ok",
                  error=None, retryable=False, error_kind=None)
    merged.setdefault("html_paths", list(dict.fromkeys(candidate_paths)))
    merged["html_path"] = best["html_path"]
    merged["coverage"] = {
        **(merged.get("coverage") or {}), "pages_seen": 1, "complete": True,
        "list_complete": True, "stop_reason": "single_job_complete",
        "detail_urls": [], "scope": "single_job", "pagination_applied": False,
    }
    merged["offline_reparse"] = {"kind": "zhiye_detail_html", "network_accessed": False,
                                 "artifact": best["html_path"]}
    return merged


def reparse_offline(adapter: str, result: dict, url: str) -> dict:
    if adapter == "ZhiyeJobDetailPortal":
        return normalize_zhiye_detail(result, url)
    if adapter == "MokahrPublicPortal":
        merged = crawler.reparse_saved_json(result)
        coverage = merged.setdefault("coverage", {})
        total = coverage.get("total_reported")
        if isinstance(total, int) and total >= 0 and len(merged.get("jobs") or []) >= total:
            coverage["list_complete"] = True
            if not coverage.get("detail_urls"):
                coverage.update(complete=True, stop_reason="offline_json_complete")
                merged["status"] = "ok"
        return merged
    return result


def completed_scope(root: Path, workstation: Workstation) -> tuple[dict[str, dict], list[str]]:
    batches = sorted((root / "data" / "batches").glob("batch_00[3-9]/complete.json"))
    roots: dict[str, dict] = {}
    names = []
    for path in batches:
        payload = read_json(path)
        if payload.get("state") != "completed":
            continue
        names.append(path.parent.name)
        for company in payload.get("companies") or []:
            url = company.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            try:
                roots[task_id(url)] = {"batch": path.parent.name,
                                       "company": company.get("company") or ""}
            except ValueError:
                continue

    with workstation.store.connect() as db:
        tasks = [dict(row) for row in db.execute("SELECT * FROM tasks")]
    children: dict[str, list[dict]] = {}
    for task in tasks:
        if task.get("parent_id"):
            children.setdefault(task["parent_id"], []).append(task)
    scoped: dict[str, dict] = {}
    queue = [(identifier, meta) for identifier, meta in roots.items()]
    by_id = {task["id"]: task for task in tasks}
    while queue:
        identifier, meta = queue.pop(0)
        if identifier in scoped:
            continue
        if identifier in by_id:
            scoped[identifier] = {**meta, "task": by_id[identifier]}
        queue.extend((child["id"], meta) for child in children.get(identifier, []))
    return scoped, names


def summary(rows: list[dict], batches: list[str], selected: int, state: str) -> dict:
    return {
        "state": state,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": {
            "completed_batches": batches,
            "adapters": sorted(TARGET_ADAPTERS),
            "statuses": ["partial", "error"],
            "selected": selected,
            "ok_urls_rerun": 0,
            "wechat_touched": False,
            "ocr_calls": 0,
            "runner_pid_19304_stopped": False,
        },
        "summary": {
            "processed": len(rows),
            "recovered": sum(bool(row.get("recovered")) for row in rows),
            "jobs_added": sum(int(row.get("added_jobs") or 0) for row in rows),
            "offline_only": sum(row.get("mode") == "offline" for row in rows),
            "network_fallbacks": sum(bool(row.get("network_attempted")) for row in rows),
            "preserved_prior": sum(bool(row.get("preserved_prior")) for row in rows),
            "remaining_incomplete": sum(row.get("after_status") in {"partial", "error"}
                                        for row in rows),
        },
        "items": rows,
    }


async def run(root: Path, json_path: Path, csv_path: Path) -> dict:
    workstation = Workstation(load_settings(root))
    scoped, batches = completed_scope(root, workstation)
    selected = []
    for meta in scoped.values():
        task = meta["task"]
        if task["status"] not in {"partial", "error"}:
            continue
        adapter = workstation.adapters.select(task["url"])
        if adapter.name in TARGET_ADAPTERS:
            selected.append((meta, adapter.name))
    selected.sort(key=lambda item: (item[0]["batch"], item[1], item[0]["task"]["url"]))

    rows: list[dict] = []
    update_dashboard("subagent1", {
        "role": "连续批次专站修复应用", "phase": "continuous_adapter_remediation",
        "status": "running", "batch": ",".join(batches), "candidates": len(selected),
        "processed": 0, "success": 0, "partial": 0, "failed": 0, "errors": 0,
        "artifacts": [str(json_path), str(csv_path)],
        "next_step": "优先离线重解析 Zhiye 详情与 Mokahr JSON artifact",
    })

    for index, (meta, adapter_name) in enumerate(selected, 1):
        task = workstation.store.task(meta["task"]["id"])
        # A concurrent transition to ok is an absolute skip guard.
        if task is None or task["status"] not in {"partial", "error"}:
            continue
        before_result = result_of(task)
        before = metrics(task["status"], before_result)
        candidate = reparse_offline(adapter_name, before_result, task["url"])
        candidate_status = candidate.get("status", task["status"])
        after = metrics(candidate_status, candidate)
        mode = "offline"
        network_attempted = False
        failure_reason = ""

        if quality(after) <= quality(before):
            network_attempted = True
            mode = "network"
            options = options_for_url(workstation.config, task["url"])
            options.update({"skip_ocr": True, "max_images": 0, "max_scrolls": 0,
                            "domain_delay": 1.0, "timeout": 35, "settle_ms": 1800,
                            "max_pages": 1 if adapter_name == "ZhiyeJobDetailPortal" else 30,
                            "max_jobs": 3000, "max_json_responses": 200})
            network = await workstation.adapters.acquire(
                task["url"], workstation.artifacts, options=options, tools=workstation.tools
            )
            if adapter_name == "ZhiyeJobDetailPortal":
                network = normalize_zhiye_detail(network, task["url"])
            network_status = network.get("status", "error")
            network_metrics = metrics(network_status, network)
            if quality(network_metrics) > quality(after):
                candidate, after = network, network_metrics
            if network_metrics["status"] in {"error", "blocked", "deleted"}:
                failure_reason = network.get("error") or network_metrics["stop_reason"] or "network_failed"

        preserved = quality(after) < quality(before)
        if preserved:
            candidate, after = before_result, before
        else:
            candidate.setdefault("remediation", {}).update(
                source="continuous_adapter_remediation", adapter=adapter_name,
                mode=mode, network_attempted=network_attempted,
            )
            candidate.setdefault("source_references", before_result.get("source_references", []))
            workstation.store.finish(task["id"], after["status"], candidate,
                                     None if after["status"] != "error" else task.get("error"))
        recovered = quality(after) > quality(before)
        if not recovered and not failure_reason:
            failure_reason = "no_quality_gain_from_existing_artifacts_or_single_refetch"
        row = {
            "batch": meta["batch"], "company": meta["company"], "task_id": task["id"],
            "adapter": adapter_name, "url": task["url"],
            "before_status": before["status"], "after_status": after["status"],
            "before_jobs": before["jobs"], "after_jobs": after["jobs"],
            "added_jobs": max(0, after["jobs"] - before["jobs"]),
            "before_text_chars": before["text_chars"], "after_text_chars": after["text_chars"],
            "before_complete": before["complete"], "after_complete": after["complete"],
            "before_list_complete": before["list_complete"],
            "after_list_complete": after["list_complete"], "mode": mode,
            "network_attempted": network_attempted, "network_calls": int(network_attempted),
            "recovered": recovered, "preserved_prior": preserved,
            "failure_reason": failure_reason,
        }
        rows.append(row)
        progress = summary(rows, batches, len(selected), "running")
        atomic_json(json_path, progress)
        write_csv(csv_path, rows)
        update_dashboard("subagent1", {
            "status": "running", "processed": len(rows),
            "success": progress["summary"]["recovered"],
            "partial": progress["summary"]["remaining_incomplete"],
            "failed": sum(bool(item["failure_reason"]) for item in rows),
            "errors": sum(item["after_status"] == "error" for item in rows),
            "next_step": "继续精确匹配修复" if index < len(selected) else "生成最终报告",
        })

    report = summary(rows, batches, len(selected), "completed")
    atomic_json(json_path, report)
    write_csv(csv_path, rows)
    update_dashboard("subagent1", {
        "status": "completed", "processed": len(rows),
        "success": report["summary"]["recovered"],
        "partial": report["summary"]["remaining_incomplete"],
        "failed": sum(bool(item["failure_reason"]) for item in rows),
        "errors": sum(item["after_status"] == "error" for item in rows),
        "next_step": "交付连续批次专站修复报告；后台 runner 保持运行",
    })
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply completed-batch Zhiye/Mokahr remediations")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", type=Path, default=REPORT_JSON)
    parser.add_argument("--csv", type=Path, default=REPORT_CSV)
    args = parser.parse_args()
    root = args.root.resolve()
    resolve = lambda value: value if value.is_absolute() else root / value
    report = asyncio.run(run(root, resolve(args.json), resolve(args.csv)))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
