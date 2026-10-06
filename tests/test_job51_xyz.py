import asyncio
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from jobprep.adapters import AdapterRegistry
from jobprep.adapters.job51_xyz import (
    Job51XYZAdapter, Job51XYZPublicPortal, signed_parameters, validate_source_url,
)


URL = "https://xyz.51job.com/consumer/pc/home/index?ctmid=9588338"
GUID = "DBF8789E-4800-4F36-8503-CB85D2DD80DE"


def test_strict_url_and_ctmid_validation():
    assert validate_source_url(URL) == (URL, "9588338")
    for invalid in (
        "http://xyz.51job.com/consumer/pc/home/index?ctmid=9588338",
        "https://evil.test/consumer/pc/home/index?ctmid=9588338",
        "https://xyz.51job.com/consumer/pc/home/job?ctmid=9588338",
        "https://xyz.51job.com/consumer/pc/home/index?ctmid=../../x",
        "https://xyz.51job.com/consumer/pc/home/index?ctmid=9588338&ctmid=8949592",
    ):
        with pytest.raises(ValueError):
            validate_source_url(invalid)


def test_sign_matches_observed_public_bundle_request():
    body = {
        "ctmId": GUID, "pageIndex": 1, "pageSize": 10,
        "companyId": [], "funcType": [], "jobArea": [], "jobType": [],
        "jobCategory": [], "keyWord": "", "sceneType": "00",
    }
    signed = signed_parameters(body, timestamp=1791249983)
    assert signed["sign"] == "ecbedae64d144caaa8c32d2b2a8c2f7e"


def _response(request, data):
    return httpx.Response(200, request=request,
                          json={"result": "1", "code": "200", "data": data})


def test_complete_pagination_and_artifacts(tmp_path):
    calls = []

    async def handler(request):
        calls.append(request)
        assert request.url.host == "xyzapij.51job.com"
        assert request.headers["token"] == "null"
        if "get_customer_setting" in request.url.path:
            return _response(request, {"ctmId": GUID})
        body = json.loads(request.content)
        page = body["pageIndex"]
        records = [{"jobId": f"00000000-0000-0000-0000-00000000000{n}",
                    "jobName": f"算法工程师{n}", "eHireJobId": 100 + n,
                    "jobInfo": "深度学习", "jobAreas": "深圳"}
                   for n in ([1, 2] if page == 1 else [3])]
        return _response(request, {"records": records, "total": 3, "pages": 2})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await Job51XYZAdapter(max_pages=3, max_requests=4).acquire(
                URL, tmp_path, client=client)

    result = asyncio.run(run())
    assert result["status"] == "ok"
    assert len(result["jobs"]) == 3 and len(result["detail_urls"]) == 3
    assert result["coverage"]["list_complete"] is True
    assert result["coverage"]["pages_seen"] == 2
    assert result["coverage"]["requests_used"] == 3
    assert result["jobs"][0]["detail_parameters"] == {"ctmId": GUID,
                                                          "jobId": "00000000-0000-0000-0000-000000000001"}
    assert (tmp_path / "job51_xyz" / "9588338" / "result.json").is_file()
    assert len(calls) == 3


def test_page_limit_is_partial_and_does_not_overrun(tmp_path):
    async def handler(request):
        if "get_customer_setting" in request.url.path:
            return _response(request, {"ctmId": GUID})
        body = json.loads(request.content)
        record = {"jobId": f"00000000-0000-0000-0000-00000000000{body['pageIndex']}",
                  "jobName": "工程师", "eHireJobId": body["pageIndex"]}
        return _response(request, {"records": [record], "total": 20, "pages": 20})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Job51XYZAdapter(max_pages=2, max_requests=3).acquire(
                URL, tmp_path, client=client)

    result = asyncio.run(run())
    assert result["status"] == "partial"
    assert result["coverage"]["stop_reason"] == "max_pages"
    assert result["coverage"]["requests_used"] == 3


def test_api_rejection_is_explicit_blocked(tmp_path):
    async def handler(request):
        return httpx.Response(200, request=request,
                              json={"result": "0", "code": "Q0099", "msg": "blocked"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Job51XYZAdapter(max_requests=2).acquire(URL, tmp_path, client=client)

    result = asyncio.run(run())
    assert result["status"] == "blocked"
    assert result["coverage"]["stop_reason"] == "public_api_rejected:Q0099"
    assert result["jobs"] == [] and result["retryable"] is False


def test_protected_http_status_is_explicit_blocked(tmp_path):
    async def handler(request):
        return httpx.Response(403, request=request, text="forbidden")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Job51XYZAdapter(max_requests=2).acquire(URL, tmp_path, client=client)

    result = asyncio.run(run())
    assert result["status"] == "blocked"
    assert result["coverage"]["stop_reason"] == "public_api_http_blocked:403"


@pytest.mark.parametrize("kwargs", [
    {"max_pages": 0}, {"max_jobs": 1001}, {"max_requests": 1},
    {"timeout": 0}, {"page_size": 51},
])
def test_bounds_rejected(kwargs):
    with pytest.raises(ValueError):
        Job51XYZAdapter(**kwargs)


def test_registry_adapter_description_and_parameter_bounds():
    adapter = Job51XYZPublicPortal()
    assert adapter.priority > 95
    assert adapter.matches(URL, {})
    assert not adapter.matches("https://xyz.51job.com/consumer/pc/home/job?ctmid=9588338", {})
    assert adapter.parameters["max_requests"]["range"] == "2..60"
    assert adapter.parameters["page_size"]["range"] == "1..50"


def test_existing_xyz_artifacts_replay_without_network(tmp_path):
    source = Path(__file__).resolve().parents[1] / "data" / "artifacts" / "job51_xyz" / "9588338"
    setting = json.loads((source / "customer_setting.json").read_text(encoding="utf-8"))
    pages = [json.loads(path.read_text(encoding="utf-8"))
             for path in sorted(source.glob("jobs_page_*.json"))]
    assert len(pages) == 5

    async def handler(request):
        if "get_customer_setting" in request.url.path:
            return httpx.Response(200, request=request, json=deepcopy(setting))
        body = json.loads(request.content)
        return httpx.Response(200, request=request, json=deepcopy(pages[body["pageIndex"] - 1]))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Job51XYZAdapter(max_pages=20, max_jobs=500, max_requests=25).acquire(
                URL, tmp_path, client=client)

    result = asyncio.run(run())
    assert result["status"] == "ok"
    assert result["coverage"]["list_complete"] is True
    assert result["coverage"]["pages_seen"] == 5
    assert result["coverage"]["requests_used"] == 6
    assert len(result["jobs"]) == 42


def test_registry_delegates_options_to_xyz_collector_from_saved_result(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "data" / "artifacts" / "job51_xyz" / "9588338"
    saved = json.loads((source / "result.json").read_text(encoding="utf-8"))
    observed = {}

    async def replay(self, url, artifact_dir, *, client=None):
        observed.update(max_pages=self.max_pages, max_jobs=self.max_jobs,
                        max_requests=self.max_requests, timeout=self.timeout,
                        page_size=self.page_size, url=url, artifact_dir=artifact_dir)
        return deepcopy(saved)

    monkeypatch.setattr(Job51XYZAdapter, "acquire", replay)
    result = asyncio.run(AdapterRegistry().acquire(
        URL, tmp_path,
        {"max_pages": 7, "max_jobs": 80, "max_requests": 9,
         "timeout": 12, "page_size": 20, "browser": False},
        tools=object(),
    ))
    assert result["status"] == "ok"
    assert result["acquisition"]["adapter"] == "Job51XYZPublicPortal"
    assert observed == {
        "max_pages": 7, "max_jobs": 80, "max_requests": 9,
        "timeout": 12.0, "page_size": 20, "url": URL, "artifact_dir": tmp_path,
    }
    assert result["acquisition"]["provenance"][0]["ignored_option_names"] == ["browser"]


def test_registry_rejects_invalid_xyz_bounds_before_network(tmp_path):
    result = asyncio.run(AdapterRegistry().acquire(
        URL, tmp_path, {"max_requests": 1}, tools=object()))
    assert result["status"] == "error"
    assert result["error_kind"] == "invalid_configuration"
