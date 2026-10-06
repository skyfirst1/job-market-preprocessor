from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.runner.wechat_pool import PoolPolicy, WechatPoolRunner, WechatPoolStore
from jobprep.store import Store, canonical_url
from scripts.report_agent_status import update as update_dashboard


URL_FIELDS = ("acquisition_url", "url", "source_url", "公告链接", "投递链接")


def load_items(path: Path):
    if path.suffix.lower() == ".jsonl":
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    yield next((row.get(key) for key in URL_FIELDS if row.get(key)), ""), row
        return
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            yield next((row.get(key) for key in URL_FIELDS if row.get(key)), ""), row


def cli_processor(root: Path, *, no_ocr=False, browser_channel="chrome"):
    def process(url, metadata):
        command = [
            sys.executable,
            "-m",
            "jobprep",
            "--root",
            str(root),
            "wechat-run",
            "--url",
            url,
            "--limit",
            "1",
            "--interval",
            "10",
            "--browser-channel",
            browser_channel,
        ]
        if no_ocr:
            command.append("--no-ocr")
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
        if completed.returncode:
            return {"status": "error", "error": f"wechat-run exit={completed.returncode}"}
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {"status": "error", "error": "wechat-run returned invalid JSON"}
        exports = report.get("exports") or []
        result = next((item for item in exports if item.get("url") == url), None)
        if result is None and exports:
            result = exports[0]
        if not result:
            return {"status": "error", "error": "wechat-run produced no validated export"}
        return {
            "status": result.get("status", "error"),
            "evidence_validated": True,
            "document_path": result.get("document_path"),
            "article_path": result.get("article_path"),
            "company": metadata.get("company"),
        }

    return process


def seed_valid_workstation_results(root: Path, pool: WechatPoolStore, items):
    """Skip links whose existing result_json still has intact local evidence."""
    from jobprep.__main__ import _wechat_evidence

    workstation = Store(root / "data")
    seeded = 0
    for url, _ in items:
        if not url:
            continue
        normalized = canonical_url(url)
        task_id = hashlib.sha256(normalized.encode()).hexdigest()[:24]
        task = workstation.task(task_id)
        if not task or not task.get("result_json"):
            continue
        try:
            result = json.loads(task["result_json"])
            _wechat_evidence(result, task, workstation.root / "artifacts")
        except (ValueError, TypeError, OSError, KeyError, AttributeError, json.JSONDecodeError):
            continue
        if result.get("status") in {"ok", "partial", "pending_ocr"}:
            pool.seed_success(url, {"status": result["status"], "existing_result_json": True})
            seeded += 1
    return seeded


def main():
    parser = argparse.ArgumentParser(description="Durable single-worker WeChat acquisition pool")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--enqueue-only", action="store_true")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--browser-channel", choices=("chrome", "msedge"), default="chrome")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--batch", default="wechat")
    parser.add_argument("--dashboard-agent", choices=("subagent1",), default="subagent1")
    args = parser.parse_args()
    root = args.root.resolve()
    store = WechatPoolStore(root / "data" / "wechat_pool.sqlite3")
    items = list(load_items(args.input.resolve())) if args.input else []
    items.extend((url, {}) for url in args.url)
    enqueued = 0
    for url, metadata in items:
        if url:
            store.enqueue(url, metadata)
            enqueued += 1
    seeded = seed_valid_workstation_results(root, store, items)
    if args.enqueue_only:
        report = {"enqueued": enqueued, "seeded": seeded, **store.stats()}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    progress_counts = {"success": 0, "partial": 0}

    def progress(event, details):
        result = details.get("result") or {}
        if event == "article_finished" and details.get("success"):
            if result.get("status") == "ok":
                progress_counts["success"] += 1
            else:
                progress_counts["partial"] += 1
        processed = details.get("processed", 0)
        failed = details.get("failed", 0)
        circuit_open = event == "circuit_open" or details.get("state") == "circuit_open"
        update_dashboard(args.dashboard_agent, {
            "role": "普通网页池与独立微信池运行、监控、修复、断点续跑",
            "phase": "微信池批处理",
            "status": "completed" if event == "run_finished" and not circuit_open else
                      "blocked" if circuit_open else "running",
            "batch": args.batch,
            "candidates": len(items),
            "processed": processed,
            "success": progress_counts["success"],
            "partial": progress_counts["partial"],
            "failed": failed,
            "errors": failed,
            "circuit_open": circuit_open,
            "last_error": result.get("error") if not details.get("success", True) else "",
            "artifacts": [str(args.report)] if args.report else [],
            "next_step": "修复后使用 --retry-failed，仅重试失败项" if circuit_open else
                         "继续当前单并发队列" if event != "run_finished" else "阶段结束，交付分析",
        })

    runner = WechatPoolRunner(
        store,
        cli_processor(root, no_ocr=args.no_ocr, browser_channel=args.browser_channel),
        PoolPolicy(min_interval_seconds=args.interval),
        progress=progress,
    )
    report = {"enqueued": enqueued, "seeded": seeded,
              **asyncio.run(runner.run(limit=args.limit, retry_failed=args.retry_failed))}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        target = args.report if args.report.is_absolute() else root / args.report
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(target)
    print(rendered)


if __name__ == "__main__":
    main()
