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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

# Shared implementation: everything imported here is defined exactly once,
# in organizekit/core/. See tests/test_shared_core.py for the rule that
# keeps it that way.
from organizekit.core import (
    KIND_SYNC,
    CoordinationLock,
    LockTimeoutError,
    Report,
    RunLog,
    atomic_write_text,
    default_tool_dir,
    describe_workers,
    enable_utf8_stdio,
    iter_completed,
    open_state,
    path_is_within,
    print_text,
    resolve_library,
    resolve_workers,
    run_field_smoke_test,
    sha256_file,
    validate_srt_sidecar,
)

# The single agreed decode order. Every tool that turns subtitle bytes into
# text uses this tuple and nothing else, so a tool cannot quietly accept an
# encoding the others would reject. "utf-8-sig" first so a provider BOM does
# not make an otherwise valid file look binary; "cp1252" last because it
# decodes almost any byte sequence and would mask a genuine encoding problem.


JUNK_SUFFIXES = (".!qb", ".parts", ".part", ".crdownload", ".tmp", ".temp")

# The console/file logger every tool shares: see organizekit/core/runlog.py
# for why a logging failure is never allowed to end a run.
log = RunLog()

def is_junk_filename(name: str) -> bool:
    lower = name.casefold()
    return lower.startswith(".") or lower in {"thumbs.db", "desktop.ini"} or any(lower.endswith(s) for s in JUNK_SUFFIXES)

# =============================================================================
# Tool constants
# =============================================================================

VERSION = "1.2.0"

# The documented Windows layout; every path below is overridable per run.
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

# Sidecars are measured in parallel: ffsubsync decodes the movie's audio and
# correlates it against the subtitle, which is the slowest step in the whole
# toolchain and spends most of its time in ffmpeg rather than in Python. The
# cap is deliberately low - each worker starts an ffmpeg that is itself
# multi-threaded and reads a different movie file, so more workers than this
# turns a CPU bound into a disk bound. --workers 1 restores the serial run.
MAX_SYNC_WORKERS = 4
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


# The remembered-verdict ledger is one dict shared by every worker. Only this
# one function mutates it, so one lock here is the whole story.
_STATE_LOCK = Lock()


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
    with _STATE_LOCK:
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
    workers: int = 0  # 0 = decide from the CPU count, capped at MAX_SYNC_WORKERS
    use_state: bool = True        # publish verdicts to the shared state cache
    state_db: Path | None = None  # None = the documented default location
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
    if cfg.workers < 0:
        errors.append("--workers must be non-negative (0 = decide from the CPU count)")
    if cfg.state_db is not None and path_is_within(cfg.state_db, cfg.library):
        errors.append("--state-db must be outside the Jellyfin media library")
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
        ("Workers", describe_workers(resolve_workers(cfg.workers, cap=MAX_SYNC_WORKERS), "sidecar")),
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

def publish_state(results: list[SyncResult], cfg: Config) -> int:
    """Record each sidecar's timing verdict in the shared state cache.

    The sync ledger (``--sync-ledger``) remains the authority for "do I need to
    re-measure this?" - it is keyed by the subtitle's SHA-256 *and* the movie's
    size and mtime, which is a stricter question than this cache asks. What
    goes here is the answer ``organize status`` displays, and losing it costs a
    line of a summary, nothing more.
    """
    if cfg.dry_run:
        return 0  # a dry run measured nothing; it has nothing to publish
    store = open_state(cfg.state_db, enabled=cfg.use_state, tool="sync_subtitles")
    if not store.enabled:
        return 0
    published = 0
    try:
        for result in results:
            if result.video is None:
                continue  # an orphan sidecar: no movie to key a verdict on
            detail = f"{result.srt.name}: {result.detail}" if result.detail else result.srt.name
            store.record(result.video, KIND_SYNC, result.status, detail)
            published += 1
        store.note("sync", f"{published} sidecar(s) measured")
    except Exception as exc:  # noqa: BLE001 - a cache write can never fail a run
        log(f"state cache not updated: {exc}", level="WARNING")
    finally:
        store.close()
    return published


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
        ("Workers", describe_workers(resolve_workers(cfg.workers, cap=MAX_SYNC_WORKERS), "sidecar")),
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

    log.file = cfg.log_file

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
            workers = resolve_workers(cfg.workers, items=len(jobs), cap=MAX_SYNC_WORKERS)
            if workers > 1:
                log(f"Measuring {len(jobs)} sidecar(s) with {workers} workers; "
                    f"each one is an independent ffsubsync run.")

            def _measure(numbered: tuple[int, Job]) -> SyncResult:
                # The "syncing" line is printed by the worker as it picks the
                # job up, not by the dispatcher, so a parallel run still shows
                # what is in flight rather than announcing everything at once.
                index, job = numbered
                log(f"[{index}/{len(jobs)}] syncing {job.srt.name} against {job.video.name}")
                return sync_one(job, cfg, binary or "", features, state=sync_state)

            for outcome in iter_completed(list(enumerate(jobs, 1)), _measure, workers=workers):
                index, job = outcome.item
                if outcome.error is not None:
                    # A worker died on this sidecar. The sweep continues; the
                    # movie is reported as failed rather than silently absent.
                    result = SyncResult(srt=job.srt, video=job.video, status=STATUS_FAILED,
                                        detail=f"unhandled error: {outcome.error}")
                else:
                    result = outcome.value
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
        publish_state(results, cfg)

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
    parser.add_argument("--no-state", action="store_true",
                        help="Do not record these verdicts in the shared state cache "
                             "that `organize status` reads (the sync ledger is unaffected)")
    parser.add_argument("--state-db", type=Path, default=None, metavar="PATH",
                        help="Where that cache lives (default: beside the logs and reports)")
    parser.add_argument("--workers", type=int, default=0, metavar="N",
                        help=f"Measure N sidecars at once (0 = half the CPUs, capped at "
                             f"{MAX_SYNC_WORKERS}; 1 = the serial run)")
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
        workers=int(args.workers),
        use_state=not bool(args.no_state),
        state_db=args.state_db,
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
    """Field smoke test: can this copy read ffsubsync and find the binary?

    The trust window, the state ledger and the hold-for-review paths are
    covered in ``tests/selftests/``. Here we check the two things that are
    machine-specific: parsing this ffsubsync's output shape, and whether it is
    installed at all.
    """
    def offset_is_parsed() -> bool:
        parsed = parse_ffsubsync_output(
            "INFO:__main__:offset seconds: 2.5\nINFO:__main__:framerate scale factor: 1.000\n")
        return parsed.offset_seconds is not None and abs(parsed.offset_seconds - 2.5) < 1e-6

    def a_refusal_is_detected() -> bool:
        parsed = parse_ffsubsync_output(
            "WARNING:__main__:...\nINFO:__main__:leaving subtitles unmodified\n")
        return parsed.leaving_unmodified or parsed.failed_marker

    def ffsubsync_presence_is_reported() -> bool:
        find_ffsubsync()  # None is a legitimate answer; crashing is not
        return True

    return run_field_smoke_test("sync_subtitles.py", [
        ("an ffsubsync offset is parsed", offset_is_parsed),
        ("an ffsubsync refusal is detected", a_refusal_is_detected),
        ("the ffsubsync lookup runs", ffsubsync_presence_is_reported),
    ])

if __name__ == "__main__":
    raise SystemExit(main())
