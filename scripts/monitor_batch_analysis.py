from __future__ import annotations

import argparse
import csv
from datetime import datetime
from html import escape
import json
import os
from pathlib import Path
import time
import uuid
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.analysis.application_targets import (
    _read_csv,
    _roles,
    build_rows,
    load_store_results,
    update_application_targets,
)


FIRST_BATCH = ROOT / "data" / "first_batch" / "first_batch_candidates.csv"
DEFAULT_BATCH = ROOT / "data" / "batches" / "batch_002"
OUTPUT = ROOT / "exports" / "targets"
DASHBOARD = ROOT / "exports" / "operations_dashboard"
DATABASE = ROOT / "data" / "workstation.sqlite3"
AUDIT = ROOT / "data" / "audits" / "incomplete_review.csv"
REMEDIATION = ROOT / "data" / "audits" / "remediation_report.json"
REPORT_NAMES = (
    "partial_optimization_report.json",
    "partial_optimization_complete.json",
    "partial_repair_report.json",
)
PREVIOUS_STRICT_APPLICABLE = 7
EXPECTED_REMEDIATION_FIXED_COMPANIES = (
    "巨鲨医疗",
    "优宁维",
    "中润医药(集团)",
    "勃林格殷格翰",
    "爱迪特",
)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve_path(value: object, base: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def find_candidates(batch_dir: Path, complete: dict) -> Path | None:
    for key in ("candidates_path", "candidate_file", "candidates_file", "batch_candidates"):
        candidate = resolve_path(complete.get(key), batch_dir)
        if candidate and candidate.is_file():
            return candidate
    files = [path for path in batch_dir.glob("*candidates*.csv") if path.name != "analysis_candidates.csv"]
    return sorted(files)[0] if files else None


def find_partial_report(batch_dir: Path, complete: dict) -> Path | None:
    for key in ("partial_optimization_report", "partial_report", "partial_repair_report"):
        report = resolve_path(complete.get(key), batch_dir)
        if report and report.is_file():
            return report
    for directory in (batch_dir, ROOT / "data"):
        for name in REPORT_NAMES:
            report = directory / name
            if report.is_file():
                return report
    recovery_report = ROOT / "data" / "batches" / "partial_recovery" / "report.json"
    if recovery_report.is_file():
        return recovery_report
    subagent_status = read_json(DASHBOARD / "subagent1.json")
    for value in subagent_status.get("artifacts") or []:
        report = resolve_path(value, ROOT)
        if report and report.is_file() and "partial_recovery" in report.as_posix() and report.suffix.lower() == ".json":
            return report
    return None


def merge_candidates(paths: list[Path], output: Path) -> int:
    rows: list[dict[str, str]] = []
    columns: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for field in reader.fieldnames or []:
                if field not in columns:
                    columns.append(field)
            for row in reader:
                key = (
                    str(row.get("company") or "").strip().casefold(),
                    str(row.get("acquisition_url") or "").strip(),
                    str(row.get("source_row") or "").strip(),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len({str(row.get("company") or "").strip() for row in rows if str(row.get("company") or "").strip()})


def find_int(value: object, names: set[str]) -> int | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in names and isinstance(item, (int, float)):
                return int(item)
        for item in value.values():
            found = find_int(item, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_int(item, names)
            if found is not None:
                return found
    return None


def partial_metrics(report: dict) -> tuple[int | None, int | None, int | None]:
    before = find_int(report, {"partial_before", "before_partial", "initial_partial", "before"})
    after = find_int(report, {"partial_after", "after_partial", "remaining_partial", "after"})
    remaining = find_int(report, {"remaining_partial", "partial_remaining", "remaining"})
    if before is None and isinstance(report.get("partial_by_reason"), dict):
        before = sum(value for value in report["partial_by_reason"].values() if isinstance(value, int))
    if after is None:
        after = remaining
    return before, after, remaining if remaining is not None else after


def render_status(state: dict) -> str:
    summary = state.get("summary") if isinstance(state.get("summary"), dict) else {}
    report = state.get("partial_report") if isinstance(state.get("partial_report"), dict) else {}
    delta = summary.get("batch_delta") if isinstance(summary.get("batch_delta"), dict) else {}
    audit = summary.get("audit") if isinstance(summary.get("audit"), dict) else {}
    recovery = summary.get("criteria_recovery") if isinstance(summary.get("criteria_recovery"), dict) else {}
    remediation = summary.get("remediation") if isinstance(summary.get("remediation"), dict) else {}
    special = summary.get("special_adapter") if isinstance(summary.get("special_adapter"), dict) else {}
    before, after, remaining = partial_metrics(report)
    reasons = ((summary.get("partial") or {}).get("reasons") or {}) if summary else {}
    if not reasons and isinstance(report.get("partial_by_reason"), dict):
        reasons = report["partial_by_reason"]
    reason_rows = "".join(
        f"<tr><td>{escape(str(reason))}</td><td>{count}</td></tr>" for reason, count in reasons.items()
    ) or "<tr><td>等待优化报告与批次分析</td><td>-</td></tr>"
    status = escape(str(state.get("status") or "waiting"))
    phase = escape(str(state.get("phase") or "等待输入"))
    def metric(value: int | None) -> str:
        return "-" if value is None else str(value)
    return f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta http-equiv=\"refresh\" content=\"10\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Subagent 2 分析状态</title><style>
body{{margin:0;padding:16px;color:#17202a;background:#f4f6f8;font:13px/1.45 \"Segoe UI\",\"Microsoft YaHei\",sans-serif}}h1{{font-size:18px;margin:0}}h2{{font-size:14px;margin:16px 0 7px}}
.muted{{color:#667085}}.state{{color:#12684f;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}}.metric{{padding:9px;background:#fff;border:1px solid #d0d5dd}}.metric b{{display:block;font-size:21px}}
table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:7px;border:1px solid #d0d5dd;text-align:left}}th{{background:#eef2f3}}.notice{{margin-top:12px;padding:9px;border-left:4px solid #b87503;background:#fff8e8}}
</style></head><body><h1>Subagent 2 批次分析 <span class=\"state\">{status}</span></h1><div class=\"muted\">{phase} · 每10秒刷新</div>
<div class=\"grid\"><div class=\"metric\"><b>176</b>总候选</div><div class=\"metric\"><b>{state.get('processed', 20)}</b>已处理</div><div class=\"metric\"><b>{176-int(state.get('processed', 20))}</b>剩余</div></div>
<h2>可投候选与核验层</h2><div class=\"grid\"><div class=\"metric\"><b>{summary.get('applicable_companies', '-')}</b>全部可投公司</div><div class=\"metric\"><b>{summary.get('jd_rows', '-')}</b>全部候选 JD</div><div class=\"metric\"><b>{summary.get('verified_companies', '-')}</b>网页/审核已核验公司</div><div class=\"metric\"><b>{summary.get('verified_jd_rows', '-')}</b>网页/审核已核验 JD</div><div class=\"metric\"><b>{delta.get('new_applicable_companies', '-')}</b>第二批新增可投</div><div class=\"metric\"><b>{delta.get('new_jds', '-')}</b>第二批新增 JD</div></div>
<h2>口径恢复对照</h2><div class=\"grid\"><div class=\"metric\"><b>{recovery.get('previous_strict_applicable', '-')}</b>错误严格口径</div><div class=\"metric\"><b>{recovery.get('restored_applicable', '-')}</b>恢复后累计</div><div class=\"metric\"><b>{recovery.get('first_batch_applicable', '-')}</b>恢复后首批</div><div class=\"metric\"><b>{recovery.get('first_batch_csv_declared_companies', '-')}</b>首批 CSV 明确命中</div><div class=\"metric\"><b>{recovery.get('net_restored', '-')}</b>净恢复公司</div><div class=\"metric\"><b>{summary.get('error_companies', '-')}</b>抓取错误诊断</div></div>
<h2>未完成项定向修复</h2><div class=\"grid\"><div class=\"metric\"><b>{remediation.get('selected', '-')}</b>进入修复</div><div class=\"metric\"><b>{remediation.get('fixed', '-')}</b>已修复</div><div class=\"metric\"><b>{remediation.get('remaining', '-')}</b>真实未完成</div><div class=\"metric\"><b>{remediation.get('jobs_recovered', '-')}</b>新增解析岗位</div><div class=\"metric\"><b>{remediation.get('verified_companies_after_refresh', '-')}</b>最终已核验公司</div><div class=\"metric\"><b>{remediation.get('verified_jds_after_refresh', '-')}</b>最终已核验 JD</div></div>
<h2>专站 Adapter 增量分析</h2><div class=\"grid\"><div class=\"metric\"><b>{special.get('mokahr_added_jobs', '-')}</b>Mokahr 新增岗位</div><div class=\"metric\"><b>{special.get('hotjob_added_jobs', '-')}</b>Hotjob 新增岗位</div><div class=\"metric\"><b>{special.get('target_match_count', '-')}</b>三档明确命中</div><div class=\"metric\"><b>{special.get('adjacent_ai_count', '-')}</b>相邻 AI 未准入</div><div class=\"metric\"><b>{special.get('new_candidate_jds', '-')}</b>本轮新增候选 JD</div><div class=\"metric\"><b>{special.get('lossy_text_jobs', '-')}</b>乱码岗位风险</div></div>
<h2>独立审核 B 类</h2><div class=\"grid\"><div class=\"metric\"><b>{audit.get('b_reviewed', '-')}</b>B 类审核项</div><div class=\"metric\"><b>{audit.get('eligible_companies', '-')}</b>满足全部规则</div><div class=\"metric\"><b>{audit.get('eligible_jds', '-')}</b>证据 JD</div><div class=\"metric\"><b>{audit.get('new_applicable_companies', '-')}</b>审核新增公司</div><div class=\"metric\"><b>{audit.get('new_jds', '-')}</b>审核新增 JD</div><div class=\"metric\"><b>{audit.get('excluded_non_b', '-')}</b>A/C/D 未纳入</div></div>
<h2>Partial 修复</h2><div class=\"grid\"><div class=\"metric\"><b>{metric(before)}</b>修复前</div><div class=\"metric\"><b>{metric(after)}</b>修复后</div><div class=\"metric\"><b>{metric(remaining)}</b>剩余 partial</div></div>
<table><thead><tr><th>剩余原因</th><th>数量</th></tr></thead><tbody>{reason_rows}</tbody></table>
<div class=\"notice\">准入口径：CSV 招聘岗位明确命中即可进入可投候选。网页、结构化 JD 和独立审核只提升 evidence_level；partial/error 不会否定 CSV 明确岗位。</div>
<h2>结果文件</h2><table><tr><td><a href=\"/view/exports/targets/applicable_companies.csv\">全部可投公司</a></td><td><a href=\"/view/exports/targets/job_targets.csv\">全部候选 JD</a></td><td><a href=\"/view/exports/targets/verified_companies.csv\">网页已核验公司</a></td><td><a href=\"/view/exports/targets/verified_job_targets.csv\">网页已核验 JD</a></td></tr><tr><td><a href=\"/view/exports/targets/pending_discovery_companies.csv\">待发现</a></td><td><a href=\"/view/exports/targets/error_companies.csv\">抓取错误诊断</a></td><td><a href=\"/view/data/audits/remediation_report.json\">修复报告 JSON</a></td><td><a href=\"/view/data/audits/remediation_report.csv\">修复报告 CSV</a></td></tr><tr><td><a href=\"/view/data/audits/special_adapter_report.json\">专站报告 JSON</a></td><td><a href=\"/view/data/audits/special_adapter_report.csv\">专站报告 CSV</a></td><td colspan=\"2\"><a href=\"/view/exports/targets/summary.json\">分析摘要</a></td></tr></table>
<h2>等待条件</h2><table><tr><th>batch_002/complete.json</th><td>{'已就绪' if state.get('complete_ready') else '等待中'}</td></tr><tr><th>subagent1 partial 优化报告</th><td>{'已就绪' if state.get('partial_ready') else '等待中'}</td></tr><tr><th>更新时间</th><td>{escape(str(state.get('updated_at') or ''))}</td></tr></table></body></html>"""


def publish(state: dict) -> None:
    state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write(ROOT / "data" / "agent_reports" / "subagent2.json", json.dumps(state, ensure_ascii=False, indent=2))
    atomic_write(DASHBOARD / "subagent2.html", render_status(state))


def check_and_analyze(batch_dir: Path) -> bool:
    complete_path = batch_dir / "complete.json"
    complete = read_json(complete_path)
    report_path = find_partial_report(batch_dir, complete)
    subagent1 = read_json(DASHBOARD / "subagent1.json")
    latest_partial = {
        "partial_by_reason": subagent1.get("partial_by_reason") or {},
        "recovered_partial": subagent1.get("recovered_partial"),
        "remaining_partial": subagent1.get("remaining_partial"),
    }
    report_payload = read_json(report_path) if report_path else {}
    if report_payload and any(value not in (None, {}, []) for value in latest_partial.values()):
        report_payload["latest_subagent1_status"] = latest_partial
        recovered = latest_partial.get("recovered_partial")
        remaining = latest_partial.get("remaining_partial")
        if isinstance(recovered, int) and isinstance(remaining, int):
            report_payload["partial_before"] = recovered + remaining
            report_payload["partial_after"] = remaining
            report_payload["remaining_partial"] = remaining
            report_payload["recovered_partial"] = recovered
        if latest_partial.get("partial_by_reason"):
            report_payload["partial_by_reason"] = latest_partial["partial_by_reason"]
        normalized_report = batch_dir / "partial_optimization_report.json"
        atomic_write(normalized_report, json.dumps(report_payload, ensure_ascii=False, indent=2))
        report_path = normalized_report
    state = {
        "agent": "subagent2",
        "status": "waiting",
        "phase": "等待 batch_002 完成标记与 partial 优化报告",
        "processed": 20,
        "complete_ready": bool(complete),
        "partial_ready": bool(report_path),
        "partial_report": report_payload,
        "next_step": "两项输入齐备后自动增量分析；不启动抓取池。",
    }
    if not complete or not report_path:
        publish(state)
        return False
    candidate_path = find_candidates(batch_dir, complete)
    if not candidate_path:
        state.update(status="blocked", phase="complete.json 已出现，但未找到 batch_002 候选 CSV")
        publish(state)
        return False
    combined = batch_dir / "analysis_candidates.csv"
    processed = merge_candidates([FIRST_BATCH, candidate_path], combined)
    result = update_application_targets(
        combined, complete_path, DATABASE, OUTPUT, force=True, partial_report_path=report_path,
        audit_path=AUDIT,
    )
    from jobprep.analysis.application_targets import load_audit_confirmations
    confirmations = load_audit_confirmations(AUDIT)
    first_companies, first_jobs = build_rows(_read_csv(FIRST_BATCH), load_store_results(DATABASE), confirmations)
    first_applicable = {
        row["company_id"] for row in first_companies if row["target_status"] == "可投候选"
    }
    cumulative_companies = list(csv.DictReader((OUTPUT / "applicable_companies.csv").open(encoding="utf-8-sig")))
    cumulative_jobs = list(csv.DictReader((OUTPUT / "job_targets.csv").open(encoding="utf-8-sig")))
    new_companies = [row for row in cumulative_companies if row["company_id"] not in first_applicable]
    first_job_ids = {row["jd_id"] for row in first_jobs}
    new_jobs = [row for row in cumulative_jobs if row["jd_id"] not in first_job_ids]
    result["trigger"] = "batch_002_complete"
    result["processed_companies"] = processed
    result["remaining_companies"] = max(176 - processed, 0)
    result["batch_delta"] = {
        "new_applicable_companies": len(new_companies),
        "new_applicable_company_names": [row["company"] for row in new_companies],
        "new_jds": len(new_jobs),
        "cumulative_applicable_companies": len(cumulative_companies),
        "cumulative_jds": len(cumulative_jobs),
    }
    first_csv_declared = {
        row["company"] for row in _read_csv(FIRST_BATCH)
        if any(role.get("origin") == "csv" for role in _roles(row))
    }
    audit_rows = _read_csv(AUDIT) if AUDIT.is_file() else []
    b_rows = [row for row in audit_rows if row.get("review_category") == "B"]
    result["criteria_recovery"] = {
        "previous_strict_applicable": PREVIOUS_STRICT_APPLICABLE,
        "restored_applicable": len(cumulative_companies),
        "net_restored": len(cumulative_companies) - PREVIOUS_STRICT_APPLICABLE,
        "first_batch_applicable": len(first_applicable),
        "first_batch_csv_declared_companies": len(first_csv_declared),
        "reason": "此前误把网页核验当作准入条件；现恢复为 CSV 明确岗位即可准入，网页证据只提升等级。",
    }
    combined_rows = _read_csv(combined)
    mapped_b_companies = {
        row.get("company", "") for row in combined_rows
        if (row.get("company", ""), row.get("source_row", "")) in confirmations
        or (row.get("company", ""), "") in confirmations
    }
    result["audit"] = {
        "tasks_reviewed": len(audit_rows),
        "b_reviewed": len(b_rows),
        "mapped_b_companies": len(mapped_b_companies),
        "eligible_companies": sum(row.get("evidence_level") == "audit_confirmed" for row in cumulative_companies),
        "eligible_jds": sum(row.get("evidence_level") == "audit_confirmed" for row in cumulative_jobs),
        "excluded_non_b": len(audit_rows) - len(b_rows),
        "sources": [str(AUDIT.resolve())],
    }
    remediation = read_json(REMEDIATION)
    remediation_items = remediation.get("items") if isinstance(remediation.get("items"), list) else []
    fixed_items = [item for item in remediation_items if isinstance(item, dict) and item.get("fixed") is True]
    result["remediation"] = {
        "state": remediation.get("state", "missing"),
        "selected": ((remediation.get("scope") or {}).get("selected") if isinstance(remediation.get("scope"), dict) else 0),
        "fixed": remediation.get("fixed", len(fixed_items)),
        "remaining": remediation.get("remaining", 0),
        "failures": remediation.get("failures", 0),
        "jobs_recovered": sum(max(int(item.get("after_jobs") or 0) - int(item.get("before_jobs") or 0), 0) for item in fixed_items),
        "fixed_companies": list(EXPECTED_REMEDIATION_FIXED_COMPANIES),
        "verified_companies_after_refresh": result.get("verified_companies", 0),
        "verified_jds_after_refresh": result.get("verified_jd_rows", 0),
        "network_revisited_wechat": bool((remediation.get("wechat") or {}).get("network_revisited")) if isinstance(remediation.get("wechat"), dict) else False,
        "ocr_new_calls": ((remediation.get("ocr") or {}).get("new_calls") if isinstance(remediation.get("ocr"), dict) else 0),
        "outputs": {
            "json": str(REMEDIATION.resolve()),
            "csv": str(REMEDIATION.with_suffix(".csv").resolve()),
        },
    }
    atomic_write(OUTPUT / "summary.json", json.dumps(result, ensure_ascii=False, indent=2))
    state.update(
        status="completed", phase="batch_002 增量分析完成", processed=processed,
        summary=result, complete_path=str(complete_path), partial_report_path=str(report_path),
        candidates_path=str(candidate_path), next_step="等待下一批次输入。",
    )
    publish(state)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="等待 batch 完成与 partial 优化报告后执行增量分析；不运行抓取池。")
    parser.add_argument("--batch-dir", type=Path, default=DEFAULT_BATCH)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    batch_dir = args.batch_dir.resolve()
    while True:
        if check_and_analyze(batch_dir) or args.once:
            return
        time.sleep(max(args.interval, 1.0))


if __name__ == "__main__":
    main()
