"""Run resumable manufacturing web acquisition without processing WeChat links."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.store import Store, canonical_url
from scripts.run_web_pool import valid_result


URL_FIELDS = ("acquisition_url", "url", "source_url", "公告链接", "投递链接")
FUTURE_WECHAT_INTERVAL_SECONDS = {"minimum": 180, "maximum": 300, "mode": "random"}


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_queue(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on {path}:{line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"queue row must be an object on {path}:{line_number}")
            rows.append(row)
    return rows


def row_url(row: dict[str, Any]) -> str:
    return next((str(row.get(field)).strip() for field in URL_FIELDS if row.get(field)), "")


def is_wechat(url: str) -> bool:
    try:
        hostname = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return hostname == "weixin.qq.com" or hostname.endswith(".weixin.qq.com")


def partition_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    web: list[dict[str, Any]] = []
    deferred_wechat: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        url = row_url(row)
        if not url:
            invalid.append({**row, "deferred_reason": "missing_url"})
            continue
        try:
            normalized = canonical_url(url)
        except ValueError:
            invalid.append({**row, "deferred_reason": "invalid_url"})
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        if is_wechat(normalized):
            deferred_wechat.append({
                **row,
                "deferred_reason": "wechat_disabled_for_manufacturing_run",
                "future_interval_seconds": FUTURE_WECHAT_INTERVAL_SECONDS,
            })
        else:
            web.append(row)
    return web, deferred_wechat, invalid


def task_for_url(store: Store, url: str) -> dict[str, Any] | None:
    normalized = canonical_url(url)
    task_id = hashlib.sha256(normalized.encode()).hexdigest()[:24]
    return store.task(task_id)


def pending_rows(root: Path, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    store = Store(root / "data")
    pending: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        if valid_result(task_for_url(store, row_url(row))):
            skipped += 1
        else:
            pending.append(row)
    return pending, skipped


def read_report(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return report if isinstance(report, dict) else {}


def run_batches(
    root: Path,
    input_path: Path,
    output_dir: Path,
    *,
    batch_size: int,
    no_ocr: bool,
) -> dict[str, Any]:
    rows = load_queue(input_path)
    web_rows, deferred_wechat, invalid_rows = partition_rows(rows)
    pending, skipped_valid = pending_rows(root, web_rows)

    atomic_jsonl(output_dir / "deferred_wechat.jsonl", deferred_wechat)
    atomic_jsonl(output_dir / "invalid_rows.jsonl", invalid_rows)

    state: dict[str, Any] = {
        "state": "running",
        "input": str(input_path.resolve()),
        "output_dir": str(output_dir.resolve()),
        "input_rows": len(rows),
        "unique_web_rows": len(web_rows),
        "deferred_wechat_rows": len(deferred_wechat),
        "invalid_rows": len(invalid_rows),
        "skipped_valid_results": skipped_valid,
        "pending_at_start": len(pending),
        "batch_size": batch_size,
        "completed_sub_batches": 0,
        "processed_this_run": 0,
        "failed_this_run": 0,
        "future_wechat_policy": {
            "enabled_this_run": False,
            "interval_seconds": FUTURE_WECHAT_INTERVAL_SECONDS,
        },
        "sub_batches": [],
    }
    state_path = output_dir / "state.json"
    atomic_json(state_path, state)

    queues_dir = output_dir / "queues"
    reports_dir = output_dir / "reports"
    logs_dir = output_dir / "logs"
    for index, start in enumerate(range(0, len(pending), batch_size), start=1):
        batch_rows = pending[start:start + batch_size]
        batch_name = f"manufacturing_web_{index:04d}"
        queue_path = queues_dir / f"{batch_name}.jsonl"
        report_path = reports_dir / f"{batch_name}.json"
        log_path = logs_dir / f"{batch_name}.log"
        atomic_jsonl(queue_path, batch_rows)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(root / "scripts" / "run_web_pool.py"),
            "--root",
            str(root),
            "--input",
            str(queue_path),
            "--limit",
            str(len(batch_rows)),
            "--batch",
            batch_name,
            "--report",
            str(report_path),
        ]
        if no_ocr:
            command.append("--no-ocr")
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                command,
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        report = read_report(report_path)
        batch_state = {
            "batch": batch_name,
            "queue": str(queue_path.resolve()),
            "report": str(report_path.resolve()),
            "log": str(log_path.resolve()),
            "returncode": completed.returncode,
            "state": report.get("state") or "missing_report",
            "candidates": len(batch_rows),
            "processed": int(report.get("processed") or 0),
            "skipped_valid": int(report.get("skipped_valid") or 0),
            "failed": int(report.get("failed") or 0),
            "deferred": int(report.get("deferred") or 0),
        }
        state["sub_batches"].append(batch_state)
        state["processed_this_run"] += batch_state["processed"]
        state["failed_this_run"] += batch_state["failed"]
        if completed.returncode != 0 or not report:
            state["state"] = "error"
            state["stop_reason"] = "child_process_failed_or_missing_report"
            atomic_json(state_path, state)
            return state
        if report.get("circuit_open") is True or report.get("state") == "circuit_open":
            state["state"] = "circuit_open"
            state["stop_reason"] = f"{batch_name}_circuit_open"
            atomic_json(state_path, state)
            return state
        state["completed_sub_batches"] += 1
        atomic_json(state_path, state)

    state["state"] = "completed_with_failures" if state["failed_this_run"] else "completed"
    state["stop_reason"] = "all_pending_web_rows_attempted"
    atomic_json(state_path, state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run resumable manufacturing web batches; defer all WeChat rows."
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/manufacturing_candidates/web_queue.jsonl"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/manufacturing_batches"),
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--no-ocr", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 20:
        parser.error("--batch-size must be between 1 and 20")
    root = args.root.resolve()
    input_path = args.input if args.input.is_absolute() else root / args.input
    output_dir = args.output_dir if args.output_dir.is_absolute() else root / args.output_dir
    if not input_path.is_file():
        parser.error(f"input queue does not exist: {input_path}")
    result = run_batches(
        root,
        input_path.resolve(),
        output_dir.resolve(),
        batch_size=args.batch_size,
        no_ocr=args.no_ocr,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["state"] in {"circuit_open", "error"}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
