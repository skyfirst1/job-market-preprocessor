# GitHub reuse for recruitment preprocessing

## Verification and selection

Verified on **2026-10-05** using unauthenticated GitHub REST requests from PowerShell.
Stars are a discovery signal, not a guarantee of extraction quality, safety, or suitability.
GitHub `pushed_at` indicates repository activity, not a promise of future maintenance.

| Project | Stars | License (GitHub SPDX) | Last push UTC | Latest GitHub release / date UTC | Decision |
| --- | ---: | --- | --- | --- | --- |
| [python-readability](https://github.com/buriy/python-readability) | 2,897 | Apache-2.0 | 2026-08-27 19:40:50 | 0.9 / 2026-08-26 | Selected: narrow article extraction library; integrates with existing BeautifulSoup cleanup. |
| [trafilatura](https://github.com/adbar/trafilatura) | 6,909 | Apache-2.0 | 2026-10-02 16:53:41 | v2.3.0 / 2026-10-02 | Mature extraction alternative with additional metadata/output features; unnecessary for this small adapter. |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | 84,764 | Apache-2.0 | 2026-09-25 06:37:14 | v0.9.4 / 2026-09-23 | Broad crawling/browser framework; adopting it would exceed this component's scope. |

All three returned `archived=false`. Source requests:

- [readability repository API](https://api.github.com/repos/buriy/python-readability), [releases](https://api.github.com/repos/buriy/python-readability/releases?per_page=1).
- [trafilatura repository API](https://api.github.com/repos/adbar/trafilatura), [releases](https://api.github.com/repos/adbar/trafilatura/releases?per_page=1).
- [Crawl4AI repository API](https://api.github.com/repos/unclecode/crawl4ai), [releases](https://api.github.com/repos/unclecode/crawl4ai/releases?per_page=1).

The web reader could not open API URLs; actual API responses were successfully queried with `Invoke-RestMethod`.
No GitHub account, token, browser, login bypass, or repository setup script was used.

## Download, license and dependencies

Selected version: **readability-lxml 0.9**, pinned as the only new direct requirement.
Before installation, reviewed [upstream LICENSE](https://github.com/buriy/python-readability/blob/0.9/LICENSE),
[setup.py](https://github.com/buriy/python-readability/blob/0.9/setup.py), and
[PyPI release metadata](https://pypi.org/pypi/readability-lxml/0.9/json).
GitHub source tag and PyPI wheel dependency bounds differ; installation follows wheel metadata.

Wheel requirements on the project's Python 3.12: chardet >=5.2,<6;
cssselect >=1.3,<1.4; lxml[html-clean] >=5.4,<7. The html-clean extra adds lxml_html_clean.
Installed from official PyPI into the existing `.venv`:

| Distribution | Installed version |
| --- | --- |
| readability-lxml | 0.9 |
| chardet | 5.2.0 |
| cssselect | 1.3.0 |
| lxml | 6.1.3 |
| lxml_html_clean | 0.4.5 |

Command: `.venv\Scripts\python.exe -m pip install --only-binary=:all: --index-url https://pypi.org/simple readability-lxml==0.9`.
Only published package wheels were installed, including the lxml native extension;
no standalone executable was downloaded and no source-build/setup script was run.

Source reference snapshot: `third_party/python-readability-0.9/`, downloaded from raw.githubusercontent.com
at commit **7159b09ad688378779294cbb5d9f3cfa3274368f**, resolved through
[GitHub tag tree API](https://api.github.com/repos/buriy/python-readability/git/trees/0.9?recursive=1).
This deliberately partial snapshot contains the nine top-level `readability/*.py` files,
LICENSE, README.md and setup.py: **12 files / 77,372 bytes**. No large fixtures, repository history,
other frameworks or binaries were copied. No `.gitignore` changes are needed.
The runtime imports the installed distribution, not this reference snapshot.
Retain upstream LICENSE/README and attribution when redistributing this snapshot.

## Contract for exporter integration

`jobprep.preprocess.preprocess_document(result: dict) -> dict` is synchronous, JSON-safe,
has no network/file I/O, and does not mutate `result`. Supply collector results or existing
`codex_review.jsonl` packets. Caller retains original audit records and writes the returned
derived view to `agent_tasks.jsonl`; this component does not change exporter/pipeline/API.

Input text sources: `html`, `html_text`, `text`, `ocr_text`, `evidence[*].text`, and
`ocr[*].text/lines`. OCR lines may be strings or dictionaries containing `text`.
Missing lines supplement OCR text; line indexes, image indexes/hashes and existing evidence IDs
remain in provenance. Audit file paths are references only and are never opened.
Collector results normally provide `html_path/text`, not inline HTML. The integrator must read
`html_path` from its controlled artifact directory and supply `html_text` alongside the packet
to activate readability (validate the resolved path against that artifact directory, never arbitrary
input paths). This module deliberately never opens paths. For example, the integrator may call
`preprocess_document({**result, "html_text": trusted_artifact_html})` after its controlled read.
`html_text` takes precedence over `html`. Reliable HTML extraction (at least 80 characters,
without an extraction fallback) supersedes `result.text`, avoiding reintroduction of navigation.
Sparse/uncertain HTML retains the original `text` conservatively and records
`provenance.source_text_retained_due_fallback=true` plus the corresponding quality reason.
Jobs and OCR remain independent in both cases. With only plain text, DOM cleanup cannot recover
structure and readability is not invoked; passing `html_text` is required for this integration.
OCR strings are not executed.

Required output:

- `compact_text`: cleaned body/OCR plus normalized known job fields. No keyword eligibility filtering.
- `jobs`: light `id`, `title`, observed `url`, `text`, `description`, `requirements`,
  `company`, `location`, `department`, `source_ids`, `raw_job_id`, `field_sources`,
  `evidence_sources`, `needs_details`, and `needs_confirmation`. No large `raw` payload.
- `signals`: matched recruitment/AI/access-challenge strings, plus
  `keyword_matches_are_not_exclusions=true`. These are candidates for review only.
- `quality`: `boilerplate_removed`, `dedup_count`, separate job/line dedup counts,
  `changes`, `body_insufficient`, `needs_details`, `needs_confirmation`, `confirmation_reasons`,
  counts, `semantic_verified=false` and `no_jobs_proven=false`.

Extra `provenance` retains task/evidence identifiers, compact source references (including all
original companies), coverage, warnings and original HTML/JSON audit paths. Existing evidence IDs
are carried forward; normalized job evidence IDs can be added by the exporter integration owner.
Job `field_sources` refer to exact input paths; JSON paths are referenced through
`provenance.raw_json_paths`, avoiding repetition per job.

HTML cleanup removes script/style/template, comments, structural nav/header/footer and explicit
hidden nodes. Readability supplies article extraction. Explicit main/article/WeChat content roots
are retained when scoring disagrees; short/failed extraction falls back to structurally cleaned
content and flags uncertainty. This is a heuristic derived view, not proof that removed text was irrelevant.
Duplicate body lines of at least 20 characters are collapsed only within the same evidence item,
with source/index pairs recorded in `provenance.dedup_events`. Different evidence contexts and each
job's description/requirements remain independent.
Jobs merge only with an existing ID/upstream ID/observed URL and when all identities and normalized fields agree;
all original IDs and per-occurrence field sources survive. Distinct upstream IDs or descriptions stay separate.
Jobs with no reliable identity and blank jobs stay present. No detail URL is constructed from a job ID.
Shared-site CSV company references are retained for audit and never assigned as a job's employer.

Raw field aliases include Huawei `mainBusiness` / `jobDesc` for description,
`jobRequire` for requirements, `workPlace` for location and `deptName` for department;
common English and Chinese equivalents are supported. Existing normalized fields and raw aliases
are combined without discarding distinct supplied text. Placeholder wording such as
`请您详见岗位意向中的岗位职责` is preserved and marked for confirmation.

Example:

```python
view = preprocess_document({"jobs": [{"id": "original-1", "title": "算法工程师", "url": "",
    "raw": {"mainBusiness": "<p>研发训练平台</p>", "jobRequire": "熟悉Python"}}]})
assert view["jobs"][0]["description"] == "研发训练平台"
assert view["jobs"][0]["source_ids"] == ["original-1"]
assert view["jobs"][0]["url"] == ""
assert view["quality"]["body_insufficient"] is True
assert view["quality"]["semantic_verified"] is False
```

`body_insufficient` uses fewer than 80 body/OCR characters as a review signal; structured jobs
remain even when body is absent. Coverage, OCR gaps, pending details, blocked/deleted/error/partial
acquisition and placeholders trigger confirmation. No empty result proves absence of jobs.

## WeChat boundary

Readability cleans already acquired public article HTML, including `#js_content`; it does not
log in to WeChat, render JavaScript, solve CAPTCHAs, refresh private sessions, discover inaccessible
articles or recover removed content. Login/client-only/safety-verification screens need authorized
manual acquisition or the existing collector's access workflow. Signals flag such wording for review.
Poster-only notices depend on acquired OCR evidence; both OCR text and lines are included.
No preprocessing library or high-star crawler guarantees access past platform authentication or verification.

## Validation

`python -m pytest tests/test_preprocess.py -q`: **17 passed**. Covers Chinese recruitment,
JS/navigation/comments, job HTML and raw aliases, identity-safe duplicate merging, empty/sparse
known jobs, placeholders, OCR-only text/lines, exported evidence IDs and company preservation,
real readability extraction, and no audit-path reads. `python -m pip check`: no broken requirements.
Read-only validation of the first real Huawei export packet preserves **10/10 jobs**, removes six
within-evidence duplicate lines, and produces **2,965 compact_text characters**. JSON serialized with
`ensure_ascii=False` measures 82,135 characters for the original packet and 26,815 for the derived view
(default JSON spacing; measured before omitting null source IDs on unidentified jobs).
Placeholder details correctly trigger `needs_details=true`; coverage/OCR gaps remain unconfirmed.
These sizes are character counts, not model token counts, and describe one sample only.
OCR credentials and `docs/pilot_findings.md` were not read or modified.
