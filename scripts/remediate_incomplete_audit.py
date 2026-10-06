from __future__ import annotations

import argparse
import asyncio
import csv
import json
from pathlib import Path
import sqlite3
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.crawler import reparse_saved_json
from jobprep.pipeline import Workstation
from jobprep.settings import load_settings
from scripts.report_agent_status import update as update_dashboard


PRIORITY = {
    "巨鲨医疗": 0,
    "优宁维": 1,
    "中润医药(集团)": 2,
    "京东方-博士专项医疗类": 3,
    "信立泰药业": 4,
}
REPORT_COLUMNS = (
    "company", "task_id", "category", "root_cause", "before_status", "after_status",
    "before_jobs", "after_jobs", "before_details", "after_details", "before_text_chars",
    "after_text_chars", "fixed", "fix_method", "ocr_calls", "network_revisited",
    "source_url", "error",
)


def read_csv(path: Path) -> list[dict[str, str]]:
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


def metrics(task: dict | None, result: dict) -> dict:
    coverage = result.get("coverage") or {}
    return {
        "status": task.get("status") if task else None,
        "jobs": len(result.get("jobs") or []),
        "details": len(coverage.get("detail_urls") or []),
        "text_chars": len(result.get("text") or ""),
        "total_reported": coverage.get("total_reported"),
        "stop_reason": coverage.get("stop_reason"),
        "list_complete": coverage.get("list_complete"),
    }


def fixed(category: str, before: dict, after: dict) -> bool:
    if after["status"] in {"error", "blocked", None}:
        return False
    if category == "A":
        total = after["total_reported"]
        enough = total is None or after["jobs"] >= total
        return after["jobs"] > before["jobs"] and enough
    return after["jobs"] > 0 or after["text_chars"] > max(0, before["text_chars"])


def audited_before(row: dict[str, str]) -> dict:
    def number(name):
        raw = row.get(name, "")
        return int(raw) if raw not in ("", None) else None
    return {"status": row.get("original_status"), "jobs": number("jobs_count") or 0,
            "details": number("detail_urls_count") or 0, "text_chars": number("text_chars") or 0,
            "total_reported": number("total_reported"), "stop_reason": row.get("stop_reason"),
            "list_complete": row.get("list_complete") == "True"}


def quality(item: dict) -> tuple[int, int, int, int]:
    rank = {"ok": 3, "partial": 2, "pending_ocr": 2, "error": 0, "blocked": 0}.get(item["status"], 0)
    return rank, item["jobs"], item["details"], item["text_chars"]


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def ocr_count(root: Path) -> int:
    path = root / "data" / "ocr" / "baidu_ocr.sqlite3"
    if not path.exists():
        return 0
    with sqlite3.connect(path) as db:
        return int(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])


async def remediate(root: Path, audit_path: Path, json_path: Path, csv_path: Path) -> dict:
    rows = [row for row in read_csv(audit_path)
            if row.get("worth_retrying") == "是" and row.get("review_category") in {"A", "C", "D"}]
    if len(rows) != 14:
        raise RuntimeError(f"Audit retry scope changed: expected 14 rows, found {len(rows)}")
    rows.sort(key=lambda row: (PRIORITY.get(row["company"], 99), row["company"]))
    workstation = Workstation(load_settings(root))
    selected = {row["task_id"] for row in rows}
    if any(workstation.store.task(identifier).get("status") == "ok" for identifier in selected):
        raise RuntimeError("Refusing to remediate an ok task")

    calls_before = ocr_count(root)
    output_rows = []
    failures = 0
    update_dashboard("subagent1", {
        "role": "审计 A/C/D 真正不完整案例修复",
        "phase": "14项定向修复",
        "status": "running",
        "batch": "incomplete_audit_remediation",
        "candidates": 14,
        "processed": 0,
        "success": 0,
        "partial": 0,
        "failed": 0,
        "errors": 0,
        "artifacts": [str(json_path), str(csv_path)],
        "next_step": "先本地重解析，再仅重试仍缺证据的非微信任务",
    })

    for index, row in enumerate(rows, 1):
        task = workstation.store.task(row["task_id"])
        before_result = result_of(task)
        current_before = metrics(task, before_result)
        before = audited_before(row)
        reparsed = reparse_saved_json(before_result)
        cleaned_invalid_jobs = len(reparsed.get("jobs") or []) < len(before_result.get("jobs") or [])
        local_after = metrics({**task, "status": reparsed.get("status", task["status"])}, reparsed)
        method = "local_json_reparse"
        revisited = False

        if reparsed != before_result:
            reparsed.setdefault("remediation", {}).update(
                source="incomplete_audit", network_revisited=False, method=method
            )
            workstation.store.finish(task["id"], reparsed.get("status", task["status"]), reparsed)
        if not fixed(row["review_category"], before, local_after):
            if row["host"].lower() == "mp.weixin.qq.com":
                method = "local_only_wechat"
            else:
                method = "targeted_browser_retry"
                revisited = True
                workstation.store.retry([task["status"]], [task["id"]], reset_attempts=True)
                await workstation.run(
                    limit=1,
                    ai_only=False,
                    task_ids=[task["id"]],
                    options={
                        "force_acquire": True,
                        "skip_ocr": True,
                        "wait_retries": False,
                        "browser": True,
                        "browser_channel": "chrome",
                        "max_pages": 12,
                        "max_scrolls": 12,
                        "max_images": 0,
                        "max_json_responses": 120,
                        "settle_ms": 1800,
                        "timeout": 35,
                    },
                )

        final_task = workstation.store.task(row["task_id"])
        final_result = reparse_saved_json(result_of(final_task))
        after = metrics(final_task, final_result)
        if quality(after) < quality(current_before) and not cleaned_invalid_jobs:
            workstation.store.finish(task["id"], task["status"], before_result)
            final_task = workstation.store.task(row["task_id"])
            final_result = before_result
            after = current_before
            method += "+preserved_better_prior_evidence"
        was_fixed = fixed(row["review_category"], before, after)
        if was_fixed and final_result != result_of(final_task):
            final_result.setdefault("remediation", {}).update(
                source="incomplete_audit", network_revisited=revisited, method=method
            )
            workstation.store.finish(final_task["id"], final_result.get("status", final_task["status"]), final_result)
            final_task = workstation.store.task(row["task_id"])
            after = metrics(final_task, final_result)
        if after["status"] in {"error", "blocked"}:
            failures += 1
        output_rows.append({
            "company": row["company"], "task_id": row["task_id"],
            "category": row["review_category"], "root_cause": row["root_cause"],
            "before_status": before["status"], "after_status": after["status"],
            "before_jobs": before["jobs"], "after_jobs": after["jobs"],
            "before_details": before["details"], "after_details": after["details"],
            "before_text_chars": before["text_chars"], "after_text_chars": after["text_chars"],
            "fixed": was_fixed, "fix_method": method, "ocr_calls": 0,
            "network_revisited": revisited, "source_url": row["source_url"],
            "error": final_task.get("error") or final_result.get("error") or "",
        })
        if failures > 10:
            break
        atomic_json(json_path, {"state": "running", "items": output_rows})
        write_csv(csv_path, output_rows)
        update_dashboard("subagent1", {
            "status": "running", "processed": index,
            "success": sum(item["fixed"] for item in output_rows),
            "partial": sum(not item["fixed"] and item["after_status"] not in {"error", "blocked"} for item in output_rows),
            "failed": failures, "errors": failures, "circuit_open": failures > 10,
            "last_error": output_rows[-1]["error"],
            "next_step": "继续剩余定向修复" if failures <= 10 else "第11项失败，停止并修复",
        })

    calls_after = ocr_count(root)
    report = {
        "state": "circuit_open" if failures > 10 else "completed",
        "scope": {"selected": len(rows), "processed": len(output_rows),
                  "categories": {name: sum(row["review_category"] == name for row in rows)
                                 for name in ("A", "C", "D")},
                  "excluded_b": True, "excluded_ok": True},
        "fixed": sum(item["fixed"] for item in output_rows),
        "remaining": sum(not item["fixed"] for item in output_rows),
        "failures": failures,
        "ocr": {"model": "general_basic", "calls_before": calls_before,
                "calls_after": calls_after, "new_calls": calls_after - calls_before,
                "budget_raised": False},
        "wechat": {"network_revisited": False, "single_concurrency": 1,
                   "minimum_interval_seconds": 10, "failure_circuit_breaker": 11},
        "items": output_rows,
    }
    atomic_json(json_path, report)
    write_csv(csv_path, output_rows)
    update_dashboard("subagent1", {
        "status": "blocked" if failures > 10 else "completed",
        "processed": len(output_rows), "success": report["fixed"],
        "partial": report["remaining"] - failures, "failed": failures, "errors": failures,
        "circuit_open": failures > 10,
        "next_step": "修复失败根因后继续" if failures > 10 else "交付 remediation 报告",
    })
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Remediate only audit-approved incomplete tasks")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--audit", type=Path, default=Path("data/audits/incomplete_review.csv"))
    parser.add_argument("--json", type=Path, default=Path("data/audits/remediation_report.json"))
    parser.add_argument("--csv", type=Path, default=Path("data/audits/remediation_report.csv"))
    args = parser.parse_args()
    root = args.root.resolve()
    resolve = lambda path: path if path.is_absolute() else root / path
    report = asyncio.run(remediate(root, resolve(args.audit), resolve(args.json), resolve(args.csv)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
