import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


def workflow():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'preprocess_wechat_pdf.py'
    spec = importlib.util.spec_from_file_location('wechat_pdf_workflow', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arguments(tmp_path, **changes):
    args = {'artifact_dir': str(tmp_path), 'path': 'source.pdf',
            'source_url': 'https://mp.weixin.qq.com/s/public',
            'min_image_width': 500, 'min_image_height': 200,
            'max_images': 20, 'ocr': False}
    args.update(changes)
    return argparse.Namespace(**args)


def test_offline_delivery_requires_semantic_review(tmp_path, monkeypatch):
    module = workflow()
    result = {'url': 'https://mp.weixin.qq.com/s/public', 'status': 'partial',
              'text': 'Agent role\nother line', 'images': [], 'page_count': 2}
    monkeypatch.setitem(sys.modules, 'jobprep.app.pdf_import',
                        SimpleNamespace(import_pdf=lambda *a, **kw: result))
    monkeypatch.setattr(module, 'load_settings', lambda: pytest.fail('offline must not load credentials'))
    assert asyncio.run(module.process(arguments(tmp_path))) == 0
    payload = json.loads((tmp_path / 'preprocessed.json').read_text(encoding='utf-8'))
    assert payload['semantic_review_required'] is True
    assert payload['keyword_evidence'] == ['Agent role']
    assert payload['ocr_calls_this_run'] is None


def test_image_cap_precedes_paid_operations(tmp_path, monkeypatch):
    module = workflow()
    monkeypatch.setitem(sys.modules, 'jobprep.app.pdf_import',
                        SimpleNamespace(import_pdf=lambda *a, **kw: {'images': [{}, {}]}))
    monkeypatch.setattr(module, 'load_settings', lambda: pytest.fail('over-limit must not start OCR'))
    with pytest.raises(ValueError, match='count exceeds'):
        asyncio.run(module.process(arguments(tmp_path, ocr=True, max_images=1)))


def test_ocr_failure_is_not_success(tmp_path, monkeypatch):
    module = workflow()
    result = {'url': 'https://mp.weixin.qq.com/s/public', 'status': 'pending_ocr',
              'text': '', 'images': [{}], 'page_count': 1}
    monkeypatch.setitem(sys.modules, 'jobprep.app.pdf_import',
                        SimpleNamespace(import_pdf=lambda *a, **kw: result))
    monkeypatch.setattr(module, 'load_settings', lambda: {'data_dir': tmp_path, 'ocr_max_calls': 50})
    monkeypatch.setattr(module, 'BaiduOCR', lambda *a, **kw: SimpleNamespace(calls_used=7))

    class Tools:
        def __init__(self, root):
            pass

        async def ocr(self, document, engine):
            return {**document, 'ocr': [{'status': 'error', 'text': ''}],
                    'ocr_gaps': [{'image': 0, 'reason': 'error'}]}

    monkeypatch.setattr(module, 'AppTools', Tools)
    assert asyncio.run(module.process(arguments(tmp_path, ocr=True))) == 1
