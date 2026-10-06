import json
import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from jobprep import __main__ as cli
from jobprep import pipeline
from jobprep import settings
from jobprep.store import Store, worker_lock


URL = 'https://mp.weixin.qq.com/s/article'


@pytest.fixture
def workstation(tmp_path, monkeypatch):
    store = Store(tmp_path / 'data')
    instance = Mock(store=store)
    artifacts = store.root / 'artifacts'
    artifacts.mkdir()
    html = artifacts / 'article.html'
    html.write_text('<article>local fixture</article>', encoding='utf-8')

    async def run(**kwargs):
        rows = []
        with worker_lock(store.root):
            for task_id in kwargs['task_ids'][:kwargs['limit']]:
                task = store.claim(ai_only=kwargs['ai_only'], task_ids=[task_id])
                result = {'url': task['url'], 'status': 'ok', 'text': 'local fixture',
                          'html_path': str(html), 'images': []}
                store.finish(task_id, 'ok', result)
                rows.append({'id': task_id, 'status': 'ok'})
        return {'processed': len(rows), 'results': rows, 'stats': store.stats()}

    instance.run = AsyncMock(side_effect=run)
    store.retry = Mock(wraps=store.retry)

    def create(config):
        instance.config = config
        return instance

    factory = Mock(side_effect=create)
    monkeypatch.setattr(pipeline, 'Workstation', factory)
    monkeypatch.setattr(settings, 'load_dotenv', Mock())
    monkeypatch.setattr(cli, 'load_settings', Mock(wraps=settings.load_settings))
    return instance, factory


def invoke(tmp_path, monkeypatch, capsys, *arguments):
    monkeypatch.setattr(sys, 'argv', ['jobprep', '--root', str(tmp_path),
                                    'wechat-run', *arguments])
    cli.main()
    return json.loads(capsys.readouterr().out)


def test_defaults_and_scoped_policy(tmp_path, monkeypatch, capsys, workstation):
    instance, factory = workstation
    report = invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    assert report['processed'] == 1
    config = factory.call_args.args[0]
    assert config['ocr_max_calls'] == 50
    assert config['runner']['automatic_retry'] is False
    assert config['runner']['wait_for_retries'] is False
    call = instance.run.call_args.kwargs
    assert call == {'limit': 1, 'ai_only': False, 'task_ids': report['task_ids'],
                    'options': {'adapter': 'WechatImage', 'images_only': False,
                                'max_images': 30, 'max_requests': 80, 'interval': 15,
                                'browser_channel': 'chrome', 'wait_retries': False}}
    assert instance.store.retry.call_args.kwargs == {'reset_attempts': False}


@pytest.mark.parametrize('flag,value', [('--limit', '0'), ('--limit', '6'),
    ('--max-images', '-1'), ('--max-images', '101'), ('--max-requests', '0'),
    ('--max-requests', '201'), ('--interval', '0'), ('--interval', '301'),
    ('--browser-channel', 'firefox'), ('--limit', '1.5')])
def test_invalid_bounds_before_settings_or_workstation(tmp_path, monkeypatch, capsys,
                                                      workstation, flag, value):
    _, factory = workstation
    settings = Mock(side_effect=AssertionError('Must validate first'))
    monkeypatch.setattr(cli, '_wechat_settings', settings)
    with pytest.raises(SystemExit) as error:
        invoke(tmp_path, monkeypatch, capsys, '--url', URL, flag, value)
    assert error.value.code == 2
    factory.assert_not_called()
    settings.assert_not_called()


@pytest.mark.parametrize('url', ['https://example.com/s/a',
    'https://mp.weixin.qq.com.evil.test/s/a', 'https://mp.weixin.qq.com/',
    'https://user@mp.weixin.qq.com/s/a', 'https://mp.weixin.qq.com:443/s/a',
    'file:///s/a', 'https://mp.weixin.qq.com/s/a\n'])
def test_invalid_url_before_config(tmp_path, monkeypatch, capsys, workstation, url):
    _, factory = workstation
    settings = Mock()
    monkeypatch.setattr(cli, '_wechat_settings', settings)
    with pytest.raises(SystemExit):
        invoke(tmp_path, monkeypatch, capsys, '--url', url)
    settings.assert_not_called()
    factory.assert_not_called()


def test_six_urls_rejected_even_when_identical(tmp_path, monkeypatch, capsys, workstation):
    _, factory = workstation
    with pytest.raises(SystemExit):
        invoke(tmp_path, monkeypatch, capsys, *(['--url', URL] * 6))
    factory.assert_not_called()


def test_explicit_limit_dedup_and_options(tmp_path, monkeypatch, capsys, workstation):
    instance, _ = workstation
    report = invoke(tmp_path, monkeypatch, capsys, '--url', URL, '--url', URL,
                    '--url', URL + '2', '--url', URL + '3', '--limit', '2',
                    '--no-ocr', '--max-images', '0', '--max-requests', '200',
                    '--interval', '300', '--browser-channel', 'msedge')
    assert report['processed'] == 2
    assert len(report['deferred_task_ids']) == 1
    assert instance.run.call_count == 1
    options = instance.run.call_args.kwargs['options']
    assert options['skip_ocr'] is True
    assert options['max_images'] == 0
    assert options['max_requests'] == 200
    assert options['interval'] == 300
    assert options['browser_channel'] == 'msedge'


@pytest.mark.parametrize('status', ['ok', 'partial', 'pending_ocr', 'blocked', 'deleted', 'error'])
def test_cached_results_preserve_attempts_and_paths(tmp_path, monkeypatch, capsys,
                                                   workstation, status):
    instance, _ = workstation
    task_id = instance.store.add_task(URL)
    for _ in range(3):
        instance.store.claim(task_ids=[task_id])
        instance.store.finish(task_id, 'pending')
    result = {'url': URL, 'status': status,
              'html_path': str(instance.store.root / 'artifacts' / 'article.html'), 'images': []}
    instance.store.finish(task_id, status, result)
    for _ in range(4):
        report = invoke(tmp_path, monkeypatch, capsys, '--url', URL)
        assert 'result' not in report['cached'][0]
        document = json.loads(Path(report['cached'][0]['document_path']).read_text(encoding='utf-8'))
        assert document['html_path'] == result['html_path']
        assert report['processed'] == 0
    instance.run.assert_not_called()
    assert instance.store.task(task_id)['attempts'] == 3
    assert instance.store.task(task_id)['status'] == status
    assert all(call.args[1] == [] for call in instance.store.retry.call_args_list)


def test_second_invocation_reuses_new_result(tmp_path, monkeypatch, capsys, workstation):
    instance, _ = workstation
    first = invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    second = invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    assert first['processed'] == 1
    assert second['processed'] == 0
    assert second['cached'][0]['document_path'].endswith('document.json')
    assert instance.run.call_count == 1
    assert instance.store.task(first['task_ids'][0])['attempts'] == 1


def test_uncached_error_requeues_without_reset(tmp_path, monkeypatch, capsys, workstation):
    instance, _ = workstation
    task_id = instance.store.add_task(URL)
    instance.store.claim(task_ids=[task_id])
    instance.store.finish(task_id, 'error')
    report = invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    assert report['processed'] == 1
    assert instance.store.task(task_id)['attempts'] == 2
    assert instance.store.retry.call_args.args[1] == [task_id]
    assert instance.store.retry.call_args.kwargs['reset_attempts'] is False


@pytest.mark.parametrize('raw,reason', [('broken', 'invalid_cached_result'),
                                     ('[]', 'invalid_cached_result'),
                                     (None, 'attempt_budget_exhausted')])
def test_unsafe_or_exhausted_cache_stops(tmp_path, monkeypatch, capsys,
                                       workstation, raw, reason):
    instance, _ = workstation
    task_id = instance.store.add_task(URL)
    with instance.store.connect() as db:
        db.execute('UPDATE tasks SET attempts=3,status=?,result_json=? WHERE id=?',
                   ('error', raw, task_id))
    report = invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    assert report['stopped'][0]['stop_reason'] == reason
    instance.run.assert_not_called()
    assert instance.store.task(task_id)['attempts'] == 3


def test_lock_conflict_prevents_queue_mutation(tmp_path, monkeypatch, capsys, workstation):
    instance, _ = workstation
    with worker_lock(instance.store.root), pytest.raises(RuntimeError):
        invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    assert instance.store.stats()['tasks'] == 0
    instance.run.assert_not_called()


def test_json_settings_preserve_budget_and_other_config(tmp_path, monkeypatch, capsys,
                                                       workstation):
    _, factory = workstation
    directory = tmp_path / 'config'
    directory.mkdir()
    config = {'ocr_max_calls': 50, 'ocr_enabled': False,
              'runner': {'max_attempts': 3, 'automatic_retry': True},
              'sites': {'other.example': {'max_pages': 7}}, 'crawl': {'timeout': 20}}
    (directory / 'workstation.json').write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setenv('JOBPREP_OCR_MAX_CALLS', '999')
    invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    loaded = factory.call_args.args[0]
    assert loaded['ocr_max_calls'] == 999
    assert loaded['runner']['max_attempts'] == 3
    assert loaded['sites'] == config['sites']
    assert loaded['crawl'] == config['crawl']
    assert json.loads((directory / 'workstation.json').read_text()) == config
    settings.load_dotenv.assert_called_once_with(tmp_path / '.env', override=False)


@pytest.mark.parametrize('url', [URL + '?pass_ticket=hidden', URL + '?TOKEN=hidden',
    'https://mp.weixin.qq.com/s?__biz=biz&mid=1&idx=1&uin=hidden',
    'https://mp.weixin.qq.com/s?__biz=biz&mid=no',
    'https://mp.weixin.qq.com/s?__biz=biz&mid=1&mid=2'])
def test_session_and_invalid_identity_rejected_without_loading(tmp_path, monkeypatch,
                                                              capsys, workstation, url):
    with pytest.raises(SystemExit):
        invoke(tmp_path, monkeypatch, capsys, '--url', url)
    cli.load_settings.assert_not_called()
    assert 'hidden' not in capsys.readouterr().err


@pytest.mark.parametrize('first,second', [
    (URL + '?scene=1&utm_source=test#wechat_redirect', URL),
    ('http://mp.weixin.qq.com/s?mid=1&__biz=biz&idx=1&scene=2',
     'https://mp.weixin.qq.com/s?__biz=biz&idx=1&mid=1')])
def test_normalized_article_identity_deduplicates(tmp_path, monkeypatch, capsys,
                                                 workstation, first, second):
    instance, _ = workstation
    report = invoke(tmp_path, monkeypatch, capsys, '--url', first, '--url', second,
                    '--limit', '2')
    assert len(report['task_ids']) == 1
    assert instance.store.task(report['task_ids'][0])['url'] == second


def image_result(instance):
    from PIL import Image

    path = instance.store.root / 'artifacts' / 'image.png'
    Image.new('RGB', (20, 20), 'white').save(path)
    return {'url': URL, 'status': 'partial', 'text': 'Article body',
            'html_path': str(instance.store.root / 'artifacts' / 'article.html'),
            'images': [{'path': str(path), 'status': 'ok',
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}],
            'ocr_skipped': True, 'coverage': {'complete': False}}


def setup_ocr(instance):
    from jobprep.app import AppTools

    engine = Mock()
    engine.recognize = AsyncMock(return_value={'status': 'ok', 'text': 'OCR fixture'})
    instance.ocr_engine.return_value = engine
    instance.add_ocr = AsyncMock(side_effect=AppTools(artifact_dir=instance.store.root / 'artifacts').ocr)
    return engine


def test_cached_no_ocr_then_ocr_without_claim_or_navigation(tmp_path, monkeypatch,
                                                         capsys, workstation):
    instance, _ = workstation
    task_id = instance.store.add_task(URL)
    instance.store.claim(task_ids=[task_id])
    instance.store.finish(task_id, 'partial', image_result(instance))
    engine = setup_ocr(instance)
    invoke(tmp_path, monkeypatch, capsys, '--url', URL, '--no-ocr')
    instance.add_ocr.assert_not_called()
    report = invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    assert report['cached'][0]['ocr_updated'] is True
    assert instance.store.task(task_id)['attempts'] == 1
    saved = json.loads(instance.store.task(task_id)['result_json'])
    assert 'ocr_skipped' not in saved
    assert saved['ocr'][0]['status'] == 'ok'
    document = json.loads(Path(report['exports'][0]['document_path']).read_text(encoding='utf-8'))
    assert 'OCR fixture' in document['derived']['compact_text']
    assert Path(report['exports'][0]['article_path']).read_text(encoding='utf-8') == document['derived']['compact_text']
    assert 'Article body' not in json.dumps(report)
    invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    assert instance.add_ocr.call_count == 1
    assert engine.recognize.call_count == 1
    instance.run.assert_not_called()


@pytest.mark.parametrize('status', ['blocked', 'error', 'deleted'])
def test_terminal_cache_never_ocr(tmp_path, monkeypatch, capsys, workstation, status):
    instance, _ = workstation
    result = image_result(instance)
    result['status'] = status
    task_id = instance.store.add_task(URL)
    instance.store.finish(task_id, status, result)
    invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    instance.add_ocr.assert_not_called()
    instance.ocr_engine.assert_not_called()
    instance.run.assert_not_called()


@pytest.mark.parametrize('failure', ['missing', 'hash', 'outside', 'html_missing'])
def test_corrupt_local_evidence_stops_without_ocr_or_navigation(tmp_path, monkeypatch,
                                                              capsys, workstation, failure):
    instance, _ = workstation
    result = image_result(instance)
    image = result['images'][0]
    if failure == 'missing':
        Path(image['path']).unlink()
    elif failure == 'hash':
        image['sha256'] = '0' * 64
    elif failure == 'outside':
        image['path'] = str(tmp_path / 'outside.png')
    else:
        Path(result['html_path']).unlink()
    task_id = instance.store.add_task(URL)
    instance.store.finish(task_id, 'partial', result)
    report = invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    assert report['cached'] == []
    assert report['stopped'][0]['stop_reason'] == 'invalid_cached_result'
    assert report['exports'][0]['status'] == 'error'
    instance.add_ocr.assert_not_called()
    instance.run.assert_not_called()


def test_pending_ocr_gap_resolves_at_exhausted_attempt_budget(tmp_path, monkeypatch,
                                                          capsys, workstation):
    instance, _ = workstation
    task_id = instance.store.add_task(URL)
    result = image_result(instance)
    result.update(status='pending_ocr', acquisition_status='partial',
                  ocr_gaps=[{'image': 0, 'reason': 'credentials_missing'}])
    result['coverage']['ocr_gaps'] = result['ocr_gaps']
    instance.store.finish(task_id, 'pending_ocr', result)
    with instance.store.connect() as db:
        db.execute('UPDATE tasks SET attempts=3 WHERE id=?', (task_id,))
    setup_ocr(instance)
    invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    saved = json.loads(instance.store.task(task_id)['result_json'])
    assert saved['status'] == 'partial'
    assert saved['ocr_gaps'] == []
    assert 'ocr_gaps' not in saved['coverage']
    assert instance.store.task(task_id)['attempts'] == 3
    instance.run.assert_not_called()


def test_complete_ocr_reused_even_with_old_skipped_flag(tmp_path, monkeypatch, capsys,
                                                      workstation):
    instance, _ = workstation
    result = image_result(instance)
    result['ocr'] = [{'image_index': 0, 'status': 'ok', 'text': 'Already recognized'}]
    instance.store.finish(instance.store.add_task(URL), 'partial', result)
    invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    instance.add_ocr.assert_not_called()
    instance.ocr_engine.assert_not_called()


def test_unsuccessful_cached_ocr_persists_gaps_without_new_attempt(tmp_path, monkeypatch,
                                                                capsys, workstation):
    instance, _ = workstation
    task_id = instance.store.add_task(URL)
    instance.store.finish(task_id, 'partial', image_result(instance))
    engine = setup_ocr(instance)
    engine.recognize.return_value = {'status': 'credentials_missing'}
    report = invoke(tmp_path, monkeypatch, capsys, '--url', URL)
    saved = json.loads(instance.store.task(task_id)['result_json'])
    assert saved['status'] == 'pending_ocr'
    assert saved['ocr_skipped'] is True
    assert saved['ocr_gaps'][0]['reason'] == 'credentials_missing'
    assert report['cached'][0]['ocr_gaps'] == 1
    assert instance.store.task(task_id)['attempts'] == 0
    instance.run.assert_not_called()


@pytest.mark.parametrize('complete', [True, False, None])
def test_export_distinguishes_discovered_and_saved_images(tmp_path, workstation, complete):
    instance, _ = workstation
    instance.config = {'export_dir': tmp_path / 'exports'}
    result = image_result(instance)
    result['images'].extend([{'path': '', 'status': 'pending_image'},
                             {'path': '', 'status': 'ok'},
                             {'path': 'failed.png', 'status': 'error'},
                             {'path': '', 'status': 'skipped_duplicate'}])
    if complete is not None:
        result['coverage']['image_complete'] = complete
    summary = cli._wechat_export(instance, 'local-fixture', result)
    assert summary['images'] == 5
    assert summary['images_saved'] == 1
    assert summary['image_complete'] is (complete if complete is not None else False)
    assert 'result' not in summary
