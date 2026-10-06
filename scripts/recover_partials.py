from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.pipeline import Workstation
from jobprep.settings import load_settings
from jobprep.store import canonical_url
from scripts.report_agent_status import update as update_dashboard


def task_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:24]


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def result_of(task: dict | None) -> dict:
    if not task or not task.get("result_json"):
        return {}
    try:
        value = json.loads(task["result_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def reason_for(task: dict | None, result: dict) -> str | None:
    if not task or not result:
        return None
    host = (urlsplit(task["url"]).hostname or "").lower()
    text = result.get("text", "")
    jobs = result.get("jobs") or []
    coverage = result.get("coverage") or {}
    status = task.get("status")
    if host == "mp.weixin.qq.com" and result.get("ocr_gaps"):
        return "wechat_local_images_pending_ocr"
    if host.endswith("zhiye.com") and status == "pending_ocr" and not jobs and result.get("json_paths"):
        return "zhiye_json_reparse_required"
    if host != "mp.weixin.qq.com" and status == "pending_ocr" and result.get("ocr_gaps") \
            and result.get("acquisition_status") in {"ok", "partial"}:
        return "web_extraction_recovered_pending_ocr"
    shell_marker = "project config start" in text.lower() or "projectconfigstart" in text.lower().replace(" ", "")
    if not jobs and len(text) <= 180 and (shell_marker or host.endswith("zhiye.com") or len(text.strip()) == 109):
        return "spa_shell"
    if status == "partial" and any(host == domain or host.endswith("." + domain)
                                    for domain in ("mokahr.com", "51job.com", "hotjob.cn")):
        return "spa_pagination_or_details"
    if status == "partial" and coverage.get("stop_reason") in {
            "unknown_pagination", "max_pages", "max_scrolls", "reported_total_mismatch"}:
        return "pagination_or_details_not_complete"
    if status in {"partial", "pending_ocr"}:
        return "other_partial"
    return None


def metrics(task: dict | None, result: dict) -> dict:
    coverage = result.get("coverage") or {}
    return {
        "status": task.get("status") if task else None,
        "text_chars": len(result.get("text", "")),
        "jobs": len(result.get("jobs") or []),
        "detail_urls": len(coverage.get("detail_urls") or []),
        "pages_seen": coverage.get("pages_seen"),
        "stop_reason": coverage.get("stop_reason"),
        "complete": coverage.get("complete"),
        "list_complete": coverage.get("list_complete"),
        "ocr_gaps": len(result.get("ocr_gaps") or []),
    }


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def recover(root: Path, input_path: Path, report_path: Path) -> dict:
    rows = read_rows(input_path)
    workstation = Workstation(load_settings(root))
    snapshots = report_path.parent / "snapshots"
    previous_recovered = set()
    previous_items = {}
    if report_path.exists():
        try:
            previous = json.loads(report_path.read_text(encoding="utf-8"))
            previous_items = {item["id"]: item for item in previous.get("comparisons", []) if item.get("id")}
            previous_recovered = {identifier for identifier, item in previous_items.items() if item.get("recovered")}
        except (OSError, json.JSONDecodeError, TypeError):
            previous_recovered = set()
    candidates = []
    all_reasons = Counter()
    for row in rows:
        url = row.get("acquisition_url") or row.get("url") or ""
        if not url:
            continue
        identifier = task_id(url)
        task = workstation.store.task(identifier)
        result = result_of(task)
        reason = reason_for(task, result)
        if reason:
            all_reasons[reason] += 1
        if reason == "wechat_local_images_pending_ocr":
            previous_recovered.discard(identifier)
        if reason not in {"spa_shell", "spa_pagination_or_details", "wechat_local_images_pending_ocr",
                          "web_extraction_recovered_pending_ocr", "zhiye_json_reparse_required"}:
            continue
        if identifier in previous_recovered and reason != "wechat_local_images_pending_ocr":
            continue
        snapshot = snapshots / f"{identifier}.before.json"
        if result and not snapshot.exists():
            atomic_json(snapshot, result)
        candidates.append({"id": identifier, "company": row.get("company", ""), "url": url,
                           "host": urlsplit(url).hostname or "", "reason": reason,
                           "before": metrics(task, result), "snapshot": str(snapshot)})

    update_dashboard("subagent1", {
        "role": "剩余医疗/医药候选实际批处理与 partial 修复",
        "phase": "首批/第二批 partial 根因修复",
        "status": "running",
        "batch": "partial_recovery_before_batch_002",
        "total_candidates": 176,
        "processed_companies": 20,
        "remaining_companies": 156,
        "partial_by_reason": dict(all_reasons),
        "recovered_partial": 0,
        "remaining_partial": sum(all_reasons.values()),
        "artifacts": [str(report_path), str(snapshots)],
        "next_step": "仅回放受修复根因影响的 partial；ok 链接保持不动",
    })

    comparisons = []
    failures = 0
    for index, item in enumerate(candidates, 1):
        before_status = item["before"]["status"]
        workstation.store.retry([before_status], [item["id"]],
                                reset_attempts=item["reason"] == "wechat_local_images_pending_ocr")
        skip_ocr = item["host"].lower() != "mp.weixin.qq.com"
        force_acquire = item["reason"] == "zhiye_json_reparse_required"
        run = await workstation.run(limit=1, ai_only=False, task_ids=[item["id"]],
                                    options={"wait_retries": False, "skip_ocr": skip_ocr,
                                             "force_acquire": force_acquire})
        task = workstation.store.task(item["id"])
        result = result_of(task)
        after = metrics(task, result)
        improved = (
            after["text_chars"] > item["before"]["text_chars"]
            or after["jobs"] > item["before"]["jobs"]
            or after["detail_urls"] > item["before"]["detail_urls"]
            or after["ocr_gaps"] < item["before"]["ocr_gaps"]
            or after["complete"] is True and item["before"]["complete"] is not True
        )
        recovered = improved and after["status"] not in {"error", "blocked"}
        if item["reason"] == "web_extraction_recovered_pending_ocr" and after["status"] == "partial":
            recovered = True
        if item["reason"] == "wechat_local_images_pending_ocr":
            recovered = after["ocr_gaps"] == 0 and after["status"] not in {
                "pending_ocr", "error", "blocked"
            }
        if after["status"] in {"error", "blocked"}:
            failures += 1
        comparisons.append({**item, "after": after, "improved": improved,
                            "recovered": recovered, "run": run.get("results", [])})
        if failures > 10:
            break
        recovered_count = len(previous_recovered) + sum(entry["recovered"] for entry in comparisons)
        remaining = sum(all_reasons.values()) - recovered_count
        update_dashboard("subagent1", {
            "status": "running", "processed": index, "failed": failures, "errors": failures,
            "circuit_open": failures > 10, "recovered_partial": recovered_count,
            "remaining_partial": remaining,
            "last_error": task.get("error") or "",
            "next_step": "继续仅回放受影响 partial" if failures <= 10 else "第11个失败，停止并修复",
        })
        progress_items = dict(previous_items)
        progress_items.update({entry["id"]: entry for entry in comparisons})
        atomic_json(report_path, {"partial_by_reason": dict(all_reasons),
                                  "comparisons": list(progress_items.values())})

    recovered_count = len(previous_recovered) + sum(entry["recovered"] for entry in comparisons)
    merged = dict(previous_items)
    merged.update({item["id"]: item for item in comparisons})
    report = {
        "state": "circuit_open" if failures > 10 else "completed",
        "partial_by_reason": dict(all_reasons),
        "selected_for_retry": len(candidates),
        "processed": len(comparisons),
        "failures": failures,
        "recovered_partial": recovered_count,
        "remaining_partial": sum(all_reasons.values()) - recovered_count,
        "comparisons": list(merged.values()),
    }
    atomic_json(report_path, report)
    update_dashboard("subagent1", {
        "status": "blocked" if failures > 10 else "completed",
        "processed": len(comparisons), "failed": failures, "errors": failures,
        "circuit_open": failures > 10, "recovered_partial": recovered_count,
        "remaining_partial": report["remaining_partial"],
        "next_step": "修复失败根因后再继续" if failures > 10 else "继续第二批20家公司",
    })
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Cluster and selectively retry recoverable partial acquisitions")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--input", type=Path, default=Path("data/first_batch/first_batch_candidates.csv"))
    parser.add_argument("--report", type=Path, default=Path("data/batches/partial_recovery/report.json"))
    args = parser.parse_args()
    root = args.root.resolve()
    input_path = args.input if args.input.is_absolute() else root / args.input
    report_path = args.report if args.report.is_absolute() else root / args.report
    print(json.dumps(asyncio.run(recover(root, input_path, report_path)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
