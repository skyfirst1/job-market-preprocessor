"""Process a user-exported WeChat PDF without touching WeChat or proxies."""

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobprep.app.tools import AppTools
from jobprep.ocr import BaiduOCR
from jobprep.settings import load_settings
from jobprep.app.tools import MAX_FILE_BYTES, read_bounded
from jobprep.preprocess import preprocess_document


async def process(args):
    from jobprep.app.pdf_import import import_pdf

    root = Path(args.artifact_dir).resolve()
    if getattr(args, 'document_json', None):
        result = json.loads(read_bounded(Path(args.document_json).resolve(), MAX_FILE_BYTES))
        if not isinstance(result, dict) or result.get('url') != args.source_url:
            raise ValueError('Document source must match the supplied public URL')
        if not isinstance(result.get('images'), list):
            raise ValueError('Document must contain an image list')
    else:
        result = import_pdf(args.path, args.source_url, root,
                            min_image_width=args.min_image_width,
                            min_image_height=args.min_image_height)
    if asyncio.iscoroutine(result):
        result = await result
    if len(result.get('images', [])) > args.max_images:
        raise ValueError('Image count exceeds this run limit; inspect offline output first')
    before = after = None
    if args.ocr:
        settings = load_settings()
        engine = BaiduOCR(settings['data_dir'] / 'ocr',
                          max_calls=settings['ocr_max_calls'], high_accuracy_retry=False,
                          model=settings.get('ocr_model', 'general'))
        before = engine.calls_used
        result = await AppTools(root).ocr(result, engine)
        after = engine.calls_used
    text = '\n\n'.join([result.get('text', '')] +
                         [item.get('text', '') for item in result.get('ocr', [])]).strip()
    terms = [r'\bAgent\b', r'\bAI\s*Infra\b', '\u7b97\u6cd5', '\u8ba1\u7b97\u673a\u89c6\u89c9',
             '\u6821\u56ed\u62db\u8058', '\u6295\u9012', '\u7b80\u5386']
    matches = [line for line in text.splitlines()
               if any(re.search(term, line, re.I) for term in terms)]
    result['preprocessing'] = preprocess_document(result)
    task_id = None
    if getattr(args, 'persist', False):
        from jobprep.store import Store, now, worker_lock

        settings = load_settings()
        with worker_lock(settings['data_dir']):
            store = Store(settings['data_dir'])
            task_id = store.add_task(result['url'], priority=100, kind='manual')
            old = store.task(task_id)
            previous = root / 'previous_task_result.json'
            if old.get('result_json') and not previous.exists():
                with previous.open('x', encoding='utf-8') as handle:
                    handle.write(old['result_json'])
            result.update(fetched_at=now(), semantic_status='unassessed',
                          source_references=store.source_references(task_id))
            store.finish(task_id, result['status'], result)
            store.event(task_id, 'offline_preprocessed', 'source evidence and OCR imported; completeness unverified')
    delivery = {'source_url': result['url'], 'document': result,
                'combined_text': text, 'keyword_evidence': matches,
                'semantic_review_required': True,
                'workstation_task_id': task_id,
                'ocr_calls_this_run': None if before is None else after - before,
                'coverage_note': 'A successful export does not prove every source image or linked JD was included.'}
    target = root / 'preprocessed.json'
    target.write_text(json.dumps(delivery, ensure_ascii=False, indent=2), encoding='utf-8')
    (root / 'article.txt').write_text(text, encoding='utf-8')
    print(json.dumps({'output': str(target), 'status': result['status'],
                      'pages': result.get('page_count'), 'images': len(result.get('images', [])),
                      'ocr_statuses': [item.get('status') for item in result.get('ocr', [])],
                      'ocr_gaps': len(result.get('ocr_gaps', [])), 'text_chars': len(text),
                      'workstation_task_id': task_id,
                      'keyword_evidence': matches, 'ocr_calls_this_run': delivery['ocr_calls_this_run']},
                     ensure_ascii=False))
    return 1 if result.get('status') in ('error', 'blocked', 'deleted') or (args.ocr and (result.get('ocr_gaps') or not text)) else 0


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--path', help='Local PDF exported using the browser Print function')
    source.add_argument('--document-json', help='Existing app-tool document JSON with downloaded image evidence')
    parser.add_argument('--source-url', required=True, help='Public article URL, never a signed browser-session URL')
    parser.add_argument('--artifact-dir', required=True, help='Local output root for PDF evidence, images and text')
    parser.add_argument('--ocr', action='store_true', help='Explicitly upload extracted images to configured Baidu OCR')
    parser.add_argument('--persist', action='store_true', help='Save this result to the workstation task database, retaining previous evidence')
    parser.add_argument('--min-image-width', type=int, default=500, help='Minimum retained image width in pixels (default 500)')
    parser.add_argument('--min-image-height', type=int, default=200, help='Minimum retained image height in pixels (default 200)')
    parser.add_argument('--max-images', type=int, default=20, help='Abort before OCR if retained images exceed this count')
    args = parser.parse_args()
    if not 1 <= args.max_images <= 100:
        parser.error('--max-images must be between 1 and 100')
    return asyncio.run(process(args))


if __name__ == '__main__':
    raise SystemExit(main())
