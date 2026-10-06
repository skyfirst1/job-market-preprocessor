# Native WeChat PDF preprocessing

## Verified public-script route

The independent browser also received normal HTML for this specific article.
Its browser-stage images were placeholders because the trial blocked CDN hosts.
Without another article navigation, the saved HTML's 11 explicit `data-src`
references were downloaded from `mmbiz.qpic.cn`: 11 GETs, 1,088,557 bytes, all
decoded successfully. OCR then succeeded for 11/11 images, yielding 1,575
characters including article text. The localized HTML was imported into the
workstation as task `a3d06acb233d95db53caa339`.

```powershell
.venv/Scripts/python.exe scripts/run_wechat_pilot.py --ocr
```

This single command coordinates existing scripts. Both navigation and download
reservations are persistent and refuse a second acquisition; later runs reuse
already verified files and OCR cache. It never deletes a gate to retry. This is
a pilot pinned to one article, not a universal or batch WeChat downloader.
No WeChat UI click, session credential, root certificate or system proxy change
is used. A challenge or incomplete download stops the pipeline. Existing source
reports remain available under `data/public_wechat_trial`.

Browser acquisition: max one navigation and 40 allowed page requests, no media
or non-article hosts. Image acquisition: only explicit public CDN references,
max 11 images and 15 reserved requests, no redirects or retries, no cookies,
10 MiB per image / 30 MiB aggregate. OCR uses the existing persistent budget.
Completeness remains unverified beyond the images listed in this saved HTML.

## Native PDF fallback

This is a certificate-free fallback for an article already readable in WeChat.
It does not change Windows proxy settings, inspect WeChat credentials, inject
code into WeChat, or bypass a verification page.

## Acquisition

In the existing article window use the menu's Print command (Ctrl+P), select
Microsoft Print to PDF, select all pages, and save the file locally. Check that
the preview includes recruitment content and the end of the article. Printing
is a supervised acquisition step, not yet an unattended URL downloader.

The tested public CSV article is:
https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA

Its native export is `data/hidream_wechat_native.pdf`: 10 pages, approximately
1.14 MB. Inspection confirmed embedded recruitment images and the Agent role.
Native printing rasterized the article text; embedded-image OCR is necessary.
Cross-page image duplicates must be deduplicated before paid recognition.

The real OCR trial completed all 12 retained images successfully and produced
1,479 characters. It used 12 general OCR calls. A second run used the cache and
made zero new OCR calls. Visual review confirmed Agent Algorithm Engineer,
2027 campus eligibility, a 2026-11-30 deadline and `career@hidream.ai`. Two OCR
errors (the extra character in Agent and an extra parenthesis in the mailbox)
are recorded separately in `exports/evidence/hidream_native_review.json`;
the original OCR text remains unchanged.

## Script

Run from the repository root. Dependencies are in `requirements.txt`.

```powershell
.venv/Scripts/python.exe scripts/preprocess_wechat_pdf.py --path data/hidream_wechat_native.pdf --source-url https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA --artifact-dir data/artifacts/wechat_native_pdf
```

This command is offline. Add `--ocr` to explicitly upload retained public article
images to the configured Baidu OCR API. Credentials stay in local `.env`; the
script neither prints them nor stores them in its output. The shared durable
budget in `data/ocr` remains authoritative; it is never reset by this script.
High-accuracy retries are disabled for this fallback.

- `--path`: local, unencrypted PDF; source is retained with a SHA-256 reference.
- `--document-json`: alternative to `--path`; reuse an existing app-tool document
  from the public HTML/image downloader. Its URL must exactly match `--source-url`;
  images must be inside `--artifact-dir` and satisfy the normal OCR safety checks.
- `--source-url`: public source identity, not a signed WeChat browser-session URL.
- `--artifact-dir`: output root containing PDF evidence, extracted images,
  `document.json`, `preprocessed.json`, and `article.txt`.
- `--ocr`: enable paid recognition; omitted by default.
- `--persist`: write the result into the existing workstation task record under
  its worker lock. Existing task evidence is retained as `previous_task_result.json`.
  The derived compact preprocessing view uses the existing `preprocess_document`
  implementation; semantic status remains unassessed.
- `--min-image-width`, `--min-image-height`: default 500 and 200 pixels to avoid
  individual rasterized glyphs. Skipped images remain documented as evidence.
  Lower these thresholds for articles with small text graphics.
- `--max-images`: default 20, range 1..100; refuse OCR above the retained-image
  count limit. This is an image limit, not an API-call limit; large images can
  require multiple tiles under the shared OCR budget.

Keyword evidence is a deterministic prefilter, not a semantic match decision.
Source completeness and linked JD completeness remain unverified even when
every retained image is recognized successfully. Printing may clip an image
or omit lazy-loaded content, so keep the PDF and image evidence for review.

## Isolation

The previous MITM route is stopped. Existing supervision windows and retained
certificate files are not deleted. The native route does not require them.
The existing static Clash proxy remains `127.0.0.1:7897`, with no active PAC.
