# Desktop Downloader Installation

Prepared on 2026-10-05, without launching the application or contacting WeChat.

- Author repository: https://github.com/qiye45/wechatDownload
- Release: https://github.com/qiye45/wechatDownload/releases/tag/4.7
- Package: https://github.com/qiye45/wechatDownload/releases/download/4.7/wechatDownload4.7.zip
- Published SHA-256: `2f9dfb81f47ab82122f29beea05756d2f51cabc5eda4e1a24ecdc6f2e5292f48`
- Downloaded package SHA-256 matches the published value exactly.
- Directory: `tools/wechatDownload/4.7` (portable executable plus bundled PDF/DOCX converters).
- Archive paths were checked for destination containment before extraction.
- Windows Authenticode: all three executables are unsigned (`NotSigned`).

Hash verification establishes correspondence to the published release, not a
security audit. Version 5.0 has no application binary in its GitHub release
assets; its distribution link requires a WeChat article. Version 4.7 was chosen
to use a direct author-controlled GitHub package without cloud-drive login.
Its current compatibility must still be verified; do not silently upgrade or
follow remote fallback after failure.

Await user confirmation immediately before launching the unsigned program.
Keep the supervision window open, avoid account/batch/history operations, check
GUI download settings and enable only the local MCP required for the one-call
pilot. No article download has been attempted during installation preparation.
