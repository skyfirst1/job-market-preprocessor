"""Bounded HTTP/Playwright acquisition with explicit provenance and coverage.

collect(url, artifact_dir, options=None) is the integration entry point. Options:
browser (True/False/"auto"), max_pages=3, max_scrolls=3, max_images=8,
timeout=20 (seconds), retries=2, delay=1.2 (alias domain_delay), search_terms=[],
search_selector/next_selector (site CSS selectors), settle_ms=500,
max_html_bytes=5MiB, max_image_bytes=10MiB, max_total_image_bytes=30MiB,
max_json_bytes=2MiB, max_json_responses=20, browser_channel (alias channel).
Browser sessions are fresh, headless contexts with no user profile or credentials.
Search requires a configured input; it is filled and submitted with Enter.
Next defaults to rel=next, then visible buttons/links named Next/下一页/下页/›/».
pages_seen counts pagination snapshots, not additional scroll snapshots. max_pages
is shared across search terms; max_scrolls applies per page. list_complete means
terminal discovery; complete also requires resolved detail/image evidence.
Search evidence is recorded in coverage.searches; search_applied requires a changed
DOM extraction or observed JSON after submission. Multi-term totals stay per term.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from PIL import Image

from .html_extract import absolute_url, extract_html, is_detail_url


class BodyLimitError(ValueError):
    pass


_DOMAIN_LAST: dict[str, float] = {}


class _Pacer:
    def __init__(self, delay: float):
        self.delay = delay

    async def wait(self, url: str) -> None:
        domain = urlsplit(url).netloc.lower()
        now = time.monotonic()
        scheduled = max(now, _DOMAIN_LAST.get(domain, 0) + self.delay)
        _DOMAIN_LAST[domain] = scheduled
        await asyncio.sleep(max(0, scheduled - now))


def _options(options: dict | None) -> dict:
    out = {"browser": "auto", "max_pages": 3, "max_scrolls": 3, "max_images": 8,
           "timeout": 20, "retries": 2, "domain_delay": 1.2, "settle_ms": 500,
           "max_html_bytes": 5 * 1024**2, "max_image_bytes": 10 * 1024**2,
           "max_total_image_bytes": 30 * 1024**2, "max_json_bytes": 2 * 1024**2,
           "max_json_responses": 20, "search_terms": []}
    out.update(options or {})
    if options and "delay" in options and "domain_delay" not in options:
        out["domain_delay"] = options["delay"]
    if options and "channel" in options and "browser_channel" not in options:
        out["browser_channel"] = options["channel"]
    if out["browser"] not in (True, False, "auto"):
        raise ValueError("browser must be true, false or 'auto'")
    for key in ("max_pages", "max_scrolls", "max_images", "retries", "settle_ms", "max_html_bytes", "max_image_bytes", "max_total_image_bytes", "max_json_bytes", "max_json_responses"):
        out[key] = max(0, int(out[key]))
    out["max_pages"] = max(1, out["max_pages"])
    out["timeout"] = max(0.1, float(out["timeout"]))
    out["cleanup_timeout"] = max(0.01, float(out.get("cleanup_timeout", min(3, out["timeout"]))))
    out["domain_delay"] = max(0, float(out["domain_delay"]))
    terms = out["search_terms"] or []
    out["search_terms"] = [str(term) for term in ([terms] if isinstance(terms, str) else terms)]
    return out


def _base(url: str, method: str, terms: list) -> dict:
    return {"url": url, "final_url": url, "title": "", "text": "", "status": "error",
            "method": method, "html_path": "", "images": [], "links": [], "jobs": [],
            "coverage": {"pages_seen": 0, "complete": False, "list_complete": False, "stop_reason": "error",
                         "search_terms": terms, "search_applied": None if not terms else False,
                         "search_scope": "terms" if terms else "source", "searches": [],
                         "total_reported": None, "detail_urls": []},
            "warnings": [], "html_paths": [], "json_paths": [], "evidence": []}


def _save(directory: Path, body: bytes, suffix: str) -> str:
    digest = hashlib.sha256(body).hexdigest()
    path = directory / (digest + suffix)
    if not path.exists():
        path.write_bytes(body)
    return str(path.resolve())


async def _fetch(client: httpx.AsyncClient, url: str, opts: dict, pacer: _Pacer,
                 limit: int, headers: dict | None = None) -> tuple[bytes, str, int, str]:
    for attempt in range(opts["retries"] + 1):
        await pacer.wait(url)
        try:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code in {408, 425, 429, 500, 502, 503, 504} and attempt < opts["retries"]:
                    retry_after = response.headers.get("retry-after", "")
                    pause = min(5, float(retry_after)) if retry_after.replace(".", "", 1).isdigit() else min(4, 0.5 * 2**attempt)
                    await asyncio.sleep(pause)
                    continue
                size = response.headers.get("content-length", "")
                if size.isdigit() and int(size) > limit:
                    raise BodyLimitError(f"Response exceeds {limit} byte limit")
                chunks, count = [], 0
                async for chunk in response.aiter_bytes():
                    count += len(chunk)
                    if count > limit:
                        raise BodyLimitError(f"Response exceeds {limit} byte limit")
                    chunks.append(chunk)
                final = str(response.url)
                if not urlsplit(final).fragment and urlsplit(url).fragment:
                    final += "#" + urlsplit(url).fragment
                return b"".join(chunks), final, response.status_code, response.encoding or "utf-8"
        except httpx.TransportError:
            if attempt == opts["retries"]:
                raise
            await asyncio.sleep(min(4, 0.5 * 2**attempt))
    raise RuntimeError("Retry loop exhausted")


def _merge(result: dict, parsed: dict, final_url: str, html_path: str) -> None:
    result["final_url"] = final_url
    result["title"] = result["title"] or parsed["title"]
    if parsed["text"] and parsed["text"] not in result["text"]:
        result["text"] += ("\n\n" if result["text"] else "") + parsed["text"]
    result["html_path"] = result["html_path"] or html_path
    if html_path not in result["html_paths"]:
        result["html_paths"].append(html_path)
    for name, key in (("links", "url"), ("jobs", "id")):
        existing = {item[key] for item in result[name]}
        for item in parsed[name]:
            if item[key] not in existing:
                result[name].append(item)
                existing.add(item[key])
    result["evidence"].append({"url": final_url, "html_path": html_path, "items": parsed["evidence"]})
    if parsed["total_reported"] is not None:
        result["coverage"]["total_reported"] = parsed["total_reported"]
    # Extraction reports pending images/details; acquisition resolves image gaps here.
    result["status"] = "ok" if parsed["status"] == "partial" and (parsed["text"] or parsed["image_urls"]) else parsed["status"]
    if parsed.get("error"):
        result["error"] = parsed["error"]


async def _images(result: dict, sources: list, client: httpx.AsyncClient, directory: Path,
                  opts: dict, pacer: _Pacer) -> bool:
    total, seen, gap = 0, {}, False
    for source in sources:
        url = source["url"]
        item = {"url": url, "path": "", "sha256": "", "width": None, "height": None,
                "status": "error", "error": None, "order": source["order"]}
        if url in seen:
            result["images"].append({**seen[url], "order": source["order"]})
            continue
        if len(seen) >= opts["max_images"] or total >= opts["max_total_image_bytes"]:
            gap = True
            item.update(status="skipped", error="Image count or total byte limit reached")
        else:
            try:
                limit = min(opts["max_image_bytes"], opts["max_total_image_bytes"] - total)
                body, _, code, _ = await _fetch(client, url, opts, pacer, limit, {"Referer": source.get("referer", result["final_url"])})
                total += len(body)
                if code >= 400:
                    raise ValueError(f"HTTP {code}")
                with Image.open(io.BytesIO(body)) as img:
                    item["width"], item["height"] = img.size
                    fmt = (img.format or "img").lower()
                    img.verify()
                item.update(path=_save(directory, body, "." + fmt), sha256=hashlib.sha256(body).hexdigest(), status="ok")
            except Exception as exc:
                gap = True
                item["error"] = f"{type(exc).__name__}: {exc}"
        seen[url] = item
        result["images"].append(item)
    if gap:
        result["warnings"].append("Some article images are missing or limited; OCR evidence is incomplete")
    return gap


def _job_key(job: dict) -> tuple[str, str]:
    if job["url"]:
        parts = urlsplit(job["url"])
        canonical = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                               urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True))), parts.fragment))
        return "url", canonical
    return "id", job["id"]


def _search_payload(payload) -> bool:
    if isinstance(payload, list):
        return any(_search_payload(item) for item in payload)
    if isinstance(payload, dict):
        if any(isinstance(payload.get(key), list) for key in ("jobs", "jobList", "positions", "positionList", "vacancies")):
            return True
        return any(_search_payload(item) for item in payload.values() if isinstance(item, (dict, list)))
    return False


def _finish(result: dict, terminal: bool, reason: str, image_gap: bool = False, list_gap: bool = False) -> dict:
    details = list(dict.fromkeys(item["url"] for item in result["links"] if is_detail_url(item["url"])))
    details.extend(item["url"] for item in result["jobs"] if item["url"] and item["url"] not in details)
    result["coverage"].update(detail_urls=details, stop_reason=reason)
    missing_urls = any(not item["url"] for item in result["jobs"])
    total = result["coverage"]["total_reported"]
    missing_records = total is not None and total > len({_job_key(job) for job in result["jobs"]})
    list_complete = terminal and not missing_records and not list_gap and result["status"] == "ok"
    result["coverage"]["list_complete"] = list_complete
    complete = list_complete and not details and not image_gap and not missing_urls
    result["coverage"]["complete"] = complete
    if details:
        result["warnings"].append("Discovered job detail URLs require separate pipeline collection")
    if missing_urls:
        result["warnings"].append("Observed jobs without explicit detail URLs require site adaptation")
    if missing_records:
        result["warnings"].append("Fewer jobs were extracted than the page's reported total")
    if result["status"] in {"ok", "partial"} and not complete:
        result["status"] = "partial"
    return result


async def _http(url: str, directory: Path, opts: dict, client: httpx.AsyncClient, pacer: _Pacer) -> tuple[dict, bool]:
    result = _base(url, "http", opts["search_terms"])
    body, final, code, encoding = await _fetch(client, url, opts, pacer, opts["max_html_bytes"])
    html_path = _save(directory, body, ".html")
    parsed = extract_html(body.decode(encoding, errors="replace"), final, code)
    result["coverage"]["pages_seen"] = 1
    _merge(result, parsed, final, html_path)
    sources = [{**source, "referer": final} for source in parsed["image_urls"]]
    gap = await _images(result, sources, client, directory, opts, pacer) if parsed["status"] not in {"blocked", "deleted", "error"} else False
    listing = bool(parsed["jobs"] or parsed["has_next"])
    needs_browser = parsed["requires_browser"] or listing or bool(opts["search_terms"])
    terminal = parsed["terminal"] or (not listing and not parsed["requires_browser"])
    reason = parsed["status"] if parsed["status"] in {"blocked", "deleted", "error"} else "terminal_pagination" if parsed["terminal"] else "static_document" if terminal else "browser_required"
    if opts["search_terms"]:
        terminal, reason = False, "search_requires_browser"
    return _finish(result, terminal, reason, gap), needs_browser


def _secret(key: str) -> bool:
    lowered = key.lower().replace("-", "").replace("_", "")
    return any(word in lowered for word in ("token", "password", "secret", "authorization", "cookie", "apikey", "credential")) or lowered in {"auth", "session", "sessionid"}


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode([(key, "[REDACTED]" if _secret(key) else value) for key, value in parse_qsl(parts.query, keep_blank_values=True)])
    # Hash routers may carry query credentials as well.
    fragment = parts.fragment
    if "?" in fragment:
        route, query_part = fragment.split("?", 1)
        fragment = route + "?" + urlencode([(key, "[REDACTED]" if _secret(key) else value) for key, value in parse_qsl(query_part, keep_blank_values=True)])
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, query, fragment))


def _sanitize(value):
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if _secret(str(key)) else _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return _safe_url(value)
    return value


def _json_jobs(payload, base: str) -> tuple[list, int | None]:
    jobs, total = [], None
    stack = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(reversed(value))
        elif isinstance(value, dict):
            for key in ("total", "Total", "totalCount", "total_count", "totalRows", "totalSize", "count", "Count"):
                if isinstance(value.get(key), int) and not isinstance(value[key], bool) and value[key] >= 0:
                    total = value[key] if total is None else max(total, value[key])
            title_key = next((key for key in ("jobTitle", "jobName", "positionName", "positionTitle", "JobAdName", "title", "name") if isinstance(value.get(key), str)), None)
            title = value[title_key] if title_key else None
            identifier = next((value[key] for key in ("jobId", "positionId", "JobAdId", "Id", "job_id", "id", "code") if isinstance(value.get(key), (str, int))), None)
            link = next((value[key] for key in ("jobUrl", "detailUrl", "url", "href") if isinstance(value.get(key), str)), "")
            if not link and title and identifier is not None and (urlsplit(base).hostname or "").lower().endswith("zhiye.com"):
                category = str(value.get("Category", ""))
                section = "campus" if "校园" in category else "intern" if "实习" in category else "social"
                link = f"/{section}/jobdetails?jobId={identifier}"
            job_fields = ("description", "jobDescription", "requirements", "Duty", "Require",
                          "responsibility", "claim")
            generic_name_job = title_key == "name" and any(value.get(key) for key in ("responsibility", "claim"))
            if title and (identifier is not None or link) and (generic_name_job or any(key in value for key in ("jobId", "positionId", "JobAdId", "JobAdName", "jobTitle", "jobName", "positionName", "positionTitle")) or (title_key != "name" and any(key in value for key in job_fields))):
                target = absolute_url(link, base) or ""
                stable = str(identifier) if identifier is not None else target
                jobs.append({"id": hashlib.sha256((urlsplit(base).netloc + ":" + stable).encode()).hexdigest()[:24], "title": title, "url": target,
                             "text": "\n".join(str(value.get(key, "")) for key in job_fields if value.get(key)), "raw": value})
            stack.extend(item for item in value.values() if isinstance(item, (dict, list)))
    return jobs, total


def reparse_saved_json(result: dict) -> dict:
    """Merge jobs from this result's existing JSON artifacts without network I/O."""
    merged = json.loads(json.dumps(result))
    merged["jobs"] = [item for item in merged.get("jobs", []) if not (
        isinstance(item.get("raw"), dict)
        and item["raw"].get("name") == item.get("title")
        and not any(item["raw"].get(key) for key in ("responsibility", "claim"))
        and not any(key in item["raw"] for key in ("JobAdName", "jobTitle", "jobName",
                                                    "positionName", "positionTitle"))
    )]
    existing = {item.get("id") for item in merged["jobs"]}
    total = (merged.get("coverage") or {}).get("total_reported")
    reparsed_paths = []
    for raw_path in dict.fromkeys(merged.get("json_paths") or []):
        path = Path(raw_path)
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        base = record.get("url", merged.get("final_url", merged.get("url", "")))
        jobs, reported = _json_jobs(record.get("body", record), base)
        for job in jobs:
            if job["id"] not in existing:
                merged.setdefault("jobs", []).append(job)
                existing.add(job["id"])
        if reported is not None:
            total = reported if total is None else max(total, reported)
        if jobs:
            reparsed_paths.append(str(path.resolve()))
    coverage = merged.setdefault("coverage", {})
    coverage["total_reported"] = total
    details = list(coverage.get("detail_urls") or [])
    details.extend(job["url"] for job in merged.get("jobs", []) if job.get("url"))
    coverage["detail_urls"] = list(dict.fromkeys(details))
    if reparsed_paths:
        merged["local_json_reparse"] = {"paths": reparsed_paths, "network_accessed": False}
    return merged


async def _next_control(page, selector: str | None):
    if selector:
        locator = page.locator(selector)
    else:
        locator = page.locator("a[rel='next'], button[rel='next']")
        if not await locator.count():
            locator = page.get_by_role("button", name=re.compile(r"^(?:next(?:\s+page)?|下一页|下页|›|»)$", re.I))
        if not await locator.count():
            locator = page.get_by_role("link", name=re.compile(r"^(?:next(?:\s+page)?|下一页|下页|›|»)$", re.I))
        if not await locator.count():
            # Some white-label ATS portals expose only numbered controls.
            # Explicit data-page attributes keep this fallback narrowly scoped.
            numbered = page.locator("button[data-page], a[data-page]")
            current = None
            candidates = []
            for index in range(await numbered.count()):
                item = numbered.nth(index)
                raw = await item.get_attribute("data-page")
                if not raw or not raw.isdigit() or not await item.is_visible():
                    continue
                number = int(raw)
                classes = (await item.get_attribute("class") or "").lower()
                if await item.get_attribute("aria-current") == "page" or any(
                        marker in classes for marker in ("is-active", "active", "current")):
                    current = number
                candidates.append((number, item))
            if current is not None:
                following = [entry for entry in candidates if entry[0] > current]
                if following:
                    return min(following, key=lambda entry: entry[0])[1]
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        if await candidate.is_visible():
            return candidate
    return None


async def _is_disabled(control) -> bool:
    classes = (await control.get_attribute("class") or "").lower().split()
    return await control.is_disabled() or await control.get_attribute("aria-disabled") == "true" or any("disabled" in token for token in classes)


async def _bounded(awaitable, seconds: float, phase: str):
    """Do not wait indefinitely for a driver's cancellation acknowledgement."""
    task = asyncio.ensure_future(awaitable)
    def consume(done):
        if not done.cancelled():
            done.exception()
    try:
        finished, _ = await asyncio.wait([task], timeout=seconds)
        if not finished:
            task.cancel()
            task.add_done_callback(consume)
            raise TimeoutError(f"{phase} exceeded {seconds:g}s")
        return task.result()
    except BaseException:
        if not task.done():
            task.cancel()
            task.add_done_callback(consume)
        raise


@asynccontextmanager
async def _playwright_session(factory, opts: dict, result: dict):
    manager = factory()
    playwright = None
    try:
        try:
            playwright = await _bounded(manager.start(), opts["timeout"], "Playwright driver startup")
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["status"] = "error"
            result["warnings"].append("Playwright driver startup failed or timed out; browser evidence is unverified")
        yield playwright
    finally:
        try:
            shutdown = playwright.stop() if playwright else manager.__aexit__(None, None, None)
            await _bounded(shutdown, opts["cleanup_timeout"], "Playwright driver shutdown")
        except Exception as exc:
            result["warnings"].append(f"Playwright cleanup failed: {type(exc).__name__}: {exc}")
            result["error"] = result.get("error") or f"{type(exc).__name__}: {exc}"
            result["status"] = "partial" if result["html_path"] else "error"


async def _browser(url: str, directory: Path, opts: dict, client: httpx.AsyncClient, pacer: _Pacer) -> dict:
    from playwright.async_api import async_playwright

    result = _base(url, "browser", opts["search_terms"])
    sources, signatures, pending, observations = [], set(), set(), []
    terminal, reason = False, "unknown_pagination"
    accepted = 0
    json_gap = False

    async def observe(response):
        nonlocal json_gap
        try:
            size = response.headers.get("content-length", "")
            if size.isdigit() and int(size) > opts["max_json_bytes"]:
                json_gap = True
                return
            body = await response.body()
            if len(body) > opts["max_json_bytes"]:
                json_gap = True
                return
            payload = _sanitize(json.loads(body))
            record = {"url": _safe_url(response.url), "status": response.status, "body": payload}
            result["json_paths"].append(_save(directory, json.dumps(record, ensure_ascii=False).encode(), ".json"))
            jobs, total = _json_jobs(payload, response.url)
            relevant = bool(jobs) or _search_payload(payload) or bool(opts.get("search_response_pattern") and re.search(opts["search_response_pattern"], response.url))
            if relevant:
                observations.append({"signature": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), "total_reported": total,
                                     "job_keys": [_job_key(job) for job in jobs],
                                     "detail_urls": [job["url"] for job in jobs if job["url"]]})
            existing = {item["id"] for item in result["jobs"]}
            for job in jobs:
                if job["id"] not in existing:
                    result["jobs"].append(job)
                    existing.add(job["id"])
            if relevant and total is not None:
                result["coverage"]["total_reported"] = total
        except Exception as exc:
            json_gap = True
            result["warnings"].append(f"Observed JSON capture failed: {type(exc).__name__}: {exc}")

    def on_response(response):
        nonlocal accepted, json_gap
        if "json" not in response.headers.get("content-type", "").lower() or response.request.resource_type not in {"xhr", "fetch"}:
            return
        if accepted >= opts["max_json_responses"]:
            json_gap = True
            return
        accepted += 1
        task = asyncio.create_task(observe(response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def snapshot(page, code=200):
        body = (await page.content()).encode("utf-8")
        if len(body) > opts["max_html_bytes"]:
            raise BodyLimitError("Browser HTML byte limit reached")
        parsed = extract_html(body, page.url, code)
        path = _save(directory, body, ".html")
        _merge(result, parsed, page.url, path)
        sources.extend({**source, "referer": page.url} for source in parsed["image_urls"])
        signature = hashlib.sha256(json.dumps([parsed["text"], parsed["image_urls"], parsed["jobs"]], sort_keys=True).encode()).hexdigest()
        return parsed, signature

    async def drain_pending():
        nonlocal json_gap
        if pending:
            _, unfinished = await asyncio.wait(list(pending), timeout=opts["timeout"])
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
                json_gap = True

    async with _playwright_session(async_playwright, opts, result) as playwright:
        if playwright is None:
            return _finish(result, False, "driver_startup_error")
        browser = None
        try:
            launch = {"headless": True, "timeout": opts["timeout"] * 1000}
            if opts.get("browser_channel"):
                launch["channel"] = opts["browser_channel"]
            try:
                browser = await _bounded(playwright.chromium.launch(**launch), opts["timeout"], "Browser launch")
            except Exception as exc:
                if opts.get("browser_channel"):
                    raise
                result["warnings"].append(f"Bundled Chromium unavailable ({type(exc).__name__}); trying installed Chrome channel")
                browser = await _bounded(playwright.chromium.launch(**launch, channel="chrome"), opts["timeout"], "Chrome launch")
            context = await _bounded(browser.new_context(accept_downloads=False), opts["timeout"], "Browser context creation")
            page = await _bounded(context.new_page(), opts["timeout"], "Browser page creation")
            page.set_default_timeout(opts["timeout"] * 1000)
            page.on("response", on_response)
            terms = opts["search_terms"] or [None]
            if opts["search_terms"] and not opts.get("search_selector"):
                result["warnings"].append("search_terms provided without search_selector; search was not applied")
                terms = [None]
            all_terminal = True
            for term in terms:
                search_start = result["coverage"]["pages_seen"]
                search_applied = term is None
                observation_start = len(observations)
                search_details = []
                search_ids = set()
                search_total = None
                if result["coverage"]["pages_seen"] >= opts["max_pages"]:
                    all_terminal, reason = False, "max_pages"
                    break
                await pacer.wait(url)
                navigation = await page.goto(url, wait_until="domcontentloaded", timeout=opts["timeout"] * 1000)
                await page.wait_for_timeout(opts["settle_ms"])
                if term is not None:
                    await drain_pending()
                    initial = extract_html(await page.content(), page.url, navigation.status if navigation else 200)
                    if initial["status"] in {"blocked", "deleted", "error"}:
                        result["coverage"]["pages_seen"] += 1
                        await snapshot(page, navigation.status if navigation else 200)
                        all_terminal, reason = False, initial["status"]
                        break
                    baseline = (initial["text"], initial["jobs"])
                    observation_start = len(observations)
                    prior_json = {item["signature"] for item in observations}
                    field = page.locator(opts["search_selector"]).first
                    await field.fill(term)
                    await field.press("Enter")
                    await page.wait_for_timeout(opts["settle_ms"])
                    await drain_pending()
                    after_search = extract_html(await page.content(), page.url)
                    search_applied = (after_search["text"], after_search["jobs"]) != baseline or any(item["signature"] not in prior_json for item in observations[observation_start:])
                    if not search_applied:
                        result["warnings"].append(f"Search {term!r} did not produce changed content or observed JSON; application is unverified")
                signatures.clear()
                while True:
                    result["coverage"]["pages_seen"] += 1
                    parsed, signature = await snapshot(page, navigation.status if navigation else 200)
                    search_details.extend(job["url"] for job in parsed["jobs"] if job["url"])
                    search_ids.update(_job_key(job) for job in parsed["jobs"])
                    if parsed["total_reported"] is not None:
                        search_total = parsed["total_reported"]
                    if parsed["status"] in {"blocked", "deleted", "error"}:
                        all_terminal, reason = False, parsed["status"]
                        break
                    if signature in signatures:
                        all_terminal, reason = False, "repeated_content"
                        break
                    signatures.add(signature)
                    changed = False
                    control = await _next_control(page, opts.get("next_selector"))
                    if not control and not parsed["terminal"]:
                        for _ in range(opts["max_scrolls"]):
                            await pacer.wait(page.url)
                            await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                            await page.wait_for_timeout(opts["settle_ms"])
                            parsed, new_signature = await snapshot(page)
                            search_details.extend(job["url"] for job in parsed["jobs"] if job["url"])
                            search_ids.update(_job_key(job) for job in parsed["jobs"])
                            if parsed["total_reported"] is not None:
                                search_total = parsed["total_reported"]
                            if parsed["status"] in {"blocked", "deleted", "error"}:
                                break
                            if new_signature == signature:
                                break
                            changed = True
                            signature = new_signature
                            control = await _next_control(page, opts.get("next_selector"))
                            if control or parsed["terminal"]:
                                break
                    if parsed["status"] in {"blocked", "deleted", "error"}:
                        all_terminal, reason = False, parsed["status"]
                        break
                    if parsed["terminal"] or (control and await _is_disabled(control)):
                        reason = "terminal_pagination"
                        break
                    if control is None:
                        all_terminal, reason = False, "max_scrolls" if changed or opts["max_scrolls"] == 0 else "unknown_pagination"
                        break
                    if result["coverage"]["pages_seen"] >= opts["max_pages"]:
                        all_terminal, reason = False, "max_pages"
                        break
                    href = await control.get_attribute("href")
                    target = absolute_url(href or "", page.url)
                    if target and urlsplit(target).netloc != urlsplit(page.url).netloc:
                        all_terminal, reason = False, "unsafe_next_link"
                        break
                    await pacer.wait(page.url)
                    await control.click()
                    await page.wait_for_timeout(opts["settle_ms"])
                    navigation = None
                await drain_pending()
                for observation in observations[observation_start:]:
                    search_details.extend(observation["detail_urls"])
                    search_ids.update(observation["job_keys"])
                    if observation["total_reported"] is not None:
                        search_total = observation["total_reported"]
                if term is not None:
                    term_terminal = reason == "terminal_pagination" and search_applied and (search_total is None or search_total <= len(search_ids))
                    result["coverage"]["searches"].append({"term": term, "applied": search_applied,
                        "pages_seen": result["coverage"]["pages_seen"] - search_start,
                        "total_reported": search_total, "detail_urls": list(dict.fromkeys(search_details)),
                        "list_complete": term_terminal, "stop_reason": reason if search_applied else "search_unverified"})
                    if not search_applied:
                        all_terminal, reason = False, "search_unverified"
                    elif reason == "terminal_pagination" and not term_terminal:
                        all_terminal, reason = False, "reported_total_mismatch"
                if result["status"] in {"blocked", "deleted", "error"}:
                    break
            terminal = all_terminal and not (opts["search_terms"] and not opts.get("search_selector"))
            if opts["search_terms"] and not opts.get("search_selector"):
                reason = "search_not_applied"
            if opts["search_terms"]:
                searches = result["coverage"]["searches"]
                result["coverage"]["search_applied"] = len(searches) == len(opts["search_terms"]) and all(item["applied"] for item in searches)
                result["coverage"]["total_reported"] = searches[0]["total_reported"] if len(opts["search_terms"]) == 1 and searches else None
                if len(opts["search_terms"]) > 1:
                    result["warnings"].append("Reported totals are scoped per search term; no company-wide total is inferred")
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["status"] = "partial" if result["html_path"] else "error"
            reason = "browser_error"
            result["warnings"].append("Browser acquisition did not finish; install Playwright Chromium or provide an available browser_channel")
        finally:
            await drain_pending()
            if browser:
                try:
                    await _bounded(browser.close(), opts["cleanup_timeout"], "Browser close")
                except Exception as exc:
                    result["warnings"].append(f"Browser cleanup failed: {type(exc).__name__}: {exc}")
                    result["error"] = result.get("error") or f"{type(exc).__name__}: {exc}"
                    result["status"] = "partial" if result["html_path"] else "error"
                    terminal, reason = False, "browser_cleanup_error"
    if json_gap:
        result["warnings"].append("Observed JSON evidence was limited or failed")
    gap = await _images(result, sources, client, directory, opts, pacer)
    return _finish(result, terminal, reason, gap, json_gap)


async def collect(url: str, artifact_dir: Path, options: dict | None = None) -> dict:
    """Collect a single source; discovered job details are queued by the caller."""
    result = _base(url, "http", [])
    try:
        opts = _options(options)
        result["coverage"]["search_terms"] = opts["search_terms"]
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError("Only http and https source URLs are supported")
        directory = Path(artifact_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        pacer = _Pacer(opts["domain_delay"])
        async with httpx.AsyncClient(timeout=opts["timeout"], follow_redirects=True,
                                     headers={"User-Agent": "JobPrep/1.0 (public recruitment evidence collector)"}) as client:
            if opts["browser"] is True:
                result["method"] = "browser"
                return await _browser(url, directory, opts, client, pacer)
            http_error = None
            try:
                result, needs_browser = await _http(url, directory, opts, client, pacer)
            except Exception as exc:
                http_error = f"{type(exc).__name__}: {exc}"
                result["error"] = http_error
                needs_browser = not isinstance(exc, BodyLimitError)
            if opts["browser"] == "auto" and needs_browser and result["status"] not in {"blocked", "deleted"}:
                try:
                    browser_result = await _browser(url, directory, opts, client, pacer)
                except Exception as exc:
                    browser_result = _base(url, "browser", opts["search_terms"])
                    browser_result["error"] = f"{type(exc).__name__}: {exc}"
                if browser_result["html_path"]:
                    browser_result["warnings"].extend(result["warnings"])
                    if http_error:
                        browser_result["warnings"].append("HTTP acquisition failed: " + http_error)
                    return browser_result
                result["warnings"].extend(browser_result["warnings"])
                result["warnings"].append("Browser fallback unavailable: " + browser_result.get("error", "unknown error"))
            return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
