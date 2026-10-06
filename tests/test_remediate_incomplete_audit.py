import json

from jobprep.crawler import _json_jobs, reparse_saved_json


def test_json_jobs_supports_recruit_name_responsibility_claim():
    jobs, total = _json_jobs({"data": {"list": [{
        "id": 32,
        "name": "AI算法工程师",
        "responsibility": "开发深度学习算法",
        "claim": "硕士及以上",
    }], "total": 1}}, "https://example.com/jobs")
    assert total == 1
    assert len(jobs) == 1
    assert jobs[0]["title"] == "AI算法工程师"
    assert "深度学习" in jobs[0]["text"]


def test_json_jobs_does_not_treat_generic_name_as_job():
    jobs, _ = _json_jobs({"data": {"id": 1, "name": "导航菜单"}}, "https://example.com")
    assert jobs == []


def test_json_jobs_does_not_treat_named_content_as_job():
    jobs, _ = _json_jobs({"data": {"id": 1, "name": "产品中心",
                                           "description": "产品导航"}}, "https://example.com")
    assert jobs == []


def test_reparse_saved_json_is_local_and_deduplicates(tmp_path):
    artifact = tmp_path / "capture.json"
    artifact.write_text(json.dumps({
        "url": "https://example.com/api/jobs",
        "body": {"data": {"list": [{"code": "P1", "name": "视觉算法工程师",
                                      "responsibility": "医学影像分割", "claim": "Python"}],
                          "total": 1}},
    }, ensure_ascii=False), encoding="utf-8")
    result = {"url": "https://example.com", "status": "partial", "jobs": [],
              "json_paths": [str(artifact), str(artifact)],
              "coverage": {"detail_urls": [], "total_reported": None}}
    reparsed = reparse_saved_json(result)
    assert len(reparsed["jobs"]) == 1
    assert reparsed["coverage"]["total_reported"] == 1
    assert reparsed["local_json_reparse"]["network_accessed"] is False
