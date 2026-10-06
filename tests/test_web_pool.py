import json

from scripts.run_web_pool import valid_result


def test_valid_result_requires_matching_successful_result_json():
    task = {"url": "https://example.com/jobs", "status": "partial",
            "result_json": json.dumps({"url": "https://example.com/jobs", "status": "partial"})}
    assert valid_result(task)
    task["result_json"] = json.dumps({"url": "https://other.example/jobs", "status": "partial"})
    assert not valid_result(task)


def test_error_or_invalid_json_is_not_skipped():
    assert not valid_result({"url": "https://example.com", "status": "error", "result_json": "{}"})
    assert not valid_result({"url": "https://example.com", "status": "ok", "result_json": "broken"})
