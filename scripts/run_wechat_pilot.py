"""Bounded one-article pilot: public browser HTML, CDN images, OCR, workstation."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCE = 'https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA'
TRIAL = ROOT / 'data' / 'public_wechat_trial'
IMAGES = TRIAL / 'cdn_images'


def stage(script, arguments=(), accepted=(0,)):
    result = subprocess.run([sys.executable, str(ROOT / script), *arguments],
                            cwd=ROOT, capture_output=True, encoding='utf-8',
                            errors='replace', timeout=120)
    if result.returncode not in accepted:
        raise RuntimeError(f'{Path(script).name} stopped with exit code {result.returncode}; inspect its local report')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ocr', action='store_true', help='Explicitly enable configured Baidu OCR; successful cache entries are reused')
    args = parser.parse_args()
    stage('scripts/save_public_wechat.py', accepted=(0, 2, 3))
    browser = json.loads((TRIAL / 'status.json').read_text(encoding='utf-8'))
    evidence = browser.get('evidence', {})
    if (browser.get('target') != SOURCE or browser.get('status') not in ('partial', 'success')
            or not evidence.get('article_title') or not evidence.get('js_content_present')):
        raise RuntimeError('Normal public article not verified; no image download or OCR will run')
    stage('scripts/download_saved_wechat_images.py', accepted=(0, 3))
    download = json.loads((IMAGES / 'download_status.json').read_text(encoding='utf-8'))
    if download.get('source_url') != SOURCE or download.get('status') != 'success':
        raise RuntimeError('Image download is incomplete; no automatic retry or paid OCR will run')
    options = ['--document-json', str(IMAGES / 'document.json'), '--source-url', SOURCE,
               '--artifact-dir', str(IMAGES)]
    if args.ocr:
        options.append('--ocr')
    stage('scripts/preprocess_wechat_pdf.py', options)
    options = ['import-file', str(IMAGES / 'article_local.html'), '--url', SOURCE, '--kind', 'html']
    if not args.ocr:
        options.append('--no-ocr')
    result = subprocess.run([sys.executable, '-m', 'jobprep', *options], cwd=ROOT,
                            capture_output=True, encoding='utf-8', errors='replace', timeout=120)
    if result.returncode:
        raise RuntimeError('Workstation import failed; preserve artifacts and inspect task state')
    imported = json.loads(result.stdout)
    delivery = json.loads((IMAGES / 'preprocessed.json').read_text(encoding='utf-8'))
    print(json.dumps({'source': SOURCE, 'workstation_task_id': imported['id'],
                      'status': imported['status'], 'images': download['unique_images'],
                      'text_chars': len(delivery['combined_text']),
                      'ocr_calls_this_run': delivery['ocr_calls_this_run'],
                      'output': str(IMAGES / 'preprocessed.json'),
                      'scope': 'single tested article; not a universal WeChat downloader'}, ensure_ascii=True))


if __name__ == '__main__':
    main()
