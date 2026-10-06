import csv
import json

from scripts import build_list_delivery as delivery


def test_delivery_preserves_source_rows_and_unicode_separators(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, 'ROOT', tmp_path)
    monkeypatch.setattr(delivery, 'SOURCES', [
        {'name': 'example_campus', 'company': 'Example', 'post_key': 'postId',
         'url_template': 'https://example.test/job?id={id}'},
        {'name': 'example_public', 'company': 'Example', 'post_key': 'postId'},
    ])
    for name in ('example_campus', 'example_public'):
        root = tmp_path / 'exports' / name
        root.mkdir(parents=True)
        record = {'id': 123, 'raw': {'postId': '123'}, 'fields': {},
                  'url': None, 'url_provenance': 'missing', 'title': 'First\u2028Second',
                  'location': ['A', 'B'], 'dept': None, 'category': None,
                  'description': None, 'requirements': None, 'needs_details': True,
                  'scope': 'source scope', 'raw_ref': 'audit.json', 'raw_index': 0}
        (root / 'jobs.jsonl').write_text(json.dumps(record, ensure_ascii=False) + '\n', encoding='utf-8')
        coverage = {'scope': 'source scope', 'list_complete': True, 'expected_total': 1,
                    'record_count': 1, 'pages_received': 1, 'jd_complete': False,
                    'needs_details_count': 1}
        (root / 'coverage.json').write_text(json.dumps(coverage), encoding='utf-8')
    summary = delivery.build()
    assert summary['source_record_count'] == 2
    assert summary['unique_scoped_post_ids'] == 1
    assert summary['cross_source_overlap_records'] == 1
    assert summary['campus_source_records'] == 1
    assert not summary['all_company_channels_complete']
    with (tmp_path / 'exports/full_lists/campus_lists.csv').open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]['title'] == 'First\u2028Second'
    assert rows[0]['url'] == 'https://example.test/job?id=123'
    assert rows[0]['url_provenance'].startswith('constructed')
