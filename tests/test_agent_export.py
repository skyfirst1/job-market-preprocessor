import csv
import json

import pytest

from jobprep import exporter
from jobprep.store import Store


URL = 'https://careers.example/jobs'


def build_store(tmp_path, result, status='partial'):
    source = tmp_path / 'input.csv'
    fields = ['更新时间', '公司名称', '招聘岗位', '届次', '学历要求', '工作地点', '截止时间', '公告链接', '投递链接']
    with source.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for company in ['CSV Company A', 'CSV Company B']:
            writer.writerow({'更新时间': '2026-10-05', '公司名称': company,
                             '招聘岗位': 'FULL_CSV_ROLE_LIST_DO_NOT_REPEAT ' * 500,
                             '届次': '2027届', '学历要求': '硕士', '工作地点': '上海',
                             '截止时间': '尽快投递', '公告链接': URL, '投递链接': URL})
    store = Store(tmp_path / 'data')
    store.import_csv(source)
    task_id = store.add_task(URL)
    store.finish(task_id, status, result)
    return store, task_id


def read_exports(directory):
    def read(name):
        return [json.loads(line) for line in (directory / name).read_text(encoding='utf-8').splitlines()]
    return read('codex_review.jsonl'), read('agent_tasks.jsonl')


def job(identifier='original-a'):
    return {'id': identifier, 'title': 'Agent Engineer', 'url': 'https://careers.example/job/42', 'text': '',
            'raw': {'jobId': 42, 'jobDesc': '<p>Build Agent workflows.</p>', 'jobRequire': 'Python and testing.',
                    'workPlace': '上海', 'provider_blob': 'AUDIT_ONLY_LARGE_RAW ' * 1000}}


def test_normalized_jobs_deduplicate_keep_raw_audit_and_label_csv_claims(tmp_path):
    result = {'title': 'Careers', 'text': 'Original HTML evidence', 'jobs': [job(), job('original-b')],
              'ocr': [{'text': 'Original OCR evidence', 'image_index': 0, 'image_sha256': 'hash', 'model': 'general'}],
              'images': [], 'coverage': {'complete': False, 'list_complete': True, 'detail_urls': []}}
    store, task_id = build_store(tmp_path, result)
    output = tmp_path / 'exports'
    summary = exporter.export_review(store, output)
    reviews, tasks = read_exports(output)
    assert summary['agent_tasks_exported'] == 1
    assert len(tasks) == 1 and len(reviews[0]['jobs']) == 2
    assert reviews[0]['jobs'][0]['raw']['provider_blob'] == result['jobs'][0]['raw']['provider_blob']
    task = tasks[0]
    assert task['job']['description'] == 'Build Agent workflows.'
    assert task['job']['requirements'] == 'Python and testing.'
    assert task['job']['location'] == '上海'
    assert task['source_companies'] == ['CSV Company A', 'CSV Company B']
    assert len(task['source_rows']) == 2
    assert task['source_rows'] == [2, 3]
    assert len(set(task['source_row_ids'])) == 2
    assertions = task['source_assertions']
    assert assertions['origin'] == 'csv' and assertions['verified'] is False
    assert len(assertions['degree']) == 1
    assert assertions['degree'][0]['source_row_indices'] == [0, 1]
    assert assertions['deadline'][0]['value'] == '尽快投递'
    assert 'employer' not in task and not task['job']['company']
    compact = json.dumps(task)
    assert 'AUDIT_ONLY_LARGE_RAW' not in compact
    assert 'FULL_CSV_ROLE_LIST_DO_NOT_REPEAT' not in compact
    assert 'raw' not in task['job']
    assert task['coverage']['complete'] is False
    assert task['semantic_review']['suitable'] is None
    ids = {chunk['id'] for chunk in reviews[0]['evidence']}
    assert f'{task_id}:html:0' in ids
    assert f'{task_id}:ocr:0' in ids
    assert set(task['evidence_ids']) <= ids
    assert any(':job:' in identifier for identifier in task['evidence_ids'])
    assert task['full_review_ref']['task_id'] == task_id
    exporter.export_review(store, output)
    assert read_exports(output)[1][0]['evidence_ids'] == task['evidence_ids']


def test_long_job_text_truncation_has_complete_recoverable_evidence(tmp_path):
    description = 'Description sentence.\n' * 500 + 'FINAL_DESCRIPTION_PARAGRAPH'
    requirements = 'Requirements sentence.\n' * 400 + 'FINAL_REQUIREMENTS_PARAGRAPH'
    store, _ = build_store(tmp_path, {'jobs': [{'id': 'long', 'title': 'Research role',
        'description': description, 'requirements': requirements, 'text': 'Additional notes.'}],
        'coverage': {'complete': False}})
    output = tmp_path / 'exports'
    exporter.export_review(store, output)
    reviews, tasks = read_exports(output)
    task = tasks[0]
    assert task['truncated'] is True
    assert task['included_text_chars'] <= exporter.TEXT_LIMIT
    assert 'requirements' in task['truncated_fields']
    assert task['job']['requirements']
    assert task['audit_ref'] == task['full_review_ref']
    evidence = {chunk['id']: chunk for chunk in reviews[0]['evidence']}
    job_chunks = [evidence[identifier] for identifier in task['evidence_ids']]
    restored_description = ''.join(chunk['text'] for chunk in job_chunks if chunk['field'] == 'description')
    restored_requirements = ''.join(chunk['text'] for chunk in job_chunks if chunk['field'] == 'requirements')
    assert 'FINAL_DESCRIPTION_PARAGRAPH' in restored_description
    assert 'FINAL_REQUIREMENTS_PARAGRAPH' in restored_requirements


def test_empty_jobs_export_document_summary_without_proving_no_jobs(tmp_path):
    store, task_id = build_store(tmp_path, {'title': 'Blocked document', 'text': '环境异常，请进行验证',
        'status': 'blocked', 'jobs': [], 'coverage': {'complete': False, 'list_complete': False,
        'stop_reason': 'blocked'}}, status='blocked')
    output = tmp_path / 'exports'
    exporter.export_review(store, output)
    _, tasks = read_exports(output)
    assert len(tasks) == 1 and tasks[0]['kind'] == 'document'
    assert '环境异常' in tasks[0]['compact_text']
    assert tasks[0]['coverage']['complete'] is False
    assert tasks[0]['quality']['no_jobs_proven'] is False
    assert f'{task_id}:html:0' in tasks[0]['evidence_ids']


def test_unqueued_details_and_image_gaps_cannot_be_complete(tmp_path):
    store, _ = build_store(tmp_path, {'text': 'List evidence', 'jobs': [],
        'images': [{'status': 'skipped', 'path': ''}], 'coverage': {'complete': True,
        'list_complete': True, 'detail_urls': ['https://careers.example/job/9']},
        'ocr_gaps': [{'reason': 'Image limit reached'}]}, status='ok')
    output = tmp_path / 'exports'
    exporter.export_review(store, output)
    reviews, tasks = read_exports(output)
    assert reviews[0]['coverage']['complete'] is False
    assert tasks[0]['coverage']['complete'] is False
    assert tasks[0]['coverage']['unresolved_details'] == 1
    assert tasks[0]['coverage']['ocr_gaps_count'] == 1
    assert tasks[0]['coverage']['missing_images_count'] == 1


def test_helper_contract_can_be_mocked_without_raw_leak(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, '_preprocess', lambda packet: {
        'compact_text': 'Compact source', 'signals': {'recruitment': []}, 'quality': {'needs_confirmation': True},
        'jobs': [{'id': 'plain', 'title': 'Thermal Engineer', 'description': 'Thermal design.', 'raw': {'large': 'SECRET_RAW'}},
                 {'id': 'plain', 'title': 'Thermal Engineer', 'description': 'Thermal design.', 'raw': {'large': 'SECRET_RAW'}}]})
    store, _ = build_store(tmp_path, {'jobs': [], 'coverage': {'complete': False}})
    output = tmp_path / 'exports'
    exporter.export_review(store, output)
    _, tasks = read_exports(output)
    assert len(tasks) == 1
    assert tasks[0]['job']['title'] == 'Thermal Engineer'
    assert 'SECRET_RAW' not in json.dumps(tasks)


def test_no_acquisition_result_stays_only_in_uncollected(tmp_path):
    store, _ = build_store(tmp_path, None, status='pending')
    output = tmp_path / 'exports'
    summary = exporter.export_review(store, output)
    assert summary['documents_exported'] == summary['agent_tasks_exported'] == 0
    assert summary['uncollected'] == 1
    assert read_exports(output) == ([], [])


def test_controlled_latest_html_keeps_audit_history_jobs_and_page_conditions(tmp_path):
    result = {'text': 'Original history and navigation', 'jobs': [job()],
              'coverage': {'complete': False}, 'html_paths': ['artifacts/old.html', 'artifacts/latest.html']}
    store, _ = build_store(tmp_path, result)
    artifacts = store.root / 'artifacts'
    artifacts.mkdir()
    (artifacts / 'old.html').write_text('<article>Older unique condition</article>', encoding='utf-8')
    (artifacts / 'latest.html').write_text('<html><body><nav>BOILERPLATE_NAV</nav><article>'
        '<p>UNIQUE_PAGE_CONDITION: applicant must confirm graduation cohort.</p>'
        '</article></body></html>', encoding='utf-8')
    output = tmp_path / 'exports'
    exporter.export_review(store, output)
    reviews, tasks = read_exports(output)
    assert reviews[0]['text'] == result['text']
    assert reviews[0]['raw_html_paths'] == result['html_paths']
    assert tasks[0]['view_scope']['mode'] == 'latest_snapshot'
    assert tasks[0]['view_scope']['snapshots_available'] == 2
    assert tasks[0]['job']['title'] == 'Agent Engineer'
    assert 'UNIQUE_PAGE_CONDITION' in tasks[0]['document_context']
    assert 'BOILERPLATE_NAV' not in tasks[0]['document_context']
    assert any('Original history' in chunk['text'] for chunk in reviews[0]['evidence'])


def test_gbk_html_falls_back_to_correct_audit_text(tmp_path):
    text = '任职要求：硕士学历，计算机视觉研究经验。'
    store, _ = build_store(tmp_path, {'text': text, 'jobs': [],
        'html_path': 'artifacts/gbk.html', 'coverage': {'complete': False}})
    artifacts = store.root / 'artifacts'
    artifacts.mkdir()
    (artifacts / 'gbk.html').write_bytes(('<html><body><article>' + text +
        '</article></body></html>').encode('gbk'))
    packet = {'text': text, 'evidence': [{'kind': 'html', 'text': text}],
              'raw_html_path': 'artifacts/gbk.html', 'jobs': []}
    view, scope = exporter._derived_view(packet, store)
    assert 'html_text' not in view
    assert view['evidence'][0]['text'] == text
    assert scope['mode'] == 'audit_text_fallback'
    assert scope['fallback_reason'] == 'html_decode_uncertain'
    output = tmp_path / 'exports'
    exporter.export_review(store, output)
    reviews, tasks = read_exports(output)
    assert reviews[0]['text'] == text
    assert tasks[0]['compact_text'] == text
    assert '\ufffd' not in tasks[0]['compact_text']
    assert tasks[0]['view_scope']['decoding_replacements'] is True
    assert tasks[0]['coverage']['complete'] is False


@pytest.mark.parametrize('path_kind', ['outside_html', 'env_file', 'oversize'])
def test_html_boundary_and_limit_fall_back_without_reading_rejected_paths(tmp_path, monkeypatch, path_kind):
    store, _ = build_store(tmp_path, {'text': 'Safe audit fallback', 'jobs': [], 'coverage': {'complete': False}})
    artifacts = store.root / 'artifacts'
    artifacts.mkdir()
    candidate = artifacts / 'large.html' if path_kind == 'oversize' else tmp_path / ('private.env' if path_kind == 'env_file' else 'outside.html')
    candidate.write_text('REJECTED_FILE_CONTENT', encoding='utf-8')
    task_id = store.add_task(URL)
    store.finish(task_id, 'partial', {'text': 'Safe audit fallback', 'html_path': str(candidate),
                                    'jobs': [], 'coverage': {'complete': False}})
    original_open = type(candidate).open
    def guarded_open(path, *args, **kwargs):
        if path.resolve() == candidate.resolve():
            raise AssertionError('Rejected or oversize HTML must not be opened')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(type(candidate), 'open', guarded_open)
    if path_kind == 'oversize':
        monkeypatch.setattr(exporter, 'HTML_LIMIT', 5)
    output = tmp_path / 'exports'
    exporter.export_review(store, output)
    _, tasks = read_exports(output)
    assert tasks[0]['compact_text'] == 'Safe audit fallback'
    assert tasks[0]['view_scope']['mode'] == 'audit_text_fallback'
    assert tasks[0]['view_scope']['html_input_truncated'] is (path_kind == 'oversize')


def test_distinct_or_unidentified_jobs_are_not_dropped(tmp_path, monkeypatch):
    jobs = [{'id': 'a', 'title': 'Thermal Engineer'}, {'id': 'b', 'title': 'Thermal Engineer'},
            {'title': 'Unknown role'}, {'title': 'Unknown role'}]
    monkeypatch.setattr(exporter, '_preprocess', lambda packet: {'compact_text': '', 'jobs': jobs,
        'signals': {}, 'quality': {'needs_confirmation': True}})
    store, _ = build_store(tmp_path, {'jobs': [], 'coverage': {'complete': False}})
    output = tmp_path / 'exports'
    exporter.export_review(store, output)
    _, tasks = read_exports(output)
    assert len(tasks) == 4
    assert len({task['record_id'] for task in tasks}) == 4


def test_batch_fold_only_identical_context_and_is_reversible():
    records = [dict(task_id='t', source_url='https://example.test', quality={'gap': True},
        source_assertions={'verified': False}, record_id='a', kind='job',
        job={'title': 'A'}, evidence_ids=['a:job'], truncated=False),
        dict(task_id='t', source_url='https://example.test', quality={'gap': False},
        source_assertions={'verified': False}, record_id='b', kind='job',
        job={'title': 'B'}, evidence_ids=['b:job'], truncated=True)]
    original = json.loads(json.dumps(records))
    batch = exporter._agent_batch(records)
    assert 'quality' not in batch['shared_context']
    assert not {'record_id', 'kind', 'job', 'evidence_ids', 'truncated'} & batch['shared_context'].keys()
    assert batch['shared_context']['source_assertions'] == {'verified': False}
    assert [batch['shared_context'] | record for record in batch['records']] == original
    assert records == original


def test_batch_export_reconstructs_job_and_empty_job_records(tmp_path):
    store, _ = build_store(tmp_path, {'jobs': [job(), {**job(), 'id': 'another', 'title': 'Vision Engineer'}],
        'text': 'Shared conditions', 'coverage': {'complete': False}})
    task_id = store.add_task('https://example.test/blocked')
    store.finish(task_id, 'blocked', {'text': 'Verification required', 'jobs': [],
        'coverage': {'complete': False}})
    output = tmp_path / 'exports'
    summary = exporter.export_review(store, output)
    _, tasks = read_exports(output)
    batches = [json.loads(line) for line in (output / 'agent_batches.jsonl').read_text(encoding='utf-8').splitlines()]
    restored = [batch['shared_context'] | record for batch in batches for record in batch['records']]
    assert restored == tasks
    assert len(batches) == summary['agent_batches_exported'] == 2
    assert len(tasks) == 3
    assert all(task['coverage']['complete'] is False for task in restored)
    assert any(task['kind'] == 'document' for task in restored)


def test_job_company_upstream_ids_and_text_alias_are_preserved(tmp_path, monkeypatch):
    description = 'Known official description.'
    normalized = {'id': 'job-id', 'title': 'Research Engineer', 'company': 'Observed Job Employer',
                  'source_ids': ['upstream-1', 'upstream-2'], 'needs_details': True,
                  'description': description, 'requirements': 'Known requirements.', 'text': description}
    monkeypatch.setattr(exporter, '_preprocess', lambda packet: {'compact_text': '', 'jobs': [normalized],
        'signals': {}, 'quality': {'needs_confirmation': True}})
    store, _ = build_store(tmp_path, {'jobs': [], 'coverage': {'complete': False}})
    output = tmp_path / 'exports'
    exporter.export_review(store, output)
    reviews, tasks = read_exports(output)
    compact_job = tasks[0]['job']
    assert compact_job['company'] == 'Observed Job Employer'
    assert tasks[0]['source_companies'] == ['CSV Company A', 'CSV Company B']
    assert compact_job['source_ids'] == ['upstream-1', 'upstream-2']
    assert compact_job['needs_details'] is True
    assert compact_job['field_aliases'] == {'text': 'description'}
    assert 'text' not in compact_job
    assert any(chunk.get('field') == 'text' and chunk['text'] == description for chunk in reviews[0]['evidence'])
