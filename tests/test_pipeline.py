import asyncio
from pathlib import Path

from PIL import Image

from jobprep.pipeline import Workstation


def config(root):
    return {'root': root, 'data_dir': root / 'data', 'export_dir': root / 'exports',
            'ocr_max_calls': 0, 'crawl': {}, 'sites': {}, 'max_details_per_page': 1}


def test_manual_saved_relative_image_remains_pending_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv('BAIDU_OCR_API_KEY', raising=False)
    monkeypatch.delenv('BAIDU_OCR_SECRET_KEY', raising=False)
    folder = tmp_path / 'manual'
    folder.mkdir()
    Image.new('RGB', (100, 100), 'white').save(folder / 'poster.png')
    html = folder / 'article.html'
    html.write_text('<div id="js_content">Agent recruitment<img src="poster.png"></div>', encoding='utf-8')
    workstation = Workstation(config(tmp_path))
    outcome = asyncio.run(workstation.import_file(html, 'https://example.com/article'))
    assert outcome['status'] == 'pending_ocr'
    import json

    result = json.loads(workstation.store.task(outcome['id'])['result_json'])
    assert Path(result['images'][0]['path']).is_file()
    assert result['ocr'][0]['status'] == 'pending_credentials'


def test_detail_limit_is_reported_and_nav_not_enqueued(tmp_path, monkeypatch):
    from jobprep import crawler

    async def fake_collect(url, directory, options):
        return {'url': url, 'status': 'partial', 'text': 'Jobs', 'images': [], 'jobs': [],
                'links': [{'kind': 'job', 'url': 'https://example.com/careers'}],
                'coverage': {'complete': False, 'detail_urls': ['https://example.com/job/1',
                                                               'https://example.com/job/2']}}

    monkeypatch.setattr(crawler, 'collect', fake_collect)
    workstation = Workstation(config(tmp_path))
    task_id = workstation.store.add_task('https://example.com/list', priority=10)
    summary = asyncio.run(workstation.run(1))
    assert summary['processed'] == 1
    assert workstation.store.stats()['tasks'] == 2
    import json

    result = json.loads(workstation.store.task(task_id)['result_json'])
    assert result['coverage']['detail_limit_reached']
    assert result['coverage']['detail_urls_not_enqueued'] == 1
    assert not result['coverage']['complete']


def test_missing_download_retries_acquisition_not_only_ocr(tmp_path, monkeypatch):
    from jobprep import crawler

    calls = []

    async def fake_collect(url, directory, options):
        calls.append(url)
        return {'url': url, 'status': 'ok', 'text': 'Recovered text', 'images': [],
                'jobs': [], 'links': [], 'coverage': {'complete': True}}

    monkeypatch.setattr(crawler, 'collect', fake_collect)
    workstation = Workstation(config(tmp_path))
    task_id = workstation.store.add_task('https://example.com/article', priority=10)
    workstation.store.finish(task_id, 'pending_ocr', {
        'status': 'pending_ocr', 'acquisition_status': 'partial',
        'images': [{'url': 'https://example.com/poster.png', 'path': '', 'status': 'error'}],
        'ocr_gaps': [{'image': 0, 'reason': 'missing_image'}]})
    workstation.store.retry(['pending_ocr'], [task_id])
    asyncio.run(workstation.run(1))
    assert calls == ['https://example.com/article']
    assert workstation.store.task(task_id)['status'] == 'ok'
