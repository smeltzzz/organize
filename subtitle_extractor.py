#!/usr/bin/env python3
"""
Embedded-Subtitle Extractor for Jellyfin Movies
===============================================
After ``movie_standardizer.py`` and before ``mkv_track_cleaner.py``: walk the
canonical movie library and create at most one validated external English SRT
sidecar per movie, in this order of preference:

* a movie that already has an ``.eng.srt`` beside it is left completely
  alone - no extraction, no rewriting, no provider anything. An existing
  sidecar is authoritative;
* a movie without one gets its embedded English *text* subtitle track
  (SRT/SSA/ASS/WebVTT/USF) extracted into ``Title (Year).eng.srt`` and
  converted in-process. The movie's own track is the most trustworthy source
  there is: it is exact for this release and carries the container's own
  timestamps. Forced/signs-only, commentary, non-English and too-short
  tracks are refused, so a movie is never left with a partial "subtitle";
* a movie whose *only* English subtitle tracks are image-based (PGS/VobSub/
  DVB) is not OCR'd any more - OCR was unreliable and barely worked. Instead
  this tool reaches out to OpenSubtitles and downloads an English SRT whose
  movie hash matches this movie's hash *exactly* (the same hash the service
  keys uploaded subtitles by, computed from the file's bytes and size),
  validates it with the same gate an extraction passes, and writes it as
  ``Title (Year).eng.srt``. An OpenSubtitles account is configured with
  ``OPENSUBTITLES_API_KEY``/``OPENSUBTITLES_USERNAME``/``OPENSUBTITLES_PASSWORD``
  (or the ``--osdb-*`` flags) and the fallback can be switched off with
  ``--no-open-subtitles``;
* MP4 movies are read through a temporary subtitle-only MKV bridge, because
  mkvextract reads Matroska only. The bridge lives outside the library and
  is deleted with the run; ``mkv_track_cleaner.py`` later converts the
  container itself, so no embedded track is lost to the MP4 -> MKV
  conversion;
* every sidecar this tool writes is recorded in a provenance ledger outside
  the library: which movie it came from, which embedded track (or which
  OpenSubtitles file), by which method, its SHA-256 and when. The ledger is
  the durable answer to "did this tool write this sidecar?", and it is what
  makes a re-run cheap;
* a movie with no usable embedded text track and no exact-hash download
  (or no account configured) is listed in the report as needing attention.
  That is a human's decision, not a fuzzy title search.

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
``mkvmerge``/``mkvextract`` (MKVToolNix), and the network is only touched
for the exact-hash OpenSubtitles download of image-only movies.
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
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Shared implementation: everything imported here is defined exactly once,
# in organizekit/core/. See tests/test_shared_core.py for the rule that
# keeps it that way.
from organizekit.core import (
    COVERING_ENGLISH_SRT_SUFFIXES,
    EXTERNAL_SRT_ENCODINGS,
    EXTERNAL_SRT_LANG,
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
    load_dotenv,
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

__version__ = "4.0.0"

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
REASON_DOWNLOADED = "downloaded"
REASON_DRY_RUN = "dry_run"
REASON_NO_TRACK = "no_track"
REASON_SIDECAR_UNUSABLE = "sidecar_unusable"
REASON_SIDECAR_NAME = "sidecar_name"
REASON_LAYOUT = "layout"
REASON_ERROR = "error"

@dataclass
class JobResult:
    video: Path
    status: str  # have, skip, extracted, downloaded, dry-run, review, error
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
# EMBEDDED SUBTITLE EXTRACTION - the tool's first job
#
# A Jellyfin movie very often already carries the English subtitle as an
# embedded track, and that track is exact for this release and cannot be the
# wrong cut. The sidecar is built from it:
#
#   * the cues carry the container's own timestamps, so the sidecar is
#     frame-accurate for this exact file and needs no offline timing
#     correction - anything left over is handled at playback time;
#   * mkv_track_cleaner.py strips every embedded subtitle afterwards, so the
#     external sidecar becomes the sole subtitle option - which is why this
#     tool runs first;
#   * MKVToolNix does the reading: ``mkvmerge -J`` lists the tracks and
#     ``mkvextract`` writes one out. MP4 movies are read through a temporary
#     subtitle-only MKV bridge (mkvextract reads Matroska only), built
#     outside the library and deleted with the run.
#
# Text tracks (SRT/SSA/ASS/WebVTT/USF) are converted to SRT in-process with
# the standard library. Image tracks (PGS/SUP, VobSub, DVB) are recognised
# but never OCR'd: OCR output was unreliable in the field - garbled cue
# text, dropped dialogue, half-transcribed songs - and a bad sidecar is
# worse than none. A movie whose only English tracks are image-based is
# instead served from the OpenSubtitles exact-hash download (below), and a
# movie with no usable track at all is reported as needing attention.
#
# Extraction never rewrites or deletes the movie: mkvextract and the bridge
# builder only read it, and every temporary file lives outside the library.
# =============================================================================

# Embedded text subtitle codecs this tool can turn into an external SRT.
EXTRACT_TEXT_CODECS: dict[str, str] = {
    "S_TEXT/UTF8": ".srt",
    "S_TEXT/ASCII": ".srt",
    "S_TEXT/SSA": ".ssa",
    "S_TEXT/ASS": ".ass",
    "S_TEXT/WEBVTT": ".vtt",
    "S_TEXT/USF": ".usf",
}

# Embedded image subtitle codecs. Recognised so the run can tell a
# "text-extractable" movie from an "image-only" one - the image-only
# verdict is what triggers the OpenSubtitles exact-hash download. They are
# never extracted or OCR'd here.
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
# movie's English subtitle would be a silent downgrade. The same floor applies
# to a downloaded sidecar: a two-cue "subtitle" is not a subtitle.
DEFAULT_EXTRACT_MIN_CUES = 10
# How many text candidates to try per movie before giving up on extraction.
DEFAULT_EXTRACT_TEXT_CANDIDATE_LIMIT = 3
DEFAULT_MKVEXTRACT_TIMEOUT_SEC = 900.0

# Durable, outside-the-library record of which sidecars this tool created from
# the movie's own tracks: the provenance that says "this .eng.srt came out of
# this movie, by this method, at this time". It lives beside the other
# ReportsAndLogs artefacts.
EXTRACTED_LEDGER_NAME = "subtitle_extractor_extracted.json"
# Libraries cut over from the fetching era still hold provenance records
# written by subtitle_fetcher.py under this name; they are read (never
# written) until this tool has a ledger of its own.
LEGACY_EXTRACTED_LEDGER_NAME = "subtitle_fetcher_extracted.json"
EXTRACTED_LEDGER_ENV = "SUBTITLE_EXTRACTED_LEDGER"
EXTRACTED_LEDGER_VERSION = 1

# =============================================================================
# OPENSUBTITLES - the exact-hash download for image-only movies
#
# The fallback for a movie whose only English subtitle tracks are image-based:
# the OpenSubtitles API (https://api.opensubtitles.com, v1 endpoints). The
# identification is an *exact* match on the movie's hash - the same MD5 the
# service computes over uploaded subtitle files - so a hit is a subtitle for
# this exact release. There is deliberately no title/year search: fuzzy
# matches are how the wrong cut, the wrong language or a trailer ends up on
# a movie.
#
# Credentials: a free OpenSubtitles account. The API key (from the profile
# page) authorises the search; the account's username and password are
# exchanged for a bearer token that authorises the download. All three come
# from the environment or the --osdb-* flags; with any of them missing the
# fallback simply does not run, and the report says so.
# =============================================================================

OSDB_API_BASE = "https://api.opensubtitles.com/api/v1"
OSDB_API_KEY_ENV = "OPENSUBTITLES_API_KEY"
OSDB_USERNAME_ENV = "OPENSUBTITLES_USERNAME"
OSDB_PASSWORD_ENV = "OPENSUBTITLES_PASSWORD"
# The service wants a User-Agent that says what is calling; a bare python-urllib
# string is what the old fetcher got blocked for.
OSDB_USER_AGENT = f"organizekit-subtitle-extractor/{__version__}"
OSDB_HTTP_TIMEOUT_SEC = 30.0
# A hit on the hash usually lists several uploads of the same file. At most
# this many are tried per movie, best ranked first.
OSDB_MAX_DOWNLOAD_CANDIDATES = 3
# The search answer is capped before it is parsed: a subtitle listing is
# small, and a hostile answer must not stream gigabytes into memory.
OSDB_MAX_BODY_BYTES = MAX_SUBTITLE_BYTES

# The OpenSubtitles v2 file hash, which uploaded subtitles are keyed by:
# MD5 over the file's leading bytes plus its size as an 8-byte little-endian
# integer. Files up to 64 MiB hash their whole body; bigger files hash their
# first 64 KiB plus the size (which distinguishes them).
OSDB_HASH_HEAD_BYTES = 64 * 1024
OSDB_HASH_LARGE_FILE_BYTES = 64 * 1024 * 1024
# Consecutive connection failures that stop the rest of the run's downloads:
# a dead network will not recover movie by movie, and every retry is a full
# timeout of wall time.
OSDB_MAX_CONSECUTIVE_NETWORK_FAILURES = 3

# Very common English function words. Their share of a real dialogue track is
# far above this floor; garbage and wrong-language tracks fall below it.
ENGLISH_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "is", "was", "are", "were", "be", "been", "it", "its", "you", "your",
    "i", "me", "my", "we", "us", "he", "she", "they", "them", "his", "her",
    "that", "this", "these", "those", "for", "with", "as", "so", "not", "no",
    "do", "did", "does", "have", "has", "had", "what", "when", "where", "who",
    "how", "why", "all", "just", "get", "got", "go", "going", "know", "think",
    "will", "can", "cant", "dont", "im", "thats", "there", "here", "up", "out",
})


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
    streams sort before image streams - the runner extracts only the former
    and uses the presence of the latter for the image_only verdict - and
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
# Quality gate: is this text a real English subtitle?
# ---------------------------------------------------------------------------
def non_latin_ratio(text: str) -> float:
    """Share of alphabetic characters outside the Latin blocks.

    A Cyrillic, Greek, CJK, or Arabic track or upload is not an English
    subtitle however good the extraction or the download was.
    """
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    non_latin = sum(1 for char in letters if ord(char) > 0x024F)
    return non_latin / len(letters)


def english_subtitle_quality(
    text: str, *, min_cues: int = DEFAULT_EXTRACT_MIN_CUES
) -> tuple[bool, str]:
    """Decide whether text may become the movie's English sidecar.

    A subtitle taken from the movie's own track is authoritative about timing
    but not about content, and an upload on a subtitle service is not
    authoritative at all: a mis-tagged foreign track, a wrong cut, or an
    error page would all produce a file that looks like success. Every
    candidate sidecar - extracted or downloaded - therefore passes the same
    conservative gate.
    """
    if not text.strip():
        return False, "the candidate contained no subtitle text"
    if len(text.encode("utf-8", errors="replace")) > MAX_SUBTITLE_BYTES:
        return False, f"the subtitle exceeds the {MAX_SUBTITLE_BYTES // (1024 * 1024)} MiB safety limit"
    if not looks_like_srt(text):
        return False, "the candidate did not convert to valid SRT cues"
    cues = parse_srt_cues(text)
    if len(cues) < min_cues:
        return (
            False,
            f"only {len(cues)} cue(s); a complete movie subtitle needs "
            f"at least {min_cues} (this is probably signs/songs-only)",
        )
    sample = " ".join(cue[2] for cue in cues)
    if non_latin_ratio(sample) > 0.40:
        return False, "the text is not Latin-script (this is not English)"
    words = re.findall(r"[A-Za-z']+", sample)
    if len(words) >= 100:
        hits = sum(1 for word in words if word.lower() in ENGLISH_STOPWORDS)
        if hits / len(words) < 0.04:
            return False, "the text does not read as English (garbled or a foreign track)"
    return True, ""


# ---------------------------------------------------------------------------
# The movie's OpenSubtitles hash
# ---------------------------------------------------------------------------
def compute_movie_hash(video: Path) -> str | None:
    """The OpenSubtitles v2 file hash, the one uploaded subtitles are keyed by.

    MD5 over the file's leading bytes plus its size as an 8-byte little-endian
    integer: the whole body for a file up to 64 MiB, only the first 64 KiB
    for a bigger one (the appended size is what distinguishes those). A
    subtitle uploaded against this exact release therefore carries this
    movie's hash, and an exact match on it is the strongest identification
    the service can offer - no title, no year, no guessing.
    """
    try:
        size = video.stat().st_size
    except OSError:
        return None
    try:
        with video.open("rb") as handle:
            head = handle.read(
                size if size <= OSDB_HASH_LARGE_FILE_BYTES else OSDB_HASH_HEAD_BYTES
            )
    except OSError:
        return None
    # This MD5 is the *service's* identification format, not an integrity
    # check of our own: the bytes it runs over are what OpenSubtitles matches
    # its uploads against, and there is no weaker substitute for that.
    digest = hashlib.md5(head + size.to_bytes(8, "little"))  # noqa: S324
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# HTTP - one exchange, never an exception
# ---------------------------------------------------------------------------
def osdb_http(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = OSDB_HTTP_TIMEOUT_SEC,
) -> tuple[int, bytes, str, dict[str, str]]:
    """One HTTP exchange against the service; never raises.

    Returns ``(status, body, error, headers)``. ``status`` is 0 when the
    connection itself failed, in which case ``error`` names the cause. The
    body is read only up to the safety cap: a subtitle answer is small, and a
    hostile one must not stream gigabytes into memory.
    """
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(OSDB_MAX_BODY_BYTES + 1)
            return int(response.status), data, "", dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read(OSDB_MAX_BODY_BYTES + 1)
        except (OSError, ValueError):
            data = b""
        return int(exc.code), data, "", dict((exc.headers or {}).items())
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return 0, b"", f"could not reach OpenSubtitles ({reason})", {}


@dataclass(frozen=True)
class OsdbError:
    """A failed service call: what kind of failure, and the sentence to show."""

    code: str  # "network" | "auth" | "rate" | "http" | "bad-answer"
    message: str


def _osdb_network_error(error: str) -> OsdbError:
    return OsdbError("network", error)


# ---------------------------------------------------------------------------
# The OpenSubtitles client
# ---------------------------------------------------------------------------
@dataclass
class OpenSubtitlesClient:
    """One session with api.opensubtitles.com (v1 endpoints).

    The API key authorises the search; the account's username and password
    are exchanged (once per session) for the bearer token that authorises a
    download. The token is cached on the instance, so a run that downloads
    for several movies logs in exactly once.
    """

    api_key: str
    username: str = ""
    password: str = ""
    base_url: str = OSDB_API_BASE
    timeout: float = OSDB_HTTP_TIMEOUT_SEC
    _token: str | None = field(default=None, repr=False)

    @property
    def configured(self) -> bool:
        """Search needs the key; a download needs the account as well."""
        return bool(self.api_key and self.username and self.password)

    def _headers(self) -> dict[str, str]:
        return {
            "Api-Key": self.api_key,
            "User-Agent": OSDB_USER_AGENT,
            "Accept": "application/json",
        }

    def search_by_hash(self, movie_hash: str) -> tuple[list[dict[str, Any]], OsdbError | None]:
        """The English SRT uploads that carry exactly this movie's hash."""
        url = f"{self.base_url}/subtitles?" + urllib.parse.urlencode(
            {"moviehash": movie_hash, "languages": "en", "format": "srt", "per_page": "50"}
        )
        status, body, error, _headers = osdb_http(
            url, method="GET", headers=self._headers(), timeout=self.timeout
        )
        if status == 0:
            return [], _osdb_network_error(error)
        if status in (401, 403):
            return [], OsdbError("auth", f"OpenSubtitles rejected the API key (HTTP {status})")
        if status == 429:
            return [], OsdbError("rate", "OpenSubtitles rate limit reached (HTTP 429); re-run later")
        if status != 200:
            return [], OsdbError("http", f"the OpenSubtitles search failed (HTTP {status})")
        try:
            payload = json.loads(body)
        except ValueError:
            return [], OsdbError("bad-answer", "OpenSubtitles returned an unreadable search answer")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return [], None
        return [entry for entry in data if isinstance(entry, dict)], None

    def _login(self) -> tuple[str, OsdbError | None]:
        status, body, error, _headers = osdb_http(
            f"{self.base_url}/login",
            method="POST",
            headers=self._headers(),
            body=json.dumps({"username": self.username, "password": self.password}).encode("utf-8"),
            timeout=self.timeout,
        )
        if status == 0:
            return "", _osdb_network_error(error)
        if status in (401, 403):
            return "", OsdbError("auth", "OpenSubtitles rejected the account credentials (HTTP 401)")
        if status == 429:
            return "", OsdbError("rate", "OpenSubtitles rate limit reached (HTTP 429); re-run later")
        if status != 200:
            return "", OsdbError("http", f"the OpenSubtitles login failed (HTTP {status})")
        try:
            payload = json.loads(body)
        except ValueError:
            return "", OsdbError("bad-answer", "OpenSubtitles returned an unreadable login answer")
        token = str(payload.get("token") or "") if isinstance(payload, dict) else ""
        if not token:
            return "", OsdbError("bad-answer", "OpenSubtitles login returned no token")
        return token, None

    def download_link(self, file_id: int) -> tuple[str, OsdbError | None]:
        """The temporary URL serving one subtitle file, logging in if needed."""
        token = self._token
        if token is None:
            token, login_error = self._login()
            if login_error is not None or not token:
                return "", login_error or OsdbError("bad-answer", "OpenSubtitles login returned no token")
            self._token = token
        for attempt in (1, 2):
            status, body, error, _headers = osdb_http(
                f"{self.base_url}/download",
                method="POST",
                headers={**self._headers(),
                         "Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                body=json.dumps({"file_id": file_id}).encode("utf-8"),
                timeout=self.timeout,
            )
            if status == 0:
                return "", _osdb_network_error(error)
            if status in (401, 403):
                # A stale token is worth one re-login; a rejected account is
                # not. The first retry after a fresh login settles the question.
                if attempt == 1:
                    self._token = None
                    token, login_error = self._login()
                    if login_error is not None or not token:
                        return "", login_error or OsdbError("bad-answer", "OpenSubtitles login returned no token")
                    self._token = token
                    continue
                return "", OsdbError("auth", "OpenSubtitles rejected the download (HTTP 401)")
            if status == 429:
                return "", OsdbError("rate", "OpenSubtitles rate limit reached (HTTP 429); re-run later")
            if status != 200:
                return "", OsdbError("http", f"the OpenSubtitles download request failed (HTTP {status})")
            try:
                payload = json.loads(body)
            except ValueError:
                return "", OsdbError("bad-answer", "OpenSubtitles returned an unreadable download answer")
            link = str(payload.get("link") or "") if isinstance(payload, dict) else ""
            if not link:
                return "", OsdbError("bad-answer", "OpenSubtitles returned no download link")
            return link, None
        raise AssertionError("unreachable: the retry loop above always returns")

    def fetch_subtitle(self, url: str) -> tuple[bytes | None, OsdbError | None]:
        """One temporary download URL, read with the same cap everything else gets."""
        status, body, error, _headers = osdb_http(
            url, method="GET", headers={"User-Agent": OSDB_USER_AGENT}, timeout=self.timeout
        )
        if status == 0:
            return None, _osdb_network_error(error)
        if status != 200:
            return None, OsdbError("http", f"the subtitle file could not be downloaded (HTTP {status})")
        if len(body) > OSDB_MAX_BODY_BYTES:
            return None, OsdbError("http", f"the downloaded file exceeds the {OSDB_MAX_BODY_BYTES // (1024 * 1024)} MiB safety limit")
        return body, None


def choose_download_candidates(entries: Sequence[dict[str, Any]]) -> list[tuple[int, int]]:
    """The ``(subtitle_id, file_id)`` pairs to try, best first, bounded.

    A non-SDH English SRT is what the movie's ``.eng.srt`` should be; among
    equal ones the more-downloaded file has more human verification behind
    it, and the id breaks the last tie deterministically.
    """
    ranked: list[tuple[int, int, int, int]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        subtitle_id = _nonnegative_int(entry.get("id"))
        hearing_impaired = 1 if entry.get("hearing_impaired") else 0
        downloads = _nonnegative_int(entry.get("downloads"))
        files = entry.get("files")
        if not isinstance(files, list):
            continue
        for file in files:
            if not isinstance(file, dict):
                continue
            file_id = _nonnegative_int(file.get("file_id"))
            if file_id:
                ranked.append((hearing_impaired, -downloads, subtitle_id, file_id))
    ranked.sort()
    return [(subtitle_id, file_id)
            for _hi, _downloads, subtitle_id, file_id in ranked[:OSDB_MAX_DOWNLOAD_CANDIDATES]]


# ---------------------------------------------------------------------------
# One image-only movie, end to end
# ---------------------------------------------------------------------------
@dataclass
class DownloadOutcome:
    """What one exact-hash download produced, and why if it produced nothing."""

    ok: bool = False
    detail: str = ""
    unavailable_reason: str = ""
    movie_hash: str = ""
    subtitle_id: int = 0
    file_id: int = 0
    cue_count: int = 0
    text: str = ""


@dataclass
class OpenSubtitlesRunState:
    """What the run has learned about the service, carried between movies.

    One login serves the whole run (it is the client's token cache), and the
    state stops further attempts when retrying cannot help: a rejected
    credential or a rate limit is the same answer for every remaining
    movie, and a network that has failed three times in a row is not going
    to recover by movie 30.
    """

    consecutive_network_failures: int = 0
    stopped: str = ""


def _note_service_failure(state: OpenSubtitlesRunState | None, error: OsdbError) -> None:
    if state is None:
        return
    if error.code in ("auth", "rate"):
        state.stopped = error.message
    elif error.code == "network":
        state.consecutive_network_failures += 1
        if state.consecutive_network_failures >= OSDB_MAX_CONSECUTIVE_NETWORK_FAILURES:
            state.stopped = (
                f"OpenSubtitles is unreachable "
                f"({state.consecutive_network_failures} consecutive connection failures)"
            )
    else:
        state.consecutive_network_failures = 0


def download_exact_hash_subtitle(
    video: Path,
    dest: Path,
    client: OpenSubtitlesClient | None,
    state: OpenSubtitlesRunState | None = None,
    *,
    min_cues: int = DEFAULT_EXTRACT_MIN_CUES,
    dry_run: bool = False,
    log_file: Path | None = None,
) -> DownloadOutcome:
    """Fetch ``dest`` from OpenSubtitles when this movie's hash matches exactly.

    Called only for a movie whose English subtitle tracks are all image-based
    - the one case extraction cannot serve. ``client`` is ``None`` when the
    fallback is disabled or unconfigured; ``state`` carries what earlier
    movies learned about the service (pass ``None`` for a one-off call).

    The pipeline: hash the movie, search the hash, rank the hits, and for
    each candidate in turn - log in (once), get the temporary URL, download,
    decode, re-render, and pass the same quality gate an extraction passes.
    Only a candidate that survives is published, create-only, and recorded
    in the provenance ledger.
    """
    if client is None:
        return DownloadOutcome(
            unavailable_reason="the OpenSubtitles download is disabled (--no-open-subtitles)")
    if state is not None and state.stopped:
        return DownloadOutcome(unavailable_reason=state.stopped)
    movie_hash = compute_movie_hash(video)
    if not movie_hash:
        return DownloadOutcome(
            unavailable_reason="could not read the movie to compute its OpenSubtitles hash")
    if not client.api_key:
        return DownloadOutcome(
            unavailable_reason=f"no OpenSubtitles API key is configured "
                               f"({OSDB_API_KEY_ENV} or --osdb-api-key)")
    if not dry_run and not client.configured:
        missing = " and ".join(
            name for name, value in (
                (OSDB_USERNAME_ENV, client.username), (OSDB_PASSWORD_ENV, client.password))
            if not value.strip())
        return DownloadOutcome(
            unavailable_reason=f"OpenSubtitles account credentials are incomplete "
                               f"(missing: {missing})")

    entries, search_error = client.search_by_hash(movie_hash)
    if search_error is not None:
        _note_service_failure(state, search_error)
        return DownloadOutcome(detail=search_error.message, movie_hash=movie_hash)
    if state is not None:
        state.consecutive_network_failures = 0
    if not entries:
        return DownloadOutcome(
            detail="OpenSubtitles has no exact-hash English SRT for this movie",
            movie_hash=movie_hash)

    if dry_run:
        subtitle_id, file_id = choose_download_candidates(entries)[0]
        return DownloadOutcome(
            ok=True,
            movie_hash=movie_hash,
            subtitle_id=subtitle_id,
            file_id=file_id,
            detail=(f"would download the exact-hash match from OpenSubtitles "
                    f"(subtitle {subtitle_id}, file {file_id}) -> {dest.name}"))

    last_error = "no downloadable file among the exact-hash matches"
    for subtitle_id, file_id in choose_download_candidates(entries):
        link, link_error = client.download_link(file_id)
        if link_error is not None:
            _note_service_failure(state, link_error)
            if state is not None and state.stopped:
                return DownloadOutcome(detail=state.stopped, movie_hash=movie_hash)
            last_error = link_error.message
            continue
        raw, fetch_error = client.fetch_subtitle(link)
        if raw is None:
            _note_service_failure(state, fetch_error)
            if state is not None and state.stopped:
                return DownloadOutcome(detail=state.stopped, movie_hash=movie_hash)
            last_error = fetch_error.message
            continue
        try:
            decoded = decode_subtitle_bytes(raw)
        except ValueError as exc:
            last_error = f"the downloaded file is not readable subtitle text ({exc})"
            continue
        decoded = normalize_srt_newlines(decoded)
        if decoded.startswith("\ufeff"):
            decoded = decoded[1:]
        candidate = normalize_extracted_srt(decoded)
        good, reason = english_subtitle_quality(candidate, min_cues=min_cues)
        if not good:
            last_error = f"the exact-hash match failed the quality gate ({reason})"
            continue

        cue_count = len(parse_srt_cues(candidate))
        try:
            # Create-only, exactly like an extraction: a sidecar that appears
            # while the download is in flight is preserved, never overwritten.
            atomic_write_text(dest, candidate, replace=False)
        except FileExistsError:
            return DownloadOutcome(
                ok=True,
                movie_hash=movie_hash,
                subtitle_id=subtitle_id,
                file_id=file_id,
                cue_count=cue_count,
                detail=f"{dest.name} appeared during download; the existing sidecar was kept")
        except OSError as exc:
            return DownloadOutcome(
                detail=f"could not write the downloaded sidecar ({exc})",
                movie_hash=movie_hash)

        record_extracted_sidecar(
            video,
            dest,
            track=None,
            method="download",
            cue_count=cue_count,
            sha256=sha256_text(candidate),
            download={
                "provider": "opensubtitles",
                "movie_hash": movie_hash,
                "subtitle_id": subtitle_id,
                "file_id": file_id,
            },
        )
        log(
            f"Downloaded {cue_count} cue(s) from OpenSubtitles "
            f"(exact hash match {movie_hash[:12]}..., file {file_id}) -> {dest.name}",
            log_file=log_file,
        )
        return DownloadOutcome(
            ok=True,
            movie_hash=movie_hash,
            subtitle_id=subtitle_id,
            file_id=file_id,
            cue_count=cue_count,
            text=candidate,
            detail=(f"downloaded from OpenSubtitles (exact hash match, {cue_count} cues) "
                    f"-> {dest.name}"),
        )
    return DownloadOutcome(detail=last_error, movie_hash=movie_hash)


def open_subtitles_config_status() -> tuple[bool, str]:
    """Whether the exact-hash download can run on this machine at all.

    The doctor asks this before it prints anything, so the answer reflects
    the same resolution as a real run: an exported variable, or a ``.env``
    beside the scripts. The note names what is missing - never a value,
    because doctor output is what people paste into bug reports.
    """
    load_dotenv()
    key = os.environ.get(OSDB_API_KEY_ENV, "").strip()
    username = os.environ.get(OSDB_USERNAME_ENV, "").strip()
    password = os.environ.get(OSDB_PASSWORD_ENV, "").strip()
    if key and username and password:
        return True, ""
    if not key:
        return False, (f"no OpenSubtitles API key is configured ({OSDB_API_KEY_ENV} or --osdb-api-key)")
    missing = " and ".join(
        name for name, value in ((OSDB_USERNAME_ENV, username), (OSDB_PASSWORD_ENV, password))
        if not value
    )
    return False, f"OpenSubtitles credentials are incomplete (missing: {missing})"


# ---------------------------------------------------------------------------
# Durable record of extracted sidecars (the provenance ledger)
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


# Extraction runs worker threads, and every one of them load-mutate-writes
# this one JSON file. The lock serialises the write path only (reads stay
# lock-free): the ledger is supposed to be the durable answer to "was this
# sidecar extracted?", so it must not lose records to interleaving.
_LEDGER_WRITE_LOCK = threading.Lock()


def load_extracted_ledger(path: Path | None = None) -> dict[str, Any]:
    """Read the extraction record; a missing or damaged file is an empty one.

    When the current ledger does not exist yet, the legacy
    ``subtitle_fetcher_extracted.json`` is read instead: those provenance
    records are still true, and honoring them means a library cut over from
    the fetching era keeps the provenance it already had.
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
    track: EmbeddedSubtitleTrack | None,
    method: str,
    cue_count: int,
    sha256: str,
    download: dict[str, Any] | None = None,
    path: Path | None = None,
) -> bool:
    """Remember that ``sidecar`` was written by this tool.

    This is the provenance record: which movie, which embedded track (or,
    for a download, which OpenSubtitles file), which method, which bytes and
    when. ``method`` is ``"text"`` for an extraction and ``"download"`` for
    an exact-hash OpenSubtitles fetch; a download passes ``track=None`` and
    the ``download`` metadata instead. Best effort by design - a read-only
    installation loses only the record, never the subtitle itself.
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
        record: dict[str, Any] = {
            "movie": str(video),
            "sidecar": str(sidecar),
            "movie_size": movie_size,
            "movie_mtime_ns": movie_mtime,
            "sha256": sha256,
            "track_id": track.track_id if track is not None else None,
            "codec_id": track.codec_id if track is not None else None,
            "track_name": track.name if track is not None else None,
            "language": track.language if track is not None else EXTERNAL_SRT_LANG,
            "method": method,
            "cue_count": cue_count,
            "extracted_utc": utc_timestamp(),
        }
        if download is not None:
            record["download"] = dict(download)
        payload["sidecars"][path_norm(sidecar)] = record
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
    no provenance and is reported as such.
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
    """Knobs for one extraction attempt (mirrors the CLI flags)."""

    enabled: bool = True
    mkvmerge_bin: str | None = None
    mkvextract_bin: str | None = None
    extract_timeout_seconds: float = DEFAULT_MKVEXTRACT_TIMEOUT_SEC
    min_cues: int = DEFAULT_EXTRACT_MIN_CUES
    text_candidate_limit: int = DEFAULT_EXTRACT_TEXT_CANDIDATE_LIMIT
    dry_run: bool = False


@dataclass
class ExtractionOutcome:
    """What one extraction attempt produced, and why if it produced nothing."""

    ok: bool = False
    detail: str = ""
    unavailable_reason: str = ""
    track: EmbeddedSubtitleTrack | None = None
    method: str = ""  # "text"
    cue_count: int = 0
    dest: Path | None = None
    text: str = ""
    attempted: int = 0
    rejected: tuple[str, ...] = ()
    image_only: bool = False

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
    """Create ``dest`` from the movie's own embedded English text track.

    Returns an :class:`ExtractionOutcome`. ``ok`` means ``dest`` holds a
    validated external English SRT (or, in a dry run, that it would).
    ``image_only`` means the movie's English subtitle tracks are all
    image-based - extraction has nothing to read, and the caller may fall
    back to the OpenSubtitles exact-hash download. ``unavailable_reason``
    means extraction could not even be attempted here (no MKVToolNix, no
    English track at all, or an unreadable container) and names the fix.
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

        attempts: list[str] = []
        attempted = 0
        text_candidates = [item for item in candidates if item.kind == "text"][: max(0, opts.text_candidate_limit)]
        image_candidates = [item for item in candidates if item.kind == "image"]

        if not text_candidates:
            # Image-only (PGS/VobSub/DVB): this tool does not OCR, and will
            # not. The verdict hands the movie to the OpenSubtitles
            # exact-hash download, which is the only other source a
            # movie-only release has a trustworthy English subtitle from.
            return ExtractionOutcome(
                ok=False,
                image_only=bool(image_candidates),
                detail=("the movie's English subtitle tracks are image-based (PGS/VobSub/DVB) "
                        "and are not OCR'd"
                        if image_candidates
                        else "the movie has no embedded English text subtitle track"),
                attempted=0,
                unavailable_reason="" if image_candidates
                else "the movie has no embedded English text subtitle track",
            )

        for track in text_candidates:
            attempted += 1
            outcome = _extract_one_track(
                video, source, dest, track, tmp, opts, log_file=log_file
            )
            if outcome.ok:
                return ExtractionOutcome(
                    ok=True,
                    detail=(f"would extract the embedded {track.label} -> {dest.name}"
                            if opts.dry_run else outcome.detail),
                    track=outcome.track,
                    method=outcome.method,
                    cue_count=outcome.cue_count,
                    dest=dest,
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
    log_file: Path | None = None,
) -> ExtractionOutcome:
    """Extract one text track, convert it, validate it, and publish it as ``dest``.

    ``video`` is the movie the sidecar belongs to (recorded in the provenance
    ledger); ``source`` is the file mkvextract reads - the movie itself, or
    the temporary MKV bridge built for a non-Matroska container.
    """
    mkvextract_bin = find_mkvtoolnix_binary("mkvextract", opts.mkvextract_bin)
    if not mkvextract_bin:
        return ExtractionOutcome(detail="mkvextract is not installed")
    if track.kind != "text":
        # Image tracks are recognised by the caller (they set image_only) and
        # never extracted here: this function is the text path only.
        return ExtractionOutcome(detail="image tracks are not extracted (not OCR'd)")
    staged = tmp / f"track{track.track_id}{track.extension}"
    rc, _out, err = run_external_command(
        [mkvextract_bin, "tracks", str(source), f"{track.track_id}:{staged}"],
        timeout=opts.extract_timeout_seconds,
    )
    if rc != 0:
        return ExtractionOutcome(detail=f"mkvextract failed (exit {rc}): {_command_tail(err)}")

    method = "text"
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

    good, reason = english_subtitle_quality(produced_text, min_cues=opts.min_cues)
    if not good:
        return ExtractionOutcome(detail=reason, track=track, method=method)

    cue_count = len(parse_srt_cues(produced_text))
    if opts.dry_run:
        return ExtractionOutcome(
            ok=True,
            detail=f"embedded {track.label} converts to {cue_count} cues",
            track=track,
            method=method,
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
    )
    log(
        f"Extracted {cue_count} cue(s) from the embedded {track.label} -> {dest.name}",
        log_file=log_file,
    )
    return ExtractionOutcome(
        ok=True,
        detail=f"extracted {cue_count} cue(s) from the embedded {track.label}",
        track=track,
        method=method,
        cue_count=cue_count,
        dest=dest,
        text=produced_text,
    )


@dataclass
class ExtractorConfig:
    """Every knob of one extraction pass."""
    library: Path
    log_file: Path | None
    report_file: Path
    min_movie_size_mb: float = MIN_MOVIE_SIZE_MB
    lock_timeout_seconds: float = 60.0
    dry_run: bool = False
    limit: int = 0
    extract_min_cues: int = DEFAULT_EXTRACT_MIN_CUES
    # The exact-hash OpenSubtitles download, for image-only movies. All three
    # must be present for a download to run; a missing key still lets a dry
    # run report what it would search for, and --no-open-subtitles disables
    # the fallback entirely (text extraction is unaffected either way).
    open_subtitles_enabled: bool = True
    osdb_api_key: str = ""
    osdb_username: str = ""
    osdb_password: str = ""
    osdb_timeout_seconds: float = OSDB_HTTP_TIMEOUT_SEC
    # Workers for the local pre-flight only (layout, existing sidecars,
    # identity). 0 = decide from the CPU count. Extraction, the download and
    # every write stay on the single main thread whatever this says.
    workers: int = 0

    def extract_options(self, *, dry_run: bool = False) -> ExtractOptions:
        return ExtractOptions(
            enabled=True,
            min_cues=self.extract_min_cues,
            dry_run=dry_run,
        )

    def open_subtitles_client(self) -> OpenSubtitlesClient | None:
        """This run's download session, or ``None`` when the fallback is off.

        A session with only an API key can still search - which is exactly
        what a dry run wants to report - so incompleteness is not a
        construction error; ``download_exact_hash_subtitle`` explains it for
        the run that would actually log in.
        """
        if not self.open_subtitles_enabled:
            return None
        return OpenSubtitlesClient(
            api_key=self.osdb_api_key,
            username=self.osdb_username,
            password=self.osdb_password,
            timeout=self.osdb_timeout_seconds,
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
    extracted or downloaded this run. A dry run counts what it would have
    done, because the number it prints is a forecast of the real run.
    """
    return sum(
        1 for result in results
        if result.reason in (REASON_COVERED, REASON_EXTRACTED, REASON_DOWNLOADED)
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
       movie is reported as covered and nothing is extracted or rewritten.
       An existing sidecar is authoritative;
    2. otherwise the movie's own embedded English *text* track is extracted
       into the canonical sidecar (mkvextract, MP4 through the temporary
       bridge);
    3. a movie whose only English tracks are image-based gets the
       OpenSubtitles exact-hash download instead (when configured);
    4. a movie with no usable embedded text track and no exact-hash download
       is reported as needing attention.
    """
    results: list[JobResult] = []
    # One session serves every image-only movie in the run: one login, one
    # view of what the service is willing to answer.
    osdb_client = cfg.open_subtitles_client()
    osdb_state = OpenSubtitlesRunState()
    osdb_notes: set[str] = set()
    downloaded_count = 0
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
            # validated .eng.srt is not touched. No extraction attempt,
            # nothing.
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
            cfg.extract_options(dry_run=cfg.dry_run),
            log_file=cfg.log_file,
        )
        if outcome.ok:
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

        # Extraction produced nothing. A movie whose English tracks are all
        # image-based is the one case with a second source: the OpenSubtitles
        # exact-hash download, tried here and nowhere else.
        download_detail = ""
        if outcome.image_only and osdb_client is not None:
            download = download_exact_hash_subtitle(
                video,
                extract_dest,
                osdb_client,
                osdb_state,
                min_cues=cfg.extract_min_cues,
                dry_run=cfg.dry_run,
                log_file=cfg.log_file,
            )
            if download.ok:
                if cfg.dry_run:
                    result = JobResult(
                        video, "dry-run", f"{outcome.detail}; {download.detail}",
                        extract_dest, reason=REASON_DRY_RUN)
                else:
                    downloaded_count += 1
                    result = JobResult(
                        video, "downloaded", download.detail,
                        extract_dest, reason=REASON_DOWNLOADED)
                results.append(result)
                emit(index, "DRYRUN" if cfg.dry_run else "DOWNLOAD", video, download.detail)
                continue
            download_detail = download.unavailable_reason or download.detail
            if download.unavailable_reason and download.unavailable_reason not in osdb_notes:
                osdb_notes.add(download.unavailable_reason)
                log(download.unavailable_reason, level="WARNING", log_file=cfg.log_file)

        # Nothing covered this movie. Distinguish "nothing to extract"
        # (the healthy common case) from per-track failures, and surface a
        # missing toolchain once per run rather than once per movie.
        detail = outcome.unavailable_reason or outcome.detail or "no usable embedded English subtitle track"
        if download_detail:
            detail = f"{detail}; {download_detail}"
        if outcome.unavailable_reason and outcome.unavailable_reason not in extract_notes:
            extract_notes.add(outcome.unavailable_reason)
            if "not installed" in outcome.unavailable_reason:
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
        "downloaded_from_opensubtitles": downloaded_count,
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
        "NO EMBEDDED TEXT TRACK (AND NO EXACT-HASH MATCH)",
        "add an .eng.srt yourself",
        "This movie has no external English subtitle and no embedded text track this "
        "tool can convert. Image-only movies (PGS/VobSub) are downloaded from "
        "OpenSubtitles when the movie's hash matches an uploaded English SRT exactly "
        "and an account is configured (OPENSUBTITLES_API_KEY/USERNAME/PASSWORD); the "
        "movies left here have no usable text track and no such match - the log "
        "carries the per-movie reason. The fix is a human one: find the subtitle and "
        "place it beside the movie.",
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
) -> tuple[dict[str, list[tuple[Path, str]]], list[JobResult], list[JobResult], list[JobResult], list[JobResult]]:
    """Split one run into (needs buckets, covered, extracted, downloaded, dry-run)."""
    buckets: dict[str, list[tuple[Path, str]]] = {bucket.reason: [] for bucket in NEEDS_SUBTITLE_BUCKETS}
    covered: list[JobResult] = []
    dry_run: list[JobResult] = []
    extracted: list[JobResult] = []
    downloaded: list[JobResult] = []
    for result in results:
        if result.reason == REASON_COVERED:
            covered.append(result)
        elif result.reason == REASON_EXTRACTED:
            extracted.append(result)
        elif result.reason == REASON_DOWNLOADED:
            downloaded.append(result)
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
    downloaded.sort(key=lambda item: str(item.video).casefold())
    return buckets, covered, extracted, downloaded, dry_run

def build_report(results: Sequence[JobResult], cfg: ExtractorConfig, summary: dict[str, Any]) -> str:
    """Render the whole run as one report a human can act on in ten seconds."""
    buckets, covered, extracted, downloaded, dry_run = group_results(results)
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
        (len(covered), "Already have .eng.srt", "authoritative; never re-extracted"),
        (len(extracted), "Extracted this run", f"written as <movie>{EXTERNAL_SRT_SUFFIX}"),
        (len(downloaded), "Downloaded this run (exact hash match)",
         f"OpenSubtitles, written as <movie>{EXTERNAL_SRT_SUFFIX}"),
    ]
    if dry_run or cfg.dry_run:
        rows.append((len(dry_run), "Dry-run sidecars", "no files were written"))
    rows.append((needs, "NEED ATTENTION", "no sidecar, no extractable text track, no exact-hash match"))
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
            "These movies have no external English subtitle, no embedded text track this "
            "tool could extract and - where OpenSubtitles was consulted - no exact-hash "
            "match either. Each one is a human decision: place the subtitle yourself. "
            "Groups are ordered cheapest fix first."
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
                "come from the container's own timeline, so no offline timing correction "
                "is needed. mkv_track_cleaner.py then strips every embedded subtitle, "
                "leaving this sidecar as the sole subtitle option."
            ),
        )
        report.entries(
            [{"text": movie_label(result.video, cfg.library), "detail": result.detail}
             for result in extracted],
        )

    if downloaded:
        report.section(
            "DOWNLOADED FROM OPENSUBTITLES (EXACT HASH MATCH)",
            count=len(downloaded),
            total=total,
            intro=(
                "These movies' only English subtitle tracks were image-based, so the "
                "sidecar came from OpenSubtitles: the movie's hash matched an uploaded "
                "English SRT exactly, which makes it a subtitle for this exact release. "
                "Each download passed the same quality gate an extraction passes before "
                f"it was written as the canonical <movie>{EXTERNAL_SRT_SUFFIX}."
            ),
        )
        report.entries(
            [{"text": movie_label(result.video, cfg.library), "detail": result.detail}
             for result in downloaded],
        )

    if dry_run:
        report.section(
            "DRY-RUN SIDECARS (NOTHING WAS WRITTEN)",
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
            "sidecar is authoritative and is never re-extracted or rewritten."
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
    """One banner line saying what this run can do on this machine.

    The run has to say up front whether the movies' tracks are usable here -
    otherwise an image-only library looks broken when it is really a missing
    OpenSubtitles configuration, and a text library looks broken when it is
    really a missing MKVToolNix.
    """
    if not find_mkvtoolnix_binary("mkvmerge") or not find_mkvtoolnix_binary("mkvextract"):
        return f"unavailable: {MKVTOOLNIX_INSTALL_HINT}"
    text_part = f"text tracks (SRT/SSA/ASS) with mkvextract (>= {cfg.extract_min_cues} cues)"
    if not cfg.open_subtitles_enabled:
        image_part = "image-only movies: OpenSubtitles download disabled"
    elif cfg.osdb_api_key.strip() and cfg.osdb_username.strip() and cfg.osdb_password.strip():
        image_part = "image-only movies: OpenSubtitles exact-hash download (configured)"
    else:
        image_part = (f"image-only movies: OpenSubtitles not configured "
                      f"({OSDB_API_KEY_ENV}/{OSDB_USERNAME_ENV}/{OSDB_PASSWORD_ENV} or the --osdb-* flags)")
    return f"{text_part}; {image_part}; MP4s read through a temporary MKV bridge"



# =============================================================================
# COMPACT ROOT-LEVEL DRIVER
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract one validated external English SRT per movie from its own "
            "embedded text subtitle track, and for a movie whose only subtitle "
            "tracks are image-based, download the OpenSubtitles English SRT that "
            "matches the movie's hash exactly. An existing .eng.srt beside a "
            "movie is authoritative and is never re-extracted or rewritten. A "
            "movie with no usable text track and no exact-hash match is reported "
            "for manual attention."
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
                        help="Preview what would be written; no sidecar is written "
                             "(an MP4's temporary probe bridge is still built outside the library, "
                             "and an image-only movie's OpenSubtitles hash is still searched)")
    parser.add_argument("--extract-min-cues", type=int, default=DEFAULT_EXTRACT_MIN_CUES, metavar="N",
                        help="Reject a candidate subtitle with fewer than N cues as signs/songs-only "
                             "(applies to extractions and downloads alike)")
    parser.add_argument("--no-open-subtitles", action="store_true",
                        help="Disable the OpenSubtitles exact-hash download for image-only movies "
                             "(text extraction is unaffected)")
    parser.add_argument("--osdb-api-key", default=os.environ.get(OSDB_API_KEY_ENV, "").strip(),
                        metavar="KEY",
                        help="OpenSubtitles API key (from your profile page); from "
                             f"{OSDB_API_KEY_ENV} when not given")
    parser.add_argument("--osdb-username", default=os.environ.get(OSDB_USERNAME_ENV, "").strip(),
                        metavar="USER",
                        help="OpenSubtitles account username, needed for downloads; from "
                             f"{OSDB_USERNAME_ENV} when not given")
    parser.add_argument("--osdb-password", default=os.environ.get(OSDB_PASSWORD_ENV, "").strip(),
                        metavar="PASS",
                        help="OpenSubtitles account password, needed for downloads; from "
                             f"{OSDB_PASSWORD_ENV} when not given")
    parser.add_argument("--osdb-timeout", type=float, default=OSDB_HTTP_TIMEOUT_SEC, metavar="SEC",
                        help="Per-request time limit for the OpenSubtitles API")
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
        open_subtitles_enabled=not bool(args.no_open_subtitles),
        osdb_api_key=str(args.osdb_api_key).strip(),
        osdb_username=str(args.osdb_username).strip(),
        osdb_password=str(args.osdb_password).strip(),
        osdb_timeout_seconds=max(0.0, float(args.osdb_timeout)),
    )


def validate_config(cfg: ExtractorConfig) -> list[str]:
    errors: list[str] = []
    if not cfg.library.is_dir() or cfg.library.is_symlink():
        errors.append("--source must be an existing non-symlink movie-library directory")
    if cfg.extract_min_cues < 1:
        errors.append("--extract-min-cues must be at least 1")
    if cfg.osdb_timeout_seconds < 0:
        errors.append("--osdb-timeout must be zero (no limit) or greater")
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
            "JELLYFIN ENGLISH SRT EXTRACTOR",
            f"One validated external English {EXTERNAL_SRT_SUFFIX} per movie",
            [
                ("Mode", mode),
                ("Library", cfg.library),
                ("Policy", "Embedded text track first; image-only movies fall back to the "
                           "OpenSubtitles exact-hash download; an existing sidecar is "
                           "authoritative"),
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

    The extraction paths (probe, classify, convert, the MP4 bridge, the
    OpenSubtitles hash and the provenance ledger) are covered exhaustively in
    ``tests/``. The things worth re-checking on an unfamiliar machine are the
    sidecar contract, the provenance ledger and the OpenSubtitles hash,
    because a wrong one silently breaks the rest of the pipeline.
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

    def the_movie_hash_is_the_service_hash() -> bool:
        """The OpenSubtitles hash is MD5(body + 8-byte little-endian size).

        A regression here would make every exact-hash search miss, and the
        report would just say "no match" - so the shape is pinned here.
        """
        with tempfile.TemporaryDirectory(prefix="extractor_smoke_") as td:
            movie = Path(td) / "Movie (2020).mkv"
            body = b"subtitle-extractor-hash-smoke" * 17
            movie.write_bytes(body)
            first = compute_movie_hash(movie)
            second = compute_movie_hash(movie)
            expected = hashlib.md5(body + len(body).to_bytes(8, "little")).hexdigest()
            return first == second == expected and len(first) == 32

    def the_provenance_ledger_round_trips() -> bool:
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
            record = find_extracted_record(sidecar, sha, path=ledger)
            # The record must come back for the bytes that were written, and
            # must NOT come back for bytes somebody else replaced them with.
            return (isinstance(record, dict)
                    and record.get("sha256") == sha
                    and find_extracted_record(sidecar, sha256_text("edited"),
                                              path=ledger) is None)

    return run_field_smoke_test("subtitle_extractor.py", [
        ("a valid .eng.srt is accepted", a_real_srt_validates),
        ("an HTML error page is rejected", html_is_rejected),
        ("the sidecar path is canonical", the_sidecar_path_is_canonical),
        ("the OpenSubtitles movie hash matches the spec", the_movie_hash_is_the_service_hash),
        ("the provenance ledger round-trips", the_provenance_ledger_round_trips),
    ])

if __name__ == "__main__":
    raise SystemExit(main())
