# Public WeChat article export alternatives

Research date: 2026-10-05. Target (registered, NOT fetched in this research):
`https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA`.

## Recommendation

Try **SingleFile CLI v2.16.4 with installed Edge** first. Its Windows standalone
executable avoids a Python environment, Node/Deno installation and a separate
browser download. It saves a rendered page and its resources into one HTML file.
No root certificate, HTTPS interception or system proxy modification is required.
This is the fastest setup recommendation, not a verified download of this target.

If explicit request filtering and a check of the live article DOM are required,
use **Microsoft Playwright Python with Edge** instead. The example below rejects
additional document navigations and fails when the usual article body is absent.
It is more configurable but requires a small export script.

The fact that WeChat's embedded browser displays the article does not establish
that a fresh independent browser or an HTTP client will receive it. Neither
project transfers that embedded browser session. If an independent browser gets
a challenge, login page, restriction or empty shell, stop that attempt. Do not
try alternate identities, credentials, stealth plugins or challenge solvers.

Only the commands in this document are proposed for the main agent's trial.
None was executed by this research task. Do not automatically run both methods
in sequence on failure, and do not retry a timed-out attempt automatically.

| Project | Acquisition and output | Main limitations |
| --- | --- | --- |
| [SingleFile CLI](https://github.com/gildas-lormeau/single-file-cli/tree/v2.16.4) | Independent Chromium/Edge rendering; self-contained HTML | Can save an error page successfully; lazy images can remain missing; one article can generate many resource requests |
| [Playwright Python](https://github.com/microsoft/playwright-python) | Fresh Edge context; DOM HTML, text, screenshot and PDF | Custom script required; DOM HTML is not a self-contained archive; headless browsing may be rejected; sample intentionally rejects nonstandard article templates |

## 1. SingleFile CLI

### Source and issue evidence

- [README at v2.16.4](https://github.com/gildas-lormeau/single-file-cli/blob/v2.16.4/README.MD): standalone executables, installed browser and positional URL/output syntax. License: AGPL with third-party exceptions.
- [CLI implementation](https://github.com/gildas-lormeau/single-file-cli/blob/v2.16.4/single-file-cli-api.js): dispatches capture to the CDP/BiDi backend and writes the captured result.
- [Chromium backend](https://github.com/gildas-lormeau/single-file-cli/blob/v2.16.4/lib/cdp-client.js): browser launch, navigation and capture via CDP. This operates on an independently launched browser, not the WeChat process.
- [Option definitions](https://github.com/gildas-lormeau/single-file-cli/blob/v2.16.4/options.js): verified spelling of the flags below. Windows defaults single-process mode on, but the source notes that browsers other than Google Chrome exit in this mode; explicitly disable it for Edge.
- [Release asset list](https://github.com/gildas-lormeau/single-file-cli/releases/expanded_assets/v2.16.4): Windows `single-file.exe`, 78 MB, published 2026-10-04, displayed SHA-256 `bc7cb9076c2d0be0d142c1ab111d093ab905e837a1132ff44b4781554b639983`.
- [Issue #201](https://github.com/gildas-lormeau/single-file-cli/issues/201): open report of deferred-image capture hanging on a video-bearing page. It is not a WeChat-specific report; timeouts and missing-image inspection remain necessary.
- [Issue #183](https://github.com/gildas-lormeau/single-file-cli/issues/183): closed Windows browser-path report; the shown failing command lacked an article URL. This research did not retrieve a maintainer explanation, so it is not evidence of a confirmed path bug or fix. Supply both URL and output as below.

### Exact PowerShell trial commands

Run only in the main agent's authorized trial. Each run creates a new directory.
The release download is a GitHub request, not a request to the article.

```powershell
$trial = Join-Path 'D:\job_market\out' ('wechat-singlefile-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $trial -ErrorAction Stop | Out-Null
$singleFile = Join-Path $trial 'single-file.exe'
Invoke-WebRequest -Uri 'https://github.com/gildas-lormeau/single-file-cli/releases/download/v2.16.4/single-file.exe' -OutFile $singleFile
$expectedHash = 'bc7cb9076c2d0be0d142c1ab111d093ab905e837a1132ff44b4781554b639983'
if ((Get-FileHash -LiteralPath $singleFile -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
    throw 'Release asset hash mismatch; stop.'
}
$edge = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $edge) { throw 'Installed Edge not found; stop and select an installed browser explicitly.' }
& $singleFile 'https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA' (Join-Path $trial 'article.html') `
    --browser-executable-path $edge `
    --browser-headless=true `
    --browser-single-process=false `
    --browser-wait-until=DOMContentLoaded `
    --browser-wait-delay=2000 `
    --browser-load-max-time=30000 `
    --browser-capture-max-time=30000 `
    --browser-wait-until-fallback=false `
    --crawl-links=false `
    --block-videos=true `
    --block-audios=true
$captureExit = $LASTEXITCODE
if ($captureExit -ne 0) { throw "Capture failed with exit code $captureExit; do not retry automatically." }
Get-Item -LiteralPath (Join-Path $trial 'article.html') | Select-Object FullName, Length
```

The executable hash establishes identity with the displayed GitHub asset, not a
source-to-binary reproducibility guarantee. No user profile, cookie file, remote
debugging attachment, certificate-ignore flag or proxy setting is supplied.
Default deferred-image loading remains enabled because the target may be a job
poster. Audio/video flags describe capture behavior; they do not establish a
global network prohibition before page scripts execute. Page rendering and
resource embedding can make multiple requests. This command does not implement
a total HTTP request budget, an immediate challenge detector or a no-redirect
policy. If those are prerequisites, use the filtered Playwright example instead.

Inspect the saved HTML locally before marking success: an actual article title,
meaningful body, expected employer/job information and readable poster images
must be present. A file, HTTP 200 or exit code 0 alone is insufficient. Report
challenge pages and missing images as failed/incomplete output.

## 2. Microsoft Playwright Python

### Source evidence

- [Python implementation](https://github.com/microsoft/playwright-python/blob/main/playwright/_impl/_page.py): `goto`, `content`, `screenshot` and `pdf` delegate to the browser protocol. License: Apache-2.0.
- [Browser documentation](https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge): installed Edge can be selected using `channel="msedge"`.
- [Page API](https://playwright.dev/python/docs/api/class-page): DOM capture and Chromium headless PDF printing. `page.content()` serializes HTML; it does not embed image files.
- [Context API](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route): routing can filter requests; block service workers for routing coverage. This still does not control all browser-process background traffic.

### Exact PowerShell trial commands

This example uses a new environment and fresh browser context. It performs one
explicit `goto`, blocks further document navigations (including redirects),
blocks audio/video and non-GET traffic, and permits only selected public resource
hosts. No existing browser profile or WeChat credentials are used. Filtering can
cause an otherwise accessible page to fail; report that rather than widening the
policy or trying another identity automatically.

```powershell
$trial = Join-Path 'D:\job_market\out' ('wechat-playwright-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $trial -ErrorAction Stop | Out-Null
py -3 -m venv (Join-Path $trial 'venv')
if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
$trialPython = Join-Path $trial 'venv\Scripts\python.exe'
& $trialPython -m pip install playwright
if ($LASTEXITCODE -ne 0) { throw 'Playwright installation failed.' }
@'
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

target = "https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA"
out = Path(sys.argv[1])
navigation_attempts = 0

def filter_request(route):
    global navigation_attempts
    request = route.request
    if request.is_navigation_request():
        navigation_attempts += 1
        allowed = navigation_attempts == 1 and request.url == target
    else:
        parsed = urlsplit(request.url)
        host = parsed.hostname or ""
        allowed = parsed.scheme == "https" and (
            host == "mp.weixin.qq.com" or host.endswith(
                (".qpic.cn", ".qlogo.cn", ".wx.qq.com")
            )
        )
    if request.method != "GET" or request.resource_type == "media":
        allowed = False
    route.continue_() if allowed else route.abort()

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    try:
        context = browser.new_context(service_workers="block")
        context.route("**/*", filter_request)
        page = context.new_page()
        response = page.goto(target, wait_until="domcontentloaded", timeout=30000)
        if response is None or response.status != 200:
            raise RuntimeError("Article did not return HTTP 200; stop.")
        body = page.locator("#js_content")
        body.wait_for(state="visible", timeout=10000)
        title = page.locator("#activity-name").inner_text(timeout=3000).strip()
        text = body.inner_text().strip()
        images = body.locator("img").count()
        if not title or (not text and images == 0):
            raise RuntimeError("Missing article title/content; stop.")
        # Bounded scrolling loads normal lazy images on the same page.
        height = page.evaluate("document.documentElement.scrollHeight")
        if height > 80000:
            raise RuntimeError("Page too long for this bounded trial; stop.")
        for y in range(0, height, 1000):
            page.evaluate("y => window.scrollTo(0, y)", y)
            page.wait_for_timeout(100)
        page.wait_for_timeout(2000)
        missing = body.locator("img").evaluate_all(
            "imgs => imgs.filter(i => !i.complete || i.naturalWidth === 0).length"
        )
        (out / "article.html").write_text(page.content(), encoding="utf-8")
        (out / "article.txt").write_text(title + "\n\n" + text, encoding="utf-8")
        page.screenshot(path=str(out / "article.png"), full_page=True)
        page.emulate_media(media="screen")
        page.pdf(path=str(out / "article.pdf"), print_background=True)
        print(json.dumps({"title": title, "images": images, "missing_images": missing}))
        if missing:
            raise RuntimeError("Saved partial output; some article images are missing.")
    finally:
        browser.close()
'@ | & $trialPython - $trial
if ($LASTEXITCODE -ne 0) { throw 'Export incomplete or failed; do not retry automatically.' }
```

This script is a proposed main-agent trial, not a tested repository addition.
Absent `#js_content`, a timeout or a blocked redirect ends it without credential
capture or challenge interaction. New image-message templates may use other
selectors; the example intentionally fails on those rather than claiming they
are empty articles. Screenshot/PDF preserve only successfully rendered content.
The HTML can still refer to remote resources, so use the screenshot/PDF for an
offline visual record. Image-based job text will need downstream OCR, performed
on the local output. Check the document against the normally visible article.

## Normal user export fallback

If only the WeChat embedded browser can display the article, neither independent
browser method above has a demonstrated automatic path to that existing DOM.
The user can use a normal Save/Print/Copy/Screenshot command if the installed
client offers it, then provide the resulting local file for offline processing.
The existence of those commands in this particular WeChat build is unverified.
An external Chrome/Edge page that the user can normally open can also be saved
using the [SingleFile extension](https://github.com/gildas-lormeau/SingleFile).
Do not assume that extension can be installed inside WeChat. This fallback needs
a user export step and is not an end-to-end scripted download.

## Screened out of the fastest recommendation

[tingaidehua/wechat-article-downloader-skill](https://github.com/tingaidehua/wechat-article-downloader-skill)
offers direct public HTML parsing and a credential-free `article` subcommand,
but its [client.py](https://github.com/tingaidehua/wechat-article-downloader-skill/blob/main/skills/wechat-article-downloader/scripts/wechat_article_downloader/client.py)
installs `Retry(total=3, connect=3, read=2)` including HTTP 429, follows up to five
redirects, and leaves Requests' environment trust enabled. The ordinary CLI does
not expose a retry-disable flag. Its [service.py](https://github.com/tingaidehua/wechat-article-downloader-skill/blob/main/skills/wechat-article-downloader/scripts/wechat_article_downloader/service.py)
separates single-article fetching from account-history credential lookup, but
the project also implements local WeChat cache credential capture. No such
feature was run or is proposed here. An audited direct-fetch adaptation could
be small, but the unmodified CLI is not the best bounded first trial.

## Research verification

Public GitHub READMEs, source files, release assets and issue pages were fetched
over the network. Anonymous GitHub tree API calls hit a rate limit; public HTML
and raw source reads supplied the evidence without authentication. Selected
existing WeChat review documents were read for workspace context. No `.env`,
secret, account cache or credential value was read. No UI was controlled, no
article URL was fetched, no downloader was installed or executed, and no existing
file was modified. The only new workspace file is this document.

## Authorized real trial: 2026-10-05

The later user request authorized a real independent-browser trial. Added
`scripts/save_public_wechat.py`; existing crawler code was read but not modified.
Reused the existing `.venv` Playwright 1.63.0 installation and installed Chrome.
No dependency installation, WeChat session, system proxy change or UI control.

Executed at 2026-10-05 21:00:51 through 21:00:58 Asia/Shanghai:

```powershell
Set-Location 'D:\job_market'
.\.venv\Scripts\python.exe -B scripts\save_public_wechat.py
```

**Final result: PARTIAL, not a usable job-poster export.**

- Exactly one `goto` and one allowed document request for the pinned URL.
- Three allowed page requests total; 158 requests blocked for non-article hosts.
  Only HTTPS `mp.weixin.qq.com` is allowed; CDN hosts are deliberately excluded.
- No challenge detected, no document redirect, no retry or alternate route.
- HTTP 200, visible `#js_content`, nonempty `#activity-name` title:
  `HiDream.ai 2027 campus recruitment` (the full Chinese title is in status.json).
- Body text length: 104 characters. Body image elements: 11.
- All 11 image elements retained SVG placeholders with `data-src`; actual loaded
  article images: **0**. Screenshot inspection confirms large blank image areas.
  This browser route did not recover the recruitment poster under the host policy.

The first image check incorrectly counted decoded one-pixel SVG placeholders
as loaded images and initially classified success. Local HTML and screenshot QA
identified this immediately; the new script's image check was fixed, and the
status report was corrected to `partial` using only local files:

```powershell
.\.venv\Scripts\python.exe -B scripts\save_public_wechat.py --verify-local
```

This correction performs no browser launch or navigation. A subsequent invocation
of the ordinary command verified that the existing status reservation refuses
another navigation before launching a browser. Do not remove/reset that record.

Artifacts, all under `D:\job_market\data\public_wechat_trial`:

| File | Bytes | Meaning |
| --- | --- | --- |
| `status.json` | variable | Corrected partial result, policy, counts and placeholder evidence |
| `article.html` | 3559627 | Normal article DOM; external images remain references/placeholders |
| `article.png` | 126798 | 1554 x 14337 screenshot with missing poster images |

SHA-256 of HTML:
`c848497cd644c0cf170f566dcabc12621e9c814d835294ccd4660b5107b3194d`.
SHA-256 of screenshot:
`98536ba4c2637f6c8b30cd7b4b2646d9ebab97a53c9cf8971ac5c19d5c75fd7e`.

Filtering uses Chromium CDP request-stage interception to inspect redirect hops;
it allows one exact document URL, GET only, and caps permitted page requests at
40. Audio/video, WebSockets, service workers, extra documents and other hosts are
blocked. A fresh context and browser-only `--no-proxy-server` avoid existing session
state and system proxy edits. Counts describe intercepted page requests, not a
process-wide network firewall or proof about browser background connections.
The separately successful native WeChat ten-page PDF remains the demonstrated
source for the local PDF-to-OCR workflow. No further article request is proposed.

## Authorized CDN-only recovery of the saved HTML images

The user subsequently authorized normal public CDN image GETs, without another
article request or browser navigation. Added the independent
`scripts/download_saved_wechat_images.py`; the browser script and its conservative
report remain unchanged. The only input was the existing local `article.html`.

```powershell
Set-Location 'D:\job_market'
.\.venv\Scripts\python.exe -B scripts\download_saved_wechat_images.py
```

Actual result: **11 GET requests, 11 validated images, 11 unique SHA-256 hashes,
zero duplicates, 1,088,557 bytes received and saved**. All responses were HTTP 200
image data, and all files passed PIL verification, full decode, the AppTools
dimension/pixel bounds, and subsequent local SHA/path/dimension checks.
This recovery made **zero article requests and zero browser navigations**.

The downloader accepts only the existing `data-src` image URLs on HTTPS
`mmbiz.qpic.cn`, with the public `wx_fmt` query parameter. It uses GET only,
explicit HTTPX transport retries=0, redirects disabled, environment proxies
disabled, a new single-request client for each image, and a separately built
request without Cookie or authorization headers. Normal public article Referer
is supplied. No WeChat session, certificate, credential or process access.
Non-200/redirect, unexpected content type/encoding/length, byte-limit violations
or invalid images stop the run without retries. Limits: at most 11 image nodes,
15 reserved requests, 10 MiB per image and 30 MiB total; response lengths are
checked before streaming and actual body sizes are checked while streaming.

The durable SQLite gate is consumed before any request; every attempt is also
reserved before sending. A second ordinary invocation was verified to refuse
immediately with **zero additional requests**. Never reset/delete that gate to
repeat this trial.

Outputs are under the absolute root
`D:\job_market\data\public_wechat_trial\cdn_images`:

- `document.json`: AppTools document/image contract; `url`, `final_url` and source
  fields use only the pinned public short article URL, not signed session URLs.
- Eleven SHA-named JPEG/PNG images with absolute paths under `artifact_dir`,
  `sha256`, `width`, `height`, `order`, `bytes` and `status=ok`.
- `article.txt`: title and saved DOM text; no OCR performed.
- `article_local.html`: script-free local HTML referencing the downloaded files.
- `download_status.json`: actual request/image/byte counts and validation results.
- `download_gate.sqlite3`: persistent one-run gate, terminal state `success`,
  request reservations `11`.

Image dimensions in DOM order:
`1080x300, 1080x2384, 1080x1816, 1080x2297, 1080x1986, 1080x1247,
1080x166, 900x383, 900x383, 900x383, 1080x301`.

The main wrapper can consume these exact arguments, with the existing wrapper
command selected by the main agent:

```powershell
--document-json 'D:\job_market\data\public_wechat_trial\cdn_images\document.json' `
--source-url 'https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA' `
--artifact-dir 'D:\job_market\data\public_wechat_trial\cdn_images' `
--ocr
```

The image acquisition report is successful, but `document.status=partial`,
`coverage.complete=false`, `coverage.jd_complete=false` and the warning about
unassessed OCR/semantic coverage remain. `coverage.image_complete=true` means
all image references in this saved DOM were recovered, not that every source
image, linked JD or company position is complete. This is now a demonstrated
public-browser-HTML plus public-CDN-image acquisition route for this article.
