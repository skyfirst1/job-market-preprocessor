"""Compatibility CLI for :mod:`jobprep.runner.dashboard_server`."""

from __future__ import annotations

import sys

from jobprep.runner import dashboard_server as _implementation


if __name__ == "__main__":
    _implementation.main()

sys.modules[__name__] = _implementation
