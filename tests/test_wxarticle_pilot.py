"""Exercise the patched export function without proxy setup or network."""

import ast
import html
import json
import time
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup


SOURCE = Path(__file__).resolve().parents[1] / 'tools' / 'WxArticleSaver-pilot'


def test_export_keeps_images_without_downloading_videos(tmp_path):
    tree = ast.parse((SOURCE / 'wx_article_saver.py').read_text(encoding='utf-8-sig'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'save_article')
    image_calls = []

    def forbidden(*args, **kwargs):
        raise AssertionError('Video downloader must never run')

    def images(article, folder, url):
        image_calls.append(len(article.find_all('img')))
        return image_calls[-1]

    env = {
        'Path': Path, 'BeautifulSoup': BeautifulSoup, 'EXPORT_ROOT': tmp_path,
        'detect_title': lambda soup: 'Pilot', 'detect_author': lambda soup: 'Author',
        'find_article_content': lambda soup: (soup.find('div'), '#js_content'),
        'safe_name': lambda name: name, 'time': time, 'html_lib': html, 'json': json,
        'replace_video_embeds': lambda article, url: [{'url': 'https://example.com/video.mp4'}],
        'download_videos': forbidden,
        'apply_downloaded_video_links': lambda article, videos: None,
        'normalize_article': lambda article: article,
        'download_images': images, 'md': lambda value, **kwargs: value,
        'ctx': SimpleNamespace(log=SimpleNamespace(info=lambda msg: None)),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<pilot-export>', 'exec'), env)
    markup = '<div id="js_content">JD' + '<img src="https://example.com/a.png">' * 101 + '</div>'
    folder, title, count = env['save_article']('https://mp.weixin.qq.com/s/test', markup, [])
    metadata = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
    assert image_calls == [101]
    assert title == 'Pilot' and count == 0
    assert metadata['videos_found'] == 1
    assert metadata['video_downloads'] == []
    assert metadata['images_downloaded'] == 101


def test_refresh_and_launcher_cleanup_policy():
    upstream = SOURCE.parents[1] / 'third_party' / 'WxArticleSaver-1.1.0'
    launcher = ast.parse((SOURCE / 'launcher.py').read_text(encoding='utf-8-sig'))
    main = next(n for n in launcher.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == 'remove_own_ca' for n in ast.walk(main))
    def refresh_nodes(path):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        return [ast.dump(n) for n in tree.body if isinstance(n, ast.FunctionDef) and 'refresh' in n.name]
    assert refresh_nodes(SOURCE / 'wx_article_saver.py') == refresh_nodes(upstream / 'wx_article_saver.py')
