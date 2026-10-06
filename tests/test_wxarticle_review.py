"""Offline demonstrations of pinned upstream behavior; no launcher imports."""

import ast
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup
import hashlib
from urllib.parse import urljoin


SOURCE = Path(__file__).resolve().parents[1] / 'third_party' / 'WxArticleSaver-1.1.0'


def extracted(name, env):
    tree = ast.parse((SOURCE / 'wx_article_saver.py').read_text(encoding='utf-8-sig'))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<reviewed-function>', 'exec'), env)
    return env[name]


def test_images_have_no_count_limit(tmp_path):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200, content=b'image', headers={})
    env = {'urljoin': urljoin, 'hashlib': hashlib, 'HOST': 'mp.weixin.qq.com',
           'allowed_image_url': lambda url: True, 'ext_from_response': lambda *args: '.png',
           'requests': SimpleNamespace(get=get)}
    images = BeautifulSoup(''.join(f'<img src="https://mmbiz.qpic.cn/{i}.png">' for i in range(101)), 'html.parser')
    assert extracted('download_images', env)(images, tmp_path, 'https://mp.weixin.qq.com/s/test') == 101
    assert len(calls) == 101


def test_duplicate_failed_image_is_requested_again(tmp_path):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        raise TimeoutError('mock only')
    env = {'urljoin': urljoin, 'hashlib': hashlib, 'HOST': 'mp.weixin.qq.com',
           'allowed_image_url': lambda url: True, 'ext_from_response': lambda *args: '.png',
           'requests': SimpleNamespace(get=get), 'ctx': SimpleNamespace(log=SimpleNamespace(warn=lambda *args: None))}
    images = BeautifulSoup('<img src="https://mmbiz.qpic.cn/a.png"><img src="https://mmbiz.qpic.cn/a.png">', 'html.parser')
    assert extracted('download_images', env)(images, tmp_path, 'https://mp.weixin.qq.com/s/test') == 0
    assert len(calls) == 2


def test_export_unconditionally_calls_video_downloader():
    tree = ast.parse((SOURCE / 'wx_article_saver.py').read_text(encoding='utf-8-sig'))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'save_article')
    assert any(isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call)
               and isinstance(statement.value.func, ast.Name) and statement.value.func.id == 'download_videos'
               for statement in node.body)


def test_proxy_and_ca_changes_precede_cleanup_try():
    tree = ast.parse((SOURCE / 'launcher.py').read_text(encoding='utf-8-sig'))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    cleanup = next(index for index, node in enumerate(main.body) if isinstance(node, ast.Try) and node.finalbody)
    prior_calls = {node.func.id for statement in main.body[:cleanup] for node in ast.walk(statement)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert {'trust_ca', 'start_pac_server', 'backup_and_enable_proxy'} <= prior_calls
