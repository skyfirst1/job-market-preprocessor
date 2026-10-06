import asyncio
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from jobprep.adapters import AdapterRegistry, BaseAdapter
from jobprep.adapters import validation


def doc(url="https://example.test/", **changes):
    result = {"url": url, "status": "ok", "jobs": [], "images": [], "links": [],
              "coverage": {"complete": True, "list_complete": True},
              "attempts": [{"transport": "http"}], "method": "http"}
    result.update(changes)
    return result


def list_config():
    return {"url": "https://api.example.test/jobs", "pagination": {"path": "page"},
            "response": {"items_path": "data.items"}, "fields": {"title": "name"}}


class MockTools:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def _call(self, name, target, artifact_dir, options=None):
        self.calls.append((name, deepcopy(target), artifact_dir, deepcopy(options)))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    async def web(self, url, artifact_dir, options=None):
        return await self._call("web", url, artifact_dir, options)

    async def wechat(self, url, artifact_dir, options=None):
        return await self._call("wechat", url, artifact_dir, options)

    async def feishu_list(self, url, artifact_dir, options=None):
        return await self._call("feishu_list", url, artifact_dir, options)

    async def zhiye_list(self, url, artifact_dir, options=None):
        return await self._call("zhiye_list", url, artifact_dir, options)

    async def public_list(self, config, artifact_dir):
        return await self._call("public_list", config, artifact_dir)


def acquire(tools, url="https://example.test/", options=None, config=None):
    return asyncio.run(AdapterRegistry(config).acquire(url, "artifacts", options, tools))


def test_generic_preserves_evidence_and_options():
    original = doc(jobs=[{"title": "unfiltered role"}], images=[{"path": "img.png"}],
                   acquisition={"attempts": [{"transport": "browser"}]})
    tools = MockTools(original)
    options = {"max_images": 3, "search_terms": ["A"], "browser": "auto"}
    result = acquire(tools, options=options)
    assert tools.calls == [("web", "https://example.test/", "artifacts", options)]
    assert result["jobs"] == original["jobs"]
    assert result["images"] == original["images"]
    assert result["attempts"] == original["attempts"]
    trace = result["acquisition"]
    assert trace["adapter"] == "genericweb"
    assert trace["tool_metadata"] == original["acquisition"]
    assert trace["provenance"][0]["acquisition"] == original["acquisition"]
    assert trace["attempts"][0]["status"] == "ok"
    assert options["max_images"] == 3 and "acquisition" in original


@pytest.mark.parametrize("url,expected", [
    ("https://mp.weixin.qq.com/s?a=1", "WechatImage"),
    ("https://MP.WEIXIN.QQ.COM/s", "WechatImage"),
    ("https://mp.weixin.qq.com.evil.test/s", "genericweb"),
    ("https://evilmp.weixin.qq.com/s", "genericweb"),
    ("https://evil.test/?host=mp.weixin.qq.com", "genericweb"),
    ("https://tenant.jobs.feishu.cn/position/list", "FeishuPublicPortal"),
    ("https://tenant.jobs.feishu.cn/2027/position/list", "FeishuPublicPortal"),
    ("https://tenant.jobs.feishu.cn/huixicampus/position/list?type=campus", "FeishuPublicPortal"),
    ("https://tenant.jobs.feishu.cn/#/2027/position/list", "FeishuPublicPortal"),
    ("https://tenant.jobs.feishu.cn/2027/position/list/", "FeishuPublicPortal"),
    ("https://tenant.jobs.feishu.cn/#/position/list", "FeishuPublicPortal"),
    ("https://tenant.jobs.feishu.cn/", "FeishuPublicPortal"),
    ("https://tenant.jobs.feishu.cn/position/123", "genericweb"),
    ("https://tenant.jobs.feishu.cn/position/detail/123", "genericweb"),
    ("https://tenant.jobs.feishu.cn/2027/position/list/detail/123", "genericweb"),
    ("https://tenant.jobs.feishu.cn/2027/position/listing", "genericweb"),
    ("https://tenant.jobs.feishu.cn/huixicampus/position/detail/42", "genericweb"),
    ("https://tenant.jobs.feishu.cn/#/position/detail/123", "genericweb"),
    ("https://tenant.jobs.feishu.cn/about", "genericweb"),
    ("https://tenant.jobs.feishu.cn.evil.test/position/list", "genericweb"),
    ("https://eviljobs.feishu.cn/position/list", "genericweb"),
    ("https://xyz.51job.com/consumer/pc/home/index?ctmid=9588338", "Job51XYZPublicPortal"),
    ("https://xyz.51job.com/consumer/pc/home/job?ctmid=9588338&_jobId=x", "genericweb"),
    ("https://campus.51job.com/Innovent2027/index2.html", "Job51StaticPortal"),
    ("https://campus.51job.com/Innovent2027/", "Job51StaticPortal"),
    ("https://campus.51job.com/", "genericweb"),
    ("https://campus.51job.com/Innovent2027/js/data.js", "genericweb"),
    ("https://jobs.51job.com/example", "genericweb"),
    ("https://www.51job.com/", "genericweb"),
    ("https://tenant.zhiye.com/campus/detail?jobAdId=abc", "ZhiyeJobDetailPortal"),
    ("https://tenant.zhiye.com/campus/jobdetails?jobId=123", "ZhiyeJobDetailPortal"),
])
def test_selection_boundaries(url, expected):
    assert AdapterRegistry().select(url).name == expected


@pytest.mark.parametrize("url,expected", [
    ("https://app.mokahr.com/campus-recruitment/jointown/141506?locale=zh-CN#/", "MokahrPublicPortal"),
    ("https://app.mokahr.com/campus_apply/pinsmedical/2726#/jobs?page=1", "MokahrPublicPortal"),
    ("https://zhaopin.zhongjing.cn/campus-recruitment/zhongjing/142977#/page/", "MokahrPublicPortal"),
    ("https://wecruit.hotjob.cn/SU61458d83bef57c54dcb4e43f/pb/school.html#/", "HotjobPublicPortal"),
    ("https://salubris.hotjob.cn/", "HotjobPublicPortal"),
    ("https://campus.51job.com/Innovent2027/index2.html", "Job51StaticPortal"),
])
def test_special_site_selection(url, expected):
    assert AdapterRegistry().select(url).name == expected


def test_mokahr_rewrites_jobs_route_and_bounds_options():
    url = "https://app.mokahr.com/campus-recruitment/jointown/141506?locale=zh-CN#/"
    tools = MockTools(doc(url, status="partial", coverage={"complete": False, "list_complete": False}))
    result = acquire(tools, url, {"max_pages": 12, "max_jobs": 500, "timeout": 30})
    call = tools.calls[0]
    assert call[0] == "web" and call[1].endswith("?locale=zh-CN#/jobs")
    assert call[3]["max_pages"] == 12 and call[3]["max_scrolls"] == 0
    assert "Pagination-forward" in call[3]["next_selector"]
    assert result["coverage"]["list_url"] == call[1]


def test_mokahr_legacy_campus_apply_route_uses_special_adapter():
    url = "https://app.mokahr.com/campus_apply/pinsmedical/2726#/jobs?page=1"
    tools = MockTools(doc(url, status="partial", coverage={"complete": False,
                                                           "list_complete": False}))
    result = acquire(tools, url, {"max_pages": 30})
    assert result["acquisition"]["adapter"] == "MokahrPublicPortal"
    assert tools.calls[0][1] == "https://app.mokahr.com/campus_apply/pinsmedical/2726#/jobs"
    assert tools.calls[0][3]["max_pages"] == 30


def test_zhiye_detail_is_bounded_single_job_web_extraction():
    url = "https://innoventbio.zhiye.com/campus/detail?jobAdId=abc"
    job = {"id": "generated", "title": "算法工程师",
           "url": "https://innoventbio.zhiye.com/campus/jobdetails?jobId=123",
           "text": ("负责模型训练、推理、评估与优化，使用深度学习框架完成算法研发和工程落地；"
                    "维护训练数据、实验记录、性能基线及线上推理服务。"),
           "raw": {"Id": "abc", "JobAdId": 123}}
    tools = MockTools(doc(url, status="partial", jobs=[job],
                          coverage={"complete": False, "list_complete": False,
                                    "stop_reason": "unknown_pagination"}))
    result = acquire(tools, url, {"max_pages": 10, "max_scrolls": 8, "max_images": 8})
    call = tools.calls[0]
    assert call[0] == "web" and call[1] == url
    assert call[3]["max_pages"] == 1 and call[3]["max_scrolls"] == 0
    assert call[3]["max_images"] == 0
    assert result["acquisition"]["adapter"] == "ZhiyeJobDetailPortal"
    assert result["coverage"]["scope"] == "single_job"
    assert result["coverage"]["stop_reason"] == "single_job_complete"
    assert result["coverage"]["completion_basis"] == "stable_job_id_and_single_structured_job"
    assert result["status"] == "ok" and result["coverage"]["jd_complete"]
    assert result["jobs"][0]["needs_details"] is False
    assert result["coverage"]["detail_urls"] == []
    assert result["coverage"]["needs_details_count"] == 0


def test_zhiye_detail_without_identity_matched_job_stays_partial():
    url = "https://innoventbio.zhiye.com/campus/detail?jobAdId=missing"
    tools = MockTools(doc(url, jobs=[], coverage={"complete": False,
                                                  "list_complete": False,
                                                  "stop_reason": "unknown_pagination"}))
    result = acquire(tools, url)
    assert result["status"] == "partial"
    assert not result["coverage"]["complete"]
    assert result["coverage"]["stop_reason"] == "single_job_evidence_incomplete"


def test_mokahr_detail_is_not_rewritten_to_jobs_list():
    job_id = "9cc574cb-9aae-430f-a7d7-789d2e76da7f"
    url = ("https://app.mokahr.com/campus-recruitment/bayer/148388#/job/" + job_id
           + "?from=qrcode&isRecommendation=undefined")
    job = {"id": job_id, "title": "数据科学家", "url": url,
           "text": ("负责机器学习模型的训练、评估、部署和持续优化，并维护可复现的工程管线；"
                    "建立数据质量、离线指标、线上监控和回归测试机制。")}
    tools = MockTools(doc(url, status="partial", jobs=[job],
                          coverage={"complete": False, "list_complete": False,
                                    "stop_reason": "terminal_pagination"}))
    result = acquire(tools, url, {"max_pages": 30})
    call = tools.calls[0]
    assert result["acquisition"]["adapter"] == "MokahrJobDetailPortal"
    assert call[0] == "web" and call[1] == url
    assert call[3]["max_pages"] == 1 and call[3]["max_scrolls"] == 0
    assert result["status"] == "ok"
    assert result["coverage"]["stop_reason"] == "single_job_complete"
    assert result["jobs"][0]["needs_details"] is False


def test_hotjob_builds_bounded_public_list_config():
    url = "https://wecruit.hotjob.cn/SU61458d83bef57c54dcb4e43f/pb/school.html#/"
    raw = doc(url, jobs=[{"id": "abc", "title": "AI", "needs_details": True}],
              json_paths=["page-1.json"], evidence=[{"raw_ref": "page-1.json"}],
              coverage={"complete": False, "list_complete": True,
                        "stop_reason": "last_page", "expected_total": 1,
                        "expected_total_pages": 1, "pages_received": 1,
                        "pages": [{"actual_page": 1, "accepted": True,
                                   "raw_ref": "page-1.json"}]})
    tools = MockTools(raw)
    result = acquire(tools, url, {"max_pages": 20, "max_jobs": 1000})
    name, config, _, options = tools.calls[0]
    assert name == "public_list" and options is None
    assert config["method"] == "POST" and config["body_encoding"] == "form"
    assert config["pagination"] == {"location": "body", "path": "currentPage", "start": 1, "max_pages": 20}
    assert config["response"]["total_pages_path"] == "data.pageForm.totalPage"
    assert result["jobs"][0]["url"].endswith("school.html#abc")
    assert result["status"] == "ok" and not result["coverage"]["complete"]
    assert result["coverage"]["jd_complete"] is False
    assert result["coverage"]["list_completion_evidence"]["expected_total"] == 1


def test_hotjob_does_not_promote_unproved_last_page():
    url = "https://wecruit.hotjob.cn/SU61458d83bef57c54dcb4e43f/pb/school.html#/"
    raw = doc(url, status="partial", jobs=[{"id": "abc", "title": "AI"}],
              coverage={"complete": False, "list_complete": True,
                        "stop_reason": "last_page", "expected_total": 2,
                        "expected_total_pages": 1, "pages_received": 1,
                        "pages": [{"actual_page": 1, "accepted": True,
                                   "raw_ref": "page-1.json"}]})
    result = acquire(MockTools(raw), url)
    assert result["status"] == "partial"
    assert "list_completion_evidence" not in result["coverage"]


def test_mokahr_terminal_list_success_does_not_require_jd_bodies():
    url = "https://app.mokahr.com/campus-recruitment/acme/123#/jobs"
    raw = doc(url, status="partial", jobs=[{"id": "one", "title": "AI"}],
              html_paths=["page-1.html"],
              coverage={"complete": False, "list_complete": True,
                        "jd_complete": False, "pages_seen": 1,
                        "stop_reason": "terminal_pagination"})
    result = acquire(MockTools(raw), url)
    assert result["status"] == "ok"
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["list_completion_evidence"]["pages_seen"] == 1


def test_mokahr_terminal_without_artifact_evidence_stays_partial():
    url = "https://app.mokahr.com/campus-recruitment/acme/123#/jobs"
    raw = doc(url, status="partial", jobs=[{"id": "one", "title": "AI"}],
              coverage={"complete": False, "list_complete": True,
                        "pages_seen": 1, "stop_reason": "terminal_pagination"})
    result = acquire(MockTools(raw), url)
    assert result["status"] == "partial"


def test_mokahr_offline_snapshots_prove_stable_terminal_list(tmp_path):
    url = "https://app.mokahr.com/campus-recruitment/acme/123#/jobs"
    page1 = tmp_path / "page1.html"
    page2 = tmp_path / "page2.html"
    page3 = tmp_path / "page3.html"
    page1.write_text('<a href="#/job/one-1">岗位一</a>', encoding="utf-8")
    final = '<a href="#/job/two-2">岗位二</a>'
    page2.write_text(final, encoding="utf-8")
    page3.write_text(final + "<!-- stable render marker -->", encoding="utf-8")
    first = __import__("jobprep.html_extract", fromlist=["extract_html"]).extract_html(
        page1.read_text(encoding="utf-8"), url)["jobs"]
    second = __import__("jobprep.html_extract", fromlist=["extract_html"]).extract_html(
        page2.read_text(encoding="utf-8"), url)["jobs"]
    raw = doc(url, status="partial", jobs=first + second,
              html_paths=[str(page1), str(page2), str(page3)],
              coverage={"complete": False, "list_complete": False,
                        "pages_seen": 2, "stop_reason": "unknown_pagination"})
    result = acquire(MockTools(raw), url)
    assert result["status"] == "ok"
    assert result["coverage"]["stop_reason"] == "stable_terminal_snapshots"
    assert result["coverage"]["list_completion_evidence"]["job_count"] == 2


def test_mokahr_offline_snapshots_reject_incomplete_identity_union(tmp_path):
    url = "https://app.mokahr.com/campus-recruitment/acme/123#/jobs"
    page1 = tmp_path / "page1.html"
    page2 = tmp_path / "page2.html"
    page1.write_text('<a href="#/job/one-1">岗位一</a>', encoding="utf-8")
    page2.write_text('<a href="#/job/one-1">岗位一</a>', encoding="utf-8")
    raw = doc(url, status="partial", jobs=[{"id": "unobserved", "title": "岗位二"}],
              html_paths=[str(page1), str(page2)],
              coverage={"complete": False, "list_complete": False,
                        "pages_seen": 1, "stop_reason": "unknown_pagination"})
    result = acquire(MockTools(raw), url)
    assert result["status"] == "partial"
    assert result["coverage"]["stop_reason"] == "unknown_pagination"


def test_hotjob_root_discovers_suite_before_public_api():
    url = "https://salubris.hotjob.cn/"
    landing = doc(url, links=[{"url": "https://salubris.hotjob.cn/SU68b7fe15720ec026827c5d67/pb/school.html"}])
    listed = doc(url, jobs=[], coverage={"complete": True, "list_complete": True})
    tools = MockTools(landing, listed)
    result = acquire(tools, url)
    assert [call[0] for call in tools.calls] == ["web", "public_list"]
    assert "/SU68b7fe15720ec026827c5d67" in tools.calls[1][1]["url"]
    assert result["acquisition"]["adapter"] == "HotjobPublicPortal"


@pytest.mark.parametrize("override", [None, False, True])
def test_wechat_images_only_default_and_override(override):
    tools = MockTools(doc("https://mp.weixin.qq.com/s"))
    options = {} if override is None else {"images_only": override}
    result = acquire(tools, "https://mp.weixin.qq.com/s", options)
    assert result["status"] == "ok"
    assert tools.calls[0][3]["images_only"] is (False if override is None else override)
    assert options == ({} if override is None else {"images_only": override})


def test_wechat_filters_runner_options_and_maps_aliases():
    url = "https://mp.weixin.qq.com/s"
    options = {"browser": "auto", "max_pages": 9, "max_scrolls": 3, "delay": 0,
               "channel": "msedge", "timeout": 20, "max_images": 30, "max_requests": 80,
               "search_terms": ["AI"], "search_selector": "input", "retries": 2}
    original = deepcopy(options)
    tools = MockTools(doc(url, text="article"))
    result = acquire(tools, url, options)
    assert tools.calls[0][3] == {"images_only": False, "interval": 0,
                                  "browser_channel": "msedge", "timeout": 20,
                                  "max_images": 30, "max_requests": 80}
    assert options == original
    assert result["status"] == "partial" and result["text"] == "article"
    assert result["coverage"]["stop_reason"] == "search_not_applied"
    assert result["coverage"]["search_applied"] is False
    assert result["coverage"]["pagination_applied"] is False
    assert result["coverage"]["requested_max_pages"] == 9
    assert result["coverage"]["scope"] == "single_article"
    assert not result["coverage"]["complete"] and not result["coverage"]["list_complete"]


def test_wechat_explicit_options_take_precedence_over_aliases():
    url = "https://mp.weixin.qq.com/s"
    tools = MockTools(doc(url))
    result = acquire(tools, url, {"interval": 0, "domain_delay": 15, "delay": 10,
                                 "browser_channel": None, "channel": "chrome"})
    assert tools.calls[0][3] == {"images_only": False, "interval": 0, "browser_channel": None}
    assert result["coverage"]["list_complete"] is False


@pytest.mark.parametrize("error", [TimeoutError("private"), ConnectionError("private"),
                                 httpx.ConnectError("private"), ValueError("private")])
def test_wechat_exceptions_never_allow_runner_retry(error):
    tools = MockTools(error)
    result = acquire(tools, "https://mp.weixin.qq.com/s")
    assert result["status"] == "error" and result["retryable"] is False
    assert "private" not in json.dumps(result)
    assert len(tools.calls) == 1
    assert result["acquisition"]["attempts"][0]["status"] == "error"


def test_wechat_search_cannot_hide_structured_challenge():
    url = "https://mp.weixin.qq.com/s"
    tools = MockTools(doc(url, coverage={"complete": True, "stop_reason": "challenge"}))
    result = acquire(tools, url, {"search_terms": ["AI"]})
    assert result["status"] == "blocked" and result["retryable"] is False
    assert result["coverage"]["stop_reason"] == "wechat_challenge"
    assert result["coverage"]["search_applied"] is False


def test_wechat_explicit_tool_retryability_is_overridden():
    url = "https://mp.weixin.qq.com/s"
    tools = MockTools(doc(url, status="error", retryable=True, http_status=503))
    result = acquire(tools, url)
    assert result["status"] == "error" and result["retryable"] is False


@pytest.mark.parametrize("changes", [{"status": "blocked"}, {"challenge": True},
                                     {"coverage": {"stop_reason": "wechat_challenge", "complete": True}}])
def test_wechat_challenge_terminal(changes):
    tools = MockTools(doc("https://mp.weixin.qq.com/s", **changes))
    result = acquire(tools, "https://mp.weixin.qq.com/s")
    assert result["status"] == "blocked" and result["retryable"] is False
    assert not result["coverage"]["complete"]
    assert len(tools.calls) == 1


def test_feishu_discovery_uses_only_observed_same_host_link():
    url = "https://tenant.jobs.feishu.cn/"
    target = url + "position/list?type=campus"
    tools = MockTools(doc(url, links=[{"url": "https://evil.test/position/list"},
                                     {"url": "/position/detail/42"}, {"url": "/position/list?type=campus"}]),
                      doc(target, jobs=[{"title": "HR"}], json_paths=["page.json"]))
    result = acquire(tools, url, {"max_images": 8, "max_pages": 9, "search_terms": ["AI"]})
    assert [c[0] for c in tools.calls] == ["web", "feishu_list"]
    assert tools.calls[0][3]["max_images"] == 0
    assert tools.calls[0][3]["max_pages"] == 1
    assert tools.calls[0][3]["search_terms"] == []
    assert tools.calls[1][1] == target
    assert "max_images" not in tools.calls[1][3]
    assert tools.calls[1][3]["timeout"] == 25000
    assert tools.calls[1][3]["max_pages"] == 9
    assert result["url"] == url and result["coverage"]["list_url"] == target
    assert result["jobs"] == [{"title": "HR"}]
    assert len(result["acquisition"]["attempts"]) == 2
    assert result["status"] == "partial" and not result["coverage"]["complete"]
    assert result["coverage"]["search_applied"] is False
    assert result["acquisition"]["provenance"][-1]["artifact_refs"]["json_paths"] == ["page.json"]


def test_feishu_campus_landing_is_discovered_with_one_bounded_probe():
    url = "https://tenant.jobs.feishu.cn/Campus"
    target = "https://tenant.jobs.feishu.cn/2027/position/list"
    tools = MockTools(doc(url, links=[{"url": "/2027/position/list"}]), doc(target))
    result = acquire(tools, url, {"max_pages": 25})
    assert result["acquisition"]["adapter"] == "FeishuPublicPortal"
    assert [call[0] for call in tools.calls] == ["web", "feishu_list"]
    assert tools.calls[0][3]["max_pages"] == 1
    assert tools.calls[1][1] == target


@pytest.mark.parametrize("links,reason", [([], "list_link_not_found"),
    (["/position/detail/42", "https://other.jobs.feishu.cn/position/list"], "list_link_not_found"),
    (["/position/list?a=1", "/position/list?a=2"], "ambiguous_list_links")])
def test_feishu_discovery_unconfirmed_never_complete(links, reason):
    url = "https://tenant.jobs.feishu.cn/"
    tools = MockTools(doc(url, links=links))
    result = acquire(tools, url)
    assert result["status"] == "partial"
    assert not result["coverage"]["list_complete"] and not result["coverage"]["complete"]
    assert result["coverage"]["stop_reason"] == reason and len(tools.calls) == 1


@pytest.mark.parametrize("status", ["blocked", "deleted", "error"])
def test_feishu_terminal_discovery_no_list_call(status):
    url = "https://tenant.jobs.feishu.cn/"
    tools = MockTools(doc(url, status=status, links=["/position/list"]))
    result = acquire(tools, url)
    assert result["status"] == status and len(tools.calls) == 1
    assert not result["coverage"]["complete"]


def test_direct_feishu_has_no_speculative_web_probe():
    url = "https://tenant.jobs.feishu.cn/position/list"
    tools = MockTools(doc(url, status="partial", coverage={"complete": False, "list_complete": False}))
    result = acquire(tools, url)
    assert [c[0] for c in tools.calls] == ["feishu_list"]
    assert result["status"] == "partial"


@pytest.mark.parametrize("route", ["/2027/position/list", "/huixicampus/position/list"])
def test_actual_feishu_routes_direct_and_discovered(route):
    base = "https://tenant.jobs.feishu.cn"
    target = base + route
    direct = MockTools(doc(target))
    result = acquire(direct, target, {"max_pages": 25})
    assert result["status"] == "ok"
    assert direct.calls[0][0:2] == ("feishu_list", target)
    assert direct.calls[0][3]["max_pages"] == 25
    landing = MockTools(doc(base + "/", links=[{"url": route}]), doc(target))
    discovered = acquire(landing, base + "/")
    assert discovered["coverage"]["list_url"] == target
    assert [call[0] for call in landing.calls] == ["web", "feishu_list"]


@pytest.mark.parametrize("explicit", [True, False])
def test_configured_list_exact_mapping_and_precedence(explicit):
    url = "https://mp.weixin.qq.com/s"
    cfg = list_config()
    settings = {"list_sources": {url: cfg}}
    options = {"list_config": cfg} if explicit else {}
    tools = MockTools(doc(cfg["url"], jobs=[{"title": "HR"}, {"title": "AI"}]))
    result = acquire(tools, url, options, settings)
    assert tools.calls == [("public_list", cfg, "artifacts", None)]
    assert result["acquisition"]["adapter"] == "ConfiguredPublicList"
    assert len(result["jobs"]) == 2
    assert AdapterRegistry(settings).select(url + "&extra=1").name == "WechatImage"


def test_runtime_config_overrides_mapping():
    url = "https://example.test/"
    cfg = list_config()
    tools = MockTools(doc(cfg["url"]))
    result = acquire(tools, options={"list_config": cfg}, config={"list_sources": {url: "config/missing.json"}})
    assert result["status"] == "ok"


def test_public_list_does_not_override_pagination_with_crawl_options():
    cfg = list_config()
    cfg["pagination"]["max_pages"] = 100
    tools = MockTools(doc(cfg["url"]))
    result = acquire(tools, options={"max_pages": 3, "timeout": 20, "browser": "auto"},
                     config={"list_sources": {"https://example.test/": cfg}})
    assert result["status"] == "ok"
    assert result["url"] == "https://example.test/"
    assert result["coverage"]["list_url"] == cfg["url"]
    assert tools.calls[0][1]["pagination"]["max_pages"] == 100
    assert cfg["pagination"]["max_pages"] == 100


def test_feishu_shared_options_match_real_app_validation():
    from jobprep.app.feishu import validate_options

    url = "https://tenant.jobs.feishu.cn/position/list"
    tools = MockTools(doc(url))
    result = acquire(tools, url, {"browser": "auto", "max_pages": 100, "max_scrolls": 3,
                                  "max_images": 0, "timeout": 20, "delay": 1.2})
    assert result["status"] == "ok"
    passed = tools.calls[0][3]
    assert validate_options(passed)["max_pages"] == 100
    assert passed["timeout"] == 20000 and passed["interval"] == 1.2
    assert "max_images" not in passed and "max_scrolls" not in passed


def test_feishu_browser_false_terminal_before_tools():
    tools = MockTools()
    result = acquire(tools, "https://tenant.jobs.feishu.cn/", {"browser": False})
    assert result["status"] == "error" and not result["retryable"] and not tools.calls


def test_json_config_path_and_no_env(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "WORKSPACE", tmp_path)
    (tmp_path / "config").mkdir()
    cfg = list_config()
    path = tmp_path / "config" / "list.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    (tmp_path / ".env").write_text("NOT_JSON=secret", encoding="utf-8")
    tools = MockTools(doc(cfg["url"]))
    result = acquire(tools, config={"root": str(tmp_path / "evil"), "list_sources": {"https://example.test/": "config/list.json"}})
    assert result["status"] == "ok"
    assert tools.calls[0][1] == cfg


@pytest.mark.parametrize("absolute", [False, True])
def test_explicit_list_config_cli_path(tmp_path, monkeypatch, absolute):
    monkeypatch.setattr(validation, "WORKSPACE", tmp_path)
    (tmp_path / "config").mkdir()
    cfg = list_config()
    cfg["pagination"]["max_pages"] = 100
    path = tmp_path / "config" / "list.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    supplied = str(path) if absolute else "config/list.json"
    tools = MockTools(doc(cfg["url"]))
    result = acquire(tools, options={"adapter": "ConfiguredPublicList", "list_config": supplied, "max_pages": 3})
    assert result["status"] == "ok"
    assert tools.calls[0][1] == cfg
    assert AdapterRegistry().describe()[0]["parameters"]["list_config"]["type"] == "object|path"


@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize("path", [".env", "config/source.env", "config/../outside.json", "outside.json"])
def test_config_path_rejected_before_read(tmp_path, monkeypatch, path, explicit):
    monkeypatch.setattr(validation, "WORKSPACE", tmp_path)
    tools = MockTools()
    result = acquire(tools, options={"list_config": path} if explicit else None,
                     config={} if explicit else {"list_sources": {"https://example.test/": path}})
    assert result["status"] == "error" and result["retryable"] is False
    assert result["error_kind"] == "invalid_configuration" and not tools.calls


@pytest.mark.parametrize("content", ['[]', '{"url":"a","url":"b"}', '{"bad":NaN}', 'broken'])
def test_bad_json_config_permanent(tmp_path, monkeypatch, content):
    monkeypatch.setattr(validation, "WORKSPACE", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "list.json").write_text(content, encoding="utf-8")
    tools = MockTools()
    result = acquire(tools, config={"list_sources": {"https://example.test/": "config/list.json"}})
    assert result["status"] == "error" and not result["retryable"] and not tools.calls


@pytest.mark.parametrize("changes", [{"url": "file:///secret"}, {"pagination": {}},
    {"response": {"items_path": []}}, {"http": {"timeout": float("nan")}},
    {"http": {"retries": True}}, {"headers": []}, {"fields": {"title": ""}},
    {"pagination": {"path": "page", "max_pages": -1}}])
def test_config_schema_permanent(changes):
    cfg = {**list_config(), **changes}
    tools = MockTools()
    result = acquire(tools, options={"list_config": cfg})
    assert result["status"] == "error" and not result["retryable"] and not tools.calls


@pytest.mark.parametrize("raw", [None, [], {}, doc(status="unsupported"),
    doc(jobs={}), doc(images=["image.png"]), doc(coverage={"complete": "true"}), doc(links={})])
def test_schema_error_is_terminal(raw):
    tools = MockTools(raw)
    result = acquire(tools)
    assert result["status"] == "error" and not result["retryable"]
    assert result["error_kind"] == "invalid_schema"
    assert len(tools.calls) == 1
    assert result["acquisition"]["attempts"][0]["error_type"] == "ToolSchemaError"


@pytest.mark.parametrize("error,retryable,kind", [
    (TimeoutError("timed out"), True, "transport_error"),
    (httpx.ConnectError("offline"), True, "transport_error"),
    (NotImplementedError("unsupported"), False, "unsupported_tool"),
    (ValueError("bad options"), False, "invalid_configuration"),
])
def test_exception_classification_no_retry(error, retryable, kind):
    tools = MockTools(error)
    result = acquire(tools)
    assert result["retryable"] is retryable and result["error_kind"] == kind
    assert len(tools.calls) == 1


@pytest.mark.parametrize("error", [RuntimeError("token=private"), ValueError("password=private"),
                                     httpx.ConnectError("Authorization: Bearer private")])
def test_exception_secrets_never_persist(error):
    result = acquire(MockTools(error))
    serialized = json.dumps(result, ensure_ascii=False)
    assert "private" not in serialized
    assert "token=" not in serialized and "password=" not in serialized
    assert type(error).__name__ in result["error"]
    assert len(result["acquisition"]["attempts"]) == 1


@pytest.mark.parametrize("status,retryable,kind", [(503, True, "http_error"), (404, False, "deleted"),
                                                  (429, False, "blocked"), (400, False, "http_error")])
def test_http_exception_classification(status, retryable, kind):
    response = httpx.Response(status, request=httpx.Request("GET", "https://example.test/"))
    error = httpx.HTTPStatusError("HTTP failure", request=response.request, response=response)
    result = acquire(MockTools(error))
    assert result["http_status"] == status and result["retryable"] is retryable
    assert result["error_kind"] == kind


def test_unknown_explicit_adapter_permanent_no_fallback():
    tools = MockTools()
    result = acquire(tools, options={"adapter": "not-installed"})
    assert result["status"] == "error" and not result["retryable"]
    assert result["error_kind"] == "unsupported_adapter" and not tools.calls
    with pytest.raises(ValueError):
        AdapterRegistry().select("https://example.test/", {"adapter": "not-installed"})


def test_explicit_adapter_selection_and_scope():
    result = acquire(MockTools(doc()), options={"adapter": "genericweb"})
    assert result["acquisition"]["selection"] == "explicit"
    tools = MockTools()
    result = acquire(tools, options={"adapter": "WechatImage"})
    assert result["error_kind"] == "unsupported_adapter" and not tools.calls


def test_catalog_extension_and_replacement():
    class Custom(BaseAdapter):
        name = "Custom"
        priority = 300
        scope = "example.test"
        parameters = {"flag": {"default": False}}

        def matches(self, url, options):
            return url == "https://example.test/"

        async def acquire(self, context):
            return await context.call("web", context.url, context.options)

    registry = AdapterRegistry()
    registry.register(Custom)
    assert registry.select("https://example.test/").name == "Custom"
    catalog = registry.describe()
    json.dumps(catalog, allow_nan=False)
    assert {item["name"] for item in catalog} == {
        "Custom", "genericweb", "WechatImage", "WjxPublicForm", "Job51XYZPublicPortal", "Job51StaticPortal", "MokahrJobDetailPortal", "MokahrPublicPortal",
            "HotjobPublicPortal", "ZhiyeJobDetailPortal", "ZhiyePublicPortal", "FeishuPublicPortal",
        "ConfiguredPublicList",
    }
    catalog[0]["parameters"]["flag"]["default"] = True
    assert registry.describe()[0]["parameters"]["flag"]["default"] is False
    with pytest.raises(ValueError):
        registry.register(Custom())
    registry.register(Custom(), replace=True)
    result = asyncio.run(registry.acquire("https://example.test/", Path("artifacts"), tools=MockTools(doc())))
    assert result["acquisition"]["adapter"] == "Custom"


def test_default_tools_lazy_import(monkeypatch):
    import sys
    from types import ModuleType

    module = ModuleType("jobprep.app")
    tools = MockTools(doc())
    module.AppTools = lambda: tools
    monkeypatch.setitem(sys.modules, "jobprep.app", module)
    result = asyncio.run(AdapterRegistry().acquire("https://example.test/", "artifacts"))
    assert result["status"] == "ok" and len(tools.calls) == 1


@pytest.mark.parametrize("url", ["file:///tmp/a", "https://user:pass@example.test/", "https://example.test:bad/", "https://example.test/\n"])
def test_invalid_url_no_tools(url):
    tools = MockTools()
    result = acquire(tools, url)
    assert result["status"] == "error" and not result["retryable"] and not tools.calls
