from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.settings import load_settings
from jobprep.store import Store, canonical_url
from jobprep.app.wechat_public import public_url
from scripts.report_agent_status import update as update_dashboard


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                         encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def task_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:24]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run one fixed 20-company medical recruitment batch")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--input", type=Path,
                        default=Path("data/medical_candidates/first_batch_candidates.csv"))
    parser.add_argument("--batch-number", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()
    if args.batch_number < 1 or not 1 <= args.batch_size <= 20:
        parser.error("batch number must be positive and batch size must be 1..20")
    root = args.root.resolve()
    source = args.input if args.input.is_absolute() else root / args.input
    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        all_rows = list(reader)
    start = (args.batch_number - 1) * args.batch_size
    rows = all_rows[start:start + args.batch_size]
    if not rows:
        parser.error("batch slice is empty")
    name = f"batch_{args.batch_number:03d}"
    output = root / "data" / "batches" / name
    output.mkdir(parents=True, exist_ok=True)
    candidates_path = output / "candidates.csv"
    web_queue = output / "web_queue.jsonl"
    wechat_queue = output / "wechat_queue.jsonl"
    web_report = output / "web_report.json"
    wechat_report = output / "wechat_report.json"
    complete_path = output / "complete.json"
    write_csv(candidates_path, rows, fields)
    web_rows, wechat_rows = [], []
    for row in rows:
        url = row.get("acquisition_url") or ""
        if (urlsplit(url).hostname or "").lower() == "mp.weixin.qq.com":
            wechat_rows.append(row)
        else:
            web_rows.append(row)
    write_jsonl(web_queue, web_rows)
    write_jsonl(wechat_queue, wechat_rows)
    manifest = {
        "batch": name, "source_start_one_based": start + 1,
        "source_end_one_based": start + len(rows), "candidate_count": len(rows),
        "web_count": len(web_rows), "wechat_count": len(wechat_rows),
        "first_batch_excluded": start >= 20,
    }
    atomic_json(output / "manifest.json", manifest)
    update_dashboard("subagent1", {
        "role": "剩余医疗/医药候选实际批处理与 partial 修复",
        "phase": f"{name} 普通网页池与微信池并行处理",
        "status": "running", "batch": name, "candidates": len(rows),
        "processed": 0, "failed": 0, "errors": 0, "circuit_open": False,
        "total_candidates": len(all_rows), "processed_companies": start,
        "remaining_companies": len(all_rows) - start,
        "artifacts": [str(candidates_path), str(web_report), str(wechat_report), str(complete_path)],
        "next_step": "普通网页与微信独立池并行；第11个失败立即熔断",
    })

    commands = []
    if web_rows:
        commands.append(("web", [sys.executable, str(root / "scripts" / "run_web_pool.py"),
            "--root", str(root), "--input", str(web_queue), "--limit", str(len(web_rows)),
            "--batch", name, "--report", str(web_report), "--no-ocr"]))
    else:
        atomic_json(web_report, {"state": "completed", "candidates": 0, "processed": 0,
                                 "failed": 0, "results": [], "skipped": []})
    if wechat_rows:
        commands.append(("wechat", [sys.executable, str(root / "scripts" / "run_wechat_pool.py"),
            "--root", str(root), "--input", str(wechat_queue), "--limit", str(len(wechat_rows)),
            "--min-interval", "180", "--max-interval", "300",
            "--batch", name, "--report", str(wechat_report)]))
    else:
        atomic_json(wechat_report, {"state": "completed", "enqueued": 0, "processed": 0,
                                    "succeeded": 0, "failed": 0})
    processes = [(pool, subprocess.Popen(command, cwd=root, text=True,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace"))
                 for pool, command in commands]
    process_results = {}
    for pool, process in processes:
        stdout, stderr = process.communicate()
        process_results[pool] = {"returncode": process.returncode,
                                 "stdout_tail": stdout[-2000:], "stderr_tail": stderr[-2000:]}

    web = load_json(web_report)
    wechat = load_json(wechat_report)
    store = Store(root / "data")
    companies = []
    counts = {"ok": 0, "partial": 0, "error": 0, "deferred": 0}
    detail_total = 0
    for row in rows:
        url = row.get("acquisition_url") or ""
        task = store.task(task_id(url)) if url else None
        if task and task.get("status") in {"pending", "running"} and (
            urlsplit(url).hostname or ""
        ).lower() == "mp.weixin.qq.com":
            normalized_task = store.task(task_id(public_url(url)))
            if normalized_task and normalized_task.get("status") not in {"pending", "running"}:
                task = normalized_task
        result = {}
        if task and task.get("result_json"):
            try:
                result = json.loads(task["result_json"])
            except json.JSONDecodeError:
                result = {}
        status = task.get("status") if task else None
        bucket = (
            "ok" if status == "ok" else
            "partial" if status in {"partial", "pending_ocr"} else
            "deferred" if status in {None, "pending", "running"} else
            "error"
        )
        counts[bucket] += 1
        details = len((result.get("coverage") or {}).get("detail_urls") or [])
        detail_total += details
        companies.append({"company": row.get("company"), "url": url, "status": status,
                          "text_chars": len(result.get("text", "")),
                          "jobs": len(result.get("jobs") or []), "detail_urls": details,
                          "stop_reason": (result.get("coverage") or {}).get("stop_reason")})
    circuit_open = web.get("circuit_open") is True or wechat.get("state") == "circuit_open"
    pool_error = any(item["returncode"] != 0 for item in process_results.values())
    state = "circuit_open" if circuit_open else "error" if pool_error else "completed"
    completed_companies = start + sum(
        1 for item in companies if item["status"] not in {None, "pending", "running"}
    )
    report = {
        **manifest, "state": state, "counts": counts,
        "new_detail_urls": detail_total, "companies": companies,
        "web_report": str(web_report), "wechat_report": str(wechat_report),
        "processes": process_results,
    }
    atomic_json(complete_path, report)
    update_dashboard("subagent1", {
        "phase": f"{name} 完成" if state == "completed" else f"{name} 暂停待修复",
        "status": "completed" if state == "completed" else "blocked",
        "batch": name, "processed": len(companies) - counts["deferred"],
        "deferred": counts["deferred"], "success": counts["ok"],
        "partial": counts["partial"], "failed": counts["error"], "errors": counts["error"],
        "circuit_open": circuit_open, "processed_companies": completed_companies,
        "remaining_companies": max(0, len(all_rows) - completed_companies),
        "last_error": "" if state == "completed" else json.dumps(process_results, ensure_ascii=False)[-1000:],
        "artifacts": [str(candidates_path), str(web_report), str(wechat_report), str(complete_path)],
        "next_step": "交给 subagent2 增量分析并继续下一批" if state == "completed" else "按失败根因修复后断点续跑",
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
