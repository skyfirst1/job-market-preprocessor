"""Feishu public portal acquisition through normal, fresh Playwright UI."""

import argparse
import copy
import json
from pathlib import Path
from uuid import uuid4

import httpx

from .tools import directory, empty_document, list_document, validate_url, failure_fields

API_PATH = '/api/v1/search/job/posts'
DEFAULT_OPTIONS = {'company': 'configured company', 'max_pages': 100, 'timeout': 25000,
                   'next_selector': 'li.atsx-pagination-next', 'browser_channel': 'chrome',
                   'interval': 1.2, 'max_response_bytes': 5 * 1024**2}


def validate_options(options):
    import math

    opts = copy.deepcopy(DEFAULT_OPTIONS)
    supplied = options or {}
    if not isinstance(supplied, dict) or set(supplied) - set(opts):
        raise ValueError('unknown Feishu option or options is not an object')
    opts.update(supplied)
    for name, low, high in (('max_pages', 1, 1000), ('timeout', 100, 120000),
                            ('max_response_bytes', 1, 30 * 1024**2)):
        value = opts[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{name} must be an integer in [{low}, {high}]')
    if isinstance(opts['interval'], bool) or not isinstance(opts['interval'], (int, float)) or not math.isfinite(opts['interval']) or not 0 <= opts['interval'] <= 60:
        raise ValueError('interval must be finite and between 0 and 60 seconds')
    if not isinstance(opts['next_selector'], str) or not opts['next_selector'].strip():
        raise ValueError('next_selector must be a nonempty CSS selector')
    if not isinstance(opts['company'], str):
        raise ValueError('company must be a string label')
    if opts['browser_channel'] is not None and (not isinstance(opts['browser_channel'], str) or not opts['browser_channel']):
        raise ValueError('browser_channel must be a nonempty string or null')
    return opts


def matches_response(response, offset=None):
    if API_PATH not in response.url or response.request.method != 'POST':
        return False
    if offset is None:
        # The public frontend may issue an unsigned 405 before its normal retry.
        return response.status == 200
    try:
        return response.request.post_data_json.get('offset') == offset
    except (AttributeError, TypeError, ValueError):
        return False


class PortalTransport(httpx.BaseTransport):
    def __init__(self, page, first, timeout, next_selector='li.atsx-pagination-next', max_response_bytes=5 * 1024**2):
        self.page, self.first, self.timeout = page, first, timeout
        self.first_body = first.request.post_data_json
        self.last_offset = None
        self.next_selector, self.max_response_bytes = next_selector, max_response_bytes

    def handle_request(self, request):
        body = json.loads(request.content)
        offset = body['offset']
        try:
            if self.last_offset is None:
                response = self.first
            else:
                selector = self.page.locator(self.next_selector)
                if ('disabled' in (selector.get_attribute('class', timeout=self.timeout) or '') or
                        selector.get_attribute('aria-disabled', timeout=self.timeout) == 'true' or
                        selector.get_attribute('disabled', timeout=self.timeout) is not None):
                    raise ValueError('UI next page disabled before response total reached')
                with self.page.expect_response(lambda r: matches_response(r, offset), timeout=self.timeout) as pending:
                    selector.click(timeout=self.timeout)
                response = pending.value
            if response.request.post_data_json != body:
                raise ValueError('browser pagination/filter body differs from configured request')
            raw = response.body()
            if len(raw) > self.max_response_bytes:
                raise ValueError('observed response exceeds byte limit')
            self.last_offset = offset
            return httpx.Response(response.status, content=raw, request=request)
        except ValueError:
            raise
        except Exception as exc:
            raise httpx.TransportError(f'Browser UI response failed: {type(exc).__name__}') from exc


def collect_feishu(url, root, opts):
    from jobprep import list_collect

    result = empty_document(url, 'feishu-ui-list', root)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=opts['browser_channel'], headless=True, timeout=opts['timeout'])
            try:
                page = browser.new_page()
                page.set_default_timeout(opts['timeout'])
                with page.expect_response(matches_response, timeout=opts['timeout']) as pending:
                    page.goto(url, wait_until='domcontentloaded', timeout=opts['timeout'])
                first = pending.value
                body = first.request.post_data_json
                if not isinstance(body, dict) or body.get('offset') != 0 or isinstance(body.get('limit'), bool) or not isinstance(body.get('limit'), int) or body['limit'] <= 0:
                    raise ValueError('initial list must begin at offset zero with a positive limit')
                config = {
                    'company': opts['company'], 'scope': url + ' ; exact normal-UI initial filters; not all company channels',
                    'url': first.url.split('?')[0], 'method': 'POST', 'body': copy.deepcopy(body),
                    'pagination': {'location': 'body', 'path': 'offset', 'start': 0, 'increment': body['limit'], 'max_pages': opts['max_pages']},
                    'response': {'items_path': 'data.job_post_list', 'total_path': 'data.count', 'id_path': 'id', 'success_path': 'code', 'success_value': 0},
                    'fields': {'title': 'title', 'description': 'description', 'requirements': 'requirement',
                               'location': 'city_list', 'category': 'job_function.name', 'recruit_label': 'recruit_type.parent.name'},
                    'http': {'interval': opts['interval'], 'retries': 0},
                }
                output = root / ('feishu-list-' + uuid4().hex)
                transport = PortalTransport(page, first, opts['timeout'], opts['next_selector'], opts['max_response_bytes'])
                with httpx.Client(transport=transport, trust_env=False) as client:
                    coverage = list_collect.collect(config, output, client=client, artifacts_dir=root / 'list_runs')
                result = list_document(config, root, output, coverage, 'feishu-ui-list')
                result.update(url=url, final_url=page.url)
                config_path = output / 'request_config.json'
                config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
                result['output_paths']['request_config_json'] = str(config_path.resolve())
            finally:
                try:
                    browser.close()
                except Exception as exc:
                    result['warnings'].append(f'Browser cleanup failed: {type(exc).__name__}')
                    result['coverage'].update(complete=False, list_complete=False, stop_reason='browser_cleanup_error')
                    result['status'] = 'partial' if result['jobs'] else 'error'
                    result.update(retryable=False, error_kind='cleanup')
    except Exception as exc:
        result['status'] = 'partial' if result['jobs'] else 'error'
        result['error'] = f'{type(exc).__name__}: Feishu UI acquisition failed'
        result['coverage'].update(complete=False, list_complete=False, stop_reason='browser_error')
        result['warnings'].append(result['error'])
        result.update(failure_fields(exc))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--company', default=DEFAULT_OPTIONS['company'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-pages', type=int, default=100)
    parser.add_argument('--timeout', type=int, default=25000, help='UI operation timeout in milliseconds')
    parser.add_argument('--next-selector', default=DEFAULT_OPTIONS['next_selector'])
    args = parser.parse_args(argv)
    from urllib.parse import urlsplit

    validate_url(args.url)
    if not (urlsplit(args.url).hostname or '').endswith('.jobs.feishu.cn'):
        parser.error('expected a public *.jobs.feishu.cn portal URL')
    opts = validate_options({'company': args.company, 'max_pages': args.max_pages, 'timeout': args.timeout, 'next_selector': args.next_selector})
    result = collect_feishu(args.url, directory(args.output), opts)
    for name in ('jobs_jsonl', 'jobs_csv', 'coverage_json', 'request_config_json'):
        if name in result.get('output_paths', {}):
            path = Path(result['output_paths'][name])
            (args.output / path.name).write_bytes(path.read_bytes())
    print(json.dumps({'status': result['status'], 'coverage': result['coverage']}, ensure_ascii=True))
    return 0 if result['coverage']['list_complete'] else 2
