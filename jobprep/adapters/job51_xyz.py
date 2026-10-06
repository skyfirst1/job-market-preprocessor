"""Bounded collector for public xyz.51job.com recruitment portals."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx


PUBLIC_HOST = "xyz.51job.com"
API_HOST = "xyzapij.51job.com"
API_BASE = f"https://{API_HOST}"
SETTING_PATH = "/talent-domain/consumer/noauth/apply/get_customer_setting"
LIST_PATH = "/position-domain/consumer/noauth/get_job_list"
DETAIL_PATH = "/position-domain/consumer/noauth/get_job_detail"
SIGN_KEY = "sfhVda5dsmZf"
CTMID_RE = re.compile(r"^[1-9][0-9]{5,11}$")
GUID_RE = re.compile(r"^[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}$", re.I)


class Job51XYZError(RuntimeError):
    """A reproducible public-interface or response-contract failure."""


def validate_source_url(url: str) -> tuple[str, str]:
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() != PUBLIC_HOST:
        raise ValueError("expected an HTTPS xyz.51job.com URL")
    if parts.path.rstrip("/") != "/consumer/pc/home/index":
        raise ValueError("expected /consumer/pc/home/index")
    values = parse_qs(parts.query, keep_blank_values=True).get("ctmid", [])
    if len(values) != 1 or not CTMID_RE.fullmatch(values[0]):
        raise ValueError("ctmid must be one 6..12 digit positive identifier")
    ctmid = values[0]
    canonical = urlunsplit(("https", PUBLIC_HOST, "/consumer/pc/home/index",
                            urlencode({"ctmid": ctmid}), ""))
    return canonical, ctmid


def _js_value(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return str(value).lower()
    return str(value).strip()


def signed_parameters(data: dict, *, timestamp: int | None = None) -> dict:
    """Reproduce the signing function published in the portal's JavaScript bundle."""
    payload = dict(data)
    payload["timestamp"] = int(time.time()) if timestamp is None else timestamp
    if isinstance(payload["timestamp"], bool) or not isinstance(payload["timestamp"], int):
        raise ValueError("timestamp must be an integer")
    joined = "".join(_js_value(payload[key]) for key in sorted(payload)
                     if payload[key] is not None)
    first = hashlib.md5(("null" + joined + SIGN_KEY).encode("utf-8")).hexdigest()
    payload["sign"] = hashlib.md5(first.encode("ascii")).hexdigest()
    return payload


def _bounded_int(name: str, value: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be {minimum}..{maximum}")
    return value


def _bounded_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be numeric")
    value = float(value)
    if not math.isfinite(value) or not 1 <= value <= 60:
        raise ValueError("timeout must be 1..60 seconds")
    return value


class Job51XYZAdapter:
    name = "Job51XYZPublicPortal"

    def __init__(self, *, max_pages: int = 20, max_jobs: int = 500,
                 max_requests: int = 25, timeout: float = 20, page_size: int = 10):
        self.max_pages = _bounded_int("max_pages", max_pages, 1, 50)
        self.max_jobs = _bounded_int("max_jobs", max_jobs, 1, 1000)
        self.max_requests = _bounded_int("max_requests", max_requests, 2, 60)
        self.page_size = _bounded_int("page_size", page_size, 1, 50)
        self.timeout = _bounded_timeout(timeout)
        self._requests = 0

    def _consume_request(self) -> None:
        if self._requests >= self.max_requests:
            raise Job51XYZError("request_budget_exhausted")
        self._requests += 1

    @staticmethod
    def _headers() -> dict:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://xyz.51job.com",
            "Referer": "https://xyz.51job.com/",
            "User-Agent": "Mozilla/5.0 jobprep-bounded-public-collector/1.0",
            "token": "null",
        }

    async def _json(self, client: httpx.AsyncClient, method: str, path: str,
                    *, params: dict | None = None, body: dict | None = None) -> dict:
        self._consume_request()
        if method == "GET":
            response = await client.get(API_BASE + path, params=signed_parameters(params or {}),
                                        headers=self._headers())
        else:
            response = await client.post(API_BASE + path, json=signed_parameters(body or {}),
                                         headers=self._headers())
        if response.status_code in {401, 403, 429}:
            raise Job51XYZError(f"public_api_http_blocked:{response.status_code}")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise Job51XYZError(f"public_api_http_error:{response.status_code}") from exc
        try:
            value = response.json()
        except ValueError as exc:
            raise Job51XYZError("non_json_response") from exc
        if not isinstance(value, dict):
            raise Job51XYZError("invalid_response_object")
        if str(value.get("result")) != "1" or not isinstance(value.get("data"), dict):
            code = str(value.get("code") or "unknown")[:40]
            raise Job51XYZError(f"public_api_rejected:{code}")
        return value

    @staticmethod
    def _detail_url(numeric_ctmid: str, record: dict) -> str:
        query = {
            "ctmid": numeric_ctmid,
            "_jobId": str(record["jobId"]),
            "jobid": str(record.get("eHireJobId") or record.get("ehireJobId") or ""),
        }
        return f"https://{PUBLIC_HOST}/consumer/pc/home/job?{urlencode(query)}"

    @classmethod
    def _job(cls, numeric_ctmid: str, internal_ctmid: str, record: dict) -> dict:
        job_id = record.get("jobId")
        title = record.get("jobName")
        if not isinstance(job_id, str) or not job_id or not isinstance(title, str) or not title.strip():
            raise Job51XYZError("invalid_job_record")
        return {
            "id": job_id,
            "title": title.strip(),
            "company": str(record.get("companyName") or record.get("jobCompanyName") or "").strip(),
            "location": str(record.get("jobAreas") or "").strip(),
            "degree": str(record.get("degree") or "").strip(),
            "salary": str(record.get("salary") or "").strip(),
            "description": str(record.get("jobInfo") or "").strip(),
            "requirements": "",
            "url": cls._detail_url(numeric_ctmid, record),
            "needs_details": False,
            "url_provenance": "observed_xyz_51job_route",
            "detail_parameters": {"ctmId": internal_ctmid, "jobId": job_id},
            "raw": record,
        }

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    async def acquire(self, url: str, artifact_dir, *, client: httpx.AsyncClient | None = None) -> dict:
        canonical, numeric_ctmid = validate_source_url(url)
        self._requests = 0
        root = Path(artifact_dir).resolve() / "job51_xyz" / numeric_ctmid
        own_client = client is None
        if own_client:
            client = httpx.AsyncClient(timeout=self.timeout, headers=self._headers(), follow_redirects=False)
        assert client is not None
        jobs: list[dict] = []
        pages_seen = 0
        total_reported = None
        reported_pages = None
        internal_ctmid = None
        stop_reason = "unknown"
        raw_refs: list[str] = []
        try:
            setting = await self._json(client, "GET", SETTING_PATH,
                                       params={"ehireCtmId": numeric_ctmid})
            self._write_json(root / "customer_setting.json", setting)
            raw_refs.append(str((root / "customer_setting.json").resolve()))
            internal_ctmid = setting["data"].get("ctmId")
            if not isinstance(internal_ctmid, str) or not GUID_RE.fullmatch(internal_ctmid):
                raise Job51XYZError("customer_setting_missing_internal_ctmid")

            seen_ids: set[str] = set()
            for page in range(1, self.max_pages + 1):
                if len(jobs) >= self.max_jobs:
                    stop_reason = "max_jobs"
                    break
                body = {
                    "ctmId": internal_ctmid,
                    "pageIndex": page,
                    "pageSize": min(self.page_size, self.max_jobs - len(jobs)),
                    "companyId": [], "funcType": [], "jobArea": [], "jobType": [],
                    "jobCategory": [], "keyWord": "", "sceneType": "00",
                }
                payload = await self._json(client, "POST", LIST_PATH, body=body)
                path = root / f"jobs_page_{page:03d}.json"
                self._write_json(path, payload)
                raw_refs.append(str(path.resolve()))
                data = payload["data"]
                records = data.get("records")
                if not isinstance(records, list):
                    raise Job51XYZError("job_list_records_missing")
                pages_seen += 1
                if total_reported is None and isinstance(data.get("total"), int):
                    total_reported = data["total"]
                if reported_pages is None and isinstance(data.get("pages"), int):
                    reported_pages = data["pages"]
                added = 0
                for record in records:
                    if not isinstance(record, dict):
                        raise Job51XYZError("invalid_job_record")
                    job = self._job(numeric_ctmid, internal_ctmid, record)
                    if job["id"] in seen_ids:
                        continue
                    seen_ids.add(job["id"])
                    jobs.append(job)
                    added += 1
                    if len(jobs) >= self.max_jobs:
                        break
                if not records:
                    stop_reason = "empty_page"
                    break
                if added == 0:
                    stop_reason = "duplicate_page"
                    break
                if total_reported is not None and len(jobs) >= total_reported:
                    stop_reason = "reported_total_reached"
                    break
                if reported_pages is not None and page >= reported_pages:
                    stop_reason = "reported_last_page"
                    break
            else:
                stop_reason = "max_pages"

            list_complete = (isinstance(total_reported, int) and len(jobs) == total_reported
                             and stop_reason in {"reported_total_reached", "reported_last_page"})
            if stop_reason == "reported_last_page" and len(jobs) != total_reported:
                stop_reason = "reported_total_mismatch"
            detail_urls = [job["url"] for job in jobs]
            result = {
                "url": canonical,
                "final_url": canonical,
                "status": "ok" if list_complete else "partial",
                "method": "xyz_51job_public_api",
                "title": "",
                "text": "",
                "jobs": jobs,
                "detail_urls": detail_urls,
                "images": [],
                "links": detail_urls,
                "warnings": [] if list_complete else [f"collection stopped: {stop_reason}"],
                "json_paths": raw_refs,
                "coverage": {
                    "complete": list_complete,
                    "list_complete": list_complete,
                    "jd_complete": True,
                    "pages_seen": pages_seen,
                    "total_reported": total_reported,
                    "reported_pages": reported_pages,
                    "jobs_received": len(jobs),
                    "detail_urls": detail_urls,
                    "detail_endpoint": API_BASE + DETAIL_PATH,
                    "detail_parameter_names": ["ctmId", "jobId"],
                    "requests_used": self._requests,
                    "max_requests": self.max_requests,
                    "stop_reason": stop_reason,
                },
            }
            self._write_json(root / "result.json", result)
            return result
        except (httpx.HTTPError, Job51XYZError) as exc:
            reason = str(exc) if isinstance(exc, Job51XYZError) else "transport_or_http_error"
            blocked = reason.startswith(("public_api_rejected:", "public_api_http_blocked:"))
            partial = bool(jobs) and reason == "request_budget_exhausted"
            result = {
                "url": canonical, "final_url": canonical,
                "status": "blocked" if blocked else "partial" if partial else "error",
                "method": "xyz_51job_public_api", "jobs": jobs,
                "detail_urls": [job["url"] for job in jobs], "images": [], "links": [],
                "warnings": [reason], "error": reason, "json_paths": raw_refs,
                "retryable": False,
                "coverage": {"complete": False, "list_complete": False,
                             "jd_complete": False, "pages_seen": pages_seen,
                             "jobs_received": len(jobs), "requests_used": self._requests,
                             "max_requests": self.max_requests, "stop_reason": reason},
            }
            self._write_json(root / "result.json", result)
            return result
        finally:
            if own_client:
                await client.aclose()


class Job51XYZPublicPortal:
    """Registry bridge for the bounded XYZ collector.

    The collector remains independently testable; this class only translates the
    registry context and documented options into its constructor.
    """

    name = "Job51XYZPublicPortal"
    priority = 110
    scope = "Public xyz.51job.com list portals with one numeric ctmid"
    parameters = {
        "max_pages": {"type": "integer", "default": 20, "range": "1..50"},
        "max_jobs": {"type": "integer", "default": 500, "range": "1..1000"},
        "max_requests": {"type": "integer", "default": 25, "range": "2..60"},
        "timeout": {"type": "number", "default": 20, "range": "1..60 seconds"},
        "page_size": {"type": "integer", "default": 10, "range": "1..50"},
    }

    def matches(self, url, options):
        try:
            validate_source_url(url)
            return True
        except (TypeError, ValueError):
            return False

    async def acquire(self, context):
        supplied = context.options
        supported = set(self.parameters)
        options = {key: supplied[key] for key in supported if key in supplied}
        ignored = sorted(set(supplied) - supported)
        if ignored:
            context.provenance.append({
                "rule": "51job_xyz_options_translation",
                "ignored_option_names": ignored,
            })
        collector = Job51XYZAdapter(**options)
        result = await collector.acquire(context.url, context.artifact_dir)
        result.setdefault("title", "")
        result.setdefault("text", "")
        result.setdefault("links", result.get("detail_urls", []))
        result.setdefault("warnings", [])
        context.provenance.append({
            "rule": "51job_xyz_public_api",
            "url": result.get("final_url", context.url),
            "requests_used": result.get("coverage", {}).get("requests_used", 0),
        })
        return result


async def _run_cli(args) -> int:
    adapter = Job51XYZAdapter(max_pages=args.max_pages, max_jobs=args.max_jobs,
                              max_requests=args.max_requests, timeout=args.timeout)
    result = await adapter.acquire(args.url, args.artifact_dir)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"ok", "partial"} else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Collect one public xyz.51job.com portal")
    parser.add_argument("url")
    parser.add_argument("--artifact-dir", default="data/artifacts")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-jobs", type=int, default=500)
    parser.add_argument("--max-requests", type=int, default=25)
    parser.add_argument("--timeout", type=float, default=20)
    return asyncio.run(_run_cli(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Job51XYZAdapter", "Job51XYZPublicPortal", "Job51XYZError",
    "signed_parameters", "validate_source_url",
]
