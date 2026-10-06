from __future__ import annotations

import argparse
import csv
from datetime import datetime
from html import escape
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.analysis.application_targets import update_application_targets
from jobprep.app.fileio import atomic_write_text, read_json_object


BATCHES = ROOT / "data" / "batches"
MASTER = ROOT / "data" / "medical_candidates" / "first_batch_candidates.csv"
DATABASE = ROOT / "data" / "workstation.sqlite3"
OUTPUT = ROOT / "exports" / "targets"
DASHBOARD = ROOT / "exports" / "operations_dashboard"
AUDIT = ROOT / "data" / "audits" / "incomplete_review.csv"
STATE_PATH = ROOT / "data" / "agent_reports" / "subagent4.json"
SLOT_STATE_PATH = ROOT / "data" / "agent_reports" / "subagent2.json"
BOARD_STATE_PATH = DASHBOARD / "subagent2.json"
SLOT_HTML_PATH = DASHBOARD / "subagent2.html"
OUTPUT_NAMES = (
    "applicable_companies.csv",
    "job_targets.csv",
    "verified_companies.csv",
    "verified_job_targets.csv",
    "pending_discovery_companies.csv",
    "error_companies.csv",
    "summary.json",
)


def atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def read_json(path: Path) -> dict:
    return read_json_object(path)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def batch_number(path: Path) -> int | None:
    try:
        return int(path.name.removeprefix("batch_"))
    except ValueError:
        return None


def complete_batches(start_batch: int, batches_dir: Path = BATCHES) -> list[Path]:
    output = []
    for path in batches_dir.glob("batch_[0-9][0-9][0-9]"):
        number = batch_number(path)
        if number is not None and number >= start_batch and (path / "complete.json").is_file():
            output.append(path)
    return sorted(output, key=lambda path: batch_number(path) or 0)


def merge_candidates(paths: list[Path], output: Path) -> tuple[int, int]:
    rows: list[dict[str, str]] = []
    columns: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    companies: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for field in reader.fieldnames or []:
                if field not in columns:
                    columns.append(field)
            for row in reader:
                company = str(row.get("company") or "").strip()
                key = (
                    company.casefold(),
                    str(row.get("acquisition_url") or "").strip(),
                    str(row.get("source_row") or "").strip(),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
                if company:
                    companies.add(company.casefold())
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows), len(companies)


def _ids(path: Path, field: str) -> set[str]:
    return {row.get(field, "") for row in read_csv(path) if row.get(field)}


def _batch_links(processed: list[int], batches_dir: Path) -> str:
    rows = []
    for number in processed:
        base = f"data/batches/batch_{number:03d}"
        cells = []
        for name in ("complete.json", "candidates.csv", "web_report.json", "wechat_report.json"):
            if (batches_dir / f"batch_{number:03d}" / name).is_file():
                cells.append(f'<a href="/view/{base}/{name}">{escape(name)}</a>')
        rows.append(f"<tr><td>batch_{number:03d}</td><td>{' · '.join(cells)}</td></tr>")
    return "".join(rows) or "<tr><td colspan=\"2\">等待 batch_003/complete.json</td></tr>"


def render_status(state: dict, batches_dir: Path = BATCHES) -> str:
    summary = state.get("summary") if isinstance(state.get("summary"), dict) else {}
    processed = [int(value) for value in state.get("processed_batches", [])]
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>持续分析状态</title><style>
body{{margin:0;padding:16px;color:#17202a;background:#f4f6f8;font:13px/1.45 "Segoe UI","Microsoft YaHei",sans-serif}}h1{{font-size:18px;margin:0}}h2{{font-size:14px;margin:16px 0 7px}}.muted{{color:#667085}}.state{{color:#12684f;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}}.metric{{padding:9px;background:#fff;border:1px solid #d0d5dd}}.metric b{{display:block;font-size:21px}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:7px;border:1px solid #d0d5dd;text-align:left}}th{{background:#eef2f3}}a{{color:#075e54}}
</style></head><body><h1>Subagent 4 持续增量分析 <span class="state">{escape(str(state.get('status', 'waiting')))}</span></h1>
<div class="muted">{escape(str(state.get('phase', '等待批次')))} · 每 10 秒刷新 · 不运行抓取或 OCR</div>
<div class="grid"><div class="metric"><b>{summary.get('applicable_companies', '-')}</b>可投公司</div><div class="metric"><b>{summary.get('jd_rows', '-')}</b>候选 JD</div><div class="metric"><b>{summary.get('verified_companies', '-')}</b>已核验公司</div><div class="metric"><b>{summary.get('verified_jd_rows', '-')}</b>已核验 JD</div><div class="metric"><b>{summary.get('pending_discovery_companies', '-')}</b>待发现</div><div class="metric"><b>{summary.get('error_companies', '-')}</b>错误</div></div>
<h2>批次关键报告</h2><table><thead><tr><th>批次</th><th>文件</th></tr></thead><tbody>{_batch_links(processed, batches_dir)}</tbody></table>
<h2>结果文件</h2><table><tr><td><a href="/view/exports/targets/applicable_companies.csv">可投公司</a></td><td><a href="/view/exports/targets/job_targets.csv">候选 JD</a></td><td><a href="/view/exports/targets/verified_companies.csv">已核验公司</a></td></tr><tr><td><a href="/view/exports/targets/verified_job_targets.csv">已核验 JD</a></td><td><a href="/view/exports/targets/pending_discovery_companies.csv">待发现</a></td><td><a href="/view/exports/targets/error_companies.csv">错误</a></td></tr><tr><td colspan="3"><a href="/view/exports/targets/summary.json">summary</a></td></tr></table>
<h2>监控</h2><table><tr><th>下一个批次</th><td>batch_{int(state.get('next_batch', 3)):03d}</td></tr><tr><th>已处理</th><td>{', '.join(f'batch_{n:03d}' for n in processed) or '-'}</td></tr><tr><th>更新时间</th><td>{escape(str(state.get('updated_at', '')))}</td></tr></table></body></html>"""


def publish(state: dict, state_path: Path = STATE_PATH, slot_state: Path = SLOT_STATE_PATH,
            slot_html: Path = SLOT_HTML_PATH, batches_dir: Path = BATCHES,
            board_state: Path | None = None) -> None:
    state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    atomic_write(state_path, payload)
    atomic_write(slot_state, payload)
    atomic_write(board_state or slot_html.with_suffix(".json"), payload)
    atomic_write(slot_html, render_status(state, batches_dir))


def analyze_batch(number: int, state: dict, *, root: Path = ROOT) -> dict:
    batches_dir = root / "data" / "batches"
    batch_dir = batches_dir / f"batch_{number:03d}"
    complete_path = batch_dir / "complete.json"
    complete = read_json(complete_path)
    if not complete:
        raise RuntimeError(f"invalid or incomplete marker: {complete_path}")
    circuit_open = complete.get("state") in {"circuit_open", "blocked"}

    candidate_paths = [root / "data" / "first_batch" / "first_batch_candidates.csv"]
    for batch in sorted(batches_dir.glob("batch_[0-9][0-9][0-9]"), key=lambda path: batch_number(path) or 0):
        batch_no = batch_number(batch)
        if batch_no is not None and 2 <= batch_no <= number and (batch / "complete.json").is_file():
            candidate_paths.append(batch / "candidates.csv")

    previous_summary = read_json(root / "exports" / "targets" / "summary.json")
    previous_companies = _ids(root / "exports" / "targets" / "applicable_companies.csv", "company_id")
    previous_jobs = _ids(root / "exports" / "targets" / "job_targets.csv", "jd_id")
    with tempfile.TemporaryDirectory(prefix="jobprep-analysis-", dir=root / "data") as temp:
        stage = Path(temp)
        combined = stage / "analysis_candidates.csv"
        candidate_rows, processed_companies = merge_candidates(candidate_paths, combined)
        stage_output = stage / "targets"
        summary = update_application_targets(
            combined, complete_path, root / "data" / "workstation.sqlite3", stage_output,
            force=True, audit_path=root / "data" / "audits" / "incomplete_review.csv",
        )
        new_companies = _ids(stage_output / "applicable_companies.csv", "company_id") - previous_companies
        new_jobs = _ids(stage_output / "job_targets.csv", "jd_id") - previous_jobs
        preserved = {
            key: previous_summary[key]
            for key in ("criteria_recovery", "audit", "remediation", "special_adapter")
            if key in previous_summary
        }
        summary.update(preserved)
        deferred_companies = int(
            complete.get("deferred_count")
            or (complete.get("counts") or {}).get("deferred")
            or 0
        )
        processed_company_count = max(processed_companies - deferred_companies, 0)
        summary.update({
            "trigger": f"batch_{number:03d}_complete",
            "analysis_snapshot_companies": processed_companies,
            "processed_companies": processed_company_count,
            "deferred_companies": deferred_companies,
            "remaining_companies": max(
                int(state.get("total_companies", 176)) - processed_company_count, 0
            ),
            "candidate_rows": candidate_rows,
            "processed_batches": [*state.get("processed_batches", []), number],
            "runner_state": str(complete.get("state") or "completed"),
            "batch_delta": {
                "batch": f"batch_{number:03d}",
                "new_applicable_companies": len(new_companies),
                "new_jds": len(new_jobs),
                "cumulative_applicable_companies": summary.get("applicable_companies", 0),
                "cumulative_jds": summary.get("jd_rows", 0),
            },
        })
        summary["outputs"] = {
            "companies": str((root / "exports" / "targets" / "applicable_companies.csv").resolve()),
            "verified_companies": str((root / "exports" / "targets" / "verified_companies.csv").resolve()),
            "pending_discovery": str((root / "exports" / "targets" / "pending_discovery_companies.csv").resolve()),
            "errors": str((root / "exports" / "targets" / "error_companies.csv").resolve()),
            "jobs": str((root / "exports" / "targets" / "job_targets.csv").resolve()),
            "verified_jobs": str((root / "exports" / "targets" / "verified_job_targets.csv").resolve()),
        }
        (stage_output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        output = root / "exports" / "targets"
        output.mkdir(parents=True, exist_ok=True)
        for name in OUTPUT_NAMES:
            os.replace(stage_output / name, output / name)

    source_end = int(complete.get("source_end_one_based") or 0)
    total = int(state.get("total_companies", 176))
    return {
        "stop": circuit_open or source_end >= total,
        "reason": (
            "runner_circuit_open" if circuit_open
            else "runner_completed" if source_end >= total
            else "batch_completed"
        ),
        "summary": summary,
        "complete": complete,
    }


def run_monitor(*, start_batch: int = 3, interval: float = 10.0, once: bool = False,
                root: Path = ROOT) -> int:
    batches_dir = root / "data" / "batches"
    state_path = root / "data" / "agent_reports" / "subagent4.json"
    slot_state = root / "data" / "agent_reports" / "subagent2.json"
    slot_html = root / "exports" / "operations_dashboard" / "subagent2.html"
    board_state = root / "exports" / "operations_dashboard" / "subagent2.json"
    state = read_json(state_path)
    processed = sorted({int(value) for value in state.get("processed_batches", [])})
    relevant_processed = [value for value in processed if value >= start_batch]
    next_batch = max(relevant_processed, default=start_batch - 1) + 1
    state.update({
        "agent": "subagent4", "dashboard_slot": "subagent2", "status": "waiting",
        "phase": f"等待 batch_{next_batch:03d}/complete.json", "processed_batches": processed,
        "next_batch": next_batch, "total_companies": int(state.get("total_companies", 176)),
        "safety": {"runs_scraping": False, "runs_ocr": False},
    })
    while True:
        batch_dir = batches_dir / f"batch_{next_batch:03d}"
        complete_path = batch_dir / "complete.json"
        if not complete_path.is_file():
            publish(state, state_path, slot_state, slot_html, batches_dir, board_state)
            if once:
                return 0
            time.sleep(max(interval, 1.0))
            continue
        state.update(status="running", phase=f"增量分析 batch_{next_batch:03d}")
        publish(state, state_path, slot_state, slot_html, batches_dir, board_state)
        result = analyze_batch(next_batch, state, root=root)
        processed.append(next_batch)
        circuit_open = result.get("reason") == "runner_circuit_open"
        state.update(
            status="completed_with_deferred" if circuit_open else "completed" if result.get("stop") else "waiting",
            phase=(
                f"batch_{next_batch:03d} 熔断状态已纳入分析，保留 deferred"
                if circuit_open else
                "runner 全部批次完成" if result.get("stop") else
                f"batch_{next_batch:03d} 已更新，等待下一批"
            ),
            processed_batches=processed, summary=result.get("summary", {}),
            last_complete=str(complete_path), stop_reason=result.get("reason") if result.get("stop") else "",
        )
        next_batch += 1
        state["next_batch"] = next_batch
        publish(state, state_path, slot_state, slot_html, batches_dir, board_state)
        if result.get("stop") or once:
            return 2 if circuit_open else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="持续消费已完成招聘批次；不运行抓取或 OCR。")
    parser.add_argument("--start-batch", type=int, default=3)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run_monitor(start_batch=args.start_batch, interval=args.interval, once=args.once))


if __name__ == "__main__":
    main()
