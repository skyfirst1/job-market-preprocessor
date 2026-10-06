# Acquisition adapters

`jobprep.adapters` is the deterministic routing layer between the runner and
application tools. It does not import runner, store, settings, OCR engines or
CLI modules. It never loads `.env`, performs OCR, filters jobs by keywords,
constructs speculative portal routes, or owns a retry queue.

## Contract

```python
from jobprep.adapters import AdapterRegistry

registry = AdapterRegistry(config=settings_config)  # already loaded by caller
result = await registry.acquire(url, artifact_dir, options=merged_options, tools=tools)
adapter = registry.select(url, options=merged_options)
catalog = registry.describe()  # JSON-safe list for GET /adapters
```

`config` and `options` must be dictionaries (or `None`). The registry copies
them and never mutates caller state. `tools=None` lazily constructs
`jobprep.app.AppTools()` after URL, configuration and selection validation.
Importing this package does not import AppTools or load settings. `artifact_dir`
is passed to tools unchanged; tools own artifact creation and validation.

The application contract is async `web(url, artifact_dir, options=None)`,
`wechat(url, artifact_dir, options=None)`, `public_list(config, artifact_dir)`
and `feishu_list(url, artifact_dir, options=None)`. AppTools also exposes `file`
and `ocr`; those remain outside URL acquisition routing.

Each tool must return a dictionary with HTTP(S) `url`, a canonical `status`
(`ok`, `partial`, `error`, `blocked`, `deleted`), object lists `jobs` and `images`,
and dictionary `coverage`. `complete` and `list_complete`, when present, must
be booleans. Missing completeness fields default to false. Optional `links`
accepts strings or dictionaries with `url`/`href`; optional `warnings` is a list.
Invalid result schema becomes a permanent error after that single tool call.
Other canonical fields, jobs, images, artifact references, coverage details and
tool metadata are preserved without keyword filtering.

`describe()` returns one entry per registered adapter with `name`, `priority`,
`scope` and `parameters`, sorted by priority. It performs no configuration-file
reads and includes no configured endpoint values or credentials. App tool
catalogs (`GET /tools`) remain the application's responsibility.

## Selection And Scope

| Adapter name | Priority | Automatic support | Calls |
| --- | ---: | --- | --- |
| `ConfiguredPublicList` | 200 | `options.list_config` or an exact URL key in `config.list_sources` | `public_list` once |
| `WechatImage` | 100 | Exact hostname `mp.weixin.qq.com` | `wechat` once |
| `FeishuPublicPortal` | 50 | `jobs.feishu.cn` or its subdomains, with a `/position/list` route or root landing | `feishu_list` once; landing discovery adds one `web` call |
| `genericweb` | -100 | Other HTTP(S) URLs | `web` once |

URL hosts are parsed, case-insensitive and matched on hostname boundaries.
`mp.weixin.qq.com.evil.example`, `evilmp.weixin.qq.com` and
`tenant.jobs.feishu.cn.evil.example` do not match their specialized adapters.
URLs containing credentials, invalid ports, whitespace, backslashes or
non-HTTP(S) schemes fail before tool execution.

`options['adapter']` explicitly selects an exact registered name. Unknown names
or an adapter whose scope does not match return a permanent error and make no
tool calls. `select()` raises `ValueError` for the same conditions; `acquire()`
returns the canonical error with acquisition metadata. An explicit
`genericweb` can be used for a supported HTTP(S) source. Malformed supplied
public-list configuration is validated before routing and does not silently
fall back to generic acquisition.

Feishu list recognition requires the complete path-segment suffix
`/position/list` (with an optional trailing slash and query). It accepts
`/2027/position/list`, `/huixicampus/position/list`, `#/position/list`,
`#/2027/position/list` and `#!/position/list`. Detail routes such
as `/position/123` and `/position/detail/123` go through genericweb. A known
public hostname alone does not turn arbitrary pages into lists.

For a root landing (`/`, optionally `#/`), the adapter first calls `web` with
`max_images=0`, `max_pages=1`, `max_scrolls=0`, `search_terms=[]`. It reads only
links returned by that tool; it never reads arbitrary HTML paths. Relative
links resolve against the returned final URL. Only an observed same-host
position/list link is eligible. Duplicate identical links collapse; exactly
one distinct eligible link is required. With no link or multiple distinct
links, the captured landing is returned as `partial`, with both completeness
flags false and `list_link_not_found` or `ambiguous_list_links`. Blocked,
deleted or failed landings stop immediately. A successful discovery invokes
`feishu_list` for that link, retains the input source `url`, and records the
actual endpoint in `coverage.list_url` and provenance.

## Options

The caller already merges defaults, site overrides and runtime overrides via
`options_for_url`. The registry does not repeat that merge. Adapter defaults
are applied only where documented below.

`genericweb` forwards tool options unchanged after removing registry-only
`adapter` and `list_config`. HTTP/browser auto selection and bounded fallback
are owned by AppTools.web; the adapter adds no HTTP/browser retry sequence.

`WechatImage` defaults `images_only=False`, retaining article text and images;
a boolean runtime/site value can override it. AppTools.wechat directly calls
the async public-browser/CDN collector, with persistent bounded caching and
`method=wechat`. No desktop agent operation, WeChat session, legacy crawler or
certificate/GUI fallback is involved. Only `max_images`, `timeout`,
`browser_channel`, `interval`, `max_requests`, `max_html_bytes`,
`max_image_bytes`, `max_total_image_bytes`, and `images_only` reach the collector.
`channel` maps to `browser_channel` when absent; `domain_delay` or `delay` maps
to `interval` when absent, with `domain_delay` taking precedence.
Other shared option names are recorded in translation provenance.

Defaults are `max_images=30` (0..100), `timeout=20` seconds (1..120),
`interval=15` seconds (0..300), `max_requests=80` (1..200),
`browser_channel=chrome` (`chrome`, `msedge`, or null), and HTML/per-image/total-image
byte limits of 5/10/30 MiB. The collector strictly validates supplied limits.
`browser` and `max_scrolls` do not switch this collector's transport.
`coverage.scope=single_article`, `pagination_applied=False`, and
`list_complete=False` identify single-article coverage; supplied `max_pages` is
recorded as `requested_max_pages`. Requested `search_terms` sets
`search_applied=False` and false completeness; successful acquisition becomes
`partial` with `stop_reason=search_not_applied`. Failed/blocked/deleted evidence
keeps its terminal stop reason. All WeChat failures, including exceptions from
injected tools, are nonretryable and perform only one tool call.

A tool `blocked` status is terminal. Explicit structured
`challenge=True`, `captcha_required=True`, or coverage stop reasons
`challenge`, `captcha`, `wechat_challenge`, `authentication_required` also
produce `blocked` and false completeness. Detection of page content/challenge
DOM belongs to AppTools. No bypass, challenge solving or second attempt is
performed by the adapter.

`FeishuPublicPortal` accepts shared `timeout` in **seconds** (default 25,
range 0.1..120) and converts it to AppTools.feishu_list milliseconds. It
forwards `max_pages`, `company`, `next_selector`, `browser_channel`,
`max_response_bytes`, and `interval`. The tool supplies its own defaults,
including `max_pages=100` when absent. `domain_delay` or `delay` maps to
`interval` if no explicit interval exists; `channel` maps to `browser_channel`
if absent. Thus a caller's site override `max_pages=100` is honored. Shared
web-only keys such as `max_images` and `max_scrolls` are not sent to the strict
Feishu list tool; their names are recorded in provenance. `browser=False`
fails permanently because this tool requires normal browser UI. The adapter
does not infer employer identity from a hostname; `company` is a user label.

The Feishu list tool does not apply `search_terms`. If requested, its returned
jobs are retained, but `search_applied=False`, `stop_reason=search_not_applied`,
and both completeness flags become false. An otherwise `ok` result becomes
`partial`. Adapter routing never filters the returned jobs itself.

`ConfiguredPublicList` uses the **entire supplied list config** independently
of generic crawl options. In particular, crawl `max_pages=3`, `timeout` or
`retries` cannot overwrite `list_config.pagination.max_pages=100` or
`list_config.http`. To override list collection at runtime, provide a complete
`options.list_config` object or a trusted workspace/config JSON path; it takes
precedence over an exact mapping. CLI `--list-config config/source.json` uses
this same path resolution. Selection of exactly one explicit URL is enforced
by API/CLI; batches use `list_sources` mappings.

```python
settings_config = {
    'list_sources': {
        'https://careers.tencent.com/search.html': 'config/tencent_public.json',
        'https://hr.163.com/job-list.html': 'config/netease_public.json',
    },
}
list_config = {
    'url': 'https://api.example.test/public/jobs',
    'method': 'GET',
    'pagination': {'path': 'page', 'max_pages': 100},
    'response': {'items_path': 'data.items', 'total_path': 'data.total', 'id_path': 'id'},
    'fields': {'title': 'title', 'description': 'description', 'url': 'detailUrl'},
    'http': {'retries': 0, 'timeout': 30, 'interval': 0.5},
}
result = await registry.acquire(source_url, artifact_dir,
                                options={'list_config': list_config}, tools=tools)
```

`list_sources` must be a dictionary keyed by the exact input URL string. No
query removal, trailing-slash normalization, keyword matching or substring
matching takes place. Values are inline config dictionaries or JSON paths.
The identical resolver and path restrictions apply to explicit
`options.list_config` strings/Path objects and mapped `list_sources` paths.
Relative paths are workspace-relative (e.g. `config/tencent_public.json`);
absolute paths are allowed only inside the resolved workspace `config`
directory. Traversal and resolved symlink/junction escapes are rejected.
The trusted workspace comes from the package location, not `config['root']`.
Only `.json` files are read, capped at 1 MiB; `.env` is never read. UTF-8 and
UTF-8 BOM are accepted. Duplicate JSON keys, nonfinite constants, missing files,
invalid JSON and non-object configurations are permanent errors.

List config requires HTTP(S) `url`, `pagination.path`, and
`response.items_path`. Methods are GET/POST; pagination location is query/body;
body encoding is json/form. Paths are nonempty dotted strings or lists of
nonempty keys/nonnegative integer indices. `pagination.start` is an integer
>=0, `increment` >=1, `max_pages` 1..10000; booleans are rejected as numbers.
`response.*_path` and field mappings are validated. Query/body/headers/fields/
http must be dictionaries; `null_items_are_empty` must be boolean. HTTP
timeout/connect_timeout are positive finite numbers, interval/backoff are
nonnegative finite numbers, retries is an integer 0..10. The tool performs
its own collector validation as well. No adapter retry loop is added; configured
tool retries remain tool-owned. Source `url` remains the requested source;
the configured endpoint is retained in `coverage.list_url` and tool provenance.

## Coverage And Errors

HTTP success, returned jobs and landing discovery alone do not prove list or
JD completeness. Adapters preserve the tool's evidence-based flags. Missing
flags, unsuccessful statuses, unsuccessful discovery or unapplied requested
search cannot be promoted to complete. `list_complete=True` says enumeration
of the tool's declared scope ended; `complete=True` additionally depends on
detail/image evidence. The adapter never asserts that one portal channel
represents all company jobs and does not recalculate totals or infer terminal
pagination from a page-size guess.

Every result includes `retryable`, `error_kind`, `http_status` (null if unknown),
plus `acquisition`:

```json
{
  "adapter": "FeishuPublicPortal",
  "selection": "rules",
  "attempts": [{"tool": "web", "url": "https://tenant.jobs.feishu.cn/", "ordinal": 1, "status": "ok"}],
  "provenance": []
}
```

An attempt is recorded before invoking its tool, including an exception's
type on failure. Provenance records tool URLs/final URLs, method, coverage,
HTML/JSON/evidence references, original tool attempts/provenance and acquisition
metadata. Discovery and Feishu option translation add explicit rule records.
Existing result `acquisition` is retained under `acquisition.tool_metadata`.
The trace is an adapter-call history; tool-owned transport attempts remain
separate, avoiding claims that a single adapter call was a single HTTP request.
Exception messages are never persisted: `error` and `warnings` contain only
the exception type plus a fixed generic description. Arbitrary exception
strings containing tokens, passwords or Authorization headers are not copied
into the result or trace. Known exception HTTP status is retained separately.

Unknown/unsupported explicit adapters use `error_kind=unsupported_adapter`;
configuration/option failures use `invalid_configuration`; invalid responses
use `invalid_schema`. All are permanent. Unsupported tool implementations
use `unsupported_tool`. Known timeout/connection/httpx transport exceptions
use `transport_error` and `retryable=True`, except WeChat exceptions, which
always remain nonretryable. Unclassified exceptions are
nonretryable `tool_exception`. Known HTTP 401/403/429 are blocked and 404/410
are deleted; neither retries. Other HTTP error classification uses known
status codes, permitting 408/425/500/502/503/504 unless the tool explicitly
declared its own retryability. Application error classifications are retained
where possible. No status code is invented from document text or titles.
`blocked`/`deleted` always force false completeness and retryable=false.

There is no adapter retry loop, including on schema failure, blocked pages or
unsupported tools. Feishu root discovery has a fixed maximum of two tool calls;
other built-ins have one. The runner owns retry scheduling and queue policy.

## Extension

```python
from urllib.parse import urlsplit
from jobprep.adapters import BaseAdapter

class ExamplePortal(BaseAdapter):
    name = 'ExamplePortal'
    priority = 250
    scope = 'Exact public careers.example.test hostname'
    parameters = {'max_pages': {'type': 'integer'}}

    def matches(self, url, options):
        return urlsplit(url).hostname == 'careers.example.test'

    async def acquire(self, context):
        return await context.call('web', context.url, context.options)

registry.register(ExamplePortal())
assert registry.select('https://careers.example.test/').name == 'ExamplePortal'
```

An adapter supplies `name`, integer `priority`, synchronous
`matches(url, options)`, and async `acquire(context)`. Use `context.call` to
invoke supported app tools with canonical validation and trace capture.
Context exposes `url`, `artifact_dir`, copied tool `options`, resolved
`list_config`, `tools`, `attempts` and `provenance`. Higher priority wins;
ties preserve registration order. Classes with no-argument constructors may
also be registered. Duplicate names raise unless `replace=True`; a registry
priority override is available via `register(adapter, priority=300)`.
Extension `scope`/`parameters` must be JSON-safe for `describe()`. Keep
matching deterministic and side-effect-free, transport inside tools, and
coverage assertions tied to observed evidence.

## Offline Verification

```powershell
.venv\Scripts\python.exe -m pytest tests/test_adapters.py -q -p no:cacheprovider
```

Tests use async mock tools and temporary JSON fixtures. They cover exact host
and route boundaries, root discovery budgets, challenge stops, override
precedence, independent list pagination, malformed schema/config, secure path
resolution, permanent explicit-selection errors, error classification,
lazy tool imports and registry extension/catalog behavior. One read-only App
validator test verifies Feishu parameter compatibility. No real network,
browser, OCR or environment-file read is involved.
