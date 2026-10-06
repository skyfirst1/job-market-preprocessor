"""Bounded adapter for static campus.51job.com campaign sites.

These sites commonly keep their job records in a same-origin JavaScript asset
and point applications at a separate ATS.  This adapter only discovers and
normalizes that static data; Zhiye acquisition remains owned by AppTools.
"""

from __future__ import annotations

from copy import deepcopy
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


_TITLE_KEYS = ("attr2", "title", "jobName", "positionName", "name")


def is_static_topic_url(url):
    """Recognize campaign entry pages, not every campus.51job.com resource."""
    try:
        parts = urlsplit(url)
    except (TypeError, ValueError):
        return False
    if (parts.scheme.lower() != "https" or (parts.hostname or "").lower() != "campus.51job.com"
            or parts.username is not None or parts.password is not None):
        return False
    path = parts.path or "/"
    if path == "/":
        return False
    leaf = posixpath.basename(path.rstrip("/")).lower()
    if path.endswith("/"):
        return True
    return leaf.endswith((".html", ".htm"))
_URL_KEYS = ("attr5", "url", "href", "link", "applyUrl", "detailUrl")
_TRACKING_KEYS = {"from", "source", "src", "track", "tracking", "spm"}
_STATIC_SCRIPT_HINTS = ("data", "job", "position", "recruit", "main")


def canonical_url(value: str, base_url: str) -> str | None:
    """Resolve a public link and remove fragments/tracking-only noise."""
    if not isinstance(value, str):
        return None
    value = unescape(value.strip().strip('"\''))
    if not value or value in {"#", "即将上线"}:
        return None
    try:
        parts = urlsplit(urljoin(base_url, value))
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        host = parts.hostname.lower()
        port = parts.port
        netloc = host if port is None or (parts.scheme == "http" and port == 80) or (
            parts.scheme == "https" and port == 443
        ) else f"{host}:{port}"
        path = re.sub(r"/{2,}", "/", parts.path or "/")
        path = posixpath.normpath(path)
        if parts.path.endswith("/") and not path.endswith("/"):
            path += "/"
        query = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            lowered = key.lower()
            if lowered.startswith("utm_") or lowered in _TRACKING_KEYS:
                continue
            query.append((key, item))
        return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(sorted(query)), ""))
    except (TypeError, ValueError):
        return None


class _MarkupLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.scripts: list[str] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag.lower() == "a" and values.get("href"):
            self._href = values["href"]
            self._text = []
        elif tag.lower() == "script" and values.get("src"):
            self.scripts.append(values["src"])

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


def _json_objects(source: str):
    """Yield strict JSON objects embedded in otherwise ordinary JS/HTML."""
    decoder = json.JSONDecoder()
    cursor = 0
    while True:
        start = source.find("{", cursor)
        if start < 0:
            return
        try:
            value, length = decoder.raw_decode(source[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        cursor = start + length
        if isinstance(value, dict):
            yield value


def _first_text(record: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def parse_static_source(source: str, base_url: str) -> dict:
    """Extract job records plus bounded-crawl candidates from HTML or JS."""
    parser = _MarkupLinks()
    parser.feed(source)
    jobs = []
    for record in _json_objects(source):
        title = _first_text(record, _TITLE_KEYS)
        if not title:
            continue
        raw_url = _first_text(record, _URL_KEYS)
        target = canonical_url(raw_url, base_url) if raw_url else None
        job = {
            "id": target or f"static:{title}:{_first_text(record, ('attr4', 'location', 'city'))}",
            "title": title,
            "url": target,
            "needs_details": bool(target),
            "source": "51job_static_record",
        }
        mapping = (("category", ("value", "category", "department")),
                   ("education", ("attr3", "education")),
                   ("location", ("attr4", "location", "city")))
        for output, keys in mapping:
            value = _first_text(record, keys)
            if value:
                job[output] = value
        jobs.append(job)

    anchor_jobs = []
    for href, title in parser.links:
        target = canonical_url(href, base_url)
        if not target or not title:
            continue
        parts = urlsplit(target)
        if parts.hostname and (parts.hostname.endswith(".zhiye.com") or
                               "jobadid=" in parts.query.lower() or
                               "/detail" in parts.path.lower()):
            anchor_jobs.append({"id": target, "title": title, "url": target,
                                "needs_details": True, "source": "51job_static_anchor"})

    scripts = []
    for value in parser.scripts:
        target = canonical_url(value, base_url)
        if not target or urlsplit(target).hostname != urlsplit(base_url).hostname:
            continue
        name = posixpath.basename(urlsplit(target).path).lower()
        if name.endswith(".js") and any(hint in name for hint in _STATIC_SCRIPT_HINTS):
            scripts.append(target)

    pages = []
    for href, _ in parser.links:
        target = canonical_url(href, base_url)
        if not target:
            continue
        parts = urlsplit(target)
        if (parts.hostname == urlsplit(base_url).hostname and
                re.search(r"(?:^|/)(?:index|job|position)[^/]*\.html?$", parts.path, re.I)):
            pages.append(target)
    return {"jobs": jobs + anchor_jobs, "scripts": list(dict.fromkeys(scripts)),
            "pages": list(dict.fromkeys(pages))}


def _read_source(result: dict) -> str:
    for key in ("raw_html", "html", "text"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            if key != "text" or not result.get("html_path"):
                return value
    path = result.get("html_path")
    if isinstance(path, str):
        try:
            raw = Path(path).read_bytes()
        except OSError:
            return result.get("text", "")
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    return result.get("text", "") if isinstance(result.get("text"), str) else ""


def _dedupe_jobs(jobs: list[dict], limit: int) -> tuple[list[dict], bool]:
    output = []
    seen = set()
    truncated = False
    for job in jobs:
        key = job.get("url") or (job.get("title"), job.get("location"), job.get("category"))
        if key in seen:
            continue
        seen.add(key)
        if len(output) >= limit:
            truncated = True
            continue
        output.append(job)
    return output, truncated


class Job51StaticPortal:
    name = "Job51StaticPortal"
    priority = 95
    scope = "Static campus.51job.com campaign pages with same-origin data assets"
    parameters = {
        "max_pages": {"type": "integer", "default": 10, "range": "1..50"},
        "max_jobs": {"type": "integer", "default": 1000, "range": "1..5000"},
        "max_details": {"type": "integer", "default": 1000, "range": "1..5000"},
        "max_zhiye_handoffs": {"type": "integer", "default": 10, "range": "0..50"},
        "timeout": {"type": "number", "default": 25, "range": "1..120 seconds"},
    }

    def matches(self, url, options):
        return is_static_topic_url(url)

    @staticmethod
    def _bound(options, name, default, upper, *, minimum=1):
        value = options.get(name, default)
        if type(value) is not int or not minimum <= value <= upper:
            raise ValueError(f"51job {name} must be {minimum}..{upper}")
        return value

    async def acquire(self, context):
        options = context.options
        max_pages = self._bound(options, "max_pages", 10, 50)
        max_jobs = self._bound(options, "max_jobs", 1000, 5000)
        max_details = self._bound(options, "max_details", 1000, 5000)
        max_handoffs = self._bound(options, "max_zhiye_handoffs", 10, 50, minimum=0)
        timeout = options.get("timeout", 25)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
            raise ValueError("51job timeout must be 1..120")

        queue = [canonical_url(context.url, context.url)]
        seen_pages: list[str] = []
        page_results = []
        discovered_jobs: list[dict] = []
        hit_page_limit = False
        while queue:
            target = queue.pop(0)
            if not target or target in seen_pages:
                continue
            if len(seen_pages) >= max_pages:
                hit_page_limit = True
                break
            result = await context.call("web", target, {
                "browser": False, "max_pages": 1, "max_scrolls": 0,
                "max_images": 0, "timeout": timeout,
            })
            seen_pages.append(target)
            page_results.append(result)
            if result["status"] in {"error", "blocked", "deleted"}:
                continue
            parsed = parse_static_source(_read_source(result), result.get("final_url", target))
            discovered_jobs.extend(parsed["jobs"])
            for candidate in parsed["scripts"] + parsed["pages"]:
                if candidate not in seen_pages and candidate not in queue:
                    queue.append(candidate)

        static_jobs, hit_job_limit = _dedupe_jobs(discovered_jobs, max_jobs)
        all_details = list(dict.fromkeys(job["url"] for job in static_jobs if job.get("url")))
        hit_detail_limit = len(all_details) > max_details
        detail_urls = all_details[:max_details]

        zhiye_representatives = []
        seen_hosts = set()
        for target in detail_urls:
            host = (urlsplit(target).hostname or "").lower()
            if not host.endswith(".zhiye.com") or host in seen_hosts:
                continue
            seen_hosts.add(host)
            zhiye_representatives.append(target)
        hit_handoff_limit = len(zhiye_representatives) > max_handoffs
        zhiye_results = []
        for target in zhiye_representatives[:max_handoffs]:
            result = await context.call("zhiye_list", target, {
                "max_pages": max_pages, "max_json_responses": max(40, max_pages * 4),
                "timeout": timeout,
            })
            zhiye_results.append(result)
            context.provenance.append({"rule": "51job_zhiye_handoff", "url": target,
                                       "host": urlsplit(target).hostname})

        merged_jobs, merged_truncated = _dedupe_jobs(
            static_jobs + [deepcopy(job) for result in zhiye_results for job in result.get("jobs", [])],
            max_jobs,
        )
        terminal_failures = [result for result in page_results if result["status"] in {"error", "blocked", "deleted"}]
        limited = hit_page_limit or hit_job_limit or hit_detail_limit or hit_handoff_limit or merged_truncated
        list_complete = bool(static_jobs) and not limited and not terminal_failures and not queue
        if hit_page_limit:
            reason = "max_pages"
        elif hit_job_limit or merged_truncated:
            reason = "max_jobs"
        elif hit_detail_limit:
            reason = "max_details"
        elif hit_handoff_limit:
            reason = "max_zhiye_handoffs"
        elif terminal_failures:
            reason = "static_asset_error"
        elif static_jobs:
            reason = "static_topic_complete"
        else:
            reason = "job_records_not_found"

        landing = deepcopy(page_results[0]) if page_results else {
            "url": context.url, "final_url": context.url, "title": "", "text": "",
            "images": [], "links": [], "warnings": [], "coverage": {},
        }
        landing.update(url=context.url, jobs=merged_jobs)
        landing.setdefault("images", [])
        landing.setdefault("links", [])
        landing.setdefault("warnings", [])
        landing["status"] = "ok" if list_complete and not detail_urls else "partial"
        landing.pop("error", None)
        landing["coverage"] = {
            **landing.get("coverage", {}),
            "pages_seen": len(seen_pages),
            "page_urls": seen_pages,
            "total_reported": len(static_jobs),
            "detail_urls": detail_urls,
            "details_discovered": len(all_details),
            "zhiye_handoffs": len(zhiye_results),
            "list_complete": list_complete,
            "complete": list_complete and not detail_urls,
            "jd_complete": False if detail_urls else list_complete,
            "stop_reason": reason,
        }
        if limited:
            landing["warnings"].append("51job static acquisition stopped at a configured bound")
        if terminal_failures:
            landing["warnings"].append("One or more 51job static pages/assets could not be read")
        context.provenance.append({"rule": "51job_static_campaign", "url": context.url,
                                   "pages_seen": len(seen_pages),
                                   "static_jobs": len(static_jobs),
                                   "detail_urls": len(detail_urls)})
        return landing


__all__ = ["Job51StaticPortal", "canonical_url", "is_static_topic_url", "parse_static_source"]
