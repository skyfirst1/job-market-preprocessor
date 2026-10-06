import asyncio
import json
import sqlite3

import pytest

from jobprep.runner import RunnerPolicy, Workstation
from jobprep.store import Store


def config(root):
    return {'root': root, 'data_dir': root / 'data', 'export_dir': root / 'exports',
            'ocr_max_calls': 0, 'ocr_enabled': False, 'crawl': {}, 'sites': {}}


class FakeAdapters:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    async def acquire(self, url, artifact_dir, options, tools=None):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome, url=url, images=[], jobs=[])


def test_retry_is_durable_and_scope_aware(tmp_path):
    store = Store(tmp_path)
    task = store.add_task('https://example.com/a', priority=1)
    assert store.claim()['attempts'] == 1
    store.schedule_retry(task, 3600, 'timeout')
    reopened = Store(tmp_path)
    assert reopened.claim() is None
    assert reopened.next_retry_delay(task_ids=[task]) > 3500
    assert reopened.next_retry_delay(task_ids=[]) is None
    reopened.schedule_retry(task, 0, 'timeout')
    assert reopened.claim()['attempts'] == 2


def test_transient_failure_retries_then_succeeds_without_ocr(tmp_path):
    adapters = FakeAdapters([TimeoutError('secret'), {'status': 'ok', 'coverage': {'complete': True}}])
    workstation = Workstation(config(tmp_path), adapters=adapters,
                              policy=RunnerPolicy(base_delay_seconds=0))
    workstation.ocr_engine = lambda: pytest.fail('OCR must not be initialized')
    task = workstation.store.add_task('https://example.com/a', priority=1)
    summary = asyncio.run(workstation.run(3))
    assert [r['status'] for r in summary['results']] == ['retry_wait', 'ok']
    assert workstation.store.task(task)['attempts'] == 2
    assert 'secret' not in json.dumps(summary)


def test_retry_budget_and_explicit_reset(tmp_path):
    adapters = FakeAdapters([{'status': 'error', 'retryable': True}] * 3)
    workstation = Workstation(config(tmp_path), adapters=adapters,
                              policy=RunnerPolicy(max_attempts=2, base_delay_seconds=0))
    task = workstation.store.add_task('https://example.com/a', priority=1)
    assert asyncio.run(workstation.run(5))['processed'] == 2
    assert workstation.store.task(task)['status'] == 'error'
    workstation.store.retry(['error'], [task])
    asyncio.run(workstation.run(1))
    assert adapters.calls == 2
    workstation.store.retry(['error'], [task], reset_attempts=True)
    assert workstation.store.task(task)['attempts'] == 0


def test_future_retry_does_not_exceed_batch_wait_budget(tmp_path):
    adapters = FakeAdapters([{'status': 'error', 'retryable': True}])
    workstation = Workstation(config(tmp_path), adapters=adapters,
                              policy=RunnerPolicy(base_delay_seconds=3600, max_delay_seconds=3600,
                                                  max_wait_seconds=0))
    task = workstation.store.add_task('https://example.com/a', priority=1)
    summary = asyncio.run(workstation.run(5))
    assert summary['processed'] == 1
    assert workstation.store.task(task)['status'] == 'retry_wait'
    assert workstation.store.next_retry_delay(task_ids=[task]) > 3500


def test_http_error_can_explicitly_disable_retries(tmp_path):
    adapters = FakeAdapters([{'status': 'error', 'http_status': 503, 'retryable': False}])
    workstation = Workstation(config(tmp_path), adapters=adapters)
    task = workstation.store.add_task('https://example.com/a', priority=1)
    assert asyncio.run(workstation.run(5))['processed'] == 1
    assert workstation.store.task(task)['status'] == 'error'


@pytest.mark.parametrize('status', ['blocked', 'deleted', 'partial', 'pending_ocr'])
def test_nontransient_results_are_not_retried(tmp_path, status):
    adapters = FakeAdapters([{'status': status, 'retryable': True}])
    workstation = Workstation(config(tmp_path), adapters=adapters)
    workstation.store.add_task('https://example.com/a', priority=1)
    assert asyncio.run(workstation.run(5))['processed'] == 1
    assert adapters.calls == 1


def test_cancel_returns_task_to_pending(tmp_path):
    workstation = Workstation(config(tmp_path), adapters=FakeAdapters([asyncio.CancelledError()]))
    task = workstation.store.add_task('https://example.com/a', priority=1)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(workstation.run(1))
    assert workstation.store.task(task)['status'] == 'pending'


def test_complete_api_jds_do_not_enqueue_duplicate_fetches(tmp_path):
    workstation = Workstation(config(tmp_path))
    task_id = workstation.store.add_task('https://example.com/list', priority=1)
    task = workstation.store.claim()
    result = {'status': 'ok', 'jobs': [
        {'url': 'https://example.com/job/1', 'needs_details': False},
        {'url': 'https://example.com/job/2', 'needs_details': True}], 'coverage': {}}
    assert workstation._enqueue_details(task, result) == 1
    assert workstation.store.stats()['tasks'] == 2
    assert workstation.store.task(task_id) is not None


def test_old_database_migration_preserves_records(tmp_path):
    db = sqlite3.connect(tmp_path / 'workstation.sqlite3')
    db.execute('''CREATE TABLE tasks (id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL, priority INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0, updated_at TEXT NOT NULL, error TEXT,
                result_json TEXT, parent_id TEXT)''')
    db.execute("INSERT INTO tasks(id,url,kind,updated_at) VALUES('old','https://example.com/','page','old')")
    db.commit()
    db.close()
    store = Store(tmp_path)
    assert store.task('old')['available_at'] is None
    assert store.claim()['id'] == 'old'


@pytest.mark.parametrize('values', [{'max_attempts': True}, {'base_delay_seconds': float('nan')},
                                    {'automatic_retry': 'yes'}])
def test_invalid_policy_is_rejected(values):
    with pytest.raises(ValueError):
        RunnerPolicy.from_config(values)
