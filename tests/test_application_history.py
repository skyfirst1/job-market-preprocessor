from pathlib import Path

from jobprep.app.application_history import (
    applied_company_tokens,
    company_was_applied,
    read_application_history,
)


def test_history_distinguishes_applied_and_pending_companies(tmp_path: Path):
    path = tmp_path / "工作.md"
    path.write_text(
        "| company | 状态 | index |\n"
        "| --- | --- | --- |\n"
        "| 开立 | DL | https://example.test/a |\n"
        "| 贝壳 | 还没投 | https://example.test/b |\n"
        "| 农夫 | | https://example.test/c |\n",
        encoding="utf-8",
    )

    records = read_application_history(path)
    assert [record.company for record in records] == ["开立", "贝壳", "农夫"]
    tokens = applied_company_tokens(path)
    assert company_was_applied("开立医疗", tokens)
    assert company_was_applied("养生堂·农夫山泉·万泰生物", tokens)
    assert not company_was_applied("贝壳找房", tokens)


def test_missing_history_is_empty(tmp_path: Path):
    path = tmp_path / "missing.md"
    assert read_application_history(path) == []
    assert applied_company_tokens(path) == set()
    assert not company_was_applied("任何公司", set())
