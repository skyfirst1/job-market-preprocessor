from pathlib import Path

from jobprep.app.application_history import (
    applied_company_tokens,
    applied_url_keys,
    company_was_applied,
    read_application_history,
    url_was_applied,
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


def test_abbreviation_can_match_inside_full_name_and_parenthetical_alias(tmp_path: Path):
    path = tmp_path / "工作.md"
    path.write_text(
        "| company | 状态 |\n| --- | --- |\n| 发那科 | 已投 |\n| 蔚来（NIO） | 已投 |\n",
        encoding="utf-8",
    )

    tokens = applied_company_tokens(path)

    assert company_was_applied("上海发那科", tokens)
    assert company_was_applied("NIO", tokens)


def test_recruiting_tenant_url_matches_alias_without_sharing_entire_platform(tmp_path: Path):
    path = tmp_path / "工作.md"
    path.write_text(
        "| company | 状态 | index |\n"
        "| --- | --- | --- |\n"
        "| 简称 | 已投 | https://app.mokahr.com/campus_apply/example/123#/job/abc |\n",
        encoding="utf-8",
    )

    keys = applied_url_keys(path)

    assert url_was_applied("https://app.mokahr.com/campus-recruitment/example/456#/jobs", keys)
    assert not url_was_applied("https://app.mokahr.com/campus-recruitment/other/456#/jobs", keys)
