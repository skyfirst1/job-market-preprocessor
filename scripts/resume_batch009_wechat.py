"""Safely resume only the batch_009 WeChat checkpoint.

The command is deliberately inert unless --execute is supplied.  It uses a
batch-local pool, reads the public-capture ledger before starting a worker and
never enables the global pool's --retry-failed switch.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.app.wechat_public import MAX_NEW_ARTICLES_24H, VERSION, public_url
from jobprep.runner.wechat_pool import PoolPolicy, WechatPoolRunner, WechatPoolStore, pool_lock
from jobprep.store import Store


BATCH = "batch_009"
QUEUE_RELATIVE = Path("data/batches/batch_009/wechat_queue.jsonl")
STATE_RELATIVE = Path("data/batches/batch_009/wechat_resume.sqlite3")
REPORT_RELATIVE = Path("data/batches/batch_009/wechat_resume_report.json")
EXPECTED_ITEMS = 16
URL_FIELDS = ("acquisition_url", "url", "source_url", "announcement_url", "application_url")


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_checkpoint(path: Path) -> list[tuple[str, dict]]:
    items: list[tuple[str, dict]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            raw_url = next((row.get(name) for name in URL_FIELDS if row.get(name)), "")
            try:
                url = public_url(raw_url)
            except ValueError as exc:
                raise ValueError(f"Invalid WeChat URL on checkpoint line {line_number}") from exc
            if url in seen:
                raise ValueError(f"Duplicate WeChat URL on checkpoint line {line_number}")
            seen.add(url)
            items.append((url, row))
    if len(items) != EXPECTED_ITEMS:
        raise ValueError(f"{BATCH} checkpoint must contain exactly {EXPECTED_ITEMS} unique items")
    return items


def capture_key(url: str) -> str:
    return hashlib.sha256((VERSION + ":" + public_url(url)).encode()).hexdigest()


def capture_budget(root: Path, *, now_value: float | None = None,
                   limit: int = MAX_NEW_ARTICLES_24H) -> dict:
    """Read rolling capture capacity without reserving an article or making a request."""
    now_value = time.time() if now_value is None else now_value
    path = root / "data" / "artifacts" / ".wechat_public" / "captures.sqlite3"
    if not path.is_file():
        return {"used": 0, "limit": limit,
                "available": limit, "next_available_at": None}
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(
            "SELECT scheduled FROM captures WHERE scheduled>=? ORDER BY scheduled",
            (now_value - 86400,),
        ).fetchall()
    used = len(rows)
    next_value = rows[0][0] + 86400 if used >= limit else None
    return {
        "used": used,
        "limit": limit,
        "available": max(0, limit - used),
        "next_available_at": (
            datetime.fromtimestamp(next_value, timezone.utc).isoformat() if next_value else None
        ),
    }


def _valid_workstation_success(root: Path, url: str) -> dict | None:
    from jobprep.__main__ import _wechat_evidence

    workstation = Store(root / "data")
    task_id = hashlib.sha256(url.encode()).hexdigest()[:24]
    task = workstation.task(task_id)
    if not task or not task.get("result_json"):
        return None
    try:
        result = json.loads(task["result_json"])
        _wechat_evidence(result, task, workstation.root / "artifacts")
    except (ValueError, TypeError, OSError, KeyError, AttributeError, json.JSONDecodeError):
        return None
    if result.get("status") not in {"ok", "partial", "pending_ocr"}:
        return None
    return {"status": result["status"], "existing_result_json": True}


def seed_successes(root: Path, pool: WechatPoolStore, items: list[tuple[str, dict]]) -> int:
    """Import success only; failed global rows never enter this batch-local queue state."""
    global_path = root / "data" / "wechat_pool.sqlite3"
    global_successes: dict[str, dict] = {}
    if global_path.is_file():
        with closing(sqlite3.connect(global_path)) as db:
            for url, result_json in db.execute(
                "SELECT url,result_json FROM articles WHERE status='succeeded'"
            ):
                try:
                    global_successes[public_url(url)] = json.loads(result_json or "{}")
                except (ValueError, json.JSONDecodeError):
                    continue
    seeded = 0
    for url, _ in items:
        result = _valid_workstation_success(root, url) or global_successes.get(url)
        if result:
            pool.seed_success(url, result)
            seeded += 1
    return seeded


def reset_local_failures(pool: WechatPoolStore, allowed_urls: set[str], now_value: float) -> int:
    """Retry failures only inside the 16-row isolated checkpoint database."""
    with closing(pool.connect()) as db, db:
        rows = db.execute("SELECT id,url FROM articles WHERE status='failed'").fetchall()
        ids = [row["id"] for row in rows if row["url"] in allowed_urls]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        return db.execute(
            f"UPDATE articles SET status='pending',last_error=NULL,updated_at=? WHERE id IN ({marks})",
            (now_value, *ids),
        ).rowcount


def prepare_zero_request_budget_retry(root: Path, url: str) -> None:
    """Clear only the prior zero-request budget result for this whitelisted URL."""
    store = Store(root / "data")
    task_id = hashlib.sha256(url.encode()).hexdigest()[:24]
    task = store.task(task_id)
    if not task or not task.get("result_json"):
        return
    try:
        result = json.loads(task["result_json"])
    except json.JSONDecodeError:
        return
    if not (
        result.get("error_kind") == "new_article_24h_budget_exhausted"
        and result.get("requests_this_run") == 0
    ):
        return
    with store.connect() as db:
        db.execute(
            """UPDATE tasks SET status='pending',result_json=NULL,error=NULL,
               available_at=NULL,last_error_kind=NULL,updated_at=?
               WHERE id=? AND result_json=?""",
            (datetime.now(timezone.utc).isoformat(), task_id, task["result_json"]),
        )


def checkpoint_processor(root: Path, *, no_ocr: bool, browser_channel: str,
                         budget_limit: int):
    """Run one URL and preserve budget/request diagnostics from durable task state."""
    def process(url: str, metadata: dict) -> dict:
        prepare_zero_request_budget_retry(root, url)
        command = [
            sys.executable, "-m", "jobprep", "--root", str(root), "wechat-run",
            "--url", url, "--limit", "1", "--interval", "10",
            "--browser-channel", browser_channel,
        ]
        if no_ocr:
            command.append("--no-ocr")
        environment = os.environ.copy()
        environment["JOBPREP_WECHAT_MAX_NEW_ARTICLES_24H"] = str(budget_limit)
        completed = subprocess.run(
            command, cwd=root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300, check=False, env=environment,
        )
        task_id = hashlib.sha256(url.encode()).hexdigest()[:24]
        task = Store(root / "data").task(task_id)
        if task and task.get("result_json"):
            try:
                result = json.loads(task["result_json"])
                result["company"] = metadata.get("company")
                result["evidence_validated"] = result.get("status") in {
                    "ok", "partial", "pending_ocr"
                }
                return result
            except json.JSONDecodeError:
                pass
        if completed.returncode:
            return {"status": "error", "error": f"wechat-run exit={completed.returncode}",
                    "requests_this_run": 0}
        return {"status": "error", "error": "wechat-run produced no durable result",
                "requests_this_run": 0}

    return process


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely resume only the batch_009 WeChat checkpoint"
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--execute", action="store_true",
                        help="Allow processing after the local rolling budget preflight passes")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--browser-channel", choices=("chrome", "msedge"), default="chrome")
    parser.add_argument("--override-budget", type=int, metavar="LIMIT",
                        help="Explicit one-run rolling article cap (1-100)")
    return parser


def main(argv: list[str] | None = None) -> dict:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    queue = root / QUEUE_RELATIVE
    state_path = root / STATE_RELATIVE
    report_path = root / REPORT_RELATIVE
    items = load_checkpoint(queue)
    allowed_urls = {url for url, _ in items}
    pool = WechatPoolStore(state_path)
    for url, metadata in items:
        pool.enqueue(url, metadata)
    seeded = seed_successes(root, pool, items)
    if args.override_budget is not None and not 1 <= args.override_budget <= 100:
        raise ValueError("--override-budget must be between 1 and 100")
    budget_limit = args.override_budget or MAX_NEW_ARTICLES_24H
    budget = (capture_budget(root, limit=budget_limit)
              if args.override_budget is not None else capture_budget(root))
    base = {
        "batch": BATCH,
        "checkpoint": str(queue),
        "checkpoint_items": len(items),
        "execute_requested": args.execute,
        "seeded_successes": seeded,
        "budget": budget,
        "network_requests_started": 0,
    }
    if not args.execute:
        report = {**base, "state": "dry_run", "reason": "pass --execute to allow requests",
                  **pool.stats()}
        atomic_json(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    if budget["available"] <= 0:
        report = {**base, "state": "budget_wait", "reason": "new_article_24h_budget_exhausted",
                  **pool.stats()}
        atomic_json(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    reset = reset_local_failures(pool, allowed_urls, time.time())
    request_counts = {"started": 0}

    def progress(event: str, details: dict) -> None:
        if event == "article_finished":
            result = details.get("result") or {}
            count = result.get("requests_this_run", 0)
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                request_counts["started"] += count

    global_lock = root / "data" / "wechat_pool.lock"
    with pool_lock(global_lock):
        runner = WechatPoolRunner(
            pool,
            checkpoint_processor(root, no_ocr=args.no_ocr,
                                 browser_channel=args.browser_channel,
                                 budget_limit=budget_limit),
            PoolPolicy(min_interval_seconds=10.0, max_failures=10),
            progress=progress,
        )
        run_report = asyncio.run(runner.run(limit=EXPECTED_ITEMS, retry_failed=False))
    report = {**base, "state": run_report["state"], "local_failures_reset": reset,
              "network_requests_started": request_counts["started"], **run_report}
    atomic_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    main()
