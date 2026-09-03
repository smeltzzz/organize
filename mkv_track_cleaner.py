#!/usr/bin/env python3
"""
Lossless Canonical Jellyfin MKV Track Cleaner
==============================================
Remux-only (no video/audio re-encode). Keeps the single best English audio
track when one exists; for foreign films with no English audio, a validated
external English SRT unlocks the same cleanup using the best non-commentary
audio of any language. Whenever a validated external English SRT is present it
becomes the sole subtitle option (all embedded subs stripped).

Safety:
  * Idempotent — already-clean files are not rewritten
  * Foreign films without a validated external English SRT are left untouched
  * Foreign films *with* a validated ``.eng.srt`` are cleaned: best audio kept,
    commentary/DVS dropped, every embedded subtitle removed
  * SDH, text-description, and Forced *subtitles* are kept only when no
    validated external SRT exists (never treated as DVS)
  * DVS / commentary *audio* is dropped
  * Post-remux fingerprint verification (tracks, chapters, attachments, duration,
    frames, size) — a bad remux is never swapped over the original
  * Unique same-directory transaction journal + atomic replace; orphan output is
    recoverable only after durable proof of full verification
  * The optional metadata cache stores only `mkvmerge -J` output for files
    whose size and mtime are unchanged. It never stores a decision, so every
    track choice is still made fresh from live state each run.
  * Coordinates with movie_standardizer.py before scanning/remuxing
  * Canonical hardlinked movies are ALWAYS deferred while qBittorrent is
    seeding - there is no override flag. A movie being seeded is left alone.
  * Extra folders (Featurettes, Trailers, BDMV, …) are never processed

On hardlink deferral: movie_standardizer.py is hardlink-only, so a completed
torrent shares an inode with its qBittorrent source and this tool defers it to
avoid holding two copies. qBittorrent's default seed-limit action only PAUSES
the torrent and leaves the file, so that deferral persists until you delete the
source yourself or configure qBittorrent to remove the content.

On the temporary file: a remux cannot rewrite a container in place, so this tool
always stages a full-size sibling temp file and atomically swaps it in. That temp
file becomes the new movie rather than being discarded, free space is verified
before it starts, and the remux is refused outright when there is not enough room.

Pipeline position: run this AFTER movie_standardizer.py and AFTER
subtitle_fetcher.py. A remux rewrites the container bytes, which permanently
changes the OpenSubtitles moviehash for that file; fetching subtitles first is
what preserves exact-hash subtitle matching. This tool warns (but does not
stop) whenever it remuxes a movie that has no validated external English SRT.

    python track_cleaner.py --dry-run
    python track_cleaner.py --dir "E:\\torrents\\final_organized"
    python track_cleaner.py --self-test
"""

from __future__ import annotations

import argparse
import atexit
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import IO, Any

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

COVERING_ENGLISH_SRT_SUFFIXES: tuple[str, ...] = (
    EXTERNAL_SRT_SUFFIX,
    f".{EXTERNAL_SRT_LANG}.sdh.srt",
)

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

class MediaProbeCache:
    """Best-effort ``(path, size, mtime) -> probe payload`` cache.

    ``bitdepth.py`` spawns one ``ffprobe`` per movie and ``mkv_track_cleaner.py``
    spawns one ``mkvmerge -J`` per movie, on every single run, even for a
    library that has not changed since the last sweep. Those subprocesses
    dominate the cost of a maintenance run.

    A probe is a pure function of a file's bytes, so a stored payload is reused
    only while both the size and ``st_mtime_ns`` are unchanged. Crucially, only
    the *probe output* is cached and never a tool's verdict: every consumer
    still re-derives its own decision from live filesystem state. A cached
    entry therefore cannot make a tool blind to a change it must react to — a
    sidecar appearing next to a movie, a hardlink count dropping when seeding
    stops, or a remux landing.

    Deliberately fail-open on reads and fail-silent on writes: a missing,
    unreadable, truncated, corrupt, foreign or stale cache is a miss rather
    than an error, and a cache that cannot be saved costs only the next run's
    speed. Nothing here can turn a correct run into an incorrect one.

    ``path_norm`` keys mean the two tools agree on identity the same way they
    already agree on lock keys.
    """

    SCHEMA = 1

    def __init__(
        self,
        path: Path | str,
        *,
        tool: str = "probe",
        enabled: bool = True,
        max_entries: int = 20000,
    ) -> None:
        self.path = Path(path)
        self.tool = tool
        self.enabled = bool(enabled)
        self.max_entries = max(1, int(max_entries))
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._dirty = False
        if self.enabled:
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        if raw.get("schema") != self.SCHEMA or raw.get("tool") != self.tool:
            # A different tool's cache or an older format: start clean rather
            # than guess at a layout we do not understand.
            return
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return
        self._entries = {
            str(key): value for key, value in entries.items() if isinstance(value, dict)
        }

    def get(self, file_path: Path | str, size: int, mtime_ns: int) -> dict[str, Any] | None:
        """Return a stored payload for an unchanged file, else ``None``."""
        if not self.enabled:
            self.misses += 1
            return None
        key = path_norm(file_path)
        with self._lock:
            entry = self._entries.get(key)
            if (
                entry is not None
                and entry.get("size") == int(size)
                and entry.get("mtime_ns") == int(mtime_ns)
            ):
                payload = entry.get("payload")
                if isinstance(payload, dict):
                    self.hits += 1
                    return payload
            self.misses += 1
            return None

    def put(self, file_path: Path | str, size: int, mtime_ns: int, payload: dict[str, Any]) -> None:
        """Store a probe payload, evicting oldest entries past ``max_entries``."""
        if not self.enabled:
            return
        key = path_norm(file_path)
        with self._lock:
            # Pop-then-insert refreshes recency: a plain dict preserves
            # insertion order but has no OrderedDict.move_to_end.
            self._entries.pop(key, None)
            self._entries[key] = {
                "size": int(size),
                "mtime_ns": int(mtime_ns),
                "payload": payload,
            }
            while len(self._entries) > self.max_entries:
                self._entries.pop(next(iter(self._entries)), None)
            self._dirty = True

    def save(self) -> None:
        """Persist the cache atomically. Failures are swallowed by design."""
        if not self.enabled or not self._dirty:
            return
        with self._lock:
            snapshot = dict(self._entries)
            self._dirty = False
        document = {"schema": self.SCHEMA, "tool": self.tool, "entries": snapshot}
        try:
            atomic_write_text(
                self.path,
                json.dumps(document, separators=(",", ":"), ensure_ascii=False) + "\n",
            )
        except OSError:
            pass

    def __len__(self) -> int:
        return len(self._entries)

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

VERSION = "2.6.2"

# ==================== DEFAULT CONFIGURATION ====================
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


TARGET_DIR = str(resolve_library())
MKVMERGE_PATH = "mkvmerge"
AUDIO_LANGUAGES = {"eng", "en"}
SUBTITLE_LANGUAGES = {"eng", "en"}
REMOVE_COMMENTARY = True
# Logs, reports, and the probe cache live under tools\ReportsAndLogs so the
# root of E:\torrents stays media-only.
LOG_FILE = str(default_tool_dir("mkv_track_cleaner") / "mkv_track_cleaner.log")
REPORT_FILE = str(default_tool_dir("mkv_track_cleaner") / "mkv_track_cleaner_report.txt")
# Reused `mkvmerge -J` metadata for files whose size and mtime have not
# changed, so a re-scan of an unchanged library does not respawn mkvmerge per
# movie. Only the metadata read is cached; every decision is made fresh.
CACHE_FILE = str(default_tool_dir("mkv_track_cleaner") / "mkv_track_cleaner_probe_cache.json")
# ===============================================================

# Legacy temporary files used this deterministic prefix. New work uses unique
# same-directory transaction artifacts, but legacy files are recognized safely.
TEMP_PREFIX = "temp_clean_"
TRANSACTION_MARKER = ".track_cleaner."
TRANSACTION_PART_SUFFIX = ".partial.mkv"
TRANSACTION_JOURNAL_SUFFIX = ".json"
TRANSACTION_SCHEMA_VERSION = 1
LOCK_FILENAME = ".track_cleaner.lock"
STANDARDIZER_LOCK_TIMEOUT_SECONDS = 60.0
ORPHAN_MIN_AGE_SECONDS = 60.0
MIN_OUTPUT_RATIO = 0.50  # remux smaller than 50% of source → reject (likely truncated)
# Hardlinked movies are always deferred. Replacing one would break the seed
# link and consume another full movie-sized allocation until seeding ends.
# The external-SRT size limit and cue pattern are vendored into this script
# (see the shared helpers section below) so this tool cannot drift from the
# others on what counts as a usable subtitle.

KNOWN_MKVMERGE_PATHS = [
    r"C:\Program Files\MKVToolNix\mkvmerge.exe",
    r"C:\Program Files (x86)\MKVToolNix\mkvmerge.exe",
    "/usr/bin/mkvmerge",
    "/usr/local/bin/mkvmerge",
    "/opt/homebrew/bin/mkvmerge",
]

EXTRA_DIR_NAMES = frozenset({
    "featurettes", "extras", "specials", "shorts", "bonus",
    "behind the scenes", "deleted scenes", "interviews", "scenes",
    "trailers", "other", "samples", "sample", "clips",
    "bdmv", "certificate", "video_ts", "audio_ts", "hvdvd_ts",
    "subs", "sub", "subtitles", "subtitle",
    "proof", "screens", "screenshots",
})

SAMPLE_NAME_RE = re.compile(
    r"(?i)(?:^|[._\-\s\[(])(sample|trailer|teaser)(?:[.)\]\-\s_]|$)"
)

_LANG_NORMALIZE = {
    "fre": "fr", "fra": "fr",
    "ger": "de", "deu": "de",
    "gre": "el", "ell": "el",
    "chi": "zh", "zho": "zh",
    "dut": "nl", "nld": "nl",
    "per": "fa", "fas": "fa",
    "rum": "ro", "ron": "ro",
    "slo": "sk", "slk": "sk",
    "cze": "cs", "ces": "cs",
    "wel": "cy", "cym": "cy",
    "baq": "eu", "eus": "eu",
    "arm": "hy", "hye": "hy",
    "geo": "ka", "kat": "ka",
    "bur": "my", "mya": "my",
    "ice": "is", "isl": "is",
    "mac": "mk", "mkd": "mk",
    "may": "ms", "msa": "ms",
    "mao": "mi", "mri": "mi",
    "tib": "bo", "bod": "bo",
    "eng": "en", "spa": "es", "ita": "it", "por": "pt", "jpn": "ja",
    "kor": "ko", "rus": "ru", "ara": "ar", "hin": "hi", "swe": "sv",
    "nor": "no", "dan": "da", "fin": "fi", "pol": "pl", "tur": "tr",
    "vie": "vi", "tha": "th", "ind": "id", "heb": "he", "ukr": "uk",
    "hun": "hu", "bul": "bg", "hrv": "hr", "srp": "sr", "slv": "sl",
    "lit": "lt", "lav": "lv", "est": "et", "cat": "ca",
}

_DISK_SLACK_BYTES = 64 * 1024 * 1024
_DISK_SLACK_RATIO = 0.02

def normalize_language(code: str) -> str:
    return _LANG_NORMALIZE.get((code or "").strip().lower(), (code or "").strip().lower())

def resolve_mkvmerge_path(custom_path: str | None = None) -> str:
    if custom_path:
        found = shutil.which(custom_path)
        if found:
            return found
        p = Path(custom_path)
        if p.is_file():
            return str(p)
        raise FileNotFoundError(f"Specified mkvmerge binary not found: '{custom_path}'")

    found = shutil.which(MKVMERGE_PATH)
    if found:
        return found
    for candidate in KNOWN_MKVMERGE_PATHS:
        found = shutil.which(candidate)
        if found:
            return found
        p = Path(candidate)
        if p.is_file():
            return str(p)
    raise FileNotFoundError(
        f"'{MKVMERGE_PATH}' was not found in PATH or standard locations. "
        "Install MKVToolNix or pass --mkvmerge."
    )

def get_mkvmerge_version(mkvmerge_bin: str) -> str:
    try:
        rc, out, err = _run_mkvmerge([mkvmerge_bin, "--version"])
        if rc not in (0, 1):
            return "unknown version"
        banner = (out or err or "").strip().splitlines()
        return (banner[0] if banner else "unknown version").strip()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return "unknown version"

def format_size(bytes_val: int) -> str:
    if not isinstance(bytes_val, (int, float)) or bytes_val <= 0:
        return "0 Bytes"
    n = float(bytes_val)
    for unit, div in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{int(n)} Bytes"

def format_duration(seconds: float) -> str:
    try:
        seconds_f = float(seconds)
    except (TypeError, ValueError):
        return "0s"
    if seconds_f < 0 or seconds_f != seconds_f:
        return "0s"
    total = int(seconds_f)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    if seconds_f < 10:
        return f"{seconds_f:.1f}s"
    return f"{s}s"

def _this_hostname() -> str:
    try:
        return (socket.gethostname() or "").strip() or "unknown"
    except Exception:
        return "unknown"

def _eta_seconds(elapsed: float, done_bytes: int, total_bytes: int, done_files: int, total_files: int) -> float | None:
    if elapsed <= 0:
        return None
    if total_bytes > 0 and done_bytes > 0:
        remain = total_bytes - done_bytes
        return 0.0 if remain <= 0 else remain / (done_bytes / elapsed)
    if total_files > 0 and done_files > 0:
        remain_n = total_files - done_files
        return 0.0 if remain_n <= 0 else (elapsed / done_files) * remain_n
    return None

def check_free_space(target_dir: Path, source_size: int) -> tuple[bool, int, int, str | None]:
    required = int(max(0, source_size) * (1.0 + _DISK_SLACK_RATIO)) + _DISK_SLACK_BYTES
    try:
        free = int(shutil.disk_usage(str(target_dir)).free)
    except Exception as e:
        return True, 0, required, f"could not query free space: {e}"
    if free < required:
        return False, free, required, None
    return True, free, required, None

def _unix_ns_to_filetime(unix_ns: int) -> tuple[int, int]:
    windows_epoch_offset_ns = 11_644_473_600 * 1_000_000_000
    ft = (int(unix_ns) + windows_epoch_offset_ns) // 100
    if ft < 0:
        ft = 0
    return ft & 0xFFFFFFFF, (ft >> 32) & 0xFFFFFFFF

def _restore_windows_ctime(path: Path, orig_stat: os.stat_result) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        unix_ns = int(getattr(orig_stat, "st_ctime_ns", int(orig_stat.st_ctime * 1_000_000_000)))
        low, high = _unix_ns_to_filetime(unix_ns)

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        creation = FILETIME(low, high)
        windll = getattr(ctypes, "windll", None)
        if not windll:
            return
        kernel32 = windll.kernel32
        handle = kernel32.CreateFileW(str(path), 0x0100, 0x00000007, None, 3, 0x02000000, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return
        try:
            kernel32.SetFileTime(handle, ctypes.byref(creation), None, None)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass

def restore_file_times(path: Path, orig_stat: os.stat_result) -> None:
    try:
        ns = getattr(orig_stat, "st_atime_ns", None)
        ms = getattr(orig_stat, "st_mtime_ns", None)
        if ns is not None and ms is not None:
            os.utime(path, ns=(int(ns), int(ms)))
        else:
            os.utime(path, (orig_stat.st_atime, orig_stat.st_mtime))
    except Exception:
        pass
    _restore_windows_ctime(path, orig_stat)

def apply_low_priority() -> str:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            windll_cls = getattr(ctypes, "WinDLL", None)
            if windll_cls:
                kernel32 = windll_cls("kernel32", use_last_error=True)
                kernel32.GetCurrentProcess.restype = wintypes.HANDLE
                kernel32.GetCurrentProcess.argtypes = []
                kernel32.SetPriorityClass.restype = wintypes.BOOL
                kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel32.GetCurrentThread.restype = wintypes.HANDLE
                kernel32.GetCurrentThread.argtypes = []
                kernel32.SetThreadPriority.restype = wintypes.BOOL
                kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
                if kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000):
                    return "below-normal (Windows)"
                if kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1):
                    return "thread below-normal (Windows)"
                get_err = getattr(ctypes, "get_last_error", lambda: 0)
                return f"unchanged (Windows error {get_err()})"
        except Exception as e:
            return f"unchanged ({e})"
    try:
        os.nice(10)
        return "nice +10"
    except PermissionError:
        return "unchanged (nice: permission denied)"
    except Exception as e:
        return f"unchanged ({e})"

def _print_safe(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        try:
            enc = sys.stdout.encoding or "utf-8"
            sys.stdout.buffer.write((msg + "\n").encode(enc, errors="replace"))
            sys.stdout.buffer.flush()
        except Exception:
            pass
    except Exception:
        pass

def _write_raw(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass

_ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)

def _ellipsize_path(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return "..." + text[-(max_len - 3):]

def _enable_windows_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        windll = getattr(ctypes, "windll", None)
        if windll:
            kernel32 = windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            if not handle or handle == -1:
                return False
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            if mode.value & 0x0004:
                return True
            return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False
    return False

_GUI_PROGRESS_RE = re.compile(
    r"#\s*GUI\s*#\s*progress(?:\s+(\d+)\s*%?|#percent=(\d+)|#parts=(\d+)/(\d+))",
    re.IGNORECASE,
)
_PLAIN_PROGRESS_RE = re.compile(r"Progress:\s*(\d+)\s*%", re.IGNORECASE)
_GUI_MSG_RE = re.compile(r"#\s*GUI\s*#\s*(warning|error)#message=(.*)$", re.IGNORECASE)

def _clamp_percent(value: int) -> int:
    return 0 if value < 0 else 100 if value > 100 else value

def _parse_mkvmerge_progress(line: str) -> int | None:
    if not line:
        return None
    m = _GUI_PROGRESS_RE.search(line)
    if m:
        if m.group(1) is not None:
            return _clamp_percent(int(m.group(1)))
        if m.group(2) is not None:
            return _clamp_percent(int(m.group(2)))
        if m.group(3) is not None and m.group(4) is not None:
            denom = int(m.group(4))
            if denom > 0:
                return _clamp_percent(int(round(100.0 * int(m.group(3)) / denom)))
            return None
    m = _PLAIN_PROGRESS_RE.search(line)
    if m:
        return _clamp_percent(int(m.group(1)))
    return None

def _summarize_mkvmerge_failure(output: str, rc: int) -> str:
    if not (output or "").strip():
        return f"mkvmerge remux failed with code {rc}"
    lines: list[str] = []
    for raw in output.splitlines():
        s = raw.strip()
        if not s:
            continue
        gm = _GUI_MSG_RE.search(s)
        if gm:
            lines.append(f"{gm.group(1).upper()}: {gm.group(2).strip()}")
            continue
        if _parse_mkvmerge_progress(s) is not None:
            continue
        if s.startswith(("#GUI#", "# GUI #")) or s.lower().startswith("progress:"):
            continue
        lines.append(s)
    text = " | ".join(lines).strip() or f"mkvmerge remux failed with code {rc}"
    return text[:497] + "..." if len(text) > 500 else text

class LiveConsole:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"

    def __init__(self, use_color: bool | None = None):
        self.is_tty = False
        try:
            self.is_tty = bool(sys.stdout and sys.stdout.isatty())
        except Exception:
            self.is_tty = False
        env_no_color = bool(os.environ.get("NO_COLOR"))
        env_force = os.environ.get("FORCE_COLOR", "").strip() not in ("", "0")
        env_dumb = os.environ.get("TERM", "").lower() == "dumb"
        if use_color is False:
            want_color = False
        elif use_color is True or env_force:
            want_color = True
        elif env_no_color or env_dumb:
            want_color = False
        else:
            want_color = self.is_tty
        ansi_ok = _enable_windows_vt() if want_color else False
        if os.name != "nt":
            ansi_ok = want_color
        if env_force and use_color is not False:
            ansi_ok = True
        self.use_color = bool(want_color and ansi_ok)
        self._can_erase = self.use_color or (os.name != "nt" and self.is_tty)
        try:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            "█░".encode(enc)
            self._bar_fill, self._bar_empty = "█", "░"
        except Exception:
            self._bar_fill, self._bar_empty = "#", "-"
        self.target_root: Path | None = None
        self._detail_indent = "           "
        self._file_ts = ""
        self._file_tag = ""
        self._file_name = ""
        self._file_size_note = ""
        self._file_line_pending = False
        self._progress_active = False
        self._last_progress_pct = -1
        self._last_progress_draw = 0.0
        self._last_progress_bucket = -1

    def style(self, text: str, *codes: str) -> str:
        if not self.use_color or not codes:
            return text
        return "".join(codes) + text + self.RESET

    def _cols(self) -> int:
        try:
            return max(40, int(shutil.get_terminal_size((100, 24)).columns))
        except Exception:
            return 100

    def _compose_file_line(self, suffix_plain: str = "", suffix_styled: str = "") -> tuple[str, str]:
        prefix = f"[{self._file_ts}] {self._file_tag}"
        reserved = len(prefix) + len(self._file_size_note) + len(suffix_plain)
        budget = max(8, self._cols() - 1 - reserved)
        name_fit = _ellipsize_path(self._file_name, budget)
        plain = f"{prefix}{name_fit}{self._file_size_note}{suffix_plain}"
        styled = (
            self.style(f"[{self._file_ts}]", self.DIM) + " " + self.style(self._file_tag, self.CYAN)
            + self.style(name_fit, self.BOLD) + self.style(self._file_size_note, self.DIM)
            + (suffix_styled if suffix_styled else suffix_plain)
        )
        return plain, styled

    def _overwrite_line(self, text: str) -> None:
        cols = self._cols()
        visible = _strip_ansi(text)
        if len(visible) > cols - 1:
            keep = max(1, cols - 1)
            text = visible[:keep] if keep <= 3 else "..." + visible[-(keep - 3):]
            visible = text
        try:
            if self._can_erase:
                _write_raw("\r" + text + "\033[K")
            else:
                _write_raw("\r" + text + (" " * max(0, cols - 1 - len(visible))))
        except Exception:
            _print_safe(_strip_ansi(text))

    def _commit_open_line(self) -> None:
        if self._file_line_pending or self._progress_active:
            try:
                if self.is_tty:
                    _write_raw("\n")
            except Exception:
                pass
        self._file_line_pending = False
        self._progress_active = False
        self._last_progress_pct = -1
        self._last_progress_bucket = -1

    def finish_progress(self) -> None:
        self._commit_open_line()

    def log_line(self, timestamp: str, level: str, msg: str) -> None:
        self._commit_open_line()
        lvl_color = {"INFO": self.CYAN, "WARNING": self.YELLOW, "ERROR": self.RED,
                     "SUCCESS": self.GREEN, "SKIP": self.DIM}.get(level, self.CYAN)
        if self.use_color:
            body = self.style(msg, self.RED) if level == "ERROR" else msg
            _print_safe(f"{self.style('['+timestamp+']', self.DIM)} {self.style('['+level+']', lvl_color, self.BOLD)} {body}")
        else:
            _print_safe(f"[{timestamp}] [{level}] {msg}")

    def begin_file(self, tag: str, name: str, size: int) -> None:
        self._commit_open_line()
        self._file_ts = datetime.now().strftime("%H:%M:%S")
        self._file_tag = tag
        self._file_name = name
        self._file_size_note = f"  ({format_size(size)})" if size else ""
        self._detail_indent = " " * len(f"[{self._file_ts}] {tag}")
        inspect_plain = "  inspecting..." if self.is_tty else ""
        inspect_styled = self.style(inspect_plain, self.DIM) if inspect_plain else ""
        plain, styled = self._compose_file_line(inspect_plain, inspect_styled)
        shown = styled if self.use_color else plain
        if self.is_tty:
            self._overwrite_line(shown)
            self._file_line_pending = True
        else:
            _print_safe(shown)
            self._file_line_pending = False

    def end_file_inline(self, suffix: str, kind: str = "info") -> None:
        color = {"skip": self.DIM, "error": self.RED, "success": self.GREEN, "warn": self.YELLOW}.get(kind)
        suffix_plain = f"  {suffix}"
        suffix_styled = self.style(suffix_plain, color) if color else suffix_plain
        if self.is_tty and self._file_name:
            plain, styled = self._compose_file_line(suffix_plain, suffix_styled)
            self._overwrite_line(styled if self.use_color else plain)
            _write_raw("\n")
        else:
            indent = self._detail_indent or "           "
            _print_safe(f"{indent}{self.style(suffix, color) if color else suffix}")
        self._file_line_pending = False
        self._progress_active = False
        self._file_name = ""

    def mark_details(self) -> None:
        if self._file_line_pending:
            if self.is_tty and self._file_name:
                plain, styled = self._compose_file_line()
                self._overwrite_line(styled if self.use_color else plain)
            _write_raw("\n")
            self._file_line_pending = False

    def detail(self, msg: str, kind: str = "info") -> None:
        self.mark_details()
        if self._progress_active:
            self._commit_open_line()
        indent = self._detail_indent or ""
        color = {"success": self.GREEN, "error": self.RED, "warn": self.YELLOW}.get(kind, "")
        if self.use_color and color:
            _print_safe(f"{indent}{self.style(msg.strip(), color)}")
        else:
            _print_safe(f"{indent}{msg.strip()}")

    def remux_progress(self, percent: int, started: float) -> None:
        self.mark_details()
        percent = _clamp_percent(int(percent))
        elapsed = time.monotonic() - started
        eta = ""
        if percent >= 4 and elapsed >= 0.8:
            remain = elapsed * (100.0 - percent) / max(percent, 1)
            if remain >= 0:
                eta = f"  ~{format_duration(remain)} left"
        width = 22
        filled = max(0, min(width, int(round(width * percent / 100.0))))
        bar = (
            (self.style(self._bar_fill * filled, self.CYAN) + self.style(self._bar_empty * (width - filled), self.DIM))
            if self.use_color else self._bar_fill * filled + self._bar_empty * (width - filled)
        )
        indent = self._detail_indent or "           "
        prefix = f"{indent}-> remux      : "
        suffix = f" {percent:3d}%  {format_duration(elapsed)} elapsed{eta}"
        now = time.monotonic()
        if self.is_tty:
            if percent == self._last_progress_pct and (now - self._last_progress_draw) < 0.08 and percent < 100:
                return
            self._overwrite_line(f"{prefix}[{bar}]{suffix}")
            self._progress_active = True
            self._last_progress_pct = percent
            self._last_progress_draw = now
        else:
            bucket = percent // 10
            if bucket == self._last_progress_bucket and percent < 100:
                return
            self._last_progress_bucket = bucket
            _print_safe(f"{prefix}[{self._bar_fill * filled}{self._bar_empty * (width - filled)}]{suffix}")
            self._progress_active = False

    def progress_message(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        shown = (self.style(f"[{ts}]", self.DIM) + " " + msg) if self.use_color else f"[{ts}] {msg}"
        if self.is_tty:
            self._overwrite_line(shown)
            self._progress_active = True
        else:
            now = time.monotonic()
            if now - self._last_progress_draw >= 2.0:
                _print_safe(shown)
                self._last_progress_draw = now

_console: LiveConsole | None = None
_target_root: Path | None = None
_interrupt_requested: bool = False
_log_fp: IO[str] | None = None
_log_fp_path: str | None = None
_active_temp_file: Path | None = None
_active_proc: subprocess.Popen | None = None

def _progress_tag(file_index: int, file_total: int) -> str:
    if file_total <= 0 or file_index <= 0:
        return ""
    width = max(len(str(file_total)), 3)
    return f"[{file_index:>{width}d}/{file_total}] "

def _rel_display_name(mkv_path: Path) -> str:
    if _target_root is not None:
        try:
            return str(mkv_path.relative_to(_target_root))
        except ValueError:
            pass
    return mkv_path.name

def _open_log_fp(log_file_path: str) -> IO[str] | None:
    global _log_fp, _log_fp_path
    if _log_fp is not None and _log_fp_path == log_file_path:
        return _log_fp
    close_log_fp()
    try:
        log_dir = os.path.dirname(log_file_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        _log_fp = open(log_file_path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 - module-level append log, closed at exit
        _log_fp_path = log_file_path
        return _log_fp
    except Exception:
        _log_fp = None
        _log_fp_path = None
        return None

def close_log_fp() -> None:
    global _log_fp, _log_fp_path
    fp = _log_fp
    _log_fp = None
    _log_fp_path = None
    if fp is not None:
        try:
            fp.flush()
            fp.close()
        except Exception:
            pass

def log(msg: str, level: str = "INFO", to_console: bool = True, log_file_path: str | None = LOG_FILE):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] [{level}] {msg}"
    if to_console:
        if _console is not None:
            _console.log_line(timestamp, level, msg)
        else:
            _print_safe(formatted)
    if log_file_path and level != "PROGRESS":
        try:
            fp = _open_log_fp(log_file_path)
            if fp is not None:
                fp.write(formatted + "\n")
                fp.flush()
        except Exception:
            pass

def _log_detail(msg: str, log_file_path: str | None, kind: str = "info", level: str = "INFO") -> None:
    if _console is not None:
        _console.detail(msg, kind=kind)
        log(msg, level=level, to_console=False, log_file_path=log_file_path)
    else:
        log(msg, level=level, to_console=True, log_file_path=log_file_path)

# =============================================================================
# TRACK CLASSIFICATION
# =============================================================================

def is_commentary_name(name: str, *, track_type: str = "audio") -> bool:
    """Commentary / isolated-score / DVS *titles*. SDH is NOT commentary."""
    if not name:
        return False
    name_lower = name.lower()
    if re.search(r"\b(commentary|riff|riffing|rifftrax)\b", name_lower):
        return True
    if re.search(
        r"\b(isolated\s+score|isolated\s+music|music\s*(&|and)\s*effects|"
        r"isolated\s+audio|score\s+only)\b",
        name_lower,
    ):
        return True
    if re.search(
        r"\b(audio\s+description|descriptive\s+audio|described\s+video|"
        r"visual\s+description|\bdvs\b)\b",
        name_lower,
    ):
        return True
    # Bare "description" on AUDIO is almost always DVS. On SUBS it is often
    # "English (SDH) / hearing-impaired description" — keep those.
    if track_type != "subtitles" and re.search(r"\b(description|descriptive)\b", name_lower):
        return True
    cleaned = re.sub(
        r"\b(director'?s?|producer'?s?)\s+(cut|edition|version|theatrical)\b",
        "",
        name_lower,
    )
    return bool(
        re.search(
            r"\b(director|directors|producer|producers|cast|crew|"
            r"filmmaker|filmmakers|writer|writers|actor|actors|"
            r"discussion|interview)\b",
            cleaned,
        )
    )

def is_commentary_track(track: dict[str, Any], remove_commentary: bool = True) -> bool:
    """Drop commentary / DVS audio. Never drop hearing-impaired (SDH) subtitles."""
    if not remove_commentary:
        return False
    props = track.get("properties") or {}
    ttype = str(track.get("type") or "")
    if props.get("flag_commentary"):
        return True
    # Matroska's text-descriptions flag marks an accessibility text stream for
    # text-to-speech; it is not commentary and must not discard English SDH.
    # Visual-impaired on AUDIO = descriptive video service. On SUBS it is
    # often how muxers mark SDH — those must be kept.
    if ttype != "subtitles" and props.get("flag_visual_impaired"):
        return True
    return is_commentary_name(str(props.get("track_name") or ""), track_type=ttype)

def is_forced_subtitle(track: dict[str, Any]) -> bool:
    props = track.get("properties") or {}
    if props.get("flag_forced") or props.get("forced_track"):
        return True
    name = str(props.get("track_name") or "")
    return bool(re.search(r"\b(forced|foreign only|signs?/?songs?)\b", name.lower()))

def get_audio_quality_score(track: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    """(codec tier, atmos, channels, bitrate, sample-rate, original-flag)."""
    props = track.get("properties") or {}
    codec_id = str(props.get("codec_id") or "").upper()
    codec = str(track.get("codec") or "").upper()
    name = str(props.get("track_name") or "").upper()
    blob = f"{codec} {codec_id} {name}"

    tier = 10
    if "TRUEHD" in blob or "A_MLP" in codec_id:
        tier = 100
    elif any(k in blob for k in ("DTS-HD MA", "DTS-HD MASTER", "DTS/HD_MA", "DTS:X", "DTS-X")):
        tier = 95
    elif any(k in blob for k in ("PCM", "FLAC", "ALAC", "WAVPACK", "APE", "MONKEY")):
        tier = 90
    elif any(k in blob for k in ("DTS-HD HRA", "DTS-HD HR", "DTS/HD_HRA", "DTS-HD HIGH RESOLUTION", "DTS-HD")):
        tier = 85
    elif any(k in blob for k in ("E-AC-3", "EAC3", "E_AC3", "DOLBY DIGITAL PLUS", "DD+", "DDPLUS")):
        tier = 80
    elif "DTS" in blob:
        tier = 70
    elif any(k in blob for k in ("AC-3", "AC3", "A_AC3", "DOLBY DIGITAL")):
        tier = 60
    elif "OPUS" in blob:
        tier = 50
    elif "AAC" in blob:
        tier = 40
    elif any(k in blob for k in ("MP3", "MPEG", "VORBIS", "WMA")):
        tier = 20

    try:
        channels = int(props.get("audio_channels") or 2)
    except (ValueError, TypeError):
        channels = 2
    atmos_flag = 1 if any(k in blob for k in ("ATMOS", "JOC")) else 0
    try:
        bitrate = int(props.get("tag_bps") or props.get("bps") or props.get("tag_bitrate") or props.get("bitrate") or 0)
    except (ValueError, TypeError):
        bitrate = 0
    try:
        sampling_freq = int(float(props.get("audio_sampling_frequency") or 48000))
    except (ValueError, TypeError):
        sampling_freq = 48000
    original = 1 if props.get("flag_original") else 0
    return (tier, atmos_flag, channels, bitrate, sampling_freq, original)

def is_matching_language(track: dict[str, Any] | None, target_languages: set[str]) -> bool:
    if not track:
        return False
    props = track.get("properties") or {}
    candidates = [
        str(props.get("language") or "").strip().lower(),
        str(props.get("language_ietf") or "").strip().lower(),
        str(props.get("tag_language") or "").strip().lower(),
    ]
    norm_targets = set()
    for c in target_languages:
        base = re.split(r"[-_.]", (c or "").strip().lower())[0]
        norm_targets.add(normalize_language(base))
    for code in candidates:
        if not code:
            continue
        base = re.split(r"[-_.]", code)[0].strip().lower()
        if normalize_language(base) in norm_targets:
            return True
    return False

def name_implies_english(name: str) -> bool:
    if not name:
        return False
    return bool(re.search(r"\b(english|eng)\b", name.lower()))

def is_english_named_untagged(track: dict[str, Any] | None) -> bool:
    if not track:
        return False
    props = track.get("properties") or {}
    lang = str(props.get("language") or "").strip().lower()
    lang_ietf = str(props.get("language_ietf") or "").strip().lower()
    if lang not in ("und", "") or lang_ietf not in ("und", ""):
        return False
    return name_implies_english(str(props.get("track_name") or ""))

def hardlink_count(path: Path) -> int:
    """Return the visible hardlink count; treat unsupported values as one link."""
    try:
        return max(1, int(path.stat().st_nlink))
    except (OSError, AttributeError, TypeError, ValueError):
        return 1

def _fsync_directory(directory: Path) -> None:
    """Best-effort directory sync after atomically replacing a journal file."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

def _source_snapshot(path: Path, stat_result: os.stat_result | None = None) -> dict[str, Any]:
    """Return a cheap identity snapshot used to reject concurrent source changes."""
    st = stat_result if stat_result is not None else path.stat()
    fields: dict[str, Any] = {
        "size": int(st.st_size),
        "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
        "device": int(getattr(st, "st_dev", 0)),
        "inode": int(getattr(st, "st_ino", 0)),
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fields["identity"] = hashlib.sha256(canonical).hexdigest()
    return fields

def _source_snapshot_matches(path: Path, snapshot: dict[str, Any]) -> bool:
    try:
        observed = _source_snapshot(path)
    except OSError:
        return False
    expected = dict(snapshot or {})
    # Older journal records might not carry a digest, but no current run writes
    # one. Compare named fields rather than trusting a malformed record.
    for key in ("size", "mtime_ns", "device", "inode"):
        if key not in expected or expected.get(key) != observed.get(key):
            return False
    return bool(expected.get("identity") == observed.get("identity"))

def _validate_srt_file(sidecar: Path) -> tuple[bool, str, dict[str, Any] | None]:
    """Validate one covering SRT file, returning ``(valid, reason, snapshot)``.

    ``snapshot`` is ``None`` unless the file is a regular, non-symlink,
    size-bounded SRT that decoded cleanly and did not change while being read.
    This is the exact check the single-sidecar path always applied, extracted
    so an unusable ``.eng.srt`` can fall through to a valid ``.eng.sdh.srt``
    instead of hiding it.
    """
    try:
        file_stat = sidecar.stat(follow_symlinks=False)
    except OSError as exc:
        return False, f"could not stat external SRT: {exc}", None
    if sidecar.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
        return False, "external SRT is not a regular non-symlink file", None
    if file_stat.st_size <= 0:
        return False, "external SRT is empty", None
    if file_stat.st_size > EXTERNAL_SRT_MAX_BYTES:
        return False, f"external SRT exceeds {format_size(EXTERNAL_SRT_MAX_BYTES)} safety limit", None
    try:
        raw = sidecar.read_bytes()
    except OSError as exc:
        return False, f"could not read external SRT: {exc}", None
    text = decode_srt_bytes(raw)
    if text is None:
        return False, "external SRT has an unsupported text encoding", None
    if not EXTERNAL_SRT_CUE_RE.search(normalize_srt_newlines(text)):
        return False, "external SRT does not contain a valid numbered cue", None
    try:
        after_read_stat = sidecar.stat(follow_symlinks=False)
    except OSError as exc:
        return False, f"could not re-stat external SRT after reading: {exc}", None
    if _source_snapshot(sidecar, file_stat)["identity"] != _source_snapshot(sidecar, after_read_stat)["identity"]:
        return False, "external SRT changed while being validated", None
    snapshot = _source_snapshot(sidecar, after_read_stat)
    snapshot["sha256"] = hashlib.sha256(raw).hexdigest()
    return True, "", snapshot


def validate_exact_external_english_srt(mkv_path: Path) -> dict[str, Any]:
    """Validate the sole sidecar allowed to replace embedded subtitle choices.

    Either ``<exact MKV stem>.eng.srt`` or ``<exact MKV stem>.eng.sdh.srt``
    beside the movie qualifies, preferring the canonical ``.eng.srt``. A
    validated legacy ``.en.srt`` is renamed to the canonical name first. When
    the preferred name exists but is unusable (wrong encoding, oversized,
    malformed, or a symlink), the alternate covering name is still checked so a
    broken ``.eng.srt`` cannot hide a perfectly valid ``.eng.sdh.srt``. The
    helper stays conservative: with no usable covering sidecar the embedded
    subtitle selection is left unchanged.
    """
    promoted, promote_reason = promote_legacy_external_english_srt(mkv_path)
    covering = [
        mkv_path.with_name(f"{mkv_path.stem}{suffix}")
        for suffix in COVERING_ENGLISH_SRT_SUFFIXES
    ]
    result: dict[str, Any] = {"mkv_path": str(mkv_path), "path": str(covering[0]), "valid": False, "reason": ""}
    if promoted is None and promote_reason and "absent" not in promote_reason:  # noqa: SIM102 - kept nested so the comment below scopes to the inner test
        # Dual-name / occupied-destination cases must not silently drop embeds.
        if "unusable" not in promote_reason:
            result["reason"] = f"legacy external SRT could not be promoted: {promote_reason}"
            return result
    last_reason = "external SRT is absent"
    for candidate in covering:
        ok, reason, snapshot = _validate_srt_file(candidate)
        if ok and snapshot is not None:
            # The record must point at the sidecar that was actually validated
            # (an ``.eng.sdh.srt`` found through the covering list, for
            # example). Leaving the path at the initialized canonical name
            # would make the post-remux snapshot re-check stat a file that
            # never existed and reject an untouched, perfectly valid sidecar.
            result.update({"valid": True, "reason": "", "path": str(candidate), "snapshot": snapshot})
            return result
        # Distinguish "nothing there" from "something there but unusable": a
        # fully-absent pair still reads as absent, while a broken sidecar that
        # hid a valid alternate keeps its fall-through reason for the log.
        if candidate.exists() or candidate.is_symlink():
            last_reason = f"{candidate.name} is unusable ({reason})"
    result["reason"] = last_reason
    return result

def external_srt_snapshot_matches(record: dict[str, Any]) -> bool:
    """Revalidate the external SRT and require the pre-remux identity to match."""
    if not record or not record.get("valid") or not record.get("snapshot"):
        return False
    path = Path(str(record.get("path") or ""))
    mkv_path = Path(str(record.get("mkv_path") or ""))
    current = validate_exact_external_english_srt(mkv_path)
    expected_digest = str((record.get("snapshot") or {}).get("sha256") or "")
    current_digest = str((current.get("snapshot") or {}).get("sha256") or "")
    return (
        bool(current.get("valid"))
        and str(current.get("path") or "") == str(path)
        and bool(re.fullmatch(r"[0-9a-f]{64}", expected_digest))
        and current_digest == expected_digest
        and _source_snapshot_matches(path, record["snapshot"])
    )

def _transaction_token_from_temp_name(name: str) -> str | None:
    if not name.startswith(TEMP_PREFIX):
        return None
    remainder = name[len(TEMP_PREFIX):]
    token, separator, _ = remainder.partition("__")
    if not separator or not re.fullmatch(r"[0-9a-f]{32}", token):
        return None
    return token

def _transaction_journal_path(parent: Path, token: str) -> Path:
    return parent / f"{TRANSACTION_MARKER}{token}{TRANSACTION_JOURNAL_SUFFIX}"

def new_transaction_paths(original: Path) -> tuple[Path, Path, str]:
    """Create unique sibling paths so staging and atomic replacement share a filesystem."""
    token = uuid.uuid4().hex
    temp = original.with_name(f"{TEMP_PREFIX}{token}__{original.name}")
    return temp, _transaction_journal_path(original.parent, token), token

def read_transaction(journal_path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != TRANSACTION_SCHEMA_VERSION:
        return None
    return data

def write_transaction(journal_path: Path, payload: dict[str, Any]) -> None:
    """Durably replace a compact JSON transaction journal on the media volume."""
    tmp = journal_path.with_name(f".{journal_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(journal_path))
        _fsync_directory(journal_path.parent)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

def create_transaction(original: Path, temp: Path, token: str, orig_stat: os.stat_result) -> dict[str, Any]:
    return {
        "schema": TRANSACTION_SCHEMA_VERSION,
        "token": token,
        "phase": "remuxing",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_path": str(original),
        "source_name": original.name,
        "temp_name": temp.name,
        "source_snapshot": _source_snapshot(original, orig_stat),
    }

def cleanup_transaction_artifacts(temp_path: Path, journal_path: Path | None = None) -> None:
    safe_delete(temp_path)
    if journal_path is not None:
        safe_delete(journal_path)

def safe_replace(src_path: Path, dst_path: Path, max_retries: int = 10, initial_delay: float = 0.5) -> bool:
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            os.replace(str(src_path), str(dst_path))
            return True
        except (PermissionError, OSError) as e:
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay = min(delay * 1.5, 3.0)
            else:
                raise e
    return False

def safe_delete(file_path: Path, max_retries: int = 6, delay: float = 0.5):
    for _ in range(max_retries):
        try:
            if file_path.exists():
                file_path.unlink(missing_ok=True)
            return
        except Exception:
            time.sleep(delay)

def describe_track(track: dict[str, Any]) -> str:
    props = track.get("properties") or {}
    tid = track.get("id")
    codec = track.get("codec") or "Unknown"
    channels = props.get("audio_channels")
    lang = props.get("language") or "und"
    name = props.get("track_name") or ""
    ch_str = f" {channels}ch" if channels is not None else ""
    name_str = f" - '{name}'" if name else ""
    return f"ID {tid}: {codec}{ch_str} [{lang}]{name_str}"

def _kill_active_child() -> None:
    proc = _active_proc
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass

def request_interrupt() -> None:
    global _interrupt_requested
    already = _interrupt_requested
    _interrupt_requested = True
    _kill_active_child()
    if already:
        try:
            if _active_temp_file is not None:
                safe_delete(_active_temp_file)
        except Exception:
            pass
        try:
            close_log_fp()
        except Exception:
            pass
        os._exit(1)
    if _console is not None:
        try:
            _console.finish_progress()
        except Exception:
            pass

def _run_mkvmerge(cmd: list[str], on_progress: Callable[[int], None] | None = None) -> tuple[int, str, str]:
    global _active_proc
    live = on_progress is not None
    run_cmd = list(cmd)
    # ``--ui-language`` is not a supported mkvmerge CLI option on current
    # MKVToolNix releases; JSON output is locale-neutral, so do not inject it.
    if live and "--gui-mode" not in run_cmd:
        run_cmd.insert(1, "--gui-mode")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    popen_kwargs: dict[str, Any] = {
        "args": run_cmd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT if live else subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "creationflags": creationflags,
    }
    if live:
        popen_kwargs["bufsize"] = 1

    proc = subprocess.Popen(**popen_kwargs)
    _active_proc = proc
    try:
        if not live:
            stdout, stderr = proc.communicate()
            if _interrupt_requested:
                raise KeyboardInterrupt
            return proc.returncode, stdout, stderr

        output_parts: list[str] = []
        stdout_handle = proc.stdout
        if stdout_handle is None:
            proc.wait()
            return proc.returncode, "", ""

        def _emit(part: str) -> None:
            if not part:
                return
            output_parts.append(part + "\n")
            pct = _parse_mkvmerge_progress(part)
            if pct is not None and on_progress is not None:
                try:
                    on_progress(pct)
                except Exception:
                    pass

        carry = ""
        try:
            while True:
                chunk = stdout_handle.read(256)
                if chunk == "":
                    _emit(carry)
                    break
                carry += chunk
                pieces = re.split(r"[\r\n]+", carry)
                carry = pieces.pop()
                for piece in pieces:
                    _emit(piece)
        finally:
            stdout_handle.close()
        proc.wait()
        if _interrupt_requested:
            raise KeyboardInterrupt
        out = "".join(output_parts)
        return proc.returncode, out, out
    except BaseException:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        try:
            proc.wait()
        except Exception:
            pass
        raise
    finally:
        if _active_proc is proc:
            _active_proc = None

_FINGERPRINT_FLAG_ALIASES = {
    # mkvmerge JSON has used both the Matroska-style ``*_track`` names and
    # newer ``flag_*`` names across fields/releases. Normalize both forms.
    "flag_default": ("flag_default", "default_track"),
    "flag_forced": ("flag_forced", "forced_track"),
    "flag_enabled": ("flag_enabled", "enabled_track"),
    "flag_hearing_impaired": ("flag_hearing_impaired",),
    "flag_visual_impaired": ("flag_visual_impaired",),
    "flag_text_descriptions": ("flag_text_descriptions",),
    "flag_original": ("flag_original",),
    "flag_commentary": ("flag_commentary",),
}

# Only these stable identity/timing fields are emitted by diagnostic mode. Full
# mkvmerge JSON can be enormous and includes regenerated statistics; those are
# deliberately not used as the verifier contract.
_TRACK_DIAGNOSTIC_PROPERTY_KEYS = (
    "codec_id", "pixel_dimensions", "display_dimensions", "default_duration",
    "tag_number_of_frames", "language", "language_ietf", "track_name",
    "audio_channels", "audio_sampling_frequency",
    *tuple(alias for aliases in _FINGERPRINT_FLAG_ALIASES.values() for alias in aliases),
)

def _bool_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)

def _normal_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def track_fingerprint(
    track: dict[str, Any], *, default_override: bool | None = None,
    forced_override: bool | None = None,
) -> dict[str, Any]:
    """Return stable track metadata sufficient to detect wrong retained streams.

    IDs and statistics tags are intentionally excluded because mkvmerge may
    legitimately renumber tracks and regenerate statistics during a remux.
    """
    props = track.get("properties") or {}
    flags = {
        key: next((_bool_flag(props.get(alias)) for alias in aliases if alias in props), False)
        for key, aliases in _FINGERPRINT_FLAG_ALIASES.items()
    }
    if default_override is not None:
        flags["flag_default"] = bool(default_override)
    if forced_override is not None:
        flags["flag_forced"] = bool(forced_override)
    language = str(props.get("language") or "und").strip().lower()
    raw_language_ietf = str(props.get("language_ietf") or "").strip().lower()
    # MKVToolNix can materialize a BCP-47 tag (for example, ``en``) in a
    # remuxed file when the source represented the same language only through
    # legacy ISO-639 metadata (for example, ``eng``). Treat the absent source
    # tag as its canonical language equivalent, but preserve an explicit tag
    # when present so genuine language/tag changes remain verification failures.
    language_ietf = raw_language_ietf or normalize_language(language)
    result: dict[str, Any] = {
        "type": str(track.get("type") or ""),
        "codec": str(track.get("codec") or ""),
        "codec_id": str(props.get("codec_id") or ""),
        "language": language,
        "language_ietf": language_ietf,
        "name": str(props.get("track_name") or ""),
        "flags": flags,
    }
    if result["type"] == "audio":
        result.update({
            "channels": _normal_int(props.get("audio_channels")),
            "sample_rate": _normal_int(props.get("audio_sampling_frequency")),
        })
    elif result["type"] == "video":
        result.update({
            "pixel_dimensions": str(props.get("pixel_dimensions") or ""),
            "display_dimensions": str(props.get("display_dimensions") or ""),
            "default_duration": _normal_int(props.get("default_duration")),
        })
    return result

def _diagnostic_track_record(track: dict[str, Any]) -> dict[str, Any]:
    """Return compact raw and normalized metadata for a failed-track diagnosis."""
    props = track.get("properties") or {}
    raw_properties = {
        key: props.get(key) for key in _TRACK_DIAGNOSTIC_PROPERTY_KEYS if key in props
    }
    return {
        "track_id": track.get("id"),
        "type": track.get("type"),
        "codec": track.get("codec"),
        "raw_properties": raw_properties,
        "normalized_fingerprint": track_fingerprint(track),
    }

def build_verification_diagnostic(
    source_info: dict[str, Any], output_info: dict[str, Any] | None, plan: dict[str, Any], reason: str,
) -> dict[str, Any]:
    """Produce actionable evidence for a fail-closed remux mismatch.

    The payload intentionally records only comparison-relevant metadata. It is
    diagnostic evidence and never changes the verification decision.
    """
    source_tracks = source_info.get("tracks") or []
    output_tracks = (output_info or {}).get("tracks") or []
    return {
        "reason": reason,
        "expected_audio_fingerprint": plan.get("audio"),
        "actual_audio_fingerprints": _fingerprint_list(
            [track for track in output_tracks if track.get("type") == "audio"]
        ),
        "source_audio_tracks": [
            _diagnostic_track_record(track) for track in source_tracks if track.get("type") == "audio"
        ],
        "output_audio_tracks": [
            _diagnostic_track_record(track) for track in output_tracks if track.get("type") == "audio"
        ],
        "expected_video_fingerprints": (plan.get("preserved_tracks") or {}).get("video", []),
        "actual_video_fingerprints": _fingerprint_list(
            [track for track in output_tracks if track.get("type") == "video"]
        ),
        "source_video_tracks": [
            _diagnostic_track_record(track) for track in source_tracks if track.get("type") == "video"
        ],
        "output_video_tracks": [
            _diagnostic_track_record(track) for track in output_tracks if track.get("type") == "video"
        ],
        "expected_video_frame_counts": plan.get("video_frame_counts", []),
        "actual_video_frame_counts": _video_frame_counts(output_tracks),
        "expected_duration_ns": plan.get("source_duration_ns"),
        "actual_duration_ns": (((output_info or {}).get("container") or {}).get("properties") or {}).get("duration"),
    }

def _fingerprint_key(fingerprint: dict[str, Any]) -> str:
    return json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def _fingerprint_list(tracks: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
    return sorted((track_fingerprint(track, **kwargs) for track in tracks), key=_fingerprint_key)

def retained_audio_fingerprint_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Compare the selected audio contract without masking a real stream change.

    MKVToolNix 100 preserves the AAC payload but can report an AAC stream whose
    source Matroska ``audio_channels`` value is 7 as 8 after remux. The observed
    difference is the well-known AAC 7.1 reporting ambiguity, not a changed
    stream: codec, language, sample rate, flags, and the extracted raw AAC bytes
    remain identical. Accept only that exact 7-to-8 representation change and
    require every other normalized audio fingerprint field to match exactly.
    """
    if actual == expected:
        return True
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    if (
        actual.get("type") != "audio"
        or expected.get("type") != "audio"
        or actual.get("codec_id") != "A_AAC"
        or expected.get("codec_id") != "A_AAC"
        or expected.get("channels") != 7
        or actual.get("channels") != 8
    ):
        return False
    normalized_expected = dict(expected)
    normalized_expected["channels"] = 8
    return actual == normalized_expected

def _chapter_entry_count(info: dict[str, Any]) -> int:
    return sum(int(c.get("num_entries", 0) or 0) for c in (info.get("chapters") or []))

def _video_frame_counts(tracks: list[dict[str, Any]]) -> list[int | None]:
    return sorted(
        (_normal_int((track.get("properties") or {}).get("tag_number_of_frames"))
         for track in tracks if track.get("type") == "video"),
        key=lambda value: (-1 if value is None else value),
    )

def build_verification_plan(
    input_info: dict[str, Any], best_audio: dict[str, Any], keep_subtitles: list[dict[str, Any]],
    source_size: int,
) -> dict[str, Any]:
    """Build a JSON-serializable contract for normal and orphan remux verification."""
    tracks = input_info.get("tracks") or []
    preserved: dict[str, list[dict[str, Any]]] = {}
    for track_type in ("video", "buttons", "menu", "complex"):
        preserved[track_type] = _fingerprint_list(
            [track for track in tracks if track.get("type") == track_type]
        )
    return {
        "source_size": int(source_size),
        "audio": track_fingerprint(best_audio, default_override=True),
        "subtitles": sorted(
            (
                track_fingerprint(
                    subtitle, default_override=False, forced_override=is_forced_subtitle(subtitle),
                )
                for subtitle in keep_subtitles
            ),
            key=_fingerprint_key,
        ),
        "preserved_tracks": preserved,
        "attachment_count": len(input_info.get("attachments") or []),
        "chapter_entries": _chapter_entry_count(input_info),
        "video_frame_counts": _video_frame_counts(tracks),
        "source_duration_ns": ((input_info.get("container") or {}).get("properties") or {}).get("duration"),
    }

def _verify_remux_info(temp_path: Path, out_info: dict[str, Any], plan: dict[str, Any]) -> tuple[bool, str]:
    container = out_info.get("container") or {}
    if not (container.get("recognized") and container.get("supported")):
        return False, "remuxed file is not a recognized/supported media container"
    try:
        out_size = temp_path.stat().st_size
    except OSError as exc:
        return False, f"could not stat remuxed file: {exc}"
    source_size = int(plan.get("source_size") or 0)
    if out_size < 1024:
        return False, f"remuxed file is tiny ({format_size(out_size)})"
    if source_size > 0 and out_size < int(source_size * MIN_OUTPUT_RATIO):
        return False, (
            f"remuxed file shrank too much ({format_size(source_size)} -> {format_size(out_size)}); "
            "refusing to replace original"
        )

    out_tracks = out_info.get("tracks") or []
    out_audio = [track for track in out_tracks if track.get("type") == "audio"]
    if len(out_audio) != 1:
        return False, f"expected exactly 1 audio track in output, found {len(out_audio)}"
    if not retained_audio_fingerprint_matches(track_fingerprint(out_audio[0]), plan.get("audio") or {}):
        return False, "retained audio fingerprint differs from the selected source audio"

    actual_subtitles = _fingerprint_list([track for track in out_tracks if track.get("type") == "subtitles"])
    if actual_subtitles != plan.get("subtitles", []):
        return False, "retained subtitle fingerprints differ from the planned subtitle set"

    for track_type, expected in (plan.get("preserved_tracks") or {}).items():
        actual = _fingerprint_list([track for track in out_tracks if track.get("type") == track_type])
        if actual != expected:
            return False, f"{track_type} track fingerprints changed during remux"
    if len(out_info.get("attachments") or []) != int(plan.get("attachment_count") or 0):
        return False, "attachment count changed during remux"
    if _chapter_entry_count(out_info) != int(plan.get("chapter_entries") or 0):
        return False, "chapter count changed during remux"

    # MKVToolNix can generate the NumberOfFrames statistics tag during a remux
    # when the source did not carry one. Frame counts present in the source
    # remain a verification signal, but newly materialized output-only values
    # are informational and must not reject an otherwise identical remux.
    expected_frames = plan.get("video_frame_counts", [])
    actual_frames = _video_frame_counts(out_tracks)
    remaining_actual_frames = list(actual_frames)
    for expected_frame in expected_frames:
        if expected_frame is None:
            continue
        try:
            remaining_actual_frames.remove(expected_frame)
        except ValueError:
            return False, (
                "a source video frame count is absent from the remuxed output "
                f"({expected_frames} -> {actual_frames})"
            )

    # Container duration is the longest track, not necessarily picture duration.
    # Removing padded commentary/DVS can legitimately shorten it; without usable
    # frame counts, retain the old conservative floor to reject a truncated output.
    in_duration = plan.get("source_duration_ns")
    out_duration = ((out_info.get("container") or {}).get("properties") or {}).get("duration")
    if in_duration and out_duration:
        try:
            in_ns, out_ns = int(in_duration), int(out_duration)
            slack = max(1_000_000_000, int(in_ns * 0.02))
            frames_confirmed = bool(expected_frames) and expected_frames == actual_frames and all(
                value is not None for value in expected_frames
            )
            if out_ns > in_ns + slack:
                return False, f"duration grew during remux ({in_ns / 1e9:.2f}s -> {out_ns / 1e9:.2f}s)"
            if not frames_confirmed and out_ns + slack < in_ns * 0.85:
                return False, (
                    f"duration shrank too much during remux ({in_ns / 1e9:.2f}s -> {out_ns / 1e9:.2f}s) "
                    "and usable video frame counts were not available"
                )
        except (TypeError, ValueError):
            pass
    return True, ""

def verify_remux_output(
    temp_path: Path, mkvmerge_bin: str, plan: dict[str, Any],
) -> tuple[bool, str, dict[str, Any] | None]:
    try:
        rc, out, err = _run_mkvmerge([mkvmerge_bin, "-J", str(temp_path)])
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        return False, f"could not re-inspect remuxed file: {exc}", None
    if rc not in (0, 1):
        return False, f"remuxed file inspection failed (code {rc}): {(err or '').strip()[:300]}", None
    try:
        out_info = json.loads(out or "{}")
    except (TypeError, ValueError) as exc:
        return False, f"could not parse remuxed file metadata: {exc}", None
    ok, reason = _verify_remux_info(temp_path, out_info, plan)
    return ok, reason, out_info

def _pid_alive(pid: int) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            windll = getattr(ctypes, "windll", None)
            if windll:
                kernel32 = windll.kernel32
                handle = kernel32.OpenProcess(0x1000, False, pid)
                if not handle:
                    return False
                kernel32.CloseHandle(handle)
                return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, OverflowError, ValueError):
        return True
    return True

def acquire_lock(lock_path: Path, log_file_path: str | None = LOG_FILE) -> bool:
    try:
        for _ in range(2):
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(f"{_this_hostname()}\n{os.getpid()}\n{time.time()}\n")
                return True
            except FileExistsError:
                host_str = ""
                pid_str = ""
                try:
                    content = lock_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                    if (len(content) >= 2 and not content[0].strip().isdigit()
                            and content[1].strip().isdigit()):
                        host_str, pid_str = content[0].strip(), content[1].strip()
                    elif content and content[0].strip().isdigit():
                        host_str, pid_str = _this_hostname(), content[0].strip()
                    else:
                        log(f"Lock file '{lock_path}' is unreadable; assuming another instance holds it. Exiting.",
                            level="WARNING", log_file_path=log_file_path)
                        return False
                except OSError:
                    return False
                same_host = host_str.casefold() == _this_hostname().casefold()
                pid_live = pid_str.isdigit() and _pid_alive(int(pid_str))
                if not same_host:
                    log(f"Another instance appears to be running on host '{host_str}' "
                        f"(lock held by PID {pid_str or 'unknown'}). Exiting.",
                        level="WARNING", log_file_path=log_file_path)
                    return False
                if pid_live:
                    log(f"Another instance appears to be running (lock held by PID {pid_str}). Exiting.",
                        level="WARNING", log_file_path=log_file_path)
                    return False
                log(f"Removing stale lock file from a previous run (PID {pid_str or 'unknown'}).",
                    level="WARNING", log_file_path=log_file_path)
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    return False
            except OSError as e:
                log(f"Could not create lock file '{lock_path}': {e}",
                    level="ERROR", log_file_path=log_file_path)
                return False
        return False
    except Exception as e:
        log(f"Lock acquisition error: {e}", level="ERROR", log_file_path=log_file_path)
        return False

def release_lock(lock_path: Path):
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass

def cleanup_orphan_temps(target_path: Path, mkvmerge_bin: str, log_file_path: str | None = LOG_FILE) -> int:
    """Resolve only journal-proven, fully verified interrupted transactions.

    A recognized MKV is not evidence that it passed this program's selection and
    integrity checks. Therefore legacy files and incomplete/new unverified
    transactions remain available for manual review whenever the original is
    absent; they are never promoted automatically.
    """
    log("Checking for orphaned remux transactions from previous runs...", log_file_path=log_file_path)
    handled = 0
    preserved = 0
    now = time.time()
    for root, _, files in os.walk(target_path):
        if _interrupt_requested:
            break
        for filename in files:
            if _interrupt_requested:
                break
            if not (filename.startswith(TEMP_PREFIX) and filename.lower().endswith(".mkv")):
                continue
            temp = Path(root) / filename
            try:
                if now - temp.stat().st_mtime < ORPHAN_MIN_AGE_SECONDS:
                    continue
            except OSError:
                continue

            token = _transaction_token_from_temp_name(filename)
            if token is None:
                original_name = filename[len(TEMP_PREFIX):]
                original = Path(root) / original_name if original_name else None
                if original is not None and original.exists():
                    log(f"Removing legacy orphan temp beside intact original: '{filename}'",
                        level="WARNING", log_file_path=log_file_path)
                    safe_delete(temp)
                    handled += 1
                else:
                    log(f"Leaving legacy orphan temp for manual review: '{filename}'",
                        level="WARNING", log_file_path=log_file_path)
                    preserved += 1
                continue

            journal_path = _transaction_journal_path(Path(root), token)
            journal = read_transaction(journal_path)
            source_name = str((journal or {}).get("source_name") or "")
            valid_names = (
                journal is not None
                and journal.get("token") == token
                and journal.get("temp_name") == filename
                and bool(source_name)
                and Path(source_name).name == source_name
                and source_name not in {".", ".."}
            )
            if not valid_names:
                log(f"Leaving temp without a valid transaction journal: '{filename}'",
                    level="WARNING", log_file_path=log_file_path)
                preserved += 1
                continue
            assert journal is not None

            original = Path(root) / source_name
            if original.exists():
                log(f"Removing completed/abandoned transaction artifacts beside intact original: '{filename}'",
                    level="WARNING", log_file_path=log_file_path)
                cleanup_transaction_artifacts(temp, journal_path)
                handled += 1
                continue
            if journal.get("phase") != "verified":
                log(f"Leaving unverified orphan remux for manual review: '{filename}'",
                    level="WARNING", log_file_path=log_file_path)
                preserved += 1
                continue
            if not _source_snapshot_matches(temp, journal.get("temp_snapshot") or {}):
                log(f"Leaving changed verified temp for manual review: '{filename}'",
                    level="WARNING", log_file_path=log_file_path)
                preserved += 1
                continue
            plan = journal.get("verification_plan")
            if not isinstance(plan, dict):
                log(f"Leaving verified temp with incomplete journal for manual review: '{filename}'",
                    level="WARNING", log_file_path=log_file_path)
                preserved += 1
                continue
            external_srt = journal.get("external_srt")
            if external_srt is not None and not external_srt_snapshot_matches(external_srt):
                log(f"Leaving verified temp because its validated external SRT changed: '{filename}'",
                    level="WARNING", log_file_path=log_file_path)
                preserved += 1
                continue
            try:
                ok, reason, _ = verify_remux_output(temp, mkvmerge_bin, plan)
                if not ok:
                    log(f"Leaving failed-verification orphan remux for manual review: '{filename}' ({reason})",
                        level="WARNING", log_file_path=log_file_path)
                    preserved += 1
                    continue
                log(f"Recovering journal-verified remux: '{filename}' -> '{source_name}'",
                    level="WARNING", log_file_path=log_file_path)
                safe_replace(temp, original)
                safe_delete(journal_path)
                handled += 1
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                log(f"Could not recover verified temp '{filename}': {exc}; leaving it",
                    level="WARNING", log_file_path=log_file_path)
                preserved += 1

    # A crash immediately after a successful os.replace can leave the journal
    # behind while the temp path no longer exists. That is safe to remove only
    # after validating the journal's naming contract and confirming the original
    # is present; never infer recovery from a journal alone when the original is
    # missing.
    for root, _, files in os.walk(target_path):
        if _interrupt_requested:
            break
        for filename in files:
            if not (filename.startswith(TRANSACTION_MARKER) and filename.endswith(TRANSACTION_JOURNAL_SUFFIX)):
                continue
            journal_path = Path(root) / filename
            journal = read_transaction(journal_path)
            token = str((journal or {}).get("token") or "")
            source_name = str((journal or {}).get("source_name") or "")
            temp_name = str((journal or {}).get("temp_name") or "")
            if not (
                journal is not None
                and re.fullmatch(r"[0-9a-f]{32}", token)
                and journal_path == _transaction_journal_path(Path(root), token)
                and Path(source_name).name == source_name
                and Path(temp_name).name == temp_name
                and _transaction_token_from_temp_name(temp_name) == token
            ):
                continue
            original = Path(root) / source_name
            temp = Path(root) / temp_name
            if original.exists() and not temp.exists():
                log(f"Removing stale transaction journal beside intact original: '{filename}'",
                    level="WARNING", log_file_path=log_file_path)
                safe_delete(journal_path)
                handled += 1

    if handled or preserved:
        log(
            f"Orphan recovery finished: cleaned/recovered {handled}; retained for manual review {preserved}.",
            log_file_path=log_file_path,
        )
    else:
        log("No orphaned remux transactions found.", log_file_path=log_file_path)
    return handled

def _in_extra_dir(path: Path, root: Path) -> bool:
    try:
        rel = path.parent.relative_to(root)
    except ValueError:
        rel = path.parent
    return any(part.strip().lower() in EXTRA_DIR_NAMES for part in rel.parts)

def canonical_movie_layout_issue(mkv_path: Path, target_root: Path) -> str | None:
    """Return a reason when a movie does not follow the canonical folder contract."""
    parent = mkv_path.parent
    if parent == target_root:
        return "noncanonical layout: MKV is directly under the library root"
    if mkv_path.is_symlink() or parent.is_symlink() or not mkv_path.is_file():
        return "noncanonical layout: MKV is not a regular non-symlink file in a regular folder"
    if mkv_path.stem.casefold() != parent.name.casefold():
        return "noncanonical layout: MKV stem does not match its movie-folder name"
    try:
        siblings = [
            path for path in parent.iterdir()
            if path.suffix.lower() == ".mkv" and path.is_file() and not path.is_symlink()
            and not path.name.startswith(TEMP_PREFIX) and not SAMPLE_NAME_RE.search(path.stem)
        ]
    except OSError as exc:
        return f"noncanonical layout: could not inspect movie folder ({exc})"
    if len(siblings) != 1:
        return f"noncanonical layout: expected one regular MKV in movie folder, found {len(siblings)}"
    return None

def discover_mkv_files(
    target_path: Path,
    log_file_path: str | None,
    onerror: Callable[[OSError], None] | None = None,
    *,
    skip_extras: bool = True,
    min_size: int = 0,
) -> tuple[list[Path], list[int], int]:
    log(f"Scanning '{target_path}' for MKV files...", log_file_path=log_file_path)
    found: list[tuple[Path, int]] = []
    total_bytes = 0
    last_draw = 0.0
    walk_kwargs: dict[str, Any] = {}
    if onerror is not None:
        walk_kwargs["onerror"] = onerror
    for root, dirnames, names in os.walk(target_path, **walk_kwargs):
        if _interrupt_requested:
            break
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if skip_extras:
            dirnames[:] = [d for d in dirnames if d.strip().lower() not in EXTRA_DIR_NAMES]
        for f in names:
            if _interrupt_requested:
                break
            if not f.lower().endswith(".mkv") or f.startswith(TEMP_PREFIX):
                continue
            if SAMPLE_NAME_RE.search(Path(f).stem):
                continue
            pth = Path(root) / f
            if skip_extras and _in_extra_dir(pth, target_path):
                continue
            try:
                sz = int(pth.stat().st_size)
            except OSError:
                sz = 0
            if min_size and sz < min_size:
                continue
            found.append((pth, sz))
            total_bytes += sz
            if _console is not None:
                now = time.monotonic()
                if now - last_draw >= 0.12:
                    _console.progress_message(f"Scanning...  {len(found)} MKV file(s) found")
                    last_draw = now
    if _console is not None:
        _console.finish_progress()
    found.sort(key=lambda item: os.path.normcase(str(item[0])))
    files = [item[0] for item in found]
    sizes = [item[1] for item in found]
    log(f"Found {len(files)} MKV file(s) totaling {format_size(total_bytes)}", log_file_path=log_file_path)
    return files, sizes, total_bytes

def _log_live_totals(
    stats: dict[str, Any], index: int, total: int, run_started: float,
    log_file_path: str | None, done_bytes: int = 0, total_bytes: int = 0,
) -> None:
    elapsed = time.monotonic() - run_started
    remain_est = _eta_seconds(elapsed, done_bytes, total_bytes, index, total)
    eta = f"  ~{format_duration(remain_est)} left" if remain_est is not None else ""
    byte_part = f"  {format_size(done_bytes)}/{format_size(total_bytes)}" if total_bytes > 0 else ""
    msg = (
        f"-- {index}/{total}  cleaned {len(stats['cleaned'])}  "
        f"already {len(stats['already_clean'])}  "
        f"deferred {len(stats.get('deferred_hardlinked', []))}  "
        f"skipped {len(stats['skipped_no_english'])}  "
        f"layout {len(stats.get('skipped_layout', []))}  "
        f"errors {len(stats['errors'])}  "
        f"saved {format_size(stats['total_space_saved_bytes'])}"
        f"{byte_part}  elapsed {format_duration(elapsed)}{eta}"
    )
    log(msg, log_file_path=log_file_path)

def process_mkv(
    mkv_path: Path,
    stats: dict[str, Any],
    mkvmerge_bin: str,
    dry_run: bool = False,
    remove_commentary: bool | None = None,
    audio_langs: set[str] | None = None,
    sub_langs: set[str] | None = None,
    log_file_path: str | None = LOG_FILE,
    file_index: int = 0,
    file_total: int = 0,
    probe_cache: MediaProbeCache | None = None,
):
    global _active_temp_file
    if _interrupt_requested:
        raise KeyboardInterrupt
    proc_start = time.monotonic()
    if remove_commentary is None:
        remove_commentary = REMOVE_COMMENTARY
    if audio_langs is None:
        audio_langs = AUDIO_LANGUAGES
    if sub_langs is None:
        sub_langs = SUBTITLE_LANGUAGES

    movie_name = mkv_path.name
    display_name = _rel_display_name(mkv_path)
    tag = _progress_tag(file_index, file_total)
    temp_output: Path | None = None
    journal_path: Path | None = None

    orig_stat: os.stat_result | None = None
    try:
        orig_stat = mkv_path.stat()
        size_before = orig_stat.st_size
    except Exception:
        size_before = 0

    if _console is not None:
        _console.begin_file(tag, display_name, size_before)
    layout_issue = canonical_movie_layout_issue(mkv_path, _target_root) if _target_root is not None else None
    if layout_issue:
        if _console is not None:
            _console.end_file_inline("skipped (noncanonical layout)", kind="warn")
        log(f"{tag}Skipping '{display_name}' ({layout_issue})", level="WARNING", to_console=_console is None,
            log_file_path=log_file_path)
        stats.setdefault("skipped_layout", []).append({"name": movie_name, "reason": layout_issue})
        return

    if orig_stat is None:
        err_msg = "could not stat source file; refusing to remux without a stable source snapshot"
        if _console is not None:
            _console.end_file_inline(f"ERROR: {err_msg}", kind="error")
        log(f"{tag}{err_msg}: '{display_name}'", level="ERROR", to_console=_console is None,
            log_file_path=log_file_path)
        stats["errors"].append({"name": movie_name, "error": err_msg})
        return

    # Hard policy: never touch a movie that is still being seeded. There is
    # deliberately no override flag - release the source copy first.
    links = hardlink_count(mkv_path)
    if links > 1:
        reason = f"deferred: {links} hardlinks (seeded source still linked)"
        if _console is not None:
            _console.end_file_inline(reason, kind="skip")
        log(
            f"{tag}Deferring '{display_name}': it has {links} hardlinks, so the seeded source "
            "copy still shares this file and the movie is still being served. Left completely "
            "untouched. Note that qBittorrent's default 'stop seeding' action only PAUSES the "
            "torrent and leaves the file in place, so this can persist forever. Either let "
            "qBittorrent delete the content when seeding stops, or remove the source yourself. "
            "There is no flag to force it.",
            level="WARNING", to_console=_console is None, log_file_path=log_file_path,
        )
        stats.setdefault("deferred_hardlinked", []).append({"name": movie_name, "hardlinks": links})
        return

    try:
        # Only the mkvmerge metadata read is cached, never the cleaning
        # decision. Everything below still runs on live state, so a sidecar
        # appearing beside the movie or a hardlink count dropping when seeding
        # stops is still acted on immediately.
        media_info: dict[str, Any] | None = None
        if probe_cache is not None:
            media_info = probe_cache.get(mkv_path, orig_stat.st_size, orig_stat.st_mtime_ns)
        if media_info is None:
            rc, stdout, stderr = _run_mkvmerge([mkvmerge_bin, "-J", str(mkv_path)])
            if rc not in (0, 1):
                err_msg = (stderr or "").strip() or f"mkvmerge exited with code {rc}"
                if _console is not None:
                    _console.end_file_inline(f"ERROR: {err_msg}", kind="error")
                log(f"{tag}Metadata inspection failed for '{display_name}': {err_msg}",
                    level="ERROR", to_console=_console is None, log_file_path=log_file_path)
                stats["errors"].append({"name": movie_name, "error": err_msg})
                return
            media_info = json.loads(stdout)
            if probe_cache is not None:
                probe_cache.put(mkv_path, orig_stat.st_size, orig_stat.st_mtime_ns, media_info)
        container = media_info.get("container") or {}
        if not (container.get("recognized") and container.get("supported")):
            err_msg = "file is not a recognized/supported media container"
            if _console is not None:
                _console.end_file_inline(f"ERROR: {err_msg}", kind="error")
            log(f"{tag}Metadata inspection failed for '{display_name}': {err_msg}",
                level="ERROR", to_console=_console is None, log_file_path=log_file_path)
            stats["errors"].append({"name": movie_name, "error": err_msg})
            return
    except json.JSONDecodeError as e:
        err_msg = f"Invalid mkvmerge JSON: {e}"
        if _console is not None:
            _console.end_file_inline(f"ERROR: {err_msg}", kind="error")
        log(f"{tag}Invalid metadata output for '{display_name}': {e}",
            level="ERROR", to_console=_console is None, log_file_path=log_file_path)
        stats["errors"].append({"name": movie_name, "error": err_msg})
        return
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        err_msg = f"Metadata inspection exception: {e}"
        if _console is not None:
            _console.end_file_inline(f"ERROR: {err_msg}", kind="error")
        log(f"{tag}Failed to inspect metadata for '{display_name}': {e}",
            level="ERROR", to_console=_console is None, log_file_path=log_file_path)
        stats["errors"].append({"name": movie_name, "error": err_msg})
        return

    tracks = media_info.get("tracks") or []
    audio_tracks = [t for t in tracks if t.get("type") == "audio"]
    subtitle_tracks = [t for t in tracks if t.get("type") == "subtitles"]

    # Prefer tagged/named English audio. A foreign original-language track is
    # only considered when a validated external English SRT is present — that
    # sidecar is what makes the movie playable for an English library, so the
    # same cleanup (one best audio, no embeds) is safe and useful.
    english_audio = [
        t for t in audio_tracks
        if is_matching_language(t, audio_langs) and not is_commentary_track(t, remove_commentary)
    ]
    if not english_audio:
        # An untagged stream is safe only when its explicit title identifies it
        # as English. Never guess that a bare ``und`` audio stream is English.
        english_audio = [
            t for t in audio_tracks
            if is_english_named_untagged(t) and not is_commentary_track(t, remove_commentary)
        ]

    external_srt: dict[str, Any] | None = None
    candidate = validate_exact_external_english_srt(mkv_path)
    if candidate.get("valid"):
        external_srt = candidate
    elif candidate.get("reason") != "external SRT is absent":
        log(
            f"{tag}External SRT ignored for '{display_name}': {candidate.get('reason')}; "
            "retaining the established embedded subtitle selection",
            level="WARNING", to_console=_console is None, log_file_path=log_file_path,
        )

    foreign_with_srt = False
    if english_audio:
        valid_audio = english_audio
    elif external_srt is not None:
        # Foreign / untagged-audio film with a verified external English SRT:
        # keep the single best non-commentary audio of any language and strip
        # every embedded subtitle so the sidecar is the sole subtitle option.
        valid_audio = [
            t for t in audio_tracks
            if not is_commentary_track(t, remove_commentary)
        ]
        foreign_with_srt = True
        if not valid_audio:
            reason = "no non-commentary audio track to retain beside external English SRT"
            if _console is not None:
                _console.end_file_inline(f"skipped ({reason})", kind="warn")
            log(f"{tag}Skipping '{display_name}' ({reason})",
                level="WARNING", to_console=_console is None, log_file_path=log_file_path)
            stats["skipped_no_english"].append({"name": movie_name, "reason": reason})
            return
    else:
        any_english = any(is_matching_language(t, audio_langs) for t in audio_tracks)
        reason = ("all English audio tracks are commentary/descriptive"
                  if any_english else "foreign film / no tagged or explicitly named English audio")
        if _console is not None:
            _console.end_file_inline(f"skipped ({reason})", kind="warn")
        log(f"{tag}Skipping '{display_name}' ({reason})",
            level="WARNING", to_console=_console is None, log_file_path=log_file_path)
        stats["skipped_no_english"].append({"name": movie_name, "reason": reason})
        return

    best_audio = max(valid_audio, key=get_audio_quality_score)
    best_audio_id = int(best_audio["id"])

    keep_subtitles = [
        t for t in subtitle_tracks
        if (is_matching_language(t, sub_langs) or is_english_named_untagged(t))
        and not is_commentary_track(t, remove_commentary)
    ]
    if external_srt is not None:
        # A verified exact-stem external SRT is always the authoritative
        # Jellyfin subtitle choice. Remove every embedded subtitle option,
        # including normal, SDH, forced, and non-English tracks.
        keep_subtitles = []
    keep_sub_ids = [int(t["id"]) for t in keep_subtitles]
    existing_audio_ids = [int(t["id"]) for t in audio_tracks]
    existing_sub_ids = [int(t["id"]) for t in subtitle_tracks]
    needs_audio_cleanup = existing_audio_ids != [best_audio_id]
    needs_sub_cleanup = set(existing_sub_ids) != set(keep_sub_ids)

    if not needs_audio_cleanup and not needs_sub_cleanup:
        stats["already_clean"].append(movie_name)
        if _console is not None:
            _console.end_file_inline("already clean", kind="skip")
        log(f"{tag}Already clean: {display_name}",
            to_console=_console is None, log_file_path=log_file_path)
        return

    if external_srt is None:
        # Pipeline-ordering guardrail. The documented order is
        # movie_standardizer -> subtitle_fetcher -> this cleaner, because a remux
        # rewrites the container bytes and therefore permanently changes the
        # OpenSubtitles moviehash (file size plus the first and last 64 KiB).
        # Once this file is rewritten it can no longer reproduce the hash of the
        # release it came from, so subtitle_fetcher.py loses its exact-match
        # search and degrades to title/year guessing. Warn rather than block:
        # the remux is still correct work, it just forfeits the exact hash.
        stats.setdefault("remux_without_srt", []).append(movie_name)
        log(
            f"{tag}No validated external English SRT for '{display_name}': this remux permanently "
            "changes the OpenSubtitles moviehash, so an exact-hash subtitle match is no longer "
            "possible for this file. Run subtitle_fetcher.py before this cleaner to preserve it. "
            "(Remux will continue.)",
            level="WARNING", to_console=_console is None, log_file_path=log_file_path,
        )

    removed_audio = [t for t in audio_tracks if int(t["id"]) != best_audio_id]
    removed_subs = [t for t in subtitle_tracks if int(t["id"]) not in set(keep_sub_ids)]
    best_audio_desc = describe_track(best_audio)
    removed_audio_descs = [describe_track(t) for t in removed_audio]
    kept_subs_descs = [describe_track(t) for t in keep_subtitles]
    removed_subs_descs = [describe_track(t) for t in removed_subs]

    if _console is not None:
        _console.mark_details()
    log(f"{tag}Processing: {display_name} ({format_size(size_before)})",
        to_console=_console is None, log_file_path=log_file_path)
    if foreign_with_srt:
        _log_detail(
            "  -> Foreign / non-English audio film with validated external English SRT: "
            "retaining best non-commentary audio and stripping all embedded subtitles",
            log_file_path,
        )
    _log_detail(f"  -> Retaining Audio: {best_audio_desc}", log_file_path)
    if removed_audio_descs:
        if len(removed_audio_descs) <= 8:
            _log_detail(f"  -> Removing {len(removed_audio_descs)} Audio Track(s):", log_file_path)
            for desc in removed_audio_descs:
                _log_detail(f"       drop  {desc}", log_file_path)
        else:
            _log_detail(
                f"  -> Removing {len(removed_audio_descs)} Audio Track(s): "
                f"{', '.join(removed_audio_descs[:8])} ... +{len(removed_audio_descs) - 8} more",
                log_file_path,
            )
    if external_srt is not None:
        _log_detail(
            f"  -> Validated External SRT: {Path(str(external_srt['path'])).name} "
            "(sole subtitle option; embedded subtitles removed)",
            log_file_path,
        )
    _log_detail(f"  -> Subtitles Kept: {len(keep_sub_ids)} | Removed: {len(removed_subs)}", log_file_path)

    if dry_run:
        _log_detail(f"  -> [DRY-RUN] No changes written to '{display_name}'.", log_file_path, kind="warn")
        stats["cleaned"].append({
            "name": movie_name, "kept_audio": best_audio_desc,
            "removed_audio_count": len(removed_audio_descs), "removed_audio_desc": removed_audio_descs,
            "kept_subs_count": len(keep_sub_ids), "kept_subs_desc": kept_subs_descs,
            "removed_subs_count": len(removed_subs), "removed_subs_desc": removed_subs_descs,
            "external_srt": external_srt,
            "size_before": size_before, "size_after": size_before, "space_saved": 0,
            "elapsed_seconds": round(time.monotonic() - proc_start, 2),
        })
        return

    space_ok, free_bytes, required_bytes, space_warn = check_free_space(mkv_path.parent, size_before)
    if space_warn:
        log(f"  -> Free-space check warning: {space_warn}. Continuing.",
            level="WARNING", log_file_path=log_file_path)
    if not space_ok:
        err_msg = (f"not enough free disk space to remux "
                   f"(need {format_size(required_bytes)}, have {format_size(free_bytes)}). "
                   f"Original file left untouched.")
        log(f"{tag}{err_msg}", level="ERROR", log_file_path=log_file_path)
        stats["errors"].append({"name": movie_name, "error": err_msg})
        return

    verification_plan = build_verification_plan(media_info, best_audio, keep_subtitles, size_before)
    temp_output, journal_path, transaction_token = new_transaction_paths(mkv_path)
    transaction = create_transaction(mkv_path, temp_output, transaction_token, orig_stat)
    transaction["verification_plan"] = verification_plan
    if external_srt is not None:
        transaction["external_srt"] = external_srt
    try:
        write_transaction(journal_path, transaction)
    except Exception as exc:
        err_msg = f"could not create remux transaction journal: {exc}"
        log(f"{tag}{err_msg}", level="ERROR", log_file_path=log_file_path)
        stats["errors"].append({"name": movie_name, "error": err_msg})
        return
    _active_temp_file = temp_output

    merge_cmd = [
        mkvmerge_bin, "-o", str(temp_output),
        "--audio-tracks", str(best_audio_id),
        "--default-track-flag", f"{best_audio_id}:1",
    ]
    if keep_sub_ids:
        merge_cmd.extend(["--subtitle-tracks", ",".join(map(str, keep_sub_ids))])
        for sub in keep_subtitles:
            sid = int(sub["id"])
            # Keep all retained English subtitles available, but do not force
            # subtitle auto-display through inherited default flags. Forced
            # flags are retained for player-side forced-subtitle handling.
            merge_cmd.extend(["--default-track-flag", f"{sid}:0"])
            merge_cmd.extend([
                "--forced-display-flag", f"{sid}:{1 if is_forced_subtitle(sub) else 0}",
            ])
    else:
        merge_cmd.append("--no-subtitles")
    merge_cmd.append(str(mkv_path))

    remux_started = time.monotonic()

    def _on_remux_progress(pct: int) -> None:
        if _console is not None:
            _console.remux_progress(pct, remux_started)

    try:
        rc, merge_out, merge_err = _run_mkvmerge(merge_cmd, on_progress=_on_remux_progress)
        if _console is not None:
            _console.finish_progress()
        if rc not in (0, 1):
            err_msg = _summarize_mkvmerge_failure(merge_err or merge_out, rc)
            log(f"mkvmerge failed for '{display_name}': {err_msg}",
                level="ERROR", log_file_path=log_file_path)
            stats["errors"].append({"name": movie_name, "error": err_msg})
            cleanup_transaction_artifacts(temp_output, journal_path)
            _active_temp_file = None
            return

        _log_detail("  -> Verifying remux integrity...", log_file_path)
        ok, verr, output_info = verify_remux_output(temp_output, mkvmerge_bin, verification_plan)
        if not ok:
            diagnostic = build_verification_diagnostic(media_info, output_info, verification_plan, verr)
            log(
                f"Verification diagnostic for '{display_name}': "
                + json.dumps(diagnostic, sort_keys=True, ensure_ascii=False),
                level="ERROR", log_file_path=log_file_path,
            )
            log(f"Verification failed for '{display_name}': {verr}. Original file left untouched.",
                level="ERROR", log_file_path=log_file_path)
            stats["errors"].append({"name": movie_name, "error": f"Post-remux verification failed: {verr}",
                                    "video_diagnostic": diagnostic})
            cleanup_transaction_artifacts(temp_output, journal_path)
            _active_temp_file = None
            return
        if _interrupt_requested:
            raise KeyboardInterrupt
        if external_srt is not None and not external_srt_snapshot_matches(external_srt):
            err_msg = "validated external SRT changed or became invalid while remuxing; refusing to replace MKV"
            log(f"{err_msg}: '{display_name}'", level="ERROR", log_file_path=log_file_path)
            stats["errors"].append({"name": movie_name, "error": err_msg})
            cleanup_transaction_artifacts(temp_output, journal_path)
            _active_temp_file = None
            return
        if not _source_snapshot_matches(mkv_path, transaction["source_snapshot"]):
            err_msg = "source changed while remuxing; refusing to replace it"
            log(f"{err_msg}: '{display_name}'", level="ERROR", log_file_path=log_file_path)
            stats["errors"].append({"name": movie_name, "error": err_msg})
            cleanup_transaction_artifacts(temp_output, journal_path)
            _active_temp_file = None
            return

        transaction["phase"] = "verified"
        transaction["verified_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        transaction["temp_snapshot"] = _source_snapshot(temp_output)
        write_transaction(journal_path, transaction)

        _log_detail("  -> Atomic swap over original...", log_file_path)
        time.sleep(0.1)
        if _interrupt_requested:
            raise KeyboardInterrupt
        if external_srt is not None and not external_srt_snapshot_matches(external_srt):
            err_msg = "validated external SRT changed before atomic swap; refusing to replace MKV"
            log(f"{err_msg}: '{display_name}'", level="ERROR", log_file_path=log_file_path)
            stats["errors"].append({"name": movie_name, "error": err_msg})
            cleanup_transaction_artifacts(temp_output, journal_path)
            _active_temp_file = None
            return
        safe_replace(temp_output, mkv_path)
        _active_temp_file = None
        safe_delete(journal_path)
        restore_file_times(mkv_path, orig_stat)

        final_stat = mkv_path.stat()
        # Re-key the metadata cache onto the file that now exists. The entry
        # written above describes the *source*: a remux preserves the mtime but
        # changes the size, so that entry can never match the file it describes
        # and the next run would re-scan every movie this run just cleaned.
        # ``output_info`` is the verification probe of exactly these bytes, so
        # storing it under the new stat is the answer mkvmerge would give.
        if probe_cache is not None and output_info is not None:
            probe_cache.put(
                mkv_path, final_stat.st_size, final_stat.st_mtime_ns, output_info
            )

        size_after = final_stat.st_size
        saved_bytes = max(0, size_before - size_after)
        stats["total_space_saved_bytes"] += saved_bytes
        result = (
            f"  -> Successfully cleaned: {display_name} "
            f"({format_size(size_before)} -> {format_size(size_after)} | "
            f"Saved: {format_size(saved_bytes)} | "
            f"{format_duration(time.monotonic() - proc_start)})"
        )
        _log_detail(result, log_file_path, kind="success")
        stats["cleaned"].append({
            "name": movie_name, "kept_audio": best_audio_desc,
            "removed_audio_count": len(removed_audio_descs), "removed_audio_desc": removed_audio_descs,
            "kept_subs_count": len(keep_sub_ids), "kept_subs_desc": kept_subs_descs,
            "removed_subs_count": len(removed_subs), "removed_subs_desc": removed_subs_descs,
            "external_srt": external_srt,
            "size_before": size_before, "size_after": size_after, "space_saved": saved_bytes,
            "elapsed_seconds": round(time.monotonic() - proc_start, 2),
        })
    except KeyboardInterrupt:
        if temp_output is not None:
            cleanup_transaction_artifacts(temp_output, journal_path)
        _active_temp_file = None
        raise
    except SystemExit:
        raise
    except Exception as exc:
        log(f"Exception processing '{display_name}': {exc}", level="ERROR", log_file_path=log_file_path)
        stats["errors"].append({"name": movie_name, "error": str(exc)})
        if temp_output is not None:
            cleanup_transaction_artifacts(temp_output, journal_path)
        _active_temp_file = None

def generate_and_save_report(
    stats: dict[str, Any], dry_run: bool, report_file: str,
    log_file_path: str | None = LOG_FILE, meta: dict[str, Any] | None = None,
) -> str:
    """Render and publish the run report.

    Layout follows the shared renderer so this reads like every other report in
    the toolkit: scorecard first, then anything that needs a decision, then the
    per-movie detail, then the inventory of what was left alone.
    """
    if _console is not None:
        _console.finish_progress()
    end_time = datetime.now()
    duration = end_time - stats["start_time"]
    dur_str = str(duration).split(".")[0]
    meta = meta or {}

    cleaned: list[dict[str, Any]] = list(stats.get("cleaned") or [])
    already_clean: list[Any] = list(stats.get("already_clean") or [])
    remux_without_srt: list[Any] = list(stats.get("remux_without_srt") or [])
    deferred: list[Any] = list(stats.get("deferred_hardlinked") or [])
    skipped_english: list[Any] = list(stats.get("skipped_no_english") or [])
    skipped_layout: list[Any] = list(stats.get("skipped_layout") or [])
    errors: list[dict[str, Any]] = list(stats.get("errors") or [])
    attention = len(remux_without_srt) + len(deferred) + len(skipped_layout) + len(errors)
    total = int(stats.get("total_scanned") or 0)

    report = Report(
        "JELLYFIN MKV TRACK CLEANUP REPORT",
        "Lossless mkvmerge remux \u00b7 commentary, dubs and embedded bitmaps removed; "
        "video bytes never touched",
    )
    header: list[tuple[str, Any]] = [
        ("Mode", "DRY-RUN (simulation, no files modified)" if dry_run else "LIVE RUN (changes applied)"),
    ]
    if meta.get("interrupted"):
        header.append(("Run status", "INTERRUPTED by user (partial results below)"))
    if meta.get("target_dir"):
        header.append(("Target", meta["target_dir"]))
    if meta.get("mkvmerge"):
        version = f"  \u00b7  {meta['mkvmerge_version']}" if meta.get("mkvmerge_version") else ""
        header.append(("mkvmerge", f"{meta['mkvmerge']}{version}"))
    if meta.get("audio_langs"):
        header.append(("Audio languages", ", ".join(sorted(meta["audio_langs"]))))
    if meta.get("sub_langs"):
        header.append(("Subtitle languages", ", ".join(sorted(meta["sub_langs"]))))
    if "remove_commentary" in meta:
        header.append(("Commentary / DVS", "removed (fixed policy)" if meta["remove_commentary"] else "kept"))
    if meta.get("external_srt_auto_preference"):
        header.append(("External SRT", f"a validated exact {EXTERNAL_SRT_SUFFIX} becomes the sole subtitle option"))
    if "standardizer_lock_acquired" in meta:
        header.append((
            "Standardizer lock",
            f"{'acquired' if meta['standardizer_lock_acquired'] else 'NOT acquired'} "
            f"(timeout {meta.get('standardizer_lock_timeout_seconds', '?')} s)",
        ))
    header += [
        ("Started", stats["start_time"].strftime("%Y-%m-%d %H:%M:%S")),
        ("Finished", end_time.strftime("%Y-%m-%d %H:%M:%S")),
        ("Duration", dur_str),
        ("Report", report_file),
    ]
    report.metas(header)

    rows: list[tuple[Any, str, str]] = [
        (len(cleaned), "Cleaned / remuxed", "simulated" if dry_run else "tracks pruned, video untouched"),
        (len(already_clean), "Already clean", "no writes needed"),
        (len(errors), "Errors", "unreadable or failed"),
        (len(remux_without_srt), "Remuxed without SRT", "moviehash now invalidated"),
        (len(deferred), "Deferred (hardlinked)", "still being seeded"),
        (len(skipped_layout), "Skipped (layout)", "folder is not canonical"),
        (len(skipped_english), "Skipped (no English)", "foreign film, kept as-is"),
        (total, "Movies scanned", "every MKV found in the target"),
    ]
    report.blank()
    report.scorecard(rows)

    if cleaned:
        before = sum(int(item.get("size_before", 0) or 0) for item in cleaned)
        after = sum(int(item.get("size_after", 0) or 0) for item in cleaned)
        saved = int(stats.get("total_space_saved_bytes") or 0)
        if dry_run:
            report.paragraph(
                f"Projected: {format_size(before)} before  \u00b7  {format_size(after)} after  \u00b7  "
                f"{format_size(saved)} would be reclaimed across {len(cleaned)} movie(s)."
            )
        else:
            report.paragraph(
                f"Reclaimed {format_size(saved)} across {len(cleaned)} movie(s): "
                f"{format_size(before)} before  \u00b7  {format_size(after)} after."
            )
    if attention:
        report.paragraph(
            f"Start here: {attention} movie(s) need a decision \u00b7 they are listed first, below."
        )
    elif not cleaned:
        report.paragraph("Nothing to do: every movie scanned was already clean.")

    # ---- anything needing a decision --------------------------------------
    if attention:
        report.section(
            "NEEDS YOUR ATTENTION",
            count=attention,
            total=total,
            intro="Ordered by how much it costs you to leave it alone.",
        )
        if errors:
            report.subsection("ERRORS ENCOUNTERED", count=len(errors))
            report.paragraph(
                "These files could not be processed. Nothing was written for them; read the "
                "error, fix the cause, and re-run."
            )
            report.blank()
            report.entries([(str(item.get("name", "?")), str(item.get("error", ""))) for item in errors])
        if remux_without_srt:
            report.subsection("REMUXED WITH NO EXTERNAL SRT (MOVIEHASH INVALIDATED)", count=len(remux_without_srt))
            report.paragraph(
                "These movies were remuxed without a validated external English SRT beside "
                "them. A remux rewrites the container bytes, which permanently changes the "
                "OpenSubtitles moviehash (file size plus the first and last 64 KiB). "
                "subtitle_fetcher.py can no longer find an exact hash match for any movie "
                "listed here and falls back to the less reliable title/year search, which is "
                "held for review rather than downloaded. To keep exact-hash matching, run "
                "subtitle_fetcher.py BEFORE this cleaner."
            )
            report.blank()
            report.entries([(str(name), "moviehash no longer matches the original release")
                            for name in remux_without_srt])
        if deferred:
            report.subsection("DEFERRED (STILL HARDLINKED / SEEDED)", count=len(deferred))
            report.paragraph(
                "These movies were NOT cleaned because they still have multiple hardlinks: "
                "the qBittorrent source copy is still present. Cleaning is safe for seeding "
                "either way - qBittorrent keeps seeding its own copy - but it would consume "
                "another full movie allocation until the source is deleted. qBittorrent's "
                "default 'stop seeding' action only PAUSES the torrent and leaves the file in "
                "place, so this list can persist indefinitely. Either configure qBittorrent to "
                "delete the content when seeding stops, or delete the source yourself. There "
                "is no flag to force it: this tool never remuxes a movie that is still seeded."
            )
            report.blank()
            report.entries([
                (str(item.get("name", "?")) if isinstance(item, dict) else str(item),
                 f"{item.get('hardlinks', '?')} hardlinks" if isinstance(item, dict) else "")
                for item in deferred
            ])
        if skipped_layout:
            report.subsection("SKIPPED (LAYOUT CONTRACT)", count=len(skipped_layout))
            report.paragraph(
                "One MKV per folder, named exactly like the folder. Run movie_standardizer.py, "
                "or fix the folder by hand, and these will be cleaned on the next run."
            )
            report.blank()
            report.entries([
                (str(item.get("name", "?")), str(item.get("reason", "noncanonical layout")))
                for item in skipped_layout
            ])

    # ---- what the run changed ---------------------------------------------
    report.section(
        "CLEANED THIS RUN (SIMULATED)" if dry_run else "CLEANED THIS RUN",
        count=len(cleaned),
        total=total,
        intro=(
            "Nothing would be written." if dry_run
            else "Each remux kept the best English audio and dropped everything else; the "
                 "video stream was copied untouched."
        ),
    )
    if not cleaned:
        report.paragraph("None.")
    else:
        for position, item in enumerate(cleaned, start=1):
            fields: list[tuple[str, str]] = [
                ("Kept audio", str(item.get("kept_audio", ""))),
                ("Removed audio",
                 ", ".join(item.get("removed_audio_desc") or [])
                 or "(none - a single English audio track was already the only one)"),
            ]
            if item.get("external_srt"):
                fields.append(("External SRT", f"{Path(str(item['external_srt']['path'])).name} (validated; preserved)"))
            kept = item.get("kept_subs_desc") or []
            fields.append(("Subtitles kept", str(item.get("kept_subs_count", len(kept)))))
            fields.extend([("", f"\u00b7  {desc}") for desc in kept])
            removed = item.get("removed_subs_desc") or []
            fields.append(("Subtitles removed", str(item.get("removed_subs_count", len(removed)))))
            fields.extend([("", f"\u00b7  {desc}") for desc in removed])
            if not dry_run and "space_saved" in item:
                fields.append((
                    "File size",
                    f"{format_size(item.get('size_before', 0))} \u00b7  "
                    f"{format_size(item.get('size_after', 0))}  \u00b7  "
                    f"saved {format_size(item.get('space_saved', 0))}",
                ))
            elif dry_run and item.get("size_before", 0) > 0:
                fields.append(("File size", f"{format_size(item['size_before'])} (unchanged in a dry run)"))
            if item.get("elapsed_seconds") is not None:
                fields.append(("Elapsed", f"{item['elapsed_seconds']:.2f} s"))
            report.entry(str(item.get("name", "?")), ordinal=position, fields=fields)

    # ---- inventory ---------------------------------------------------------
    report.section(
        "ALREADY CLEAN (NO WRITES)",
        count=len(already_clean),
        total=total,
        intro="These needed no changes and were skipped with zero disk writes.",
    )
    if not already_clean:
        report.paragraph("None.")
    else:
        report.entries([(str(name), "") for name in already_clean])

    report.section(
        "SKIPPED (FOREIGN / NO ENGLISH AUDIO)",
        count=len(skipped_english),
        total=total,
        intro=(
            "Policy, not a fault: a film with no usable English audio is left exactly as it "
            "is unless it already has a validated external English SRT."
        ),
    )
    if not skipped_english:
        report.paragraph("None.")
    else:
        report.entries([
            (str(item.get("name", "?")) if isinstance(item, dict) else str(item),
             str(item.get("reason", "no English audio")) if isinstance(item, dict) else "")
            for item in skipped_english
        ])

    report.footer([
        "Hardlinked movies are always deferred until qBittorrent removes the seeded source.",
        f"Log: {log_file_path or '(none)'}",
    ])
    report_text = report.render()
    _print_safe("\n" + report_text)
    try:
        destination = Path(report_file)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = destination.with_name(f".{destination.name}.partial.{os.getpid()}.{uuid.uuid4().hex}")
        try:
            with stage.open("x", encoding="utf-8", errors="replace", newline="\n") as handle:
                handle.write(report_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(stage, destination)
        finally:
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass
        log(f"Detailed summary report saved to: '{destination}'", log_file_path=log_file_path)
    except Exception as e:
        log(f"Failed to save summary report: {e}", level="ERROR", log_file_path=log_file_path)
    return report_text

def _print_startup_banner(
    target_path: Path, mkvmerge_bin: str, mkvmerge_version: str, dry_run: bool,
    audio_langs: set[str], sub_langs: set[str], remove_commentary: bool,
    log_file_path: str | None, priority: str = "normal",
) -> None:
    width = 79
    mode = "DRY-RUN (simulation - no files modified)" if dry_run else "LIVE RUN (modifications will be applied)"
    rows = [
        "=" * width, "  LOSSLESS JELLYFIN MKV TRACK CLEANER", "=" * width,
        f"  Target directory    : {target_path}",
        f"  Execution mode      : {mode}",
        f"  mkvmerge            : {mkvmerge_bin}",
        f"  mkvmerge version    : {mkvmerge_version}",
        f"  Audio languages     : {', '.join(sorted(audio_langs))}",
        f"  Subtitle languages  : {', '.join(sorted(sub_langs))}",
        f"  Commentary / DVS    : {'remove (fixed policy)' if remove_commentary else 'keep'}",
        f"  Accessibility subs  : retained only when no valid external {EXTERNAL_SRT_SUFFIX} exists",
        f"  External {EXTERNAL_SRT_SUFFIX}  : validated exact sidecar automatically becomes sole subtitle option",
        "  Foreign films       : cleaned when a validated external English SRT is present "
        "(best non-commentary audio kept)",
        "  Sidecar subtitles   : never modified by this cleaner (legacy .en.srt is renamed to .eng.srt)",
        "  Pipeline order      : movie_standardizer.py -> subtitle_fetcher.py -> this cleaner",
        "  (remuxing first invalidates the OpenSubtitles moviehash; a warning is logged per file)",
        "  Hardlinked movies   : always deferred (never remuxed while seeding)",
        f"  Process priority    : {priority}",
        f"  Log file            : {log_file_path or '(disabled)'}",
        "=" * width,
    ]
    if _console is not None:
        _console.finish_progress()
    for line in rows:
        if _console is not None and _console.use_color and line.startswith("  LOSSLESS"):
            _print_safe(_console.style(line, LiveConsole.BOLD, LiveConsole.CYAN))
        else:
            _print_safe(line)
        log(line, to_console=False, log_file_path=log_file_path)

def main(argv: list[str] | None = None) -> int:
    global _console, _target_root, _interrupt_requested
    enable_utf8_stdio()
    parser = argparse.ArgumentParser(
        description=(
            "Lossless post-standardizer cleanup: keep one best English audio "
            "(or best non-commentary audio on foreign films with a validated "
            f"external {EXTERNAL_SRT_SUFFIX}) and strip embedded subs when that sidecar exists."
        )
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--dir", default=TARGET_DIR, help=f"Target library folder (Default: {TARGET_DIR})")
    parser.add_argument("--dry-run", action="store_true", help="Simulate cleanup without modifying any files")
    parser.add_argument(
        "--only", action="append", default=[], metavar="MKV",
        help="Process only this exact MKV path (may be supplied more than once)",
    )
    parser.add_argument("--mkvmerge", default=None, help="Custom path to mkvmerge executable")
    parser.add_argument("--log", default=LOG_FILE, help=f"Continuous log file path (Default: {LOG_FILE})")
    parser.add_argument("--report", default=REPORT_FILE, help=f"Single overwritten report file (Default: {REPORT_FILE})")
    parser.add_argument(
        "--standardizer-lock-timeout", type=float, default=STANDARDIZER_LOCK_TIMEOUT_SECONDS,
        metavar="SECONDS", help="Maximum wait for movie_standardizer.py coordination (default: 60)",
    )
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--nice", action="store_true", help="Lower process priority so remuxing does not starve Jellyfin")
    parser.add_argument("--min-size", type=float, default=0, metavar="MB", help="Ignore MKVs smaller than this")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N files (0 = all)")
    parser.add_argument("--cache", default=CACHE_FILE, metavar="PATH",
                        help=f"Reusable mkvmerge metadata for unchanged files (Default: {CACHE_FILE})")
    parser.add_argument("--no-cache", dest="use_cache", action="store_false",
                        help="Re-read metadata for every movie and do not read or write the cache")
    parser.set_defaults(use_cache=True)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_tests()
    if args.standardizer_lock_timeout < 0:
        parser.error("--standardizer-lock-timeout must be zero or greater")

    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    _console = LiveConsole(use_color=False if args.no_color else None)
    atexit.register(close_log_fp)

    try:
        mkvmerge_bin = resolve_mkvmerge_path(args.mkvmerge)
    except FileNotFoundError as e:
        log(str(e), level="ERROR", log_file_path=args.log)
        return 1

    target_path = Path(args.dir).resolve()
    if not target_path.exists() or not target_path.is_dir():
        log(f"Target directory does not exist: '{target_path}'", level="ERROR", log_file_path=args.log)
        return 1
    for label, raw_path in (("--log", args.log), ("--report", args.report)):
        if not raw_path:
            continue
        candidate = Path(str(raw_path)).expanduser().resolve()
        try:
            candidate.relative_to(target_path)
        except ValueError:
            continue
        log(f"{label} must be outside the Jellyfin media library: '{candidate}'", level="ERROR", log_file_path=args.log)
        return 2
    args.report = str(Path(args.report).expanduser().resolve())

    _target_root = target_path
    if _console is not None:
        _console.target_root = target_path

    audio_langs_set = set(AUDIO_LANGUAGES)
    sub_langs_set = set(SUBTITLE_LANGUAGES)
    remove_commentary = REMOVE_COMMENTARY
    mkvmerge_version = get_mkvmerge_version(mkvmerge_bin)
    priority_label = apply_low_priority() if args.nice else "normal"

    _print_startup_banner(
        target_path, mkvmerge_bin, mkvmerge_version, args.dry_run,
        audio_langs_set, sub_langs_set, remove_commentary, args.log, priority_label,
    )
    log(f"--- Starting Scan on '{target_path}' ---", log_file_path=args.log)
    log(f"Single report file: '{args.report}'", log_file_path=args.log)
    if args.dry_run:
        log("Running in DRY-RUN mode (Simulation only).", log_file_path=args.log)

    stats: dict[str, Any] = {
        "start_time": datetime.now(), "total_scanned": 0, "cleaned": [],
        "already_clean": [], "skipped_no_english": [], "skipped_layout": [], "deferred_hardlinked": [], "errors": [],
        "remux_without_srt": [], "diagnostics": [], "total_space_saved_bytes": 0,
    }
    report_meta: dict[str, Any] = {
        "target_dir": str(target_path), "report_file": args.report, "mkvmerge": mkvmerge_bin,
        "mkvmerge_version": mkvmerge_version, "audio_langs": audio_langs_set,
        "sub_langs": sub_langs_set, "remove_commentary": remove_commentary,
        "external_srt_auto_preference": True,
        "standardizer_lock_timeout_seconds": args.standardizer_lock_timeout,
    }

    # Coordinate with movie_standardizer.py before scanning. Its lock protocol is
    # deliberately mirrored so qBittorrent placement and cleanup cannot race.
    standardizer_lock = CoordinationLock(target_path, timeout_seconds=args.standardizer_lock_timeout)
    try:
        standardizer_lock.acquire()
    except (OSError, TimeoutError) as exc:
        report_meta["standardizer_lock_acquired"] = False
        log(f"Could not acquire movie_standardizer.py coordination lock: {exc}",
            level="ERROR", log_file_path=args.log)
        return 1
    report_meta["standardizer_lock_acquired"] = True
    log("movie_standardizer.py coordination lock acquired.", log_file_path=args.log)

    lock_path = target_path / LOCK_FILENAME
    lock_acquired = False
    if not args.dry_run:
        lock_acquired = acquire_lock(lock_path, log_file_path=args.log)
        if not lock_acquired:
            standardizer_lock.release()
            return 1
        log(f"Single-instance lock acquired (PID {os.getpid()}).", log_file_path=args.log)

    def _release_locks():
        if lock_acquired:
            release_lock(lock_path)
        standardizer_lock.release()

    atexit.register(_release_locks)
    if _console is not None:
        atexit.register(_console.finish_progress)

    def handle_interrupt(signum, frame):
        request_interrupt()

    signal.signal(signal.SIGINT, handle_interrupt)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_interrupt)

    if lock_acquired:
        cleanup_orphan_temps(target_path, mkvmerge_bin, log_file_path=args.log)

    def _walk_error(err):
        log(f"Could not access directory '{err.filename}': {err.strerror}",
            level="WARNING", log_file_path=args.log)

    run_started = time.monotonic()
    processed_bytes = 0
    library_bytes = 0
    probe_cache = MediaProbeCache(args.cache, tool="mkv_track_cleaner", enabled=args.use_cache)
    if args.use_cache:
        log(f"Metadata cache: {args.cache} ({len(probe_cache)} entries loaded)", log_file_path=args.log)
    try:
        mkv_files, file_sizes, library_bytes = discover_mkv_files(
            target_path, log_file_path=args.log, onerror=_walk_error,
            skip_extras=True,
            min_size=int(args.min_size * 1024 * 1024) if args.min_size else 0,
        )
        if args.only:
            requested: set[str] = set()
            for raw_path in args.only:
                candidate_path = Path(raw_path).expanduser().resolve()
                if candidate_path.suffix.lower() != ".mkv" or candidate_path.is_symlink() or not candidate_path.is_file():
                    raise ValueError(f"--only must name an existing regular MKV: {raw_path}")
                try:
                    candidate_path.relative_to(target_path)
                except ValueError as exc:
                    raise ValueError(f"--only MKV must be inside --dir: {raw_path}") from exc
                requested.add(os.path.normcase(os.path.normpath(str(candidate_path))))
            selected = [
                (path, size) for path, size in zip(mkv_files, file_sizes, strict=True)
                if os.path.normcase(os.path.normpath(str(path))) in requested
            ]
            if len(selected) != len(requested):
                found = {os.path.normcase(os.path.normpath(str(path))) for path, _ in selected}
                missing = sorted(requested - found)
                raise ValueError(f"--only MKV was not discovered under --dir: {missing[0]}")
            mkv_files = [path for path, _ in selected]
            file_sizes = [size for _, size in selected]
            library_bytes = sum(file_sizes)
        if args.limit and args.limit > 0:
            mkv_files = mkv_files[: args.limit]
            file_sizes = file_sizes[: args.limit]
            library_bytes = sum(file_sizes)
        file_total = len(mkv_files)
        if file_total == 0 and not _interrupt_requested:
            log("No MKV files found. Nothing to do.", level="WARNING", log_file_path=args.log)

        for i, file_path in enumerate(mkv_files, start=1):
            if _interrupt_requested:
                break
            stats["total_scanned"] += 1
            prev_cleaned = len(stats["cleaned"])
            prev_errors = len(stats["errors"])
            try:
                process_mkv(
                    mkv_path=file_path, stats=stats, mkvmerge_bin=mkvmerge_bin,
                    dry_run=args.dry_run, remove_commentary=remove_commentary,
                    audio_langs=audio_langs_set, sub_langs=sub_langs_set,
                    log_file_path=args.log, file_index=i, file_total=file_total,
                    probe_cache=probe_cache,
                )
            except KeyboardInterrupt:
                _interrupt_requested = True
                _kill_active_child()
                break
            except SystemExit:
                raise
            except Exception as e:
                log(f"Unexpected error processing '{file_path.name}': {e}",
                    level="ERROR", log_file_path=args.log)
                stats["errors"].append({"name": file_path.name, "error": str(e)})
            processed_bytes += file_sizes[i - 1] if (i - 1) < len(file_sizes) else 0
            if (
                len(stats["cleaned"]) != prev_cleaned or len(stats["errors"]) != prev_errors
                or i == file_total or (i % 25 == 0) or _interrupt_requested
            ):
                _log_live_totals(stats, i, file_total, run_started, args.log,
                                 done_bytes=processed_bytes, total_bytes=library_bytes)
    except KeyboardInterrupt:
        _interrupt_requested = True
        _kill_active_child()
    except Exception as e:
        log(f"Fatal unexpected error: {e}", level="ERROR", log_file_path=args.log)
        stats["errors"].append({"name": "<fatal>", "error": str(e)})
        generate_and_save_report(stats, dry_run=args.dry_run, report_file=args.report,
                                 log_file_path=args.log, meta=report_meta)
        return 1
    finally:
        probe_cache.save()
        if args.use_cache:
            log(f"Metadata cache: {probe_cache.hits} reused, {probe_cache.misses} read from mkvmerge.",
                log_file_path=args.log)
        _release_locks()

    if _interrupt_requested:
        if _console is not None:
            _console.finish_progress()
        log("Execution interrupted by user. Cleaning up active temporary files...",
            level="WARNING", log_file_path=args.log)
        if _active_temp_file:
            safe_delete(_active_temp_file)
        report_meta["interrupted"] = True
        generate_and_save_report(stats, dry_run=args.dry_run, report_file=args.report,
                                 log_file_path=args.log, meta=report_meta)
        return 130

    generate_and_save_report(stats, dry_run=args.dry_run, report_file=args.report,
                             log_file_path=args.log, meta=report_meta)
    return 1 if stats["errors"] else 0

# =============================================================================
# SELF-TEST  (no mkvmerge required)
# =============================================================================

def run_self_tests() -> int:
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    check(normalize_language("fre") == "fr", "fre->fr")
    check(normalize_language("eng") == "en", "eng->en")
    check(normalize_language(["en", "US"][0]) == "en", "en-US")

    eng = {"id": 1, "type": "audio", "properties": {"language": "eng", "language_ietf": "en"}}
    fre = {"id": 2, "type": "audio", "properties": {"language": "fre"}}
    check(is_matching_language(eng, {"en", "eng"}), "eng match")
    check(is_matching_language(fre, {"fr", "fra"}), "fra matches fre")
    check(not is_matching_language(fre, {"en"}), "fre not en")

    sdh = {"type": "subtitles", "properties": {
        "language": "eng", "track_name": "English SDH",
        "flag_hearing_impaired": True, "flag_visual_impaired": True,
    }}
    check(not is_commentary_track(sdh, True), "SDH subtitle must be KEPT")

    dvs = {"type": "audio", "properties": {
        "language": "eng", "track_name": "English Audio Description",
        "flag_visual_impaired": True,
    }}
    check(is_commentary_track(dvs, True), "DVS audio must be DROPPED")

    comm = {"type": "audio", "properties": {"language": "eng", "track_name": "Director Commentary", "flag_commentary": True}}
    check(is_commentary_track(comm, True), "commentary audio dropped")
    cut = {"type": "audio", "properties": {"language": "eng", "track_name": "Director's Cut"}}
    check(not is_commentary_track(cut, True), "Director's Cut is not commentary")

    forced = {"type": "subtitles", "properties": {"language": "eng", "track_name": "English Forced", "flag_forced": True}}
    check(is_forced_subtitle(forced), "forced flag")
    check(not is_commentary_track(forced, True), "forced sub kept")

    und_eng = {"type": "subtitles", "properties": {"language": "und", "track_name": "English"}}
    und_unknown = {"type": "audio", "properties": {"language": "und", "track_name": ""}}
    check(is_english_named_untagged(und_eng), "untagged English by name")
    check(not is_english_named_untagged(und_unknown), "untagged unknown is not English")

    truehd = {"codec": "TrueHD", "properties": {"codec_id": "A_MLP", "audio_channels": 8, "track_name": "Atmos"}}
    aac = {"codec": "AAC", "properties": {"codec_id": "A_AAC", "audio_channels": 6}}
    check(get_audio_quality_score(truehd) > get_audio_quality_score(aac), "TrueHD Atmos > AAC 5.1")

    check(_parse_mkvmerge_progress("Progress: 45%") == 45, "plain progress")
    check(_parse_mkvmerge_progress("#GUI#progress 80%") == 80, "gui progress")
    check(_parse_mkvmerge_progress("#GUI#progress#parts=1/4") == 25, "parts progress")
    check(_parse_mkvmerge_progress("hello") is None, "no progress")

    check(not SAMPLE_NAME_RE.search("The Sampler (2012)"), "false sample")
    check(bool(SAMPLE_NAME_RE.search("Movie-sample")), "sample name")

    tmp = Path(tempfile.mkdtemp(prefix="tcc_"))
    try:
        movie = tmp / "Film (2000)"
        extra = movie / "Featurettes"
        extra.mkdir(parents=True)
        (movie / "Film (2000).mkv").write_bytes(b"x")
        (extra / "Making-Of.mkv").write_bytes(b"y")
        (movie / "Film-sample.mkv").write_bytes(b"z")
        files, _, _ = discover_mkv_files(tmp, None, skip_extras=True)
        names = {p.name for p in files}
        check(names == {"Film (2000).mkv"}, f"discover extras/samples skipped: {names}")
        files2, _, _ = discover_mkv_files(tmp, None, skip_extras=False)
        check(any(p.name == "Making-Of.mkv" for p in files2), "include extras helper")
        hardlink_source = tmp / "seed-source.mkv"
        hardlink_target = tmp / "hardlink-target.mkv"
        hardlink_source.write_bytes(b"linked")
        hardlink_target.hardlink_to(hardlink_source)
        check(hardlink_count(hardlink_source) >= 2, "hardlink count detects seeded-style link")
        hardlink_target.unlink()
        check(hardlink_count(hardlink_source) == 1, "hardlink count clears after source removal")

        movie_srt = movie / f"Film (2000){EXTERNAL_SRT_SUFFIX}"
        movie_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n", encoding="utf-8")
        external_record = validate_exact_external_english_srt(movie / "Film (2000).mkv")
        check(bool(external_record.get("valid")), f"valid exact external SRT: {external_record}")
        check(external_srt_snapshot_matches(external_record), "external SRT snapshot initial match")
        (movie / "Film (2000).en.forced.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nWrong suffix\n", encoding="utf-8",
        )
        check(
            external_record.get("path", "").endswith(f"Film (2000){EXTERNAL_SRT_SUFFIX}"),
            f"only exact {EXTERNAL_SRT_SUFFIX} qualifies",
        )
        # Legacy .en.srt is promoted to the canonical .eng.srt on validate.
        legacy_movie = tmp / "Legacy (2001)"
        legacy_movie.mkdir()
        (legacy_movie / "Legacy (2001).mkv").write_bytes(b"x")
        (legacy_movie / "Legacy (2001).en.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n", encoding="utf-8",
        )
        legacy_record = validate_exact_external_english_srt(legacy_movie / "Legacy (2001).mkv")
        check(bool(legacy_record.get("valid")), f"legacy .en.srt promotes: {legacy_record}")
        check(
            str(legacy_record.get("path", "")).endswith(f"Legacy (2001){EXTERNAL_SRT_SUFFIX}"),
            "promoted path is .eng.srt",
        )
        check(not (legacy_movie / "Legacy (2001).en.srt").exists(), "legacy .en.srt removed after promote")
        # A covering .eng.sdh.srt must be recorded under its OWN name: the
        # post-remux re-check re-stats the recorded path, and a stale canonical
        # path that never existed would reject an untouched valid sidecar.
        sdh_movie = tmp / "Sdh (2002)"
        sdh_movie.mkdir()
        (sdh_movie / "Sdh (2002).mkv").write_bytes(b"x")
        (sdh_movie / "Sdh (2002).eng.sdh.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSDH line\n", encoding="utf-8",
        )
        sdh_record = validate_exact_external_english_srt(sdh_movie / "Sdh (2002).mkv")
        check(bool(sdh_record.get("valid")), f"covering .eng.sdh.srt qualifies: {sdh_record}")
        check(str(sdh_record.get("path", "")).endswith("Sdh (2002).eng.sdh.srt"),
              "sdh record names the file it was validated from")
        check(external_srt_snapshot_matches(sdh_record), "untouched sdh sidecar keeps its snapshot match")
        # A broken .eng.srt beside a valid .eng.sdh.srt must fall through to
        # the valid alternate rather than hiding it.
        fallthrough_movie = tmp / "Fallthrough (2003)"
        fallthrough_movie.mkdir()
        (fallthrough_movie / "Fallthrough (2003).mkv").write_bytes(b"x")
        (fallthrough_movie / "Fallthrough (2003).eng.srt").write_text(
            "<html>not a subtitle</html>", encoding="utf-8",
        )
        (fallthrough_movie / "Fallthrough (2003).eng.sdh.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSDH line\n", encoding="utf-8",
        )
        fallthrough_record = validate_exact_external_english_srt(fallthrough_movie / "Fallthrough (2003).mkv")
        check(bool(fallthrough_record.get("valid")),
              f"broken .eng.srt falls through to valid .eng.sdh.srt: {fallthrough_record}")
        check(str(fallthrough_record.get("path", "")).endswith("Fallthrough (2003).eng.sdh.srt"),
              "fallthrough record names the valid .eng.sdh.srt")
        check(external_srt_snapshot_matches(fallthrough_record), "fallthrough sdh keeps its snapshot match")
        movie_srt.write_text("<html>not a subtitle</html>", encoding="utf-8")
        check(not external_srt_snapshot_matches(external_record), "changed/malformed external SRT rejects activation")
        check(not validate_exact_external_english_srt(movie / "Film (2000).mkv").get("valid"),
              "malformed external SRT does not qualify")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Accessibility and selected-track verification fixtures require no media
    # binaries; they model mkvmerge JSON directly.
    text_description = {"type": "subtitles", "codec": "SubRip/SRT", "properties": {
        "language": "eng", "track_name": "English text descriptions",
        "flag_text_descriptions": True, "flag_default": False,
    }}
    check(not is_commentary_track(text_description, True), "text-description subtitle must be KEPT")

    source_video = {"type": "video", "codec": "AVC/H.264/MPEG-4p10", "properties": {
        "codec_id": "V_MPEG4/ISO/AVC", "pixel_dimensions": "1920x1080",
        "display_dimensions": "1920x1080", "tag_number_of_frames": "240", "flag_default": True,
    }}
    source_audio = {"type": "audio", "codec": "AC-3", "properties": {
        "codec_id": "A_AC3", "language": "eng", "language_ietf": "en",
        "track_name": "English 5.1", "audio_channels": 6,
        "audio_sampling_frequency": 48000, "flag_default": False,
    }}
    source_forced = {"type": "subtitles", "codec": "SubRip/SRT", "properties": {
        "codec_id": "S_TEXT/UTF8", "language": "eng", "track_name": "English Forced",
        "flag_forced": True, "flag_default": False,
    }}
    source_info = {
        "container": {"recognized": True, "supported": True, "properties": {"duration": 10_000_000_000}},
        "tracks": [source_video, source_audio, source_forced, text_description],
        "attachments": [], "chapters": [],
    }
    verification_plan = build_verification_plan(
        source_info, source_audio, [source_forced, text_description], 4096,
    )
    output_info = json.loads(json.dumps(source_info))
    output_info["tracks"][1]["properties"]["flag_default"] = True
    check(
        track_fingerprint(output_info["tracks"][1]) == verification_plan["audio"],
        "selected audio default flag is explicit",
    )
    source_without_ietf = json.loads(json.dumps(source_info))
    source_without_ietf["tracks"][1]["properties"].pop("language_ietf")
    normalized_output = json.loads(json.dumps(output_info))
    normalized_output["tracks"][1]["properties"]["language_ietf"] = "en"
    normalized_plan = build_verification_plan(
        source_without_ietf, source_without_ietf["tracks"][1], [source_forced, text_description], 4096,
    )
    check(
        track_fingerprint(normalized_output["tracks"][1]) == normalized_plan["audio"],
        "missing source IETF tag normalizes to MKVToolNix output language tag",
    )
    aac_seven_channel_source = {"type": "audio", "codec": "AAC", "properties": {
        "codec_id": "A_AAC", "language": "eng", "audio_channels": 7,
        "audio_sampling_frequency": 24000, "default_track": True,
    }}
    aac_eight_channel_output = json.loads(json.dumps(aac_seven_channel_source))
    aac_eight_channel_output["properties"]["audio_channels"] = 8
    aac_eight_channel_output["properties"]["language_ietf"] = "en"
    aac_expected = track_fingerprint(aac_seven_channel_source, default_override=True)
    check(
        retained_audio_fingerprint_matches(track_fingerprint(aac_eight_channel_output), aac_expected),
        "AAC source channel count 7 and MKVToolNix output count 8 are accepted only when all other fields match",
    )
    aac_six_channel_source = json.loads(json.dumps(aac_seven_channel_source))
    aac_six_channel_source["properties"]["audio_channels"] = 6
    aac_six_expected = track_fingerprint(aac_six_channel_source, default_override=True)
    check(
        not retained_audio_fingerprint_matches(track_fingerprint(aac_eight_channel_output), aac_six_expected),
        "AAC channel changes other than the observed 7-to-8 representation mismatch reject the remux",
    )

    tx_tmp = Path(tempfile.mkdtemp(prefix="tcc_tx_"))
    original_runner = globals()["_run_mkvmerge"]

    def age_for_recovery(path: Path) -> None:
        aged = time.time() - ORPHAN_MIN_AGE_SECONDS - 2.0
        os.utime(path, (aged, aged))

    try:
        temp_fixture = tx_tmp / "verify-fixture.mkv"
        temp_fixture.write_bytes(b"x" * 4096)
        ok, reason = _verify_remux_info(temp_fixture, output_info, verification_plan)
        check(ok, f"fingerprint verification accepted intended output: {reason}")
        source_without_frame_stats = json.loads(json.dumps(source_info))
        source_without_frame_stats["tracks"][0]["properties"].pop("tag_number_of_frames")
        generated_frame_output = json.loads(json.dumps(output_info))
        generated_frame_plan = build_verification_plan(
            source_without_frame_stats, source_without_frame_stats["tracks"][1],
            [source_forced, text_description], 4096,
        )
        ok, reason = _verify_remux_info(temp_fixture, generated_frame_output, generated_frame_plan)
        check(ok, f"generated output-only frame statistics are accepted: {reason}")
        wrong_frame_output = json.loads(json.dumps(output_info))
        wrong_frame_output["tracks"][0]["properties"]["tag_number_of_frames"] = "241"
        ok, _ = _verify_remux_info(temp_fixture, wrong_frame_output, verification_plan)
        check(not ok, "a changed source-known video frame count rejects the remux")
        changed_output = json.loads(json.dumps(output_info))
        changed_output["tracks"][1]["properties"]["language"] = "fra"
        ok, _ = _verify_remux_info(temp_fixture, changed_output, verification_plan)
        check(not ok, "fingerprint verification rejects a wrong retained audio track")

        original = tx_tmp / "Recovery Film.mkv"
        original.write_bytes(b"source" * 1024)
        temp_path, journal, token = new_transaction_paths(original)
        check(temp_path.parent == original.parent and journal.parent == original.parent, "transaction paths are siblings")
        check(temp_path.name != original.name and _transaction_token_from_temp_name(temp_path.name) == token,
              "transaction temp names are unique and parseable")
        transaction = create_transaction(original, temp_path, token, original.stat())
        transaction["verification_plan"] = verification_plan
        temp_path.write_bytes(b"x" * 4096)
        write_transaction(journal, transaction)
        check(read_transaction(journal) is not None, "transaction journal round-trip")
        check(_source_snapshot_matches(original, transaction["source_snapshot"]), "source snapshot initial match")
        original.write_bytes(b"changed" * 1024)
        check(not _source_snapshot_matches(original, transaction["source_snapshot"]), "source snapshot detects mutation")

        # An unverified missing-original transaction must be preserved, not promoted.
        original.unlink()
        age_for_recovery(temp_path)
        cleanup_orphan_temps(tx_tmp, "stub", None)
        check(temp_path.exists() and journal.exists(), "unverified orphan retained for manual review")
        cleanup_transaction_artifacts(temp_path, journal)

        # A verified journal is recoverable only after verification succeeds again.
        recovered = tx_tmp / "Recovered Film.mkv"
        recovered_temp, recovered_journal, recovered_token = new_transaction_paths(recovered)
        recovered_temp.write_bytes(b"x" * 4096)
        recovered_tx = create_transaction(recovered, recovered_temp, recovered_token, temp_fixture.stat())
        recovered_tx["verification_plan"] = verification_plan
        recovered_tx["phase"] = "verified"
        age_for_recovery(recovered_temp)
        recovered_tx["temp_snapshot"] = _source_snapshot(recovered_temp)
        write_transaction(recovered_journal, recovered_tx)
        globals()["_run_mkvmerge"] = lambda *_args, **_kwargs: (0, json.dumps(output_info), "")
        cleanup_orphan_temps(tx_tmp, "stub", None)
        check(recovered.exists() and not recovered_temp.exists() and not recovered_journal.exists(),
              "verified and rechecked orphan recovers atomically")

        # Simulate a crash after os.replace but before journal deletion.
        journal_only = tx_tmp / "Journal Only.mkv"
        journal_only.write_bytes(b"source" * 1024)
        missing_temp, stale_journal, stale_token = new_transaction_paths(journal_only)
        stale_tx = create_transaction(journal_only, missing_temp, stale_token, journal_only.stat())
        stale_tx["phase"] = "verified"
        write_transaction(stale_journal, stale_tx)
        cleanup_orphan_temps(tx_tmp, "stub", None)
        check(not stale_journal.exists() and journal_only.exists(), "stale journal removed only beside intact original")

        legacy = tx_tmp / "temp_clean_legacy-missing.mkv"
        legacy.write_bytes(b"x" * 4096)
        age_for_recovery(legacy)
        cleanup_orphan_temps(tx_tmp, "stub", None)
        check(legacy.exists(), "legacy orphan without original is never auto-promoted")
    except Exception as exc:
        errors.append(f"transaction/fingerprint self-test exception: {exc}")
    finally:
        globals()["_run_mkvmerge"] = original_runner
        shutil.rmtree(tx_tmp, ignore_errors=True)

    if errors:
        print("SELF-TEST FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print("SELF-TEST PASSED (selection + external-SRT policy + fingerprints + transactions + recovery + discovery + hardlinks)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
