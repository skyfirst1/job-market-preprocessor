# Offline Printed PDF Import

Run after a person has saved the article through the WeChat browser's native
print-to-PDF flow. This importer uses only an existing local PDF. It does not
control WeChat, fetch URLs, configure proxies/certificates, read `.env`, or call
OCR/API services.

```powershell
python -m jobprep.app.pdf_import --path "D:\job_market\out\article.pdf" --source-url "https://mp.weixin.qq.com/s/ARTICLE" --artifact-dir "D:\job_market\out\pdf-artifacts"
```

Pillow is an existing project dependency. `pypdf` is optional and imported only
when importing a PDF. If unavailable, the CLI exits 2 with an explicit message
to install it into the selected Python environment. Requirements are unchanged;
the importer never installs dependencies. `--ocr` is intentionally not provided.

The reusable signature is
`import_pdf(path, source_url, artifact_dir, min_image_width=0, min_image_height=0)`.
Use `--min-image-width 500 --min-image-height 200` to select large images for the
main OCR workflow. Both dimensions must meet the threshold; defaults of zero
keep all valid images. Small images do not enter `images`, but each occurrence
has `image_skip` evidence with its page, PDF image index, dimensions, normalized
hash and `below_min_dimensions` reason. The retained PDF contains their bytes.
`skipped_images` counts skipped occurrences, including failures/limits.

`document.json` includes every `empty_document` field, plus `page_count`,
`complete`, `pdf_path`, `pdf_sha256`, and `pages`. `text` contains extracted
page text in page order; each page records its text, image indices and warnings.
Evidence references the retained PDF, page numbers, and extracted images.
Images are metadata-stripped PNGs named `<sha256>.png`, with absolute local
paths, SHA-256, width, height, page number, order and `status: ok`. Repeated
images share a single `images` entry and file, with `pages` and `occurrences`
mapping every page and PDF image index. `image_duplicate` evidence records
repeated occurrences; `duplicate_images` counts them. PNG hashes
describe normalized bytes, not the original embedded encoding.

The importer reports `status: partial` even for successful extraction.
`complete`, `coverage.complete`, `list_complete`, and `jd_complete` remain false.
Printing cannot prove lazy-loaded images, the whole article, job lists or job
details were included. A textless PDF may contain scans or vector outlines;
embedded-image extraction does not rasterize pages or reconstruct the page
layout. No usable extraction is reported as partial with an explicit warning.
Encrypted PDFs and parser failures produce an error document and CLI exit 1.
Invalid input, missing dependencies and filesystem failures exit 2. Partial
results exit 0; callers must inspect warnings and coverage.

Limits: PDF 30 MiB, 500 processed pages, 2 million extracted text characters
(excluding page separators), 100 unique selected images, 10,000 attempted
embedded images, 10 MiB per image
before and after normalization, 30 MiB total saved image bytes, 20,000 pixels
per side and 40 million pixels per image. Pillow verifies and decodes images
under decompression-bomb checks. Rejected images and extraction failures become
page warnings without discarding usable text. `pypdf` may allocate decoded
streams before image validation; these limits are not a process memory/CPU
sandbox for hostile PDFs. Use PDFs you printed yourself.

Source URLs must use HTTP(S), no credentials, and a syntactically public host.
Private IP literals, local hostnames and nonstandard ports are rejected.
No DNS resolution or availability check occurs. Fragments and all unknown query
parameters are removed. Only WeChat `/s` article identity fields `__biz`, `mid`
and `idx` are retained with restricted values; `sn`, `chksm`, tokens, signed URL
fields and session parameters are removed. Supply a public article permalink
when a signature is needed to access a given URL. HTTP(S) URLs recognized in
extracted text receive the same sanitization. This is not general secret
redaction: retained raw PDF bytes may contain original URLs, metadata or private
content, and ordinary text is preserved. Keep artifacts local.

For explicit OCR, the authorized main workflow can load the document and reuse
an already configured engine through the existing interface:

```python
result = await AppTools(artifact_dir=artifact_dir).ocr(document, engine)
```

The importer does not construct an engine or invoke this step. Persist the
returned OCR result through the main workflow. OCR results appear in `ocr` and
gaps in `ocr_gaps`; OCR success still does not establish print coverage.

Offline tests:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_pdf_import.py -q
```

Tests use simulated PDF reader results for dependency-independent boundary
checks. Real blank-PDF and embedded-image/text integration tests run when `pypdf` exists
and are otherwise explicitly skipped.
