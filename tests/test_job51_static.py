import asyncio
from copy import deepcopy

from jobprep.adapters import AdapterRegistry
from jobprep.adapters.job51_static import canonical_url, is_static_topic_url, parse_static_source


def doc(url, *, html="", jobs=None, status="ok", complete=True):
    return {"url": url, "final_url": url, "status": status, "title": "topic",
            "text": "", "raw_html": html, "jobs": jobs or [], "images": [], "links": [],
            "warnings": [], "coverage": {"complete": complete, "list_complete": complete}}


class Tools:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def web(self, url, artifact_dir, options=None):
        self.calls.append(("web", url, deepcopy(options)))
        return deepcopy(self.responses.pop(0))

    async def zhiye_list(self, url, artifact_dir, options=None):
        self.calls.append(("zhiye_list", url, deepcopy(options)))
        return deepcopy(self.responses.pop(0))


def acquire(tools, options=None):
    return asyncio.run(AdapterRegistry().acquire(
        "https://campus.51job.com/Acme/index.html", "artifacts", options, tools))


def test_canonical_url_resolves_relative_dedup_form_and_removes_tracking():
    base = "https://campus.51job.com/Acme/index.html"
    assert canonical_url("../Acme/job.html?utm_source=x&id=2#top", base) == (
        "https://campus.51job.com/Acme/job.html?id=2")
    assert canonical_url("javascript:alert(1)", base) is None


def test_static_topic_match_is_conservative():
    assert is_static_topic_url("https://campus.51job.com/Acme/index.html")
    assert is_static_topic_url("https://campus.51job.com/Acme/")
    assert not is_static_topic_url("https://campus.51job.com/")
    assert not is_static_topic_url("https://campus.51job.com/Acme/js/data.js")
    assert not is_static_topic_url("http://campus.51job.com/Acme/index.html")
    assert not is_static_topic_url("https://campus.51job.com.evil.test/Acme/index.html")


def test_parser_handles_html_anchors_scripts_and_json_record_variants():
    base = "https://campus.51job.com/Acme/index.html"
    source = '''
      <script src="js/data.js"></script><a href="jobs.html">下一页</a>
      <a href="https://tenant.zhiye.com/campus/detail?jobAdId=1&utm_source=x">视觉算法</a>
      {"value":"研发", "attr2":"大模型开发", "attr3":"硕士",
       "attr4":"上海", "attr5":"/apply/detail?jobAdId=2"}
    '''
    result = parse_static_source(source, base)
    assert result["scripts"] == ["https://campus.51job.com/Acme/js/data.js"]
    assert result["pages"] == ["https://campus.51job.com/Acme/jobs.html"]
    assert {job["title"] for job in result["jobs"]} == {"视觉算法", "大模型开发"}
    assert result["jobs"][0]["category"] == "研发"


def test_adapter_reads_relative_static_asset_dedupes_and_hands_off_once_per_zhiye_host():
    root = "https://campus.51job.com/Acme/index.html"
    script = "https://campus.51job.com/Acme/js/data.js"
    landing = doc(root, html='<script src="js/data.js"></script>')
    data = doc(script, html='''
      {"attr2":"视觉算法", "attr4":"上海", "attr5":"https://tenant.zhiye.com/campus/detail?jobAdId=1"}
      {"attr2":"视觉算法", "attr4":"上海", "attr5":"https://tenant.zhiye.com/campus/detail?jobAdId=1&utm_source=x"}
      {"title":"Agent开发", "location":"北京", "url":"https://tenant.zhiye.com/campus/detail?jobAdId=2"}
    ''')
    handed = doc("https://tenant.zhiye.com/campus/detail?jobAdId=1", jobs=[
        {"id": "3", "title": "大模型算法", "url": "https://tenant.zhiye.com/campus/detail?jobAdId=3"}
    ], complete=False)
    tools = Tools([landing, data, handed])
    result = acquire(tools)
    assert [call[0] for call in tools.calls] == ["web", "web", "zhiye_list"]
    assert result["acquisition"]["adapter"] == "Job51StaticPortal"
    assert result["coverage"]["pages_seen"] == 2
    assert result["coverage"]["total_reported"] == 2
    assert len(result["coverage"]["detail_urls"]) == 2
    assert result["coverage"]["list_complete"] is True
    assert result["coverage"]["stop_reason"] == "static_topic_complete"
    assert len(result["jobs"]) == 3


def test_adapter_stops_at_bounds_and_does_not_handoff_hidden_details():
    root = "https://campus.51job.com/Acme/index.html"
    landing = doc(root, html='''
      {"attr2":"A", "attr5":"https://one.zhiye.com/campus/detail?jobAdId=1"}
      {"attr2":"B", "attr5":"https://two.zhiye.com/campus/detail?jobAdId=2"}
    ''')
    tools = Tools([landing])
    result = acquire(tools, {"max_details": 1, "max_zhiye_handoffs": 0})
    assert [call[0] for call in tools.calls] == ["web"]
    assert result["coverage"]["details_discovered"] == 2
    assert result["coverage"]["detail_urls"] == [
        "https://one.zhiye.com/campus/detail?jobAdId=1"]
    assert result["coverage"]["list_complete"] is False
    assert result["coverage"]["stop_reason"] == "max_details"


def test_adapter_rejects_invalid_limits_before_network():
    tools = Tools([])
    result = acquire(tools, {"max_pages": 0})
    assert result["status"] == "error" and result["error_kind"] == "invalid_configuration"
    assert tools.calls == []
