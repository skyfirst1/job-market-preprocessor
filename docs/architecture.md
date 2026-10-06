# Workstation Architecture

## Dependency Direction

`runner -> adapters -> app -> acquisition/OCR primitives`

- `jobprep.app`: independently callable web, WeChat, local-file, public-list,
  Feishu-list and OCR tools. No queue or semantic decisions. Parameter contracts
  are available through `python -m jobprep tools` and `GET /tools`.
- `jobprep.adapters`: select a platform strategy, validate configuration and
  tune app calls. Extend the registry rather than the runner for new platforms.
  Inspect strategies with `python -m jobprep adapters` or `GET /adapters`.
- `jobprep.runner`: SQLite queue, worker lock, recovery, bounded task retries,
  OCR budget/cache coordination, detail discovery and provenance persistence.
- `scripts`: compatibility-only CLI entry points. Reusable behavior must live in
  `jobprep`; production modules must not import `scripts`.
- `jobprep.pipeline`: compatibility import for existing callers.

## Operational Modules

The operational workflow is collected under the same three layers instead of
growing standalone business logic in `scripts`:

| Responsibility | Production module | Compatible command |
| --- | --- | --- |
| Cached batch 009 image recovery and OCR coordination | `jobprep.app.batch009_image_recovery` | `scripts/recover_batch009_image_gaps.py` |
| Batch 009 evidence analysis and target merge | `jobprep.analysis.batch009_targets` | `scripts/analyze_batch009_targets.py` |
| Continuous medical batch orchestration | `jobprep.runner.continuous_batches` | `scripts/run_continuous_medical_batches.py` |
| Incremental target analysis | `jobprep.runner.continuous_analysis` | `scripts/monitor_continuous_analysis.py` |
| Agent status publishing | `jobprep.runner.dashboard_status` | `scripts/report_agent_status.py` |
| Read-only delivery dashboard | `jobprep.runner.dashboard_server` | `scripts/serve_operations_dashboard.py` |

Shared atomic JSON/CSV/text artifact I/O lives in `jobprep.app.fileio`.
CLI wrappers intentionally contain no workflow logic so existing commands and
imports remain valid while tests can target production modules directly.

Existing crawler, HTML extraction, list collector and Baidu OCR remain lower-level
primitives. Runtime acquisition is script-driven; no LLM is required. An agent
can review the exported evidence later without driving pagination itself.

See [app tools](app_tools.md) and [adapters](adapters.md) for tool contracts and
platform extension details.

## Execution Policy

`config/workstation.json.runner` controls `max_attempts` (default 3),
`automatic_retry`, `base_delay_seconds` (5), `max_delay_seconds` (60),
`wait_for_retries` and `max_wait_seconds` (60 per batch).

Only transient `error` results retry automatically: transport/timeouts, explicit
retryable errors, or HTTP 408/425/429/500/502/503/504. Backoff is capped exponential.
`blocked`, `deleted`, `partial` and `pending_ocr` retain evidence for intervention;
they do not trigger blind task retries. Tool-local request retries may still occur
within one queue attempt. OCR calls remain governed by the separate durable budget.

Retry due times and cumulative attempts are stored in SQLite. A restarted worker
recovers interrupted `running` tasks. A single-worker lock prevents two batches
from consuming the same queue. `--limit` counts executions, including retries,
not distinct source URLs. Scheduled retries exceeding the batch wait budget remain
queued for the next run; this is not a background scheduler.

Manual requeue preserves attempts. Use `--reset-attempts` deliberately after fixing
a permanent error or changing acquisition strategy. `--no-wait-retries` returns
without waiting for future retry due times. `--no-ocr` avoids initializing OCR.

## Completeness And Boundaries

List completeness, JD completeness, image/OCR gaps and discovered-detail limits
remain separate evidence. A total-count match does not establish that every hiring
channel has been covered. Adapters must preserve pagination and raw provenance.

WeChat challenges are reported, not bypassed. Image-only collection is explicitly
scoped and cannot imply whole-article completeness. Feishu public portals require
normal browser interaction on sites whose direct unsigned API rejects requests.
Local imports are validated; secrets are not exposed through tool metadata.

## Commands

```powershell
.venv\Scripts\python.exe -m jobprep tools
.venv\Scripts\python.exe -m jobprep adapters
.venv\Scripts\python.exe -m jobprep run --url https://example.com/jobs --no-ocr
.venv\Scripts\python.exe -m jobprep retry --status error --reset-attempts
```

Use `--list-config config/example.json` with exactly one explicit `--url`;
batch sources belong in the exact-URL `list_sources` mapping. Complete API JD
records (`needs_details=false`) do not enqueue redundant detail fetches.
Use the CLI help for adapter options.
The API preserves previous endpoints and adds metadata endpoints; the old CLI and
`from jobprep.pipeline import Workstation` remain supported.
