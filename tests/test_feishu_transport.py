import json
from types import SimpleNamespace

import httpx
import pytest

from scripts.collect_feishu_list import PortalTransport
from jobprep.app.feishu import matches_response


def response(offset=0):
    return SimpleNamespace(
        status=200,
        request=SimpleNamespace(post_data_json={'offset': offset, 'limit': 10}),
        body=lambda: b'{"code":0,"data":{"count":1,"job_post_list":[{"id":"1"}]}}',
    )


def test_first_observed_response_is_reused_without_http_request():
    transport = PortalTransport(None, response(), 1000)
    request = httpx.Request('POST', 'https://example.jobs.feishu.cn/api/v1/search/job/posts',
                            json={'offset': 0, 'limit': 10})
    result = transport.handle_request(request)
    assert result.json()['data']['count'] == 1
    assert transport.last_offset == 0


def test_body_mismatch_is_not_reported_as_complete():
    transport = PortalTransport(None, response(), 1000)
    request = httpx.Request('POST', 'https://example.jobs.feishu.cn/api/v1/search/job/posts',
                            json={'offset': 0, 'limit': 20})
    with pytest.raises(ValueError, match='differs'):
        transport.handle_request(request)


def test_initial_unsigned_failure_is_ignored_but_pagination_failure_is_observed():
    observed = response()
    observed.url = 'https://example.jobs.feishu.cn/api/v1/search/job/posts'
    observed.request.method = 'POST'
    observed.status = 405
    assert not matches_response(observed)
    assert matches_response(observed, offset=0)
    observed.status = 200
    assert matches_response(observed)
