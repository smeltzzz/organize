#!/usr/bin/env python3
"""Shared, stdlib-only infrastructure for the media-library tools.

The five top-level scripts (``10bit.py``, ``library_auditor.py``,
``mkv_track_cleaner.py``, ``movie_standardizer.py``, ``subtitle_fetcher.py``)
originally copy-pasted these helpers.  They are gathered here so a fix only has
to land once.  Every function is a byte-for-byte behaviour-preserving extraction
of the code that previously lived in each script.

Importing this module has no side effects and writes nothing to disk.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

# The canonical coordination-lock file name shared by the standardizer, the
# track cleaner and the subtitle fetcher.  It lives in the system temp dir so
# no media-library file needs to be created just to coordinate.
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
EXTERNAL_SRT_MAX_BYTES = 4 * 1024 * 1024
EXTERNAL_SRT_CUE_RE = re.compile(
    r"(?m)^\s*\d+\s*\n\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}"
)

# The single agreed decode order. Every tool that turns subtitle bytes into
# text uses this tuple and nothing else, so a tool cannot quietly accept an
# encoding the others would reject. "utf-8-sig" first so a provider BOM does
# not make an otherwise valid file look binary; "cp1252" last because it
# decodes almost any byte sequence and would mask a genuine encoding problem.
EXTERNAL_SRT_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "cp1252")

__all__ = [
    "STANDARDIZER_LOCK_NAME",
    "LockTimeoutError",
    "CoordinationLock",
    "try_file_lock",
    "atomic_write_text",
    "path_is_within",
    "path_norm",
    "paths_equal",
    "EXTERNAL_SRT_MAX_BYTES",
    "EXTERNAL_SRT_CUE_RE",
    "EXTERNAL_SRT_ENCODINGS",
    "decode_srt_bytes",
    "normalize_srt_newlines",
    "srt_looks_valid",
    "validate_srt_sidecar",
]


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
      ``OSError`` as "busy" — ``10bit.py`` and ``library_auditor.py`` retried
      every failure until they timed out.
    * ``True`` (the historical behaviour of the standardizer coordination lock)
      re-raises genuine errors and only reports the well-known
      "already locked" codes as busy.
    """
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
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
        handle = open(self.path, "a+b")
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
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._fh = None

    def __enter__(self) -> "CoordinationLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def atomic_write_text(path: Path, text: str) -> None:
    """Publish ``text`` to ``path`` atomically.

    Writes through a unique sibling file then ``os.replace``\ s it into place, so
    a crash never leaves a truncated report and a read in progress always sees
    either the previous file or the complete new one.  On failure the staged
    file is removed and the prior report is retained.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    try:
        staged.write_text(text, encoding="utf-8")
        os.replace(str(staged), str(path))
    except OSError:
        try:
            staged.unlink(missing_ok=True)
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


def path_norm(path: Path) -> str:
    """Normalize a path the same way every tool compares them.

    ``normcase`` lower-cases on Windows and is a no-op on POSIX; ``normpath``
    collapses ``..`` and duplicate separators.  Matching this exactly is what
    lets the standardizer, cleaner and subtitle fetcher agree on a lock key and
    on whether two paths are the same file.
    """
    return os.path.normcase(os.path.normpath(str(path)))


def paths_equal(a: Path, b: Path) -> bool:
    """True when two paths refer to the same file.

    If both paths already exist, ``samefile`` resolves hard links and symlinks
    (the most accurate answer); otherwise it falls back to the normalized-text
    comparison used for paths that may not have been created yet.
    """
    try:
        if a.exists() and b.exists():
            return a.samefile(b)
    except OSError:
        pass
    return path_norm(a) == path_norm(b)
