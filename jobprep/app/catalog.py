"""Inspectable tool metadata. Returned objects are independent JSON-safe copies."""

from copy import deepcopy


def parameter(type_, default=None, *, required=False, limits='', description='', **extra):
    return {'type': type_, 'required': required, 'default': default,
            'range': limits, 'description': description, **extra}


P = parameter
URL = P('string', required=True, limits='HTTP(S), hostname required, no URL credentials', description='Observed source URL; not employer verification')
ARTIFACTS = P('string|PathLike', required=True, limits='caller-owned writable directory', description='Resolved artifact root; created if needed')
WEB_OPTIONS = {
    'browser': P('boolean|string', 'auto', limits='true, false, auto', description='Fresh headless Playwright or HTTP; auto may fall back'),
    'max_pages': P('integer', 3, limits='coerced to >=1', description='Shared page cap across search terms'),
    'max_scrolls': P('integer', 3, limits='coerced to >=0', description='Per-page scroll snapshots'),
    'max_images': P('integer', 8, limits='coerced to >=0', description='Unique image download cap'),
    'timeout': P('number', 20, limits='coerced to >=0.1 seconds', description='Network/browser operation timeout'),
    'retries': P('integer', 2, limits='coerced to >=0', description='Short HTTP retries, not task retries'),
    'domain_delay': P('number', 1.2, limits='coerced to >=0 seconds', description='Per-domain pacing'),
    'delay': P('number', None, limits='>=0 seconds', description='Alias when domain_delay is absent'),
    'settle_ms': P('integer', 500, limits='coerced to >=0 milliseconds', description='Browser settle delay'),
    'cleanup_timeout': P('number', None, limits='coerced to >=0.01 seconds', description='Default min(3, timeout); browser cleanup bound'),
    'search_terms': P('array[string]|string', [], limits='page cap shared by all terms', description='Search via configured input; no inferred employer scope'),
    'search_selector': P('string|null', None, limits='CSS selector', description='Required input selector for applying search terms'),
    'next_selector': P('string|null', None, limits='CSS selector', description='Otherwise rel=next then standard next labels'),
    'browser_channel': P('string|null', None, limits='installed Playwright browser channel', description='Fresh browser, no login profile'),
    'channel': P('string|null', None, description='Alias when browser_channel is absent'),
}
for _name, _default, _description in (
        ('max_html_bytes', 5 * 1024**2, 'HTML response bytes'),
        ('max_image_bytes', 10 * 1024**2, 'Per-image bytes'),
        ('max_total_image_bytes', 30 * 1024**2, 'Total image bytes'),
        ('max_json_bytes', 2 * 1024**2, 'Observed JSON response bytes'),
        ('max_json_responses', 20, 'Observed JSON response count')):
    WEB_OPTIONS[_name] = P('integer', _default, limits='coerced to >=0', description=_description)

PATH = 'dotted string or array of object keys/list indexes; missing paths fail unless explicitly optional'
LIST_CONFIG = {
    'url': URL,
    'method': P('string', 'GET', limits='GET|POST'),
    'company': P('string', '', description='Unverified user label'),
    'scope': P('string', 'configured public list', description='Unverified scope label'),
    'query': P('object', {}, description='Query parameters; copied each page'),
    'body': P('object', {}, description='Request body; copied each page'),
    'headers': P('object', {}, description='Caller-supplied headers; may appear in caller config'),
    'body_encoding': P('string', 'json', limits='json|form'),
    'fields': P('object', {}, limits='field name -> response path', description='title/category/location/dept/description/requirements/url and arbitrary extras; preserved in fields'),
    'pagination': P('object', required=True, properties={
        'path': P('string|array', required=True, limits=PATH),
        'location': P('string', 'query', limits='query|body'),
        'start': P('integer|integer-string', 1, limits='>=0'),
        'increment': P('integer|integer-string', 1, limits='>=1'),
        'max_pages': P('integer|integer-string', 1000, limits='>=1')}),
    'response': P('object', required=True, properties={
        'items_path': P('string|array', required=True, limits=PATH, description='Array of objects'),
        **{name: P('string|array|null', None, limits=PATH, description=description) for name, description in (
            ('id_path', 'Stable ID; absent uses raw-object hash for deduplication'),
            ('total_path', 'Nonnegative total items; must remain consistent'),
            ('page_path', 'Actual page must match requested page'),
            ('total_pages_path', 'Nonnegative declared page count'),
            ('last_page_path', 'Boolean last-page marker'),
            ('success_path', 'Response success marker'))},
        'success_value': P('JSON value', True, description='Exact equality check when success_path exists'),
        'null_items_are_empty': P('boolean', False, description='Explicit null becomes empty only when true')}),
    'http': P('object', {}, properties={
        'timeout': P('number', 30, limits='finite >0 seconds'),
        'connect_timeout': P('number', 10, limits='finite >0 seconds'),
        'interval': P('number', 0.5, limits='finite >=0 seconds'),
        'retries': P('integer|integer-string', 3, limits='>=0', description='Existing collector HTTP retries only'),
        'backoff': P('number', 1, limits='finite >=0 seconds; exponential per attempt')})}

DOCUMENT_OUTPUT = {
    'type': 'document dict',
    'fields': ['url', 'final_url', 'title', 'text', 'status', 'method', 'html_path',
               'images', 'links', 'jobs', 'coverage', 'warnings', 'evidence', 'artifact_dir'],
    'coverage': 'complete never follows list_complete alone; JD/image/detail gaps remain explicit',
    'images': 'url/path/sha256/status/order and available width/height/error',
    'jobs': 'Existing DOM or list_collect schema, without inferred URLs or employer verification'}


def tool_catalog():
    """Return a mapping of six tool names to parameters, outputs and boundaries."""
    common = ['No LLM, runner, adapter, store, queue retry or automatic OCR',
              'Caller owns artifacts and task scheduling; cancellation of to_thread does not stop its worker']
    web = {'url': URL, 'artifact_dir': ARTIFACTS,
           'options': P('object|null', None, properties=WEB_OPTIONS)}
    wechat_options = {
        'max_images': P('integer', 30, limits='0..100'),
        'timeout': P('number', 20, limits='finite 1..120 seconds'),
        'interval': P('number', 15, limits='finite 0..300 seconds'),
        'max_requests': P('integer', 80, limits='1..200'),
        'browser_channel': P('string|null', 'chrome', limits='chrome|msedge|null', description='Fresh public browser context'),
        'max_html_bytes': P('integer', 5 * 1024**2, limits='1..10485760 bytes', description='Bounded HTML response bytes; strictly validated'),
        'max_image_bytes': P('integer', 10 * 1024**2, limits='1..10485760 bytes', description='Bounded per-image bytes; strictly validated'),
        'max_total_image_bytes': P('integer', 30 * 1024**2, limits='1..31457280 bytes', description='Bounded total image bytes; strictly validated'),
        'images_only': P('boolean', False, description='Omit text; retain warnings/evidence; complete stays false')}
    list_output = {**DOCUMENT_OUTPUT, 'status': 'ok|partial|error',
                   'extra_fields': ['json_paths', 'output_paths', 'retryable', 'error_kind', 'http_status'],
                   'jobs': 'Unmodified list_collect records including fields/raw/raw_ref/raw_index/page/needs_details/url_provenance',
                   'complete': 'list_complete AND jd_complete; list terminal success may be ok with complete=false'}
    catalog = {
        'web': {'parameters': web, 'output': DOCUMENT_OUTPUT,
                'limits': common + ['Dynamic crawler import; options normalization and network limits follow existing crawler', 'Discovered details are not fetched automatically']},
        'wechat': {'parameters': {**web, 'url': {**URL, 'range': 'HTTP(S) mp.weixin.qq.com URL, no credentials'},
                                 'options': P('object|null', None, properties=wechat_options)},
                   'output': DOCUMENT_OUTPUT, 'limits': common + ['Public browser and CDN image collection with persistent bounded cache; method=wechat',
                       'Strict option validation; no legacy crawler, desktop session or certificate fallback',
                       'Single article only; adapter marks pagination unsupported and requested search unapplied',
                       'Acquisition failures are nonretryable',
                       'Blocked/deleted pages do not download images or run OCR', 'images_only does not prove textual or semantic completeness']},
        'file': {'parameters': {'path': P('string|PathLike', required=True, limits='Existing .html/.htm or raster image, never .env'),
                                'source_url': URL, 'kind': P('string', required=True, limits='html|image'), 'artifact_dir': ARTIFACTS},
                 'output': DOCUMENT_OUTPUT, 'limits': common + ['HTML <=30 MiB; image <=10 MiB; dimensions <=20000 each; <=40 million pixels',
                    'Adjacent images resolve below HTML parent, including symlink resolution; no remote fetch',
                    'At most first 100 img nodes; total copied adjacent images <=30 MiB',
                    'Images: .png/.jpg/.jpeg/.gif/.webp/.bmp/.tif/.tiff; validate actual image; unresolved images stay pending_manual_image']},
        'ocr': {'parameters': {'result': P('object', required=True, description='Acquired document with image paths and artifact_dir'),
                               'engine': P('object', required=True, description='Injected async recognize(Path)->dict; engine owns budget/cache')},
                'output': {**DOCUMENT_OUTPUT, 'extra_fields': ['ocr', 'ocr_gaps', 'acquisition_status'], 'status': 'Input status or pending_ocr when gaps exist'},
                'limits': ['No engine creation or budget mutation; copy input; call recognize per safe image',
                    'Root: AppTools(artifact_dir), then result.artifact_dir, then engine.cache_dir.parent/artifacts',
                    'Resolved paths must remain in root; suffix/bytes/dimensions/hash checked; missing root is a gap',
                    'Blocked/deleted/error acquisition skips OCR; successful OCR never upgrades acquisition coverage',
                    'Decorative/skipped_duplicate without paths are ignored; other missing paths are gaps']},
        'public_list': {'parameters': {'config': P('object', required=True, properties=LIST_CONFIG), 'artifact_dir': ARTIFACTS},
                        'output': list_output, 'limits': common + ['Run generic collect via asyncio.to_thread; preserve JSONL/CSV and all raw references',
                            'Counts, IDs, repeated pages and terminal evidence follow generic collector; no detail fetch',
                            'Bad configuration raises ValueError; acquisition failures return canonical documents']},
        'zhiye_list': {'parameters': {'url': {**URL, 'range': 'HTTP(S) *.zhiye.com URL'},
                                     'artifact_dir': ARTIFACTS,
                                     'options': P('object|null', None, properties=WEB_OPTIONS)},
                        'output': DOCUMENT_OUTPUT,
                        'limits': common + ['Fresh public browser session; no login profile',
                                            'Observed public JobAd API only; 100 records per page and max_pages bounds pagination',
                                            'A job skips detail fetch only when both structured Duty and Require are substantive']},
        'feishu_list': {'parameters': {'url': {**URL, 'range': 'HTTP(S) *.jobs.feishu.cn URL'}, 'artifact_dir': ARTIFACTS,
                                     'options': P('object|null', None, properties={
                                         'company': P('string', 'configured company', description='Unverified label'),
                                         'max_pages': P('integer', 100, limits='1..1000'),
                                         'timeout': P('integer', 25000, limits='100..120000 milliseconds per UI operation'),
                                         'next_selector': P('string', 'li.atsx-pagination-next', limits='nonempty CSS selector'),
                                         'browser_channel': P('string|null', 'chrome', description='null uses bundled Chromium'),
                                         'interval': P('number', 1.2, limits='finite 0..60 seconds'),
                                         'max_response_bytes': P('integer', 5 * 1024**2, limits='1..31457280 bytes; checked after browser receives body')})},
                        'output': list_output, 'limits': common + ['Fresh headless normal UI only; no login profile or fabricated signatures',
                            'Reuse initial offset=0 response; positive integer limit; filters must remain exactly equal',
                'Initial list requires HTTP 200 (normal frontend may emit 405 first); later page failures preserve gaps',
                            'No invented detail URLs; no scripts dependency; browser operation timeout is not a total run deadline',
                            'Unknown options rejected; Playwright and browser must already be available']}}
    return deepcopy(catalog)
