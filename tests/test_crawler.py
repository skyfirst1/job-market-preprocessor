import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from jobprep import crawler


REAL_CLIENT = httpx.AsyncClient


def test_json_jobs_reads_common_pagination_totals():
    jobs, total = crawler._json_jobs({
        "data": {"totalRows": 42, "records": [{"jobId": "a1", "jobName": "视觉算法工程师"}]}
    }, "https://campus.51job.com/jobs")
    assert total == 42
    assert len(jobs) == 1
    assert jobs[0]["title"] == "视觉算法工程师"


def test_json_jobs_reads_zhiye_fields_and_builds_detail_url():
    jobs, total = crawler._json_jobs({
        "Count": 42,
        "Data": [{"Id": "abc-guid", "JobAdId": 123, "JobAdName": "大模型开发工程师",
                  "Category": "校园招聘", "Duty": "开发 Agent", "Require": "硕士"}],
    }, "https://demo.zhiye.com/api/Jobad/GetJobAdPageList")
    assert total == 42
    assert jobs[0]["title"] == "大模型开发工程师"
    assert jobs[0]["url"] == "https://demo.zhiye.com/campus/jobdetails?jobId=123"
    assert jobs[0]["text"] == "开发 Agent\n硕士"


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    def unexpected(request):
        raise AssertionError(f"Unexpected live request: {request.url}")
    transport = httpx.MockTransport(unexpected)
    # Mock transports need neither TLS certificates nor system proxy discovery.
    monkeypatch.setattr(crawler.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=transport, verify=False, trust_env=False, **kwargs))


@pytest.fixture
def mock_http(monkeypatch):
    def install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(crawler.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=transport, verify=False, trust_env=False, **kwargs))
    return install


def png():
    stream = io.BytesIO()
    Image.new("RGB", (9, 12), "white").save(stream, format="PNG")
    return stream.getvalue()


def test_http_images_referer_hash_dedup_and_contract(tmp_path, mock_http):
    seen = []
    body = png()

    def handler(request):
        seen.append(request)
        if request.url.path == "/article":
            return httpx.Response(200, text='<h1 id="activity-name">Announcement</h1><div id="js_content"><section>Nested<img data-src="/a"><img src="/b"><img src="/a"></section></div>')
        return httpx.Response(200, content=body)
    mock_http(handler)
    result = run(crawler.collect("https://example.com/article", tmp_path, {"browser": False, "delay": 0}))
    assert result["status"] == "ok"
    assert result["coverage"]["complete"]
    assert len(seen) == 3
    assert all(request.headers["referer"] == "https://example.com/article" for request in seen[1:])
    assert len(result["images"]) == 3
    assert len({image["path"] for image in result["images"]}) == 1
    assert result["images"][0]["width"] == 9 and result["images"][0]["height"] == 12
    assert Path(result["html_path"]).exists()
    assert Path(result["images"][0]["path"]).read_bytes() == body
    json.dumps(result)


def test_image_limits_and_invalid_image_are_partial(tmp_path, mock_http):
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, text='<article>Recruitment<img src="/a"><img src="/b"></article>')
        return httpx.Response(200, content=b"not an image")
    mock_http(handler)
    result = run(crawler.collect("https://example.com/", tmp_path, {"browser": False, "delay": 0, "max_images": 1}))
    assert [item["status"] for item in result["images"]] == ["error", "skipped"]
    assert result["status"] == "partial" and not result["coverage"]["complete"]


def test_streamed_html_byte_limit(tmp_path, mock_http):
    mock_http(lambda request: httpx.Response(200, content=b"x" * 100))
    result = run(crawler.collect("https://example.com", tmp_path, {"browser": False, "delay": 0, "max_html_bytes": 20}))
    assert result["status"] == "error"
    assert "BodyLimitError" in result["error"]
    assert not result["coverage"]["complete"]


def test_transient_http_retry_preserves_fragment(tmp_path, mock_http, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, text='<article>Public announcement</article>')
    mock_http(handler)
    async def no_sleep(delay):
        pass
    monkeypatch.setattr(crawler.asyncio, "sleep", no_sleep)
    result = run(crawler.collect("https://example.com/#/job/17", tmp_path, {"browser": False, "delay": 0}))
    assert len(calls) == 2 and result["status"] == "ok"
    assert result["final_url"] == "https://example.com/#/job/17"


def test_challenge_does_not_trigger_browser(tmp_path, mock_http, monkeypatch):
    mock_http(lambda request: httpx.Response(200, text='<p>请完成安全验证</p>'))
    async def forbidden(*args):
        pytest.fail("challenge must not be bypassed")
    monkeypatch.setattr(crawler, "_browser", forbidden)
    result = run(crawler.collect("https://example.com", tmp_path, {"delay": 0}))
    assert result["status"] == "blocked"
    assert result["html_path"]


class FakeControl:
    def __init__(self, page, disabled=False):
        self.page, self.disabled = page, disabled

    async def is_disabled(self):
        return self.disabled

    async def get_attribute(self, name):
        return None

    async def click(self):
        self.page.index = min(self.page.index + 1, len(self.page.pages) - 1)
        self.page.clicks += 1


class FakePage:
    def __init__(self, pages, controls, scroll_pages=None, response=None):
        self.pages, self.controls = pages, controls
        self.scroll_pages = scroll_pages or []
        self.index = 0
        self.url = "https://jobs.example/#/list"
        self.clicks = 0
        self.scrolls = 0
        self.response = response
        self.handler = None

    def set_default_timeout(self, timeout):
        pass

    def on(self, event, handler):
        self.handler = handler

    async def goto(self, url, **kwargs):
        self.url = url
        self.index = 0
        if self.response:
            self.handler(self.response)
        return SimpleNamespace(status=200)

    async def content(self):
        return self.pages[self.index]

    async def wait_for_timeout(self, timeout):
        await asyncio.sleep(0)

    async def evaluate(self, script):
        if self.scrolls < len(self.scroll_pages):
            self.pages[self.index] = self.scroll_pages[self.scrolls]
        self.scrolls += 1


def fake_browser(monkeypatch, page, missing_chromium=False):
    import playwright.async_api
    launches = []

    class FakeBrowser:
        async def new_context(self, **kwargs):
            return self
        async def new_page(self):
            return page
        async def close(self):
            self.closed = True

    browser = FakeBrowser()
    async def launch(**kwargs):
        launches.append(kwargs)
        if missing_chromium and "channel" not in kwargs:
            raise RuntimeError("Executable doesn't exist")
        return browser

    class Manager:
        async def start(self):
            async def stop():
                pass
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=stop)
        async def __aexit__(self, *args):
            pass

    async def next_control(current, selector):
        value = current.controls[current.index]
        return None if value is None else FakeControl(current, value)

    monkeypatch.setattr(playwright.async_api, "async_playwright", Manager)
    monkeypatch.setattr(crawler, "_next_control", next_control)
    return launches, browser


def browser_collect(tmp_path, **options):
    return run(crawler.collect("https://jobs.example/#/list", tmp_path, {"browser": True, "delay": 0, "settle_ms": 0, **options}))


def test_browser_disabled_next_terminal_and_detail_urls(tmp_path, monkeypatch):
    page = FakePage(['<a href="#/job/detail/1">Agent job</a>', '<a href="#/job/detail/2">视觉岗位</a><button disabled>Next</button>'], [False, True])
    _, browser = fake_browser(monkeypatch, page)
    result = browser_collect(tmp_path)
    assert page.clicks == 1 and browser.closed
    assert result["coverage"]["pages_seen"] == 2
    assert result["coverage"]["stop_reason"] == "terminal_pagination"
    assert result["coverage"]["list_complete"]
    assert len(result["coverage"]["detail_urls"]) == 2
    assert result["status"] == "partial" and not result["coverage"]["complete"]


@pytest.mark.parametrize(("pages", "max_pages", "reason"), [
    (["<main>First page</main>", "<main>First page</main>"], 3, "repeated_content"),
    (["<main>First page</main>", "<main>Second page</main>"], 1, "max_pages"),
])
def test_browser_limits_and_repeated_content(tmp_path, monkeypatch, pages, max_pages, reason):
    page = FakePage(pages, [False] * len(pages))
    fake_browser(monkeypatch, page)
    result = browser_collect(tmp_path, max_pages=max_pages)
    assert result["coverage"]["stop_reason"] == reason
    assert not result["coverage"]["complete"] and result["status"] == "partial"
    assert not result["coverage"]["list_complete"]


def test_browser_infinite_scroll_limit(tmp_path, monkeypatch):
    page = FakePage(['<main>First</main>'], [None], scroll_pages=['<main>First Second</main>', '<main>First Second Third</main>'])
    fake_browser(monkeypatch, page)
    result = browser_collect(tmp_path, max_scrolls=2)
    assert "Third" in result["text"]
    assert page.scrolls == 2
    assert result["coverage"]["stop_reason"] == "max_scrolls"
    assert not result["coverage"]["complete"]


def test_browser_unknown_end_is_not_complete(tmp_path, monkeypatch):
    page = FakePage(['<main>Current records</main>'], [None])
    fake_browser(monkeypatch, page)
    result = browser_collect(tmp_path)
    assert result["coverage"]["stop_reason"] == "unknown_pagination"
    assert not result["coverage"]["complete"]


def test_browser_chrome_fallback_and_json_redaction(tmp_path, monkeypatch):
    class Response:
        headers = {"content-type": "application/json", "authorization": "must-not-save"}
        request = SimpleNamespace(resource_type="fetch")
        url = "https://jobs.example/observed?token=secret"
        status = 200
        async def body(self):
            return json.dumps({"access_token": "secret", "total": 1, "jobs": [{"jobId": 42, "jobName": "Agent", "url": "/#/job/42", "cookie": "secret"}]}).encode()
    page = FakePage(['<main>Public listing<button disabled>Next</button></main>'], [True], response=Response())
    launches, _ = fake_browser(monkeypatch, page, missing_chromium=True)
    result = browser_collect(tmp_path)
    assert launches[-1]["channel"] == "chrome"
    assert any("Chrome" in warning for warning in result["warnings"])
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["url"] == "https://jobs.example/#/job/42"
    record = Path(result["json_paths"][0]).read_text(encoding="utf-8")
    assert "secret" not in record and "must-not-save" not in record
    assert result["coverage"]["total_reported"] == 1
    assert not result["coverage"]["complete"]


def test_search_without_selector_is_explicitly_partial(tmp_path, monkeypatch):
    page = FakePage(['<main>No records<button disabled>Next</button></main>'], [True])
    fake_browser(monkeypatch, page)
    result = browser_collect(tmp_path, search_terms=["Agent", "视觉"])
    assert result["coverage"]["search_terms"] == ["Agent", "视觉"]
    assert result["coverage"]["stop_reason"] == "search_not_applied"
    assert not result["coverage"]["complete"]


def test_delay_alias_and_bad_url(tmp_path):
    assert crawler._options({"delay": 1.2})["domain_delay"] == 1.2
    result = run(crawler.collect("file:///tmp/page.html", tmp_path))
    assert result["status"] == "error"
    assert "Only http" in result["error"]


def test_configured_search_terms_share_page_budget(tmp_path, monkeypatch):
    page = FakePage(['<main>Choose a search</main>'], [True])
    filled = []

    class Field:
        @property
        def first(self):
            return self
        async def fill(self, term):
            filled.append(term)
        async def press(self, key):
            assert key == "Enter"
            page.pages[0] = '<main>' + filled[-1] + '<button disabled>Next</button></main>'

    field = Field()
    def locator(selector):
        assert selector == "#search"
        return field
    page.locator = locator
    fake_browser(monkeypatch, page)
    result = browser_collect(tmp_path, search_terms=["Agent", "视觉"], search_selector="#search", max_pages=1)
    assert filled == ["Agent"]
    assert result["coverage"]["stop_reason"] == "max_pages"
    assert not result["coverage"]["list_complete"]
    assert not result["coverage"]["complete"]


@pytest.mark.parametrize("changes", [True, False])
def test_search_application_requires_changed_evidence(tmp_path, monkeypatch, changes):
    original = '<main>Public records<button disabled>Next</button><p>Total 0 jobs</p></main>'
    page = FakePage([original], [True])
    terms = []

    class Search:
        @property
        def first(self):
            return self
        async def fill(self, term):
            terms.append(term)
        async def press(self, key):
            if changes:
                page.pages[0] = '<main>Results for ' + terms[-1] + '<button disabled>Next</button><p>Total 0 jobs</p></main>'

    page.locator = lambda selector: Search()
    fake_browser(monkeypatch, page)
    result = browser_collect(tmp_path, search_terms=["Agent", "视觉"], search_selector="#search")
    assert result["coverage"]["search_applied"] is changes
    assert len(result["coverage"]["searches"]) == 2
    assert all(record["applied"] is changes for record in result["coverage"]["searches"])
    assert all(record["total_reported"] == 0 for record in result["coverage"]["searches"])
    assert result["coverage"]["total_reported"] is None
    assert result["coverage"]["list_complete"] is changes
    assert result["coverage"]["complete"] is changes


def test_dom_json_duplicate_urls_cannot_satisfy_reported_total():
    result = crawler._base("https://jobs.example/", "browser", [])
    result["status"] = "ok"
    result["coverage"]["total_reported"] = 2
    result["jobs"] = [
        {"id": "dom-id", "url": "https://jobs.example/job/detail/42?b=2&a=1"},
        {"id": "json-id", "url": "https://jobs.example/job/detail/42?a=1&b=2"},
    ]
    crawler._finish(result, True, "terminal_pagination")
    assert not result["coverage"]["list_complete"]
    assert any("reported total" in warning for warning in result["warnings"])


def test_unrelated_telemetry_cannot_verify_search(tmp_path, monkeypatch):
    class Telemetry:
        headers = {"content-type": "application/json"}
        request = SimpleNamespace(resource_type="fetch")
        url = "https://jobs.example/telemetry"
        status = 200
        async def body(self):
            return b'{"counter":123,"total":999}'

    page = FakePage(['<main>Unchanged results<button disabled>Next</button></main>'], [True])
    class Field:
        @property
        def first(self):
            return self
        async def fill(self, term):
            pass
        async def press(self, key):
            page.handler(Telemetry())
    page.locator = lambda selector: Field()
    fake_browser(monkeypatch, page)
    result = browser_collect(tmp_path, search_terms=["Agent"], search_selector="#search")
    assert result["json_paths"]
    assert not result["coverage"]["search_applied"]
    assert result["coverage"]["total_reported"] is None
    assert not result["coverage"]["list_complete"]


def test_driver_startup_and_shutdown_are_bounded(tmp_path, monkeypatch):
    import playwright.async_api
    class Stalled:
        async def start(self):
            await asyncio.Event().wait()
        async def __aexit__(self, *args):
            await asyncio.Event().wait()
    monkeypatch.setattr(playwright.async_api, "async_playwright", Stalled)
    result = run(asyncio.wait_for(crawler.collect("https://jobs.example/", tmp_path,
        {"browser": True, "timeout": 0.1, "cleanup_timeout": 0.01, "delay": 0}), timeout=1))
    assert result["status"] == "error"
    assert result["coverage"]["stop_reason"] == "driver_startup_error"
    assert "startup" in result["error"]
    assert any("cleanup" in warning for warning in result["warnings"])


def test_browser_close_is_bounded(tmp_path, monkeypatch):
    page = FakePage(['<main>Empty results<button disabled>Next</button></main>'], [True])
    _, browser = fake_browser(monkeypatch, page)
    async def stalled_close():
        await asyncio.Event().wait()
    browser.close = stalled_close
    result = run(asyncio.wait_for(crawler.collect("https://jobs.example/", tmp_path,
        {"browser": True, "cleanup_timeout": 0.01, "delay": 0}), timeout=1))
    assert result["status"] == "partial"
    assert result["coverage"]["stop_reason"] == "browser_cleanup_error"
    assert not result["coverage"]["list_complete"]
