"""Compatibility CLI for the application Feishu public UI collector."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobprep.app.feishu import API_PATH, PortalTransport, main

__all__ = ['API_PATH', 'PortalTransport', 'main']


if __name__ == '__main__':
    raise SystemExit(main())
