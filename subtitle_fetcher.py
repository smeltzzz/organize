#!/usr/bin/env python3
"""
English Subtitle Fetcher for Jellyfin Movies
============================================
After ``movie_standardizer.py`` and before ``mkv_track_cleaner.py``: walk the
canonical movie library and create at most one validated external English SRT
sidecar per MKV. This single script owns its persistent UTC request ledger;
there is no separate queue script or launcher to run.

OpenSubtitles and SubDL are treated as equal sources. Both providers'
release-identifying routes are consulted for every movie - the exact
OpenSubtitles moviehash and SubDL's score-gated release-aware filename
match (score >= 0.80) - and the qualifying release with the most downloads
is downloaded, whichever provider it came from. When neither release route
yields a pick, both providers' strict title/year routes are pooled the same
way. A wrong cut or a tie the quality signals cannot break is held for
review rather than downloaded.

When every API source misses, the fetcher does not stop: seven scraping
sources are consulted in a fixed failover order - Subf2me, Podnapisi,
Addic7ed, SubSource, Subsunacs, YIFY Subtitles, and Subs.Sab.BZ - vendored
in the scraping-sources section of this file (Python standard library only,
no keys, no accounts). A scraped candidate is only accepted when it names
the movie, matches its release year, and decodes to a valid English SRT;
each source carries a per-run circuit breaker and a UTC daily search cap so
one dead or hostile site can never stall the library. The product goal is a
validated English SRT beside every movie: movies that still lack one are
listed by name in the report, retried on the next UTC day, and make the
process exit non-zero (override with --allow-missing) until they are
covered.

A candidate is auto-selected only when its release name carries the movie
title, the release year, and an explicit Blu-ray keyword (``BluRay``,
``Blu-ray``, ``BLU RAY``, ...). Among the qualifying candidates the one
with the highest download count wins; the trusted flag, community rating and
votes remain as tiebreakers, and a tie they cannot break is held for manual
review. There is no separate rating/votes quality floor, so popular but
unvoted subtitles for big-name movies are fetched automatically.

The position in the pipeline is deliberate, not cosmetic. The moviehash is the
file size plus the sum of the first and last 64 KiB, and this tool submits it
with ``moviehash_match=only`` so the provider returns only subtitles uploaded
against a byte-identical release. ``mkv_track_cleaner.py`` rewrites those bytes,
so any movie that is remuxed first can never reproduce its release hash again
and is silently reduced to the far weaker title/year fallback. Fetching first
keeps the pristine release hash available while it still exists.

    py -3 subtitle_fetcher.py --dry-run
    py -3 subtitle_fetcher.py
    py -3 subtitle_fetcher.py --self-test

The default policy intentionally downloads only UTF-8 SRT sidecars. SRT is the
most broadly direct-play-safe external subtitle choice across Jellyfin clients;
ASS/SSA, VobSub, PGS, and other formats are never requested or written here.

Configure one or both API providers through environment variables (the
scraping sources need no credentials at all):
    set OPENSUBTITLES_API_KEY=...
    set SUBDL_API_KEY=...

Credentials are read only from environment variables, never command-line
arguments. Development-anonymous mode uses only the OpenSubtitles API key for
consumers that OpenSubtitles currently permits to download anonymously.
Authenticated user mode remains available as an explicit fallback. A run with
no API keys configured still works: every movie is offered to the scraping
sources instead.

OpenSubtitles key: https://www.opensubtitles.com/en/consumers
SubDL key: https://subdl.com/panel/api
"""

from __future__ import annotations

import argparse
import errno
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import textwrap
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

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

LEGACY_EXTERNAL_SRT_SUFFIX = ".en.srt"

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

def report_banner(
    title: str,
    subtitle: str = "",
    meta: Iterable[tuple[str, object]] = (),
    *,
    width: int = REPORT_WIDTH,
) -> str:
    """The boxed header on its own, for a tool's startup print."""
    report = Report(title, subtitle, width=width)
    report.metas(meta)
    return report.render_header()

# =============================================================================
# CONFIGURATION

# =============================================================================
# SCRAPING FALLBACK SOURCES (vendored)
#
# Tier 3: seven scraping subtitle sources, consulted in fixed failover
# order when the OpenSubtitles/SubDL API tiers miss. Originally developed
# as the standalone module subtitle_sources.py; vendored here so the
# fetcher remains one self-contained file. Standard library only, no keys,
# no accounts. Adapters raise ScrapeSourceError (hard failure) or
# CandidateRejected (soft refusal); ScrapeChain adds the per-run circuit
# breakers, the durable UTC search caps (reserve_cb), and failover.
# =============================================================================

import html as _html  # used by the vendored section below



from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDER_SUBF2ME = "subf2me"
PROVIDER_PODNAPISI = "podnapisi"
PROVIDER_ADDIC7ED = "addic7ed"
PROVIDER_SUBSOURCE = "subsource"
PROVIDER_SUBSUNACS = "subsunacs"
PROVIDER_YIFY = "yifysubtitles"
PROVIDER_SUBSAB = "subsab"

#: Execution order for the failover chain. API sources (OpenSubtitles, SubDL)
#: run first in subtitle_fetcher.py; this is the order of the scraped chain.
SCRAPE_PROVIDER_ORDER: tuple[str, ...] = (
    PROVIDER_SUBF2ME,
    PROVIDER_PODNAPISI,
    PROVIDER_ADDIC7ED,
    PROVIDER_SUBSOURCE,
    PROVIDER_SUBSUNACS,
    PROVIDER_YIFY,
    PROVIDER_SUBSAB,
)

SCRAPE_PROVIDER_LABELS: dict[str, str] = {
    PROVIDER_SUBF2ME: "Subf2m.co",
    PROVIDER_PODNAPISI: "Podnapisi.NET",
    PROVIDER_ADDIC7ED: "Addic7ed.com",
    PROVIDER_SUBSOURCE: "SubSource.net",
    PROVIDER_SUBSUNACS: "Subsunacs.net",
    PROVIDER_YIFY: "YIFY Subtitles",
    PROVIDER_SUBSAB: "Subs.sab.bz",
}

#: Polite default: search requests per UTC day per scraped source.
DEFAULT_SEARCH_DAILY_CAP = 20

#: A source with this many consecutive hard failures is disabled for the run.
BREAKER_HARD_FAILURES = 3
#: A source whose structure parsing keeps failing is disabled too.
BREAKER_PARSE_FAILURES = 3

SCRAPE_HTTP_TIMEOUT_SEC = 20.0
SCRAPE_REQUEST_GAP_SEC = 1.0
SCRAPE_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SCRAPE_MAX_CANDIDATES_PER_SOURCE = 3

SCRAPE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScrapeSourceError(RuntimeError):
    """Hard failure against one source (network, HTTP, structure, archive).

    Counts toward the source's circuit breaker.
    """


class CandidateRejected(RuntimeError):
    """A specific candidate was inspected and refused (soft miss).

    Does not count toward the breaker: the chain simply tries the next
    candidate. Example: a subsunacs subtitle page that turns out to be
    Bulgarian, or a download whose bytes are not an SRT.
    """


class SourceUnavailable(RuntimeError):
    """The chain refused to work a source this run (breaker open or the
    source's UTC daily search cap is exhausted)."""


# ---------------------------------------------------------------------------
# Identity / candidate types (local to avoid a circular import)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceIdentity:
    """Canonical movie identity derived from a ``Title (Year)`` filename."""

    title: str
    year: int
    normalized_title: str = ""


@dataclass
class ScrapeCandidate:
    """One addressable subtitle on one source, before acceptance checks.

    ``file_id`` is the source-specific reference the adapter's ``fetch``
    understands (an id, a URL path, or an attach id). ``downloads`` and
    ``rating`` are best-effort popularity signals used for ordering.
    """

    provider: str
    file_id: str
    release: str = ""
    feature_title: str = ""
    feature_year: int = 0
    downloads: int = 0
    rating: float = 0.0
    hearing_impaired: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Transport seam (stdlib urllib by default; tests inject a fake)
# ---------------------------------------------------------------------------


class ScrapeTransport:
    """Small HTTP client seam shared by every adapter.

    ``get``/``post`` return raw bytes and raise :class:`ScrapeSourceError`
    for anything that is not a clean 2xx response within the size limit.
    A per-instance throttle keeps request rates under the polite gap.
    """

    def __init__(self, *, timeout: float = SCRAPE_HTTP_TIMEOUT_SEC,
                 gap: float = SCRAPE_REQUEST_GAP_SEC,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.timeout = timeout
        self.gap = gap
        self._sleep = sleep
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self.gap - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()

    def _open(self, url: str, data: bytes | None, headers: dict[str, str]) -> bytes:
        base = {
            "User-Agent": SCRAPE_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        base.update(headers or {})
        req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST",
                                     headers=base)
        try:
            # URLs here are fixed provider endpoints (see the adapter that
            # built them); user-controlled data only ever appears in a
            # percent-encoded query string or POST body.
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
                status = getattr(resp, "status", 200)
                if not (200 <= int(status) < 300):
                    raise ScrapeSourceError(f"HTTP {status} for {urllib.parse.urlsplit(url).path}")
                raw = resp.read(SCRAPE_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ScrapeSourceError(f"HTTP {exc.code} for {urllib.parse.urlsplit(url).path}") from exc
        except urllib.error.URLError as exc:
            raise ScrapeSourceError(f"network error for {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise ScrapeSourceError(f"transport error for {urllib.parse.urlsplit(url).netloc}: {exc}") from exc
        if len(raw) > SCRAPE_MAX_RESPONSE_BYTES:
            raise ScrapeSourceError("response exceeds the size limit")
        return raw

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> bytes:
        self._throttle()
        return self._open(url, None, headers or {})

    def post(self, url: str, form: dict[str, str], *, headers: dict[str, str] | None = None) -> bytes:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
        hdrs.update(headers or {})
        self._throttle()
        return self._open(url, data, hdrs)


def default_transport() -> ScrapeTransport:
    return ScrapeTransport()


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def unescape(text: str) -> str:
    return _html.unescape(text or "")


def strip_tags(fragment: str) -> str:
    return re.sub(r"<[^>]+>", " ", fragment or "")


def scrape_normalize_title(text: str) -> str:
    """Lowercase, de-accent-free token set used for title comparisons."""
    value = unescape(text or "").casefold()
    value = re.sub(r"\(hearing impaired\)|\[hi\]|\(hi\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    return re.sub(r"\s+", " ", value)


def title_tokens(text: str) -> frozenset[str]:
    return frozenset(scrape_normalize_title(text).split())


def title_similarity(a: str, b: str) -> float:
    """Token-overlap similarity in [0, 1]; containment scores 1.0."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb or ta <= tb or tb <= ta:
        return 1.0
    return len(ta & tb) / min(len(ta), len(tb))


def titles_match(a: str, b: str, *, threshold: float = 0.6) -> bool:
    if not a or not b:
        return False
    return title_similarity(a, b) >= threshold


def looks_like_srt_text(text: str) -> bool:
    """At least one well-formed cue: index line + ``HH:MM:SS,mmm --> ...``."""
    if not text or len(text) > 4 * 1024 * 1024:
        return False
    return bool(re.search(
        r"(?m)^\s*\d{1,6}\s*\r?\n\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}",
        text,
    ))


def decode_scrape_subtitle_bytes(raw: bytes) -> str:
    """utf-8-sig, then utf-8, then cp1252 — the shared sidecar contract."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\ufffd" in text:
            continue
        return text
    raise ScrapeSourceError("subtitle bytes are not decodable text (not a subtitle?)")


def mostly_cyrillic(text: str) -> bool:
    """Heuristic language guard for sources that expose no language metadata.

    True when the letter content is dominated by Cyrillic: a Bulgarian (or
    any Cyrillic) subtitle must never be installed as the English sidecar.
    """
    cyr = lat = 0
    for ch in text:
        if "\u0400" <= ch <= "\u04FF":
            cyr += 1
        elif ch.isalpha() and ord(ch) < 0x0250:
            lat += 1
    if cyr + lat < 8:
        return False
    return cyr > 0.3 * (cyr + lat)


def slugify(title: str) -> str:
    """The SubSource-style slug: lowercase, apostrophes dropped, runs of
    anything non-alphanumeric collapsed to a single hyphen."""
    value = unescape(title or "").casefold().replace("'", "").replace("\u2019", "")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return re.sub(r"-{2,}", "-", value)


def first_bytes_are_zip(raw: bytes) -> bool:
    return raw[:4] in (b"PK\x03\x04", b"PK\x05\x06")


def pick_zip_subtitle(raw: bytes) -> bytes:
    """Extract the SRT payload from a one-file subtitle archive.

    Prefers an entry whose name advertises UTF-8 (Subf2m ships a UTF-8 and a
    non-UTF-8 copy in the same zip), then the first .srt, then the first
    entry. Raises ScrapeSourceError for non-zips and unreadable archives.
    """
    if not first_bytes_are_zip(raw):
        raise ScrapeSourceError("expected a subtitle archive, got a non-zip payload")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            if not names:
                raise ScrapeSourceError("subtitle archive is empty")
            utf_entry = next((n for n in names if "utf" in n.casefold()), None)
            srt_entry = next((n for n in names if n.casefold().endswith(".srt")), None)
            chosen = utf_entry or srt_entry or names[0]
            return zf.read(chosen)
    except zipfile.BadZipFile as exc:
        raise ScrapeSourceError(f"unreadable subtitle archive: {exc}") from exc


def valid_srt_bytes(raw: bytes) -> bool:
    if not raw or len(raw) > 4 * 1024 * 1024:
        return False
    try:
        return looks_like_srt_text(decode_scrape_subtitle_bytes(raw))
    except ScrapeSourceError:
        return False


def absolute_url(base: str, value: str) -> str:
    value = (value or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    if not value.startswith("/"):
        value = "/" + value
    return base.rstrip("/") + value


# ---------------------------------------------------------------------------
# Base adapter
# ---------------------------------------------------------------------------


class BaseSource:
    """One community subtitle source.

    ``search`` returns candidates (metadata only, no download). ``fetch``
    retrieves one candidate's payload bytes; it must raise
    :class:`CandidateRejected` for "wrong subtitle" outcomes and
    :class:`ScrapeSourceError` for "source is broken" outcomes.
    """

    key: str = ""
    label: str = ""

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        raise NotImplementedError

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. Subf2m.co
# ---------------------------------------------------------------------------


class Subf2meSource(BaseSource):
    """Subf2m.co: title search (language-scoped), movie page, zipped SRT.

    Verified against the site's own structure (as consumed by the Emby
    Subf2m plugin): ``/subtitles/searchbytitle?query=..&l=en`` returns a
    ``div.search-result`` whose ``ul`` lists ``Title (YYYY)`` links; the
    movie page (``<link>/<lang>``) holds ``li.item`` rows with
    ``a.download.icon-download`` links; the download page carries a
    ``div.download`` link to a zip.
    """

    key = PROVIDER_SUBF2ME
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_SUBF2ME]
    BASE = "https://subf2m.co"

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        url = f"{self.BASE}/subtitles/searchbytitle?query={urllib.parse.quote_plus(identity.title)}&l=en"
        page = t.get(url)
        text = page.decode("utf-8", errors="replace")
        marker = re.search(r"<div[^>]*class=[\"'][^\"']*search-result[^\"']*[\"']", text, re.I)
        if not marker:
            return []
        region = text[marker.start():]
        ul = re.search(r"<ul.*?</ul>", region, re.S | re.I)
        if ul:
            region = ul.group(0)
        else:
            region = region[:4000]
        cands: list[ScrapeCandidate] = []
        for href, inner in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", region, re.S | re.I):
            inner_text = unescape(strip_tags(inner))
            year = re.search(r"\((\d{4})\)", inner_text)
            if not year or int(year.group(1)) != identity.year:
                continue
            title = re.sub(r"\s*\(\d{4}\)\s*$", "", inner_text).strip()
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=href, release=title,
                feature_title=title, feature_year=int(year.group(1)),
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        movie_page_url = absolute_url(self.BASE, candidate.file_id)
        if not movie_page_url.rstrip("/").endswith("/en"):
            movie_page_url = movie_page_url.rstrip("/") + "/en"
        page = t.get(movie_page_url).decode("utf-8", errors="replace")
        download_href: str | None = None
        # Split on the item markers (nested <li> children make a simple
        # (.*?)</li> capture stop at the wrong closing tag); each segment is
        # one row's subtree, which holds its own download anchor.
        segments = re.split(r"<li[^>]*class=[\"'][^\"']*item[^\"']*[\"'][^>]*>", page, flags=re.I)
        for block in segments[1:]:
            m = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*class=[\"'][^\"']*download[^\"']*[\"']", block, re.I) \
                or re.search(r"<a[^>]+class=[\"'][^\"']*download[^\"']*[\"'][^>]*href=[\"']([^\"']+)[\"']", block, re.I)
            if m:
                download_href = m.group(1)
                break
        if not download_href:
            raise ScrapeSourceError("no download rows on the movie page")
        dl_page = t.get(absolute_url(self.BASE, download_href)).decode("utf-8", errors="replace")
        dl_div = re.search(r"<div[^>]+class=[\"'][^\"']*download[^\"']*[\"'][^>]*>(.*?)</div>", dl_page, re.S | re.I)
        scope = dl_div.group(1) if dl_div else dl_page
        m = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"']", scope, re.I)
        if not m:
            raise ScrapeSourceError("no download link on the download page")
        raw = t.get(absolute_url(self.BASE, m.group(1)))
        return pick_zip_subtitle(raw)


# ---------------------------------------------------------------------------
# 2. Podnapisi.NET
# ---------------------------------------------------------------------------


class PodnapisiSource(BaseSource):
    """Podnapisi.NET's documented JSON advanced-search (movies only).

    ``GET /subtitles/search/advanced?keywords=..&language=en&movie_type=movie
    &year=..`` returns ``{"data": [{id, releases[], custom_releases[],
    movie:{title, year}}], "page", "all_pages"}``. Download:
    ``GET /subtitles/<id>/download?container=zip`` (single-file zip).
    """

    key = PROVIDER_PODNAPISI
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_PODNAPISI]
    BASE = "https://www.podnapisi.net/subtitles"

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        params = {
            "keywords": identity.title,
            "language": "en",
            "movie_type": "movie",
            "year": str(identity.year),
        }
        cands: list[ScrapeCandidate] = []
        seen: set[str] = set()
        for page_no in (1, 2):  # the site paginates; two pages are plenty
            params["page"] = str(page_no)
            payload = json.loads(t.get(f"{self.BASE}/search/advanced?{urllib.parse.urlencode(params)}").decode("utf-8", "replace"))
            data = payload.get("data") or []
            if not isinstance(data, list):
                raise ScrapeSourceError("unexpected search payload shape")
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                pid = entry.get("id")
                if pid is None or str(pid) in seen:
                    continue
                seen.add(str(pid))
                movie = entry.get("movie") or {}
                try:
                    year = int(movie.get("year") or 0)
                except (TypeError, ValueError):
                    year = 0
                if year and year != identity.year:
                    continue
                releases = list(entry.get("releases") or []) + list(entry.get("custom_releases") or [])
                cands.append(ScrapeCandidate(
                    provider=self.key, file_id=str(pid),
                    release=next((str(r) for r in releases if str(r).strip()), ""),
                    feature_title=str(movie.get("title") or ""),
                    feature_year=year,
                ))
                if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                    break
            try:
                if int(payload.get("page") or 1) >= int(payload.get("all_pages") or 1):
                    break
            except (TypeError, ValueError):
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        raw = t.get(f"{self.BASE}/{candidate.file_id}/download?container=zip")
        return pick_zip_subtitle(raw)


# ---------------------------------------------------------------------------
# 3. Addic7ed.com
# ---------------------------------------------------------------------------


class Addic7edSource(BaseSource):
    """Addic7ed.com movies: ``srch.php`` search, movie page, gated SRT.

    Verified against the site's current layout (as consumed by the
    addic7ed-api scraper): the search page lists ``href="movie/<id>"`` for
    movie hits; the movie page contains ``Version <release>,`` blocks whose
    rows pair ``td.language`` text with a ``Download``/``most updated``
    anchor (``a.buttonDownload``) and an ``N Downloads`` count. Only
    *Completed* subtitles carry a working download link; the download
    requires a ``Referer`` pointing at the show page.
    """

    key = PROVIDER_ADDIC7ED
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_ADDIC7ED]
    BASE = "https://www.addic7ed.com"

    def _headers(self) -> dict[str, str]:
        return {"Referer": self.BASE}

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        url = f"{self.BASE}/srch.php?search={urllib.parse.quote_plus(identity.title)}&Submit=Search"
        body = t.get(url, headers=self._headers()).decode("utf-8", errors="replace")
        if re.search(r"<b>\s*0\s+results\s+found\s*</b>", body, re.I):
            return []
        movie_links = re.findall(r'href="(movie/\d+)"', body)
        if not movie_links:
            return []
        movie_html = t.get(f"{self.BASE}/{movie_links[0]}", headers=self._headers()).decode("utf-8", errors="replace")
        referer_m = re.search(r"/show/\d+", movie_html)
        referer = f"{self.BASE}{referer_m.group(0)}" if referer_m else f"{self.BASE}/show/1"
        header_m = re.search(r"(?P<title>.*?)\s*\((?P<year>\d{4})\)\s*<small", movie_html, re.S)
        header_title = re.sub(r"\s+", " ", unescape(strip_tags(header_m.group("title")))).strip() if header_m else ""
        try:
            header_year = int(header_m.group("year")) if header_m else 0
        except ValueError:
            header_year = 0
        cands: list[ScrapeCandidate] = []
        version_re = re.compile(r"Version\s+([^,<]+),")
        # Window-based row parsing: layout details (which cells carry
        # anchors, in what order) shift over time, so each language cell is
        # inspected inside its own bounded window instead of with one long
        # all-in-one pattern.
        for lm in re.finditer(r'class="language"[^>]*>', movie_html):
            end = movie_html.find('class="language"', lm.end())
            window = movie_html[lm.end(): end if end != -1 else lm.end() + 3000]
            text = unescape(strip_tags(window))
            # The language name precedes the completion status in the row.
            pre_status = text.split("Completed", 1)[0].strip()
            lang_name = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", pre_status).strip()
            if lang_name.casefold() != "english":
                continue
            if re.search(r"%\s*Completed", text, re.I):
                continue  # "% Completed" rows are not downloadable
            if not re.search(r"Completed", text, re.I):
                continue
            dl = re.search(r'href="([^"]+?)"[^>]*>\s*<strong>\s*(?:most updated|Download)', window, re.I)
            if not dl:
                continue
            dl_count = re.search(r"(\d+)\s*Downloads", text)
            pre_versions = list(version_re.finditer(movie_html[: lm.start()]))
            release = pre_versions[-1].group(1).strip() if pre_versions else ""
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=dl.group(1), release=release,
                feature_title=header_title or identity.title,
                feature_year=header_year or identity.year,
                downloads=int(dl_count.group(1)) if dl_count else 0,
                hearing_impaired="hearing impaired" in pre_status.casefold(),
                extra={"referer": referer},
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        url = absolute_url(self.BASE, candidate.file_id)
        headers = {"Referer": str(candidate.extra.get("referer") or f"{self.BASE}/show/1")}
        raw = t.get(url, headers=headers)
        if not valid_srt_bytes(raw):
            raise CandidateRejected("addic7ed payload is not a valid SRT")
        return raw


# ---------------------------------------------------------------------------
# 4. SubSource.net
# ---------------------------------------------------------------------------


class SubSourceSource(BaseSource):
    """SubSource.net (the Subscene-successor catalog).

    Deterministic slugs (``/subtitles/<slug>-<year>``) are tried first,
    falling back to the public search page (``/search?q=<title>``). The
    movie page lists one row per subtitle file with a language anchor
    (``/subtitle/<slug>/english/<id>``); that file page carries the direct
    API download link (``api.subsource.net/v1/subtitle/download/<hash>``).
    """

    key = PROVIDER_SUBSOURCE
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_SUBSOURCE]
    BASE = "https://subsource.net"

    def _movie_page_candidates(self, page: bytes, identity: SourceIdentity) -> list[ScrapeCandidate]:
        text = page.decode("utf-8", errors="replace")
        cands: list[ScrapeCandidate] = []
        seen: set[str] = set()
        for path in re.findall(r'href="(/subtitle/[^"]+/english/(\d+))"', text):
            href = path[0]
            if href in seen:
                continue
            seen.add(href)
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=href,
                feature_title=identity.title, feature_year=identity.year,
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE:
                break
        return cands

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        slug = slugify(identity.title)
        direct = f"{self.BASE}/subtitles/{slug}-{identity.year}"
        try:
            page = t.get(direct)
        except ScrapeSourceError:
            page = b""
        if page:
            cands = self._movie_page_candidates(page, identity)
            if cands:
                return cands
        search_page = t.get(f"{self.BASE}/search?q={urllib.parse.quote_plus(identity.title)}")
        text = search_page.decode("utf-8", errors="replace")
        slugs = set(re.findall(r'href="(/subtitles/[a-z0-9\-]+)"', text))
        wanted = f"/subtitles/{slug}-{identity.year}"
        cands: list[ScrapeCandidate] = []
        if wanted in slugs:
            cands.extend(self._movie_page_candidates(t.get(f"{self.BASE}{wanted}"), identity))
        for other in sorted(s for s in slugs if s.endswith(f"-{identity.year}")):
            if other == wanted or len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE:
                continue
            title_guess = other.rsplit("/", 1)[-1][: -len(f"-{identity.year}")].replace("-", " ")
            if not titles_match(title_guess, identity.title):
                continue
            try:
                cands.extend(self._movie_page_candidates(t.get(f"{self.BASE}{other}"), identity))
            except ScrapeSourceError:
                continue
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        page = t.get(f"{self.BASE}{candidate.file_id}").decode("utf-8", errors="replace")
        m = re.search(r"(https://api\.subsource\.net/v1/subtitle/download/[A-Za-z0-9]+)", page)
        if not m:
            raise ScrapeSourceError("no API download link on the subtitle page")
        raw = t.get(m.group(1))
        if not first_bytes_are_zip(raw):
            if valid_srt_bytes(raw):
                return raw
            raise CandidateRejected("subsource payload is not a valid SRT")
        return pick_zip_subtitle(raw)


# ---------------------------------------------------------------------------
# 5. Subsunacs.net
# ---------------------------------------------------------------------------


class SubsunacsSource(BaseSource):
    """Subsunacs.net: POST search, per-candidate language verification.

    The search form (``search.php``) takes ``m`` (title), ``y`` (year) and
    ``l`` (language: 0 = all). Results are ``/subtitles/<Name>-<id>/`` rows
    with a ``(YYYY)`` year span. Because the search cannot be scoped to
    English reliably, each candidate's subtitle page is re-checked before
    any download: the page states ``Език: <language>`` and repeats the
    title and year, and hosts the direct SRT entry
    (``getentry.php?id=<id>&ei=0``).
    """

    key = PROVIDER_SUBSUNACS
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_SUBSUNACS]
    BASE = "https://subsunacs.net"

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        form = {"m": identity.title, "y": str(identity.year), "l": "0", "t": "Submit"}
        page = t.post(f"{self.BASE}/search.php", form).decode("utf-8", errors="replace")
        cands: list[ScrapeCandidate] = []
        seen: set[str] = set()
        for href, inner, year in re.findall(
            r'<a[^>]+href="(/subtitles/[^"]+/)"[^>]*>(.*?)</a>\s*(?:<[^>]+>)?\((\d{4})\)',
            page, re.S,
        ):
            if href in seen:
                continue
            seen.add(href)
            title = unescape(strip_tags(inner)).strip()
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=href, release=title,
                feature_title=title, feature_year=int(year),
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        page = t.get(f"{self.BASE}{candidate.file_id}").decode("utf-8", errors="replace")
        lang_m = re.search(r"Език:\s*([^/]+)", page)
        if lang_m:
            lang_text = unescape(lang_m.group(1)).strip()
            if "англ" not in lang_text.casefold() and "english" not in lang_text.casefold():
                raise CandidateRejected(f"subsunacs subtitle is not English ({lang_text})")
        head_m = re.search(r"<h1[^>]*>(.*?)\s*\((\d{4})\)", page, re.S)
        if head_m:
            head_title = unescape(strip_tags(head_m.group(1))).strip()
            head_year = int(head_m.group(2))
            if candidate.feature_year and head_year != candidate.feature_year:
                raise CandidateRejected("subsunacs page year does not match the search row")
            if candidate.feature_title and not titles_match(head_title, candidate.feature_title):
                raise CandidateRejected("subsunacs page title does not match the search row")
        entry_m = re.search(
            r'href="((?:https://subsunacs\.net)?/getentry\.php\?id=\d+&(?:amp;)?ei=0)"', page)
        if not entry_m:
            raise ScrapeSourceError("no archive entry on the subtitle page")
        raw = t.get(absolute_url(self.BASE, entry_m.group(1)))
        if not valid_srt_bytes(raw):
            raise CandidateRejected("subsunacs payload is not a valid SRT")
        return raw


# ---------------------------------------------------------------------------
# 6. YIFY Subtitles
# ---------------------------------------------------------------------------


class YifySubtitlesSource(BaseSource):
    """YIFY Subtitles (yifysubtitles.ch — the current YTS/YIFY domain).

    ``/search?q=<title>`` returns ``div.media-body`` result cards carrying
    an ``h3[itemprop=name]`` title, a ``span.movinfo-section`` year and the
    movie link. The movie page lists subtitle rows (``tr[data-id]``) with
    ``span.sub-lang``, a rating cell and the ``/subtitles/...`` address;
    the download is the same address with ``/subtitles/`` rewritten to
    ``/subtitle/`` plus ``.zip``.
    """

    key = PROVIDER_YIFY
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_YIFY]
    BASE = "https://yifysubtitles.ch"

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        page = t.get(f"{self.BASE}/search?q={urllib.parse.quote_plus(identity.title)}").decode("utf-8", errors="replace")
        cands: list[ScrapeCandidate] = []
        seen: set[str] = set()
        for chunk in re.split(r"<div[^>]+class=[\"']media-body[\"']", page)[1:]:
            chunk = chunk[:4000]
            title_m = re.search(r"<h3[^>]*itemprop=[\"']name[\"'][^>]*>(.*?)</h3>", chunk, re.S)
            year_m = re.search(r"<span[^>]*class=[\"']movinfo-section[\"'][^>]*>\s*(\d{4})", chunk)
            href_m = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"']", chunk)
            if not (title_m and year_m and href_m):
                continue
            href = href_m.group(1)
            if href in seen:
                continue
            seen.add(href)
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=href,
                release=unescape(strip_tags(title_m.group(1))).strip(),
                feature_title=unescape(strip_tags(title_m.group(1))).strip(),
                feature_year=int(year_m.group(1)),
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        page = t.get(absolute_url(self.BASE, candidate.file_id)).decode("utf-8", errors="replace")
        best_href: str | None = None
        best_rating = -1.0
        for row in re.findall(r"<tr data-id=[\"'][^\"']*[\"']>(.*?)(?:</tr>|$)", page, re.S):
            lang_m = re.search(r"<span[^>]*class=[\"']sub-lang[\"'][^>]*>([^<]+)</span>", row, re.I)
            if not lang_m or lang_m.group(1).strip().casefold() != "english":
                continue
            cell_m = re.search(r"<td[^>]*class=[\"']rating-cell[\"'][^>]*>(.*?)(?:</td>|$)", row, re.S)
            numbers = re.findall(r"-?\d+(?:\.\d+)?", cell_m.group(1)) if cell_m else []
            try:
                rating = float(numbers[-1]) if numbers else 0.0
            except ValueError:
                rating = 0.0
            if rating < 0:
                continue
            href_m = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"']", row, re.I)
            if not href_m:
                continue
            if rating > best_rating:
                best_rating, best_href = rating, href_m.group(1)
        if not best_href:
            raise CandidateRejected("no English subtitle rows on the YIFY movie page")
        zip_url = absolute_url(self.BASE, best_href).replace("/subtitles/", "/subtitle/") + ".zip"
        raw = t.get(zip_url)
        return pick_zip_subtitle(raw)


# ---------------------------------------------------------------------------
# 7. Subs.sab.bz
# ---------------------------------------------------------------------------


class SubsSabSource(BaseSource):
    """Subs.sab.bz (Bulgarian-era catalog that still carries English subs).

    The search form (``index.php?``) takes ``movie`` + ``yr``; results are
    rows with ``attach_id=<n>`` download links and a ``(YYYY)`` year. The
    site exposes no per-row language metadata we can trust, so every
    downloaded payload is language-guarded (Cyrillic-dominant content is
    rejected) before it may become a sidecar.
    """

    key = PROVIDER_SUBSAB
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_SUBSAB]
    BASE = "http://subs.sab.bz"

    def _headers(self) -> dict[str, str]:
        return {"Referer": f"{self.BASE}/index.php?"}

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        form = {"movie": identity.title, "act": "search", "select-language": "1",
                "upldr": "", "yr": str(identity.year), "release": ""}
        page = t.post(f"{self.BASE}/index.php?", form, headers=self._headers()).decode("utf-8", errors="replace")
        cands: list[ScrapeCandidate] = []
        seen: set[str] = set()
        for m in re.finditer(r'href="[^"]*attach_id=(\d+)[^"]*"', page):
            attach_id = m.group(1)
            if attach_id in seen:
                continue
            seen.add(attach_id)
            context = page[max(0, m.start() - 300): m.start() + 300]
            year_m = re.search(r"\((\d{4})\)", context)
            title_m = re.search(r"<a[^>]*>([^<]+?)\s*\(\d{4}\)", context)
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=attach_id,
                release=unescape(title_m.group(1)).strip() if title_m else "",
                feature_title=unescape(title_m.group(1)).strip() if title_m else identity.title,
                feature_year=int(year_m.group(1)) if year_m else identity.year,
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        raw = t.get(f"{self.BASE}/index.php?act=download&attach_id={candidate.file_id}",
                    headers=self._headers())
        try:
            text = decode_scrape_subtitle_bytes(raw)
        except ScrapeSourceError as exc:
            raise CandidateRejected("subs.sab.bz payload is not text") from exc
        if mostly_cyrillic(text):
            raise CandidateRejected("subs.sab.bz payload is a non-English (Cyrillic) subtitle")
        if not looks_like_srt_text(text):
            raise CandidateRejected("subs.sab.bz payload is not a valid SRT")
        return raw


# ---------------------------------------------------------------------------
# Registry + chain
# ---------------------------------------------------------------------------

SCRAPE_SOURCES: dict[str, BaseSource] = {
    src.key: src
    for src in (
        Subf2meSource(), PodnapisiSource(), Addic7edSource(), SubSourceSource(),
        SubsunacsSource(), YifySubtitlesSource(), SubsSabSource(),
    )
}


def scrape_provider_keys() -> tuple[str, ...]:
    return tuple(key for key in SCRAPE_PROVIDER_ORDER if key in SCRAPE_SOURCES)


def is_scrape_provider(key: str) -> bool:
    return key in SCRAPE_SOURCES


def scrape_provider_label(key: str) -> str:
    return SCRAPE_PROVIDER_LABELS.get(key, key)


@dataclass
class SourceHealth:
    """Circuit-breaker state for one source within one run."""

    hard_failures: int = 0
    parse_failures: int = 0
    disabled_reason: str = ""

    @property
    def disabled(self) -> bool:
        return bool(self.disabled_reason)


def pick_candidates(identity: SourceIdentity, candidates: Iterable[ScrapeCandidate],
                    *, limit: int = SCRAPE_MAX_CANDIDATES_PER_SOURCE) -> list[ScrapeCandidate]:
    """Order a source's candidates by how confidently they name the movie.

    Requires a real title match (and the source's year, when the source
    states one). Ties break on popularity signals.
    """
    scored: list[tuple[float, float, float, ScrapeCandidate]] = []
    for cand in candidates:
        sim = title_similarity(cand.feature_title or cand.release, identity.title)
        if sim < 0.6:
            continue
        if cand.feature_year and cand.feature_year != identity.year:
            continue
        year_penalty = 0.0 if (not cand.feature_year or cand.feature_year == identity.year) else 0.25
        scored.append((sim - year_penalty, float(cand.downloads or 0), float(cand.rating or 0.0), cand))
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in scored[:max(0, limit)]]


class ScrapeChain:
    """Runs the failover chain with per-source breakers and daily caps.

    ``reserve_cb(source)`` is invoked before each search leaves this
    process so an interrupted request still counts in the durable ledger
    (the fetcher passes a callback that persists the ledger).
    """

    def __init__(self, *, keys: tuple[str, ...] = scrape_provider_keys(),
                 transport: ScrapeTransport | None = None,
                 search_caps: dict[str, int] | None = None,
                 reserved: dict[str, int] | None = None,
                 reserve_cb: Callable[[str], None] | None = None) -> None:
        self.keys = tuple(keys)
        self.transport = transport or default_transport()
        self.search_caps = dict(search_caps or {})
        self.reserved = dict(reserved or {})
        self.reserve_cb = reserve_cb
        self.health: dict[str, SourceHealth] = {key: SourceHealth() for key in self.keys}
        self.notes: dict[str, list[str]] = {key: [] for key in self.keys}

    # -- status -----------------------------------------------------------

    def enabled_keys(self) -> list[str]:
        return [key for key in self.keys if not self.health[key].disabled]

    def status(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key in self.keys:
            health = self.health[key]
            if health.disabled:
                out[key] = f"disabled: {health.disabled_reason}"
                continue
            cap = self.search_caps.get(key, DEFAULT_SEARCH_DAILY_CAP)
            used = self.reserved.get(key, 0)
            out[key] = f"ok (searches used {used}/{cap})"
        return out

    # -- breaker ----------------------------------------------------------

    def _note_hard_failure(self, key: str, reason: str) -> None:
        health = self.health[key]
        health.hard_failures += 1
        self.notes[key].append(f"hard failure ({health.hard_failures}): {reason}")
        if health.hard_failures >= BREAKER_HARD_FAILURES:
            health.disabled_reason = f"{health.hard_failures} consecutive hard failures (last: {reason})"

    def _note_parse_failure(self, key: str, reason: str) -> None:
        health = self.health[key]
        health.parse_failures += 1
        self.notes[key].append(f"parse failure ({health.parse_failures}): {reason}")
        if health.parse_failures >= BREAKER_PARSE_FAILURES:
            health.disabled_reason = f"{health.parse_failures} repeated parse failures (last: {reason})"

    def _note_success(self, key: str) -> None:
        health = self.health[key]
        health.hard_failures = 0
        health.parse_failures = 0

    # -- operations ---------------------------------------------------------

    def search(self, key: str, identity: SourceIdentity) -> list[ScrapeCandidate]:
        source = SCRAPE_SOURCES.get(key)
        if source is None:
            raise ValueError(f"unknown scraped source: {key}")
        health = self.health[key]
        if health.disabled:
            raise SourceUnavailable(f"source disabled this run: {health.disabled_reason}")
        cap = self.search_caps.get(key, DEFAULT_SEARCH_DAILY_CAP)
        if self.reserved.get(key, 0) >= cap:
            raise SourceUnavailable(f"UTC daily search cap reached ({cap})")
        self.reserved[key] = self.reserved.get(key, 0) + 1
        if self.reserve_cb is not None:
            self.reserve_cb(key)
        try:
            cands = source.search(identity, self.transport)
        except ScrapeSourceError as exc:
            self._note_hard_failure(key, str(exc))
            raise SourceUnavailable(str(exc)) from exc
        except Exception as exc:  # structural surprises must not kill the run
            self._note_parse_failure(key, f"{type(exc).__name__}: {exc}")
            raise SourceUnavailable(f"unparseable response ({exc})") from exc
        self._note_success(key)
        return cands

    def fetch(self, key: str, candidate: ScrapeCandidate) -> bytes:
        source = SCRAPE_SOURCES.get(key)
        if source is None:
            raise ValueError(f"unknown scraped source: {key}")
        health = self.health[key]
        if health.disabled:
            raise SourceUnavailable(f"source disabled this run: {health.disabled_reason}")
        try:
            raw = source.fetch(candidate, self.transport)
        except CandidateRejected:
            raise
        except ScrapeSourceError as exc:
            self._note_hard_failure(key, str(exc))
            raise SourceUnavailable(str(exc)) from exc
        except Exception as exc:
            self._note_parse_failure(key, f"{type(exc).__name__}: {exc}")
            raise SourceUnavailable(f"unparseable response ({exc})") from exc
        self._note_success(key)
        if not valid_srt_bytes(raw):
            raise CandidateRejected("payload is not a valid SRT")
        return raw


def run_scrape_chain(
    identity: SourceIdentity,
    *,
    keys: tuple[str, ...],
    chain: ScrapeChain,
    on_reason: Callable[[str, str], None] | None = None,
) -> tuple[ScrapeCandidate | None, str, bytes | None]:
    """Offer the movie to every enabled source in order.

    Returns ``(candidate, provider, raw_bytes)`` on the first accepted
    subtitle, else ``(None, "", None)`` with every source's verdict
    appended through ``on_reason(source, reason)`` so the fetcher can fold
    them into the movie's review detail.
    """
    for key in keys:
        if key not in chain.health or chain.health[key].disabled:
            reason = (f"disabled: {chain.health[key].disabled_reason}" if key in chain.health
                      else "not enabled")
            if on_reason:
                on_reason(key, reason)
            continue
        try:
            cands = chain.search(key, identity)
        except SourceUnavailable as exc:
            if on_reason:
                on_reason(key, str(exc))
            continue
        cands = pick_candidates(identity, cands)
        if not cands:
            if on_reason:
                on_reason(key, "no matching English subtitle")
            continue
        for cand in cands:
            try:
                raw = chain.fetch(key, cand)
            except CandidateRejected as exc:
                if on_reason:
                    on_reason(key, f"candidate refused: {exc}")
                continue
            except SourceUnavailable as exc:
                if on_reason:
                    on_reason(key, str(exc))
                break
            return cand, key, raw
        else:
            if on_reason:
                on_reason(key, "candidates were checked but none produced a valid English SRT")
    return None, "", None



def run_scrape_self_tests(errors: list[str]) -> None:
    """Offline self-test: registry invariants, every parser, breaker, chain."""

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    check(tuple(SCRAPE_SOURCES.keys()) == SCRAPE_PROVIDER_ORDER, "registry keys follow the documented order")
    check(all(src.key in SCRAPE_PROVIDER_LABELS for src in SCRAPE_SOURCES.values()), "every source has a label")
    check(len(SCRAPE_SOURCES) == 7, "exactly seven scraped sources are registered")

    identity = SourceIdentity("The Father", 2020, scrape_normalize_title("The Father"))

    # --- Subf2m: search result year match + movie page + zip --------------
    subf2me_search = (
        b"<html><body><div class=\"search-result\">"
        b"<h2 class=\"exact\">The Father</h2><ul>"
        b"<li><a href=\"/subtitles/111\">The Father (2019)</a></li>"
        b"<li><a href=\"/subtitles/222\">The Father (2020)</a></li>"
        b"</ul></div></body></html>"
    )
    subf2me_movie = (
        b"<html><body><ul>"
        b"<li class=\"item\"><li>playWEB</li>"
        b"<a class=\"download icon-download\" href=\"/subtitles/222/en/999\"></a></li>"
        b"</ul></body></html>"
    )
    subf2me_dl_page = b"<html><body><div class=\"download\"><a href=\"/dl/file.zip\">get</a></div></body></html>"
    def make_zip(name: str, payload: bytes) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(name, payload)
        return buf.getvalue()

    SRT = b"1\n00:00:01,000 --> 00:00:03,000\nhello\n\n2\n00:00:04,000 --> 00:00:06,000\nworld\n"
    subf2me_zip = make_zip("sub.utf.srt", SRT)

    class FakeT:
        def __init__(self, routes: dict[str, bytes]) -> None:
            self.routes = routes
            self.calls: list[str] = []

        def _route(self, url: str) -> bytes:
            best: tuple[int, bytes] | None = None
            for prefix, payload in self.routes.items():
                if url.startswith(prefix) and (best is None or len(prefix) > best[0]):
                    best = (len(prefix), payload)
            if best is None:
                raise ScrapeSourceError(f"unrouted {url}")
            return best[1]

        def get(self, url: str, *, headers: dict[str, str] | None = None) -> bytes:
            self.calls.append(url)
            return self._route(url)

        def post(self, url: str, form: dict[str, str], *, headers: dict[str, str] | None = None) -> bytes:
            self.calls.append("POST " + url)
            return self._route(url)

    t = FakeT({
        "https://subf2m.co/subtitles/searchbytitle": subf2me_search,
        "https://subf2m.co/subtitles/222/en": subf2me_movie,
        "https://subf2m.co/subtitles/222/en/999": subf2me_dl_page,
        "https://subf2m.co/dl/file.zip": subf2me_zip,
    })
    src = Subf2meSource()
    cands = src.search(identity, t)
    check(len(cands) == 1 and cands[0].file_id == "/subtitles/222", "subf2m search keeps the right-year entry only")
    raw = src.fetch(cands[0], t)
    check(raw == SRT, "subf2m fetch extracts the UTF-8 entry from the zip")

    # --- Podnapisi: JSON search + year filter + zip ------------------------
    podnapisi_payload = (
        b'{"data":[{"id":77,"releases":["The.Father.2020.1080p.BluRay.x264-GRP"],'
        b'"custom_releases":[],"movie":{"title":"The Father","year":"2020"}},'
        b'{"id":78,"releases":[],"custom_releases":[],"movie":{"title":"The Father","year":"2019"}}],'
        b'"page":"1","all_pages":"1"}'
    )
    t = FakeT({
        "https://www.podnapisi.net/subtitles/search/advanced": podnapisi_payload,
        "https://www.podnapisi.net/subtitles/77/download": make_zip("77.srt", SRT),
    })
    cands = PodnapisiSource().search(identity, t)
    check(len(cands) == 1 and cands[0].file_id == "77" and cands[0].release.startswith("The.Father.2020"),
          "podnapisi keeps English year-matching subtitles only")
    check(PodnapisiSource().fetch(cands[0], t) == SRT, "podnapisi fetch unzips the single-file archive")

    # --- Addic7ed: search + completed English rows + Referer ---------------
    addic7ed_search = b"<html><body><b>1 results found</b><a href=\"movie/555\">x</a></body></html>"
    addic7ed_movie = (
        b"<html><body><div>Deadpool 2 (2018) <small>...</small></div>"
        b"<table><tr><td class=\"version\">Version 1080p x264-KILLERS,</td></tr></table>"
        b"<table><tr><td class=\"language\">English</td><td>Completed</td><td>"
        b"<a href=\"/sd/9001\"><strong>Download</strong></a> 123 Downloads</td></tr>"
        b"<tr><td class=\"language\">English (Hearing Impaired)</td><td>Completed</td><td>"
        b"<a href=\"/sd/9002\"><strong>most updated</strong></a> 5 Downloads</td></tr>"
        b"<tr><td class=\"language\">Fran\xc3\xa7ais</td><td>Completed</td><td>"
        b"<a href=\"/sd/9003\"><strong>Download</strong></a> 900 Downloads</td></tr>"
        b"<tr><td class=\"language\">English</td><td>% Completed</td><td>"
        b"<a href=\"/sd/9004\"><strong>Download</strong></a> 400 Downloads</td></tr></table>"
        b"<a href=\"/show/12\">show</a></body></html>"
    )
    t = FakeT({
        "https://www.addic7ed.com/srch.php": addic7ed_search,
        "https://www.addic7ed.com/movie/555": addic7ed_movie,
        "https://www.addic7ed.com/sd/": SRT,
    })
    ident_dp = SourceIdentity("Deadpool 2", 2018, scrape_normalize_title("Deadpool 2"))
    cands = Addic7edSource().search(ident_dp, t)
    check(len(cands) == 2 and all(c.feature_title == "Deadpool 2" for c in cands),
          "addic7ed keeps English rows only and drops the incomplete (% Completed) row")
    check(all(c.extra.get("referer", "").endswith("/show/12") for c in cands), "addic7ed captures the movie referer")
    check(Addic7edSource().fetch(cands[0], t) == SRT, "addic7ed fetch returns the raw SRT")

    # --- SubSource: direct slug + English rows + API link -------------------
    subsource_movie = (
        b"<html><body><table>"
        b"<tr><td><a href=\"/subtitle/the-father-2020/english/501\">English</a></td>"
        b"<td><a href=\"/subtitle/the-father-2020/english/501\">The.Father.2020.1080p</a></td></tr>"
        b"<tr><td><a href=\"/subtitle/the-father-2020/french/502\">French</a></td></tr>"
        b"</table></body></html>"
    )
    t = FakeT({
        "https://subsource.net/subtitles/the-father-2020": subsource_movie,
        "https://subsource.net/subtitle/the-father-2020/english/501": (
            b"<html><a href=\"https://api.subsource.net/v1/subtitle/download/abc123\">Download</a></html>"
        ),
        "https://api.subsource.net/v1/subtitle/download/abc123": SRT,
    })
    cands = SubSourceSource().search(identity, t)
    check(len(cands) == 1 and cands[0].file_id.endswith("/english/501"), "subsource finds the English file row")
    check(SubSourceSource().fetch(cands[0], t) == SRT, "subsource fetch follows the API download link")

    # --- Subsunacs: POST search + language guard + getentry ------------------
    subsunacs_search = (
        b"<html><body><table><tr>"
        b"<td><a href=\"/subtitles/The_Father-9001/\">The Father</a> <span>(2020)</span></td>"
        b"</tr></table></body></html>"
    )
    subsunacs_page_en = (
        "<html><h1>The Father (2020)</h1>Език: Английски"
        "<a href=\"https://subsunacs.net/getentry.php?id=9001&amp;ei=0\">srt</a></html>"
    ).encode("utf-8")
    t = FakeT({
        "https://subsunacs.net/search.php": subsunacs_search,
        "https://subsunacs.net/subtitles/The_Father-9001/": subsunacs_page_en,
        "https://subsunacs.net/getentry.php": SRT,
    })
    cands = SubsunacsSource().search(identity, t)
    check(len(cands) == 1 and cands[0].feature_year == 2020, "subsunacs parses the search row and year")
    check(SubsunacsSource().fetch(cands[0], t) == SRT, "subsunacs fetch verifies English and downloads the entry")
    t2 = FakeT({
        "https://subsunacs.net/search.php": subsunacs_search,
        "https://subsunacs.net/subtitles/The_Father-9001/": (
            "<html><h1>The Father (2020)</h1>Език: Български</html>"
        ).encode("utf-8"),
    })
    try:
        SubsunacsSource().fetch(cands[0], t2)
        check(False, "subsunacs must reject a Bulgarian subtitle page")
    except CandidateRejected:
        pass

    # --- YIFY: search cards + English rows + zip ----------------------------
    yify_search = (
        b"<html><body>"
        b"<div class=\"media\"><div class=\"media-body\">"
        b"<h3 class=\"media-heading\" itemprop=\"name\">The Father</h3>"
        b"<span class=\"movinfo-section\">2020<small>year</small></span>"
        b"<a href=\"/movie-imdb/tt111\">go</a></div></div>"
        b"<div class=\"media\"><div class=\"media-body\">"
        b"<h3 class=\"media-heading\" itemprop=\"name\">The Father</h3>"
        b"<span class=\"movinfo-section\">2019<small>year</small></span>"
        b"<a href=\"/movie-imdb/tt222\">go</a></div></div>"
        b"</body></html>"
    )
    yify_movie = (
        b"<html><tbody>"
        b"<tr data-id=\"1\"><span class=\"sub-lang\">Bulgarian</span>"
        b"<td class=\"rating-cell\">4</td><a href=\"/subtitles/77\">x</a></tr>"
        b"<tr data-id=\"2\"><span class=\"sub-lang\">English</span>"
        b"<td class=\"rating-cell\">2</td><a href=\"/subtitles/88\">x</a></tr>"
        b"<tr data-id=\"3\"><span class=\"sub-lang\">English</span>"
        b"<td class=\"rating-cell\">5</td><a href=\"/subtitles/99\">x</a></tr>"
        b"</tbody></html>"
    )
    t = FakeT({
        "https://yifysubtitles.ch/search": yify_search,
        "https://yifysubtitles.ch/movie-imdb/tt111": yify_movie,
        "https://yifysubtitles.ch/subtitle/99.zip": make_zip("88.srt", SRT),
    })
    cands = YifySubtitlesSource().search(identity, t)
    check(len(cands) == 2 and cands[0].file_id == "/movie-imdb/tt111"
          and cands[1].feature_year == 2019,
          "yify search returns the movie cards with their years")
    picked = pick_candidates(identity, cands, limit=SCRAPE_MAX_CANDIDATES_PER_SOURCE)
    check(len(picked) == 1 and picked[0].file_id == "/movie-imdb/tt111",
          "year filtering keeps only the right-year card")
    check(YifySubtitlesSource().fetch(cands[0], t) == SRT, "yify fetch picks the highest-rated English row")

    # --- Subs.sab.bz: POST search + Cyrillic guard ---------------------------
    subsab_search = (
        b"<html><body><table><tr>"
        b"<td><a href=\"http://subs.sab.bz/index.php?s=x&amp;act=download&amp;attach_id=4242\">The Father (2020)</a></td>"
        b"</tr></table></body></html>"
    )
    cyrillic = "1\n00:00:01,000 --> 00:00:03,000\nздравей свят\n\n".encode("utf-8")
    t = FakeT({
        "http://subs.sab.bz/index.php?act=download": SRT,
        "http://subs.sab.bz/index.php?": subsab_search,
    })
    cands = SubsSabSource().search(identity, t)
    check(len(cands) == 1 and cands[0].file_id == "4242", "subs.sab.bz captures the attach id")
    check(SubsSabSource().fetch(cands[0], t) == SRT, "subs.sab.bz fetch accepts an English SRT")
    t2 = FakeT({
        "http://subs.sab.bz/index.php?act=download": cyrillic,
        "http://subs.sab.bz/index.php?": subsab_search,
    })
    try:
        SubsSabSource().fetch(cands[0], t2)
        check(False, "subs.sab.bz must reject a Cyrillic payload")
    except CandidateRejected:
        pass

    # --- selection + breaker + chain ------------------------------------------
    mixed = [
        ScrapeCandidate(provider="x", file_id="a", feature_title="The Father", feature_year=2020, downloads=10),
        ScrapeCandidate(provider="x", file_id="b", feature_title="Totally Different", feature_year=2020),
        ScrapeCandidate(provider="x", file_id="c", feature_title="The Father", feature_year=2019),
    ]
    picked = pick_candidates(identity, mixed)
    check([c.file_id for c in picked] == ["a"], "selection requires title match and year match")

    chain = ScrapeChain(keys=(PROVIDER_SUBF2ME,), transport=FakeT({}))
    for _ in range(BREAKER_HARD_FAILURES):
        try:
            chain.search(PROVIDER_SUBF2ME, identity)
        except SourceUnavailable:
            pass
    check(chain.health[PROVIDER_SUBF2ME].disabled, "three hard failures disable the source")
    try:
        chain.search(PROVIDER_SUBF2ME, identity)
        check(False, "disabled source must not be searched")
    except SourceUnavailable:
        pass

    cap_chain = ScrapeChain(keys=(PROVIDER_SUBF2ME,), transport=FakeT({}),
                            search_caps={PROVIDER_SUBF2ME: 1}, reserved={PROVIDER_SUBF2ME: 1})
    try:
        cap_chain.search(PROVIDER_SUBF2ME, identity)
        check(False, "exhausted search cap must refuse the source")
    except SourceUnavailable as exc:
        check("cap" in str(exc), "cap exhaustion is named in the reason")

    # chain: first source dead, second source delivers
    ok_routes = {
        "https://www.podnapisi.net/subtitles/search/advanced": podnapisi_payload,
        "https://www.podnapisi.net/subtitles/77/download": make_zip("77.srt", SRT),
    }
    reasons: list[tuple[str, str]] = []
    # A FakeT with only the podnapisi routes hard-fails every subf2me request
    # ("unrouted"), so the chain must fail over to podnapisi.
    mixed_chain = ScrapeChain(
        keys=(PROVIDER_SUBF2ME, PROVIDER_PODNAPISI),
        transport=FakeT(ok_routes),
    )
    got = run_scrape_chain(
        identity, keys=(PROVIDER_SUBF2ME, PROVIDER_PODNAPISI), chain=mixed_chain,
        on_reason=lambda k, r: reasons.append((k, r)),
    )
    check(got[1] == PROVIDER_PODNAPISI and got[2] == SRT, "chain fails over to the next live source")
    check(any(k == PROVIDER_SUBF2ME for k, _ in reasons), "the failed source's verdict is reported")




# =============================================================================

LIBRARY_DIR = r"E:\torrents\final_organized"
# Logs and reports live under tools\ReportsAndLogs so the root of E:\torrents
# stays media-only.
LOG_FILE = r"E:\torrents\tools\ReportsAndLogs\subtitle_fetcher\subtitle_fetcher.log"  # Appended every run; this is also the durable quota/retry ledger.
REPORT_FILE = r"E:\torrents\tools\ReportsAndLogs\subtitle_fetcher\subtitle_fetcher_report.txt"
# The append-only log is the durable quota ledger; no state/cache file is created.
LEDGER_EVENT = "SUBTITLE_LEDGER"
USER_DAILY_CAP = 20
DEVELOPMENT_ANONYMOUS_DAILY_CAP = 100
AUTH_MODE_DEVELOPMENT_ANONYMOUS = "development-anonymous"
AUTH_MODE_USER = "user"
DEFAULT_AUTH_MODE = AUTH_MODE_DEVELOPMENT_ANONYMOUS

# Leave blank to use environment variables instead. In development-anonymous
# mode only the API key is used; username/password are intentionally ignored.
OPENSUBTITLES_API_KEY = ""
OPENSUBTITLES_USERNAME = ""
OPENSUBTITLES_PASSWORD = ""
SUBDL_API_KEY = ""
SUBDL_API_BASE = "https://api.subdl.com/api/v2"
SUBDL_DOWNLOAD_HOST = "dl.subdl.com"
# SubDL's current v2 developer docs publish separate free-tier allowances:
# 2,000 searches and 50 downloads per day. Keep conservative local guards for
# both; users on a paid plan can explicitly raise either cap with the matching
# --subdl-*-daily-cap flag.
SUBDL_DEFAULT_SEARCH_DAILY_CAP = 2_000
SCRAPE_DEFAULT_SEARCH_DAILY_CAP = DEFAULT_SEARCH_DAILY_CAP

SUBDL_DEFAULT_DAILY_CAP = 50
SUBDL_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

__version__ = "2.10.0"
APP_USER_AGENT = "JellyfinMovieSubtitleFetcher v2.9"
API_BASE = "https://api.opensubtitles.com/api/v1"

# The preceding standardizer emits canonical MKV movies only. Limiting the
# fetcher to that exact contract prevents unrelated videos or media variants
# from receiving sidecars.
VIDEO_EXTENSIONS = {".mkv"}
DIRECT_PLAY_SUBTITLE_EXTENSION = ".srt"
DOWNLOAD_SUBTITLE_FORMAT = "srt"
MIN_MOVIE_SIZE_MB = 300
REQUEST_GAP_SEC = 1.1  # stay under the documented per-second limit
# Bound to the one shared limit (vendored below), not a second copy of the number.
MAX_SUBTITLE_BYTES = EXTERNAL_SRT_MAX_BYTES
LANGUAGES = "en"

PROVIDER_OPENSUBTITLES = "opensubtitles"
PROVIDER_SUBDL = "subdl"

# =============================================================================
# CONSTANTS
# =============================================================================

HASH_CHUNK = 65536  # 64 KiB
MIN_HASH_SIZE = HASH_CHUNK * 2

EXTRA_DIR_NAMES = frozenset({
    "featurettes", "extras", "specials", "shorts", "bonus",
    "behind the scenes", "deleted scenes", "interviews", "scenes",
    "trailers", "other", "samples", "sample", "clips",
    "bdmv", "certificate", "video_ts", "audio_ts",
    "subs", "sub", "subtitles",
})
DISC_DIR_NAMES = frozenset({"bdmv", "certificate", "video_ts", "audio_ts", "hvdvd_ts"})
SAMPLE_NAME_RE = re.compile(
    r"(?i)(?:^|[._\-\s\[(])(sample|trailer|teaser)(?:[.)\]\-\s_]|$)"
)
ENGLISH_LANGUAGE_TOKENS = frozenset({"en", "eng", "english"})
MOVIE_IDENTITY_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<year>(?:18|19|20)\d{2})\)\s*$")
# Without an original release name, edition-labelled subtitles are too uncertain
# for automatic selection. They remain visible in the report for manual review.
EDITION_MARKERS = frozenset({
    "extended", "unrated", "directors cut", "director s cut", "theatrical",
    "ultimate", "special edition", "collectors edition", "anniversary",
    "remastered", "redux", "final cut", "alternate cut",
})
# The library is Blu-ray material, so automatic selection requires an explicit
# Blu-ray keyword in the release name. It confirms the source matches the MKV
# and keeps WEBDVDRip-style uploads out of the automatic pick. The match is
# case- and separator-insensitive: "BluRay", "Blu-ray", "BLU RAY", "blu.ray".
BLURAY_RELEASE_RE = re.compile(r"blu[\s._-]*ray", re.IGNORECASE)
# One sentence for the banner and the report: what automatic selection does.
SELECTION_POLICY_TEXT = (
    "auto-selects, across OpenSubtitles and SubDL as equal sources, "
    "the release that names the movie and its release year, carries a "
    "Blu-ray keyword, and has the most downloads"
)
# SubDL documents this as a confident release-level filename match. It applies
# only to its /files/search endpoint; title-only fallback retains its separate
# strict identity and provider-quality policy.
MIN_SUBDL_RELEASE_MATCH_SCORE = 0.80

# Official OSHash test: first+last 64KiB of a synthetic pattern is tested in --self-test.

@dataclass
class Config:
    library: Path = field(default_factory=lambda: Path(LIBRARY_DIR))
    log_file: Path | None = field(default_factory=lambda: Path(LOG_FILE) if LOG_FILE else None)
    report_file: Path = field(default_factory=lambda: Path(REPORT_FILE))
    api_key: str = ""
    subdl_api_key: str = ""
    username: str = ""
    password: str = ""
    dry_run: bool = False
    min_movie_size_mb: float = MIN_MOVIE_SIZE_MB
    lock_timeout_seconds: float = 60.0
    limit: int = 0
    identity_fallback: bool = False
    auth_mode: str = DEFAULT_AUTH_MODE

    @property
    def min_bytes(self) -> int:
        return int(self.min_movie_size_mb * 1024 * 1024)

    @property
    def sidecar_suffix(self) -> str:
        """The sole output sidecar: a normal English UTF-8 SRT (``.eng.srt``)."""
        return EXTERNAL_SRT_SUFFIX

@dataclass
class Candidate:
    # OpenSubtitles uses a numeric ``file_id`` while SubDL exposes opaque
    # ``n_id`` values. Keep the common selection model without throwing away
    # the provider's stable identifier.
    file_id: int | str
    release: str
    moviehash_match: bool
    downloads: int
    votes: int
    rating: float
    trusted: bool
    hearing_impaired: bool
    machine_translated: bool
    ai_translated: bool
    foreign_parts_only: bool
    language: str
    feature_title: str = ""
    feature_year: int = 0
    feature_imdb_id: int = 0
    # /api/v2/files/search provides this release-name similarity in [0, 1].
    # ``None`` means the provider did not offer a filename-match score.
    subdl_match_score: float | None = None

@dataclass(frozen=True)
class SubdlDownload:
    """A vetted SubDL download reference kept out of human-facing logs.

    ``url`` is an optional raw-file URL returned for an unpacked SRT. ``n_id``
    is the documented v2 API download identifier and is used when no raw URL
    is available. Neither value is ever printed because a provider may attach
    short-lived query credentials to a URL.
    """

    n_id: str = ""
    url: str = ""

class SubdlSearchQuotaExhausted(RuntimeError):
    """Raised before a SubDL search that would exceed the durable local cap."""

@dataclass(frozen=True)
class MovieIdentity:
    """Canonical identity inferred only from a standardized ``Title (Year)`` name."""
    title: str
    year: int
    normalized_title: str

# Every result carries a machine-readable reason alongside its human detail so
# the report groups movies by what the user has to *do*, instead of guessing
# that grouping back out of a prose sentence.
REASON_COVERED = "covered"
REASON_DOWNLOADED = "downloaded"
REASON_DRY_RUN = "dry_run"
REASON_NO_MATCH = "no_match"
REASON_SIDECAR_UNUSABLE = "sidecar_unusable"
REASON_SIDECAR_NAME = "sidecar_name"
REASON_REVIEW = "review"
REASON_QUOTA = "quota"
REASON_LAYOUT = "layout"
REASON_ERROR = "error"

@dataclass
class JobResult:
    video: Path
    status: str  # have, skip, download, dry-run, review, error
    detail: str
    dest: Path | None = None
    reason: str = ""

@dataclass(frozen=True)
class VideoSnapshot:
    device: int
    inode: int
    size: int
    mtime_ns: int

# =============================================================================
# LOGGING / HTTP
# =============================================================================

class ConcurrentSidecarError(RuntimeError):
    """Raised when another actor safely created the requested sidecar first."""

def log(msg: str, level: str = "INFO", log_file: Path | None = None) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
    # Never let a console encoding abort a run: the progress lines carry an em
    # dash and the report carries box-drawing characters.
    print_text(line)
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

def video_snapshot(path: Path) -> VideoSnapshot:
    """Capture a no-follow video identity before an external-provider transaction."""
    file_stat = path.stat(follow_symlinks=False)
    if path.is_symlink() or not path.is_file():
        raise OSError(f"not a regular non-symlink movie file: {path}")
    return VideoSnapshot(
        device=int(file_stat.st_dev), inode=int(file_stat.st_ino),
        size=int(file_stat.st_size), mtime_ns=int(file_stat.st_mtime_ns),
    )

def video_snapshot_matches(path: Path, expected: VideoSnapshot) -> bool:
    try:
        return video_snapshot(path) == expected
    except OSError:
        return False

def _sum_u64_le(fh, nbytes: int) -> int:
    fmt = "<Q"
    n = nbytes // 8
    total = 0
    for _ in range(n):
        chunk = fh.read(8)
        if len(chunk) != 8:
            raise ValueError("short read while hashing")
        total = (total + struct.unpack(fmt, chunk)[0]) & 0xFFFFFFFFFFFFFFFF
    return total

def moviehash(path: Path) -> str:
    """OpenSubtitles OSHash: size + uint64le sum of first/last 64 KiB."""
    size = path.stat().st_size
    if size < MIN_HASH_SIZE:
        raise ValueError(f"file too small to hash ({size} bytes)")
    with path.open("rb") as fh:
        total = size & 0xFFFFFFFFFFFFFFFF
        total = (total + _sum_u64_le(fh, HASH_CHUNK)) & 0xFFFFFFFFFFFFFFFF
        fh.seek(size - HASH_CHUNK)
        total = (total + _sum_u64_le(fh, HASH_CHUNK)) & 0xFFFFFFFFFFFFFFFF
    return f"{total:016x}"

def moviehash_bytes(data: bytes) -> str:
    """Same algorithm over an in-memory blob (tests)."""
    size = len(data)
    if size < MIN_HASH_SIZE:
        raise ValueError("too small")
    fmt = "<Q"
    n = HASH_CHUNK // 8
    total = size & 0xFFFFFFFFFFFFFFFF
    for i in range(n):
        total = (total + struct.unpack_from(fmt, data, i * 8)[0]) & 0xFFFFFFFFFFFFFFFF
    tail = size - HASH_CHUNK
    for i in range(n):
        total = (total + struct.unpack_from(fmt, data, tail + i * 8)[0]) & 0xFFFFFFFFFFFFFFFF
    return f"{total:016x}"

class OpenSubtitlesClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.token: str | None = None
        self._last_call = 0.0

    def _headers(self, *, auth: bool = False) -> dict[str, str]:
        h = {
            "Api-Key": self.cfg.api_key,
            "User-Agent": APP_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _throttle(self) -> None:
        wait = REQUEST_GAP_SEC - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
        _retried_auth: bool = False,
    ) -> dict[str, Any]:
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None if body is None else json.dumps(body).encode("utf-8")

        last_err: Exception | None = None
        for attempt in range(4):
            self._throttle()
            req = urllib.request.Request(
                url, data=data, method=method, headers=self._headers(auth=auth),
            )
            try:
                # API_BASE is a fixed HTTPS provider endpoint.
                with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
                    raw = resp.read().decode("utf-8", errors="replace")
                break
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")[:400]
                invalid_token = auth and "invalid" in err_body.casefold()
                if (exc.code == 401 or (exc.code == 500 and invalid_token)) and auth and not _retried_auth:
                    self.token = None
                    self.login()
                    return self._request(
                        method, path, params=params, body=body, auth=True, _retried_auth=True,
                    )
                retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
                if retryable and attempt < 3:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = min(30.0, float(retry_after)) if retry_after else 2.0 * (attempt + 1)
                    except ValueError:
                        delay = 2.0 * (attempt + 1)
                    time.sleep(delay)
                    last_err = RuntimeError(f"HTTP {exc.code} {path}: {err_body}")
                    continue
                raise RuntimeError(f"HTTP {exc.code} {path}: {err_body}") from exc
            except urllib.error.URLError as exc:
                last_err = RuntimeError(f"network error {path}: {exc}")
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_err from exc
        else:
            raise last_err or RuntimeError(f"request failed {path}")

        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSON from {path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"unexpected JSON from {path}")
        return parsed

    def login(self) -> None:
        if not self.cfg.username or not self.cfg.password:
            raise RuntimeError(
                "OpenSubtitles username/password required for downloads. "
                "Set OPENSUBTITLES_USERNAME / OPENSUBTITLES_PASSWORD."
            )
        payload = self._request(
            "POST",
            "/login",
            body={"username": self.cfg.username, "password": self.cfg.password},
        )
        token = payload.get("token")
        if not token:
            raise RuntimeError(f"login failed: {payload}")
        self.token = str(token)

    def search(self, *, movie_hash: str, query: str) -> list[Candidate]:
        # OpenSubtitles recommends submitting the moviehash and filename query
        # together. Explicit filters reduce unsafe/irrelevant candidates before
        # local ranking; no provider-side ordering is requested.
        params = {
            "moviehash": movie_hash,
            "moviehash_match": "only",
            "query": query,
            "languages": LANGUAGES,
            "type": "movie",
            "machine_translated": "exclude",
            "ai_translated": "exclude",
        }
        params["foreign_parts_only"] = "exclude"
        params["hearing_impaired"] = "exclude"
        payload = self._request("GET", "/subtitles", params=params)
        return parse_candidates(payload)

    def search_identity(self, identity: MovieIdentity) -> list[Candidate]:
        """Search only a normalized title/year identity after a hash search fails.

        This method deliberately has no moviehash parameter. Its results are never
        accepted by the strict picker; they must pass ``pick_identity_candidate``.
        """
        params = {
            "query": f"{identity.title} {identity.year}",
            "languages": LANGUAGES,
            "type": "movie",
            "machine_translated": "exclude",
            "ai_translated": "exclude",
            "foreign_parts_only": "exclude",
            "hearing_impaired": "exclude",
        }
        payload = self._request("GET", "/subtitles", params=params)
        return parse_candidates(payload)

    def download_srt(self, file_id: int, dest: Path, *, video: Path, expected_video: VideoSnapshot) -> None:
        """Download exactly one provider-rendered UTF-8 SRT and activate it atomically.

        The development-anonymous mode sends the consumer API key and no JWT,
        matching OpenSubtitles' temporary Under Development allowance. User mode
        retains the previous login/JWT path.
        """
        if self.cfg.auth_mode == AUTH_MODE_USER:
            if not self.token:
                self.login()
            use_user_token = True
        elif self.cfg.auth_mode == AUTH_MODE_DEVELOPMENT_ANONYMOUS:
            use_user_token = False
        else:
            raise RuntimeError(f"unsupported authentication mode: {self.cfg.auth_mode}")
        try:
            payload = self._request(
                "POST", "/download", body={"file_id": file_id, "sub_format": DOWNLOAD_SUBTITLE_FORMAT}, auth=use_user_token,
            )
        except RuntimeError as exc:
            message = str(exc)
            if self.cfg.auth_mode == AUTH_MODE_DEVELOPMENT_ANONYMOUS and ("HTTP 401" in message or "HTTP 403" in message):
                raise RuntimeError(
                    "OpenSubtitles rejected the development-anonymous download. Confirm this API consumer still has "
                    "Under Development and Allow anonymous enabled, then retry. If the temporary allowance has ended, "
                    "run with --auth-mode user and configured username/password."
                ) from exc
            raise
        link = payload.get("link")
        if not link:
            raise RuntimeError(f"download endpoint returned no link: {payload}")
        download_url = str(link)
        parsed_link = urllib.parse.urlsplit(download_url)
        # The download URL is provider-supplied data, not a trusted local path.
        # Restrict it to an absolute HTTPS URL so file:, data:, ftp:, malformed,
        # and downgrade links cannot be dereferenced by urllib.
        if parsed_link.scheme.lower() != "https" or not parsed_link.netloc:
            raise RuntimeError("download endpoint returned an invalid non-HTTPS subtitle link")
        self._throttle()
        req = urllib.request.Request(
            download_url, method="GET", headers={"User-Agent": APP_USER_AGENT, "Accept": "text/plain, */*;q=0.1"},
        )
        try:
            # urlsplit above requires an absolute HTTPS provider link.
            with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > MAX_SUBTITLE_BYTES:
                    raise RuntimeError(f"subtitle exceeds {MAX_SUBTITLE_BYTES} byte safety limit")
                data = resp.read(MAX_SUBTITLE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"subtitle download HTTP {exc.code}") from exc
        except ValueError as exc:
            raise RuntimeError("invalid subtitle content length") from exc
        if len(data) > MAX_SUBTITLE_BYTES:
            raise RuntimeError(f"subtitle exceeds {MAX_SUBTITLE_BYTES} byte safety limit")
        try:
            text = decode_subtitle_bytes(data)
        except (OSError, EOFError, ValueError) as exc:
            raise RuntimeError("downloaded subtitle could not be decompressed") from exc
        text = normalize_srt_newlines(text)
        if not looks_like_srt(text):
            raise RuntimeError("downloaded payload is not a valid SRT subtitle")
        if not video_snapshot_matches(video, expected_video):
            raise RuntimeError("movie changed during subtitle lookup; downloaded SRT was not activated")
        try:
            atomic_write_text(dest, text, replace=False)
        except FileExistsError as exc:
            raise ConcurrentSidecarError("English SRT appeared during download; preserved the existing sidecar") from exc

def _subdl_text(value: Any) -> str:
    """Return a bounded, stripped API scalar without treating containers as text."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()

def _subdl_identifier(value: Any) -> str:
    """Accept only a compact identifier that is safe in a v2 URL path segment."""
    identifier = _subdl_text(value)
    if not identifier or len(identifier) > 256:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", identifier):
        return ""
    return identifier

def _subdl_match_score(value: Any) -> float | None:
    """Parse SubDL's documented [0, 1] release-match score fail-closed."""
    text = _subdl_text(value)
    if not text:
        return None
    try:
        score = float(text)
    except ValueError:
        return None
    return score if 0.0 <= score <= 1.0 else None

def normalize_subdl_download_url(value: Any) -> str:
    """Validate a SubDL raw-file URL before ``urllib`` can dereference it.

    Search responses are remote input. SubDL documents relative ``/subtitle/``
    URLs and ``dl.subdl.com`` raw URLs; accepting an arbitrary absolute URL
    here would turn a subtitle lookup into an SSRF primitive. The v2 API
    download endpoint is built locally instead and therefore needs no URL from
    the response.
    """
    raw = _subdl_text(value)
    if not raw:
        raise ValueError("SubDL returned an empty download URL")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        hostname = (parsed.hostname or "").casefold()
        if parsed.scheme.casefold() != "https" or hostname != SUBDL_DOWNLOAD_HOST:
            raise ValueError("SubDL returned a download URL outside dl.subdl.com")
        try:
            unsafe_port = parsed.port not in (None, 443)
        except ValueError as exc:
            raise ValueError("SubDL returned an unsafe download URL") from exc
        if parsed.username or parsed.password or unsafe_port:
            raise ValueError("SubDL returned an unsafe download URL")
        normalized = raw
    else:
        # Network-path URLs (//host/path) are absolute URLs in disguise.
        if not raw.startswith("/") or raw.startswith("//"):
            raise ValueError("SubDL returned an invalid relative download URL")
        normalized = f"https://{SUBDL_DOWNLOAD_HOST}{raw}"

    final = urllib.parse.urlsplit(normalized)
    decoded_parts = urllib.parse.unquote(final.path).split("/")
    if not final.path.startswith("/subtitle/") or any(part in {".", ".."} for part in decoded_parts):
        raise ValueError("SubDL returned an invalid subtitle download path")
    return normalized

def _subdl_exact_feature_record(
    feature: dict[str, Any], identity: MovieIdentity,
) -> tuple[str, int, int] | None:
    """Validate one provider-supplied movie identity record."""
    titles = [
        _subdl_text(feature.get(field_name))
        for field_name in ("name", "title", "original_name")
    ]
    matched_title = next(
        (title for title in titles if normalize_title(title) == identity.normalized_title), ""
    )
    year = _nonnegative_int(feature.get("year"))
    media_type = _subdl_text(feature.get("type")).casefold()
    if media_type != "movie" or year != identity.year or not matched_title:
        return None

    imdb_text = _subdl_text(feature.get("imdb_id"))
    imdb_match = re.search(r"(\d+)$", imdb_text)
    return matched_title, year, int(imdb_match.group(1)) if imdb_match else 0

def _subdl_exact_feature(
    payload: dict[str, Any], identity: MovieIdentity, *, require_match: bool = False,
) -> tuple[str, int, int] | None:
    """Confirm the provider says these subtitles belong to this exact movie.

    ``/files/search`` returns a ``match`` record that is specifically bound to
    the filename supplied by this client, so it is mandatory for that route.
    The title-search endpoint documents its subtitle array as belonging to the
    first entry in ``results``, which remains its authoritative identity.
    """
    match = payload.get("match")
    if require_match:
        # The documented filename endpoint attaches ``subtitles`` to this
        # parsed-release record. Do not substitute a generic search result if
        # it is absent or disagrees; that would turn release matching into a
        # weaker title search without the caller's knowledge.
        return _subdl_exact_feature_record(match, identity) if isinstance(match, dict) else None

    results = payload.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return _subdl_exact_feature_record(results[0], identity)
    if isinstance(match, dict):
        return _subdl_exact_feature_record(match, identity)
    return None

def _subdl_value(child: dict[str, Any], parent: dict[str, Any], *names: str) -> Any:
    """Read an unpacked-file field first, then its parent subtitle record."""
    for name in names:
        if name in child and child[name] is not None:
            return child[name]
    for name in names:
        if name in parent and parent[name] is not None:
            return parent[name]
    return None

def _subdl_is_srt_or_archive(child: dict[str, Any], parent: dict[str, Any]) -> bool:
    """Reject an explicitly non-SRT SubDL result before it reaches download."""
    media_format = _subdl_text(_subdl_value(child, parent, "format")).casefold().lstrip(".")
    if media_format and media_format not in {"srt", "zip"}:
        return False
    name = _subdl_text(_subdl_value(child, parent, "name", "file_name"))
    if not name:
        return True
    lower_name = name.casefold().split("?", 1)[0]
    # A provider may call an archive simply "subtitle"; accept an unknown
    # extension only when no explicit format says otherwise, then validate the
    # bytes after download. Known non-SRT formats are never candidates.
    known_non_srt = (".ass", ".ssa", ".sub", ".idx", ".vtt", ".ttml", ".dfxp")
    return not lower_name.endswith(known_non_srt)

def _subdl_candidate_reference(
    child: dict[str, Any], parent: dict[str, Any],
) -> tuple[str, SubdlDownload] | None:
    """Build a stable, non-secret candidate key and safe download reference."""
    n_id = _subdl_identifier(_subdl_value(child, parent, "n_id", "nId"))
    file_n_id = _subdl_identifier(_subdl_value(child, parent, "file_n_id", "fileNId"))
    raw_url = _subdl_value(child, parent, "url", "download_link")
    url = ""
    if raw_url:
        try:
            url = normalize_subdl_download_url(raw_url)
        except ValueError:
            # An authenticated v2 n_id gives us a safer locally constructed
            # endpoint, so an unexpected response URL is not fatal in that
            # case. Without an n_id there is nothing safe to download.
            if not n_id:
                return None
    if not n_id and not url:
        return None

    if n_id:
        candidate_id = f"subdl:{n_id}"
        if file_n_id:
            candidate_id += f":{file_n_id}"
        elif url:
            candidate_id += ":" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    else:
        # A legacy v1-shaped response may expose only a raw URL. A deterministic
        # digest is stable across processes, unlike Python's randomized hash().
        candidate_id = "subdl:url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return candidate_id, SubdlDownload(n_id=n_id, url=url)

def _identity_candidate_basics(cands: Sequence[Candidate], identity: MovieIdentity) -> list[Candidate]:
    """Return title/year-exact candidates before provider-specific quality rules."""
    return [
        candidate for candidate in cands
        if _is_normal_english_human_candidate(candidate)
        and candidate.feature_year == identity.year
        and normalize_title(candidate.feature_title) == identity.normalized_title
        and not release_has_edition_marker(candidate.release)
    ]

def pick_subdl_identity_candidate(
    cands: Sequence[Candidate],
    identity: MovieIdentity,
    *,
    require_release_match_score: bool = False,
) -> tuple[Candidate | None, str]:
    """Choose a conservative SubDL fallback candidate.

    The generic title/year route has no documented release-similarity signal, so
    it retains the existing strict provider-quality policy (or one uniquely
    normal English SRT when the v2 response omits quality metadata). In contrast,
    ``/files/search`` explicitly ranks exact-release candidates by
    ``match_score``. There, choose only the single highest valid score at or
    above SubDL's documented confident threshold; provider vote metadata must
    not accidentally outrank the release match, and a tied top score is review
    rather than a guess.
    """
    if require_release_match_score:
        scored: list[tuple[Candidate, float]] = []
        for candidate in _identity_candidate_basics(cands, identity):
            score = _subdl_match_score(candidate.subdl_match_score)
            if score is not None and score >= MIN_SUBDL_RELEASE_MATCH_SCORE:
                scored.append((candidate, score))
        if not scored:
            return (
                None,
                "SubDL did not return a confident release match "
                f"(requires match_score >= {MIN_SUBDL_RELEASE_MATCH_SCORE:.2f})",
            )
        highest = max(score for _candidate, score in scored)
        top = [(candidate, score) for candidate, score in scored if score == highest]
        if len(top) != 1:
            return None, "multiple equally scored confident SubDL release matches require review"
        candidate, score = top[0]
        return candidate, f"title/year exact; SubDL highest release match {score:.2f}"

    pick, reason = pick_identity_candidate(cands, identity)
    if pick is not None:
        return pick, reason

    usable = _identity_candidate_basics(cands, identity)
    if len(usable) != 1:
        return None, "SubDL did not return one unambiguous title/year-exact normal English SRT"
    candidate = usable[0]
    if candidate.downloads or candidate.votes or candidate.rating:
        return None, reason
    return candidate, "title/year exact; one normal English SubDL SRT (no provider vote metadata)"

def subdl_download_redirect_url(data: bytes) -> str | None:
    """Return a vetted raw-file URL when the v2 download endpoint returns JSON.

    Some SubDL deployments respond with the file directly while others return a
    short-lived raw download URL. Supporting both shapes keeps the client on
    the documented v2 endpoint without trusting a URL outside the provider.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if error:
        message = _subdl_text(error.get("message") if isinstance(error, dict) else error)
        raise RuntimeError(f"SubDL download failed{': ' + message if message else ''}")
    containers = (payload, payload.get("data"), payload.get("download"))
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("download_url", "url", "link"):
            value = container.get(key)
            if value:
                try:
                    return normalize_subdl_download_url(value)
                except ValueError as exc:
                    raise RuntimeError("SubDL returned an unsafe download URL") from exc
    return None

def decode_subdl_srt_payload(data: bytes, max_bytes: int) -> str:
    """Decode a raw SRT or exactly one SRT member from a bounded archive."""
    if len(data) > max_bytes:
        raise RuntimeError(f"subtitle exceeds {max_bytes} byte safety limit")
    if zipfile.is_zipfile(io.BytesIO(data)):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                candidates = [
                    info for info in archive.infolist()
                    if not info.is_dir()
                    and not (info.flag_bits & 0x1)  # encrypted archives cannot be safely inspected
                    and info.filename.casefold().endswith(".srt")
                    and info.file_size <= max_bytes
                ]
                if not candidates:
                    raise RuntimeError("no usable .srt file found in SubDL zip archive")
                # An unpacked file URL is selected before an archive reaches this
                # branch. Without that per-file reference, choosing one of several
                # SRTs would be a guess, so keep it for manual review instead.
                if len(candidates) != 1:
                    raise RuntimeError("SubDL zip archive contains multiple usable .srt files")
                selected = candidates[0]
                with archive.open(selected, "r") as member:
                    raw_srt = member.read(max_bytes + 1)
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
            if isinstance(exc, RuntimeError) and (
                str(exc).startswith("no usable .srt")
                or str(exc).startswith("SubDL zip archive contains multiple")
            ):
                raise
            raise RuntimeError("SubDL zip archive could not be read safely") from exc
        if len(raw_srt) > max_bytes:
            raise RuntimeError(f"subtitle exceeds {max_bytes} byte safety limit")
        data = raw_srt
    if data.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as archive:
                data = archive.read(max_bytes + 1)
        except (OSError, EOFError) as exc:
            raise RuntimeError("SubDL gzip subtitle could not be read safely") from exc
        if len(data) > max_bytes:
            raise RuntimeError(f"subtitle exceeds {max_bytes} byte safety limit")
    text = normalize_srt_newlines(decode_subtitle_bytes(data))
    if not looks_like_srt(text):
        raise RuntimeError("downloaded payload from SubDL is not a valid SRT subtitle")
    return text

class SubdlClient:
    """Small stdlib-only client for SubDL's authenticated v2 API."""

    def __init__(
        self,
        api_key: str,
        *,
        before_search_request: Callable[[], None] | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        # Queue mode supplies a durable reservation callback. Keep it optional
        # so this small client remains usable on its own and in focused tests.
        self._before_search_request = before_search_request
        self._last_call = 0.0

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"User-Agent": APP_USER_AGENT, "Accept": accept}
        if self.api_key:
            # v2 documents Bearer authentication. Keeping credentials out of
            # query strings prevents a key from leaking into proxy/access logs.
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _throttle(self) -> None:
        wait = REQUEST_GAP_SEC - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    @staticmethod
    def _read_limited(response: Any, max_bytes: int, label: str) -> bytes:
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                if int(declared) > max_bytes:
                    raise RuntimeError(f"{label} exceeds {max_bytes} byte safety limit")
            except ValueError as exc:
                raise RuntimeError(f"invalid {label} content length") from exc
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RuntimeError(f"{label} exceeds {max_bytes} byte safety limit")
        return data

    def _request_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("SubDL API key is required")
        url = SUBDL_API_BASE + path + "?" + urllib.parse.urlencode(params)
        last_error: RuntimeError | None = None
        for attempt in range(4):
            # A retry is another HTTP search request and can count against the
            # provider quota, so reserve it before each outbound attempt. Do
            # this before throttling too: an exhausted cap must not sleep only
            # to reject a request that will never be sent.
            if self._before_search_request is not None:
                self._before_search_request()
            self._throttle()
            request = urllib.request.Request(url, headers=self._headers("application/json"))
            try:
                with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - fixed provider API endpoint
                    raw = self._read_limited(response, SUBDL_MAX_RESPONSE_BYTES, "SubDL API response")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read(400).decode("utf-8", errors="replace").strip()
                last_error = RuntimeError(f"SubDL API HTTP {exc.code}: {body}".rstrip())
                if exc.code in {408, 425, 429, 500, 502, 503, 504} and attempt < 3:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = min(30.0, float(retry_after)) if retry_after else 2.0 * (attempt + 1)
                    except ValueError:
                        delay = 2.0 * (attempt + 1)
                    time.sleep(delay)
                    continue
                raise last_error from exc
            except urllib.error.URLError as exc:
                last_error = RuntimeError(f"SubDL API network error: {exc.reason}")
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_error from exc
        else:
            raise last_error or RuntimeError("SubDL API request failed")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("SubDL API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("SubDL API returned an unexpected JSON document")
        error = payload.get("error")
        if payload.get("status") is False or error:
            message = _subdl_text(error.get("message") if isinstance(error, dict) else error)
            message = message or _subdl_text(payload.get("message"))
            raise RuntimeError(f"SubDL API rejected the search{': ' + message if message else ''}")
        return payload

    def _candidate(
        self,
        parent: dict[str, Any],
        child: dict[str, Any],
        feature_title: str,
        feature_year: int,
        feature_imdb_id: int,
    ) -> tuple[Candidate, str, SubdlDownload] | None:
        language = _subdl_text(_subdl_value(child, parent, "language", "lang")).casefold()
        if language not in ENGLISH_LANGUAGE_TOKENS or not _subdl_is_srt_or_archive(child, parent):
            return None
        reference = _subdl_candidate_reference(child, parent)
        if reference is None:
            return None
        candidate_id, download = reference
        release = _subdl_text(_subdl_value(child, parent, "release_name", "release", "name", "file_name"))
        return (
            Candidate(
                file_id=candidate_id,
                release=release,
                moviehash_match=False,
                downloads=_nonnegative_int(_subdl_value(child, parent, "downloads", "download_count")),
                votes=_nonnegative_int(_subdl_value(child, parent, "votes", "vote_count")),
                rating=_nonnegative_float(_subdl_value(child, parent, "ratings", "rating")),
                trusted=as_bool(_subdl_value(child, parent, "trusted", "from_trusted")),
                hearing_impaired=as_bool(_subdl_value(child, parent, "hi", "hearing_impaired")),
                machine_translated=as_bool(_subdl_value(child, parent, "machine_translated", "machine_translation")),
                ai_translated=as_bool(_subdl_value(child, parent, "ai_translated", "ai_translation")),
                foreign_parts_only=as_bool(_subdl_value(child, parent, "foreign_parts_only", "forced")),
                language=language,
                feature_title=feature_title,
                feature_year=feature_year,
                feature_imdb_id=feature_imdb_id,
                subdl_match_score=_subdl_match_score(_subdl_value(child, parent, "match_score")),
            ),
            candidate_id,
            download,
        )

    def _parse_search_payload(
        self,
        payload: dict[str, Any],
        identity: MovieIdentity,
        *,
        require_match: bool = False,
    ) -> tuple[list[Candidate], dict[str, SubdlDownload]]:
        """Turn one vetted SubDL search response into downloadable candidates."""
        feature = _subdl_exact_feature(payload, identity, require_match=require_match)
        if feature is None:
            return [], {}
        feature_title, feature_year, feature_imdb_id = feature

        candidates: list[Candidate] = []
        downloads: dict[str, SubdlDownload] = {}
        subtitles = payload.get("subtitles")
        if not isinstance(subtitles, list):
            return candidates, downloads
        for parent in subtitles:
            if not isinstance(parent, dict):
                continue
            # ``unpack_files`` is the documented subtitle-search shape. Accept
            # the two equivalent spellings defensively because v2 is evolving,
            # but never infer a file reference from a non-object value.
            unpacked = parent.get("unpack_files")
            if not isinstance(unpacked, list):
                unpacked = parent.get("unpacked_files") or parent.get("files")
            entries: list[dict[str, Any]]
            if isinstance(unpacked, list) and unpacked:
                entries = [entry for entry in unpacked if isinstance(entry, dict)]
            else:
                entries = [parent]
            for child in entries:
                built = self._candidate(parent, child, feature_title, feature_year, feature_imdb_id)
                if built is None:
                    continue
                candidate, candidate_id, download = built
                # A duplicate key is the same provider record; preserving the
                # first result maintains provider ordering without ambiguity.
                if candidate_id not in downloads:
                    candidates.append(candidate)
                    downloads[candidate_id] = download
        return candidates, downloads

    def search_filename(
        self,
        filename: str,
        identity: MovieIdentity,
    ) -> tuple[list[Candidate], dict[str, SubdlDownload]]:
        """Use SubDL's release-aware v2 media-manager search endpoint.

        The API documents ``/files/search`` as the route for library scanners:
        it returns the movie identity plus a per-subtitle ``match_score`` that
        measures release-name similarity. Only the basename is sent, never a
        local directory path.
        """
        if not self.api_key:
            return [], {}
        # ``Path.name`` on Linux does not split a Windows backslash path, so
        # normalize both separators before taking the basename.
        name = str(filename).replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or "\x00" in name or len(name) > 512:
            return [], {}
        payload = self._request_json(
            "/files/search",
            {
                "filename": name,
                "type": "movie",
                "languages": "en",
                "hi": "0",
                "subs_per_page": "30",
            },
        )
        return self._parse_search_payload(payload, identity, require_match=True)

    def search_identity(self, identity: MovieIdentity) -> tuple[list[Candidate], dict[str, SubdlDownload]]:
        """Use documented title search only when filename matching found nothing."""
        if not self.api_key:
            return [], {}
        payload = self._request_json(
            "/subtitles/search",
            {
                "film_name": identity.title,
                "type": "movie",
                "languages": "en",
                "unpack": "1",
            },
        )
        return self._parse_search_payload(payload, identity)

    def _download_bytes(self, url: str, max_bytes: int) -> bytes:
        self._throttle()
        request = urllib.request.Request(url, headers=self._headers("application/octet-stream, */*;q=0.1"))
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 - URL is provider-host validated or locally built
                return self._read_limited(response, max_bytes, "subtitle")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"SubDL subtitle download HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"SubDL subtitle download network error: {exc.reason}") from exc

    def download_srt(
        self,
        download: SubdlDownload,
        dest: Path,
        *,
        video: Path | None = None,
        expected_video: VideoSnapshot | None = None,
        max_bytes: int = MAX_SUBTITLE_BYTES,
    ) -> None:
        """Download, validate, snapshot-check, and atomically publish one SRT."""
        if download.url:
            url = normalize_subdl_download_url(download.url)
        elif download.n_id:
            subtitle_id = _subdl_identifier(download.n_id)
            if not subtitle_id:
                raise RuntimeError("SubDL candidate has an invalid subtitle identifier")
            # The documented ``format=file`` mode returns a non-ZIP payload
            # only when SubDL can identify one obvious file. That is safer than
            # silently choosing from an archive; unexpected ZIP responses still
            # pass through the one-SRT-only validator below.
            url = (
                f"{SUBDL_API_BASE}/subtitles/{urllib.parse.quote(subtitle_id, safe='')}/download?"
                "format=file"
            )
        else:
            raise RuntimeError("SubDL candidate has no safe download reference")

        data = self._download_bytes(url, max_bytes)
        redirected_url = subdl_download_redirect_url(data)
        if redirected_url is not None:
            data = self._download_bytes(redirected_url, max_bytes)
        text = decode_subdl_srt_payload(data, max_bytes)
        if video is not None and expected_video is not None and not video_snapshot_matches(video, expected_video):
            raise RuntimeError("movie changed during subtitle lookup; downloaded SRT was not activated")
        try:
            atomic_write_text(dest, text, replace=False)
        except FileExistsError as exc:
            raise ConcurrentSidecarError("English SRT appeared during download; preserved the existing sidecar") from exc

def download_subdl_srt(
    url: str,
    dest: Path,
    max_bytes: int,
    *,
    api_key: str = "",
    video: Path | None = None,
    expected_video: VideoSnapshot | None = None,
) -> None:
    """Backward-compatible raw-URL helper; new queue code uses ``SubdlClient``."""
    client = SubdlClient(api_key)
    client.download_srt(
        SubdlDownload(url=normalize_subdl_download_url(url)),
        dest,
        video=video,
        expected_video=expected_video,
        max_bytes=max_bytes,
    )

def atomic_write_text(dest: Path, text: str, *, replace: bool = True) -> None:
    """Publish verified UTF-8 text atomically, optionally refusing replacement."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = dest.with_name(f".{dest.name}.partial.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with stage.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(str(stage), str(dest))
        else:
            # ``link`` is an atomic create-if-absent operation. It prevents a
            # concurrent/manual English sidecar from being silently replaced.
            os.link(str(stage), str(dest))
            stage.unlink()
    except OSError:
        try:
            stage.unlink(missing_ok=True)
        except OSError:
            pass
        raise

def as_bool(value: Any) -> bool:
    """API fields arrive as true/false, 0/1, or the strings \"0\"/\"true\"."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

def decode_subtitle_bytes(data: bytes) -> str:
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as archive:
            data = archive.read(MAX_SUBTITLE_BYTES + 1)
        if len(data) > MAX_SUBTITLE_BYTES:
            raise ValueError("decompressed subtitle exceeds safety limit")
    for enc in EXTERNAL_SRT_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    # Unlike the shared decode_srt_bytes helper this must return a string:
    # the caller inspects a rejected download to explain why it was rejected.
    return data.decode("utf-8", errors="replace")

def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

def _nonnegative_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0

def normalize_language(value: str) -> str:
    return value.strip().casefold()

def normalize_title(value: str) -> str:
    """Return a punctuation/diacritic-insensitive title key for exact comparison."""
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", plain.casefold()).strip()

def movie_identity_from_video(video: Path) -> MovieIdentity | None:
    """Accept only a canonical ``Title (YYYY).mkv`` name as fallback input."""
    match = MOVIE_IDENTITY_RE.fullmatch(video.stem.strip())
    if not match:
        return None
    title = match.group("title").strip()
    normalized = normalize_title(title)
    if not normalized:
        return None
    return MovieIdentity(title=title, year=int(match.group("year")), normalized_title=normalized)

def release_has_edition_marker(release: str) -> bool:
    normalized = normalize_title(release)
    return any(marker in normalized for marker in EDITION_MARKERS)

def release_has_bluray_keyword(release: str) -> bool:
    """True when the release name carries an explicit Blu-ray keyword."""
    return bool(BLURAY_RELEASE_RE.search(release or ""))

def release_matches_movie_title(release: str, title: str) -> bool:
    """True when ``title`` appears as a whole phrase inside the release name.

    Both sides go through ``normalize_title`` (case-, punctuation- and
    diacritic-insensitive) and the match is phrase-boundary aware, so the
    title "Alien" does not match an "Aliens" release and vice versa.
    """
    normalized_title = normalize_title(title)
    if not normalized_title:
        return False
    normalized_release = normalize_title(release or "")
    return re.search(rf"(?<!\w){re.escape(normalized_title)}(?!\w)", normalized_release) is not None

def release_contains_year(release: str, year: int) -> bool:
    """True when the release name carries ``year`` as a standalone number.

    Digit boundaries keep "2009" from matching "20091" and let the check run
    on the normalized name, where ``.``/``-``/``_`` separators are spaces.
    """
    if not 1000 <= int(year) <= 9999:
        return False
    normalized_release = normalize_title(release or "")
    return re.search(rf"(?<!\d){int(year)}(?!\d)", normalized_release) is not None

def release_matches_movie_identity(release: str, identity: MovieIdentity) -> bool:
    """True when the release name names the movie: title and release year."""
    return (
        release_matches_movie_title(release, identity.title)
        and release_contains_year(release, identity.year)
    )

def candidate_rank_key(candidate: Candidate) -> tuple:
    """Downloads-first selection key shared by every automatic picker.

    The candidate with the most downloads wins; the historical quality
    signals (trusted flag, community rating, votes) remain as tiebreakers so
    a download-count tie still resolves deterministically instead of by file
    id.
    """
    return (-candidate.downloads, -int(candidate.trusted), -candidate.rating, -candidate.votes)

def parse_candidates(payload: dict[str, Any]) -> list[Candidate]:
    out: list[Candidate] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") or {}
        feature = attrs.get("feature_details") or {}
        if not isinstance(feature, dict):
            feature = {}
        files = attrs.get("files") or []
        if not files or not isinstance(files[0], dict):
            continue
        file_id = files[0].get("file_id")
        if file_id is None:
            continue
        out.append(
            Candidate(
                file_id=int(file_id),
                release=str(files[0].get("file_name") or attrs.get("release") or ""),
                moviehash_match=as_bool(attrs.get("moviehash_match")),
                downloads=_nonnegative_int(attrs.get("download_count")),
                votes=_nonnegative_int(attrs.get("votes")),
                rating=_nonnegative_float(attrs.get("ratings")),
                trusted=as_bool(attrs.get("from_trusted")),
                hearing_impaired=as_bool(attrs.get("hearing_impaired")),
                machine_translated=as_bool(attrs.get("machine_translated")),
                ai_translated=as_bool(attrs.get("ai_translated")),
                foreign_parts_only=as_bool(attrs.get("foreign_parts_only")),
                language=str(attrs.get("language") or "en"),
                feature_title=str(feature.get("title") or ""),
                feature_year=_nonnegative_int(feature.get("year")),
                feature_imdb_id=_nonnegative_int(feature.get("imdb_id")),
            )
        )
    return out

def _is_normal_english_human_candidate(candidate: Candidate) -> bool:
    return (
        normalize_language(candidate.language) in ENGLISH_LANGUAGE_TOKENS
        and not candidate.machine_translated
        and not candidate.ai_translated
        and not candidate.hearing_impaired
        and not candidate.foreign_parts_only
    )
def pick_candidate(
    cands: Sequence[Candidate], cfg: Config, *, identity: MovieIdentity | None = None,
) -> Candidate | None:
    """Return one strict best candidate for the requested English subtitle mode.

    A candidate must be a moviehash match on a normal English human SRT whose
    release name carries an explicit Blu-ray keyword and, when ``identity`` is
    given, names the movie (title and release year). Among the qualifying
    candidates the highest download count wins; the trusted flag, rating and
    votes remain as tiebreakers. This yields one deterministic SRT.
    """
    usable = [
        candidate for candidate in cands
        if candidate.moviehash_match
        and _is_normal_english_human_candidate(candidate)
        and release_has_bluray_keyword(candidate.release)
        and (identity is None or release_matches_movie_identity(candidate.release, identity))
    ]
    if not usable:
        return None
    usable.sort(
        key=lambda candidate: (
            *candidate_rank_key(candidate),
            str(candidate.file_id), candidate.release.casefold(),
        ),
    )
    return usable[0]

def pick_identity_candidate(cands: Sequence[Candidate], identity: MovieIdentity) -> tuple[Candidate | None, str]:
    """Choose one non-hash candidate when identity and release name agree.

    Title/year must exactly match provider feature metadata, and the release
    name must carry the movie title, the release year, and an explicit
    Blu-ray keyword. Among the qualifying candidates the highest download
    count wins, with the trusted flag, rating and votes as tiebreakers - no
    separate quality floor, so popular but unvoted subtitles still fetch.
    Edition-labelled releases are deliberately not auto-selected because a
    canonical local name contains no reliable edition/cut marker to compare
    against.
    """
    usable = [
        candidate for candidate in cands
        if _is_normal_english_human_candidate(candidate)
        and candidate.feature_year == identity.year
        and normalize_title(candidate.feature_title) == identity.normalized_title
        and not release_has_edition_marker(candidate.release)
        and release_has_bluray_keyword(candidate.release)
        and release_matches_movie_identity(candidate.release, identity)
    ]
    if not usable:
        return None, "no title/year-exact Blu-ray release naming the movie and its release year"
    usable.sort(
        key=lambda candidate: (
            *candidate_rank_key(candidate),
            str(candidate.file_id), candidate.release.casefold(),
        ),
    )
    top = usable[0]
    top_key = candidate_rank_key(top)
    tied = [candidate for candidate in usable if candidate_rank_key(candidate) == top_key]
    if len(tied) != 1:
        return None, "multiple equally ranked title/year-exact Blu-ray SRT candidates require review"
    return top, "title/year exact; Blu-ray release naming the movie and its release year; highest download count"

def pick_pooled_candidates(
    entries: list[tuple[Candidate, str, str, str]],
    identity: MovieIdentity | None,
) -> tuple[Candidate | None, str, str, str]:
    """Rank same-tier candidates from different providers as equal sources.

    ``entries`` are ``(candidate, provider, method, provider_reason)`` tuples,
    at most one per provider. A lone entry stands exactly as its provider
    selected it. When both providers contribute, every contributor must also
    carry the release-name policy - movie title, release year and an explicit
    Blu-ray keyword - and the highest download count wins regardless of
    provider; an unbroken tie is held for manual review rather than resolved
    by a provider default.
    """
    if not entries:
        return None, "", "", ""
    if len(entries) == 1:
        candidate, provider, method, reason = entries[0]
        return candidate, provider, method, reason
    conforming: list[tuple[Candidate, str, str, str]] = []
    rejected: list[str] = []
    for candidate, provider, method, reason in entries:
        if release_has_bluray_keyword(candidate.release) and (
            identity is None or release_matches_movie_identity(candidate.release, identity)
        ):
            conforming.append((candidate, provider, method, reason))
        else:
            rejected.append(provider_label(provider))
    if not conforming:
        return (
            None, "", "",
            f"no release met the selection policy on either provider ({'; '.join(rejected)} rejected)",
        )
    if len(conforming) == 1:
        candidate, provider, method, reason = conforming[0]
        return candidate, provider, method, f"{reason}; {'; '.join(rejected)} release did not meet the selection policy"
    conforming.sort(
        key=lambda entry: (
            *candidate_rank_key(entry[0]),
            entry[1], str(entry[0].file_id), entry[0].release.casefold(),
        ),
    )
    top_key = candidate_rank_key(conforming[0][0])
    tied = [entry for entry in conforming if candidate_rank_key(entry[0]) == top_key]
    if len(tied) != 1:
        return None, "", "", "multiple equally ranked candidates across providers require review"
    candidate, provider, method, reason = tied[0]
    loser = next(entry for entry in conforming if entry[1] != provider)
    return (
        candidate, provider, method,
        f"{reason}; best across both providers (beats {provider_label(loser[1])})",
    )

def looks_like_srt(text: str) -> bool:
    """The shared verdict on whether text contains a well-formed SRT cue.

    This used to be a private copy of the cue pattern, and it had drifted: it
    anchored the cue number at column 0 while the other four tools allowed
    leading whitespace. A subtitle with an indented cue number was therefore
    rejected here at download time ("downloaded payload is not a valid SRT
    subtitle") yet accepted as canonical by library_auditor, movie_standardizer
    and mkv_track_cleaner. Delegating to the shared helper makes that
    disagreement impossible.
    """
    return srt_looks_valid(text)

def is_english_srt_sidecar(path: Path, video_stem: str) -> bool:
    """Return true only for an English SRT attached to this exact movie stem."""
    if not path.is_file() or path.is_symlink() or path.suffix.lower() != DIRECT_PLAY_SUBTITLE_EXTENSION:
        return False
    prefix = video_stem.casefold() + "."
    stem = path.stem.casefold()
    if not stem.startswith(prefix):
        return False
    tokens = [token for token in stem[len(prefix):].split(".") if token]
    # Jellyfin permits descriptive title fields, so only require that one token
    # is English. The filename prefix check keeps a neighboring movie's SRT from
    # blocking this fetch.
    return any(token in ENGLISH_LANGUAGE_TOKENS for token in tokens)

def has_english_sidecar(folder: Path, video_stem: str) -> Path | None:
    """Return the first direct-play-safe English SRT for this exact movie file."""
    try:
        names = sorted(folder.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return None
    return next((path for path in names if is_english_srt_sidecar(path, video_stem)), None)

def discover_videos(root: Path, min_bytes: int) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d.strip().lower() not in DISC_DIR_NAMES
            and d.strip().lower() not in EXTRA_DIR_NAMES
            and not (Path(dirpath) / d).is_symlink()
        ]
        current = Path(dirpath)
        for name in filenames:
            if SAMPLE_NAME_RE.search(Path(name).stem):
                continue
            ext = Path(name).suffix.lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            path = current / name
            if path.is_symlink():
                continue
            try:
                if path.stat().st_size < min_bytes:
                    continue
            except OSError:
                continue
            found.append(path)
    found.sort(key=lambda p: str(p).casefold())
    return found

def canonical_movie_layout_issue(video: Path, library: Path) -> str | None:
    """Return a reason when a file violates the one-movie-per-folder contract."""
    parent = video.parent
    if parent == library:
        return "noncanonical layout: movie MKV is directly under the library root"
    if parent.is_symlink() or video.is_symlink() or not video.is_file():
        return "noncanonical layout: movie is not a regular non-symlink file in a regular folder"
    if video.stem.casefold() != parent.name.casefold():
        return "noncanonical layout: MKV stem does not match its movie-folder name"
    try:
        sibling_mkvs = [
            item for item in parent.iterdir()
            if item.suffix.lower() == ".mkv" and item.is_file() and not item.is_symlink()
        ]
    except OSError as exc:
        return f"noncanonical layout: could not inspect movie folder ({exc})"
    if len(sibling_mkvs) != 1:
        return f"noncanonical layout: expected one regular MKV in movie folder, found {len(sibling_mkvs)}"
    return None

def dest_for(video: Path, cfg: Config) -> Path:
    # Plex/Jellyfin: file next to the video, same stem + language suffix.
    # cfg.sidecar_suffix is always EXTERNAL_SRT_SUFFIX (".eng.srt"); keep the
    # Config hook so a future override stays one call site away.
    _ = cfg.sidecar_suffix
    return exact_external_english_srt_path(video)

def run_self_tests() -> int:
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # Deterministic hash: 128 KiB of incrementing bytes.
    blob = bytes(i & 0xFF for i in range(MIN_HASH_SIZE))
    digest = moviehash_bytes(blob)
    check(len(digest) == 16 and all(c in "0123456789abcdef" for c in digest), f"hash format {digest}")
    # Same bytes → same hash
    check(moviehash_bytes(blob) == digest, "hash stable")
    # Size change changes hash
    blob2 = blob + b"\x00"
    check(moviehash_bytes(blob2[:MIN_HASH_SIZE]) == digest, "same first/last 128k")

    payload = {
        "data": [
            {"attributes": {
                "moviehash_match": False, "download_count": 99999,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en", "release": "wrong",
                "files": [{"file_id": 1, "file_name": "fuzzy.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 12, "from_trusted": True,
                "ratings": 8.5, "votes": 8,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en", "release": "hashy",
                "files": [{"file_id": 2, "file_name": "Knowing.2009.BluRay.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 500,
                "machine_translated": True, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 3, "file_name": "mt.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 9000, "from_trusted": True,
                "ratings": 10, "votes": 100,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "fr",
                "files": [{"file_id": 4, "file_name": "wrong-language.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 1000,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": True, "foreign_parts_only": False,
                "language": "eng",
                "files": [{"file_id": 5, "file_name": "sdh.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 1000,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": True,
                "language": "english",
                "files": [{"file_id": 6, "file_name": "forced.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 500,
                "ratings": 6.5, "votes": 10,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 7, "file_name": "Knowing.2009.1080p.BluRay.ENG.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 300, "from_trusted": True,
                "ratings": 10, "votes": 100,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 8, "file_name": "Knowing.2009.2160p.BluRay.ENG.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 9999, "from_trusted": True,
                "ratings": 10, "votes": 100,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 9, "file_name": "Inception.2010.1080p.BluRay.ENG.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 50000, "from_trusted": True,
                "ratings": 10, "votes": 100,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 10, "file_name": "Knowing.2009.1080p.WEB.ENG.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 7000, "from_trusted": True,
                "ratings": 10, "votes": 100,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 11, "file_name": "Knowing.2010.1080p.BluRay.ENG.srt"}],
            }},
        ]
    }
    cands = parse_candidates(payload)
    hash_identity = MovieIdentity("Knowing", 2009, "knowing")
    # Downloads-first: without an identity the most-downloaded Blu-ray release
    # wins even though it names another movie; with the movie identity the
    # Inception upload and the wrong-year Knowing upload drop out and the
    # 2009 Knowing release with the most downloads wins. The 50k-download WEB
    # release never qualifies.
    pick = pick_candidate(cands, Config())
    check(pick is not None and pick.file_id == 9, f"downloads-first pick {pick}")
    pick_named = pick_candidate(cands, Config(), identity=hash_identity)
    check(pick_named is not None and pick_named.file_id == 7, f"named downloads-first pick {pick_named}")
    web_candidate = next(candidate for candidate in cands if candidate.file_id == 10)
    check(pick_candidate([web_candidate], Config()) is None,
          "non-Blu-ray release must not be auto-selected")
    inception_candidate = next(candidate for candidate in cands if candidate.file_id == 9)
    check(pick_candidate([inception_candidate], Config(), identity=hash_identity) is None,
          "release for another movie must not be picked")
    check(pick_candidate([inception_candidate], Config(),
                         identity=MovieIdentity("Inception", 2010, "inception")) is not None,
          "title/year-matched release is selectable")
    wrong_year = next(candidate for candidate in cands if candidate.file_id == 11)
    check(pick_candidate([wrong_year], Config(), identity=hash_identity) is None,
          "wrong release year must not be picked")
    check(pick_candidate([candidate for candidate in cands if candidate.hearing_impaired], Config()) is None,
          "SDH candidates must be excluded")
    check(pick_candidate([candidate for candidate in cands if candidate.foreign_parts_only], Config()) is None,
          "forced/foreign-part candidates must be excluded")
    check(pick_candidate([candidate for candidate in cands if not candidate.moviehash_match], Config()) is None,
          "no hash match → none")
    os_pick = next(candidate for candidate in cands if candidate.file_id == 7)
    subdl_pick = next(candidate for candidate in cands if candidate.file_id == 8)
    web_pick = next(candidate for candidate in cands if candidate.file_id == 10)
    # Equal sources: when both providers offer a qualifying release, the
    # most-downloaded one wins regardless of provider.
    pooled, pooled_provider, _pooled_method, pooled_reason = pick_pooled_candidates(
        [(subdl_pick, PROVIDER_SUBDL, "subdl-release", "subdl release match"),
         (os_pick, PROVIDER_OPENSUBTITLES, "hash", "moviehash match")],
        hash_identity,
    )
    check(pooled is not None and pooled.file_id == 7 and pooled_provider == PROVIDER_OPENSUBTITLES,
          f"pool picks the most-downloaded release across providers ({pooled_reason})")
    # A non-qualifying (WEB) release from one provider never beats a
    # qualifying release from the other.
    pooled2, provider2, _method2, reason2 = pick_pooled_candidates(
        [(web_pick, PROVIDER_SUBDL, "subdl-release", "subdl release match"),
         (os_pick, PROVIDER_OPENSUBTITLES, "hash", "moviehash match")],
        hash_identity,
    )
    check(pooled2 is not None and pooled2.file_id == 7 and provider2 == PROVIDER_OPENSUBTITLES,
          f"non-qualifying provider release loses to the qualifying one ({reason2})")
    # An unbroken cross-provider tie is held for review, not defaulted.
    twin_pick = Candidate(**os_pick.__dict__)
    tied_pool, _p, _m, tied_reason = pick_pooled_candidates(
        [(os_pick, PROVIDER_OPENSUBTITLES, "hash", "moviehash match"),
         (twin_pick, PROVIDER_SUBDL, "subdl-release", "subdl release match")],
        hash_identity,
    )
    check(tied_pool is None and "review" in tied_reason,
          f"cross-provider ties remain review-only ({tied_reason})")

    sample = (
        "1\n"
        "00:00:01,000 --> 00:00:02,000\n"
        "Hello\n"
    )
    check(looks_like_srt(sample), "srt detect")
    check(not looks_like_srt("<html>nope</html>"), "html not srt")

    subdl_identity = MovieIdentity("Knowing", 2009, "knowing")
    subdl_candidate = Candidate(
        file_id="subdl:fixture", release="Knowing.2009.1080p.BluRay",
        moviehash_match=False, downloads=0, votes=0, rating=0.0, trusted=False,
        hearing_impaired=False, machine_translated=False, ai_translated=False,
        foreign_parts_only=False, language="en", feature_title="Knowing", feature_year=2009,
        subdl_match_score=0.92,
    )
    subdl_pick, _subdl_reason = pick_subdl_identity_candidate([subdl_candidate], subdl_identity)
    check(subdl_pick == subdl_candidate, "SubDL unique title/year fallback")
    subdl_release_pick, _subdl_release_reason = pick_subdl_identity_candidate(
        [subdl_candidate], subdl_identity, require_release_match_score=True,
    )
    check(subdl_release_pick == subdl_candidate, "SubDL confident release match")

    def ident_candidate(file_id, release, downloads, rating, votes, trusted):
        return Candidate(
            file_id=file_id, release=release, moviehash_match=False, downloads=downloads,
            votes=votes, rating=rating, trusted=trusted, hearing_impaired=False,
            machine_translated=False, ai_translated=False, foreign_parts_only=False,
            language="en", feature_title="Knowing", feature_year=2009,
        )

    popular_id = ident_candidate(21, "Knowing.2009.1080p.BluRay.ENG.srt", 300, 8.5, 25, False)
    elite_id = ident_candidate(22, "Knowing.2009.2160p.BluRay.ENG.srt", 100, 10.0, 50, True)
    web_id = ident_candidate(23, "Knowing.2009.1080p.WEB.ENG.srt", 9999, 10.0, 100, True)
    twin_id = ident_candidate(24, "Knowing.2009.1080p.BluRay.OTHER-GROUP.srt", 300, 8.5, 25, False)
    identity_pick, identity_reason = pick_identity_candidate([elite_id, popular_id, web_id], subdl_identity)
    check(identity_pick is not None and identity_pick.file_id == 21,
          f"identity downloads-first pick {identity_pick} ({identity_reason})")
    check(pick_identity_candidate([web_id], subdl_identity)[0] is None,
          "non-Blu-ray release must not pass the identity policy")
    tied_pick, tied_reason = pick_identity_candidate([popular_id, twin_id], subdl_identity)
    check(tied_pick is None and "review" in tied_reason,
          f"tied download counts still held for review ({tied_reason})")
    # No quality floor: a popular-but-unvoted Blu-ray release is auto-selected.
    fresh_id = ident_candidate(25, "Knowing.2009.1080p.BluRay.ENG.srt", 120, 0.0, 0, False)
    fresh_pick, fresh_reason = pick_identity_candidate([fresh_id], subdl_identity)
    check(fresh_pick is not None and fresh_pick.file_id == 25,
          f"popular-but-unvoted subtitle is auto-selected ({fresh_reason})")
    wrong_year_id = ident_candidate(26, "Knowing.2010.1080p.BluRay.ENG.srt", 9999, 10.0, 100, True)
    check(pick_identity_candidate([wrong_year_id], subdl_identity)[0] is None,
          "wrong release year must not pass the identity policy")
    check(
        normalize_subdl_download_url("/subtitle/fixture/file") == "https://dl.subdl.com/subtitle/fixture/file",
        "SubDL relative download URL is constrained",
    )
    try:
        normalize_subdl_download_url("https://example.invalid/subtitle/fixture")
        errors.append("untrusted SubDL URL unexpectedly accepted")
    except ValueError:
        pass

    tmp = Path(tempfile.mkdtemp(prefix="subf_"))
    try:
        movie = tmp / "Knowing (2009)"
        extra = movie / "Featurettes"
        extra.mkdir(parents=True)
        vid = movie / "Knowing (2009).mkv"
        with vid.open("wb") as fh:
            fh.truncate(400 * 1024 * 1024)
        (extra / "Making-Of.mkv").write_bytes(b"x")
        sidecar = movie / f"Knowing (2009){EXTERNAL_SRT_SUFFIX}"
        sidecar.write_text(sample, encoding="utf-8")
        (movie / f"Another Movie (2009){EXTERNAL_SRT_SUFFIX}").write_text(sample, encoding="utf-8")
        (movie / "Knowing (2009).eng.ass").write_text("[Script Info]", encoding="utf-8")
        with (movie / "Knowing (2009).mp4").open("wb") as fh:
            fh.truncate(400 * 1024 * 1024)
        found = discover_videos(tmp, 300 * 1024 * 1024)
        check(found == [vid], f"discover {found}")
        check(has_english_sidecar(movie, "Knowing (2009)") == sidecar, "exact existing English SRT")
        check(not is_english_srt_sidecar(movie / f"Another Movie (2009){EXTERNAL_SRT_SUFFIX}", "Knowing (2009)"),
              "neighboring movie subtitle must not block download")
        check(not is_english_srt_sidecar(movie / "Knowing (2009).eng.ass", "Knowing (2009)"),
              "non-SRT sidecar must not count as direct-play policy output")

        guarded = movie / f"Guarded{EXTERNAL_SRT_SUFFIX}"
        atomic_write_text(guarded, sample, replace=False)
        try:
            atomic_write_text(guarded, "1\\n00:00:00,000 --> 00:00:01,000\\nreplacement\\n", replace=False)
            errors.append("create-only sidecar write unexpectedly replaced destination")
        except FileExistsError:
            pass
        check(guarded.read_text(encoding="utf-8") == sample, "create-only sidecar retains existing content")
        check(not list(movie.glob(f".Guarded{EXTERNAL_SRT_SUFFIX}.partial.*")), "create-only sidecar leaves no temp")

        # Legacy .en.srt is promoted to the canonical .eng.srt on inspect.
        legacy_movie = tmp / "Legacy Film (2010)"
        legacy_movie.mkdir()
        legacy_vid = legacy_movie / "Legacy Film (2010).mkv"
        with legacy_vid.open("wb") as fh:
            fh.truncate(400 * 1024 * 1024)
        (legacy_movie / "Legacy Film (2010).en.srt").write_text(sample, encoding="utf-8")
        status, path, detail, _reason = inspect_existing_sidecars(legacy_vid)
        check(status == "covered", f"legacy .en.srt promotes to covered: {status} {detail}")
        check(path is not None and path.name.endswith(EXTERNAL_SRT_SUFFIX), f"promoted path {path}")
        check(not (legacy_movie / "Legacy Film (2010).en.srt").exists(), "legacy .en.srt removed after promote")

        snapshot = video_snapshot(vid)
        with vid.open("ab") as fh:
            fh.write(b"changed")
        check(not video_snapshot_matches(vid, snapshot), "video snapshot detects change")
        try:
            decode_subtitle_bytes(gzip.compress(b"x" * (MAX_SUBTITLE_BYTES + 1)))
            errors.append("oversized gzip subtitle unexpectedly accepted")
        except ValueError:
            pass

        bad_cfg = QueueConfig(
            library=movie, report_file=movie / "report.txt", log_file=tmp / "log.txt",
        )
        check(bool(validate_compact_config(bad_cfg)), "report-inside-library validation")

        # The normal workflow is limited to a log and a report. Verify that a
        # durable quota/retry checkpoint can be reconstructed from the log alone.
        ledger_log = tmp / "subtitle_fetcher.log"
        ledger_state = new_state(tmp)
        ledger_day = day_ledger(ledger_state, "2026-01-02")
        ledger_day["download_requests_reserved"] = 1
        ledger_state["movies"]["fixture"] = {
            "path": str(vid), "status": "reserved", "attempts": 1,
        }
        ledger_state["_dirty_movies"].add("fixture")
        persist_state(ledger_state, ledger_log)
        recovered_ledger = load_state(ledger_log, tmp)
        check(
            recovered_ledger["days"].get("2026-01-02", {}).get("download_requests_reserved") == 1,
            "log ledger recovers reserved download count",
        )
        check(
            recovered_ledger["movies"].get("fixture", {}).get("status") == "reserved",
            "log ledger recovers pending movie status",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- vendored scraping sources: adapter/chain self-tests --------------
    run_scrape_self_tests(errors)

    # ---- scraping fallback tier (queue wiring) ----------------------------
    check(active_scrape_sources(QueueConfig(library=Path("/x"), log_file=None, report_file=Path("/r"))) == (),
          "scraping tier is off by default in bare QueueConfig")
    cfg_scrape = QueueConfig(library=Path("/x"), log_file=None, report_file=Path("/r"), scrape_daily_cap=20)
    check(active_scrape_sources(cfg_scrape) == SCRAPE_PROVIDER_ORDER,
          "scraping tier enables all seven sources in failover order")
    check(active_scrape_sources(QueueConfig(library=Path("/x"), log_file=None, report_file=Path("/r"),
                                            scrape_daily_cap=20, skip_sources=("subf2me",)))
          == SCRAPE_PROVIDER_ORDER[1:],
          "skip_sources removes one source")
    check(provider_daily_cap(cfg_scrape, "subf2me") == 20
          and provider_reservation_field("subf2me") == "subf2me_search_requests_reserved"
          and provider_success_field("subf2me") == "subf2me_successful_downloads"
          and provider_label("subf2me") == SCRAPE_PROVIDER_LABELS["subf2me"],
          "scraping keys map onto the generic quota helpers")
    scrape_only_cfg = QueueConfig(library=Path("/x"), log_file=None, report_file=Path("/r"),
                                  scrape_daily_cap=20)
    scrape_only_cfg.library = Path(__file__).parent  # an existing directory
    check(validate_compact_config(scrape_only_cfg) == [],
          "a scraping-only configuration (no API keys) is valid")
    dead_cfg = QueueConfig(library=Path(__file__).parent, log_file=None,
                           report_file=Path("/r"))
    check(any("scraping sources enabled" in e for e in validate_compact_config(dead_cfg)),
          "no API keys and no scraping sources is rejected")

    class _FakeScrapeT(ScrapeTransport):
        def __init__(self, routes: dict[str, bytes]) -> None:
            super().__init__(gap=0.0)
            self.routes = routes

        def _route(self, url: str) -> bytes:
            best: tuple[int, bytes] | None = None
            for prefix, payload in self.routes.items():
                if url.startswith(prefix) and (best is None or len(prefix) > best[0]):
                    best = (len(prefix), payload)
            if best is None:
                raise ScrapeSourceError(f"unrouted {url}")
            return best[1]

        def get(self, url: str, *, headers: dict[str, str] | None = None) -> bytes:
            return self._route(url)

        def post(self, url: str, form: dict[str, str], *, headers: dict[str, str] | None = None) -> bytes:
            try:
                return self._route(url)
            except ScrapeSourceError as exc:
                raise ScrapeSourceError(f"unrouted POST {url}") from exc

    sample_srt = "1\n00:00:01,000 --> 00:00:03,000\nhello\n\n2\n00:00:04,000 --> 00:00:06,000\nworld\n"
    import zipfile as _zf
    import io as _io
    _buf = _io.BytesIO()
    with _zf.ZipFile(_buf, "w", _zf.ZIP_DEFLATED) as _z:
        _z.writestr("The.Father.2020.utf.srt", sample_srt.encode("utf-8"))
    subf2me_zip = _buf.getvalue()
    subf2me_routes = {
        "https://subf2m.co/subtitles/searchbytitle": (
            b"<html><body><div class=\"search-result\"><h2 class=\"close\">close</h2>"
            b"<ul><li><a href=\"/subtitles/222\">The Father (2020)</a></li>"
            b"<li><a href=\"/subtitles/333\">The Father (2019)</a></li></ul></div></body></html>"
        ).decode("utf-8").encode("utf-8"),
        "https://subf2m.co/subtitles/222/en": (
            b"<html><body><ul><li class=\"item\"><li>playWEB</li>"
            b"<a class=\"download icon-download\" href=\"/subtitles/222/en/999\"></a></li></ul>"
            b"</body></html>"
        ).decode("utf-8").encode("utf-8"),
        "https://subf2m.co/subtitles/222/en/999": (
            b"<html><body><div class=\"download\"><a href=\"/dl/file.zip\">zip</a>"
            b"</div></body></html>"
        ).decode("utf-8").encode("utf-8"),
        "https://subf2m.co/dl/file.zip": subf2me_zip,
    }

    def run_scrape_queue(routes: dict[str, bytes], tmp: Path | None = None
                         ) -> tuple[list[JobResult], dict[str, Any], Path]:
        """Run one queue over a one-movie scraping-only library.

        Pass a previous tmp dir to run again over the same library and ledger
        (the same-UTC-day retry gate).
        """
        if tmp is None:
            tmp = Path(tempfile.mkdtemp(prefix="scrape-selftest-"))
        library = tmp / "library"
        movie = library / "The Father (2020)"
        if not movie.exists():
            movie.mkdir(parents=True)
            (movie / "The Father (2020).mkv").write_bytes(b"v" * 64)
        cfg = QueueConfig(
            library=library, log_file=tmp / "fetcher.log", report_file=tmp / "report.txt",
            scrape_daily_cap=20, min_movie_size_mb=0,
        )
        with mock.patch.object(sys.modules[__name__], "make_scrape_transport",
                               return_value=_FakeScrapeT(routes)):
            results, summary = queue_run(cfg)
        return results, summary, tmp

    results, summary, tmp = run_scrape_queue(subf2me_routes)
    sidecar = tmp / "library" / "The Father (2020)" / "The Father (2020).eng.srt"
    check(len(results) == 1 and results[0].status == "download"
          and results[0].reason == REASON_DOWNLOADED,
          "scraping tier downloads when every API source is absent")
    check(sidecar.exists() and sidecar.read_text(encoding="utf-8").startswith("1\n00:00:01"),
          "scraped SRT is written under the canonical sidecar name")
    check(summary.get("scrape_successful_downloads", {}).get("subf2me") == 1,
          "the scraping success is metered per source in the summary")
    check(summary.get("coverage_covered") == 1 and summary.get("coverage_total") == 1,
          "coverage counts the scraped movie as covered")
    check(all(summary.get("scrape_sources_enabled") and k in summary["scrape_sources_enabled"]
              for k in SCRAPE_PROVIDER_ORDER),
          "the summary names every enabled scraping source")
    report_text = build_report(results, QueueConfig(
        library=tmp / "library", log_file=tmp / "fetcher.log", report_file=tmp / "report.txt",
        scrape_daily_cap=20, min_movie_size_mb=0), summary)
    check("1/1 (100.0%)" in report_text and "Subf2m.co" in report_text,
          "the report shows 100% coverage and the scraping sources")
    shutil.rmtree(tmp, ignore_errors=True)

    results2, _summary2, tmp2 = run_scrape_queue({})
    check(len(results2) == 1 and results2[0].status == "review"
          and results2[0].reason == REASON_REVIEW,
          "a movie no scraping source can cover is held for review")
    detail2 = results2[0].detail
    for key in SCRAPE_PROVIDER_ORDER:
        check(scrape_provider_label(key) in detail2,
              f"the review detail names the verdict of {key}")
    state2 = load_state(tmp2 / "fetcher.log", tmp2 / "library")
    check(any(str(rec.get("scrape_failed_utc_day") or "") == utc_day() and rec.get("scrape_failed")
              for rec in state2["movies"].values()),
          "the scraping failure is persisted for the next-UTC-day retry")

    results3, _summary3, _tmp3 = run_scrape_queue({}, tmp=tmp2)
    check(len(results3) == 1 and results3[0].status == "skip"
          and results3[0].reason == REASON_QUOTA
          and "already exhausted" in results3[0].detail,
          "a movie exhausted today is not offered to the scraping tier twice")
    shutil.rmtree(tmp2, ignore_errors=True)

    if errors:
        print("SELF-TEST FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print("SELF-TEST PASSED (hash + OpenSubtitles/SubDL picks + SRT safety + discovery + "
          "transaction guards + scraping fallback tier)")
    return 0

@dataclass
class QueueConfig:
    library: Path
    log_file: Path | None
    report_file: Path
    api_key: str = ""
    subdl_api_key: str = ""
    username: str = ""
    password: str = ""
    # ``daily_cap`` remains the OpenSubtitles cap for backwards-compatible
    # command-line/config names. SubDL publishes independently metered search
    # and download quotas, both tracked in the same durable ledger.
    daily_cap: int = DEVELOPMENT_ANONYMOUS_DAILY_CAP
    subdl_daily_cap: int = SUBDL_DEFAULT_DAILY_CAP
    min_movie_size_mb: float = MIN_MOVIE_SIZE_MB
    lock_timeout_seconds: float = 60.0
    retry_no_match: bool = False
    identity_fallback: bool = True
    dry_run: bool = False
    limit: int = 0
    auth_mode: str = DEFAULT_AUTH_MODE
    # Appended to preserve positional compatibility with pre-search-cap callers.
    subdl_search_daily_cap: int = SUBDL_DEFAULT_SEARCH_DAILY_CAP
    # Scraping fallback tier (vendored section below). 0 disables the tier;
    # the CLI resolves its default to SCRAPE_DEFAULT_SEARCH_DAILY_CAP per source.
    scrape_daily_cap: int = 0
    # Scraping sources to skip entirely (keys from SCRAPE_PROVIDER_ORDER).
    skip_sources: tuple[str, ...] = ()
    # Exit 0 even when movies finish the run without a validated SRT.
    allow_missing: bool = False

    @property
    def min_bytes(self) -> int:
        return int(self.min_movie_size_mb * 1024 * 1024)

    def fetcher_config(self) -> Config:
        return Config(
            library=self.library,
            log_file=self.log_file,
            report_file=self.report_file,
            api_key=self.api_key,
            subdl_api_key=self.subdl_api_key,
            username=self.username,
            password=self.password,
            dry_run=self.dry_run,
            min_movie_size_mb=self.min_movie_size_mb,
            lock_timeout_seconds=self.lock_timeout_seconds,
            identity_fallback=self.identity_fallback,
            auth_mode=self.auth_mode,
        )

def utc_day() -> str:
    return datetime.now(UTC).date().isoformat()

def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(f".{path.name}.partial.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with stage.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(stage, path)
    finally:
        try:
            stage.unlink(missing_ok=True)
        except OSError:
            pass

def new_state(library: Path) -> dict[str, Any]:
    """Create in-memory retry and quota state reconstructed from the run log."""
    return {"library": path_norm(library), "days": {}, "movies": {}, "_dirty_movies": set()}

def _ledger_payload(state: dict[str, Any]) -> dict[str, Any]:
    """Return the changed movie records plus the small daily quota totals."""
    movies: dict[str, dict[str, Any]] = {}
    dirty = state.get("_dirty_movies") or set()
    for key in dirty:
        record = state["movies"].get(key)
        if isinstance(record, dict):
            movies[key] = {name: value for name, value in record.items() if name != "_dirty"}
    return {"library": state["library"], "days": state["days"], "movies": movies}

def load_state(log_path: Path | None, library: Path) -> dict[str, Any]:
    """Recover durable quota/retry state from append-only ledger events in the log.

    Ordinary log lines are ignored. A malformed or partial final event is ignored
    rather than blocking subtitle work; provider download reservations are never
    decremented, which keeps the quota guard conservative after interruption.
    """
    state = new_state(library)
    if log_path is None or not log_path.exists():
        return state
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                marker = line.find(LEDGER_EVENT + " ")
                if marker < 0:
                    continue
                try:
                    payload = json.loads(line[marker + len(LEDGER_EVENT) + 1:].strip())
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict) or payload.get("library") != state["library"]:
                    continue
                days, movies = payload.get("days"), payload.get("movies")
                if isinstance(days, dict) and isinstance(movies, dict):
                    state["days"].update(days)
                    state["movies"].update(movies)
    except OSError as exc:
        raise RuntimeError(f"could not read subtitle log ledger: {exc}") from exc
    return state

def persist_state(state: dict[str, Any], log_path: Path | None) -> None:
    """Append a compact, fsync-backed ledger checkpoint to the one allowed log."""
    if log_path is None:
        return
    payload = json.dumps(_ledger_payload(state), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] {LEDGER_EVENT} {payload}\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        state["_dirty_movies"] = set()
    except OSError as exc:
        raise RuntimeError(f"could not persist subtitle log ledger: {exc}") from exc

def day_ledger(state: dict[str, Any], day: str) -> dict[str, int]:
    """Return a backward-compatible per-provider quota ledger for one UTC day.

    Older logs have only ``download_requests_reserved`` and
    ``successful_downloads``. They are historical OpenSubtitles values, so map
    them to the provider-specific fields on first read and continue writing the
    legacy reservation field for a smooth upgrade.
    """
    ledger = state["days"].setdefault(day, {})
    legacy_open_reserved = ledger.get("opensubtitles_download_requests_reserved",
                                     ledger.get("download_requests_reserved", 0))
    legacy_open_successful = ledger.get("opensubtitles_successful_downloads",
                                       ledger.get("successful_downloads", 0))
    defaults: dict[str, Any] = {
        "opensubtitles_download_requests_reserved": legacy_open_reserved,
        "subdl_search_requests_reserved": 0,
        "subdl_download_requests_reserved": 0,
        "opensubtitles_successful_downloads": legacy_open_successful,
        "subdl_successful_downloads": 0,
        "successful_downloads": ledger.get("successful_downloads", 0),
        "no_match": 0,
        "identity_review": 0,
        "errors": 0,
        "already_have": 0,
    }
    for field_name, default in defaults.items():
        try:
            ledger[field_name] = max(0, int(ledger.get(field_name, default) or 0))
        except (TypeError, ValueError):
            ledger[field_name] = 0
    # Legacy consumers and existing reports use this field for the
    # OpenSubtitles reservation count. Do not make SubDL downloads consume it.
    ledger["download_requests_reserved"] = ledger["opensubtitles_download_requests_reserved"]
    return ledger

def configured_providers(cfg: QueueConfig) -> tuple[str, ...]:
    providers: list[str] = []
    if cfg.api_key.strip():
        providers.append(PROVIDER_OPENSUBTITLES)
    if cfg.subdl_api_key.strip():
        providers.append(PROVIDER_SUBDL)
    return tuple(providers)

def active_scrape_sources(cfg: QueueConfig) -> tuple[str, ...]:
    """Scraping fallback sources enabled for this run, in failover order.

    A zero ``scrape_daily_cap`` disables the whole tier; ``skip_sources``
    removes individual sites (for example one that is down for everyone).
    """
    if cfg.scrape_daily_cap < 1:
        return ()
    skipped = set(cfg.skip_sources)
    return tuple(key for key in SCRAPE_PROVIDER_ORDER if key not in skipped)

def scrape_sources_enabled(cfg: QueueConfig) -> bool:
    return bool(active_scrape_sources(cfg))

def provider_daily_cap(cfg: QueueConfig, provider: str) -> int:
    if provider == PROVIDER_OPENSUBTITLES:
        return cfg.daily_cap
    if provider == PROVIDER_SUBDL:
        return cfg.subdl_daily_cap
    if is_scrape_provider(provider):
        return cfg.scrape_daily_cap
    raise ValueError(f"unknown subtitle provider: {provider}")

def provider_reservation_field(provider: str) -> str:
    if provider == PROVIDER_OPENSUBTITLES:
        return "opensubtitles_download_requests_reserved"
    if provider == PROVIDER_SUBDL:
        return "subdl_download_requests_reserved"
    if is_scrape_provider(provider):
        # Scraping sources meter one durable reservation per search; the
        # follow-up candidate downloads belong to the same search.
        return f"{provider}_search_requests_reserved"
    raise ValueError(f"unknown subtitle provider: {provider}")

def provider_success_field(provider: str) -> str:
    if provider == PROVIDER_OPENSUBTITLES:
        return "opensubtitles_successful_downloads"
    if provider == PROVIDER_SUBDL:
        return "subdl_successful_downloads"
    if is_scrape_provider(provider):
        return f"{provider}_successful_downloads"
    raise ValueError(f"unknown subtitle provider: {provider}")

def provider_reserved(ledger: dict[str, int], provider: str) -> int:
    return int(ledger.get(provider_reservation_field(provider), 0) or 0)

def provider_has_quota(cfg: QueueConfig, ledger: dict[str, int], provider: str) -> bool:
    return provider_reserved(ledger, provider) < provider_daily_cap(cfg, provider)

def subdl_search_reserved(ledger: dict[str, int]) -> int:
    """Return durable SubDL search requests reserved for the current UTC day."""
    return max(0, int(ledger.get("subdl_search_requests_reserved", 0) or 0))

def subdl_search_has_quota(cfg: QueueConfig, ledger: dict[str, int]) -> bool:
    return subdl_search_reserved(ledger) < cfg.subdl_search_daily_cap

def reserve_subdl_search(ledger: dict[str, int]) -> int:
    """Reserve one SubDL API search before it can leave this process."""
    reserved = subdl_search_reserved(ledger) + 1
    ledger["subdl_search_requests_reserved"] = reserved
    return reserved

def reserve_provider_download(ledger: dict[str, int], provider: str) -> int:
    field_name = provider_reservation_field(provider)
    ledger[field_name] = provider_reserved(ledger, provider) + 1
    if provider == PROVIDER_OPENSUBTITLES:
        ledger["download_requests_reserved"] = ledger[field_name]
    return ledger[field_name]

def record_provider_success(ledger: dict[str, int], provider: str) -> None:
    field_name = provider_success_field(provider)
    ledger[field_name] = max(0, int(ledger.get(field_name, 0) or 0)) + 1
    ledger["successful_downloads"] = max(0, int(ledger.get("successful_downloads", 0) or 0)) + 1

def provider_label(provider: str) -> str:
    if provider == PROVIDER_OPENSUBTITLES:
        return "OpenSubtitles"
    if provider == PROVIDER_SUBDL:
        return "SubDL"
    if is_scrape_provider(provider):
        return scrape_provider_label(provider)
    return provider

def provider_quota_text(cfg: QueueConfig, ledger: dict[str, int]) -> str:
    """Format enabled providers' durable local quota reservations."""
    parts: list[str] = []
    for provider in configured_providers(cfg):
        downloads = f"downloads {provider_reserved(ledger, provider)}/{provider_daily_cap(cfg, provider)}"
        if provider == PROVIDER_SUBDL:
            parts.append(
                f"SubDL {downloads}; searches "
                f"{subdl_search_reserved(ledger)}/{cfg.subdl_search_daily_cap}"
            )
        else:
            parts.append(f"{provider_label(provider)} {downloads}")
    for provider in active_scrape_sources(cfg):
        searches = f"searches {provider_reserved(ledger, provider)}/{cfg.scrape_daily_cap}"
        parts.append(f"{provider_label(provider)} {searches}")
    return " · ".join(parts) or "no source configured"

def provider_configuration_text(cfg: QueueConfig) -> str:
    """Describe active providers without exposing any secret configuration."""
    parts: list[str] = []
    if cfg.api_key.strip():
        parts.append(f"OpenSubtitles {cfg.auth_mode}; cap {cfg.daily_cap}")
    if cfg.subdl_api_key.strip():
        subdl_role = "fallback" if cfg.api_key.strip() else "release-aware/title-year"
        parts.append(
            f"SubDL {subdl_role}; downloads {cfg.subdl_daily_cap}; "
            f"searches {cfg.subdl_search_daily_cap}"
        )
    scrape_keys = active_scrape_sources(cfg)
    if scrape_keys:
        parts.append(
            f"{len(scrape_keys)} scraping sources as fallback "
            f"({scrape_provider_label(scrape_keys[0])}, "
            f"{scrape_provider_label(scrape_keys[1])}, ...); {cfg.scrape_daily_cap} searches/day each"
        )
    return " · ".join(parts) or "no source configured"

def provider_policy_text(cfg: QueueConfig) -> str:
    """Explain the actual matching strength available in this run."""
    if not cfg.identity_fallback:
        if cfg.api_key.strip():
            return "OpenSubtitles exact moviehash matching only"
        return "title/year fallback disabled"
    scrape_suffix = ""
    if active_scrape_sources(cfg):
        scrape_suffix = " · 7-site scraping fallback for any remaining movie"
    if cfg.api_key.strip() and cfg.subdl_api_key.strip():
        return f"OpenSubtitles + SubDL as equal sources (release match scored ≥ 0.80) · most downloads wins{scrape_suffix}"
    if cfg.api_key.strip():
        return f"OpenSubtitles only · exact moviehash then conservative title/year{scrape_suffix}"
    if cfg.subdl_api_key.strip():
        return f"SubDL only (release match scored ≥ 0.80) · no exact moviehash provider{scrape_suffix}"
    if active_scrape_sources(cfg):
        return "no API provider configured · scraping sources only"
    return "no source configured"

def movie_key(video: Path, snapshot: VideoSnapshot) -> str:
    token = "|".join((path_norm(video), str(snapshot.device), str(snapshot.inode), str(snapshot.size), str(snapshot.mtime_ns)))
    return hashlib.sha256(token.encode("utf-8", errors="surrogatepass")).hexdigest()

def state_movie(state: dict[str, Any], key: str, video: Path) -> dict[str, Any]:
    record = state["movies"].setdefault(key, {"path": str(video), "status": "pending", "attempts": 0})
    record["path"] = str(video)
    state.setdefault("_dirty_movies", set()).add(key)
    return record

def set_movie_status(record: dict[str, Any], status: str, detail: str = "", **extras: Any) -> None:
    record["status"] = status
    record["detail"] = detail
    record["updated_utc"] = utc_timestamp()
    record.update(extras)

def inspect_existing_sidecars(video: Path) -> tuple[str, Path | None, str, str]:
    """Classify existing English sidecars without trusting filename alone.

    The cleaner's automatic external-subtitle policy requires the exact
    ``Movie.eng.srt`` name. A validated legacy ``Movie.en.srt`` is renamed in
    place to that canonical name. Any other noncanonical or invalid English
    sidecar is kept for manual review rather than triggering a duplicate
    download request.

    Returns ``(status, path, detail, reason)`` where ``reason`` is one of the
    ``REASON_*`` codes (empty for ``missing``, which means "go and fetch one").
    """
    exact = dest_for(video, Config())
    # dest_for uses only the video name and the fixed .eng.srt suffix, so no
    # configured library path leaks into the decision.
    promoted, promote_reason = promote_legacy_external_english_srt(video)
    if promoted is not None and promote_reason == "" and promoted == exact:
        # A successful rename (or an already-canonical sidecar) is re-validated
        # below through the normal candidate walk.
        pass
    elif promote_reason and "absent" not in promote_reason and "unusable" not in promote_reason:
        # Ambiguous dual-name or occupied-destination cases need a human.
        return (
            "review", exact if exact.exists() else None,
            f"legacy .en.srt could not be promoted to .eng.srt ({promote_reason})",
            REASON_SIDECAR_NAME,
        )
    candidates: list[Path] = []
    try:
        candidates = [
            path for path in sorted(video.parent.iterdir(), key=lambda item: item.name.casefold())
            if is_english_srt_sidecar(path, video.stem)
        ]
    except OSError:
        return "missing", None, "could not inspect sibling subtitles", ""
    if not candidates:
        return "missing", None, "no English SRT sidecar", ""
    for path in candidates:
        try:
            file_stat = path.stat(follow_symlinks=False)
            if path.is_symlink() or not path.is_file() or file_stat.st_size <= 0 or file_stat.st_size > MAX_SUBTITLE_BYTES:
                continue
            text = normalize_srt_newlines(decode_subtitle_bytes(path.read_bytes()))
            valid = looks_like_srt(text)
        except (OSError, EOFError, ValueError):
            valid = False
        if path == exact and valid:
            return "covered", path, f"validated exact {EXTERNAL_SRT_SUFFIX}", REASON_COVERED
        if valid:
            return (
                "review", path,
                f"'{path.name}' is a valid English SRT but not the exact {EXTERNAL_SRT_SUFFIX} sidecar; "
                "rename or remove it to let this movie be fetched",
                REASON_SIDECAR_NAME,
            )
    broken = candidates[0]
    return (
        "review", broken,
        f"'{broken.name}' exists but is unusable (empty, truncated, or not an SRT); "
        "delete it and re-run to allow a replacement download",
        REASON_SIDECAR_UNUSABLE,
    )

def relative_text(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)

def make_scrape_transport() -> ScrapeTransport:
    """Factory for the scraping tier's HTTP transport (tests substitute a fake)."""
    return default_transport()

def build_scrape_chain(cfg: QueueConfig, ledger: dict[str, int],
                       state: dict[str, Any]) -> ScrapeChain | None:
    """Build this run's scraping failover chain, or None when the tier is off.

    The durable ledger's per-source search reservations seed the in-memory
    counters, and the callback persists a reservation before each search
    leaves this process, so an interrupted request still counts against the
    source's UTC cap on the next run.
    """
    keys = active_scrape_sources(cfg)
    if not keys:
        return None

    def reserve_search(key: str) -> None:
        reserved = provider_reserved(ledger, key)
        cap = provider_daily_cap(cfg, key)
        if reserved >= cap:
            raise SourceUnavailable(
                f"UTC daily search cap exhausted ({reserved}/{cap})")
        ledger[provider_reservation_field(key)] = reserved + 1
        persist_state(state, cfg.log_file)

    return ScrapeChain(
        keys=keys,
        transport=make_scrape_transport(),
        search_caps={key: cfg.scrape_daily_cap for key in keys},
        reserved={key: provider_reserved(ledger, key) for key in keys},
        reserve_cb=reserve_search,
    )

def queue_run(cfg: QueueConfig) -> tuple[list[JobResult], dict[str, Any]]:
    """Process one daily batch with independent provider quotas.

    OpenSubtitles and SubDL are equal sources. Each movie is offered both
    providers' release-identifying routes - OpenSubtitles exact moviehash and
    SubDL score-gated filename match (score >= 0.80) - and the qualifying
    release with the most downloads wins, regardless of provider. When
    neither release route produces a pick, both providers' strict title/year
    routes are pooled the same way; SubDL's generic title route is used only
    when its release lookup returned no candidates at all, so a low-score
    release match never weakens to a generic one.
    Automatic selection only accepts a release that names the movie and its
    release year and carries a Blu-ray keyword, and ranks those by download
    count.
    """
    state = load_state(cfg.log_file, cfg.library)
    today = utc_day()
    ledger = day_ledger(state, today)
    fetcher_cfg = cfg.fetcher_config()

    def reserve_subdl_search_request() -> None:
        """Persist a search reservation before every SubDL API attempt."""
        if not subdl_search_has_quota(cfg, ledger):
            raise SubdlSearchQuotaExhausted(
                "SubDL daily search cap exhausted "
                f"({subdl_search_reserved(ledger)}/{cfg.subdl_search_daily_cap} requests reserved)"
            )
        reserve_subdl_search(ledger)
        # A network timeout can still count remotely, so never wait until the
        # response to make this local reservation durable.
        persist_state(state, cfg.log_file)

    results: list[JobResult] = []
    open_client = OpenSubtitlesClient(fetcher_cfg) if cfg.api_key.strip() else None
    subdl_client = (
        SubdlClient(cfg.subdl_api_key, before_search_request=reserve_subdl_search_request)
        if cfg.subdl_api_key.strip() else None
    )
    active_providers = configured_providers(cfg)
    scrape_keys = active_scrape_sources(cfg)
    # Dry runs spend no scraping requests: searches would count against the
    # real UTC caps, so the tier is skipped entirely (report says so).
    scrape_chain = build_scrape_chain(cfg, ledger, state) if not cfg.dry_run else None
    deferred_remaining = 0
    deferred_videos: list[Path] = []

    videos = discover_videos(cfg.library, cfg.min_bytes)
    if cfg.limit > 0:
        videos = videos[:cfg.limit]
    total = len(videos)
    log(
        f"Found {total} eligible movies. UTC local reservations: {provider_quota_text(cfg, ledger)}.",
        log_file=cfg.log_file,
    )

    def emit(index: int, status: str, video: Path, detail: str) -> None:
        log(
            f"[{index:03d}/{total:03d}] {status:<8} "
            f"{relative_text(video, cfg.library)} — {detail}",
            log_file=cfg.log_file,
        )

    def has_new_provider(record: dict[str, Any]) -> bool:
        prior = record.get("providers_checked")
        if not isinstance(prior, list):
            # A pre-SubDL ledger cannot say which sources it queried. Preserve
            # its intentional OpenSubtitles review hold unless the newly added
            # provider is actually enabled, then revisit once for that source.
            # The new scraping tier counts as a new source for such records.
            if scrape_keys and not record.get("scrape_checked"):
                return True
            return PROVIDER_SUBDL in active_providers
        previous = {str(provider) for provider in prior}
        if any(provider not in previous for provider in active_providers):
            return True
        # Legacy records predate the scraping tier: offer it to them once so
        # every previously-held movie is re-checked against all nine sources.
        if scrape_keys and not record.get("scrape_checked"):
            return True
        return False

    for index, video in enumerate(videos, start=1):
        layout_issue = canonical_movie_layout_issue(video, cfg.library)
        if layout_issue:
            result = JobResult(video, "skip", layout_issue, reason=REASON_LAYOUT)
            results.append(result)
            emit(index, "SKIP", video, layout_issue)
            continue
        sidecar_status, existing, sidecar_detail, sidecar_reason = inspect_existing_sidecars(video)
        if sidecar_status == "covered" and existing is not None:
            ledger["already_have"] += 1
            result = JobResult(video, "have", sidecar_detail, existing, reason=REASON_COVERED)
            results.append(result)
            emit(index, "HAVE", video, sidecar_detail)
            continue
        if sidecar_status == "review":
            result = JobResult(video, "review", sidecar_detail, existing, reason=sidecar_reason)
            results.append(result)
            emit(index, "REVIEW", video, sidecar_detail)
            continue

        try:
            snapshot = video_snapshot(video)
            key = movie_key(video, snapshot)
        except OSError as exc:
            ledger["errors"] += 1
            result = JobResult(video, "error", str(exc), reason=REASON_ERROR)
            results.append(result)
            emit(index, "ERROR", video, str(exc))
            continue
        record = state_movie(state, key, video)
        old_status = str(record.get("status") or "pending")
        # Scraping retry economy: a movie the scraping tier already exhausted
        # today is not offered to it twice, and a movie that exhausted it on
        # an earlier day goes straight back to the scraping tier (the API
        # tiers already miss for it, so re-spending their quota is wasted).
        scrape_failed_day = str(record.get("scrape_failed_utc_day") or "")
        scrape_tried_today = bool(record.get("scrape_failed")) and scrape_failed_day == today
        scrape_retry_today = (
            bool(record.get("scrape_failed"))
            and cfg.identity_fallback
            and bool(scrape_keys)
            and scrape_failed_day != today
        )
        if scrape_tried_today and old_status in ("manual_review", "no_match"):
            result = JobResult(
                video, "skip",
                "scraping sources were already exhausted for this movie today; retrying on the next UTC day",
                reason=REASON_QUOTA)
            results.append(result)
            emit(index, "SKIP", video, result.detail)
            continue
        if old_status == "no_match" and not (cfg.retry_no_match or cfg.identity_fallback):
            result = JobResult(video, "skip", "previous strict moviehash search had no match",
                               reason=REASON_NO_MATCH)
            results.append(result)
            emit(index, "SKIP", video, result.detail)
            continue
        if (old_status == "manual_review" and not cfg.retry_no_match
                and not scrape_retry_today and not has_new_provider(record)):
            result = JobResult(video, "review", "previous identity fallback was intentionally held for review",
                               reason=REASON_REVIEW)
            results.append(result)
            emit(index, "REVIEW", video, result.detail)
            continue
        if old_status == "reserved" and str(record.get("updated_utc") or "").startswith(today):
            result = JobResult(video, "skip", "a provider download was already reserved today; waiting for next UTC day",
                               reason=REASON_QUOTA)
            results.append(result)
            emit(index, "SKIP", video, result.detail)
            continue

        open_available = (
            open_client is not None
            and provider_has_quota(cfg, ledger, PROVIDER_OPENSUBTITLES)
        )
        # SubDL has no byte-exact release hash, so --no-identity-fallback also
        # intentionally disables its release-aware/title-year lookup.
        subdl_available = (
            subdl_client is not None
            and cfg.identity_fallback
            and provider_has_quota(cfg, ledger, PROVIDER_SUBDL)
            and subdl_search_has_quota(cfg, ledger)
        )
        # On a scraping retry the API tiers are already known to miss for
        # this movie, so they are not asked again; the scraping tier is.
        api_tiers_allowed = not (scrape_retry_today and not scrape_tried_today)
        open_tier_available = open_available and api_tiers_allowed
        subdl_tier_available = subdl_available and api_tiers_allowed
        scrape_available = (
            scrape_chain is not None
            and cfg.identity_fallback
            and any(provider_has_quota(cfg, ledger, key) for key in scrape_keys)
        )
        if not open_available and not subdl_available and not scrape_available:
            deferred_remaining = total - index + 1
            deferred_videos = list(videos[index - 1:])
            log(
                "QUOTA REACHED: no configured source with an enabled matching mode has "
                f"remaining local capacity ({provider_quota_text(cfg, ledger)}). "
                f"{deferred_remaining} movie(s) remain for the next UTC day.",
                level="WARNING", log_file=cfg.log_file,
            )
            break

        digest = ""
        pick: Candidate | None = None
        selected_provider = ""
        selection_method = ""
        selection_reason = "no usable Blu-ray English moviehash-matched human SRT naming the movie and its release year"
        providers_checked: list[str] = []
        subdl_downloads: dict[str, SubdlDownload] = {}
        # Tier 3 result: the validated bytes the chain already downloaded for
        # the winning scrape candidate (None until the chain produces one).
        scrape_download: bytes | None = None
        # Distinguish an exhausted SubDL cap before a lookup from a filename
        # lookup that actually returned a low-score or ambiguous candidate.
        # The former should be retried on the next quota day; the latter is a
        # deliberate manual-review decision.
        subdl_lookup_attempted = False
        # The selection policy matches the release name against the movie
        # title, so derive the canonical identity once and reuse it in both
        # the hash branch and the title/year fallback below.
        identity = movie_identity_from_video(video)

        open_lookup_error = ""
        pool_reasons: list[str] = []
        os_tier1: Candidate | None = None
        os_tier1_reason = (
            "no usable Blu-ray English moviehash-matched human SRT "
            "naming the movie and its release year"
        )
        subdl_tier1: Candidate | None = None
        subdl_tier1_reason = ""
        subdl_release_candidates: list[Candidate] = []

        # Tier 1 - release-identifying routes, queried as equal sources:
        # OpenSubtitles' exact-moviehash match and SubDL's score-gated
        # filename match. Whichever qualifying release has the most downloads
        # wins, regardless of provider.
        if open_tier_available and open_client is not None:
            providers_checked.append(PROVIDER_OPENSUBTITLES)
            emit(index, "SEARCH", video, "calculating moviehash and checking OpenSubtitles")
            try:
                digest = moviehash(video)
                if not video_snapshot_matches(video, snapshot):
                    raise RuntimeError("movie changed while calculating moviehash")
            except (RuntimeError, ValueError) as exc:
                # ValueError matters as much as RuntimeError here: moviehash()
                # raises it for a file below MIN_HASH_SIZE, and the size gate ran
                # at scan time, so a file truncated in between must not abort the
                # rest of the daily queue. A local movie problem cannot safely
                # fall through to title/year matching on another provider.
                set_movie_status(
                    record, "error", str(exc), attempts=int(record.get("attempts", 0) or 0) + 1,
                    providers_checked=providers_checked,
                )
                ledger["errors"] += 1
                persist_state(state, cfg.log_file)
                result = JobResult(video, "error", str(exc), reason=REASON_ERROR)
                results.append(result)
                emit(index, "ERROR", video, str(exc))
                continue
            try:
                candidates = open_client.search(movie_hash=digest, query=video.stem)
                os_tier1 = pick_candidate(candidates, fetcher_cfg, identity=identity)
            except (RuntimeError, ValueError) as exc:
                if not subdl_tier_available:
                    set_movie_status(
                        record, "error", str(exc), attempts=int(record.get("attempts", 0) or 0) + 1,
                        providers_checked=providers_checked,
                    )
                    ledger["errors"] += 1
                    persist_state(state, cfg.log_file)
                    result = JobResult(video, "error", str(exc), reason=REASON_ERROR)
                    results.append(result)
                    emit(index, "ERROR", video, str(exc))
                    continue
                open_lookup_error = f"OpenSubtitles moviehash lookup failed: {exc}"
                pool_reasons.append(open_lookup_error)
                emit(index, "FALLBACK", video, f"{open_lookup_error}; continuing to SubDL")
            if os_tier1 is not None:
                os_tier1_reason = (
                    "moviehash match; Blu-ray release naming the movie and its release year; "
                    "highest download count"
                )

        if os_tier1 is not None and (not cfg.identity_fallback or identity is None):
            # A strict hash match stands alone when nothing else may be
            # asked: the title/year fallback is disabled, or the filename
            # carries no canonical Title (Year) pair to search by.
            pick, selected_provider, selection_method, selection_reason = (
                os_tier1, PROVIDER_OPENSUBTITLES, "hash", os_tier1_reason,
            )

        if pick is None:
            if not cfg.identity_fallback:
                detail = (
                    "no usable Blu-ray English moviehash-matched human SRT naming the movie and its release year"
                    if open_available else
                    "no exact-moviehash provider is available and title/year fallback is disabled"
                )
                set_movie_status(
                    record, "no_match", detail, moviehash=digest,
                    attempts=int(record.get("attempts", 0) or 0) + 1,
                    providers_checked=providers_checked,
                )
                ledger["no_match"] += 1
                persist_state(state, cfg.log_file)
                result = JobResult(video, "skip", detail, reason=REASON_NO_MATCH)
                results.append(result)
                emit(index, "NO MATCH", video, detail)
                continue

            if identity is None:
                detail = (
                    "no strict hash match and filename is not canonical Title (Year)"
                    if open_available else
                    "SubDL title/year fallback requires a canonical Title (Year) filename"
                )
                set_movie_status(
                    record, "manual_review", detail, moviehash=digest,
                    attempts=int(record.get("attempts", 0) or 0) + 1,
                    providers_checked=providers_checked,
                )
                ledger["identity_review"] += 1
                persist_state(state, cfg.log_file)
                result = JobResult(video, "review", detail, reason=REASON_REVIEW)
                results.append(result)
                emit(index, "REVIEW", video, detail)
                continue
            assert identity is not None

            if subdl_tier_available and subdl_client is not None:
                providers_checked.append(PROVIDER_SUBDL)
                emit(
                    index,
                    "SEARCH",
                    video,
                    f"checking SubDL release-aware filename match: {video.name}",
                )
                try:
                    subdl_lookup_attempted = True
                    subdl_release_candidates, subdl_downloads = subdl_client.search_filename(
                        video.name, identity,
                    )
                    subdl_tier1, subdl_tier1_reason = pick_subdl_identity_candidate(
                        subdl_release_candidates, identity, require_release_match_score=True,
                    )
                except SubdlSearchQuotaExhausted as exc:
                    # The callback fires before an outbound request. This movie
                    # was not fully evaluated, so defer it rather than turning a
                    # temporary provider limit into a manual-review decision.
                    detail = str(exc)
                    result = JobResult(video, "skip", detail, reason=REASON_QUOTA)
                    results.append(result)
                    emit(index, "SKIP", video, detail)
                    continue
                except (RuntimeError, ValueError) as exc:
                    detail = f"SubDL lookup failed: {exc}"
                    set_movie_status(
                        record, "error", detail, moviehash=digest,
                        attempts=int(record.get("attempts", 0) or 0) + 1,
                        providers_checked=providers_checked,
                    )
                    ledger["errors"] += 1
                    persist_state(state, cfg.log_file)
                    result = JobResult(video, "error", detail, reason=REASON_ERROR)
                    results.append(result)
                    emit(index, "ERROR", video, detail)
                    continue
            elif subdl_client is not None:
                if not api_tiers_allowed:
                    pool_reasons.append("SubDL: not re-queried on a scraping retry (known API miss)")
                elif not provider_has_quota(cfg, ledger, PROVIDER_SUBDL):
                    pool_reasons.append("SubDL: daily download cap exhausted")
                elif not subdl_search_has_quota(cfg, ledger):
                    pool_reasons.append("SubDL: daily search cap exhausted")
                else:
                    pool_reasons.append("SubDL: identity fallback disabled")

            tier1_entries: list[tuple[Candidate, str, str, str]] = []
            if os_tier1 is not None:
                tier1_entries.append((os_tier1, PROVIDER_OPENSUBTITLES, "hash", os_tier1_reason))
            if subdl_tier1 is not None:
                tier1_entries.append((subdl_tier1, PROVIDER_SUBDL, "subdl-release", subdl_tier1_reason))
            elif subdl_client is not None and subdl_lookup_attempted:
                if not subdl_release_candidates:
                    # The title route below will explain this provider's miss.
                    pass
                else:
                    # A low-score or ambiguous release match deliberately does
                    # not weaken to SubDL's generic title route, so this is the
                    # final SubDL verdict for the review detail.
                    pool_reasons.append(f"SubDL: {subdl_tier1_reason}")

            pick, selected_provider, selection_method, selection_reason = pick_pooled_candidates(
                tier1_entries, identity,
            )
            if pick is None and selection_reason:
                pool_reasons.append(selection_reason)

            if pick is None:
                # Tier 2 - strict title/year routes, also queried as equal
                # sources: OpenSubtitles title/year and SubDL's documented
                # title search.
                os_tier2: Candidate | None = None
                os_tier2_reason = ""
                subdl_tier2: Candidate | None = None
                subdl_tier2_reason = ""
                if open_tier_available and open_client is not None and not open_lookup_error:
                    emit(
                        index, "FALLBACK", video,
                        f"checking OpenSubtitles title/year: {identity.title} ({identity.year})",
                    )
                    try:
                        identity_candidates = open_client.search_identity(identity)
                        os_tier2, os_tier2_reason = pick_identity_candidate(identity_candidates, identity)
                    except (RuntimeError, ValueError) as exc:
                        if not subdl_tier_available:
                            set_movie_status(
                                record, "error", str(exc),
                                attempts=int(record.get("attempts", 0) or 0) + 1,
                                providers_checked=providers_checked,
                            )
                            ledger["errors"] += 1
                            persist_state(state, cfg.log_file)
                            result = JobResult(video, "error", str(exc), reason=REASON_ERROR)
                            results.append(result)
                            emit(index, "ERROR", video, str(exc))
                            continue
                        open_lookup_error = f"OpenSubtitles title/year lookup failed: {exc}"
                        pool_reasons.append(open_lookup_error)
                        emit(index, "FALLBACK", video, f"{open_lookup_error}; continuing to SubDL")
                elif open_client is not None and not open_lookup_error:
                    if not api_tiers_allowed:
                        pool_reasons.append("OpenSubtitles: not re-queried on a scraping retry (known API miss)")
                    else:
                        pool_reasons.append("OpenSubtitles: daily download cap exhausted")

                # The local canonical filename deliberately omits scene tags.
                # If SubDL's release lookup resolved nothing at all, use its
                # documented title route once, still requiring exact provider
                # title/year metadata. A low-score release match never weakens
                # to the generic route.
                subdl_title_allowed = subdl_lookup_attempted and not subdl_release_candidates
                if (
                    subdl_tier_available and subdl_client is not None
                    and subdl_title_allowed
                ):
                    emit(
                        index, "FALLBACK", video,
                        f"checking SubDL strict title/year: {identity.title} ({identity.year})",
                    )
                    try:
                        subdl_title_candidates, subdl_downloads = subdl_client.search_identity(identity)
                        subdl_tier2, subdl_tier2_reason = pick_subdl_identity_candidate(
                            subdl_title_candidates, identity,
                        )
                    except SubdlSearchQuotaExhausted as exc:
                        # The callback fires before an outbound request. This
                        # movie was not fully evaluated, so defer it rather
                        # than turning a temporary provider limit into a
                        # manual-review decision.
                        detail = str(exc)
                        result = JobResult(video, "skip", detail, reason=REASON_QUOTA)
                        results.append(result)
                        emit(index, "SKIP", video, detail)
                        continue
                    except (RuntimeError, ValueError) as exc:
                        detail = f"SubDL lookup failed: {exc}"
                        set_movie_status(
                            record, "error", detail, moviehash=digest,
                            attempts=int(record.get("attempts", 0) or 0) + 1,
                            providers_checked=providers_checked,
                        )
                        ledger["errors"] += 1
                        persist_state(state, cfg.log_file)
                        result = JobResult(video, "error", detail, reason=REASON_ERROR)
                        results.append(result)
                        emit(index, "ERROR", video, detail)
                        continue

                tier2_entries: list[tuple[Candidate, str, str, str]] = []
                if os_tier2 is not None:
                    tier2_entries.append((os_tier2, PROVIDER_OPENSUBTITLES, "identity", os_tier2_reason))
                elif open_client is not None and not open_lookup_error:
                    pool_reasons.append(
                        "OpenSubtitles: daily download cap exhausted"
                        if not open_available else
                        f"OpenSubtitles: {os_tier2_reason}"
                    )
                if subdl_tier2 is not None:
                    tier2_entries.append((subdl_tier2, PROVIDER_SUBDL, "subdl-identity", subdl_tier2_reason))
                elif subdl_client is not None and subdl_title_allowed:
                    pool_reasons.append(f"SubDL: {subdl_tier2_reason}")

                pick, selected_provider, selection_method, selection_reason = pick_pooled_candidates(
                    tier2_entries, identity,
                )
                if pick is None and selection_reason:
                    pool_reasons.append(selection_reason)

            if (pick is None and subdl_client is not None and not subdl_lookup_attempted
                    and not subdl_available and api_tiers_allowed):
                if not provider_has_quota(cfg, ledger, PROVIDER_SUBDL):
                    detail = "SubDL daily download cap exhausted before lookup; deferred to the next UTC day"
                else:
                    detail = "SubDL daily search cap exhausted before lookup; deferred to the next UTC day"
                result = JobResult(video, "skip", detail, reason=REASON_QUOTA)
                results.append(result)
                emit(index, "SKIP", video, detail)
                continue

            if pick is None and scrape_chain is not None:
                # Tier 3 - the scraping fallback sources (no API keys needed):
                # Subf2me, Podnapisi, Addic7ed, SubSource, Subsunacs, YIFY
                # Subtitles, Subs.Sab.BZ. Each source is searched once per
                # movie in failover order; a candidate wins only when it
                # names the movie, matches its release year, and decodes to
                # a valid SRT. The chain's breaker disables a source for the
                # rest of the run after repeated hard or parse failures.
                emit(
                    index, "SEARCH", video,
                    "checking scraping sources: "
                    + " · ".join(scrape_provider_label(key) for key in scrape_keys),
                )
                try:
                    scrape_cand, scrape_key, scrape_raw = run_scrape_chain(
                        SourceIdentity(
                            identity.title, identity.year, identity.normalized_title),
                        keys=tuple(scrape_keys),
                        chain=scrape_chain,
                        on_reason=lambda key, why: pool_reasons.append(
                            f"{scrape_provider_label(key)}: {why}"),
                    )
                except Exception as exc:  # a scraping-tier bug must not kill the run
                    pool_reasons.append(f"scraping sources failed: {type(exc).__name__}: {exc}")
                    scrape_cand, scrape_key, scrape_raw = None, "", None
                if scrape_cand is not None and scrape_raw is not None:
                    pick = Candidate(
                        file_id=f"scrape:{scrape_key}:{scrape_cand.file_id}",
                        release=scrape_cand.release or "",
                        moviehash_match=False,
                        downloads=int(scrape_cand.downloads or 0),
                        votes=0,
                        rating=float(scrape_cand.rating or 0.0),
                        trusted=False,
                        hearing_impaired=bool(scrape_cand.hearing_impaired),
                        machine_translated=False,
                        ai_translated=False,
                        foreign_parts_only=False,
                        language="en",
                        feature_title=scrape_cand.feature_title or identity.title,
                        feature_year=scrape_cand.feature_year or identity.year,
                    )
                    selected_provider = scrape_key
                    selection_method = "scrape"
                    selection_reason = (
                        f"scraping source {scrape_provider_label(scrape_key)} "
                        f"(candidate validated as an English SRT naming the movie)"
                    )
                    scrape_download = scrape_raw

            if pick is None:
                reason = "; ".join(pool_reasons) or selection_reason
                detail = f"identity fallback held for review: {reason}"
                extras: dict[str, Any] = {}
                if scrape_chain is not None:
                    # Every scraping source was offered to this movie and
                    # produced nothing usable today. It is retried on the
                    # next UTC day (see the retry gates at the top of the
                    # loop), and the scrape keys are deliberately not written
                    # into providers_checked so has_new_provider does not
                    # mistake a finished scraping attempt for a new provider.
                    extras = {
                        "scrape_checked": True,
                        "scrape_failed": True,
                        "scrape_failed_utc_day": today,
                    }
                set_movie_status(
                    record, "manual_review", detail, moviehash=digest,
                    attempts=int(record.get("attempts", 0) or 0) + 1,
                    providers_checked=providers_checked,
                    **extras,
                )
                ledger["identity_review"] += 1
                # The scraping chain's reservation callbacks already persisted
                # (and cleared) the dirty set, so re-mark this record: its
                # scrape_failed flags must survive for the next-UTC-day gate.
                state.setdefault("_dirty_movies", set()).add(key)
                persist_state(state, cfg.log_file)
                result = JobResult(video, "review", detail, reason=REASON_REVIEW)
                results.append(result)
                emit(index, "REVIEW", video, detail)
                continue

        dest = dest_for(video, fetcher_cfg)
        note = (
            f"provider={provider_label(selected_provider)}; method={selection_method}; id={pick.file_id}; "
            f"trusted={'yes' if pick.trusted else 'no'}; rating={pick.rating:g}/{pick.votes}; "
            f"{selection_reason}; {pick.release or 'unnamed release'}"
        )
        if cfg.dry_run:
            result = JobResult(video, "dry-run", note, dest, reason=REASON_DRY_RUN)
            results.append(result)
            emit(index, "WOULD GET", video, note)
            continue

        if is_scrape_provider(selected_provider):
            # The scraping chain already reserved and persisted the search
            # before fetching, and the bytes are on hand: no second
            # reservation, no "reserved" state (an interrupted write should
            # be retried immediately, not parked until the next UTC day).
            print(
                f"[{index:03d}/{total:03d}] SAVING {relative_text(video, cfg.library)} — "
                f"{provider_label(selected_provider)} (validated scraping candidate)",
                flush=True,
            )
        else:
            # Persist a provider-specific reservation before the download: an
            # interrupted request may still count against that provider's quota.
            reservation = reserve_provider_download(ledger, selected_provider)
            set_movie_status(
                record, "reserved", note, moviehash=digest, selection_method=selection_method,
                selected_provider=selected_provider, selected_file_id=str(pick.file_id),
                attempts=int(record.get("attempts", 0) or 0) + 1,
                providers_checked=providers_checked,
            )
            persist_state(state, cfg.log_file)
            print(
                f"[{index:03d}/{total:03d}] DOWNLOAD {relative_text(video, cfg.library)} — "
                f"{provider_label(selected_provider)} request "
                f"{reservation}/{provider_daily_cap(cfg, selected_provider)}",
                flush=True,
            )
        try:
            if selected_provider == PROVIDER_SUBDL:
                if subdl_client is None:
                    raise RuntimeError("SubDL client is unavailable")
                download = subdl_downloads.get(str(pick.file_id))
                if download is None:
                    raise RuntimeError("SubDL candidate download reference is missing")
                subdl_client.download_srt(download, dest, video=video, expected_video=snapshot)
            elif is_scrape_provider(selected_provider):
                # The chain already downloaded and validated these bytes
                # (valid_srt_bytes); the shared sidecar contract is applied
                # here exactly as for the API providers.
                if scrape_download is None:
                    raise RuntimeError("scraping candidate download reference is missing")
                if len(scrape_download) > MAX_SUBTITLE_BYTES:
                    raise RuntimeError(f"subtitle exceeds {MAX_SUBTITLE_BYTES} byte safety limit")
                text = decode_subtitle_bytes(scrape_download)
                text = normalize_srt_newlines(text)
                if not looks_like_srt(text):
                    raise RuntimeError("scraping payload is not a valid SRT subtitle")
                if not video_snapshot_matches(video, snapshot):
                    raise RuntimeError("movie changed during subtitle lookup; scraped SRT was not activated")
                try:
                    atomic_write_text(dest, text, replace=False)
                except FileExistsError as exc:
                    raise ConcurrentSidecarError(
                        "English SRT appeared during download; preserved the existing sidecar") from exc
            else:
                if open_client is None or not isinstance(pick.file_id, int):
                    raise RuntimeError("OpenSubtitles candidate has an invalid file identifier")
                open_client.download_srt(pick.file_id, dest, video=video, expected_video=snapshot)
        except ConcurrentSidecarError as exc:
            set_movie_status(record, "have", str(exc), sidecar=str(dest))
            ledger["already_have"] += 1
            result = JobResult(video, "have", str(exc), dest, reason=REASON_COVERED)
            results.append(result)
            emit(index, "HAVE", video, str(exc))
        except (RuntimeError, ValueError) as exc:
            # decode_subtitle_bytes() raises ValueError for a subtitle that
            # decompresses past MAX_SUBTITLE_BYTES, so a single hostile or
            # corrupt provider payload must not abort the rest of the library.
            set_movie_status(record, "error", str(exc))
            ledger["errors"] += 1
            result = JobResult(video, "error", str(exc), reason=REASON_ERROR)
            results.append(result)
            emit(index, "ERROR", video, str(exc))
        else:
            set_movie_status(record, "downloaded", note, sidecar=str(dest))
            record_provider_success(ledger, selected_provider)
            result = JobResult(video, "download", note, dest, reason=REASON_DOWNLOADED)
            results.append(result)
            emit(index, "SAVED", video, dest.name)
        persist_state(state, cfg.log_file)

    available_after_run = [
        provider for provider in active_providers
        if provider_has_quota(cfg, ledger, provider)
        and (provider != PROVIDER_SUBDL or cfg.identity_fallback)
        and (provider != PROVIDER_SUBDL or subdl_search_has_quota(cfg, ledger))
    ]
    available_after_run += [
        key for key in scrape_keys
        if provider_has_quota(cfg, ledger, key)
    ]
    covered_count = sum(
        1 for result in results
        if result.reason in (REASON_COVERED, REASON_DOWNLOADED)
        or (cfg.dry_run and result.reason == REASON_DRY_RUN)
    )
    summary = {
        "utc_day": today,
        # Legacy summary fields remain OpenSubtitles values for downstream
        # consumers that predate the second provider.
        "daily_cap": cfg.daily_cap,
        "download_requests_reserved": provider_reserved(ledger, PROVIDER_OPENSUBTITLES),
        "successful_downloads": ledger["successful_downloads"],
        "opensubtitles_daily_cap": cfg.daily_cap,
        "opensubtitles_download_requests_reserved": provider_reserved(ledger, PROVIDER_OPENSUBTITLES),
        "opensubtitles_successful_downloads": ledger["opensubtitles_successful_downloads"],
        "subdl_search_daily_cap": cfg.subdl_search_daily_cap,
        "subdl_search_requests_reserved": subdl_search_reserved(ledger),
        "subdl_daily_cap": cfg.subdl_daily_cap,
        "subdl_download_requests_reserved": provider_reserved(ledger, PROVIDER_SUBDL),
        "subdl_successful_downloads": ledger["subdl_successful_downloads"],
        "scrape_search_daily_cap": cfg.scrape_daily_cap,
        "scrape_sources_enabled": list(scrape_keys),
        "scrape_sources_status": scrape_chain.status() if scrape_chain is not None else {},
        "scrape_search_requests_reserved": {
            key: provider_reserved(ledger, key) for key in scrape_keys
        },
        "scrape_successful_downloads": {
            key: int(ledger.get(provider_success_field(key), 0) or 0) for key in scrape_keys
        },
        "quota_reached": not available_after_run,
        "deferred_remaining": deferred_remaining,
        "ledger_log": str(cfg.log_file),
        "movies_discovered": total,
        # Coverage is the product promise: every movie ends the run with a
        # validated English SRT (dry runs count their candidates as would-be
        # covered). Anything else - review holds, misses, errors, deferred -
        # is uncovered and names its movies in the report.
        "coverage_covered": covered_count,
        "coverage_total": total,
        # Which movies, not just how many: the report has to be able to name
        # what was never reached when all usable provider caps cut the batch short.
        "deferred_videos": deferred_videos,
    }
    return results, summary

@dataclass(frozen=True)
class NeedsBucket:
    """One reason a movie still has no usable external English SRT.

    ``order`` is implicit in the tuple order of :data:`NEEDS_SUBTITLE_BUCKETS`:
    the cheapest, most certain fix comes first, so the top of the report is
    always the thing to do next.
    """

    reason: str
    title: str
    quick: str
    fix: str

NEEDS_SUBTITLE_BUCKETS: tuple[NeedsBucket, ...] = (
    NeedsBucket(
        REASON_SIDECAR_UNUSABLE,
        "SIDECAR EXISTS BUT IS UNUSABLE",
        "delete the file, then re-run",
        "Delete the named file, then re-run this tool. Nothing replaces a sidecar it "
        "believes is already present, so a corrupt file blocks a good download forever.",
    ),
    NeedsBucket(
        REASON_SIDECAR_NAME,
        "SIDECAR NAME IS NOT CANONICAL",
        f"rename it to <movie>{EXTERNAL_SRT_SUFFIX}, or delete it",
        f"Rename the file to \"<movie>{EXTERNAL_SRT_SUFFIX}\" (or delete it) and re-run. "
        "Jellyfin and Plex only direct play that exact name, and this tool will not "
        "download a second copy over a subtitle that is already there.",
    ),
    NeedsBucket(
        REASON_LAYOUT,
        "LIBRARY LAYOUT MUST BE FIXED FIRST",
        "run movie_standardizer.py on that folder",
        "Each movie must be one MKV in a folder of the same name: "
        "\"Title (Year)/Title (Year).mkv\". Run movie_standardizer.py, or fix the "
        "folder by hand, and this movie will be picked up on the next run.",
    ),
    NeedsBucket(
        REASON_REVIEW,
        "HELD FOR MANUAL REVIEW",
        "inspect the candidate yourself, or wait for the next UTC day",
        "Every source (both API providers and the seven scraping fallbacks) was checked "
        "and nothing usable was found for this movie, so the download was deliberately "
        "not made. Catalogues grow and sources come back, so the scraping tier is offered "
        "to this movie again on every later UTC day automatically; you can also place the "
        "subtitle yourself or re-run with --retry-review.",
    ),
    NeedsBucket(
        REASON_NO_MATCH,
        "NO MATCHING SUBTITLE ON ANY SOURCE",
        "re-run on a later day, or add the SRT by hand",
        "No source - OpenSubtitles, SubDL, or any of the scraping fallbacks - returned a "
        "safe English SRT that names the movie and its year. Catalogues grow over time, so "
        "a later run can succeed; otherwise add the subtitle yourself.",
    ),
    NeedsBucket(
        REASON_QUOTA,
        "DEFERRED TO THE NEXT UTC DAY",
        "nothing to fix - re-run after the UTC day rolls over",
        "Every source's usable daily allowance was exhausted (API download caps and/or "
        "scraping search caps), so these movies were not searched. Re-run after the UTC day "
        "rolls over; no request is wasted.",
    ),
    NeedsBucket(
        REASON_ERROR,
        "ERRORS",
        "read the log entry for each one",
        "Something failed while reading the movie or talking to the provider. The log "
        "carries the exact error; fix the cause and re-run.",
    ),
)

DEFERRED_NOT_SCANNED = "never scanned: the UTC request cap was reached before this movie"

def movie_label(video: Path, library: Path) -> str:
    """The movie's folder, relative to the library.

    The layout contract is ``Title (Year)/Title (Year).mkv``, so the folder
    already names the movie; repeating the ``.mkv`` beside it only made every
    line longer without saying anything new.
    """
    if video.parent != library:
        return relative_text(video.parent, library)
    return relative_text(video, library)

def group_results(
    results: Sequence[JobResult], summary: dict[str, Any]
) -> tuple[dict[str, list[tuple[Path, str]]], list[JobResult], list[JobResult], list[JobResult]]:
    """Split one run into (needs buckets, covered, downloaded, dry-run).

    Movies the quota cut off before they were scanned join the quota bucket so
    the report names them instead of only reporting a count.
    """
    buckets: dict[str, list[tuple[Path, str]]] = {bucket.reason: [] for bucket in NEEDS_SUBTITLE_BUCKETS}
    covered: list[JobResult] = []
    downloaded: list[JobResult] = []
    dry_run: list[JobResult] = []
    for result in results:
        if result.reason == REASON_COVERED:
            covered.append(result)
        elif result.reason == REASON_DOWNLOADED:
            downloaded.append(result)
        elif result.reason == REASON_DRY_RUN:
            dry_run.append(result)
        elif result.reason in buckets:
            buckets[result.reason].append((result.video, result.detail))
        else:  # a reason nobody knows about must still be visible, not dropped
            buckets.setdefault(REASON_ERROR, []).append((result.video, result.detail or result.status))
    for video in summary.get("deferred_videos") or ():
        buckets[REASON_QUOTA].append((Path(video), DEFERRED_NOT_SCANNED))
    for items in buckets.values():
        items.sort(key=lambda item: str(item[0]).casefold())
    covered.sort(key=lambda item: str(item.video).casefold())
    downloaded.sort(key=lambda item: str(item.video).casefold())
    dry_run.sort(key=lambda item: str(item.video).casefold())
    return buckets, covered, downloaded, dry_run

def report_provider_quota_text(cfg: QueueConfig, summary: dict[str, Any]) -> str:
    """Format provider reservations for the report, including old summaries."""
    parts: list[str] = []
    # Unit callers and old log-derived summaries have only the legacy
    # OpenSubtitles fields, so retain that display when no SubDL key is set.
    if cfg.api_key.strip() or not cfg.subdl_api_key.strip():
        reserved = int(summary.get("opensubtitles_download_requests_reserved",
                                   summary.get("download_requests_reserved", 0)) or 0)
        cap = int(summary.get("opensubtitles_daily_cap", summary.get("daily_cap", cfg.daily_cap)) or 0)
        parts.append(f"OpenSubtitles {reserved}/{cap} reserved · {max(0, cap - reserved)} left")
    if cfg.subdl_api_key.strip():
        downloads_reserved = int(summary.get("subdl_download_requests_reserved", 0) or 0)
        downloads_cap = int(summary.get("subdl_daily_cap", cfg.subdl_daily_cap) or 0)
        searches_reserved = int(summary.get("subdl_search_requests_reserved", 0) or 0)
        searches_cap = int(summary.get("subdl_search_daily_cap", cfg.subdl_search_daily_cap) or 0)
        parts.append(
            f"SubDL downloads {downloads_reserved}/{downloads_cap} reserved · "
            f"{max(0, downloads_cap - downloads_reserved)} left; searches "
            f"{searches_reserved}/{searches_cap} reserved · {max(0, searches_cap - searches_reserved)} left"
        )
    if summary.get("scrape_sources_enabled") or cfg.scrape_daily_cap > 0:
        reserved_by_source = summary.get("scrape_search_requests_reserved") or {}
        cap = int(summary.get("scrape_search_daily_cap", cfg.scrape_daily_cap) or 0)
        enabled = summary.get("scrape_sources_enabled")
        if enabled:
            total_reserved = sum(int(v) for v in reserved_by_source.values())
            parts.append(
                f"scraping ({len(enabled)} sources) {total_reserved} searches reserved today "
                f"({cap}/source cap)"
            )
        else:
            parts.append("scraping sources not configured for this run")
    return "  ·  ".join(parts) or "No source configured"

def report_download_text(cfg: QueueConfig, summary: dict[str, Any]) -> str:
    """Show a useful provider breakdown without breaking old report callers."""
    total = int(summary.get("successful_downloads", 0) or 0)
    parts: list[str] = []
    if cfg.api_key.strip():
        parts.append(f"OpenSubtitles {int(summary.get('opensubtitles_successful_downloads', 0) or 0)}")
    if cfg.subdl_api_key.strip():
        parts.append(f"SubDL {int(summary.get('subdl_successful_downloads', 0) or 0)}")
    scrape_success = summary.get("scrape_successful_downloads") or {}
    scrape_total = sum(int(v) for v in scrape_success.values())
    if scrape_total:
        detail = " · ".join(
            f"{scrape_provider_label(key)} {int(count)}"
            for key, count in scrape_success.items() if int(count or 0)
        )
        parts.append(f"scraping {scrape_total} ({detail})")
    return f"{total} successful this run" + (f" ({' · '.join(parts)})" if parts else "")

def build_report(results: Sequence[JobResult], cfg: QueueConfig, summary: dict[str, Any]) -> str:
    """Render the whole run as one report a human can act on in ten seconds.

    The two questions this report exists to answer come first and in full:
    which movies still need a subtitle, and which already have their external
    ``.eng.srt``.
    """
    buckets, covered, downloaded, dry_run = group_results(results, summary)
    needs = sum(len(items) for items in buckets.values())
    total = int(summary.get("movies_discovered") or len(results))
    covered_count = int(summary.get("coverage_covered", len(covered) + len(downloaded)
                              + (len(dry_run) if cfg.dry_run else 0)) or 0)
    coverage_pct = (100.0 * covered_count / total) if total else 100.0

    policy = provider_policy_text(cfg)
    sources_meta: list[str] = []
    if cfg.api_key.strip():
        sources_meta.append("OpenSubtitles")
    if cfg.subdl_api_key.strip():
        sources_meta.append("SubDL")
    scrape_enabled = summary.get("scrape_sources_enabled")
    if scrape_enabled:
        labels = [scrape_provider_label(key) for key in scrape_enabled]
        sources_meta.append("scraping fallback: " + " · ".join(labels))
    sources_meta_line = " + ".join(sources_meta) if sources_meta else "no source configured"
    scrape_status = summary.get("scrape_sources_status") or {}
    if scrape_status:
        sources_meta_line += "  ·  " + "; ".join(
            f"{scrape_provider_label(key)}: {text}" for key, text in scrape_status.items()
        )
    elif scrape_enabled and cfg.dry_run:
        sources_meta_line += "  ·  scraping sources skipped in dry-run (no requests are spent)"
    report = Report(
        "JELLYFIN DAILY SUBTITLE QUEUE REPORT",
        f"One validated external English {EXTERNAL_SRT_SUFFIX} beside every movie \u00b7 {policy}",
    )
    report.metas([
        ("Generated", f"{utc_timestamp()} (UTC)"),
        ("Library", cfg.library),
        ("Sources", sources_meta_line),
        ("Quota", f"{summary['utc_day']}  \u00b7  {report_provider_quota_text(cfg, summary)}"),
        ("Downloads", report_download_text(cfg, summary)),
        ("Policy", f"English human-authored UTF-8 SRT only  \u00b7  {policy}  \u00b7  {SELECTION_POLICY_TEXT}"),
        ("Ledger", cfg.log_file or "(none)"),
    ])

    rows: list[tuple[object, str, str]] = [
        (f"{covered_count}/{total} ({coverage_pct:.1f}%)",
         "COVERAGE: movies with a validated English SRT" + (" (would be covered)" if cfg.dry_run else ""),
         "the goal: 100% - every uncovered movie is named below"),
        (len(covered), "Already have .eng.srt", "validated sidecar beside the movie"),
        (len(downloaded), "Downloaded this run", f"written as <movie>{EXTERNAL_SRT_SUFFIX}"),
    ]
    if dry_run or cfg.dry_run:
        rows.append((len(dry_run), "Dry-run candidates", "no files were written"))
    rows.append((needs, "NEED A SUBTITLE", "action required \u00b7 every one is listed below"))
    rows.append((total, "Movies in the library", "every folder holding an eligible MKV"))
    report.blank()
    report.scorecard(rows)

    first_action = next(
        (bucket for bucket in NEEDS_SUBTITLE_BUCKETS if buckets.get(bucket.reason)), None
    )
    if first_action is not None:
        count = len(buckets[first_action.reason])
        report.paragraph(
            f"Start here: {count} movie(s) in \"{first_action.title}\" \u00b7 {first_action.quick}."
        )
    elif needs == 0:
        report.paragraph(
            f"Nothing to do: every one of the {total} movie(s) in the library has a "
            f"validated external English {EXTERNAL_SRT_SUFFIX}."
        )

    # ---- what still needs a subtitle -------------------------------------
    report.section(
        "MOVIES THAT NEED A SUBTITLE",
        count=needs,
        total=total,
        intro=(
            "Jellyfin and Plex direct play an external subtitle only when it is named exactly "
            f"\"<movie folder>{EXTERNAL_SRT_SUFFIX}\" and sits beside the MKV. Every movie below is "
            "missing one. Groups are ordered cheapest fix first."
        ),
    )
    if needs == 0:
        report.paragraph("None. Every movie already has a validated external English subtitle.")
    else:
        for bucket in NEEDS_SUBTITLE_BUCKETS:
            items = buckets.get(bucket.reason) or []
            if not items:
                continue
            report.subsection(bucket.title, count=len(items))
            report.paragraph(bucket.fix)
            report.blank()
            report.entries(
                [(movie_label(video, cfg.library), detail) for video, detail in items],
            )

    # ---- what this run changed -------------------------------------------
    if downloaded:
        report.section(
            "DOWNLOADED DURING THIS RUN",
            count=len(downloaded),
            total=total,
            intro="Each of these was matched, validated and written this run.",
        )
        report.entries(
            [{"text": movie_label(result.video, cfg.library),
              "detail": (result.dest.name if result.dest else "")}
             for result in downloaded],
            detail_column=48,
        )
    if dry_run:
        report.section(
            "DRY-RUN CANDIDATES (NOTHING WAS WRITTEN)",
            count=len(dry_run),
            total=total,
            intro="Re-run without --dry-run to actually download these.",
        )
        report.entries(
            [{"text": movie_label(result.video, cfg.library), "detail": result.detail}
             for result in dry_run],
        )

    # ---- what is already covered -----------------------------------------
    report.section(
        f"MOVIES THAT ALREADY HAVE AN EXTERNAL {EXTERNAL_SRT_SUFFIX}",
        count=len(covered),
        total=total,
        intro=(
            "Every movie here has a validated sidecar with the exact canonical name, so "
            "Jellyfin and Plex will direct play it. No action needed."
        ),
    )
    if not covered:
        report.paragraph("None yet.")
    else:
        report.entries(
            [{"text": movie_label(result.video, cfg.library),
              "detail": (result.dest.name if result.dest else f"<movie>{EXTERNAL_SRT_SUFFIX}")}
             for result in covered],
            detail_column=48,
        )

    report.footer([
        f"Coverage this run: {covered_count} of {total} movie(s) "
        f"({coverage_pct:.1f}%) end with a validated external English SRT.",
        f"Durable quota and retry ledger  {cfg.log_file or '(none)'}",
        f"This report  {cfg.report_file}",
        "Re-running is always safe: covered movies are skipped without spending a request, "
        "uncovered movies are re-offered to the scraping sources on every UTC day, and "
        "the ledger keeps every run inside each source's UTC cap.",
    ])
    return report.render()

def write_report(results: Sequence[JobResult], cfg: QueueConfig, summary: dict[str, Any]) -> None:
    """Publish the report: written atomically, then echoed to the console."""
    text = build_report(results, cfg, summary)
    atomic_write_text(cfg.report_file, text, replace=True)
    print_text(text)
    log(f"Report written: {cfg.report_file}", log_file=cfg.log_file)

# =============================================================================
# COMPACT ROOT-LEVEL DRIVER
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch one validated external English SRT per Jellyfin MKV. "
            "OpenSubtitles and SubDL are equal sources: both providers' "
            "release-identifying routes are consulted (SubDL's score-gated "
            "release match requires score >= 0.80), and the qualifying release "
            "with the most downloads wins. A candidate is auto-selected only "
            "when its release name names the movie and its release year, "
            "carries a Blu-ray keyword, and has the most downloads."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", type=Path, default=Path(LIBRARY_DIR),
                        help="Jellyfin movie-library root")
    parser.add_argument("--report", type=Path, default=Path(REPORT_FILE),
                        help="Single replaceable human-readable report outside the library")
    parser.add_argument("--log", type=Path, default=Path(LOG_FILE),
                        help="Single root log outside the media library")
    parser.add_argument(
        "--auth-mode", choices=(AUTH_MODE_DEVELOPMENT_ANONYMOUS, AUTH_MODE_USER),
        default=DEFAULT_AUTH_MODE,
        help=("OpenSubtitles download path. development-anonymous is the default and uses only an "
              "API key where the provider permits it; user is the authenticated fallback."),
    )
    parser.add_argument("--daily-cap", type=int, default=0, metavar="N",
                        help="Maximum OpenSubtitles download requests per UTC day (0 selects the free cap for --auth-mode)")
    parser.add_argument("--subdl-daily-cap", type=int, default=0, metavar="N",
                        help=("Maximum SubDL download requests per UTC day (0 uses the conservative "
                              f"free allowance of {SUBDL_DEFAULT_DAILY_CAP})"))
    parser.add_argument("--subdl-search-daily-cap", type=int, default=0, metavar="N",
                        help=("Maximum SubDL search requests per UTC day (0 uses the conservative "
                              f"free allowance of {SUBDL_DEFAULT_SEARCH_DAILY_CAP})"))
    parser.add_argument("--scrape-daily-cap", type=int, default=None, metavar="N",
                        help=(f"Maximum scraping-source search requests per UTC day per source "
                              f"(default uses the conservative allowance of "
                              f"{SCRAPE_DEFAULT_SEARCH_DAILY_CAP}; 0 disables the scraping "
                              "fallback sources entirely)"))
    parser.add_argument("--skip-source", action="append", default=[], metavar="SOURCE",
                        choices=list(SCRAPE_PROVIDER_ORDER),
                        help="Disable one scraping source for this run (repeatable)")
    parser.add_argument("--allow-missing", action="store_true",
                        help="Exit 0 even when some movies finish without a validated English SRT "
                             "(default: exit 1 while any movie is uncovered, so the gap is loud)")
    parser.add_argument("--min-size", type=float, default=MIN_MOVIE_SIZE_MB, metavar="MB")
    parser.add_argument("--lock-timeout", type=float, default=60.0, metavar="SEC")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="Process at most N movies (0 means all eligible movies)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview candidates; searches still run, but no download request or SRT write")
    parser.add_argument("--no-identity-fallback", dest="identity_fallback", action="store_false",
                        help="Disable all conservative non-hash fallback matching after hash misses")
    parser.set_defaults(identity_fallback=True)
    parser.add_argument("--retry-review", action="store_true",
                        help="Reconsider movies previously held for manual identity review")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser

def resolve_daily_cap(auth_mode: str, requested_cap: int) -> int:
    """Select and bound the free daily limit for the explicit authentication path."""
    permitted = {
        AUTH_MODE_DEVELOPMENT_ANONYMOUS: DEVELOPMENT_ANONYMOUS_DAILY_CAP,
        AUTH_MODE_USER: USER_DAILY_CAP,
    }
    if auth_mode not in permitted:
        raise ValueError(f"unsupported authentication mode: {auth_mode}")
    cap = permitted[auth_mode] if requested_cap == 0 else int(requested_cap)
    if cap < 1:
        raise ValueError("--daily-cap must be zero (automatic) or at least 1")
    if cap > permitted[auth_mode]:
        raise ValueError(
            f"--daily-cap {cap} exceeds the documented free limit for {auth_mode}: {permitted[auth_mode]}"
        )
    return cap

def resolve_subdl_daily_cap(requested_cap: int) -> int:
    """Choose SubDL's conservative free download allowance or a user override."""
    cap = SUBDL_DEFAULT_DAILY_CAP if requested_cap == 0 else int(requested_cap)
    if cap < 1:
        raise ValueError("--subdl-daily-cap must be zero (automatic) or at least 1")
    return cap

def resolve_subdl_search_daily_cap(requested_cap: int) -> int:
    """Choose SubDL's free search allowance or a user plan-specific override."""
    cap = SUBDL_DEFAULT_SEARCH_DAILY_CAP if requested_cap == 0 else int(requested_cap)
    if cap < 1:
        raise ValueError("--subdl-search-daily-cap must be zero (automatic) or at least 1")
    return cap

def resolve_scrape_daily_cap(requested_cap: int | None) -> int:
    """Choose the scraping sources' conservative search allowance.

    None (the CLI default) keeps the tier on with the built-in allowance;
    0 disables the scraping fallback entirely; any positive N overrides it.
    """
    if requested_cap is None:
        return SCRAPE_DEFAULT_SEARCH_DAILY_CAP
    cap = int(requested_cap)
    if cap < 0:
        raise ValueError("--scrape-daily-cap must be zero (disabled) or at least 1")
    return cap

def compact_config_from_args(args: argparse.Namespace) -> QueueConfig:
    return QueueConfig(
        library=args.source.resolve(),
        log_file=args.log.resolve() if args.log else None,
        report_file=args.report.resolve(),
        api_key=(os.environ.get("OPENSUBTITLES_API_KEY") or OPENSUBTITLES_API_KEY).strip(),
        subdl_api_key=(os.environ.get("SUBDL_API_KEY") or SUBDL_API_KEY).strip(),
        username=(os.environ.get("OPENSUBTITLES_USERNAME") or OPENSUBTITLES_USERNAME).strip(),
        password=(os.environ.get("OPENSUBTITLES_PASSWORD") or OPENSUBTITLES_PASSWORD).strip(),
        daily_cap=resolve_daily_cap(str(args.auth_mode), int(args.daily_cap)),
        subdl_daily_cap=resolve_subdl_daily_cap(int(args.subdl_daily_cap)),
        subdl_search_daily_cap=resolve_subdl_search_daily_cap(int(args.subdl_search_daily_cap)),
        scrape_daily_cap=resolve_scrape_daily_cap(args.scrape_daily_cap),
        skip_sources=tuple(args.skip_source),
        allow_missing=bool(args.allow_missing),
        min_movie_size_mb=float(args.min_size),
        lock_timeout_seconds=max(0.0, float(args.lock_timeout)),
        retry_no_match=bool(args.retry_review),
        identity_fallback=bool(args.identity_fallback),
        dry_run=bool(args.dry_run),
        limit=max(0, int(args.limit)),
        auth_mode=str(args.auth_mode),
    )

def validate_compact_config(cfg: QueueConfig) -> list[str]:
    errors: list[str] = []
    if not cfg.library.is_dir() or cfg.library.is_symlink():
        errors.append("--source must be an existing non-symlink movie-library directory")
    if cfg.daily_cap < 1:
        errors.append("--daily-cap must be at least 1")
    if cfg.subdl_daily_cap < 1:
        errors.append("--subdl-daily-cap must be at least 1")
    if cfg.subdl_search_daily_cap < 1:
        errors.append("--subdl-search-daily-cap must be at least 1")
    if cfg.auth_mode not in {AUTH_MODE_DEVELOPMENT_ANONYMOUS, AUTH_MODE_USER}:
        errors.append("--auth-mode is unsupported")
    if not configured_providers(cfg) and not active_scrape_sources(cfg):
        errors.append(
            "configure OPENSUBTITLES_API_KEY and/or SUBDL_API_KEY, or keep the scraping "
            "sources enabled (--scrape-daily-cap 0 disables them)"
        )
    if cfg.api_key and cfg.auth_mode == AUTH_MODE_USER and (not cfg.username or not cfg.password):
        errors.append("--auth-mode user requires an OpenSubtitles username and password")
    if cfg.subdl_api_key.strip() and not cfg.api_key.strip() and not cfg.identity_fallback:
        errors.append("SubDL-only mode requires fallback matching; omit --no-identity-fallback")
    if cfg.min_movie_size_mb < 0 or cfg.lock_timeout_seconds < 0 or cfg.limit < 0:
        errors.append("--min-size, --lock-timeout, and --limit must be non-negative")
    if cfg.report_file == cfg.library or cfg.report_file.is_relative_to(cfg.library):
        errors.append("--report must be outside the Jellyfin media library")
    if cfg.log_file and (cfg.log_file == cfg.library or cfg.log_file.is_relative_to(cfg.library)):
        errors.append("--log must be outside the Jellyfin media library")
    return errors

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_tests()
    try:
        enable_utf8_stdio()
        cfg = compact_config_from_args(args)
        errors = validate_compact_config(cfg)
        if errors:
            for error in errors:
                print(f"Configuration error: {error}", file=sys.stderr)
            return 2
        mode = "DRY-RUN (nothing will be written)" if cfg.dry_run else "LIVE"
        print_text(report_banner(
            "JELLYFIN EXTERNAL ENGLISH SRT FETCHER",
            f"One validated external English {EXTERNAL_SRT_SUFFIX} per movie",
            [
                ("Mode", mode),
                ("Library", cfg.library),
                ("Policy", "English human-authored UTF-8 SRT; " + provider_policy_text(cfg) + "; " + SELECTION_POLICY_TEXT),
                ("Sources", provider_configuration_text(cfg) + " (UTC caps)"),
                ("Ledger", cfg.log_file),
                ("Report", cfg.report_file),
            ],
        ))
        with CoordinationLock(cfg.library, timeout_seconds=cfg.lock_timeout_seconds):
            results, summary = queue_run(cfg)
            write_report(results, cfg, summary)
        if any(result.status == "error" for result in results):
            return 1
        uncovered = int(summary.get("coverage_total", 0)) - int(summary.get("coverage_covered", 0))
        if uncovered > 0 and not cfg.allow_missing:
            print(
                f"Coverage incomplete: {uncovered} of {summary.get('coverage_total')} movie(s) "
                "still lack a validated English SRT. They are named in the report and are "
                "re-offered to the scraping sources on the next UTC day. "
                "Use --allow-missing to exit 0 anyway.",
                file=sys.stderr,
            )
            return 1
        return 0
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Subtitle fetcher failure: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
