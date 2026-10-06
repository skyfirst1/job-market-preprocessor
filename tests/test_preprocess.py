from copy import deepcopy

from jobprep.preprocess import preprocess_document


def test_chinese_html_navigation_and_scripts():
    result = {"html": """<html><body><nav>首页 校园招聘</nav><script>window.jobs=[];</script>
    <!-- secret comment --><main><h1>华为招聘算法工程师</h1><p>岗位职责：研发机器学习模型，优化训练效率。</p>
    <p>任职要求：计算机相关专业，熟悉Python。</p></main><footer>版权所有</footer></body></html>"""}
    view = preprocess_document(result)
    assert "华为招聘算法工程师" in view["compact_text"]
    assert "任职要求" in view["compact_text"]
    for noise in ("首页", "window.jobs", "secret comment", "版权所有"):
        assert noise not in view["compact_text"]
    assert view["quality"]["boilerplate_removed"]
    assert view["signals"]["ai"]


def test_job_html_enrichment_preserves_input_and_no_invented_url():
    result = {"jobs": [{"id": "original", "title": "算法工程师", "url": "", "raw": {
        "jobId": 103891, "mainBusiness": "<p>研发训练平台</p><script>noise()</script>",
        "jobRequire": "<ul><li>硕士学历</li><li>熟悉Python</li></ul>", "workPlace": "深圳"}}]}
    before = deepcopy(result)
    view = preprocess_document(result)
    job = view["jobs"][0]
    assert result == before
    assert job["description"] == "研发训练平台"
    assert "硕士学历" in job["requirements"]
    assert job["location"] == "深圳"
    assert job["id"] == "original" and job["source_ids"] == ["original"]
    assert job["raw_job_id"] == 103891 and job["url"] == ""
    assert "raw" not in job
    assert job["field_sources"]["description"] == ["jobs[0].raw.mainBusiness"]
    assert job["evidence_sources"][0]["kind"] == "json"


def test_duplicates_keep_all_original_ids_and_sources():
    jobs = [{"id": name, "title": "研发工程师", "raw": {"jobRequire": "熟悉Python"}} for name in ("a", "a")]
    view = preprocess_document({"jobs": jobs, "text": ("招聘研发工程师，负责训练与推理平台建设，欢迎应届生。\n" * 2)})
    assert len(view["jobs"]) == 1
    assert view["jobs"][0]["source_ids"] == ["a"]
    assert len(view["jobs"][0]["evidence_sources"]) == 2
    assert view["quality"]["dedup_count"] == 2
    assert len(view["provenance"]["dedup_events"]) == 2


def test_distinct_upstream_ids_or_requirements_do_not_merge():
    view = preprocess_document({"jobs": [
        {"id": "a", "title": "工程师", "raw": {"jobId": 1}},
        {"id": "b", "title": "工程师", "raw": {"jobId": 2}},
        {"id": "c", "title": "工程师", "requirements": "博士"}]})
    assert len(view["jobs"]) == 3


def test_sparse_known_jobs_and_placeholder_are_not_absence():
    view = preprocess_document({"jobs": [{"id": "known", "title": "AI Infra工程师", "raw": {
        "mainBusiness": "请您详见岗位意向中的岗位职责", "jobRequire": "请您详见岗位意向中的岗位要求"}}],
        "coverage": {"complete": False, "total_reported": 70}, "status": "pending_ocr"})
    assert len(view["jobs"]) == 1
    assert "AI Infra工程师" in view["compact_text"]
    assert view["quality"]["body_insufficient"]
    assert view["quality"]["needs_confirmation"]
    assert not view["quality"]["no_jobs_proven"]
    assert view["jobs"][0]["needs_confirmation"]
    assert view["quality"]["needs_details"]


def test_poster_ocr_text_and_lines_are_both_preserved():
    view = preprocess_document({"ocr": [{"text": "秋季校园招聘", "image_index": 4,
        "image_sha256": "image-hash", "lines": [{"text": "秋季校园招聘"}, {"text": "招聘算法工程师"}, "要求硕士学历"]}]})
    assert view["compact_text"].count("秋季校园招聘") == 1
    assert "招聘算法工程师" in view["compact_text"]
    assert "要求硕士学历" in view["compact_text"]
    source = view["provenance"]["evidence_sources"][0]
    assert source["kind"] == "ocr" and source["line_indices"] == [0, 1, 2]
    assert source["image_sha256"] == "image-hash"


def test_export_packet_evidence_and_companies_survive():
    view = preprocess_document({"evidence": [{"id": "task:ocr:0", "kind": "ocr", "text": "人工智能岗位招聘"}],
        "source_references": [{"source_id": "company", "raw": {"公司名称": "非技术公司", "行业分类": "制造"}}]})
    assert "人工智能岗位招聘" in view["compact_text"]
    assert view["provenance"]["evidence_sources"][0]["id"] == "task:ocr:0"
    assert view["provenance"]["source_references"][0]["company"] == "非技术公司"
    assert view["signals"]["keyword_matches_are_not_exclusions"]


def test_no_audit_path_read_or_network_and_empty_is_unverified():
    view = preprocess_document({"raw_html_path": "DO_NOT_OPEN.env"})
    assert view["compact_text"] == "" and view["jobs"] == []
    assert view["quality"]["needs_confirmation"]
    assert not view["quality"]["semantic_verified"]
    assert not view["quality"]["no_jobs_proven"]


def test_readability_extracts_unframed_article():
    paragraph = "本次招聘软件研发工程师，负责平台研发与数据分析。要求具备团队协作能力，熟悉Python与软件工程。"
    view = preprocess_document({"html": "<html><body><div>" + "".join(f"<p>{paragraph}</p>" for _ in range(5)) + "</div></body></html>"})
    assert paragraph in view["compact_text"]
    assert "article_extraction_fallback" not in view["quality"]["confirmation_reasons"]


def test_full_ocr_body_not_reported_as_sparse():
    view = preprocess_document({"ocr": [{"lines": [{"text": "招聘研发岗位，负责软件设计开发与维护，任职要求包括沟通能力与计算机基础。" * 4}]}],
                                "coverage": {"complete": True}})
    assert not view["quality"]["body_insufficient"]
    assert not view["quality"]["needs_confirmation"]
    assert not view["quality"]["semantic_verified"]


def test_no_identity_or_different_ids_companies_cities_preserve_jobs():
    jobs = [{"title": "研发工程师"}, {"title": "研发工程师"},
            {"id": "a", "title": "研发工程师", "company": "甲", "location": "深圳"},
            {"id": "b", "title": "研发工程师", "company": "甲", "location": "深圳"},
            {"id": "a", "title": "研发工程师", "company": "乙", "location": "北京"}]
    assert len(preprocess_document({"jobs": jobs})["jobs"]) == 5


def test_same_observed_url_and_identical_fields_can_merge():
    jobs = [{"title": "工程师", "url": "https://example.org/jobs/1"}] * 2
    assert len(preprocess_document({"jobs": jobs})["jobs"]) == 1


def test_shared_company_references_are_not_job_employers():
    view = preprocess_document({"jobs": [{"id": "a", "title": "工程师"}],
        "source_references": [{"source_id": "csv1", "raw": {"公司名称": "甲"}},
                              {"source_id": "csv2", "raw": {"公司名称": "乙"}}]})
    assert view["jobs"][0]["company"] == ""
    assert len(view["provenance"]["source_references"]) == 2


def test_cross_evidence_and_job_requirements_remain_independent():
    shared = "学历要求本科及以上，工作地点深圳，熟悉软件研发流程。"
    view = preprocess_document({"evidence": [{"id": "one", "text": shared}, {"id": "two", "text": shared}],
        "jobs": [{"id": "a", "title": "工程师", "description": shared, "requirements": "本科"},
                 {"id": "b", "title": "工程师", "description": shared, "requirements": "本科"}]})
    assert view["quality"]["dedup_count"] == 0
    assert view["compact_text"].count(shared) == 4
    assert all(job["requirements"] == "本科" for job in view["jobs"])


def test_original_id_is_identity_when_url_and_raw_job_id_missing():
    view = preprocess_document({"jobs": [{"id": "recruitment-1", "title": "工程师", "url": "", "raw": {}},
                                        {"id": "recruitment-2", "title": "工程师", "url": "", "raw": {}}]})
    assert [job["source_ids"] for job in view["jobs"]] == [["recruitment-1"], ["recruitment-2"]]
    assert view["quality"]["job_dedup_count"] == 0


def test_integrator_html_supersedes_plain_navigation_preserves_jobs_ocr():
    paragraph = "招聘软件研发工程师，负责平台研发与数据分析。要求具备团队协作能力，熟悉Python与软件工程。" * 3
    view = preprocess_document({"html_path": "audit.html",
        "html_text": f"<html><body><nav>首页 关于我们</nav><div><p>{paragraph}</p></div></body></html>",
        "text": "首页 关于我们\n" + paragraph,
        "jobs": [{"id": "a", "title": "已知岗位"}], "ocr": [{"text": "海报独有岗位"}]})
    assert "首页" not in view["compact_text"]
    assert paragraph in view["compact_text"]
    assert "海报独有岗位" in view["compact_text"] and len(view["jobs"]) == 1
    assert view["provenance"]["source_text_superseded_by_html"]
    assert not view["provenance"]["source_text_retained_due_fallback"]


def test_sparse_html_retains_original_text_and_flags_fallback():
    view = preprocess_document({"html_text": "<main>招聘</main>", "text": "原始正文里的岗位要求",
                                "ocr": [{"lines": ["海报详情"]}], "jobs": [{"id": "a", "title": "工程师"}]})
    assert "原始正文里的岗位要求" in view["compact_text"]
    assert "海报详情" in view["compact_text"]
    assert view["provenance"]["source_text_retained_due_fallback"]
    assert "source_text_retained_due_fallback" in view["quality"]["confirmation_reasons"]
    assert len(view["jobs"]) == 1


def test_review_packet_reliable_html_skips_html_evidence_text_only():
    paragraph = "招聘软件研发工程师，负责平台研发与数据分析。要求具备团队协作能力，熟悉Python与软件工程。" * 3
    packet = {"task_id": "task", "raw_html_path": "audit.html",
              "html_text": f"<html><body><nav>首页 关于我们</nav><div><p>{paragraph}</p></div></body></html>",
              "evidence": [{"id": "task:html:0", "kind": "html", "text": "首页 关于我们\n" + paragraph,
                            "source_url": "https://example.org/jobs"},
                           {"id": "task:ocr:0", "kind": "ocr", "text": "海报独有岗位"},
                           {"id": "task:job:0", "kind": "job", "text": "岗位独有要求"}]}
    before = deepcopy(packet)
    view = preprocess_document(packet)
    assert "首页" not in view["compact_text"] and "关于我们" not in view["compact_text"]
    assert view["compact_text"].count(paragraph) == 1
    assert "海报独有岗位" in view["compact_text"] and "岗位独有要求" in view["compact_text"]
    refs = {ref.get("id"): ref for ref in view["provenance"]["evidence_sources"]}
    assert refs["task:html:0"]["skip_reason"] == "superseded_by_reliable_html"
    assert refs["task:html:0"]["superseded_by"] == "html_text"
    assert refs["task:html:0"]["source_url"] == "https://example.org/jobs"
    assert "skip_reason" not in refs["task:ocr:0"] and "skip_reason" not in refs["task:job:0"]
    assert packet == before


def test_review_packet_sparse_or_missing_html_retains_all_html_evidence():
    for html_fields in ({"html_text": "<main>招聘</main>"}, {}):
        packet = {**html_fields, "evidence": [
            {"id": "task:html:0", "kind": "html", "text": "首页 原HTML里的岗位职责"},
            {"id": "task:html:6000", "kind": "html", "text": "下一页 原HTML里的岗位要求"},
            {"id": "task:ocr:0", "kind": "ocr", "text": "海报独有岗位"}]}
        view = preprocess_document(packet)
        assert "首页 原HTML里的岗位职责" in view["compact_text"]
        assert "下一页 原HTML里的岗位要求" in view["compact_text"]
        assert "海报独有岗位" in view["compact_text"]
        refs = view["provenance"]["evidence_sources"]
        assert not any("skip_reason" in ref for ref in refs)
        assert {"task:html:0", "task:html:6000", "task:ocr:0"} <= {ref.get("id") for ref in refs}
        if html_fields:
            assert view["provenance"]["source_text_retained_due_fallback"]
            assert "source_text_retained_due_fallback" in view["quality"]["confirmation_reasons"]
