from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.pipeline import Workstation
from jobprep.settings import load_settings
from scripts.run_web_pool import valid_result


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def select_tasks(root: Path, parent_id: str) -> list[dict]:
    path = root / "data" / "workstation.sqlite3"
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT id,url,status,attempts,error,result_json,parent_id
               FROM tasks WHERE kind='job_detail' AND parent_id=?
               ORDER BY updated_at,id""",
            (parent_id,),
        )
        return [dict(row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bounded detail-task pool that never revisits the parent list page"
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--parent-id", required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--failure-limit", type=int, default=10)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if args.failure_limit < 0:
        parser.error("--failure-limit cannot be negative")

    root = args.root.resolve()
    target = args.report if args.report.is_absolute() else root / args.report
    workstation = Workstation(load_settings(root))
    selected = select_tasks(root, args.parent_id)
    skipped = []
    runnable = []
    for row in selected:
        task = workstation.store.task(row["id"])
        if valid_result(task):
            skipped.append({"id": row["id"], "url": row["url"], "status": row["status"]})
        else:
            runnable.append(row)

    results = []
    failures = 0
    circuit_open = False
    for row in runnable[: args.limit]:
        # Only child detail tasks are reset. The parent list task is never claimed here.
        workstation.store.retry(
            ["error", "blocked", "retry_wait"], [row["id"]], reset_attempts=False
        )
        asyncio.run(
            workstation.run(
                limit=1,
                ai_only=False,
                task_ids=[row["id"]],
                options={"skip_ocr": True, "wait_retries": False},
            )
        )
        task = workstation.store.task(row["id"])
        success = valid_result(task)
        results.append(
            {
                "id": row["id"],
                "url": row["url"],
                "status": task.get("status") if task else "missing",
                "success": success,
                "error": task.get("error") if task else "task missing",
            }
        )
        if not success:
            failures += 1
        if failures > args.failure_limit:
            circuit_open = True
            break

    report = {
        "state": "circuit_open" if circuit_open else "completed",
        "parent_id": args.parent_id,
        "parent_revisited": False,
        "discovered": len(selected),
        "skipped_valid": len(skipped),
        "processed": len(results),
        "succeeded": sum(item["success"] for item in results),
        "failed": failures,
        "circuit_open": circuit_open,
        "deferred": max(0, len(runnable) - len(results)),
        "skipped": skipped,
        "results": results,
    }
    atomic_json(target, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if circuit_open:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
