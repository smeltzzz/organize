"""Cross-platform advisory locking."""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STANDARDIZER_LOCK_NAME = ".movie_standardizer.lock"


class LockTimeoutError(TimeoutError):
    """Raised when a ``CoordinationLock`` cannot be acquired in time.

    Subclasses :class:`TimeoutError` so callers that historically caught the
    built-in ``TimeoutError`` (e.g. the mkv track cleaner) keep working.
    """


class LockUnavailable(RuntimeError):
    """Raised when another inspector instance owns the run lock."""


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


class ExclusiveRunLock:
    """A fail-closed advisory lock compatible with Windows and POSIX hosts.

    ``busy_message`` is the only thing that ever differed between the tools'
    copies of this class (an inspector says "another inspector run holds", the
    auditor says "another audit owns"), so it is a parameter rather than a
    seventh transcription of the locking protocol.

    The Windows branch of :meth:`_try_lock` materialises the lock byte *only
    when the file is empty*. One of the two vendored copies had dropped that
    guard and appended a ``0`` on every retry, growing the lock file for the
    lifetime of a contended wait.
    """

    def __init__(
        self,
        path: Path,
        timeout_seconds: float,
        busy_message: str = "another run holds {path}",
    ) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.busy_message = busy_message
        self.handle: Any | None = None

    def _try_lock(self) -> bool:
        assert self.handle is not None
        if os.name == "nt":
            # Materialize a leading byte once, exactly as the original did.
            self.handle.seek(0)
            if self.handle.tell() == 0:
                self.handle.write("0")
                self.handle.flush()
        return try_file_lock(self.handle, strict_non_contention=False)

    def __enter__(self) -> ExclusiveRunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if self._try_lock():
                self.handle.seek(0)
                self.handle.truncate()
                self.handle.write(f"pid={os.getpid()} started={datetime.now(UTC).isoformat()}\n")
                self.handle.flush()
                return self
            if time.monotonic() >= deadline:
                self.handle.close()
                self.handle = None
                raise LockUnavailable(self.busy_message.format(path=self.path))
            time.sleep(0.2)

    def __exit__(self, exc_type, exc, traceback_obj) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.handle.close()
            self.handle = None
