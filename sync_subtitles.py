#!/usr/bin/env python3
"""
Subtitle Synchronizer for Jellyfin Movies (ffsubsync)
=====================================================
The pipeline's final content step, run just before the library audit. Walks
the canonical movie library, pairs every external ``.srt`` sidecar with its
movie file, and uses ``ffsubsync`` to measure how far the subtitle timing
drifts from the actual audio. When the drift is real and trustworthy the
sidecar is atomically replaced with the corrected copy; when it is
essentially zero the file is left byte-identical. When a candidate is
untrustworthy - or ffsubsync fails outright - up to 10 different replacement
downloads are tested. If none can be synced, the entry-time sidecar is restored
byte-for-byte and the movie is held for review.

Why this position:

* ``subtitle_fetcher.py`` fetches the right subtitle for the movie, but a
  fetched subtitle is only as well timed as the upload: it was authored
  against the distributor's cut, and small (or large) offsets against the
  bytes you actually own are common.
* Syncing rewrites subtitle bytes only - never movie bytes - so it does not
  disturb the OpenSubtitles moviehash. Unlike a remux it is safe at any point
  after fetching.
* It runs immediately before ``library_auditor.py`` so the audit validates
  the finished state of the library, synced sidecars included.

``ffsubsync`` (https://github.com/smacke/ffsubsync) is an external program
installed separately - ``pip install ffsubsync`` - and needs ``ffmpeg`` on
the PATH for audio extraction. This script itself stays 100% standard
library: it launches ffsubsync as a subprocess, exactly the way the cleaner
launches mkvmerge and the inspector launches ffprobe. When ffsubsync or
ffmpeg is missing the pipeline skips this step with a clear reason instead
of failing the run.

Trust window (fail-closed):

* ffsubsync runs with ``--skip-sync-on-low-quality`` when the installed
  version supports it (all current releases do), so clearly wrong alignments
  are refused by ffsubsync itself.
* Independently, this tool never applies a sync whose measured |offset|
  exceeds ``--max-offset`` (default 30 s) or whose alignment score is
  negative: a movie whose subtitles are more than half a minute off is more
  likely the wrong file than a badly desynced one. Such movies are held for
  review with the original kept.
* Offsets below ``--min-offset`` (default 0.1 s, just over one 24 fps frame)
  count as "already in sync": the original bytes are untouched.
* Every replacement stages to a dot-prefixed sibling (``.ffsync_staging.``)
  that every other tool in the pipeline treats as junk, then swaps it in
  with ``os.replace`` - a power cut can never leave a half-synced subtitle.

Remembered verdicts (idempotent re-runs):

* Measuring a movie costs a full ffsubsync run - an audio decode and an
  alignment pass - and the answer does not change while the two files are
  unchanged. A sidecar measured "in sync", or corrected and swapped in, is
  therefore recorded outside the library (``sync_state.json``) with the
  subtitles' SHA-256 and the movie's size and mtime.
* The record is evidence, never a decision: it is used only while **both**
  the sidecar bytes and the movie bytes still match it. Re-download,
  re-extract, hand-edit or replace the subtitle, or remux/replace the movie,
  and the sidecar is measured again like any other.
* Held-for-review and failed syncs are never recorded - those still need a
  human or another attempt. Nothing here can make the tool blind to a change
  it must react to; a missing, corrupt or foreign state file is simply an
  empty memory.

    py -3 sync_subtitles.py --dry-run
    py -3 sync_subtitles.py --source "E:\\torrents\\final_organized"
    py -3 sync_subtitles.py --self-test

Exit codes (designed for cron / Task Scheduler gating):

    0   every sidecar synced, in sync, or skipped cleanly
    1   at least one ffsubsync failure (originals are untouched)
    2   configuration error, or ffsubsync / ffmpeg not installed
    3   at least one sidecar held for review and --fail-on-review was given
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
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
      ``OSError`` as "busy" — ``bitdepth.py`` and ``library_auditor.py`` retried
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

def atomic_write_text(dest: Path, text: str, *, replace: bool = True) -> None:
    r"""Publish ``text`` to ``dest`` atomically and durably.

    Writes through a unique sibling file, ``fsync``\ s it, then publishes it
    with a single atomic operation, so a crash never leaves a truncated file
    and a reader always sees either the previous contents or the complete new
    ones. On failure the staged file is removed and the prior file is kept.

    The ``fsync`` is what makes this survive power loss rather than only a
    process crash: without it the rename can land while the bytes it points at
    are still only in the page cache, publishing an empty or partial file.
    ``newline="\n"`` keeps output byte-identical across platforms instead of
    silently gaining CRLFs on Windows.

    With ``replace=False`` the publish uses ``os.link``, an atomic
    create-if-absent, so an existing file is never clobbered. The subtitle
    fetcher needs this: a concurrent or hand-placed English sidecar must win
    over a download rather than be silently overwritten.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = dest.with_name(f".{dest.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp")
    try:
        with stage.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(str(stage), str(dest))
        else:
            os.link(str(stage), str(dest))
            stage.unlink()
    except OSError:
        try:
            stage.unlink(missing_ok=True)
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
        fields: Iterable[tuple[str, object]] = (),
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

STANDARDIZER_LOCK_NAME = ".movie_standardizer.lock"

class LockTimeoutError(TimeoutError):
    """Raised when a ``CoordinationLock`` cannot be acquired in time.

    Subclasses :class:`TimeoutError` so callers that historically caught the
    built-in ``TimeoutError`` (e.g. the mkv track cleaner) keep working.
    """

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
        handle = open(self.path, "a+b")  # noqa: SIM115 - released in release(), not here
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

JUNK_SUFFIXES = (".!qb", ".parts", ".part", ".crdownload", ".tmp", ".temp")

PRINT_LOCK = Lock()

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

# =============================================================================
# Tool constants
# =============================================================================

VERSION = "1.2.0"

# The documented Windows layout; every path below is overridable per run.
# ---------------------------------------------------------------------------
# Library-root resolution (vendored inline; keep every copy identical)
#
# The movie-library root used to be a bare literal repeated in six files, with
# only two of them honouring MOVIE_STD_TARGET. On a non-Windows host the tools
# that ignored it happily defaulted to a Windows drive letter, wrote reports to
# a literal path like `E:\torrents\...` in the current directory, and .gitignore
# grew an `E:*` rule to catch the debris. One resolver, used by every tool,
# removes that whole class of problem.
#
# Precedence: explicit --flag > ORGANIZE_LIBRARY > MOVIE_STD_TARGET > platform
# default. A `.env` beside the scripts is loaded first, but never overrides a
# variable already exported in the environment.
# ---------------------------------------------------------------------------

ENV_FILE_NAME = ".env"
LIBRARY_ENV_VAR = "ORGANIZE_LIBRARY"
LEGACY_LIBRARY_ENV_VAR = "MOVIE_STD_TARGET"


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Load ``KEY=value`` pairs from a .env file next to the scripts.

    The repo ships a fully documented ``.env.example`` telling users to copy it
    to ``.env``, but nothing ever read that file: every documented variable
    silently did nothing unless separately exported. This closes that gap.

    Real environment variables always win, so an explicit export still beats a
    stale file. Blank lines, ``#`` comments, a leading ``export``, and single or
    double quotes around the value are all accepted. Malformed lines are
    skipped rather than raising: a typo in a config file must not stop a
    maintenance run that would otherwise work.
    """
    env_path = path or (Path(__file__).resolve().parent / ENV_FILE_NAME)
    loaded: dict[str, str] = {}
    try:
        raw = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return loaded
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def default_library_root() -> Path:
    """The platform's documented library root when nothing else is configured.

    The Windows default is the layout the README documents. Pointing a POSIX
    host at ``E:\\torrents\\final_organized`` only ever produced a confusing
    "does not exist" (or worse, a literal ``E:...`` directory in the CWD), so
    those hosts get a sensible home-relative default instead.
    """
    if os.name == "nt":
        return Path(r"E:\torrents\final_organized")
    return Path.home() / "Media" / "Movies"


def resolve_library(explicit: Path | str | None = None) -> Path:
    """Resolve the movie-library root that every tool in the toolchain shares.

    Precedence: an explicit flag, then ORGANIZE_LIBRARY, then the legacy
    MOVIE_STD_TARGET, then the platform default.
    """
    load_dotenv()
    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser()
    for var in (LIBRARY_ENV_VAR, LEGACY_LIBRARY_ENV_VAR):
        value = (os.environ.get(var) or "").strip()
        if value:
            return Path(value).expanduser()
    return default_library_root()


def describe_library_origin(explicit: Path | str | None = None) -> str:
    """Human-readable provenance of the resolved root, for error messages."""
    load_dotenv()
    if explicit is not None and str(explicit).strip():
        return "--source"
    for var in (LIBRARY_ENV_VAR, LEGACY_LIBRARY_ENV_VAR):
        if (os.environ.get(var) or "").strip():
            return var
    return f"the default library root ({default_library_root()})"


def default_reports_root() -> Path:
    r"""Where logs, reports and probe caches go when nothing is configured.

    These must live OUTSIDE the media library (the auditor would otherwise
    count a log folder at the library root as a movie folder). On Windows that
    is the documented tools directory; elsewhere it follows the XDG state
    convention. Hardcoding the Windows path for every platform is what made a
    POSIX run scatter literal `E:\torrents\...` filenames into the current
    working directory.
    """
    if os.name == "nt":
        return Path(r"E:\torrents\tools\ReportsAndLogs")
    state_home = (os.environ.get("XDG_STATE_HOME") or "").strip()
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "organize"


def default_tool_dir(tool_name: str) -> Path:
    """The per-tool subdirectory of :func:`default_reports_root`."""
    return default_reports_root() / tool_name


DEFAULT_LIBRARY = str(resolve_library())
LOG_FILE = str(default_tool_dir("sync_subtitles") / "sync_subtitles.log")
REPORT_FILE = str(default_tool_dir("sync_subtitles") / "sync_subtitles_report.txt")

# Remembered sync verdicts, outside the library like every other artifact.
# Keyed by sidecar path; a record is honoured only while the sidecar's own
# SHA-256 and the movie's size and mtime still match it. Override with
# SUBTITLE_SYNC_LEDGER or --sync-ledger.
SYNC_STATE_FILE = str(default_tool_dir("sync_subtitles") / "sync_state.json")
SYNC_STATE_ENV = "SUBTITLE_SYNC_LEDGER"
SYNC_STATE_SCHEMA = 1
MAX_SYNC_STATE_ENTRIES = 20000  # oldest forgotten first; a miss only costs time

# Staging files sit next to the sidecar (so the final os.replace stays atomic
# on one filesystem) with a leading dot: every other tool in the pipeline
# treats dot-prefixed names as junk, so an in-flight sync is invisible to the
# auditor, the fetcher, the cleaner and the inspector.
STAGING_PREFIX = ".ffsync_staging."

# The entry points ``pip install ffsubsync`` registers; any of them works.
FFSUBSYNC_NAMES = ("ffsubsync", "ffs", "subsync")

# Containers a sidecar can be synced against. .mkv is the canonical
# movie_standardizer.py output and is always preferred; the rest exist so a
# hybrid library is not silently left out of the sync.
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".webm",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mts",
}
VIDEO_PRIORITY = (
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".webm",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mts",
)

# A subtitle must be at least this far out of sync (seconds) before the
# original is replaced; below this the measured offset is noise (ffsubsync
# resolution is 1/16000 s, and an already-synced file typically measures a
# few hundredths of a second).
DEFAULT_MIN_OFFSET_SECONDS = 0.1

# A sync that demands a shift beyond this (seconds) is held for review
# instead of applied: the most common cause of a 30+ second "offset" is a
# subtitle file for the wrong cut of the movie, and applying it would make a
# bad sidecar worse, not better.
DEFAULT_MAX_OFFSET_SECONDS = 30.0

# ffsubsync framerate corrections are discrete ratios (23.976/24 -> 1.001,
# 24/25 -> 1.042, 24/30 -> 1.25); a scale within this of 1.0 means no
# framerate change at all.
FRAMERATE_EPSILON = 0.001

DEFAULT_TIMEOUT_SECONDS = 1800.0  # a feature film's audio, with margin
DEFAULT_LOCK_TIMEOUT_SECONDS = 60.0

# When ffsubsync cannot trust the current sidecar, download a different
# qualifying English SRT and retry.  This counts replacement downloads only:
# the sidecar that entered the sync step was fetched earlier in the workflow
# (or supplied by the user) and does not consume this retry budget.  Keep the
# cap finite so one movie cannot consume an unbounded provider quota.
MAX_SYNC_REFETCHES = 10

# Result statuses. Reading order in the report is urgency order:
# review -> failed -> synced -> preview -> skipped -> in_sync -> remembered
# -> extracted.
STATUS_REVIEW = "review"
STATUS_FAILED = "failed"
STATUS_SYNCED = "synced"
STATUS_PREVIEW = "preview"
STATUS_SKIPPED = "skipped"
STATUS_IN_SYNC = "in_sync"
STATUS_REMEMBERED = "remembered"
STATUS_EXTRACTED = "extracted"

# ffsubsync writes its diagnostics to stderr (stdout is reserved for subtitle
# output, so piping stays clean). Every version logs the three measurements
# below at INFO level; newer releases render them through a rich console
# layout, so the patterns match the message text wherever it appears in a
# line. NOTE: ffsubsync exits 0 even when a sync fails (it logs the failure
# and keeps going), so these measurements - not the exit code alone - decide
# what is trustworthy.
OFFSET_RE = re.compile(r"offset seconds:\s*(-?\d+(?:\.\d+)?)")
SCALE_RE = re.compile(r"framerate scale factor:\s*(-?\d+(?:\.\d+)?)")
SCORE_RE = re.compile(r"score:\s*(-?\d+(?:\.\d+)?)")
FAILED_MARKER_RE = re.compile(r"failed to sync", re.IGNORECASE)
LEAVING_UNMODIFIED_RE = re.compile(r"leaving subtitles unmodified", re.IGNORECASE)


# =============================================================================
# ffsubsync output parsing
# =============================================================================

@dataclass(frozen=True)
class ParsedSync:
    """The three measurements ffsubsync logs, plus its failure markers."""

    score: float | None = None
    offset_seconds: float | None = None
    scale_factor: float | None = None
    failed_marker: bool = False
    leaving_unmodified: bool = False

def _last_float(pattern: re.Pattern[str], text: str) -> float | None:
    """The last measurement wins: a single invocation logs each once."""
    matches = pattern.findall(text)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None

def parse_ffsubsync_output(stderr_text: str) -> ParsedSync:
    """Extract the alignment measurements from one ffsubsync invocation's log."""
    text = stderr_text or ""
    return ParsedSync(
        score=_last_float(SCORE_RE, text),
        offset_seconds=_last_float(OFFSET_RE, text),
        scale_factor=_last_float(SCALE_RE, text),
        failed_marker=bool(FAILED_MARKER_RE.search(text)),
        leaving_unmodified=bool(LEAVING_UNMODIFIED_RE.search(text)),
    )


# =============================================================================
# Discovery
# =============================================================================

@dataclass(frozen=True)
class Job:
    """One subtitle sidecar paired with the movie file it syncs against."""

    srt: Path
    video: Path

def pick_video_for(srt_name: str, names: Sequence[str]) -> str | None:
    """Pick the movie file a sidecar syncs against among a folder's file names.

    Mirrors ffsubsync's own sibling detection: a video qualifies when its stem
    equals the sidecar's stem (``movie.srt`` beside ``movie.mkv``) or the
    sidecar's stem starts with the video's stem plus a dot (``movie.eng.srt``
    beside ``movie.mkv``). Among qualifiers an exact stem match wins, then
    .mkv (the canonical standardizer output), then a fixed extension order.
    """
    srt_stem = srt_name[: -len(".srt")]
    exact: list[str] = []
    prefixed: list[str] = []
    for name in names:
        candidate = Path(name)
        if candidate.suffix.casefold() not in VIDEO_EXTENSIONS:
            continue
        video_stem = candidate.stem
        if video_stem == srt_stem:
            exact.append(name)
        elif srt_stem.startswith(video_stem + "."):
            prefixed.append(name)
    candidates = exact or prefixed
    if not candidates:
        return None
    candidates.sort(key=lambda name: (VIDEO_PRIORITY.index(Path(name).suffix.casefold()), name))
    return candidates[0]

def discover_jobs(library: Path) -> tuple[list[Job], list[SyncResult], int]:
    """Walk the library and pair every non-junk .srt with its movie file.

    Returns ``(jobs, skipped_results, video_file_count)``. A sidecar without
    a matching movie file is a skip, not an error: there is simply nothing
    to sync it against.
    """
    jobs: list[Job] = []
    skipped: list[SyncResult] = []
    video_count = 0
    if not library.is_dir():
        return jobs, skipped, 0
    for dirpath, dirnames, filenames in os.walk(library):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        names = sorted(f for f in filenames if not is_junk_filename(f))
        for name in names:
            path = Path(dirpath) / name
            extension = Path(name).suffix.casefold()
            if extension in VIDEO_EXTENSIONS:
                try:
                    if path.is_file() and not path.is_symlink():
                        video_count += 1
                except OSError:
                    continue
            elif extension == ".srt":
                video_name = pick_video_for(name, names)
                if video_name is None:
                    skipped.append(SyncResult(
                        srt=path, video=None, status=STATUS_SKIPPED,
                        detail="no matching movie file beside the subtitle",
                    ))
                    continue
                jobs.append(Job(srt=path, video=Path(dirpath) / video_name))
    jobs.sort(key=lambda job: str(job.srt).casefold())
    skipped.sort(key=lambda res: str(res.srt).casefold())
    return jobs, skipped, video_count


# =============================================================================
# One sidecar, end to end
# =============================================================================

@dataclass
class SyncResult:
    """The outcome of one sidecar (or the reason it was never attempted)."""

    srt: Path
    video: Path | None
    status: str
    detail: str = ""
    offset_seconds: float | None = None
    scale_factor: float | None = None
    score: float | None = None
    seconds: float = 0.0
    original_sha: str = ""
    new_sha: str = ""
    error_tail: str = ""

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def classify_outcome(
    rc: int,
    staged_present: bool,
    staged_valid: bool,
    staged_reason: str,
    parsed: ParsedSync,
    cfg: Config,
) -> tuple[str, str]:
    """The decision table for one ffsubsync invocation (pure, unit-tested).

    The order matters: ffsubsync exits 0 even on a failed sync, so the output
    file and the measured values - not just the exit code - decide anything.
    """
    if rc != 0:
        return STATUS_FAILED, f"ffsubsync exited with code {rc}"
    if not staged_present:
        return STATUS_FAILED, "ffsubsync wrote no output file"
    if not staged_valid:
        return STATUS_FAILED, f"ffsubsync output is not a usable subtitle ({staged_reason})"
    if parsed.leaving_unmodified:
        return STATUS_REVIEW, (
            "ffsubsync's quality gate rejected the alignment; original kept for review"
        )
    if parsed.failed_marker or parsed.offset_seconds is None:
        return STATUS_REVIEW, (
            "ffsubsync could not measure a reliable offset; original kept for review"
        )
    if parsed.score is not None and parsed.score < 0:
        return STATUS_REVIEW, (
            f"anti-correlated alignment score {parsed.score:.0f}; original kept for review"
        )
    if abs(parsed.offset_seconds) > cfg.max_offset_seconds:
        return STATUS_REVIEW, (
            f"offset {parsed.offset_seconds:+.3f}s is beyond the "
            f"+/-{cfg.max_offset_seconds:g}s trust window; original kept for review"
        )
    if (
        abs(parsed.offset_seconds) < cfg.min_offset_seconds
        and (parsed.scale_factor is None or abs(parsed.scale_factor - 1.0) <= FRAMERATE_EPSILON)
    ):
        return STATUS_IN_SYNC, (
            f"already aligned (offset {parsed.offset_seconds:+.3f}s, below the "
            f"{cfg.min_offset_seconds:g}s threshold); original untouched"
        )
    return STATUS_SYNCED, ""

def error_tail_from(stderr_text: str, max_lines: int = 4) -> str:
    """The last few non-empty log lines, for a report that must explain itself."""
    lines = [line.strip() for line in (stderr_text or "").splitlines() if line.strip()]
    return " | ".join(lines[-max_lines:])[:400]


@dataclass(frozen=True)
class FfsubsyncFeatures:
    """Which optional ffsubsync flags the installed version supports."""

    strict: bool = False
    quality_gate: bool = False
    help_ok: bool = False

def parse_feature_flags(help_text: str) -> FfsubsyncFeatures:
    """Which quality flags exist in an ffsubsync --help dump (pure)."""
    text = help_text or ""
    return FfsubsyncFeatures(
        strict="--strict" in text,
        quality_gate="--skip-sync-on-low-quality" in text,
        help_ok=True,
    )

def detect_ffsubsync_features(binary: str) -> FfsubsyncFeatures:
    try:
        proc = subprocess.run(
            [str(binary), "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        text = _decode(proc.stdout) + "\n" + _decode(proc.stderr)
    except (OSError, subprocess.TimeoutExpired):
        return FfsubsyncFeatures()
    return parse_feature_flags(text)

def ffsubsync_version(binary: str) -> str:
    """The first line of ``ffsubsync --version`` (e.g. ``ffsubsync 0.5.1``)."""
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = (_decode(proc.stdout) + "\n" + _decode(proc.stderr)).strip().splitlines()
    return lines[0].strip() if lines else ""

def find_ffsubsync(explicit: str | None = None) -> str | None:
    """Resolve the ffsubsync executable: an explicit path, else PATH search."""
    if explicit:
        expanded = os.path.expanduser(explicit)
        return shutil.which(expanded) or (expanded if os.path.isfile(expanded) else None)
    for name in FFSUBSYNC_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None

def build_ffsubsync_command(
    binary: str,
    video: Path,
    srt: Path,
    staging: Path,
    features: FfsubsyncFeatures | None = None,
) -> list[str]:
    """The argv for one sync: reference video, one input, one staged output.

    ``--output-encoding utf-8`` is explicit because library sidecars are
    UTF-8 by contract and ffsubsync's default output is UTF-8 anyway; the
    quality flags are added only when the installed version supports them,
    so the tool works on older releases too.
    """
    command = [
        str(binary),
        str(video),
        "-i", str(srt),
        "-o", str(staging),
        "--output-encoding", "utf-8",
    ]
    if features is not None:
        if features.strict:
            command.append("--strict")
        if features.quality_gate:
            command.append("--skip-sync-on-low-quality")
    return command

def _decode(data: bytes) -> str:
    # Children pin their stdio to UTF-8 (ffsubsync included); never decode
    # with the locale encoding.
    return data.decode("utf-8", errors="replace")

def run_ffsubsync(cfg: Config, command: Sequence[str]) -> tuple[int, str, str]:
    """Launch one ffsubsync invocation.

    Kept as a single module-level function so tests can substitute a
    deterministic fake instead of launching a real binary. ``stdin`` is
    DEVNULL on purpose: ffsubsync treats piped-in data as a subtitle stream,
    and this tool always names its input explicitly.
    """
    proc = subprocess.run(
        list(command),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=cfg.timeout_seconds,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)

def _remove_staging(staging: Path) -> None:
    try:
        if staging.exists():
            staging.unlink()
    except OSError:
        pass

def _refetch_sidecar(video: Path, srt: Path, exclude_ids: list[str], log_file: Path | None) -> tuple[bool, str, str]:
    """Download a different qualifying English SRT over ``srt``."""
    try:
        from subtitle_fetcher import refetch_english_srt
    except ImportError as exc:
        return False, "", f"subtitle_fetcher unavailable ({exc})"
    return refetch_english_srt(video, srt, exclude_ids=exclude_ids, log_file=log_file)


def _extracted_sidecar_record(srt: Path, sha256: str) -> dict[str, Any] | None:
    """subtitle_fetcher's extraction record for this sidecar, when it has one.

    A sidecar extracted from the movie's own embedded track carries the
    container's own timestamps, so it is already frame-accurate for this exact
    file. Import is lazy and failure-tolerant: without subtitle_fetcher.py
    beside this script (or with a damaged record) every sidecar is simply
    measured like before.
    """
    try:
        from subtitle_fetcher import find_extracted_record
    except ImportError:
        return None
    try:
        record = find_extracted_record(srt, sha256)
    except Exception:
        return None
    return record if isinstance(record, dict) else None


def default_sync_ledger() -> Path:
    """Where remembered verdicts live: env override, then the documented path."""
    return Path(os.environ.get(SYNC_STATE_ENV) or SYNC_STATE_FILE).expanduser()


def sync_state_key(srt: Path) -> str:
    """One stable key per sidecar, normalized the way every tool compares paths."""
    return os.path.normcase(os.path.normpath(str(srt)))


def _video_snapshot(video: Path) -> dict[str, int] | None:
    """The two facts that prove the movie's bytes are unchanged."""
    try:
        stat_result = video.stat()
    except OSError:
        return None
    return {"size": int(stat_result.st_size), "mtime_ns": int(stat_result.st_mtime_ns)}


def load_sync_state(path: Path) -> dict[str, Any]:
    """Read remembered verdicts. Fail-open: an absent, unreadable, corrupt or
    foreign file is simply an empty memory, and the run measures everything."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema") != SYNC_STATE_SCHEMA:
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {str(key): value for key, value in entries.items() if isinstance(value, dict)}


def sync_state_matches(record: dict[str, Any], srt_sha: str, video: Path) -> bool:
    """True only while **both** files still match the recorded measurement.

    A sidecar re-downloaded, re-extracted, replaced by a sync or edited by
    hand has a different SHA-256; a remuxed or replaced movie has a different
    size or mtime. Either one makes the record stale and the sidecar is
    measured again exactly as if it had never been checked.
    """
    if not srt_sha or record.get("srt_sha256") != srt_sha:
        return False
    recorded = record.get("video")
    if not isinstance(recorded, dict):
        return False
    current = _video_snapshot(video)
    if current is None:
        return False
    return (
        int(recorded.get("size", -1)) == current["size"]
        and int(recorded.get("mtime_ns", -1)) == current["mtime_ns"]
    )


def remember_sync_state(
    state: dict[str, Any],
    srt: Path,
    srt_sha: str,
    video: Path,
    status: str,
    offset_seconds: float | None,
) -> None:
    """Record a finished measurement so the next run does not repeat it."""
    snapshot = _video_snapshot(video)
    if not srt_sha or snapshot is None:
        return
    key = sync_state_key(srt)
    state.pop(key, None)  # pop-then-insert refreshes recency
    state[key] = {
        "srt_sha256": srt_sha,
        "video": snapshot,
        "status": status,
        "offset_seconds": offset_seconds,
        "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def save_sync_state(path: Path, state: dict[str, Any]) -> None:
    """Persist remembered verdicts atomically, forgetting dead entries first."""
    if not state and not path.exists():
        return  # nothing measured and nothing to forget: leave no file behind
    live = {key: value for key, value in state.items() if Path(key).is_file()}
    while len(live) > MAX_SYNC_STATE_ENTRIES:
        live.pop(next(iter(live)), None)
    document = {"schema": SYNC_STATE_SCHEMA, "entries": live}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path, json.dumps(document, separators=(",", ":"), ensure_ascii=False) + "\n"
        )
    except OSError:
        pass  # a state file that cannot be saved costs the next run's speed only


def _remembered_offset(record: dict[str, Any]) -> float | None:
    value = record.get("offset_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _restore_sidecar_bytes(path: Path, content: bytes) -> str | None:
    """Atomically restore the entry-time sidecar bytes; return an error detail."""
    try:
        if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
            return None
    except OSError:
        pass
    staging = path.with_name(f"{STAGING_PREFIX}restore.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        staging.write_bytes(content)
        os.replace(staging, path)
        return None
    except OSError as exc:
        _remove_staging(staging)
        return f"CRITICAL: could not restore original sidecar ({exc})"


def sync_one(
    job: Job,
    cfg: Config,
    binary: str,
    features: FfsubsyncFeatures,
    state: dict[str, Any] | None = None,
) -> SyncResult:
    """Sync one sidecar against its movie: validate, stage, run, decide, swap.

    If ffsubsync cannot complete a trusted sync, fetch a different sidecar
    (up to ``MAX_SYNC_REFETCHES`` extra downloads) and retry until one is
    already in sync or can be synced.

    ``state`` carries the remembered verdicts of earlier runs: a sidecar whose
    bytes and whose movie's bytes are unchanged since a finished measurement
    is reported instead of being measured again.
    """
    srt, video = job.srt, job.video
    started = time.monotonic()

    usable, reason = validate_srt_sidecar(srt)
    if not usable:
        return SyncResult(srt=srt, video=video, status=STATUS_SKIPPED,
                          detail=f"sidecar is unusable ({reason})")
    try:
        if video.is_symlink() or not video.is_file():
            return SyncResult(srt=srt, video=video, status=STATUS_SKIPPED,
                              detail="movie file is not a regular readable file")
    except OSError as exc:
        return SyncResult(srt=srt, video=video, status=STATUS_SKIPPED,
                          detail=f"could not stat movie file ({exc})")

    # Keep the entry-time bytes for the whole retry transaction.  Replacement
    # candidates may temporarily occupy the canonical path because ffsubsync
    # consumes that path, but every terminal REVIEW/FAILED result restores
    # these exact bytes before returning.
    try:
        entry_bytes = srt.read_bytes()
    except OSError as exc:
        return SyncResult(srt=srt, video=video, status=STATUS_SKIPPED,
                          detail=f"could not read sidecar ({exc})")
    entry_sha = hashlib.sha256(entry_bytes).hexdigest()
    original_sha = entry_sha

    # Extracted, not downloaded: the cues came out of this movie's own
    # container timeline, so there is no drift to correct. Measuring it would
    # spend an ffsubsync run (audio decode + alignment) to prove a known zero.
    record = _extracted_sidecar_record(srt, original_sha)
    if record is not None:
        track = record.get("track_id") or "?"
        codec = record.get("codec_id") or "embedded"
        return SyncResult(
            srt=srt, video=video, status=STATUS_EXTRACTED,
            detail=(f"extracted from this movie's own {codec} track {track} - "
                    "frame-accurate by construction, no sync needed"),
            seconds=time.monotonic() - started,
            original_sha=original_sha,
            new_sha=original_sha,
        )

    # Already measured, and nothing has changed since: ffsubsync would spend a
    # full audio decode and alignment pass to reach the answer this run
    # already recorded. The record is checked against the sidecar's SHA-256
    # *and* the movie's size and mtime, so any change to either file sends the
    # sidecar back through ffsubsync.
    if state is not None:
        remembered = state.get(sync_state_key(srt))
        if isinstance(remembered, dict) and sync_state_matches(remembered, original_sha, video):
            when = str(remembered.get("measured_at") or "an earlier run")
            offset = _remembered_offset(remembered)
            detail = f"measured in sync on {when}; unchanged since, so ffsubsync was not re-run"
            if offset is not None:
                detail = (f"measured in sync on {when} (offset {offset:+.3f}s); "
                          "unchanged since, so ffsubsync was not re-run")
            return SyncResult(
                srt=srt, video=video, status=STATUS_REMEMBERED, detail=detail,
                offset_seconds=offset, seconds=time.monotonic() - started,
                original_sha=original_sha, new_sha=original_sha,
            )

    if cfg.dry_run:
        return SyncResult(srt=srt, video=video, status=STATUS_PREVIEW,
                          detail="would run ffsubsync and replace the sidecar only on a trusted sync")

    exclude_ids: list[str] = []
    last: SyncResult | None = None
    for attempt in range(MAX_SYNC_REFETCHES + 1):
        staging = srt.with_name(f"{STAGING_PREFIX}{os.getpid()}.{uuid.uuid4().hex}.srt")
        command = build_ffsubsync_command(binary, video, srt, staging, features)
        try:
            rc, _stdout, stderr = run_ffsubsync(cfg, command)
        except subprocess.TimeoutExpired:
            _remove_staging(staging)
            last = SyncResult(srt=srt, video=video, status=STATUS_FAILED,
                              detail=f"ffsubsync timed out after {cfg.timeout_seconds:.0f}s",
                              seconds=time.monotonic() - started,
                              error_tail=error_tail_from("timeout"))
            break
        except OSError as exc:
            _remove_staging(staging)
            last = SyncResult(srt=srt, video=video, status=STATUS_FAILED,
                              detail=f"could not run ffsubsync ({exc})",
                              seconds=time.monotonic() - started)
            break

        parsed = parse_ffsubsync_output(stderr)
        if parsed.offset_seconds is not None:
            log(
                f"ffsubsync measured: offset {parsed.offset_seconds:+.3f}s, "
                f"framerate x{parsed.scale_factor if parsed.scale_factor is not None else 0:.3f}, "
                f"score {parsed.score if parsed.score is not None else 0:.1f}"
            )
        staged_valid, staged_reason = False, "no output file was written"
        if staging.exists():
            staged_valid, staged_reason = validate_srt_sidecar(staging)

        status, detail = classify_outcome(rc, staging.exists(), staged_valid,
                                          staged_reason, parsed, cfg)
        error_tail = error_tail_from(stderr) if (rc != 0 or parsed.failed_marker) else ""

        if status == STATUS_SYNCED:
            new_sha = sha256_file(staging)
            try:
                os.replace(staging, srt)
            except OSError as exc:
                status, detail = STATUS_FAILED, f"could not replace sidecar ({exc})"
                _remove_staging(staging)
            else:
                detail = (
                    f"offset {parsed.offset_seconds:+.3f}s"
                    + (f", framerate x{parsed.scale_factor:.3f}" if parsed.scale_factor is not None else "")
                )
                if state is not None:
                    # The bytes now on disk are the aligned ones: remember them,
                    # not the sidecar that was just replaced.
                    remember_sync_state(state, srt, new_sha, video, STATUS_SYNCED,
                                        parsed.offset_seconds)
                return SyncResult(srt=srt, video=video, status=status, detail=detail,
                                  offset_seconds=parsed.offset_seconds,
                                  scale_factor=parsed.scale_factor, score=parsed.score,
                                  seconds=time.monotonic() - started,
                                  original_sha=entry_sha, new_sha=new_sha)

        _remove_staging(staging)
        last = SyncResult(srt=srt, video=video, status=status, detail=detail,
                          offset_seconds=parsed.offset_seconds,
                          scale_factor=parsed.scale_factor, score=parsed.score,
                          seconds=time.monotonic() - started,
                          original_sha=entry_sha, error_tail=error_tail)
        if status not in {STATUS_REVIEW, STATUS_FAILED}:
            if status == STATUS_IN_SYNC and attempt > 0:
                # A downloaded candidate is a real sidecar replacement even
                # when ffsubsync measures no correction.  Report the write
                # honestly instead of claiming the entry-time file was
                # untouched.
                candidate_sha = sha256_file(srt)
                last.status = STATUS_SYNCED
                last.detail = (
                    "replacement subtitle verified in sync"
                    + (f" (offset {parsed.offset_seconds:+.3f}s)"
                       if parsed.offset_seconds is not None else "")
                )
                last.new_sha = candidate_sha
                if state is not None:
                    remember_sync_state(state, srt, candidate_sha, video, STATUS_SYNCED,
                                        parsed.offset_seconds)
            elif status == STATUS_IN_SYNC and state is not None:
                # Nothing was written, so these are the bytes that measured
                # in sync. Held-for-review and failed syncs are never recorded.
                remember_sync_state(state, srt, entry_sha, video, STATUS_IN_SYNC,
                                    parsed.offset_seconds)
            return last
        if attempt >= MAX_SYNC_REFETCHES:
            restore_error = _restore_sidecar_bytes(srt, entry_bytes)
            if restore_error:
                last.status = STATUS_FAILED
                last.detail = f"{last.detail}; {restore_error}"
            return last
        ok, file_id, fetch_detail = _refetch_sidecar(video, srt, exclude_ids, cfg.log_file)
        if file_id:
            exclude_ids.append(str(file_id))
        if not ok:
            last.detail = f"{last.detail}; replacement fetch stopped: {fetch_detail}"
            restore_error = _restore_sidecar_bytes(srt, entry_bytes)
            if restore_error:
                last.status = STATUS_FAILED
                last.detail = f"{last.detail}; {restore_error}"
            return last
        log(f"fetched replacement subtitle id={file_id} ({fetch_detail}); retrying ffsubsync")
        original_sha = sha256_file(srt)
    assert last is not None
    restore_error = _restore_sidecar_bytes(srt, entry_bytes)
    if restore_error:
        last.status = STATUS_FAILED
        last.detail = f"{last.detail}; {restore_error}"
    return last


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class Config:
    library: Path = field(default_factory=lambda: Path(DEFAULT_LIBRARY))
    log_file: Path = field(default_factory=lambda: Path(LOG_FILE))
    report_file: Path = field(default_factory=lambda: Path(REPORT_FILE))
    sync_ledger: Path = field(default_factory=default_sync_ledger)
    min_offset_seconds: float = DEFAULT_MIN_OFFSET_SECONDS
    max_offset_seconds: float = DEFAULT_MAX_OFFSET_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS
    limit: int = 0
    dry_run: bool = False
    fail_on_review: bool = False
    ffsubsync_binary: str = ""

def validate_config(cfg: Config) -> list[str]:
    errors: list[str] = []
    if not cfg.library.is_dir() or cfg.library.is_symlink():
        errors.append("--source must be an existing non-symlink movie-library directory")
    if path_is_within(cfg.report_file, cfg.library) or cfg.report_file == cfg.library:
        errors.append("--report must be outside the Jellyfin media library")
    if path_is_within(cfg.log_file, cfg.library) or cfg.log_file == cfg.library:
        errors.append("--log must be outside the Jellyfin media library")
    if path_is_within(cfg.sync_ledger, cfg.library) or cfg.sync_ledger == cfg.library:
        errors.append("--sync-ledger must be outside the Jellyfin media library")
    if cfg.min_offset_seconds < 0:
        errors.append("--min-offset must be non-negative")
    if cfg.max_offset_seconds <= cfg.min_offset_seconds:
        errors.append("--max-offset must be greater than --min-offset")
    if cfg.timeout_seconds <= 0:
        errors.append("--timeout must be positive")
    if cfg.lock_timeout_seconds < 0:
        errors.append("--lock-timeout must be non-negative")
    if cfg.limit < 0:
        errors.append("--limit must be non-negative")
    return errors


# =============================================================================
# Report
# =============================================================================

def _short_sha(sha: str) -> str:
    return f"{sha[:12]}..." if sha else "-"

def _fmt_offset(value: float | None) -> str:
    return f"{value:+.2f}s" if value is not None else "-"

def _fmt_scale(value: float | None) -> str:
    return f"x{value:.3f}" if value is not None else "-"

def _fmt_score(value: float | None) -> str:
    return f"{value:.0f}" if value is not None else "-"

def build_report(
    results: Sequence[SyncResult],
    cfg: Config,
    *,
    video_count: int,
    ffsubsync_info: str,
    features: FfsubsyncFeatures,
    elapsed_sec: float,
    truncated: bool,
) -> str:
    """Render the run report in the shared layout."""
    report = Report(
        "JELLYFIN SUBTITLE SYNCHRONIZER (FFSUBSYNC)",
        "Every .srt sidecar checked against its movie - the final content step, right before the library audit",
    )
    report.metas([
        ("Mode", "DRY-RUN (nothing will be written)" if cfg.dry_run else "LIVE"),
        ("Library", cfg.library),
        ("ffsubsync", ffsubsync_info or "not installed (dry-run only)"),
        ("Quality gate", "on" if features.quality_gate
         else "off (older ffsubsync; client-side trust window still applies)"),
        ("Trust window", f"apply >= {cfg.min_offset_seconds:g}s drift, hold beyond +/-{cfg.max_offset_seconds:g}s"),
        ("Log", cfg.log_file),
        ("Report", cfg.report_file),
    ])
    report.blank()

    review = [r for r in results if r.status == STATUS_REVIEW]
    failed = [r for r in results if r.status == STATUS_FAILED]
    synced = [r for r in results if r.status == STATUS_SYNCED]
    preview = [r for r in results if r.status == STATUS_PREVIEW]
    skipped = [r for r in results if r.status == STATUS_SKIPPED]
    in_sync = [r for r in results if r.status == STATUS_IN_SYNC]
    extracted = [r for r in results if r.status == STATUS_EXTRACTED]
    remembered = [r for r in results if r.status == STATUS_REMEMBERED]

    report.scorecard([
        (len(synced), "Synced", "timing corrected, sidecar replaced atomically"),
        (len(in_sync), "In sync", "already aligned, file untouched"),
        (len(extracted), "Extracted (not synced)", "built from the movie's own track; no drift exists"),
        (len(remembered), "Remembered in sync", "measured on an earlier run; unchanged, not re-measured"),
        (len(review), "Held for review", "untrustworthy sync, original kept"),
        (len(failed), "Failed", "ffsubsync error, original kept"),
        (len(skipped), "Skipped", "nothing to sync (no video / unusable sidecar)"),
        (len(results), "Sidecars checked", "every non-junk .srt in the library"),
    ])
    if truncated:
        report.paragraph(f"Run limited to the first {cfg.limit} sidecar(s); the rest are not yet checked.")

    if review:
        report.paragraph(f"Start here: {len(review)} subtitle(s) need a human decision - "
                         "the sync looked wrong, and the original is untouched.")
    elif failed:
        report.paragraph(f"Start here: {len(failed)} ffsubsync failure(s) - the originals are untouched; "
                         "the section below carries ffsubsync's own error lines.")
    elif not cfg.dry_run:
        report.paragraph("Nothing needs attention: every sidecar is synced, in sync, or was safely skipped.")
    else:
        report.paragraph("Dry run: no ffsubsync invocation was made and no file will be written.")

    if review:
        report.section("SUBTITLES HELD FOR REVIEW", count=len(review),
                       intro="A sync was measured but refused: the offset is beyond the trust window, "
                             "the alignment is anti-correlated, or ffsubsync's own quality gate rejected "
                             "it. The original file is byte-identical. Watch the movie or re-fetch the "
                             "subtitle for the cut you actually own.")
        for res in review:
            report.entry(str(res.srt), detail=res.detail, fields=[
                ("Offset", _fmt_offset(res.offset_seconds)),
                ("Framerate", _fmt_scale(res.scale_factor)),
                ("Score", _fmt_score(res.score)),
                ("Video", res.video or "-"),
            ])

    if failed:
        report.section("FAILED SYNC ATTEMPTS", count=len(failed),
                       intro="ffsubsync could not finish. The original sidecar is untouched; the staged "
                             "output (if any) was removed. The error lines are ffsubsync's own stderr.")
        for res in failed:
            fields = [("Video", res.video or "-"), ("Took", f"{res.seconds:.1f}s")]
            if res.error_tail:
                fields.append(("ffsubsync said", res.error_tail))
            report.entry(str(res.srt), detail=res.detail, fields=fields)

    if synced:
        report.section("SUBTITLES SYNCED (TIMING CORRECTED)", count=len(synced),
                       intro="The drift was real and inside the trust window, so the sidecar was "
                             "replaced with the corrected copy (verified staged file, atomic swap).")
        for res in synced:
            report.entry(str(res.srt), fields=[
                ("Offset", _fmt_offset(res.offset_seconds)),
                ("Framerate", _fmt_scale(res.scale_factor)),
                ("Score", _fmt_score(res.score)),
                ("Took", f"{res.seconds:.1f}s"),
                ("SHA256", f"{_short_sha(res.original_sha)} -> {_short_sha(res.new_sha)}"),
            ])

    if preview:
        report.section("DRY-RUN PREVIEW (WOULD RUN FFSUBSYNC)", count=len(preview),
                       intro="These sidecars would be measured on a live run; only a trusted sync "
                             "would replace the file.")
        for res in preview:
            report.entry(str(res.srt), detail=res.detail, fields=[("Video", res.video or "-")])

    if skipped:
        report.section("SKIPPED (NOTHING SYNCED)", count=len(skipped),
                       intro="No sync was attempted: there is no matching movie file, or the sidecar "
                             "fails the shared subtitle contract (delete it and re-run the fetcher).")
        for res in skipped:
            report.entry(str(res.srt), detail=res.detail)

    if in_sync:
        report.section("ALREADY IN SYNC", count=len(in_sync),
                       intro="Measured drift below the threshold (and no framerate correction): the "
                             "original bytes were deliberately left untouched.")
        for res in in_sync:
            report.entry(str(res.srt), detail=res.detail, fields=[
                ("Offset", _fmt_offset(res.offset_seconds)),
                ("Took", f"{res.seconds:.1f}s"),
            ])

    if remembered:
        report.section("REMEMBERED IN SYNC (NOT RE-MEASURED)", count=len(remembered),
                       intro="An earlier run measured these sidecars, and both the subtitle and the "
                             "movie are byte-identical since, so ffsubsync was deliberately not run "
                             "again: re-measuring cannot produce a different answer and costs a full "
                             "audio decode per movie. Replace, re-download, re-extract or hand-edit "
                             "the subtitle, or remux the movie, and it is measured again.")
        for res in remembered:
            report.entry(str(res.srt), detail=res.detail, fields=[
                ("Offset", _fmt_offset(res.offset_seconds)),
                ("Video", res.video or "-"),
            ])

    if extracted:
        report.section("EXTRACTED FROM THE MOVIE (SYNC NOT NEEDED)", count=len(extracted),
                       intro="subtitle_fetcher.py built these sidecars from the movie's own embedded "
                             "subtitle track. The cues carry that container's timestamps, so they are "
                             "already aligned to this exact file; ffsubsync was deliberately not run. "
                             "A sidecar that is later replaced by a download is measured normally again.")
        for res in extracted:
            report.entry(str(res.srt), detail=res.detail, fields=[("Video", res.video or "-")])

    if not results:
        report.section("NOTHING FOUND")
        report.paragraph("No .srt sidecars exist anywhere in the library - there is nothing to sync. "
                         "Run subtitle_fetcher.py first to create the sidecars this tool aligns.")

    closing = [
        f"Sidecars checked: {len(results)} - movies with a video file: {video_count}",
        f"Elapsed: {elapsed_sec:.1f}s - Log: {cfg.log_file}",
        "A failed or untrusted sync never touches the original: every replacement is a verified "
        "staged copy swapped in with os.replace.",
    ]
    report.footer(closing)
    return report.render()

def write_report(text: str, cfg: Config) -> None:
    atomic_write_text(cfg.report_file, text)
    log(f"Report written: {cfg.report_file}")


# =============================================================================
# Run
# =============================================================================

def exit_code_for(results: Sequence[SyncResult], cfg: Config) -> int:
    """Scheduler-friendly exit code: failures dominate reviews, reviews need the flag."""
    if any(r.status == STATUS_FAILED for r in results):
        return 1
    if cfg.fail_on_review and any(r.status == STATUS_REVIEW for r in results):
        return 3
    return 0

def run(cfg: Config) -> int:
    binary = find_ffsubsync(cfg.ffsubsync_binary or None)
    if cfg.ffsubsync_binary and binary is None:
        print(f"ffsubsync not found: {cfg.ffsubsync_binary}", file=sys.stderr)
        return 2
    if binary is None and not cfg.dry_run:
        log("ffsubsync not found on PATH; nothing to do until it is installed.", level="ERROR")
        print(
            "ffsubsync not found on PATH.\n"
            "Install it once:  pip install ffsubsync\n"
            "and make sure ffmpeg is on the PATH as well (ffsubsync needs it to extract audio).\n"
            "Dry runs still work:  sync_subtitles.py --dry-run",
            file=sys.stderr,
        )
        return 2
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    if not ffmpeg_ok and not cfg.dry_run:
        log("ffmpeg not found on PATH; ffsubsync cannot extract audio without it.", level="ERROR")
        print(
            "ffmpeg not found on PATH (ffsubsync shells out to it for audio extraction).\n"
            "Install FFmpeg and keep ffmpeg.exe on the PATH, then re-run.",
            file=sys.stderr,
        )
        return 2

    version = ffsubsync_version(binary) if binary else ""
    features = detect_ffsubsync_features(binary) if binary else FfsubsyncFeatures()
    ffsubsync_info = " ".join(part for part in (binary, version) if part)
    if not features.help_ok and binary is not None:
        log("could not read ffsubsync --help; assuming no optional quality flags", level="WARNING")

    banner = Report(
        "JELLYFIN SUBTITLE SYNCHRONIZER (FFSUBSYNC)",
        "Every .srt sidecar checked against its movie - the final content step, right before the library audit",
    )
    banner.metas([
        ("Mode", "DRY-RUN (nothing will be written)" if cfg.dry_run else "LIVE"),
        ("Library", cfg.library),
        ("ffsubsync", ffsubsync_info or "not installed (dry-run only)"),
        ("Quality gate", "on" if features.quality_gate else "off (older ffsubsync)"),
        ("Trust window", f"apply >= {cfg.min_offset_seconds:g}s drift, hold beyond +/-{cfg.max_offset_seconds:g}s"),
        ("Log", cfg.log_file),
        ("Report", cfg.report_file),
    ])
    print_text(banner.render_header())
    print_text("")

    log("=" * 79)
    log("SUBTITLE SYNCHRONIZER (FFSUBSYNC)")
    log("=" * 79)
    log(f"Library  : {cfg.library}")
    log(f"Mode     : {'DRY-RUN' if cfg.dry_run else 'LIVE'}")
    log(f"ffsubsync: {ffsubsync_info or '(not installed; dry-run only)'}")
    log(f"ffmpeg   : {'found' if ffmpeg_ok else 'NOT FOUND'}")
    log(f"Trust    : min {cfg.min_offset_seconds:g}s, max +/{cfg.max_offset_seconds:g}s, "
        f"timeout {cfg.timeout_seconds:.0f}s/movie")
    log(f"Log      : {cfg.log_file}")
    log(f"Report   : {cfg.report_file}")
    log(f"Memory   : {cfg.sync_ledger}")
    log("")

    global _ACTIVE_LOG_FILE
    _ACTIVE_LOG_FILE = cfg.log_file

    results: list[SyncResult] = []
    video_count = 0
    truncated = False
    started = time.monotonic()
    # Remembered verdicts from earlier runs. A dry run reads them to show what
    # a live run would skip, and never writes: it measures nothing new.
    sync_state = load_sync_state(cfg.sync_ledger)
    try:
        with CoordinationLock(cfg.library, timeout_seconds=cfg.lock_timeout_seconds):
            jobs, skipped, video_count = discover_jobs(cfg.library)
            log(f"Found {video_count} movie file(s) and {len(jobs)} syncable subtitle sidecar(s).")
            results.extend(skipped)
            if cfg.limit and len(jobs) > cfg.limit:
                truncated = True
                log(f"--limit {cfg.limit}: checking the first {cfg.limit} sidecar(s), "
                    f"{len(jobs) - cfg.limit} not yet checked.")
                jobs = jobs[: cfg.limit]
            for index, job in enumerate(jobs, 1):
                log(f"[{index}/{len(jobs)}] syncing {job.srt.name} against {job.video.name}")
                result = sync_one(job, cfg, binary or "", features, state=sync_state)
                results.append(result)
                suffix = f" ({result.detail})" if result.detail else ""
                log(f"[{index}/{len(jobs)}] {result.status.upper():<8} {result.srt.name} "
                    f"in {result.seconds:.1f}s{suffix}")
    except LockTimeoutError as exc:
        log(str(exc), level="ERROR")
        return 2
    finally:
        elapsed = time.monotonic() - started
        results.sort(key=lambda res: str(res.srt).casefold())
        text = build_report(
            results, cfg,
            video_count=video_count,
            ffsubsync_info=ffsubsync_info,
            features=features,
            elapsed_sec=elapsed,
            truncated=truncated,
        )
        try:
            write_report(text, cfg)
        except OSError as exc:
            log(f"could not write report: {exc}", level="ERROR")
        if not cfg.dry_run:
            # A dry run measured nothing, so it has nothing new to remember.
            save_sync_state(cfg.sync_ledger, sync_state)

    review = sum(1 for r in results if r.status == STATUS_REVIEW)
    failed = sum(1 for r in results if r.status == STATUS_FAILED)
    synced = sum(1 for r in results if r.status == STATUS_SYNCED)
    in_sync = sum(1 for r in results if r.status == STATUS_IN_SYNC)
    remembered = sum(1 for r in results if r.status == STATUS_REMEMBERED)
    log("")
    log("SYNC COMPLETE")
    log(f"  Synced (replaced)   : {synced}")
    log(f"  Already in sync     : {in_sync}")
    log(f"  Remembered (skipped): {remembered}")
    log(f"  Extracted (skipped) : {sum(1 for r in results if r.status == STATUS_EXTRACTED)}")
    log(f"  Held for review     : {review}")
    log(f"  Failed              : {failed}")
    log(f"  Skipped             : {sum(1 for r in results if r.status == STATUS_SKIPPED)}")
    log(f"Report: {cfg.report_file}")

    code = exit_code_for(results, cfg)
    if code == 1:
        log(f"{failed} failure(s); see the report for ffsubsync's error lines.", level="ERROR")
    elif code == 3:
        log(f"{review} sidecar(s) held for review (--fail-on-review).", level="WARNING")
    return code


# =============================================================================
# CLI
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sync every external .srt sidecar with its movie using ffsubsync: "
            "trustworthy drift is applied atomically, zero drift leaves the file "
            "untouched, and untrustworthy drift is held for review."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--source", type=Path, default=Path(DEFAULT_LIBRARY),
                        help="Jellyfin movie-library root")
    parser.add_argument("--report", type=Path, default=Path(REPORT_FILE),
                        help="Single replaceable human-readable report outside the library")
    parser.add_argument("--log", type=Path, default=Path(LOG_FILE),
                        help="Append-only execution log outside the media library")
    parser.add_argument("--sync-ledger", type=Path, default=default_sync_ledger(),
                        metavar="PATH",
                        help="Remembered sync verdicts outside the media library "
                             f"(env {SYNC_STATE_ENV} also works). Delete it to "
                             "re-measure every sidecar.")
    parser.add_argument("--min-offset", type=float, default=DEFAULT_MIN_OFFSET_SECONDS, metavar="SEC",
                        help="Smallest |offset| (seconds) that counts as drift; below it the file is untouched")
    parser.add_argument("--max-offset", type=float, default=DEFAULT_MAX_OFFSET_SECONDS, metavar="SEC",
                        help="Largest |offset| (seconds) that will be applied; beyond it the movie is held for review")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, metavar="SEC",
                        help="Per-movie ffsubsync timeout (seconds)")
    parser.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT_SECONDS, metavar="SEC",
                        help="Maximum wait for the cross-tool coordination lock")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="Check at most N sidecars (0 means all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover and preview only; ffsubsync is never launched and nothing is written")
    parser.add_argument("--fail-on-review", action="store_true",
                        help="Exit 3 when any sidecar is held for review (for schedulers)")
    parser.add_argument("--ffsubsync", default="", metavar="PATH",
                        help=f"ffsubsync executable (default: first of {', '.join(FFSUBSYNC_NAMES)} found on PATH)")
    parser.add_argument("--self-test", action="store_true")
    return parser


def cfg_from_args(args: argparse.Namespace) -> Config:
    return Config(
        library=args.source.resolve(),
        log_file=args.log.resolve(),
        report_file=args.report.resolve(),
        sync_ledger=args.sync_ledger.expanduser().resolve(),
        min_offset_seconds=float(args.min_offset),
        max_offset_seconds=float(args.max_offset),
        timeout_seconds=float(args.timeout),
        lock_timeout_seconds=float(args.lock_timeout),
        limit=max(0, int(args.limit)),
        dry_run=bool(args.dry_run),
        fail_on_review=bool(args.fail_on_review),
        ffsubsync_binary=str(args.ffsubsync or ""),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_tests()
    try:
        enable_utf8_stdio()
        cfg = cfg_from_args(args)
        errors = validate_config(cfg)
        if errors:
            for error in errors:
                print(f"Configuration error: {error}", file=sys.stderr)
            return 2
        return run(cfg)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Subtitle sync failure: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


# =============================================================================
# SELF-TEST  (offline; never launches ffsubsync)
# =============================================================================

def run_self_tests() -> int:
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # -- log parsing ------------------------------------------------------
    rich = (
        "           INFO     score: 551.000                              ffsubsync.py:255\n"
        "           INFO     offset seconds: -3.950                      ffsubsync.py:256\n"
        "           INFO     framerate scale factor: 1.000               ffsubsync.py:257\n"
        "           INFO     writing output to out.srt                   ffsubsync.py:350\n"
    )
    parsed = parse_ffsubsync_output(rich)
    check(abs(parsed.offset_seconds - -3.950) < 1e-9, f"rich offset {parsed.offset_seconds}")
    check(abs(parsed.scale_factor - 1.0) < 1e-9, f"rich scale {parsed.scale_factor}")
    check(abs(parsed.score - 551.0) < 1e-9, f"rich score {parsed.score}")
    check(not parsed.failed_marker and not parsed.leaving_unmodified, "rich has no failure markers")

    plain = (
        "INFO:ffsubsync:score: 12.345\n"
        "INFO:ffsubsync:offset seconds: 2.5\n"
        "INFO:ffsubsync:framerate scale factor: 1.042\n"
    )
    p2 = parse_ffsubsync_output(plain)
    check(p2.offset_seconds == 2.5 and p2.scale_factor == 1.042 and p2.score == 12.345,
          f"plain parsing {p2}")

    p3 = parse_ffsubsync_output("hello world\nno numbers here\n")
    check(p3.offset_seconds is None and p3.scale_factor is None and p3.score is None,
          "unparseable text yields None")

    p4 = parse_ffsubsync_output("offset seconds: 1.0\nERROR:ffsubsync:failed to sync x.srt\n")
    check(p4.failed_marker, "failure marker detected")

    p5 = parse_ffsubsync_output("offset seconds: 1.0\noffset seconds: 2.0\n")
    check(p5.offset_seconds == 2.0, "last measurement wins")

    p6 = parse_ffsubsync_output("WARNING: low-quality alignment; leaving subtitles unmodified\n")
    check(p6.leaving_unmodified, "quality-gate marker detected")

    # -- feature flag parsing ---------------------------------------------
    feats = parse_feature_flags("usage: ffs [--strict] [--skip-sync-on-low-quality] ...")
    check(feats.strict and feats.quality_gate and feats.help_ok, "both flags detected")
    feats_old = parse_feature_flags("usage: ffs [-o SRTOUT] [--encoding ENCODING]")
    check(not feats_old.strict and not feats_old.quality_gate, "older release has neither flag")

    # -- command building ---------------------------------------------------
    cmd = build_ffsubsync_command("ffs", Path("v.mkv"), Path("s.srt"), Path("st.srt"))
    check(cmd == ["ffs", str(Path("v.mkv")), "-i", str(Path("s.srt")),
                  "-o", str(Path("st.srt")), "--output-encoding", "utf-8"],
          f"plain argv {cmd}")
    cmd2 = build_ffsubsync_command("ffs", Path("v.mkv"), Path("s.srt"), Path("st.srt"),
                                   FfsubsyncFeatures(strict=True, quality_gate=True, help_ok=True))
    check(cmd2[-2:] == ["--strict", "--skip-sync-on-low-quality"], f"flag argv {cmd2}")
    cmd3 = build_ffsubsync_command("ffs", Path("v.mkv"), Path("s.srt"), Path("st.srt"),
                                   FfsubsyncFeatures())
    check("--strict" not in cmd3 and "--skip-sync-on-low-quality" not in cmd3,
          "no optional flags when unsupported")

    # -- decision table ------------------------------------------------------
    cfg = Config(library=Path("/lib"), log_file=Path("/out/sync_subtitles.log"),
                 report_file=Path("/out/sync_subtitles_report.txt"))
    ok_parsed = ParsedSync(score=551.0, offset_seconds=-3.95, scale_factor=1.0)
    check(classify_outcome(1, True, True, "", ok_parsed, cfg)[0] == STATUS_FAILED,
          "non-zero exit is a failure even with output")
    check(classify_outcome(0, False, False, "", ok_parsed, cfg)[0] == STATUS_FAILED,
          "missing output is a failure")
    check(classify_outcome(0, True, False, "no valid cue", ok_parsed, cfg)[0] == STATUS_FAILED,
          "invalid output is a failure")
    gate = ParsedSync(score=5.0, offset_seconds=1.0, scale_factor=1.0, leaving_unmodified=True)
    check(classify_outcome(0, True, True, "", gate, cfg)[0] == STATUS_REVIEW,
          "ffsubsync quality gate refusal is a review")
    check(classify_outcome(0, True, True, "", ParsedSync(), cfg)[0] == STATUS_REVIEW,
          "unmeasured offset is a review, not a replace")
    check(classify_outcome(0, True, True, "", ParsedSync(score=-12.0, offset_seconds=1.0,
                                                         scale_factor=1.0), cfg)[0] == STATUS_REVIEW,
          "anti-correlated score is a review")
    big = ParsedSync(score=10.0, offset_seconds=45.0, scale_factor=1.0)
    check(classify_outcome(0, True, True, "", big, cfg)[0] == STATUS_REVIEW,
          "offset beyond the trust window is a review")
    tiny = ParsedSync(score=10.0, offset_seconds=0.02, scale_factor=1.0)
    check(classify_outcome(0, True, True, "", tiny, cfg)[0] == STATUS_IN_SYNC,
          "sub-threshold offset with scale 1.0 is in sync")
    fps_fix = ParsedSync(score=10.0, offset_seconds=0.02, scale_factor=1.041667)
    check(classify_outcome(0, True, True, "", fps_fix, cfg)[0] == STATUS_SYNCED,
          "a real framerate correction is applied even with a tiny offset")
    check(classify_outcome(0, True, True, "", ok_parsed, cfg)[0] == STATUS_SYNCED,
          "trusted drift is applied")

    # -- staging name ---------------------------------------------------------
    staged_name = f"{STAGING_PREFIX}{os.getpid()}.{uuid.uuid4().hex}.srt"
    check(staged_name.startswith("."), "staging file is dot-prefixed")
    check(staged_name.endswith(".srt"), "staging file keeps the .srt extension")
    check(is_junk_filename(staged_name), "staging file is junk to the other tools")

    # -- discovery ------------------------------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="sync_selftest_"))
    try:
        film = tmp / "Film (2000)"
        film.mkdir()
        (film / "Film (2000).mkv").write_bytes(b"fake video")
        (film / "Film (2000).eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHello.\n", encoding="utf-8")
        orphan = tmp / "Orphan (2001)"
        orphan.mkdir()
        (orphan / "Orphan (2001).eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nAlone.\n", encoding="utf-8")
        multi = tmp / "Dual (2002)"
        multi.mkdir()
        (multi / "Dual (2002).mkv").write_bytes(b"mkv")
        (multi / "Dual (2002).mp4").write_bytes(b"mp4")
        (multi / "Dual (2002).eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nTwo.\n", encoding="utf-8")
        (film / ".hidden.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nJunk.\n", encoding="utf-8")
        (film / "Film (2000).eng.srt.tmp").write_text("1\n00:00:01,000 --> 00:00:02,000\nJunk.\n",
                                                      encoding="utf-8")
        (film / "sample.mkv").write_bytes(b"sample video")
        (film / "sample.mkv.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nSample.\n",
                                             encoding="utf-8")
        jobs, skips, video_count = discover_jobs(tmp)
        check(len(jobs) == 3, f"jobs {len(jobs)}")
        check({j.srt.name for j in jobs} ==
              {"Film (2000).eng.srt", "Dual (2002).eng.srt", "sample.mkv.srt"},
              f"job names {[j.srt.name for j in jobs]}")
        dual = next(j for j in jobs if j.srt.name == "Dual (2002).eng.srt")
        check(dual.video.name == "Dual (2002).mkv", "mkv preferred over mp4")
        sample = next(j for j in jobs if j.srt.name == "sample.mkv.srt")
        check(sample.video.name == "sample.mkv", "plain-stem sidecar pairs with its video")
        check(len(skips) == 1 and skips[0].srt.name == "Orphan (2001).eng.srt",
              f"skips {[(s.srt.name, s.detail) for s in skips]}")
        check(video_count == 4, f"video count {video_count}")
    finally:
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)

    # -- report rendering -------------------------------------------------------
    results = [
        SyncResult(srt=Path("/lib/Film (2000)/Film (2000).eng.srt"),
                   video=Path("/lib/Film (2000)/Film (2000).mkv"),
                   status=STATUS_SYNCED, detail="offset -3.950s",
                   offset_seconds=-3.95, scale_factor=1.0, score=551.0, seconds=12.3,
                   original_sha="a" * 64, new_sha="b" * 64),
        SyncResult(srt=Path("/lib/Review (2001)/Review (2001).eng.srt"),
                   video=Path("/lib/Review (2001)/Review (2001).mkv"),
                   status=STATUS_REVIEW, detail="offset +45.0s beyond window"),
        SyncResult(srt=Path("/lib/Broken (2002)/Broken (2002).eng.srt"),
                   video=Path("/lib/Broken (2002)/Broken (2002).mkv"),
                   status=STATUS_FAILED, detail="ffsubsync exited with code 1",
                   error_tail="ffmpeg not found"),
        SyncResult(srt=Path("/lib/Skipped (2003)/Skipped (2003).eng.srt"),
                   video=None, status=STATUS_SKIPPED, detail="no matching movie file"),
        SyncResult(srt=Path("/lib/Fine (2004)/Fine (2004).eng.srt"),
                   video=Path("/lib/Fine (2004)/Fine (2004).mkv"),
                   status=STATUS_IN_SYNC, detail="already aligned (offset +0.020s)"),
    ]
    text = build_report(results, cfg, video_count=5, ffsubsync_info="ffs ffsubsync 0.5.1",
                        features=FfsubsyncFeatures(strict=True, quality_gate=True, help_ok=True),
                        elapsed_sec=12.3, truncated=False)
    lines = text.splitlines()
    check(text.endswith("\n"), "report ends with a newline")
    check(all(not line.endswith(" ") for line in lines), "no trailing whitespace")
    check(all(len(line) <= REPORT_WIDTH for line in lines), "every line fits the page width")
    check("JELLYFIN SUBTITLE SYNCHRONIZER" in text, "title present")
    for title in ("SUBTITLES HELD FOR REVIEW", "FAILED SYNC ATTEMPTS",
                  "SUBTITLES SYNCED (TIMING CORRECTED)", "SKIPPED (NOTHING SYNCED)",
                  "ALREADY IN SYNC"):
        check(title in text, f"section {title} present")
    review_pos = text.index("SUBTITLES HELD FOR REVIEW")
    failed_pos = text.index("FAILED SYNC ATTEMPTS")
    synced_pos = text.index("SUBTITLES SYNCED")
    check(review_pos < failed_pos < synced_pos, "urgency order: review, failed, synced")

    # -- exit codes ---------------------------------------------------------------
    check(exit_code_for([results[0]], cfg) == 0, "all synced is 0")
    check(exit_code_for([results[2]], cfg) == 1, "a failure is 1")
    check(exit_code_for([results[1]], cfg) == 0, "a review alone is 0 without the flag")
    strict_cfg = Config(library=Path("/lib"), log_file=Path("/out/x.log"),
                        report_file=Path("/out/x.txt"), fail_on_review=True)
    check(exit_code_for([results[1]], strict_cfg) == 3, "a review is 3 with --fail-on-review")
    check(exit_code_for([results[1], results[2]], strict_cfg) == 1, "failure dominates review")

    # -- lock identity ----------------------------------------------------------------
    lock = CoordinationLock(Path("/some/library"))
    check(lock.path.name.startswith(".movie_standardizer.lock."),
          "shares the standardizer coordination lock key")

    # -- constants ---------------------------------------------------------------------
    check(EXTERNAL_SRT_SUFFIX == ".eng.srt", "canonical sidecar suffix")
    check(Path(LOG_FILE).parent == default_tool_dir("sync_subtitles"),
          "log defaults under the platform reports root")
    check(Path(REPORT_FILE).parent == default_tool_dir("sync_subtitles"),
          "report defaults under the platform reports root")

    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print("  -", error)
        return 1
    print("SELF-TEST PASSED (parse + flags + argv + decision table + discovery + report + exit codes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
