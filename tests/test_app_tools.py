import asyncio
import copy
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType
import sys

import httpx
import pytest
from PIL import Image

from jobprep.app import AppTools, tool_catalog
from jobprep.app.feishu import PortalTransport, matches_response


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def wechat_collector(monkeypatch):
    module = ModuleType('jobprep.app.wechat_public')
    module.collect = None
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(sys.modules['jobprep.app'], 'wechat_public', module, raising=False)
    return module


def config():
    return {'url': 'https://example.com/list',
            'pagination': {'path': 'page', 'max_pages': 2},
            'response': {'items_path': 'items', 'id_path': 'id', 'total_path': 'total'},
            'fields': {'title': 'title', 'description': 'description', 'requirements': 'requirements', 'url': 'url'},
            'http': {'interval': 0, 'retries': 0}}


def mock_lists(monkeypatch, responses):
    from jobprep import list_collect

    original = list_collect.collect
    threads = []
    def collect(cfg, output, **kwargs):
        threads.append(threading.get_ident())
        with httpx.Client(transport=httpx.MockTransport(lambda request: responses.pop(0))) as client:
            return original(cfg, output, client=client, sleep=lambda _: None, **kwargs)
    monkeypatch.setattr(list_collect, 'collect', collect)
    return threads


def test_catalog_contract_and_independent_metadata():
    catalog = tool_catalog()
    assert set(catalog) == {'web', 'wechat', 'file', 'ocr', 'public_list', 'feishu_list', 'zhiye_list'}
    assert AppTools()
    for spec in catalog.values():
        assert spec['output'] and spec['limits']
        for parameter in spec['parameters'].values():
            assert {'type', 'required', 'default', 'range', 'description'} <= parameter.keys()
    json.dumps(catalog)
    catalog['web']['parameters']['options']['properties']['max_pages']['default'] = 999
    assert tool_catalog()['web']['parameters']['options']['properties']['max_pages']['default'] == 3


def test_web_dynamic_import_and_options_copy(tmp_path, monkeypatch):
    from jobprep import crawler

    seen = []
    async def collect(url, root, options):
        seen.append((url, root, options))
        options['max_pages'] = 99
        return {'status': 'ok', 'coverage': {'complete': True}}
    monkeypatch.setattr(crawler, 'collect', collect)
    opts = {'max_pages': 1}
    result = run(AppTools().web('https://example.com', tmp_path, opts))
    assert opts == {'max_pages': 1}
    assert seen[0][1] == tmp_path.resolve()
    assert result['artifact_dir'] == str(tmp_path.resolve())


def test_wechat_images_only_preserves_warnings(tmp_path, monkeypatch, wechat_collector):

    async def collect(url, root, options):
        assert options['images_only'] is True
        return {'status': 'ok', 'text': 'visible text', 'warnings': ['original warning'],
                'coverage': {'complete': True, 'list_complete': False}, 'images': []}
    monkeypatch.setattr(wechat_collector, 'collect', collect)
    result = run(AppTools().wechat('https://mp.weixin.qq.com/s/test', tmp_path, {'images_only': True}))
    assert result['text'] == '' and result['status'] == 'partial'
    assert result['warnings'][0] == 'original warning'
    assert not result['coverage']['complete']
    assert result['coverage']['text_omitted']


def test_wechat_blocked_collector_does_not_ocr(tmp_path, monkeypatch, wechat_collector):
    from jobprep.app.tools import empty_document

    hits = []
    async def collect(url, root, options):
        hits.append(url)
        result = empty_document(url, 'wechat', root)
        result.update(status='blocked', error_kind='blocked', http_status=403)
        return result
    monkeypatch.setattr(wechat_collector, 'collect', collect)
    result = run(AppTools().wechat('https://mp.weixin.qq.com/s/blocked', tmp_path))
    assert result['status'] == 'blocked'
    assert len(hits) == 1
    assert result['images'] == [] and result['retryable'] is False
    class Engine:
        async def recognize(self, path):
            pytest.fail('blocked page must never invoke OCR')
    assert run(AppTools().ocr(result, Engine()))['ocr'] == []


def test_file_manual_html_relative_image_and_unresolved_remote(tmp_path):
    manual = tmp_path / 'manual'
    manual.mkdir()
    Image.new('RGB', (100, 100)).save(manual / 'poster.png')
    html = manual / 'article.html'
    html.write_text('<div id="js_content">Jobs<img src="poster.png"><img src="https://example.com/remote.png"></div>', encoding='utf-8')
    result = run(AppTools().file(html, 'https://example.com/article', 'html', tmp_path / 'artifacts'))
    assert result['method'] == 'manual-html'
    assert result['images'][0]['status'] == 'ok'
    assert Path(result['images'][0]['path']).read_bytes() == (manual / 'poster.png').read_bytes()
    assert result['images'][1]['status'] == 'pending_manual_image'
    assert not result['coverage']['complete'] and 'ocr' not in result
    assert not (tmp_path / 'artifacts' / 'workstation.sqlite3').exists()


def test_file_refuses_env_traversal_and_nonimage_suffix(tmp_path):
    (tmp_path / '.env').write_text('SECRET', encoding='utf-8')
    with pytest.raises(ValueError, match='suffix'):
        run(AppTools().file(tmp_path / '.env', 'https://example.com', 'html', tmp_path / 'artifacts'))
    manual = tmp_path / 'manual'
    manual.mkdir()
    Image.new('RGB', (20, 20)).save(tmp_path / 'outside.png')
    html = manual / 'a.htm'
    html.write_text('<article><img src="../outside.png"><img src="%2e%2e/.env"><img src="C:/private.png"></article>', encoding='utf-8')
    result = run(AppTools().file(html, 'https://example.com', 'html', tmp_path / 'artifacts'))
    assert all(not image.get('path') for image in result['images'])
    assert result['status'] == 'partial'


def test_file_dimension_and_corrupt_image_limits(tmp_path):
    path = tmp_path / 'huge.png'
    Image.new('L', (20001, 1)).save(path)
    with pytest.raises(ValueError, match='dimension'):
        run(AppTools().file(path, 'https://example.com', 'image', tmp_path / 'artifacts'))
    path.write_bytes(b'not an image')
    with pytest.raises(OSError):
        run(AppTools().file(path, 'https://example.com', 'image', tmp_path / 'artifacts'))


def test_file_blocked_does_not_copy_adjacent_images(tmp_path):
    path = tmp_path / 'blocked.html'
    path.write_text('<title>Access denied</title><img src="poster.png">', encoding='utf-8')
    Image.new('RGB', (20, 20)).save(tmp_path / 'poster.png')
    result = run(AppTools().file(path, 'https://example.com', 'html', tmp_path / 'artifacts'))
    assert result['status'] == 'blocked'
    assert not list((tmp_path / 'artifacts').glob('*.png'))


def test_ocr_gaps_path_hash_budget_and_legacy_root(tmp_path):
    root = tmp_path / 'artifacts'
    root.mkdir()
    Image.new('RGB', (20, 20)).save(root / 'a.png')
    Image.new('RGB', (20, 20)).save(tmp_path / 'outside.png')
    class Engine:
        cache_dir = tmp_path / 'ocr'
        max_calls = 7
        calls = []
        async def recognize(self, path):
            self.calls.append(path)
            return {'status': 'pending_budget', 'text': ''}
    result = {'status': 'partial', 'coverage': {'complete': False}, 'images': [
        {'path': 'a.png'}, {'path': '../outside.png'}, {'path': 'a.png', 'sha256': 'wrong'},
        {'status': 'pending_manual_image'}, {'status': 'decorative'}]}
    original = copy.deepcopy(result)
    engine = Engine()
    output = run(AppTools().ocr(result, engine))
    assert len(engine.calls) == 1 and engine.max_calls == 7
    assert output['status'] == 'pending_ocr' and len(output['ocr_gaps']) == 4
    assert output['ocr'][0]['image_index'] == 0
    assert output['error_kind'] == 'budget' and output['retryable'] is False
    assert result == original


def test_ocr_missing_root_and_engine_exception(tmp_path):
    image = tmp_path / 'a.png'
    Image.new('RGB', (20, 20)).save(image)
    class Engine:
        async def recognize(self, path):
            raise RuntimeError('sensitive request')
    document = {'status': 'ok', 'images': [{'path': str(image)}]}
    output = run(AppTools().ocr(document, Engine()))
    assert output['ocr_gaps'][0]['reason'] == 'missing_artifact_root'
    document['artifact_dir'] = str(tmp_path)
    output = run(AppTools().ocr(document, Engine()))
    assert output['status'] == 'pending_ocr'
    assert 'sensitive' not in output['ocr_gaps'][0]['reason']


@pytest.mark.parametrize('complete_jd', [False, True])
def test_public_list_canonical_raw_exports_and_worker(tmp_path, monkeypatch, complete_jd):
    job = {'id': '1', 'title': 'Engineer', 'url': 'https://example.com/job/1',
           'description': 'Build', 'requirements': 'Python' if complete_jd else ''}
    threads = mock_lists(monkeypatch, [httpx.Response(200, json={'total': 1, 'items': [job]})])
    result = run(AppTools().public_list(config(), tmp_path))
    assert threads == [threads[0]] and threads[0] != threading.get_ident()
    assert result['status'] == 'ok'
    assert result['coverage']['list_complete']
    assert result['coverage']['jd_complete'] == complete_jd
    assert result['coverage']['complete'] == complete_jd
    assert result['jobs'][0]['raw'] == job and result['jobs'][0]['id'] == '1'
    assert Path(result['jobs'][0]['raw_ref']).is_file()
    assert result['evidence'][0]['raw_ref'] == result['jobs'][0]['raw_ref']
    for path in result['output_paths'].values():
        assert Path(path).is_file()


@pytest.mark.parametrize('code,retryable,kind', [(503, True, 'http'), (403, False, 'blocked'), (429, True, 'rate_limited'), (404, False, 'deleted')])
def test_public_failure_fields(tmp_path, monkeypatch, code, retryable, kind):
    mock_lists(monkeypatch, [httpx.Response(code, json={'error': 'unavailable'})])
    result = run(AppTools().public_list(config(), tmp_path))
    assert result['status'] == 'error' and result['jobs'] == []
    assert result['retryable'] == retryable and result['error_kind'] == kind
    assert result['http_status'] == code
    assert Path(result['json_paths'][0]).is_file()


def test_public_list_late_error_preserves_accepted_jobs(tmp_path, monkeypatch):
    mock_lists(monkeypatch, [httpx.Response(200, json={'total': 2, 'items': [{'id': 1}]}),
                             httpx.Response(503, json={'error': 'temporary'})])
    result = run(AppTools().public_list(config(), tmp_path))
    assert result['status'] == 'partial' and len(result['jobs']) == 1
    assert result['retryable'] and not result['coverage']['list_complete']
    assert len(result['evidence']) == 2


def browser_response(offset, status=200):
    return SimpleNamespace(url='https://example.jobs.feishu.cn/api/v1/search/job/posts', status=status,
                           request=SimpleNamespace(method='POST', post_data_json={'offset': offset, 'limit': 1}),
                           body=lambda: json.dumps({'code': 0, 'data': {'count': 2, 'job_post_list': [{'id': str(offset), 'title': 'Engineer'}]}}).encode())


def test_feishu_transport_failed_response_not_filtered_and_timeout():
    first, failed = browser_response(0), browser_response(1, 503)
    class Page:
        def locator(self, selector):
            assert selector == '.custom-next'
            return SimpleNamespace(get_attribute=lambda *args, **kwargs: None, click=lambda **kwargs: None)
        @contextmanager
        def expect_response(self, predicate, timeout):
            assert timeout == 200
            assert predicate(failed)
            yield SimpleNamespace(value=failed)
    transport = PortalTransport(Page(), first, 200, '.custom-next')
    transport.handle_request(httpx.Request('POST', first.url, json={'offset': 0, 'limit': 1}))
    response = transport.handle_request(httpx.Request('POST', first.url, json={'offset': 1, 'limit': 1}))
    assert response.status_code == 503
    assert not matches_response(failed)
    assert matches_response(failed, offset=1)
    transport.page.expect_response = lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(httpx.TransportError):
        transport.handle_request(httpx.Request('POST', first.url, json={'offset': 2, 'limit': 1}))


def test_feishu_full_mock_ui_and_generic_collect(tmp_path, monkeypatch):
    from playwright import sync_api

    responses = [browser_response(0), browser_response(1)]
    closed = []
    class Page:
        url = 'https://example.jobs.feishu.cn'
        def set_default_timeout(self, timeout):
            pass
        def goto(self, *args, **kwargs):
            pass
        def locator(self, selector):
            return SimpleNamespace(get_attribute=lambda *args, **kwargs: None, click=lambda **kwargs: None)
        @contextmanager
        def expect_response(self, predicate, timeout):
            response = responses.pop(0)
            assert predicate(response)
            yield SimpleNamespace(value=response)
    browser = SimpleNamespace(new_page=lambda: Page(), close=lambda: closed.append(True))
    @contextmanager
    def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))
    monkeypatch.setattr(sync_api, 'sync_playwright', playwright)
    result = run(AppTools().feishu_list('https://example.jobs.feishu.cn', tmp_path, {'interval': 0, 'max_pages': 2}))
    assert result['status'] == 'ok' and len(result['jobs']) == 2
    assert result['coverage']['list_complete'] and not result['coverage']['complete']
    assert all(job['url'] is None for job in result['jobs'])
    assert closed == [True]
    assert Path(result['output_paths']['request_config_json']).is_file()


def test_feishu_initial_timeout_canonical(tmp_path, monkeypatch):
    from playwright import sync_api

    @contextmanager
    def playwright():
        raise TimeoutError('simulated')
        yield
    monkeypatch.setattr(sync_api, 'sync_playwright', playwright)
    result = run(AppTools().feishu_list('https://example.jobs.feishu.cn', tmp_path))
    assert result['status'] == 'error' and result['retryable']
    assert result['jobs'] == [] and not result['coverage']['complete']


@pytest.mark.parametrize('url,options', [('https://eviljobs.feishu.cn', {}),
                                      ('https://example.jobs.feishu.cn', {'max_pages': 0}),
                                      ('https://example.jobs.feishu.cn', {'timeout': True}),
                                      ('https://example.jobs.feishu.cn', {'profile': 'private'})])
def test_feishu_rejects_invalid_contract(tmp_path, url, options):
    with pytest.raises(ValueError):
        run(AppTools().feishu_list(url, tmp_path, options))
