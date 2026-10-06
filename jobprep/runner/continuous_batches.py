from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.app.fileio import read_json_object
from jobprep.runner.dashboard_status import update as update_dashboard


def load_json(path: Path) -> dict:
    return read_json_object(path)


def candidate_count(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def static_51job_parent(root: Path) -> str | None:
    path = root / "data" / "workstation.sqlite3"
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT id,url,result_json FROM tasks
               WHERE url LIKE 'https://campus.51job.com/%' AND result_json IS NOT NULL
               ORDER BY updated_at DESC"""
        )
        for row in rows:
            try:
                result = json.loads(row["result_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            coverage = result.get("coverage") or {}
            if coverage.get("list_complete") is True:
                return str(row["id"])
    return None


def run(command: list[str], root: Path, log) -> subprocess.CompletedProcess:
    log.write("\n$ " + subprocess.list2cmdline(command) + "\n")
    log.flush()
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
    log.write(f"\n[exit={completed.returncode}]\n")
    log.flush()
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuously run remaining medical batches")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/medical_candidates/first_batch_candidates.csv"),
    )
    parser.add_argument("--start-batch", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--detail-limit", type=int, default=50)
    parser.add_argument("--log", type=Path, default=Path("data/continuous_batch.log"))
    args = parser.parse_args()
    root = args.root.resolve()
    source = args.input if args.input.is_absolute() else root / args.input
    log_path = args.log if args.log.is_absolute() else root / args.log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    total = candidate_count(source)
    final_batch = (total + args.batch_size - 1) // args.batch_size
    parent_id = static_51job_parent(root)

    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        log.write(
            f"continuous start batch={args.start_batch} final={final_batch} "
            f"total={total} detail_parent={parent_id}\n"
        )
        for batch_number in range(args.start_batch, final_batch + 1):
            name = f"batch_{batch_number:03d}"
            output = root / "data" / "batches" / name
            complete_path = output / "complete.json"
            existing = load_json(complete_path)
            if existing.get("state") == "completed":
                log.write(f"skip completed {name}\n")
                continue

            processed_before = min((batch_number - 1) * args.batch_size, total)
            update_dashboard(
                "subagent1",
                {
                    "role": "持续医疗/医药候选批处理",
                    "phase": f"{name} 连续处理",
                    "status": "running",
                    "batch": name,
                    "total_candidates": total,
                    "processed_companies": processed_before,
                    "remaining_companies": total - processed_before,
                    "next_step": "本批完成后直接进入下一批；普通 partial 不停机",
                    "artifacts": [str(complete_path), str(log_path)],
                },
            )
            completed = run(
                [
                    sys.executable,
                    str(root / "scripts" / "run_medical_batch.py"),
                    "--root",
                    str(root),
                    "--input",
                    str(source),
                    "--batch-number",
                    str(batch_number),
                    "--batch-size",
                    str(args.batch_size),
                ],
                root,
                log,
            )
            report = load_json(complete_path)
            if completed.returncode != 0 or report.get("state") != "completed":
                log.write(f"stop on {name}: return={completed.returncode} state={report.get('state')}\n")
                return

            if parent_id:
                detail_report = output / "job_detail_report.json"
                detail = run(
                    [
                        sys.executable,
                        str(root / "scripts" / "run_job_detail_pool.py"),
                        "--root",
                        str(root),
                        "--parent-id",
                        parent_id,
                        "--limit",
                        str(args.detail_limit),
                        "--failure-limit",
                        "10",
                        "--report",
                        str(detail_report),
                    ],
                    root,
                    log,
                )
                if detail.returncode != 0:
                    log.write(f"stop on detail circuit after {name}\n")
                    return

        update_dashboard(
            "subagent1",
            {
                "role": "持续医疗/医药候选批处理",
                "phase": "全部候选批次处理完毕",
                "status": "completed",
                "batch": f"batch_{final_batch:03d}",
                "total_candidates": total,
                "processed_companies": total,
                "remaining_companies": 0,
                "next_step": "交给分析任务增量更新可投公司与 JD 清单",
                "artifacts": [str(log_path)],
            },
        )
        log.write("continuous completed remaining=0\n")


if __name__ == "__main__":
    main()
