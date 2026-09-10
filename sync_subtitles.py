#!/usr/bin/env python3
"""
Subtitle Synchronizer for Jellyfin Movies (ffsubsync)
=====================================================
The pipeline's final content step, run just before the library audit. It has
exactly one job: measure every sidecar that subtitle_extractor.py just built
from a movie's own embedded track against that movie's real audio, and correct
the timing when - and only when - the drift is real and trustworthy.

Which sidecars are synced is a closed rule:

* a sidecar extracted from the movie's own embedded track is synced exactly
  once, on the run that follows its extraction. The extractor's provenance
  ledger names those files, and this tool marks each one done after it has
  been measured (whether it needed a correction or not);
* every other sidecar - one that already existed beside the movie, was
  placed by hand, downloaded in the fetching era, or already synced - is
  left untouched. An existing ``.eng.srt`` is authoritative; re-syncing it
  is not this tool's call.

A container's own timestamps are not always honest, which is why even an
extracted sidecar is measured once rather than trusted blindly. When the
drift is real and within the trust window the sidecar is atomically replaced
with the corrected copy; when it is essentially zero the file is left
byte-identical and marked done; when the measurement is untrustworthy the
movie is held for review and the sidecar stays unmarked, so the next run
tries again. There are no replacement downloads any more - the fetching
machinery is gone - so a held sidecar stays exactly as it is until a human
decides otherwise.

Why this position:

* ``subtitle_extractor.py`` writes the sidecar from the movie's own track;
  the cleaner that follows strips every embedded subtitle, so the sync must
  happen after extraction (else it would measure against a movie whose
  audio it will no longer have) and before the audit (which must see the
  finished state).
* Syncing rewrites subtitle bytes only - never movie bytes - so it is safe
  against whatever container the movie currently is (the MP4 -> MKV
  conversion preserves the audio stream's timeline).

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
  count as "already in sync": the original bytes are untouched and the
  sidecar is marked done.
* Every candidate stages to a dot-prefixed sibling (``.ffsync_staging.``)
  that every other tool in the pipeline treats as junk, then swaps it in
  with ``os.replace`` - a power cut can never leave a half-synced subtitle.

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
from pathlib import Path

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

# Staging files sit next to the sidecar (so the final os.replace stays atomic
# on one filesystem) with a leading dot: every other tool in the pipeline
# treats dot-prefixed names as junk, so an in-flight sync is invisible to the
# auditor, the extractor, the cleaner and the inspector.
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

# Result statuses. Reading order in the report is urgency order:
# review -> failed -> synced -> preview -> skipped -> in_sync.
STATUS_REVIEW = "review"
STATUS_FAILED = "failed"
STATUS_SYNCED = "synced"
STATUS_PREVIEW = "preview"
STATUS_SKIPPED = "skipped"
STATUS_IN_SYNC = "in_sync"

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


def _sidecar_needs_sync(srt: Path, sha256: str) -> tuple[bool, str]:
    """Ask the extractor's provenance ledger whether this sidecar may be synced.

    The closed rule this tool runs on: only a sidecar extracted from the
    movie's own embedded track - and not yet marked done - is measured.
    Everything else (a pre-existing ``.eng.srt``, a hand-placed file, an
    already-synced extraction) is left untouched. Import is lazy and
    failure-tolerant: without subtitle_extractor.py beside this script the
    answer is simply "not ours", which is the safe default.
    """
    try:
        import subtitle_extractor as sx
    except ImportError:
        return False, "subtitle_extractor is unavailable; no sidecar can be proven extracted"
    try:
        needs = sx.extracted_sidecar_needs_sync(srt, sha256)
        record = sx.find_extracted_record(srt, sha256)
    except Exception:  # noqa: BLE001 - reaching into another tool's ledger: any
        # failure at all means "no provenance record", which is the safe answer.
        return False, "the extraction ledger could not be read"
    if not needs:
        if record is not None and str(record.get("synced_utc") or ""):
            return False, f"already synced on {record['synced_utc']}; marked done in the extraction ledger"
        return False, "not extracted from this movie's own tracks; an existing sidecar is authoritative"
    return True, ""


def _mark_sidecar_synced(srt: Path, provenance_sha: str, new_sha: str | None = None) -> None:
    """Flag the provenance record so this sidecar is never re-synced.

    ``provenance_sha`` is the sha that authorised the sync (the extraction
    record's own sha); ``new_sha`` re-points the record at the replaced bytes
    when ffsubsync's output was swapped in.
    """
    try:
        import subtitle_extractor as sx
        sx.mark_extracted_sidecar_synced(srt, provenance_sha, new_sha256=new_sha)
    except Exception:  # noqa: BLE001 - best effort: a lost mark costs the next
        # run one redundant measurement, never a wrong sync.
        pass


def sync_one(
    job: Job,
    cfg: Config,
    binary: str,
    features: FfsubsyncFeatures,
) -> SyncResult:
    """Sync one extracted sidecar against its movie: validate, stage, run, decide, swap.

    Only a sidecar the extractor's provenance ledger names - and has not yet
    marked done - reaches ffsubsync. A held-for-review or failed measurement
    leaves the record unmarked, so the next run retries it; a synced or
    in-sync outcome marks it done, permanently.
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

    try:
        entry_bytes = srt.read_bytes()
    except OSError as exc:
        return SyncResult(srt=srt, video=video, status=STATUS_SKIPPED,
                          detail=f"could not read sidecar ({exc})")
    original_sha = hashlib.sha256(entry_bytes).hexdigest()

    # The closed rule: sync exactly the sidecars extracted from the movie's
    # own tracks, exactly once. Everything else is somebody else's file.
    needs_sync, sync_skip_reason = _sidecar_needs_sync(srt, original_sha)
    if not needs_sync:
        return SyncResult(srt=srt, video=video, status=STATUS_SKIPPED,
                          detail=sync_skip_reason,
                          seconds=time.monotonic() - started,
                          original_sha=original_sha, new_sha=original_sha)

    if cfg.dry_run:
        return SyncResult(srt=srt, video=video, status=STATUS_PREVIEW,
                          detail="would run ffsubsync and replace the sidecar only on a trusted sync")

    staging = srt.with_name(f"{STAGING_PREFIX}{os.getpid()}.{uuid.uuid4().hex}.srt")
    command = build_ffsubsync_command(binary, video, srt, staging, features)
    try:
        rc, _stdout, stderr = run_ffsubsync(cfg, command)
    except subprocess.TimeoutExpired:
        _remove_staging(staging)
        return SyncResult(srt=srt, video=video, status=STATUS_FAILED,
                          detail=f"ffsubsync timed out after {cfg.timeout_seconds:.0f}s",
                          seconds=time.monotonic() - started,
                          error_tail=error_tail_from("timeout"))
    except OSError as exc:
        _remove_staging(staging)
        return SyncResult(srt=srt, video=video, status=STATUS_FAILED,
                          detail=f"could not run ffsubsync ({exc})",
                          seconds=time.monotonic() - started)

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
            _remove_staging(staging)
            return SyncResult(srt=srt, video=video, status=STATUS_FAILED,
                              detail=f"could not replace sidecar ({exc})",
                              seconds=time.monotonic() - started,
                              original_sha=original_sha, error_tail=error_tail)
        detail = (
            f"offset {parsed.offset_seconds:+.3f}s"
            + (f", framerate x{parsed.scale_factor:.3f}" if parsed.scale_factor is not None else "")
        )
        _mark_sidecar_synced(srt, original_sha, new_sha=new_sha)
        return SyncResult(srt=srt, video=video, status=status, detail=detail,
                          offset_seconds=parsed.offset_seconds,
                          scale_factor=parsed.scale_factor, score=parsed.score,
                          seconds=time.monotonic() - started,
                          original_sha=original_sha, new_sha=new_sha)

    _remove_staging(staging)
    if status == STATUS_IN_SYNC:
        # Measured and already aligned: these are the bytes that measured in
        # sync, so the sidecar is done. Held-for-review and failed syncs stay
        # unmarked - those still need another attempt.
        _mark_sidecar_synced(srt, original_sha)
    return SyncResult(srt=srt, video=video, status=status, detail=detail,
                      offset_seconds=parsed.offset_seconds,
                      scale_factor=parsed.scale_factor, score=parsed.score,
                      seconds=time.monotonic() - started,
                      original_sha=original_sha, error_tail=error_tail)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class Config:
    library: Path = field(default_factory=lambda: Path(DEFAULT_LIBRARY))
    log_file: Path = field(default_factory=lambda: Path(LOG_FILE))
    report_file: Path = field(default_factory=lambda: Path(REPORT_FILE))
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
        "Freshly extracted sidecars measured against their movie - the final content step, right before the library audit",
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

    report.scorecard([
        (len(synced), "Synced", "timing corrected, sidecar replaced atomically"),
        (len(in_sync), "In sync", "already aligned, file untouched, marked done"),
        (len(review), "Held for review", "untrustworthy sync, original kept, retried next run"),
        (len(failed), "Failed", "ffsubsync error, original kept, retried next run"),
        (len(skipped), "Skipped", "not extracted this era: existing sidecars are authoritative"),
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
                             "it. The original file is byte-identical and its provenance record stays "
                             "unmarked, so the next run tries again. There is no replacement download "
                             "any more: watch the movie, or place a correct .eng.srt yourself.")
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
                       intro="No sync was attempted, by rule: the sidecar was not extracted from its "
                             "movie's own embedded track (an existing .eng.srt is authoritative), it "
                             "was already synced on an earlier run, there is no matching movie file, "
                             "or it fails the shared subtitle contract. Only freshly extracted "
                             "sidecars are ever synced.")
        for res in skipped:
            report.entry(str(res.srt), detail=res.detail)

    if in_sync:
        report.section("ALREADY IN SYNC", count=len(in_sync),
                       intro="Measured drift below the threshold (and no framerate correction): the "
                             "original bytes were deliberately left untouched, and the sidecar is "
                             "marked done in the extraction ledger so it is never re-measured.")
        for res in in_sync:
            report.entry(str(res.srt), detail=res.detail, fields=[
                ("Offset", _fmt_offset(res.offset_seconds)),
                ("Took", f"{res.seconds:.1f}s"),
            ])

    if not results:
        report.section("NOTHING FOUND")
        report.paragraph("No .srt sidecars exist anywhere in the library - there is nothing to sync. "
                         "Run subtitle_extractor.py first to create the sidecars this tool aligns.")

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

    The extraction provenance ledger remains the authority for "may this
    sidecar be synced at all?" - it is written by subtitle_extractor.py and
    marked done by this tool, which is a stricter question than this cache
    asks. What goes here is the answer ``organize status`` displays, and
    losing it costs a line of a summary, nothing more.
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
        "Freshly extracted sidecars measured against their movie - the final content step, right before the library audit",
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
    log("")

    log.file = cfg.log_file
    # ffsubsync decodes a whole movie's audio and correlates it against the
    # subtitle: minutes per sidecar is normal, and between the "syncing" line
    # and its verdict the tool has nothing to say. On a terminal it now says
    # how far through the sweep it is on one rewritten line; off a terminal it
    # draws nothing, so a logged run is byte-for-byte what it was.
    live = log.attach_live()

    results: list[SyncResult] = []
    video_count = 0
    truncated = False
    started = time.monotonic()
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
                return sync_one(job, cfg, binary or "", features)

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
                live.progress(len(results) - len(skipped), len(jobs), label="syncing",
                              detail=result.srt.name, started=started)
    except LockTimeoutError as exc:
        log(str(exc), level="ERROR")
        return 2
    finally:
        live.clear()
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
        publish_state(results, cfg)

    review = sum(1 for r in results if r.status == STATUS_REVIEW)
    failed = sum(1 for r in results if r.status == STATUS_FAILED)
    synced = sum(1 for r in results if r.status == STATUS_SYNCED)
    in_sync = sum(1 for r in results if r.status == STATUS_IN_SYNC)
    log("")
    log("SYNC COMPLETE")
    log(f"  Synced (replaced)   : {synced}")
    log(f"  Already in sync     : {in_sync}")
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
            "Sync freshly extracted .srt sidecars with their movie using ffsubsync: "
            "trustworthy drift is applied atomically, zero drift leaves the file "
            "untouched, and untrustworthy drift is held for review. Sidecars that "
            "were not extracted from the movie's own embedded track are never touched."
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
                             "that `organize status` reads")
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
    except Exception as exc:  # noqa: BLE001 - last resort: whatever went wrong, this run
        # leaves through one exit code instead of an unhandled traceback.
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
