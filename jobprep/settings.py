import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def load_settings(root=ROOT):
    root = Path(root).resolve()
    load_dotenv(root / '.env', override=False)
    path = root / 'config' / 'workstation.json'
    config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    config.setdefault('crawl', {})
    config.setdefault('sites', {})
    config['root'] = root
    config['data_dir'] = root / 'data'
    config['export_dir'] = root / 'exports'
    config['ocr_max_calls'] = int(os.getenv('JOBPREP_OCR_MAX_CALLS', config.get('ocr_max_calls', 50)))
    config['ocr_model'] = os.getenv('JOBPREP_OCR_MODEL', config.get('ocr_model', 'general')).strip()
    return config


def options_for_url(config, url):
    from urllib.parse import urlsplit

    options = dict(config.get('crawl', {}))
    host = (urlsplit(url).hostname or '').lower()
    for domain, overrides in config.get('sites', {}).items():
        if host == domain or host.endswith('.' + domain):
            options.update(overrides)
    return options
