# Job Preprocessing Workstation Plan

Updated: 2026-10-05 (Asia/Shanghai)

## Scope and responsibilities

- Run the workstation in D:/job_market. Do not connect to UU or server25 for this task.
- Use Baidu Intelligent Cloud OCR API. Do not deploy or download OCR models.
- Preserve the raw CSV and existing analysis outputs.
- Codex performs semantic suitability assessment. Keywords only prioritize collection; they do not determine eligibility or justify excluding incomplete records.
- Output recruitment announcements, job lists and job details, including provenance, acquisition status and missing information.

## Implementation sequence

1. Create a Python package, local virtual environment, SQLite queue and content-addressed artifact storage. Import all CSV metadata; prioritize explicit AI/Agent/CV sources for initial validation.
2. Implement static HTTP acquisition and DOM extraction. WeChat uses #js_content, #activity-name and article metadata; collect data-src/src/srcset images inside the actual article, preserving ordered text/image evidence. Do not treat generic verify/captcha strings inside scripts as proof of blocking. Preserve deleted, challenged and inaccessible pages explicitly.
3. Implement Playwright collection for dynamic recruitment sites. Discover real job links and observed JSON responses; retain URL fragments. Support configured search inputs, next-page selectors and scroll-to-load. Track stable job IDs, repeated-page signatures, disabled next buttons, configured limits and total counts. A limit or error means partial coverage, not complete coverage. Site-specific selectors belong in configuration, never guessed API URLs.
4. Implement Baidu OAuth and standard/high-accuracy OCR, credentials from local environment/.env, timeout and error classification, long-image slicing with overlap and coordinate offsets, image-hash caching and durable call budgets. Missing credentials keep work pending. High-accuracy retry is configurable and recorded, including extra calls.
5. Join CSV, page, image and OCR evidence. Retain dates/degree/cohort/city as source assertions, not verified application eligibility. Deduplicate sources/jobs without losing original CSV references. Export JSONL review bundles, evidence Markdown, source coverage reports and CSV indexes.
6. Provide CLI and loopback-only API for import, bounded runs, status, retry, manual HTML/image imports, page inspection and export. Use durable task states with recovery and limits; never launch unbounded full-site scans from initial CSV import.
7. Validate fixtures (nested WeChat DOM, challenge detection, URL hashes, pagination, image slicing, OCR errors and budgets), then run a small real-source collection. Report exactly what was fetched and what still requires credentials or site adaptation.

## Architecture

CSV -> sources/queue -> HTTP or Playwright -> raw HTML/JSON/images -> OCR API -> evidence store -> Codex review exports

- SQLite: source records, durable crawl queue, result metadata, OCR cache/call accounting.
- Artifact directory: immutable hashed HTML/image/raw API evidence; exports are regenerated.
- One local worker initially. Per-domain throttling, bounded retry, request/body/image/page limits.
- Browser fallback uses a dedicated profile, not user cookies. Authentication/challenges produce manual tasks. Saved HTML and images can be imported with original source URLs.
- No application submissions, credential transmission to job sites or hidden scraping success claims.

## Definition of completeness

HTTP 200 is not extraction success. A recruitment list is complete only when its terminal pagination condition is observed and all discovered detail URLs have results or explicit failures. Search-limited runs describe completeness within those search terms, not all company jobs. Unknown total counts, configured limits, unresolved details and image/OCR gaps remain visible.

## Initial acceptance

- Original CSV remains unchanged; all valid rows imported with original fields.
- Real WeChat HTML extraction and image acquisition demonstrated or explicitly reported blocked.
- At least one dynamic recruitment source captured with browser evidence and honest pagination coverage.
- OCR integration tested with deterministic mocked responses; live OCR only when Baidu credentials are configured.
- Restart/retry does not silently lose work or repeatedly charge for cached successful OCR.
- Review exports include raw-source links and coverage/missing-data fields.

## References and design decisions

- https://github.com/qiye45/wechatDownload : WeChat sessions, article/image export and saved-file fallback. Reference only; no bundled binary or certificate interception.
- https://github.com/TongyiDai/career-ops-zh : public-source adapters and user-captured JD inbox patterns. Strongly protected sources need explicit missing-data handling.
- https://github.com/rrrrrredy/agent-job-monitor/blob/main/references/company-endpoints.md : domain-specific selectors and API/DOM differences. Treat endpoint examples as hypotheses until observed on the actual source.
- https://github.com/apify/crawlee-python : persistent request queues, retry and browser routing patterns. A small SQLite queue is sufficient for the initial single-worker workstation.
- https://playwright.dev/python/docs/network : observe XHR/fetch responses and preserve evidence; do not invent private endpoints.
- https://ai.baidu.com/ai-doc/OCR/9k3h7xuv6 : Baidu OCR variants and pricing. Call limits are tracked separately from estimated currency cost.

## Parallel work (maximum two agents)

- Agent A owns HTTP/DOM/Playwright crawler modules and their tests.
- Agent B owns Baidu OCR module and its tests.
- Main agent owns storage, queue, CLI/API, configuration, exports, integration and real-source validation.
