import json

from scripts.report_agent_status import update


def test_agent_pages_are_independent_and_index_is_stable(tmp_path):
    first = update("subagent1", {"status": "running", "processed": 3}, tmp_path)
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    update("subagent2", {"status": "waiting", "processed": 8}, tmp_path)
    assert (tmp_path / "index.html").read_text(encoding="utf-8") == index
    assert json.loads((tmp_path / "subagent1.json").read_text(encoding="utf-8"))["processed"] == 3
    assert json.loads((tmp_path / "subagent2.json").read_text(encoding="utf-8"))["processed"] == 8
    assert 'http-equiv="refresh" content="10"' in (tmp_path / "subagent1.html").read_text(encoding="utf-8")


def test_omitted_fields_are_preserved(tmp_path):
    update("subagent1", {"phase": "crawl", "failed": 2}, tmp_path)
    current = update("subagent1", {"status": "running"}, tmp_path)
    assert current["phase"] == "crawl"
    assert current["failed"] == 2


def test_partial_recovery_fields_render(tmp_path):
    update("subagent1", {"total_candidates": 176, "processed_companies": 20,
                         "remaining_companies": 156, "partial_by_reason": {"spa_shell": 9},
                         "recovered_partial": 3, "remaining_partial": 6}, tmp_path)
    html = (tmp_path / "subagent1.html").read_text(encoding="utf-8")
    assert "候选全集" in html and "176" in html
    assert "spa_shell" in html
    assert "恢复 partial" in html and "剩余 partial" in html
