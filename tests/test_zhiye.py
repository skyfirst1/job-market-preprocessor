import asyncio
import json

import pytest

from jobprep.app import zhiye


def job(number):
    return {
        "JobAdId": number,
        "JobAdName": f"岗位 {number}",
        "Duty": "负责模型研发、训练、验证、部署和持续优化，并维护完整实验记录。",
        "Require": "本科及以上学历，具备扎实工程能力和良好沟通协作能力。",
        "Category": "校园招聘",
    }


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class Client:
    def __init__(self, responses, calls, **kwargs):
        self.responses = iter(responses)
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, json, headers):
        self.calls.append((url, json, headers))
        return next(self.responses)


def browser_result(paths):
    return {
        "url": "https://demo.zhiye.com/campus/jobs",
        "status": "partial",
        "jobs": [],
        "warnings": [],
        "json_paths": [str(path) for path in paths],
        "coverage": {"complete": False, "list_complete": False},
    }


def test_finalize_requires_both_structured_duty_and_require():
    complete = {
        "title": "算法工程师",
        "url": "https://demo.zhiye.com/campus/jobdetails?jobId=1",
        "text": job(1)["Duty"] + "\n" + job(1)["Require"],
        "raw": job(1),
    }
    missing = {
        "title": "数据工程师",
        "url": "https://demo.zhiye.com/campus/jobdetails?jobId=2",
        "text": "只有一段很长的职责说明，不能据此声称任职要求也已完整采集。" * 3,
        "raw": {"Duty": "只有职责，没有要求字段。" * 3},
    }
    result = {"status": "partial", "jobs": [complete, missing], "coverage": {}}
    zhiye.finalize_public_list(result, 2)
    assert complete["needs_details"] is False
    assert missing["needs_details"] is True
    assert result["status"] == "ok"
    assert result["coverage"]["list_complete"] is True
    assert result["coverage"]["jd_complete"] is False
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["details_optional"] is True
    assert result["coverage"]["detail_gap"] == "duties_or_requirements_incomplete"
    assert result["coverage"]["stop_reason"] == "zhiye_public_api_list_complete_details_optional"
    assert result["coverage"]["detail_urls"] == [missing["url"]]


def test_collect_paginates_observed_api_until_reported_total(tmp_path, monkeypatch):
    endpoint = "https://demo.zhiye.com/api/JobAd/GetJobAdPageList"
    portal = "https://demo.zhiye.com/api/PortalAgent/GetPortalAgentConfig?portalId=abc"
    endpoint_record = tmp_path / "endpoint.json"
    portal_record = tmp_path / "portal.json"
    endpoint_record.write_text(json.dumps({"url": endpoint}), encoding="utf-8")
    portal_record.write_text(json.dumps({"url": portal}), encoding="utf-8")

    async def fake_collect(url, artifact_dir, options):
        return browser_result([endpoint_record, portal_record])

    calls = []
    responses = [
        Response({"total": 101, "items": [job(index) for index in range(100)]}),
        Response({"total": 101, "items": [job(100)]}),
    ]
    monkeypatch.setattr(zhiye.crawler, "collect", fake_collect)
    monkeypatch.setattr(
        zhiye.httpx,
        "AsyncClient",
        lambda **kwargs: Client(responses, calls, **kwargs),
    )

    result = asyncio.run(zhiye.collect(
        "https://demo.zhiye.com/campus/jobs", tmp_path, {"max_pages": 3}
    ))
    assert [call[1]["PageIndex"] for call in calls] == [0, 1]
    assert len(result["jobs"]) == 101
    assert result["status"] == "ok"
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["api_items_seen"] == 101
    assert result["coverage"]["jd_complete"] is True
    assert result["coverage"]["detail_urls"] == []
    assert all(item["needs_details"] is False for item in result["jobs"])


def test_collect_page_bound_retains_reported_total_mismatch(tmp_path, monkeypatch):
    endpoint = "https://demo.zhiye.com/api/JobAd/GetJobAdPageList"
    portal = "https://demo.zhiye.com/api/PortalAgent/GetPortalAgentConfig?portalId=abc"
    records = []
    for index, url in enumerate((endpoint, portal)):
        path = tmp_path / f"record-{index}.json"
        path.write_text(json.dumps({"url": url}), encoding="utf-8")
        records.append(path)

    async def fake_collect(url, artifact_dir, options):
        return browser_result(records)

    calls = []
    monkeypatch.setattr(zhiye.crawler, "collect", fake_collect)
    monkeypatch.setattr(
        zhiye.httpx,
        "AsyncClient",
        lambda **kwargs: Client(
            [Response({"total": 150, "items": [job(index) for index in range(100)]})],
            calls,
            **kwargs,
        ),
    )
    result = asyncio.run(zhiye.collect(
        "https://demo.zhiye.com/campus/jobs", tmp_path, {"max_pages": 1}
    ))
    assert result["status"] == "partial"
    assert result["coverage"]["list_complete"] is False
    assert result["coverage"]["stop_reason"] == "reported_total_mismatch"


@pytest.mark.parametrize("value", [0, 101, True, 1.5])
def test_collect_rejects_invalid_page_bound(tmp_path, value):
    with pytest.raises(ValueError, match="max_pages"):
        asyncio.run(zhiye.collect("https://demo.zhiye.com/campus/jobs", tmp_path,
                                  {"max_pages": value}))
