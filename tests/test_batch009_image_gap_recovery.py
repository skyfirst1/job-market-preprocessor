import json
from pathlib import Path

from scripts import recover_batch009_image_gaps as module


def test_manifest_url_rejects_missing_and_svg():
    for entry in ({"status": "unsupported_reference"}, {
        "public_image_url": "https://mmbiz.qpic.cn/mmbiz_svg/a/640?wx_fmt=svg"}):
        try:
            module._manifest_url(entry)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe manifest entry accepted")


def test_request_ledger_allows_each_url_once(tmp_path):
    ledger = module.RequestLedger(tmp_path / "gate.sqlite3", max_requests=1)
    url = "https://mmbiz.qpic.cn/mmbiz_png/a/640?wx_fmt=png"
    permit, previous = ledger.reserve(url)
    assert previous is None and permit.request() == 1
    ledger.finish(url, "failed", {"reason": "test"})
    permit, previous = ledger.reserve(url)
    assert permit is None and previous["state"] == "failed"
    try:
        ledger.reserve("https://mmbiz.qpic.cn/mmbiz_png/b/640?wx_fmt=png")
    except ValueError as exc:
        assert str(exc) == "batch009_image_request_budget_exhausted"
    else:
        raise AssertionError("request ceiling was not enforced")


def test_merge_preserves_successful_ocr_and_keeps_true_gap(tmp_path):
    old = {"status": "pending_ocr", "acquisition_status": "partial",
           "images": [{"status": "ok", "path": "old.png", "sha256": "old"},
                      {"status": "image_limit", "path": ""},
                      {"status": "image_limit", "path": ""}],
           "ocr": [{"image_index": 0, "image_sha256": "old", "status": "ok", "text": "old evidence"}],
           "ocr_gaps": [{"image": 1, "reason": "image_count_limit"},
                        {"image": 2, "reason": "image_count_limit"}],
           "evidence": [], "warnings": [], "coverage": {"complete": False}}
    new_path = tmp_path / "new.png"
    new_path.write_bytes(b"fixture")
    merged = module._merge_result(old, {1: {
        "image": {"status": "ok", "path": str(new_path), "sha256": "new", "order": 1},
        "ocr": {"image_index": 1, "image_sha256": "new", "status": "ok", "text": "new evidence"},
    }}, {1: "old", 2: "manifest_has_no_public_image_url"})
    assert merged["ocr"][0]["text"] == "old evidence"
    assert {item["image_index"] for item in merged["ocr"]} == {0, 1}
    assert merged["ocr_gaps"] == [{"image": 2, "reason": "manifest_has_no_public_image_url"}]
    assert merged["status"] == "pending_ocr"
    json.dumps(merged)
