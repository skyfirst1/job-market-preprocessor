from __future__ import annotations

import argparse
import asyncio
import csv
import json
from pathlib import Path
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.pipeline import Workstation
from jobprep.settings import load_settings, options_for_url
from scripts.report_agent_status import update as update_dashboard


TARGET_ADAPTERS = {"MokahrPublicPortal", "HotjobPublicPortal"}
COMPANY_NAMES = {
    "f3d8b9574f2ba8f497de125f": "京东方-博士专项医疗类",
    "aa47b5f48ec21d18c0ff0adb": "信立泰药业",
    "0d255a76972d61bca33ad371": "一心堂",
    "2a6fcbd3ddf304830ef04123": "九州通医药",
    "584b8140448836795ec3e021": "亿帆医药",
    "d7cf6df6791ef0e582847414": "仲景宛西制药",
    "52107deea3d314934348d5b0": "信达生物",
    "366d752a69814f11922e6c60": "先声药业集团",
    "fd29023d6010204227ca650f": "阿里巴巴千问办公",
}
REQUIRED_COMPANIES = {"信立泰药业", "九州通医药", "仲景宛西制药", "先声药业集团"}
MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "锟斤拷", "烫烫烫")
COLUMNS = ("company", "task_id", "adapter", "source_url", "before_status", "after_status",
           "before_jobs", "after_jobs", "added_jobs", "before_details", "after_details",
           "list_complete", "stop_reason", "outcome", "preserved_prior", "error")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def payload(task: dict) -> dict:
    try:
        value = json.loads(task.get("result_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def metrics(task: dict, result: dict) -> dict:
    coverage = result.get("coverage") or {}
    return {"status": task.get("status"), "jobs": len(result.get("jobs") or []),
            "details": len(coverage.get("detail_urls") or []),
            "list_complete": coverage.get("list_complete") is True,
            "stop_reason": coverage.get("stop_reason")}


def quality(value: dict) -> tuple[int, int, int, int]:
    rank = {"ok": 3, "partial": 2, "pending_ocr": 2, "error": 0, "blocked": 0}.get(value["status"], 0)
    return rank, int(value["list_complete"]), value["jobs"], value["details"]


def blocked_reason(url: str) -> tuple[str, str]:
    if "campus.51job.com" in url:
        return ("adapter_not_implemented_in_stage",
                "Static campaign page exposes 11 innoventbio.zhiye.com jobAdId detail links; "
                "next step is bounded HTML link extraction followed by Zhiye detail/API collection")
    return ("deferred_unstable_or_single_site",
            "Single-site or unstable endpoint deferred outside the Mokahr/Hotjob stage")


def canonical_source_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                       parts.query, parts.fragment))


def company_map_from_raw(path: Path) -> dict[str, str]:
    mapping = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            company = (row.get("公司名称") or "").strip()
            for field in ("公告链接", "投递链接"):
                url = (row.get(field) or "").strip()
                if company and url.startswith(("http://", "https://")):
                    mapping[canonical_source_url(url)] = company
    return mapping


def looks_mojibake(value: str) -> bool:
    if "\ufffd" in value or any(marker in value for marker in MOJIBAKE_MARKERS):
        return True
    if any(0x80 <= ord(char) <= 0x9F for char in value):
        return True
    for encoding in ("latin1", "gb18030"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired != value and any("\u4e00" <= char <= "\u9fff" for char in repaired):
            return True
    return False


def assert_clean_report(report: dict) -> None:
    names = [str(item.get("company") or "")
             for group in (report.get("items") or [], report.get("blocked") or [])
             for item in group]
    missing = REQUIRED_COMPANIES.difference(names)
    if missing:
        raise ValueError(f"required company names missing: {sorted(missing)}")
    bad = [name for name in names if not name or looks_mojibake(name)]
    if bad:
        raise ValueError("company names contain mojibake or invalid text")


def finalize_report(report: dict, company_by_url: dict[str, str] | None = None) -> dict:
    report["state"] = "completed"
    company_by_url = company_by_url or {}
    for collection in (report.get("items") or [], report.get("blocked") or []):
        for item in collection:
            mapped = company_by_url.get(canonical_source_url(item.get("source_url", "")))
            item["company"] = mapped or COMPANY_NAMES.get(item.get("task_id"), item.get("company", ""))
    for item in report.get("items") or []:
        if item.get("task_id") == "aa47b5f48ec21d18c0ff0adb" and item.get("outcome") == "unchanged":
            item["outcome"] = "blocked"
            item["reason"] = "hotjob_suite_route_not_stable_from_root"
            item["next_interface_direction"] = (
                "Persist the suite key discovered from config API responses, then call the bounded "
                "positionInfo/listPosition API without repeating landing-page discovery"
            )
    for item in report.get("blocked") or []:
        reason, direction = blocked_reason(item.get("source_url", ""))
        item["reason"] = reason
        item["next_interface_direction"] = direction
    by_adapter = {}
    for item in report.get("items") or []:
        bucket = by_adapter.setdefault(item["adapter"], {"cases": 0, "fixed": 0, "added_jobs": 0})
        bucket["cases"] += 1
        bucket["fixed"] += item.get("outcome") == "fixed"
        bucket["added_jobs"] += int(item.get("added_jobs") or 0)
    report["by_adapter"] = by_adapter
    processed_blocked = sum(item.get("outcome") == "blocked" for item in report.get("items") or [])
    deferred_blocked = len(report.get("blocked") or [])
    report["summary"] = {
        "processed": len(report.get("items") or []),
        "fixed": sum(item.get("outcome") == "fixed" for item in report.get("items") or []),
        "blocked": processed_blocked + deferred_blocked,
        "processed_blocked": processed_blocked,
        "deferred_blocked": deferred_blocked,
        "jobs_added": sum(int(item.get("added_jobs") or 0) for item in report.get("items") or []),
        "by_adapter": by_adapter,
    }
    return report


def finalize_files(json_path: Path, csv_path: Path, canonical_csv: Path | None = None) -> dict:
    company_by_url = company_map_from_raw(canonical_csv) if canonical_csv is not None else {}
    report = finalize_report(json.loads(json_path.read_text(encoding="utf-8")), company_by_url)
    atomic_json(json_path, report)
    write_csv(csv_path, report.get("items") or [])
    reread = json.load(json_path.open(encoding="utf-8"))
    assert_clean_report(reread)
    if reread.get("summary") != report["summary"] or reread.get("state") != "completed":
        raise ValueError("UTF-8 report read-back verification failed")
    return report


async def run(root: Path, remediation_path: Path, json_path: Path, csv_path: Path) -> dict:
    remediation = json.loads(remediation_path.read_text(encoding="utf-8"))
    unfinished = [item for item in remediation.get("items", []) if not item.get("fixed")]
    workstation = Workstation(load_settings(root))
    selected = []
    deferred = []
    for item in unfinished:
        adapter = workstation.adapters.select(item["source_url"])
        if adapter.name in TARGET_ADAPTERS:
            selected.append((item, adapter.name))
        else:
            reason, direction = blocked_reason(item["source_url"])
            deferred.append({"company": COMPANY_NAMES.get(item["task_id"], item["company"]),
                             "task_id": item["task_id"],
                             "source_url": item["source_url"], "adapter": adapter.name,
                             "outcome": "blocked", "reason": reason,
                             "next_interface_direction": direction})
    rows = []
    update_dashboard("subagent1", {"role": "Mokahr/Hotjob 专站 adapter 阶段修复",
        "phase": "special_adapter_stage", "status": "running", "batch": "special_adapters",
        "candidates": len(selected), "processed": 0, "success": 0, "partial": 0,
        "failed": 0, "errors": 0, "artifacts": [str(json_path), str(csv_path)],
        "next_step": "仅运行审计报告中 fixed=false 且命中 Mokahr/Hotjob 的任务"})
    for index, (item, adapter_name) in enumerate(selected, 1):
        task = workstation.store.task(item["task_id"])
        before_result = payload(task)
        before = metrics(task, before_result)
        options = options_for_url(workstation.config, task["url"])
        options.update({"max_pages": 30 if adapter_name == "MokahrPublicPortal" else 50,
                        "max_jobs": 3000 if adapter_name == "MokahrPublicPortal" else 1500,
                        "timeout": 35, "interval": 0.5, "retries": 2, "backoff": 1.0,
                        "max_json_responses": 300, "settle_ms": 1800})
        result = await workstation.adapters.acquire(task["url"], workstation.artifacts,
                                                    options=options, tools=workstation.tools)
        after = metrics({"status": result.get("status")}, result)
        preserved = quality(after) < quality(before)
        if preserved:
            result = before_result
            after = before
        else:
            result.setdefault("remediation", {}).update(source="special_adapter_stage",
                adapter=adapter_name, prior_artifact_preserved=False)
            workstation.store.finish(task["id"], result.get("status", task["status"]), result)
        outcome = "fixed" if after["jobs"] > before["jobs"] else "unchanged"
        row = {"company": COMPANY_NAMES.get(task["id"], item["company"]),
               "task_id": task["id"], "adapter": adapter_name,
               "source_url": task["url"], "before_status": before["status"],
               "after_status": after["status"], "before_jobs": before["jobs"],
               "after_jobs": after["jobs"], "added_jobs": max(0, after["jobs"] - before["jobs"]),
               "before_details": before["details"], "after_details": after["details"],
               "list_complete": after["list_complete"], "stop_reason": after["stop_reason"],
               "outcome": outcome, "preserved_prior": preserved,
               "error": task.get("error") or result.get("error") or ""}
        rows.append(row)
        atomic_json(json_path, {"state": "running", "items": rows, "blocked": deferred})
        write_csv(csv_path, rows)
        update_dashboard("subagent1", {"processed": index,
            "success": sum(row["outcome"] == "fixed" for row in rows),
            "partial": sum(row["outcome"] == "unchanged" for row in rows),
            "failed": 0, "errors": 0,
            "next_step": "继续受控专站修复" if index < len(selected) else "生成阶段报告"})
    report = {"state": "completed", "scope": {"adapters": sorted(TARGET_ADAPTERS),
              "selected": len(selected), "excluded_ok_and_b": True, "wechat_revisited": False,
              "ocr_calls": 0}, "fixed": sum(row["outcome"] == "fixed" for row in rows),
              "added_jobs": sum(row["added_jobs"] for row in rows),
              "items": rows, "blocked": deferred}
    canonical_csv = root / "job_market_raw.csv"
    report = finalize_report(report, company_map_from_raw(canonical_csv))
    assert_clean_report(report)
    atomic_json(json_path, report)
    write_csv(csv_path, report["items"])
    update_dashboard("subagent1", {"status": "completed", "processed": len(rows),
        "success": report["fixed"], "partial": len(rows) - report["fixed"],
        "failed": 0, "errors": 0, "next_step": "交付 Mokahr/Hotjob 阶段报告"})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the bounded Mokahr/Hotjob adapter remediation stage")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--remediation", type=Path, default=Path("data/audits/remediation_report.json"))
    parser.add_argument("--json", type=Path, default=Path("data/audits/special_adapter_report.json"))
    parser.add_argument("--csv", type=Path, default=Path("data/audits/special_adapter_report.csv"))
    parser.add_argument("--finalize-only", action="store_true",
                        help="Repair encoding/terminal metadata in an existing report without network access")
    args = parser.parse_args()
    root = args.root.resolve()
    resolve = lambda value: value if value.is_absolute() else root / value
    if args.finalize_only:
        report = finalize_files(resolve(args.json), resolve(args.csv),
                                root / "job_market_raw.csv")
    else:
        report = asyncio.run(run(root, resolve(args.remediation), resolve(args.json), resolve(args.csv)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
