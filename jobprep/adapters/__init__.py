"""Async acquisition routing; tools own transport, extraction and artifacts."""

from copy import deepcopy
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
import math
import re

from .validation import configured_list, http_url
from .job51_static import Job51StaticPortal
from .job51_xyz import Job51XYZPublicPortal
from .pool import accept_complete_list, accept_stable_mokahr_snapshots


STATUSES = {"ok", "partial", "error", "blocked", "deleted"}
FEISHU_HOST = "jobs.feishu.cn"


def _public_portal(url):
    host = http_url(url).hostname.lower()
    return host == FEISHU_HOST or host.endswith("." + FEISHU_HOST)


def _list_url(url):
    parts = http_url(url)
    route = unquote(parts.path).rstrip("/")
    fragment = unquote(parts.fragment).split("?", 1)[0].rstrip("/")
    return route.endswith("/position/list") or fragment.endswith("/position/list")


def _landing(url):
    parts = http_url(url)
    return parts.path.rstrip("/").lower() in {"", "/campus"} and parts.fragment in {"", "/"}


def _single_job_identity(url):
    """Return a stable job identifier only for an explicit detail URL."""
    parts = http_url(url)
    query = parse_qs(parts.query)
    for key in ("jobAdId", "jobId"):
        values = query.get(key)
        if values and values[0].strip():
            return values[0].strip()
    match = re.search(r"(?:^|/)job/([^/?#]+)", unquote(parts.fragment), re.I)
    return match.group(1) if match else None


def _single_job_has_identity(job, identity):
    if not isinstance(job, dict) or not identity:
        return False
    candidates = [job.get("id")]
    raw = job.get("raw")
    if isinstance(raw, dict):
        candidates.extend(raw.get(key) for key in ("Id", "id", "JobAdId", "jobAdId", "jobId"))
    job_url = job.get("url")
    if isinstance(job_url, str):
        try:
            parts = http_url(job_url)
            query = parse_qs(parts.query)
            candidates.extend(value for key in ("jobAdId", "jobId")
                              for value in query.get(key, []))
            match = re.search(r"(?:^|/)job/([^/?#]+)", unquote(parts.fragment), re.I)
            if match:
                candidates.append(match.group(1))
        except ValueError:
            pass
    return identity in {str(value).strip() for value in candidates if value is not None}


def _finish_single_job(result, source_url):
    """Promote a detail fetch only when its one-job boundary is auditable."""
    coverage = result["coverage"]
    coverage.update(scope="single_job", pagination_applied=False)
    identity = _single_job_identity(source_url)
    jobs = result.get("jobs") or []
    job = jobs[0] if len(jobs) == 1 else None
    text = job.get("text") if isinstance(job, dict) else None
    verified = (result["status"] not in {"error", "blocked", "deleted"}
                and isinstance(job, dict)
                and isinstance(job.get("title"), str) and bool(job["title"].strip())
                and isinstance(text, str) and len(text.strip()) >= 40
                and _single_job_has_identity(job, identity))
    if verified:
        job["needs_details"] = False
        result["status"] = "ok"
        coverage.update(complete=True, list_complete=True, jd_complete=True,
                        stop_reason="single_job_complete",
                        completion_basis="stable_job_id_and_single_structured_job",
                        detail_fetched=True, needs_details_count=0, detail_urls=[])
    elif result["status"] not in {"error", "blocked", "deleted"}:
        result["status"] = "partial"
        coverage.update(complete=False, list_complete=False, jd_complete=False,
                        stop_reason="single_job_evidence_incomplete")
        warning = "Single-job URL did not yield one identity-matched structured job with substantive text"
        if warning not in result["warnings"]:
            result["warnings"].append(warning)
    return result


def _failure(url, reason, message):
    return {"url": url, "final_url": url, "status": "error", "title": "", "text": "",
            "jobs": [], "images": [], "links": [], "warnings": [message],
            "coverage": {"complete": False, "list_complete": False, "stop_reason": reason},
            "error": message}


def _classification(result):
    """Use explicit status evidence; never infer a transport success from jobs."""
    status = result["status"]
    http_status = result.get("http_status")
    if type(http_status) is not int:
        http_status = None
    result["http_status"] = http_status
    if status in {"blocked", "deleted"}:
        result.update(retryable=False, error_kind=status)
    elif status == "error":
        permanent = {"invalid_schema", "invalid_configuration", "unsupported_adapter"}
        reason = result["coverage"].get("stop_reason")
        kind = result.get("error_kind") or reason or "tool_error"
        if reason in permanent or kind in permanent:
            result.update(retryable=False, error_kind=kind)
        elif kind in {"blocked", "deleted"}:
            result.update(retryable=False, error_kind=kind)
        elif http_status in {401, 403, 429, 404, 410}:
            kind = "deleted" if http_status in {404, 410} else "blocked"
            result.update(status=kind, retryable=False, error_kind=kind)
        elif http_status is not None:
            result.setdefault("retryable", http_status in {408, 425, 500, 502, 503, 504})
            result["error_kind"] = result.get("error_kind") or "http_error"
        else:
            result.setdefault("retryable", False)
            result["error_kind"] = kind
    else:
        result.setdefault("retryable", False)
        result.setdefault("error_kind", None)
    return result


def _canonical(value):
    if not isinstance(value, dict):
        raise ValueError("tool result must be a canonical object")
    http_url(value.get("url"))
    if not isinstance(value.get("status"), str) or value["status"] not in STATUSES:
        raise ValueError("tool result has an invalid canonical status")
    for key in ("jobs", "images"):
        if not isinstance(value.get(key), list) or any(not isinstance(x, dict) for x in value[key]):
            raise ValueError(f"tool result {key} must be a list of objects")
    coverage = value.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("tool result coverage must be an object")
    for key in ("complete", "list_complete"):
        if key in coverage and type(coverage[key]) is not bool:
            raise ValueError(f"coverage.{key} must be boolean")
    if "retryable" in value and type(value["retryable"]) is not bool:
        raise ValueError("tool result retryable must be boolean")
    if value.get("error_kind") is not None and not isinstance(value["error_kind"], str):
        raise ValueError("tool result error_kind must be a string or null")
    if value.get("http_status") is not None and (type(value["http_status"]) is not int
                                                or not 100 <= value["http_status"] <= 599):
        raise ValueError("tool result http_status must be an HTTP status integer or null")
    if "links" in value and (not isinstance(value["links"], list)
                             or any(not isinstance(x, (dict, str)) for x in value["links"])):
        raise ValueError("tool result links must be a list of strings or objects")
    if "warnings" in value and not isinstance(value["warnings"], list):
        raise ValueError("tool result warnings must be a list")
    result = deepcopy(value)
    result.setdefault("links", [])
    result.setdefault("warnings", [])
    result["coverage"].setdefault("complete", False)
    result["coverage"].setdefault("list_complete", False)
    if result["status"] in {"error", "blocked", "deleted"}:
        result["coverage"].update(complete=False, list_complete=False)
    return result


class ToolSchemaError(ValueError):
    pass


@dataclass
class AcquisitionContext:
    url: str
    artifact_dir: object
    options: dict
    tools: object
    list_config: dict | None = None
    attempts: list = field(default_factory=list)
    provenance: list = field(default_factory=list)

    async def call(self, method, target, options=None):
        attempt = {"tool": method, "url": target if isinstance(target, str) else target["url"],
                   "ordinal": len(self.attempts) + 1}
        self.attempts.append(attempt)
        try:
            function = getattr(self.tools, method)
            if method == "public_list":
                raw = await function(deepcopy(target), self.artifact_dir)
            else:
                raw = await function(target, self.artifact_dir, options=deepcopy(options or {}))
            try:
                result = _canonical(raw)
            except (ValueError, TypeError) as exc:
                raise ToolSchemaError(str(exc)) from exc
            attempt["status"] = result["status"]
            self.provenance.append({"tool": method, "url": result["url"],
                                    "final_url": result.get("final_url", result["url"]),
                                    "method": result.get("method"),
                                    "artifact_refs": {key: deepcopy(result[key]) for key in
                                                      ("html_path", "html_paths", "json_paths", "evidence")
                                                      if key in result},
                                    "coverage": deepcopy(result["coverage"]),
                                    "attempts": deepcopy(result.get("attempts", [])),
                                    "provenance": deepcopy(result.get("provenance", [])),
                                    "acquisition": deepcopy(result.get("acquisition", {}))})
            return result
        except Exception as exc:
            attempt.update(status="error", error_type=type(exc).__name__)
            raise


class BaseAdapter:
    """Extension protocol: name, priority, matches(url, options), acquire(context)."""

    name = "base"
    priority = 0
    scope = "Custom acquisition rule"
    parameters = {}

    def matches(self, url, options):
        return False

    async def acquire(self, context):
        raise NotImplementedError


class GenericWeb(BaseAdapter):
    name = "genericweb"
    priority = -100
    scope = "Any HTTP(S) URL; delegates transport and coverage to AppTools.web"
    parameters = {"browser": {"default": "auto", "values": [True, False, "auto"]},
                  "max_pages": {"type": "integer"}, "max_images": {"type": "integer"}}

    def matches(self, url, options):
        return True

    async def acquire(self, context):
        return await context.call("web", context.url, context.options)


class WjxPublicForm(BaseAdapter):
    name = "WjxPublicForm"
    priority = 95
    scope = "Public *.wjx.cn and *.wjx.top application forms; static full-form extraction"
    parameters = {
        "timeout": {"type": "number", "default": 20, "range": "1..120 seconds"},
        "max_html_bytes": {"type": "integer", "default": 5 * 1024**2,
                           "range": "1024..10485760"},
    }

    def matches(self, url, options):
        host = http_url(url).hostname.lower().rstrip(".")
        return any(host == suffix or host.endswith("." + suffix)
                   for suffix in ("wjx.cn", "wjx.top"))

    async def acquire(self, context):
        supported = {key: deepcopy(value) for key, value in context.options.items()
                     if key in self.parameters}
        result = await context.call("wjx_form", context.url, supported)
        context.provenance.append({"rule": "wjx_static_full_form",
                                   "browser_used": False, "ocr_used": False,
                                   "network_requests": 1})
        return result


class WechatImage(BaseAdapter):
    name = "WechatImage"
    priority = 100
    scope = "Exact mp.weixin.qq.com host; challenges are terminal, no bypass"
    parameters = {"images_only": {"type": "boolean", "default": False},
                  "max_images": {"type": "integer", "default": 30, "range": "0..100"},
                  "timeout": {"type": "number", "default": 20, "range": "1..120 seconds"},
                  "interval": {"type": "number", "default": 15, "range": "0..300 seconds"},
                  "max_requests": {"type": "integer", "default": 80, "range": "1..200"},
                  "browser_channel": {"type": "string|null", "default": "chrome", "values": [None, "chrome", "msedge"]},
                  "max_html_bytes": {"type": "integer", "default": 5 * 1024**2, "range": "1..10485760"},
                  "max_image_bytes": {"type": "integer", "default": 10 * 1024**2, "range": "1..10485760"},
                  "max_total_image_bytes": {"type": "integer", "default": 30 * 1024**2, "range": "1..31457280"}}

    def matches(self, url, options):
        return http_url(url).hostname.lower() == "mp.weixin.qq.com"

    async def acquire(self, context):
        supplied = context.options
        supported = set(self.parameters)
        options = {key: deepcopy(value) for key, value in supplied.items() if key in supported}
        options.setdefault("images_only", False)
        if "browser_channel" not in options and "channel" in supplied:
            options["browser_channel"] = supplied["channel"]
        if "interval" not in options and ("domain_delay" in supplied or "delay" in supplied):
            options["interval"] = supplied.get("domain_delay", supplied.get("delay"))
        context.provenance.append({"rule": "wechat_options_translation",
                                   "ignored_option_names": sorted(set(supplied) - supported -
                                                                  {"channel", "domain_delay", "delay"})})
        if type(options["images_only"]) is not bool:
            raise ValueError("images_only must be boolean")
        try:
            result = await context.call("wechat", context.url, options)
        except Exception as exc:
            reason = "invalid_schema" if isinstance(exc, ToolSchemaError) else (
                "invalid_configuration" if isinstance(exc, ValueError) else "tool_exception")
            result = _failure(context.url, reason, f"{type(exc).__name__}: WeChat acquisition failed")
            response = getattr(exc, "response", None)
            result.update(http_status=getattr(response, "status_code", None), error_kind=reason)
        reason = result["coverage"].get("stop_reason", "")
        result["retryable"] = False
        result["coverage"].update(list_complete=False, pagination_applied=False,
                                  scope="single_article")
        if "max_pages" in supplied:
            result["coverage"]["requested_max_pages"] = supplied["max_pages"]
        if supplied.get("search_terms"):
            result["coverage"].update(complete=False, search_applied=False,
                                      search_terms=deepcopy(supplied["search_terms"]))
            if result["status"] in {"ok", "partial"}:
                result["coverage"]["stop_reason"] = "search_not_applied"
            if result["status"] == "ok":
                result["status"] = "partial"
            result["warnings"].append("WeChat single-article acquisition does not apply search_terms")
        # Structured challenge evidence is terminal, even if a tool reports ok.
        if (result.get("challenge") is True or result.get("captcha_required") is True
                or reason in {"challenge", "captcha", "wechat_challenge", "authentication_required"}):
            result["status"] = "blocked"
            result["coverage"].update(complete=False, list_complete=False,
                                      stop_reason="wechat_challenge")
        return result


class ZhiyePublicPortal(BaseAdapter):
    name = "ZhiyePublicPortal"
    priority = 80
    scope = "Public *.zhiye.com portals; expands observed public job-list API"
    parameters = {"max_pages": {"type": "integer", "default": 10},
                  "max_json_responses": {"type": "integer", "default": 80},
                  "timeout": {"type": "number", "default": 20}}

    def matches(self, url, options):
        host = http_url(url).hostname.lower()
        path = http_url(url).path.lower()
        return host.endswith(".zhiye.com") and not re.search(
            r"/(?:detail|jobdetails)(?:/|$)", path
        )

    async def acquire(self, context):
        parts = http_url(context.url)
        path = parts.path.rstrip("/")
        target = context.url
        if path in {"", "/campus"}:
            suffix = "/campus/jobs" if path == "/campus" else "/campus/jobs"
            target = f"{parts.scheme}://{parts.netloc}{suffix}"
            context.provenance.append({"rule": "zhiye_campus_list_route", "url": target,
                                       "discovered_from": context.url})
        result = await context.call("zhiye_list", target, context.options)
        result["url"] = context.url
        if target != context.url:
            result["coverage"]["list_url"] = target
        return result


class ZhiyeJobDetailPortal(BaseAdapter):
    name = "ZhiyeJobDetailPortal"
    priority = 85
    scope = "Public *.zhiye.com single-job detail routes; bounded one-page extraction"
    parameters = {
        "timeout": {"type": "number", "default": 20, "range": "1..120 seconds"},
        "settle_ms": {"type": "integer", "default": 1500, "range": "100..10000"},
        "max_json_responses": {"type": "integer", "default": 40, "range": "1..200"},
    }

    def matches(self, url, options):
        parts = http_url(url)
        return parts.hostname.lower().endswith(".zhiye.com") and bool(
            re.search(r"/(?:detail|jobdetails)(?:/|$)", parts.path.lower())
        )

    async def acquire(self, context):
        supplied = context.options
        options = {key: deepcopy(value) for key, value in supplied.items()
                   if key in {"timeout", "settle_ms", "max_json_responses",
                              "browser_channel", "domain_delay", "delay"}}
        options.update(browser="auto", max_pages=1, max_scrolls=0, max_images=0)
        result = await context.call("web", context.url, options)
        result = _finish_single_job(result, context.url)
        context.provenance.append({"rule": "zhiye_single_job_route",
                                   "url": context.url, "max_pages": 1})
        return result


class MokahrJobDetailPortal(BaseAdapter):
    name = "MokahrJobDetailPortal"
    priority = 95
    scope = "Public Mokahr single-job fragment routes; bounded one-page extraction"
    parameters = {
        "timeout": {"type": "number", "default": 20, "range": "1..120 seconds"},
        "settle_ms": {"type": "integer", "default": 1500, "range": "100..10000"},
        "max_json_responses": {"type": "integer", "default": 40, "range": "1..200"},
    }

    def matches(self, url, options):
        parts = http_url(url)
        supported_path = re.search(
            r"/(?:campus-recruitment|social-recruitment|campus_apply)/[^/]+/\d+",
            parts.path,
        )
        return bool(supported_path and _single_job_identity(url))

    async def acquire(self, context):
        options = {key: deepcopy(value) for key, value in context.options.items()
                   if key in {"timeout", "settle_ms", "max_json_responses",
                              "browser_channel", "domain_delay", "delay"}}
        options.update(browser="auto", max_pages=1, max_scrolls=0, max_images=0)
        result = await context.call("web", context.url, options)
        result = _finish_single_job(result, context.url)
        context.provenance.append({"rule": "mokahr_single_job_route",
                                   "url": context.url, "max_pages": 1})
        return result


class MokahrPublicPortal(BaseAdapter):
    name = "MokahrPublicPortal"
    priority = 90
    scope = "Public Mokahr campus/social portals; drives the jobs route and bounded API-backed pagination"
    parameters = {
        "max_pages": {"type": "integer", "default": 30, "range": "1..100"},
        "max_jobs": {"type": "integer", "default": 3000, "range": "1..5000"},
        "timeout": {"type": "number", "default": 35, "range": "1..120 seconds"},
        "interval": {"type": "number", "default": 1.0, "range": "0..30 seconds"},
        "settle_ms": {"type": "integer", "default": 1800, "range": "100..10000"},
        "max_json_responses": {"type": "integer", "default": 200, "range": "1..500"},
    }

    def matches(self, url, options):
        parts = http_url(url)
        return bool(re.search(
            r"/(?:campus-recruitment|social-recruitment|campus_apply)/[^/]+/\d+",
            parts.path,
        ))

    async def acquire(self, context):
        supplied = context.options
        max_pages = supplied.get("max_pages", 30)
        max_jobs = supplied.get("max_jobs", 3000)
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("Mokahr max_pages must be 1..100")
        if type(max_jobs) is not int or not 1 <= max_jobs <= 5000:
            raise ValueError("Mokahr max_jobs must be 1..5000")
        base = context.url.split("#", 1)[0]
        target = base + "#/jobs"
        options = {key: deepcopy(value) for key, value in supplied.items()
                   if key in {"timeout", "interval", "settle_ms", "max_json_responses",
                              "browser_channel", "domain_delay", "delay"}}
        options.update(browser=True, max_pages=max_pages, max_scrolls=0, max_images=0,
                       next_selector='[class*="Pagination-forward"]')
        result = await context.call("web", target, options)
        if len(result.get("jobs") or []) > max_jobs:
            result["jobs"] = result["jobs"][:max_jobs]
            result["status"] = "partial"
            result["coverage"].update(complete=False, list_complete=False,
                                      stop_reason="max_jobs")
            result["warnings"].append("Mokahr max_jobs limit reached")
        result["url"] = context.url
        result["coverage"]["list_url"] = target
        context.provenance.append({"rule": "mokahr_jobs_route", "url": target,
                                   "pagination": "public jobs/v2 API via UI next control"})
        result = accept_complete_list(result, allowed={"terminal_pagination"})
        if result.get("status") != "ok":
            result = accept_stable_mokahr_snapshots(result, context.url)
        return result


class HotjobPublicPortal(BaseAdapter):
    name = "HotjobPublicPortal"
    priority = 90
    scope = "Public *.hotjob.cn portals; enumerates the public positionInfo API"
    parameters = {
        "max_pages": {"type": "integer", "default": 50, "range": "1..100"},
        "max_jobs": {"type": "integer", "default": 1500, "range": "1..5000"},
        "timeout": {"type": "number", "default": 25, "range": "1..120 seconds"},
        "interval": {"type": "number", "default": 0.5, "range": "0.1..30 seconds"},
        "retries": {"type": "integer", "default": 2, "range": "0..5"},
        "backoff": {"type": "number", "default": 1.0, "range": "0.1..30 seconds"},
    }

    def matches(self, url, options):
        host = http_url(url).hostname.lower()
        return host == "hotjob.cn" or host.endswith(".hotjob.cn")

    @staticmethod
    def _suite(url):
        match = re.search(r"/(SU[0-9a-fA-F]+)/", url)
        return match.group(1) if match else None

    async def acquire(self, context):
        supplied = context.options
        max_pages = supplied.get("max_pages", 50)
        max_jobs = supplied.get("max_jobs", 1500)
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("Hotjob max_pages must be 1..100")
        if type(max_jobs) is not int or not 1 <= max_jobs <= 5000:
            raise ValueError("Hotjob max_jobs must be 1..5000")
        suite = self._suite(context.url)
        landing = None
        if suite is None:
            landing = await context.call("web", context.url,
                                         {"browser": True, "max_pages": 1, "max_scrolls": 0,
                                          "max_images": 0, "max_json_responses": 20,
                                          "timeout": supplied.get("timeout", 25)})
            candidates = []
            for link in landing.get("links") or []:
                href = link.get("url", link.get("href")) if isinstance(link, dict) else link
                if isinstance(href, str) and self._suite(href):
                    candidates.append(href)
            if candidates:
                suite = self._suite(candidates[0])
        if suite is None:
            return landing or _failure(context.url, "suite_not_found", "Hotjob suite key not found")
        parts = http_url(context.url)
        host_base = f"{parts.scheme}://{parts.netloc}"
        route = "social" if "/social.html" in parts.path else "school"
        recruit_type = 2 if route == "social" else 1
        page_url = f"{host_base}/{suite}/pb/{route}.html"
        endpoint = f"{host_base}/wecruit/positionInfo/listPosition/{suite}"
        config = {
            "company": "", "scope": f"Hotjob public {route} list",
            "url": endpoint, "method": "POST", "body_encoding": "form",
            "headers": {"Referer": page_url},
            "query": {"iSaJAx": "isAjax", "request_locale": "zh_CN"},
            "body": {"isFrompb": "true", "recruitType": recruit_type, "pageSize": 15},
            "pagination": {"location": "body", "path": "currentPage", "start": 1,
                           "max_pages": max_pages},
            "response": {"items_path": "data.pageForm.pageData",
                         "total_path": "data.pageForm.dataCount",
                         "page_path": "data.pageForm.currentPage",
                         "total_pages_path": "data.pageForm.totalPage", "id_path": "postId",
                         "success_path": "state", "success_value": "200"},
            "fields": {"title": "postName", "category": "postTypeName",
                       "location": "workPlaceStr", "dept": "department",
                       "project": "projectName", "post_code": "postCode"},
            "http": {"interval": supplied.get("interval", 0.5),
                     "timeout": supplied.get("timeout", 25),
                     "retries": supplied.get("retries", 2),
                     "backoff": supplied.get("backoff", 1.0)},
        }
        result = await context.call("public_list", config)
        result["url"] = context.url
        for job in result.get("jobs") or []:
            identifier = job.get("id")
            job["url"] = f"{page_url}#{identifier}" if identifier else page_url
            job["url_provenance"] = "adapter_public_list_anchor"
            job["needs_details"] = True
        if len(result.get("jobs") or []) > max_jobs:
            result["jobs"] = result["jobs"][:max_jobs]
            result["coverage"].update(list_complete=False, complete=False,
                                      stop_reason="max_jobs")
        detail_urls = [job["url"] for job in result.get("jobs") or [] if job.get("url")]
        result["coverage"].update(detail_urls=detail_urls, jd_complete=False,
                                  complete=False, list_url=page_url)
        context.provenance.append({"rule": "hotjob_public_position_api", "url": endpoint,
                                   "suite": suite, "recruit_type": recruit_type})
        return accept_complete_list(result, allowed={"last_page", "reported_last_page",
                                                     "reported_total_reached"})


class ConfiguredPublicList(BaseAdapter):
    name = "ConfiguredPublicList"
    priority = 200
    scope = "Explicit list_config object/config JSON path or exact URL in config.list_sources"
    parameters = {"list_config": {"type": "object|path", "required": ["url", "pagination.path", "response.items_path"],
                                  "path_scope": "workspace/config/*.json (including subdirectories)"}}

    def matches(self, url, options):
        return "list_config" in options

    async def acquire(self, context):
        if context.list_config is None:
            raise ValueError("ConfiguredPublicList requires list_config or an exact list_sources entry")
        result = await context.call("public_list", context.list_config)
        result["url"] = context.url
        result["coverage"]["list_url"] = context.list_config["url"]
        return result


class FeishuPublicPortal(BaseAdapter):
    name = "FeishuPublicPortal"
    priority = 50
    scope = "jobs.feishu.cn and subdomains: observed position/list route, root or /Campus landing discovery"
    parameters = {"max_pages": {"type": "integer", "default": 100},
                  "timeout": {"type": "number", "unit": "seconds", "default": 25},
                  "interval": {"type": "number", "unit": "seconds", "default": 1.2},
                  "company": {"type": "string"}, "next_selector": {"type": "string"},
                  "browser_channel": {"type": "string or null"},
                  "max_response_bytes": {"type": "integer"},
                  "discovery": {"max_calls": 1, "max_images": 0, "max_pages": 1, "max_scrolls": 0}}

    @staticmethod
    def _tool_options(context):
        # Shared crawler timeouts are seconds; the normal-UI Feishu tool uses ms.
        supplied = context.options
        timeout = supplied.get("timeout", 25)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0.1 <= timeout <= 120):
            raise ValueError("Feishu adapter timeout must be 0.1..120 seconds")
        if supplied.get("browser") is False:
            raise ValueError("Feishu public-list UI requires browser; browser=false is unsupported")
        supported = {"max_pages", "company", "next_selector", "browser_channel", "max_response_bytes", "interval"}
        options = {key: deepcopy(value) for key, value in supplied.items() if key in supported}
        options["timeout"] = round(timeout * 1000)
        if "interval" not in options and ("domain_delay" in supplied or "delay" in supplied):
            options["interval"] = supplied.get("domain_delay", supplied.get("delay"))
        if "browser_channel" not in options and "channel" in supplied:
            options["browser_channel"] = supplied["channel"]
        context.provenance.append({"rule": "feishu_options_translation", "timeout_unit": "milliseconds",
                                   "ignored_option_names": sorted(set(supplied) - supported -
                                                                  {"timeout", "domain_delay", "delay", "channel", "browser"})})
        return options

    @staticmethod
    async def _collect(context, url, options):
        result = await context.call("feishu_list", url, options)
        if context.options.get("search_terms"):
            result["coverage"].update(complete=False, list_complete=False, search_applied=False,
                                      search_terms=deepcopy(context.options["search_terms"]),
                                      stop_reason="search_not_applied")
            if result["status"] == "ok":
                result["status"] = "partial"
            result["warnings"].append("Feishu normal-UI list tool does not apply search_terms")
        return result

    def matches(self, url, options):
        return _public_portal(url) and (_list_url(url) or _landing(url))

    async def acquire(self, context):
        tool_options = self._tool_options(context)
        if _list_url(context.url):
            return await self._collect(context, context.url, tool_options)
        landing_options = {**context.options, "max_images": 0, "max_pages": 1,
                           "max_scrolls": 0, "search_terms": []}
        landing = await context.call("web", context.url, landing_options)
        if landing["status"] in {"blocked", "deleted", "error"}:
            return landing
        host = http_url(context.url).hostname.lower()
        base = landing.get("final_url", context.url)
        links = []
        for link in landing["links"]:
            href = link.get("url", link.get("href")) if isinstance(link, dict) else link
            if not isinstance(href, str) or not href:
                continue
            target = urljoin(base, href)
            try:
                parts = http_url(target)
                if parts.hostname.lower() == host and _list_url(target) and target not in links:
                    links.append(target)
            except ValueError:
                continue
        if len(links) != 1:
            landing["status"] = "partial"
            landing["coverage"].update(complete=False, list_complete=False,
                                      stop_reason="list_link_not_found" if not links else "ambiguous_list_links")
            landing["warnings"].append("Public landing did not identify one unambiguous same-host position/list link")
            return landing
        context.provenance.append({"rule": "observed_same_host_position_list", "url": links[0],
                                   "discovered_from": context.url})
        result = await self._collect(context, links[0], tool_options)
        result["url"] = context.url
        result["coverage"]["list_url"] = links[0]
        return result


class AdapterRegistry:
    def __init__(self, config=None):
        if config is not None and not isinstance(config, dict):
            raise ValueError("config must be an object")
        self.config = deepcopy(config or {})
        self._adapters = {}
        for adapter in (GenericWeb(), WechatImage(), WjxPublicForm(), Job51XYZPublicPortal(), Job51StaticPortal(), MokahrJobDetailPortal(), MokahrPublicPortal(), HotjobPublicPortal(),
                        ZhiyeJobDetailPortal(), ZhiyePublicPortal(), FeishuPublicPortal(), ConfiguredPublicList()):
            self.register(adapter)

    def register(self, adapter, *, priority=None, replace=False):
        if isinstance(adapter, type):
            adapter = adapter()
        name = getattr(adapter, "name", None)
        if not isinstance(name, str) or not name or not callable(getattr(adapter, "matches", None)) or not callable(getattr(adapter, "acquire", None)):
            raise ValueError("adapter must have name, matches and acquire")
        rank = getattr(adapter, "priority", 0) if priority is None else priority
        if type(rank) is not int:
            raise ValueError("adapter priority must be integer")
        if name in self._adapters and not replace:
            raise ValueError(f"adapter already registered: {name}")
        self._adapters[name] = (rank, adapter)
        return adapter

    def _select(self, url, options, list_config):
        explicit = options.get("adapter")
        if explicit is not None:
            if not isinstance(explicit, str) or explicit not in self._adapters:
                raise ValueError("unsupported adapter")
            adapter = self._adapters[explicit][1]
            if not adapter.matches(url, options) and not (explicit == "ConfiguredPublicList" and list_config is not None):
                raise ValueError("adapter does not support this URL/options")
            return adapter
        routed_options = {**options, **({"list_config": list_config} if list_config is not None else {})}
        for _, adapter in sorted(self._adapters.values(), key=lambda entry: entry[0], reverse=True):
            if adapter.matches(url, routed_options):
                return adapter
        raise ValueError("no adapter supports this URL")

    @staticmethod
    def _options(options):
        if options is not None and not isinstance(options, dict):
            raise ValueError("options must be an object")
        return deepcopy(options or {})

    def select(self, url, options=None):
        http_url(url)
        options = self._options(options)
        return self._select(url, options, configured_list(url, options, self.config))

    def describe(self):
        """JSON-safe discovery for API/catalog clients, without I/O or settings."""
        import json

        catalog = [{"name": adapter.name, "priority": rank,
                    "scope": getattr(adapter, "scope", "Custom acquisition rule"),
                    "parameters": deepcopy(getattr(adapter, "parameters", {}))}
                   for rank, adapter in sorted(self._adapters.values(), key=lambda entry: entry[0], reverse=True)]
        return json.loads(json.dumps(catalog, allow_nan=False))

    async def acquire(self, url, artifact_dir, options=None, tools=None):
        adapter, context = None, None
        try:
            http_url(url)
            options = self._options(options)
            list_config = configured_list(url, options, self.config)
            adapter = self._select(url, options, list_config)
            if tools is None:
                from jobprep.app import AppTools
                tools = AppTools()
            tool_options = {key: value for key, value in options.items() if key not in {"adapter", "list_config"}}
            context = AcquisitionContext(url, artifact_dir, tool_options, tools, list_config)
            raw = await adapter.acquire(context)
            try:
                result = _canonical(raw)
            except (ValueError, TypeError) as exc:
                raise ToolSchemaError(str(exc)) from exc
        except Exception as exc:
            reason = "invalid_schema" if isinstance(exc, ToolSchemaError) else (
                "invalid_configuration" if context is None or isinstance(exc, ValueError) else "tool_exception")
            if isinstance(options, dict) and options.get("adapter") and adapter is None:
                reason = "unsupported_adapter"
            # Exceptions are represented once; scheduling/retries belong to the runner.
            messages = {"invalid_schema": "Acquisition returned an invalid canonical document",
                        "invalid_configuration": "Invalid URL, configuration or adapter options",
                        "unsupported_adapter": "Explicit adapter is unknown or does not support this source",
                        "tool_exception": "Acquisition tool failed"}
            result = _failure(url, reason, f"{type(exc).__name__}: {messages[reason]}")
            response = getattr(exc, "response", None)
            result["http_status"] = getattr(response, "status_code", None)
            if reason == "tool_exception":
                import httpx

                if isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError)):
                    result.update(retryable=True, error_kind="transport_error")
                elif isinstance(exc, (NotImplementedError, AttributeError, ImportError)):
                    result.update(retryable=False, error_kind="unsupported_tool")
        previous = result.get("acquisition")
        result["acquisition"] = {"adapter": adapter.name if adapter else None,
                                 "selection": "explicit" if isinstance(options, dict) and options.get("adapter") else "rules",
                                 "attempts": context.attempts if context else [],
                                 "provenance": context.provenance if context else []}
        if previous is not None:
            result["acquisition"]["tool_metadata"] = previous
        return _classification(result)


__all__ = ["AdapterRegistry", "BaseAdapter", "AcquisitionContext", "GenericWeb", "WechatImage", "WjxPublicForm",
           "Job51XYZPublicPortal", "Job51StaticPortal", "MokahrPublicPortal", "HotjobPublicPortal",
           "ZhiyePublicPortal", "FeishuPublicPortal", "ConfiguredPublicList"]
