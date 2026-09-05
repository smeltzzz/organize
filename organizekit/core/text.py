"""Report text layout primitives."""

from __future__ import annotations

import re
import textwrap

REPORT_WIDTH = 96


REPORT_MIN_WIDTH = 64


REPORT_INDENT = 2


_RULE_HEAVY = "═"


_RULE_LIGHT = "─"


def clip_text(text: str, width: int, *, ellipsis: str = "...") -> str:
    """Shorten ``text`` to at most ``width`` columns, marking the cut."""
    text = str(text)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= len(ellipsis):
        return text[:width]
    return text[: width - len(ellipsis)].rstrip() + ellipsis


def wrap_text(text: str, width: int) -> list[str]:
    """Wrap ``text`` to ``width`` columns, preserving explicit line breaks."""
    width = max(1, int(width))
    out: list[str] = []
    for paragraph in str(text).split("\n"):
        if not paragraph.strip():
            out.append("")
            continue
        chunks = textwrap.wrap(
            paragraph,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
        )
        out.extend(chunks or [""])
    return out


_PATH_BREAK_RE = re.compile(r"(?<=[/\\])|(?<=\s)")


def _pack_on_separators(text: str, width: int) -> list[str]:
    """Greedily fill lines, breaking only after a separator or a space."""
    lines: list[str] = []
    current = ""
    for token in (tok for tok in _PATH_BREAK_RE.split(text) if tok):
        if len(token) > width:
            if current.strip():
                lines.append(current.rstrip())
            current = ""
            lines.extend(line.rstrip() for line in wrap_text(token, width))
            continue
        if current and len(current) + len(token) > width:
            lines.append(current.rstrip())
            current = token
        else:
            current += token
    if current.strip():
        lines.append(current.rstrip())
    return lines


def wrap_path_text(text: str, width: int) -> list[str]:
    """Wrap ``text`` on path separators and spaces, keeping names whole.

    Report lines are usually paths, and the tail of a path - the movie folder
    or file name - is what a reader scans for.  Breaking after ``/`` and ``\\``
    keeps that name on one line, where ``wrap_text`` would happily split it in
    half.  Only a single component longer than ``width`` is hard-broken, and
    nothing is ever ellipsised away.
    """
    width = max(1, int(width))
    text = str(text)
    if len(text) <= width:
        return [text]
    out: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            out.append("")
        elif len(paragraph) <= width:
            out.append(paragraph.rstrip())
        else:
            out.extend(_pack_on_separators(paragraph, width))
    return out or [""]
