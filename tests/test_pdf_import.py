import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from jobprep.app import pdf_import as module
from jobprep.app.tools import empty_document


def png():
    buffer = io.BytesIO()
    Image.new('RGB', (8, 6), 'red').save(buffer, format='PNG')
    return buffer.getvalue()


def fake_pdf(tmp_path, monkeypatch, pages=None, encrypted=False):
    source = tmp_path / 'printed.pdf'
    source.write_bytes(b'%PDF-1.7\nfixture')
    reader = SimpleNamespace(is_encrypted=encrypted, pages=pages or [])
    monkeypatch.setattr(module, '_reader_class', lambda: lambda body: reader)
    return source


def page(text='', images=()):
    return SimpleNamespace(extract_text=lambda: text,
                           images=[SimpleNamespace(data=body) for body in images])


def test_contract_text_images_and_provenance(tmp_path, monkeypatch):
    source = fake_pdf(tmp_path, monkeypatch, [page('Job title', [png()]), page('Requirements')])
    root = tmp_path / 'artifacts'
    result = module.import_pdf(source, 'https://mp.weixin.qq.com/s/article?token=secret#fragment', root)
    assert empty_document('', '', root).keys() <= result.keys()
    assert result['text'] == 'Job title\n\nRequirements'
    assert result['page_count'] == result['coverage']['pages_seen'] == 2
    assert result['status'] == 'partial'
    assert result['complete'] is result['coverage']['complete'] is False
    assert not result['coverage']['list_complete'] and not result['coverage']['jd_complete']
    assert 'secret' not in json.dumps(result)
    image = result['images'][0]
    body = Path(image['path']).read_bytes()
    assert image['sha256'] == hashlib.sha256(body).hexdigest()
    assert Path(image['path']).name == image['sha256'] + '.png'
    assert (image['width'], image['height'], image['page']) == (8, 6, 1)
    assert Path(image['path']).resolve().is_relative_to(root.resolve())
    assert json.loads((root / 'document.json').read_text(encoding='utf-8')) == result
    assert {'pdf', 'pdf_page', 'image'} == {item['kind'] for item in result['evidence']}
    assert any('lazy-loaded' in warning for warning in result['warnings'])


@pytest.mark.parametrize('url', ['file:///tmp/a', 'https://u:secret@example.com/a',
                                'http://127.0.0.1/a', 'http://[::1]/a',
                                'http://10.0.0.1/a', 'http://localhost/a',
                                'http://host.local/a', 'https://example.com:bad/a',
                                'https://example.com/\nsecret', 'https://example.com\\@evil.com'])
def test_reject_nonpublic_source(url):
    with pytest.raises(ValueError):
        module.public_source_url(url)


def test_query_allowlist():
    assert module.public_source_url('https://mp.weixin.qq.com/s?__biz=abc%3D%3D&mid=123&idx=1&sn=secret&chksm=secret&token=secret#x') == 'https://mp.weixin.qq.com/s?__biz=abc%3D%3D&mid=123&idx=1'
    assert module.public_source_url('https://example.com/a?X-Amz-Signature=secret&q=private#secret') == 'https://example.com/a'
    assert module.public_source_url('https://mp.weixin.qq.com/s?mid=secret&idx=token') == 'https://mp.weixin.qq.com/s'


def test_missing_dependency_has_actionable_error(monkeypatch, tmp_path, capsys):
    def missing(name):
        raise ImportError('fixture')
    monkeypatch.setattr(module.importlib, 'import_module', missing)
    with pytest.raises(RuntimeError, match='Missing optional dependency pypdf'):
        module.import_pdf(tmp_path / 'any.pdf', 'https://example.com', tmp_path / 'out')
    with pytest.raises(SystemExit) as exit_info:
        module.main(['--path', 'any.pdf', '--source-url', 'https://example.com', '--artifact-dir', str(tmp_path)])
    assert exit_info.value.code == 2
    assert 'pypdf' in capsys.readouterr().err


@pytest.mark.parametrize('suffix,body', [('.html', b'%PDF-fixture'), ('.pdf', b'not PDF')])
def test_bad_input(tmp_path, monkeypatch, suffix, body):
    fake_pdf(tmp_path, monkeypatch)
    source = tmp_path / ('input' + suffix)
    source.write_bytes(body)
    with pytest.raises(ValueError):
        module.import_pdf(source, 'https://example.com', tmp_path / 'out')


def test_pdf_byte_limit(tmp_path, monkeypatch):
    source = fake_pdf(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'MAX_FILE_BYTES', 4)
    with pytest.raises(ValueError):
        module.import_pdf(source, 'https://example.com', tmp_path / 'out')


def test_limits_and_bad_image_preserve_text(tmp_path, monkeypatch):
    source = fake_pdf(tmp_path, monkeypatch, [page('abcdef', [b'bad image', png()]), page('unseen')])
    monkeypatch.setattr(module, 'MAX_PAGES', 1)
    monkeypatch.setattr(module, 'MAX_TEXT_CHARS', 3)
    monkeypatch.setattr(module, 'MAX_IMAGE_PIXELS', 40)
    result = module.import_pdf(source, 'https://example.com', tmp_path / 'out')
    assert result['text'] == 'abc' and result['page_count'] == 2
    assert result['coverage']['pages_seen'] == 1 and not result['images']
    assert any('text_character_limit' in w for w in result['warnings'])
    assert any('page_count_limit' in w for w in result['warnings'])
    assert sum('rejected' in w for w in result['warnings']) == 2


@pytest.mark.parametrize('limit_name,limit', [('MAX_IMAGE_BYTES', 1), ('MAX_LOCAL_IMAGE_BYTES', 1), ('MAX_LOCAL_IMAGES', 0)])
def test_image_byte_and_count_limits(tmp_path, monkeypatch, limit_name, limit):
    source = fake_pdf(tmp_path, monkeypatch, [page('text', [png()])])
    monkeypatch.setattr(module, limit_name, limit)
    result = module.import_pdf(source, 'https://example.com', tmp_path / 'out')
    assert not result['images'] and result['text'] == 'text'
    assert result['warnings']


def test_encrypted_and_corrupt(tmp_path, monkeypatch):
    source = fake_pdf(tmp_path, monkeypatch, encrypted=True)
    result = module.import_pdf(source, 'https://example.com', tmp_path / 'encrypted')
    assert result['error_kind'] == 'encrypted_pdf' and result['status'] == 'error'
    def fail(body):
        raise ValueError('https://example.com?token=secret')
    monkeypatch.setattr(module, '_reader_class', lambda: fail)
    result = module.import_pdf(source, 'https://example.com', tmp_path / 'corrupt')
    assert result['error_kind'] == 'invalid_pdf' and result['status'] == 'error'
    assert 'secret' not in json.dumps(result)


def test_empty_and_extraction_failure(tmp_path, monkeypatch):
    def fail():
        raise RuntimeError('secret')
    source = fake_pdf(tmp_path, monkeypatch, [SimpleNamespace(extract_text=fail, images=[])])
    result = module.import_pdf(source, 'https://example.com', tmp_path / 'out')
    assert result['text'] == '' and result['status'] == 'partial'
    assert any('text_extraction_failed' in w for w in result['warnings'])
    assert any('No usable' in w for w in result['warnings'])
    assert 'secret' not in json.dumps(result)


def test_cli_and_printed_url_sanitizing(tmp_path, monkeypatch, capsys):
    source = fake_pdf(tmp_path, monkeypatch, [page('Visit https://example.com/a?token=secret#x')])
    root = tmp_path / 'out'
    assert module.main(['--path', str(source), '--source-url', 'https://example.com', '--artifact-dir', str(root)]) == 0
    assert json.loads(capsys.readouterr().out)['complete'] is False
    assert 'secret' not in (root / 'document.json').read_text(encoding='utf-8')


def test_real_pypdf_when_available(tmp_path):
    pypdf = pytest.importorskip('pypdf')
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    source = tmp_path / 'blank.pdf'
    with source.open('wb') as handle:
        writer.write(handle)
    result = module.import_pdf(source, 'https://example.com', tmp_path / 'out')
    assert result['page_count'] == 1 and result['status'] == 'partial'


def test_dedup_page_mapping_and_small_image_evidence(tmp_path, monkeypatch):
    source = fake_pdf(tmp_path, monkeypatch, [page('', [png(), png()]), page('', [png()])])
    result = module.import_pdf(source, 'https://example.com', tmp_path / 'all')
    assert len(result['images']) == 1 and result['duplicate_images'] == 2
    assert result['images'][0]['pages'] == [1, 2]
    assert result['images'][0]['occurrences'] == [{'page': 1, 'image_index': 0},
                                                {'page': 1, 'image_index': 1},
                                                {'page': 2, 'image_index': 0}]
    assert result['pages'][0]['image_indices'] == [0, 0]
    assert result['pages'][1]['image_indices'] == [0]
    filtered = module.import_pdf(source, 'https://example.com', tmp_path / 'filtered', 500, 200)
    assert filtered['images'] == [] and filtered['skipped_images'] == 3
    skipped = [e for e in filtered['evidence'] if e['kind'] == 'image_skip']
    assert [e['page'] for e in skipped] == [1, 1, 2]
    assert all(e['reason'] == 'below_min_dimensions' and e['width'] == 8 for e in skipped)


def test_filter_does_not_consume_large_image_count(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    Image.new('RGB', (12, 12), 'blue').save(buffer, format='PNG')
    source = fake_pdf(tmp_path, monkeypatch, [page('', [png(), buffer.getvalue()])])
    monkeypatch.setattr(module, 'MAX_LOCAL_IMAGES', 1)
    result = module.import_pdf(source, 'https://example.com', tmp_path / 'out', 10, 10)
    assert len(result['images']) == 1 and result['skipped_images'] == 1


@pytest.mark.parametrize('value', [-1, True, 1.5, 20001])
def test_invalid_minimum_dimensions(value, tmp_path):
    with pytest.raises(ValueError):
        module.import_pdf('a.pdf', 'https://example.com', tmp_path, value, 0)


def test_real_embedded_image_and_text(tmp_path):
    pypdf = pytest.importorskip('pypdf')
    from pypdf.generic import (DecodedStreamObject, DictionaryObject,
                              NameObject, NumberObject)
    writer = pypdf.PdfWriter()
    image = DecodedStreamObject()
    image.set_data(bytes([255, 0, 0]) * 48)
    image.update({NameObject('/Type'): NameObject('/XObject'),
                  NameObject('/Subtype'): NameObject('/Image'),
                  NameObject('/Width'): NumberObject(8),
                  NameObject('/Height'): NumberObject(6),
                  NameObject('/ColorSpace'): NameObject('/DeviceRGB'),
                  NameObject('/BitsPerComponent'): NumberObject(8)})
    image_ref = writer._add_object(image)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    font_ref = writer._add_object(font)
    for _ in range(2):
        pdf_page = writer.add_blank_page(width=100, height=100)
        pdf_page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/XObject'): DictionaryObject({NameObject('/Im0'): image_ref}),
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): font_ref})})
        stream = DecodedStreamObject()
        stream.set_data(b'q 8 0 0 6 0 0 cm /Im0 Do Q BT /F1 10 Tf 10 50 Td (Job title) Tj ET')
        pdf_page[NameObject('/Contents')] = writer._add_object(stream)
    source = tmp_path / 'images.pdf'
    with source.open('wb') as handle:
        writer.write(handle)
    result = module.import_pdf(source, 'https://example.com', tmp_path / 'out')
    assert result['text'].count('Job title') == 2
    assert len(result['images']) == 1 and result['duplicate_images'] == 1
    assert result['images'][0]['pages'] == [1, 2]
