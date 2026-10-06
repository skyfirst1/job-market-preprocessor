import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

from .settings import load_settings


def _wechat_settings(root):
    config = load_settings(root) if root is not None else load_settings()
    config['runner'] = {**config.get('runner', {}), 'automatic_retry': False,
                        'wait_for_retries': False}
    return config


def _validate_wechat(parser, args):
    for name, low, high in [('limit', 1, 5), ('max_images', 0, 100),
                            ('max_requests', 1, 200), ('interval', 1, 300)]:
        if not low <= getattr(args, name) <= high:
            parser.error(f'--{name.replace("_", "-")} must be {low}..{high}')
    if len(args.url) > 5:
        parser.error('wechat-run accepts at most five --url arguments')
    from .app.wechat_public import public_url

    try:
        args.url = [public_url(url) for url in args.url]
    except ValueError:
        parser.error('--url requires a public WeChat article link without session parameters')


def _wechat_evidence(result, task, root):
    from .app.tools import (HTML_SUFFIXES, IMAGE_SUFFIXES, MAX_FILE_BYTES,
                            MAX_IMAGE_BYTES, image_dimensions, read_bounded)
    from .app.wechat_public import public_url

    if not isinstance(result, dict) or public_url(result.get('url')) != task['url']:
        raise ValueError('invalid_cached_result')
    if result.get('status') not in ('ok', 'partial', 'pending_ocr', 'blocked', 'error', 'deleted'):
        raise ValueError('invalid_cached_result')
    root = root.resolve()

    def artifact(value, suffixes, limit):
        path = Path(value)
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() not in suffixes:
            raise ValueError('unsafe_cached_artifact')
        return read_bounded(path, limit)

    paths = list(result.get('html_paths') or [])
    if result.get('html_path'):
        paths.append(result['html_path'])
    for path in dict.fromkeys(paths):
        artifact(path, HTML_SUFFIXES, MAX_FILE_BYTES)
    for image in result.get('images', []):
        if image.get('path'):
            body = artifact(image['path'], IMAGE_SUFFIXES, MAX_IMAGE_BYTES)
            image_dimensions(body)
            if not image.get('sha256') or hashlib.sha256(body).hexdigest() != image['sha256']:
                raise ValueError('cached_image_hash_mismatch')
        elif image.get('status') == 'ok':
            raise ValueError('missing_cached_image')


def _wechat_needs_ocr(result):
    if result['status'] in ('blocked', 'error', 'deleted'):
        return False
    images = [(index, image) for index, image in enumerate(result.get('images', []))
              if image.get('status') not in ('decorative', 'skipped_duplicate')]
    recognized = {item.get('image_index') for item in result.get('ocr', [])
                  if item.get('status') == 'ok'}
    gaps = result.get('ocr_gaps') or result.get('coverage', {}).get('ocr_gaps')
    if images and not gaps and all(index in recognized for index, _ in images):
        return False
    return bool(gaps or
                result.get('ocr_skipped') or
                any(index not in recognized for index, _ in images))


def _wechat_export(workstation, task_id, result):
    from .preprocess import preprocess_document

    root = (workstation.config['export_dir'] / 'wechat').resolve()
    target = (root / task_id).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Unsafe export directory')
    target.mkdir(parents=True, exist_ok=True)
    derived = preprocess_document(result)
    document = target / 'document.json'
    article = target / 'article.txt'
    document.write_text(json.dumps({**result, 'derived': derived}, ensure_ascii=False, indent=2),
                        encoding='utf-8')
    article.write_text(derived['compact_text'], encoding='utf-8')
    return {'id': task_id, 'url': result.get('url'), 'status': result['status'],
            'text_chars': len(derived['compact_text']), 'images': len(result.get('images', [])),
            'images_saved': sum(bool(image.get('path')) and image.get('status') == 'ok'
                                for image in result.get('images', [])),
            'image_complete': result.get('coverage', {}).get('image_complete', False),
            'ocr_gaps': len(result.get('ocr_gaps', [])),
            'document_path': str(document), 'article_path': str(article)}


def _wechat_run(workstation, args):
    from .store import worker_lock

    cached, runnable, stopped, exports = [], [], [], []
    engine = None
    with worker_lock(workstation.store.root):
        ids = list(dict.fromkeys(workstation.store.add_task(url, priority=10000)
                                 for url in args.url))
        selected = ids[:args.limit]
        for task_id in selected:
            task = workstation.store.task(task_id)
            if task.get('result_json'):
                try:
                    result = json.loads(task['result_json'])
                    _wechat_evidence(result, task, workstation.store.root / 'artifacts')
                except (ValueError, TypeError, OSError, KeyError, AttributeError):
                    stopped.append({'id': task_id, 'status': task['status'],
                                    'stop_reason': 'invalid_cached_result'})
                    continue
                updated = False
                if not args.no_ocr and workstation.config.get('ocr_enabled', True) and _wechat_needs_ocr(result):
                    engine = engine or workstation.ocr_engine()
                    result = asyncio.run(workstation.add_ocr(result, engine))
                    if not result.get('ocr_gaps'):
                        result.get('coverage', {}).pop('ocr_gaps', None)
                    if not _wechat_needs_ocr({**result, 'ocr_skipped': False}):
                        result.pop('ocr_skipped', None)
                        if result['status'] == 'pending_ocr':
                            acquisition = result.get('acquisition_status')
                            result['status'] = acquisition if acquisition in ('ok', 'partial') else 'partial'
                        result.get('coverage', {}).pop('ocr_gaps', None)
                    workstation.store.finish(task_id, result['status'], result)
                    updated = True
                summary = _wechat_export(workstation, task_id, result)
                cached.append({**summary, 'cached': True, 'ocr_updated': updated})
                exports.append(summary)
            elif task['attempts'] >= workstation.config['runner'].get('max_attempts', 3):
                stopped.append({'id': task_id, 'url': task['url'], 'status': task['status'],
                                'stop_reason': 'attempt_budget_exhausted'})
            else:
                runnable.append(task_id)
        workstation.store.retry(['error', 'blocked', 'partial', 'pending_ocr',
                                 'retry_wait', 'ok', 'deleted'], runnable, reset_attempts=False)
    options = {'adapter': 'WechatImage', 'images_only': False,
               'max_images': args.max_images, 'max_requests': args.max_requests,
               'interval': args.interval, 'browser_channel': args.browser_channel,
               'wait_retries': False}
    if args.no_ocr:
        options['skip_ocr'] = True
    report = (asyncio.run(workstation.run(limit=len(runnable), ai_only=False,
                                         task_ids=runnable, options=options))
              if runnable else {'processed': 0, 'results': [], 'stats': workstation.store.stats()})
    with worker_lock(workstation.store.root):
        for task_id in runnable:
            task = workstation.store.task(task_id)
            if not task.get('result_json'):
                continue
            try:
                result = json.loads(task['result_json'])
                _wechat_evidence(result, task, workstation.store.root / 'artifacts')
            except (ValueError, TypeError, OSError, KeyError, AttributeError):
                stopped.append({'id': task_id, 'status': task['status'],
                                'stop_reason': 'invalid_cached_result'})
                continue
            exports.append(_wechat_export(workstation, task_id, result))
        for item in stopped:
            task = workstation.store.task(item['id'])
            exports.append(_wechat_export(workstation, item['id'],
                           {'url': task['url'], 'status': 'error',
                            'coverage': {'complete': False, 'stop_reason': item['stop_reason']}}))
    report.update(cached=cached, stopped=stopped, task_ids=selected,
                  deferred_task_ids=ids[args.limit:], exports=exports)
    return report


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Local recruitment acquisition workstation')
    parser.add_argument('--root', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    importer = commands.add_parser('import-csv')
    importer.add_argument('path', type=Path, nargs='?', default=Path('job_market_raw.csv'))
    commands.add_parser('status')
    commands.add_parser('tools', help='List app tools and detailed parameter schemas')
    commands.add_parser('adapters', help='List adapter support and extension boundaries')
    wechat = commands.add_parser('wechat-download', help='Local-only single-article pilot; no retry or remote fallback')
    wechat.add_argument('--url', required=True)
    wechat.add_argument('--execute', action='store_true', help='Send the sole permitted download call; default is metadata preflight')
    wechat_run = commands.add_parser('wechat-run', help='Bounded public WeChat acquisition with durable cache reuse')
    wechat_run.add_argument('--url', required=True, action='append')
    wechat_run.add_argument('--limit', type=int, default=1)
    wechat_run.add_argument('--no-ocr', action='store_true')
    wechat_run.add_argument('--max-images', type=int, default=30)
    wechat_run.add_argument('--max-requests', type=int, default=80)
    wechat_run.add_argument('--interval', type=int, default=15)
    wechat_run.add_argument('--browser-channel', choices=['chrome', 'msedge'], default='chrome')
    budget = commands.add_parser('ocr-budget')
    budget.add_argument('--max-calls', type=int, required=True)
    runner = commands.add_parser('run')
    runner.add_argument('--limit', type=int, default=10)
    runner.add_argument('--all', action='store_true')
    runner.add_argument('--url', action='append', default=[])
    runner.add_argument('--browser', choices=['auto', 'true', 'false'])
    runner.add_argument('--max-pages', type=int)
    runner.add_argument('--search-term', action='append')
    runner.add_argument('--adapter', help='Explicit registered adapter name; unknown names fail')
    runner.add_argument('--list-config', type=Path, help='Public-list JSON under workspace/config')
    runner.add_argument('--no-ocr', action='store_true', help='Acquire evidence without paid OCR')
    runner.add_argument('--no-wait-retries', action='store_true', help='Persist retry schedule without waiting')
    runner.add_argument('--reset-attempts', action='store_true', help='Reset attempt budget for explicitly supplied URLs')
    retry = commands.add_parser('retry')
    retry.add_argument('--status', action='append', default=[])
    retry.add_argument('--id', action='append')
    retry.add_argument('--reset-attempts', action='store_true')
    exporter = commands.add_parser('export')
    exporter.add_argument('--outdir', type=Path)
    manual = commands.add_parser('import-file')
    manual.add_argument('path', type=Path)
    manual.add_argument('--url', required=True)
    manual.add_argument('--kind', choices=['html', 'image'], default='html')
    manual.add_argument('--no-ocr', action='store_true')
    server = commands.add_parser('serve')
    server.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if args.command == 'run' and args.list_config and len(args.url) != 1:
        parser.error('--list-config requires exactly one --url; use list_sources for batch routing')
    if args.command == 'wechat-run':
        _validate_wechat(parser, args)
        config = _wechat_settings(args.root)
    else:
        config = load_settings(args.root) if args.root else load_settings()
    if args.command == 'wechat-download':
        from .app.wechat_local import LocalWechatDownload

        pilot = LocalWechatDownload(args.url, config['data_dir'] / 'wechat_download_pilot.sqlite3')
        print(json.dumps(asyncio.run(pilot.run(execute=args.execute)), ensure_ascii=False, indent=2))
        return
    if args.command == 'tools':
        from .app import tool_catalog

        print(json.dumps(tool_catalog(), ensure_ascii=False, indent=2))
        return
    if args.command == 'adapters':
        from .adapters import AdapterRegistry

        print(json.dumps(AdapterRegistry(config).describe(), ensure_ascii=False, indent=2))
        return
    from .pipeline import Workstation

    workstation = Workstation(config)
    if args.command == 'wechat-run':
        result = _wechat_run(workstation, args)
    elif args.command == 'import-csv':
        result = workstation.store.import_csv(args.path)
    elif args.command == 'status':
        result = workstation.store.stats()
        try:
            result['ocr'] = workstation.ocr_engine().status()
        except AttributeError:
            pass
    elif args.command == 'ocr-budget':
        from .store import worker_lock

        with worker_lock(workstation.store.root):
            engine = workstation.ocr_engine()
            engine.set_budget(args.max_calls)
            result = engine.status()
    elif args.command == 'run':
        ids = None
        if args.url:
            ids = [workstation.store.add_task(url, priority=10000) for url in args.url]
            workstation.store.retry(['error', 'blocked', 'partial', 'pending_ocr', 'retry_wait'], ids,
                                    reset_attempts=args.reset_attempts)
        options = {}
        if args.browser:
            options['browser'] = {'auto': 'auto', 'true': True, 'false': False}[args.browser]
        if args.max_pages:
            options['max_pages'] = args.max_pages
        if args.search_term:
            options['search_terms'] = args.search_term
        if args.adapter:
            options['adapter'] = args.adapter
        if args.list_config:
            options['list_config'] = str(args.list_config)
        if args.no_ocr:
            options['skip_ocr'] = True
        if args.no_wait_retries:
            options['wait_retries'] = False
        result = asyncio.run(workstation.run(args.limit, not args.all, ids, options))
    elif args.command == 'retry':
        from .store import worker_lock

        with worker_lock(workstation.store.root):
            result = {'requeued': workstation.store.retry(args.status or ['error', 'pending_ocr'], args.id, args.reset_attempts)}
    elif args.command == 'export':
        from .exporter import export_review

        result = export_review(workstation.store, args.outdir or config['export_dir'])
    elif args.command == 'import-file':
        result = asyncio.run(workstation.import_file(args.path, args.url, args.kind, skip_ocr=args.no_ocr))
    else:
        import uvicorn
        from .api import create_app

        uvicorn.run(create_app(config), host='127.0.0.1', port=args.port)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
