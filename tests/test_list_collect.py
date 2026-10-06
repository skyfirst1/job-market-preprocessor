import json

import httpx
import pytest

from jobprep.list_collect import collect, main


def config(**response):
    return {
        'url': 'https://example.test/jobs', 'company': '\u914d\u7f6e\u516c\u53f8',
        'scope': 'public list', 'query': {'pageSize': 60},
        'pagination': {'location': 'query', 'path': 'page', 'max_pages': 10},
        'response': {'items_path': 'Data.Posts', 'total_path': 'Data.Count',
                     'id_path': 'ID', **response},
        'fields': {'title': 'Title', 'description': 'Description', 'url': 'URL'},
        'http': {'interval': 0, 'backoff': 0, 'retries': 1},
    }


def run(tmp_path, cfg, handler):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return collect(cfg, tmp_path / 'output', client=client,
                       artifacts_dir=tmp_path / 'artifacts', sleep=lambda _: None)


def payload(ids, total, **extra):
    return {'Data': {'Posts': [{'ID': i, 'Title': '\u5de5\u7a0b\u5e08',
                               'Description': '\u8d1f\u8d23\u5f00\u53d1',
                               'URL': f'https://example.test/job/{i}',
                               'Unmapped': {'keep': True}} for i in ids],
                     'Count': total, **extra}}


def test_page_size_clamp_and_raw_exports(tmp_path):
    requested = []

    def handler(request):
        page = int(request.url.params['page'])
        requested.append(page)
        assert request.url.params['pageSize'] == '60'
        return httpx.Response(200, json=payload({1: [1, 2], 2: [3, 4], 3: [5]}[page], 5))

    coverage = run(tmp_path, config(), handler)
    assert requested == [1, 2, 3]
    assert coverage['list_complete'] and coverage['record_count'] == 5
    assert not coverage['jd_complete']
    assert coverage['needs_details_count'] == 5
    records = [json.loads(line) for line in (tmp_path / 'output/jobs.jsonl').read_text(encoding='utf-8').splitlines()]
    assert records[0]['raw']['Unmapped'] == {'keep': True}
    assert records[0]['title'] == '\u5de5\u7a0b\u5e08'
    assert records[0]['url_provenance'] == 'response'
    assert not records[0]['employer_verified'] and not records[0]['scope_verified']
    assert (tmp_path / 'output/jobs.csv').read_bytes().startswith(b'\xef\xbb\xbf')
    assert len(list((tmp_path / 'artifacts').glob('*/page-*.json'))) == 3
    assert b'\\u5de5' not in (tmp_path / 'output/jobs.jsonl').read_bytes()


def test_repeated_page_is_partial(tmp_path):
    coverage = run(tmp_path, config(), lambda _: httpx.Response(200, json=payload([1, 2], 5)))
    assert coverage['status'] == 'partial'
    assert 'repeated_page' in coverage['reasons']
    assert coverage['record_count'] == 2
    assert coverage['pages_received'] == 2


def test_total_mismatch_empty_page(tmp_path):
    coverage = run(tmp_path, config(), lambda request: httpx.Response(
        200, json=payload([1, 2] if request.url.params['page'] == '1' else [], 5)))
    assert not coverage['list_complete']
    assert 'total_count_mismatch' in coverage['reasons']


@pytest.mark.parametrize('change', ['actual', 'total', 'last'])
def test_protocol_mismatches(tmp_path, change):
    cfg = config(page_path='Data.Page', total_pages_path='Data.Pages', last_page_path='Data.Last')

    def handler(request):
        page = int(request.url.params['page'])
        return httpx.Response(200, json=payload(
            [page], 3 if change != 'total' or page == 1 else 4,
            Page=99 if change == 'actual' else page, Pages=3,
            Last=change == 'last'))

    coverage = run(tmp_path, cfg, handler)
    assert coverage['status'] == 'partial'
    assert coverage['reasons']


def test_max_pages_and_overlapping_ids(tmp_path):
    cfg = config()
    cfg['pagination']['max_pages'] = 2
    coverage = run(tmp_path, cfg, lambda r: httpx.Response(
        200, json=payload([int(r.url.params['page'])], 5)))
    assert 'max_pages_reached' in coverage['reasons']
    coverage = run(tmp_path, config(), lambda r: httpx.Response(
        200, json=payload([1, int(r.url.params['page']) + 1], 5)))
    assert 'duplicate_item_ids' in coverage['reasons']
    assert coverage['record_count'] == 3


def test_post_nested_path_increment_and_last_page_without_total(tmp_path):
    cfg = config(items_path='items', id_path='id', last_page_path='last')
    cfg['response'].pop('total_path')
    cfg.update(method='POST', body={'filter': {'keep': True}})
    cfg['pagination'].update(location='body', path='paging.offset', start=0, increment=2)
    observed = []

    def handler(request):
        body = json.loads(request.content)
        observed.append(body['paging']['offset'])
        assert body['filter']['keep']
        offset = observed[-1]
        return httpx.Response(200, json={'items': [{'id': offset}], 'last': offset == 2})

    coverage = run(tmp_path, cfg, handler)
    assert observed == [0, 2]
    assert coverage['list_complete']
    assert cfg['body'] == {'filter': {'keep': True}}


def test_retry_and_http_failure_preserve_results(tmp_path):
    attempts = []

    def handler(request):
        attempts.append(int(request.url.params['page']))
        if len(attempts) == 1:
            return httpx.Response(503, json={'error': 'retry'})
        if attempts[-1] == 1:
            return httpx.Response(200, json=payload([1], 3))
        return httpx.Response(403, json={'error': 'blocked'})

    coverage = run(tmp_path, config(), handler)
    assert attempts == [1, 1, 2]
    assert coverage['record_count'] == 1 and coverage['status'] == 'partial'
    assert len(list((tmp_path / 'artifacts').glob('*/page-*.json'))) == 3
    assert (tmp_path / 'output/coverage.json').exists()


def test_missing_items_not_treated_as_empty(tmp_path):
    coverage = run(tmp_path, config(), lambda _: httpx.Response(200, json={'Data': {'Count': 0}}))
    assert coverage['status'] == 'partial'


def test_empty_total_and_optional_mapping(tmp_path):
    cfg = config()
    cfg.pop('fields')
    coverage = run(tmp_path, cfg, lambda _: httpx.Response(200, json=payload([], 0)))
    assert coverage['list_complete'] and not coverage['jd_complete']


@pytest.mark.parametrize('enabled', [None, False, True])
def test_null_items_are_empty_opt_in(tmp_path, enabled):
    cfg = config(items_path='data.positionList', total_path='data.count',
                 id_path='postId', success_path='status', success_value=0,
                 page_path='data.pageIndex')
    cfg.update(method='POST', body={'pageSize': 60})
    cfg['pagination'].update(location='body', path='pageIndex')
    if enabled is not None:
        cfg['response']['null_items_are_empty'] = enabled

    def handler(request):
        assert json.loads(request.content) == {'pageSize': 60, 'pageIndex': 1}
        return httpx.Response(200, json={
            'status': 0, 'data': {'positionList': None, 'count': 0, 'pageIndex': 1}})

    coverage = run(tmp_path, cfg, handler)
    assert coverage['list_complete'] is (enabled is True)
    assert coverage['record_count'] == 0
    if enabled:
        assert coverage['list_completeness_basis'] == 'empty_page'
        assert coverage['pages'][0]['null_items_as_empty']
    raw = json.loads(next((tmp_path / 'artifacts').glob('*/page-*.json')).read_text(encoding='utf-8'))
    assert raw['data']['positionList'] is None


def test_null_terminal_page_still_checks_count_and_actual_page(tmp_path):
    cfg = config(null_items_are_empty=True, page_path='Data.Page')

    def handler(request):
        page = int(request.url.params['page'])
        data = payload([1], 2, Page=page) if page == 1 else {
            'Data': {'Posts': None, 'Count': 2, 'Page': page}}
        return httpx.Response(200, json=data)

    coverage = run(tmp_path, cfg, handler)
    assert coverage['status'] == 'partial'
    assert coverage['record_count'] == 1
    assert 'total_count_mismatch' in coverage['reasons']
    assert coverage['pages'][1]['actual_page'] == 2
    coverage = run(tmp_path, cfg, lambda _: httpx.Response(
        200, json={'Data': {'Posts': None, 'Count': 0, 'Page': 99}}))
    assert coverage['status'] == 'partial'
    assert any('actual page does not match' in reason for reason in coverage['reasons'])


@pytest.mark.parametrize('items', ['missing', {}, '', 0])
def test_null_opt_in_does_not_accept_missing_or_invalid_items(tmp_path, items):
    data = {'Count': 0}
    if items != 'missing':
        data['Posts'] = items
    coverage = run(tmp_path, config(null_items_are_empty=True),
                   lambda _: httpx.Response(200, json={'Data': data}))
    assert coverage['status'] == 'partial'


def test_null_terminal_page_without_total(tmp_path):
    cfg = config(null_items_are_empty=True)
    cfg['response'].pop('total_path')
    coverage = run(tmp_path, cfg, lambda request: httpx.Response(
        200, json=payload([1], 1) if request.url.params['page'] == '1'
        else {'Data': {'Posts': None}}))
    assert coverage['list_complete'] and coverage['record_count'] == 1
    assert coverage['pages_received'] == 2


def test_cli_ascii_metadata(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config(), ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr('jobprep.list_collect.collect', lambda *args: {
        'status': 'partial', 'record_count': 1, 'expected_total': 2,
        'pages_received': 1, 'list_complete': False, 'jd_complete': False,
        'reasons': ['\u9519\u8bef'], 'artifacts_dir': '\u539f\u59cb\u9875'})
    assert main(['--config', str(path), '--output', str(tmp_path / 'out')]) == 2
    assert capsys.readouterr().out.isascii()
