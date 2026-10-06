import csv
import json

import pytest

from jobprep.exporter import export_review
from jobprep.store import Store, canonical_url, coarse_tags, worker_lock


def test_hash_routes_and_wechat_signatures_are_preserved():
    assert canonical_url('https://job.example/#/job/1') != canonical_url('https://job.example/#/job/2')
    assert 'sn=abc' in canonical_url('https://mp.weixin.qq.com/s?sn=abc&utm_source=test')
    assert 'utm_source' not in canonical_url('https://mp.weixin.qq.com/s?sn=abc&utm_source=test')
    with pytest.raises(ValueError):
        canonical_url('javascript:alert(1)')


def test_import_duplicate_urls_preserves_rows_and_claim_recovery(tmp_path):
    path = tmp_path / 'source.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['更新时间', '公司名称', '招聘岗位', '公告链接'])
        writer.writeheader()
        for company in ['one', 'two']:
            writer.writerow({'更新时间': '2026-10-05', '公司名称': company, '招聘岗位': 'AI Agent',
                             '公告链接': 'https://jobs.example/#/job/1'})
    store = Store(tmp_path / 'data')
    assert store.import_csv(path)['sources'] == 2
    assert store.import_csv(path)['tasks'] == 1
    task = store.claim(ai_only=True)
    assert len(store.source_references(task['id'])) == 2
    assert store.claim() is None
    assert store.recover_running() == 1
    assert store.claim()['id'] == task['id']


def test_pending_image_and_detail_make_export_incomplete(tmp_path):
    store = Store(tmp_path / 'data')
    task = store.add_task('https://jobs.example/')
    store.finish(task, 'pending_ocr', {'title': 'jobs', 'text': 'Agent development',
                                     'coverage': {'complete': True}, 'ocr_gaps': [{'image': 0}]})
    child = store.add_task('https://jobs.example/detail/1', parent_id=task)
    result = export_review(store, tmp_path / 'exports')
    packet = json.loads((tmp_path / 'exports/codex_review.jsonl').read_text().splitlines()[0])
    assert result['documents_exported'] == 1
    assert packet['coverage']['complete'] is False
    assert packet['coverage']['unresolved_details'] == 1
    assert packet['semantic_review']['suitable'] is None


def test_keyword_matching_does_not_match_embedded_ai():
    assert coarse_tags({'招聘岗位': 'maintenance'}) == []
    assert 'ai' in coarse_tags({'招聘岗位': 'AI Engineer'})


def test_worker_lock_blocks_parallel_worker(tmp_path):
    with worker_lock(tmp_path):
        with pytest.raises(RuntimeError):
            with worker_lock(tmp_path):
                pass
