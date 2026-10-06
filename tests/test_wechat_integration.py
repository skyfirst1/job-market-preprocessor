"""Offline registry/AppTools integration with only the public collector mocked."""

import asyncio
from copy import deepcopy
import sys
from types import ModuleType

import httpx
import pytest

from jobprep.adapters import AdapterRegistry
from jobprep.app import AppTools, tool_catalog
from jobprep.app.tools import empty_document


URL = 'https://mp.weixin.qq.com/s/public-article'


@pytest.fixture
def collector(monkeypatch):
    module = ModuleType('jobprep.app.wechat_public')
    module.collect = None
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(sys.modules['jobprep.app'], 'wechat_public', module, raising=False)
    return module


def test_registry_uses_public_collector_and_preserves_article(tmp_path, monkeypatch, collector):
    calls = []

    async def collect(url, root, options=None):
        calls.append((url, root, deepcopy(options)))
        options['max_images'] = 99
        result = empty_document(url, 'wechat', root)
        result.update(status='ok', title='Public article', text='Article body',
                      html_path=str(root / 'article.html'), html_paths=[str(root / 'article.html')],
                      images=[{'url': 'https://mmbiz.qpic.cn/poster', 'path': str(root / 'poster.png'),
                               'sha256': 'observed-hash', 'status': 'ok', 'order': 0}],
                      evidence=[{'kind': 'html', 'path': str(root / 'article.html')}])
        result['coverage'].update(pages_seen=1, stop_reason='single_article')
        return result

    async def forbidden(*args, **kwargs):
        pytest.fail('WeChat must not call the legacy web tool or OCR')

    monkeypatch.setattr(collector, 'collect', collect)
    monkeypatch.setattr(AppTools, 'web', forbidden)
    monkeypatch.setattr(AppTools, 'ocr', forbidden)
    options = {'browser': 'auto', 'max_pages': 3, 'max_scrolls': 3, 'max_images': 30,
               'timeout': 20, 'domain_delay': 0, 'channel': 'chrome', 'max_requests': 80,
               'max_html_bytes': 5 * 1024**2, 'max_image_bytes': 10 * 1024**2,
               'max_total_image_bytes': 30 * 1024**2}
    original = deepcopy(options)
    result = asyncio.run(AdapterRegistry().acquire(URL, tmp_path, options))
    assert options == original
    assert calls == [(URL, tmp_path.resolve(), {
        'images_only': False, 'max_images': 30, 'timeout': 20, 'interval': 0,
        'browser_channel': 'chrome', 'max_requests': 80, 'max_html_bytes': 5 * 1024**2,
        'max_image_bytes': 10 * 1024**2, 'max_total_image_bytes': 30 * 1024**2})]
    assert result['method'] == 'wechat' and result['text'] == 'Article body'
    assert result['images'][0]['sha256'] == 'observed-hash'
    assert result['coverage']['scope'] == 'single_article'
    assert result['coverage']['list_complete'] is False
    assert result['acquisition']['adapter'] == 'WechatImage'
    provenance = result['acquisition']['provenance'][-1]
    assert provenance['artifact_refs']['html_paths'] == result['html_paths']
    assert result['retryable'] is False


@pytest.mark.parametrize('error', [TimeoutError('secret'), httpx.ConnectError('secret')])
def test_tool_failure_is_nonretryable_document(tmp_path, monkeypatch, collector, error):
    async def collect(*args, **kwargs):
        raise error

    monkeypatch.setattr(collector, 'collect', collect)
    result = asyncio.run(AppTools().wechat(URL, tmp_path))
    assert result['status'] == 'error' and result['retryable'] is False
    assert result['method'] == 'wechat'
    assert 'secret' not in result['error']


def test_tool_default_retains_text_and_copies_options(tmp_path, monkeypatch, collector):
    async def collect(url, root, options=None):
        assert options == {}
        options['interval'] = 99
        result = empty_document(url, 'wechat', root)
        result.update(status='ok', text='Body', images=[{'status': 'ok', 'path': 'poster.png'}])
        return result

    monkeypatch.setattr(collector, 'collect', collect)
    options = {}
    result = asyncio.run(AppTools().wechat(URL, tmp_path, options))
    assert result['text'] == 'Body' and result['images']
    assert options == {}


def test_collector_validation_remains_permanent(tmp_path, monkeypatch, collector):
    async def collect(url, root, options=None):
        assert options['max_images'] == 101
        raise ValueError('strict collector validation')

    monkeypatch.setattr(collector, 'collect', collect)
    result = asyncio.run(AdapterRegistry().acquire(URL, tmp_path, {'max_images': 101}))
    assert result['error_kind'] == 'invalid_configuration'
    assert result['retryable'] is False
    with pytest.raises(ValueError):
        asyncio.run(AppTools().wechat(URL, tmp_path, {'max_images': 101}))


def test_wechat_catalog_matches_public_collector_options():
    options = tool_catalog()['wechat']['parameters']['options']['properties']
    assert set(options) == {'max_images', 'timeout', 'interval', 'max_requests', 'browser_channel',
                            'max_html_bytes', 'max_image_bytes', 'max_total_image_bytes', 'images_only'}
    assert {key: value['default'] for key, value in options.items()} == {
        'max_images': 30, 'timeout': 20, 'interval': 15, 'max_requests': 80,
        'browser_channel': 'chrome', 'max_html_bytes': 5 * 1024**2,
        'max_image_bytes': 10 * 1024**2, 'max_total_image_bytes': 30 * 1024**2, 'images_only': False}


def test_real_collector_defaults_match_catalog_without_network():
    from jobprep.app.wechat_public import validate_options

    defaults = validate_options()
    catalog = tool_catalog()['wechat']['parameters']['options']['properties']
    assert defaults == {key: value['default'] for key, value in catalog.items()}
    assert defaults['images_only'] is False
