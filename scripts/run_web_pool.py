from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.pipeline import Workstation
from jobprep.settings import load_settings
from scripts.report_agent_status import update as update_dashboard
from scripts.run_wechat_pool import load_items


SUCCESS = {"ok", "partial", "pending_ocr"}


def valid_result(task):
    if not task or task.get("status") not in SUCCESS or not task.get("result_json"):
        return False
    try:
        result = json.loads(task["result_json"])
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(result, dict)
        and result.get("url") == task.get("url")
        and result.get("status") in SUCCESS
    )


def atomic_report(path: Path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description="Bounded resumable pool for ordinary recruitment pages")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--batch", default="web")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--dashboard-agent", choices=("subagent1",), default="subagent1")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    root = args.root.resolve()
    items = [(url, metadata) for url, metadata in load_items(args.input.resolve()) if url]
    workstation = Workstation(load_settings(root))
    skipped = []
    runnable = []
    for url, metadata in items:
        task_id = workstation.store.add_task(url, priority=10000)
        task = workstation.store.task(task_id)
        if valid_result(task):
            skipped.append({"id": task_id, "url": task["url"], "status": task["status"],
                            "company": metadata.get("company")})
        else:
            runnable.append((task_id, metadata))

    results = []
    failed = 0
    circuit_open = False
    target = args.report if args.report and args.report.is_absolute() else root / (args.report or Path("data/web_pool_report.json"))

    def dashboard(status, next_step, last_error=""):
        ok = sum(item.get("status") == "ok" for item in skipped + results)
        partial = sum(item.get("status") in {"partial", "pending_ocr"} for item in skipped + results)
        update_dashboard(args.dashboard_agent, {
            "role": "普通网页池与独立微信池运行、监控、修复、断点续跑",
            "phase": "普通网页池批处理",
            "status": status,
            "batch": args.batch,
            "candidates": len(items),
            "processed": len(skipped) + len(results),
            "success": ok,
            "partial": partial,
            "failed": failed,
            "errors": failed,
            "circuit_open": circuit_open,
            "last_error": last_error,
            "artifacts": [str(target)],
            "next_step": next_step,
        })

    dashboard("running", "按有效 result_json 跳过成功项，逐条处理剩余项")
    for task_id, metadata in runnable[: args.limit]:
        workstation.store.retry(["error", "blocked", "retry_wait"], [task_id], reset_attempts=False)
        report = asyncio.run(workstation.run(
            limit=1,
            ai_only=False,
            task_ids=[task_id],
            options={"skip_ocr": args.no_ocr, "wait_retries": False},
        ))
        task = workstation.store.task(task_id)
        item = {"id": task_id, "url": task["url"], "status": task["status"],
                "company": metadata.get("company")}
        results.append(item)
        if not valid_result(task):
            failed += 1
        if failed > 10:
            circuit_open = True
            dashboard("blocked", "修复后再次运行；有效成功项仍会跳过", task.get("error") or "failure_count=11")
            break
        dashboard("running", "继续普通网页池", task.get("error") or "")

    state = "circuit_open" if circuit_open else "completed"
    report = {
        "state": state,
        "candidates": len(items),
        "skipped_valid": len(skipped),
        "processed": len(results),
        "failed": failed,
        "circuit_open": circuit_open,
        "skipped": skipped,
        "results": results,
        "deferred": max(0, len(runnable) - len(results)),
    }
    atomic_report(target, report)
    dashboard("blocked" if circuit_open else "completed",
              "修复后重试失败项" if circuit_open else "阶段结束，交付分析")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
