#!/usr/bin/env python3
"""
Movie Filename Standardizer for qBittorrent
===========================================
Organizes movie downloads into the exact canonical layout
``Title (Year)/Title (Year).mkv`` with English subtitle sidecars only.

Zero third-party Python dependencies. Initial placement needs no external
binary; replacing an existing canonical movie uses optional ``ffprobe`` and
otherwise keeps the existing copy. Safe by default: never deletes a unique
source file and never follows a failed hard-link with a silent overwrite.

Movies-first: this tool is for movie libraries. TV-name detection exists
purely to *exclude* TV content; ``--allow-tv`` is an escape hatch for
movies that merely look like TV shows.

qBittorrent → Options → Downloads → Run external program on torrent completion::

    python "C:\\path\\to\\movie_standardizer.py" "%F"

Also accepts the older ``"%D" "%N"`` (save-path + torrent-name) form.
Run with no arguments to batch-scan SOURCE_DIR. Use ``--dry-run`` first.

v2.1 safety/correctness pass
----------------------------
- Destinations are replaced atomically (temp sibling + os.replace): a failed
  hardlink can no longer delete an existing target file.
- Dedup never deletes a video-less duplicate folder that still holds files.
- Generic save-path folders ("downloads", "completed", ...) no longer rename
  the movie inside them; movie-style folders never erase TV episode names.
- ``--allow-tv`` now also works for TV-named files inside folders, and TV
  names get trailing scene tags stripped.
- ``--dry-run`` never creates the target directory.
- Roman-numeral casing no longer mangles "Mix"/"Li"-style words ("MIX").
- Subtitle suffix stripping is generated from the language/flag tables;
  output stays `Title.eng.forced.srt` order (Plex & Jellyfin compatible).
- Added ``--version``; dead code removed; stricter lint-clean.

v2.7 hardlink-only canonical output
-----------------------------------
- HARDLINK is the sole placement method. The program exposes no copy, move,
  symlink, or cross-device fallback path, so completed torrents remain safe to
  seed without temporarily duplicating movie data.
- The source and target must be distinct directories on the same filesystem.

v2.6 canonical movie-and-English-subtitle output
--------------------------------------------------
- The default and documented contract is exactly one canonical MKV per movie:
  ``Title (Year)/Title (Year).mkv``.
- Only recognized English subtitle sidecars are placed beside that MKV. Artwork,
  extras, provider IDs, edition/version labels, disc trees, multipart stacks,
  cleanup, and deduplication are not emitted by the default workflow.
- Non-MKV and multipart/disc releases are skipped rather than converted or
  misrepresented as a complete canonical movie.

v2.4 safety, auditability, and performance
--------------------------------------------
- Maintenance candidates now default to REPORT, never deletion; optional
  QUARANTINE and explicit DELETE modes provide controlled escalation.
- File replacement no longer deletes a read-only destination to retry; unique
  staging paths, verified copies, and safe cross-device moves preserve data.
- Locking is fail-closed and serializes all modes without creating target dirs.
- Preflight rejects recursive source/target layouts; optional JSON manifests
  report material outcomes and failures to qBittorrent via exit status.
- Bounded parse/subtitle caches accelerate repeated release-name work.

v2.3 (one-movie-file-per-folder guarantee)
------------------------------------------
- Removed the opt-in --keep-versions multi-encode mode entirely: the library
  invariant is exactly ONE movie file per movie folder, and nothing can turn
  that off. Two encodes of the same movie -> the larger is organized, the
  smaller stays untouched in its source folder; duplicate *folders* of the
  same movie are identified by the dedup scan (near-identical sizes are kept
  and logged, never guessed; maintenance defaults to REPORT).
- The only physical exceptions, both intentional: split releases are kept as
  "... - cd1"/" - cd2" parts of the same movie, and full-disc backups keep
  their BDMV / VIDEO_TS folder trees.
- Movies-first by design; TV logic exists purely to exclude TV content
  (--allow-tv stays as an escape hatch for misdetected movies).
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from stat import S_ISREG
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

STANDARDIZER_LOCK_NAME = ".movie_standardizer.lock"

# ---------------------------------------------------------------------------
# External English SRT sidecar contract
# ---------------------------------------------------------------------------
# Every tool in the pipeline that reasons about an external subtitle agrees on
# the same conservative contract: a plain-text file beside the movie, small,
# non-empty, and carrying at least one well-formed cue.  The content verdict
# lives here so a new tool cannot quietly disagree with the others about
# whether a sidecar is usable.
#
# The cue pattern is the tolerant form: leading whitespace before the cue
# number is accepted, because some muxers and editors indent it.  This is a
# "does it look like a subtitle at all" test, not a full SRT parser.
#
# Canonical language tag is ISO 639-2/B ``eng`` (``.eng.srt``).  The older
# ISO 639-1 ``.en.srt`` form is recognized only as a legacy rename source so a
# library cut over from the previous convention is not stuck in review.

EXTERNAL_SRT_MAX_BYTES = 4 * 1024 * 1024

EXTERNAL_SRT_CUE_RE = re.compile(
    r"(?m)^\s*\d+\s*\n\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}"
)

EXTERNAL_SRT_LANG = "eng"

EXTERNAL_SRT_SUFFIX = f".{EXTERNAL_SRT_LANG}.srt"  # ".eng.srt"

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

class LockTimeoutError(TimeoutError):
    """Raised when a ``CoordinationLock`` cannot be acquired in time.

    Subclasses :class:`TimeoutError` so callers that historically caught the
    built-in ``TimeoutError`` (e.g. the mkv track cleaner) keep working.
    """

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

class CoordinationLock:
    """Advisory, cross-platform, fail-closed lock shared across the tools.

    This is the single implementation of the lock protocol used by
    ``movie_standardizer.py``, ``mkv_track_cleaner.py`` and
    ``subtitle_fetcher.py``.  Because all three hash the *same normalized
    target path* with the *same lock file name* in the system temp directory,
    they all contend on the identical file — which is exactly what prevents a
    qBittorrent completion hook from placing or replacing canonical hardlinks
    while another tool scans or remuxes them.

    Usable as a context manager::

        with CoordinationLock(library, timeout_seconds=60.0):
            ...

    or with explicit acquire/release::

        lock = CoordinationLock(target, timeout_seconds=60.0)
        lock.acquire()
        try:
            ...
        finally:
            lock.release()
    """

    def __init__(self, target: Path | str, *, timeout_seconds: float = 60.0) -> None:
        normalized = os.path.normcase(os.path.normpath(str(target)))
        key = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()[:20]
        self.path = Path(tempfile.gettempdir()) / f"{STANDARDIZER_LOCK_NAME}.{key}"
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self._fh: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        self._fh = handle
        # Windows msvcrt locks byte ranges; materialize the first byte once.
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while not try_file_lock(handle, strict_non_contention=True):
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"Timed out after {self.timeout_seconds:.1f}s waiting for "
                        f"library coordination lock: {self.path}"
                    )
                time.sleep(0.1)
        except BaseException:
            handle.close()
            self._fh = None
            raise

    def release(self) -> None:
        handle = self._fh
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._fh = None

    def __enter__(self) -> CoordinationLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

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

def path_norm(path: Path | str) -> str:
    """Normalize a path the same way every tool compares them.

    ``normcase`` lower-cases on Windows and is a no-op on POSIX; ``normpath``
    collapses ``..`` and duplicate separators.  Matching this exactly is what
    lets the standardizer, cleaner and subtitle fetcher agree on a lock key and
    on whether two paths are the same file.
    """
    return os.path.normcase(os.path.normpath(str(path)))

def paths_equal(a: Path | str, b: Path | str) -> bool:
    """True when two paths refer to the same file.

    If both paths already exist, ``samefile`` resolves hard links and symlinks
    (the most accurate answer); otherwise it falls back to the normalized-text
    comparison used for paths that may not have been created yet.
    """
    pa = Path(a) if isinstance(a, str) else a
    pb = Path(b) if isinstance(b, str) else b
    try:
        if pa.exists() and pb.exists():
            return pa.samefile(pb)
    except OSError:
        pass
    return path_norm(pa) == path_norm(pb)

# ---------------------------------------------------------------------------
# Plain-text report renderer
# ---------------------------------------------------------------------------
# Every tool publishes exactly one replaceable plain-text report, and each one
# used to hand-roll its own separators, label padding and section banners.  The
# result was six reports that looked nothing alike, where the one thing the
# reader came for - "what needs my attention?" - was buried under a wall of
# undifferentiated lines.
#
# ``Report`` is now the single source of that layout: a boxed header with
# aligned metadata, a right-aligned scorecard, and titled sections whose
# entries share one hanging indent.  It stays plain text on purpose: reports
# are read in a terminal, in a text editor, and pasted into bug reports.
#
# Every glyph used here is single-width and present in both UTF-8 and the
# legacy Windows console code pages (cp437/cp850), so a report never turns
# into question marks on an old console.

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

# =====================================================================
# CONFIGURATION  (CLI flags and supported environment variables override these)
# =====================================================================

# HARDLINK is intentionally the sole placement method. It requires the
# source and target to share one filesystem (on Windows: the same NTFS volume).
PROCESS_MODE = "HARDLINK"

TARGET_DIR = r"E:\torrents\final_organized"
SOURCE_DIR = r"E:\torrents\final"
CREATE_SUBFOLDERS = True
SKIP_TV_SHOWS = True
MIN_MOVIE_SIZE_MB = 300

# The requested canonical output is an MKV. This script never transcodes;
# non-MKV sources are skipped instead of being renamed with a false extension.
CANONICAL_VIDEO_EXTENSION = ".mkv"
VIDEO_EXTENSIONS = {CANONICAL_VIDEO_EXTENSION}
SUBTITLE_EXTENSIONS = {
    ".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sup", ".smi",
}
ARTWORK_NAMES = {
    "poster", "fanart", "folder", "backdrop", "banner", "cover",
    "logo", "clearlogo", "clearart", "discart", "thumb", "landscape",
    "default", "movie", "background", "art",
}
ARTWORK_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Logs and reports live under tools\ReportsAndLogs so the root of E:\torrents
# stays media-only.
LOG_FILE = r"E:\torrents\tools\ReportsAndLogs\movie_standardizer\movie_standardizer.log"
REPORT_FILE = r"E:\torrents\tools\ReportsAndLogs\movie_standardizer\movie_standardizer_report.txt"
# The external-SRT size limit and cue pattern are vendored into this script
# (see the shared helpers section below) so this tool cannot drift from the
# others on what counts as a usable subtitle.

# Canonical-library contract: output no artwork, extras, cleanup artifacts,
# or duplicate-management actions—only one MKV and English subtitles.
COPY_EXTRAS = False
COPY_ARTWORK = False
RUN_CLEANUP_ON_TARGET = False
ENABLE_DEDUPLICATION = False
# REPORT is non-destructive. QUARANTINE moves candidates outside the library.
# DELETE removes candidates only when explicitly selected from the CLI or env.
MAINTENANCE_MODE = "REPORT"
# If two "duplicates" are within this % of each other by size, leave both
# (likely different encodes / unmarked cuts — do not guess).
DEDUP_SIZE_MARGIN_PCT = 15.0
# Canonical names omit edition/version markers and provider IDs.
INCLUDE_EDITION_TAG = False
JELLYFIN_MODE = False
# Cross-device placement is rejected: copying would defeat the seeding and
# no-temporary-duplication contract.

# =====================================================================
# CONSTANTS
# =====================================================================

MIN_YEAR = 1880
LOCK_NAME = ".movie_standardizer.lock"

# Replacing an existing canonical movie is deliberately much stricter than
# initial placement. Title/year comes from the canonical destination, while a
# close runtime match establishes that the files are the same cut. A weighted
# ffprobe score must then show a meaningful technical upgrade; file size alone
# is never sufficient.
DUPLICATE_DURATION_MAX_SECONDS = 30.0
DUPLICATE_DURATION_MAX_FRACTION = 0.01
DUPLICATE_MIN_SCORE_GAIN = 10.0
FFPROBE_TIMEOUT_SECONDS = 30.0

# Plex / Jellyfin extra directory names (compared case-insensitively).
EXTRA_FOLDER_NAMES = frozenset({
    "featurettes", "extras", "specials", "shorts", "bonus",
    "behind the scenes", "deleted scenes", "interviews", "scenes",
    "trailers", "other", "samples", "sample", "clips", "backdrops",
    "theme-music", "theme music", "sub", "subs", "subtitle", "subtitles",
    "proof", "screens", "screenshots",
})

# Subtitle-only folders are NOT extras in the "delete me" sense when we
# are hunting for .srt files — they are searched on purpose. The set
# above is used for extra-*video* detection; subtitle scan has its own skip.
SUBTITLE_FOLDER_NAMES = frozenset({
    "sub", "subs", "subtitle", "subtitles",
})

# Directories that mean "this is one disc movie, do not unpack".
DISC_FOLDER_NAMES = frozenset({"bdmv", "video_ts", "audio_ts", "certificate", "hvdvd_ts"})

GENERIC_STEMS = frozenset({
    "movie", "video", "film", "title", "videots", "stream",
    "feature", "main", "mainmovie", "bdmv", "index",
    "video_ts", "vts_01_1", "vts_01_0",
})

SKIP_NAME_SUFFIXES = (".!qb", ".parts", ".part", ".crdownload", ".tmp", ".temp")
SKIP_NAME_EXACT = frozenset({
    "thumbs.db", "desktop.ini", ".ds_store", ".localized",
    "rarbg.txt", "rarbg.com.txt", "new text document.txt",
})

# Extensions we know how to rename (used to split a stem from its suffix).
_KNOWN_EXTS = frozenset(VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS | ARTWORK_EXTENSIONS)

# Tokens stripped from titles (never kept as words).
# Intentionally omits English function words (it, in, on, to, no, or, am)
# and short ambiguous codes (dc, ts as a title token is handled in context).
_TAG_TOKENS = frozenset({
    # resolution / container
    "240p", "360p", "480p", "576p", "720p", "900p", "1080p", "1080i",
    "1440p", "2160p", "4320p", "4k", "5k", "8k", "uhd", "qhd", "fhd",
    "hd", "sd", "ntsc", "pal",
    # source
    "bluray", "blu-ray", "blu_ray", "blurayrip", "bdrip", "brrip", "bdmv",
    "bdremux", "brremux", "remux", "webdl", "web-dl", "web_dl", "webrip",
    "web-rip", "web", "webmux", "hdtv", "pdtv", "dsr", "dsrip", "dvdrip",
    "dvdscr", "dvd", "dvdr", "hddvd", "hdrip", "dlrip", "cam", "hdcam",
    "camrip", "telesync", "telecine", "screener", "scr", "r5", "ppvrip",
    "ppv", "vcd", "vhsrip", "vhs", "workprint", "wp", "bd5", "bd9", "bdr",
    "webcap", "amzn", "amazon", "nf", "netflix", "dsnp", "dsny", "disney",
    "atvp", "hmax", "hulu", "hbo", "itunes", "pcok", "paramount", "crav",
    "max", "ip", "atv", "pmtp", "cr", "funi", "crunchyroll",
    # codec / colour
    "x264", "x265", "h264", "h265", "h.264", "h.265", "avc", "hevc",
    # Jellyfin 3D flags (preserved as a version label rather than title text)
    "3d", "hsbs", "fsbs", "htab", "ftab", "mvc",
    "x266", "h266", "av1", "xvid", "divx", "vc1", "vc-1", "vp9", "vp8",
    "mpeg2", "mpeg-2", "mpeg4", "mpeg-4", "10bit", "10-bit", "8bit",
    "8-bit", "12bit", "12-bit", "hi10p", "hi10", "10bits", "8bits",
    "hdr", "hdr10", "hdr10+", "hdr10p", "hdr10plus", "dv", "dovi",
    "dolby", "vision", "dolbyvision", "hlg", "sdr", "sdr10",
    # audio
    "aac", "ac3", "eac3", "dd", "ddp", "dd+", "atmos", "truehd", "dts",
    "dtsx", "flac", "opus", "mp3", "lpcm", "pcm", "ogg", "ma",
    "2.0", "5.1", "7.1", "2ch", "6ch", "8ch", "1ch", "stereo", "mono",
    "ddp5.1", "ddp7.1", "ddp2.0", "dd5.1", "dd7.1", "dd2.0",
    "aac5.1", "aac2.0", "aac7.1", "dts-hd", "dts-hdma", "dts-x",
    "dtshd", "dtsma", "hdma", "true-hd", "true_hd", "e-ac3", "e-ac-3",
    "atmos7.1", "atmos5.1", "truehd7.1", "truehd5.1",
    "dual-audio", "dualaudio", "dual", "multi-audio", "multiaudio",
    "multich", "multi-ch",
    # language / subs (full words + 3-letter; 2-letter handled separately)
    "multi", "multi4", "multi5", "multi8", "multisub", "multi-sub",
    "multi-subs", "subs", "sub", "subbed", "dubbed", "md", "ld",
    "english", "eng", "french", "fre", "fra", "truefrench", "vff",
    "vfq", "vfi", "vostfr", "vos", "german", "ger", "deu", "spanish",
    "spa", "castellano", "latino", "italian", "ita", "japanese", "jap",
    "jpn", "korean", "kor", "chinese", "chi", "zho", "mandarin",
    "cantonese", "russian", "rus", "portuguese", "por", "brazilian",
    "hindi", "hin", "tamil", "telugu", "thai", "tha", "arabic", "ara",
    "nordic", "nordicsubs", "swedish", "swe", "norwegian", "nor",
    "danish", "dan", "finnish", "fin", "dutch", "dut", "nld", "polish",
    "pol", "turkish", "tur", "czech", "cze", "hungarian", "hun",
    "greek", "gre", "heb", "hebrew", "vietnamese", "vie", "ukrainian",
    "ukr", "romanian", "rum", "indonesian", "ind",
    "hcsub", "hc-sub", "hardsub", "softsub",
    # release flags
    "proper", "repack", "repack2", "rerip", "real", "internal",
    "limited", "retail", "festival", "complete", "nfo", "readnfo",
    "dirfix", "nfofix", "syncfix", "prooffix", "samplefix", "refixed",
    "hybrid", "regraded", "colorized", "restored", "remaster",
    "remastered", "unrated", "uncut", "extended", "theatrical",
    "criterion", "imax", "rm4k", "repack3", "proper2",
    "int", "stv", "custom", "remuxed",
    # groups / indexers commonly glued on as tokens
    "yts", "yify", "rarbg", "ettv", "ethd", "tgx", "qxr", "psa",
    "galaxyrg", "galaxytv", "tigole", "ntb", "evo", "rartv", "sparkle",
    "flux", "kogi", "cmrg", "ntg",
})

# Two-letter tokens that are language/region codes, NOT English words.
_SAFE_TWO_LETTER_TAGS = frozenset({
    "en", "es", "fr", "de", "ja", "ko", "zh", "pt", "ru", "ar", "nl",
    "sv", "tr", "vi", "pl", "cs", "da", "fi", "hu", "el", "he", "id",
    "th", "uk", "ro", "ms", "fa", "hi", "ta", "te", "ml", "bn", "pa",
    "mx", "br", "cn", "tw", "hk", "gb", "us", "au", "ca", "nz",
    "jp", "kr", "in", "se", "no", "dk", "be", "ch", "at", "ie",
})

# Two-letter English words we must NEVER treat as tags.
_TWO_LETTER_WORDS = frozenset({
    "a", "i", "am", "an", "as", "at", "be", "by", "do", "go", "he",
    "if", "in", "is", "it", "me", "my", "no", "of", "ok", "on", "or",
    "so", "to", "up", "us", "we",
})

_SEP = r"[\s._\-]+"
_EDITION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pat, re.IGNORECASE), label)
    for pat, label in (
        (rf"\bdirector'?s?{_SEP}cut\b", "Director's Cut"),
        (rf"\bdirectors{_SEP}cut\b", "Director's Cut"),
        (rf"\bfinal{_SEP}cut\b", "Final Cut"),
        (rf"\btheatrical(?:{_SEP}(?:cut|edition))?\b", "Theatrical"),
        (rf"\bextended(?:{_SEP}(?:cut|edition|version))?\b", "Extended"),
        (rf"\bunrated(?:{_SEP}(?:cut|edition))?\b", "Unrated"),
        (r"\buncensored\b", "Uncensored"),
        (rf"\buncut(?:{_SEP}(?:edition|version))?\b", "Uncut"),
        (rf"\bultimate(?:{_SEP}(?:cut|edition))?\b", "Ultimate"),
        (rf"\bspecial{_SEP}edition\b", "Special Edition"),
        (rf"\bcollector'?s?{_SEP}edition\b", "Collector's Edition"),
        (rf"\b(?:\d+(?:st|nd|rd|th){_SEP}anniversary(?:{_SEP}edition)?|anniversary{_SEP}edition)\b", "Anniversary"),
        (rf"\bcriterion(?:{_SEP}(?:edition|collection))?\b", "Criterion"),
        (rf"\bimax(?:{_SEP}(?:edition|version|cut))?\b", "IMAX"),
        (r"\bremastered\b", "Remastered"),
        (r"\brestored\b", "Restored"),
        (rf"\bfan{_SEP}edit\b", "Fan Edit"),
        (rf"\bopen{_SEP}matte\b", "Open Matte"),
        (rf"\brough{_SEP}cut\b", "Rough Cut"),
        (rf"\balternate{_SEP}(?:cut|ending|version)\b", "Alternate"),
        (r"\bredux\b", "Redux"),
    )
)

_PART_RE = re.compile(
    r"(?i)(?:^|[\s._\-\[\(])(?:cd|dvd|disc|disk|part|pt)\s*[-._]?\s*([0-9]{1,2})(?:$|[\s._\-\]\)]| )"
)

_YEAR_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s._\-\(\[\{,]))((?:18|19|20)\d{2})(?=[\s._\-\)\]\},]|$)"
)

_TV_RE = re.compile(
    r"(?ix)"
    r"(?:\bS\d{1,2}\s*E\d{1,3}\b)"          # S01E02
    r"|(?:\bS\d{1,2}\s*E\d{1,3}"
    r"\s*-\s*E?\d{1,3}\b)"                   # S01E01-E08
    r"|(?:\b\d{1,2}x\d{1,3}\b)"              # 1x02
    r"|(?:\bSeason\s*\d{1,2}\b)"
    r"|(?:\bEpisode\s*\d{1,3}\b)"
    r"|(?:\bS(?:0[1-9]|[1-3]\d|40)\b)"       # season pack S01–S40
    r"|(?:\b(?:19|20)\d{2}[.\-]\d{2}[.\-]\d{2}\b)"  # daily 2024-03-15
)

# Only a *real* site prefix: www.x.y, [x.y], or "x.y - " / "x.y:".
# Never a bare word.tld — that eats titles (Blade.Runner, Back.to.the.Future).
_WEBSITE_RE = re.compile(
    r"(?ix)^(?:"
    r"(?:\[|\()?(?:www\.)[a-z0-9][\w\-]*\.[a-z]{2,12}(?:]|\))?[\s._:\-–—]*"
    r"|"
    r"(?:\[|\()[a-z0-9][\w\-]*\.[a-z]{2,12}(?:]|\))[\s._:\-–—]*"
    r"|"
    r"(?:www\.)?[a-z0-9][\w\-]*\.[a-z]{2,12}\s*(?:-+|:)\s+"
    r")"
)

_BRACKET_BLOCK_RE = re.compile(r"(\[[^\]]*]|\{[^}]*}|\([^)]*\))")
_PROVIDER_ID_RE = re.compile(r"(?i)[\[\{(]\s*((?:imdbid-tt\d+|tmdbid-\d+|tvdbid-\d+))\s*[\]})]")
_RESOLUTION_RE = re.compile(r"(?i)(?:^|[\s._\-\[(])(4320p|2160p|1440p|1080[pi]|720p|576p|480p|360p|240p|8k|5k|4k)(?=$|[\s._\-\])])")
_3D_RE = re.compile(r"(?i)(?:^|[\s._\-\[(])3d[\s._\-]+(hsbs|fsbs|htab|ftab|mvc)(?=$|[\s._\-\])])")
_JELLYFIN_EXTRA_SUFFIX_RE = re.compile(
    r"(?i)(?:^|[\s._\-])(?:trailer|sample|scene|clip|interview|behindthescenes|deleted|deletedscene|featurette|short|other|extra)$"
)
_ARTWORK_NUMBERED_BACKDROP_RE = re.compile(r"(?i)^(?:backdrop|fanart|background|art)[-_]?\d+$")
_ROMAN_RE = re.compile(
    r"(?i)^(?=[IVXLCDM]{1,8}$)M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$"
)
# Words that are valid Roman-numeral shapes but are really English words
# ("Mix", "Jet Li"). Only upper-case input counts as a Roman numeral.
_ROMAN_AMBIGUOUS = frozenset({"mix", "div", "civ", "di", "mi", "xi", "li", "dix", "liv"})
_MINOR_WORDS = frozenset({
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "if",
    "in", "into", "nor", "of", "off", "on", "or", "per", "so", "the",
    "to", "vs", "via", "with", "yet", "v",
})
_ACRONYMS = frozenset({
    "usa", "uk", "us", "uae", "ussr", "fbi", "cia", "nsa", "nasa",
    "ufo", "ai", "ok", "nyc", "la", "ny", "dc", "r", "pg", "tv",
    "bbc", "hbo", "amc", "mtv", "wwii", "wwi", "sng", "ost",
})

_LANG_NAME_TO_ISO1 = {
    "english": "en", "eng": "en", "en": "en",
    "french": "fr", "fre": "fr", "fra": "fr", "fr": "fr",
    "spanish": "es", "spa": "es", "es": "es", "castellano": "es", "latino": "es",
    "german": "de", "ger": "de", "deu": "de", "de": "de",
    "italian": "it", "ita": "it", "it": "it",
    "japanese": "ja", "jap": "ja", "jpn": "ja", "ja": "ja",
    "korean": "ko", "kor": "ko", "ko": "ko",
    "chinese": "zh", "chi": "zh", "zho": "zh", "zh": "zh",
    "mandarin": "zh", "cantonese": "zh", "traditional": "zh", "simplified": "zh",
    "portuguese": "pt", "por": "pt", "pt": "pt", "brazilian": "pt",
    "russian": "ru", "rus": "ru", "ru": "ru",
    "arabic": "ar", "ara": "ar", "ar": "ar",
    "dutch": "nl", "dut": "nl", "nld": "nl", "nl": "nl",
    "swedish": "sv", "swe": "sv", "sv": "sv",
    "turkish": "tr", "tur": "tr", "tr": "tr",
    "vietnamese": "vi", "vie": "vi", "vi": "vi",
    "polish": "pl", "pol": "pl", "pl": "pl",
    "czech": "cs", "cze": "cs", "ces": "cs", "cs": "cs",
    "danish": "da", "dan": "da", "da": "da",
    "finnish": "fi", "fin": "fi", "fi": "fi",
    "hungarian": "hu", "hun": "hu", "hu": "hu",
    "norwegian": "no", "nor": "no", "nb": "no", "nn": "no", "no": "no",
    "greek": "el", "gre": "el", "ell": "el", "el": "el",
    "hebrew": "he", "heb": "he", "he": "he",
    "hindi": "hi", "hin": "hi", "hi": "hi",
    "thai": "th", "tha": "th", "th": "th",
    "ukrainian": "uk", "ukr": "uk", "uk": "uk",
    "romanian": "ro", "rum": "ro", "ron": "ro", "ro": "ro",
    "indonesian": "id", "ind": "id",
    "tamil": "ta", "telugu": "te", "malay": "ms", "ms": "ms",
    "persian": "fa", "farsi": "fa", "fa": "fa",
    "bulgarian": "bg", "bg": "bg",
    "croatian": "hr", "hr": "hr",
    "serbian": "sr", "sr": "sr",
    "slovak": "sk", "sk": "sk",
    "slovenian": "sl", "sl": "sl",
    "lithuanian": "lt", "lt": "lt",
    "latvian": "lv", "lv": "lv",
    "estonian": "et", "et": "et",
    "icelandic": "is", "is": "is",
    "catalan": "ca", "ca": "ca",
    "basque": "eu", "eu": "eu",
    "galician": "gl", "gl": "gl",
    "tagalog": "tl", "filipino": "tl", "tl": "tl",
    "urdu": "ur", "ur": "ur",
    "bengali": "bn", "bn": "bn",
    "canadian": "fr", "european": "es", "latin": "es",
}

_SUB_FLAGS = {
    "sdh": "sdh", "hi": "sdh", "cc": "cc",
    "forced": "forced", "foreign": "forced",
    "hearing-impaired": "sdh", "hearingimpaired": "sdh",
    "default": "default",
}

# Tokens considered "language/flag" when stripping known suffixes from
# subtitle stems. Generated from the tables above so they never drift.
_SUB_SUFFIX_TOKENS = frozenset(_LANG_NAME_TO_ISO1) | frozenset(_SUB_FLAGS)
_SUB_SUFFIX_STRIP_RE = re.compile(
    r"(?i)[\.\-_](?:" + "|".join(
        re.escape(t) for t in sorted(_SUB_SUFFIX_TOKENS, key=lambda t: (-len(t), t))
    ) + r")$"
)

_EXTRA_NAME_RE = re.compile(
    r"(?ix)(?<![a-z])(?:"
    r"featurettes?|deleted\s*scenes?|short\s*films?|"
    r"behind\s*the\s*scenes?|special\s*features?|"
    r"bonus\s*features?|making\s*of|"
    r"sample|trailer|teaser"
    r")(?![a-z])"
)

_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})

# =====================================================================
# CONFIG OBJECT / LOGGING
# =====================================================================

@dataclass
class Config:
    target_dir: Path = field(default_factory=lambda: Path(TARGET_DIR))
    source_dir: Path = field(default_factory=lambda: Path(SOURCE_DIR))
    create_subfolders: bool = CREATE_SUBFOLDERS
    skip_tv_shows: bool = SKIP_TV_SHOWS
    min_movie_size_mb: float = MIN_MOVIE_SIZE_MB
    log_file: Path | None = field(
        default_factory=lambda: Path(LOG_FILE) if LOG_FILE else None
    )
    report_file: Path | None = field(
        default_factory=lambda: Path(REPORT_FILE) if REPORT_FILE else None
    )
    copy_extras: bool = COPY_EXTRAS
    copy_artwork: bool = COPY_ARTWORK
    run_cleanup_on_target: bool = RUN_CLEANUP_ON_TARGET
    enable_deduplication: bool = ENABLE_DEDUPLICATION
    dedup_size_margin_pct: float = DEDUP_SIZE_MARGIN_PCT
    include_edition_tag: bool = INCLUDE_EDITION_TAG
    jellyfin_mode: bool = JELLYFIN_MODE
    maintenance_mode: str = MAINTENANCE_MODE
    quarantine_dir: Path | None = None
    manifest_file: Path | None = None
    dry_run: bool = False
    verbose: bool = False
    lock_timeout_seconds: float = 60.0
    ffprobe: str = "ffprobe"

    @property
    def min_movie_bytes(self) -> int:
        return int(self.min_movie_size_mb * 1024 * 1024)

CFG = Config()
LOG = logging.getLogger("movie_standardizer")

@dataclass
class RunSummary:
    """Aggregate outcomes for human and automation-friendly run reporting."""

    attempted: int = 0
    completed: int = 0
    skipped: int = 0
    reported: int = 0
    quarantined: int = 0
    deleted: int = 0
    failed: int = 0
    events: list[dict[str, str]] = field(default_factory=list)

RUN_SUMMARY = RunSummary()
# Per-run, human-readable events captured for the end-of-run text report.
# Always collected (not gated on the optional JSON manifest) so every run
# leaves a clear, self-contained report of what was done.
RUN_EVENTS: list[dict[str, str]] = []

def record_outcome(
    status: str,
    action: str,
    *,
    src: Path | None = None,
    dest: Path | None = None,
    reason: str = "",
) -> None:
    """Record a material operation for the run summary and end-of-run report."""
    attr = {
        "completed": "completed",
        "skipped": "skipped",
        "reported": "reported",
        "quarantined": "quarantined",
        "deleted": "deleted",
        "failed": "failed",
    }.get(status)
    if attr:
        setattr(RUN_SUMMARY, attr, getattr(RUN_SUMMARY, attr) + 1)
    event = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "action": action,
        "source": str(src) if src else "",
        "destination": str(dest) if dest else "",
        "reason": reason,
    }
    RUN_EVENTS.append(event)
    if CFG.manifest_file:
        RUN_SUMMARY.events.append(event)

def decline_source(item: Path, reason: str) -> None:
    """Record a source item this run deliberately left in the torrent folder.

    Skips used to be console-log-only, so ``E:\\torrents\\final`` could quietly
    accumulate non-MKV, multipart, undersized and unparseable releases with no
    durable record anywhere but the append-only log — and the report still read
    ``Skipped : 0``. Recording every decline makes that counter truthful and
    gives the report an actionable "still in source" section.
    """
    record_outcome("skipped", "left in source", src=item, reason=reason)

def write_manifest() -> None:
    """Write the optional JSON run manifest after all media actions complete."""
    if not CFG.manifest_file:
        return
    payload = {
        "version": __version__,
        "summary": {
            "attempted": RUN_SUMMARY.attempted,
            "completed": RUN_SUMMARY.completed,
            "skipped": RUN_SUMMARY.skipped,
            "reported": RUN_SUMMARY.reported,
            "quarantined": RUN_SUMMARY.quarantined,
            "deleted": RUN_SUMMARY.deleted,
            "failed": RUN_SUMMARY.failed,
        },
        "events": RUN_SUMMARY.events,
    }
    try:
        atomic_write_text(CFG.manifest_file, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        RUN_SUMMARY.failed += 1
        LOG.error("Could not write manifest %s: %s", CFG.manifest_file, exc)

def build_report() -> str:
    """Render the run report: what needs a decision first, then the full ledger.

    The one thing this report has to make unmissable is the set of items still
    sitting in the torrent folder, because nothing else will ever move them.
    """
    declined = [ev for ev in RUN_EVENTS if ev.get("action") == "left in source"]
    placed = [ev for ev in RUN_EVENTS if ev.get("status") == "completed"]
    failed = [ev for ev in RUN_EVENTS if ev.get("status") == "failed"]
    # RUN_SUMMARY.attempted counts top-level source items that reached the
    # placement stage, so it is not a denominator for anything here: every
    # section is a share of the recorded outcomes instead.
    outcomes = len(RUN_EVENTS)

    report = Report(
        "MOVIE STANDARDIZER REPORT",
        "Scene release names parsed into canonical \"Title (Year)/Title (Year).mkv\" "
        "\u00b7 hardlinked, never copied",
    )
    report.metas([
        ("Generated", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")),
        ("Mode", "DRY-RUN (no files written)" if CFG.dry_run else "LIVE (hardlinks created)"),
        ("Source (torrents)", CFG.source_dir),
        ("Target library", CFG.target_dir),
        ("Maintenance", CFG.maintenance_mode),
    ])

    rows: list[tuple[object, str, str]] = [
        (RUN_SUMMARY.completed, "Organized (placed)", "hardlinked into the library"),
        (RUN_SUMMARY.reported, "Reported (no change)", "already in place or a duplicate"),
        (RUN_SUMMARY.skipped, "Left in source", "declined; needs a decision below"),
        (RUN_SUMMARY.quarantined, "Quarantined", "moved outside the library"),
        (RUN_SUMMARY.deleted, "Deleted", "removed by the maintenance policy"),
        (RUN_SUMMARY.failed, "Failed", "an operation did not complete"),
        (outcomes, "Outcomes recorded", "every event in the ledger below"),
    ]
    report.blank()
    report.scorecard(rows)

    if declined or failed:
        report.paragraph(
            f"Start here: {len(declined)} item(s) left in the torrent folder"
            + (f" and {len(failed)} failure(s)" if failed else "")
            + " \u00b7 each is listed below with its reason."
        )
    elif outcomes:
        report.paragraph(
            f"Nothing left behind: all {outcomes} recorded outcome(s) were placements, "
            "duplicates already in the library, or deliberate reports."
        )

    if declined:
        report.section(
            "ITEMS LEFT IN SOURCE",
            count=len(declined),
            total=outcomes or None,
            intro=(
                "These stay in the torrent folder indefinitely unless acted on: lower "
                "--min-size, place the release yourself, or delete it. Nothing is silently "
                "lost and nothing here was deleted."
            ),
        )
        report.entries(
            [{"text": ev.get("source") or "(unknown)", "detail": ev.get("reason") or ""}
             for ev in declined],
        )
    if failed:
        report.section(
            "FAILED OPERATIONS",
            count=len(failed),
            total=outcomes or None,
            intro=(
                "An operation did not complete. Nothing was half-written: every placement is "
                "staged and swapped atomically."
            ),
        )
        report.entries(
            [{"text": ev.get("destination") or ev.get("source") or "(unknown)",
              "detail": ev.get("reason") or ev.get("action") or ""}
             for ev in failed],
        )
    if placed:
        report.section(
            "ORGANIZED INTO THE LIBRARY",
            count=len(placed),
            total=outcomes or None,
            intro="Hardlinks share disk sectors with the seed, so this added no duplicate bytes.",
        )
        report.entries(
            [{"text": ev.get("source") or "(unknown)",
              "detail": ev.get("destination") or ""}
             for ev in placed],
        )

    report.section(
        "EVERY OUTCOME THIS RUN",
        count=outcomes,
        intro="The complete ledger, in the order it happened.",
    )
    if not RUN_EVENTS:
        report.paragraph("No events recorded.")
    else:
        report.table(
            ["Status", "Action", "Where", "Reason"],
            [[ev.get("status", "").upper(),
              ev.get("action", ""),
              ev.get("destination") or ev.get("source") or "",
              ev.get("reason") or ""]
             for ev in RUN_EVENTS],
            aligns="<<<<",
        )

    report.footer([
        f"Log: {CFG.log_file or '(none)'}",
        f"Report: {CFG.report_file or '(none)'}",
    ])
    return report.render()

def write_report() -> None:
    """Write the human-readable run report (and echo it) for the terminal/user."""
    text = build_report()
    print_text(text)
    if CFG.report_file:
        try:
            atomic_write_text(CFG.report_file, text)
            LOG.info("Report written to %s", CFG.report_file)
        except OSError as exc:
            LOG.error("Could not write report %s: %s", CFG.report_file, exc)

def setup_logging(cfg: Config) -> None:
    for handler in LOG.handlers[:]:
        LOG.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass
    LOG.setLevel(logging.DEBUG if cfg.verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG if cfg.verbose else logging.INFO)
    LOG.addHandler(sh)
    if cfg.log_file:
        try:
            cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
            # The workflow intentionally emits one append-only log, not rotated log siblings.
            fh = logging.FileHandler(cfg.log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            fh.setLevel(logging.DEBUG)
            LOG.addHandler(fh)
        except OSError as exc:
            LOG.warning("Cannot write log file %s: %s", cfg.log_file, exc)

# =====================================================================
# PATH / FS HELPERS
# =====================================================================

def max_movie_year() -> int:
    # Deliberately naive: the user's *local* calendar year is the right
    # reference for what counts as a plausible release year.
    return datetime.now().year + 1  # noqa: DTZ005 - local year is intentional

def is_valid_year(value: int | str) -> bool:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return False
    return MIN_YEAR <= year <= max_movie_year()

def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)

def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0

def is_skipped_junk_name(name: str) -> bool:
    lower = name.lower()
    if lower in SKIP_NAME_EXACT or lower.startswith("."):
        return True
    return any(lower.endswith(suf) for suf in SKIP_NAME_SUFFIXES)

# =====================================================================
# NAME PARSING
# =====================================================================

@dataclass(frozen=True)
class ParsedName:
    title: str
    year: int | None = None
    edition: str | None = None
    resolution: str | None = None
    three_d: str | None = None
    provider_id: str | None = None
    part: str | None = None  # "cd1", "part2", ...
    is_tv: bool = False
    raw: str = ""

    @property
    def folder_name(self) -> str:
        name = self.title
        if self.year:
            name = f"{name} ({self.year})"
        if self.edition and CFG.include_edition_tag and not CFG.jellyfin_mode:
            name = f"{name} {{edition-{self.edition}}}"
        return sanitize_filename(name)

    @property
    def identity(self) -> tuple[str, int | None, str]:
        edition_identity = "" if CFG.jellyfin_mode else (self.edition or "").casefold()
        return (self.title.casefold(), self.year, edition_identity)

    @property
    def version_label(self) -> str | None:
        if not CFG.jellyfin_mode:
            return None
        labels = [label for label in (self.edition, self.three_d, self.resolution) if label]
        return " ".join(labels) or None

    def file_stem(self, part: str | None = None) -> str:
        stem = self.folder_name
        use_part = part if part is not None else self.part
        if use_part:
            return f"{stem}-{use_part}"
        return stem

def is_tv_show(name: str) -> bool:
    if not name:
        return False
    # Don't treat a lone movie-like "Se7en" / "S.W.A.T" as TV.
    return bool(_TV_RE.search(name.replace("_", " ")))

# Tags that are safe to peel even when they are common English words,
# but ONLY after a strong technical tag has already been seen.
_WEAK_TAG_TOKENS = frozenset({
    "web", "hd", "sd", "tv", "complete", "dual", "real", "limited",
    "internal", "retail", "festival", "custom", "hybrid", "vision",
    "dolby", "multi", "subs", "sub", "readnfo", "nfo", "int",
})

# Precompiled micro-regexes for _is_tag_token (hot path).
_RE_CHANNELS_TAG = re.compile(r"\d+ch")
_RE_AUDIO_JOINED_TAG = re.compile(r"(?:dd[p+]?|aac|dts|truehd|eac3|ac3|atmos)\d+(?:[.\-]\d+)?")
_RE_RARBG_PART = re.compile(r"r\d+")

def _is_tag_token(token: str, *, allow_weak: bool = True) -> bool:
    t = token.lower().strip("[](){}.-_")
    if not t:
        return True
    if not allow_weak and t in _WEAK_TAG_TOKENS:
        return False
    if t in _TAG_TOKENS:
        return True
    # Never treat a bare number as a tag — 2049, 2001, 500, 13 stay in titles.
    if _RE_CHANNELS_TAG.fullmatch(t):
        return True
    if _RE_AUDIO_JOINED_TAG.fullmatch(t):
        return True
    if len(t) == 2 and t in _SAFE_TWO_LETTER_TAGS and t not in _TWO_LETTER_WORDS:
        return True
    return bool(_RE_RARBG_PART.fullmatch(t))  # RARBG r00, r01

def _is_tag_block(block: str) -> bool:
    inner = block.strip("[](){} \t")
    if not inner:
        return True
    if re.fullmatch(r"(?:18|19|20)\d{2}", inner):
        return False  # keep (1999) — year handling is separate
    tokens = [t for t in re.split(r"[^a-z0-9.+]+", inner.lower()) if t]
    if not tokens:
        return True
    # (500), (13), [9] are titles / title prefixes, not release tags.
    if all(t.isdigit() for t in tokens):
        return False
    return all(_is_tag_token(t) for t in tokens)

def _strip_website_prefix(name: str) -> str:
    prev = None
    text = name
    while prev != text:
        prev = text
        nxt = _WEBSITE_RE.sub("", text, count=1)
        if nxt == text:
            break
        text = nxt.strip(" -_")
    return text

def _extract_part(name: str) -> tuple[str, str | None]:
    match = _PART_RE.search(name)
    if not match:
        return name, None
    # Ignore "Part IV" style roman / word parts that are the actual title
    # ("Harry Potter and the Deathly Hallows Part 2" is a distinct movie —
    # we only treat cd/dvd/disc/pt as stackable, and "part" only when the
    # rest of the filename looks like a split release).
    kind = re.search(r"(?i)(cd|dvd|disc|disk|part|pt)", match.group(0))
    token = (kind.group(1).lower() if kind else "cd")
    if token == "part":
        # Title-integral "Part 2" is extremely common. Only treat as a
        # stackable split when a disc-like sibling cue exists (cd/disc)
        # or the stem is otherwise generic.
        if not re.search(r"(?i)\b(?:cd|dvd|disc|disk)\b", name):
            return name, None
        token = "cd"
    if token == "pt":
        token = "cd"
    if token == "disk":
        token = "disc"
    part = f"{token}{int(match.group(1))}"
    cleaned = (name[: match.start()] + " " + name[match.end() :]).strip()
    return cleaned, part

def _extract_provider_id(text: str) -> tuple[str, str | None]:
    """Extract a bracketed Jellyfin-supported provider ID without title pollution."""
    match = _PROVIDER_ID_RE.search(text)
    if not match:
        return text, None
    cleaned = (text[:match.start()] + " " + text[match.end():]).strip(" -_.")
    return cleaned, match.group(1).lower()

def _extract_resolution_label(text: str) -> str | None:
    """Return the highest explicit resolution label in a scene-style name."""
    matches = [match.group(1).lower() for match in _RESOLUTION_RE.finditer(text)]
    if not matches:
        return None
    normalized = {"4k": "2160p", "5k": "2880p", "8k": "4320p"}
    return normalized.get(matches[0], matches[0])

def _extract_3d_label(text: str) -> str | None:
    """Return Jellyfin's documented 3D label form, e.g. ``3D_HSBS``."""
    match = _3D_RE.search(text)
    return f"3D_{match.group(1).upper()}" if match else None

def _extract_edition(text: str) -> tuple[str, str | None]:
    found: str | None = None
    remaining = text
    for pattern, label in _EDITION_PATTERNS:
        if pattern.search(remaining):
            found = label
            remaining = pattern.sub(" ", remaining)
            break
    remaining = re.sub(r"\s+", " ", remaining).strip(" -_.")
    return remaining, found

def _score_year_match(name: str, match: re.Match[str]) -> int:
    year = int(match.group(1))
    if not is_valid_year(year):
        return -10_000
    # 2160p / 1080i-style false positives (2160 is a "year" by digits).
    after = name[match.end() : match.end() + 4]
    if re.match(r"[pi]\b", after, re.IGNORECASE) or re.match(r"[pi][\s._\-]", after, re.IGNORECASE):
        return -10_000
    before = name[: match.start()]
    score = 0
    # Wrapped in parentheses / brackets is almost always the release year.
    left = name[max(0, match.start() - 1) : match.start()]
    right = name[match.end() : match.end() + 1]
    if left == "(" and right == ")":
        score += 12
    elif left in "[ {" and right in "] }":
        score += 8
    tail = name[match.end() :]
    if re.search(
        r"(?i)^[\s._\-]*[\(\[\{]?(?:18|19|20)\d{2}p|720p|1080p|2160p|480p|"
        r"bluray|web|webrip|web-dl|hdrip|dvdrip|hdtv|remux|x26|h26|hevc|xvid",
        tail,
    ):
        score += 9
    # Last year in the string is often the release year
    # ("Wonder Woman 1984 2020", "Blade Runner 2049 2017").
    rest_years = [
        m for m in _YEAR_RE.finditer(name[match.end() :])
        if is_valid_year(m.group(1))
        and not re.match(r"[pi]\b", name[match.end() + m.end() : match.end() + m.end() + 2], re.IGNORECASE)
    ]
    if not rest_years:
        score += 5
    # A year as the very first token is often the *title* (2012, 1917, 1984).
    prefix = re.sub(r"^[\s._\-]+", "", before)
    if not re.sub(r"[\s._\-]+", "", prefix):
        score -= 6
    # Words after the year that look like a title continuation
    # ("2001 A Space Odyssey 1968") — penalize.
    tail_words = [w for w in re.split(r"[\s._\-]+", tail) if w]
    title_like = 0
    for word in tail_words[:4]:
        core = re.sub(r"[^A-Za-z]", "", word)
        if core and not _is_tag_token(word) and not is_valid_year(word):
            title_like += 1
        else:
            break
    if title_like >= 2:
        score -= 8
    return score

def _pick_year(name: str) -> tuple[str, int | None]:
    candidates: list[tuple[int, re.Match[str]]] = []
    for match in _YEAR_RE.finditer(name):
        score = _score_year_match(name, match)
        if score > -1000:
            candidates.append((score, match))
    if not candidates:
        return name, None
    candidates.sort(key=lambda item: (item[0], item[1].start()), reverse=True)
    best = candidates[0][1]
    year = int(best.group(1))
    title = name[: best.start()]
    # Drop the opening wrapper of "(2020)" / "[2020]" that stayed in the title.
    title = re.sub(r"[\s._\-]*[\(\[\{]\s*$", "", title)
    return title, year

def _peel_hyphen_tags(token: str, *, allow_weak: bool) -> str:
    bits = token.split("-")
    while bits:
        if _is_tag_token(bits[-1], allow_weak=allow_weak):
            bits.pop()
            continue
        # x264-SPARKS / HEVC-PSA: release group glued onto a codec tag
        if (
            len(bits) >= 2
            and _is_tag_token(bits[-2], allow_weak=True)
            and re.fullmatch(r"[A-Za-z0-9]+", bits[-1] or "")
        ):
            bits.pop()
            continue
        break
    return "-".join(bits)

def _strip_trailing_tags(title: str) -> str:
    text = title.strip()
    while True:
        stripped = text.rstrip(" -_.|,;:")
        m = re.search(r"(\[[^\]]*]|\{[^}]*}|\([^)]*\))$", stripped)
        if m and _is_tag_block(m.group(1)):
            text = stripped[: m.start()]
            continue
        break
    parts = [p for p in re.split(r"[\s._]+", text.strip(" -_.")) if p]
    saw_strong = False
    while parts:
        allow_weak = saw_strong
        peeled = _peel_hyphen_tags(parts[-1], allow_weak=allow_weak)
        if peeled != parts[-1]:
            saw_strong = True
            if peeled:
                parts[-1] = peeled
                continue
            parts.pop()
            continue
        if _is_tag_token(parts[-1], allow_weak=allow_weak):
            if parts[-1].lower().strip("[](){}.-_") not in _WEAK_TAG_TOKENS:
                saw_strong = True
            parts.pop()
            continue
        break
    return " ".join(parts)

def _strip_leading_tag_blocks(title: str) -> str:
    text = title.strip()
    while True:
        m = re.match(r"^(\[[^\]]*]|\{[^}]*})[\s._\-]*", text)
        if m and _is_tag_block(m.group(1)):
            text = text[m.end() :]
            continue
        # Parentheses only if they are clearly tags, not "(500) Days of Summer"
        m = re.match(r"^(\([^)]*\))[\s._\-]*", text)
        if m and _is_tag_block(m.group(1)):
            text = text[m.end() :]
            continue
        break
    return text

def _clean_separators(title: str) -> str:
    text = title.replace("·", " ").replace("–", "-").replace("—", "-")
    # Keep Dr. / Mr. / St. so they don't become "Dr Strangelove".
    text = re.sub(r"\b(Dr|Mr|Mrs|Ms|St|Jr|Sr|Vs)\.", lambda m: m.group(1) + "\0", text, flags=re.IGNORECASE)
    text = re.sub(r"[\._]+", " ", text)
    text = text.replace("\0", ".")
    text = re.sub(r"\s+-\s+", " - ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t-_,.;")

def _is_stylized_token(word: str) -> bool:
    """True for Se7en / iPhone / McConaughey / WALL-E-style tokens we must not smash."""
    if any(c.isdigit() for c in word) and any(c.isalpha() for c in word):
        return True
    if word and word[0].islower() and any(c.isupper() for c in word[1:]):
        return True
    return bool(re.match(r"(?:Mc|Mac)[A-Z]", word))

def _title_case_word(word: str, index: int, total: int, after_break: bool) -> str:
    if not word:
        return word
    if _ROMAN_RE.match(word) and (word.isupper() or word.lower() not in _ROMAN_AMBIGUOUS):
        return word.upper()
    lower = word.lower()
    if lower in _ACRONYMS:
        return word.upper()
    if _is_stylized_token(word):
        if word[0].islower() and (index == 0 or after_break):
            return word[0].upper() + word[1:]
        return word
    if lower in _MINOR_WORDS and index not in (0, total - 1) and not after_break:
        return lower
    if "-" in word and word != "-":
        return "-".join(
            _title_case_word(part, 0, 1, True) if part else part
            for part in word.split("-")
        )
    if "'" in word:
        head, *rest = word.split("'")
        cased = head[:1].upper() + head[1:].lower() if head else ""
        return cased + "'" + "'".join(p.lower() for p in rest)
    return word[:1].upper() + word[1:].lower()

def custom_title_case(title: str) -> str:
    text = title.strip()
    if not text:
        return text
    # Keep short bracket titles like [REC] intact.
    if re.fullmatch(r"\[REC\]\d*", text, re.IGNORECASE):
        return re.sub(r"(?i)\[rec\]", "[REC]", text)
    if text.isupper() and any(c.isalpha() for c in text):
        text = text.lower()
    words = text.split(" ")
    out: list[str] = []
    after_break = True
    total = len([w for w in words if w])
    prev_numeric = False
    written = 0
    for word in words:
        if not word:
            continue
        prefix = ""
        suffix = ""
        core = word
        while core and core[0] in "[(":
            prefix += core[0]
            core = core[1:]
        while core and core[-1] in "])!,.;":
            suffix = core[-1] + suffix
            core = core[:-1]
        ended_colon = False
        if core.endswith(":"):
            core = core[:-1]
            suffix = ":" + suffix
            ended_colon = True
        force = after_break or prev_numeric
        cased = _title_case_word(core, written, total, force) if core else core
        out.append(prefix + cased + suffix)
        after_break = ended_colon or word.endswith((":", "/", "—")) or word == "-"
        prev_numeric = bool(core) and core.isdigit()
        written += 1
    return " ".join(out)

def sanitize_filename(name: str) -> str:
    """Make a cross-platform, Plex/Jellyfin-safe file/folder name."""
    text = nfc(name)
    text = text.replace(":", " -")
    text = text.replace("/", " - ").replace("\\", " - ")
    text = text.replace('"', "'")
    text = re.sub(r"[<>|?*\x00-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "Unknown"
    # Windows reserved device names
    stem = text.split(".")[0]
    if stem.lower() in _WINDOWS_RESERVED:
        text = f"_{text}"
    # Keep names reasonable (NTFS 255 bytes per component)
    if len(text.encode("utf-8")) > 200:
        encoded = text.encode("utf-8")[:200]
        text = encoded.decode("utf-8", errors="ignore").rstrip(" .")
    return text

@lru_cache(maxsize=8192)
def parse_movie_name(name: str) -> ParsedName:
    raw = name
    if is_tv_show(name):
        stem = os.path.splitext(name)[0]
        # TV names keep S01E01 etc., but trailing scene tags can still go.
        stripped = _strip_trailing_tags(_strip_leading_tag_blocks(stem))
        stem = stripped or stem
        return ParsedName(title=sanitize_filename(_clean_separators(stem)), is_tv=True, raw=raw)

    base = name
    if "." in name:
        stem, maybe_ext = os.path.splitext(name)
        if maybe_ext.lower() in _KNOWN_EXTS:
            base = stem

    cleaned = _strip_website_prefix(base)
    cleaned, provider_id = _extract_provider_id(cleaned)
    resolution = _extract_resolution_label(base)
    three_d = _extract_3d_label(base)
    cleaned = re.sub(
        r"\b(Dr|Mr|Mrs|Ms|St|Jr|Sr|Vs)\.",
        lambda m: m.group(1) + "\u2056",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned, part = _extract_part(cleaned)

    # Pull edition from the *whole* string first so "Extended.1080p" works,
    # then again on the title remainder after the year is sliced off.
    cleaned, edition_global = _extract_edition(cleaned)

    title_part, year = _pick_year(cleaned)
    if year is None:
        title_part = cleaned

    title_part, edition_title = _extract_edition(title_part)
    edition = edition_title or edition_global

    # Remove leftover tag bracket blocks and trailing scene tags
    title_part = _strip_leading_tag_blocks(title_part)
    title_part = _strip_trailing_tags(title_part)
    # Also drop leftover inner tag blocks that are not the title
    def _keep_or_drop(match: re.Match[str]) -> str:
        return "" if _is_tag_block(match.group(1)) else match.group(1)

    title_part = _BRACKET_BLOCK_RE.sub(_keep_or_drop, title_part)
    title_part = _clean_separators(title_part)
    title_part = title_part.replace("\u2056", ". ")
    # This canonical title is intentionally stylized in metadata providers; the
    # generic title-caser would turn it into "Wall E" after separator cleanup.
    if re.fullmatch(r"wall[._\-\s]?e", title_part, flags=re.IGNORECASE):
        title_part = "WALL.E"
    else:
        title_part = custom_title_case(title_part)
    title_part = sanitize_filename(title_part)

    if not title_part:
        if year:
            title_part = str(year)
            year = None
        else:
            title_part = sanitize_filename(_clean_separators(base)) or "Unknown"

    return ParsedName(
        title=title_part,
        year=year,
        edition=edition,
        resolution=resolution,
        three_d=three_d,
        provider_id=provider_id,
        part=part,
        raw=raw,
    )

# =====================================================================
# SUBTITLE LANGUAGE
# =====================================================================

@lru_cache(maxsize=4096)
def subtitle_suffix(filename: str) -> str:
    """Return ``.eng.sdh.srt``-style suffix (ISO 639-2/B + flags + original ext).

    English language tokens (``en`` / ``eng`` / ``english``) always collapse to
    the canonical library tag ``eng`` so every tool emits ``.eng.srt``.
    """
    base, ext = os.path.splitext(filename)
    tokens = [t for t in re.split(r"[\.\-\s_]+", base.lower()) if t]
    langs: list[str] = []
    flags: list[str] = []
    has_other_language = any(
        token != "hi" and token in _LANG_NAME_TO_ISO1 for token in tokens
    )
    for token in tokens:
        # Jellyfin documents a special ambiguity: a lone ``hi`` is Hindi;
        # only alongside another language does it mean hearing impaired.
        if token == "hi":
            if has_other_language:
                flags.append("sdh")
            else:
                langs.append("hi")
            continue
        if token in _SUB_FLAGS:
            flags.append(_SUB_FLAGS[token])
            continue
        iso = _LANG_NAME_TO_ISO1.get(token)
        if iso:
            # Canonical English tag is ISO 639-2/B ``eng`` (``.eng.srt``).
            langs.append(EXTERNAL_SRT_LANG if iso == "en" else iso)
    # de-dupe, preserve order
    def unique(seq: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in seq:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    bits = unique(langs) + unique(flags)
    if bits:
        return "." + ".".join(bits) + ext.lower()
    return ext.lower()

def is_english_subtitle(path: Path) -> bool:
    """Return true only when the sidecar carries an English language suffix."""
    suffix_fields = {field.casefold() for field in subtitle_suffix(path.name).split(".") if field}
    return EXTERNAL_SRT_LANG in suffix_fields or "en" in suffix_fields

def is_valid_plain_english_srt(path: Path) -> tuple[bool, str]:
    """Admit only one direct-play-safe normal English SRT into the library.

    Accepts either the canonical ``.eng.srt`` name or the legacy ``.en.srt``
    form so a release that still ships the old suffix can be hardlinked and
    then renamed to the canonical name by the fetcher/cleaner promote step.
    """
    if path.suffix.lower() != ".srt":
        return False, "not an SRT"
    try:
        st = path.stat(follow_symlinks=False)
    except OSError as exc:
        return False, f"could not stat subtitle: {exc}"
    if path.is_symlink() or not path.is_file():
        return False, "subtitle is not a regular non-symlink file"
    if st.st_size <= 0 or st.st_size > EXTERNAL_SRT_MAX_BYTES:
        return False, "subtitle size is unsafe"
    suffix_fields = [field.casefold() for field in subtitle_suffix(path.name).split(".") if field]
    # Canonical is eng.srt; legacy en.srt is still a normal English SRT that
    # the promote step will rename after placement.
    if suffix_fields not in (["eng", "srt"], ["en", "srt"]):
        return False, "subtitle is not a normal English SRT"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return False, f"could not read subtitle: {exc}"
    text = decode_srt_bytes(raw)
    if text is None or not EXTERNAL_SRT_CUE_RE.search(normalize_srt_newlines(text)):
        return False, "subtitle does not contain a valid numbered SRT cue"
    return True, "validated normal English SRT"

def _strip_known_sub_suffixes(stem: str) -> str:
    cleaned = stem
    while True:
        nxt = _SUB_SUFFIX_STRIP_RE.sub("", cleaned)
        if nxt == cleaned:
            return cleaned
        cleaned = nxt

# =====================================================================
# MEDIA CLASSIFICATION / SCAN
# =====================================================================

def is_extra_folder_name(name: str) -> bool:
    return name.strip().lower() in EXTRA_FOLDER_NAMES

def is_disc_folder_name(name: str) -> bool:
    return name.strip().lower() in DISC_FOLDER_NAMES

def path_has_disc_structure(root: Path) -> bool:
    if not root.is_dir():
        return False
    try:
        names = {p.name.lower() for p in root.iterdir()}
    except OSError:
        return False
    if names & DISC_FOLDER_NAMES:
        return True
    # One extra wrapper is common: Movie/BDMV or Movie/Something/BDMV
    try:
        for child in root.iterdir():
            if child.is_dir():
                try:
                    child_names = {p.name.lower() for p in child.iterdir()}
                except OSError:
                    continue
                if child_names & DISC_FOLDER_NAMES:
                    return True
    except OSError:
        return False
    return False

def path_inside_named(path: Path, names: frozenset[str], stop_at: Path | None = None) -> bool:
    for parent in path.parents:
        if stop_at is not None and path_norm(parent) == path_norm(stop_at):
            break
        if parent.name.lower() in names:
            return True
    return False

def is_extra_video(path: Path, *, root: Path | None = None, size: int | None = None) -> bool:
    """True if this video should not be treated as a feature film."""
    if CFG.jellyfin_mode and _JELLYFIN_EXTRA_SUFFIX_RE.search(path.stem):
        return True
    in_extra_dir = path_inside_named(path, EXTRA_FOLDER_NAMES - SUBTITLE_FOLDER_NAMES, stop_at=root)
    if in_extra_dir:
        return True
    name = path.name
    return bool(_EXTRA_NAME_RE.search(name))

def is_artwork_file(path: Path) -> bool:
    if path.suffix.lower() not in ARTWORK_EXTENSIONS:
        return False
    stem = path.stem.lower()
    return stem in ARTWORK_NAMES or bool(_ARTWORK_NUMBERED_BACKDROP_RE.fullmatch(stem))

@dataclass
class ScannedFile:
    path: Path
    size: int
    kind: str  # video, subtitle, artwork, extra, other

@dataclass
class FolderScan:
    root: Path
    files: list[ScannedFile]
    is_disc: bool

    @property
    def videos(self) -> list[ScannedFile]:
        return [f for f in self.files if f.kind == "video"]

    @property
    def subtitles(self) -> list[ScannedFile]:
        return [f for f in self.files if f.kind == "subtitle"]

    @property
    def artwork(self) -> list[ScannedFile]:
        return [f for f in self.files if f.kind == "artwork"]

    @property
    def extras(self) -> list[ScannedFile]:
        return [f for f in self.files if f.kind == "extra"]

def scan_tree(root: Path) -> FolderScan:
    files: list[ScannedFile] = []
    is_disc = path_has_disc_structure(root)
    if not root.is_dir():
        return FolderScan(root, files, is_disc)

    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        # Do not descend into other disc structures nested oddly, but DO
        # walk BDMV if *this* root is a disc so we can hardlink it later.
        if not is_disc:
            dirnames[:] = [d for d in dirnames if not is_disc_folder_name(d)]
        # Skip junk / incomplete
        dirnames[:] = [d for d in dirnames if not is_skipped_junk_name(d)]
        for name in filenames:
            if is_skipped_junk_name(name):
                continue
            path = current / name
            ext = path.suffix.lower()
            try:
                st = path.stat(follow_symlinks=False)
            except OSError:
                continue
            if path.is_symlink() or not S_ISREG(st.st_mode):
                # A symlink inside a torrent folder can point anywhere on the
                # machine, and os.link() follows it: hardlinking one would pull
                # a file from outside E:\torrents into the library and present
                # it as a movie the release never shipped. Top-level items and
                # SRT sidecars already refuse symlinks; nested videos must too.
                LOG.warning("Skipping symlinked/non-regular file inside folder: %s", path)
                continue
            size = st.st_size
            if ext in VIDEO_EXTENSIONS:
                if is_disc:
                    # Individual .m2ts files are not movies.
                    files.append(ScannedFile(path, size, "disc-file"))
                elif is_extra_video(path, root=root, size=size):
                    files.append(ScannedFile(path, size, "extra"))
                elif size >= CFG.min_movie_bytes:
                    files.append(ScannedFile(path, size, "video"))
                else:
                    files.append(ScannedFile(path, size, "extra"))
            elif ext in SUBTITLE_EXTENSIONS:
                if path_inside_named(path, EXTRA_FOLDER_NAMES - SUBTITLE_FOLDER_NAMES, stop_at=root):
                    files.append(ScannedFile(path, size, "other"))
                else:
                    files.append(ScannedFile(path, size, "subtitle"))
            elif is_artwork_file(path):
                files.append(ScannedFile(path, size, "artwork"))
            else:
                files.append(ScannedFile(path, size, "other"))
    return FolderScan(root, files, is_disc)

# Folder names that carry no movie information (typical save-path names).
# When the movie folder has one of these names, its name must NOT override
# a usable parsed filename ("downloads" → "Src (2019)" would be wrong).
_GENERIC_FOLDER_STEMS = frozenset({
    "all", "anime", "backup", "complete", "completed", "content", "contents",
    "data", "deluge", "done", "download", "downloaded", "downloads", "dst",
    "extracted", "film", "films", "final", "finished", "incoming", "input",
    "library", "media", "misc", "movie", "movies", "new", "out", "output",
    "qbittorrent", "radarr", "rar", "rtorrent", "seed", "seeding", "seeds",
    "sonarr", "src", "stuff", "temp", "tmp", "torrent", "torrents", "towatch",
    "transmission", "tv", "unpacked", "unsorted", "video", "videos", "watch",
})

def stem_is_generic(stem: str) -> bool:
    cleaned = re.sub(r"[\s._\-]+", "", stem).lower()
    if cleaned in GENERIC_STEMS:
        return True
    if _PART_RE.search(stem) and not re.search(r"(?:18|19|20)\d{2}", stem):
        # "cd1" / "Disc 2" with no year — generic split name
        leftover = _PART_RE.sub("", stem)
        leftover = re.sub(r"[\s._\-]+", "", leftover).lower()
        if not leftover or leftover in GENERIC_STEMS:
            return True
    return False

# Folder names that are collections of several movies, not one movie's name.
# When the movie folder carries one of these, its name must NOT override the
# identity parsed from the actual video file ("Duo.Collection" containing
# only "First.Blood.1982.mkv" is "First Blood (1982)", not "Duo Collection").
_COLLECTION_FOLDER_RE = re.compile(
    r"(?i)(?:^|[\s._\-])(trilogy|collection|anthology|duology|quadrilogy|pentalogy|saga|franchise|boxset|box[\s._\-]?set|series)(?:$|[\s._\-])"
)

def folder_name_is_generic(name: str) -> bool:
    cleaned = re.sub(r"[\s._\-]+", "", name).lower()
    return cleaned in _GENERIC_FOLDER_STEMS

def parse_video_identity(video: Path, fallback: Path | None = None) -> ParsedName:
    parsed = parse_movie_name(video.name)
    if parsed.is_tv:
        return parsed
    if (
        parsed.title
        and not stem_is_generic(os.path.splitext(video.name)[0])
        and parsed.title.casefold() not in GENERIC_STEMS
        # If the filename parsed to something useful, keep it.
        # Still prefer a parent folder when the filename is just tags + year.
        and parsed.title.lower() not in {"unknown", ""}
    ):
        return parsed
    if fallback is not None and not folder_name_is_generic(fallback.name):
        parent_parsed = parse_movie_name(fallback.name)
        if not parent_parsed.is_tv and parent_parsed.title:
            return ParsedName(
                title=parent_parsed.title,
                year=parent_parsed.year or parsed.year,
                edition=parsed.edition or parent_parsed.edition,
                resolution=parsed.resolution,
                three_d=parsed.three_d,
                provider_id=parent_parsed.provider_id or parsed.provider_id,
                part=parsed.part,
                raw=parsed.raw,
            )
    # A generic parent ("downloads") must never rename the movie; prefer
    # whatever the filename itself parsed to.
    return parsed

def match_subtitles_for_video(
    video: Path,
    parsed: ParsedName,
    subtitles: Sequence[ScannedFile],
    *,
    multi: bool,
) -> list[ScannedFile]:
    if not multi:
        return list(subtitles)

    video_stem = os.path.splitext(video.name)[0].casefold()
    video_title = parsed.title.casefold()
    hits: list[ScannedFile] = []
    for sub in subtitles:
        sub_stem = os.path.splitext(sub.path.name)[0]
        sub_stem_clean = _strip_known_sub_suffixes(sub_stem).casefold()
        parent_names = {p.name.casefold() for p in sub.path.parents}

        if sub_stem_clean == video_stem or sub_stem.casefold() == video_stem:
            hits.append(sub)
            continue
        if video_stem and (video_stem in parent_names):
            hits.append(sub)
            continue
        sub_parsed = parse_movie_name(sub_stem_clean)
        if sub_parsed.title.casefold() == video_title and (
            sub_parsed.year == parsed.year or sub_parsed.year is None or parsed.year is None
        ):
            hits.append(sub)
            continue
        # Parent folder named after this movie
        for parent in sub.path.parents:
            parent_parsed = parse_movie_name(parent.name)
            if parent_parsed.title.casefold() == video_title and parent_parsed.title.casefold() not in {
                "subs", "sub", "subtitles", "subtitle",
            }:
                hits.append(sub)
                break
    return hits

# =====================================================================
# FILE ACTIONS
# =====================================================================

def dest_for(
    parsed: ParsedName,
    ext: str,
    *,
    part: str | None = None,
) -> Path:
    stem = parsed.file_stem(part)
    filename = f"{stem}{ext}"
    if CFG.create_subfolders:
        return CFG.target_dir / parsed.folder_name / filename
    return CFG.target_dir / filename

def _ensure_parent(path: Path) -> None:
    if CFG.dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)

def _quarantine_destination(candidate: Path) -> Path:
    """Create a unique, non-media-tree destination for a quarantined candidate."""
    if CFG.quarantine_dir is None:
        raise ValueError("QUARANTINE mode requires --quarantine-dir")
    try:
        relative = candidate.relative_to(CFG.target_dir)
    except ValueError:
        relative = Path(candidate.name)
    base = CFG.quarantine_dir / relative
    if not base.exists() and not base.is_symlink():
        return base
    return base.with_name(f"{base.name}.conflict.{uuid.uuid4().hex[:12]}")

def dispose_candidate(candidate: Path, *, action: str, reason: str) -> str:
    """Report, quarantine, or explicitly delete a maintenance candidate safely."""
    mode = CFG.maintenance_mode.upper()
    if mode == "REPORT" or CFG.dry_run:
        prefix = "[DRY-RUN] would" if CFG.dry_run and mode != "REPORT" else "Would"
        LOG.info("%s %s candidate: %s (%s)", prefix, action, candidate, reason)
        record_outcome("reported", action, src=candidate, reason=reason)
        return "reported"
    if mode == "QUARANTINE":
        dest = _quarantine_destination(candidate)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate), str(dest))
        except OSError as exc:
            LOG.error("Failed to quarantine %s: %s", candidate, exc)
            record_outcome("failed", action, src=candidate, dest=dest, reason=str(exc))
            return "failed"
        LOG.info("Quarantined %s candidate: %s -> %s (%s)", action, candidate, dest, reason)
        record_outcome("quarantined", action, src=candidate, dest=dest, reason=reason)
        return "quarantined"
    if mode == "DELETE":
        try:
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
        except OSError as exc:
            LOG.error("Failed to delete %s: %s", candidate, exc)
            record_outcome("failed", action, src=candidate, reason=str(exc))
            return "failed"
        LOG.info("Deleted %s candidate: %s (%s)", action, candidate, reason)
        record_outcome("deleted", action, src=candidate, reason=reason)
        return "deleted"
    raise ValueError(f"Unknown maintenance mode: {mode}")

def _staging_path(dest: Path, label: str) -> Path:
    """Return a collision-resistant sibling path on the destination volume."""
    return dest.with_name(f".{dest.name}.{label}.{os.getpid()}.{uuid.uuid4().hex}")

def _unlink_if_file(path: Path) -> None:
    """Remove only an ordinary staging file or symlink created by this process."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
    except OSError:
        pass

def _replace_with(src_action_name: str, dest: Path, producer) -> bool:
    """Stage a replacement beside ``dest`` and atomically commit it when possible.

    The existing destination is never deleted merely to retry a failed replace.
    A locked/read-only destination therefore remains intact and the operation
    reports failure instead of risking data loss.
    """
    _ensure_parent(dest)
    if CFG.dry_run:
        LOG.info("[DRY-RUN] %s -> %s", src_action_name, dest)
        return True
    tmp = _staging_path(dest, "partial")
    try:
        producer(tmp)
        os.replace(str(tmp), str(dest))
        return True
    except OSError:
        _unlink_if_file(tmp)
        raise

def _create_hardlink(src: Path, dest: Path) -> None:
    """Create one hardlink or raise; never fall back to copying or moving."""
    os.link(str(src), str(dest))

@dataclass(frozen=True)
class MediaTechnicalInfo:
    """Small, stable subset of ffprobe data used for duplicate decisions."""

    duration: float
    width: int
    height: int
    video_codec: str
    bit_depth: int
    hdr: bool
    video_bitrate: int
    audio_channels: int
    audio_bitrate: int

    @property
    def resolution_tier(self) -> int:
        longest = max(self.width, self.height)
        if longest >= 3800:
            return 4  # UHD / 4K
        if longest >= 2500:
            return 3  # 1440p-ish
        if longest >= 1800:
            return 2  # 1080p
        if longest >= 1200:
            return 1  # 720p
        return 0

def find_ffprobe(explicit: str = "ffprobe") -> str | None:
    """Find ffprobe without making it a prerequisite for initial placement."""
    candidates: list[str] = []
    if explicit and explicit != "ffprobe":
        candidates.append(explicit)
        explicit_on_path = shutil.which(explicit)
        if explicit_on_path:
            candidates.append(explicit_on_path)
    located = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if located:
        candidates.append(located)
    here = Path(__file__).resolve().parent
    candidates.extend([
        str(here / "ffprobe.exe"),
        str(here / "ffprobe"),
        str(here / "ffmpeg" / "ffprobe.exe"),
        r"C:\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files\FFmpeg\bin\ffprobe.exe",
    ])
    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen and Path(candidate).is_file():
            return str(Path(candidate))
        seen.add(candidate)
    return None

def _probe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0

def _probe_float(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0

def _stream_bit_depth(stream: dict) -> int:
    raw = _probe_int(stream.get("bits_per_raw_sample"))
    if raw:
        return raw
    pix_fmt = str(stream.get("pix_fmt") or "").casefold()
    match = re.search(r"(?:p|p0)(10|12|14|16)(?:le|be)?$", pix_fmt)
    return int(match.group(1)) if match else 8

def _stream_is_hdr(stream: dict) -> bool:
    transfer = str(stream.get("color_transfer") or "").casefold()
    if transfer in {"smpte2084", "arib-std-b67", "smpte428"}:
        return True
    side_data = " ".join(
        str(item.get("side_data_type") or "")
        for item in stream.get("side_data_list") or []
        if isinstance(item, dict)
    ).casefold()
    return any(marker in side_data for marker in ("dovi", "dolby vision", "hdr dynamic"))

def probe_media(path: Path, ffprobe: str) -> tuple[MediaTechnicalInfo | None, str]:
    """Inspect one movie for conservative identity and quality comparison."""
    command = [
        ffprobe, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # ffprobe speaks ASCII, but decode explicitly anyway: the locale
            # encoding (cp1252 on Windows) is never what we want here.
            encoding="utf-8",
            errors="replace",
            timeout=FFPROBE_TIMEOUT_SECONDS,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"ffprobe could not inspect {path.name}: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        return None, f"ffprobe rejected {path.name}: {detail[-1] if detail else f'exit {completed.returncode}'}"
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return None, f"ffprobe returned invalid JSON for {path.name}: {exc}"

    streams = payload.get("streams") or []
    videos = [
        stream for stream in streams
        if isinstance(stream, dict)
        and stream.get("codec_type") == "video"
        and not _probe_int((stream.get("disposition") or {}).get("attached_pic"))
    ]
    if not videos:
        return None, f"ffprobe found no feature video stream in {path.name}"
    video = max(videos, key=lambda stream: _probe_int(stream.get("width")) * _probe_int(stream.get("height")))
    audios = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"]
    audio = max(
        audios,
        key=lambda stream: (_probe_int(stream.get("channels")), _probe_int(stream.get("bit_rate"))),
        default={},
    )
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _probe_float(fmt.get("duration")) or _probe_float(video.get("duration"))
    width, height = _probe_int(video.get("width")), _probe_int(video.get("height"))
    if duration <= 0 or width <= 0 or height <= 0:
        return None, f"ffprobe returned incomplete duration/resolution data for {path.name}"
    return MediaTechnicalInfo(
        duration=duration,
        width=width,
        height=height,
        video_codec=str(video.get("codec_name") or "unknown").casefold(),
        bit_depth=_stream_bit_depth(video),
        hdr=_stream_is_hdr(video),
        video_bitrate=_probe_int(video.get("bit_rate")) or _probe_int(fmt.get("bit_rate")),
        audio_channels=_probe_int(audio.get("channels")),
        audio_bitrate=_probe_int(audio.get("bit_rate")),
    ), ""

def technical_quality_score(info: MediaTechnicalInfo) -> float:
    """Balanced score; a replacement still must pass all downgrade guards."""
    codec_bonus = {
        "mpeg2video": -3.0, "h264": 0.0, "vp9": 4.0, "hevc": 5.0,
        "av1": 8.0,
    }.get(info.video_codec, 0.0)
    video_mbps = min(info.video_bitrate / 1_000_000.0, 20.0)
    audio_mbps = min(info.audio_bitrate / 1_000_000.0, 3.0)
    return (
        info.resolution_tier * 25.0
        + (20.0 if info.hdr else 0.0)
        + info.bit_depth * 3.0
        + video_mbps * 2.0
        + info.audio_channels * 1.5
        + audio_mbps * 2.0
        + codec_bonus
    )

def _movie_upgrade_decision(src: Path, dest: Path) -> tuple[bool, str]:
    """Require same-cut identity and a meaningful, non-regressive upgrade."""
    source_name = parse_video_identity(src, fallback=src.parent)
    existing_name = parse_movie_name(dest.name)
    if (source_name.title.casefold(), source_name.year) != (existing_name.title.casefold(), existing_name.year):
        return False, "conflict: source and canonical title/year identities differ"
    if source_name.edition or source_name.three_d or source_name.part:
        markers = ", ".join(filter(None, (source_name.edition, source_name.three_d, source_name.part)))
        return False, f"conflict: incoming release has alternate-cut/version marker ({markers})"

    ffprobe = find_ffprobe(CFG.ffprobe)
    if not ffprobe:
        return False, "conflict: ffprobe unavailable; keeping existing movie (size alone never replaces)"
    source_info, source_error = probe_media(src, ffprobe)
    existing_info, existing_error = probe_media(dest, ffprobe)
    if source_info is None or existing_info is None:
        return False, f"conflict: {source_error or existing_error}; keeping existing movie"

    duration_gap = abs(source_info.duration - existing_info.duration)
    duration_limit = max(
        DUPLICATE_DURATION_MAX_SECONDS,
        max(source_info.duration, existing_info.duration) * DUPLICATE_DURATION_MAX_FRACTION,
    )
    if duration_gap > duration_limit:
        return False, (
            f"conflict: runtime differs by {duration_gap:.1f}s (limit {duration_limit:.1f}s); "
            "likely a different cut"
        )
    if source_info.resolution_tier < existing_info.resolution_tier:
        return False, "conflict: incoming movie has a lower resolution tier"
    if existing_info.hdr and not source_info.hdr:
        return False, "conflict: incoming movie would replace HDR with SDR"
    if source_info.bit_depth < existing_info.bit_depth:
        return False, "conflict: incoming movie has lower video bit depth"
    if source_info.audio_channels < existing_info.audio_channels:
        return False, "conflict: incoming movie has fewer audio channels"

    source_score = technical_quality_score(source_info)
    existing_score = technical_quality_score(existing_info)
    gain = source_score - existing_score
    if gain < DUPLICATE_MIN_SCORE_GAIN:
        return False, (
            f"conflict: no clear technical upgrade (score {source_score:.1f} vs "
            f"{existing_score:.1f}, need +{DUPLICATE_MIN_SCORE_GAIN:.1f})"
        )
    return True, (
        f"verified same-cut technical upgrade (runtime gap {duration_gap:.1f}s; "
        f"score {source_score:.1f} vs {existing_score:.1f}, +{gain:.1f})"
    )

def should_replace(src: Path, dest: Path) -> tuple[bool, str]:
    """Decide whether a destination may be replaced without relying on size alone."""
    if not dest.exists():
        return True, "missing"
    if paths_equal(src, dest):
        return False, "same-file"
    try:
        if dest.exists() and src.exists() and dest.samefile(src):
            return False, "already-linked"
    except OSError:
        pass
    if src.suffix.casefold() == CANONICAL_VIDEO_EXTENSION and dest.suffix.casefold() == CANONICAL_VIDEO_EXTENSION:
        return _movie_upgrade_decision(src, dest)

    # Non-movie sidecars retain their established behavior. The stricter probe
    # policy applies only to replacement of the canonical MKV.
    src_sz, dest_sz = file_size(src), file_size(dest)
    if src_sz > dest_sz:
        return True, f"src-larger ({src_sz} > {dest_sz})"
    if src_sz == dest_sz:
        return False, "same-size-exists"
    return False, f"dest-larger ({dest_sz} >= {src_sz})"

def process_file_action(src: Path, dest: Path) -> bool:
    """Hardlink ``src`` to ``dest`` safely and idempotently.

    The existing destination is never deleted before a staged hardlink is
    ready: a failed link cannot destroy the target or create a copy.
    """
    RUN_SUMMARY.attempted += 1
    if not src.exists():
        LOG.warning("Source vanished: %s", src)
        record_outcome("failed", "media placement", src=src, dest=dest, reason="source vanished")
        return False
    if paths_equal(src, dest):
        LOG.info("Already in place: %s", dest)
        record_outcome("skipped", "media placement", src=src, dest=dest, reason="already in place")
        return True

    mode = PROCESS_MODE
    replace, reason = should_replace(src, dest)
    if not replace:
        LOG.info("Skip %s (%s)", dest, reason)
        record_outcome("skipped", "media placement", src=src, dest=dest, reason=reason)
        return True

    _ensure_parent(dest)
    if CFG.dry_run:
        LOG.info("[DRY-RUN] %s: %s -> %s", mode, src, dest)
        record_outcome("reported", mode, src=src, dest=dest, reason="dry run")
        return True

    try:
        # Stage the hardlink beside the destination, then atomically swap it.
        def producer(tmp: Path, _src: Path = src) -> None:
            _create_hardlink(_src, tmp)

        _replace_with(f"{mode} {src}", dest, producer)
        try:
            if not src.samefile(dest):
                raise OSError("post-placement hardlink verification failed")
        except OSError as exc:
            LOG.error("%s verification failed for '%s': %s", mode, src, exc)
            record_outcome("failed", mode, src=src, dest=dest, reason=f"hardlink verification failed: {exc}")
            return False
        used = mode
        LOG.info("%s: '%s' -> '%s' (verified hardlink)", used, src, dest)
        record_outcome("completed", used, src=src, dest=dest, reason="verified hardlink")
        return True
    except OSError as exc:
        LOG.error("%s failed for '%s': %s", mode, src, exc)
        record_outcome("failed", mode, src=src, dest=dest, reason=str(exc))
        return False

def process_disc_folder(src_dir: Path, parsed: ParsedName) -> bool:
    """Hardlink a disc tree only when called explicitly (canonical scans skip discs)."""
    dest_root = CFG.target_dir / parsed.folder_name
    LOG.info("Disc structure detected: '%s' -> '%s'", src_dir, dest_root)
    ok = True
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames[:] = [d for d in dirnames if not is_skipped_junk_name(d)]
        rel = Path(dirpath).relative_to(src_dir)
        for name in filenames:
            if is_skipped_junk_name(name):
                continue
            src = Path(dirpath) / name
            dest = dest_root / rel / name
            if not process_file_action(src, dest):
                ok = False
    return ok

def copy_extras_into(src_root: Path, dest_movie_folder: Path, extras: Sequence[ScannedFile]) -> None:
    if not CFG.copy_extras or not extras:
        return
    if not CFG.create_subfolders:
        return
    for item in extras:
        try:
            rel = item.path.relative_to(src_root)
        except ValueError:
            rel = Path("extras") / item.path.name
        # Keep extra-folder names Plex understands; if the extra was a
        # loose file, drop it into extras/.
        if rel.parent == Path("."):
            rel = Path("extras") / rel.name
        process_file_action(item.path, dest_movie_folder / rel)

def copy_artwork_into(dest_movie_folder: Path, artwork: Sequence[ScannedFile]) -> None:
    if not CFG.copy_artwork or not artwork or not CFG.create_subfolders:
        return
    for item in artwork:
        process_file_action(item.path, dest_movie_folder / item.path.name.lower())

def pair_idx_files(subtitles: Sequence[ScannedFile]) -> dict[Path, Path]:
    """Map .sub → sibling .idx when both exist (VobSub pair)."""
    pairs: dict[Path, Path] = {}
    for item in subtitles:
        if item.path.suffix.lower() != ".sub":
            continue
        idx = item.path.with_suffix(".idx")
        if idx.exists():
            pairs[item.path] = idx
    return pairs

# =====================================================================
# PROCESSING
# =====================================================================

def place_one_safe_external_srt(candidates: Sequence[Path], destination: Path) -> None:
    """Place one validated normal English SRT and never overwrite a sidecar."""
    valid: list[Path] = []
    for candidate in sorted(candidates, key=lambda path: path.name.casefold()):
        ok, reason = is_valid_plain_english_srt(candidate)
        if ok:
            valid.append(candidate)
        elif candidate.suffix.lower() in SUBTITLE_EXTENSIONS:
            LOG.info("Leaving source subtitle '%s' (%s)", candidate.name, reason)
    if len(valid) != 1:
        if len(valid) > 1:
            reason = f"{len(valid)} valid normal English SRT candidates; leaving all source subtitles for fetcher/review"
            LOG.warning("%s", reason)
            record_outcome("reported", "subtitle ambiguity", reason=reason)
        return
    source = valid[0]
    if destination.exists() or destination.is_symlink():
        LOG.info("Preserving existing canonical external SRT: %s", destination)
        record_outcome("skipped", "subtitle placement", src=source, dest=destination, reason="destination already exists")
        return
    process_file_action(source, destination)

def skip_tv(name: str, origin: str) -> bool:
    if CFG.skip_tv_shows and is_tv_show(name):
        LOG.info("Skipping TV show (%s): %s", origin, name)
        return True
    return False

def handle_single_file(path: Path) -> None:
    if is_skipped_junk_name(path.name):
        # Transient by definition (".!qb", ".part", ...): not reported as a
        # leftover, because it is expected to disappear on its own.
        LOG.info("Skipping junk / incomplete: %s", path.name)
        return
    if path.suffix.lower() != CANONICAL_VIDEO_EXTENSION:
        LOG.info("Skipping non-MKV file; no transcoding is performed: %s", path.name)
        decline_source(path, "not an MKV; this tool never transcodes")
        return
    if is_extra_video(path):
        LOG.info("Skipping extra/sample: %s", path.name)
        decline_source(path, "extra/sample video, not a feature")
        return
    if file_size(path) < CFG.min_movie_bytes:
        size_mb = file_size(path) / (1024 * 1024)
        LOG.info("Skipping small file (%.1f MB): %s", size_mb, path.name)
        decline_source(path, f"smaller than the {CFG.min_movie_size_mb:.0f} MB minimum ({size_mb:.1f} MB)")
        return
    if skip_tv(path.name, "filename"):
        decline_source(path, "looks like a TV episode, not a movie")
        return
    parsed = parse_movie_name(path.name)
    if not parsed.title:
        LOG.warning("Could not parse title: %s", path.name)
        decline_source(path, "no title/year could be parsed from the name")
        return
    if parsed.part:
        LOG.info("Skipping multipart fragment; canonical output requires one complete MKV: %s", path.name)
        decline_source(path, "multipart fragment; canonical output requires one complete MKV")
        return
    LOG.info("Single file '%s' -> '%s'", path.name, parsed.folder_name)
    dest = dest_for(
        parsed,
        path.suffix.lower(),
        part=parsed.part,
    )
    if process_file_action(path, dest):
        # Sidecar subtitles next to the video
        parent = path.parent
        stem = path.stem
        sidecars: list[Path] = []
        try:
            for sibling in parent.iterdir():
                if not sibling.is_file():
                    continue
                if sibling.suffix.lower() not in SUBTITLE_EXTENSIONS:
                    continue
                sstem = sibling.stem
                if (
                    sstem.casefold() == stem.casefold()
                    or sstem.casefold().startswith(stem.casefold() + ".")
                    or _strip_known_sub_suffixes(sstem).casefold() == stem.casefold()
                ):
                    sidecars.append(sibling)
        except OSError as exc:
            LOG.warning("Cannot list sidecars in %s: %s", parent, exc)
        place_one_safe_external_srt(sidecars, dest.with_name(parsed.file_stem(parsed.part) + EXTERNAL_SRT_SUFFIX))

def _group_videos(videos: Sequence[ScannedFile], root: Path) -> dict[tuple, list[tuple[ScannedFile, ParsedName]]]:
    groups: dict[tuple, list[tuple[ScannedFile, ParsedName]]] = {}
    for video in videos:
        parsed = parse_video_identity(video.path, fallback=video.path.parent if video.path.parent != root else root)
        if parsed.is_tv and CFG.skip_tv_shows:
            LOG.info("Skipping TV video inside folder: %s", video.path.name)
            continue
        key = parsed.identity
        groups.setdefault(key, []).append((video, parsed))
    return groups

# Non-MKV containers a finished movie can plausibly arrive in. Used only to
# explain a decline precisely — this tool never transcodes, so these are always
# left where they are. The vocabulary matches library_auditor.MOVIE_EXTENSIONS
# so the two tools never disagree about what counts as a movie container.
OTHER_MOVIE_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".webm", ".mpg", ".mpeg", ".ts",
    ".m2ts", ".mts", ".vob", ".flv", ".ogv", ".3gp", ".asf", ".rm", ".rmvb",
    ".m2v", ".divx", ".f4v", ".mxf", ".dv", ".wtv", ".dvr-ms", ".iso", ".img",
    ".nrg",
})

def explain_no_canonical_video(root: Path) -> str:
    """Return an actionable reason when a folder yields no placeable MKV.

    ``decline_source`` exists so the report tells the user what to do next, but
    the blanket "no movie-sized video found inside" was unhelpful for the two
    common cases that are not actually about size or absence: a 15 GB ``.mp4``
    release (the tool will not transcode, so nothing is wrong with the file) and
    an MKV that is merely under the size floor. Distinguishing them keeps the
    "items left in source" section trustworthy.
    """
    biggest_other = 0
    biggest_mkv = 0
    for _root, _dirs, files in os.walk(root, onerror=lambda _e: None):
        for filename in files:
            if is_skipped_junk_name(filename):
                continue
            candidate = Path(_root) / filename
            ext = candidate.suffix.lower()
            if ext not in VIDEO_EXTENSIONS and ext not in OTHER_MOVIE_EXTENSIONS:
                continue
            try:
                st = candidate.stat(follow_symlinks=False)
            except OSError:
                continue
            if candidate.is_symlink() or not S_ISREG(st.st_mode):
                continue
            if ext in VIDEO_EXTENSIONS:
                biggest_mkv = max(biggest_mkv, st.st_size)
            else:
                biggest_other = max(biggest_other, st.st_size)

    if biggest_mkv and biggest_mkv < CFG.min_movie_bytes:
        return (
            f"smaller than the {CFG.min_movie_size_mb:.0f} MB minimum "
            f"({biggest_mkv / (1024 * 1024):.1f} MB)"
        )
    # `biggest_other and ...`: with a 0 MB size floor the comparison alone is
    # always true and would invent "not an MKV (0 MB)" for a folder holding no
    # other-container at all.
    if biggest_other and biggest_other >= CFG.min_movie_bytes:
        return f"not an MKV ({biggest_other / (1024 * 1024):.0f} MB); this tool never transcodes"
    return "no movie-sized video found inside"

def handle_directory(path: Path) -> None:
    name = path.name
    if skip_tv(name, "folder"):
        decline_source(path, "looks like a TV show, not a movie")
        return

    scan = scan_tree(path)
    if scan.is_disc:
        LOG.info("Skipping disc structure; canonical output requires one complete MKV: %s", path)
        decline_source(path, "disc structure (BDMV/VIDEO_TS); canonical output requires one complete MKV")
        return

    if not scan.videos:
        reason = explain_no_canonical_video(path)
        LOG.warning("No placeable movie in: %s (%s)", path, reason)
        decline_source(path, reason)
        return

    groups = _group_videos(scan.videos, path)
    if not groups:
        LOG.warning("No usable movies in: %s", path)
        decline_source(path, "no usable movie video after excluding TV content")
        return

    multi_titles = len(groups) > 1
    if multi_titles:
        LOG.info("Box set '%s': %d distinct movies", name, len(groups))

    for items in groups.values():
        # Prefer a parsed name that came from a non-generic filename;
        # otherwise use the folder name for single-title folders.
        parsed = items[0][1]
        if not multi_titles:
            folder_parsed = parse_movie_name(name)
            informative = (
                not folder_parsed.is_tv
                and folder_parsed.title
                and not folder_name_is_generic(name)
                and not _COLLECTION_FOLDER_RE.search(name)
                and not parsed.is_tv
                # A folder is only a better name when it carries real
                # information: a year or a multi-word title. A single
                # meaningless token ("src0") must not rename the movie.
                and (folder_parsed.year is not None or re.search(r"[\s\-]", folder_parsed.title))
            )
            if informative:
                # Folder names are usually cleaner than scene filenames —
                # but a generic save-path folder ("downloads", "completed")
                # must never rename the movie, and a movie-style folder
                # name must never erase a TV episode identity.
                parsed = ParsedName(
                    title=folder_parsed.title,
                    year=folder_parsed.year or parsed.year,
                    edition=folder_parsed.edition or parsed.edition,
                    resolution=parsed.resolution,
                    three_d=parsed.three_d,
                    provider_id=folder_parsed.provider_id or parsed.provider_id,
                    part=None,
                    raw=folder_parsed.raw,
                )

        parts = [(v, p) for v, p in items if p.part]
        if len(items) > 1 and len(parts) == len(items):
            LOG.info("Skipping multipart movie; canonical output requires one complete MKV: %s", path)
            decline_source(path, "multipart movie; canonical output requires one complete MKV")
            continue

        # _extract_part() deliberately refuses to call a bare "part" token a
        # split marker, because "Deathly Hallows Part 2" is a distinct movie
        # and must never lose its title. That is the right call for a file
        # standing alone, but it left a real hole: a folder holding
        # "Title.2018.part1.mkv" and "Title.2018.part2.mkv" grouped as one
        # title with no part metadata at all, so the largest file won and
        # part 1 was hardlinked into the library as a complete movie.
        # Siblings that differ only in a part number are an unambiguous split
        # release, so detect the stack from the raw names rather than from
        # parsed.part. Re-reading the names here keeps the title-integral
        # "Part 2" protection exactly as it was for single files.
        if len(items) > 1:
            raw_stack_numbers = []
            for video, _ in items:
                stack_match = _PART_RE.search(video.path.stem)
                raw_stack_numbers.append(int(stack_match.group(1)) if stack_match else None)
            valid_stack_numbers = [n for n in raw_stack_numbers if n is not None]
            if len(valid_stack_numbers) == len(raw_stack_numbers) and len(set(valid_stack_numbers)) == len(valid_stack_numbers):
                LOG.info(
                    "Skipping part-numbered split release (parts %s); "
                    "canonical output requires one complete MKV: %s",
                    ", ".join(str(n) for n in sorted(valid_stack_numbers)),
                    path,
                )
                decline_source(
                    path,
                    "multipart movie; canonical output requires one complete MKV",
                )
                continue

        if any(item_parsed.part for _, item_parsed in items):
            LOG.info("Skipping multipart fragments; canonical output requires one complete MKV: %s", path)
            decline_source(path, "multipart fragments; canonical output requires one complete MKV")
            continue

        # One (or several unmarked) files of the same title: keep the largest.
        best_video, _ = max(items, key=lambda ip: ip[0].size)
        if len(items) > 1:
            LOG.info(
                "Multiple files for '%s'; keeping largest (%.1f MB)",
                parsed.folder_name, best_video.size / (1024 * 1024),
            )
        LOG.info("Movie '%s' -> '%s'", best_video.path.name, parsed.folder_name)
        dest = dest_for(
            parsed, best_video.path.suffix.lower(), part=None,
        )
        if process_file_action(best_video.path, dest):
            _place_subs(best_video.path, parsed, scan.subtitles, dest, multi=multi_titles, part=None)

def _place_subs(
    video: Path,
    parsed: ParsedName,
    subtitles: Sequence[ScannedFile],
    video_dest: Path,
    *,
    multi: bool,
    part: str | None,
) -> None:
    matched = match_subtitles_for_video(video, parsed, subtitles, multi=multi)
    place_one_safe_external_srt([sub.path for sub in matched], video_dest.with_name(parsed.file_stem(part) + EXTERNAL_SRT_SUFFIX))

def handle_item(item_path: Path) -> None:
    try:
        item_path = item_path.expanduser()
        if item_path.is_symlink():
            LOG.warning("Skipping symlinked input: %s", item_path)
            record_outcome("skipped", "process item", src=item_path, reason="symlink input")
            return
        if not item_path.exists():
            LOG.warning("Path does not exist: %s", item_path)
            return
        # Resolve once for stability, but keep the original name (don't
        # follow a symlink out of the torrent folder for naming).
        if item_path.is_dir():
            handle_directory(item_path)
        elif item_path.is_file():
            handle_single_file(item_path)
        else:
            LOG.warning("Not a file or directory: %s", item_path)
    except OSError as exc:
        LOG.error("Failed to process %s: %s", item_path, exc)
        record_outcome("failed", "process item", src=item_path, reason=str(exc))

# =====================================================================
# TARGET MAINTENANCE
# =====================================================================

def clean_existing_extras(target: Path) -> None:
    LOG.info("--- Target extras cleanup: %s ---", target)
    if not target.exists():
        LOG.warning("Target does not exist; nothing to clean")
        return
    deleted_files = 0
    deleted_folders = 0
    for dirpath, dirnames, filenames in os.walk(target, topdown=True):
        current = Path(dirpath)
        for dirname in list(dirnames):
            if not is_extra_folder_name(dirname):
                continue
            if dirname.lower() in SUBTITLE_FOLDER_NAMES:
                continue
            full = current / dirname
            # Refuse to delete a folder that holds a feature-sized video.
            scan = scan_tree(full)
            if scan.videos or scan.is_disc:
                LOG.warning("Kept extra-named folder that contains a movie: %s", full)
                continue
            outcome = dispose_candidate(full, action="extra folder", reason="extra-named folder without a movie")
            if outcome in {"deleted", "quarantined"}:
                dirnames.remove(dirname)
            if outcome == "deleted":
                deleted_folders += 1
        for name in filenames:
            full = current / name
            ext = full.suffix.lower()
            if ext in VIDEO_EXTENSIONS and is_extra_video(full, root=target):
                if file_size(full) >= CFG.min_movie_bytes:
                    continue
                outcome = dispose_candidate(full, action="extra file", reason="small extra/sample video")
                if outcome == "deleted":
                    deleted_files += 1
    LOG.info("--- Extras cleanup done. Folders=%d files=%d ---", deleted_folders, deleted_files)

def _folder_has_files(folder: Path) -> bool:
    """True if folder holds at least one file anywhere below it.

    Walk errors count as "has files" — if we cannot prove it is empty,
    we must not delete it.
    """
    had_error = False

    def _on_error(_exc: OSError) -> None:
        nonlocal had_error
        had_error = True

    for _root, _dirs, files in os.walk(folder, onerror=_on_error):
        if files:
            return True
    return had_error

def _is_jellyfin_multi_version_folder(folder: Path) -> bool:
    """Detect documented direct-child Jellyfin versions with an exact prefix."""
    if not CFG.jellyfin_mode or not folder.is_dir():
        return False
    prefix = folder.name.casefold() + " - "
    count = 0
    try:
        for path in folder.iterdir():
            if (
                path.is_file()
                and path.suffix.lower() in VIDEO_EXTENSIONS
                and path.stem.casefold().startswith(prefix)
                and not is_extra_video(path, root=folder)
            ):
                count += 1
                if count > 1:
                    return True
    except OSError:
        return True  # Cannot prove safety; never collapse a potentially valid set.
    return False

def _largest_video_in(folder: Path) -> tuple[Path | None, int]:
    best: Path | None = None
    best_sz = 0
    if not folder.is_dir():
        return None, 0
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not is_extra_folder_name(d) and not is_disc_folder_name(d)]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if is_extra_video(path, root=folder):
                continue
            sz = file_size(path)
            if sz > best_sz:
                best, best_sz = path, sz
    return best, best_sz

def deduplicate_movies(target: Path) -> None:
    if not CFG.enable_deduplication:
        return
    LOG.info("--- Duplicate scan: %s ---", target)
    if not target.exists():
        LOG.warning("Target does not exist; skipping dedup")
        return
    deleted = 0
    kept_conflicts = 0

    if CFG.create_subfolders:
        try:
            folders = [p for p in target.iterdir() if p.is_dir() and not p.name.startswith(".")]
        except OSError as exc:
            LOG.error("Cannot list %s: %s", target, exc)
            return
        groups: dict[tuple, list[Path]] = {}
        for folder in folders:
            parsed = parse_movie_name(folder.name)
            if not parsed.title or parsed.is_tv:
                continue
            groups.setdefault(parsed.identity, []).append(folder)
        for key, folder_list in groups.items():
            if len(folder_list) < 2:
                continue
            if any(_is_jellyfin_multi_version_folder(folder) for folder in folder_list):
                LOG.warning("Keeping duplicate group with a native Jellyfin multi-version folder: %s", key)
                kept_conflicts += len(folder_list) - 1
                continue
            stats: list[tuple[Path, int, Path | None]] = []
            for folder in folder_list:
                video, size = _largest_video_in(folder)
                stats.append((folder, size, video))
            stats.sort(key=lambda s: s[1], reverse=True)
            keep_folder, keep_size, keep_video = stats[0]
            LOG.info(
                "Duplicate group %s (%s): keeping '%s' (%.1f MB)",
                key[0], key[1] or "no-year", keep_folder.name, keep_size / (1024 * 1024),
            )
            for folder, size, video in stats[1:]:
                # Same inode → leftover hardlink tree, safe to drop.
                same = False
                try:
                    if keep_video and video and keep_video.exists() and video.exists():
                        same = keep_video.samefile(video)
                except OSError:
                    same = False
                if video is None:
                    # No video inside: only safe to remove when the folder
                    # is provably empty (e.g. a leftover shell). A folder
                    # that still holds subtitles/artwork is unique data.
                    if _folder_has_files(folder):
                        LOG.warning(
                            "Keeping video-less duplicate folder that still holds files: %s", folder,
                        )
                        kept_conflicts += 1
                        continue
                    outcome = dispose_candidate(folder, action="empty duplicate folder", reason="duplicate identity with no files")
                    if outcome == "deleted":
                        deleted += 1
                    continue
                if not same and keep_size > 0 and size > 0:
                    margin = abs(keep_size - size) / keep_size * 100.0
                    if margin <= CFG.dedup_size_margin_pct:
                        LOG.warning(
                            "Leaving ambiguous duplicate '%s' (within %.1f%% of keeper)",
                            folder, margin,
                        )
                        kept_conflicts += 1
                        continue
                outcome = dispose_candidate(
                    folder,
                    action="smaller duplicate folder",
                    reason=f"duplicate identity; feature video {size / (1024 * 1024):.1f} MB vs keeper {keep_size / (1024 * 1024):.1f} MB",
                )
                if outcome == "deleted":
                    deleted += 1
    else:
        try:
            videos = [
                p for p in target.iterdir()
                if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and not is_extra_video(p, root=target)
            ]
        except OSError as exc:
            LOG.error("Cannot list %s: %s", target, exc)
            return
        groups_f: dict[tuple, list[Path]] = {}
        for video in videos:
            parsed = parse_movie_name(video.name)
            if not parsed.title or parsed.is_tv:
                continue
            groups_f.setdefault(parsed.identity, []).append(video)
        for key, paths in groups_f.items():
            if len(paths) < 2:
                continue
            stats_f = sorted(((p, file_size(p)) for p in paths), key=lambda s: s[1], reverse=True)
            keep, keep_size = stats_f[0]
            LOG.info("Duplicate files %s (%s): keeping '%s'", key[0], key[1] or "no-year", keep.name)
            for path, size in stats_f[1:]:
                same = False
                try:
                    same = keep.exists() and path.exists() and keep.samefile(path)
                except OSError:
                    same = False
                if not same and keep_size > 0 and size > 0:
                    margin = abs(keep_size - size) / keep_size * 100.0
                    if margin <= CFG.dedup_size_margin_pct:
                        LOG.warning("Leaving ambiguous duplicate file '%s'", path)
                        kept_conflicts += 1
                        continue
                outcome = dispose_candidate(
                    path,
                    action="smaller duplicate file",
                    reason=f"duplicate identity; file {size / (1024 * 1024):.1f} MB vs keeper {keep_size / (1024 * 1024):.1f} MB",
                )
                if outcome == "deleted":
                    deleted += 1
                # Sidecars are removed only after an explicit destructive delete.
                if outcome == "deleted":
                    stem = path.stem
                    try:
                        for sibling in path.parent.iterdir():
                            if (
                                sibling.is_file()
                                and sibling.suffix.lower() in SUBTITLE_EXTENSIONS
                                and (sibling.stem == stem or sibling.stem.startswith(stem + "."))
                            ):
                                dispose_candidate(sibling, action="duplicate subtitle", reason=f"sidecar of deleted {path.name}")
                    except OSError:
                        pass

    LOG.info("--- Duplicate scan done. Deleted=%d ambiguous-kept=%d ---", deleted, kept_conflicts)

# =====================================================================
# CLI / QBITTORRENT ARGUMENTS
# =====================================================================

__version__ = "3.0.0"

def resolve_input_path(positional: Sequence[str]) -> Path | None:
    """Interpret qBittorrent ``%F`` or ``%D %N`` (and a few mix-ups)."""
    args = [a for a in positional if a not in (None, "")]
    if not args:
        return None
    if len(args) == 1:
        return Path(args[0])

    first, second = Path(args[0]), Path(args[1])
    # "%D" "%N" → save path / torrent name
    if first.is_dir():
        joined = first / second.name if second.name else first / str(second)
        # If %N is a relative name
        joined_alt = first / args[1]
        for candidate in (joined_alt, joined):
            if candidate.exists():
                return candidate
    # "%N" "%F" (name then content path)
    if second.exists() and not first.exists():
        return second
    if first.exists() and not second.exists():
        return first
    if first.exists() and second.exists():
        # Prefer the more specific path
        try:
            if first.resolve() in second.resolve().parents or first.resolve() == second.resolve().parent:
                return second
        except OSError:
            pass
        return first if first.is_dir() or not second.is_dir() else second
    # Last resort
    return first

def apply_env(cfg: Config) -> None:
    mapping: dict[str, tuple[str, Callable[[str], Any]]] = {
        "MOVIE_STD_TARGET": ("target_dir", Path),
        "MOVIE_STD_SOURCE": ("source_dir", Path),
        "MOVIE_STD_LOG": ("log_file", Path),
        "MOVIE_STD_MIN_SIZE": ("min_movie_size_mb", float),
        "MOVIE_STD_REPORT": ("report_file", Path),
        "MOVIE_STD_LOCK_TIMEOUT": ("lock_timeout_seconds", float),
        "MOVIE_STD_FFPROBE": ("ffprobe", str),
        "MOVIE_STD_DEDUPLICATE": ("enable_deduplication", lambda v: v.lower() in {"1", "true", "yes"}),
        "MOVIE_STD_MAINTENANCE_MODE": ("maintenance_mode", str),
        "MOVIE_STD_QUARANTINE": ("quarantine_dir", Path),
        "MOVIE_STD_MANIFEST": ("manifest_file", Path),
        "MOVIE_STD_DRY_RUN": ("dry_run", lambda v: v.lower() in {"1", "true", "yes"}),
    }
    for env, (attr, caster) in mapping.items():
        raw = os.environ.get(env)
        if raw:
            setattr(cfg, attr, caster(raw))

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Place one canonical MKV and English subtitles per movie folder.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog='qBittorrent:  python movie_standardizer.py "%F"',
    )
    p.add_argument("paths", nargs="*", help="Content path (%%F) and/or save path + name (%%D %%N)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--target", type=Path, help="Organized-library directory")
    p.add_argument("--source", type=Path, help="Batch-scan directory (no paths given)")
    p.add_argument("--log", type=Path, help="Log file path")
    p.add_argument("--min-size", type=float, metavar="MB", help="Minimum movie size in MB")
    p.add_argument("--report", type=Path, help="Human-readable text report file (default: E:\\torrents\\tools\\ReportsAndLogs\\movie_standardizer\\movie_standardizer_report.txt)")
    p.add_argument("--lock-timeout", type=float, metavar="SECONDS", help="Maximum wait for another organizer run")
    p.add_argument(
        "--ffprobe", default=None, metavar="PATH",
        help="ffprobe used to verify same-cut technical upgrades before replacing an existing MKV",
    )
    p.add_argument(
        "--deduplicate", action="store_true",
        help="Scan the organized library for duplicate folders of the same movie",
    )
    p.add_argument(
        "--maintenance-mode", choices=("REPORT", "QUARANTINE", "DELETE"),
        help=("What to do with duplicate-maintenance candidates (needs --deduplicate): "
              "REPORT only logs them (default, non-destructive), QUARANTINE moves them "
              "outside the library, DELETE removes them"),
    )
    p.add_argument("--quarantine-dir", type=Path, metavar="DIR",
                   help="Destination outside the library for --maintenance-mode QUARANTINE")
    p.add_argument("--manifest", type=Path, metavar="PATH",
                   help="Optional JSON run manifest, outside --source and --target")
    p.add_argument("--allow-tv", action="store_true", help="Do not skip S01E02-style names")
    p.add_argument("--category", default="", help="qBittorrent %%L — skip if it looks like TV")
    p.add_argument("--dry-run", action="store_true", help="Log actions without writing")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--self-test", action="store_true", help="Run built-in tests and exit")
    return p

def cfg_from_args(args: argparse.Namespace) -> Config:
    cfg = Config()
    apply_env(cfg)
    if args.target:
        cfg.target_dir = args.target
    if args.source:
        cfg.source_dir = args.source
    if args.log:
        cfg.log_file = args.log
    if args.min_size is not None:
        cfg.min_movie_size_mb = args.min_size
    if args.report:
        cfg.report_file = args.report
    if args.lock_timeout is not None:
        cfg.lock_timeout_seconds = args.lock_timeout
    if args.ffprobe:
        cfg.ffprobe = args.ffprobe
    if args.deduplicate:
        cfg.enable_deduplication = True
    if args.maintenance_mode:
        cfg.maintenance_mode = args.maintenance_mode
    if args.quarantine_dir:
        cfg.quarantine_dir = args.quarantine_dir
    if args.manifest:
        cfg.manifest_file = args.manifest
    if args.allow_tv:
        cfg.skip_tv_shows = False
    cfg.dry_run = bool(args.dry_run)
    cfg.verbose = bool(args.verbose)
    cfg.maintenance_mode = cfg.maintenance_mode.upper()
    return cfg

_TV_CATEGORY_TOKENS = frozenset({
    "tv", "tvshow", "tvshows", "show", "shows", "series", "anime", "sonarr",
})

def _filesystem_device(path: Path) -> int:
    """Return the device ID for an existing ancestor of ``path``."""
    probe = path.resolve(strict=False)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return int(probe.stat().st_dev)

def validate_config(cfg: Config) -> list[str]:
    """Return actionable configuration errors before any filesystem mutation."""
    errors: list[str] = []
    if cfg.maintenance_mode not in {"REPORT", "QUARANTINE", "DELETE"}:
        errors.append(f"Unsupported maintenance mode: {cfg.maintenance_mode}")
    if cfg.maintenance_mode == "QUARANTINE" and cfg.quarantine_dir is None:
        errors.append("QUARANTINE maintenance mode requires --quarantine-dir")
    if cfg.min_movie_size_mb < 0:
        errors.append("--min-size must be zero or greater")
    if cfg.lock_timeout_seconds < 0:
        errors.append("--lock-timeout must be zero or greater")
    if paths_equal(cfg.source_dir, cfg.target_dir):
        errors.append("--source and --target must be different directories")
    elif path_is_within(cfg.target_dir, cfg.source_dir) or path_is_within(cfg.source_dir, cfg.target_dir):
        errors.append("--source and --target must not be nested to prevent batch self-processing")
    else:
        try:
            if _filesystem_device(cfg.source_dir) != _filesystem_device(cfg.target_dir):
                errors.append("--source and --target must be on the same filesystem for hardlink-only placement")
        except OSError as exc:
            errors.append(f"Could not verify source/target filesystem for hardlinks: {exc}")
    if cfg.target_dir.exists() and not cfg.target_dir.is_dir():
        errors.append("--target exists but is not a directory")
    if cfg.quarantine_dir is not None:
        if paths_equal(cfg.quarantine_dir, cfg.target_dir) or path_is_within(cfg.quarantine_dir, cfg.target_dir):
            errors.append("--quarantine-dir must be outside the organized library directory")
        if path_is_within(cfg.quarantine_dir, cfg.source_dir):
            errors.append("--quarantine-dir must be outside --source to prevent batch self-processing")
    if cfg.manifest_file is not None:
        if path_is_within(cfg.manifest_file, cfg.target_dir):
            errors.append("--manifest must be outside --target so media servers do not index it")
        if path_is_within(cfg.manifest_file, cfg.source_dir):
            errors.append("--manifest must be outside --source so batch scans do not treat it as input")
    if cfg.report_file is not None:
        if path_is_within(cfg.report_file, cfg.target_dir):
            errors.append("--report must be outside --target so media servers do not index it")
        if path_is_within(cfg.report_file, cfg.source_dir):
            errors.append("--report must be outside --source so batch scans do not treat it as input")
    if cfg.log_file is not None:
        if path_is_within(cfg.log_file, cfg.target_dir):
            errors.append("--log must be outside --target so media servers do not index it")
        if path_is_within(cfg.log_file, cfg.source_dir):
            errors.append("--log must be outside --source so batch scans do not treat it as input")
    return errors

def category_is_tv(category: str) -> bool:
    """True for TV-ish qBittorrent categories ("tv", "tv-sonarr", "series.anime")."""
    if not category:
        return False
    token = category.strip().lower()
    parts = [p for p in re.split(r"[\s._\-/]+", token) if p]
    return any(p in _TV_CATEGORY_TOKENS for p in parts)

def validate_automated_input(item_path: Path, cfg: Config) -> str | None:
    """Reject unsafe qBittorrent paths before the organizer touches media."""
    try:
        if not item_path.exists():
            return "qBittorrent input path does not exist"
        if item_path.is_symlink():
            return "qBittorrent input must not be a symlink"
        if path_is_within(item_path, cfg.target_dir):
            return "qBittorrent input is inside the organized library"
        if item_path.is_file() and item_path.suffix.lower() != CANONICAL_VIDEO_EXTENSION:
            return "qBittorrent input is not an MKV movie file"
    except OSError as exc:
        return f"could not validate qBittorrent input: {exc}"
    return None

def batch_scan(source: Path) -> None:
    LOG.info("--- Batch scan: %s ---", source)
    if not source.exists():
        LOG.error("Source folder does not exist: %s", source)
        record_outcome("failed", "batch scan", src=source, reason="source folder does not exist")
        return
    try:
        entries = sorted(source.iterdir(), key=lambda p: p.name.casefold())
    except OSError as exc:
        LOG.error("Cannot list %s: %s", source, exc)
        record_outcome("failed", "batch scan", src=source, reason=str(exc))
        return
    count = 0
    for item in entries:
        if is_skipped_junk_name(item.name):
            continue
        handle_item(item)
        count += 1
    LOG.info("--- Batch scan finished (%d items) ---", count)

def _print_banner() -> None:
    """One-screen summary of what this run will do, for the live terminal."""
    mode = "DRY-RUN (no files written)" if CFG.dry_run else "LIVE (hardlink into library)"
    print("=" * 70)
    print("MOVIE STANDARDIZER  —  qBittorrent completion hook")
    print("=" * 70)
    print(f"Mode      : {mode}")
    print(f"Source    : {CFG.source_dir}")
    print(f"Target    : {CFG.target_dir}")
    print(f"Log file  : {CFG.log_file}")
    print(f"Report    : {CFG.report_file}")
    print(f"Min size  : {CFG.min_movie_size_mb:.0f} MB   Skip TV: {CFG.skip_tv_shows}")
    print("=" * 70)
    print("Scanning for completed movie torrents to organize...\n", flush=True)

def run(args: argparse.Namespace) -> int:
    global CFG, RUN_SUMMARY, RUN_EVENTS
    CFG = cfg_from_args(args)
    RUN_SUMMARY = RunSummary()
    RUN_EVENTS = []
    setup_logging(CFG)
    _print_banner()

    errors = validate_config(CFG)
    if errors:
        for error in errors:
            LOG.error("Configuration error: %s", error)
            record_outcome("failed", "configuration", reason=error)
        write_manifest()
        write_report()
        return 2

    if args.category and category_is_tv(args.category):
        LOG.info("Skipping: qBittorrent category '%s' is TV", args.category)
        record_outcome("skipped", "category", reason="TV category")
        write_manifest()
        write_report()
        return 0

    # A deterministic temp lock serializes dry-runs and hardlink writes without
    # creating a target directory merely to lock.  The lock key is derived from
    # the normalized target path so the track cleaner and subtitle fetcher
    # coordinate on the very same file.
    try:
        with CoordinationLock(CFG.target_dir, timeout_seconds=CFG.lock_timeout_seconds):
            target = resolve_input_path(args.paths)
            if target is not None:
                reason = validate_automated_input(target, CFG)
                if reason:
                    LOG.error("Rejected automated input '%s': %s", target, reason)
                    record_outcome("failed", "automated input", src=target, reason=reason)
                else:
                    LOG.info("--- Automated run: %s ---", target)
                    handle_item(target)
                    deduplicate_movies(CFG.target_dir)
            else:
                if CFG.run_cleanup_on_target:
                    clean_existing_extras(CFG.target_dir)
                batch_scan(CFG.source_dir)
                deduplicate_movies(CFG.target_dir)
    except LockTimeoutError as exc:
        LOG.error("Organizer lock unavailable: %s", exc)
        record_outcome("failed", "lock", reason=str(exc))
    finally:
        write_manifest()
        write_report()
        LOG.info(
            "--- Run summary: attempted=%d completed=%d skipped=%d reported=%d quarantined=%d deleted=%d failed=%d ---",
            RUN_SUMMARY.attempted,
            RUN_SUMMARY.completed,
            RUN_SUMMARY.skipped,
            RUN_SUMMARY.reported,
            RUN_SUMMARY.quarantined,
            RUN_SUMMARY.deleted,
            RUN_SUMMARY.failed,
        )
    return 1 if RUN_SUMMARY.failed else 0

# =====================================================================
# SELF-TEST
# =====================================================================

def _assert_eq(actual, expected, label: str, errors: list[str]) -> None:
    if actual != expected:
        errors.append(f"{label}: got {actual!r} expected {expected!r}")

def run_canonical_self_tests() -> int:
    """Exercise the exact canonical-output contract in isolated temp folders."""
    global CFG, RUN_SUMMARY
    original_cfg = CFG
    root = Path(tempfile.mkdtemp(prefix="ms_canonical_"))
    src, dst = root / "source", root / "final_organized"
    src.mkdir()
    dst.mkdir()
    errors: list[str] = []
    try:
        # report_file=None on purpose: the default is a Windows path that
        # would materialize as a literal "E:\..." file in the CWD on POSIX.
        CFG = Config(
            source_dir=src,
            target_dir=dst,
            log_file=None,
            report_file=None,
            min_movie_size_mb=0,
            copy_extras=False,
            copy_artwork=False,
            run_cleanup_on_target=False,
            enable_deduplication=False,
        )
        RUN_SUMMARY = RunSummary()
        setup_logging(CFG)

        release = src / "Example.Film.2020.1080p.WEB-DL"
        release.mkdir()
        (release / "Example.Film.2020.1080p.WEB-DL.mkv").write_bytes(b"movie")
        (release / "Example.Film.2020.English.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nEnglish\n", encoding="utf-8"
        )
        (release / "Example.Film.2020.en.forced.ass").write_text("forced", encoding="utf-8")
        (release / "Example.Film.2020.Spanish.srt").write_text("spanish", encoding="utf-8")
        (release / "poster.jpg").write_bytes(b"art")
        (release / "Example.Film.2020-trailer.mkv").write_bytes(b"trailer")
        handle_directory(release)
        output_dir = dst / "Example Film (2020)"
        _assert_eq(
            sorted(path.name for path in output_dir.iterdir()) if output_dir.exists() else [],
            [
                f"Example Film (2020){EXTERNAL_SRT_SUFFIX}",
                "Example Film (2020).mkv",
            ],
            "exact canonical output",
            errors,
        )

        dual = src / "Dual.Film.2021"
        dual.mkdir()
        (dual / "Dual.Film.2021.720p.mkv").write_bytes(b"a" * 10)
        (dual / "Dual.Film.2021.1080p.mkv").write_bytes(b"b" * 20)
        handle_directory(dual)
        dual_out = dst / "Dual Film (2021)" / "Dual Film (2021).mkv"
        _assert_eq(dual_out.read_bytes() if dual_out.exists() else b"", b"b" * 20, "largest MKV only", errors)

        (src / "Unsupported.Film.2022.mp4").write_bytes(b"mp4")
        handle_single_file(src / "Unsupported.Film.2022.mp4")
        parts = src / "Parts"
        parts.mkdir()
        (parts / "Parts.Film.2023.cd1.mkv").write_bytes(b"one")
        (parts / "Parts.Film.2023.cd2.mkv").write_bytes(b"two")
        handle_directory(parts)
        disc = src / "Disc"
        (disc / "BDMV" / "STREAM").mkdir(parents=True)
        (disc / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"disc")
        handle_directory(disc)
        if (dst / "Unsupported Film (2022)").exists() or (dst / "Parts Film (2023)").exists() or (dst / "Disc").exists():
            errors.append("unsupported MP4, multipart, or disc release was emitted")

        _assert_eq(is_english_subtitle(Path("Film.English.srt")), True, "english subtitle", errors)
        _assert_eq(is_english_subtitle(Path("Film.en.sdh.srt")), True, "english SDH subtitle", errors)
        _assert_eq(is_english_subtitle(Path("Film.Spanish.srt")), False, "non-English subtitle", errors)
        _assert_eq(parse_movie_name("The.Matrix.1999.1080p.mkv").file_stem(), "The Matrix (1999)", "canonical filename", errors)

        guard_src = src / "Guard.2019.mkv"
        guard_src.write_bytes(b"source-replacement")
        guard_dest = dst / "Guard (2019)" / "Guard (2019).mkv"
        guard_dest.parent.mkdir()
        guard_dest.write_bytes(b"destination")
        real_replace = os.replace
        real_upgrade_decision = globals()["_movie_upgrade_decision"]
        try:
            # This test isolates atomic activation failure. Duplicate identity
            # and quality policy is covered separately by the unit suite.
            globals()["_movie_upgrade_decision"] = lambda *_args: (True, "self-test upgrade")
            os.replace = lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("locked"))
            if process_file_action(guard_src, guard_dest):
                errors.append("locked destination replacement unexpectedly succeeded")
        finally:
            os.replace = real_replace
            globals()["_movie_upgrade_decision"] = real_upgrade_decision
        _assert_eq(guard_src.read_bytes(), b"source-replacement", "failed replacement keeps source", errors)
        _assert_eq(guard_dest.read_bytes(), b"destination", "failed replacement keeps destination", errors)
    finally:
        CFG = original_cfg
        shutil.rmtree(root, ignore_errors=True)

    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print("  -", error)
        return 1
    print("SELF-TEST PASSED (canonical MKV + English subtitles + skip and safety guards)")
    return 0

# =====================================================================
# ENTRY
# =====================================================================

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return run_canonical_self_tests()
    try:
        enable_utf8_stdio()
        return run(args)
    except KeyboardInterrupt:
        LOG.warning("Interrupted")
        return 130
    except Exception:  # noqa: BLE001 - last-resort crash handler must catch everything
        # Last-resort logging so a qBittorrent-launched crash is not silent.
        try:
            LOG.exception("Fatal error")
        except Exception:  # noqa: BLE001 - logging itself is failing
            traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
