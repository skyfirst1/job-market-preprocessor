import importlib.util
import json
from pathlib import Path
import sys

import pytest


def runner():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'run_wechat_pilot.py'
    spec = importlib.util.spec_from_file_location('wechat_pilot_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_challenge_stops_before_images_and_ocr(tmp_path, monkeypatch):
    module = runner()
    monkeypatch.setattr(module, 'TRIAL', tmp_path)
    (tmp_path / 'status.json').write_text(json.dumps({'target': module.SOURCE, 'status': 'blocked'}))
    calls = []
    monkeypatch.setattr(module, 'stage', lambda script, *a, **kw: calls.append(script))
    monkeypatch.setattr(sys, 'argv', ['run_wechat_pilot.py', '--ocr'])
    with pytest.raises(RuntimeError, match='Normal public article'):
        module.main()
    assert calls == ['scripts/save_public_wechat.py']


def test_incomplete_download_stops_before_paid_ocr(tmp_path, monkeypatch):
    module = runner()
    monkeypatch.setattr(module, 'TRIAL', tmp_path)
    monkeypatch.setattr(module, 'IMAGES', tmp_path)
    (tmp_path / 'status.json').write_text(json.dumps({'target': module.SOURCE, 'status': 'partial',
        'evidence': {'article_title': 'public article', 'js_content_present': True}}))
    (tmp_path / 'download_status.json').write_text(json.dumps({'source_url': module.SOURCE, 'status': 'partial'}))
    calls = []
    monkeypatch.setattr(module, 'stage', lambda script, *a, **kw: calls.append(script))
    monkeypatch.setattr(sys, 'argv', ['run_wechat_pilot.py', '--ocr'])
    with pytest.raises(RuntimeError, match='Image download is incomplete'):
        module.main()
    assert calls == ['scripts/save_public_wechat.py', 'scripts/download_saved_wechat_images.py']
