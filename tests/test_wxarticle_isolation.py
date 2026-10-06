"""The isolated launcher must never use the global-proxy launcher."""

import importlib.util
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / 'tools' / 'WxArticleSaver-pilot' / 'local_launcher.py'
spec = importlib.util.spec_from_file_location('wxas_local_launcher', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_capture_targets_exact_pid_and_article_domain():
    command = module.capture_command(2256)
    assert 'local:2256' in command
    assert '--allow-hosts' in command
    assert 'mp\\.weixin\\.qq\\.com' in command[command.index('--allow-hosts') + 1]
    for forbidden in ('winreg', 'backup_and_enable_proxy', 'trust_ca(', 'import launcher', 'ExecutionPolicy'):
        assert forbidden not in SOURCE.read_text(encoding='utf-8')
    assert 'delstore' not in SOURCE.read_text(encoding='utf-8')


@pytest.mark.parametrize('pid', [0, -1])
def test_rejects_all_process_capture(pid):
    with pytest.raises(ValueError):
        module.capture_command(pid)
