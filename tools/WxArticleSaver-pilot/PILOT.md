# Scripted single-article pilot

This route is stopped: real export failed because Windows did not retain the
required CurrentUser Root trust. Do not restart it as an automatic fallback.
This is an unverified end-to-end pilot, not a working bulk downloader yet.
The untouched AGPL upstream snapshot remains in `third_party`.

## Start

Double-click `start_pilot.cmd`, or run:

```powershell
.\.venv\Scripts\python.exe coexist_launcher.py --seconds 300 --auto-export-pilot
```

Run from this tool directory. The dedicated virtual environment is already
installed. The supervisor remains open after capture stops. No execution-policy
bypass is used. The script does not start/restart WeChat or send any messages.

## Parameters and behavior

- `--seconds`: capture duration after proxy startup, 1..300; default 180.
- `--auto-export-pilot`: opt-in automatic export of the selected CSV article.
  Without this switch the upstream manual export button remains in use.
- Selected URL: https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA
- Its canonical article identity is also recognized after WeChat redirects.
- The injected script waits for page load and three seconds, verifies article
  DOM exists, then invokes the exporter. No UI-agent decision is required.
- `.wxas_state/pilot_export_gate.json` durably limits export attempts to one,
  including across script restarts. Failed attempts are not automatically retried.
  Do not delete this marker to retry without reviewing the failure and bounds.
- Videos are never downloaded by the exporter; image handling is unchanged.
- Output: `exports/<timestamp_title>` with HTML, Markdown, text, images, metadata.
- No paid OCR call or batch-link opening is performed by this pilot launcher.
- Windows may require manual CA trust confirmation. Never
  automate authentication, verification, or security confirmation dialogs.
- Certificate presence is verified after the trust command. If it is absent,
  startup aborts before changing proxy settings. A successful command exit code
  alone is not evidence of trust.
- The first client TLS rejection stops capture without automatic retry.

## Clash coexistence

- Requires existing enabled static proxy exactly `127.0.0.1:7897`, no existing PAC.
- Article hostname goes to capture proxy 8899, with Clash fallback.
- Other public destinations retain Clash. Common private/loopback IPs bypass it.
- Static proxy fields are untouched. Only PAC URL is temporarily set to
  `http://127.0.0.1:8918/proxy.pac`; previous value is restored on exit.
- PAC selection is still a system-wide change, not true per-process isolation.
- Current running WeChat did not adopt PAC after a focused refresh. A fresh
  browser/app startup remains to be tested; success is not guaranteed.

## Logs and recovery

- `coexist_status.json`: current phase, including security-confirmation waits.
- `coexist_capture.log`: proxy startup and capture/export events.
- `coexist_proxy_backup.json`: retained on recovery failure; next startup refuses
  to overwrite it. Inspect it before any manual restoration.
- Supervisor window stays open after success or failure. Ctrl+C stops capture.
- On a verification page or unexpected account warning, stop; do not keep
  refreshing, solve challenges automatically, or switch to a remote service.

## Current verification

Offline tests cover the one-attempt gate, selected-article restriction, optional
injection, disabled video downloads, Clash retention, and PAC restoration.
Article reading worked, but real capture/export did not yet succeed. The latest
app restart launched WeChat's updater rather than restoring its main window;
the user must complete normal startup/authentication before further testing.

After the user restored WeChat, its article traffic did reach capture port 8899,
but TLS handshakes rejected the proxy certificate. The generated certificate
was not present in CurrentUser Root. Proxy routing was restored and capture
stopped; no article/export attempt gate or paid OCR call occurred. The exact
cause of the missing certificate (confirmation, policy, or another change) has
not been established. Current startup checks fail safely in that state.

Temporary certificate file: `.wxas_ca/mitmproxy-ca-cert.cer`.
Expected SHA-1 thumbprint: `23E970368292D5178D37F1C25087BDB511DC306C`.
Certificate trust is retained at the user's request. None of the pilot launchers
automatically removes it. Remove it only when the user explicitly requests
cleanup after the tools are no longer needed.
Do not install it machine-wide, disable TLS checks, or override system policy
as an automatic fallback.
