"""Recover batch_009 cached WeChat image gaps without revisiting articles."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jobprep.app.fileio import write_csv_atomic, write_json_atomic
from jobprep.app.tools import directory, save_bytes
from jobprep.app.wechat_public import FORMATS, download_image, public_image_url, validate_options
from jobprep.runner.workstation import Workstation
from jobprep.settings import load_settings
from jobprep.store import canonical_url


BATCH = "batch_009"
MAX_REQUESTS = 40
MAX_IMAGE_BYTES = 10 * 1024**2
MAX_TOTAL_BYTES = 120 * 1024**2
REPORT_JSON = ROOT / "data" / "audits" / "batch009_image_gap_recovery.json"
REPORT_CSV = ROOT / "data" / "audits" / "batch009_image_gap_recovery.csv"
LEDGER_PATH = ROOT / "data" / "audits" / "batch009_image_gap_recovery.sqlite3"
CSV_FIELDS = (
    "task_id", "company", "image_index", "manifest_url", "outcome", "reason",
    "request_made", "bytes", "width", "height", "sha256", "ocr_status", "ocr_cache_hit",
)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: object) -> None:
    write_json_atomic(path, value, ensure_ascii=True, trailing_newline=True)


class RequestLedger:
    """Persistent one-attempt-per-URL gate used by the existing image downloader."""

    def __init__(self, path: Path, max_requests: int = MAX_REQUESTS):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_requests = max_requests
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                url TEXT PRIMARY KEY, state TEXT NOT NULL, reserved_at TEXT NOT NULL,
                finished_at TEXT, detail_json TEXT NOT NULL DEFAULT '{}')""")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def reserve(self, url: str):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM attempts WHERE url=?", (url,)).fetchone()
            if existing:
                return None, dict(existing)
            count = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            if count >= self.max_requests:
                raise ValueError("batch009_image_request_budget_exhausted")
            db.execute("INSERT INTO attempts(url,state,reserved_at) VALUES(?,?,?)",
                       (url, "reserved", timestamp()))
        return RequestPermit(self, url), None

    def finish(self, url: str, state: str, detail: dict) -> None:
        with closing(self.connect()) as db, db:
            db.execute("UPDATE attempts SET state=?,finished_at=?,detail_json=? WHERE url=?",
                       (state, timestamp(), json.dumps(detail, ensure_ascii=True), url))

    def snapshot(self) -> list[dict]:
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY reserved_at,url")]


class RequestPermit:
    def __init__(self, ledger: RequestLedger, url: str):
        self.ledger = ledger
        self.url = url
        self.used = False

    def request(self) -> int:
        if self.used:
            raise ValueError("image_url_already_requested")
        with closing(self.ledger.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM attempts WHERE url=?", (self.url,)).fetchone()
            if not row or row[0] != "reserved":
                raise ValueError("image_request_reservation_invalid")
            db.execute("UPDATE attempts SET state='requested' WHERE url=?", (self.url,))
            count = db.execute("SELECT COUNT(*) FROM attempts WHERE state!='reserved'").fetchone()[0]
        self.used = True
        return count


def _queue_companies(root: Path) -> dict[str, str]:
    queue = root / "data" / "batches" / BATCH / "wechat_queue.jsonl"
    result = {}
    for line in queue.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        url = row.get("acquisition_url")
        if url:
            result[canonical_url(url)] = str(row.get("company") or "")
    return result


def discover_tasks(root: Path) -> list[dict]:
    companies = _queue_companies(root)
    db_path = root / "data" / "workstation.sqlite3"
    output = []
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute("SELECT id,url,status,result_json FROM tasks WHERE status='pending_ocr'"):
            if canonical_url(row["url"]) not in companies or not row["result_json"]:
                continue
            result = json.loads(row["result_json"])
            gaps = result.get("ocr_gaps") or []
            manifest = next((item.get("path") for item in result.get("evidence", [])
                             if item.get("kind") == "public_image_manifest"), None)
            if gaps and manifest:
                output.append({"id": row["id"], "url": row["url"],
                               "company": companies[canonical_url(row["url"])],
                               "result": result, "manifest": manifest})
    return sorted(output, key=lambda item: item["id"])


def _manifest_url(entry: dict) -> str:
    url = entry.get("public_image_url")
    if not url:
        raise ValueError("manifest_has_no_public_image_url")
    public_image_url(url)
    fmt = dict(parse_qsl(urlsplit(url).query)).get("wx_fmt", "").lower()
    if fmt not in {"jpeg", "jpg", "png", "gif", "webp"}:
        raise ValueError("manifest_format_not_raster_image")
    return url


def _merge_result(current: dict, recovered: dict[int, dict], unresolved: dict[int, str]) -> dict:
    images = current.setdefault("images", [])
    existing_ocr = list(current.get("ocr") or [])
    successful_indices = {item.get("image_index") for item in existing_ocr if item.get("status") == "ok"}
    evidence = current.setdefault("evidence", [])
    evidence_keys = {(item.get("kind"), item.get("sha256"), item.get("order")) for item in evidence}

    for index, item in sorted(recovered.items()):
        if index >= len(images):
            unresolved[index] = "image_index_out_of_range"
            continue
        image = images[index]
        if image.get("path") and image.get("status") in {"ok", "skipped_duplicate"}:
            continue
        image.update(item["image"])
        key = ("image", image.get("sha256"), index)
        if key not in evidence_keys:
            evidence.append({"kind": "image", "path": image["path"],
                             "sha256": image["sha256"], "order": index,
                             "recovery": "batch009_cached_manifest"})
            evidence_keys.add(key)
        recognized = item.get("ocr")
        if recognized and recognized.get("status") == "ok" and index not in successful_indices:
            existing_ocr.append(recognized)
            successful_indices.add(index)
        if index in successful_indices:
            unresolved.pop(index, None)
        elif recognized:
            unresolved[index] = recognized.get("status") or "ocr_error"

    current["ocr"] = sorted(existing_ocr, key=lambda item: (item.get("image_index", -1),
                                                             item.get("status") != "ok"))
    current["ocr_gaps"] = [{"image": index, "reason": reason}
                           for index, reason in sorted(unresolved.items())]
    coverage = current.setdefault("coverage", {})
    if current["ocr_gaps"]:
        current["status"] = "pending_ocr"
        coverage["complete"] = False
        coverage["ocr_gaps"] = current["ocr_gaps"]
    else:
        current["status"] = current.get("acquisition_status") or "partial"
        if current["status"] == "ok" and not coverage.get("jd_complete"):
            current["status"] = "partial"
        coverage.pop("ocr_gaps", None)
    coverage["image_complete"] = all(
        image.get("status") in {"ok", "skipped_duplicate", "decorative"} for image in images)
    current.setdefault("warnings", []).append(
        "Cached batch_009 public image references were recovered without reopening the WeChat article.")
    return current


def commit_task(root: Path, task_id: str, recovered: dict[int, dict], unresolved: dict[int, str]) -> dict:
    db_path = root / "data" / "workstation.sqlite3"
    with closing(sqlite3.connect(db_path, timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status,result_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row or not row["result_json"]:
            raise ValueError("task_result_missing_at_commit")
        current = json.loads(row["result_json"])
        current_success = {item.get("image_index") for item in current.get("ocr", [])
                           if item.get("status") == "ok"}
        for index in list(recovered):
            if index in current_success:
                recovered.pop(index)
                unresolved.pop(index, None)
        merged = _merge_result(current, recovered, unresolved)
        db.execute("""UPDATE tasks SET status=?,result_json=?,error=NULL,updated_at=?,
                      available_at=NULL,last_error_kind=NULL WHERE id=?""",
                   (merged["status"], json.dumps(merged, ensure_ascii=False), timestamp(), task_id))
    return merged


async def run(root: Path, ledger_path: Path = LEDGER_PATH) -> dict:
    root = root.resolve()
    tasks = discover_tasks(root)
    settings = load_settings(root)
    workstation = Workstation(settings)
    engine = workstation.ocr_engine()
    if engine.model != "general_basic":
        raise ValueError("batch009_recovery_requires_general_basic")
    ledger = RequestLedger(ledger_path)
    rows = []
    total_bytes = 0
    calls_before = engine.status()["calls_used"]

    for task in tasks:
        manifest_path = Path(task["manifest"]).resolve()
        capture_dir = manifest_path.parent
        if (not manifest_path.is_file() or not capture_dir.is_relative_to(root / "data" / "artifacts")
                or manifest_path.stat().st_size > 1024**2):
            raise ValueError("unsafe_public_image_manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        recovered: dict[int, dict] = {}
        unresolved = {int(item["image"]): str(item.get("reason") or "ocr_gap")
                      for item in task["result"].get("ocr_gaps", [])}
        for index in sorted(unresolved):
            row = {key: "" for key in CSV_FIELDS}
            row.update(task_id=task["id"], company=task["company"], image_index=index,
                       outcome="unresolved", request_made=0, bytes=0)
            try:
                if index >= len(manifest):
                    raise ValueError("manifest_index_missing")
                url = _manifest_url(manifest[index])
                row["manifest_url"] = url
                permit, previous = ledger.reserve(url)
                if permit is None:
                    raise ValueError("public_image_url_already_attempted_in_recovery")
                remaining = MAX_TOTAL_BYTES - total_bytes
                if remaining <= 0:
                    ledger.finish(url, "rejected", {"reason": "total_image_byte_limit"})
                    raise ValueError("total_image_byte_limit")
                try:
                    options = validate_options({"max_image_bytes": MAX_IMAGE_BYTES,
                                                "max_total_image_bytes": min(MAX_TOTAL_BYTES, 30 * 1024**2)})
                    body, suffix, width, height = await download_image(
                        url, task["url"], permit, options, remaining)
                    row["request_made"] = 1
                    total_bytes += len(body)
                    path, digest = save_bytes(directory(capture_dir), body, suffix)
                    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
                        raise ValueError("saved_artifact_hash_mismatch")
                    recognized = dict(await engine.recognize(Path(path)))
                    recognized.update(image_index=index, image_sha256=digest)
                    recovered[index] = {
                        "image": {"path": path, "sha256": digest, "status": "ok", "error": None,
                                  "width": width, "height": height,
                                  "public_image_url": url, "source_host": urlsplit(url).hostname,
                                  "order": index, "bytes": len(body)},
                        "ocr": recognized,
                    }
                    row.update(outcome="recovered" if recognized.get("status") == "ok" else "ocr_gap",
                               reason="" if recognized.get("status") == "ok" else recognized.get("status", "ocr_error"),
                               bytes=len(body), width=width, height=height, sha256=digest,
                               ocr_status=recognized.get("status", ""),
                               ocr_cache_hit=recognized.get("cache_hit", False))
                    ledger.finish(url, "success", {"path": path, "sha256": digest, "bytes": len(body),
                                                   "width": width, "height": height,
                                                   "ocr_status": recognized.get("status")})
                except Exception as exc:
                    if permit.used:
                        row["request_made"] = 1
                    reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    ledger.finish(url, "failed", {"reason": reason})
                    raise ValueError(reason) from None
            except ValueError as exc:
                row["reason"] = str(exc)
                unresolved[index] = str(exc)
            rows.append(row)
        commit_task(root, task["id"], recovered, unresolved)

    calls_after = engine.status()["calls_used"]
    report = {
        "batch": BATCH, "started_and_finished_at": timestamp(),
        "scope": {"tasks": len(tasks), "gaps": len(rows), "article_requests": 0,
                  "wechat_client_operations": 0},
        "limits": {"image_requests": MAX_REQUESTS, "max_image_bytes": MAX_IMAGE_BYTES,
                   "max_total_bytes": MAX_TOTAL_BYTES},
        "totals": {
            "requests": sum(int(row["request_made"]) for row in rows),
            "recovered": sum(row["outcome"] == "recovered" for row in rows),
            "ocr_gaps": sum(row["outcome"] != "recovered" for row in rows),
            "downloaded_bytes": total_bytes, "ocr_calls": calls_after - calls_before,
        },
        "ocr": {"provider": "baidu", "model": engine.model, "calls_before": calls_before,
                "calls_after": calls_after, "budget": engine.status()},
        "request_ledger": {"path": str(Path(ledger_path).resolve()),
                           "unique_urls": len(ledger.snapshot())},
        "rows": rows,
    }
    atomic_json(REPORT_JSON, report)
    write_csv_atomic(REPORT_CSV, rows, CSV_FIELDS)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    report = asyncio.run(run(args.root))
    print(json.dumps(report["totals"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
