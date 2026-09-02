#!/usr/bin/env python3
"""
Jellyfin 10-Bit Movie Compliance Inspector
===========================================
Walk a movie library with ffprobe and split every real feature into
action queues that match how HandBrake should actually be used:

  1. 8-bit SDR        → QUEUE   (re-encode H.265/AV1 10-bit)
  2. 10/12/16-bit SDR → SKIP    (already high bit-depth SDR)
  3. HDR (10-bit+)    → KEEP    (HDR10 / HDR10+ / Dolby Vision / HLG)
  4. 8-bit "HDR"      → REVIEW  (mis-tagged or broken; do NOT treat as SDR)
  5. Unknown bit depth → REVIEW (never assume this is 8-bit SDR)
  6. Errors            → listed, never silently dropped

BT.2020 primaries alone are *not* HDR (wide-gamut SDR exists).
8-bit + PQ/HLG is *not* an SDR HandBrake candidate.

Requires ``ffprobe`` on PATH (or next to this script / common install dirs).
Zero Python third-party dependencies.

    python 10bit.py
    python 10bit.py --source "E:\\torrents\\final_organized" --dry-run
    python 10bit.py --self-test
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
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

class MediaProbeCache:
    """Best-effort ``(path, size, mtime) -> probe payload`` cache.

    ``10bit.py`` spawns one ``ffprobe`` per movie and ``mkv_track_cleaner.py``
    spawns one ``mkvmerge -J`` per movie, on every single run, even for a
    library that has not changed since the last sweep. Those subprocesses
    dominate the cost of a maintenance run.

    A probe is a pure function of a file's bytes, so a stored payload is reused
    only while both the size and ``st_mtime_ns`` are unchanged. Crucially, only
    the *probe output* is cached and never a tool's verdict: every consumer
    still re-derives its own decision from live filesystem state. A cached
    entry therefore cannot make a tool blind to a change it must react to — a
    sidecar appearing next to a movie, a hardlink count dropping when seeding
    stops, or a remux landing.

    Deliberately fail-open on reads and fail-silent on writes: a missing,
    unreadable, truncated, corrupt, foreign or stale cache is a miss rather
    than an error, and a cache that cannot be saved costs only the next run's
    speed. Nothing here can turn a correct run into an incorrect one.

    ``path_norm`` keys mean the two tools agree on identity the same way they
    already agree on lock keys.
    """

    SCHEMA = 1

    def __init__(
        self,
        path: Path | str,
        *,
        tool: str = "probe",
        enabled: bool = True,
        max_entries: int = 20000,
    ) -> None:
        self.path = Path(path)
        self.tool = tool
        self.enabled = bool(enabled)
        self.max_entries = max(1, int(max_entries))
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._dirty = False
        if self.enabled:
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        if raw.get("schema") != self.SCHEMA or raw.get("tool") != self.tool:
            # A different tool's cache or an older format: start clean rather
            # than guess at a layout we do not understand.
            return
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return
        self._entries = {
            str(key): value for key, value in entries.items() if isinstance(value, dict)
        }

    def get(self, file_path: Path | str, size: int, mtime_ns: int) -> dict[str, Any] | None:
        """Return a stored payload for an unchanged file, else ``None``."""
        if not self.enabled:
            self.misses += 1
            return None
        key = path_norm(file_path)
        with self._lock:
            entry = self._entries.get(key)
            if (
                entry is not None
                and entry.get("size") == int(size)
                and entry.get("mtime_ns") == int(mtime_ns)
            ):
                payload = entry.get("payload")
                if isinstance(payload, dict):
                    self.hits += 1
                    return payload
            self.misses += 1
            return None

    def put(self, file_path: Path | str, size: int, mtime_ns: int, payload: dict[str, Any]) -> None:
        """Store a probe payload, evicting oldest entries past ``max_entries``."""
        if not self.enabled:
            return
        key = path_norm(file_path)
        with self._lock:
            # Pop-then-insert refreshes recency: a plain dict preserves
            # insertion order but has no OrderedDict.move_to_end.
            self._entries.pop(key, None)
            self._entries[key] = {
                "size": int(size),
                "mtime_ns": int(mtime_ns),
                "payload": payload,
            }
            while len(self._entries) > self.max_entries:
                self._entries.pop(next(iter(self._entries)), None)
            self._dirty = True

    def save(self) -> None:
        """Persist the cache atomically. Failures are swallowed by design."""
        if not self.enabled or not self._dirty:
            return
        with self._lock:
            snapshot = dict(self._entries)
            self._dirty = False
        document = {"schema": self.SCHEMA, "tool": self.tool, "entries": snapshot}
        try:
            atomic_write_text(
                self.path,
                json.dumps(document, separators=(",", ":"), ensure_ascii=False) + "\n",
            )
        except OSError:
            pass

    def __len__(self) -> int:
        return len(self._entries)

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

def path_norm(path: Path | str) -> str:
    """Normalize a path the same way every tool compares them.

    ``normcase`` lower-cases on Windows and is a no-op on POSIX; ``normpath``
    collapses ``..`` and duplicate separators.  Matching this exactly is what
    lets the standardizer, cleaner and subtitle fetcher agree on a lock key and
    on whether two paths are the same file.
    """
    return os.path.normcase(os.path.normpath(str(path)))

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

def format_bytes(size: int | float | None) -> str:
    """Human file size with the unit spacing the reports use."""
    if size is None or size <= 0:
        return "0 B"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"  # pragma: no cover - unreachable

def format_duration(seconds: float | None) -> str:
    """``H:MM:SS`` (or ``M:SS`` under an hour); an em-dash-free ``-`` when unknown."""
    if not seconds or seconds <= 0:
        return "-"
    total = int(round(seconds))
    hours, total = divmod(total, 3600)
    minutes, secs = divmod(total, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

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

# =============================================================================
# CONFIGURATION  (CLI flags override these)
# =============================================================================

SOURCE_DIR = r"E:\torrents\final_organized"
# Logs, reports, and the probe cache live under tools\ReportsAndLogs so the
# root of E:\torrents stays media-only.
OUTPUT_DIR = r"E:\torrents\tools\ReportsAndLogs\10bit"
LOG_FILE = r"E:\torrents\tools\ReportsAndLogs\10bit\10bit.log"
REPORT_FILE = r"E:\torrents\tools\ReportsAndLogs\10bit\10bit_report.txt"
# Reused ffprobe output for files whose size and mtime have not changed, so a
# re-scan of an unchanged library does not respawn ffprobe per movie.
CACHE_FILE = r"E:\torrents\tools\ReportsAndLogs\10bit\10bit_probe_cache.json"

# movie_standardizer.py emits canonical MKV feature files only.
VIDEO_EXTENSIONS = {".mkv"}
# Do not silently omit a valid movie because it is unusually short or small.
MIN_FILE_SIZE_MB = 0
MAX_CPU_WORKERS = 8
PROBE_TIMEOUT_SEC = 45
PROBE_SIZE = "8M"
ANALYZE_DURATION = "10M"

# Skip Plex/Jellyfin extra folders and disc-structure internals.
SKIP_DIR_NAMES = frozenset({
    "featurettes", "extras", "specials", "shorts", "bonus",
    "behind the scenes", "deleted scenes", "interviews", "scenes",
    "trailers", "other", "samples", "sample", "clips",
    "bdmv", "certificate", "video_ts", "audio_ts",
    "subs", "subtitles", "proof", "screens", "screenshots",
})

# =============================================================================
# CONSTANTS
# =============================================================================

VERSION = "2.4.0"
# This inspector never changes media. It writes one append-only log, one
# replaceable report, and (unless --no-cache) one reusable probe cache; all
# three live outside the media library.
CREATE_NO_WINDOW = 0x08000000
LOCK_NAME = ".jellyfin_10bit_inspector.lock"

# PQ (SMPTE ST 2084) and HLG are HDR transfer signals. SMPTE ST 428-1 is
# digital-cinema gamma, not an HDR signal for this Jellyfin movie workflow.
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67", "hlg"})
# Primaries/matrix alone are wide color, not HDR.
WCG_PRIMARIES = frozenset({"bt2020", "bt2020nc", "bt2020-10", "bt2020-12"})

# Dolby Vision base-layer compatibility, from dv_bl_signal_compatibility_id.
# "Dolby Vision" on its own does not answer the practical question - whether a
# client that cannot decode Dolby Vision can still play the file.
DOVI_BASE_LAYER = {
    0: "no SDR/HDR10 fallback",
    1: "HDR10 base",
    2: "SDR base",
    3: "base layer not specified",
    4: "HLG base",
}
# Sample-entry names that mean Dolby Vision by definition, used when a file
# carries no DV configuration record to read.
DV_CODEC_NAMES = frozenset({"dvh1", "dvhe", "dvav", "dva1", "dv1e", "dvh1e", "dva1e"})

# Explicit 8-bit names (no trailing bit count in the token).
PIX_FMT_BITS: dict[str, int] = {
    "yuv420p": 8, "yuv422p": 8, "yuv444p": 8, "yuv410p": 8, "yuv411p": 8,
    "yuvj420p": 8, "yuvj422p": 8, "yuvj444p": 8,
    "nv12": 8, "nv21": 8, "nv16": 8, "nv24": 8,
    "rgb24": 8, "bgr24": 8, "rgba": 8, "bgra": 8, "argb": 8, "abgr": 8,
    "gbrp": 8, "gray": 8, "ya8": 8, "pal8": 8, "uyvy422": 8, "yuyv422": 8,
    "p010le": 10, "p010be": 10, "p210le": 10, "p210be": 10,
    "p410le": 10, "p410be": 10, "v210": 10, "v410": 10,
    "p012le": 12, "p012be": 12,
    "p016le": 16, "p016be": 16, "p216le": 16, "p216be": 16,
    "v216": 16, "rgb48le": 16, "rgb48be": 16, "rgba64le": 16, "rgba64be": 16,
    "gray16le": 16, "gray16be": 16,
}

STATUS_QUEUE = "QUEUE_FOR_HANDBRAKE"
STATUS_SKIP_SDR = "SKIP_SDR_HIBIT"
STATUS_SKIP_HDR = "SKIP_HDR"
STATUS_REVIEW_8BIT_HDR = "REVIEW_8BIT_HDR"
STATUS_REVIEW_UNKNOWN_DEPTH = "REVIEW_UNKNOWN_BIT_DEPTH"
STATUS_ERROR = "ERROR"
STATUS_SKIPPED = "SKIPPED"

CATEGORY_LABELS = {
    STATUS_QUEUE: "1. 8-BIT SDR  —  QUEUE FOR HANDBRAKE",
    STATUS_SKIP_SDR: "2. HIGH BIT-DEPTH SDR  —  SKIP (already 10/12/16-bit)",
    STATUS_SKIP_HDR: "3. NATIVE HDR  —  KEEP (do not send through HandBrake)",
    STATUS_REVIEW_8BIT_HDR: "4. 8-BIT TAGGED HDR  —  REVIEW (do not treat as SDR)",
    STATUS_REVIEW_UNKNOWN_DEPTH: "5. UNKNOWN BIT DEPTH  —  REVIEW (do not queue)",
    STATUS_ERROR: "6. UNREADABLE / ERRORS",
}

# =============================================================================
# DATA
# =============================================================================

@dataclass
class Config:
    source_dir: Path = field(default_factory=lambda: Path(SOURCE_DIR))
    log_file: Path = field(default_factory=lambda: Path(LOG_FILE))
    report_file: Path = field(default_factory=lambda: Path(REPORT_FILE))
    cache_file: Path = field(default_factory=lambda: Path(CACHE_FILE))
    use_cache: bool = True
    min_file_size_mb: float = MIN_FILE_SIZE_MB
    workers: int = MAX_CPU_WORKERS
    timeout: float = PROBE_TIMEOUT_SEC
    dry_run: bool = False
    verbose: bool = False
    ffprobe: str = "ffprobe"
    lock_timeout_seconds: float = 60.0
    fail_if_queue: bool = False
    fail_if_review: bool = False
    fail_if_error: bool = False

    @property
    def min_bytes(self) -> int:
        return int(self.min_file_size_mb * 1024 * 1024)

@dataclass
class ProbeResult:
    path: str
    status: str
    category: str
    info: str
    bit_depth: int | None = None
    bit_depth_evidence: str = ""
    hdr: bool = False
    hdr_flavors: list[str] = field(default_factory=list)
    hdr_evidence: list[str] = field(default_factory=list)
    dv_profile: str = ""
    codec: str = ""
    pix_fmt: str = ""
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    duration_sec: float | None = None
    error: str = ""

CFG = Config()
PRINT_LOCK = Lock()
_ACTIVE_LOG_FILE: Path | None = None

def log(msg: str, level: str = "INFO", log_file: Path | None = None) -> None:
    """Print a timestamped event and append the identical event to this script's log."""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
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
            # Logging must never make this read-only inspector alter or abandon media work.
            pass

# =============================================================================
# FFPROBE LOCATION
# =============================================================================

def find_ffprobe(explicit: str | None = None) -> str | None:
    candidates: list[str] = []
    if explicit and explicit != "ffprobe":
        candidates.append(explicit)
    which = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if which:
        candidates.append(which)
    here = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
    candidates.extend([
        str(here / "ffprobe.exe"),
        str(here / "ffprobe"),
        str(here / "ffmpeg" / "ffprobe.exe"),
        r"C:\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files\FFmpeg\bin\ffprobe.exe",
    ])
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        path = Path(cand)
        if path.is_file():
            return str(path)
    return which

def ffprobe_works(binary: str) -> bool:
    try:
        r = subprocess.run(
            [binary, "-version"],
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

class LockUnavailable(RuntimeError):
    """Raised when another inspector instance owns the run lock."""

class ExclusiveRunLock:
    """A fail-closed advisory lock compatible with Windows and POSIX hosts."""

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.handle: Any | None = None

    def _try_lock(self) -> bool:
        assert self.handle is not None
        if os.name == "nt":
            # Materialize a leading byte once, exactly as the original did.
            self.handle.seek(0)
            if self.handle.tell() == 0:
                self.handle.write("0")
                self.handle.flush()
        return try_file_lock(self.handle, strict_non_contention=False)

    def __enter__(self) -> ExclusiveRunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if self._try_lock():
                self.handle.seek(0)
                self.handle.truncate()
                self.handle.write(f"pid={os.getpid()} started={datetime.now(UTC).isoformat()}\n")
                self.handle.flush()
                return self
            if time.monotonic() >= deadline:
                self.handle.close()
                self.handle = None
                raise LockUnavailable(f"another inspector run holds {self.path}")
            time.sleep(0.2)

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

# =============================================================================
# BIT DEPTH / HDR CLASSIFICATION  (pure — unit-tested)
# =============================================================================

def _as_int(value: Any) -> int | None:
    if value is None or value == "" or value == "N/A":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None

def _as_float(value: Any) -> float | None:
    if value is None or value == "" or value == "N/A":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None

def bit_depth_from_pix_fmt(pix_fmt: str) -> int | None:
    fmt = (pix_fmt or "").lower().strip()
    if not fmt:
        return None
    if fmt in PIX_FMT_BITS:
        return PIX_FMT_BITS[fmt]
    if "p010" in fmt:
        return 10
    if "p012" in fmt:
        return 12
    if "p016" in fmt:
        return 16
    match = re.search(r"(\d{1,2})(?:le|be)?$", fmt)
    if match:
        n = int(match.group(1))
        if n in {8, 9, 10, 12, 14, 16}:
            return n
    return None

def bit_depth_from_profile(profile: str) -> int | None:
    p = (profile or "").lower()
    if not p:
        return None
    if re.search(r"\b(?:main|high|professional)\s*12\b", p) or "main 12" in p:
        return 12
    if re.search(r"\b(?:main|high)\s*10\b", p) or "main10" in p or "main 10" in p:
        return 10
    if "main 8" in p or p in {"main", "high", "baseline", "constrained baseline"}:
        return None  # not decisive
    return None

def resolve_bit_depth(stream: dict[str, Any]) -> tuple[int | None, str]:
    """Return confirmed bit depth and evidence; never assume unknown is 8-bit."""
    raw = _as_int(stream.get("bits_per_raw_sample")) or _as_int(stream.get("bits_per_component"))
    if raw and 8 <= raw <= 16:
        return raw, "raw-sample metadata"
    pix_fmt = str(stream.get("pix_fmt") or "")
    from_fmt = bit_depth_from_pix_fmt(pix_fmt)
    if from_fmt:
        return from_fmt, f"pixel format {pix_fmt}"
    profile = str(stream.get("profile") or "")
    from_prof = bit_depth_from_profile(profile)
    if from_prof:
        return from_prof, f"codec profile {profile}"
    return None, "no reliable raw-sample, pixel-format, or profile bit-depth metadata"

def dolby_vision_detail(stream: dict[str, Any]) -> str:
    """Describe a Dolby Vision stream's profile, or ``""`` if it is not reported.

    ffprobe surfaces the configuration record carried in the bitstream or the
    container, and ``dv_profile`` plus ``dv_bl_signal_compatibility_id``
    together say what the plain label cannot: which profile it is, and whether
    there is a base layer for a client that has no Dolby Vision support.
    Older ffprobe builds report Dolby Vision without the profile; that is
    simply left undescribed rather than guessed at.
    """
    for sd in _iter_side_data(stream):
        kind = str(sd.get("side_data_type") or "").lower()
        if not ("dovi" in kind or "dolby vision" in kind or "dvcc" in kind or "dvvc" in kind):
            continue
        prof = _as_int(sd.get("dv_profile"))
        compat = _as_int(sd.get("dv_bl_signal_compatibility_id"))
        if prof is None:
            return ""
        # Profile 8 with a base-layer id is what the industry calls 8.1 / 8.2 /
        # 8.4 - spell it the way anyone looking it up would search for it.
        if prof == 8 and compat in (1, 2, 4):
            parts = [f"profile 8.{compat}"]
        else:
            parts = [f"profile {prof}"]
        if compat is not None:
            parts.append(DOVI_BASE_LAYER.get(compat, f"base-layer id {compat}"))
        if _as_int(sd.get("el_present_flag")) == 1:
            parts.append("dual layer (enhancement layer present)")
        return " \u00b7 ".join(parts)
    return ""


def bit_depth_conflict(stream: dict[str, Any]) -> tuple[int, int] | None:
    """Return ``(raw-sample bits, pixel-format bits)`` when the two disagree.

    Two independent fields claim a bit depth and they can contradict each
    other - a 10-bit pixel format behind an 8-bit raw-sample count, most
    often. Neither wins here: the caller turns a disagreement into a review,
    because acting on half a label means deciding whether to re-encode.
    """
    raw = _as_int(stream.get("bits_per_raw_sample")) or _as_int(stream.get("bits_per_component"))
    if not raw or not (8 <= raw <= 16):
        return None
    from_fmt = bit_depth_from_pix_fmt(str(stream.get("pix_fmt") or ""))
    if from_fmt is None or from_fmt == raw:
        return None
    return raw, from_fmt


def _iter_side_data(stream: dict[str, Any]) -> list[dict[str, Any]]:
    raw = stream.get("side_data_list") or []
    return [sd for sd in raw if isinstance(sd, dict)]

def _tag_blob(stream: dict[str, Any], fmt: dict[str, Any] | None) -> str:
    parts: list[str] = []
    fmt_tags = fmt.get("tags") if fmt is not None else None
    for src in (stream.get("tags"), fmt_tags):
        if not isinstance(src, dict):
            continue
        for key, val in src.items():
            parts.append(f"{key}={val}")
    return " ".join(parts).lower()

def classify_hdr(stream: dict[str, Any], fmt: dict[str, Any] | None = None) -> tuple[bool, list[str], list[str]]:
    """Return HDR status, labels, and evidence. BT.2020 primaries alone are not HDR."""
    flavors: list[str] = []
    evidence: list[str] = []
    transfer = str(stream.get("color_transfer") or "").lower()
    tags = _tag_blob(stream, fmt)

    for sd in _iter_side_data(stream):
        kind = str(sd.get("side_data_type") or "").lower()
        if "dovi" in kind or "dolby vision" in kind or "dvcc" in kind or "dvvc" in kind:
            flavors.append("Dolby Vision")
            evidence.append(f"side data: {kind}")
        elif "2094" in kind or "hdr10+" in kind or "dynamic hdr" in kind or "hdr dynamic" in kind:
            flavors.append("HDR10+")
            evidence.append(f"side data: {kind}")
        elif "mastering display" in kind or "content light" in kind:
            flavors.append("HDR10")
            evidence.append(f"side data: {kind}")

    if "dolby vision" in tags or "dvhe" in tags or "dvav" in tags or "dvh1" in tags:
        if "Dolby Vision" not in flavors:
            flavors.append("Dolby Vision")
        evidence.append("stream/container tag: Dolby Vision signature")
    # A stream whose codec *is* a Dolby Vision sample entry is Dolby Vision
    # even when the file carries no configuration record to read.
    dv_codec = str(stream.get("codec_name") or "").lower().strip()
    if dv_codec in DV_CODEC_NAMES:
        if "Dolby Vision" not in flavors:
            flavors.append("Dolby Vision")
        evidence.append(f"codec name: {dv_codec} (Dolby Vision)")
    if "hdr10+" in tags or "hdr10plus" in tags or "smpte2094" in tags.replace(" ", ""):
        if "HDR10+" not in flavors:
            flavors.append("HDR10+")
        evidence.append("stream/container tag: HDR10+ signature")
    hdr_fmt = ""
    st_raw = stream.get("tags")
    stream_tags = st_raw if isinstance(st_raw, dict) else {}
    ft_raw = fmt.get("tags") if fmt is not None else None
    fmt_tags = ft_raw if isinstance(ft_raw, dict) else {}
    for key in ("HDR_Format", "hdr_format", "HDR_Format_String", "HDR_Format_Compatibility"):
        hdr_fmt += " " + str(stream_tags.get(key) or fmt_tags.get(key) or "")
    hf = hdr_fmt.lower()
    if "dolby" in hf and "Dolby Vision" not in flavors:
        flavors.append("Dolby Vision")
        evidence.append("HDR format tag: Dolby Vision")
    if "hdr10+" in hf and "HDR10+" not in flavors:
        flavors.append("HDR10+")
        evidence.append("HDR format tag: HDR10+")
    if "hdr10" in hf and "HDR10+" not in hf and "HDR10" not in flavors:
        flavors.append("HDR10")
        evidence.append("HDR format tag: HDR10")
    if "hlg" in hf and "HLG" not in flavors:
        flavors.append("HLG")
        evidence.append("HDR format tag: HLG")

    if transfer == "smpte2084":
        if not any(f in flavors for f in ("HDR10", "HDR10+", "Dolby Vision")):
            flavors.append("HDR10")
        evidence.append(f"transfer: {transfer}")
    elif transfer in {"arib-std-b67", "hlg"}:
        if "HLG" not in flavors:
            flavors.append("HLG")
        evidence.append(f"transfer: {transfer}")

    # De-dupe, stable order
    order = ["Dolby Vision", "HDR10+", "HDR10", "HLG"]
    seen: set[str] = set()
    unique: list[str] = []
    for name in order + [f for f in flavors if f not in order]:
        if name in flavors and name not in seen:
            seen.add(name)
            unique.append(name)
    is_hdr = bool(unique) or transfer in HDR_TRANSFERS
    return is_hdr, unique, list(dict.fromkeys(evidence))

def pick_video_stream(payload: dict[str, Any]) -> dict[str, Any] | None:
    streams = [s for s in payload.get("streams", []) if isinstance(s, dict)]
    real = [
        s for s in streams
        if str(s.get("codec_type") or "video") == "video"
        and (s.get("disposition") or {}).get("attached_pic") != 1
    ]
    if not real:
        return None
    # The container's own "this is the main feature" mark outranks picture
    # size: a scope main feature (1920x816) is shorter than a full-frame bonus
    # featurette (1920x1080), and reading the featurette would report the
    # wrong movie's bit depth and HDR. Size decides only between streams the
    # file does not distinguish.
    real.sort(
        key=lambda s: (
            int((s.get("disposition") or {}).get("default") or 0),
            (_as_int(s.get("width")) or 0) * (_as_int(s.get("height")) or 0),
            _as_int(s.get("bit_rate")) or 0,
            int((s.get("disposition") or {}).get("default") or 0),
            -(_as_int(s.get("index")) or 0),
        ),
        reverse=True,
    )
    return real[0]

def categorize(bit_depth: int | None, is_hdr: bool) -> str:
    if bit_depth is None:
        return STATUS_REVIEW_UNKNOWN_DEPTH
    if bit_depth <= 8 and is_hdr:
        return STATUS_REVIEW_8BIT_HDR
    if bit_depth <= 8:
        return STATUS_QUEUE
    if is_hdr:
        return STATUS_SKIP_HDR
    return STATUS_SKIP_SDR

def result_from_probe(
    path: str,
    payload: dict[str, Any],
    *,
    size_bytes: int = 0,
) -> ProbeResult:
    stream = pick_video_stream(payload)
    if stream is None:
        return ProbeResult(
            path=path,
            status=STATUS_ERROR,
            category=CATEGORY_LABELS[STATUS_ERROR],
            info="No main video stream (cover-art-only or empty)",
            size_bytes=size_bytes,
            error="no video stream",
        )

    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    bit_depth, bit_depth_evidence = resolve_bit_depth(stream)
    conflict = bit_depth_conflict(stream)
    is_hdr, flavors, hdr_evidence = classify_hdr(stream, fmt)
    status = categorize(bit_depth, is_hdr)
    if conflict is not None:
        # Two independent fields disagree, so the bit depth is not known -
        # only claimed twice, differently. A queue is a re-encode, so the
        # file waits for a human instead of being decided on half a label.
        raw_bits, fmt_bits = conflict
        status = STATUS_REVIEW_UNKNOWN_DEPTH
        bit_depth_evidence = (
            f"conflicting metadata: raw sample says {raw_bits}-bit but pixel "
            f"format {stream.get('pix_fmt')} says {fmt_bits}-bit - not queued"
        )

    codec = str(stream.get("codec_name") or "unknown").upper()
    profile = str(stream.get("profile") or "").strip()
    pix_fmt = str(stream.get("pix_fmt") or "unknown")
    width = _as_int(stream.get("width")) or 0
    height = _as_int(stream.get("height")) or 0
    res = f"{width}x{height}" if width and height else "unknown-res"
    duration = _as_float(stream.get("duration")) or _as_float((fmt or {}).get("duration"))
    transfer = str(stream.get("color_transfer") or "")
    primaries = str(stream.get("color_primaries") or "")
    wcg = primaries.lower() in WCG_PRIMARIES and not is_hdr

    bits_label = f"{bit_depth}-bit" if bit_depth is not None else "unknown-bit"
    if conflict is not None:
        bits_label += "?"
    # "Dolby Vision" alone does not say whether anything else can play it.
    dv_detail = dolby_vision_detail(stream)
    shown = [f"{name} ({dv_detail})" if name == "Dolby Vision" and dv_detail else name
             for name in flavors]
    hdr_label = "/".join(shown) if shown else ("HDR" if is_hdr else "SDR")
    if wcg:
        hdr_label = "SDR WCG (BT.2020 primaries, not HDR)"
    prof = f" {profile}" if profile else ""
    info = f"{res} | {codec}{prof} | {bits_label} {hdr_label} | {pix_fmt}"
    if transfer:
        info += f" | trc={transfer}"
    info += f" | depth={bit_depth_evidence}"

    return ProbeResult(
        path=path,
        status=status,
        category=CATEGORY_LABELS[status],
        info=info,
        bit_depth=bit_depth,
        bit_depth_evidence=bit_depth_evidence,
        hdr=is_hdr,
        hdr_flavors=flavors,
        hdr_evidence=hdr_evidence,
        dv_profile=dv_detail,
        codec=codec,
        pix_fmt=pix_fmt,
        width=width,
        height=height,
        size_bytes=size_bytes,
        duration_sec=duration,
    )

# =============================================================================
# FILE DISCOVERY
# =============================================================================

def is_skipped_dir(name: str) -> bool:
    return name.strip().lower() in SKIP_DIR_NAMES

def is_junk_name(name: str) -> bool:
    lower = name.lower()
    if lower.startswith("."):
        return True
    if any(lower.endswith(s) for s in (".!qb", ".parts", ".part", ".crdownload", ".tmp")):
        return True
    if re.search(r"(?i)(?:^|[._\-\s])(sample|trailer|teaser)(?:[._\-\s]|$)", Path(name).stem):
        return True
    return False

def discover_videos(root: Path, cfg: Config) -> list[Path]:
    found: list[Path] = []
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_skipped_dir(d) and not d.startswith(".")]
        for name in filenames:
            if is_junk_name(name):
                continue
            ext = Path(name).suffix.lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size < cfg.min_bytes:
                    continue
            except OSError:
                continue
            found.append(path)
    found.sort(key=lambda p: str(p).casefold())
    return found

# =============================================================================
# PROBE
# =============================================================================

def run_ffprobe(binary: str, file_path: Path, cfg: Config) -> dict[str, Any]:
    cmd = [
        binary,
        "-v", "error",
        "-hide_banner",
        "-probesize", PROBE_SIZE,
        "-analyzeduration", ANALYZE_DURATION,
        "-show_entries",
        "stream=index,codec_name,codec_type,profile,pix_fmt,bit_rate,"
        "bits_per_raw_sample,bits_per_component,width,height,"
        "color_space,color_transfer,color_primaries,color_range,"
        "duration,disposition,side_data_list:"
        "stream_tags:"
        "format=duration,size,bit_rate:"
        "format_tags",
        "-of", "json",
        str(file_path),
    ]
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=cfg.timeout,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffprobe timed out after {cfg.timeout:.0f}s") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to launch ffprobe: {exc}") from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"ffprobe failed: {err[:400]}")

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ffprobe JSON was not an object")
    return payload

def inspect_movie(
    file_path: Path,
    cfg: Config,
    cache: MediaProbeCache | None = None,
) -> ProbeResult:
    try:
        file_stat = file_path.stat()
        size = file_stat.st_size
    except OSError as exc:
        return ProbeResult(
            path=str(file_path),
            status=STATUS_ERROR,
            category=CATEGORY_LABELS[STATUS_ERROR],
            info=str(exc),
            error=str(exc),
        )
    try:
        # Only the ffprobe output is cached, never the classification below:
        # result_from_probe runs on every call so a change in the rules or in
        # the file is never masked by a stale verdict.
        payload = cache.get(file_path, size, file_stat.st_mtime_ns) if cache is not None else None
        if payload is None:
            payload = run_ffprobe(cfg.ffprobe, file_path, cfg)
            if cache is not None:
                cache.put(file_path, size, file_stat.st_mtime_ns, payload)
        return result_from_probe(str(file_path), payload, size_bytes=size)
    except Exception as exc:
        # Failures are deliberately not cached: a transient ffprobe problem
        # must not become a sticky verdict for that movie.
        return ProbeResult(
            path=str(file_path),
            status=STATUS_ERROR,
            category=CATEGORY_LABELS[STATUS_ERROR],
            info=str(exc),
            size_bytes=size,
            error=str(exc),
        )

# =============================================================================
# REPORT
# =============================================================================

def fmt_size(n: int) -> str:
    """Human file size, formatted the same way in every tool's report."""
    return format_bytes(n)

def fmt_dur(seconds: float | None) -> str:
    """Human duration, formatted the same way in every tool's report."""
    return format_duration(seconds)

@dataclass(frozen=True)
class ActionGroup:
    """One classification bucket as the report presents it.

    ``order`` is the tuple order of :data:`ACTION_GROUPS`: the work to do comes
    first, then the things a human must decide, then the categories that only
    confirm nothing should be touched.
    """

    status: str
    title: str
    scorecard_label: str
    scorecard_hint: str
    action: str

ACTION_GROUPS: tuple[ActionGroup, ...] = (
    ActionGroup(
        STATUS_QUEUE,
        "QUEUE FOR HANDBRAKE (8-BIT SDR)",
        "8-bit SDR (QUEUE)",
        "re-encode these to 10-bit",
        "Re-encode in HandBrake with H.265 (x265 / NVENC / QSV) 10-bit, or AV1 10-bit.",
    ),
    ActionGroup(
        STATUS_REVIEW_8BIT_HDR,
        "8-BIT TAGGED HDR (REVIEW)",
        "8-bit tagged HDR (REVIEW)",
        "never queue automatically",
        "Inspect manually. These carry HDR metadata on 8-bit video, so dumping them "
        "into the SDR queue would tone-map a film that was never mastered that way.",
    ),
    ActionGroup(
        STATUS_REVIEW_UNKNOWN_DEPTH,
        "UNKNOWN BIT DEPTH (REVIEW)",
        "Unknown bit depth (REVIEW)",
        "inspect the metadata",
        "Bit depth could not be established with confidence. Read the evidence line "
        "below each file before deciding anything.",
    ),
    ActionGroup(
        STATUS_ERROR,
        "UNREADABLE / ERRORS",
        "Errors",
        "ffprobe could not read them",
        "ffprobe could not read these. Corrupt, incomplete, or an unsupported container.",
    ),
    ActionGroup(
        STATUS_SKIP_HDR,
        "NATIVE HDR (KEEP - DO NOT RE-ENCODE)",
        "Native HDR (KEEP)",
        "protected from tone-mapping",
        "Keep the original. HandBrake tone-maps or strips HDR10 / HDR10+ / Dolby "
        "Vision dynamic metadata unless you know exactly what you are doing.",
    ),
    ActionGroup(
        STATUS_SKIP_SDR,
        "HIGH BIT-DEPTH SDR (SKIP - NOTHING TO DO)",
        "10/12/16-bit SDR (SKIP)",
        "already high bit depth",
        "Do nothing. Re-encoding an already high bit-depth file only loses quality.",
    ),
)

def build_report(results: Sequence[ProbeResult], cfg: Config, elapsed: float) -> str:
    """Render the inspector report: what to re-encode first, what never to touch last."""
    groups: dict[str, list[ProbeResult]] = {group.status: [] for group in ACTION_GROUPS}
    for item in results:
        groups.setdefault(item.status, []).append(item)
    for bucket in groups.values():
        bucket.sort(key=lambda r: Path(r.path).name.casefold())

    report = Report(
        "HANDBRAKE WORKFLOW ACTION REPORT",
        "Bit-depth and HDR classification for every movie \u00b7 fail-closed: anything "
        "uncertain is never queued",
    )
    report.metas([
        ("Generated", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")),
        ("Target directory", cfg.source_dir),
        ("Movies inspected", len(results)),
        ("Elapsed", f"{elapsed:.1f}s"),
        ("Report", cfg.report_file),
    ])

    rows: list[tuple[object, str, str]] = [
        (len(groups[group.status]), group.scorecard_label, group.scorecard_hint)
        for group in ACTION_GROUPS
    ]
    rows.append((len(results), "Movies inspected", "every movie-sized video found"))
    report.blank()
    report.scorecard(rows)

    queue = groups[STATUS_QUEUE]
    review = groups[STATUS_REVIEW_8BIT_HDR] + groups[STATUS_REVIEW_UNKNOWN_DEPTH]
    if queue or review:
        report.paragraph(
            f"Start here: {len(queue)} movie(s) to re-encode"
            + (f" and {len(review)} needing a human decision" if review else "")
            + " \u00b7 both groups are listed first, below."
        )
    else:
        report.paragraph(
            "Nothing to queue: no 8-bit SDR movie was found, and nothing is uncertain "
            "enough to need a decision."
        )

    for group in ACTION_GROUPS:
        items = groups.get(group.status) or []
        report.section(
            group.title,
            count=len(items),
            total=len(results),
            intro=f"Action: {group.action}",
        )
        if not items:
            report.paragraph("None found.")
            continue
        for position, item in enumerate(items, start=1):
            fields: list[tuple[str, str]] = [
                ("Path", item.path),
                ("Info", item.info),
                ("Size", f"{fmt_size(item.size_bytes)}   \u00b7   duration {fmt_dur(item.duration_sec)}"),
            ]
            if item.hdr_flavors:
                fields.append(("HDR", ", ".join(item.hdr_flavors)))
            if item.dv_profile:
                fields.append(("Dolby Vision", item.dv_profile))
            if item.hdr_evidence:
                fields.append(("HDR evidence", "; ".join(item.hdr_evidence)))
            if item.bit_depth_evidence:
                fields.append(("Depth evidence", item.bit_depth_evidence))
            if item.error:
                fields.append(("Error", item.error))
            report.entry(Path(item.path).name, ordinal=position, fields=fields)

    report.footer([
        "QUEUE = 8-bit SDR. Re-encode to H.265 10-bit (or AV1 10-bit) in HandBrake.",
        "SKIP = already 10-bit or better SDR. Re-encoding only loses quality.",
        "KEEP = HDR10 / HDR10+ / Dolby Vision / HLG. HandBrake tone-maps or strips "
        "dynamic metadata.",
        "A Dolby Vision profile ending in 'no fallback' needs a Dolby Vision "
        "client; one listing a base layer still plays as HDR10, HLG or SDR.",
        "REVIEW = HDR-tagged 8-bit, or metadata too uncertain to trust. Never queued "
        "automatically.",
        "BT.2020 primaries without PQ or HLG is wide-gamut SDR, not HDR.",
    ])
    return report.render()

def write_report(results: Sequence[ProbeResult], cfg: Config, elapsed: float) -> bool:
    """Publish the sole inspector artifact as a complete atomic text report."""
    try:
        atomic_write_text(cfg.report_file, build_report(results, cfg, elapsed))
        return True
    except OSError as exc:
        log(f"[ERROR] Cannot write report {cfg.report_file}: {exc}")
        return False

# =============================================================================
# DRIVER
# =============================================================================

def validate_config(cfg: Config) -> list[str]:
    """Return actionable safety errors before probing or writing any outputs."""
    errors: list[str] = []
    if not cfg.source_dir.is_dir():
        errors.append(f"--source is not an accessible directory: {cfg.source_dir}")
    if cfg.min_file_size_mb < 0:
        errors.append("--min-size must be zero or greater")
    if cfg.workers <= 0:
        errors.append("--workers must be greater than zero")
    if cfg.timeout <= 0:
        errors.append("--timeout must be greater than zero")
    if cfg.lock_timeout_seconds < 0:
        errors.append("--lock-timeout must be zero or greater")

    if path_is_within(cfg.report_file, cfg.source_dir):
        errors.append(f"Report path must be outside --source: {cfg.report_file}")
    if path_is_within(cfg.log_file, cfg.source_dir):
        errors.append(f"Log path must be outside --source: {cfg.log_file}")
    if cfg.use_cache and path_is_within(cfg.cache_file, cfg.source_dir):
        errors.append(f"Cache path must be outside --source: {cfg.cache_file}")
    if os.path.normcase(os.path.normpath(str(cfg.log_file))) == os.path.normcase(os.path.normpath(str(cfg.report_file))):
        errors.append("--log and --report must be different files")
    return errors

def run_lock_path(source_dir: Path) -> Path:
    key = hashlib.sha256(str(source_dir.resolve(strict=False)).encode("utf-8", errors="surrogatepass")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"{LOCK_NAME}.{key}"

def scan(cfg: Config) -> int:
    log("=" * 79)
    log("HANDBRAKE BIT-DEPTH INSPECTOR")
    log("=" * 79)
    log(f"Library   : {cfg.source_dir}")
    log(f"Workers   : {cfg.workers}")
    log(f"Min size  : {cfg.min_file_size_mb:g} MB")
    log(f"ffprobe   : {cfg.ffprobe}")
    log(f"Log       : {cfg.log_file}")
    log(f"Report    : {cfg.report_file}")
    log("")

    if not cfg.source_dir.exists():
        log(f"[ERROR] Directory does not exist: {cfg.source_dir}")
        return 2

    files = discover_videos(cfg.source_dir, cfg)
    log(f"Found {len(files)} canonical MKV movie file(s).")
    if cfg.dry_run:
        for p in files:
            log(f"  {p}")
        log("(dry-run — not probing)")
        return 0
    if not files:
        if not write_report([], cfg, 0.0):
            return 2
        log("Nothing to inspect.")
        return 0

    results: list[ProbeResult] = []
    started = time.perf_counter()
    done = 0
    total = len(files)

    def _store(res: ProbeResult) -> None:
        results.append(res)

    cache = MediaProbeCache(cfg.cache_file, tool="10bit", enabled=cfg.use_cache)
    if cfg.use_cache:
        log(f"Probe cache: {cfg.cache_file} ({len(cache)} entries loaded)")

    if files:
        workers = max(1, min(cfg.workers, len(files)))
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
                futs = {pool.submit(inspect_movie, p, cfg, cache): p for p in files}
                for fut in as_completed(futs):
                    path = futs[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:
                        res = ProbeResult(
                            path=str(path),
                            status=STATUS_ERROR,
                            category=CATEGORY_LABELS[STATUS_ERROR],
                            info=str(exc),
                            error=str(exc),
                        )
                    _store(res)
                    done += 1
                    tag = {
                        STATUS_QUEUE: "QUEUE",
                        STATUS_SKIP_SDR: "SKIP-SDR",
                        STATUS_SKIP_HDR: "KEEP-HDR",
                        STATUS_REVIEW_8BIT_HDR: "REVIEW-HDR",
                        STATUS_REVIEW_UNKNOWN_DEPTH: "REVIEW-DEPTH",
                        STATUS_ERROR: "ERROR",
                    }.get(res.status, res.status)
                    log(f"[{done}/{total}] {tag:<9} {path.name}")
        except KeyboardInterrupt:
            log("\nInterrupted — writing partial results.")

    cache.save()
    if cfg.use_cache:
        log(f"Probe cache: {cache.hits} reused, {cache.misses} probed.")

    elapsed = time.perf_counter() - started
    if not write_report(results, cfg, elapsed):
        return 2

    q = sum(1 for r in results if r.status == STATUS_QUEUE)
    sdr = sum(1 for r in results if r.status == STATUS_SKIP_SDR)
    hdr = sum(1 for r in results if r.status == STATUS_SKIP_HDR)
    rev_hdr = sum(1 for r in results if r.status == STATUS_REVIEW_8BIT_HDR)
    rev_unknown = sum(1 for r in results if r.status == STATUS_REVIEW_UNKNOWN_DEPTH)
    rev = rev_hdr + rev_unknown
    err = sum(1 for r in results if r.status == STATUS_ERROR)

    log("")
    log("=" * 79)
    log("SCAN COMPLETE")
    log("=" * 79)
    log(f"  8-bit SDR (QUEUE)         : {q}")
    log(f"  10/12/16-bit SDR (SKIP)   : {sdr}")
    log(f"  Native HDR (KEEP)         : {hdr}")
    log(f"  8-bit tagged HDR (REVIEW) : {rev_hdr}")
    log(f"  Unknown bit depth (REVIEW): {rev_unknown}")
    log(f"  Total review required     : {rev}")
    log(f"  Errors                    : {err}")
    log("=" * 79)
    log(f"Report : {cfg.report_file}")
    if cfg.fail_if_error and err:
        return 5
    if cfg.fail_if_review and rev:
        return 4
    if cfg.fail_if_queue and q:
        return 3
    return 0

# =============================================================================
# CLI
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Categorize a movie library for HandBrake 10-bit re-encodes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--source", type=Path, default=Path(SOURCE_DIR), help="Canonical Jellyfin movie-library root")
    p.add_argument("--log", type=Path, default=Path(LOG_FILE), help="Append-only execution log outside the media library")
    p.add_argument("--report", type=Path, default=Path(REPORT_FILE), help="The sole replaceable plain-text output report")
    p.add_argument("--min-size", type=float, default=MIN_FILE_SIZE_MB, metavar="MB")
    p.add_argument("--workers", type=int, default=MAX_CPU_WORKERS)
    p.add_argument("--timeout", type=float, default=PROBE_TIMEOUT_SEC, help="ffprobe timeout seconds")
    p.add_argument("--lock-timeout", type=float, default=60.0, metavar="SECONDS", help="Maximum wait for another inspector run")
    p.add_argument("--ffprobe", default="ffprobe", help="ffprobe binary")
    p.add_argument("--cache", type=Path, default=Path(CACHE_FILE), metavar="PATH",
                   help="Reusable ffprobe output for unchanged files, outside the media library")
    p.add_argument("--no-cache", dest="use_cache", action="store_false",
                   help="Probe every movie again and do not read or write the cache")
    p.set_defaults(use_cache=True)
    p.add_argument("--fail-if-queue", action="store_true", help="Exit non-zero if known 8-bit SDR movies are queued")
    p.add_argument("--fail-if-review", action="store_true", help="Exit non-zero if a movie requires metadata review")
    p.add_argument("--fail-if-error", action="store_true", help="Exit non-zero if FFprobe cannot inspect a movie")
    p.add_argument("--dry-run", action="store_true", help="List files that would be probed")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--self-test", action="store_true")
    return p

def cfg_from_args(args: argparse.Namespace) -> Config:
    workers = args.workers if args.workers > 0 else (os.cpu_count() or 4)
    return Config(
        source_dir=args.source,
        log_file=args.log,
        report_file=args.report,
        min_file_size_mb=args.min_size,
        workers=workers,
        timeout=args.timeout,
        lock_timeout_seconds=args.lock_timeout,
        fail_if_queue=bool(args.fail_if_queue),
        fail_if_review=bool(args.fail_if_review),
        fail_if_error=bool(args.fail_if_error),
        dry_run=bool(args.dry_run),
        verbose=bool(args.verbose),
        ffprobe=args.ffprobe,
        cache_file=args.cache,
        use_cache=bool(args.use_cache),
    )

# =============================================================================
# SELF-TEST
# =============================================================================

def _assert(cond: bool, msg: str, errors: list[str]) -> None:
    if not cond:
        errors.append(msg)

def run_self_tests() -> int:
    errors: list[str] = []

    def probe(name: str, stream: dict[str, Any], fmt: dict[str, Any] | None = None) -> ProbeResult:
        payload: dict[str, Any] = {"streams": [stream]}
        if fmt is not None:
            payload["format"] = fmt
        return result_from_probe(name, payload, size_bytes=1_000_000)

    sdr8 = probe("a.mkv", {
        "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920, "height": 1080,
        "color_transfer": "bt709", "color_primaries": "bt709",
        "disposition": {"attached_pic": 0},
    })
    _assert(sdr8.status == STATUS_QUEUE, f"8-bit SDR should queue, got {sdr8.status}", errors)
    _assert(sdr8.bit_depth == 8, f"8-bit depth, got {sdr8.bit_depth}", errors)
    _assert(not sdr8.hdr, "8-bit SDR must not be HDR", errors)

    sdr10 = probe("b.mkv", {
        "codec_name": "hevc", "profile": "Main 10", "pix_fmt": "yuv420p10le",
        "width": 1920, "height": 1080, "color_transfer": "bt709",
        "disposition": {"attached_pic": 0},
    })
    _assert(sdr10.status == STATUS_SKIP_SDR, f"10-bit SDR should skip, got {sdr10.status}", errors)
    _assert(sdr10.bit_depth == 10, f"10-bit, got {sdr10.bit_depth}", errors)

    hdr10 = probe("c.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p10le", "bits_per_raw_sample": "10",
        "width": 3840, "height": 2160,
        "color_transfer": "smpte2084", "color_primaries": "bt2020",
        "side_data_list": [
            {"side_data_type": "Mastering display metadata"},
            {"side_data_type": "Content light level metadata"},
        ],
        "disposition": {"attached_pic": 0},
    })
    _assert(hdr10.status == STATUS_SKIP_HDR, f"HDR10 should keep, got {hdr10.status}", errors)
    _assert("HDR10" in hdr10.hdr_flavors, f"flavors {hdr10.hdr_flavors}", errors)

    dv = probe("d.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p10le",
        "width": 3840, "height": 2160,
        "side_data_list": [{"side_data_type": "DOVI configuration record"}],
        "disposition": {"attached_pic": 0},
    })
    _assert(dv.status == STATUS_SKIP_HDR, f"DV should keep, got {dv.status}", errors)
    _assert("Dolby Vision" in dv.hdr_flavors, f"DV flavors {dv.hdr_flavors}", errors)

    hlg = probe("e.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p10le",
        "color_transfer": "arib-std-b67", "color_primaries": "bt2020",
        "disposition": {"attached_pic": 0},
    })
    _assert(hlg.status == STATUS_SKIP_HDR and "HLG" in hlg.hdr_flavors, f"HLG {hlg}", errors)

    plus = probe("f.mkv", {
        "codec_name": "hevc", "pix_fmt": "p010le",
        "color_transfer": "smpte2084",
        "side_data_list": [{"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}],
        "disposition": {"attached_pic": 0},
    })
    _assert("HDR10+" in plus.hdr_flavors, f"HDR10+ flavors {plus.hdr_flavors}", errors)
    _assert(plus.status == STATUS_SKIP_HDR, "HDR10+ keep", errors)

    # The original script's worst bug: BT.2020 primaries ≠ HDR
    wcg = probe("g.mkv", {
        "codec_name": "h264", "pix_fmt": "yuv420p",
        "color_transfer": "bt709", "color_primaries": "bt2020",
        "disposition": {"attached_pic": 0},
    })
    _assert(wcg.status == STATUS_QUEUE, f"WCG SDR 8-bit should QUEUE, got {wcg.status}", errors)
    _assert(not wcg.hdr, "BT.2020 + bt709 is not HDR", errors)
    _assert("WCG" in wcg.info, f"should mention WCG: {wcg.info}", errors)

    # The original's other bug: 8-bit + PQ dumped into the SDR HandBrake queue
    bad = probe("h.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p",
        "color_transfer": "smpte2084", "color_primaries": "bt2020",
        "disposition": {"attached_pic": 0},
    })
    _assert(bad.status == STATUS_REVIEW_8BIT_HDR, f"8-bit HDR must REVIEW, got {bad.status}", errors)

    # Cover art must not win
    mixed = result_from_probe("i.mkv", {"streams": [
        {"codec_name": "mjpeg", "pix_fmt": "yuvj420p", "width": 600, "height": 900,
         "disposition": {"attached_pic": 1}},
        {"codec_name": "hevc", "pix_fmt": "yuv420p10le", "width": 1920, "height": 800,
         "color_transfer": "bt709", "disposition": {"attached_pic": 0}},
    ]})
    _assert(mixed.bit_depth == 10 and mixed.status == STATUS_SKIP_SDR, f"cover-art mix {mixed}", errors)

    # bits_per_raw_sample wins over a misleading 8-bit-looking default
    raw = probe("j.mkv", {
        "codec_name": "ffv1", "pix_fmt": "something_custom",
        "bits_per_raw_sample": "12",
        "disposition": {"attached_pic": 0},
    })
    _assert(raw.bit_depth == 12 and raw.status == STATUS_SKIP_SDR, f"12-bit raw {raw}", errors)

    unknown = probe("k.mkv", {
        "codec_name": "unknown", "pix_fmt": "custom_layout", "profile": "",
        "width": 1920, "height": 1080, "disposition": {"attached_pic": 0},
    })
    _assert(unknown.status == STATUS_REVIEW_UNKNOWN_DEPTH, f"unknown depth must review, got {unknown.status}", errors)
    _assert(unknown.bit_depth is None, f"unknown depth should remain None, got {unknown.bit_depth}", errors)

    multi_feature = result_from_probe("l.mkv", {"streams": [
        {"index": 0, "codec_type": "video", "pix_fmt": "yuv420p10le", "width": 64, "height": 64, "disposition": {"attached_pic": 0}},
        {"index": 1, "codec_type": "video", "pix_fmt": "yuv420p", "width": 1920, "height": 1080, "disposition": {"attached_pic": 0}},
        {"index": 2, "codec_type": "video", "pix_fmt": "yuv420p", "width": 4000, "height": 4000, "disposition": {"attached_pic": 1}},
    ]})
    _assert(multi_feature.status == STATUS_QUEUE and multi_feature.width == 1920, f"main feature selection {multi_feature}", errors)

    _assert(bit_depth_from_pix_fmt("yuv420p") == 8, "yuv420p", errors)
    _assert(bit_depth_from_pix_fmt("yuv420p10le") == 10, "p10le", errors)
    _assert(bit_depth_from_pix_fmt("p010le") == 10, "p010", errors)
    _assert(bit_depth_from_pix_fmt("yuv420p12le") == 12, "p12", errors)

    # Discovery skips extras / samples
    tmp = Path(tempfile.mkdtemp(prefix="hb_ins_"))
    try:
        movie = tmp / "Movie (1999)"
        extra = movie / "Featurettes"
        extra.mkdir(parents=True)
        (movie / "Movie (1999).mkv").write_bytes(b"x" * (120 * 1024 * 1024))
        (extra / "Making-Of.mkv").write_bytes(b"y" * (120 * 1024 * 1024))
        (movie / "Movie (1999)-sample.mkv").write_bytes(b"z" * (120 * 1024 * 1024))
        cfg = Config(source_dir=tmp, min_file_size_mb=100)
        found = discover_videos(tmp, cfg)
        names = {p.name for p in found}
        _assert(names == {"Movie (1999).mkv"}, f"canonical discovery {names}", errors)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Report includes errors (original dropped them)
    fake_cfg = Config(source_dir=Path("X:\\lib"))
    text = build_report([
        ProbeResult("a.mkv", STATUS_QUEUE, CATEGORY_LABELS[STATUS_QUEUE], "info", size_bytes=1),
        ProbeResult("e.mkv", STATUS_ERROR, CATEGORY_LABELS[STATUS_ERROR], "boom", error="boom"),
    ], fake_cfg, 1.0)
    _assert("UNREADABLE" in text and "boom" in text, "report must include errors", errors)
    _assert("QUEUE FOR HANDBRAKE" in text, "report queue heading", errors)

    report_dir = Path(tempfile.mkdtemp(prefix="hb_report_"))
    try:
        report_cfg = Config(source_dir=Path("X:\\lib"), report_file=report_dir / "report.txt")
        _assert(write_report([sdr8], report_cfg, 0.1), "atomic report write", errors)
        _assert(report_cfg.report_file.is_file(), "single report exists", errors)
        # The default cache lives in the tool's own output dir, so the report
        # directory still holds exactly one artifact.
        _assert(not list(report_dir.glob("*.json")), "no JSON side output", errors)
        _assert(not list(report_dir.glob("*.tmp")), "no staged report remains", errors)

        # Probe cache: reused only while size and mtime agree, and a cache that
        # cannot be read is a miss rather than an error.
        cache_path = report_dir / "probe_cache.json"
        cache = MediaProbeCache(cache_path, tool="10bit")
        _assert(cache.get("movie.mkv", 100, 5) is None, "cold cache is a miss", errors)
        cache.put("movie.mkv", 100, 5, {"streams": []})
        _assert(cache.get("movie.mkv", 100, 5) == {"streams": []}, "warm cache is a hit", errors)
        _assert(cache.get("movie.mkv", 101, 5) is None, "size change invalidates", errors)
        _assert(cache.get("movie.mkv", 100, 6) is None, "mtime change invalidates", errors)
        cache.save()
        reloaded = MediaProbeCache(cache_path, tool="10bit")
        _assert(reloaded.get("movie.mkv", 100, 5) == {"streams": []}, "cache survives a reload", errors)
        _assert(MediaProbeCache(cache_path, tool="other").get("movie.mkv", 100, 5) is None,
                "a different tool's cache is not reused", errors)
        cache_path.write_text("{not json", encoding="utf-8")
        _assert(MediaProbeCache(cache_path, tool="10bit").get("movie.mkv", 100, 5) is None,
                "corrupt cache degrades to a miss", errors)
        disabled = MediaProbeCache(report_dir / "unused.json", tool="10bit", enabled=False)
        disabled.put("movie.mkv", 1, 1, {"streams": []})
        _assert(disabled.get("movie.mkv", 1, 1) is None, "--no-cache stores nothing", errors)
        _assert(not (report_dir / "unused.json").exists(), "--no-cache writes no file", errors)
        cache_path.unlink()
    finally:
        shutil.rmtree(report_dir, ignore_errors=True)

    if errors:
        print("SELF-TEST FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print("SELF-TEST PASSED (fail-closed classification + HDR rules + discovery + single report)")
    return 0

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_tests()
    try:
        enable_utf8_stdio()
        cfg = cfg_from_args(args)
        errors = validate_config(cfg)
        if errors:
            for error in errors:
                log(f"Configuration error: {error}", level="CRITICAL", log_file=None)
            return 2
        global _ACTIVE_LOG_FILE
        _ACTIVE_LOG_FILE = cfg.log_file
        log(f"Starting read-only 10-bit inspection; source={cfg.source_dir}")
        binary = find_ffprobe(args.ffprobe)
        if not binary or not ffprobe_works(binary):
            log("[CRITICAL] ffprobe not found or not runnable.")
            log("Install FFmpeg and add it to PATH, or pass --ffprobe C:\\path\\to\\ffprobe.exe")
            return 2
        cfg.ffprobe = binary
        global CFG
        CFG = cfg
        try:
            with ExclusiveRunLock(run_lock_path(cfg.source_dir), cfg.lock_timeout_seconds):
                return scan(cfg)
        except LockUnavailable as exc:
            log(f"[CRITICAL] Inspector lock unavailable: {exc}")
            return 3
    except KeyboardInterrupt:
        log("Interrupted")
        return 130
    except Exception:
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
