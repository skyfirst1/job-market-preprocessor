"""Configurable public HTTP list collector, independent of site adapters.

Required config: url, pagination.path, response.items_path. Optional response
paths: total_path, id_path, page_path, total_pages_path, last_page_path,
success_path (with success_value). Paths are dotted strings or key/index lists.
response.null_items_are_empty accepts explicit null lists only when true.
Pagination location is query or body; body_encoding is json (default) or form.
Company and scope are user labels, never verified employer attribution.
"""

import argparse
import copy
import csv
import hashlib
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
MISSING = object()


def _parts(path):
    return path.split('.') if isinstance(path, str) else list(path)


def get_path(value, path, default=MISSING):
    if path is None:
        return default
    try:
        for key in _parts(path):
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return default


def _set_path(value, path, item):
    parts = _parts(path)
    for key in parts[:-1]:
        value = value.setdefault(key, {})
        if not isinstance(value, dict):
            raise ValueError('pagination path crosses a non-object')
    value[parts[-1]] = item


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f'{name} must be an integer')
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f'{name} must be an integer') from None
    if number < minimum:
        raise ValueError(f'{name} must be >= {minimum}')
    return number


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                               allow_nan=False) + '\n', encoding='utf-8')


def _identity(item, id_path):
    value = get_path(item, id_path) if id_path is not None else MISSING
    if id_path is not None and (value is MISSING or value is None or value == ''):
        raise ValueError('missing item ID')
    if value is MISSING:
        value = item
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode('utf-8')).hexdigest()


def _placeholder(value):
    if value is None or value == '' or value == [] or value == {}:
        return True
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized in {'', '-', '--', 'n/a', 'null', 'none', 'tbd',
                              '暂无', '待补充', '详见官网', '详见职位详情'}
    return False


def _record(item, config, raw_path, page, index):
    fields = {name: get_path(item, path, None)
              for name, path in config.get('fields', {}).items()}
    # Detail completeness is independent of list enumeration and URLs.
    needs_details = any(_placeholder(fields.get(name))
                        for name in ('description', 'requirements'))
    id_path = config['response'].get('id_path')
    return {
        'id': get_path(item, id_path, None),
        'company': config.get('company', ''),
        'scope': config.get('scope', 'configured public list'),
        'scope_verified': False,
        'employer_verified': False,
        **{name: fields.get(name) for name in
           ('title', 'category', 'location', 'dept', 'description',
            'requirements', 'url')},
        'fields': fields,
        'url_provenance': 'response' if fields.get('url') else 'missing',
        'detail_fetched': False,
        'needs_details': needs_details,
        'raw_ref': str(raw_path.resolve()),
        'raw_index': index,
        'page': page,
        'raw': item,
    }


def _validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('config must be an object')
    if httpx.URL(config.get('url', '')).scheme not in ('http', 'https'):
        raise ValueError('url must use HTTP or HTTPS')
    if config.get('method', 'GET').upper() not in ('GET', 'POST'):
        raise ValueError('method must be GET or POST')
    pagination = config.get('pagination', {})
    if not pagination.get('path'):
        raise ValueError('pagination.path is required')
    if pagination.get('location', 'query') not in ('query', 'body'):
        raise ValueError('pagination.location must be query or body')
    if not config.get('response', {}).get('items_path'):
        raise ValueError('response.items_path is required')
    if not isinstance(config['response'].get('null_items_are_empty', False), bool):
        raise ValueError('response.null_items_are_empty must be boolean')
    for name, default, minimum in (('start', 1, 0), ('increment', 1, 1),
                                   ('max_pages', 1000, 1)):
        _integer(pagination.get(name, default), name, minimum)
    options = config.get('http', {})
    _integer(options.get('retries', 3), 'retries')
    for name, default, minimum in (('timeout', 30, 0), ('connect_timeout', 10, 0),
                                   ('interval', 0.5, -1), ('backoff', 1, -1)):
        number = float(options.get(name, default))
        if not math.isfinite(number) or number <= minimum:
            raise ValueError(f'invalid http.{name}')
    if config.get('body_encoding', 'json') not in ('json', 'form'):
        raise ValueError('body_encoding must be json or form')
    for name in ('query', 'body', 'headers', 'fields'):
        if not isinstance(config.get(name, {}), dict):
            raise ValueError(f'{name} must be an object')


def collect(config, output, *, client=None, artifacts_dir=None, sleep=time.sleep):
    """Collect sequential pages; persist partial results on protocol/HTTP errors.

    A terminal empty page, declared last page, or exact response total ends the
    run. Requested page size never determines termination. Configured counts,
    page numbers and page totals must agree; repeats and safety caps are partial.
    A supplied httpx.Client (e.g. MockTransport) remains owned by the caller.
    """
    _validate_config(config)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    run_dir = Path(artifacts_dir or ROOT / 'data' / 'artifacts' / 'list_runs') / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    pagination = config['pagination']
    response_config = config['response']
    options = config.get('http', {})
    page = _integer(pagination.get('start', 1), 'start')
    increment = _integer(pagination.get('increment', 1), 'increment', 1)
    max_pages = _integer(pagination.get('max_pages', 1000), 'max_pages', 1)
    interval = float(options.get('interval', 0.5))
    retries = _integer(options.get('retries', 3), 'retries')
    backoff = float(options.get('backoff', 1))
    own_client = client is None
    if own_client:
        client = httpx.Client(
            timeout=httpx.Timeout(float(options.get('timeout', 30)),
                                  connect=float(options.get('connect_timeout', 10))),
            follow_redirects=False, trust_env=False)
    records, pages, reasons = [], [], []
    seen_ids, seen_pages = set(), set()
    total = total_pages = None
    terminal = None
    attempts = 0
    started = datetime.now(timezone.utc).isoformat()
    try:
        for ordinal in range(1, max_pages + 1):
            query = copy.deepcopy(config.get('query', {}))
            body = copy.deepcopy(config.get('body', {}))
            location = pagination.get('location', 'query')
            _set_path(query if location == 'query' else body, pagination['path'], page)
            kwargs = {'params': query, 'headers': config.get('headers', {})}
            if body or location == 'body':
                kwargs['json' if config.get('body_encoding', 'json') == 'json' else 'data'] = body
            payload = MISSING
            raw_path = None
            for attempt in range(retries + 1):
                if attempts:
                    sleep(interval)
                attempts += 1
                raw_path = run_dir / f'page-{ordinal:05d}-attempt-{attempt + 1:02d}.json'
                try:
                    response = client.request(config.get('method', 'GET').upper(),
                                              config['url'], **kwargs)
                    try:
                        payload = response.json()
                        _write_json(raw_path, payload)
                    except (ValueError, UnicodeError):
                        payload = MISSING
                        raw_path = raw_path.with_suffix('.txt')
                        raw_path.write_bytes(response.content)
                    retryable = response.status_code in (408, 429, 500, 502, 503, 504)
                    if retryable and attempt < retries:
                        sleep(backoff * (2 ** attempt))
                        continue
                    response.raise_for_status()
                    if payload is MISSING:
                        raise ValueError('response is not valid JSON')
                    break
                except httpx.TransportError:
                    if attempt == retries:
                        raise
                    sleep(backoff * (2 ** attempt))
            page_info = {'page': page, 'ordinal': ordinal,
                         'raw_ref': str(raw_path.resolve()), 'accepted': False}
            pages.append(page_info)
            if 'success_path' in response_config:
                if get_path(payload, response_config['success_path']) != response_config.get('success_value', True):
                    raise ValueError('response success marker mismatch')
            items = get_path(payload, response_config['items_path'])
            if items is None and response_config.get('null_items_are_empty', False):
                items = []
                page_info['null_items_as_empty'] = True
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError('response items must be an array of objects')
            page_info['item_count'] = len(items)
            if 'page_path' in response_config:
                actual = _integer(get_path(payload, response_config['page_path']), 'actual page')
                page_info['actual_page'] = actual
                if actual != page:
                    raise ValueError('actual page does not match requested page')
            if 'total_path' in response_config:
                reported = _integer(get_path(payload, response_config['total_path']), 'response total')
                page_info['reported_total'] = reported
                if total is not None and reported != total:
                    raise ValueError('response total changed during pagination')
                total = reported
            if 'total_pages_path' in response_config:
                reported_pages = _integer(get_path(payload, response_config['total_pages_path']), 'total pages')
                page_info['reported_total_pages'] = reported_pages
                if total_pages is not None and reported_pages != total_pages:
                    raise ValueError('response total pages changed during pagination')
                total_pages = reported_pages
                if ordinal > max(1, total_pages):
                    raise ValueError('page exceeds response total pages')
            last = False
            if 'last_page_path' in response_config:
                last = get_path(payload, response_config['last_page_path'])
                if not isinstance(last, bool):
                    raise ValueError('last page marker must be boolean')
            identities = [_identity(item, response_config.get('id_path')) for item in items]
            signature = tuple(sorted(identities))
            duplicate = False
            if items:
                if signature in seen_pages:
                    reasons.append('repeated_page')
                    duplicate = True
                if len(set(identities)) != len(identities) or seen_ids.intersection(identities):
                    reasons.append('duplicate_item_ids')
                    duplicate = True
                seen_pages.add(signature)
            for index, (item, identity) in enumerate(zip(items, identities)):
                if identity not in seen_ids:
                    records.append(_record(item, config, raw_path, page, index))
                    seen_ids.add(identity)
            page_info['accepted'] = not duplicate
            if duplicate:
                break
            count = len(records)
            if total is not None and count > total:
                reasons.append('collected_count_exceeds_total')
                break
            declared_end = total_pages is not None and ordinal == total_pages
            if total_pages == 0:
                if items:
                    reasons.append('nonempty_zero_total_pages')
                declared_end = True
            if last and total_pages is not None and not declared_end:
                reasons.append('last_page_total_pages_mismatch')
                break
            if declared_end and 'last_page_path' in response_config and not last:
                reasons.append('last_page_total_pages_mismatch')
                break
            if not items or last or declared_end:
                terminal = 'empty_page' if not items else 'last_page'
                if total is not None and count != total:
                    reasons.append('total_count_mismatch')
                if not items and total_pages is not None and ordinal < total_pages:
                    reasons.append('empty_page_before_last')
                break
            if total is not None and count == total:
                terminal = 'response_total_reached'
                if total_pages is not None and not declared_end:
                    reasons.append('total_reached_before_last_page')
                if 'last_page_path' in response_config and not last:
                    reasons.append('total_reached_without_last_marker')
                break
            page += increment
        else:
            reasons.append('max_pages_reached')
    except KeyboardInterrupt:
        reasons.append('interrupted')
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError) as exc:
        # Error messages can contain non-ASCII URL/text; console JSON escapes it.
        reasons.append(f'{type(exc).__name__}: {exc}')
    finally:
        if own_client:
            client.close()
    if total is not None and len(records) != total and 'total_count_mismatch' not in reasons:
        reasons.append('total_count_mismatch')
    list_complete = terminal is not None and not reasons
    needs_details_count = sum(record['needs_details'] for record in records)
    coverage = {
        'run_id': run_id, 'started_at': started,
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'company': config.get('company', ''),
        'scope': config.get('scope', 'configured public list'),
        'scope_verified': False, 'employer_verified': False,
        'source_url': config['url'],
        'status': 'complete' if list_complete else 'partial',
        'list_complete': list_complete,
        'list_completeness_basis': terminal,
        'jd_complete': bool(records) and needs_details_count == 0,
        'jd_completeness_basis': 'mapped description and requirements are non-placeholder; no detail fetch',
        'detail_fetched': False, 'needs_details_count': needs_details_count,
        'record_count': len(records), 'expected_total': total,
        'expected_total_pages': total_pages,
        'pages_received': len(pages), 'request_attempts': attempts,
        'reasons': list(dict.fromkeys(reasons)),
        'artifacts_dir': str(run_dir.resolve()), 'pages': pages,
        'identity_basis': 'configured ID' if response_config.get('id_path') else 'raw object hash',
    }
    with (output / 'jobs.jsonl').open('w', encoding='utf-8', newline='\n') as handle:
        for record in records:
            handle.write(_json(record) + '\n')
    columns = ['id', 'company', 'scope', 'scope_verified', 'employer_verified',
               'title', 'category', 'location', 'dept', 'description',
               'requirements', 'url', 'url_provenance', 'detail_fetched',
               'needs_details', 'raw_ref', 'raw_index', 'page', 'fields', 'raw']
    with (output / 'jobs.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for record in records:
            writer.writerow({key: _json(value) if isinstance(value, (dict, list)) else value
                             for key, value in record.items()})
    _write_json(output / 'coverage.json', coverage)
    _write_json(run_dir / 'coverage.json', coverage)
    return coverage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding='utf-8-sig'))
        coverage = collect(config, args.output)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps({key: coverage[key] for key in
                      ('status', 'record_count', 'expected_total', 'pages_received',
                       'list_complete', 'jd_complete', 'reasons', 'artifacts_dir')},
                     ensure_ascii=True))
    return 0 if coverage['list_complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
