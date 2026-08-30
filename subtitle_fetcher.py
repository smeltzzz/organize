#!/usr/bin/env python3
"""
English Subtitle Fetcher for Jellyfin Movies
============================================
After ``movie_standardizer.py`` and before ``mkv_track_cleaner.py``: walk the
canonical movie library and create at most one validated external English SRT
sidecar per MKV. This single script owns its persistent UTC request ledger;
there is no separate queue script or launcher to run.

When configured, it always attempts the exact OpenSubtitles moviehash first.
After a hash miss, it automatically allows only a high-confidence exact
title/year candidate. An optional score-gated SubDL release-aware lookup is the final
fallback; because SubDL has no equivalent release hash, it is never allowed ahead of an available
OpenSubtitles hash match. A wrong cut is held for review rather than downloaded.

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

Configure one or both providers through environment variables:
    set OPENSUBTITLES_API_KEY=...
    set SUBDL_API_KEY=...

Credentials are read only from environment variables, never command-line
arguments. Development-anonymous mode uses only the OpenSubtitles API key for
consumers that OpenSubtitles currently permits to download anonymously.
Authenticated user mode remains available as an explicit fallback.

OpenSubtitles key: https://www.opensubtitles.com/en/consumers
SubDL key: https://subdl.com/panel/api
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common import (
    EXTERNAL_SRT_ENCODINGS,
    EXTERNAL_SRT_MAX_BYTES,
    EXTERNAL_SRT_SUFFIX,
    CoordinationLock,
    Report,
    enable_utf8_stdio,
    exact_external_english_srt_path,
    normalize_srt_newlines,
    path_norm,
    print_text,
    promote_legacy_external_english_srt,
    report_banner,
    srt_looks_valid,
)

# =============================================================================
# CONFIGURATION
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
SUBDL_DEFAULT_DAILY_CAP = 50
SUBDL_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

__version__ = "2.6.0"
APP_USER_AGENT = "JellyfinMovieSubtitleFetcher v2.6"
API_BASE = "https://api.opensubtitles.com/api/v1"

# The preceding standardizer emits canonical MKV movies only. Limiting the
# fetcher to that exact contract prevents unrelated videos or media variants
# from receiving sidecars.
VIDEO_EXTENSIONS = {".mkv"}
DIRECT_PLAY_SUBTITLE_EXTENSION = ".srt"
DOWNLOAD_SUBTITLE_FORMAT = "srt"
MIN_MOVIE_SIZE_MB = 300
REQUEST_GAP_SEC = 1.1  # stay under the documented per-second limit
# Bound to the one shared limit in common.py, not a second copy of the number.
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
MIN_IDENTITY_RATING = 6.0
MIN_IDENTITY_VOTES = 3
MIN_IDENTITY_DOWNLOADS = 50
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
    # Unlike common.decode_srt_bytes this must return a string: the caller
    # inspects a rejected download in order to explain why it was rejected.
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
def pick_candidate(cands: Sequence[Candidate], cfg: Config) -> Candidate | None:
    """Return one strict best candidate for the requested English subtitle mode."""
    usable = [
        candidate for candidate in cands
        if candidate.moviehash_match and _is_normal_english_human_candidate(candidate)
    ]
    if not usable:
        return None
    # A trusted provider flag and community rating outrank raw download count;
    # the latter remains a useful tiebreaker. This yields one deterministic SRT.
    usable.sort(
        key=lambda candidate: (
            -int(candidate.trusted), -candidate.rating, -candidate.votes, -candidate.downloads,
            str(candidate.file_id), candidate.release.casefold(),
        ),
    )
    return usable[0]


def pick_identity_candidate(cands: Sequence[Candidate], identity: MovieIdentity) -> tuple[Candidate | None, str]:
    """Choose one non-hash candidate only when identity and quality are strong.

    Title/year must exactly match provider feature metadata. Edition-labelled
    releases are deliberately not auto-selected because a canonical local name
    contains no reliable edition/cut marker to compare against.
    """
    usable = [
        candidate for candidate in cands
        if _is_normal_english_human_candidate(candidate)
        and candidate.feature_year == identity.year
        and normalize_title(candidate.feature_title) == identity.normalized_title
        and not release_has_edition_marker(candidate.release)
        and candidate.rating >= MIN_IDENTITY_RATING
        and candidate.votes >= MIN_IDENTITY_VOTES
        and candidate.downloads >= MIN_IDENTITY_DOWNLOADS
        and (candidate.trusted or (
            candidate.rating >= 8.0 and candidate.votes >= 20 and candidate.downloads >= 200
        ))
    ]
    if not usable:
        return None, "no title/year-exact, normal English SRT met the automatic quality policy"
    usable.sort(
        key=lambda candidate: (
            -int(candidate.trusted), -candidate.rating, -candidate.votes, -candidate.downloads,
            str(candidate.file_id), candidate.release.casefold(),
        ),
    )
    top = usable[0]
    top_key = (top.trusted, top.rating, top.votes, top.downloads)
    tied = [candidate for candidate in usable if (
        candidate.trusted, candidate.rating, candidate.votes, candidate.downloads
    ) == top_key]
    if len(tied) != 1:
        return None, "multiple equally ranked title/year-exact SRT candidates require review"
    return top, "title/year exact; high-confidence provider candidate"


def looks_like_srt(text: str) -> bool:
    """Shared verdict from common.py — see the note on why this is not local.

    This used to be a private copy of the cue pattern, and it had drifted: it
    anchored the cue number at column 0 while the other four tools allowed
    leading whitespace. A subtitle with an indented cue number was therefore
    rejected here at download time ("downloaded payload is not a valid SRT
    subtitle") yet accepted as canonical by library_auditor, movie_standardizer
    and mkv_track_cleaner. Delegating makes that disagreement impossible.
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
        ]
    }
    cands = parse_candidates(payload)
    pick = pick_candidate(cands, Config())
    check(pick is not None and pick.file_id == 2, f"strict normal pick {pick}")
    check(pick_candidate([candidate for candidate in cands if candidate.hearing_impaired], Config()) is None,
          "SDH candidates must be excluded")
    check(pick_candidate([candidate for candidate in cands if candidate.foreign_parts_only], Config()) is None,
          "forced/foreign-part candidates must be excluded")
    check(pick_candidate([candidate for candidate in cands if not candidate.moviehash_match], Config()) is None,
          "no hash match → none")

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

    if errors:
        print("SELF-TEST FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print("SELF-TEST PASSED (hash + OpenSubtitles/SubDL picks + SRT safety + discovery + transaction guards)")
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


def provider_daily_cap(cfg: QueueConfig, provider: str) -> int:
    if provider == PROVIDER_OPENSUBTITLES:
        return cfg.daily_cap
    if provider == PROVIDER_SUBDL:
        return cfg.subdl_daily_cap
    raise ValueError(f"unknown subtitle provider: {provider}")


def provider_reservation_field(provider: str) -> str:
    if provider == PROVIDER_OPENSUBTITLES:
        return "opensubtitles_download_requests_reserved"
    if provider == PROVIDER_SUBDL:
        return "subdl_download_requests_reserved"
    raise ValueError(f"unknown subtitle provider: {provider}")


def provider_success_field(provider: str) -> str:
    if provider == PROVIDER_OPENSUBTITLES:
        return "opensubtitles_successful_downloads"
    if provider == PROVIDER_SUBDL:
        return "subdl_successful_downloads"
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
    return " · ".join(parts) or "no provider configured"


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
    return " · ".join(parts) or "no provider configured"


def provider_policy_text(cfg: QueueConfig) -> str:
    """Explain the actual matching strength available in this run."""
    if not cfg.identity_fallback:
        if cfg.api_key.strip():
            return "OpenSubtitles exact moviehash matching only"
        return "title/year fallback disabled"
    if cfg.api_key.strip() and cfg.subdl_api_key.strip():
        return "OpenSubtitles exact moviehash first · SubDL release-aware fallback (score ≥ 0.80)"
    if cfg.api_key.strip():
        return "OpenSubtitles exact moviehash first · conservative title/year fallback"
    if cfg.subdl_api_key.strip():
        return "SubDL release-aware matching (score ≥ 0.80) · no exact moviehash provider"
    return "no provider configured"


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


def queue_run(cfg: QueueConfig) -> tuple[list[JobResult], dict[str, Any]]:
    """Process one daily batch with independent provider quotas.

    OpenSubtitles remains the only exact-release (OSHash) source. SubDL runs
    only after OpenSubtitles has no safe candidate or is unavailable for the
    day; its documented filename match is score-gated, then its strict
    title/year route is used only when filename matching found no usable candidate.
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
            return PROVIDER_SUBDL in active_providers
        previous = {str(provider) for provider in prior}
        return any(provider not in previous for provider in active_providers)

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
        if old_status == "no_match" and not (cfg.retry_no_match or cfg.identity_fallback):
            result = JobResult(video, "skip", "previous strict moviehash search had no match",
                               reason=REASON_NO_MATCH)
            results.append(result)
            emit(index, "SKIP", video, result.detail)
            continue
        if old_status == "manual_review" and not cfg.retry_no_match and not has_new_provider(record):
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
        if not open_available and not subdl_available:
            deferred_remaining = total - index + 1
            deferred_videos = list(videos[index - 1:])
            log(
                "QUOTA REACHED: no configured provider with an enabled matching mode has "
                f"remaining local capacity ({provider_quota_text(cfg, ledger)}). "
                f"{deferred_remaining} movie(s) remain for the next UTC day.",
                level="WARNING", log_file=cfg.log_file,
            )
            break

        digest = ""
        pick: Candidate | None = None
        selected_provider = ""
        selection_method = ""
        selection_reason = "no usable English moviehash-matched human SRT"
        providers_checked: list[str] = []
        subdl_downloads: dict[str, SubdlDownload] = {}
        # Distinguish an exhausted SubDL cap before a lookup from a filename
        # lookup that actually returned a low-score or ambiguous candidate.
        # The former should be retried on the next quota day; the latter is a
        # deliberate manual-review decision.
        subdl_lookup_attempted = False

        open_lookup_error = ""
        if open_available and open_client is not None:
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
                pick = pick_candidate(candidates, fetcher_cfg)
            except (RuntimeError, ValueError) as exc:
                if not subdl_available:
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
                emit(index, "FALLBACK", video, f"{open_lookup_error}; continuing to SubDL")
            if pick is not None:
                selected_provider = PROVIDER_OPENSUBTITLES
                selection_method = "hash"
                selection_reason = "moviehash match"

        if pick is None:
            if not cfg.identity_fallback:
                detail = (
                    "no usable English moviehash-matched human SRT"
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

            identity = movie_identity_from_video(video)
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

            identity_reasons: list[str] = [open_lookup_error] if open_lookup_error else []
            if open_available and open_client is not None and not open_lookup_error:
                emit(index, "FALLBACK", video,
                     f"exact hash missed; checking OpenSubtitles title/year: {identity.title} ({identity.year})")
                try:
                    identity_candidates = open_client.search_identity(identity)
                    pick, selection_reason = pick_identity_candidate(identity_candidates, identity)
                except (RuntimeError, ValueError) as exc:
                    if not subdl_available:
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
                    open_lookup_error = f"OpenSubtitles title/year lookup failed: {exc}"
                    identity_reasons.append(open_lookup_error)
                    emit(index, "FALLBACK", video, f"{open_lookup_error}; continuing to SubDL")
                if pick is not None:
                    selected_provider = PROVIDER_OPENSUBTITLES
                    selection_method = "identity"
                elif not open_lookup_error:
                    identity_reasons.append(f"OpenSubtitles: {selection_reason}")
            elif open_client is not None and not open_lookup_error:
                identity_reasons.append("OpenSubtitles: daily download cap exhausted")

            if pick is None and subdl_available and subdl_client is not None:
                providers_checked.append(PROVIDER_SUBDL)
                prefix = (
                    "OpenSubtitles lookup failed; " if open_lookup_error else
                    "OpenSubtitles missed; " if open_available else
                    "OpenSubtitles quota exhausted; " if open_client is not None else ""
                )
                emit(
                    index,
                    "FALLBACK",
                    video,
                    f"{prefix}checking SubDL release-aware filename match: {video.name}",
                )
                try:
                    subdl_lookup_attempted = True
                    subdl_candidates, subdl_downloads = subdl_client.search_filename(video.name, identity)
                    pick, selection_reason = pick_subdl_identity_candidate(
                        subdl_candidates, identity, require_release_match_score=True,
                    )
                    if pick is not None:
                        selected_provider = PROVIDER_SUBDL
                        selection_method = "subdl-release"
                    elif not subdl_candidates:
                        # The local canonical filename deliberately omits scene
                        # tags. If SubDL cannot resolve it at all, use its
                        # documented title route once, still requiring exact
                        # provider title/year metadata and one unambiguous SRT.
                        emit(
                            index,
                            "FALLBACK",
                            video,
                            f"SubDL filename lookup found no usable candidate; checking strict title/year: "
                            f"{identity.title} ({identity.year})",
                        )
                        subdl_candidates, subdl_downloads = subdl_client.search_identity(identity)
                        pick, selection_reason = pick_subdl_identity_candidate(subdl_candidates, identity)
                        if pick is not None:
                            selected_provider = PROVIDER_SUBDL
                            selection_method = "subdl-identity"
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
                if pick is None:
                    identity_reasons.append(f"SubDL: {selection_reason}")
            elif pick is None and subdl_client is not None:
                if not provider_has_quota(cfg, ledger, PROVIDER_SUBDL):
                    identity_reasons.append("SubDL: daily download cap exhausted")
                elif not subdl_search_has_quota(cfg, ledger):
                    identity_reasons.append("SubDL: daily search cap exhausted")
                else:
                    identity_reasons.append("SubDL: identity fallback disabled")

            if pick is None and subdl_client is not None and not subdl_lookup_attempted and not subdl_available:
                if not provider_has_quota(cfg, ledger, PROVIDER_SUBDL):
                    detail = "SubDL daily download cap exhausted before lookup; deferred to the next UTC day"
                else:
                    detail = "SubDL daily search cap exhausted before lookup; deferred to the next UTC day"
                result = JobResult(video, "skip", detail, reason=REASON_QUOTA)
                results.append(result)
                emit(index, "SKIP", video, detail)
                continue

            if pick is None:
                reason = "; ".join(identity_reasons) or selection_reason
                detail = f"identity fallback held for review: {reason}"
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
        "quota_reached": not available_after_run,
        "deferred_remaining": deferred_remaining,
        "ledger_log": str(cfg.log_file),
        "movies_discovered": total,
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
        "inspect the title/year candidate yourself",
        "The exact moviehash missed and only a title/year match was found, so the "
        "download was deliberately not made. Inspect the candidate, then either place "
        "the subtitle yourself or re-run with --retry-review to reconsider the match.",
    ),
    NeedsBucket(
        REASON_NO_MATCH,
        "NO MATCHING SUBTITLE ON CONFIGURED PROVIDERS",
        "re-run on a later day, or add the SRT by hand",
        "No configured provider returned a safe English, human-authored SRT. Provider "
        "catalogues grow over time, so a later run can succeed; otherwise add the subtitle "
        "yourself.",
    ),
    NeedsBucket(
        REASON_QUOTA,
        "DEFERRED TO THE NEXT UTC DAY",
        "nothing to fix - re-run after the UTC day rolls over",
        "Every configured provider's usable daily download allowance was exhausted, so "
        "these movies were not searched. Re-run after the UTC day rolls over; no request is wasted.",
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
    return "  ·  ".join(parts) or "No provider configured"


def report_download_text(cfg: QueueConfig, summary: dict[str, Any]) -> str:
    """Show a useful provider breakdown without breaking old report callers."""
    total = int(summary.get("successful_downloads", 0) or 0)
    parts: list[str] = []
    if cfg.api_key.strip():
        parts.append(f"OpenSubtitles {int(summary.get('opensubtitles_successful_downloads', 0) or 0)}")
    if cfg.subdl_api_key.strip():
        parts.append(f"SubDL {int(summary.get('subdl_successful_downloads', 0) or 0)}")
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

    policy = provider_policy_text(cfg)
    report = Report(
        "JELLYFIN DAILY SUBTITLE QUEUE REPORT",
        f"One validated external English {EXTERNAL_SRT_SUFFIX} beside every movie \u00b7 {policy}",
    )
    report.metas([
        ("Generated", f"{utc_timestamp()} (UTC)"),
        ("Library", cfg.library),
        ("Quota", f"{summary['utc_day']}  \u00b7  {report_provider_quota_text(cfg, summary)}"),
        ("Downloads", report_download_text(cfg, summary)),
        ("Policy", f"English human-authored UTF-8 SRT only  \u00b7  {policy}"),
        ("Ledger", cfg.log_file or "(none)"),
    ])

    rows: list[tuple[object, str, str]] = [
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
        f"Durable quota and retry ledger  {cfg.log_file or '(none)'}",
        f"This report  {cfg.report_file}",
        "Re-running is always safe: covered movies are skipped without spending a request, and "
        "the ledger keeps every run inside each configured provider's UTC cap.",
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
            "OpenSubtitles exact moviehash is preferred; SubDL is an optional "
            "score-gated release-aware fallback when no hash-safe result is available."
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
    if not configured_providers(cfg):
        errors.append("configure OPENSUBTITLES_API_KEY and/or SUBDL_API_KEY")
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
                ("Policy", "English human-authored UTF-8 SRT; " + provider_policy_text(cfg)),
                ("Providers", provider_configuration_text(cfg) + " (UTC download caps)"),
                ("Ledger", cfg.log_file),
                ("Report", cfg.report_file),
            ],
        ))
        with CoordinationLock(cfg.library, timeout_seconds=cfg.lock_timeout_seconds):
            results, summary = queue_run(cfg)
            write_report(results, cfg, summary)
        return 1 if any(result.status == "error" for result in results) else 0
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Subtitle fetcher failure: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
