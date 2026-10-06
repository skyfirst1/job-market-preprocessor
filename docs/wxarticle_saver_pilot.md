# WxArticleSaver single-article pilot

## Local patch

- Source: pinned v1.1.0 snapshot in `third_party/WxArticleSaver-1.1.0`.
- Working copy: `tools/WxArticleSaver-pilot` (AGPL license retained).
- `save_article` uses an empty `video_downloads` list instead of calling
  `download_videos`. Video references remain in exported content/metadata.
- Images retain upstream behavior without an added image-count cap.
- Automatic refresh and launcher cleanup remain unchanged by user request.
- This disables exporter video downloads, not browser-side media requests.

## Verification on 2026-10-05

- Dedicated virtual environment installed from the upstream requirements.
- Patched module imports successfully using that environment.
- Six targeted offline tests pass, including an export with 101 image nodes
  and a video downloader mock that raises if called.
- Full workstation suite: 275 passed, 6 subtests passed, one existing warning.
- Existing system proxy observed: enabled, `127.0.0.1:7897`.
- Original global-proxy startup was not run. The user requires keeping Clash
  for Codex connectivity, so a separate local-capture launcher is used.
- Existing WeChat and supervision windows have not been closed.

## Live scope

- Candidate from CSV: https://mp.weixin.qq.com/s/qd86zzbPnzHHGHn407ktQA
- One article and one export action; no history/account batch crawl.
- Keep visible supervision and WeChat windows open.
- Original automatic refresh may generate additional requests; this is not
  a guarantee of exactly one HTTP request or of account safety.
- Upstream cleanup risk was accepted by the user; no cleanup redesign added.
- Expected output: `tools/WxArticleSaver-pilot/exports/<timestamp_title>`.
- Do not bypass Windows execution restrictions or browser security warnings.

## Process-isolated trial

- `local_launcher.py --pid <browser-network-pid> --seconds 180` targets one
  explicit PID; 0/all-process capture is rejected. Maximum duration is 300s.
- No global proxy or PAC settings are changed. Only `mp.weixin.qq.com`
  connections are allowed for TLS inspection; image downloads keep upstream
  behavior. Temporary CA trust remains user-wide, not process-isolated.
- Existing trusted CA certificates are preserved. A CA added by this launcher
  is removed by fingerprint in the cleanup block.
- Windows may require a manual redirector elevation prompt. Do not automate
  security prompts or circumvent app-control failures.
- First runtime trial started successfully: log reports `Local redirector
  started`. Windows system proxy still enabled at `127.0.0.1:7897` afterward.
- This verifies capture startup and unchanged proxy configuration, not article
  acquisition/export. One public link to File Transfer Assistant requires the
  user's action-time confirmation; no message has been sent yet.
- Browser traffic already tunneled through Clash may not expose the article
  hostname to local capture. If no article is captured, do not silently enable
  a global proxy or claim the export succeeded.
- Trial timed out after the bounded capture period without any article export.
  Capture subprocess exited and the system proxy remains unchanged. Cleanup is
  waiting on Windows' root-certificate deletion confirmation, not completed.
  The user must approve this security dialog manually. A redundant deletion
  request was stopped, leaving the original launcher's request in place.
- User confirmed manual deletion; subsequent certificate-store inspection
  verified root `23E970368292D5178D37F1C25087BDB511DC306C` is absent.
- No matching certificate-deletion or capture process remains. System proxy
  is still enabled at `127.0.0.1:7897`, with no PAC URL configured.
- Supervision and WeChat windows remain open; no File Transfer Assistant message
  was sent and no article export occurred.
- Full suite after adding isolation tests: 278 passed, 6 subtests passed.

## Authorized single-article browser verification

- User explicitly authorized sending the chosen public URL to their own File
  Transfer Assistant. Exactly one message was sent; no other recipients used.
- The URL was clicked once in WeChat's embedded browser. It displayed
  "HiDream.ai 2027 campus recruitment" (Chinese article title), publication
  timestamp 2026-09-30 18:47, and a visible recruitment image.
- The second isolated-capture startup failed: log says `Failed to start the
  interception process as administrator.` This alone does not establish
  whether elevation was declined, unavailable, or blocked by policy.
- No alternate global proxy or policy bypass was attempted. No export or OCR
  call occurred. Normal browser readability is not proof of scripted export.
- After failure, temporary root CA absence was verified and the system proxy
  remains enabled at `127.0.0.1:7897`. Supervision and article windows stay open.
- Further capture attempts require the user to handle any Windows elevation
  prompt manually; do not automate security confirmation dialogs.

## Follow-up trial: Clash-proxied browser incompatibility

- A later bounded trial successfully started the local redirector, focused the
  article pane, and issued one actual article refresh. Article remained readable
  but no injected export button appeared and no article capture was logged.
- Established TCP connections for browser network PID 2256: five total, all to
  loopback Clash port 7897, none directly to remote HTTPS port 443.
- This supports a proxy-path incompatibility diagnosis, not an account challenge.
  Installed mitmproxy HTTP code rejects CONNECT requests in transparent mode;
  this launcher also restricts interception to the article hostname rather than
  the loopback Clash endpoint. Do not solve this by widening capture blindly.
- No repeated article-opening, export, OCR call, or global proxy change followed.
- At trial end, capture and certificate commands were absent, the temporary CA
  was absent, and Windows proxy remained enabled at `127.0.0.1:7897` without PAC.
- A selective PAC preserving Clash for other destinations is a possible next
  experiment, not verified isolation. It requires separate user authorization
  because it changes system proxy selection; do not silently switch to it.

## Authorized Clash coexistence PAC trial

- User authorized this experiment. `coexist_launcher.py` starts regular capture
  on loopback 8899 and PAC on loopback 8898, then sets only `AutoConfigURL`.
  It never writes `ProxyEnable`, `ProxyServer`, or `ProxyOverride`.
- PAC sends the article hostname to 8899 with Clash 7897 as fallback, other
  public destinations to Clash, and common private/loopback IPs directly.
  This is routing coexistence, not full process or certificate isolation.
- PAC HTTP fetch returned 200. Both services started and registry retained
  `ProxyEnable=1`, `ProxyServer=127.0.0.1:7897` during the trial.
- After one focused article refresh, browser PID 2256 still had seven
  established connections to Clash and no observed capture/export button.
  Existing browser adoption of PAC remains unverified; no repeat refresh used.
- An initial cleanup access-right defect was discovered and corrected:
  querying a PAC registry value requires `KEY_QUERY_VALUE` as well as
  `KEY_SET_VALUE`. Recovery was executed with the corrected helper, removing
  only the trial PAC and leaving the original Clash fields unchanged.
- The trial's regular proxy subprocess was stopped. Temporary CA absence was
  verified after certificate deletion. The supervisor window stays open;
  the old trial's unused PAC server may remain until that window is closed,
  but Windows no longer references it. Do not claim complete process cleanup.
- Added an offline regression test for query permission during recovery and
  preservation of the static Clash configuration.
- No article export or OCR call occurred. A one-time WeChat restart to test
  fresh proxy configuration requires user authorization, not an assumption
  that restarting guarantees capture success.

## Opt-in scripted export and fresh-start trial

- Added `start_pilot.cmd` and `--auto-export-pilot` to the coexistence launcher.
  PAC now uses port 8918 to avoid the earlier supervisor's unused 8898 server.
- The selected article's injected page script invokes export automatically once
  after load/DOM readiness. Server-side atomic file creation durably caps export
  attempts at one; other articles cannot consume the gate. No automatic retry.
- This does not yet implement bulk link opening or OCR; runtime export must be
  verified before enabling those stages. Parameters are in the tool's PILOT.md.
- During the fresh-start trial, WeChat's process tree was stopped and the known
  installed executable relaunched. Only its own updater was observed afterward,
  not its main window. No update/install/authentication dialog was automated.
- Trial was stopped early while awaiting normal WeChat startup. Corrected PAC
  restoration completed; static Clash proxy remains at 7897 without a PAC URL.
- No export gate was consumed, no export created, and no OCR call issued.
- Supervisor windows were not closed. Actual export success remains unverified.

## Restored WeChat session and certificate preflight

- User restored the main WeChat session. Its existing File Transfer Assistant
  message was clicked once, with automatic pilot export enabled.
- New browser traffic reached regular capture port 8899. This establishes that
  the PAC route can work with the restarted browser, not that export works.
- Proxy log recorded repeated client TLS handshake rejection before any article
  HTML was captured. Routing was restored immediately upon diagnosis and the
  capture worker stopped. WeChat's browser retried internally; these were not
  scripted repeated clicks. No exact one-network-request guarantee is made.
- Generated PEM and CER certificate fingerprints agree, but the expected root
  was absent from CurrentUser Root. The cause of absent trust is unestablished.
- Added actual post-install trust verification before PAC changes and first-TLS-
  failure abort monitoring. A subsequent local startup failed certificate
  preflight without proxy mutation or further article requests.
- No export gate was consumed, no export created, and no OCR call made. Clash
  remains enabled at 7897 without PAC; no temporary root remains trusted.
