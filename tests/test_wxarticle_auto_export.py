"""One-article automatic export without importing the proxy runtime."""

import ast
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'tools' / 'WxArticleSaver-pilot' / 'wx_article_saver.py'
URL = 'https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA'


def helpers(tmp_path, enabled=True):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
    names = {'is_pilot_article', 'claim_pilot_export', 'auto_export_markup'}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    env = {'urlparse': urlparse, 'parse_qs': parse_qs, 'json': json, 'time': time,
           'HOST': 'mp.weixin.qq.com', 'PILOT_SLUG': 'qd86zzbPnzHHGHn407ktQA',
           'AUTO_EXPORT_PILOT': enabled, 'PILOT_GATE': tmp_path / 'gate.json'}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<auto-export>', 'exec'), env)
    return env


def test_selected_article_and_canonical_url(tmp_path):
    env = helpers(tmp_path)
    assert env['is_pilot_article'](URL)
    assert env['is_pilot_article']('https://mp.weixin.qq.com/s?__biz=MzkyOTUzNTYzMQ%3D%3D&mid=2247497949&idx=1&sn=test')
    assert not env['is_pilot_article']('https://example.com/s/qd86zzbPnzHHGHn407ktQA')
    assert not env['is_pilot_article']('https://mp.weixin.qq.com/s/other')


def test_durable_one_attempt_gate(tmp_path):
    env = helpers(tmp_path)
    gate = env['PILOT_GATE']
    env['claim_pilot_export'](URL, gate)
    with pytest.raises(ValueError, match='already attempted'):
        helpers(tmp_path)['claim_pilot_export'](URL, gate)
    assert json.loads(gate.read_text())['article'] == URL
    assert env['auto_export_markup']('token', URL) == ''


def test_other_article_cannot_consume_gate(tmp_path):
    env = helpers(tmp_path)
    with pytest.raises(ValueError, match='restricted'):
        env['claim_pilot_export']('https://mp.weixin.qq.com/s/other', env['PILOT_GATE'])
    assert not env['PILOT_GATE'].exists()


def test_script_waits_for_loaded_body_and_is_opt_in(tmp_path):
    script = helpers(tmp_path)['auto_export_markup']('token', URL)
    assert 'window.__wxasExport(btn)' in script
    assert "document.getElementById('js_content')" in script
    assert '{once:true}' in script and '__wxasAutoStarted' in script
    assert '3000' in script
    assert helpers(tmp_path, False)['auto_export_markup']('token', URL) == ''
