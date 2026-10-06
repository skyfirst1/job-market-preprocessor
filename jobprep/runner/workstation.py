"""Queue execution and recovery. Acquisition decisions belong to adapters."""

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..settings import options_for_url
from ..store import Store, now, worker_lock
from .policy import RunnerPolicy


def safe_error(error):
    return f'{type(error).__name__}: operation failed; see artifact and task state'


class Workstation:
    def __init__(self, config, *, tools=None, adapters=None, policy=None):
        from ..app import AppTools
        from ..adapters import AdapterRegistry

        self.config = config
        self.store = Store(config['data_dir'])
        self.artifacts = self.store.root / 'artifacts'
        self.artifacts.mkdir(exist_ok=True)
        self.tools = tools if tools is not None else AppTools(artifact_dir=self.artifacts)
        self.adapters = adapters if adapters is not None else AdapterRegistry(config)
        self.policy = policy or RunnerPolicy.from_config(config.get('runner'))

    def ocr_engine(self):
        from ..ocr import BaiduOCR

        return BaiduOCR(self.store.root / 'ocr', max_calls=self.config['ocr_max_calls'],
                        high_accuracy_retry=self.config.get('ocr_high_accuracy_retry', False),
                        model=self.config.get('ocr_model', 'general'))

    async def add_ocr(self, result, engine):
        return await self.tools.ocr(result, engine)

    def _cached_ocr(self, task):
        cached = json.loads(task['result_json']) if task.get('result_json') else None
        available = cached and all(
            bool(item.get('path')) and Path(item['path']).is_file() or
            item.get('status') in ('decorative', 'skipped_duplicate')
            for item in cached.get('images', []))
        if cached and cached.get('ocr_gaps') and available and cached.get('acquisition_status') in ('ok', 'partial'):
            cached['status'] = cached['acquisition_status']
            return cached
        return None

    def _enqueue_details(self, task, result):
        if result.get('status') in ('blocked', 'deleted', 'error'):
            return 0
        urls = list(result.get('coverage', {}).get('detail_urls', []))
        urls.extend(job['url'] for job in result.get('jobs', [])
                    if job.get('url') and job.get('needs_details') is not False)
        urls = list(dict.fromkeys(url for url in urls if isinstance(url, str)))
        cap = self.config.get('max_details_per_page', 100)
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
            raise ValueError('max_details_per_page must be a nonnegative integer')
        if len(urls) > cap:
            result.setdefault('coverage', {}).update(complete=False, detail_limit_reached=True,
                                                      detail_urls_not_enqueued=len(urls) - cap)
        discovered = 0
        for url in urls[:cap]:
            parsed = urlsplit(url)
            if url == task['url'] or parsed.scheme not in ('http', 'https') or not parsed.hostname:
                continue
            self.store.add_task(url, task['priority'], kind='job_detail', parent_id=task['id'])
            discovered += 1
        return discovered

    def _persist(self, task, result):
        status = result.get('status', 'error')
        if status not in {'ok', 'partial', 'pending_ocr', 'blocked', 'deleted', 'error'}:
            raise ValueError('Unsupported acquisition status')
        retry = self.policy.retryable(result) and task['attempts'] < self.policy.max_attempts
        result['runner'] = {'attempt': task['attempts'], 'max_attempts': self.policy.max_attempts,
                            'retry_scheduled': retry}
        diagnostic = 'acquisition_failed; inspect artifact' if result.get('error') or status == 'error' else None
        if retry:
            kind = result.get('error_kind', 'transient')
            due = self.store.schedule_retry(task['id'], self.policy.delay(task['attempts']), kind,
                                            result, diagnostic)
            self.store.event(task['id'], 'retry_scheduled', f'attempt={task["attempts"]} available_at={due}')
            return 'retry_wait'
        if status == 'error' and task['attempts'] >= self.policy.max_attempts:
            result['runner']['retry_exhausted'] = True
        self.store.finish(task['id'], status, result, diagnostic)
        return status

    async def run(self, limit=10, ai_only=True, task_ids=None, options=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('Run limit must be between 1 and 1000')
        options = dict(options or {})
        skip_ocr = options.pop('skip_ocr', not self.config.get('ocr_enabled', True))
        if not isinstance(skip_ocr, bool):
            raise ValueError('skip_ocr must be boolean')
        force_acquire = options.pop('force_acquire', False)
        if not isinstance(force_acquire, bool):
            raise ValueError('force_acquire must be boolean')
        wait_retries = options.pop('wait_retries', self.policy.wait_for_retries)
        if not isinstance(wait_retries, bool):
            raise ValueError('wait_retries must be boolean')
        results = []
        with worker_lock(self.store.root):
            recovered = self.store.recover_running()
            engine = None
            waited = 0
            while len(results) < limit:
                task = self.store.claim(ai_only=ai_only, task_ids=task_ids)
                if task is None:
                    delay = self.store.next_retry_delay(ai_only, task_ids) if wait_retries else None
                    if delay is not None and waited + delay <= self.policy.max_wait_seconds:
                        await asyncio.sleep(delay)
                        waited += delay
                        continue
                    break
                try:
                    if task['attempts'] > self.policy.max_attempts:
                        result = {'url': task['url'], 'status': 'error', 'retryable': False,
                                  'error_kind': 'attempt_budget', 'coverage': {'complete': False},
                                  'error': 'attempt_budget_exhausted'}
                        self._persist(task, result)
                        results.append({'id': task['id'], 'status': 'error', 'attempt_budget_exhausted': True})
                        continue
                    result = None if force_acquire else self._cached_ocr(task)
                    if result is None:
                        task_options = options_for_url(self.config, task['url'])
                        task_options.update(options)
                        result = await self.adapters.acquire(task['url'], self.artifacts,
                                                             task_options, tools=self.tools)
                    result['fetched_at'] = result.get('fetched_at', now())
                    if result.get('status') not in ('blocked', 'deleted', 'error'):
                        if skip_ocr:
                            result['ocr_skipped'] = True
                            if result.get('images'):
                                result.setdefault('coverage', {})['complete'] = False
                        else:
                            engine = engine or self.ocr_engine()
                            result = await self.add_ocr(result, engine)
                    result['source_references'] = self.store.source_references(task['id'])
                    result['semantic_status'] = 'unassessed'
                    discovered = self._enqueue_details(task, result)
                    status = self._persist(task, result)
                    self.store.event(task['id'], 'collected', f'status={status} discovered={discovered}')
                    results.append({'id': task['id'], 'url': task['url'], 'status': status,
                                    'text_chars': len(result.get('text', '')), 'images': len(result.get('images', [])),
                                    'discovered_details': discovered})
                except asyncio.CancelledError:
                    self.store.finish(task['id'], 'pending', error='worker_cancelled')
                    raise
                except Exception as exc:
                    transient = isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError))
                    result = {'url': task['url'], 'status': 'error', 'error': safe_error(exc),
                              'error_kind': 'transport' if transient else 'configuration_or_execution',
                              'retryable': transient, 'coverage': {'complete': False}}
                    status = self._persist(task, result)
                    self.store.event(task['id'], 'error', safe_error(exc))
                    results.append({'id': task['id'], 'status': status, 'error': safe_error(exc)})
            return {'processed': len(results), 'results': results, 'stats': self.store.stats(),
                    'recovered_tasks': recovered}

    async def import_file(self, path, source_url, kind='html', *, skip_ocr=False):
        if not isinstance(skip_ocr, bool):
            raise ValueError('skip_ocr must be boolean')
        with worker_lock(self.store.root):
            result = await self.tools.file(path, source_url, kind, self.artifacts)
            task_id = self.store.add_task(source_url, priority=100, kind='manual')
            if not skip_ocr:
                result = await self.add_ocr(result, self.ocr_engine())
            else:
                result['ocr_skipped'] = True
                result.setdefault('coverage', {})['complete'] = False
            result.update(fetched_at=now(), source_references=self.store.source_references(task_id),
                          semantic_status='unassessed')
            self.store.finish(task_id, result.get('status', 'partial'), result)
        return {'id': task_id, 'status': result.get('status')}
