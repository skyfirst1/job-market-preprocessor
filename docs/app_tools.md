# Application Tools

`AppTools` contains six reusable async tools. No tool imports the queue, store,
adapter registry or runner. No tool invokes an LLM. Acquisition does not implicitly
invoke OCR. Existing primitives own HTTP, browser, extraction and Baidu logic.

## Parameter Reference

The complete nested parameter reference is generated from `tool_catalog()`:

```powershell
.venv\Scripts\python.exe -m jobprep tools
```

`GET /tools` returns the same JSON-safe catalog: types, required fields, defaults,
units, ranges, descriptions, output fields and behavioral limits. It includes
all web options, nested public-list request/response paths and Feishu options.
Metadata contains no loaded credentials or configured private endpoint values.

The separately gated local-only desktop downloader pilot is documented in
[Local WeChat call](wechat_local_call.md). It is not part of automatic acquisition
or retries; it limits tool calls, not the downloader's internal HTTP requests.

## Calling Convention

```python
from jobprep.app import AppTools, tool_catalog

tools = AppTools(artifact_dir=trusted_artifacts)
document = await tools.web(url, trusted_artifacts, options={"browser": "auto"})
document = await tools.ocr(document, existing_baidu_engine)
```

All acquisition source URLs require HTTP(S), a hostname and no embedded login
credentials. `artifact_dir` is a caller-owned writable directory. Artifacts use
resolved paths; local imports and OCR verify path containment, limits and images.
Pin `AppTools(artifact_dir=...)` when processing evidence supplied by other callers.

| Tool | Required arguments | Optional arguments | Behavior |
| --- | --- | --- | --- |
| `web` | `url`, `artifact_dir` | `options=None` | HTTP/browser acquisition, observed pagination/search, bounded images and XHR evidence |
| `wechat` | `url`, `artifact_dir` | `options=None` | Exact `mp.weixin.qq.com`; public browser and CDN images; text and images retained by default |
| `file` | `path`, `source_url`, `kind`, `artifact_dir` | None | Local `kind=html` or `image`; does not download remote HTML images |
| `ocr` | `result`, `engine` | None | Adds OCR and gap records; caller supplies configured engine and durable budget |
| `public_list` | `config`, `artifact_dir` | None | GET/POST list requests, configured pagination and response paths, raw evidence/CSV/JSONL |
| `feishu_list` | `url`, `artifact_dir` | `options=None` | Fresh normal browser UI, observed request bodies and next-page responses |

## Web And WeChat

Web defaults: `browser=auto`, `max_pages=3`, `max_scrolls=3`, `max_images=8`,
`timeout=20` seconds, `retries=2`, `domain_delay=1.2` seconds. HTML/image/JSON byte
limits and CSS `search_selector`/`next_selector` are documented in the catalog.
Search without an observed/configured input is marked unapplied, not inferred.
Page limits are shared across search terms. Browser profiles are fresh, without
user login state. Partial pagination and missing images retain coverage gaps.

WeChat calls async `jobprep.app.wechat_public.collect` directly and returns
`method=wechat`. Public browser acquisition and CDN image downloading use bounded
persistent caching. It requires no agent desktop operation or WeChat session;
there is no legacy crawler or certificate/GUI fallback.

Options default to `max_images=30` (0..100), `timeout=20` seconds (1..120),
`interval=15` seconds (0..300, including zero for tests), `max_requests=80`
(1..200), `browser_channel=chrome` (or `msedge`/null), `max_html_bytes=5242880`,
`max_image_bytes=10485760`, `max_total_image_bytes=31457280`, and
`images_only=false`. The public collector strictly validates limits and options.
The adapter filters shared runner options, maps `channel` to `browser_channel`
and `domain_delay`/`delay` to `interval` when the explicit option is absent.
Collection covers one article, not list pagination; requested search is recorded
as `search_not_applied`. WeChat failures always return `retryable=false`;
invalid options raise `ValueError` for direct calls and become permanent adapter
configuration errors.

WeChat challenges/deleted articles are terminal evidence, not bypassed. In
`images_only=true`, text is omitted and completeness remains false even if image
download succeeds. Both adapter and direct tool calls default it to false,
retaining article text and images. OCR is a separate explicit step.

## Files And OCR

HTML accepts `.html`/`.htm` up to 30 MiB. Images accept PNG/JPEG/GIF/WebP/BMP/TIFF,
up to 10 MiB, maximum dimension 20,000 and 40 million pixels. HTML relative images
must remain inside the original HTML directory tree after URL decoding and path
resolution; absolute paths, remote images and escaping links remain gaps.
Relative-image copies are limited to 100 images and 30 MiB total.

OCR validates artifact containment, suffix, image dimensions and any supplied
SHA-256 before calling the engine. Blocked/deleted/error acquisition is skipped.
Decorative/duplicate image placeholders need no OCR; missing files, credentials,
budget or recognition errors become `ocr_gaps` and usually `pending_ocr`.
Existing Baidu cache, long-image tiling and durable budget remain authoritative.

## Public Lists And Feishu

`public_list.config` requires `url`, `pagination.path`, and `response.items_path`.
Pagination supports query/body, start/increment/max_pages; response paths support
IDs, total records/pages, actual page, terminal flags and success markers. Fields
map title, description, requirements, location and optional URLs. See the catalog
for all nested fields and existing examples in `config/*_public.json`.
No inferred URL is added. Short pages alone do not establish completion; counts,
IDs, repeated pages, protocol failures and explicit caps are recorded separately.

Feishu defaults: `max_pages=100`, `timeout=25000` milliseconds, `interval=1.2`
seconds, `browser_channel=chrome`, `next_selector=li.atsx-pagination-next`,
`max_response_bytes=5242880`, `company="configured company"` (unverified label).
Unknown options fail. The adapter translates its seconds timeout into milliseconds.
The initial normal-UI list response must be HTTP 200: an unsigned 405 emitted
before the frontend's successful request is ignored. Later responses match the
expected offset and request filters exactly; failed later pages preserve gaps.
No signatures are forged and no speculative detail URL is constructed.

## Results And Execution Limits

Canonical acquisition includes `url`, `status`, `jobs`, `images`, `coverage`,
warnings and artifact references. Statuses are `ok`, `partial`, `error`, `blocked`,
`deleted`; OCR can produce `pending_ocr`. `list_complete` does not by itself imply
`jd_complete` or whole-company completeness. HTTP429/transient web errors expose
retry classification for the runner; tools do not own task retry schedules.

Synchronous list/browser/file work uses `asyncio.to_thread`; cancelling its await
does not forcibly stop the thread. Per-request/browser operation timeouts are not
total-run deadlines. Configure page/byte limits and pacing for bounded operation.
