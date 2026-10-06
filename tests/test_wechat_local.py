import asyncio
import json

import httpx
import pytest

from jobprep.app.wechat_local import LocalWechatDownload

URL = 'https://mp.weixin.qq.com/s/approved'


def transport(calls, failure=False):
    def handle(request):
        assert str(request.url) == 'http://127.0.0.1:4545/mcp'
        payload = json.loads(request.content)
        calls.append(payload)
        method = payload['method']
        if method == 'tools/call' and failure:
            raise httpx.ReadTimeout('private token')
        result = {'tools': [{'name': 'single_article_download', 'inputSchema': {
            'type': 'object', 'properties': {'url': {'type': 'string'}}, 'required': ['url']}}]} if method == 'tools/list' else {}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': payload['id'], 'result': result})
    return httpx.MockTransport(handle)


def test_preflight_never_downloads(tmp_path):
    calls = []
    pilot = LocalWechatDownload(URL, tmp_path / 'ledger.db', transport=transport(calls))
    assert asyncio.run(pilot.run())['status'] == 'ready'
    assert [call['method'] for call in calls] == ['initialize', 'tools/list']
    assert pilot.status()['download_calls_used'] == 0


@pytest.mark.parametrize('failure', [False, True])
def test_one_durable_call_even_after_timeout(tmp_path, failure):
    calls = []
    path = tmp_path / 'ledger.db'
    pilot = LocalWechatDownload(URL, path, transport=transport(calls, failure))
    result = asyncio.run(pilot.run(execute=True))
    assert result['download_calls_used'] == 1
    assert 'private token' not in json.dumps(result)
    assert len([call for call in calls if call['method'] == 'tools/call']) == 1
    assert calls[-1]['params']['arguments'] == {'url': URL}
    reopened = LocalWechatDownload(URL, path, transport=transport(calls))
    with pytest.raises(RuntimeError, match='budget exhausted'):
        asyncio.run(reopened.run(execute=True))
    assert len(calls) == 3


def test_no_redirect_follow_or_fallback(tmp_path):
    calls = []
    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'Location': 'https://remote.example/'})
    pilot = LocalWechatDownload(URL, tmp_path / 'ledger.db', transport=httpx.MockTransport(redirect))
    assert asyncio.run(pilot.run(execute=True))['status'] == 'unavailable'
    assert calls == ['http://127.0.0.1:4545/mcp']
    assert pilot.status()['download_calls_used'] == 0


def test_url_is_pinned(tmp_path):
    path = tmp_path / 'ledger.db'
    LocalWechatDownload(URL, path)
    with pytest.raises(ValueError, match='already pinned'):
        LocalWechatDownload('https://mp.weixin.qq.com/s/other', path)


@pytest.mark.parametrize('url', ['http://mp.weixin.qq.com/s/a', 'https://mp.weixin.qq.com.evil/s/a',
                               'https://mp.weixin.qq.com/appmsgalbum', 'https://user:pass@mp.weixin.qq.com/s/a'])
def test_unsupported_url_rejected(tmp_path, url):
    with pytest.raises(ValueError):
        LocalWechatDownload(url, tmp_path / 'ledger.db')
