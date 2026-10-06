"""One reserved public article navigation in a fresh, filtered browser."""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobprep.html_extract import BLOCKED_RE, DELETED_RE
from playwright.sync_api import sync_playwright

TARGET = "https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA"
OUTPUT = ROOT / "data" / "public_wechat_trial"
STATUS = OUTPUT / "status.json"
CHALLENGE_PATH = re.compile(r"captcha|verify|challenge|wappoc", re.I)
EXTRA_BLOCKED = re.compile(
    r"\u9a8c\u8bc1\u7801|\u8bbf\u95ee\u5f02\u5e38|"
    r"\u8bbf\u95ee\u53d7\u9650|\u8bf7\u5728\u5fae\u4fe1|"
    r"\u7f51\u7edc\u5f02\u5e38|captcha|access denied", re.I
)
INSPECT = """() => {
  const body = document.querySelector('#js_content');
  const title = document.querySelector('#activity-name');
  const imgs = body ? [...body.querySelectorAll('img')] : [];
  return {
    visible_text: document.body?.innerText || '',
    document_title: document.title,
    article_title: title?.innerText.trim() || '',
    js_content_present: !!body,
    js_content_visible: !!body && !!body.getClientRects().length,
    text_length: body?.innerText.trim().length || 0,
    image_count: imgs.length,
    loaded_image_count: imgs.filter(i => i.complete && i.naturalWidth > 1 &&
      !(i.dataset.src && (i.currentSrc || i.src).startsWith('data:image/svg'))).length
  };
}"""


def save_status(report: dict) -> None:
    temporary = OUTPUT / "status.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    temporary.replace(STATUS)


def verify_local() -> int:
    """Correct placeholder-image evidence without opening any browser or URL."""
    from bs4 import BeautifulSoup

    report = json.loads(STATUS.read_text(encoding="utf-8"))
    soup = BeautifulSoup((OUTPUT / "article.html").read_text(encoding="utf-8"), "html.parser")
    body = soup.select_one("#js_content")
    images = body.select("img") if body else []
    placeholders = sum(
        bool(image.get("data-src")) and image.get("src", "").startswith("data:image/svg")
        for image in images
    )
    evidence = report.setdefault("evidence", {})
    evidence["image_count"] = len(images)
    evidence["placeholder_image_count"] = placeholders
    evidence["initial_decoded_image_count"] = evidence["loaded_image_count"]
    evidence["loaded_image_count"] = min(evidence["loaded_image_count"], len(images) - placeholders)
    report["local_verification"] = {
        "method": "saved_HTML_placeholder_detection_and_screenshot_inspection",
        "additional_navigation_calls": 0,
        "initial_success_classification_corrected": True,
        "initial_image_check_issue": "decoded_1px_SVG_placeholders_counted_as_loaded",
    }
    if placeholders:
        report["status"] = "partial"
        report["reason"] = "article_images_are_placeholders_non_article_hosts_blocked"
    save_status(report)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {
        "target": TARGET,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "reserved",
        "reason": "one_navigation_reserved_before_browser_launch",
        "navigation_calls": 0,
        "document_requests_allowed": 0,
        "document_requests_blocked": 0,
        "requests_allowed": 0,
        "requests_blocked": 0,
        "blocked_reasons": {},
        "http_status": None,
        "artifacts": [],
        "policy": {
            "fresh_context": True, "channel": "chrome", "headless": True,
            "allowed_host": "mp.weixin.qq.com", "max_document_requests": 1,
            "no_retry": True, "service_workers": "block",
            "system_proxy_modified": False, "browser_proxy": "disabled",
            "credentials_or_existing_profile_used": False,
            "media_blocked": True,
            "scope": "page HTTP requests; not a process-wide network firewall",
        },
    }
    try:
        with STATUS.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    except FileExistsError:
        print("Existing trial reservation found; refusing another navigation.")
        return 3

    fatal = None
    browser = None
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    channel="chrome", headless=True, timeout=20000,
                    args=["--no-proxy-server"],
                    ignore_default_args=["--disable-popup-blocking"],
                )
                context = browser.new_context(
                    accept_downloads=False, service_workers="block",
                )
                context.route_web_socket("**/*", lambda socket: socket.close())
                page = context.new_page()
                page.set_default_timeout(3000)
                session = context.new_cdp_session(page)

                def intercept(event):
                    nonlocal fatal
                    request = event["request"]
                    parsed = urlsplit(request["url"])
                    kind = event.get("resourceType", "")
                    reason = None
                    if kind == "Document":
                        if CHALLENGE_PATH.search(parsed.path):
                            fatal = "challenge_navigation_blocked"
                        if request["url"] != TARGET or report["document_requests_allowed"]:
                            reason = "additional_or_non_target_document"
                            fatal = fatal or "additional_navigation_blocked"
                        else:
                            report["document_requests_allowed"] += 1
                    if CHALLENGE_PATH.search(parsed.path):
                        fatal = fatal or "challenge_request_blocked"
                        reason = reason or "challenge_path"
                    if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com" or parsed.port not in (None, 443):
                        reason = reason or "non_article_host"
                    if request["method"] != "GET":
                        reason = reason or "non_get"
                    if kind == "Media" or re.search(r"\.(mp4|mp3|m3u8|m4a|aac|ogg|wav|webm)(?:$|\?)", request["url"], re.I):
                        reason = reason or "audio_video"
                    if fatal:
                        reason = reason or "trial_stopped"
                    if report["requests_allowed"] >= 40:
                        fatal = fatal or "page_request_limit"
                        reason = reason or "page_request_limit"
                    if reason:
                        report["requests_blocked"] += 1
                        if kind == "Document":
                            report["document_requests_blocked"] += 1
                        counts = report["blocked_reasons"]
                        counts[reason] = counts.get(reason, 0) + 1
                        session.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "BlockedByClient"})
                    else:
                        report["requests_allowed"] += 1
                        session.send("Fetch.continueRequest", {"requestId": event["requestId"]})

                # CDP request-stage interception covers each redirect hop too.
                session.on("Fetch.requestPaused", intercept)
                session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
                report["navigation_calls"] = 1
                report["status"] = "running"
                save_status(report)
                response = page.goto(TARGET, wait_until="domcontentloaded", timeout=20000)
                report["http_status"] = response.status if response else None
                if fatal:
                    raise RuntimeError(fatal)
                if response is None or response.status != 200:
                    raise RuntimeError("abnormal_http_response")
                deadline = time.monotonic() + 8
                evidence = None
                while time.monotonic() < deadline:
                    if fatal:
                        raise RuntimeError(fatal)
                    evidence = page.evaluate(INSPECT)
                    visible = evidence.pop("visible_text")
                    report["evidence"] = evidence
                    if BLOCKED_RE.search(visible) or EXTRA_BLOCKED.search(visible):
                        fatal = "challenge_or_access_restriction"
                        raise RuntimeError(fatal)
                    if DELETED_RE.search(visible):
                        fatal = "deleted_or_unavailable_article"
                        raise RuntimeError(fatal)
                    if evidence["js_content_visible"] and evidence["article_title"] and (
                        evidence["text_length"] > 0 or evidence["image_count"] > 0
                    ):
                        break
                    page.wait_for_timeout(200)
                else:
                    raise RuntimeError("normal_article_not_verified")

                height = page.evaluate("document.documentElement.scrollHeight")
                if height > 80000:
                    raise RuntimeError("page_height_limit")
                for y in range(0, height, 1000):
                    if fatal:
                        raise RuntimeError(fatal)
                    page.evaluate("y => window.scrollTo(0, y)", y)
                    page.wait_for_timeout(100)
                evidence = page.evaluate(INSPECT)
                visible = evidence.pop("visible_text")
                if fatal or BLOCKED_RE.search(visible) or EXTRA_BLOCKED.search(visible):
                    raise RuntimeError(fatal or "challenge_or_access_restriction")
                report["evidence"] = evidence
                html = page.content()
                if len(html.encode("utf-8")) > 10 * 1024 * 1024:
                    raise RuntimeError("html_size_limit")
                screenshot = page.screenshot(full_page=True, timeout=10000)
                if fatal:
                    raise RuntimeError(fatal)
                (OUTPUT / "article.html").write_text(html, encoding="utf-8")
                (OUTPUT / "article.png").write_bytes(screenshot)
                report["artifacts"] = ["article.html", "article.png"]
                missing = evidence["image_count"] - evidence["loaded_image_count"]
                report["status"] = "partial" if missing else "success"
                report["reason"] = "article_images_missing" if missing else "normal_article_verified"
            finally:
                if browser:
                    browser.close()
    except Exception as exc:
        report["status"] = "blocked" if fatal else "failed"
        report["reason"] = fatal or (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        report["error_type"] = type(exc).__name__
        # Do not serialize arbitrary exception strings or authenticated URLs.
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save_status(report)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["status"] == "success" else 2


if __name__ == "__main__":
    if sys.argv[1:] == ["--verify-local"]:
        raise SystemExit(verify_local())
    if sys.argv[1:]:
        raise SystemExit("Usage: save_public_wechat.py [--verify-local]")
    raise SystemExit(main())
