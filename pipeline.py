#!/usr/bin/env python3
"""Run the manual half of the Jellyfin movie pipeline in the right order.

``movie_standardizer.py`` is the qBittorrent completion hook and runs by itself
the moment a download stops, so it is deliberately not part of this sweep. What
is left — fetching subtitles, cleaning tracks, checking bit depth, syncing
subtitle timing, auditing the library — is five separate commands, and the
order between the first two is load-bearing:

    subtitle_fetcher.py   MUST run before mkv_track_cleaner.py

``subtitle_fetcher.py`` searches OpenSubtitles by moviehash, which is the file
size plus the sum of the first and last 64 KiB. It can then use SubDL's
score-gated release-aware fallback. A remux rewrites those bytes, so any movie cleaned
first can never reproduce its release hash and is silently demoted to the much
weaker title/year search. ``sync_subtitles.py`` runs last of the content
steps, just before the audit: it rewrites subtitle bytes only (never movie
bytes), so the moviehash is undisturbed - but the audit must see the finished
sidecars. Running the five scripts
by hand makes that easy to get wrong on a busy day; this script cannot get it
wrong.

Each tool runs as its own subprocess so it keeps its own locks, logs and
reports exactly as it would standalone. Steps whose prerequisites are missing
are skipped with a clear reason rather than crashing the run — no API key means
no fetching, no mkvmerge means no cleaning, no ffprobe means no bit-depth scan.

    python pipeline.py --dry-run
    python pipeline.py --source "E:\\torrents\\final_organized"
    python pipeline.py --steps fetcher,auditor
    python pipeline.py --self-test

Stdlib only. No Python packages required.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared helpers (vendored inline)
#
# This script is self-contained on purpose: every helper it needs is copied
# below instead of imported from a shared module, so you can take this single
# file anywhere and run it with nothing but the Python standard library.
# The other scripts in this repo carry byte-identical copies of the same
# helpers; if you change one, keep the others in sync.
# ---------------------------------------------------------------------------

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

VERSION = "1.0.0"

HERE = Path(__file__).resolve().parent
DEFAULT_LIBRARY = r"E:\torrents\final_organized"

# The canonical order. Index order is the execution order; do not reorder
# without re-reading the moviehash note in the module docstring.
STEP_ORDER = ("fetcher", "cleaner", "10bit", "sync", "auditor")

@dataclass(frozen=True)
class Step:
    key: str
    script: str
    title: str
    # The flag each tool uses for the movie-library root; they are not uniform.
    root_flag: str
    supports_dry_run: bool = True
    supports_limit: bool = True
    supports_nice: bool = False

STEPS: dict[str, Step] = {
    "fetcher": Step(
        key="fetcher", script="subtitle_fetcher.py", title="Fetch English SRT subtitles",
        root_flag="--source",
    ),
    "cleaner": Step(
        key="cleaner", script="mkv_track_cleaner.py", title="Clean MKV tracks (remux)",
        root_flag="--dir", supports_nice=True,
    ),
    "10bit": Step(
        key="10bit", script="10bit.py", title="Check 8-bit vs 10-bit / HDR",
        root_flag="--source",
    ),
    "sync": Step(
        key="sync", script="sync_subtitles.py", title="Sync subtitle timing (ffsubsync)",
        root_flag="--source",
    ),
    "auditor": Step(
        key="auditor", script="library_auditor.py", title="Audit library layout",
        root_flag="--source", supports_dry_run=False, supports_limit=False,
    ),
}

@dataclass
class Config:
    library: Path = Path(DEFAULT_LIBRARY)
    steps: tuple[str, ...] = STEP_ORDER
    dry_run: bool = False
    limit: int = 0
    nice: bool = False
    continue_on_error: bool = True

# Shown before a step runs, because the failure mode is silent and easy to
# misread as "nothing to do".
HINTS: dict[str, str] = {
    "cleaner": (
        "Movies still hardlinked to their qBittorrent source are ALWAYS deferred, never "
        "cleaned - there is no override. If this step cleans nothing, open E:\\torrents\\final: "
        "qBittorrent's default seed-limit action only pauses the torrent and leaves the file, so "
        "the link count never drops and the deferral never clears. Delete the source (safe - it "
        "is a hardlink, so your library copy keeps the data) or set qBittorrent to remove the "
        "content when seeding stops."
    ),
    "fetcher": (
        "Runs before the cleaner on purpose: a remux rewrites the OpenSubtitles moviehash, "
        "so cleaning first would force the weaker title/year search."
    ),
    "sync": (
        "Runs after every other tool on purpose: it rewrites subtitle bytes, never movie "
        "bytes, so the moviehash is undisturbed - but it must finish before the audit so the "
        "audit sees the finished sidecars. A bad sync is worse than none: untrusted alignments "
        "are held for review, never applied."
    ),
}

@dataclass
class StepResult:
    key: str
    title: str
    status: str          # ran | skipped | missing
    returncode: int | None = None
    seconds: float = 0.0
    detail: str = ""

@dataclass
class Run:
    results: list[StepResult] = field(default_factory=list)
    elapsed: float = 0.0

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

def _api_key_present() -> bool:
    """Return whether at least one supported subtitle provider is configured."""
    try:
        import subtitle_fetcher as sf
    except Exception:
        return False
    keys = (
        os.environ.get("OPENSUBTITLES_API_KEY"),
        sf.OPENSUBTITLES_API_KEY,
        os.environ.get("SUBDL_API_KEY"),
        sf.SUBDL_API_KEY,
    )
    return any(str(key or "").strip() for key in keys)

def _mkvmerge_present() -> bool:
    try:
        import mkv_track_cleaner as tc
        tc.resolve_mkvmerge_path()
        return True
    except Exception:
        return shutil.which("mkvmerge") is not None

def _ffprobe_present() -> bool:
    try:
        import _10bit  # type: ignore  # module name starts with a digit
        return _10bit.find_ffprobe() is not None
    except Exception:
        pass
    try:
        import importlib

        probe = importlib.import_module("10bit")
        return probe.find_ffprobe() is not None
    except Exception:
        return shutil.which("ffprobe") is not None

def _ffsubsync_present() -> bool:
    """ffsubsync (any entry point) on PATH, plus the ffmpeg it shells out to."""
    try:
        import sync_subtitles as ss
        if ss.find_ffsubsync() is None:
            return False
    except Exception:
        if not any(shutil.which(name) for name in ("ffsubsync", "ffs", "subsync")):
            return False
    return shutil.which("ffmpeg") is not None

PREREQUISITES: dict[str, tuple[Callable[[], bool], str]] = {
    "fetcher": (
        _api_key_present,
        "no subtitle-provider key; set OPENSUBTITLES_API_KEY and/or SUBDL_API_KEY to enable fetching",
    ),
    "cleaner": (
        _mkvmerge_present,
        "mkvmerge (MKVToolNix) not found on PATH or in the standard install locations",
    ),
    "10bit": (
        _ffprobe_present,
        "ffprobe (FFmpeg) not found on PATH or in the standard install locations",
    ),
    "sync": (
        _ffsubsync_present,
        "ffsubsync not found on PATH (install it with `pip install ffsubsync`) or ffmpeg "
        "missing; ffsubsync needs both to sync subtitles",
    ),
}

def prerequisite_issue(step: Step) -> str | None:
    """Return a reason to skip ``step``, or ``None`` when it can run."""
    script_path = HERE / step.script
    if not script_path.is_file():
        return f"{step.script} is missing from this directory"
    check, reason = PREREQUISITES.get(step.key, (lambda: True, ""))
    try:
        if not check():
            return reason
    except Exception:
        return reason or "prerequisite check failed"
    return None

def build_command(step: Step, cfg: Config) -> list[str]:
    """Build the argv for one step, mirroring what you would type by hand."""
    command = [sys.executable, str(HERE / step.script), step.root_flag, str(cfg.library)]
    if cfg.dry_run and step.supports_dry_run:
        command.append("--dry-run")
    if cfg.limit and step.supports_limit:
        command.extend(["--limit", str(cfg.limit)])
    if cfg.nice and step.supports_nice:
        command.append("--nice")
    return command

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run_step(step: Step, cfg: Config, dry_run_pipeline: bool = False) -> StepResult:
    issue = prerequisite_issue(step)
    if issue is not None:
        return StepResult(step.key, step.title, "skipped", detail=issue)

    command = build_command(step, cfg)
    print(f"\n{'=' * 78}\nSTEP {step.key}: {step.title}\n{'=' * 78}", flush=True)
    hint = HINTS.get(step.key)
    if hint:
        print(f"  note: {hint}", flush=True)
    if dry_run_pipeline:
        print("  would run: " + " ".join(f'"{part}"' if " " in part else part
                                         for part in command), flush=True)
        return StepResult(step.key, step.title, "skipped", detail="pipeline dry-run")

    print("  " + " ".join(f'"{part}"' if " " in part else part for part in command), flush=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=str(HERE), check=False)
        code: int | None = completed.returncode
    except OSError as exc:
        return StepResult(step.key, step.title, "missing", detail=f"could not launch: {exc}")
    return StepResult(step.key, step.title, "ran", returncode=code,
                      seconds=time.monotonic() - started)

def run_pipeline(cfg: Config, dry_run: bool = False) -> Run:
    started = time.monotonic()
    run = Run()
    for key in cfg.steps:
        step = STEPS[key]
        result = run_step(step, cfg, dry_run_pipeline=dry_run)
        run.results.append(result)
        if result.status == "ran" and result.returncode:
            print(f"\n  STEP {key} exited with code {result.returncode}.", flush=True)
            if not cfg.continue_on_error:
                print("  Stopping. Use --continue-on-error (the default) to run the remaining steps anyway.",
                      flush=True)
                break
    run.elapsed = time.monotonic() - started
    return run

def build_summary(run: Run, cfg: Config) -> str:
    """Render the pipeline summary with the same layout as every tool's report."""
    label = {"ran": "RAN", "skipped": "SKIP", "missing": "MISSING"}
    failed = [r.key for r in run.results if r.status == "ran" and r.returncode]
    skipped = [r.key for r in run.results if r.status != "ran"]
    succeeded = [r.key for r in run.results if r.status == "ran" and not r.returncode]

    report = Report(
        "JELLYFIN MOVIE PIPELINE SUMMARY",
        "Every tool, in the order that keeps exact-hash subtitle matching possible",
    )
    report.metas([
        ("Library", cfg.library),
        ("Steps", ", ".join(cfg.steps) or "(none selected)"),
        ("Mode", "DRY-RUN (showing commands only)" if cfg.dry_run else "LIVE"),
        ("Elapsed", f"{run.elapsed:.1f}s"),
    ])
    report.blank()
    report.scorecard([
        (len(succeeded), "Completed", "exited 0"),
        (len(failed), "Failed", "exited non-zero; read that tool's report"),
        (len(skipped), "Not run", "skipped or a required binary is missing"),
        (len(run.results), "Steps attempted", "in the canonical order"),
    ])
    if failed:
        report.paragraph(f"Start here: {len(failed)} step(s) failed \u00b7 their own report files "
                         "carry the per-movie detail.")

    report.section("STEP RESULTS", count=len(run.results),
                   intro="In pipeline order. Each tool writes its own report and log.")
    if not run.results:
        report.paragraph("No steps ran.")
    else:
        report.table(
            ["Status", "Step", "Tool", "Outcome"],
            [[label.get(r.status, r.status.upper()), r.key, r.title,
              (f"ok ({r.seconds:.1f}s)" if not r.returncode else f"exit {r.returncode}")
              if r.status == "ran" else r.detail]
             for r in run.results],
            aligns="<<<<",
        )

    closing: list[str] = []
    if failed:
        closing.append(f"Failed steps : {', '.join(failed)}")
    if skipped:
        closing.append(f"Not run      : {', '.join(skipped)}")
    if not failed and not skipped:
        closing.append("All steps completed.")
    report.footer(closing)
    return report.render()

def resolve_steps(requested: Sequence[str]) -> tuple[str, ...]:
    """Keep the canonical order regardless of the order flags were supplied in."""
    chosen = set(requested)
    return tuple(key for key in STEP_ORDER if key in chosen)

def resolve_library(cli_source: Path | None) -> Path:
    """Resolve the library root: explicit flag, then MOVIE_STD_TARGET, then default.

    Scheduled runs (cron, Task Scheduler, qBittorrent hooks) typically set
    ``MOVIE_STD_TARGET`` once in the environment, so honoring it here means
    ``pipeline.py`` can run without retyping ``--source`` on every invocation.
    """
    if cli_source is not None:
        return cli_source.expanduser().resolve()
    return Path(os.environ.get("MOVIE_STD_TARGET") or DEFAULT_LIBRARY).expanduser().resolve()

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Run the manual Jellyfin movie steps in the correct order: "
                     "subtitles, then track cleaning, then bit depth, then subtitle sync, then audit."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("movie_standardizer.py is the qBittorrent completion hook and is not part of\n"
                "this sweep. Subtitles are fetched before the remux because a remux rewrites\n"
                "the OpenSubtitles moviehash and would force a weaker title/year search.\n"
                "Subtitle sync (ffsubsync) runs just before the audit: it only rewrites\n"
                "subtitle bytes, so the audit sees the finished sidecars."),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--source", type=Path, default=None,
                        help=f"Jellyfin movie-library root (default: {DEFAULT_LIBRARY}, or MOVIE_STD_TARGET when set)")
    parser.add_argument("--steps", default=",".join(STEP_ORDER),
                        help=f"Comma-separated subset of: {', '.join(STEP_ORDER)}")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the commands that would run, without running them")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="Pass --limit N to the steps that support it (0 = all)")
    parser.add_argument("--nice", action="store_true",
                        help="Lower remux priority so track cleaning does not starve Jellyfin")
    parser.add_argument("--continue-on-error", dest="continue_on_error", action="store_true", default=True,
                        help="Keep going after a step fails instead of stopping (default)")
    parser.add_argument("--stop-on-error", dest="continue_on_error", action="store_false",
                        help="Stop the pipeline on the first step failure")
    parser.add_argument("--list-steps", action="store_true",
                        help="Print the steps, what needs to be installed, and exit")
    parser.add_argument("--self-test", action="store_true")
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    enable_utf8_stdio()
    args = build_parser().parse_args(argv)

    if args.self_test:
        return run_self_tests()
    if args.list_steps:
        for key in STEP_ORDER:
            step = STEPS[key]
            issue = prerequisite_issue(step)
            state = f"blocked: {issue}" if issue else "ready"
            print(f"  {key:<9} {step.title:<32} {state}")
        return 0

    requested = [part.strip() for part in str(args.steps).split(",") if part.strip()]
    unknown = [key for key in requested if key not in STEPS]
    if unknown:
        print(f"Unknown step(s): {', '.join(unknown)}. Known: {', '.join(STEP_ORDER)}",
              file=sys.stderr)
        return 2
    if not requested:
        print("No steps selected.", file=sys.stderr)
        return 2

    cfg = Config(
        library=resolve_library(args.source),
        steps=resolve_steps(requested),
        dry_run=bool(args.dry_run),
        limit=max(0, int(args.limit)),
        nice=bool(args.nice),
        continue_on_error=bool(args.continue_on_error),
    )
    if not cfg.library.is_dir():
        print(f"Library directory does not exist: {cfg.library}", file=sys.stderr)
        return 2

    run = run_pipeline(cfg, dry_run=cfg.dry_run)
    print()
    print_text(build_summary(run, cfg))
    return 1 if any(r.status == "ran" and r.returncode for r in run.results) else 0

# ---------------------------------------------------------------------------
# SELF-TEST  (offline; never launches a tool)
# ---------------------------------------------------------------------------

def run_self_tests() -> int:
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # The ordering is the whole point of this script.
    check(STEP_ORDER == ("fetcher", "cleaner", "10bit", "sync", "auditor"), "canonical step order")
    check(STEP_ORDER.index("fetcher") < STEP_ORDER.index("cleaner"),
          "subtitles must be fetched before the remux invalidates the moviehash")
    check(STEP_ORDER.index("sync") < STEP_ORDER.index("auditor"),
          "subtitle sync must finish before the audit sees the sidecars")

    # Order is preserved no matter how the user types the flag.
    for requested in (["auditor", "fetcher"], ["10bit", "cleaner", "fetcher"],
                      ["auditor"], ["cleaner", "10bit"]):
        resolved = resolve_steps(requested)
        check(resolved == tuple(k for k in STEP_ORDER if k in set(requested)),
              f"resolve_steps preserves canonical order for {requested}: {resolved}")
    check(resolve_steps([]) == (), "empty selection resolves to nothing")

    check(set(STEPS) == set(STEP_ORDER), "every step in the order has a definition")

    # Library resolution: flag wins, then the environment, then the documented
    # default. Docker and scheduled runs rely on the middle case.
    saved_target = os.environ.pop("MOVIE_STD_TARGET", None)
    try:
        check(resolve_library(None) == Path(DEFAULT_LIBRARY).resolve(),
              "no flag and no env resolves to the documented default library")
        os.environ["MOVIE_STD_TARGET"] = str(Path("/media/movies"))
        check(resolve_library(None) == Path("/media/movies").resolve(),
              "MOVIE_STD_TARGET is honored when no --source flag is given")
        check(resolve_library(Path("/srv/library")) == Path("/srv/library").resolve(),
              "an explicit --source flag beats MOVIE_STD_TARGET")
    finally:
        if saved_target is not None:
            os.environ["MOVIE_STD_TARGET"] = saved_target
        else:
            os.environ.pop("MOVIE_STD_TARGET", None)

    # Each tool's library-root flag differs; getting these wrong silently
    # points a tool at the default path instead of the requested library.
    check(STEPS["fetcher"].root_flag == "--source", "fetcher uses --source")
    check(STEPS["cleaner"].root_flag == "--dir", "cleaner uses --dir")
    check(STEPS["10bit"].root_flag == "--source", "10bit uses --source")
    check(STEPS["sync"].root_flag == "--source", "sync uses --source")
    check(STEPS["auditor"].root_flag == "--source", "auditor uses --source")

    library = Path("/media/movies")
    base = [sys.executable, str(HERE / "subtitle_fetcher.py"), "--source", str(library)]
    check(build_command(STEPS["fetcher"], Config(library=library)) == base, "plain fetcher argv")
    check(build_command(STEPS["fetcher"], Config(library=library, dry_run=True))
          == base + ["--dry-run"], "dry-run flag")
    check(build_command(STEPS["fetcher"], Config(library=library, limit=5))
          == base + ["--limit", "5"], "limit flag")
    check(build_command(STEPS["cleaner"], Config(library=library, nice=True))
          == [sys.executable, str(HERE / "mkv_track_cleaner.py"), "--dir", str(library),
              "--nice"], "cleaner flags")
    # The auditor is already read-only, so it has no --dry-run to forward.
    check("--dry-run" not in build_command(STEPS["auditor"],
                                           Config(library=library, dry_run=True)),
          "auditor has no dry-run flag to forward")
    check("--limit" not in build_command(STEPS["auditor"], Config(library=library, limit=3)),
          "auditor has no limit flag to forward")

    # A missing script is reported as a skip, never a crash.
    ghost = Step(key="ghost", script="does-not-exist.py", title="ghost", root_flag="--source")
    check(prerequisite_issue(ghost) is not None, "missing script is skipped, not crashed")

    # The summary names what happened.
    summary = build_summary(
        Run(results=[
            StepResult("fetcher", "Fetch", "ran", returncode=0, seconds=1.5),
            StepResult("cleaner", "Clean", "skipped", detail="mkvmerge not found"),
        ], elapsed=2.0),
        Config(library=library),
    )
    check("SKIP" in summary and "mkvmerge not found" in summary, "summary reports a skip")
    check("RAN" in summary and "ok" in summary, "summary reports a successful run")
    failed_summary = build_summary(
        Run(results=[StepResult("fetcher", "Fetch", "ran", returncode=1, seconds=0.1)]),
        Config(library=library),
    )
    check("Failed steps : fetcher" in failed_summary, "summary reports failures")

    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print("  -", error)
        return 1
    print("SELF-TEST PASSED (step order + flag mapping + skip handling + summary)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
