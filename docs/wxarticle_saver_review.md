# WxArticleSaver Review

Date: 2026-10-05. Source snapshot: `third_party/WxArticleSaver-1.1.0`.
Downloaded from the author's v1.1.0 tag, without executing launch/install scripts.
Archive SHA-256: `fc746ac86ca56bae888fac33c54380c8af0822c38cff67ad8f3d574f1458fca0`.
This is a locally calculated identity, not a publisher security attestation.
Original AGPL-3.0 license is retained. Source availability permits auditing, not
automatic exemption from Windows application-control policy.

## Issue And PR Evidence

- [All-state GitHub issue API](https://api.github.com/repos/shanliuling/WxArticleSaver/issues?state=all&per_page=100)
  returned one item: merged PR #1, no standalone open or closed bug issues.
- [PR #1](https://github.com/shanliuling/WxArticleSaver/pull/1) adds macOS support;
  describes packaging/dependency fixes, manual certificate trust and unsupported
  signing/notarization. Its stated tests concern macOS, not this Windows setup.
- [Release notes](https://github.com/shanliuling/WxArticleSaver/releases)
  and [README](https://github.com/shanliuling/WxArticleSaver) document restarting
  WeChat, cached-page refresh, proxy restoration and CA trust.

Sparse issue history is insufficient evidence of reliability. No public bug
report was invented; findings below come from the pinned code and offline tests.

## Findings

1. High: startup cleanup does not cover all setup stages. `launcher.py:369`
   trusts the CA, enables the PAC/system proxy and creates the exports directory
   before entering its cleanup `try` (`:403`). Setup failures can leave changes
   behind. Previous-backup restoration failure is not fatal, so a subsequent
   backup may overwrite the original recovery state. Repair transaction/rollback
   and preserve failed backups before real operation.
2. High for this pilot: automatic refresh is not a lifetime one-shot.
   `wx_article_saver.py:169` uses a 15-second cooldown and may send Ctrl+R to a
   foreground WeChat window, not a uniquely pinned article window. Disable the
   refresh path completely; do not assume the GUI observer guarantees the target.
3. High for request budget: `wx_article_saver.py:899` downloads videos during
   export. Direct/HLS paths can issue many requests and use large byte caps.
   Disable video handling for recruitment-poster acquisition.
4. Medium: image acquisition (`wx_article_saver.py:451`) has no image-count or
   aggregate-byte cap. It reads the full body before its 25 MiB check; redirects
   are not explicitly disabled. Failed identical image URLs are not remembered,
   allowing another attempt when the same image occurs twice. Add attempt-based
   limits, stream limits, no redirects/retries and attempted-URL deduplication.
5. Medium: an export executes synchronously inside the proxy request callback
   (`wx_article_saver.py:1113`). A slow asset can hold the export; repeated export
   clicks lack a durable one-export gate. Add bounded execution and a durable
   gate, with explicit incomplete evidence rather than silent success.

## Offline Verification

`tests/test_wxarticle_review.py`: four passing tests, no network or account:

- A mock article with 101 unique images causes 101 image requests.
- Two occurrences of one failing image cause two attempts.
- Video downloader is an unconditional direct step in export.
- CA/proxy setup precedes the cleanup try/finally.

Tests extract only reviewed function ASTs or inspect structure; they never import
the launcher or install certificates. These demonstrate control defects, not
successful WeChat compatibility. Root pytest discovery is restricted to our
`tests` directory so vendor test imports are not accidentally executed.

## Recommendation

Proceed with an audited pilot fork, not the original launcher. Implement exact
article binding, one durable export, disabled auto-refresh/video, bounded image
attempts and bytes, and reliable rollback. Preserve upstream license and a patch
log. Images actually missing from output must remain OCR/coverage gaps.

Only after offline validation should we separately seek consent to trust a
temporary local CA and modify the user's proxy settings. Do not terminate the
existing WeChat/supervision window; if a restart is needed, ask first. Never
disable application-control or other Windows protections to run this project.
No CA, proxy setting or account state was changed during this review. No WeChat
article was opened or downloaded; upstream runtime compatibility is untested.
