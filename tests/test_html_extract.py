import json

from jobprep.html_extract import extract_html


def test_nested_wechat_article_and_ordered_images():
    html = '''<title>fallback</title><h1 id="activity-name">招聘公告</h1>
    <img src="/logo.png"><div id="js_content" style="visibility:hidden">
    <section><p>第一段<strong>嵌套文字</strong></p><img data-src="//cdn.example/a.png" src="/placeholder.gif">
    <section>第二段<img srcset="/small.png 320w, /large.png 1200w"></section>
    <img src="/last.jpg"></section></div><script>var captcha='verify';</script>'''
    result = extract_html(html, "https://mp.example/article")
    assert result["title"] == "招聘公告"
    assert all(text in result["text"] for text in ("第一段", "嵌套文字", "第二段"))
    assert [image["url"] for image in result["images"]] == ["https://cdn.example/a.png", "https://mp.example/large.png", "https://mp.example/last.jpg"]
    assert [item["kind"] for item in result["evidence"]] == ["text", "text", "image", "text", "image", "image"]
    assert result["status"] == "partial"
    assert not result["coverage"]["complete"]
    assert all(not image["path"] for image in result["images"])
    json.dumps(result)


def test_challenge_in_script_is_not_a_page_challenge():
    html = '<title>Jobs</title><div id="js_content">Actual recruitment description</div><script>var text="环境异常 captcha verify you are human 该内容已被发布者删除";</script>'
    result = extract_html(html, "https://example.com/article")
    assert result["status"] == "ok"
    assert "captcha" not in result["text"]


def test_visible_challenge_and_deleted_page():
    assert extract_html('<div class="weui-msg__title">环境异常</div><p>请进行验证</p>', "https://example.com")["status"] == "blocked"
    assert extract_html('<p>该内容已被发布者删除</p>', "https://example.com")["status"] == "deleted"
    assert extract_html('<title>Just a moment...</title><form id="challenge-form">Verify you are human</form>', "https://example.com")["status"] == "blocked"


def test_article_discussing_challenges_is_not_blocked():
    result = extract_html('<title>安全验证研究招聘</title><article>' + '研究方向安全验证算法。' * 70 + '</article>', "https://example.com")
    assert result["status"] == "ok"


def test_hash_job_links_and_disabled_next():
    result = extract_html('''<main><ul><li><a href="#/job/detail/42">视觉算法岗位</a><p>上海 硕士</p></li></ul>
    <a href="javascript:void(0)">invalid</a><a href="mailto:test@example.com">mail</a>
    <button aria-disabled="true">下一页</button><p>共 1 个职位</p></main>''', "https://jobs.example/#/list")
    assert result["jobs"][0]["url"] == "https://jobs.example/#/job/detail/42"
    assert "上海" in result["jobs"][0]["text"]
    assert result["terminal"]
    assert result["total_reported"] == 1
    assert not result["coverage"]["complete"]
    assert result["coverage"]["detail_urls"] == ["https://jobs.example/#/job/detail/42"]


def test_enabled_next_overrides_terminal_hint():
    result = extract_html('<a rel="next" href="?page=2">Next</a><p>没有更多</p>', "https://jobs.example/")
    assert result["has_next"] and not result["terminal"]
    assert result["links"][0]["kind"] == "pagination"


def test_manual_import_contract():
    result = extract_html(html_text='<article>Complete saved announcement</article>', url="https://example.com/#/article")
    required = {"url", "final_url", "title", "text", "status", "method", "html_path", "images", "links", "jobs", "coverage", "warnings"}
    assert required <= result.keys()
    assert result["coverage"]["complete"]
    assert result["final_url"].endswith("#/article")
    assert result["html_path"] == ""


def test_http_status_and_empty_shell():
    assert extract_html("", "https://example.com", 403)["status"] == "blocked"
    assert extract_html("", "https://example.com", 410)["status"] == "deleted"
    result = extract_html('<div id="app"></div><script src="app.js"></script>', "https://example.com")
    assert result["requires_browser"] and result["status"] == "partial"


def test_zhiye_closing_tag_shell_requires_browser():
    html = """<html><head><script src='/assets/app.js'></script></head><body>
    --> <script type='text/javascript'> /*project config start*/ /*project config end*/ </script>
    </body></html>"""
    result = extract_html(html, "https://example.zhiye.com/campus/jobs")
    assert result["requires_browser"]
    assert result["status"] == "partial"
    assert result["coverage"]["stop_reason"] == "unknown_pagination"


def test_career_navigation_is_not_detail_discovery():
    result = extract_html('''<nav><ul><li><a href="/careers">Careers</a></li>
    <li><a href="/jobs/list">Jobs</a></li></ul></nav><main><ul>
    <li><a href="/jobs/search">Search jobs</a></li>
    <li><a href="/jobs/software-engineer-42">Software Engineer</a></li></ul>
    <button disabled>Next</button></main><footer><a href="/recruit">加入我们</a></footer>''', "https://jobs.example/")
    assert len(result["links"]) == 5
    assert len(result["jobs"]) == 1
    assert result["coverage"]["detail_urls"] == ["https://jobs.example/jobs/software-engineer-42"]
    assert result["coverage"]["list_complete"]
    assert not result["coverage"]["complete"]


def test_empty_login_and_challenge_form_are_blocked():
    assert extract_html('<form id="challenge-form"></form>', "https://example.com")["status"] == "blocked"
    assert extract_html('<form><input type="password"></form>', "https://example.com")["status"] == "blocked"
def test_framework_comments_are_not_job_text():
    from jobprep.html_extract import extract_html

    result = extract_html('<main><!--v-if--><!--login button-->Agent engineer</main>',
                          'https://example.com')
    assert result['text'] == 'Agent engineer'

