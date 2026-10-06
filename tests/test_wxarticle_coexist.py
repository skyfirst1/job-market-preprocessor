"""Selective PAC must retain Clash and only mutate the PAC registry value."""

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'tools' / 'WxArticleSaver-pilot' / 'coexist_launcher.py'
spec = importlib.util.spec_from_file_location('wxas_coexist', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_pac_retains_clash_for_non_article_hosts():
    pac = module.pac_text()
    assert 'host === "mp.weixin.qq.com"' in pac
    assert 'PROXY 127.0.0.1:8899; PROXY 127.0.0.1:7897' in pac
    assert pac.rstrip().endswith('return "PROXY 127.0.0.1:7897";\n}')
    assert '192.168.*' in pac and 'localhost' in pac


def test_registry_writes_only_change_pac():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    writes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and
              isinstance(n.func, ast.Attribute) and n.func.attr in ('SetValueEx', 'DeleteValue')]
    assert writes
    assert all(isinstance(n.args[1], ast.Constant) and n.args[1].value == 'AutoConfigURL' for n in writes)


@pytest.mark.parametrize('state', [
    {'ProxyEnable': 0, 'ProxyServer': '127.0.0.1:7897', 'AutoConfigURL': None},
    {'ProxyEnable': 1, 'ProxyServer': '127.0.0.1:9999', 'AutoConfigURL': None},
    {'ProxyEnable': 1, 'ProxyServer': '127.0.0.1:7897', 'AutoConfigURL': 'http://other/proxy.pac'},
])
def test_unexpected_proxy_is_rejected(state):
    with pytest.raises(RuntimeError):
        module.validate_proxy(state)


def test_expected_proxy_is_accepted():
    module.validate_proxy({'ProxyEnable': 1, 'ProxyServer': '127.0.0.1:7897', 'AutoConfigURL': None})


def test_restore_can_query_and_preserves_static_proxy(monkeypatch, tmp_path):
    state = {'AutoConfigURL': module.PAC_URL, 'ProxyServer': '127.0.0.1:7897', 'ProxyEnable': 1}
    class Key:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def open_key(root, path, reserved, access):
        assert access & 1 and access & 2
        return Key()
    registry = SimpleNamespace(HKEY_CURRENT_USER=0, KEY_SET_VALUE=2, KEY_QUERY_VALUE=1,
        OpenKey=open_key, QueryValueEx=lambda key, name: (state[name], 1),
        DeleteValue=lambda key, name: state.pop(name))
    monkeypatch.setitem(sys.modules, 'winreg', registry)
    monkeypatch.setattr(module, 'refresh_settings', lambda: None)
    backup = tmp_path / 'backup.json'
    backup.write_text('{}')
    monkeypatch.setattr(module, 'BACKUP', backup)
    module.restore_pac({'AutoConfigURL': None})
    assert state == {'ProxyServer': '127.0.0.1:7897', 'ProxyEnable': 1}
    assert not backup.exists()


def test_tls_rejection_stops_capture(tmp_path):
    log = tmp_path / 'capture.log'
    log.write_text('Client TLS handshake failed: rejected certificate\n')
    with pytest.raises(RuntimeError, match='without retry'):
        module.check_capture_log(log, 0)
    module.check_capture_log(log, log.stat().st_size)


def test_actual_trust_is_checked_not_assumed(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert not module.ca_is_trusted('A' * 40)
    assert 'Test-Path' in calls[0][-1]
    with pytest.raises(ValueError):
        module.ca_is_trusted('bad-fingerprint')


def test_certificate_is_not_deleted_on_exit():
    assert 'delstore' not in SOURCE.read_text(encoding='utf-8')
