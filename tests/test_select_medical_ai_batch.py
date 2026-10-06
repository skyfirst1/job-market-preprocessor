import csv

import pytest

from scripts.select_medical_ai_batch import audit_output, looks_mojibake


def test_audit_output_matches_canonical_source_row_and_urls(tmp_path):
    source = tmp_path / "raw.csv"
    output = tmp_path / "candidates.csv"
    source.write_text("公司名称,公告链接,投递链接\n九州通医药,https://notice.test/a,https://jobs.test/a\n",
                      encoding="utf-8-sig")
    output.write_text("company,announcement_url,application_url,acquisition_url,source_row\n"
                      "九州通医药,https://notice.test/a,https://jobs.test/a,https://jobs.test/a,2\n",
                      encoding="utf-8-sig")
    assert audit_output(source, output, expected_count=1) == {
        "companies": 1, "canonical_rows_checked": 1, "mojibake_names": 0,
    }


def test_audit_output_rejects_bad_derived_company(tmp_path):
    source = tmp_path / "raw.csv"
    output = tmp_path / "candidates.csv"
    source.write_text("公司名称,公告链接,投递链接\n九州通医药,,https://jobs.test/a\n",
                      encoding="utf-8-sig")
    output.write_text("company,announcement_url,application_url,acquisition_url,source_row\n"
                      "bad,,https://jobs.test/a,https://jobs.test/a,2\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="company mismatch"):
        audit_output(source, output)


def test_name_mojibake_detector():
    assert not looks_mojibake("先声药业集团")
    assert looks_mojibake("����")
