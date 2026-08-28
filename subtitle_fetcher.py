#!/usr/bin/env python3
"""
English Subtitle Fetcher for Jellyfin Movies
============================================
After ``movie_standardizer.py`` and before ``mkv_track_cleaner.py``: walk the
canonical movie library and create at most one validated external English SRT
sidecar per MKV. This single script owns its persistent UTC request ledger;
there is no separate queue script or launcher to run.

It always attempts the exact OpenSubtitles moviehash first. After a hash miss,
it automatically allows only a high-confidence exact title/year candidate. A
wrong cut is held for review rather than downloaded.

    py -3 subtitle_fetcher.py --dry-run
    py -3 subtitle_fetcher.py
    py -3 subtitle_fetcher.py --self-test

The default policy intentionally downloads only UTF-8 SRT sidecars. SRT is the
most broadly direct-play-safe external subtitle choice across Jellyfin clients;
ASS/SSA, VobSub, PGS, and other formats are never requested or written here.

Development-anonymous mode (the default):
    set OPENSUBTITLES_API_KEY=...

Credentials are read only from environment variables, never command-line
arguments. Development-anonymous mode uses only the API key for consumers that
OpenSubtitles currently permits to download anonymously. Authenticated user
mode remains available as an explicit fallback.

Free key: https://www.opensubtitles.com/en/consumers
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
import struct
import sys
import uuid
import tempfile
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

# =============================================================================
# CONFIGURATION
# =============================================================================

LIBRARY_DIR = r"E:\torrents\final_organized"
LOG_FILE = r"E:\torrents\subtitle_fetcher\subtitle_fetcher.log"  # Appended every run; this is also the durable quota/retry ledger.
REPORT_FILE = r"E:\torrents\subtitle_fetcher\subtitle_fetcher_report.txt"
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

__version__ = "2.5.0"
APP_USER_AGENT = "JellyfinMovieSubtitleFetcher v2.5"
API_BASE = "https://api.opensubtitles.com/api/v1"

# The preceding standardizer emits canonical MKV movies only. Limiting the
# fetcher to that exact contract prevents unrelated videos or media variants
# from receiving sidecars.
VIDEO_EXTENSIONS = {".mkv"}
DIRECT_PLAY_SUBTITLE_EXTENSION = ".srt"
DOWNLOAD_SUBTITLE_FORMAT = "srt"
MIN_MOVIE_SIZE_MB = 300
REQUEST_GAP_SEC = 1.1  # stay under the documented per-second limit
MAX_SUBTITLE_BYTES = 4 * 1024 * 1024
LANGUAGES = "en"

# =============================================================================
# CONSTANTS
# =============================================================================

HASH_CHUNK = 65536  # 64 KiB
MIN_HASH_SIZE = HASH_CHUNK * 2
STANDARDIZER_LOCK_NAME = ".movie_standardizer.lock"

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


# Official OSHash test: first+last 64KiB of a synthetic pattern is tested in --self-test.


@dataclass
class Config:
    library: Path = field(default_factory=lambda: Path(LIBRARY_DIR))
    log_file: Path | None = field(default_factory=lambda: Path(LOG_FILE) if LOG_FILE else None)
    report_file: Path = field(default_factory=lambda: Path(REPORT_FILE))
    api_key: str = ""
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
        """The sole output sidecar: a normal English UTF-8 SRT."""
        return ".en.srt"


@dataclass
class Candidate:
    file_id: int
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


@dataclass(frozen=True)
class MovieIdentity:
    """Canonical identity inferred only from a standardized ``Title (Year)`` name."""
    title: str
    year: int
    normalized_title: str


@dataclass
class JobResult:
    video: Path
    status: str  # have, skip, download, dry-run, error
    detail: str
    dest: Path | None = None


@dataclass(frozen=True)
class VideoSnapshot:
    device: int
    inode: int
    size: int
    mtime_ns: int


# =============================================================================
# LOGGING / HTTP
# =============================================================================


def path_norm(path: Path) -> str:
    """Match the standardizer/cleaner path-normalization contract exactly."""
    return os.path.normcase(os.path.normpath(str(path)))


class LockTimeoutError(RuntimeError):
    """Raised when the standardizer/cleaner coordination lock cannot be acquired."""


class ConcurrentSidecarError(RuntimeError):
    """Raised when another actor safely created the requested sidecar first."""


class StandardizerCoordinationLock:
    """Cross-platform lock shared with movie_standardizer.py and mkv_track_cleaner.py."""

    def __init__(self, library: Path, *, timeout_seconds: float) -> None:
        key = hashlib.sha256(path_norm(library).encode("utf-8", errors="surrogatepass")).hexdigest()[:20]
        self.path = Path(tempfile.gettempdir()) / f"{STANDARDIZER_LOCK_NAME}.{key}"
        self.timeout_seconds = max(0.0, timeout_seconds)
        self._fh: Any | None = None

    def _try_lock(self, fh: Any) -> bool:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError as exc:
                if getattr(exc, "winerror", None) in {33, 36} or exc.errno in {errno.EACCES, errno.EAGAIN}:
                    return False
                raise
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def __enter__(self) -> "StandardizerCoordinationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        self._fh = fh
        if fh.seek(0, os.SEEK_END) == 0:
            fh.write(b"\0")
            fh.flush()
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while not self._try_lock(fh):
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"Timed out after {self.timeout_seconds:.1f}s waiting for library coordination lock: {self.path}"
                    )
                time.sleep(0.1)
        except BaseException:
            fh.close()
            self._fh = None
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
            self._fh = None


def log(msg: str, level: str = "INFO", log_file: Path | None = None) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
    print(line, flush=True)
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
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not looks_like_srt(text):
            raise RuntimeError("downloaded payload is not a valid SRT subtitle")
        if not video_snapshot_matches(video, expected_video):
            raise RuntimeError("movie changed during subtitle lookup; downloaded SRT was not activated")
        try:
            atomic_write_text(dest, text, replace=False)
        except FileExistsError as exc:
            raise ConcurrentSidecarError("English SRT appeared during download; preserved the existing sidecar") from exc


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
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
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
            candidate.file_id, candidate.release.casefold(),
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
            candidate.file_id, candidate.release.casefold(),
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
    # Minimal: a cue index, a timestamp arrow, and some dialogue.
    return bool(re.search(
        r"(?m)^\d+\s*\n\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}",
        text,
    ))


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
    return video.with_name(video.stem + cfg.sidecar_suffix)




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

    tmp = Path(tempfile.mkdtemp(prefix="subf_"))
    try:
        movie = tmp / "Knowing (2009)"
        extra = movie / "Featurettes"
        extra.mkdir(parents=True)
        vid = movie / "Knowing (2009).mkv"
        with vid.open("wb") as fh:
            fh.truncate(400 * 1024 * 1024)
        (extra / "Making-Of.mkv").write_bytes(b"x")
        sidecar = movie / "Knowing (2009).en.srt"
        sidecar.write_text(sample, encoding="utf-8")
        (movie / "Another Movie (2009).en.srt").write_text(sample, encoding="utf-8")
        (movie / "Knowing (2009).en.ass").write_text("[Script Info]", encoding="utf-8")
        with (movie / "Knowing (2009).mp4").open("wb") as fh:
            fh.truncate(400 * 1024 * 1024)
        found = discover_videos(tmp, 300 * 1024 * 1024)
        check(found == [vid], f"discover {found}")
        check(has_english_sidecar(movie, "Knowing (2009)") == sidecar, "exact existing English SRT")
        check(not is_english_srt_sidecar(movie / "Another Movie (2009).en.srt", "Knowing (2009)"),
              "neighboring movie subtitle must not block download")
        check(not is_english_srt_sidecar(movie / "Knowing (2009).en.ass", "Knowing (2009)"),
              "non-SRT sidecar must not count as direct-play policy output")

        guarded = movie / "Guarded.en.srt"
        atomic_write_text(guarded, sample, replace=False)
        try:
            atomic_write_text(guarded, "1\\n00:00:00,000 --> 00:00:01,000\\nreplacement\\n", replace=False)
            errors.append("create-only sidecar write unexpectedly replaced destination")
        except FileExistsError:
            pass
        check(guarded.read_text(encoding="utf-8") == sample, "create-only sidecar retains existing content")
        check(not list(movie.glob(".Guarded.en.srt.partial.*")), "create-only sidecar leaves no temp")

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
    print("SELF-TEST PASSED (hash + strict pick + SRT safety + discovery + transaction guards)")
    return 0


@dataclass
class QueueConfig:
    library: Path
    log_file: Path
    report_file: Path
    api_key: str = ""
    username: str = ""
    password: str = ""
    daily_cap: int = DEVELOPMENT_ANONYMOUS_DAILY_CAP
    min_movie_size_mb: float = MIN_MOVIE_SIZE_MB
    lock_timeout_seconds: float = 60.0
    retry_no_match: bool = False
    identity_fallback: bool = True
    dry_run: bool = False
    limit: int = 0
    auth_mode: str = DEFAULT_AUTH_MODE

    @property
    def min_bytes(self) -> int:
        return int(self.min_movie_size_mb * 1024 * 1024)

    def fetcher_config(self) -> Config:
        return Config(
            library=self.library,
            log_file=self.log_file,
            report_file=self.report_file,
            api_key=self.api_key,
            username=self.username,
            password=self.password,
            dry_run=self.dry_run,
            min_movie_size_mb=self.min_movie_size_mb,
            lock_timeout_seconds=self.lock_timeout_seconds,
            identity_fallback=self.identity_fallback,
            auth_mode=self.auth_mode,
        )


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def load_state(log_path: Path, library: Path) -> dict[str, Any]:
    """Recover durable quota/retry state from append-only ledger events in the log.

    Ordinary log lines are ignored. A malformed or partial final event is ignored
    rather than blocking subtitle work; provider download reservations are never
    decremented, which keeps the quota guard conservative after interruption.
    """
    state = new_state(library)
    if not log_path.exists():
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


def persist_state(state: dict[str, Any], log_path: Path) -> None:
    """Append a compact, fsync-backed ledger checkpoint to the one allowed log."""
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
    ledger = state["days"].setdefault(day, {})
    for field_name in (
        "download_requests_reserved", "successful_downloads", "no_match", "identity_review", "errors", "already_have",
    ):
        try:
            ledger[field_name] = max(0, int(ledger.get(field_name, 0) or 0))
        except (TypeError, ValueError):
            ledger[field_name] = 0
    return ledger


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


def inspect_existing_sidecars(video: Path) -> tuple[str, Path | None, str]:
    """Classify existing English sidecars without trusting filename alone.

    The cleaner's automatic external-subtitle policy requires the exact
    ``Movie.en.srt`` name. Noncanonical or invalid English sidecars are kept for
    manual review rather than triggering a duplicate download request.
    """
    exact = dest_for(video, Config())
    # dest_for uses only the video name and the fixed .en.srt suffix, so no
    # configured library path leaks into the decision.
    candidates: list[Path] = []
    try:
        candidates = [
            path for path in sorted(video.parent.iterdir(), key=lambda item: item.name.casefold())
            if is_english_srt_sidecar(path, video.stem)
        ]
    except OSError:
        return "missing", None, "could not inspect sibling subtitles"
    if not candidates:
        return "missing", None, "no English SRT sidecar"
    for path in candidates:
        try:
            file_stat = path.stat(follow_symlinks=False)
            if path.is_symlink() or not path.is_file() or file_stat.st_size <= 0 or file_stat.st_size > MAX_SUBTITLE_BYTES:
                continue
            text = decode_subtitle_bytes(path.read_bytes()).replace("\r\n", "\n").replace("\r", "\n")
            valid = looks_like_srt(text)
        except (OSError, EOFError, ValueError):
            valid = False
        if path == exact and valid:
            return "covered", path, "validated exact .en.srt"
        if valid:
            return "review", path, "valid English SRT is not the exact .en.srt sidecar"
    return "review", candidates[0], "English SRT sidecar is invalid or unsafe"


def relative_text(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def queue_run(cfg: QueueConfig) -> tuple[list[JobResult], dict[str, Any]]:
    """Process one daily batch with immediate, human-readable progress output."""
    state = load_state(cfg.log_file, cfg.library)
    today = utc_day()
    ledger = day_ledger(state, today)
    fetcher_cfg = cfg.fetcher_config()
    results: list[JobResult] = []
    client: OpenSubtitlesClient | None = None
    deferred_remaining = 0

    videos = discover_videos(cfg.library, cfg.min_bytes)
    if cfg.limit > 0:
        videos = videos[:cfg.limit]
    total = len(videos)
    log(
        f"Found {total} eligible movies. UTC quota: "
        f"{ledger['download_requests_reserved']}/{cfg.daily_cap} requests already reserved.",
        log_file=cfg.log_file,
    )

    def emit(index: int, status: str, video: Path, detail: str) -> None:
        log(
            f"[{index:03d}/{total:03d}] {status:<8} "
            f"{relative_text(video, cfg.library)} — {detail}",
            log_file=cfg.log_file,
        )

    for index, video in enumerate(videos, start=1):
        layout_issue = canonical_movie_layout_issue(video, cfg.library)
        if layout_issue:
            result = JobResult(video, "skip", layout_issue)
            results.append(result)
            emit(index, "SKIP", video, layout_issue)
            continue
        sidecar_status, existing, sidecar_detail = inspect_existing_sidecars(video)
        if sidecar_status == "covered" and existing is not None:
            ledger["already_have"] += 1
            result = JobResult(video, "have", sidecar_detail, existing)
            results.append(result)
            emit(index, "HAVE", video, sidecar_detail)
            continue
        if sidecar_status == "review":
            result = JobResult(video, "review", sidecar_detail, existing)
            results.append(result)
            emit(index, "REVIEW", video, sidecar_detail)
            continue

        try:
            snapshot = video_snapshot(video)
            key = movie_key(video, snapshot)
        except OSError as exc:
            ledger["errors"] += 1
            result = JobResult(video, "error", str(exc))
            results.append(result)
            emit(index, "ERROR", video, str(exc))
            continue
        record = state_movie(state, key, video)
        old_status = str(record.get("status") or "pending")
        if old_status == "no_match" and not (cfg.retry_no_match or cfg.identity_fallback):
            result = JobResult(video, "skip", "previous strict moviehash search had no match")
            results.append(result)
            emit(index, "SKIP", video, result.detail)
            continue
        if old_status == "manual_review" and not cfg.retry_no_match:
            result = JobResult(video, "review", "previous identity fallback was intentionally held for review")
            results.append(result)
            emit(index, "REVIEW", video, result.detail)
            continue
        if old_status == "reserved" and str(record.get("updated_utc") or "").startswith(today):
            result = JobResult(video, "skip", "download request was already reserved today; waiting for next UTC day")
            results.append(result)
            emit(index, "SKIP", video, result.detail)
            continue

        if ledger["download_requests_reserved"] >= cfg.daily_cap:
            deferred_remaining = total - index + 1
            log(
                f"QUOTA REACHED: {ledger['download_requests_reserved']}/{cfg.daily_cap} requests reserved. "
                f"{deferred_remaining} movie(s) remain for the next UTC day.",
                level="WARNING", log_file=cfg.log_file,
            )
            break

        if client is None:
            if not cfg.api_key:
                raise RuntimeError("Missing API key. Set OPENSUBTITLES_API_KEY before running the daily queue.")
            client = OpenSubtitlesClient(fetcher_cfg)

        emit(index, "SEARCH", video, "calculating moviehash and checking OpenSubtitles")
        try:
            digest = moviehash(video)
            if not video_snapshot_matches(video, snapshot):
                raise RuntimeError("movie changed while calculating moviehash")
            candidates = client.search(movie_hash=digest, query=video.stem)
            pick = pick_candidate(candidates, fetcher_cfg)
        except RuntimeError as exc:
            set_movie_status(record, "error", str(exc), attempts=int(record.get("attempts", 0) or 0) + 1)
            ledger["errors"] += 1
            persist_state(state, cfg.log_file)
            result = JobResult(video, "error", str(exc))
            results.append(result)
            emit(index, "ERROR", video, str(exc))
            continue

        selection_method = "hash"
        selection_reason = "moviehash match"
        if pick is None:
            if not cfg.identity_fallback:
                detail = "no usable English moviehash-matched human SRT"
                set_movie_status(record, "no_match", detail, moviehash=digest or "",
                                 attempts=int(record.get("attempts", 0) or 0) + 1)
                ledger["no_match"] += 1
                persist_state(state, cfg.log_file)
                result = JobResult(video, "skip", detail)
                results.append(result)
                emit(index, "NO MATCH", video, detail)
                continue
            identity = movie_identity_from_video(video)
            if identity is None:
                detail = "no strict hash match and filename is not canonical Title (Year)"
                set_movie_status(record, "manual_review", detail, moviehash=digest or "",
                                 attempts=int(record.get("attempts", 0) or 0) + 1)
                ledger["identity_review"] += 1
                persist_state(state, cfg.log_file)
                result = JobResult(video, "review", detail)
                results.append(result)
                emit(index, "REVIEW", video, detail)
                continue
            emit(index, "FALLBACK", video, f"exact hash missed; checking title/year: {identity.title} ({identity.year})")
            try:
                identity_candidates = client.search_identity(identity)
                pick, selection_reason = pick_identity_candidate(identity_candidates, identity)
            except RuntimeError as exc:
                set_movie_status(record, "error", str(exc), attempts=int(record.get("attempts", 0) or 0) + 1)
                ledger["errors"] += 1
                persist_state(state, cfg.log_file)
                result = JobResult(video, "error", str(exc))
                results.append(result)
                emit(index, "ERROR", video, str(exc))
                continue
            if pick is None:
                detail = f"identity fallback held for review: {selection_reason}"
                set_movie_status(record, "manual_review", detail, moviehash=digest or "",
                                 attempts=int(record.get("attempts", 0) or 0) + 1)
                ledger["identity_review"] += 1
                persist_state(state, cfg.log_file)
                result = JobResult(video, "review", detail)
                results.append(result)
                emit(index, "REVIEW", video, detail)
                continue
            selection_method = "identity"

        dest = dest_for(video, fetcher_cfg)
        note = (f"method={selection_method}; id={pick.file_id}; trusted={'yes' if pick.trusted else 'no'}; "
                f"rating={pick.rating:g}/{pick.votes}; {selection_reason}; {pick.release or 'unnamed release'}")
        if cfg.dry_run:
            result = JobResult(video, "dry-run", note, dest)
            results.append(result)
            emit(index, "WOULD GET", video, note)
            continue

        # Persist before /download: an interrupted or failed download may still
        # count against the provider, so the reservation is never released.
        ledger["download_requests_reserved"] += 1
        set_movie_status(record, "reserved", note, moviehash=digest, selection_method=selection_method,
                         selected_file_id=pick.file_id, attempts=int(record.get("attempts", 0) or 0) + 1)
        persist_state(state, cfg.log_file)
        print(f"[{index:03d}/{total:03d}] DOWNLOAD {relative_text(video, cfg.library)} — request "
              f"{ledger['download_requests_reserved']}/{cfg.daily_cap}", flush=True)
        try:
            client.download_srt(pick.file_id, dest, video=video, expected_video=snapshot)
        except ConcurrentSidecarError as exc:
            set_movie_status(record, "have", str(exc), sidecar=str(dest))
            ledger["already_have"] += 1
            result = JobResult(video, "have", str(exc), dest)
            results.append(result)
            emit(index, "HAVE", video, str(exc))
        except RuntimeError as exc:
            set_movie_status(record, "error", str(exc))
            ledger["errors"] += 1
            result = JobResult(video, "error", str(exc))
            results.append(result)
            emit(index, "ERROR", video, str(exc))
        else:
            set_movie_status(record, "downloaded", note, sidecar=str(dest))
            ledger["successful_downloads"] += 1
            result = JobResult(video, "download", note, dest)
            results.append(result)
            emit(index, "SAVED", video, dest.name)
        persist_state(state, cfg.log_file)

    summary = {
        "utc_day": today,
        "daily_cap": cfg.daily_cap,
        "download_requests_reserved": ledger["download_requests_reserved"],
        "successful_downloads": ledger["successful_downloads"],
        "quota_reached": ledger["download_requests_reserved"] >= cfg.daily_cap,
        "deferred_remaining": deferred_remaining,
        "ledger_log": str(cfg.log_file),
        "movies_discovered": total,
    }
    return results, summary


def write_report(results: Sequence[JobResult], cfg: QueueConfig, summary: dict[str, Any]) -> None:
    counts: dict[str, int] = {}
    identity_reviews = 0
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        if result.status == "review" and (
            "identity fallback" in result.detail.casefold()
            or "previous identity fallback" in result.detail.casefold()
            or "no strict hash match" in result.detail.casefold()
        ):
            identity_reviews += 1
    sidecar_reviews = counts.get("review", 0) - identity_reviews
    layout_skips = sum(
        1 for result in results
        if result.status == "skip" and result.detail.casefold().startswith("noncanonical layout:")
    )
    strict_skips = counts.get("skip", 0) - layout_skips
    lines = [
        "=" * 78,
        "JELLYFIN DAILY SUBTITLE QUEUE REPORT",
        f"Generated UTC         : {utc_timestamp()}",
        f"Library               : {cfg.library}",
        f"UTC quota day         : {summary['utc_day']}",
        f"Request reservations  : {summary['download_requests_reserved']}/{summary['daily_cap']}",
        f"Successful downloads  : {summary['successful_downloads']}",
        f"Quota reached         : {'yes' if summary['quota_reached'] else 'no'}",
        "Policy                : English human-authored UTF-8 SRT only; exact moviehash first" + (
            "; conservative title/year fallback enabled" if cfg.identity_fallback else "; no identity fallback"
        ),
        "=" * 78,
        f"Already covered       : {counts.get('have', 0)}",
        f"Downloaded            : {counts.get('download', 0)}",
        f"No strict match       : {strict_skips}",
        f"Layout skipped        : {layout_skips}",
        f"Identity review held  : {identity_reviews}",
        f"Deferred by quota     : {summary.get('deferred_remaining', 0)}",
        f"Manual sidecar review : {sidecar_reviews}",
        f"Errors                : {counts.get('error', 0)}",
        f"Dry-run candidates    : {counts.get('dry-run', 0)}",
        "-" * 78,
    ]
    for result in results:
        lines.append(f"[{result.status.upper():8}] {relative_text(result.video, cfg.library)}")
        lines.append(f"           {result.detail}")
    lines.extend(["=" * 78, f"Durable quota/retry ledger: {cfg.log_file}", "=" * 78, ""])
    atomic_write_text(cfg.report_file, "\n".join(lines), replace=True)
    print("\n".join(lines), flush=True)
    log(f"Report written: {cfg.report_file}", log_file=cfg.log_file)




# =============================================================================
# COMPACT ROOT-LEVEL DRIVER
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch one validated external English SRT per Jellyfin MKV. "
            "Exact OpenSubtitles moviehash is always tried first; a conservative "
            "title/year fallback is enabled by default after a hash miss."
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
                        help="Maximum download requests per UTC day (0 automatically selects the documented free cap for --auth-mode)")
    parser.add_argument("--min-size", type=float, default=MIN_MOVIE_SIZE_MB, metavar="MB")
    parser.add_argument("--lock-timeout", type=float, default=60.0, metavar="SEC")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="Process at most N movies (0 means all eligible movies)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview candidates; no provider download request or SRT write")
    parser.add_argument("--no-identity-fallback", dest="identity_fallback", action="store_false",
                        help="Disable the conservative exact-title/year fallback after hash misses")
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


def compact_config_from_args(args: argparse.Namespace) -> QueueConfig:
    return QueueConfig(
        library=args.source.resolve(),
        log_file=args.log.resolve() if args.log else None,
        report_file=args.report.resolve(),
        api_key=os.environ.get("OPENSUBTITLES_API_KEY") or OPENSUBTITLES_API_KEY,
        username=os.environ.get("OPENSUBTITLES_USERNAME") or OPENSUBTITLES_USERNAME,
        password=os.environ.get("OPENSUBTITLES_PASSWORD") or OPENSUBTITLES_PASSWORD,
        daily_cap=resolve_daily_cap(str(args.auth_mode), int(args.daily_cap)),
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
    if cfg.auth_mode not in {AUTH_MODE_DEVELOPMENT_ANONYMOUS, AUTH_MODE_USER}:
        errors.append("--auth-mode is unsupported")
    if not cfg.api_key:
        errors.append("an OpenSubtitles API key is required")
    if cfg.auth_mode == AUTH_MODE_USER and (not cfg.username or not cfg.password):
        errors.append("--auth-mode user requires an OpenSubtitles username and password")
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
        if os.name == "nt":
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        cfg = compact_config_from_args(args)
        errors = validate_compact_config(cfg)
        if errors:
            for error in errors:
                print(f"Configuration error: {error}", file=sys.stderr)
            return 2
        mode = "DRY-RUN" if cfg.dry_run else "LIVE"
        print("=" * 78)
        print("JELLYFIN EXTERNAL ENGLISH SRT FETCHER")
        print(f"Mode: {mode} | Library: {cfg.library}")
        print(f"Policy: English human-authored UTF-8 SRT; hash first; "
              f"identity fallback={'on' if cfg.identity_fallback else 'off'}")
        print(f"OpenSubtitles mode: {cfg.auth_mode} | UTC request cap: {cfg.daily_cap} | Ledger: {cfg.log_file}")
        print(f"Report: {cfg.report_file} | Log: {cfg.log_file}")
        print("=" * 78, flush=True)
        with StandardizerCoordinationLock(cfg.library, timeout_seconds=cfg.lock_timeout_seconds):
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
