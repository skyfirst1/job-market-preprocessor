import asyncio
import io
import json
from pathlib import Path

import httpx
from PIL import Image
import pytest

from jobprep.app import wechat_public as module

URL = 'https://mp.weixin.qq.com/s/public_article'
IMAGE = 'https://mmbiz.qpic.cn/mmbiz_png/poster/0?wx_fmt=png'
HTML = '<h1 id="activity-name">Campus recruiting</h1><div id="js_content">Agent engineer<img data-src="' + IMAGE + '"></div>'


def run(value):
    return asyncio.run(value)


def image_bytes():
    output = io.BytesIO()
    Image.new('RGB', (60, 60), 'white').save(output, 'PNG')
    return output.getvalue()


@pytest.fixture
def acquisition(monkeypatch):
    calls = {'capture': 0, 'images': 0}

    async def capture(url, ledger, options):
        calls['capture'] += 1
        ledger.request()
        return HTML, 200, {'navigation_calls': 1}

    async def download(url, source, ledger, options, remaining):
        module.public_image_url(url)
        calls['images'] += 1
        ledger.request()
        return image_bytes(), '.png', 60, 60

    monkeypatch.setattr(module, 'capture_article', capture)
    monkeypatch.setattr(module, 'download_image', download)
    return calls


@pytest.mark.parametrize('url', ['https://evil.test/s/article', 'file:///tmp/x',
    'https://mp.weixin.qq.com/history', 'https://user@mp.weixin.qq.com/s/article',
    'https://mp.weixin.qq.com:444/s/article', 'https://mp.weixin.qq.com/s/article?key=secret',
    'https://mp.weixin.qq.com/s/article?pass_ticket=secret', 'https://mp.weixin.qq.com/s?mid=1',
    'https://mp.weixin.qq.com/s/article\n'])
def test_invalid_public_urls(url):
    with pytest.raises(ValueError):
        module.public_url(url)


def test_public_url_normalization():
    assert module.public_url('http://MP.WEIXIN.QQ.COM/s/article?from=share#read') == 'https://mp.weixin.qq.com/s/article'
    assert module.public_url('https://mp.weixin.qq.com/s?__biz=abcd&mid=123&idx=1&sn=abc&scene=2') == 'https://mp.weixin.qq.com/s?__biz=abcd&idx=1&mid=123&sn=abc'


@pytest.mark.parametrize('options', [{'max_images': True}, {'max_requests': 0},
    {'max_images': 101}, {'interval': float('nan')}, {'timeout': False},
    {'browser_channel': 'stealth'}, {'images_only': 'true'}, {'cookies': 'secret'}])
def test_invalid_options(options):
    with pytest.raises(ValueError):
        module.validate_options(options)


@pytest.mark.parametrize('url', ['https://example.test/img.jpg',
    'https://mmbiz.qpic.cn.evil.test/mmbiz_png/a', 'https://mmbiz.qpic.cn/mmbiz_png/a?key=secret',
    'http://mmbiz.qpic.cn/mmbiz_png/a', 'https://u:p@mmbiz.qpic.cn/mmbiz_png/a',
    'https://mmbiz.qpic.cn:bad/mmbiz_png/a'])
def test_image_url_allowlist(url):
    with pytest.raises(ValueError):
        module.public_image_url(url)


def test_lazy_images_extracted_not_placeholder():
    parsed = module.parse_article(HTML.replace('<img data-src=', '<img src="data:image/svg+xml,placeholder" data-src='), URL)
    assert parsed['images'] == [IMAGE]


def test_protocol_relative_public_cdn_is_normalized():
    parsed = module.parse_article(HTML.replace(IMAGE, IMAGE.replace('https:', '')), URL)
    assert parsed['images'] == [IMAGE]


def test_older_cdn_path_and_http_upgrade():
    older = IMAGE.replace('mmbiz_png/', 'mmbiz/').replace('https:', 'http:')
    parsed = module.parse_article(HTML.replace(IMAGE, older), URL)
    assert module.public_image_url(parsed['images'][0]).startswith('https://mmbiz.qpic.cn/mmbiz/')


def test_observed_mmecoa_cdn_is_supported():
    image = 'https://mmecoa.qpic.cn/sz_mmecoa_png/poster/0?wx_fmt=png&from=appmsg'
    assert module.public_image_url(image) == image


def test_cdns_are_exact_hosts_not_wildcard():
    with pytest.raises(ValueError):
        module.public_image_url('https://evil.qpic.cn/sz_mmecoa_png/x/0?wx_fmt=png')


@pytest.mark.parametrize('status,html,expected', [(403, HTML, 'blocked'),
    (200, '<h1>Access denied</h1>', 'blocked'), (404, HTML, 'deleted'),
    (200, '<h1>empty shell</h1>', 'blocked')])
def test_abnormal_page_never_success(status, html, expected):
    assert module.parse_article(html, URL, status)['status'] == expected


def test_full_acquisition_and_cache(tmp_path, acquisition):
    result = run(module.collect(URL, tmp_path, {'interval': 0}))
    assert result['status'] == 'partial'
    assert result['text'] == 'Agent engineer'
    assert result['coverage']['image_complete']
    assert result['requests_this_run'] == 2
    assert Path(result['images'][0]['path']).is_relative_to(tmp_path)
    assert not result['coverage']['complete']
    cached = run(module.collect(URL, tmp_path, {'interval': 0}))
    assert cached['cache_hit'] and cached['requests_this_run'] == 0
    assert acquisition == {'capture': 1, 'images': 1}


def test_images_only_is_projection_not_lost_cache_text(tmp_path, acquisition):
    projected = run(module.collect(URL, tmp_path, {'interval': 0, 'images_only': True}))
    assert projected['text'] == ''
    cached = run(module.collect(URL, tmp_path, {'interval': 0}))
    assert cached['text'] == 'Agent engineer'
    assert acquisition['capture'] == 1


def test_changed_cached_image_stops_without_revisit(tmp_path, acquisition):
    result = run(module.collect(URL, tmp_path, {'interval': 0}))
    Path(result['images'][0]['path']).write_bytes(b'changed')
    cached = run(module.collect(URL, tmp_path, {'interval': 0}))
    assert cached['status'] == 'error' and cached['requests_this_run'] == 0
    assert acquisition['capture'] == 1


def test_changed_cached_html_stops_without_revisit(tmp_path, acquisition):
    result = run(module.collect(URL, tmp_path, {'interval': 0}))
    Path(result['html_path']).write_text('changed')
    cached = run(module.collect(URL, tmp_path, {'interval': 0}))
    assert cached['status'] == 'error'
    assert acquisition['capture'] == 1


def test_existing_unfinished_gate_never_revisited(tmp_path, acquisition):
    ledger = module.CaptureLedger(tmp_path, URL, module.validate_options({'interval': 0}))
    assert ledger.reserve()[0]
    result = run(module.collect(URL, tmp_path, {'interval': 0}))
    assert result['status'] == 'error' and result['requests_this_run'] == 0
    assert acquisition['capture'] == 0


def test_same_url_concurrent_reservation_only_one(tmp_path):
    a = module.CaptureLedger(tmp_path, URL, module.validate_options())
    b = module.CaptureLedger(tmp_path, URL, module.validate_options())
    assert a.reserve()[0]
    assert not b.reserve()[0]


def test_rolling_article_cap(tmp_path, acquisition):
    for index in range(module.MAX_NEW_ARTICLES_24H):
        run(module.collect(URL + str(index), tmp_path, {'interval': 0}))
    result = run(module.collect(URL + 'next', tmp_path, {'interval': 0}))
    assert result['error_kind'] == 'new_article_24h_budget_exhausted'
    assert result['requests_this_run'] == 0
    assert acquisition['capture'] == module.MAX_NEW_ARTICLES_24H


def test_request_budget_stops_images(tmp_path, acquisition):
    result = run(module.collect(URL, tmp_path, {'interval': 0, 'max_requests': 1}))
    assert result['requests_this_run'] == 1
    assert result['images'][0]['status'] == 'image_failed'
    assert not result['retryable']


def test_image_cap_explicit_gaps(tmp_path, acquisition):
    result = run(module.collect(URL, tmp_path, {'interval': 0, 'max_images': 0}))
    assert result['images'][0]['status'] == 'image_limit'
    assert not result['coverage']['image_complete']
    assert acquisition['images'] == 0


def test_blocked_page_no_download_or_retry(tmp_path, acquisition, monkeypatch):
    async def capture(url, ledger, options):
        acquisition['capture'] += 1
        ledger.request()
        return '<h1>Access denied</h1>', 403, {}
    monkeypatch.setattr(module, 'capture_article', capture)
    result = run(module.collect(URL, tmp_path, {'interval': 0}))
    assert result['status'] == 'blocked' and not result['retryable']
    assert acquisition['images'] == 0
    run(module.collect(URL, tmp_path, {'interval': 0}))
    assert acquisition['capture'] == 1


def test_failure_retains_unrequested_image_evidence(tmp_path, acquisition, monkeypatch):
    async def capture(url, ledger, options):
        ledger.request()
        return HTML.replace('</div>', '<img data-src="' + IMAGE.replace('poster', 'another') + '"></div>'), 200, {}
    async def download(*args):
        acquisition['images'] += 1
        args[2].request()
        raise httpx.ConnectError('not serialized')
    monkeypatch.setattr(module, 'capture_article', capture)
    monkeypatch.setattr(module, 'download_image', download)
    result = run(module.collect(URL, tmp_path, {'interval': 0}))
    assert len(result['images']) == 2
    assert result['images'][1]['status'] == 'not_requested_after_failure'
    assert acquisition['images'] == 1


def test_cancelled_capture_consumes_reservation(tmp_path, acquisition, monkeypatch):
    async def capture(*args):
        acquisition['capture'] += 1
        raise asyncio.CancelledError()
    monkeypatch.setattr(module, 'capture_article', capture)
    with pytest.raises(asyncio.CancelledError):
        run(module.collect(URL, tmp_path, {'interval': 0}))
    result = run(module.collect(URL, tmp_path, {'interval': 0}))
    assert result['status'] == 'error' and result['requests_this_run'] == 0
    assert acquisition['capture'] == 1


def test_image_transport_is_cookie_free_and_bounded(tmp_path, monkeypatch):
    seen = []
    body = image_bytes()

    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={'content-type': 'image/png', 'content-length': str(len(body))},
                              stream=httpx.ByteStream(body))

    monkeypatch.setattr(module.httpx, 'AsyncHTTPTransport', lambda **kw: httpx.MockTransport(handler))
    options = module.validate_options({'interval': 0})
    ledger = module.CaptureLedger(tmp_path, URL, options)
    ledger.reserve()
    data, suffix, width, height = run(module.download_image(IMAGE, URL, ledger, options, 10000))
    assert data == body and suffix == '.png' and (width, height) == (60, 60)
    assert 'cookie' not in seen[0].headers and 'authorization' not in seen[0].headers
    assert ledger.count() == 1


@pytest.mark.parametrize('status,headers,body', [
    (302, {'location': 'http://127.0.0.1/private'}, b''),
    (200, {'content-type': 'video/mp4'}, b'video'),
    (200, {'content-type': 'image/png', 'content-length': '99999999'}, b''),
    (200, {'content-type': 'image/png', 'content-encoding': 'gzip'}, b''),
])
def test_image_transport_rejects_redirects_media_and_unbounded_body(tmp_path, monkeypatch, status, headers, body):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))

    monkeypatch.setattr(module.httpx, 'AsyncHTTPTransport', lambda **kw: httpx.MockTransport(handler))
    options = module.validate_options({'interval': 0})
    ledger = module.CaptureLedger(tmp_path, URL, options)
    ledger.reserve()
    with pytest.raises(ValueError):
        run(module.download_image(IMAGE, URL, ledger, options, 10000))
    assert len(seen) == 1
