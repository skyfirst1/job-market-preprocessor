"""Backward-compatible facade for the runner layer."""

from .runner import Workstation, safe_error

__all__ = ['Workstation', 'safe_error']
