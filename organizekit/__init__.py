"""Organize — the shared implementation behind the media-management toolkit.

The tools at the repository root (``bitdepth.py``, ``subtitle_fetcher.py`` and
friends) used to carry byte-identical copies of every shared helper so that a
single file could be dropped anywhere and run. Detection of drift was
automated; the copying never was, and 4,325 lines of the codebase were literal
duplicates of each other.

Everything shared now lives in :mod:`organize.core` exactly once. The tools
import from here, so a durability or correctness fix reaches all of them at the
moment it is written instead of when someone remembers to copy it six times.
"""

from __future__ import annotations

VERSION = "3.5.0"

__all__ = ["VERSION"]
