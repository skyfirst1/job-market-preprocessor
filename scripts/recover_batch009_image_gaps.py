"""Compatibility CLI for :mod:`jobprep.app.batch009_image_recovery`."""

from __future__ import annotations

import sys

from jobprep.app import batch009_image_recovery as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
