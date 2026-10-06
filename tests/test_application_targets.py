import csv
import json
import sqlite3
from pathlib import Path

from jobprep.analysis.application_targets import build_rows, update_application_targets


def candidate(**overrides):
    row = {
        "company": "甲医疗", "enterprise_nature": "民企", "ownership_status": "非国企（CSV标注）",
        "industry": "医疗/医药/生物", "matched_roles": json.dumps([
            {"priority": 1, "category": "视觉相关AI/深度学习算法", "role": "医学图像算法工程师"},
            {"priority": 2, "category": "Agent/大模型开发", "role": "Agent开发工程师"},
        ], ensure_ascii=False),
        "best_priority": "1", "announcement_url": "https://example.com/news",
        "application_url": "https://example.com/jobs", "acquisition_url": "https://example.com/jobs",
        "source_pool": "web", "source_row": "42", "graduation_year": "2027届",
        "location": "上海", "deadline": "尽快投递",
    }
    row.update(overrides)
    return row


def test_csv_matched_roles_are_applicable_without_page_evidence():
    companies, jobs = build_rows([candidate()], {})
    assert len(companies) == 1
    assert companies[0]["target_status"] == "可投候选"
    assert companies[0]["evidence_level"] == "csv_declared"
    assert len(jobs) == 2
    assert {row["evidence_level"] for row in jobs} == {"csv_declared"}


def test_explicit_soe_is_excluded():
    companies, jobs = build_rows([candidate(enterprise_nature="央国企")], {})
    assert companies == []
    assert jobs == []


def test_blank_priority_company_is_retained_for_page_discovery():
    companies, jobs = build_rows([candidate(best_priority="", matched_roles="[]")], {})
    assert len(companies) == 1
    assert companies[0]["best_priority"] == ""
    assert companies[0]["target_status"] == "待页面发现目标岗位"
    assert jobs == []


def test_latest_nationwide_scope_keeps_other_city():
    companies, jobs = build_rows([candidate(location="深圳")], {})
    assert len(companies) == 1
    assert len(jobs) == 2


def test_limited_source_ai_role_is_unverified_and_company_copy_does_not_qualify():
    url = "https://mp.weixin.qq.com/s/example"
    row = candidate(best_priority="", matched_roles="[]", acquisition_url=url,
                    application_url=url)
    store = {url: {
        "url": url, "status": "partial", "updated_at": "2026-10-06T00:00:00+00:00",
        "error": "", "result": {
            "title": "校园招聘", "text": "公司以人工智能驱动创新",
            "ocr": [{"text": "校招岗位\nAI工程师\n负责业务系统研发\n销售实习生\n欢迎加入人工智能企业"}],
            "jobs": [], "coverage": {"stop_reason": "article_acquired_jd_coverage_unverified"},
        },
    }}
    companies, jobs = build_rows([row], store, {("甲医疗", "42"): "old audit"})
    assert companies[0]["evidence_level"] == "limited_source_ai_mention"
    assert [item["role_title"] for item in jobs] == ["AI工程师"]
    assert jobs[0]["details_verified"] == "false"
    assert jobs[0]["audit_evidence"] == ""
    assert "excerpt=" in jobs[0]["evidence"]


def test_structured_job_can_discover_role_after_blank_csv_signal():
    row = candidate(best_priority="", matched_roles="[]")
    store = {"https://example.com/jobs": {
        "status": "ok", "updated_at": "2026-10-06T00:00:00+00:00", "error": "",
        "result": {"title": "招聘", "text": "", "coverage": {}, "jobs": [{
            "title": "计算机视觉算法工程师", "url": "https://example.com/jobs/cv", "location": "北京"
        }]},
    }}
    companies, jobs = build_rows([row], store)
    assert companies[0]["best_priority"] == 1
    assert jobs[0]["role_title"] == "计算机视觉算法工程师"
    assert jobs[0]["details_verified"] == "true"


def test_large_model_algorithm_is_secondary_algorithm_not_deep_learning_priority():
    row = candidate(matched_roles=json.dumps([{
        "priority": 1, "category": "旧分类", "role": "大模型算法科学家（机器学习/深度学习）"
    }], ensure_ascii=False))
    store = {"https://example.com/jobs": {
        "status": "ok", "updated_at": "2026-10-06T00:00:00+00:00", "error": "",
        "result": {"title": "招聘", "text": "大模型算法科学家（机器学习/深度学习）", "coverage": {}, "jobs": []},
    }}
    _, jobs = build_rows([row], store)
    assert jobs[0]["priority"] == 3


def test_structured_title_removes_rendered_ui_suffix():
    row = candidate(best_priority="", matched_roles="[]")
    store = {"https://example.com/jobs": {
        "status": "ok", "updated_at": "2026-10-06T00:00:00+00:00", "error": "",
        "result": {"title": "招聘", "text": "", "coverage": {}, "jobs": [{
            "title": "AI Agent研发工程师 发布于 2026-10-01 算法类 | 上海市", "location": "上海"
        }]},
    }}
    _, jobs = build_rows([row], store)
    assert jobs[0]["role_title"] == "AI Agent研发工程师"
    assert jobs[0]["priority"] == 2


def test_structured_title_removes_employment_metadata_and_jd_text():
    row = candidate(matched_roles=json.dumps([{
        "priority": 1,
        "category": "视觉相关AI/深度学习算法",
        "role": "人工智能应用研究员（AIDD方向）",
    }], ensure_ascii=False))
    store = {"https://example.com/jobs": {
        "status": "ok", "updated_at": "2026-10-06T00:00:00+00:00", "error": "",
        "result": {"title": "招聘", "text": "", "coverage": {}, "jobs": [{
            "title": "人工智能应用研究员（AIDD方向） 全职 全职 | 上海 团队介绍：负责药物研发",
            "location": "上海",
        }]},
    }}
    _, jobs = build_rows([row], store)
    assert jobs[0]["role_title"] == "人工智能应用研究员（AIDD方向）"
    assert jobs[0]["structured_job_title"] == "人工智能应用研究员（AIDD方向）"


def test_company_wide_category_blob_is_not_emitted_as_one_jd():
    row = candidate(matched_roles=json.dumps([{
        "priority": 1, "category": "旧分类", "role": "研发类AI算法类业务支持类市场营销类行政职能类项目运营类"
    }], ensure_ascii=False))
    companies, jobs = build_rows([row], {})
    assert companies[0]["target_status"] == "待页面发现目标岗位"
    assert jobs == []


def test_pending_ocr_records_local_only_follow_up():
    store = {"https://example.com/jobs": {
        "status": "pending_ocr", "updated_at": "2026-10-06T00:00:00+00:00", "error": "",
        "result": {"title": "微信招聘", "text": "", "coverage": {}, "jobs": []},
    }}
    companies, _ = build_rows([candidate()], store)
    assert "无需重访微信" in companies[0]["uncertainty"]


def test_audit_b_confirmation_upgrades_csv_evidence():
    confirmations = {("甲医疗", "42"): "review_category=B; evidence=本地artifact足够"}
    companies, jobs = build_rows([candidate()], {}, confirmations)
    assert companies[0]["evidence_level"] == "audit_confirmed"
    assert companies[0]["verification_status"] == "独立审核已确认"
    assert {row["evidence_level"] for row in jobs} == {"audit_confirmed"}


def test_error_task_keeps_csv_declared_applicable_candidate():
    store = {"https://example.com/jobs": {
        "status": "error", "updated_at": "2026-10-06T00:00:00+00:00", "error": "browser_error",
        "result": {"title": "招聘", "text": "医学图像算法工程师", "coverage": {}, "jobs": [{
            "title": "医学图像算法工程师", "url": "https://example.com/jobs/1", "location": "上海"
        }]},
    }}
    companies, jobs = build_rows([candidate(company="优宁维")], store)
    assert companies[0]["target_status"] == "可投候选"
    assert companies[0]["fetch_status"] == "error"
    assert len(jobs) == 2


def test_structured_job_evidence_upgrades_only_matching_role():
    store = {"https://example.com/jobs": {
        "status": "ok", "updated_at": "2026-10-06T00:00:00+00:00", "error": "",
        "result": {"title": "招聘", "text": "", "coverage": {"stop_reason": "complete"}, "jobs": [{
            "title": "医学图像算法工程师", "url": "https://example.com/jobs/1",
            "description": "负责医学影像分割", "requirements": "深度学习", "location": "上海",
        }]},
    }}
    _, jobs = build_rows([candidate()], store)
    assert jobs[0]["details_verified"] == "true"
    assert jobs[0]["structured_job_url"].endswith("/1")
    assert len(jobs) == 2
    assert {row["evidence_level"] for row in jobs} == {"structured_job", "csv_declared"}


def test_update_waits_below_threshold_without_stage_marker(tmp_path: Path):
    source = tmp_path / "candidates.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(candidate()))
        writer.writeheader(); writer.writerow(candidate())
    summary = tmp_path / "summary.json"
    summary.write_text('{"stage_complete": false}', encoding="utf-8")
    result = update_application_targets(source, summary, tmp_path / "missing.sqlite3", tmp_path / "out")
    assert result["updated"] is False
    assert not (tmp_path / "out").exists()


def test_stage_marker_writes_deduplicated_outputs(tmp_path: Path):
    source = tmp_path / "candidates.csv"
    rows = [candidate(), candidate()]
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    summary = tmp_path / "summary.json"
    summary.write_text('{"stage_complete": true}', encoding="utf-8")
    database = tmp_path / "workstation.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE tasks(url TEXT,status TEXT,updated_at TEXT,error TEXT,result_json TEXT)")
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", (
            "https://example.com/jobs", "ok", "2026-10-06T00:00:00+00:00", "",
            json.dumps({"text": "医学图像算法工程师\nAgent开发工程师", "jobs": [], "coverage": {}}),
        ))
    result = update_application_targets(source, summary, database, tmp_path / "out")
    assert result["applicable_companies"] == 1
    assert result["pending_discovery_companies"] == 0
    assert result["error_companies"] == 0
    assert result["jd_rows"] == 2
    assert result["verified_companies"] == 1
    assert result["verified_jd_rows"] == 2
    assert len(list(csv.DictReader((tmp_path / "out" / "job_targets.csv").open(encoding="utf-8-sig")))) == 2
    assert len(list(csv.DictReader((tmp_path / "out" / "verified_companies.csv").open(encoding="utf-8-sig")))) == 1
    assert len(list(csv.DictReader((tmp_path / "out" / "verified_job_targets.csv").open(encoding="utf-8-sig")))) == 2


def test_update_writes_blank_role_company_only_to_pending_file(tmp_path: Path):
    row = candidate(best_priority="", matched_roles="[]")
    source = tmp_path / "candidates.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row)); writer.writeheader(); writer.writerow(row)
    summary = tmp_path / "summary.json"
    summary.write_text('{"stage_complete": true}', encoding="utf-8")
    result = update_application_targets(source, summary, tmp_path / "missing.sqlite3", tmp_path / "out")
    applicable = list(csv.DictReader((tmp_path / "out" / "applicable_companies.csv").open(encoding="utf-8-sig")))
    pending = list(csv.DictReader((tmp_path / "out" / "pending_discovery_companies.csv").open(encoding="utf-8-sig")))
    assert result["analyzed_companies"] == 1
    assert result["applicable_companies"] == 0
    assert result["pending_discovery_companies"] == 1
    assert applicable == []
    assert pending[0]["company"] == "甲医疗"


def test_update_writes_error_company_only_to_error_file(tmp_path: Path):
    row = candidate(company="优宁维")
    source = tmp_path / "candidates.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row)); writer.writeheader(); writer.writerow(row)
    batch_summary = tmp_path / "summary.json"
    batch_summary.write_text('{"stage_complete": true}', encoding="utf-8")
    database = tmp_path / "workstation.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE tasks(url TEXT,status TEXT,updated_at TEXT,error TEXT,result_json TEXT)")
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", (
            "https://example.com/jobs", "error", "2026-10-06T00:00:00+00:00", "browser_error",
            json.dumps({"text": "医学图像算法工程师", "jobs": [{"title": "医学图像算法工程师"}]}),
        ))
    result = update_application_targets(source, batch_summary, database, tmp_path / "out")
    assert result["applicable_companies"] == 1
    assert result["pending_discovery_companies"] == 0
    assert result["error_companies"] == 1
    errors = list(csv.DictReader((tmp_path / "out" / "error_companies.csv").open(encoding="utf-8-sig")))
    assert errors[0]["company"] == "优宁维"


def test_summary_notes_round_trip_as_strict_utf8_chinese(tmp_path: Path):
    row = candidate()
    source = tmp_path / "candidates.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row)); writer.writeheader(); writer.writerow(row)
    batch_summary = tmp_path / "batch_summary.json"
    batch_summary.write_text('{"stage_complete": true}', encoding="utf-8")
    output = tmp_path / "out"
    update_application_targets(source, batch_summary, tmp_path / "missing.sqlite3", output)

    decoded = (output / "summary.json").read_bytes().decode("utf-8", errors="strict")
    notes = json.loads(decoded)["notes"]
    assert notes == [
        "每个预筛 matched role 独立成一行；公司级岗位串不会作为一个聚合JD输出。",
        "明确国企会被排除；民企/外企判断沿用CSV标注，仍保留证据与不确定性。",
        "CSV招聘岗位字段明确命中三档目标岗位即可进入可投候选；页面证据用于核验和提升证据等级。",
        "partial 或 error 仅描述网页核验状态，不得否定 CSV 已明确声明的目标岗位。",
        "网页已核验子集仅包含 page_text、structured_job 或 audit_confirmed 证据。",
    ]
    assert not any(marker in decoded for marker in ("\ufffd", "ÿ", "锟斤拷"))
