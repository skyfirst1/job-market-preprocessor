# Local WeChat Download Pilot

This integration limits the download tool calls we initiate, NOT all HTTP
requests made by the desktop application, WeChat or its embedded browser.
Internal retries/background traffic remain unverified. No zero-risk claim.
The earlier audit in `wechat_download_pilot.md` concerns strict total HTTP limits;
it remains relevant, but does not prevent a user-approved one-tool-call pilot.

## Controls

- Exactly one approved HTTPS `mp.weixin.qq.com/s` article URL, pinned in SQLite.
- Lifetime maximum of one `single_article_download` call for this pilot ledger.
- Atomic reservation BEFORE sending; timeout consumes the reservation.
- No automatic retry, reset, URL switch, account/batch/collection tools or remote
  fallback. HTTP redirects and environment proxies are disabled.
- Metadata preflight uses only local `initialize` and `tools/list` calls.
- Do not print arbitrary RPC responses, credentials or account logs.
- Do not close the GUI/supervision window. A client timeout does not cancel the
  server task; inspect the retained GUI before taking any further action.

## Parameters

`LocalWechatDownload(allowed_url, ledger_path, timeout=60, transport=None)`:

| Parameter | Meaning |
| --- | --- |
| `allowed_url` | Exact HTTPS public WeChat `/s` article; other hosts, credentials and collection routes fail. |
| `ledger_path` | Persistent caller-owned SQLite file; must not be replaced/deleted to circumvent the budget. CLI uses `data/wechat_download_pilot.sqlite3`. |
| `timeout` | Client request wait, numeric 1..120 seconds; default 60. Not a total server deadline. |
| `transport` | Optional HTTPX transport for offline tests; leave unset for actual use. |
| `run(execute=False)` | Metadata preflight by default. Explicit `True` permits the single reserved download call after tool-schema validation. |
| `status()` | Used/maximum calls, persistent state, fixed local endpoint and limitations; does not contact the server. |

The endpoint is fixed at `http://127.0.0.1:4545/mcp`; it cannot be redirected to
a remote service. The documented JSON-RPC response form is supported. Unsupported
protocols or tool schemas fail preflight without an article call. Responses are
limited to 1 MiB. `response_received` does not prove completed downloads; files
must be inspected and then imported using the existing app file/OCR tools.

## Commands

```powershell
# Metadata only; does not send a download call.
.venv\Scripts\python.exe -m jobprep wechat-download --url https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA

# Only after the local GUI is running, settings are reviewed and user approval exists.
.venv\Scripts\python.exe -m jobprep wechat-download --url https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA --execute
```

Do not run the second command again after success, error or timeout. The runner
does not automatically schedule this pilot, and no company-wide queue is used.

## Current Verification

Offline tests cover durable budget, timeout accounting, no retry, no redirects,
fixed endpoint, URL pinning, and metadata-only preflight. On 2026-10-05 the actual
local preflight returned `ConnectError`: no listener on port 4545, zero download
calls. No article has been fetched using this integration yet. The downloader
was not found in Downloads, Desktop or Start Menu; installation/path is required.
