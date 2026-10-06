"""Download public image references from saved HTML once; never fetch articles."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from bs4 import BeautifulSoup
from PIL import Image
from jobprep.app.tools import empty_document, image_dimensions

SOURCE = "https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA"
INPUT = ROOT / "data" / "public_wechat_trial" / "article.html"
OUTPUT = INPUT.parent / "cdn_images"
MAX_IMAGES = 11
MAX_REQUESTS = 15
MAX_IMAGE_BYTES = 10 * 1024**2
MAX_TOTAL_BYTES = 30 * 1024**2
FORMATS = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}


def public_image_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "mmbiz.qpic.cn"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.fragment or not parsed.path.startswith(("/mmbiz_", "/sz_mmbiz_"))):
        raise ValueError("non_public_image_reference")
    if any(key != "wx_fmt" or val not in ("jpeg", "jpg", "png", "gif", "webp")
           for key, val in parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError("unapproved_image_query")
    return value


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(name: str, value: dict) -> None:
    target = OUTPUT / name
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def reserve_gate(connection: sqlite3.Connection) -> bool:
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("CREATE TABLE IF NOT EXISTS gate (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL, requests INTEGER NOT NULL, started TEXT NOT NULL)")
    connection.execute("BEGIN IMMEDIATE")
    existing = connection.execute("SELECT id FROM gate WHERE id=1").fetchone()
    if existing:
        connection.rollback()
        return False
    connection.execute("INSERT INTO gate VALUES (1, 'reserved', 0, ?)", (timestamp(),))
    connection.commit()
    return True


def reserve_request(connection: sqlite3.Connection) -> int:
    connection.execute("BEGIN IMMEDIATE")
    count = connection.execute("SELECT requests FROM gate WHERE id=1").fetchone()[0]
    if count >= MAX_REQUESTS:
        connection.rollback()
        raise ValueError("request_budget_exhausted")
    connection.execute("UPDATE gate SET requests=requests+1, state='running' WHERE id=1")
    connection.commit()
    return count + 1


def main() -> int:
    if sys.argv[1:]:
        raise SystemExit("Usage: download_saved_wechat_images.py")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(OUTPUT / "download_gate.sqlite3", timeout=5, isolation_level=None)
    if not reserve_gate(connection):
        connection.close()
        print("Persistent gate already consumed; zero requests made.")
        return 3

    report = {
        "source_url": SOURCE, "started_at": timestamp(), "status": "reserved",
        "requests_reserved": 0, "received_bytes": 0, "saved_bytes": 0,
        "downloaded_images": 0, "unique_images": 0, "duplicate_images": 0,
        "article_navigations": 0, "article_requests": 0,
        "limits": {"requests": MAX_REQUESTS, "images": MAX_IMAGES,
                   "image_bytes": MAX_IMAGE_BYTES, "total_bytes": MAX_TOTAL_BYTES},
        "policy": {"host": "mmbiz.qpic.cn", "method": "GET",
                   "redirects": False, "retries": 0, "cookies": False,
                   "wechat_session": False, "environment_proxy": False},
        "attempts": [],
    }
    document = empty_document(SOURCE, "saved-html-public-cdn-images", OUTPUT.resolve())
    document["source_url"] = SOURCE
    document["warnings"] = ["Images acquired without OCR; semantic and job-detail completeness remain unverified."]
    document["evidence"] = [{"kind": "saved_html", "path": str(INPUT), "source": SOURCE}]
    fatal = None
    try:
        if INPUT.stat().st_size > 10 * 1024**2:
            raise ValueError("saved_html_size_limit")
        soup = BeautifulSoup(INPUT.read_text(encoding="utf-8"), "html.parser")
        body = soup.select_one("#js_content")
        title = soup.select_one("#activity-name")
        if body is None or title is None or not title.get_text(strip=True):
            raise ValueError("saved_article_not_verified")
        document["title"] = title.get_text(" ", strip=True)
        document["text"] = body.get_text("\n", strip=True)
        nodes = body.select("img")
        if not 1 <= len(nodes) <= MAX_IMAGES:
            raise ValueError("image_count_limit")
        urls = [public_image_url(node.get("data-src", "")) for node in nodes]
        unique_urls = list(dict.fromkeys(urls))
        report["image_nodes"] = len(nodes)
        report["unique_image_urls"] = len(unique_urls)
        seen_hashes = {}
        entries_by_url = {}
        for order, url in enumerate(unique_urls):
            entry = {"url": SOURCE, "source": SOURCE, "path": "", "sha256": "",
                     "status": "pending_manual_image", "order": urls.index(url)}
            document["images"].append(entry)
            entries_by_url[url] = entry
            attempt = {"order": order, "host": "mmbiz.qpic.cn", "status": "reserved"}
            report["attempts"].append(attempt)
            report["requests_reserved"] = reserve_request(connection)
            write_json("download_status.json", report)
            # A separate client for each single request cannot replay response cookies.
            transport = httpx.HTTPTransport(retries=0, trust_env=False)
            with httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(20, connect=10)) as client:
                request = httpx.Request("GET", url, headers={
                    "Accept": "image/jpeg,image/png,image/gif,image/webp",
                    "Accept-Encoding": "identity", "Referer": SOURCE,
                    "User-Agent": "job-market-public-image-export/1.0",
                })
                response = client.send(request, stream=True, follow_redirects=False)
                try:
                    attempt["http_status"] = response.status_code
                    if response.status_code != 200:
                        raise ValueError("cdn_http_not_200_stop")
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ValueError("encoded_response_rejected")
                    media_type = response.headers.get("content-type", "").split(";")[0].lower()
                    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                        raise ValueError("non_image_response_stop")
                    length = response.headers.get("content-length", "")
                    if not length.isdigit() or int(length) <= 0:
                        raise ValueError("unbounded_response_length_stop")
                    capacity = min(MAX_IMAGE_BYTES, MAX_TOTAL_BYTES - report["received_bytes"])
                    if int(length) > capacity:
                        raise ValueError("download_byte_limit")
                    chunks = []
                    size = 0
                    for chunk in response.iter_raw(chunk_size=65536):
                        size += len(chunk)
                        report["received_bytes"] += len(chunk)
                        if size > int(length) or size > capacity:
                            raise ValueError("response_length_or_byte_limit")
                        chunks.append(chunk)
                    if size != int(length):
                        raise ValueError("truncated_image_response")
                    data = b"".join(chunks)
                finally:
                    response.close()
            width, height = image_dimensions(data)
            with Image.open(io.BytesIO(data)) as image:
                suffix = FORMATS.get(image.format)
                if not suffix:
                    raise ValueError("unsupported_image_format")
                image.load()
            digest = hashlib.sha256(data).hexdigest()
            entry.update(sha256=digest, width=width, height=height, bytes=len(data))
            report["downloaded_images"] += 1
            if digest in seen_hashes:
                entry.update(status="skipped_duplicate", duplicate_of=seen_hashes[digest]["order"])
                report["duplicate_images"] += 1
            else:
                target = OUTPUT / (digest + suffix)
                with target.open("xb") as handle:
                    handle.write(data)
                entry.update(path=str(target.resolve()), status="ok")
                seen_hashes[digest] = entry
                report["saved_bytes"] += len(data)
                report["unique_images"] += 1
            attempt.update(status=entry["status"], bytes=len(data), sha256=digest,
                           width=width, height=height)
            write_json("download_status.json", report)
        report["status"] = "success"
        report["reason"] = "all_saved_article_images_downloaded_and_validated"
        document["status"] = "partial"
        document["acquisition_status"] = "ok"
        document["coverage"].update(pages_seen=1, stop_reason="saved_html_images_acquired_ocr_unassessed")
        document["coverage"]["image_complete"] = True
        # Local, script-free HTML contains no original remote/session URLs.
        parts = ["<!doctype html><meta charset='utf-8'>", "<title>" + escape(document["title"]) + "</title>",
                 "<h1>" + escape(document["title"]) + "</h1><pre>" + escape(document["text"]) + "</pre>"]
        for url in urls:
            entry = entries_by_url[url]
            original = seen_hashes[entry["sha256"]]
            parts.append("<img src='" + escape(Path(original["path"]).name) + "' alt='article image'>")
        local_html = OUTPUT / "article_local.html"
        local_html.write_text("\n".join(parts), encoding="utf-8")
        document.update(html_path=str(local_html.resolve()), html_paths=[str(local_html.resolve())])
    except Exception as exc:
        fatal = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        report.update(status="stopped", reason=fatal)
        document.update(status="partial" if report["unique_images"] else "error", error_kind=fatal)
        document["coverage"].update(stop_reason=fatal, image_complete=False)
        document["warnings"].append("Download stopped without retry; remaining images were not requested.")
        if report["attempts"]:
            report["attempts"][-1].update(status="failed", error=fatal)
    finally:
        document["download_report_path"] = str((OUTPUT / "download_status.json").resolve())
        (OUTPUT / "article.txt").write_text(document["title"] + "\n\n" + document["text"], encoding="utf-8")
        report["finished_at"] = timestamp()
        write_json("document.json", document)
        write_json("download_status.json", report)
        connection.execute("UPDATE gate SET state=? WHERE id=1", (report["status"],))
        connection.close()
    print(json.dumps({"status": report["status"], "reason": report["reason"],
                      "requests": report["requests_reserved"], "downloaded_images": report["downloaded_images"],
                      "unique_images": report["unique_images"], "duplicate_images": report["duplicate_images"],
                      "received_bytes": report["received_bytes"], "document": str(OUTPUT / "document.json")}, indent=2))
    return 0 if report["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
