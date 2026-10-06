import asyncio
from pathlib import Path

from jobprep.adapters import AdapterRegistry
from jobprep.app.wjx import parse_form


URL = "https://v.wjx.cn/vm/example.aspx"


def test_registry_routes_only_wjx_host_family():
    registry = AdapterRegistry()
    assert registry.select(URL).name == "WjxPublicForm"
    assert registry.select("https://www.wjx.top/vm/example.aspx").name == "WjxPublicForm"
    assert registry.select("https://wjx.cn.evil.test/vm/example.aspx").name == "genericweb"


def test_parse_hidden_pages_and_detailed_roles(tmp_path):
    html = """
    <html><head><title>Example recruitment</title></head><body>
    <div id="divQuestion">
      <fieldset style="display:none"><div class="cutfield">岗位列表<div>算法工程师</div><div>产品经理</div></div></fieldset>
      <fieldset><div class="cutfield">岗位详情<div>算法工程师</div><p>岗位职责：训练模型</p><div>产品经理</div><p>任职要求：沟通能力</p></div></fieldset>
    </div></body></html>
    """
    result = parse_form(html, URL, str(tmp_path / "saved.html"))
    assert result["status"] == "ok"
    assert [job["title"] for job in result["jobs"]] == ["算法工程师", "产品经理"]
    assert result["coverage"]["list_complete"] is True
    assert result["coverage"]["jd_complete"] is True
    assert "训练模型" in result["jobs"][0]["text"]


def test_parse_role_selector_without_inventing_jd(tmp_path):
    html = """
    <html><head><title>Application</title></head><body><div id="divQuestion">
      <div class="field"><div class="topichtml">您的志愿岗位方向</div>
        <label>A、Java</label><label>B、算法研发</label></div>
      <div class="field"><div class="topichtml">您的学历</div><label>本科</label></div>
    </div></body></html>
    """
    result = parse_form(html, URL, str(tmp_path / "saved.html"))
    assert result["status"] == "partial"
    assert [job["title"] for job in result["jobs"]] == ["Java", "算法研发"]
    assert result["coverage"]["list_complete"] is True
    assert result["coverage"]["jd_complete"] is False
    assert result["coverage"]["stop_reason"] == "role_details_not_exposed"


def test_adapter_uses_wjx_tool_without_browser_or_ocr(tmp_path):
    class Tools:
        def __init__(self):
            self.calls = []

        async def wjx_form(self, url, artifact_dir, options=None):
            self.calls.append((url, dict(options or {})))
            return {
                "url": url, "final_url": url, "status": "partial", "title": "form",
                "text": "role", "jobs": [], "images": [], "links": [], "warnings": [],
                "coverage": {"complete": False, "list_complete": False,
                             "stop_reason": "roles_not_found"},
            }

    tools = Tools()
    result = asyncio.run(AdapterRegistry().acquire(
        URL, tmp_path, {"browser": True, "skip_ocr": False, "timeout": 12}, tools
    ))
    assert result["acquisition"]["adapter"] == "WjxPublicForm"
    assert tools.calls == [(URL, {"timeout": 12})]
    assert result["acquisition"]["provenance"][-1]["browser_used"] is False
    assert result["acquisition"]["provenance"][-1]["ocr_used"] is False
