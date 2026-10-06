"""Compatibility CLI for :mod:`jobprep.analysis.batch009_targets`."""

from __future__ import annotations

import sys

from jobprep.analysis import batch009_targets as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
