#!/usr/bin/env python3
"""
Embedded-Subtitle Extractor for Jellyfin Movies
===============================================
After ``movie_standardizer.py`` and before ``mkv_track_cleaner.py``: walk the
canonical movie library and create at most one validated external English SRT
sidecar per movie, written as ``<Movie> (<Year>).eng.srt`` **right beside the
movie's own ``.mkv``** in its movie folder.

The source of the subtitle depends on what the movie itself carries, and the
order is not negotiable:

* a movie that already has an ``.eng.srt`` beside it is left completely
  alone - no extraction, no download, no rewriting. An existing sidecar is
  authoritative;
* **text-based embedded English tracks come first.** An SRT/SSA/ASS/WebVTT/USF
  track is extracted with ``mkvextract`` (through a temporary subtitle-only MKV
  bridge for MP4 movies) and converted in-process. It is exact for this release,
  it costs nothing, and its cues carry the container's own timeline;
* **image-based subtitles are not OCR'd any more.** OCR of PGS/VobSub bitmaps
  was removed: it was slow, it needed a fourth external program, and its output
  was frequently near-miss text that looked like success. When the *only*
  English subtitle a movie carries is image-based, the movie is looked up on
  OpenSubtitles **by its exact file hash** (the moviehash of the file on disk,
  not a title guess) and a matching English ``.srt`` is downloaded and written
  as the sidecar. An exact hash match is the provider's own guarantee that the
  subtitle belongs to this precise release;
* a movie with no usable embedded track, no image track to justify a hash
  lookup, or no matching subtitle on OpenSubtitles is listed in the report as
  needing attention - a human decision, not a guess.

A movie with no subtitle track at all is *not* looked up by title: downloading
"the most popular subtitle called Inception" is how a library ends up with the
wrong cut's subtitle, which is the failure this toolkit exists to avoid. The
hash lookup is offered only where the movie's own tracks prove it has English
subtitles, just not in text form.

``mkv_track_cleaner.py`` runs after this tool and strips every embedded
subtitle (the sidecar becomes the sole subtitle option), which is why
extraction and the hash lookup must both happen first: a remux rewrites the
container bytes and invalidates the moviehash.

    py -3 subtitle_extractor.py --dry-run
    py -3 subtitle_extractor.py
    py -3 subtitle_extractor.py --self-test

The default policy intentionally writes UTF-8 SRT sidecars only. SRT is the
most broadly direct-play-safe external subtitle choice across Jellyfin
clients; ASS/SSA, VobSub, PGS, and other formats are never written here.

Standard library only. The external programs this tool drives are
``mkvmerge``/``mkvextract`` (MKVToolNix); OpenSubtitles is reached over
``urllib`` with a free API key (``OPENSUBTITLES_API_KEY``), and an account
(``OPENSUBTITLES_USERNAME`` / ``OPENSUBTITLES_PASSWORD``) raises the provider's
daily download quota. Without a key the tool is completely offline: every
movie is still covered by its own text tracks, and image-only movies are
reported for a human.
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
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

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

__version__ = "5.0.0"

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

# ---------------------------------------------------------------------------
# OpenSubtitles (the image-only tier)
# ---------------------------------------------------------------------------
# The one job this provider has here: given the *exact* moviehash of the file
# on disk, hand back an English subtitle the provider itself reports as
# matching that hash. There is no title, year, release-name or "most
# downloaded" search anywhere in this tool - a movie whose own tracks prove it
# carries English subtitles (just as bitmaps) is matched by file identity, and
# anything else is a human decision.
#
# Facts the client below is written against (OpenSubtitles REST API v1):
#   * every request needs ``Api-Key`` and a descriptive ``User-Agent``;
#     ``/download`` additionally needs ``Authorization: Bearer <token>`` from
#     ``POST /login`` (username + password);
#   * ``GET /subtitles`` takes ``moviehash`` (exactly 16 hex chars) plus
#     ``languages``, and ``moviehash_match=only`` returns *only* subtitles that
#     matched the hash; a matched entry carries ``attributes.moviehash_match``;
#   * ``POST /download {"file_id": N}`` returns a temporary ``link`` (about
#     three hours), and *that* call - not fetching the link - consumes one
#     download from the account's daily quota (5/day anonymously per IP, 20/day
#     with a free account, 1000/day for VIP);
#   * requests are limited to 5 per second per IP, and ``429`` means back off.
# The tool makes one search and at most one download per movie, serially, so
# the rate limit is not reachable by design.
OPENSUBTITLES_API_URL = "https://api.opensubtitles.com/api/v1"
OPENSUBTITLES_DOWNLOAD_HOSTS: tuple[str, ...] = ("opensubtitles.com", "opensubtitles.org")
OPENSUBTITLES_APP_NAME = "organizekit"
OPENSUBTITLES_API_KEY_ENV = "OPENSUBTITLES_API_KEY"
OPENSUBTITLES_USERNAME_ENV = "OPENSUBTITLES_USERNAME"
OPENSUBTITLES_PASSWORD_ENV = "OPENSUBTITLES_PASSWORD"
OPENSUBTITLES_TIMEOUT_SEC = 30.0
OPENSUBTITLES_MIN_FILE_BYTES = 2 * 65536
OPENSUBTITLES_HASH_BYTES = 65536
OPENSUBTITLES_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
OPENSUBTITLES_MASK64 = (1 << 64) - 1
OPENSUBTITLES_ANSWER_MAX_BYTES = 8 * 1024 * 1024
# The provider allows 5 requests per second per IP; a quarter-second floor
# between requests keeps this tool at 4/s even on a fast connection, and the
# 429 backoff below is the safety net rather than the primary plan.
OPENSUBTITLES_MIN_INTERVAL_SEC = 0.25
OPENSUBTITLES_MAX_ATTEMPTS = 3
OPENSUBTITLES_MAX_BACKOFF_SEC = 30.0
OPENSUBTITLES_KEY_HINT = (
    "create a free API key at https://www.opensubtitles.com/en/consumers and set "
    "OPENSUBTITLES_API_KEY; an account (OPENSUBTITLES_USERNAME / "
    "OPENSUBTITLES_PASSWORD) raises the daily download limit"
)


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
# The four ways the image-only tier can end without a sidecar. They are kept
# apart because the fix is different in each case: configure a key, wait for a
# quota reset, place a subtitle by hand, or read the log.
REASON_IMAGE_ONLY = "image_only"
REASON_NO_HASH_MATCH = "no_hash_match"
REASON_QUOTA_SPENT = "quota_spent"
REASON_DOWNLOAD_FAILED = "download_failed"

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

@dataclass(frozen=True)
class LibraryScan:
    """The movie files a walk found, and what it left behind on the way.

    The counts are not decoration. `--min-size` and the sample-name rule are
    both silent filters, and a run that filtered the whole library down to
    nothing used to report "Nothing to do: every one of the 0 movie(s) in the
    library has a validated external English .eng.srt" - a green report about a
    library nothing had looked at. Carrying the counts out of the walk is what
    lets the report say which of the two happened.
    """

    videos: list[Path]
    below_min_size: int = 0
    sample_named: int = 0


def discover_videos(root: Path, min_bytes: int) -> LibraryScan:
    found: list[Path] = []
    below_min_size = 0
    sample_named = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d.strip().lower() not in DISC_DIR_NAMES
            and d.strip().lower() not in EXTRA_DIR_NAMES
            and not (Path(dirpath) / d).is_symlink()
        ]
        current = Path(dirpath)
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            path = current / name
            if path.is_symlink():
                continue
            if SAMPLE_NAME_RE.search(Path(name).stem):
                sample_named += 1
                continue
            try:
                if path.stat().st_size < min_bytes:
                    below_min_size += 1
                    continue
            except OSError:
                continue
            found.append(path)
    found.sort(key=lambda p: str(p).casefold())
    return LibraryScan(found, below_min_size, sample_named)

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
# the standard library. Image tracks (PGS/SUP, VobSub, DVB) are never
# converted: OCR was removed because a garbled transcription is worse than no
# subtitle at all. A movie whose only English subtitles are bitmaps is instead
# offered an exact-moviehash OpenSubtitles lookup (see the provider section
# below), and is reported for a human decision when that cannot help either.
#
# Extraction never rewrites or deletes the movie: mkvextract and the bridge
# builder only read it, and every temporary file lives outside the library.
# =============================================================================

# Embedded subtitle codecs this tool can turn into an external SRT. The value
# is the extension mkvextract must write. The image codecs below are listed so
# a movie that carries them can be recognised as image-only; nothing ever
# extracts them.
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
# movie's English subtitle would be a silent downgrade. The same floor is
# applied to a downloaded subtitle: language-tag mistakes happen on both sides.
DEFAULT_EXTRACT_MIN_CUES = 10
DEFAULT_DOWNLOAD_MIN_CUES = 10
# How many text candidates to try for one movie before giving up. Extraction is
# cheap and local, so three is generous; a movie whose every text track is
# refused is then treated as an image-only movie (or reported, if it has no
# image track either).
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

# Very common English function words. Their share of a real dialogue track is
# far above this floor; a wrong-language track (or machine nonsense) falls
# below it.
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
    streams outrank image streams (a conversion is free; a bitmap would need
    OCR, which this tool no longer does), and inside each class the container's
    default track wins before codec preference and track order.
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


def subtitle_quality(
    text: str, *, min_cues: int = DEFAULT_EXTRACT_MIN_CUES
) -> tuple[bool, str]:
    """Decide whether subtitle text may become the movie's English sidecar.

    One conservative gate for both sources, because both can look like success
    while being useless: a mis-tagged foreign embedded track, and an
    OpenSubtitles entry whose language field says ``en`` while its cues are
    something else. A subtitle from the movie's own track is authoritative
    about *timing* but not about *content*, and a hash-matched download is
    authoritative about *identity* but not about *language* - so both re-check
    the same things:

    * it is non-empty and no larger than the shared safety limit;
    * it parses into well-formed SRT cues, and there are enough of them to be
      a whole film rather than a signs/songs stream;
    * the cues are Latin-script and read like English dialogue.
    """
    if not text.strip():
        return False, "the subtitle contained no text"
    if len(text.encode("utf-8", errors="replace")) > MAX_SUBTITLE_BYTES:
        return False, f"the subtitle exceeds the {MAX_SUBTITLE_BYTES // (1024 * 1024)} MiB safety limit"
    if not looks_like_srt(text):
        return False, "the subtitle did not convert to valid SRT cues"
    cues = parse_srt_cues(text)
    if len(cues) < min_cues:
        return (
            False,
            f"only {len(cues)} cue(s); a complete movie subtitle needs "
            f"at least {min_cues} (this looks like a signs/songs-only stream)",
        )
    sample = " ".join(cue[2] for cue in cues)
    if non_latin_ratio(sample) > 0.40:
        return False, "the text is not Latin-script (this is not an English subtitle)"
    words = re.findall(r"[A-Za-z']+", sample)
    if len(words) >= 100:
        hits = sum(1 for word in words if word.lower() in ENGLISH_STOPWORDS)
        if hits / len(words) < 0.04:
            return False, "the text does not read as English (a foreign track or nonsense)"
    return True, ""


# ---------------------------------------------------------------------------
# OpenSubtitles: the exact-moviehash lookup for image-only movies
# ---------------------------------------------------------------------------
# Everything below is the one provider tier this tool has. It is reached only
# for a movie whose own tracks prove it carries English subtitles and whose
# only English subtitles are bitmaps; with no API key configured it is inert
# and the movie is reported for a human instead.
#
# The lookup is deliberately identity-based. ``moviehash`` is calculated from
# the bytes of the file on disk - the release, not the title - and
# ``moviehash_match=only`` makes the provider return nothing but subtitles it
# itself matched to that hash. There is no title search, no "most downloaded"
# pick and no release-name guess anywhere here: those are how a library ends up
# with the wrong cut's subtitle, which is the failure mode this toolkit exists
# to avoid.


def _json_document(payload: bytes, where: str) -> dict[str, Any]:
    """Parse an API answer that must be a JSON object, or say why it is not."""
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        document = json.loads(text) if text else {}
    except ValueError as exc:
        raise OpenSubtitlesError(f"{where} did not answer with JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise OpenSubtitlesError(
            f"{where} answered with a JSON {type(document).__name__}, not an object"
        )
    return document


class OpenSubtitlesError(RuntimeError):
    """Any failed provider interaction: transport, HTTP status, or a bad answer."""


def _json_flag(value: Any) -> bool:
    """Read a JSON boolean that a provider might spell as a word."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _checksum_chunk(chunk: bytes) -> int:
    """Sum a 64 KiB chunk as little-endian unsigned 64-bit values."""
    total = 0
    for (value,) in struct.iter_unpack("<Q", chunk):
        total = (total + value) & OPENSUBTITLES_MASK64
    return total


def moviehash_of_file(path: Path) -> tuple[str, int]:
    """The OpenSubtitles moviehash of a local movie file, and its size.

    ``hash = filesize + sum_uint64_le(first 64 KiB) + sum_uint64_le(last 64 KiB)``
    (Media Player Classic's algorithm, wrapping at 64 bits), rendered as the 16
    lowercase hex characters the API requires. The file is streamed, never read
    whole: hashing a 40 GB remux costs two 64 KiB reads.

    Raises ``OSError`` when the file cannot be read and ``ValueError`` when it
    cannot carry a hash at all - OpenSubtitles' own floor is 128 KiB, and a
    file that changed size under the reads is refused rather than hashed into a
    number that matches nothing.
    """
    try:
        size = int(path.stat().st_size)
    except OSError as exc:
        raise OSError(f"could not read the movie's size ({exc})") from exc
    if size < OPENSUBTITLES_MIN_FILE_BYTES:
        raise ValueError(
            f"the movie is {size} bytes; OpenSubtitles hashes need at least "
            f"{OPENSUBTITLES_MIN_FILE_BYTES}"
        )
    try:
        with path.open("rb") as handle:
            head = handle.read(OPENSUBTITLES_HASH_BYTES)
            handle.seek(size - OPENSUBTITLES_HASH_BYTES)
            tail = handle.read(OPENSUBTITLES_HASH_BYTES)
    except OSError as exc:
        raise OSError(f"could not read the movie ({exc})") from exc
    if len(head) != OPENSUBTITLES_HASH_BYTES or len(tail) != OPENSUBTITLES_HASH_BYTES:
        raise ValueError("the movie changed size while it was being hashed")
    digest = (size + _checksum_chunk(head) + _checksum_chunk(tail)) & OPENSUBTITLES_MASK64
    return f"{digest:016x}", size


def opensubtitles_settings_from_env() -> tuple[str, str, str]:
    """The configured ``(api_key, username, password)`` for OpenSubtitles.

    A ``.env`` beside the scripts counts - it is loaded on the way in - and a
    real environment variable always wins over it. Never logged, never stored.
    """
    load_dotenv()
    return (
        (os.environ.get(OPENSUBTITLES_API_KEY_ENV) or "").strip(),
        (os.environ.get(OPENSUBTITLES_USERNAME_ENV) or "").strip(),
        os.environ.get(OPENSUBTITLES_PASSWORD_ENV) or "",
    )


def opensubtitles_user_agent() -> str:
    """The descriptive User-Agent the API requires (``AppName vX.Y.Z``)."""
    return f"{OPENSUBTITLES_APP_NAME} v{__version__}"


@dataclass(frozen=True)
class OpenSubtitlesCandidate:
    """One English subtitle the provider says belongs to this exact file."""

    file_id: int
    file_name: str
    subtitle_id: str = ""
    language: str = "en"
    release: str = ""
    download_count: int = 0
    hearing_impaired: bool = False
    foreign_parts_only: bool = False
    machine_translated: bool = False
    ai_translated: bool = False
    moviehash_match: bool = False
    votes: int = 0
    ratings: float = 0.0
    from_trusted: bool = False
    title: str = ""
    year: int = 0

    @property
    def label(self) -> str:
        """One line naming the release and why it was picked."""
        parts = [self.release or self.file_name or f"file_id {self.file_id}"]
        if self.hearing_impaired:
            parts.append("SDH")
        if self.year:
            parts.append(str(self.year))
        parts.append(f"{self.download_count} downloads")
        return ", ".join(parts)

    @property
    def is_english(self) -> bool:
        """The provider's language tag, which is a claim worth checking."""
        tags = re.split(r"[-_,;\s]+", self.language.strip().casefold())
        return any(tag in ENGLISH_LANGUAGE_TOKENS for tag in tags if tag)


@dataclass(frozen=True)
class OpenSubtitlesDownload:
    """What ``POST /download`` answered: a temporary link and the quota left."""

    link: str
    file_name: str = ""
    remaining: int = -1
    requests: int = -1
    reset_time: str = ""


def _candidate_from_entry(entry: Any) -> OpenSubtitlesCandidate | None:
    """Turn one ``data[]`` entry into a candidate, or ``None`` if unusable.

    Anything incomplete is dropped rather than repaired: a candidate without a
    positive ``file_id`` cannot be downloaded, and one whose entry is not a
    subtitle is not ours to interpret.
    """
    if not isinstance(entry, dict):
        return None
    if str(entry.get("type") or "subtitle").strip().lower() != "subtitle":
        return None
    attributes = entry.get("attributes")
    if not isinstance(attributes, dict):
        return None
    file_id = 0
    file_name = ""
    files = attributes.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            try:
                candidate_id = int(item.get("file_id"))
            except (TypeError, ValueError):
                continue
            if candidate_id > 0:
                # The first usable file, matching the API's own ``cd_number``
                # order; a multi-file entry is rare and only its first part is
                # ever written as the sidecar.
                file_id = candidate_id
                file_name = str(item.get("file_name") or "")
                break
    if file_id <= 0:
        return None
    feature = (
        attributes.get("feature_details")
        if isinstance(attributes.get("feature_details"), dict)
        else {}
    )
    return OpenSubtitlesCandidate(
        file_id=file_id,
        file_name=file_name,
        subtitle_id=str(attributes.get("subtitle_id") or entry.get("id") or ""),
        language=str(attributes.get("language") or ""),
        release=str(attributes.get("release") or ""),
        download_count=_nonnegative_int(attributes.get("download_count")),
        hearing_impaired=_json_flag(attributes.get("hearing_impaired")),
        foreign_parts_only=_json_flag(attributes.get("foreign_parts_only")),
        machine_translated=_json_flag(attributes.get("machine_translated")),
        ai_translated=_json_flag(attributes.get("ai_translated")),
        moviehash_match=(
            _json_flag(attributes.get("moviehash_match"))
            or _json_flag(attributes.get("movie_hash_match"))
        ),
        votes=_nonnegative_int(attributes.get("votes")),
        ratings=_nonnegative_float(attributes.get("ratings")),
        from_trusted=_json_flag(attributes.get("from_trusted")),
        title=str(feature.get("title") or feature.get("movie_name") or ""),
        year=_nonnegative_int(feature.get("year")),
    )


def candidates_from_search(document: dict[str, Any], where: str) -> list[OpenSubtitlesCandidate]:
    """Every usable candidate in a ``GET /subtitles`` answer."""
    data = document.get("data")
    if not isinstance(data, list):
        raise OpenSubtitlesError(f"{where} answered without a subtitle list")
    candidates: list[OpenSubtitlesCandidate] = []
    for entry in data:
        candidate = _candidate_from_entry(entry)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def choose_hash_match(
    candidates: Sequence[OpenSubtitlesCandidate],
) -> tuple[OpenSubtitlesCandidate | None, str]:
    """Pick the subtitle to install, or say why none qualifies.

    The hash is the identity check and is not negotiable: only a candidate the
    provider flagged as a moviehash match is eligible, so this can never
    install a subtitle for a different release. On top of that, an English
    subtitle that covers only the foreign-language parts (a "forced" stream) or
    that was produced by machine/AI translation is refused rather than
    installed as the movie's dialogue. Among the rest, a plain dialogue
    subtitle beats a hearing-impaired one, a trusted upload beats an untrusted
    one, and downloads break the tie.
    """
    if not candidates:
        return None, "OpenSubtitles listed no subtitle for this exact file hash"
    matched = [candidate for candidate in candidates if candidate.moviehash_match]
    if not matched:
        return None, "OpenSubtitles listed subtitles, but none matched this exact file hash"
    english = [candidate for candidate in matched if candidate.is_english]
    if not english:
        return None, "the hash-matched subtitles were not tagged English"
    complete = [
        candidate
        for candidate in english
        if not candidate.foreign_parts_only
        and not candidate.machine_translated
        and not candidate.ai_translated
    ]
    if not complete:
        return (
            None,
            "the only hash-matched English subtitles cover foreign parts only, "
            "or are machine/AI translations",
        )
    complete.sort(
        key=lambda candidate: (
            candidate.hearing_impaired,
            not candidate.from_trusted,
            -candidate.download_count,
            -candidate.votes,
            -candidate.ratings,
            candidate.file_id,
        )
    )
    return complete[0], ""


def _require_provider_link(link: str) -> None:
    """Refuse to read a download link that is not HTTPS on the provider's own host.

    The link comes from a third party and is about to be written to disk
    beside a movie, so it is checked before it is dereferenced: HTTPS only, and
    only a host under ``opensubtitles.com``/``opensubtitles.org``.
    """
    parts = urlsplit(link)
    if parts.scheme.lower() != "https":
        raise OpenSubtitlesError("OpenSubtitles offered a non-HTTPS download link; refusing to read it")
    host = (parts.hostname or "").lower()
    allowed = any(
        host == suffix or host.endswith(f".{suffix}") for suffix in OPENSUBTITLES_DOWNLOAD_HOSTS
    )
    if not allowed:
        raise OpenSubtitlesError(
            f"OpenSubtitles offered a download link on an unexpected host "
            f"({host or 'none'}); refusing to read it"
        )


class OpenSubtitlesClient:
    """The two API calls this tool needs, with no state outside the run.

    One search per movie, at most one download, all on the thread that already
    owns the ledger and the library. The client never retries a search (a
    second identical search cannot change the answer) and only re-logs-in once,
    when a ``401`` says the session token expired mid-run.
    """

    def __init__(
        self,
        api_key: str,
        *,
        username: str = "",
        password: str = "",
        user_agent: str = "",
        api_url: str = OPENSUBTITLES_API_URL,
        timeout_seconds: float = OPENSUBTITLES_TIMEOUT_SEC,
    ) -> None:
        self.api_key = api_key.strip()
        self.username = username.strip()
        self.password = password
        self.user_agent = user_agent.strip() or opensubtitles_user_agent()
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._token = ""
        self._last_request_at = 0.0

    @property
    def has_account(self) -> bool:
        return bool(self.username and self.password)

    def _throttle(self) -> None:
        """Keep a polite gap between requests (the provider allows 5/s per IP)."""
        gap = time.monotonic() - self._last_request_at
        if 0.0 <= gap < OPENSUBTITLES_MIN_INTERVAL_SEC:
            time.sleep(OPENSUBTITLES_MIN_INTERVAL_SEC - gap)

    @staticmethod
    def _backoff_seconds(exc: HTTPError, attempt: int) -> float:
        """How long to wait after a 429: the provider's own hint, else doubling."""
        for header in ("Retry-After", "X-RateLimit-Reset", "Ratelimit-Reset"):
            value = ""
            try:
                value = str(exc.headers.get(header) or "").strip()
            except AttributeError:
                value = ""
            if value.isdigit():
                seconds = float(value)
                if header == "Retry-After" or seconds > time.time():
                    return max(1.0, min(OPENSUBTITLES_MAX_BACKOFF_SEC, seconds))
        return min(OPENSUBTITLES_MAX_BACKOFF_SEC, 2.0 ** attempt)

    # -- one place where a request leaves this process ----------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        token: str = "",
    ) -> dict[str, Any]:
        url = f"{self.api_url}/{path.lstrip('/')}"
        if params:
            # Sorted, lowercase parameter names: the API's own guidance, and it
            # makes a request reproducible in a log without leaking anything.
            url = f"{url}?{urlencode(sorted(params.items()))}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Api-Key": self.api_key,
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        where = f"{method} {path}"
        attempt = 0
        while True:
            attempt += 1
            self._throttle()
            # Stamped when the request is *sent*, not when it answers: the
            # floor is a gap between requests, and a request the provider
            # rejects spends its rate budget exactly like one it serves.
            self._last_request_at = time.monotonic()
            try:
                with urlopen(Request(url, data=payload, headers=headers, method=method),
                             timeout=self.timeout_seconds) as response:
                    answer = response.read(OPENSUBTITLES_ANSWER_MAX_BYTES + 1)
            except HTTPError as exc:
                # A 429 is the provider's rate limiter rather than a real
                # failure, so the same request is retried after waiting; the
                # credentials endpoint is not special-cased because a 429 there
                # is also a rate limit. Everything else is reported once.
                if exc.code != 429:
                    raise OpenSubtitlesError(self._http_error(exc)) from exc
                if attempt >= OPENSUBTITLES_MAX_ATTEMPTS:
                    raise OpenSubtitlesError(
                        f"{where} kept answering HTTP 429 (rate limited)"
                    ) from exc
                time.sleep(self._backoff_seconds(exc, attempt))
                continue
            except (URLError, OSError, ValueError) as exc:
                raise OpenSubtitlesError(f"could not reach OpenSubtitles ({exc})") from exc
            if len(answer) > OPENSUBTITLES_ANSWER_MAX_BYTES:
                raise OpenSubtitlesError(f"{where} answered with an oversized document")
            return _json_document(answer, where)

    @staticmethod
    def _http_error(exc: HTTPError) -> str:
        """A status code plus the provider's own message, when it sent one."""
        detail = ""
        try:
            document = json.loads(exc.read(4096).decode("utf-8", errors="replace") or "{}")
            if isinstance(document, dict):
                detail = str(document.get("message") or document.get("error") or "").strip()
        except (OSError, ValueError):
            detail = ""
        if not detail:
            detail = str(getattr(exc, "reason", "") or "").strip()
        text = f"OpenSubtitles answered HTTP {exc.code}"
        return f"{text}: {detail}" if detail else text

    # -- login --------------------------------------------------------------
    def login(self) -> str:
        """Exchange the configured account for a session token (about 24 h)."""
        if not self.has_account:
            raise OpenSubtitlesError(
                "no OpenSubtitles account was configured; set "
                "OPENSUBTITLES_USERNAME and OPENSUBTITLES_PASSWORD to download subtitles"
            )
        document = self._request(
            "POST", "login", body={"username": self.username, "password": self.password}
        )
        token = str(document.get("token") or "").strip()
        if not token:
            raise OpenSubtitlesError("OpenSubtitles accepted the login without a token")
        self._token = token
        return token

    def session_token(self, *, refresh: bool = False) -> str:
        """One login per run; ``refresh`` is for a token that just expired."""
        if refresh or not self._token:
            return self.login()
        return self._token

    # -- the two calls ------------------------------------------------------
    def search_by_movie_hash(
        self, movie_hash: str, *, file_name: str = "", languages: str = "en"
    ) -> list[OpenSubtitlesCandidate]:
        """Ask for subtitles that match this exact file hash, English only.

        ``file_name`` is sent as the API's ``query`` parameter: the provider's
        own search guidance recommends including the filename alongside the
        hash for better matching, and a filename is not a title guess.
        ``moviehash_match=only`` is what keeps the answer to hash matches;
        ``choose_hash_match`` verifies the flag again before anything is
        downloaded, so a name-only result can never be installed.
        """
        if not OPENSUBTITLES_HASH_RE.match(movie_hash):
            raise OpenSubtitlesError(f"'{movie_hash}' is not a 16-character OpenSubtitles hash")
        params: dict[str, str] = {
            "languages": languages,
            "moviehash": movie_hash,
            "moviehash_match": "only",
        }
        if file_name.strip():
            params["query"] = file_name.strip()
        document = self._request("GET", "subtitles", params=params)
        return candidates_from_search(document, f"GET subtitles?moviehash={movie_hash}")

    def download_link(self, file_id: int) -> OpenSubtitlesDownload:
        """Reserve one download and return the temporary file URL."""
        body = {"file_id": int(file_id), "sub_format": "srt"}
        token = self.session_token() if self.has_account else ""
        try:
            document = self._request("POST", "download", body=body, token=token)
        except OpenSubtitlesError as exc:
            # A token is valid for about a day; a run that outlives one gets a
            # single fresh login, never a retry loop against the credentials
            # endpoint (which is rate-limited precisely because of that).
            if not (self.has_account and "HTTP 401" in str(exc)):
                raise
            document = self._request(
                "POST", "download", body=body, token=self.session_token(refresh=True)
            )
        link = str(document.get("link") or "").strip()
        if not link:
            raise OpenSubtitlesError("OpenSubtitles answered the download request without a link")
        return OpenSubtitlesDownload(
            link=link,
            file_name=str(document.get("file_name") or "").strip(),
            remaining=int(document.get("remaining", -1))
            if str(document.get("remaining", "")).strip() != ""
            else -1,
            requests=int(document.get("requests", -1))
            if str(document.get("requests", "")).strip() != ""
            else -1,
            reset_time=str(document.get("reset_time_utc") or document.get("reset_time") or "").strip(),
        )

    def probe(self) -> str:
        """One cheap authenticated call, for the doctor: does the key work?

        ``/infos/formats`` needs no account for this, and its answer is a short
        human note rather than parsed data - the doctor only needs to know the
        request left the machine and came back accepted.
        """
        document = self._request("GET", "infos/formats")
        formats = document.get("data")
        count = len(formats) if isinstance(formats, list) else 0
        return f"{count} subtitle format(s) offered" if count else "the API answered"

    def fetch(self, link: str) -> bytes:
        """Read the subtitle bytes from a temporary link, bounded and HTTPS-only."""
        _require_provider_link(link)
        try:
            with urlopen(
                Request(link, headers={"User-Agent": self.user_agent, "Accept": "*/*"}),
                timeout=self.timeout_seconds,
            ) as response:
                payload = response.read(MAX_SUBTITLE_BYTES + 1)
        except HTTPError as exc:
            raise OpenSubtitlesError(self._http_error(exc)) from exc
        except (URLError, OSError, ValueError) as exc:
            raise OpenSubtitlesError(f"could not download the subtitle ({exc})") from exc
        if len(payload) > MAX_SUBTITLE_BYTES:
            raise OpenSubtitlesError(
                f"the downloaded subtitle exceeds the {MAX_SUBTITLE_BYTES // (1024 * 1024)} MiB safety limit"
            )
        return payload


@dataclass
class DownloadOptions:
    """Knobs for one OpenSubtitles lookup (all of them optional)."""

    enabled: bool = True
    api_key: str = ""
    username: str = ""
    password: str = ""
    timeout_seconds: float = OPENSUBTITLES_TIMEOUT_SEC
    min_cues: int = DEFAULT_DOWNLOAD_MIN_CUES
    dry_run: bool = False

    def client(self) -> OpenSubtitlesClient:
        return OpenSubtitlesClient(
            self.api_key,
            username=self.username,
            password=self.password,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass
class DownloadOutcome:
    """What one exact-hash lookup produced, and why if it produced nothing."""

    ok: bool = False
    detail: str = ""
    unavailable_reason: str = ""
    reason: str = ""
    dest: Path | None = None
    candidate: OpenSubtitlesCandidate | None = None
    movie_hash: str = ""
    cue_count: int = 0
    remaining: int = -1
    reset_time: str = ""
    covered_by_other: bool = False

    @property
    def available(self) -> bool:
        """True when the lookup could be attempted at all."""
        return not self.unavailable_reason


def _folder_accepts_new_files(directory: Path) -> bool:
    """Can a sidecar still be created in ``directory``? Checked before any spend.

    A library folder can be unwritable for reasons that have nothing to do with
    the movie - a read-only mount, a permission fix gone wrong - and the daily
    download allowance is small enough that discovering it *after* a download
    wastes a real one. The check is deliberately a real write when the cheap
    answer is "no": ``os.access`` is unreliable for directories on some
    platforms, and this verdict decides whether to spend provider quota.
    """
    try:
        if os.access(directory, os.W_OK):
            return True
    except OSError:
        pass
    probe = directory / f".organize-write-probe.{os.getpid()}"
    try:
        with probe.open("x", encoding="utf-8"):
            pass
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def download_hash_matched_srt(
    video: Path,
    dest: Path,
    options: DownloadOptions | None = None,
    *,
    snapshot: VideoSnapshot | None = None,
    log_file: Path | None = None,
) -> DownloadOutcome:
    """Write ``dest`` from an OpenSubtitles subtitle matching this file's hash.

    The sequence is the safety story: hash the file that is on disk now, ask
    the provider for hash matches only, refuse anything that is not English
    dialogue, download, re-validate the bytes with the same gate an extracted
    track passes, re-check that the movie did not change while all that was
    happening, and only then publish create-only. Anything that fails leaves no
    file behind and one sentence explaining the fix.

    ``unavailable_reason`` means the lookup never started (no key, disabled,
    nothing hashable, sidecar already present); ``detail`` means it ran and did
    not end in a sidecar, with ``reason`` naming which of the report's buckets
    that belongs to.
    """
    opts = options or DownloadOptions()
    if not opts.enabled:
        return DownloadOutcome(
            unavailable_reason="OpenSubtitles lookups are disabled (--no-download)"
        )
    if dest.exists():
        return DownloadOutcome(
            ok=True,
            covered_by_other=True,
            dest=dest,
            detail=f"{dest.name} appeared during the run; the existing sidecar was kept",
        )
    if not opts.api_key:
        return DownloadOutcome(
            unavailable_reason=f"no OpenSubtitles API key is configured; {OPENSUBTITLES_KEY_HINT}"
        )
    if not _folder_accepts_new_files(dest.parent):
        # Refused before the search, not after the download: finding out by
        # failing to write would have spent one of the day's few downloads.
        return DownloadOutcome(
            unavailable_reason=(
                f"the movie folder is not writable ({dest.parent}); nothing was "
                "requested from OpenSubtitles"
            ),
            reason=REASON_DOWNLOAD_FAILED,
        )
    try:
        movie_hash, file_size = moviehash_of_file(video)
    except (OSError, ValueError) as exc:
        return DownloadOutcome(
            unavailable_reason=f"could not hash the movie for an OpenSubtitles lookup ({exc})"
        )
    if snapshot is not None and snapshot.size and snapshot.size != file_size:
        return DownloadOutcome(
            unavailable_reason="the movie changed size after it was inspected; re-run to hash it again"
        )
    client = opts.client()
    try:
        # The basename, not the movie's title: the provider recommends
        # including the filename with a hash search, and a filename is not a
        # guess about which release this is - the hash already is the identity.
        candidates = client.search_by_movie_hash(movie_hash, file_name=video.name)
    except OpenSubtitlesError as exc:
        # A spent allowance is refused by the search as readily as by the
        # download (the API answers 406 either way), and the run-wide
        # short-circuit depends on recognizing it here too - otherwise every
        # later image-only movie would ask again only to be refused again.
        reason = REASON_QUOTA_SPENT if _looks_like_quota_error(str(exc)) else REASON_DOWNLOAD_FAILED
        return DownloadOutcome(
            detail=f"the OpenSubtitles lookup failed: {exc}",
            reason=reason,
            movie_hash=movie_hash,
        )
    candidate, refusal = choose_hash_match(candidates)
    if candidate is None:
        return DownloadOutcome(
            detail=f"{refusal} (moviehash {movie_hash})",
            reason=REASON_NO_HASH_MATCH,
            movie_hash=movie_hash,
        )
    if opts.dry_run:
        # A search spends no quota; a download request does. So a preview can
        # say exactly which subtitle the real run would install.
        return DownloadOutcome(
            ok=True,
            detail=(
                f"would download \"{candidate.label}\" from OpenSubtitles as "
                f"{dest.name} (moviehash {movie_hash})"
            ),
            dest=dest,
            candidate=candidate,
            movie_hash=movie_hash,
        )
    try:
        download = client.download_link(candidate.file_id)
        payload = client.fetch(download.link)
    except OpenSubtitlesError as exc:
        reason = REASON_QUOTA_SPENT if _looks_like_quota_error(str(exc)) else REASON_DOWNLOAD_FAILED
        return DownloadOutcome(
            detail=f"the OpenSubtitles download failed: {exc}",
            reason=reason,
            movie_hash=movie_hash,
            candidate=candidate,
        )
    try:
        text = normalize_srt_newlines(decode_subtitle_bytes(payload))
    except (ValueError, OSError) as exc:
        return DownloadOutcome(
            detail=f"the downloaded subtitle is not readable text ({exc})",
            reason=REASON_DOWNLOAD_FAILED,
            movie_hash=movie_hash,
            candidate=candidate,
            remaining=download.remaining,
            reset_time=download.reset_time,
        )
    if text.startswith("\ufeff"):
        text = text[1:]
    cues = parse_srt_cues(text)
    good, reason_text = subtitle_quality(normalize_extracted_srt(text), min_cues=opts.min_cues)
    if not good:
        return DownloadOutcome(
            detail=f"the downloaded subtitle was refused: {reason_text}",
            reason=REASON_DOWNLOAD_FAILED,
            movie_hash=movie_hash,
            candidate=candidate,
            remaining=download.remaining,
            reset_time=download.reset_time,
        )
    produced_text = render_srt_cues(cues)
    cue_count = len(cues)
    if snapshot is not None:
        try:
            current: VideoSnapshot | None = video_snapshot(video)
        except OSError as exc:
            current = None
            changed = f"the movie could not be re-checked while its subtitle was being downloaded ({exc})"
        else:
            changed = "the movie changed while its subtitle was being downloaded"
        if current is None or current != snapshot:
            return DownloadOutcome(
                detail=f"{changed}; nothing was written",
                reason=REASON_DOWNLOAD_FAILED,
                movie_hash=movie_hash,
                candidate=candidate,
                remaining=download.remaining,
                reset_time=download.reset_time,
            )
    try:
        # Create-only, exactly like an extracted sidecar: a subtitle that
        # appears while this movie is being processed is preserved, never
        # silently overwritten.
        atomic_write_text(dest, produced_text, replace=False)
    except FileExistsError:
        return DownloadOutcome(
            ok=True,
            covered_by_other=True,
            dest=dest,
            detail=f"{dest.name} appeared during the download; the existing sidecar was kept",
            candidate=candidate,
            movie_hash=movie_hash,
        )
    except OSError as exc:
        return DownloadOutcome(
            detail=f"could not write the downloaded sidecar ({exc})",
            reason=REASON_DOWNLOAD_FAILED,
            movie_hash=movie_hash,
            candidate=candidate,
            remaining=download.remaining,
            reset_time=download.reset_time,
        )
    record_downloaded_sidecar(
        video,
        dest,
        movie_hash=movie_hash,
        candidate=candidate,
        cue_count=cue_count,
        sha256=sha256_text(produced_text),
    )
    log(
        f"Downloaded {cue_count} cue(s) from OpenSubtitles ({candidate.label}) "
        f"for moviehash {movie_hash} -> {dest.name}",
        log_file=log_file,
    )
    return DownloadOutcome(
        ok=True,
        detail=(
            f"downloaded an exact-hash match from OpenSubtitles: {candidate.label}"
            + (f" ({download.remaining} download(s) left today)" if download.remaining >= 0 else "")
        ),
        dest=dest,
        candidate=candidate,
        movie_hash=movie_hash,
        cue_count=cue_count,
        remaining=download.remaining,
        reset_time=download.reset_time,
    )


def opensubtitles_check(
    api_key: str,
    *,
    username: str = "",
    password: str = "",
    timeout_seconds: float = OPENSUBTITLES_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """Prove that the configured key is accepted, for ``organize.py doctor``.

    Returns ``(ok, note)`` and never raises: the doctor runs on machines whose
    state is unknown, so a failure here is a finding to print, not a crash.
    """
    client = OpenSubtitlesClient(
        api_key,
        username=username,
        password=password,
        timeout_seconds=timeout_seconds,
    )
    try:
        return True, client.probe()
    except OpenSubtitlesError as exc:
        return False, str(exc)


def _looks_like_quota_error(message: str) -> bool:
    """Whether a provider refusal is about the daily download allowance.

    OpenSubtitles answers a spent allowance with ``406 Not Acceptable`` and a
    message naming the count ("You have downloaded your allowed N subtitles for
    24h"), so both the status and the provider's own words are checked.
    """
    lowered = message.casefold()
    if "http 406" in lowered:
        return True
    return any(
        token in lowered
        for token in ("quota", "download limit", "allowed", "24h", "remaining")
    )


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


def _write_sidecar_record(
    video: Path,
    sidecar: Path,
    *,
    method: str,
    cue_count: int,
    sha256: str,
    source_fields: dict[str, Any],
    path: Path | None = None,
) -> bool:
    """Append one provenance record to the ledger, under the write lock.

    Best effort by design - a read-only installation loses only the record,
    never the subtitle itself - and always load-mutate-write so two workers
    cannot drop each other's entries.
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
            "method": method,
            "cue_count": cue_count,
            "recorded_utc": utc_timestamp(),
        }
        record.update(source_fields)
        payload["sidecars"][path_norm(sidecar)] = record
        try:
            atomic_write_json(target, payload)
        except OSError:
            return False
        return True


def record_extracted_sidecar(
    video: Path,
    sidecar: Path,
    *,
    track: EmbeddedSubtitleTrack,
    method: str,
    cue_count: int,
    sha256: str,
    path: Path | None = None,
) -> bool:
    """Remember that ``sidecar`` came out of the movie's own embedded track.

    This is the provenance record: which movie, which track, which method,
    which bytes and when - the durable answer to "did this tool write this
    sidecar?". A file the tool did not write has no such record, which is how a
    hand-made sidecar stays distinguishable from an extracted one.
    """
    return _write_sidecar_record(
        video,
        sidecar,
        method=method,
        cue_count=cue_count,
        sha256=sha256,
        source_fields={
            "source": "embedded-track",
            "track_id": track.track_id,
            "codec_id": track.codec_id,
            "track_name": track.name,
            "language": track.language,
        },
        path=path,
    )


def record_downloaded_sidecar(
    video: Path,
    sidecar: Path,
    *,
    movie_hash: str,
    candidate: OpenSubtitlesCandidate,
    cue_count: int,
    sha256: str,
    path: Path | None = None,
) -> bool:
    """Remember that ``sidecar`` was downloaded for this exact file hash.

    The provider's own answer is part of the record: which subtitle, which
    file, which release name, whether it was a hash match. That is what makes
    "where did this .eng.srt come from?" answerable months later, and what
    distinguishes it from a sidecar a human placed.
    """
    return _write_sidecar_record(
        video,
        sidecar,
        method="download",
        cue_count=cue_count,
        sha256=sha256,
        source_fields={
            "source": "opensubtitles-hash",
            "provider": "opensubtitles",
            "moviehash": movie_hash,
            "subtitle_id": candidate.subtitle_id,
            "file_id": candidate.file_id,
            "file_name": candidate.file_name,
            "release": candidate.release,
            "language": candidate.language,
            "download_count": candidate.download_count,
            "hearing_impaired": candidate.hearing_impaired,
            "moviehash_match": candidate.moviehash_match,
            "feature_title": candidate.title,
            "feature_year": candidate.year,
        },
        path=path,
    )


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
    """Knobs for one extraction attempt (all of them optional)."""

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
    method: str = ""  # "text" for every sidecar this tool extracts
    cue_count: int = 0
    dest: Path | None = None
    text: str = ""
    attempted: int = 0
    rejected: tuple[str, ...] = ()
    # The English tracks this movie carries, by class. Text tracks are what
    # extraction works on; image tracks are never converted (OCR was removed),
    # and their presence - with no usable text track - is what earns the movie
    # an exact-hash OpenSubtitles lookup in the run loop.
    text_tracks: tuple[EmbeddedSubtitleTrack, ...] = ()
    image_tracks: tuple[EmbeddedSubtitleTrack, ...] = ()

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
    """Create ``dest`` from the movie's own *text-based* English track.

    Returns an :class:`ExtractionOutcome`. ``ok`` means ``dest`` holds a
    validated external English SRT (or, in a dry run, that it would).
    ``unavailable_reason`` means extraction could not even be attempted here
    (no MKVToolNix, no usable English track, or an unreadable container) and
    names the fix.

    Image-based tracks are never converted: they are reported back through
    ``image_tracks`` so the caller can decide whether an exact-hash
    OpenSubtitles lookup is warranted. Extraction itself never touches the
    network and never rewrites an existing sidecar.
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
        text_tracks = tuple(item for item in candidates if item.kind == "text")
        image_tracks = tuple(item for item in candidates if item.kind == "image")
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
            return ExtractionOutcome(unavailable_reason=reason, image_tracks=image_tracks)

        attempts: list[str] = []
        attempted = 0
        text_candidates = list(text_tracks)[: max(0, opts.text_candidate_limit)]
        for track in image_tracks:
            attempts.append(
                f"{track.label}: image-based subtitles are not OCR'd; "
                "an exact-hash OpenSubtitles lookup is the fallback"
            )

        for track in text_candidates:
            attempted += 1
            outcome = _extract_one_track(video, source, dest, track, tmp, opts, log_file=log_file)
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
                    text_tracks=text_tracks,
                    image_tracks=image_tracks,
                )
            attempts.append(f"{track.label}: {outcome.detail}")

    detail = "; ".join(attempts) if attempts else "no embedded English subtitle track could be converted"
    if not text_tracks and image_tracks:
        detail = (
            f"the movie's only English subtitle tracks are image-based "
            f"({len(image_tracks)} track(s)); text tracks are what this tool extracts"
        )
    return ExtractionOutcome(
        ok=False,
        detail=detail,
        attempted=attempted,
        rejected=tuple(attempts),
        unavailable_reason="" if attempted else detail,
        text_tracks=text_tracks,
        image_tracks=image_tracks,
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
    if track.kind != "text":
        return ExtractionOutcome(detail="only text-based subtitle tracks are extracted")
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

    try:
        raw = staged.read_bytes()
    except OSError as exc:
        return ExtractionOutcome(detail=f"could not read the extracted track ({exc})")
    try:
        decoded = decode_subtitle_bytes(raw)
    except (ValueError, OSError) as exc:
        return ExtractionOutcome(detail=f"the extracted track is not readable text ({exc})")
    decoded = normalize_srt_newlines(decoded)
    while decoded.startswith("\ufeff"):
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

    good, reason = subtitle_quality(produced_text, min_cues=opts.min_cues)
    if not good:
        return ExtractionOutcome(detail=reason, track=track, method="text")

    cue_count = len(parse_srt_cues(produced_text))
    if opts.dry_run:
        return ExtractionOutcome(
            ok=True,
            detail=f"embedded {track.label} converts to {cue_count} cues",
            track=track,
            method="text",
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
            method="text",
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
        method="text",
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
        method="text",
        cue_count=cue_count,
        dest=dest,
        text=produced_text,
    )


@dataclass
class ExtractorConfig:
    """Every knob of one extraction pass, including the image-only tier."""
    library: Path
    log_file: Path | None
    report_file: Path
    min_movie_size_mb: float = MIN_MOVIE_SIZE_MB
    lock_timeout_seconds: float = 60.0
    dry_run: bool = False
    limit: int = 0
    extract_min_cues: int = DEFAULT_EXTRACT_MIN_CUES
    # -- the image-only tier: exact-hash OpenSubtitles lookups --------------
    download_enabled: bool = True
    download_min_cues: int = DEFAULT_DOWNLOAD_MIN_CUES
    # 0 = no cap on downloads this run. The provider's own daily allowance is
    # the real limit (5 without an account, 20 with a free one), and the run
    # stops asking the moment the API reports it is spent.
    download_limit: int = 0
    opensubtitles_api_key: str = ""
    opensubtitles_username: str = ""
    opensubtitles_password: str = ""
    download_timeout_seconds: float = OPENSUBTITLES_TIMEOUT_SEC
    # Workers for the local pre-flight only (layout, existing sidecars,
    # identity). 0 = decide from the CPU count. Provider requests and every
    # write stay on the single main thread whatever this says.
    workers: int = 0

    def extract_options(self, *, dry_run: bool = False) -> ExtractOptions:
        return ExtractOptions(
            enabled=True,
            min_cues=self.extract_min_cues,
            dry_run=dry_run,
        )

    def download_options(self, *, dry_run: bool = False, enabled: bool = True) -> DownloadOptions:
        return DownloadOptions(
            enabled=enabled and self.download_enabled,
            api_key=self.opensubtitles_api_key,
            username=self.opensubtitles_username,
            password=self.opensubtitles_password,
            timeout_seconds=self.download_timeout_seconds,
            min_cues=self.download_min_cues,
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
       movie is reported as covered and nothing is extracted, downloaded or
       rewritten. An existing sidecar is authoritative;
    2. otherwise the movie's own embedded *text-based* English track is
       extracted into the canonical sidecar (SRT/ASS/SSA/WebVTT/USF via
       mkvextract, MP4 through the temporary bridge). This is local work and
       always runs first;
    3. a movie whose English subtitles exist only as bitmaps (PGS/VobSub/DVB)
       takes one exact-moviehash OpenSubtitles lookup, which installs a
       subtitle only when the provider matched this precise file;
    4. anything still without a sidecar is reported as needing attention,
       with the reason naming the fix.
    """
    results: list[JobResult] = []
    # The provider's daily allowance is the real cap (5 lookups without an
    # account, 20 with a free one). The run stops asking once the API says it
    # is spent, and --download-limit can cap a single run below that.
    downloads_attempted = 0
    image_only_movies = 0
    quota_exhausted = False
    download_notes: set[str] = set()
    extract_notes: set[str] = set()

    scan = discover_videos(cfg.library, cfg.min_bytes)
    videos = list(scan.videos)
    if cfg.limit > 0:
        videos = videos[: cfg.limit]
    total = len(videos)
    log(f"Found {total} eligible movies.", log_file=cfg.log_file)
    # A silent filter is only silent if nobody counts what it removed.
    if not total and (scan.below_min_size or scan.sample_named):
        log(
            f"Nothing eligible: {scan.below_min_size} movie file(s) are smaller than "
            f"--min-size ({cfg.min_movie_size_mb:g} MB) and {scan.sample_named} are named "
            "like a sample.",
            level="WARNING", log_file=cfg.log_file,
        )

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
            # no lookup, nothing.
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

        # Nothing was extracted. Two very different situations hide here:
        # the movie may hold English subtitles only as bitmaps (the one case
        # that earns a provider lookup), or it may have no usable English
        # subtitle at all.
        image_only = bool(outcome.image_tracks) and not outcome.text_tracks
        if image_only:
            # Counted here, where the fact is established, rather than from the
            # reasons below: every way this tier can end (installed, capped,
            # refused, previewed) is still the same image-only movie.
            image_only_movies += 1
        # A movie that did not qualify for the lookup keeps the empty outcome:
        # no provider reason is invented for it, so nothing is logged.
        download = DownloadOutcome()
        if image_only and quota_exhausted:
            download = DownloadOutcome(
                detail=(
                    "the OpenSubtitles daily download allowance was already spent "
                    "earlier in this run; this image-only movie is reported for a human"
                ),
                reason=REASON_QUOTA_SPENT,
            )
        elif image_only and cfg.download_limit > 0 and downloads_attempted >= cfg.download_limit:
            # The provider's allowance is not spent - this run's own cap is,
            # and the two fixes are different (a re-run vs. a new key), so the
            # cap says so in its own words.
            download = DownloadOutcome(
                detail=(
                    f"this run's --download-limit ({cfg.download_limit}) was reached "
                    "before this movie's lookup; re-run (or raise the limit) to try it"
                ),
                reason=REASON_IMAGE_ONLY,
            )
        elif image_only:
            download = download_hash_matched_srt(
                video,
                extract_dest,
                cfg.download_options(dry_run=cfg.dry_run),
                snapshot=triage.snapshot,
                log_file=cfg.log_file,
            )
        if download.ok and download.dest is not None and not download.covered_by_other:
            if not cfg.dry_run:
                downloads_attempted += 1
                if download.remaining == 0:
                    # The provider's own answer says the allowance is gone;
                    # every later lookup would be refused, so stop asking.
                    quota_exhausted = True
            detail = download.detail or "downloaded a subtitle from OpenSubtitles"
            if cfg.dry_run:
                # A previewed download consumes the run's budget too: the
                # report promises a forecast, and a live run with the same
                # arguments would have spent this one on this movie.
                downloads_attempted += 1
                result = JobResult(video, "dry-run", detail, download.dest,
                                   reason=REASON_DRY_RUN)
                results.append(result)
                emit(index, "DRYRUN", video, detail)
                continue
            result = JobResult(video, "downloaded", detail, download.dest,
                               reason=REASON_DOWNLOADED)
            results.append(result)
            emit(index, "SUB", video, detail)
            continue
        if download.ok and download.covered_by_other and download.dest is not None:
            result = JobResult(video, "have", download.detail, download.dest,
                               reason=REASON_COVERED)
            results.append(result)
            emit(index, "HAVE", video, download.detail)
            continue
        if download.available and download.reason:
            if download.candidate is not None:
                downloads_attempted += 1
            if download.reason == REASON_QUOTA_SPENT:
                quota_exhausted = True
                if download.reason not in download_notes:
                    download_notes.add(download.reason)
                    log(
                        "OpenSubtitles reported the daily download allowance is spent; "
                        "the remaining image-only movies will be reported for a human.",
                        level="WARNING", log_file=cfg.log_file,
                    )
            detail = download.detail or download.unavailable_reason
            result = JobResult(video, "skip", detail, reason=download.reason)
            results.append(result)
            emit(index, "NO-SUBS", video, detail)
            continue
        if download.unavailable_reason and download.unavailable_reason not in download_notes:
            download_notes.add(download.unavailable_reason)
            log(download.unavailable_reason, log_file=cfg.log_file)

        # No sidecar, no downloadable match: this is the human's call now.
        if image_only:
            # The movie *has* English subtitles - as bitmaps. The only route
            # left is the provider lookup, so the report says why that did not
            # run / did not help, instead of pretending there is no track.
            reason = download.reason or REASON_IMAGE_ONLY
            detail = download.detail or download.unavailable_reason or (
                "the movie's only English subtitles are image-based; no exact-hash "
                "OpenSubtitles match was installed"
            )
        else:
            reason = REASON_NO_TRACK
            detail = outcome.unavailable_reason or outcome.detail or (
                "no usable embedded English subtitle track"
            )
            if outcome.unavailable_reason and outcome.unavailable_reason not in extract_notes:
                extract_notes.add(outcome.unavailable_reason)
                if "not installed" in outcome.unavailable_reason:
                    log(outcome.unavailable_reason, level="WARNING", log_file=cfg.log_file)
        result = JobResult(video, "skip", detail, reason=reason)
        results.append(result)
        emit(index, "NO-SUBS", video, detail)

    summary = {
        "movies_discovered": total,
        "files_below_min_size": scan.below_min_size,
        "files_sample_named": scan.sample_named,
        "coverage_covered": coverage_count(results, dry_run=cfg.dry_run),
        "coverage_total": total,
        "extracted_from_embedded": sum(
            1 for r in results if r.reason == REASON_EXTRACTED
        ),
        "downloaded_from_opensubtitles": sum(
            1 for r in results if r.reason == REASON_DOWNLOADED
        ),
        "image_only_movies": image_only_movies,
        "log_file": str(cfg.log_file) if cfg.log_file else "",
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
        REASON_QUOTA_SPENT,
        "OPENSUBTITLES DOWNLOAD ALLOWANCE IS SPENT",
        "re-run tomorrow, or add an account, or place the subtitle by hand",
        "This image-only movie's lookup could not run because the daily download "
        "allowance is used up (5 per day without an account, 20 with a free "
        "OpenSubtitles account). Re-run after the provider's reset, set "
        "OPENSUBTITLES_USERNAME and OPENSUBTITLES_PASSWORD for the larger allowance, "
        "or place the .eng.srt yourself. Nothing local can recover these bitmaps.",
    ),
    NeedsBucket(
        REASON_NO_HASH_MATCH,
        "NO OPENSUBTITLES SUBTITLE MATCHES THIS EXACT FILE",
        "place the subtitle by hand, or re-run when the provider has one",
        "This movie's English subtitles are image-based, so a subtitle had to come "
        "from OpenSubtitles - and no subtitle there matched this file's moviehash. "
        "That usually means the release is rare, or the file is a remux the hash "
        "database has never seen. This tool never guesses by title or release name, "
        "because a wrong-cut subtitle is worse than none: find the right .eng.srt "
        "yourself, or re-run later.",
    ),
    NeedsBucket(
        REASON_DOWNLOAD_FAILED,
        "OPENSUBTITLES LOOKUP FAILED",
        "read the log entry, then re-run",
        "The exact-hash lookup or the download itself failed - a network problem, a "
        "provider error, or an answer that did not pass the subtitle quality gate. "
        "The log carries the exact reason; fix it and re-run.",
    ),
    NeedsBucket(
        REASON_IMAGE_ONLY,
        "OPENSUBTITLES LOOKUP NOT ATTEMPTED",
        "set OPENSUBTITLES_API_KEY (or re-enable downloads), then re-run",
        "This movie's English subtitles are image-based, which leaves the exact-hash "
        "OpenSubtitles lookup as the only route left - and that lookup was not run "
        "for this movie (no API key configured, --no-download, or this run's "
        "--download-limit was already reached). Set the key (see .env.example) and "
        "re-run, or place the .eng.srt yourself.",
    ),
    NeedsBucket(
        REASON_NO_TRACK,
        "NO USABLE EMBEDDED ENGLISH TRACK",
        "add an .eng.srt yourself",
        "This movie has no external English subtitle, no extracted text track, and no "
        "image-only subtitles that an OpenSubtitles lookup could stand in for (only "
        "forced/signs-only streams, or nothing at all) - or it could not be inspected at "
        "all, which the line under it says (a missing MKVToolNix is installed, not "
        "worked around). Otherwise the fix is a human one: find the subtitle and place "
        "it beside the movie. See the log for the per-track reasons.",
    ),
    NeedsBucket(
        REASON_ERROR,
        "ERRORS",
        "read the log entry for each one",
        "Something failed while reading the movie or running mkvmerge/mkvextract. "
        "The log carries the exact error; fix the cause and re-run.",
    ),
)

# Every reason a run can carry, in the order the report lists them.
DOWNLOAD_REASONS: tuple[str, ...] = (
    REASON_DOWNLOADED,
    REASON_IMAGE_ONLY,
    REASON_NO_HASH_MATCH,
    REASON_QUOTA_SPENT,
    REASON_DOWNLOAD_FAILED,
)


def group_results(
    results: Sequence[JobResult],
) -> tuple[dict[str, list[tuple[Path, str]]], list[JobResult], list[JobResult],
           list[JobResult], list[JobResult]]:
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
    below_min_size = int(summary.get("files_below_min_size") or 0)
    sample_named = int(summary.get("files_sample_named") or 0)
    covered_count = int(summary.get("coverage_covered", len(covered) + len(extracted)
                                    + len(downloaded)
                                    + (len(dry_run) if cfg.dry_run else 0)) or 0)
    coverage_pct = (100.0 * covered_count / total) if total else 100.0
    # Zero movies is a finding, not a pass: either the library is empty or the
    # filters removed everything, and only the walk knows which.
    filtered_everything = total == 0 and (below_min_size or sample_named) > 0
    if filtered_everything:
        reasons = []
        if below_min_size:
            reasons.append(f"{below_min_size} smaller than --min-size ({cfg.min_movie_size_mb:g} MB)")
        if sample_named:
            reasons.append(f"{sample_named} named like a sample")
        skipped_text = " and ".join(reasons)
    else:
        skipped_text = ""

    report = Report(
        "JELLYFIN SUBTITLE EXTRACTION REPORT",
        f"One validated external English {EXTERNAL_SRT_SUFFIX} per movie "
        f"\u00b7 from the movie's own tracks, or an exact-hash OpenSubtitles match",
    )
    report.metas([
        ("Generated", f"{utc_timestamp()} (UTC)"),
        ("Library", cfg.library),
        ("Embedded tracks", extract_banner_text(cfg)),
        ("Image-only fallback", download_banner_text(cfg)),
        ("Triage", describe_workers(
            resolve_workers(cfg.workers, cap=MAX_TRIAGE_WORKERS), "movie")),
        ("Log", cfg.log_file or "(none)"),
    ])

    if filtered_everything:
        coverage_row: tuple[object, str, str] = (
            "n/a", "COVERAGE: nothing was inspected",
            f"no movie was eligible: {skipped_text}",
        )
    elif total == 0:
        coverage_row = ("n/a", "COVERAGE: no movie files found",
                        "check the library root and that the movies are not in a skipped folder")
    else:
        coverage_row = (
            f"{covered_count}/{total} ({coverage_pct:.1f}%)",
            "COVERAGE: movies with a validated English SRT" + (" (would be covered)" if cfg.dry_run else ""),
            "the goal: 100% - every uncovered movie is named below",
        )

    rows: list[tuple[object, str, str]] = [
        coverage_row,
        (len(covered), "Already have .eng.srt", "authoritative; never re-extracted"),
        (len(extracted), "Extracted this run", f"written as <movie>{EXTERNAL_SRT_SUFFIX}"),
        (len(downloaded), "Downloaded from OpenSubtitles",
         "exact moviehash match; written as <movie>" + EXTERNAL_SRT_SUFFIX),
    ]
    if dry_run or cfg.dry_run:
        rows.append((len(dry_run), "Dry-run extractions", "no files were written; "
                     "a preview never spends a provider download"))
    rows.append((needs, "NEED ATTENTION", "no sidecar and no subtitle this tool could obtain"))
    if filtered_everything:
        rows.append((total, "Movies inspected",
                     f"none eligible: {skipped_text}; raise --min-size or pass 0 to include them"))
    else:
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
    elif filtered_everything:
        report.paragraph(
            f"Nothing was inspected: {skipped_text}. This is a filtering result, not "
            f"coverage - raise --min-size (or pass --min-size 0) and run again."
        )
    elif total == 0:
        report.paragraph(
            "No movie files were found under the library root. Check the path, and that "
            "the movies are not inside a skipped folder."
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
            "These movies have no external English subtitle, no text-based embedded "
            "track to extract, and no exact-hash OpenSubtitles match that could stand "
            "in for image-based subtitles. Each one is now a human decision: place the "
            "subtitle yourself. Groups are ordered by how close the fix is."
        ),
    )
    if needs == 0 and filtered_everything:
        report.paragraph(
            "Nothing was inspected, so nothing can be claimed about these movies "
            f"({skipped_text})."
        )
    elif needs == 0 and total == 0:
        report.paragraph("No movie files were found to inspect.")
    elif needs == 0:
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
            "DOWNLOADED FROM OPENSUBTITLES BY EXACT MOVIEHASH",
            count=len(downloaded),
            total=total,
            intro=(
                "These movies carried English subtitles only as images (PGS/VobSub/DVB), "
                "which this tool does not OCR. It asked OpenSubtitles for subtitles "
                "matching this precise file's moviehash - the release, not a title "
                "guess - and installed the best English match as the canonical sidecar. "
                "The lookup never runs for a movie that already has a usable text track."
            ),
        )
        report.entries(
            [{"text": movie_label(result.video, cfg.library), "detail": result.detail}
             for result in downloaded],
        )

    if extracted:
        report.section(
            "EXTRACTED FROM THE MOVIE'S OWN EMBEDDED TRACK",
            count=len(extracted),
            total=total,
            intro=(
                "These movies carried a text-based English subtitle track. It was "
                f"extracted to the canonical <movie>{EXTERNAL_SRT_SUFFIX}: exact for this "
                "release, and its cues come from the container's own timeline, so no "
                "offline timing correction is needed. mkv_track_cleaner.py then strips "
                "every embedded subtitle, leaving this sidecar as the sole subtitle option."
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

    if filtered_everything:
        coverage_line = (
            f"Coverage this run: nothing was inspected - {skipped_text}."
        )
    elif total == 0:
        coverage_line = "Coverage this run: no movie files were found."
    else:
        coverage_line = (
            f"Coverage this run: {covered_count} of {total} movie(s) "
            f"({coverage_pct:.1f}%) end with a validated external English SRT."
        )
    report.footer([
        coverage_line,
        f"Extraction provenance ledger  {extracted_ledger_path()}",
        "Subtitles come from the movie's own tracks whenever they can; the "
        "OpenSubtitles lookup runs only for image-only movies, matched by exact file "
        "hash, and never replaces a sidecar that is already there.",
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

    mkvextract is the one hard requirement. Image-based subtitles are not a
    toolchain question any more - they are the case the provider tier exists
    for - so the banner names that tier instead of an OCR backend.
    """
    if not find_mkvtoolnix_binary("mkvmerge") or not find_mkvtoolnix_binary("mkvextract"):
        return f"unavailable: {MKVTOOLNIX_INSTALL_HINT}"
    return (
        f"text tracks (SRT/SSA/ASS/WebVTT/USF) with mkvextract "
        f"(>= {cfg.extract_min_cues} cues); "
        f"MP4s read through a temporary MKV bridge; "
        f"image tracks (PGS/VobSub/DVB) are reported, never OCR'd"
    )


def download_banner_text(cfg: ExtractorConfig) -> str:
    """One banner line saying whether the image-only fallback can run.

    The fallback is the one networked step in the toolkit, so the banner says
    exactly whether it is armed - an operator reading a report should never
    have to guess whether a missing subtitle was a failed lookup or a
    disabled one.
    """
    if not cfg.download_enabled:
        return "disabled (--no-download): image-only movies are reported for a human"
    if not cfg.opensubtitles_api_key:
        return f"offline: {OPENSUBTITLES_KEY_HINT}"
    account = "with a signed-in account" if (cfg.opensubtitles_username and cfg.opensubtitles_password) else "API key only"
    cap = f", at most {cfg.download_limit} this run" if cfg.download_limit > 0 else ""
    return (
        "enabled: one exact-moviehash lookup per image-only movie "
        f"({account}{cap})"
    )


# =============================================================================
# COMPACT ROOT-LEVEL DRIVER
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract one validated external English SRT per movie from its own "
            "embedded text-based subtitle track, written beside the movie as "
            "<movie>.eng.srt. An existing .eng.srt beside a movie is authoritative "
            "and is never re-extracted or rewritten. If the movie's English "
            "subtitles exist only as images (PGS/VobSub/DVB), one OpenSubtitles "
            "lookup by the movie file's exact hash can supply a matching English "
            "subtitle; there is no title search and no OCR."
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
                        help="Reject a subtitle with fewer than N cues as signs/songs-only "
                             "(applies to extracted tracks and downloads alike)")
    parser.add_argument("--no-download", action="store_true",
                        help="Never contact OpenSubtitles; image-only movies are reported "
                             "for a human instead")
    parser.add_argument("--download-limit", type=int, default=0, metavar="N",
                        help="At most N OpenSubtitles downloads per run (0 means no run-level "
                             "cap; the provider's own daily allowance still applies). "
                             "--dry-run counts the downloads it previews, so a preview "
                             "matches the live run")
    parser.add_argument("--download-min-cues", type=int, default=DEFAULT_DOWNLOAD_MIN_CUES, metavar="N",
                        help="Reject a downloaded subtitle with fewer than N cues")
    parser.add_argument("--download-timeout", type=float,
                        default=OPENSUBTITLES_TIMEOUT_SEC, metavar="SEC",
                        help="Per-request OpenSubtitles time limit")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def extractor_config_from_args(args: argparse.Namespace) -> ExtractorConfig:
    # A configured .env beside the scripts (or a real environment variable)
    # carries the OpenSubtitles credentials; nothing is ever passed on the
    # command line where it would end up in a shell history or a process list.
    api_key, username, password = opensubtitles_settings_from_env()
    # Numbers are taken as typed, never clamped: a negative --download-limit
    # silently becoming "no cap" would spend provider quota the operator was
    # trying to bound, and validate_config already reports every out-of-range
    # flag by name.
    return ExtractorConfig(
        library=args.source.resolve(),
        log_file=args.log.resolve() if args.log else None,
        report_file=args.report.resolve(),
        min_movie_size_mb=float(args.min_size),
        lock_timeout_seconds=float(args.lock_timeout),
        workers=int(args.workers),
        dry_run=bool(args.dry_run),
        limit=int(args.limit),
        extract_min_cues=int(args.extract_min_cues),
        download_enabled=not bool(args.no_download),
        download_limit=int(args.download_limit),
        download_min_cues=int(args.download_min_cues),
        download_timeout_seconds=float(args.download_timeout),
        opensubtitles_api_key=api_key,
        opensubtitles_username=username,
        opensubtitles_password=password,
    )


def validate_config(cfg: ExtractorConfig) -> list[str]:
    errors: list[str] = []
    if not cfg.library.is_dir() or cfg.library.is_symlink():
        errors.append("--source must be an existing non-symlink movie-library directory")
    if cfg.extract_min_cues < 1:
        errors.append("--extract-min-cues must be at least 1")
    if cfg.download_min_cues < 1:
        errors.append("--download-min-cues must be at least 1")
    if cfg.download_limit < 0:
        errors.append("--download-limit must be zero (no run cap) or greater")
    if cfg.download_timeout_seconds <= 0:
        errors.append("--download-timeout must be greater than zero")
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
                ("Policy", "English human-authored UTF-8 SRT; an existing sidecar is authoritative"),
                ("Embedded tracks", extract_banner_text(cfg)),
                ("Image-only fallback", download_banner_text(cfg)),
                ("Triage", describe_workers(
                    resolve_workers(cfg.workers, cap=MAX_TRIAGE_WORKERS), "movie")),
                ("Log", cfg.log_file),
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

    The extraction paths (probe, classify, convert, the MP4 bridge and the
    provenance ledger) are covered exhaustively in ``tests/``. The things
    worth re-checking on an unfamiliar machine are the sidecar contract, the
    moviehash this tool sends to OpenSubtitles, and the provenance ledger,
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

    def the_moviehash_matches_the_documented_algorithm() -> bool:
        # An all-zero 128 KiB file hashes to its own size: no chunk contributes.
        with tempfile.TemporaryDirectory(prefix="extractor_smoke_") as td:
            movie = Path(td) / "Movie (2020).mkv"
            movie.write_bytes(b"\0" * (2 * 65536))
            return moviehash_of_file(movie) == ("0000000000020000", 2 * 65536)

    def a_file_below_the_hash_floor_is_refused() -> bool:
        with tempfile.TemporaryDirectory(prefix="extractor_smoke_") as td:
            movie = Path(td) / "Small.mkv"
            movie.write_bytes(b"\0" * 1024)
            try:
                moviehash_of_file(movie)
            except ValueError:
                return True
            return False

    def a_foreign_download_link_is_refused() -> bool:
        # A link that is not HTTPS on the provider's own host never gets read.
        for link in ("https://evil.example/SRT.srt", "http://opensubtitles.com/x.srt"):
            try:
                _require_provider_link(link)
            except OpenSubtitlesError:
                continue
            return False
        return True

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
        ("the moviehash is the documented sum", the_moviehash_matches_the_documented_algorithm),
        ("a file below the hash floor is refused", a_file_below_the_hash_floor_is_refused),
        ("a foreign download link is refused", a_foreign_download_link_is_refused),
        ("the provenance ledger round-trips", the_provenance_ledger_round_trips),
    ])

if __name__ == "__main__":
    raise SystemExit(main())
