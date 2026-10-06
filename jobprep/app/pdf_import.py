"""Offline preprocessing of a manually printed PDF; never proves coverage."""

import argparse
import hashlib
import importlib
import io
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import warnings

from PIL import Image

from .tools import (MAX_DIMENSION, MAX_FILE_BYTES, MAX_IMAGE_BYTES,
                    MAX_IMAGE_PIXELS, MAX_LOCAL_IMAGE_BYTES, MAX_LOCAL_IMAGES,
                    directory, empty_document, read_bounded, save_bytes)

MAX_PAGES = 500
MAX_TEXT_CHARS = 2_000_000
MAX_IMAGE_ATTEMPTS = 10_000
PUBLIC_WECHAT_QUERY = {'__biz', 'mid', 'idx'}


def public_source_url(url):
    """Keep only public article identifiers; never resolve DNS or fetch a URL."""
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 for c in url) or '\\' in url:
        raise ValueError('source URL must be a public HTTP(S) URL')
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').encode('idna').decode('ascii').lower()
        port = parts.port
    except (ValueError, UnicodeError):
        raise ValueError('source URL is malformed') from None
    if (parts.scheme not in ('http', 'https') or not host or parts.username is not None
            or parts.password is not None or port not in (None, 80, 443)):
        raise ValueError('source URL must be public HTTP(S), without credentials')
    host = host.rstrip('.')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ('.' not in host or host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid'))
                or not re.fullmatch(r'[a-z0-9.-]+', host)
                or any(not label or label.startswith('-') or label.endswith('-') for label in host.split('.'))
                or re.fullmatch(r'[0-9.]+', host)):
            raise ValueError('source URL must have a public host')
    else:
        if not address.is_global:
            raise ValueError('source URL must have a public host')
    # WeChat's stable article identity is useful; signatures and session fields are not.
    query = []
    if host == 'mp.weixin.qq.com' and (parts.path == '/s' or parts.path.startswith('/s/')):
        for key, value in parse_qsl(parts.query, max_num_fields=100):
            if key in PUBLIC_WECHAT_QUERY and re.fullmatch(r'[A-Za-z0-9_=+/-]{1,256}', value):
                if key == '__biz' or value.isdigit():
                    query.append((key, value))
    authority = '[' + host + ']' if ':' in host else host
    if port is not None:
        authority += ':' + str(port)
    return urlunsplit((parts.scheme, authority, parts.path or '/', urlencode(query), ''))


def _reader_class():
    try:
        return importlib.import_module('pypdf').PdfReader
    except ImportError:
        raise RuntimeError('Missing optional dependency pypdf; install pypdf in this Python environment before importing PDFs') from None


def _safe_text(text):
    def replace(match):
        try:
            return public_source_url(match.group())
        except ValueError:
            return '[redacted-url]'
    return re.sub(r'https?://[^\s<>"\u3000]+', replace, text)


def _image_bytes(body):
    if not body or len(body) > MAX_IMAGE_BYTES:
        raise ValueError('image_byte_limit')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(body)) as image:
            width, height = image.size
            if (not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION
                    or width * height > MAX_IMAGE_PIXELS):
                raise ValueError('image_pixel_limit')
            image.verify()
        # Normalize formats for the existing OCR contract and strip image metadata.
        with Image.open(io.BytesIO(body)) as image:
            image.load()
            clean = Image.new('RGBA' if 'A' in image.getbands() else 'RGB', image.size)
            clean.paste(image)
            output = io.BytesIO()
            clean.save(output, format='PNG')
    normalized = output.getvalue()
    if len(normalized) > MAX_IMAGE_BYTES:
        raise ValueError('image_byte_limit')
    return normalized, width, height


def _write_document(root, result):
    target = root / 'document.json'
    if not target.resolve().is_relative_to(root):
        raise ValueError('document destination escapes artifact root')
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def import_pdf(path, source_url, artifact_dir, min_image_width=0, min_image_height=0):
    """Return and save an empty_document-compatible result without network/OCR."""
    for value in (min_image_width, min_image_height):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_DIMENSION:
            raise ValueError('minimum image dimensions must be integers in 0..20000')
    source = public_source_url(source_url)
    reader_class = _reader_class()
    path = Path(path).resolve()
    if path.suffix.lower() != '.pdf':
        raise ValueError('input must have a .pdf suffix')
    body = read_bounded(path, MAX_FILE_BYTES)
    if not body.startswith(b'%PDF-'):
        raise ValueError('input is not a PDF')
    root = directory(artifact_dir)
    result = empty_document(source, 'manual-pdf', root)
    result.update(page_count=0, complete=False, pdf_path='', pdf_sha256='', pages=[],
                  image_filter={'min_width': min_image_width, 'min_height': min_image_height},
                  skipped_images=0, duplicate_images=0)
    result['warnings'].append('Printed PDF coverage is unverified; lazy-loaded images, clipped content and unprinted pages may be missing')
    if source != source_url:
        result['warnings'].append('Source URL query/fragment was sanitized; signatures and session parameters were not retained')
    pdf_path, digest = save_bytes(root, body, '.pdf')
    result.update(pdf_path=pdf_path, pdf_sha256=digest)
    result['evidence'].append({'kind': 'pdf', 'path': pdf_path, 'sha256': digest})
    try:
        reader = reader_class(io.BytesIO(body))
        if reader.is_encrypted:
            result.update(error_kind='encrypted_pdf', error='Encrypted PDFs are not supported')
        else:
            count = len(reader.pages)
            result['page_count'] = count
            if count > MAX_PAGES:
                result['warnings'].append('page_count_limit: remaining pages were not processed')
            texts, text_size, image_size, image_count = [], 0, 0, 0
            image_by_hash = {}
            for index in range(min(count, MAX_PAGES)):
                page_number = index + 1
                page_result = {'page': page_number, 'text': '', 'image_indices': [], 'warnings': []}
                result['pages'].append(page_result)
                try:
                    page = reader.pages[index]
                except Exception:
                    page_result['warnings'].append('page_read_failed')
                    continue
                try:
                    text = _safe_text(page.extract_text() or '')
                    available = max(0, MAX_TEXT_CHARS - text_size)
                    if len(text) > available:
                        page_result['warnings'].append('text_character_limit')
                    page_result['text'] = text[:available]
                    text_size += len(page_result['text'])
                    texts.append(page_result['text'])
                except Exception:
                    page_result['warnings'].append('text_extraction_failed')
                try:
                    images = page.images
                    for image_index in range(len(images)):
                        if image_count >= MAX_IMAGE_ATTEMPTS:
                            page_result['warnings'].append('image_attempt_limit')
                            result['evidence'].append({'kind': 'image_skip', 'page': page_number,
                                                       'path': pdf_path, 'reason': 'image_attempt_limit',
                                                       'remaining_count': len(images) - image_index})
                            result['skipped_images'] += len(images) - image_index
                            break
                        image_count += 1
                        try:
                            normalized, width, height = _image_bytes(images[image_index].data)
                            image_hash = hashlib.sha256(normalized).hexdigest()
                            if width < min_image_width or height < min_image_height:
                                result['skipped_images'] += 1
                                result['evidence'].append({'kind': 'image_skip', 'page': page_number,
                                                           'path': pdf_path, 'image_index': image_index,
                                                           'sha256': image_hash, 'width': width, 'height': height,
                                                           'reason': 'below_min_dimensions'})
                                continue
                            if image_hash in image_by_hash:
                                order = image_by_hash[image_hash]
                                existing = result['images'][order]
                                if page_number not in existing['pages']:
                                    existing['pages'].append(page_number)
                                existing['occurrences'].append({'page': page_number, 'image_index': image_index})
                                page_result['image_indices'].append(order)
                                result['duplicate_images'] += 1
                                result['evidence'].append({'kind': 'image_duplicate', 'path': existing['path'],
                                                           'page': page_number, 'image_index': image_index,
                                                           'sha256': image_hash, 'order': order})
                                continue
                            if len(result['images']) >= MAX_LOCAL_IMAGES:
                                raise ValueError('image_count_limit')
                            if image_size + len(normalized) > MAX_LOCAL_IMAGE_BYTES:
                                raise ValueError('image_total_byte_limit')
                            destination, image_hash = save_bytes(root, normalized, '.png')
                            image_size += len(normalized)
                            order = len(result['images'])
                            result['images'].append({'url': source, 'path': destination,
                                                     'sha256': image_hash, 'width': width,
                                                     'height': height, 'page': page_number,
                                                     'pages': [page_number],
                                                     'occurrences': [{'page': page_number, 'image_index': image_index}],
                                                     'order': order, 'status': 'ok'})
                            image_by_hash[image_hash] = order
                            page_result['image_indices'].append(order)
                            result['evidence'].append({'kind': 'image', 'path': destination,
                                                       'sha256': image_hash, 'page': page_number,
                                                       'image_index': image_index, 'order': order})
                        except Exception:
                            result['skipped_images'] += 1
                            result['evidence'].append({'kind': 'image_skip', 'path': pdf_path,
                                                       'page': page_number, 'image_index': image_index,
                                                       'reason': 'rejected_or_extraction_failed'})
                            page_result['warnings'].append('image_' + str(image_index) + '_rejected_or_extraction_failed')
                except Exception:
                    page_result['warnings'].append('image_enumeration_failed')
            result['text'] = '\n\n'.join(texts)
            result['coverage'].update(pages_seen=len(result['pages']), stop_reason='printed_pdf_coverage_unverified')
            result['status'] = 'partial'
            if result['images']:
                result['warnings'].append('Embedded images require explicit OCR; no OCR was performed')
            if result['skipped_images']:
                result['warnings'].append('Some embedded images were skipped; inspect image_skip evidence and retained PDF')
            if not result['text'].strip() and not result['images']:
                result['warnings'].append('No usable text or embedded images extracted; this is not proof of an empty article')
            for page_result in result['pages']:
                result['evidence'].append({'kind': 'pdf_page', 'path': pdf_path,
                                           'page': page_result['page'],
                                           'text_chars': len(page_result['text']),
                                           'image_indices': page_result['image_indices']})
                result['warnings'].extend('page_' + str(page_result['page']) + ': ' + warning
                                          for warning in page_result['warnings'])
    except Exception:
        result.update(status='error', error_kind='invalid_pdf', error='PDF parsing failed; inspect the local PDF')
    if result.get('error'):
        result['warnings'].append(result['error'])
    _write_document(root, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', required=True)
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--artifact-dir', required=True)
    parser.add_argument('--min-image-width', type=int, default=0)
    parser.add_argument('--min-image-height', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        result = import_pdf(args.path, args.source_url, args.artifact_dir,
                            args.min_image_width, args.min_image_height)
    except (OSError, ValueError, RuntimeError) as error:
        # URL/path exception strings can contain secrets; print only known messages.
        if isinstance(error, RuntimeError):
            parser.exit(2, str(error) + '\n')
        parser.exit(2, 'PDF import input or artifact directory is invalid; verify URL, suffix and size limits\n')
    print(json.dumps({'status': result['status'], 'page_count': result['page_count'],
                      'images': len(result['images']), 'text_chars': len(result['text']),
                      'complete': False, 'document_path': str(Path(result['artifact_dir']) / 'document.json')}))
    return 1 if result['status'] == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
