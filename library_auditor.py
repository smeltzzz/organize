#!/usr/bin/env python3
"""Read-only Jellyfin movie-folder container audit.

Inspects only direct movie-container files inside each top-level folder under
E:\\torrents\\final_organized, prints one report, and atomically saves that
same text outside the library. It never changes media, walks extras, or creates
JSON/cache files.

A folder holding a canonical MKV but no English ``.eng.srt`` is reported as
``MISSING_SIDECAR`` instead of ``CANONICAL_MKV``. That is the one finding in
this report that another tool in the pipeline can act on, so it is called out
in its own count and listed as an actionable section. A validated legacy
``.en.srt`` is renamed to ``.eng.srt`` during the audit so the library is not
stuck on the pre-cutover name.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
import textwrap
import time
import traceback
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

# ---------------------------------------------------------------------------
# Shared helpers (vendored inline)
#
# This script is self-contained on purpose: every helper it needs is copied
# below instead of imported from a shared module, so you can take this single
# file anywhere and run it with nothing but the Python standard library.
# The other scripts in this repo carry byte-identical copies of the same
# helpers; if you change one, keep the others in sync.
# ---------------------------------------------------------------------------

EXTERNAL_SRT_MAX_BYTES = 4 * 1024 * 1024

EXTERNAL_SRT_CUE_RE = re.compile(
    r"(?m)^\s*\d+\s*\n\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}"
)

EXTERNAL_SRT_LANG = "eng"

EXTERNAL_SRT_SUFFIX = f".{EXTERNAL_SRT_LANG}.srt"  # ".eng.srt"

LEGACY_EXTERNAL_SRT_SUFFIX = ".en.srt"

COVERING_ENGLISH_SRT_SUFFIXES: tuple[str, ...] = (
    EXTERNAL_SRT_SUFFIX,
    f".{EXTERNAL_SRT_LANG}.sdh.srt",
)

# The single agreed decode order. Every tool that turns subtitle bytes into
# text uses this tuple and nothing else, so a tool cannot quietly accept an
# encoding the others would reject. "utf-8-sig" first so a provider BOM does
# not make an otherwise valid file look binary; "cp1252" last because it
# decodes almost any byte sequence and would mask a genuine encoding problem.

EXTERNAL_SRT_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "cp1252")

def normalize_srt_newlines(text: str) -> str:
    """Collapse CRLF and bare CR to LF so the cue pattern handles one form."""
    return text.replace("\r\n", "\n").replace("\r", "\n")

def decode_srt_bytes(raw: bytes) -> str | None:
    """Decode subtitle bytes in the agreed order, or ``None`` if none applies.

    Callers that need a best-effort string anyway (the fetcher inspects a
    rejected download to explain why it was rejected) decode with
    ``errors="replace"`` themselves rather than widening this contract.
    """
    for encoding in EXTERNAL_SRT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None

def srt_looks_valid(text: str) -> bool:
    """True when ``text`` contains at least one well-formed SRT cue.

    A file that fails this is not a subtitle: it is an error page, a stub, or a
    truncated download, and must never be treated as covering a movie.
    """
    return bool(EXTERNAL_SRT_CUE_RE.search(text))

def validate_srt_sidecar(path: Path) -> tuple[bool, str]:
    """Conservatively decide whether ``path`` is a usable external SRT.

    Returns ``(True, "")`` only for a regular, non-symlink, non-empty,
    size-bounded file that decodes as text and contains at least one
    well-formed cue.  Everything else returns ``(False, reason)`` with a
    human-readable explanation suitable for a report line.

    This never writes, follows symlinks, or deletes anything.
    """
    try:
        file_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        return False, f"could not stat subtitle ({exc.strerror or exc})"
    if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
        return False, "not a regular file (symlink or special file)"
    if file_stat.st_size <= 0:
        return False, "subtitle file is empty"
    if file_stat.st_size > EXTERNAL_SRT_MAX_BYTES:
        return False, f"subtitle exceeds {EXTERNAL_SRT_MAX_BYTES // (1024 * 1024)} MiB safety limit"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return False, f"could not read subtitle ({exc.strerror or exc})"
    text = decode_srt_bytes(raw)
    if text is None:
        return False, "subtitle has an unsupported text encoding"
    if not srt_looks_valid(normalize_srt_newlines(text)):
        return False, "subtitle contains no valid SRT cue"
    return True, ""

def exact_external_english_srt_path(media_path: Path) -> Path:
    """Return the canonical ``<stem>.eng.srt`` path beside a movie file."""
    return media_path.with_name(f"{media_path.stem}{EXTERNAL_SRT_SUFFIX}")

def legacy_external_english_srt_path(media_path: Path) -> Path:
    """Return the pre-cutover ``<stem>.en.srt`` path beside a movie file."""
    return media_path.with_name(f"{media_path.stem}{LEGACY_EXTERNAL_SRT_SUFFIX}")

def promote_legacy_external_english_srt(media_path: Path) -> tuple[Path | None, str]:
    """Rename a validated legacy ``.en.srt`` to the canonical ``.eng.srt``.

    Returns ``(canonical_path, "")`` when the canonical sidecar already exists
    or was just created by renaming the legacy file.  Returns ``(None, reason)``
    when there is nothing to promote or the rename is unsafe (e.g. both names
    exist, legacy is invalid, or the destination is occupied by a non-file).

    Never overwrites an existing ``.eng.srt``.  Never follows symlinks.
    """
    canonical = exact_external_english_srt_path(media_path)
    legacy = legacy_external_english_srt_path(media_path)
    try:
        if canonical.exists() and not canonical.is_symlink() and canonical.is_file():
            return canonical, ""
        if canonical.exists() or canonical.is_symlink():
            return None, f"canonical sidecar path is occupied: {canonical.name}"
    except OSError as exc:
        return None, f"could not inspect canonical sidecar: {exc}"
    try:
        if not legacy.exists() or legacy.is_symlink() or not legacy.is_file():
            return None, "legacy .en.srt is absent"
    except OSError as exc:
        return None, f"could not inspect legacy sidecar: {exc}"
    ok, reason = validate_srt_sidecar(legacy)
    if not ok:
        return None, f"legacy .en.srt is unusable ({reason})"
    try:
        os.replace(str(legacy), str(canonical))
    except OSError as exc:
        return None, f"could not rename legacy .en.srt to .eng.srt: {exc}"
    return canonical, ""

def try_file_lock(handle: Any, *, strict_non_contention: bool = False) -> bool:
    """Attempt a non-blocking exclusive lock on ``handle``.

    Returns ``True`` when the lock is taken, ``False`` when it is held by
    another process.

    ``strict_non_contention`` controls how a *real* OS error is handled:

    * ``False`` (the historical behaviour of the per-tool run locks) treats any
      ``OSError`` as "busy" — ``10bit.py`` and ``library_auditor.py`` retried
      every failure until they timed out.
    * ``True`` (the historical behaviour of the standardizer coordination lock)
      re-raises genuine errors and only reports the well-known
      "already locked" codes as busy.
    """
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return True
        except OSError as exc:
            if not strict_non_contention:
                return False
            if getattr(exc, "winerror", None) in {33, 36} or exc.errno in {
                errno.EACCES,
                errno.EAGAIN,
            }:
                return False
            raise

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if not strict_non_contention:
            return False
        # Strict mode: the only expected "busy" condition is the lock being
        # held by another process, which surfaces as EAGAIN/EWOULDBLOCK (and
        # occasionally EACCES). Anything else is a real error worth raising.
        if getattr(exc, "errno", None) in {
            errno.EACCES,
            errno.EAGAIN,
            getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
        }:
            return False
        raise

def atomic_write_text(path: Path, text: str) -> None:
    r"""Publish ``text`` to ``path`` atomically.

    Writes through a unique sibling file then ``os.replace``\ s it into place, so
    a crash never leaves a truncated report and a read in progress always sees
    either the previous file or the complete new one.  On failure the staged
    file is removed and the prior report is retained.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    try:
        staged.write_text(text, encoding="utf-8")
        os.replace(str(staged), str(path))
    except OSError:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        raise

def path_is_within(candidate: Path, parent: Path) -> bool:
    """True when ``candidate`` is ``parent`` or a descendant after normalization.

    Uses ``resolve(strict=False)`` so it also works for paths that have not been
    created yet (e.g. the report/log files in a not-yet-existing output dir).
    """
    try:
        candidate.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False

REPORT_WIDTH = 96

REPORT_MIN_WIDTH = 64

REPORT_INDENT = 2

_RULE_HEAVY = "═"

_RULE_LIGHT = "─"

def enable_utf8_stdio() -> None:
    """Pin this process's console streams to UTF-8 with replacement errors.

    The reports are full of box-drawing characters, and every tool now prints
    one.  Two failures follow from leaving the stream encoding to the locale:
    a console that cannot represent ``\u2550`` raises ``UnicodeEncodeError``
    half-way through a run, and a parent that captures a child's output with
    ``text=True`` decodes it with the *locale* encoding - cp1252 on Windows -
    which turns those same bytes into a ``UnicodeDecodeError``.

    So every tool pins its own output to UTF-8 at startup, and every caller
    that captures a child decodes it as UTF-8.  ``errors="replace"`` means a
    console that still cannot cope degrades to ``?`` instead of aborting work
    that has already been done.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # a replaced stream, e.g. under redirect_stdout
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # closed or detached stream
            pass

def print_text(text: str) -> None:
    """Print report text without ever raising on a legacy console encoding.

    Reports contain box-drawing characters.  On a console or pipe whose
    encoding cannot represent them, ``print`` raises ``UnicodeEncodeError``,
    which used to surface as a crash *after* the work was already done.  The
    fallback writes the same text with unrepresentable characters replaced.
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        try:
            encoding = sys.stdout.encoding or "utf-8"
            sys.stdout.buffer.write((text + "\n").encode(encoding, errors="replace"))
            sys.stdout.buffer.flush()
        except Exception:  # pragma: no cover - a stream that cannot be written at all
            print(text.encode("ascii", errors="replace").decode("ascii"), flush=True)

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

class Report:
    """Builder for one tool's plain-text report.

    The layout is fixed so every tool reads the same way::

        +--------------------------------------------------------------+
        |  boxed header: title, subtitle, aligned metadata             |
        +--------------------------------------------------------------+

          scorecard: right-aligned counts, one line per outcome

          ══ SECTION TITLE ═════════════════════════════════════  n of m ══
          wrapped explanation of why this section matters

             1  first entry
                Reason    aligned, wrapped detail field
                Next      the thing to do about it

    Nothing here writes to disk; call :meth:`render` and hand the text to
    ``atomic_write_text``.
    """

    def __init__(self, title: str, subtitle: str = "", *, width: int = REPORT_WIDTH) -> None:
        self._title = title
        self._subtitle = subtitle
        self._width = max(REPORT_MIN_WIDTH, int(width))
        self._meta: list[tuple[str, str]] = []
        self._body: list[str] = []

    # -- geometry ------------------------------------------------------
    @property
    def width(self) -> int:
        return self._width

    @property
    def _inner(self) -> int:
        """Columns available inside the header box (``║ `` + text + `` ║``)."""
        return self._width - 4

    # -- header --------------------------------------------------------
    def meta(self, label: str, value: object) -> Report:
        """Add one ``label  value`` row to the boxed header."""
        self._meta.append((str(label), "" if value is None else str(value)))
        return self

    def metas(self, pairs: Iterable[tuple[str, object]]) -> Report:
        for label, value in pairs:
            self.meta(label, value)
        return self

    @staticmethod
    def _is_rule(line: str) -> bool:
        """True for a line that is only rule characters (used to space entries)."""
        stripped = line.strip()
        return bool(stripped) and set(stripped) <= {_RULE_HEAVY, _RULE_LIGHT}

    def _box_row(self, text: str) -> str:
        return "║ " + clip_text(text, self._inner, ellipsis="..").ljust(self._inner) + " ║"

    def render_header(self) -> str:
        """Render just the boxed header (used for the startup banner too)."""
        lines = ["╔" + _RULE_HEAVY * (self._width - 2) + "╗"]
        lines.append(self._box_row(self._title))
        if self._subtitle:
            for chunk in wrap_text(self._subtitle, self._inner):
                lines.append(self._box_row(chunk))
        if self._meta:
            lines.append("╟" + _RULE_LIGHT * (self._width - 2) + "╢")
            label_width = max(len(label) for label, _ in self._meta)
            value_width = self._inner - label_width - 2
            for label, value in self._meta:
                if not value:
                    lines.append(self._box_row(label))
                    continue
                # Long values here are usually paths; break them at a
                # separator so a directory name is not split mid-word.
                chunks = wrap_path_text(value, value_width) or [""]
                pad = " " * (label_width + 2)
                for position, chunk in enumerate(chunks):
                    lead = f"{label.ljust(label_width)}  " if position == 0 else pad
                    lines.append(self._box_row(lead + chunk))
        lines.append("╚" + _RULE_HEAVY * (self._width - 2) + "╝")
        return "\n".join(lines)

    # -- body ----------------------------------------------------------
    def blank(self, count: int = 1) -> Report:
        self._body.extend([""] * max(0, count))
        return self

    def rule(self, char: str = _RULE_LIGHT, *, indent: int = REPORT_INDENT) -> Report:
        self._body.append(" " * indent + char * max(0, self._width - indent))
        return self

    def paragraph(self, text: str, *, indent: int = REPORT_INDENT) -> Report:
        """A wrapped block of prose; leading spaces on continuation lines."""
        for chunk in wrap_text(text, self._width - indent):
            self._body.append(" " * indent + chunk)
        return self

    def title_line(self, text: str, *, right: str = "", indent: int = REPORT_INDENT) -> Report:
        """``text`` left-aligned with ``right`` pushed to the right margin."""
        span = self._width - indent
        if not right:
            self._body.append(" " * indent + clip_text(text, span))
            return self
        gap = span - len(right) - len(text)
        if gap < 2:
            self._body.append(" " * indent + clip_text(f"{text}  {right}", span))
        else:
            self._body.append(" " * indent + text + " " * gap + right)
        return self

    def scorecard(self, rows: Iterable[tuple], *, indent: int = REPORT_INDENT) -> Report:
        """Render ``(count, label, hint)`` rows between two light rules.

        The count is right-aligned so a reader can scan the numbers as a
        column, and the hint column is clipped rather than wrapped: a scorecard
        is meant to fit on one screen.
        """
        materialized = [(str(count), str(label), str(hint or "")) for count, label, hint in rows]
        if not materialized:
            return self
        count_width = max(4, max(len(count) for count, _, _ in materialized))
        label_width = max(len(label) for _, label, _ in materialized)
        span = self._width - indent
        self.rule(indent=indent)
        for count, label, hint in materialized:
            line = f"{count:>{count_width}}   {label:<{label_width}}"
            if hint:
                room = span - len(line) - 3
                if room > 8:
                    line += "   " + clip_text(hint, room)
            self._body.append(" " * indent + clip_text(line, span))
        self.rule(indent=indent)
        return self

    def section(
        self,
        title: str,
        *,
        count: int | None = None,
        total: int | None = None,
        intro: str = "",
        indent: int = REPORT_INDENT,
    ) -> Report:
        """Open a major section: a heavy banner plus an optional explanation."""
        if self._body and self._body[-1].strip():
            self.blank()
        tally = ""
        if count is not None:
            # A partial or interrupted run can report more items in a group than
            # the scan counted; "5 of 3" would be nonsense, so the total is only
            # shown when it is actually the larger number.
            show_total = total is not None and int(total) >= int(count)
            tally = f"{count} of {total}" if show_total else str(count)
        span = self._width - indent
        head = f"{_RULE_HEAVY}{_RULE_HEAVY} {title} "
        tail = f" {tally} {_RULE_HEAVY}{_RULE_HEAVY}" if tally else ""
        fill = span - len(head) - len(tail)
        if fill < 3:
            self._body.append(" " * indent + clip_text(head.strip() + ("  " + tally if tally else ""), span,
                                                      ellipsis=""))
        else:
            self._body.append(" " * indent + head + _RULE_HEAVY * fill + tail)
        if intro:
            self.blank()
            self.paragraph(intro, indent=indent)
        self.blank()
        return self

    def subsection(
        self,
        title: str,
        *,
        count: int | None = None,
        indent: int = REPORT_INDENT,
    ) -> Report:
        """Open a labelled group inside a section (one light rule, not a box)."""
        if self._body and self._body[-1].strip():
            self.blank()
        span = self._width - indent
        tally = f" {count}" if count is not None else ""
        head = f"{_RULE_LIGHT}{_RULE_LIGHT} {title} "
        tail = f"{tally} {_RULE_LIGHT}{_RULE_LIGHT}"
        fill = span - len(head) - len(tail)
        if fill < 3:
            self._body.append(" " * indent + clip_text(head.strip() + tally, span, ellipsis=""))
        else:
            self._body.append(" " * indent + head + _RULE_LIGHT * fill + tail)
        return self

    def entry(
        self,
        text: str,
        *,
        detail: str = "",
        ordinal: int | None = None,
        marker: str = "",
        fields: Iterable[tuple[str, str]] = (),
        detail_column: int = 0,
        indent: int = 4,
    ) -> Report:
        """One item in a section.

        ``ordinal`` numbers the entry; ``marker`` is a short tag used instead
        when numbering would be noise.  ``detail_column`` puts a short detail
        on the same line at a fixed column (used for name/sidecar tables) and
        falls back to a wrapped line underneath when it would not fit.
        ``fields`` are ``label  value`` pairs aligned under the entry text.
        """
        if ordinal is not None:
            prefix = f"{ordinal:>4}  "
        elif marker:
            prefix = f"{marker:<4}  "
        else:
            prefix = "      "
        span = self._width - indent
        head_limit = span - len(prefix)
        if detail_column > 0:
            # A fixed detail column only reads as a table when the entry text
            # stays inside it, so long titles wrap to a continuation line
            # instead of pushing every detail sideways.
            head_limit = min(head_limit, max(8, detail_column - indent - len(prefix)))
        # Entry text wraps rather than being ellipsised: the tail of a long
        # path is usually the part a reader came for, and clipping it away
        # hides the very information the report exists to convey.
        head_chunks = wrap_path_text(text, max(8, head_limit)) or [""]
        # Entries breathe: a blank line separates them, but a section banner or
        # its explanation paragraph keeps the first entry tight underneath.
        if self._body and self._body[-1].strip() and not self._is_rule(self._body[-1]):
            self._body.append("")
        head_index = len(self._body)
        self._body.append(" " * indent + prefix + head_chunks[0])
        continuation = " " * (indent + len(prefix))
        self._body.extend(continuation + chunk for chunk in head_chunks[1:])
        materialized = [(str(label), str(value or "")) for label, value in fields]
        if materialized:
            label_width = max(6, max(len(label) for label, _ in materialized))
            for label, value in materialized:
                lead = f"{label.ljust(label_width)}  "
                chunks = wrap_text(value, max(8, span - len(prefix) - len(lead))) or [""]
                self._body.append(continuation + lead + chunks[0])
                for chunk in chunks[1:]:
                    self._body.append(continuation + " " * len(lead) + chunk)
        if detail:
            head = head_chunks[0]
            # A detail can ride on the entry's own line only when that entry
            # text did not have to wrap; otherwise it belongs underneath.
            if detail_column > 0 and len(head_chunks) == 1:
                room = detail_column - indent - len(prefix) - len(head)
                if room >= 1 and len(detail) <= span - detail_column:
                    self._body[head_index] = (
                        " " * indent + prefix + head.ljust(detail_column - indent - len(prefix)) + detail
                    )
                    return self
            for chunk in wrap_text(detail, max(8, span - len(prefix) - 2)):
                self._body.append(continuation + "  " + chunk)
        return self

    def table(
        self,
        headers: Iterable[str],
        rows: Iterable[Iterable],
        *,
        aligns: str = "",
        indent: int = 4,
    ) -> Report:
        """An aligned column table with a header row and a rule under it.

        ``aligns`` is one character per column, ``<`` or ``>``.  Columns are
        sized to their content and then trimmed - widest first, never below
        their header - so the table always fits inside the report width.
        """
        head = [str(column) for column in headers]
        body = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
        columns = len(head)
        if not columns:
            return self
        aligns = (aligns or "<" * columns).ljust(columns, "<")[:columns]
        span = self._width - indent
        widths = [
            max([len(head[i])] + [len(row[i]) for row in body if i < len(row)])
            for i in range(columns)
        ]
        gaps = 2 * (columns - 1)
        minimums = [max(6, len(column)) for column in head]
        while sum(widths) + gaps > span:
            shrinkable = [i for i in range(columns) if widths[i] > minimums[i]]
            if not shrinkable:
                break
            widths[max(shrinkable, key=lambda i: widths[i])] -= 1

        def render(cells: list[str]) -> str:
            parts = []
            for i, cell in enumerate(cells[:columns]):
                text = clip_text(cell, widths[i])
                parts.append(text.rjust(widths[i]) if aligns[i] == ">" else text.ljust(widths[i]))
            return " " * indent + "  ".join(parts).rstrip()

        self._body.append(render(head))
        self._body.append(" " * indent + "  ".join(_RULE_LIGHT * width for width in widths))
        for row in body:
            self._body.append(render(list(row) + [""] * (columns - len(row))))
        return self

    def entries(self, items: Iterable, **defaults: object) -> Report:
        """Render an iterable of entry specs, numbered in order.

        Each item is either a ``(text, detail)`` tuple or a mapping of
        :meth:`entry` keyword arguments (``detail``, ``fields``, ``marker``).
        ``defaults`` supplies the keyword arguments shared by every item.
        """
        for position, item in enumerate(items, start=1):
            if isinstance(item, tuple):
                text, detail = (list(item) + [""])[:2]
                spec: dict = {"text": text, "detail": detail}
            else:
                spec = dict(item)
            spec.setdefault("ordinal", position)
            merged = {**defaults, **spec}
            self.entry(str(merged.pop("text", "")), **merged)
        return self

    def footer(self, lines: Iterable[str] = (), *, indent: int = REPORT_INDENT) -> Report:
        """Close the report with a light rule and trailing notes."""
        self.blank()
        self.rule(indent=indent)
        for line in lines:
            self.paragraph(line, indent=indent)
        return self

    # -- output --------------------------------------------------------
    def render(self) -> str:
        """The whole report as one string, always ending in a newline."""
        lines = self.render_header().split("\n")
        lines.append("")
        lines.extend(self._body)
        # Trailing spaces are invisible in a terminal and noisy in a diff.
        return "\n".join(line.rstrip() for line in lines).rstrip() + "\n"

SOURCE_DIR = r"E:\torrents\final_organized"
# Logs and reports live under tools\ReportsAndLogs so the root of E:\torrents
# stays media-only.
OUTPUT_DIR = r"E:\torrents\tools\ReportsAndLogs\library_auditor"
LOG_FILE = r"E:\torrents\tools\ReportsAndLogs\library_auditor\library_auditor.log"
REPORT_FILE = r"E:\torrents\tools\ReportsAndLogs\library_auditor\library_auditor_report.txt"
VERSION = "2.1.0"
LOCK_NAME = ".jellyfin_movie_folder_auditor.lock"

# .mkv is canonical movie_standardizer.py output. Other extensions are reported
# only as direct-folder exceptions; an extension is a container label, not a
# codec or Jellyfin direct-play guarantee.
MOVIE_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".webm",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".vob", ".flv",
    ".ogv", ".3gp", ".asf", ".rm", ".rmvb", ".m2v", ".divx",
    ".f4v", ".mxf", ".dv", ".wtv", ".dvr-ms", ".iso", ".img", ".nrg",
})
JUNK_SUFFIXES = (".!qb", ".parts", ".part", ".crdownload", ".tmp", ".temp")

# mkv_track_cleaner.py stages a remux as a full-size sibling named
# ``temp_clean_<token>__<original>.mkv`` and atomically swaps it over the
# original only after verification. An audit that happens to run mid-remux
# would otherwise count that staged copy as a second feature and report
# MULTIPLE_DIRECT_MOVIE_FILES for a perfectly healthy folder. It is a
# transaction artifact, not library content, so it is not counted.
# (The matching journal, ``.track_cleaner.<token>.json``, is already excluded
# by the leading-dot rule in is_junk_filename.) Orphaned staging files are the
# cleaner's own orphan-recovery responsibility, not a library-layout finding.
TRACK_CLEANER_TEMP_PREFIX = "temp_clean_"

# Folder states that mean the layout itself is wrong. MISSING_SIDECAR is
# deliberately absent: a freshly standardized movie has no sidecar until
# subtitle_fetcher.py runs, so counting it as a defect would make the exit-code
# gate fail on every healthy new library.
DEFECT_STATES = frozenset({
    "SINGLE_OTHER_CONTAINER",
    "MULTIPLE_DIRECT_MOVIE_FILES",
    "NO_DIRECT_MOVIE_FILE",
    "MKV_STEM_MISMATCH",
    "NONCANONICAL_SIDECAR",
    "INVALID_SIDECAR",
    "INACCESSIBLE",
})

PRINT_LOCK = Lock()

@dataclass
class Config:
    source_dir: Path = field(default_factory=lambda: Path(SOURCE_DIR))
    log_file: Path = field(default_factory=lambda: Path(LOG_FILE))
    report_file: Path = field(default_factory=lambda: Path(REPORT_FILE))
    lock_timeout_seconds: float = 60.0
    fail_on_findings: bool = False
    fail_on_defects: bool = False

@dataclass(frozen=True)
class MovieFile:
    name: str
    extension: str
    size_bytes: int

@dataclass
class FolderAudit:
    folder: Path
    state: str
    movie_files: list[MovieFile] = field(default_factory=list)
    detail: str = ""

@dataclass
class Audit:
    source_dir: Path
    folders: list[FolderAudit]
    elapsed_sec: float = 0.0

_ACTIVE_LOG_FILE: Path | None = None

def log(message: str, level: str = "INFO", log_file: Path | None = None) -> None:
    """Print a timestamped event and append the identical event to this script's log."""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}"
    target = log_file if log_file is not None else _ACTIVE_LOG_FILE
    with PRINT_LOCK:
        print_text(line)
        if target is None:
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8", errors="replace") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

def is_junk_filename(name: str) -> bool:
    lower = name.casefold()
    return lower.startswith(".") or lower in {"thumbs.db", "desktop.ini"} or any(lower.endswith(s) for s in JUNK_SUFFIXES)

def is_in_flight_remux(name: str) -> bool:
    """True for a mkv_track_cleaner.py staging file written during a remux."""
    return name.casefold().startswith(TRACK_CLEANER_TEMP_PREFIX)

class LockUnavailable(RuntimeError):
    pass

class ExclusiveRunLock:
    """Fail-closed advisory lock compatible with Windows and POSIX."""

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.handle: Any | None = None

    def _try_lock(self) -> bool:
        assert self.handle is not None
        if os.name == "nt":
            # Materialize a byte before the Windows byte-range lock.
            self.handle.seek(0)
            self.handle.write("0")
            self.handle.flush()
        return try_file_lock(self.handle, strict_non_contention=False)

    def __enter__(self) -> ExclusiveRunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while not self._try_lock():
            if time.monotonic() >= deadline:
                self.handle.close()
                self.handle = None
                raise LockUnavailable(f"another audit owns {self.path}")
            time.sleep(0.2)
        assert self.handle is not None
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()} started={datetime.now(UTC).isoformat()}\n")
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc, traceback_obj) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.handle.close()
            self.handle = None

def run_lock_path(source: Path) -> Path:
    key = hashlib.sha256(str(source.resolve(strict=False)).encode("utf-8", errors="surrogatepass")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"{LOCK_NAME}.{key}"

# =============================================================================
# DIRECT FOLDER AUDIT
# =============================================================================

def direct_movie_files(folder: Path) -> tuple[list[MovieFile], str]:
    """Inspect direct files only; nested extras, subtitles, artwork, and NFOs are ignored."""
    try:
        entries = list(folder.iterdir())
    except OSError as exc:
        return [], str(exc)
    found: list[MovieFile] = []
    for entry in entries:
        if not entry.is_file() or is_junk_filename(entry.name):
            continue
        if is_in_flight_remux(entry.name):
            continue
        extension = entry.suffix.casefold()
        if extension not in MOVIE_EXTENSIONS:
            continue
        try:
            found.append(MovieFile(entry.name, extension, entry.stat().st_size))
        except OSError as exc:
            return [], f"cannot stat {entry.name}: {exc}"
    return sorted(found, key=lambda item: item.name.casefold()), ""

def classify_folder(folder: Path) -> FolderAudit:
    files, error = direct_movie_files(folder)
    if error:
        return FolderAudit(folder, "INACCESSIBLE", detail=error)
    if not files:
        return FolderAudit(folder, "NO_DIRECT_MOVIE_FILE")
    if len(files) != 1:
        return FolderAudit(folder, "MULTIPLE_DIRECT_MOVIE_FILES", files)

    feature = files[0]
    if feature.extension != ".mkv":
        return FolderAudit(folder, "SINGLE_OTHER_CONTAINER", files)
    if feature.name != f"{folder.name}.mkv":
        return FolderAudit(folder, "MKV_STEM_MISMATCH", files, "expected exact folder-name MKV stem")

    expected_srt = f"{folder.name}{EXTERNAL_SRT_SUFFIX}"
    covering_srts = {f"{folder.name}{suffix}" for suffix in COVERING_ENGLISH_SRT_SUFFIXES}
    legacy_srt = f"{folder.name}{LEGACY_EXTERNAL_SRT_SUFFIX}"
    # Promote a validated legacy .en.srt to the canonical .eng.srt before the
    # naming check so a library cut over from the previous convention is not
    # stuck as NONCANONICAL_SIDECAR forever.
    mkv_path = folder / feature.name
    promoted, promote_reason = promote_legacy_external_english_srt(mkv_path)
    if promoted is None and promote_reason and "absent" not in promote_reason and "unusable" not in promote_reason:
        return FolderAudit(
            folder, "NONCANONICAL_SIDECAR", files,
            f"legacy {legacy_srt} could not be promoted ({promote_reason})",
        )
    try:
        srt_names = sorted(
            entry.name for entry in folder.iterdir()
            if entry.is_file() and entry.suffix.casefold() == ".srt" and not is_junk_filename(entry.name)
        )
    except OSError as exc:
        return FolderAudit(folder, "INACCESSIBLE", files, str(exc))
    unexpected_srt = [name for name in srt_names if name not in covering_srts]
    if unexpected_srt:
        return FolderAudit(folder, "NONCANONICAL_SIDECAR", files, "; ".join(unexpected_srt))
    covering_present = [name for name in srt_names if name in covering_srts]
    if not covering_present:
        # Structurally fine, but no external English subtitle yet. This is the
        # kind of audit finding another tool can act on, so it is reported as
        # its own state instead of being folded into CANONICAL_MKV.
        return FolderAudit(
            folder, "MISSING_SIDECAR", files,
            f"no English {EXTERNAL_SRT_SUFFIX} sidecar; subtitle_fetcher.py can still fetch one",
        )
    # The name is right, so check the contents. A sidecar that is empty, an
    # error page, or a truncated download looks perfectly healthy to a
    # filename-only audit, but it silently blocks every downstream tool: the
    # fetcher will not replace a file it thinks is already there and the
    # cleaner will not trust it. That dead end has to be visible here.
    last_reason = ""
    for name in covering_present:
        usable, reason = validate_srt_sidecar(folder / name)
        if usable:
            return FolderAudit(folder, "CANONICAL_MKV", files)
        last_reason = f"{name} is unusable ({reason}); delete it and re-run subtitle_fetcher.py"
    return FolderAudit(folder, "INVALID_SIDECAR", files, last_reason)

def audit_library(cfg: Config) -> Audit:
    try:
        folders = sorted((p for p in cfg.source_dir.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name.casefold())
    except OSError as exc:
        log(f"Cannot enumerate library: {exc}", level="ERROR")
        folders = []
    log(f"Found {len(folders)} top-level movie folder(s).")
    audited: list[FolderAudit] = []
    for index, folder in enumerate(folders, 1):
        result = classify_folder(folder)
        audited.append(result)
        if result.state == "MISSING_SIDECAR":
            # Advisory, not a defect: the layout is correct, only the subtitle
            # is absent. Logged at INFO so a library that simply has not been
            # fetched yet does not drown the console in warnings.
            log(f"[{index}/{len(folders)}] {result.state}: {folder.name}", level="INFO")
        elif result.state != "CANONICAL_MKV":
            log(f"[{index}/{len(folders)}] {result.state}: {folder.name}", level="WARNING")
        elif index == len(folders) or index % 25 == 0:
            log(f"[{index}/{len(folders)}] audited: {folder.name}")
    return Audit(cfg.source_dir, audited)

# =============================================================================
# REPORT
# =============================================================================

def types_for(item: FolderAudit) -> str:
    return ", ".join(sorted({file.extension.upper() for file in item.movie_files})) if item.movie_files else "—"

def names_for(item: FolderAudit) -> str:
    return "; ".join(file.name for file in item.movie_files) if item.movie_files else "—"

@dataclass(frozen=True)
class StateGuide:
    """How one audit state reads in the report: a label, a hint and a fix."""

    state: str
    label: str
    hint: str
    title: str
    fix: str

# Reading order is the order of the report: the two sidecar states share one
# actionable group (that is the group subtitle_fetcher.py exists to clear), and
# the remaining layout defects follow, cheapest fix first.
STATE_GUIDES: tuple[StateGuide, ...] = (
    StateGuide(
        "MISSING_SIDECAR", "Missing Eng SRT", "run subtitle_fetcher.py",
        "MOVIES WITH NO USABLE EXTERNAL ENGLISH SRT (ACTIONABLE)",
        "Run subtitle_fetcher.py before mkv_track_cleaner.py: fetching first keeps the "
        "pristine release moviehash, which is what makes an exact subtitle match possible.",
    ),
    StateGuide(
        "INVALID_SIDECAR", "Invalid Eng SRT", "delete the broken sidecar",
        "MOVIES WITH NO USABLE EXTERNAL ENGLISH SRT (ACTIONABLE)",
        "An INVALID entry means a sidecar exists but is unusable - delete that file first, "
        "because no tool will replace a sidecar it believes is already present.",
    ),
    StateGuide(
        "NONCANONICAL_SIDECAR", "Noncanonical SRT", f"rename it to {EXTERNAL_SRT_SUFFIX}",
        "SIDECAR NAME IS NOT CANONICAL",
        f"Rename the subtitle to \"<movie>{EXTERNAL_SRT_SUFFIX}\". Jellyfin and Plex only "
        "direct play that exact name beside the MKV.",
    ),
    StateGuide(
        "MKV_STEM_MISMATCH", "MKV stem mismatch", "rename the MKV to match its folder",
        "MKV NAME DOES NOT MATCH ITS FOLDER",
        "The movie file must be named exactly like the folder that holds it: "
        "\"Title (Year)/Title (Year).mkv\".",
    ),
    StateGuide(
        "SINGLE_OTHER_CONTAINER", "Single other container", "remux to MKV or accept it",
        "MOVIE IS NOT AN MKV",
        "MKV is the only container this toolkit cleans and the only one guaranteed to "
        "carry an external SRT plus every track type Jellyfin direct plays.",
    ),
    StateGuide(
        "MULTIPLE_DIRECT_MOVIE_FILES", "Multiple movie files", "keep one feature per folder",
        "MORE THAN ONE MOVIE FILE IN A FOLDER",
        "One folder holds one feature. Move extras into a \"extras\" subfolder or into "
        "their own \"Title (Year)\" folder.",
    ),
    StateGuide(
        "NO_DIRECT_MOVIE_FILE", "No movie file", "the folder holds no feature",
        "FOLDER HAS NO MOVIE FILE",
        "Nothing in this folder is a movie container. Delete the leftover or move the "
        "movie back in.",
    ),
    StateGuide(
        "INACCESSIBLE", "Inaccessible", "check permissions and the path",
        "FOLDER COULD NOT BE READ",
        "The folder could not be listed at all. Check permissions, ownership and that "
        "the path still exists.",
    ),
)
CANONICAL_LABEL = "Canonical MKV"
CANONICAL_HINT = f"one MKV + a validated {EXTERNAL_SRT_SUFFIX}"

def build_report(audit: Audit, cfg: Config) -> str:
    """Render the audit: what is wrong first, then the full inventory."""
    counts = Counter(item.state for item in audit.folders)
    type_counts = Counter(file.extension.upper() for item in audit.folders for file in item.movie_files)
    total = len(audit.folders)
    findings = total - counts["CANONICAL_MKV"]
    by_state: dict[str, list[FolderAudit]] = {}
    for item in audit.folders:
        by_state.setdefault(item.state, []).append(item)

    report = Report(
        "JELLYFIN MOVIE LIBRARY AUDIT",
        "Read-only health check \u00b7 canonical folder layout and external English "
        "subtitle integrity",
    )
    report.metas([
        ("Generated", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")),
        ("Library", audit.source_dir),
        ("Folders checked", total),
        ("Elapsed", f"{audit.elapsed_sec:.2f}s"),
        ("Report", cfg.report_file),
    ])

    rows: list[tuple[object, str, str]] = [(counts["CANONICAL_MKV"], CANONICAL_LABEL, CANONICAL_HINT)]
    for guide in STATE_GUIDES:
        rows.append((counts[guide.state], guide.label, guide.hint))
    rows.append((total, "Folders checked", "every top-level folder in the library"))
    report.blank()
    report.scorecard(rows)
    if findings:
        report.paragraph(
            f"Start here: {findings} of {total} folder(s) are not canonical \u00b7 every one is "
            "listed below with the fix that clears it."
        )
    else:
        report.paragraph(
            f"Nothing to do: all {total} folder(s) hold exactly one MKV named like their "
            f"folder, each with a validated {EXTERNAL_SRT_SUFFIX} beside it."
        )

    report.section(
        "FOLDERS THAT NEED ATTENTION",
        count=findings,
        total=total,
        intro="Grouped by the fix that clears them, cheapest first. A folder appears in exactly one group.",
    )
    if not findings:
        report.paragraph("None. The library is fully canonical.")
    else:
        # The two sidecar states share one group: both mean "no usable .eng.srt".
        sidecar_states = [guide for guide in STATE_GUIDES if guide.state in ("MISSING_SIDECAR", "INVALID_SIDECAR")]
        sidecar_items = [item for state in ("MISSING_SIDECAR", "INVALID_SIDECAR") for item in by_state.get(state, [])]
        if sidecar_items:
            report.subsection(sidecar_states[0].title, count=len(sidecar_items))
            for guide in sidecar_states:
                if counts[guide.state]:
                    report.paragraph(f"{guide.label}: {guide.fix}")
            report.blank()
            report.entries(
                [{"text": item.folder.name,
                  "detail": f"{_state_label(item.state)}  \u00b7  {item.detail or names_for(item)}"}
                 for item in sorted(sidecar_items, key=lambda entry: entry.folder.name.casefold())],
            )
        for guide in STATE_GUIDES:
            if guide.state in ("MISSING_SIDECAR", "INVALID_SIDECAR"):
                continue
            items = by_state.get(guide.state) or []
            if not items:
                continue
            report.subsection(guide.title, count=len(items))
            report.paragraph(guide.fix)
            report.blank()
            report.entries(
                [{"text": item.folder.name, "detail": item.detail or names_for(item)}
                 for item in sorted(items, key=lambda entry: entry.folder.name.casefold())],
            )

    report.section(
        "EVERY FOLDER CHECKED",
        count=total,
        intro="The complete inventory, healthy folders included.",
    )
    if not audit.folders:
        report.paragraph("No top-level movie folders found.")
    else:
        report.table(
            ["Folder", "Status", "Type(s)", "Movie file(s) / detail"],
            [[item.folder.name,
              item.state.replace("_", " "),
              types_for(item),
              item.detail or names_for(item)]
             for item in audit.folders],
            aligns="<<<<",
        )

    report.section("DIRECT MOVIE FILE TYPES", intro="Container labels are file extensions only.")
    if type_counts:
        report.table(
            ["Type", "Files"],
            [[extension, count]
             for extension, count in sorted(type_counts.items(), key=lambda pair: (-pair[1], pair[0]))],
            aligns="<>",
        )
    else:
        report.paragraph("No direct movie-container files found.")

    canonical = counts["CANONICAL_MKV"]
    pct = (100.0 * canonical / total) if total else 100.0
    report.footer([
        # Machine-readable verdict for orchestrators (jellyfin_one_shot.py):
        # one stable line, no layout assumptions.
        f"AUDIT SUMMARY: canonical={canonical}; total={total}; pct={pct:.1f}%",
        "Scope: direct feature containers plus direct SRT sidecar names. Artwork, NFO files, "
        "and nested extras are ignored.",
        "Container labels are file extensions only; they do not verify codecs or Jellyfin "
        "client direct-play support.",
    ])
    return report.render()

def _state_label(state: str) -> str:
    """The short scorecard label for a state (``MISSING`` for a missing sidecar)."""
    for guide in STATE_GUIDES:
        if guide.state == state:
            return guide.label.split(" ")[0].upper()
    return state.replace("_", " ")

# =============================================================================
# EXECUTION + CLI
# =============================================================================

def validate_config(cfg: Config) -> list[str]:
    errors: list[str] = []
    if not cfg.source_dir.is_dir():
        errors.append(f"--source is not an accessible directory: {cfg.source_dir}")
    if cfg.lock_timeout_seconds < 0:
        errors.append("--lock-timeout must be zero or greater")
    if path_is_within(cfg.report_file, cfg.source_dir):
        errors.append(f"--report must be outside --source: {cfg.report_file}")
    if path_is_within(cfg.log_file, cfg.source_dir):
        errors.append(f"--log must be outside --source: {cfg.log_file}")
    if os.path.normcase(os.path.normpath(str(cfg.log_file))) == os.path.normcase(os.path.normpath(str(cfg.report_file))):
        errors.append("--log and --report must be different files")
    return errors

def exit_code_for(counts: Counter, cfg: Config) -> int:
    """Translate audit results into a scheduler-usable exit status.

    The auditor used to return 0 no matter what it found, so a Task Scheduler
    or cron run could never report that the library had gone unhealthy. Both
    gates are opt-in and the default stays 0, so existing automation that only
    reads the report is unaffected.
    """
    findings = sum(n for state, n in counts.items() if state != "CANONICAL_MKV")
    defects = sum(n for state, n in counts.items() if state in DEFECT_STATES)
    if cfg.fail_on_findings and findings:
        log(f"FAIL-ON-FINDINGS: {findings} folder(s) are not canonical.", level="ERROR")
        return 1
    if cfg.fail_on_defects and defects:
        log(f"FAIL-ON-DEFECTS: {defects} folder(s) have a layout defect.", level="ERROR")
        return 1
    return 0

def run(cfg: Config) -> int:
    errors = validate_config(cfg)
    if errors:
        for error in errors:
            log(error, level="ERROR", log_file=None)
        return 2
    global _ACTIVE_LOG_FILE
    _ACTIVE_LOG_FILE = cfg.log_file
    log(f"Starting read-only library audit; source={cfg.source_dir}")
    log(f"Log={cfg.log_file}; report={cfg.report_file}")
    try:
        with ExclusiveRunLock(run_lock_path(cfg.source_dir), cfg.lock_timeout_seconds):
            started = time.perf_counter()
            audit = audit_library(cfg)
            audit.elapsed_sec = time.perf_counter() - started
            report = build_report(audit, cfg)
            atomic_write_text(cfg.report_file, report)
            counts = Counter(item.state for item in audit.folders)
            log(f"Audit complete: canonical_mkv={counts['CANONICAL_MKV']}; exceptions={len(audit.folders) - counts['CANONICAL_MKV']}; elapsed={audit.elapsed_sec:.2f}s")
            log(f"Report published: {cfg.report_file}")
            print_text(report)
        return exit_code_for(counts, cfg)
    except LockUnavailable as exc:
        log(f"Audit lock unavailable: {exc}", level="ERROR")
        return 3
    except OSError as exc:
        log(f"Could not save report {cfg.report_file}: {exc}", level="ERROR")
        return 2

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print and save a direct movie-file type report for Jellyfin movie folders.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--source", type=Path, default=Path(SOURCE_DIR), help="Top-level canonical Jellyfin movie-folder library")
    parser.add_argument("--log", type=Path, default=Path(LOG_FILE), help="Append-only execution log outside the media library")
    parser.add_argument("--report", type=Path, default=Path(REPORT_FILE), help="The sole replaceable plain-text output report")
    parser.add_argument("--lock-timeout", type=float, default=60.0, metavar="SECONDS", help="Maximum wait for another audit run")
    parser.add_argument("--fail-on-findings", action="store_true",
                        help="Exit 1 when any folder is not CANONICAL_MKV (includes missing sidecars)")
    parser.add_argument("--fail-on-defects", action="store_true",
                        help="Exit 1 only on layout defects; a missing sidecar alone still exits 0")
    parser.add_argument("--self-test", action="store_true")
    return parser

def cfg_from_args(args: argparse.Namespace) -> Config:
    return Config(
        source_dir=args.source,
        log_file=args.log,
        report_file=args.report,
        lock_timeout_seconds=args.lock_timeout,
        fail_on_findings=bool(args.fail_on_findings),
        fail_on_defects=bool(args.fail_on_defects),
    )

# =============================================================================
# SELF-TEST
# =============================================================================

def run_self_tests() -> int:
    errors: list[str] = []
    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    check(is_junk_filename("movie.mkv.!qB"), "torrent temporary suffix")
    root = Path(tempfile.mkdtemp(prefix="jellyfin_auditor_"))
    try:
        library, output = root / "library", root / "reports"
        library.mkdir()
        output.mkdir()
        valid_srt = "1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n"
        (library / "Movie One (2020)").mkdir()
        (library / "Movie One (2020)" / "Movie One (2020).mkv").write_bytes(b"mkv")
        (library / "Movie One (2020)" / f"Movie One (2020){EXTERNAL_SRT_SUFFIX}").write_text(valid_srt, encoding="utf-8")
        (library / "Movie One (2020)" / "Featurettes").mkdir()
        (library / "Movie One (2020)" / "Featurettes" / "making-of.mp4").write_bytes(b"extra")
        (library / "Legacy (1999)").mkdir()
        (library / "Legacy (1999)" / "Legacy (1999).AVI").write_bytes(b"avi")
        (library / "Multiple (2001)").mkdir()
        (library / "Multiple (2001)" / "Multiple (2001).mkv").write_bytes(b"mkv")
        (library / "Multiple (2001)" / "Multiple (2001).mp4").write_bytes(b"mp4")
        (library / "No Movie (2002)").mkdir()
        (library / "No Movie (2002)" / f"No Movie (2002){EXTERNAL_SRT_SUFFIX}").write_text("sub", encoding="utf-8")
        (library / "Stem Mismatch (2003)").mkdir()
        (library / "Stem Mismatch (2003)" / "wrong-name.mkv").write_bytes(b"mkv")
        # A forced/flagged English SRT is not the canonical plain .eng.srt.
        (library / "Sidecar Mismatch (2004)").mkdir()
        (library / "Sidecar Mismatch (2004)" / "Sidecar Mismatch (2004).mkv").write_bytes(b"mkv")
        (library / "Sidecar Mismatch (2004)" / "Sidecar Mismatch (2004).eng.forced.srt").write_text(valid_srt, encoding="utf-8")
        (library / "Sdh Cover (2007)").mkdir()
        (library / "Sdh Cover (2007)" / "Sdh Cover (2007).mkv").write_bytes(b"mkv")
        (library / "Sdh Cover (2007)" / "Sdh Cover (2007).eng.sdh.srt").write_text(valid_srt, encoding="utf-8")
        # A correctly named sidecar whose contents are unusable is a real defect:
        # nothing downstream will replace a subtitle it believes is present.
        (library / "Broken Subs (2005)").mkdir()
        (library / "Broken Subs (2005)" / "Broken Subs (2005).mkv").write_bytes(b"mkv")
        (library / "Broken Subs (2005)" / f"Broken Subs (2005){EXTERNAL_SRT_SUFFIX}").write_text("sub", encoding="utf-8")
        # A validated legacy .en.srt is promoted to .eng.srt during the audit.
        (library / "Legacy En (2006)").mkdir()
        (library / "Legacy En (2006)" / "Legacy En (2006).mkv").write_bytes(b"mkv")
        (library / "Legacy En (2006)" / f"Legacy En (2006){LEGACY_EXTERNAL_SRT_SUFFIX}").write_text(valid_srt, encoding="utf-8")
        cfg = Config(source_dir=library, log_file=output / "audit.log", report_file=output / "report.txt", lock_timeout_seconds=0)
        audit = audit_library(cfg)
        states = {item.folder.name: item.state for item in audit.folders}
        check(states == {
            "Legacy (1999)": "SINGLE_OTHER_CONTAINER",
            "Movie One (2020)": "CANONICAL_MKV",
            "Multiple (2001)": "MULTIPLE_DIRECT_MOVIE_FILES",
            "No Movie (2002)": "NO_DIRECT_MOVIE_FILE",
            "Stem Mismatch (2003)": "MKV_STEM_MISMATCH",
            "Sidecar Mismatch (2004)": "NONCANONICAL_SIDECAR",
            "Broken Subs (2005)": "INVALID_SIDECAR",
            "Legacy En (2006)": "CANONICAL_MKV",
            "Sdh Cover (2007)": "CANONICAL_MKV",
        }, f"folder states {states}")
        check(
            (library / "Legacy En (2006)" / f"Legacy En (2006){EXTERNAL_SRT_SUFFIX}").is_file(),
            "legacy .en.srt was promoted to .eng.srt",
        )
        check(
            not (library / "Legacy En (2006)" / f"Legacy En (2006){LEGACY_EXTERNAL_SRT_SUFFIX}").exists(),
            "legacy .en.srt removed after promote",
        )
        report = build_report(audit, cfg)
        check(f"Movie One (2020){EXTERNAL_SRT_SUFFIX}" not in report and "making-of.mp4" not in report, "non-direct media leaked into report")
        # The scorecard is the contract: a right-aligned count, three spaces,
        # then the label. Asserting on it keeps the report honest about what a
        # reader sees at a glance.
        check("   1   MKV stem mismatch" in report and "   1   Noncanonical SRT" in report,
              "canonical exception counts")
        check("   1   Invalid Eng SRT" in report and "MOVIES WITH NO USABLE EXTERNAL ENGLISH SRT" in report,
              "unusable sidecar reported as actionable")
        atomic_write_text(cfg.report_file, report)
        check(cfg.report_file.read_text(encoding="utf-8") == report, "saved report differs")
        check(not list(output.glob("*.json")), "JSON output exists")
        check(bool(validate_config(Config(source_dir=library, log_file=output / "audit.log", report_file=library / "bad.txt", lock_timeout_seconds=0))), "report within library accepted")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("SELF-TEST PASSED (direct folders + types + single report)")
    return 0

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_tests()
    try:
        enable_utf8_stdio()
        return run(cfg_from_args(args))
    except KeyboardInterrupt:
        log("Interrupted")
        return 130
    except Exception:
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
