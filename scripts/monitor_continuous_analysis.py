"""Compatibility CLI for :mod:`jobprep.runner.continuous_analysis`."""

from __future__ import annotations

import sys

from jobprep.runner import continuous_analysis as _implementation


if __name__ == "__main__":
    _implementation.main()

sys.modules[__name__] = _implementation
