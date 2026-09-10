#!/usr/bin/env python3
"""
Embedded-Subtitle Extractor for Jellyfin Movies
===============================================
After ``movie_standardizer.py`` and before ``mkv_track_cleaner.py``: walk the
canonical movie library and create at most one validated external English SRT
sidecar per movie, built from the movie's own embedded subtitle track.

There is no downloading any more. This tool used to fall back to
OpenSubtitles, SubDL and seven scraping sources when a movie had no embedded
track; that machinery is gone because it was not reliable enough. The
contract is now exactly this:

* a movie that already has an ``.eng.srt`` beside it is left completely
  alone - no extraction, no sync, no provider anything. An existing sidecar
  is authoritative;
* a movie without one gets its embedded English subtitle track extracted
  into ``Title (Year).eng.srt``. Text tracks (SRT/SSA/ASS/WebVTT/USF) are
  converted in-process; image tracks (PGS/VobSub/DVB) are OCR'd by an
  external backend (pgsrip, sup2srt + Tesseract, Subtitle Edit, PgsToSrt)
  when one is installed. Forced/signs-only, commentary, non-English and
  too-short tracks are refused, so a movie is never left with a partial
  "subtitle";
* MP4 movies are read through a temporary subtitle-only MKV bridge, because
  mkvextract reads Matroska only. The bridge lives outside the library and
  is deleted with the run; ``mkv_track_cleaner.py`` later converts the
  container itself, so no embedded track is lost to the MP4 -> MKV
  conversion;
* every sidecar this tool writes is recorded in a provenance ledger outside
  the library. ``sync_subtitles.py`` reads that ledger and syncs exactly
  those freshly extracted sidecars - once - against the movie's real audio,
  then marks them done. Nothing else is ever synced;
* a movie with no usable embedded English track and no sidecar is listed in
  the report as needing attention. That is a human's decision now, not a
  download.

``mkv_track_cleaner.py`` runs after this tool and strips every embedded
subtitle (the extracted sidecar becomes the sole subtitle option), which is
why extraction must come first.

    py -3 subtitle_extractor.py --dry-run
    py -3 subtitle_extractor.py
    py -3 subtitle_extractor.py --self-test

The default policy intentionally writes UTF-8 SRT sidecars only. SRT is the
most broadly direct-play-safe external subtitle choice across Jellyfin
clients; ASS/SSA, VobSub, PGS, and other formats are never written here.

Standard library only. The external programs this tool drives are
``mkvmerge``/``mkvextract`` (MKVToolNix) and, for image tracks only, one OCR
backend.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html as _html
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Shared implementation: everything imported here is defined exactly once,
# in organizekit/core/. See tests/test_shared_core.py for the rule that
# keeps it that way.
from organizekit.core import (
    COVERING_ENGLISH_SRT_SUFFIXES,
    EXTERNAL_SRT_ENCODINGS,
    EXTERNAL_SRT_MAX_BYTES,
    EXTERNAL_SRT_SUFFIX,
    REPORT_WIDTH,
    CoordinationLock,
    JobOutcome,
    Report,
    RunLog,
    atomic_write_text,
    default_tool_dir,
    describe_workers,
    enable_utf8_stdio,
    exact_external_english_srt_path,
    map_ordered,
    normalize_srt_newlines,
    path_norm,
    print_text,
    promote_legacy_external_english_srt,
    resolve_library,
    resolve_workers,
    run_field_smoke_test,
    srt_looks_valid,
    tools_home,
    validate_srt_sidecar,
)

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


# The single agreed decode order. Every tool that turns subtitle bytes into
# text uses this tuple and nothing else, so a tool cannot quietly accept an
# encoding the others would reject. "utf-8-sig" first so a provider BOM does
# not make an otherwise valid file look binary; "cp1252" last because it
# decodes almost any byte sequence and would mask a genuine encoding problem.


def covering_english_srt_paths(media_path: Path) -> tuple[Path, ...]:
    """Return ``.eng.srt`` then ``.eng.sdh.srt`` beside a movie file."""
    return tuple(
        media_path.with_name(f"{media_path.stem}{suffix}")
        for suffix in COVERING_ENGLISH_SRT_SUFFIXES
    )


def is_covering_english_sidecar(path: Path, media_path: Path) -> bool:
    wanted = {candidate.name.casefold() for candidate in covering_english_srt_paths(media_path)}
    return path.name.casefold() in wanted


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

LIBRARY_DIR = str(resolve_library())
# Logs and reports live under tools\ReportsAndLogs so the library root stays
# media-only.
LOG_FILE = str(default_tool_dir("subtitle_extractor") / "subtitle_extractor.log")  # Appended every run.
REPORT_FILE = str(default_tool_dir("subtitle_extractor") / "subtitle_extractor_report.txt")

__version__ = "3.0.0"

# The preceding standardizer emits canonical movie folders. MKV is the
# canonical container; MP4 releases are accepted and read here (through a
# temporary subtitle-only MKV bridge), and mkv_track_cleaner.py converts them
# to MKV after extraction has had its chance.
VIDEO_EXTENSIONS = {".mkv", ".mp4"}
DIRECT_PLAY_SUBTITLE_EXTENSION = ".srt"
MIN_MOVIE_SIZE_MB = 300
MAX_SUBTITLE_BYTES = EXTERNAL_SRT_MAX_BYTES

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


# Every result carries a machine-readable reason alongside its human detail so
# the report groups movies by what the user has to *do*, instead of guessing
# that grouping back out of a prose sentence.
REASON_COVERED = "covered"
REASON_EXTRACTED = "extracted"
REASON_DRY_RUN = "dry_run"
REASON_NO_TRACK = "no_track"
REASON_SIDECAR_UNUSABLE = "sidecar_unusable"
REASON_SIDECAR_NAME = "sidecar_name"
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


# Single shared run log: one line per event, console and file together.
# A logging failure is never allowed to end a run.
log = RunLog()

def video_snapshot(path: Path) -> VideoSnapshot:
    """Capture a no-follow video identity before an external-provider transaction."""
    file_stat = path.stat(follow_symlinks=False)
    if path.is_symlink() or not path.is_file():
        raise OSError(f"not a regular non-symlink movie file: {path}")
    return VideoSnapshot(
        device=int(file_stat.st_dev), inode=int(file_stat.st_ino),
        size=int(file_stat.st_size), mtime_ns=int(file_stat.st_mtime_ns),
    )


def movie_key(video: Path, snapshot: VideoSnapshot) -> str:
    """One stable state key per movie, normalized the way every tool compares paths."""
    _ = snapshot
    return path_norm(video)

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
        return "noncanonical layout: movie file is directly under the library root"
    if parent.is_symlink() or video.is_symlink() or not video.is_file():
        return "noncanonical layout: movie is not a regular non-symlink file in a regular folder"
    if video.stem.casefold() != parent.name.casefold():
        return "noncanonical layout: movie stem does not match its movie-folder name"
    try:
        siblings = [
            item for item in parent.iterdir()
            if item.suffix.lower() in {".mkv", ".mp4"} and item.is_file() and not item.is_symlink()
        ]
    except OSError as exc:
        return f"noncanonical layout: could not inspect movie folder ({exc})"
    if len(siblings) != 1:
        return f"noncanonical layout: expected one regular movie file in movie folder, found {len(siblings)}"
    return None

def dest_for(video: Path, cfg: Any = None) -> Path:
    # Plex/Jellyfin: file next to the video, same stem + language suffix.
    # ``cfg`` is unused and kept only so older callers keep working.
    _ = cfg
    return exact_external_english_srt_path(video)




# =============================================================================
# EMBEDDED SUBTITLE EXTRACTION - the tool's whole job
#
# A Jellyfin movie very often already carries the English subtitle as an
# embedded track, and that track is exact for this release and cannot be the
# wrong cut. The sidecar is built from it:
#
#   * the cues carry the container's own timestamps, so the sidecar is
#     frame-accurate for this exact file. sync_subtitles.py still measures
#     each freshly extracted sidecar once (containers are not always honest)
#     and then marks it done in the provenance ledger;
#   * mkv_track_cleaner.py strips every embedded subtitle afterwards, so the
#     external sidecar becomes the sole subtitle option - which is why this
#     tool runs first;
#   * MKVToolNix does the reading: ``mkvmerge -J`` lists the tracks and
#     ``mkvextract`` writes one out. MP4 movies are read through a temporary
#     subtitle-only MKV bridge (mkvextract reads Matroska only), built
#     outside the library and deleted with the run.
#
# Text tracks (SRT/SSA/ASS/WebVTT/USF) are converted to SRT in-process with
# the standard library. Image tracks (PGS/SUP, VobSub, DVB) need OCR, which
# a stdlib-only script cannot vendor: they are handed to an external OCR
# backend (pgsrip, sup2srt + Tesseract, Subtitle Edit, or PgsToSrt) when one
# is installed, and are reported as needing attention when none is.
#
# Extraction never rewrites or deletes the movie: mkvextract and the bridge
# builder only read it, and every temporary file lives outside the library.
# =============================================================================

# Embedded subtitle codecs this tool can turn into an external SRT. The value
# is the extension mkvextract must write; PGS becomes a .sup stream, VobSub
# becomes the .idx/.sub pair Subtitle Edit reads.
EXTRACT_TEXT_CODECS: dict[str, str] = {
    "S_TEXT/UTF8": ".srt",
    "S_TEXT/ASCII": ".srt",
    "S_TEXT/SSA": ".ssa",
    "S_TEXT/ASS": ".ass",
    "S_TEXT/WEBVTT": ".vtt",
    "S_TEXT/USF": ".usf",
}

EXTRACT_IMAGE_CODECS: dict[str, str] = {
    "S_HDMV/PGS": ".sup",
    "S_VOBSUB": ".idx",
    "S_DVBSUB": ".sub",
}

# Preference inside each class: an already-tagged plain SRT needs no
# conversion; ASS/SSA keep styling that is dropped on the way to SRT; WebVTT
# and USF are rare and converted best-effort. PGS beats DVD-era VobSub.
TEXT_CODEC_RANK: dict[str, int] = {
    "S_TEXT/UTF8": 0,
    "S_TEXT/ASCII": 0,
    "S_TEXT/ASS": 1,
    "S_TEXT/SSA": 2,
    "S_TEXT/WEBVTT": 3,
    "S_TEXT/USF": 4,
}

IMAGE_CODEC_RANK: dict[str, int] = {
    "S_HDMV/PGS": 0,
    "S_VOBSUB": 1,
    "S_DVBSUB": 2,
}

USF_XML_TEXT_RE = re.compile(r"<text[^>]*>(.*?)</text>", re.IGNORECASE | re.DOTALL)
USF_TAG_RE = re.compile(r"<[^>]+>")

# A full movie track carries hundreds of cues. A handful means the track is
# signs/songs-only or a foreign-language-forced stream, and writing it as the
# movie's English subtitle would be a silent downgrade.
DEFAULT_EXTRACT_MIN_CUES = 10
# How many candidates to try per movie before giving up and letting the
# provider tiers run. Text extraction is cheap; OCR is not, so each class is
# capped separately and the whole run can cap OCR jobs (--ocr-limit).
DEFAULT_EXTRACT_TEXT_CANDIDATE_LIMIT = 3
DEFAULT_EXTRACT_IMAGE_CANDIDATE_LIMIT = 2
DEFAULT_MKVEXTRACT_TIMEOUT_SEC = 900.0
DEFAULT_OCR_TIMEOUT_SEC = 1_800.0

# Durable, outside-the-library record of which sidecars this tool created from
# the movie's own tracks. sync_subtitles.py reads it so it never spends an
# ffsubsync run "correcting" a subtitle that is frame-accurate by
# construction. It lives beside the other ReportsAndLogs artefacts.
EXTRACTED_LEDGER_NAME = "subtitle_extractor_extracted.json"
# Libraries cut over from the fetching era still hold provenance records
# written by subtitle_fetcher.py under this name; they are read (never
# written) until this tool has a ledger of its own.
LEGACY_EXTRACTED_LEDGER_NAME = "subtitle_fetcher_extracted.json"
EXTRACTED_LEDGER_ENV = "SUBTITLE_EXTRACTED_LEDGER"
EXTRACTED_LEDGER_VERSION = 1

OCR_BACKEND_AUTO = "auto"
OCR_BACKEND_NONE = "none"
OCR_BACKEND_CUSTOM = "custom"
OCR_BACKEND_PGSRIP = "pgsrip"
OCR_BACKEND_SUP2SRT = "sup2srt"
OCR_BACKEND_SUBTITLEEDIT = "subtitleedit"
OCR_BACKEND_PGSTOSRT = "pgstosrt"
OCR_BACKEND_CHOICES: tuple[str, ...] = (
    OCR_BACKEND_AUTO,
    OCR_BACKEND_PGSRIP,
    OCR_BACKEND_SUP2SRT,
    OCR_BACKEND_SUBTITLEEDIT,
    OCR_BACKEND_PGSTOSRT,
    OCR_BACKEND_CUSTOM,
    OCR_BACKEND_NONE,
)
# Auto-detection order. pgsrip comes first: it is the actively maintained
# option, it filters by language itself, and it reads both a .sup stream and
# an .mkv, so it needs the least help from us.
OCR_BACKEND_AUTO_ORDER: tuple[str, ...] = (
    OCR_BACKEND_PGSRIP,
    OCR_BACKEND_SUP2SRT,
    OCR_BACKEND_SUBTITLEEDIT,
    OCR_BACKEND_PGSTOSRT,
)

# Tesseract language codes are ISO 639-2/T (``eng``), while PgsToSrt is
# usually called with the same three-letter codes; Subtitle Edit and sup2srt
# accept either. Keep one place that normalizes what the tools are given.
OCR_TESSERACT_LANGUAGES: dict[str, str] = {"en": "eng", "eng": "eng", "english": "eng"}

# pgsrip parses languages with babelfish, which wants ISO 639-1/2B tags
# (``en``, ``pt-BR``); the container gives us ISO 639-2/T (``eng``).
OCR_PGSRIP_LANGUAGES: dict[str, str] = {"eng": "en", "en": "en", "english": "en"}

# Very common English function words. Their share of a real dialogue track is
# far above this floor; OCR noise and wrong-language tracks fall below it.
ENGLISH_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "is", "was", "are", "were", "be", "been", "it", "its", "you", "your",
    "i", "me", "my", "we", "us", "he", "she", "they", "them", "his", "her",
    "that", "this", "these", "those", "for", "with", "as", "so", "not", "no",
    "do", "did", "does", "have", "has", "had", "what", "when", "where", "who",
    "how", "why", "all", "just", "get", "got", "go", "going", "know", "think",
    "will", "can", "cant", "dont", "im", "thats", "there", "here", "up", "out",
})

# Characters that dominate when an OCR pass mis-reads a bitmap subtitle.
OCR_NOISE_CHARS = "|~^@#"


# ---------------------------------------------------------------------------
# External binaries (MKVToolNix)
# ---------------------------------------------------------------------------
_MKVTOOLNIX_PATHS: dict[str, tuple[str, ...]] = {
    "mkvmerge": (
        r"C:\Program Files\MKVToolNix\mkvmerge.exe",
        r"C:\Program Files (x86)\MKVToolNix\mkvmerge.exe",
        "/usr/bin/mkvmerge",
        "/usr/local/bin/mkvmerge",
        "/opt/homebrew/bin/mkvmerge",
    ),
    "mkvextract": (
        r"C:\Program Files\MKVToolNix\mkvextract.exe",
        r"C:\Program Files (x86)\MKVToolNix\mkvextract.exe",
        "/usr/bin/mkvextract",
        "/usr/local/bin/mkvextract",
        "/opt/homebrew/bin/mkvextract",
    ),
}

MKVTOOLNIX_INSTALL_HINT = (
    "install MKVToolNix (https://mkvtoolnix.download/) so mkvmerge and "
    "mkvextract are on the PATH"
)


def find_mkvtoolnix_binary(name: str, explicit: str | None = None) -> str | None:
    """Locate ``mkvmerge``/``mkvextract`` on the PATH or in a known install dir."""
    if explicit:
        explicit_path = Path(explicit)
        if explicit_path.is_file():
            return str(explicit_path)
        return shutil.which(explicit)
    found = shutil.which(name)
    if found:
        return found
    for install_path in _MKVTOOLNIX_PATHS.get(name, ()):
        if Path(install_path).is_file():
            return install_path
    return None


def _decode_stream(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def run_external_command(
    command: Sequence[str], *, timeout: float = 0.0
) -> tuple[int, str, str]:
    """Run one external binary, never raising on timeout or a missing program.

    Every binary this tool shells out to is optional. A missing program, a
    non-zero exit, or a timeout all come back as a plain ``(rc, out, err)`` so
    the caller can report the fix and fall through to the next strategy.
    """
    if not command:
        # Not reachable from this tool's call sites, but subprocess answers an
        # empty argument list with IndexError, and this function's contract is
        # that it never raises.
        return 127, "", "could not run command: no program was given"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [str(part) for part in command],
            capture_output=True,
            timeout=timeout if timeout and timeout > 0 else None,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, "", f"could not run {command[0]}: {exc}"
    return completed.returncode, _decode_stream(completed.stdout), _decode_stream(completed.stderr)


def _command_tail(text: str, max_lines: int = 3) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "no output"
    return " | ".join(lines[-max_lines:])


# ---------------------------------------------------------------------------
# Track discovery and classification
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EmbeddedSubtitleTrack:
    """One embedded subtitle stream worth trying to extract."""

    track_id: int
    codec_id: str
    language: str
    name: str
    kind: str  # "text" or "image"
    extension: str
    default: bool = False
    forced: bool = False
    sdh: bool = False
    rank: int = 0

    @property
    def label(self) -> str:
        parts = [f"track {self.track_id}", self.codec_id]
        if self.name:
            parts.append(self.name)
        if self.sdh:
            parts.append("SDH")
        return ", ".join(parts)


def _track_properties(track: dict[str, Any]) -> dict[str, Any]:
    props = track.get("properties")
    return props if isinstance(props, dict) else {}


def _track_flag(props: dict[str, Any], *names: str) -> bool:
    """Read a Matroska flag under either its modern or legacy JSON name."""
    for name in names:
        value = props.get(name)
        if isinstance(value, str):
            if value.strip().lower() in {"1", "true", "yes"}:
                return True
        elif value:
            return True
    return False


def subtitle_track_languages(track: dict[str, Any]) -> set[str]:
    props = _track_properties(track)
    codes = {
        str(props.get("language") or "").strip().lower(),
        str(props.get("language_ietf") or "").strip().lower(),
        str(props.get("tag_language") or "").strip().lower(),
    }
    return {re.split(r"[-_.]", code)[0] for code in codes if code}


def subtitle_track_is_english(track: dict[str, Any]) -> bool:
    """English either by tag or by an explicit English track name.

    A bare ``und`` stream is only English when its own name says so; the
    cleaner follows the identical rule, so the two tools cannot disagree about
    which stream is the movie's English subtitle.
    """
    languages = subtitle_track_languages(track)
    if languages & ENGLISH_LANGUAGE_TOKENS:
        return True
    if languages and not (languages <= {"und", ""}):
        return False
    return bool(re.search(r"\b(english|eng)\b", str(_track_properties(track).get("track_name") or "").lower()))


SUBTITLE_FORCED_NAME_RE = re.compile(r"\b(forced|foreign only|foreign parts only|signs?/?songs?)\b")
SUBTITLE_COMMENTARY_NAME_RE = re.compile(r"\b(commentary|riff|rifftrax)\b")


def subtitle_track_is_forced(track: dict[str, Any]) -> bool:
    """True for a signs/songs or foreign-parts-only track.

    Those tracks are deliberately incomplete: they carry only the lines a
    viewer cannot already understand. Publishing one as the movie's English
    subtitle would look like success while leaving the dialogue missing, so
    they never become a sidecar here.
    """
    props = _track_properties(track)
    if _track_flag(props, "flag_forced", "forced_track"):
        return True
    return bool(SUBTITLE_FORCED_NAME_RE.search(str(props.get("track_name") or "").lower()))


def subtitle_track_is_commentary(track: dict[str, Any]) -> bool:
    props = _track_properties(track)
    if _track_flag(props, "flag_commentary"):
        return True
    return bool(SUBTITLE_COMMENTARY_NAME_RE.search(str(props.get("track_name") or "").lower()))


def subtitle_track_is_sdh(track: dict[str, Any]) -> bool:
    props = _track_properties(track)
    if _track_flag(props, "flag_hearing_impaired"):
        return True
    return bool(re.search(r"\b(sdh|hearing[ -]impaired)\b", str(props.get("track_name") or "").lower()))


def classify_embedded_subtitle_tracks(
    tracks: Sequence[dict[str, Any]],
) -> list[EmbeddedSubtitleTrack]:
    """Pick the English subtitle streams worth extracting, best first.

    Non-English, forced, and commentary streams are dropped outright. Text
    streams outrank image streams (a conversion is free, OCR is minutes), and
    inside each class the container's default track wins before codec
    preference and track order.
    """
    candidates: list[EmbeddedSubtitleTrack] = []
    for track in tracks:
        if str(track.get("type") or "").strip().lower() != "subtitles":
            continue
        props = _track_properties(track)
        codec_id = str(props.get("codec_id") or "").strip().upper()
        if codec_id in EXTRACT_TEXT_CODECS:
            kind = "text"
            extension = EXTRACT_TEXT_CODECS[codec_id]
            rank = TEXT_CODEC_RANK.get(codec_id, 9)
        elif codec_id in EXTRACT_IMAGE_CODECS:
            kind = "image"
            extension = EXTRACT_IMAGE_CODECS[codec_id]
            rank = IMAGE_CODEC_RANK.get(codec_id, 9)
        else:
            continue
        if not subtitle_track_is_english(track):
            continue
        if subtitle_track_is_forced(track) or subtitle_track_is_commentary(track):
            continue
        try:
            track_id = int(track.get("id"))
        except (TypeError, ValueError):
            continue
        candidates.append(
            EmbeddedSubtitleTrack(
                track_id=track_id,
                codec_id=codec_id,
                language=str(props.get("language") or props.get("language_ietf") or "und"),
                name=str(props.get("track_name") or ""),
                kind=kind,
                extension=extension,
                default=_track_flag(props, "flag_default", "default_track"),
                sdh=subtitle_track_is_sdh(track),
                rank=rank,
            )
        )
    candidates.sort(key=lambda item: (0 if item.kind == "text" else 1, item.rank,
                                      0 if item.default else 1, item.track_id))
    return candidates


def probe_embedded_subtitle_tracks(
    video: Path, mkvmerge_bin: str, *, timeout: float = 300.0
) -> tuple[list[dict[str, Any]] | None, str]:
    """Return the movie's subtitle tracks, or ``(None, reason)`` it could not."""
    rc, out, err = run_external_command([mkvmerge_bin, "-J", str(video)], timeout=timeout)
    if rc != 0:
        return None, f"mkvmerge could not read the movie (exit {rc}): {_command_tail(err or out)}"
    try:
        payload = json.loads(out)
    except (ValueError, TypeError):
        return None, "mkvmerge produced unreadable track information"
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        return None, "mkvmerge reported no tracks"
    return [track for track in tracks if isinstance(track, dict)], ""


# ---------------------------------------------------------------------------
# SRT rendering, ASS/SSA and WebVTT conversion
# ---------------------------------------------------------------------------
SRT_TIMING_RE = re.compile(
    r"(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})"
)
# WebVTT timestamps may legally omit the hours: MM:SS.mmm as well as HH:MM:SS.mmm.
VTT_TIMING_RE = re.compile(
    r"((?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})"
)
ASS_TIMESTAMP_RE = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})[.,](\d{1,3})$")
ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


def parse_srt_cues(text: str) -> list[tuple[str, str, str]]:
    """Parse ``text`` into ``[(start, end, body), ...]`` with SRT timestamps.

    Used to renumber and re-render anything this tool writes, so an extracted
    track can never carry a broken cue index, a stray BOM, or CRLF endings.
    """
    cues: list[tuple[str, str, str]] = []
    for block in re.split(r"\n\s*\n", normalize_srt_newlines(text).strip()):
        lines = block.split("\n")
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        match = SRT_TIMING_RE.search(lines[timing_index])
        if not match:
            continue
        body = "\n".join(lines[timing_index + 1:]).strip()
        if not body:
            continue
        start = _pad_srt_timestamp(match.group(1))
        end = _pad_srt_timestamp(match.group(2))
        if start is None or end is None:
            continue
        cues.append((start, end, body))
    return cues


def _pad_srt_timestamp(value: str) -> str | None:
    value = value.strip().replace(".", ",")
    clock, _, millis = value.rpartition(",")
    if not clock:
        return None
    parts = clock.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    millis = (millis + "000")[:3]
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis}"


def render_srt_cues(cues: Sequence[tuple[str, str, str]]) -> str:
    """Render cues as a canonical, renumbered, UTF-8 SRT document."""
    chunks: list[str] = []
    for index, (start, end, body) in enumerate(cues, start=1):
        chunks.append(f"{index}\n{start} --> {end}\n{body}\n")
    return "\n".join(chunks)


def normalize_extracted_srt(text: str) -> str:
    """Re-render any SRT text into the canonical form this tool writes."""
    return render_srt_cues(parse_srt_cues(text))


def _ass_timestamp_to_srt(token: str) -> str | None:
    match = ASS_TIMESTAMP_RE.match(token.strip())
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d},{(fraction + '000')[:3]}"


def _ass_plain_text(raw: str) -> str:
    """Strip ASS/SSA styling so the cue survives the trip into an SRT file.

    Override blocks (``{\\i1}``) carry styling SRT cannot express; ``\\N`` and
    ``\\n`` are the format's line breaks and ``\\h`` its non-breaking space.
    Everything else is literal text a player is expected to show.
    """
    text = ASS_OVERRIDE_RE.sub("", raw)
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    cleaned_lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in cleaned_lines if line).strip()


def ass_to_srt(text: str) -> str:
    """Convert an ASS/SSA subtitle document to canonical SRT text.

    Only ``Dialogue`` lines become cues; ``Comment`` lines are the format's
    non-displaying notes and are dropped. The event column order differs
    between SSA (v4) and ASS (v4+), so the ``Format:`` line is what selects
    the Start/End/Text columns rather than a fixed index.
    """
    body = normalize_srt_newlines(text)
    section = ""
    columns: list[str] = []
    parsed: list[tuple[str, str, str]] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            continue
        if section != "events":
            continue
        lowered = stripped.lower()
        if lowered.startswith("format:"):
            columns = [part.strip().lower() for part in stripped[len("format:"):].split(",")]
            continue
        if lowered.startswith("dialogue:"):
            payload = stripped[len("dialogue:"):]
        elif lowered.startswith("comment:"):
            continue
        else:
            continue
        if not columns:
            continue
        parts = payload.split(",", len(columns) - 1)
        if len(parts) != len(columns):
            continue
        row = dict(zip(columns, parts, strict=True))
        start = _ass_timestamp_to_srt(row.get("start", ""))
        end = _ass_timestamp_to_srt(row.get("end", ""))
        if not start or not end:
            continue
        cue = _ass_plain_text(row.get("text", ""))
        if not cue:
            continue
        parsed.append((start, end, cue))
    parsed.sort(key=lambda cue: cue[0])
    return render_srt_cues(parsed)


def _vtt_timestamp_to_srt(value: str) -> str | None:
    """WebVTT allows ``MM:SS.mmm`` without hours; SRT needs ``HH:MM:SS,mmm``."""
    token = value.strip()
    if token.count(":") == 1:
        token = f"00:{token}"
    return _pad_srt_timestamp(token)


def vtt_to_srt(text: str) -> str:
    """Convert a WebVTT document to canonical SRT text (best effort)."""
    body = normalize_srt_newlines(text)
    if body.startswith("\ufeff"):
        body = body[1:]
    parsed: list[tuple[str, str, str]] = []
    for block in re.split(r"\n\s*\n", body.strip()):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines or lines[0].strip().upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timing_line = lines[timing_index]
        # WebVTT allows cue settings after the end timestamp; the regex takes
        # the two timestamps and ignores the rest.
        match = VTT_TIMING_RE.search(timing_line)
        if not match:
            continue
        start = _vtt_timestamp_to_srt(match.group(1))
        end = _vtt_timestamp_to_srt(match.group(2))
        if not start or not end:
            continue
        body_text = "\n".join(
            USF_TAG_RE.sub("", line) for line in lines[timing_index + 1:]
        ).strip()
        if not body_text:
            continue
        parsed.append((start, end, body_text))
    return render_srt_cues(parsed)


def usf_to_srt(text: str) -> str:
    """Convert the rare USF (XML) subtitle track to SRT text, best effort."""
    parsed: list[tuple[str, str, str]] = []
    for match in re.finditer(
        r"<subtitle[^>]*start=\"(?P<start>[^\"]+)\"[^>]*end=\"(?P<end>[^\"]+)\"[^>]*>(?P<body>.*?)</subtitle>",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        start = _pad_srt_timestamp(match.group("start").replace(".", ","))
        end = _pad_srt_timestamp(match.group("end").replace(".", ","))
        if not start or not end:
            continue
        fragments = USF_XML_TEXT_RE.findall(match.group("body"))
        cue = "\n".join(
            _html.unescape(USF_TAG_RE.sub("", fragment)).strip() for fragment in fragments
        ).strip()
        if not cue:
            continue
        parsed.append((start, end, cue))
    return render_srt_cues(parsed)


# ---------------------------------------------------------------------------
# Quality gate: is the extracted text a real English subtitle?
# ---------------------------------------------------------------------------
def non_latin_ratio(text: str) -> float:
    """Share of alphabetic characters outside the Latin blocks.

    A Cyrillic, Greek, CJK, or Arabic embedded track is not an English
    subtitle however good the extraction was.
    """
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    non_latin = sum(1 for char in letters if ord(char) > 0x024F)
    return non_latin / len(letters)


def extracted_subtitle_quality(
    text: str, *, min_cues: int = DEFAULT_EXTRACT_MIN_CUES, method: str = "text"
) -> tuple[bool, str]:
    """Decide whether extracted text may become the movie's English sidecar.

    A subtitle taken from the movie's own track is authoritative about timing
    but not about content: a mis-tagged foreign track or a failed OCR pass
    would both produce a file that looks like success. Every extracted track
    therefore passes the same conservative gate a download passes, plus two
    checks that only matter for extraction (cue count and OCR noise).
    """
    if not text.strip():
        return False, "the extracted track contained no subtitle text"
    if len(text.encode("utf-8", errors="replace")) > MAX_SUBTITLE_BYTES:
        return False, f"the extracted subtitle exceeds the {MAX_SUBTITLE_BYTES // (1024 * 1024)} MiB safety limit"
    if not looks_like_srt(text):
        return False, "the extracted track did not convert to valid SRT cues"
    cues = parse_srt_cues(text)
    if len(cues) < min_cues:
        return (
            False,
            f"only {len(cues)} cue(s) extracted; a complete movie track needs "
            f"at least {min_cues} (this track is probably signs/songs-only)",
        )
    sample = " ".join(cue[2] for cue in cues)
    if non_latin_ratio(sample) > 0.40:
        return False, "the extracted text is not Latin-script (this track is not English)"
    words = re.findall(r"[A-Za-z']+", sample)
    if len(words) >= 100:
        hits = sum(1 for word in words if word.lower() in ENGLISH_STOPWORDS)
        if hits / len(words) < 0.04:
            return False, "the extracted text does not read as English (OCR noise or a foreign track)"
    if method == "ocr":
        noise = sum(sample.count(char) for char in OCR_NOISE_CHARS)
        if noise and noise / max(1, len(sample)) > 0.02:
            return False, "the OCR output looks like noise rather than dialogue"
    return True, ""


# ---------------------------------------------------------------------------
# OCR backends for image-based subtitles (PGS/SUP, VobSub, DVB)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OcrBackend:
    """An external program that turns a bitmap subtitle stream into text."""

    key: str
    label: str
    program: tuple[str, ...]
    supports: frozenset[str]
    # "output": writes the path we pass. "sibling": writes next to the input.
    output_mode: str = "output"
    arg_template: tuple[str, ...] = ()

    def build_command(self, source: Path, output: Path, *, track_id: int, language: str) -> list[str]:
        if self.key == OCR_BACKEND_CUSTOM:
            mapping = {
                "{input}": str(source),
                "{output}": str(output),
                "{track}": str(track_id),
                "{lang}": OCR_TESSERACT_LANGUAGES.get(language.lower(), language),
            }
            return [*self.program, *(mapping.get(token, token) for token in self.arg_template)]
        if self.key == OCR_BACKEND_PGSRIP:
            # pgsrip writes its .srt beside the input; there is no output flag.
            return [
                *self.program,
                "-l", OCR_PGSRIP_LANGUAGES.get(language.lower(), language),
                str(source),
            ]
        if self.key == OCR_BACKEND_SUP2SRT:
            return [
                *self.program,
                "-l", OCR_TESSERACT_LANGUAGES.get(language.lower(), language),
                "-o", str(output),
                str(source),
            ]
        if self.key == OCR_BACKEND_SUBTITLEEDIT:
            # Subtitle Edit OCRs an image-based input and writes <input>.srt
            # beside it; there is no output-path flag in its CLI.
            return [*self.program, "/convert", str(source), "srt", "/encoding:utf-8"]
        if self.key == OCR_BACKEND_PGSTOSRT:
            return [
                *self.program,
                "--input", str(source),
                "--output", str(output),
                "--tesseractlanguage", OCR_TESSERACT_LANGUAGES.get(language.lower(), language),
            ]
        raise ValueError(f"unknown OCR backend: {self.key}")

    def result_path(self, source: Path, output: Path) -> Path:
        if self.output_mode == "sibling":
            return source.with_suffix(".srt")
        return output

    def supports_track(self, track: EmbeddedSubtitleTrack) -> bool:
        family = {
            "S_HDMV/PGS": "PGS",
            "S_VOBSUB": "VOBSUB",
            "S_DVBSUB": "DVBSUB",
        }.get(track.codec_id.upper(), "PGS")
        return family in self.supports


def _resolve_program(explicit: str, name: str, *search_paths: str) -> str | None:
    if explicit:
        explicit_path = Path(explicit)
        if explicit_path.is_file():
            return str(explicit_path)
        found = shutil.which(explicit)
        if found:
            return found
    found = shutil.which(name)
    if found:
        return found
    for search_path in search_paths:
        if Path(search_path).is_file():
            return search_path
    return None


def _subtitleedit_program(explicit: str = "") -> tuple[str, ...] | None:
    """Subtitle Edit is a Windows GUI app; ``mono`` runs it elsewhere.

    The mono wrapper has to be decided *after* the program is found, not as a
    fallback for not finding it: the lookup already knows the Linux install
    locations, so it always resolved the ``.exe`` first and the fallback was
    unreachable, leaving a Linux install to be exec'd as a bare .NET binary.
    """
    known = (
        r"C:\Program Files\Subtitle Edit\SubtitleEdit.exe",
        r"C:\Program Files (x86)\Subtitle Edit\SubtitleEdit.exe",
        "/usr/lib/subtitleedit/SubtitleEdit.exe",
        "/opt/subtitleedit/SubtitleEdit.exe",
    )
    program = _resolve_program(explicit, "SubtitleEdit", *known)
    if not program:
        return None
    if os.name == "nt" or not program.lower().endswith(".exe"):
        return (program,)
    mono = shutil.which("mono")
    # No mono means this install cannot be run here at all, which is a backend
    # that was not found rather than one that fails minutes into a movie.
    return (mono, program) if mono else None


def _pgstosrt_program(explicit: str = "") -> tuple[str, ...] | None:
    """PgsToSrt ships as a .NET dll, so it needs dotnet plus a dll path."""
    dll = explicit or os.environ.get("PGSTOSRT_DLL", "").strip()
    if not dll or not Path(dll).is_file():
        return None
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return None
    return (dotnet, dll)


OCR_BACKEND_BUILDERS: dict[str, Callable[[str], OcrBackend | None]] = {}


def build_ocr_backend(key: str, explicit_bin: str = "") -> OcrBackend | None:
    if key == OCR_BACKEND_PGSRIP:
        program = _resolve_program(explicit_bin, "pgsrip")
        if not program:
            return None
        # pgsrip reads a .sup stream or an .mkv/.mks and OCRs the PGS tracks
        # of the languages named with -l; everything else is filtered out.
        return OcrBackend(key, "pgsrip + Tesseract", (program,), frozenset({"PGS"}),
                          output_mode="sibling")
    if key == OCR_BACKEND_SUP2SRT:
        program = _resolve_program(explicit_bin, "sup2srt")
        if not program:
            return None
        return OcrBackend(key, "sup2srt + Tesseract", (program,), frozenset({"PGS"}))
    if key == OCR_BACKEND_SUBTITLEEDIT:
        se_program = _subtitleedit_program(explicit_bin)
        if not se_program:
            return None
        # Subtitle Edit reads both PGS (.sup) and VobSub (.idx/.sub) inputs.
        return OcrBackend(key, "Subtitle Edit", se_program,
                          frozenset({"PGS", "VOBSUB", "DVBSUB"}), output_mode="sibling")
    if key == OCR_BACKEND_PGSTOSRT:
        pgstosrt_program = _pgstosrt_program(explicit_bin)
        if not pgstosrt_program:
            return None
        return OcrBackend(key, "PgsToSrt", pgstosrt_program, frozenset({"PGS"}))
    return None


OCR_INSTALL_HINT = (
    "install one image-subtitle OCR backend to extract PGS/VobSub tracks: "
    "pgsrip (pip install pgsrip, needs MKVToolNix + tesseract + tessdata), "
    "sup2srt + Tesseract (https://github.com/retrontology/sup2srt), Subtitle Edit "
    "(https://www.nikse.dk/subtitleedit), or PgsToSrt with PGSTOSRT_DLL set; "
    "text subtitle tracks are extracted without any of them"
)


def detect_ocr_backend(
    preferred: str = OCR_BACKEND_AUTO, *, explicit_bin: str = "", arg_template: str = ""
) -> tuple[OcrBackend | None, str]:
    """Return the OCR backend to use and a note saying what was (not) found.

    ``auto`` tries sup2srt, then Subtitle Edit, then PgsToSrt. Nothing here is
    fatal: an image-only movie simply falls through to the provider tiers, and
    the note is what the report and the log show as the reason.
    """
    if preferred == OCR_BACKEND_NONE:
        return None, "image-subtitle OCR is disabled (--ocr-backend none)"
    if preferred == OCR_BACKEND_CUSTOM or (arg_template.strip() and explicit_bin.strip()):
        program = _resolve_program(explicit_bin, "")
        if not program:
            return None, f"--ocr-backend custom needs --ocr-bin (not found: {explicit_bin or '(unset)'})"
        try:
            tokens = tuple(shlex.split(arg_template))
        except ValueError as exc:
            return None, f"--ocr-args could not be parsed ({exc})"
        # Both, not either: without {output} the tool has no idea where the
        # OCR result landed, and the run would fail a movie at a time.
        if any(not any(name in token for token in tokens)
               for name in ("{input}", "{output}")):
            return None, "--ocr-args must name both {input} and {output}"
        return OcrBackend(OCR_BACKEND_CUSTOM, "custom OCR command", (program,),
                          frozenset({"PGS", "VOBSUB", "DVBSUB"}), arg_template=tokens), ""
    if preferred in {OCR_BACKEND_AUTO, ""}:
        order = OCR_BACKEND_AUTO_ORDER
    elif preferred in OCR_BACKEND_CHOICES:
        order = (preferred,)
    else:
        return None, f"unknown --ocr-backend '{preferred}'"
    tried: list[str] = []
    for key in order:
        backend = build_ocr_backend(key, explicit_bin if preferred != OCR_BACKEND_AUTO else "")
        if backend is not None:
            return backend, ""
        tried.append(key)
    if preferred == OCR_BACKEND_AUTO:
        return None, f"no image-subtitle OCR backend found; {OCR_INSTALL_HINT}"
    return None, f"--ocr-backend {preferred} was not found; {OCR_INSTALL_HINT}"


def find_sibling_srt(source: Path, expected: Path) -> Path | None:
    """Locate the .srt a "writes beside its input" backend produced.

    Subtitle Edit and pgsrip both choose the output name themselves, and the
    rule differs by version (``movie.sup`` -> ``movie.srt`` vs ``movie.srt``
    vs a language-tagged name). Accept the documented name first, then fall
    back to the newest .srt that appeared next to the input, so a renamer
    change costs nothing here.
    """
    if expected.is_file() and expected.stat().st_size > 0:
        return expected
    try:
        siblings = sorted(
            (path for path in source.parent.glob("*.srt")
             if path.is_file() and path.stat().st_size > 0),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return None
    return siblings[0] if siblings else None


def run_ocr(
    backend: OcrBackend,
    source: Path,
    output: Path,
    *,
    track_id: int = 0,
    language: str = "eng",
    timeout: float = DEFAULT_OCR_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """OCR one extracted bitmap subtitle stream into ``output``."""
    command = backend.build_command(source, output, track_id=track_id, language=language)
    rc, out, err = run_external_command(command, timeout=timeout)
    produced: Path | None = (
        find_sibling_srt(source, backend.result_path(source, output))
        if backend.output_mode == "sibling" else backend.result_path(source, output)
    )
    if rc == 0 and produced is not None and produced.is_file() and produced.stat().st_size > 0:
        if produced != output:
            try:
                shutil.move(str(produced), str(output))
            except OSError as exc:
                return False, f"could not collect the OCR output ({exc})"
        return True, ""
    detail = _command_tail(err or out)
    return False, f"{backend.label} could not OCR this track (exit {rc}): {detail}"


# ---------------------------------------------------------------------------
# Durable record of extracted sidecars (read by sync_subtitles.py)
# ---------------------------------------------------------------------------
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def extracted_ledger_path() -> Path:
    """Where the extraction record lives: outside the library, every time.

    It sits beside the other ReportsAndLogs artefacts next to this script, so
    every tool in the chain finds the same file regardless of which ``--log``
    path it was given. Override with ``SUBTITLE_EXTRACTED_LEDGER``.
    """
    override = os.environ.get(EXTRACTED_LEDGER_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return tools_home() / "ReportsAndLogs" / EXTRACTED_LEDGER_NAME


# Extraction and syncing both run worker threads, and every one of them
# load-mutate-writes this one JSON file. The lock serialises the write path
# only (reads stay lock-free): a lost update would cost a redundant sync, but
# the ledger is supposed to be the durable answer to "was this sidecar
# extracted?", so it must not lose records to interleaving.
_LEDGER_WRITE_LOCK = threading.Lock()


def load_extracted_ledger(path: Path | None = None) -> dict[str, Any]:
    """Read the extraction record; a missing or damaged file is an empty one.

    When the current ledger does not exist yet, the legacy
    ``subtitle_fetcher_extracted.json`` is read instead: those provenance
    records are still true, and honoring them means a library cut over from
    the fetching era does not have its already-extracted sidecars re-synced
    from scratch.
    """
    target = path or extracted_ledger_path()
    payload: Any = None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    if payload is None and path is None and not os.environ.get(EXTRACTED_LEDGER_ENV, "").strip():
        try:
            payload = json.loads(
                (target.parent / LEGACY_EXTRACTED_LEDGER_NAME).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("sidecars"), dict):
        return {"version": EXTRACTED_LEDGER_VERSION, "sidecars": {}}
    return payload


def record_extracted_sidecar(
    video: Path,
    sidecar: Path,
    *,
    track: EmbeddedSubtitleTrack,
    method: str,
    cue_count: int,
    sha256: str,
    ocr_backend: str = "",
    path: Path | None = None,
) -> bool:
    """Remember that ``sidecar`` came from the movie's own embedded track.

    sync_subtitles.py reads this record to know which sidecars it may sync:
    exactly the ones extracted from the movie, and only until they are marked
    synced. Best effort by design: a read-only installation loses only that
    handshake, never the subtitle itself.
    """
    target = path or extracted_ledger_path()
    with _LEDGER_WRITE_LOCK:
        payload = load_extracted_ledger(target)
        payload["version"] = EXTRACTED_LEDGER_VERSION
        try:
            stat_result = video.stat(follow_symlinks=False)
            movie_size, movie_mtime = int(stat_result.st_size), int(stat_result.st_mtime_ns)
        except OSError:
            movie_size, movie_mtime = 0, 0
        payload["sidecars"][path_norm(sidecar)] = {
            "movie": str(video),
            "sidecar": str(sidecar),
            "movie_size": movie_size,
            "movie_mtime_ns": movie_mtime,
            "sha256": sha256,
            "track_id": track.track_id,
            "codec_id": track.codec_id,
            "track_name": track.name,
            "language": track.language,
            "method": method,
            "ocr_backend": ocr_backend,
            "cue_count": cue_count,
            "extracted_utc": utc_timestamp(),
        }
        try:
            atomic_write_json(target, payload)
        except OSError:
            return False
        return True


def find_extracted_record(
    sidecar: Path, sha256: str | None = None, *, path: Path | None = None
) -> dict[str, Any] | None:
    """The extraction record for ``sidecar``, if the file is still the original.

    ``sha256`` is compared when given: a sidecar whose bytes were replaced by
    a hand edit or an outside tool is no longer the extracted copy, so it has
    no provenance and the sync step leaves it untouched.
    """
    payload = load_extracted_ledger(path)
    record = payload.get("sidecars", {}).get(path_norm(sidecar))
    if not isinstance(record, dict):
        return None
    if sha256 and str(record.get("sha256") or "") != sha256:
        return None
    return record


# ---------------------------------------------------------------------------
# One movie, end to end
# ---------------------------------------------------------------------------
@dataclass
class ExtractOptions:
    """Knobs for one extraction attempt (mirrors the fetcher's CLI flags)."""

    enabled: bool = True
    mkvmerge_bin: str | None = None
    mkvextract_bin: str | None = None
    ocr_backend: str = OCR_BACKEND_AUTO
    ocr_bin: str = ""
    ocr_args: str = ""
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SEC
    ocr_allowed: bool = True
    extract_timeout_seconds: float = DEFAULT_MKVEXTRACT_TIMEOUT_SEC
    min_cues: int = DEFAULT_EXTRACT_MIN_CUES
    text_candidate_limit: int = DEFAULT_EXTRACT_TEXT_CANDIDATE_LIMIT
    image_candidate_limit: int = DEFAULT_EXTRACT_IMAGE_CANDIDATE_LIMIT
    dry_run: bool = False

    def resolved_backend(self) -> tuple[OcrBackend | None, str]:
        return detect_ocr_backend(self.ocr_backend, explicit_bin=self.ocr_bin,
                                  arg_template=self.ocr_args)


@dataclass
class ExtractionOutcome:
    """What one extraction attempt produced, and why if it produced nothing."""

    ok: bool = False
    detail: str = ""
    unavailable_reason: str = ""
    track: EmbeddedSubtitleTrack | None = None
    method: str = ""  # "text" or "ocr"
    ocr_backend: str = ""
    cue_count: int = 0
    dest: Path | None = None
    text: str = ""
    attempted: int = 0
    rejected: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        """True when extraction was possible at all for this movie."""
        return not self.unavailable_reason


def extract_embedded_english_srt(
    video: Path,
    dest: Path,
    options: ExtractOptions | None = None,
    *,
    log_file: Path | None = None,
) -> ExtractionOutcome:
    """Create ``dest`` from the movie's own English subtitle track.

    Returns an :class:`ExtractionOutcome`. ``ok`` means ``dest`` holds a
    validated external English SRT (or, in a dry run, that it would).
    ``unavailable_reason`` means extraction could not even be attempted here
    (no MKVToolNix, no usable English track, no OCR backend for an
    image-only movie, or an unreadable container) and names the fix.
    """
    opts = options or ExtractOptions()
    if not opts.enabled:
        return ExtractionOutcome(unavailable_reason="embedded extraction is disabled")
    if dest.exists():
        # Another actor (a manual copy, a concurrent run) already covered it.
        return ExtractionOutcome(unavailable_reason=f"{dest.name} already exists")

    mkvmerge_bin = find_mkvtoolnix_binary("mkvmerge", opts.mkvmerge_bin)
    mkvextract_bin = find_mkvtoolnix_binary("mkvextract", opts.mkvextract_bin)
    if not mkvmerge_bin or not mkvextract_bin:
        return ExtractionOutcome(
            unavailable_reason=f"MKVToolNix is not installed; {MKVTOOLNIX_INSTALL_HINT}"
        )

    with tempfile.TemporaryDirectory(prefix="subtitle_extract_") as tmpdir:
        tmp = Path(tmpdir)
        # mkvextract reads Matroska only, and the codec IDs a non-Matroska
        # container reports are not the IDs extraction would see. When the
        # movie itself is not an MKV but carries English subtitle tracks, its
        # subtitle tracks are first remuxed into a tiny bridge MKV here (in
        # temp space, outside the library) and everything downstream reads
        # the bridge instead. A dry run builds it too: it is the only way to
        # know which tracks a conversion would carry, and it writes nothing
        # inside the library.
        source = video
        tracks, probe_error = probe_embedded_subtitle_tracks(
            video, mkvmerge_bin, timeout=opts.extract_timeout_seconds
        )
        if tracks is None:
            return ExtractionOutcome(
                unavailable_reason=f"could not read the movie's tracks: {probe_error}"
            )
        if video.suffix.lower() != ".mkv" and any(
            subtitle_track_is_english(track)
            for track in tracks
            if str(track.get("type") or "") == "subtitles"
        ):
            source, bridge_error = _extraction_source(video, mkvmerge_bin, tmp, opts)
            if source is None:
                return ExtractionOutcome(unavailable_reason=bridge_error)
            tracks, probe_error = probe_embedded_subtitle_tracks(
                source, mkvmerge_bin, timeout=opts.extract_timeout_seconds
            )
            if tracks is None:
                return ExtractionOutcome(
                    unavailable_reason=f"could not read the movie's tracks: {probe_error}"
                )
        candidates = classify_embedded_subtitle_tracks(tracks)
        if not candidates:
            has_any_english = any(
                subtitle_track_is_english(track)
                for track in tracks
                if str(track.get("type") or "") == "subtitles"
            )
            reason = (
                "no complete English subtitle track (only forced/signs-only or commentary streams)"
                if has_any_english
                else "the movie has no English subtitle track"
            )
            return ExtractionOutcome(unavailable_reason=reason)

        backend, backend_note = opts.resolved_backend() if opts.ocr_allowed else (None, "")
        if not opts.ocr_allowed and any(item.kind == "image" for item in candidates):
            backend_note = f"the per-run OCR limit was reached; {OCR_INSTALL_HINT}"

        attempts: list[str] = []
        attempted = 0
        text_candidates = [item for item in candidates if item.kind == "text"][: max(0, opts.text_candidate_limit)]
        image_candidates = [item for item in candidates if item.kind == "image"][: max(0, opts.image_candidate_limit)]

        for track in text_candidates:
            attempted += 1
            outcome = _extract_one_track(
                video, source, dest, track, tmp, opts, backend=None, log_file=log_file
            )
            if outcome.ok:
                return ExtractionOutcome(
                    ok=True,
                    detail=(f"would extract the embedded {track.label} -> {dest.name}"
                            if opts.dry_run else outcome.detail),
                    track=outcome.track,
                    method=outcome.method,
                    ocr_backend=outcome.ocr_backend,
                    cue_count=outcome.cue_count,
                    dest=dest,
                    text=outcome.text,
                    attempted=attempted,
                )
            attempts.append(f"{track.label}: {outcome.detail}")
        for track in image_candidates:
            if backend is None:
                attempts.append(f"{track.label}: {backend_note or 'no OCR backend available'}")
                continue
            if not backend.supports_track(track):
                attempts.append(f"{track.label}: {backend.label} cannot OCR {track.codec_id}")
                continue
            if opts.dry_run:
                attempted += 1
                # OCR takes minutes; a preview must not spend them.
                return ExtractionOutcome(
                    ok=True,
                    method="ocr",
                    track=track,
                    ocr_backend=backend.label,
                    dest=dest,
                    attempted=attempted,
                    detail=(f"would OCR the embedded {track.label} with {backend.label} "
                            f"-> {dest.name}"),
                )
            attempted += 1
            outcome = _extract_one_track(
                video, source, dest, track, tmp, opts, backend=backend, log_file=log_file
            )
            if outcome.ok:
                return ExtractionOutcome(
                    ok=True,
                    detail=outcome.detail,
                    track=outcome.track,
                    method=outcome.method,
                    ocr_backend=outcome.ocr_backend,
                    cue_count=outcome.cue_count,
                    dest=outcome.dest,
                    text=outcome.text,
                    attempted=attempted,
                )
            attempts.append(f"{track.label}: {outcome.detail}")

    detail = "; ".join(attempts) if attempts else "no embedded English track could be converted"
    return ExtractionOutcome(
        ok=False,
        detail=detail,
        attempted=attempted,
        rejected=tuple(attempts),
        unavailable_reason="" if attempted else (backend_note or detail),
    )


def _extraction_source(
    video: Path, mkvmerge_bin: str, tmp: Path, opts: ExtractOptions
) -> tuple[Path | None, str]:
    """Build the MKV bridge a non-Matroska movie is extracted from.

    ``-D`` drops the video and ``-A`` the audio, so the bridge holds only the
    subtitle tracks (plus nothing else worth keeping): kilobytes to a few
    megabytes, not a second copy of the movie. It lives in ``tmp`` - outside
    the library - and is deleted with it.
    """
    bridge = tmp / f"bridge_{video.stem}.mkv"
    rc, _out, err = run_external_command(
        [mkvmerge_bin, "-o", str(bridge), "-D", "-A",
         "--no-chapters", "--no-attachments", str(video)],
        timeout=opts.extract_timeout_seconds,
    )
    if rc not in (0, 1) or not bridge.is_file():
        detail = _command_tail(err or _out)
        return None, f"mkvmerge could not read '{video.name}' for extraction (exit {rc}): {detail}"
    return bridge, ""


def _extract_one_track(
    video: Path,
    source: Path,
    dest: Path,
    track: EmbeddedSubtitleTrack,
    tmp: Path,
    opts: ExtractOptions,
    *,
    backend: OcrBackend | None,
    log_file: Path | None = None,
) -> ExtractionOutcome:
    """Extract one track, convert it, validate it, and publish it as ``dest``.

    ``video`` is the movie the sidecar belongs to (recorded in the provenance
    ledger); ``source`` is the file mkvextract reads - the movie itself, or
    the temporary MKV bridge built for a non-Matroska container.
    """
    mkvextract_bin = find_mkvtoolnix_binary("mkvextract", opts.mkvextract_bin)
    if not mkvextract_bin:
        return ExtractionOutcome(detail="mkvextract is not installed")
    staged = tmp / f"track{track.track_id}{track.extension}"
    rc, _out, err = run_external_command(
        [mkvextract_bin, "tracks", str(source), f"{track.track_id}:{staged}"],
        timeout=opts.extract_timeout_seconds,
    )
    if rc != 0:
        return ExtractionOutcome(detail=f"mkvextract failed (exit {rc}): {_command_tail(err)}")

    method = "text" if track.kind == "text" else "ocr"
    ocr_label = backend.label if backend is not None else ""
    produced_text = ""
    if track.kind == "text":
        try:
            raw = staged.read_bytes()
        except OSError as exc:
            return ExtractionOutcome(detail=f"could not read the extracted track ({exc})")
        try:
            decoded = decode_subtitle_bytes(raw)
        except (ValueError, OSError) as exc:
            return ExtractionOutcome(detail=f"the extracted track is not readable text ({exc})")
        decoded = normalize_srt_newlines(decoded)
        if decoded.startswith("\ufeff"):
            decoded = decoded[1:]
        if track.extension in {".ass", ".ssa"}:
            produced_text = ass_to_srt(decoded)
        elif track.extension == ".vtt":
            produced_text = vtt_to_srt(decoded)
        elif track.extension == ".usf":
            produced_text = usf_to_srt(decoded)
        else:
            produced_text = normalize_extracted_srt(decoded)
        if not produced_text.strip():
            return ExtractionOutcome(detail="the track converted to no subtitle cues")
    else:
        if backend is None:
            return ExtractionOutcome(detail="no OCR backend is available for this image track")
        ocr_output = tmp / f"track{track.track_id}.ocr.srt"
        ok, ocr_error = run_ocr(
            backend,
            staged,
            ocr_output,
            track_id=track.track_id,
            language=track.language or "eng",
            timeout=opts.ocr_timeout_seconds,
        )
        if not ok:
            return ExtractionOutcome(detail=ocr_error)
        try:
            produced_text = normalize_extracted_srt(
                normalize_srt_newlines(ocr_output.read_text(encoding="utf-8", errors="replace"))
            )
        except OSError as exc:
            return ExtractionOutcome(detail=f"could not read the OCR output ({exc})")

    good, reason = extracted_subtitle_quality(produced_text, min_cues=opts.min_cues, method=method)
    if not good:
        return ExtractionOutcome(detail=reason, track=track, method=method)

    cue_count = len(parse_srt_cues(produced_text))
    if opts.dry_run:
        return ExtractionOutcome(
            ok=True,
            detail=f"embedded {track.label} converts to {cue_count} cues",
            track=track,
            method=method,
            ocr_backend=ocr_label,
            cue_count=cue_count,
            dest=dest,
            text=produced_text,
        )

    try:
        # Create-only, exactly like a downloaded sidecar: a subtitle that
        # appears while this movie is being processed is preserved, never
        # silently overwritten.
        atomic_write_text(dest, produced_text, replace=False)
    except FileExistsError:
        return ExtractionOutcome(
            ok=True,
            detail=f"{dest.name} appeared during extraction; the existing sidecar was kept",
            track=track,
            method=method,
            cue_count=cue_count,
            dest=dest,
            text=produced_text,
        )
    except OSError as exc:
        return ExtractionOutcome(detail=f"could not write the extracted sidecar ({exc})", track=track)

    record_extracted_sidecar(
        video,
        dest,
        track=track,
        method=method,
        cue_count=cue_count,
        sha256=sha256_text(produced_text),
        ocr_backend=ocr_label,
    )
    log(
        f"Extracted {cue_count} cue(s) from the embedded {track.label} -> {dest.name}",
        log_file=log_file,
    )
    return ExtractionOutcome(
        ok=True,
        detail=(f"extracted {cue_count} cue(s) from the embedded {track.label}"
                + (f" via {ocr_label}" if method == "ocr" else "")),
        track=track,
        method=method,
        ocr_backend=ocr_label,
        cue_count=cue_count,
        dest=dest,
        text=produced_text,
    )


@dataclass
class ExtractorConfig:
    """Every knob of one extraction pass. No provider settings exist any more."""
    library: Path
    log_file: Path | None
    report_file: Path
    min_movie_size_mb: float = MIN_MOVIE_SIZE_MB
    lock_timeout_seconds: float = 60.0
    dry_run: bool = False
    limit: int = 0
    extract_min_cues: int = DEFAULT_EXTRACT_MIN_CUES
    ocr_backend: str = OCR_BACKEND_AUTO
    ocr_bin: str = ""
    ocr_args: str = ""
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SEC
    # 0 = no per-run cap on OCR jobs (they are local work, not provider quota)
    ocr_limit: int = 0
    # Workers for the local pre-flight only (layout, existing sidecars,
    # identity). 0 = decide from the CPU count. Provider requests, the quota
    # ledger and every write stay on the single main thread whatever this says.
    workers: int = 0

    def extract_options(self, *, ocr_allowed: bool = True, dry_run: bool = False) -> ExtractOptions:
        return ExtractOptions(
            enabled=True,
            ocr_backend=self.ocr_backend,
            ocr_bin=self.ocr_bin,
            ocr_args=self.ocr_args,
            ocr_timeout_seconds=self.ocr_timeout_seconds,
            ocr_allowed=ocr_allowed,
            min_cues=self.extract_min_cues,
            dry_run=dry_run,
        )

    @property
    def min_bytes(self) -> int:
        return int(self.min_movie_size_mb * 1024 * 1024)


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



def mark_extracted_sidecar_synced(
    sidecar: Path, sha256: str | None = None, *, path: Path | None = None,
    new_sha256: str | None = None,
) -> bool:
    """Record that the sync step has finished with this extracted sidecar.

    ``sha256`` is the provenance that authorised the mark: it must match the
    sha the record already holds. ``new_sha256`` re-points the record at the
    bytes that replaced the sidecar (ffsubsync's corrected output), so the
    record keeps describing the file that is actually on disk; without it the
    mark applies to the unchanged bytes (the "already in sync" outcome).

    Best effort, like the ledger itself: a record that cannot be updated
    costs the next sync run one redundant measurement, never a wrong sync.
    """
    target = path or extracted_ledger_path()
    with _LEDGER_WRITE_LOCK:
        payload = load_extracted_ledger(target)
        record = payload.get("sidecars", {}).get(path_norm(sidecar))
        if not isinstance(record, dict):
            return False
        if sha256 and str(record.get("sha256") or "") != sha256:
            return False
        if new_sha256:
            record["sha256"] = new_sha256
        record["synced_utc"] = utc_timestamp()
        try:
            atomic_write_json(target, payload)
        except OSError:
            return False
        return True


def extracted_sidecar_needs_sync(
    sidecar: Path, sha256: str, *, path: Path | None = None
) -> bool:
    """True when ``sidecar`` is an extracted copy the sync step has not processed.

    The provenance record is matched on the sidecar's current SHA-256, so a
    hand-edited or replaced file no longer counts as the extracted copy - it
    is left alone rather than "corrected" back.
    """
    record = find_extracted_record(sidecar, sha256, path=path)
    return record is not None and not str(record.get("synced_utc") or "")

def inspect_existing_sidecars(video: Path) -> tuple[str, Path | None, str, str]:
    """Classify existing English sidecars without trusting filename alone.

    The cleaner's automatic external-subtitle policy requires the exact
    ``Movie.eng.srt`` name. A validated legacy ``Movie.en.srt`` is renamed in
    place to that canonical name. Any other noncanonical or invalid English
    sidecar is kept for manual review rather than being overwritten by a
    fresh extraction.

    Returns ``(status, path, detail, reason)`` where ``reason`` is one of the
    ``REASON_*`` codes (empty for ``missing``, which means "go and fetch one").
    """
    exact = dest_for(video)
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
    covering = covering_english_srt_paths(video)
    for path in covering:
        if path not in candidates and not any(item.name.casefold() == path.name.casefold() for item in candidates):
            continue
        match = next((item for item in candidates if item.name.casefold() == path.name.casefold()), path)
        try:
            file_stat = match.stat(follow_symlinks=False)
            if match.is_symlink() or not match.is_file() or file_stat.st_size <= 0 or file_stat.st_size > MAX_SUBTITLE_BYTES:
                continue
            text = normalize_srt_newlines(decode_subtitle_bytes(match.read_bytes()))
            valid = looks_like_srt(text)
        except (OSError, EOFError, ValueError):
            valid = False
        if valid:
            return "covered", match, f"validated covering sidecar {match.name}", REASON_COVERED
    for path in candidates:
        try:
            file_stat = path.stat(follow_symlinks=False)
            if path.is_symlink() or not path.is_file() or file_stat.st_size <= 0 or file_stat.st_size > MAX_SUBTITLE_BYTES:
                continue
            text = normalize_srt_newlines(decode_subtitle_bytes(path.read_bytes()))
            valid = looks_like_srt(text)
        except (OSError, EOFError, ValueError):
            valid = False
        if valid:
            return (
                "review", path,
                f"'{path.name}' is a valid English SRT but not a covering .eng.srt or .eng.sdh.srt sidecar; "
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


# Before a movie is opened at all it has to get past three purely local
# questions: is its folder laid out canonically, does it already have a usable
# English sidecar, and is the file readable? On a real library the great
# majority of movies stop at question two - they are already covered, and an
# existing sidecar means the movie is never even opened - and answering it
# means listing the movie's folder and decoding every candidate SRT in it.
# That is thousands of small reads against a NAS, done one movie at a time.
#
# ``triage_movie`` is that pre-flight for one movie, pulled out whole so it can
# run in a worker pool. It asks no provider, spends no quota and touches no run
# state; the only thing it may write is the in-place legacy ``.en.srt`` ->
# ``.eng.srt`` rename that ``inspect_existing_sidecars`` has always done, and
# that is confined to the one movie's own folder.
#
# What stays strictly serial is everything downstream of triage: the
# extraction itself (one mkvmerge/mkvextract at a time) and the provenance
# ledger writes, in library order.


# Triage is filesystem-bound - one directory listing and a few small reads per
# movie - rather than CPU-bound, so several workers mostly hide the per-call
# latency of a network share. The cap keeps a spinning NAS from being turned
# into a queue of competing seeks.
MAX_TRIAGE_WORKERS = 8


@dataclass(frozen=True)
class Triage:
    """One movie's local verdict, decided before the movie is opened."""

    video: Path
    layout_issue: str = ""
    sidecar_status: str = ""
    existing: Path | None = None
    sidecar_detail: str = ""
    sidecar_reason: str = ""
    snapshot: VideoSnapshot | None = None
    key: str = ""
    error: str = ""

    @property
    def fetchable(self) -> bool:
        """True when nothing local settled this movie and extraction may run."""
        return not self.layout_issue and not self.error and self.snapshot is not None


def triage_movie(video: Path, library: Path) -> Triage:
    """Answer every local question about one movie, in the run's own order.

    The order matters and matches the sequential run exactly: a non-canonical
    folder is skipped before its sidecars are read, an existing sidecar settles
    the movie before its identity is captured, and an identity error is only
    reported for a movie that would otherwise have been fetched.
    """
    layout_issue = canonical_movie_layout_issue(video, library)
    if layout_issue:
        return Triage(video, layout_issue=layout_issue)
    status, existing, detail, reason = inspect_existing_sidecars(video)
    settled = Triage(
        video, sidecar_status=status, existing=existing,
        sidecar_detail=detail, sidecar_reason=reason,
    )
    if status in {"covered", "review"}:
        return settled
    try:
        snapshot = video_snapshot(video)
        key = movie_key(video, snapshot)
    except OSError as exc:
        return replace(settled, error=str(exc) or exc.__class__.__name__)
    return replace(settled, snapshot=snapshot, key=key)


# How far ahead of the sequential loop the triage pool is allowed to work. A
# run stops the moment the last provider quota is gone, so an unbounded
# pre-pass would read every folder in a 900-movie library to serve a run that
# only got to movie 40. One chunk is the whole waste, and it is local reads.
TRIAGE_LOOKAHEAD = 32


class TriageQueue:
    """Triage movies a chunk at a time, in parallel, handed back in order."""

    def __init__(
        self,
        videos: Sequence[Path],
        library: Path,
        *,
        workers: int = 1,
        chunk: int = TRIAGE_LOOKAHEAD,
    ) -> None:
        self._videos = list(videos)
        self._library = library
        self._workers = max(1, int(workers))
        self._chunk = max(1, int(chunk))
        self._ready: dict[int, Triage] = {}

    @property
    def workers(self) -> int:
        return self._workers

    def at(self, index: int) -> Triage:
        """Return the verdict for the 1-based ``index``-th movie."""
        if index not in self._ready:
            self._fill(index)
        return self._ready[index]

    def _fill(self, index: int) -> None:
        start = index - 1
        batch = self._videos[start:start + self._chunk]
        outcomes = map_ordered(
            batch,
            lambda video: triage_movie(video, self._library),
            workers=min(self._workers, max(1, len(batch))),
        )
        # Only the current window is kept: access is sequential, so an entry
        # behind the cursor is dead weight on a large library.
        self._ready = {
            start + 1 + outcome.index: self._verdict(outcome)
            for outcome in outcomes
        }

    @staticmethod
    def _verdict(outcome: JobOutcome[Path, Triage]) -> Triage:
        if outcome.value is not None:
            return outcome.value
        # An unexpected failure while reading one movie's folder becomes that
        # movie's error rather than the end of a run that may already have
        # downloaded subtitles. KeyboardInterrupt is re-raised by the pool.
        detail = str(outcome.error) or outcome.error.__class__.__name__
        return Triage(outcome.item, error=detail)




def coverage_count(results: Sequence[JobResult], *, dry_run: bool) -> int:
    """How many movies end this run with a validated English subtitle.

    Coverage is the product promise, so it counts outcomes rather than work:
    a movie that already had a sidecar counts exactly as much as one that was
    extracted this run. A dry run counts what it would have extracted,
    because the number it prints is a forecast of the real run.
    """
    return sum(
        1 for result in results
        if result.reason in (REASON_COVERED, REASON_EXTRACTED)
        or (dry_run and result.reason == REASON_DRY_RUN)
    )


def movie_label(video: Path, library: Path) -> str:
    """The movie's folder, relative to the library.

    The layout contract is ``Title (Year)/Title (Year).mkv``, so the folder
    already names the movie; repeating the ``.mkv`` beside it only made every
    line longer without saying anything new.
    """
    if video.parent != library:
        return relative_text(video.parent, library)
    return relative_text(video, library)

# =============================================================================
# ONE PASS OVER THE LIBRARY
# =============================================================================
#

def extraction_run(cfg: ExtractorConfig) -> tuple[list[JobResult], dict[str, Any]]:
    """Walk the library once: keep what is covered, extract what can be.

    The order of questions per movie is the whole policy:

    1. an existing validated ``.eng.srt`` beside the movie settles it - the
       movie is reported as covered and nothing is extracted, synced or
       rewritten. An existing sidecar is authoritative;
    2. otherwise the movie's own embedded English track is extracted into
       the canonical sidecar (text via mkvextract, image via OCR, MP4
       through the temporary bridge);
    3. a movie with no usable embedded track is reported as needing
       attention - there is no download to fall back to.
    """
    results: list[JobResult] = []
    # OCR jobs are minutes of local CPU each, so the run can cap them
    # (--ocr-limit) independently of everything else.
    ocr_jobs = 0
    extract_notes: set[str] = set()

    videos = discover_videos(cfg.library, cfg.min_bytes)
    if cfg.limit > 0:
        videos = videos[: cfg.limit]
    total = len(videos)
    log(f"Found {total} eligible movies.", log_file=cfg.log_file)

    def emit(index: int, status: str, video: Path, detail: str) -> None:
        log(
            f"[{index:03d}/{total:03d}] {status:<8} "
            f"{relative_text(video, cfg.library)} — {detail}",
            log_file=cfg.log_file,
        )

    triage_queue = TriageQueue(
        videos, cfg.library,
        workers=resolve_workers(cfg.workers, items=len(videos), cap=MAX_TRIAGE_WORKERS),
    )
    if triage_queue.workers > 1:
        log(
            f"Inspecting existing sidecars with {triage_queue.workers} workers "
            "(--workers 1 for the serial run).",
            log_file=cfg.log_file,
        )

    for index, video in enumerate(videos, start=1):
        triage = triage_queue.at(index)
        if triage.layout_issue:
            result = JobResult(video, "skip", triage.layout_issue, reason=REASON_LAYOUT)
            results.append(result)
            emit(index, "SKIP", video, triage.layout_issue)
            continue
        if triage.sidecar_status == "covered" and triage.existing is not None:
            # The one rule that never bends: a movie that already has a
            # validated .eng.srt is not touched. No extraction attempt, no
            # sync trigger, nothing.
            result = JobResult(video, "have", triage.sidecar_detail, triage.existing,
                               reason=REASON_COVERED)
            results.append(result)
            emit(index, "HAVE", video, triage.sidecar_detail)
            continue
        if triage.sidecar_status == "review":
            result = JobResult(video, "review", triage.sidecar_detail, triage.existing,
                               reason=triage.sidecar_reason)
            results.append(result)
            emit(index, "REVIEW", video, triage.sidecar_detail)
            continue
        if not triage.fetchable or triage.snapshot is None:
            result = JobResult(video, "error", triage.error, reason=REASON_ERROR)
            results.append(result)
            emit(index, "ERROR", video, triage.error)
            continue

        extract_dest = dest_for(video)
        outcome = extract_embedded_english_srt(
            video,
            extract_dest,
            cfg.extract_options(
                ocr_allowed=cfg.ocr_limit <= 0 or ocr_jobs < cfg.ocr_limit,
                dry_run=cfg.dry_run,
            ),
            log_file=cfg.log_file,
        )
        if outcome.ok:
            if outcome.method == "ocr":
                ocr_jobs += 1
            if cfg.dry_run:
                result = JobResult(video, "dry-run", outcome.detail, extract_dest,
                                   reason=REASON_DRY_RUN)
                results.append(result)
                emit(index, "DRYRUN", video, outcome.detail)
                continue
            result = JobResult(video, "extracted", outcome.detail, extract_dest,
                               reason=REASON_EXTRACTED)
            results.append(result)
            emit(index, "EXTRACT", video, outcome.detail)
            continue

        # Extraction produced nothing. Distinguish "nothing to extract"
        # (the healthy common case) from per-track failures, and surface a
        # missing toolchain once per run rather than once per movie.
        detail = outcome.unavailable_reason or outcome.detail or "no usable embedded English subtitle track"
        if outcome.unavailable_reason and outcome.unavailable_reason not in extract_notes:
            extract_notes.add(outcome.unavailable_reason)
            if "not installed" in outcome.unavailable_reason or "OCR" in outcome.unavailable_reason:
                log(outcome.unavailable_reason, level="WARNING", log_file=cfg.log_file)
        result = JobResult(video, "skip", detail, reason=REASON_NO_TRACK)
        results.append(result)
        emit(index, "NO-SUBS", video, detail)

    summary = {
        "movies_discovered": total,
        "coverage_covered": coverage_count(results, dry_run=cfg.dry_run),
        "coverage_total": total,
        "extracted_from_embedded": sum(
            1 for r in results if r.reason == REASON_EXTRACTED
        ),
        "ocr_jobs": ocr_jobs,
        "ledger_log": str(cfg.log_file) if cfg.log_file else "",
    }
    return results, summary


# =============================================================================
# REPORT
# =============================================================================

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
        "believes is already present, so a corrupt file blocks extraction forever.",
    ),
    NeedsBucket(
        REASON_SIDECAR_NAME,
        "SIDECAR NAME IS NOT CANONICAL",
        f"rename it to <movie>{EXTERNAL_SRT_SUFFIX}, or delete it",
        f"Rename the file to \"<movie>{EXTERNAL_SRT_SUFFIX}\" (or delete it) and re-run. "
        "Jellyfin and Plex only direct play that exact name, and this tool will not "
        "write a second subtitle over one that is already there.",
    ),
    NeedsBucket(
        REASON_LAYOUT,
        "LIBRARY LAYOUT MUST BE FIXED FIRST",
        "run movie_standardizer.py on that folder",
        "Each movie must be one video file in a folder of the same name: "
        "\"Title (Year)/Title (Year).mkv\". Run movie_standardizer.py, or fix the "
        "folder by hand, and this movie will be picked up on the next run.",
    ),
    NeedsBucket(
        REASON_NO_TRACK,
        "NO USABLE EMBEDDED ENGLISH TRACK",
        "add an .eng.srt yourself",
        "This movie has no external English subtitle and no embedded track this tool "
        "can convert (only forced/signs-only streams, image tracks with no OCR backend "
        "installed, or nothing at all). Subtitle downloading was removed from this "
        "toolkit, so the fix is a human one: find the subtitle and place it beside the "
        "movie. See the log for the per-track reasons.",
    ),
    NeedsBucket(
        REASON_ERROR,
        "ERRORS",
        "read the log entry for each one",
        "Something failed while reading the movie or running mkvmerge/mkvextract. "
        "The log carries the exact error; fix the cause and re-run.",
    ),
)


def group_results(
    results: Sequence[JobResult],
) -> tuple[dict[str, list[tuple[Path, str]]], list[JobResult], list[JobResult], list[JobResult]]:
    """Split one run into (needs buckets, covered, extracted, dry-run)."""
    buckets: dict[str, list[tuple[Path, str]]] = {bucket.reason: [] for bucket in NEEDS_SUBTITLE_BUCKETS}
    covered: list[JobResult] = []
    dry_run: list[JobResult] = []
    extracted: list[JobResult] = []
    for result in results:
        if result.reason == REASON_COVERED:
            covered.append(result)
        elif result.reason == REASON_EXTRACTED:
            extracted.append(result)
        elif result.reason == REASON_DRY_RUN:
            dry_run.append(result)
        elif result.reason in buckets:
            buckets[result.reason].append((result.video, result.detail))
        else:  # a reason nobody knows about must still be visible, not dropped
            buckets.setdefault(REASON_ERROR, []).append((result.video, result.detail or result.status))
    for items in buckets.values():
        items.sort(key=lambda item: str(item[0]).casefold())
    covered.sort(key=lambda item: str(item.video).casefold())
    dry_run.sort(key=lambda item: str(item.video).casefold())
    extracted.sort(key=lambda item: str(item.video).casefold())
    return buckets, covered, extracted, dry_run

def build_report(results: Sequence[JobResult], cfg: ExtractorConfig, summary: dict[str, Any]) -> str:
    """Render the whole run as one report a human can act on in ten seconds."""
    buckets, covered, extracted, dry_run = group_results(results)
    needs = sum(len(items) for items in buckets.values())
    total = int(summary.get("movies_discovered") or len(results))
    covered_count = int(summary.get("coverage_covered", len(covered) + len(extracted)
                                    + (len(dry_run) if cfg.dry_run else 0)) or 0)
    coverage_pct = (100.0 * covered_count / total) if total else 100.0

    report = Report(
        "JELLYFIN EMBEDDED SUBTITLE EXTRACTION REPORT",
        f"One validated external English {EXTERNAL_SRT_SUFFIX} per movie \u00b7 built from the movie's own tracks",
    )
    report.metas([
        ("Generated", f"{utc_timestamp()} (UTC)"),
        ("Library", cfg.library),
        ("Embedded tracks", extract_banner_text(cfg)),
        ("Triage", describe_workers(
            resolve_workers(cfg.workers, cap=MAX_TRIAGE_WORKERS), "movie")),
        ("Ledger", cfg.log_file or "(none)"),
    ])

    rows: list[tuple[object, str, str]] = [
        (f"{covered_count}/{total} ({coverage_pct:.1f}%)",
         "COVERAGE: movies with a validated English SRT" + (" (would be covered)" if cfg.dry_run else ""),
         "the goal: 100% - every uncovered movie is named below"),
        (len(covered), "Already have .eng.srt", "authoritative; never re-extracted or synced"),
        (len(extracted), "Extracted this run", f"written as <movie>{EXTERNAL_SRT_SUFFIX}"),
    ]
    if dry_run or cfg.dry_run:
        rows.append((len(dry_run), "Dry-run extractions", "no files were written"))
    rows.append((needs, "NEED ATTENTION", "no sidecar and no usable embedded track"))
    rows.append((total, "Movies in the library", "every folder holding an eligible MKV or MP4"))
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

    # ---- what still needs attention --------------------------------------
    report.section(
        "MOVIES THAT NEED ATTENTION",
        count=needs,
        total=total,
        intro=(
            "These movies have no external English subtitle and no embedded track this "
            "tool could convert. Subtitle downloading was removed from this toolkit, so "
            "each one is a human decision: place the subtitle yourself. Groups are "
            "ordered cheapest fix first."
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
    if extracted:
        report.section(
            "EXTRACTED FROM THE MOVIE'S OWN EMBEDDED TRACK",
            count=len(extracted),
            total=total,
            intro=(
                "These movies carried an English subtitle track. It was extracted to the "
                f"canonical <movie>{EXTERNAL_SRT_SUFFIX}: exact for this release, and its cues "
                "come from the container's own timeline. sync_subtitles.py measures each "
                "one once and corrects the timing only when the drift is real and "
                "trustworthy; mkv_track_cleaner.py then strips every embedded subtitle, "
                "leaving this sidecar as the sole subtitle option."
            ),
        )
        report.entries(
            [{"text": movie_label(result.video, cfg.library), "detail": result.detail}
             for result in extracted],
        )

    if dry_run:
        report.section(
            "DRY-RUN EXTRACTIONS (NOTHING WAS WRITTEN)",
            count=len(dry_run),
            total=total,
            intro="Re-run without --dry-run to actually write these sidecars.",
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
            "Every movie here has a validated sidecar with the exact canonical name. "
            "This tool left it - and the movie - completely untouched: an existing "
            "sidecar is authoritative and is never re-extracted or re-synced."
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
        f"Extraction provenance ledger  {extracted_ledger_path()}",
        f"This report  {cfg.report_file}",
        "Re-running is always safe: covered movies are skipped without reading the "
        "movie, and a sidecar this tool wrote is never written twice.",
    ])
    return report.render()


def write_report(results: Sequence[JobResult], cfg: ExtractorConfig, summary: dict[str, Any]) -> None:
    """Publish the report: written atomically, then echoed to the console."""
    text = build_report(results, cfg, summary)
    atomic_write_text(cfg.report_file, text, replace=True)
    print_text(text)
    log(f"Report written: {cfg.report_file}", log_file=cfg.log_file)

def extract_banner_text(cfg: ExtractorConfig) -> str:
    """One banner line saying what extraction can do on this machine.

    Every binary is required or optional somewhere, so the run has to say up
    front whether the movies' tracks are usable here - otherwise an
    image-only library looks broken when it is really a missing OCR tool.
    """
    if not find_mkvtoolnix_binary("mkvmerge") or not find_mkvtoolnix_binary("mkvextract"):
        return f"unavailable: {MKVTOOLNIX_INSTALL_HINT}"
    backend, note = detect_ocr_backend(cfg.ocr_backend, explicit_bin=cfg.ocr_bin,
                                       arg_template=cfg.ocr_args)
    text_part = f"text tracks (SRT/SSA/ASS) with mkvextract (>= {cfg.extract_min_cues} cues)"
    image_part = (f"image tracks (PGS/VobSub) with {backend.label}"
                  if backend is not None else f"image tracks (PGS/VobSub) reported: {note}")
    return f"{text_part}; {image_part}; MP4s read through a temporary MKV bridge"



# =============================================================================
# COMPACT ROOT-LEVEL DRIVER
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract one validated external English SRT per movie from its own "
            "embedded subtitle track. An existing .eng.srt beside a movie is "
            "authoritative and is never re-extracted or synced. There is no "
            "subtitle downloading: a movie with no usable embedded track and no "
            "sidecar is reported for manual attention."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", type=Path, default=Path(LIBRARY_DIR),
                        help="Jellyfin movie-library root")
    parser.add_argument("--report", type=Path, default=Path(REPORT_FILE),
                        help="Single replaceable human-readable report outside the library")
    parser.add_argument("--log", type=Path, default=Path(LOG_FILE),
                        help="Single root log outside the media library")
    parser.add_argument("--min-size", type=float, default=MIN_MOVIE_SIZE_MB, metavar="MB")
    parser.add_argument("--lock-timeout", type=float, default=60.0, metavar="SEC")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="Process at most N movies (0 means all eligible movies)")
    parser.add_argument("--workers", type=int, default=0, metavar="N",
                        help=f"Inspect N movies' existing sidecars at once (0 = half the CPUs, "
                             f"capped at {MAX_TRIAGE_WORKERS}; 1 = the serial run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be extracted; no sidecar is written "
                             "(an MP4's temporary probe bridge is still built outside the library)")
    parser.add_argument("--extract-min-cues", type=int, default=DEFAULT_EXTRACT_MIN_CUES, metavar="N",
                        help="Reject an embedded track with fewer than N cues as signs/songs-only")
    parser.add_argument("--ocr-backend", default=OCR_BACKEND_AUTO, choices=list(OCR_BACKEND_CHOICES),
                        help="OCR program for image-based tracks (PGS/VobSub): auto picks the first "
                             "one installed; none disables image tracks entirely")
    parser.add_argument("--ocr-bin", default="", metavar="PATH",
                        help="Path to the OCR program or .NET dll (PgsToSrt) instead of searching PATH")
    parser.add_argument("--ocr-args", default="", metavar="ARGS",
                        help="Argument template for --ocr-backend custom, e.g. \"{input}\" \"{output}\" "
                             "(placeholders: {input} {output} {track} {lang})")
    parser.add_argument("--ocr-timeout", type=float, default=DEFAULT_OCR_TIMEOUT_SEC, metavar="SEC",
                        help="Per-movie OCR time limit (0 disables the limit)")
    parser.add_argument("--ocr-limit", type=int, default=0, metavar="N",
                        help="OCR at most N movies per run (0 means no cap; OCR is minutes of local CPU)")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def extractor_config_from_args(args: argparse.Namespace) -> ExtractorConfig:
    return ExtractorConfig(
        library=args.source.resolve(),
        log_file=args.log.resolve() if args.log else None,
        report_file=args.report.resolve(),
        min_movie_size_mb=float(args.min_size),
        lock_timeout_seconds=max(0.0, float(args.lock_timeout)),
        workers=int(args.workers),
        dry_run=bool(args.dry_run),
        limit=max(0, int(args.limit)),
        extract_min_cues=max(1, int(args.extract_min_cues)),
        ocr_backend=str(args.ocr_backend),
        ocr_bin=str(args.ocr_bin),
        ocr_args=str(args.ocr_args),
        ocr_timeout_seconds=max(0.0, float(args.ocr_timeout)),
        ocr_limit=max(0, int(args.ocr_limit)),
    )


def validate_config(cfg: ExtractorConfig) -> list[str]:
    errors: list[str] = []
    if not cfg.library.is_dir() or cfg.library.is_symlink():
        errors.append("--source must be an existing non-symlink movie-library directory")
    if cfg.ocr_backend not in OCR_BACKEND_CHOICES:
        errors.append(f"--ocr-backend must be one of: {', '.join(OCR_BACKEND_CHOICES)}")
    if cfg.extract_min_cues < 1:
        errors.append("--extract-min-cues must be at least 1")
    if cfg.ocr_timeout_seconds < 0:
        errors.append("--ocr-timeout must be zero (no limit) or greater")
    if cfg.ocr_limit < 0:
        errors.append("--ocr-limit must be zero (no cap) or greater")
    if cfg.workers < 0:
        errors.append("--workers must be non-negative (0 = decide from the CPU count)")
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
        cfg = extractor_config_from_args(args)
        errors = validate_config(cfg)
        if errors:
            for error in errors:
                print(f"Configuration error: {error}", file=sys.stderr)
            return 2
        mode = "DRY-RUN (nothing will be written)" if cfg.dry_run else "LIVE"
        print_text(report_banner(
            "JELLYFIN EMBEDDED ENGLISH SRT EXTRACTOR",
            f"One validated external English {EXTERNAL_SRT_SUFFIX} per movie",
            [
                ("Mode", mode),
                ("Library", cfg.library),
                ("Policy", "English human-authored UTF-8 SRT; an existing sidecar is authoritative"),
                ("Embedded tracks", extract_banner_text(cfg)),
                ("Triage", describe_workers(
                    resolve_workers(cfg.workers, cap=MAX_TRIAGE_WORKERS), "movie")),
                ("Ledger", cfg.log_file),
                ("Report", cfg.report_file),
            ],
        ))
        with CoordinationLock(cfg.library, timeout_seconds=cfg.lock_timeout_seconds):
            results, summary = extraction_run(cfg)
            write_report(results, cfg, summary)
        if any(result.status == "error" for result in results):
            return 1
        return 0
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort: whatever went wrong, this run
        # leaves through one exit code instead of an unhandled traceback.
        print(f"Subtitle extractor failure: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


def run_self_tests() -> int:
    """Field smoke test: can this copy judge a subtitle and name its sidecar?

    The extraction paths (probe, classify, convert, OCR, the MP4 bridge and
    the provenance ledger) are covered exhaustively in ``tests/``. The two
    things worth re-checking on an unfamiliar machine are the sidecar
    contract and the sync handshake, because a wrong one silently breaks the
    rest of the pipeline.
    """
    def a_real_srt_validates() -> bool:
        with tempfile.TemporaryDirectory(prefix="extractor_smoke_") as td:
            srt = Path(td) / "Movie (2020).eng.srt"
            srt.write_text("1\n00:00:01,000 --> 00:00:02,500\nHello\n", encoding="utf-8")
            return validate_srt_sidecar(srt)[0]

    def html_is_rejected() -> bool:
        with tempfile.TemporaryDirectory(prefix="extractor_smoke_") as td:
            srt = Path(td) / "Movie (2020).eng.srt"
            srt.write_text("<!DOCTYPE html><html>not a subtitle</html>", encoding="utf-8")
            return not validate_srt_sidecar(srt)[0]

    def the_sidecar_path_is_canonical() -> bool:
        movie = Path("/library/Movie (2020)/Movie (2020).mkv")
        return exact_external_english_srt_path(movie).name == "Movie (2020).eng.srt"

    def the_sync_handshake_round_trips() -> bool:
        with tempfile.TemporaryDirectory(prefix="extractor_smoke_") as td:
            sidecar = Path(td) / "Movie (2020).eng.srt"
            sidecar.write_text("1\n00:00:01,000 --> 00:00:02,500\nHello\n", encoding="utf-8")
            ledger = Path(td) / "ledger.json"
            sha = sha256_text(sidecar.read_text(encoding="utf-8"))
            record_extracted_sidecar(
                Path(td) / "Movie (2020).mkv", sidecar,
                track=EmbeddedSubtitleTrack(
                    track_id=1, codec_id="S_TEXT/UTF8", language="eng", name="",
                    kind="text", extension=".srt",
                ),
                method="text", cue_count=1, sha256=sha, path=ledger,
            )
            return (
                extracted_sidecar_needs_sync(sidecar, sha, path=ledger)
                and mark_extracted_sidecar_synced(sidecar, sha, path=ledger)
                and not extracted_sidecar_needs_sync(sidecar, sha, path=ledger)
            )

    return run_field_smoke_test("subtitle_extractor.py", [
        ("a valid .eng.srt is accepted", a_real_srt_validates),
        ("an HTML error page is rejected", html_is_rejected),
        ("the sidecar path is canonical", the_sidecar_path_is_canonical),
        ("the extraction/sync handshake round-trips", the_sync_handshake_round_trips),
    ])

if __name__ == "__main__":
    raise SystemExit(main())
