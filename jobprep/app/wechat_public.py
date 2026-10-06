"""Bounded public WeChat acquisition; no native UI, credentials or proxy changes."""

import asyncio
from contextlib import closing, suppress
from copy import deepcopy
import hashlib
from html import escape
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import httpx
from PIL import Image

from jobprep.html_extract import BLOCKED_RE, DELETED_RE
from .tools import directory, empty_document, image_dimensions, save_bytes

VERSION = 'public-wechat-v1'
# The runner enforces a finite batch (20 by default) and a 10-second start interval.
MAX_NEW_ARTICLES_24H = 20
SESSION_KEYS = {'key', 'uin', 'pass_ticket', 'exportkey', 'token', 'access_token', 'auth', 'cookie'}
IDENTITY_KEYS = {'__biz', 'mid', 'idx', 'sn'}
CHALLENGE_PATH = re.compile(r'captcha|verify|challenge|wappoc', re.I)
EXTRA_BLOCKED = re.compile(r'\u9a8c\u8bc1\u7801|\u8bbf\u95ee\u5f02\u5e38|\u8bbf\u95ee\u53d7\u9650|captcha|access denied', re.I)
FORMATS = {'JPEG': '.jpg', 'PNG': '.png', 'GIF': '.gif', 'WEBP': '.webp'}
IMAGE_HOST_PATHS = {
    'mmbiz.qpic.cn': ('/mmbiz_', '/sz_mmbiz_', '/mmbiz/', '/sz_mmbiz/'),
    'mmecoa.qpic.cn': ('/mmecoa_', '/sz_mmecoa_', '/mmecoa/', '/sz_mmecoa/'),
}
DEFAULTS = {'max_images': 30, 'timeout': 20, 'interval': 15, 'max_requests': 80,
            'browser_channel': 'chrome', 'max_html_bytes': 5 * 1024**2,
            'max_image_bytes': 10 * 1024**2, 'max_total_image_bytes': 30 * 1024**2,
            'images_only': False}


def new_article_budget_limit():
    """Return the default cap or an explicit one-process operator override."""
    raw = os.environ.get('JOBPREP_WECHAT_MAX_NEW_ARTICLES_24H')
    if raw is None:
        return MAX_NEW_ARTICLES_24H
    try:
        value = int(raw)
    except ValueError:
        return MAX_NEW_ARTICLES_24H
    return value if 1 <= value <= 100 else MAX_NEW_ARTICLES_24H


def public_url(value):
    """Normalize stable article identity and reject supplied session credentials."""
    if not isinstance(value, str) or len(value) > 8192 or any(ord(x) <= 32 for x in value) or '\\' in value:
        raise ValueError('A public WeChat article URL is required')
    try:
        parts = urlsplit(value)
        pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)
        port = parts.port
    except ValueError:
        raise ValueError('Malformed article URL') from None
    if (parts.scheme not in ('https', 'http') or (parts.hostname or '').lower() != 'mp.weixin.qq.com'
            or parts.username is not None or parts.password is not None or port not in (None, 80, 443)):
        raise ValueError('Only public mp.weixin.qq.com article URLs are supported')
    if any(key.lower() in SESSION_KEYS for key, _ in pairs):
        raise ValueError('Use a public article link, not a signed browser-session URL')
    if re.fullmatch(r'/s/[A-Za-z0-9_-]{4,200}', parts.path):
        return 'https://mp.weixin.qq.com' + parts.path
    if parts.path != '/s':
        raise ValueError('Only /s article routes are supported')
    identity = {key: value for key, value in pairs if key in IDENTITY_KEYS}
    if len(identity) != sum(key in IDENTITY_KEYS for key, _ in pairs):
        raise ValueError('Duplicate article identity parameters')
    if not identity.get('__biz') or not identity.get('mid', '').isdigit() or not identity.get('idx', '1').isdigit():
        raise ValueError('Long article links need __biz and numeric mid/idx')
    if not re.fullmatch(r'[A-Za-z0-9_=+/-]{1,256}', identity['__biz']):
        raise ValueError('Invalid public article identity')
    if 'sn' in identity and not re.fullmatch(r'[A-Za-z0-9]{1,128}', identity['sn']):
        raise ValueError('Invalid article signature')
    return urlunsplit(('https', 'mp.weixin.qq.com', '/s', urlencode(sorted(identity.items())), ''))


def validate_options(options=None):
    if options is not None and not isinstance(options, dict):
        raise ValueError('WeChat options must be an object')
    options = options or {}
    unknown = set(options) - set(DEFAULTS)
    if unknown:
        raise ValueError('Unsupported WeChat option names: ' + ', '.join(sorted(unknown)))
    result = {**DEFAULTS, **deepcopy(options)}
    limits = {'max_images': (0, 100), 'max_requests': (1, 200),
              'max_html_bytes': (1, 10 * 1024**2), 'max_image_bytes': (1, 10 * 1024**2),
              'max_total_image_bytes': (1, 30 * 1024**2)}
    for key, (low, high) in limits.items():
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f'{key} must be an integer between {low} and {high}')
    for key, low, high in [('timeout', 1, 120), ('interval', 0, 300)]:
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{key} must be a finite number between {low} and {high}')
    if type(result['images_only']) is not bool:
        raise ValueError('images_only must be boolean')
    if result['browser_channel'] not in ('chrome', 'msedge', None):
        raise ValueError('browser_channel must be chrome, msedge or null')
    return result


def public_image_url(value):
    try:
        parts = urlsplit(value)
        port = parts.port
        pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=50)
    except ValueError:
        raise ValueError('invalid_public_image_url') from None
    prefixes = IMAGE_HOST_PATHS.get(parts.hostname, ())
    if (parts.scheme != 'https' or not prefixes or port not in (None, 443)
            or parts.username is not None or parts.password is not None or parts.fragment
            or not parts.path.startswith(prefixes)):
        raise ValueError('unsupported_public_image_host_or_path')
    for key, val in pairs:
        if key not in ('wx_fmt', 'wxfrom', 'wx_lazy', 'wx_co', 'from', 'tp') or not re.fullmatch(r'[A-Za-z0-9_-]{1,20}', val):
            raise ValueError('unsupported_public_image_query')
    return value


class CaptureLedger:
    """Reservations survive crashes and never authorize an automatic second visit."""

    def __init__(self, root, url, options):
        self.root, self.url, self.options = root, url, options
        self.key = hashlib.sha256((VERSION + ':' + url).encode()).hexdigest()
        self.state = directory(root / '.wechat_public')
        self.output = directory(self.state / self.key)
        if not self.state.is_relative_to(root) or not self.output.is_relative_to(root):
            raise ValueError('Capture directory must remain inside the artifact root')
        self.db_path = self.state / 'captures.sqlite3'
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS captures (key TEXT PRIMARY KEY, url TEXT NOT NULL, state TEXT NOT NULL, requests INTEGER NOT NULL, scheduled REAL NOT NULL, result_path TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS pacing (id INTEGER PRIMARY KEY CHECK(id=1), scheduled REAL NOT NULL)')

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def reserve(self):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state,result_path FROM captures WHERE key=?', (self.key,)).fetchone()
            if row:
                return False, row, 0
            last = db.execute('SELECT scheduled FROM pacing WHERE id=1').fetchone()
            now = time.time()
            count = db.execute('SELECT COUNT(*) FROM captures WHERE scheduled>=?', (now - 86400,)).fetchone()[0]
            if count >= new_article_budget_limit():
                raise ValueError('new_article_24h_budget_exhausted')
            scheduled = max(now, last[0] + self.options['interval']) if last else now
            db.execute('INSERT INTO captures VALUES (?,?,?,0,?,NULL)', (self.key, self.url, 'reserved', scheduled))
            db.execute('INSERT INTO pacing VALUES (1,?) ON CONFLICT(id) DO UPDATE SET scheduled=excluded.scheduled', (scheduled,))
            return True, None, max(0, scheduled - now)

    def request(self):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('SELECT requests FROM captures WHERE key=?', (self.key,)).fetchone()[0]
            if count >= self.options['max_requests']:
                raise ValueError('request_budget_exhausted')
            db.execute('UPDATE captures SET requests=requests+1,state=? WHERE key=?', ('running', self.key))
            return count + 1

    def count(self):
        with closing(self.connect()) as db:
            return db.execute('SELECT requests FROM captures WHERE key=?', (self.key,)).fetchone()[0]

    def finish(self, result):
        target = self.output / 'document.json'
        temporary = self.output / 'document.tmp'
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(target)
        with closing(self.connect()) as db, db:
            db.execute('UPDATE captures SET state=?,result_path=? WHERE key=?', (result['status'], str(target), self.key))

    def cached(self, row):
        result = empty_document(self.url, 'wechat-public', self.root)
        result.update(error_kind='reservation_consumed', error='Previous attempt has no reusable result; no automatic revisit')
        try:
            path = Path(row[1]).resolve()
            if not path.is_relative_to(self.output) or path.stat().st_size > 10 * 1024**2:
                raise ValueError('Unsafe cache path')
            loaded = json.loads(path.read_text(encoding='utf-8'))
            if loaded.get('url') != self.url or loaded.get('status') not in ('ok', 'partial', 'blocked', 'deleted', 'error'):
                raise ValueError('Invalid cached result')
            if not isinstance(loaded.get('images'), list) or any(not isinstance(item, dict) for item in loaded['images']):
                raise ValueError('Invalid cached image schema')
            for item in loaded['images']:
                if not item.get('path'):
                    continue
                image_path = Path(item['path']).resolve()
                if not image_path.is_relative_to(self.output) or image_path.stat().st_size > self.options['max_image_bytes']:
                    raise ValueError('Invalid cached image')
                if hashlib.sha256(image_path.read_bytes()).hexdigest() != item['sha256']:
                    raise ValueError('Changed cached image')
            result = loaded
            if result.get('html_path'):
                html_path = Path(result['html_path']).resolve()
                if not html_path.is_relative_to(self.output) or html_path.stat().st_size > 10 * 1024**2:
                    raise ValueError('Invalid cached article path')
                if hashlib.sha256(html_path.read_bytes()).hexdigest() != result.get('html_sha256'):
                    raise ValueError('Changed cached article')
        except (TypeError, ValueError, OSError, KeyError):
            result = empty_document(self.url, 'wechat-public', self.root)
            result.update(error_kind='reservation_consumed', error='Previous attempt has no reusable result; no automatic revisit')
            result['warnings'].append('Cache is missing or changed; retained reservation prevents new requests')
        result.update(cache_hit=True, requests_this_run=0, retryable=False)
        return result


def parse_article(html, url, http_status=200):
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script,style,noscript,template'):
        node.decompose()
    text = soup.get_text('\n', strip=True)
    if http_status in (404, 410) or DELETED_RE.search(text):
        return {'status': 'deleted', 'reason': 'deleted', 'images': []}
    if http_status in (401, 403, 429) or BLOCKED_RE.search(text) or EXTRA_BLOCKED.search(text):
        return {'status': 'blocked', 'reason': 'wechat_challenge', 'images': []}
    title, body = soup.select_one('#activity-name'), soup.select_one('#js_content')
    if http_status != 200 or title is None or not title.get_text(strip=True) or body is None:
        return {'status': 'blocked', 'reason': 'normal_article_not_verified', 'images': []}
    images = []
    for node in body.select('img'):
        raw = next((node.get(key) for key in ('data-src', 'data-original', 'data-lazy-src', 'src') if node.get(key)), '')
        if not raw or raw.startswith('data:'):
            continue
        raw = urljoin(url, raw.strip())
        parts = urlsplit(raw)
        if parts.scheme == 'http' and parts.hostname in IMAGE_HOST_PATHS:
            raw = urlunsplit(('https', parts.netloc, parts.path, parts.query, parts.fragment))
        images.append(raw)
    links = []
    for node in body.select('a[href]'):
        href = node['href']
        parts = urlsplit(href)
        if parts.scheme in ('https', 'http') and parts.hostname and parts.username is None and parts.password is None:
            if not any(key.lower() in SESSION_KEYS for key, _ in parse_qsl(parts.query)):
                links.append({'url': href, 'text': node.get_text(' ', strip=True), 'kind': 'observed_article_link'})
    return {'status': 'ok', 'title': title.get_text(' ', strip=True), 'text': body.get_text('\n', strip=True),
            'images': list(dict.fromkeys(images)), 'image_nodes': len(images), 'links': links, 'reason': 'normal_article_verified'}


async def capture_article(url, ledger, options):
    """Fresh Chromium context; request-stage redirect and media interception."""
    from playwright.async_api import async_playwright

    report = {'navigation_calls': 0, 'document_requests_allowed': 0, 'requests_blocked': 0,
              'media_downloads': 0, 'system_proxy_modified': False, 'existing_profile_used': False}
    fatal = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel=options['browser_channel'], headless=True,
                                                    timeout=options['timeout'] * 1000,
                                                    args=['--no-proxy-server'],
                                                    ignore_default_args=['--disable-popup-blocking'])
        try:
            context = await browser.new_context(accept_downloads=False, service_workers='block')
            await context.route_web_socket('**/*', lambda socket: socket.close())
            page = await context.new_page()
            session = await context.new_cdp_session(page)

            async def intercept(event):
                nonlocal fatal
                try:
                    request = event['request']
                    parsed = urlsplit(request['url'])
                    kind = event.get('resourceType')
                    allowed = (parsed.scheme == 'https' and parsed.hostname == 'mp.weixin.qq.com'
                               and parsed.port in (None, 443) and request['method'] == 'GET'
                               and kind not in ('Media', 'Image')
                               and not re.search(r'\.(mp4|mp3|m3u8|m4a|aac|ogg|wav|webm)(?:$|\?)', request['url'], re.I))
                    if CHALLENGE_PATH.search(parsed.path):
                        fatal = 'wechat_challenge'
                    if kind == 'Document':
                        if request['url'] != url or report['document_requests_allowed']:
                            fatal = fatal or 'additional_navigation_blocked'
                        elif allowed:
                            report['document_requests_allowed'] += 1
                    if fatal or not allowed:
                        report['requests_blocked'] += 1
                        await session.send('Fetch.failRequest', {'requestId': event['requestId'], 'errorReason': 'BlockedByClient'})
                    else:
                        try:
                            ledger.request()
                        except ValueError:
                            fatal = 'request_budget_exhausted'
                            await session.send('Fetch.failRequest', {'requestId': event['requestId'], 'errorReason': 'BlockedByClient'})
                        else:
                            await session.send('Fetch.continueRequest', {'requestId': event['requestId']})
                except Exception:
                    fatal = fatal or 'request_interception_failed'

            session.on('Fetch.requestPaused', intercept)
            await session.send('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]})
            report['navigation_calls'] = 1
            try:
                response = await page.goto(url, wait_until='domcontentloaded', timeout=options['timeout'] * 1000)
            except Exception:
                raise ValueError(fatal or 'article_navigation_failed') from None
            if fatal:
                raise ValueError(fatal)
            code = response.status if response else None
            html = await page.content()
            if len(html.encode('utf-8')) > options['max_html_bytes']:
                raise ValueError('html_byte_limit')
            return html, code, report
        finally:
            with suppress(Exception):
                await asyncio.wait_for(browser.close(), timeout=5)


async def download_image(url, source_url, ledger, options, remaining):
    """One anonymous public image GET; no cookie replay, redirects or retries."""
    public_image_url(url)
    ledger.request()
    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                timeout=httpx.Timeout(options['timeout'], connect=min(10, options['timeout']))) as client:
        request = httpx.Request('GET', url, headers={'Accept': 'image/*', 'Accept-Encoding': 'identity', 'Referer': source_url})
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            if response.status_code != 200:
                raise ValueError('image_http_failure')
            if response.headers.get('content-encoding', 'identity') != 'identity':
                raise ValueError('encoded_image_response')
            if response.headers.get('content-type', '').split(';')[0].lower() not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
                raise ValueError('non_image_response')
            capacity = min(remaining, options['max_image_bytes'])
            declared = response.headers.get('content-length')
            if declared and (not declared.isdigit() or int(declared) > capacity):
                raise ValueError('image_byte_limit')
            chunks, size = [], 0
            async for chunk in response.aiter_raw():
                size += len(chunk)
                if size > capacity:
                    raise ValueError('image_byte_limit')
                chunks.append(chunk)
            if declared and size != int(declared):
                raise ValueError('truncated_image')
            body = b''.join(chunks)
        finally:
            await response.aclose()
    width, height = image_dimensions(body)
    with Image.open(io.BytesIO(body)) as image:
        suffix = FORMATS.get(image.format)
        if suffix is None:
            raise ValueError('unsupported_image_format')
        image.load()
    return body, suffix, width, height


async def collect(url, artifact_dir, options=None):
    url, options = public_url(url), validate_options(options)
    root = directory(artifact_dir)
    ledger = CaptureLedger(root, url, options)
    try:
        reserved, existing, delay = ledger.reserve()
    except ValueError:
        result = empty_document(url, 'wechat-public', root)
        result.update(error_kind='new_article_24h_budget_exhausted', requests_this_run=0)
        result['warnings'].append('Rolling 24-hour new-article budget exhausted; no request was made')
        return result
    if not reserved:
        return _projection(ledger.cached(existing), options)
    result = empty_document(url, 'wechat-public', root)
    result.update(cache_hit=False, requests_this_run=0, retryable=False,
                  public_capture_version=VERSION, capture_dir=str(ledger.output))
    try:
        await asyncio.sleep(delay)
        html, code, browser_report = await capture_article(url, ledger, options)
        result.update(http_status=code, browser_capture=browser_report)
        parsed = parse_article(html, url, code)
        result['coverage'].update(pages_seen=1, stop_reason=parsed['reason'])
        if parsed['status'] != 'ok':
            result.update(status=parsed['status'], error_kind=parsed['reason'])
            result['warnings'].append('Public article unavailable; no image download, native-browser fallback or automatic retry')
        else:
            result.update(status='partial', title=parsed['title'], text=parsed['text'], links=parsed['links'])
            result['coverage'].update(image_nodes=parsed['image_nodes'], unique_images_reported=len(parsed['images']),
                                      image_complete=False, stop_reason='article_acquired_jd_coverage_unverified')
            manifest = []
            for reference in parsed['images']:
                parts = urlsplit(reference)
                entry = {'host': parts.hostname, 'path_prefix': parts.path.split('/')[1] if '/' in parts.path else '',
                         'scheme': parts.scheme}
                try:
                    entry['public_image_url'] = public_image_url(reference)
                except ValueError:
                    entry['status'] = 'unsupported_reference'
                manifest.append(entry)
            manifest_path = ledger.output / 'image_references.json'
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2), encoding='utf-8')
            result['evidence'].append({'kind': 'public_image_manifest', 'path': str(manifest_path)})
            total_bytes, seen_hashes = 0, {}
            for order, image_url in enumerate(parsed['images']):
                image = {'url': url, 'path': '', 'sha256': '', 'status': 'pending_image', 'order': order}
                image['source_host'] = manifest[order]['host']
                if manifest[order].get('public_image_url'):
                    image['public_image_url'] = manifest[order]['public_image_url']
                result['images'].append(image)
                if order >= options['max_images']:
                    image.update(status='image_limit', error='image_count_limit')
                    continue
                try:
                    body, suffix, width, height = await download_image(image_url, url, ledger, options,
                                                                    options['max_total_image_bytes'] - total_bytes)
                    total_bytes += len(body)
                    digest = hashlib.sha256(body).hexdigest()
                    if digest in seen_hashes:
                        image.update(status='skipped_duplicate', sha256=digest, duplicate_of=seen_hashes[digest])
                    else:
                        target, digest = save_bytes(ledger.output, body, suffix)
                        image.update(status='ok', path=target, sha256=digest, width=width, height=height)
                        seen_hashes[digest] = order
                        result['evidence'].append({'kind': 'image', 'path': target, 'sha256': digest, 'order': order})
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    image.update(status='image_failed', error=reason)
                    result['warnings'].append('Image acquisition stopped without retry; unresolved images remain explicit')
                    for later in range(order + 1, len(parsed['images'])):
                        result['images'].append({'url': url, 'path': '', 'sha256': '', 'status': 'not_requested_after_failure', 'order': later})
                    break
            result['coverage']['image_complete'] = all(item['status'] in ('ok', 'skipped_duplicate') for item in result['images'])
            result['coverage']['images_received_bytes'] = total_bytes
            localized = ['<!doctype html><meta charset="utf-8">', '<title>' + escape(result['title']) + '</title>',
                         '<h1>' + escape(result['title']) + '</h1>', '<div id="js_content"><pre>' + escape(result['text']) + '</pre>']
            for image in result['images']:
                if image.get('path'):
                    localized.append('<img src="' + escape(Path(image['path']).name) + '">')
            localized.append('</div>')
            path = ledger.output / 'article.html'
            path.write_text('\n'.join(localized), encoding='utf-8')
            result.update(html_path=str(path), html_paths=[str(path)],
                          html_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            result['evidence'].append({'kind': 'localized_article', 'path': str(path)})
            result['warnings'].append('Public article acquired; company-wide job list and linked JD completeness remain unverified')
    except asyncio.CancelledError:
        result.update(status='error', error_kind='cancelled', error='Cancelled capture; reservation retained')
        result['requests_this_run'] = ledger.count()
        ledger.finish(result)
        raise
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        status = 'blocked' if reason in ('wechat_challenge', 'additional_navigation_blocked') else 'error'
        result.update(status=status, error_kind=reason, error='Public capture stopped; inspect local status and limits')
        result['coverage']['stop_reason'] = reason
        result['warnings'].append('No automatic network retry or GUI fallback')
    result['requests_this_run'] = ledger.count()
    ledger.finish(result)
    return _projection(result, options)


def _projection(result, options):
    result = deepcopy(result)
    if options['images_only']:
        result['text'] = ''
        result['coverage']['text_omitted'] = True
        result['warnings'].append('images_only omitted article text by explicit request')
    return result
