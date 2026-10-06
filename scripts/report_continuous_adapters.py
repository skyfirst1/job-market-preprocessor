from __future__ import annotations

import csv
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
BATCHES = ROOT / "data" / "batches"
STORE = ROOT / "data" / "workstation.sqlite3"
JSON_OUT = ROOT / "data" / "audits" / "continuous_adapter_report.json"
CSV_OUT = ROOT / "data" / "audits" / "continuous_adapter_report.csv"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def main() -> None:
    completed = sorted(path for path in BATCHES.glob("batch_00[3-9]/complete.json"))
    clusters: dict[tuple[str, str, str], dict] = {}
    first_batch_time = min((path.stat().st_mtime for path in completed), default=0)
    for path in completed:
        payload = read_json(path)
        for item in payload.get("companies") or []:
            if item.get("status") not in {"partial", "error", "blocked"}:
                continue
            host = (urlsplit(item.get("url") or "").hostname or "<invalid>").lower()
            reason = item.get("stop_reason") or "unknown"
            key = (host, item["status"], reason)
            row = clusters.setdefault(key, {"domain": host, "status": item["status"],
                                             "failure_shape": reason, "companies": 0,
                                             "examples": []})
            row["companies"] += 1
            if len(row["examples"]) < 3:
                row["examples"].append(item.get("url") or "")

    detail_counts: dict[tuple[str, str], int] = {}
    replay = {"zhiye_detail": {"records": 0, "existing_html_artifacts": 0,
                                 "records_with_text": 0},
              "mokahr": {"records": 0, "existing_json_artifacts": 0,
                          "legacy_routes": 0}}
    if STORE.is_file():
        connection = sqlite3.connect(STORE)
        connection.row_factory = sqlite3.Row
        for record in connection.execute(
                "SELECT url,status,result_json,updated_at FROM tasks "
                "WHERE status IN ('partial','error','blocked')"):
            url = record["url"] or ""
            host = (urlsplit(url).hostname or "<invalid>").lower()
            try:
                result = json.loads(record["result_json"] or "{}")
            except json.JSONDecodeError:
                result = {}
            if host.endswith(".zhiye.com") and any(
                    marker in urlsplit(url).path.lower() for marker in ("/detail", "/jobdetails")):
                detail_counts[(host, record["status"])] = detail_counts.get(
                    (host, record["status"]), 0) + 1
                replay["zhiye_detail"]["records"] += 1
                html_path = result.get("html_path")
                replay["zhiye_detail"]["existing_html_artifacts"] += int(
                    isinstance(html_path, str) and Path(html_path).is_file())
                replay["zhiye_detail"]["records_with_text"] += int(bool(result.get("text")))
            if host == "app.mokahr.com":
                replay["mokahr"]["records"] += 1
                replay["mokahr"]["legacy_routes"] += int("/campus_apply/" in urlsplit(url).path)
                replay["mokahr"]["existing_json_artifacts"] += sum(
                    Path(item).is_file() for item in result.get("json_paths") or []
                    if isinstance(item, str))
        connection.close()

    rows = sorted(clusters.values(), key=lambda row: (-row["companies"], row["domain"],
                                                       row["status"], row["failure_shape"]))
    for (host, status), count in sorted(detail_counts.items(), key=lambda pair: -pair[1]):
        rows.append({"domain": host, "status": status,
                     "failure_shape": "zhiye_detail_misrouted_to_list_collector",
                     "companies": count, "examples": []})

    zhiye_yield = replay["zhiye_detail"]["records"]
    mokahr_yield = replay["mokahr"]["records"]
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": "Completed batch_003+ partial/error only; offline store/artifact inspection",
        "safety": {"network_requests": 0, "ok_urls_rerun": 0, "wechat_touched": False,
                   "ocr_used": False, "targets_modified": False, "runner_stopped": False},
        "completed_batches": [path.parent.name for path in completed],
        "first_batch_mtime": first_batch_time,
        "clusters": rows,
        "implemented": [
            {"adapter": "ZhiyeJobDetailPortal",
             "yield_basis": f"{zhiye_yield} incomplete detail tasks",
             "change": "Route /detail and /jobdetails to bounded single-job web extraction instead of zhiye_list",
             "production_registered": True},
            {"adapter": "MokahrPublicPortal",
             "yield_basis": f"{mokahr_yield} incomplete Mokahr tasks in store",
             "change": "Recognize legacy campus_apply routes; raise site pagination/json budgets to 30/200",
             "production_registered": True},
        ],
        "offline_artifact_replay": replay,
        "deferred": [
            {"domain": "wjx.cn/wjx.top/wenjuan.com", "reason": "form pages, low multi-company list yield"},
            {"domain": "bsurl.cn", "reason": "short-link detail redirects, only four tasks and no list-level gain"},
            {"domain": "hotjob.cn", "reason": "one closed portal returned explicit provider 500"},
        ],
    }
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["domain", "status", "failure_shape", "companies", "examples"]
    with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "examples": " | ".join(row["examples"])})
    print(json.dumps({"json": str(JSON_OUT), "csv": str(CSV_OUT),
                      "clusters": len(rows), "batches": report["completed_batches"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
