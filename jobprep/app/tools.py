"""Application tool contracts, with explicit acquisition and OCR boundaries."""

import asyncio
import copy
import hashlib
import io
import json
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from PIL import Image

HTML_SUFFIXES = {'.html', '.htm'}
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff'}
MAX_FILE_BYTES = 30 * 1024**2
MAX_IMAGE_BYTES = 10 * 1024**2
MAX_IMAGE_PIXELS = 40_000_000
MAX_DIMENSION = 20_000
MAX_LOCAL_IMAGES = 100
MAX_LOCAL_IMAGE_BYTES = 30 * 1024**2


def directory(path):
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('source URL must be HTTP(S), with a host and no credentials')
    return url


def read_bounded(path, limit):
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('file is missing or exceeds byte limit')
    with path.open('rb') as handle:
        body = handle.read(limit + 1)
    if len(body) > limit:
        raise ValueError('file exceeds byte limit')
    return body


def image_dimensions(body):
    with Image.open(io.BytesIO(body)) as image:
        width, height = image.size
        if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION) or width * height > MAX_IMAGE_PIXELS:
            raise ValueError('image exceeds dimension or pixel limit')
        image.verify()
    return width, height


def save_bytes(root, body, suffix):
    digest = hashlib.sha256(body).hexdigest()
    target = root / (digest + suffix)
    if not target.resolve().is_relative_to(root):
        raise ValueError('artifact destination escapes root')
    target.write_bytes(body)
    return str(target), digest


def empty_document(url, method, root):
    return {'url': url, 'final_url': url, 'title': '', 'text': '', 'status': 'error',
            'method': method, 'artifact_dir': str(root), 'html_path': '', 'html_paths': [],
            'images': [], 'links': [], 'jobs': [], 'evidence': [], 'warnings': [],
            'retryable': False, 'error_kind': None, 'http_status': None,
            'coverage': {'complete': False, 'list_complete': False, 'jd_complete': False,
                         'pages_seen': 0, 'detail_urls': [], 'stop_reason': 'error'}}


def failure_fields(error):
    import httpx

    message = str(error)
    code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
    match = re.search(r"(?:HTTP(?:StatusError)?[^\n]*?|(?:Client|Server) error\s*)['\"]?\b([45]\d\d)\b", message, re.I)
    if code is None and match:
        code = int(match.group(1))
    if code == 429:
        kind, retryable = 'rate_limited', True
    elif code in (401, 403):
        kind, retryable = 'blocked', False
    elif code in (404, 410):
        kind, retryable = 'deleted', False
    elif code is not None:
        kind, retryable = 'http', code in (408, 425, 500, 502, 503, 504)
    elif 'budget' in message.lower():
        kind, retryable = 'budget', False
    elif isinstance(error, (httpx.TransportError, TimeoutError)) or any(word in message for word in ('TimeoutError', 'ConnectError', 'ReadError', 'NetworkError', 'TransportError')):
        kind, retryable = 'transport', True
    else:
        kind, retryable = 'protocol', False
    return {'retryable': retryable, 'error_kind': kind, 'http_status': code}


def list_document(config, root, output, coverage, method='public-list'):
    result = empty_document(config['url'], method, root)
    jobs_path = output / 'jobs.jsonl'
    jobs = [json.loads(line) for line in jobs_path.read_text(encoding='utf-8').splitlines() if line.strip()]
    coverage = copy.deepcopy(coverage)
    complete = bool(coverage.get('list_complete') and coverage.get('jd_complete'))
    coverage.update(complete=complete, pages_seen=coverage.get('pages_received', 0),
                    detail_urls=list(dict.fromkeys(job['url'] for job in jobs if job.get('needs_details') and job.get('url'))),
                    stop_reason=coverage.get('list_completeness_basis') or 'partial')
    reasons = coverage.get('reasons', [])
    # Accepted empty terminal lists are successful even though JD evidence is absent.
    status = 'ok' if coverage.get('list_complete') else 'partial' if jobs else 'error'
    result.update(status=status, jobs=jobs, coverage=coverage, warnings=list(reasons),
                  json_paths=[page['raw_ref'] for page in coverage.get('pages', []) if page.get('raw_ref')],
                  evidence=copy.deepcopy(coverage.get('pages', [])),
                  links=[{'url': job['url'], 'text': job.get('title') or '', 'kind': 'job',
                          'raw_ref': job['raw_ref'], 'raw_index': job['raw_index']}
                         for job in jobs if job.get('url')],
                  output_paths={'jobs_jsonl': str(jobs_path.resolve()),
                                'jobs_csv': str((output / 'jobs.csv').resolve()),
                                'coverage_json': str((output / 'coverage.json').resolve())})
    run_dir = Path(coverage.get('artifacts_dir', ''))
    if run_dir.is_absolute() and run_dir.resolve().is_relative_to(root):
        # The collector writes unsuccessful attempts before adding coverage.pages.
        for path in sorted(run_dir.glob('page-*')):
            reference = str(path.resolve())
            if path.is_file() and path.resolve().is_relative_to(root) and reference not in result['json_paths']:
                result['json_paths'].append(reference)
                result['evidence'].append({'raw_ref': reference, 'accepted': False, 'kind': 'response_attempt'})
    if status == 'error':
        result['error'] = '; '.join(reasons) or 'List acquisition failed'
    if reasons:
        result.update(failure_fields('; '.join(reasons)))
    return result


class AppTools:
    """Optional artifact_dir pins the trusted root used by ocr()."""

    def __init__(self, artifact_dir=None):
        self.artifact_dir = Path(artifact_dir).resolve() if artifact_dir is not None else None

    async def web(self, url, artifact_dir, options=None):
        from jobprep import crawler

        validate_url(url)
        root = directory(artifact_dir)
        result = await crawler.collect(url, root, copy.deepcopy(options))
        result['artifact_dir'] = str(root)
        result.setdefault('retryable', False)
        result.setdefault('error_kind', None)
        result.setdefault('http_status', None)
        if result.get('error'):
            result.update(failure_fields(result['error']))
            result['error'] = 'Web acquisition failed; see status, error_kind and artifact evidence'
        if result.get('status') in ('blocked', 'deleted'):
            result.update(retryable=False, error_kind=result['status'])
        return result

    async def wechat(self, url, artifact_dir, options=None):
        from . import wechat_public

        validate_url(url)
        if urlsplit(url).hostname != 'mp.weixin.qq.com':
            raise ValueError('wechat requires an mp.weixin.qq.com URL')
        if options is not None and not isinstance(options, dict):
            raise ValueError('options must be an object')
        opts = copy.deepcopy(options or {})
        images_only = opts.get('images_only', False)
        if not isinstance(images_only, bool):
            raise ValueError('images_only must be boolean')
        root = directory(artifact_dir)
        try:
            result = await wechat_public.collect(url, root, opts)
        except ValueError:
            raise
        except Exception as exc:
            result = empty_document(url, 'wechat', root)
            result.update(error=f'{type(exc).__name__}: WeChat acquisition failed',
                          **failure_fields(exc))
            result['warnings'].append(result['error'])
        result.update(method='wechat', artifact_dir=str(root), retryable=False)
        if images_only:
            result['text'] = ''
            if not result.get('coverage', {}).get('text_omitted'):
                result.setdefault('warnings', []).append('images_only omits text; image OCR and semantic coverage are unassessed')
            result.setdefault('coverage', {}).update(complete=False, text_omitted=True)
            if result.get('status') == 'ok':
                result['status'] = 'partial'
        return result

    async def zhiye_list(self, url, artifact_dir, options=None):
        from .zhiye import collect

        validate_url(url)
        host = (urlsplit(url).hostname or '').lower()
        if not host.endswith('.zhiye.com'):
            raise ValueError('zhiye_list requires a public *.zhiye.com URL')
        root = directory(artifact_dir)
        result = await collect(url, root, copy.deepcopy(options))
        result['artifact_dir'] = str(root)
        result.setdefault('retryable', False)
        result.setdefault('error_kind', None)
        result.setdefault('http_status', None)
        return result

    async def wjx_form(self, url, artifact_dir, options=None):
        from .wjx import collect

        validate_url(url)
        return await collect(url, directory(artifact_dir), copy.deepcopy(options))

    async def file(self, path, source_url, kind, artifact_dir):
        validate_url(source_url)
        if kind not in ('html', 'image'):
            raise ValueError('kind must be html or image')
        return await asyncio.to_thread(self._file, path, source_url, kind, artifact_dir)

    @staticmethod
    def _file(path, source_url, kind, artifact_dir):
        from bs4 import BeautifulSoup
        from jobprep.html_extract import extract_html

        path = Path(path).resolve()
        allowed = HTML_SUFFIXES if kind == 'html' else IMAGE_SUFFIXES
        if path.suffix.lower() not in allowed:
            raise ValueError('unsupported file suffix')
        body = read_bounded(path, MAX_FILE_BYTES if kind == 'html' else MAX_IMAGE_BYTES)
        dimensions = image_dimensions(body) if kind == 'image' else None
        root = directory(artifact_dir)
        target, digest = save_bytes(root, body, path.suffix.lower())
        if kind == 'image':
            result = empty_document(source_url, 'manual-image', root)
            result.update(status='ok', title=path.stem,
                          images=[{'url': source_url, 'path': target, 'sha256': digest, 'status': 'ok',
                                   'width': dimensions[0], 'height': dimensions[1], 'order': 0}],
                          evidence=[{'kind': 'image', 'path': target, 'order': 0}])
            result['coverage']['stop_reason'] = 'manual_image_only'
            return result
        html = body.decode('utf-8-sig', errors='replace')
        result = extract_html(html, source_url)
        result.update(html_path=target, html_paths=[target], artifact_dir=str(root), method='manual-html')
        if result['status'] in ('blocked', 'deleted', 'error'):
            return result
        soup = BeautifulSoup(html, 'html.parser')
        scope = soup.select_one('#js_content') or soup.find('article') or soup.find('main') or soup
        total_bytes = 0
        for order, node in enumerate(scope.select('img')):
            raw = next((node.get(key) for key in ('data-src', 'data-original', 'data-lazy-src', 'src') if node.get(key)), '')
            if not raw:
                continue
            source = urljoin(source_url, raw)
            image = next((item for item in result['images'] if item['url'] == source), None)
            if image is None:
                image = {'url': source, 'order': order, 'path': '', 'sha256': ''}
                result['images'].append(image)
            if image.get('path'):
                continue
            image['status'] = 'pending_manual_image'
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc:
                image['error'] = 'remote_image_not_fetched'
                continue
            try:
                relative = Path(unquote(parsed.path))
                if relative.is_absolute() or relative.drive:
                    raise ValueError('absolute_image_path')
                local = (path.parent / relative).resolve()
                if not local.is_relative_to(path.parent) or local.suffix.lower() not in IMAGE_SUFFIXES:
                    raise ValueError('unsafe_image_path_or_suffix')
                if order >= MAX_LOCAL_IMAGES:
                    raise ValueError('local_image_count_limit')
                image_body = read_bounded(local, min(MAX_IMAGE_BYTES, MAX_LOCAL_IMAGE_BYTES - total_bytes))
                width, height = image_dimensions(image_body)
                destination, image_hash = save_bytes(root, image_body, local.suffix.lower())
                total_bytes += len(image_body)
                image.update(path=destination, sha256=image_hash, status='ok', error=None, width=width, height=height)
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                image['error'] = str(exc)
        for image in result['images']:
            if not image.get('path'):
                image['status'] = 'pending_manual_image'
        if result['images']:
            result['coverage']['complete'] = False
            result['status'] = 'partial'
            result['warnings'].append('Manual images require explicit OCR; unresolved images require manual evidence')
        return result

    async def ocr(self, result, engine):
        result = copy.deepcopy(result)
        result['ocr'], result['ocr_gaps'] = [], []
        result['acquisition_status'] = result.get('acquisition_status', result.get('status', 'error'))
        if result.get('status') in ('blocked', 'deleted', 'error'):
            result.setdefault('warnings', []).append('OCR skipped for blocked, deleted or failed acquisition')
            return result
        root_value = self.artifact_dir or result.get('artifact_dir')
        # Legacy pipeline engines keep OCR cache beside data/artifacts.
        if not root_value and getattr(engine, 'cache_dir', None) is not None:
            root_value = Path(engine.cache_dir).resolve().parent / 'artifacts'
        root = Path(root_value).resolve() if root_value else None
        for index, image in enumerate(result.get('images', [])):
            if not image.get('path'):
                if image.get('status') not in ('decorative', 'skipped_duplicate'):
                    result['ocr_gaps'].append({'image': index, 'reason': image.get('error') or image.get('status') or 'missing_image'})
                continue
            try:
                if root is None:
                    raise ValueError('missing_artifact_root')
                path = Path(image['path'])
                path = (path if path.is_absolute() else root / path).resolve()
                if not path.is_relative_to(root) or path.suffix.lower() not in IMAGE_SUFFIXES:
                    raise ValueError('unsafe_artifact_path')
                body = await asyncio.to_thread(read_bounded, path, MAX_IMAGE_BYTES)
                await asyncio.to_thread(image_dimensions, body)
                digest = hashlib.sha256(body).hexdigest()
                if image.get('sha256') and digest != image['sha256']:
                    raise ValueError('artifact_hash_mismatch')
                recognized = dict(await engine.recognize(path))
                recognized.update(image_index=index, image_sha256=image.get('sha256'))
                result['ocr'].append(recognized)
                if recognized.get('status') != 'ok':
                    result['ocr_gaps'].append({'image': index, 'reason': recognized.get('status', 'ocr_error')})
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ValueError) else f'{type(exc).__name__}: OCR operation failed'
                result['ocr_gaps'].append({'image': index, 'reason': reason})
        if result['ocr_gaps']:
            result['coverage'] = {**result.get('coverage', {}), 'complete': False}
            if result.get('status') in ('ok', 'partial', 'pending_ocr'):
                result['status'] = 'pending_ocr'
            statuses = {item.get('status') for item in result['ocr']}
            if any(status and 'budget' in status for status in statuses):
                result.update(retryable=False, error_kind='budget', http_status=None)
        return result

    async def public_list(self, config, artifact_dir):
        from jobprep import list_collect

        config = copy.deepcopy(config)
        list_collect._validate_config(config)
        validate_url(config['url'])
        root = directory(artifact_dir)
        return await asyncio.to_thread(self._public_list, config, root)

    @staticmethod
    def _public_list(config, root):
        from jobprep import list_collect
        from uuid import uuid4

        output = root / ('public-list-' + uuid4().hex)
        try:
            coverage = list_collect.collect(config, output, artifacts_dir=root / 'list_runs')
            return list_document(config, root, output, coverage)
        except Exception as exc:
            result = empty_document(config['url'], 'public-list', root)
            result.update(error=f'{type(exc).__name__}: Public list acquisition failed', **failure_fields(exc))
            result['warnings'].append(result['error'])
            return result

    async def feishu_list(self, url, artifact_dir, options=None):
        from .feishu import collect_feishu, validate_options

        validate_url(url)
        if not (urlsplit(url).hostname or '').endswith('.jobs.feishu.cn'):
            raise ValueError('expected a public *.jobs.feishu.cn portal URL')
        opts = validate_options(options)
        root = directory(artifact_dir)
        return await asyncio.to_thread(collect_feishu, url, root, opts)
