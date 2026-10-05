"""Cross-platform advisory locking, including the half no POSIX runner reaches.

Every tool that can modify a movie takes a lock first, and the locks are the
reason a qBittorrent completion hook cannot place a hardlink while the cleaner
is mid-remux. Two properties make that work and neither is visible from a
happy-path test:

* **They all contend on the same file.** The cleaner, the standardizer and the
  extractor each build the lock path from a hash of the *normalized* target.
  If the normalization ever drifted between them - a trailing separator, a
  case difference on Windows - the two runs would hold different locks and
  believe they were alone with the library. Nothing would fail until a movie
  was replaced underneath a running remux.
* **A real OS error is not "busy".** The historical behaviour genuinely differs
  by caller and both halves are load-bearing: a per-tool run lock treats any
  ``OSError`` as contention and retries until its timeout, while the
  standardizer's coordination lock re-raises anything that is not a
  well-known "already locked" code. Without that distinction a read-only
  volume turns into a sixty-second hang followed by a message blaming another
  instance that does not exist.

The Windows branches are exercised through :mod:`tests.platforms` and a
recording ``msvcrt`` double. They are the branches a Windows operator runs and
the coverage runner never sees, and one of them carries a scar: an unguarded
lock-byte write grew the lock file by one byte per retry for the lifetime of a
contended wait.
"""

from __future__ import annotations

import builtins
import errno
import ntpath
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import platforms

from organizekit.core import locking

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    import fcntl
except ModuleNotFoundError:  # a Windows runner has no flock at all
    fcntl = None  # type: ignore[assignment]

#: ``flock`` conflicts per *open file description*, so two handles in one
#: process really do contend on Linux and macOS - which is what makes an
#: in-process contention test honest rather than simulated. Everywhere else the
#: same guarantees are asserted with contention injected, below.
HAS_FLOCK = fcntl is not None
needs_flock = unittest.skipUnless(HAS_FLOCK, "flock is the POSIX locking primitive")


class FakeMsvcrt(types.ModuleType):
    """``msvcrt`` as a recording double: the byte-range locker Windows uses.

    ``busy`` makes the first N lock attempts fail the way a held range does, so
    a contended wait can be run to completion without a second process.
    """

    LK_NBLCK = 2
    LK_UNLCK = 3

    def __init__(self, *, busy: int = 0, lock_error: OSError | None = None,
                 unlock_error: OSError | None = None) -> None:
        super().__init__("msvcrt")
        self.calls: list[tuple[str, int, int]] = []
        self.lock_error = lock_error
        self.unlock_error = unlock_error
        self._busy = busy

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append(("unlock" if mode == self.LK_UNLCK else "lock", fd, nbytes))
        if mode == self.LK_UNLCK:
            if self.unlock_error is not None:
                raise self.unlock_error
            return
        if self._busy > 0:
            self._busy -= 1
            raise self.lock_error or _windows_busy_error()
        if self.lock_error is not None:
            raise self.lock_error


def _windows_busy_error() -> OSError:
    """What ``msvcrt.locking`` raises for a range another process holds."""
    error = OSError("the process cannot access the file because another process has locked it")
    error.winerror = 33  # type: ignore[attr-defined]
    return error


def _windows_other_error(winerror: int | None = None, code: int = errno.EBADF) -> OSError:
    """A genuine OS failure, not contention."""
    error = OSError(code, "not a locking problem")
    if winerror is not None:
        error.winerror = winerror  # type: ignore[attr-defined]
    return error


class FakeHandle:
    """The file-object surface the locking helpers actually touch."""

    def __init__(self, *, seek_error: OSError | None = None, write_error: OSError | None = None,
                 size: int = 0) -> None:
        self.position = 0
        self.size = size
        self.seek_error = seek_error
        self.write_error = write_error
        self.seeked_to: list[tuple[int, int]] = []
        self.written: list[str | bytes] = []
        self.flushes = 0
        self.closed = False

    def fileno(self) -> int:
        return 7

    def seek(self, offset: int, whence: int = 0) -> int:
        # The end-relative probe is the one a network or device handle can
        # refuse while still serving an absolute seek, which is the failure
        # _try_lock's guard exists for.
        if self.seek_error is not None and whence == os.SEEK_END:
            raise self.seek_error
        self.seeked_to.append((offset, whence))
        self.position = self.size if whence == os.SEEK_END else offset
        return self.position

    def tell(self) -> int:
        return self.position

    def write(self, data: str | bytes) -> int:
        if self.write_error is not None:
            raise self.write_error
        self.written.append(data)
        self.size += len(data)
        return len(data)

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.closed = True


class TryFileLockTests(unittest.TestCase):
    """One function, two deliberately different failure contracts."""

    @needs_flock
    def test_a_lock_another_handle_holds_is_busy_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            with path.open("a+b") as held, path.open("a+b") as second:
                assert fcntl is not None
                fcntl.flock(held.fileno(), fcntl.LOCK_EX)
                self.assertFalse(locking.try_file_lock(second))
                self.assertFalse(locking.try_file_lock(second, strict_non_contention=True))

    @needs_flock
    def test_a_free_lock_is_taken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, (Path(tmp) / "run.lock").open("a+b") as handle:
            self.assertTrue(locking.try_file_lock(handle))

    @needs_flock
    def test_a_genuine_os_error_is_busy_for_a_run_lock_and_fatal_for_the_coordination_lock(self) -> None:
        """The difference the two callers were written against, asserted once.

        A read-only volume, a revoked network share, a file descriptor the OS
        has already closed: none of these are another instance holding the
        lock. The per-tool run locks historically retried them until the
        timeout, and still do - a run lock that raises on a transient share
        blip would abort a sweep. The coordination lock re-raises, because
        sixty seconds of retrying an ``EROFS`` and then reporting "timed out
        waiting for the lock" sends the operator looking for a phantom second
        process instead of at their mount.
        """
        handle = FakeHandle()
        assert fcntl is not None
        with mock.patch.object(fcntl, "flock",
                               side_effect=OSError(errno.EROFS, "read-only volume")):
            self.assertFalse(locking.try_file_lock(handle))
            with self.assertRaises(OSError) as caught:
                locking.try_file_lock(handle, strict_non_contention=True)
        self.assertEqual(caught.exception.errno, errno.EROFS)

    @needs_flock
    def test_the_well_known_busy_codes_are_busy_even_in_strict_mode(self) -> None:
        handle = FakeHandle()
        assert fcntl is not None
        for code in (errno.EACCES, errno.EAGAIN):
            with self.subTest(errno=code), \
                    mock.patch.object(fcntl, "flock", side_effect=OSError(code, "busy")):
                self.assertFalse(locking.try_file_lock(handle, strict_non_contention=True))

    def test_the_windows_lock_is_taken_at_the_start_of_the_file(self) -> None:
        """``msvcrt.locking`` locks a byte range at the *current* position.

        The ``seek(0)`` is what makes the range stable: without it a retry
        after any write would lock a different byte than the holder did, and
        two processes would each believe they owned the file.
        """
        handle = FakeHandle()
        fake = FakeMsvcrt()
        with platforms.windows(), mock.patch.dict(sys.modules, {"msvcrt": fake}):
            self.assertTrue(locking.try_file_lock(handle))
        self.assertEqual(handle.seeked_to, [(0, 0)])
        self.assertEqual(fake.calls, [("lock", 7, 1)])

    def test_windows_contention_is_busy_under_both_contracts(self) -> None:
        handle = FakeHandle()
        for strict in (False, True):
            with self.subTest(strict=strict):
                fake = FakeMsvcrt(lock_error=_windows_busy_error())
                with platforms.windows(), mock.patch.dict(sys.modules, {"msvcrt": fake}):
                    self.assertFalse(locking.try_file_lock(handle, strict_non_contention=strict))

    def test_windows_reports_the_documented_lock_sharing_codes_as_busy(self) -> None:
        """33 and 36 are "another process has it"; anything else is a fault."""
        handle = FakeHandle()
        for winerror in (33, 36):
            for code in (errno.EACCES, errno.EAGAIN, None):
                with self.subTest(winerror=winerror, errno=code):
                    error = OSError("locked")
                    error.winerror = winerror  # type: ignore[attr-defined]
                    if code is not None:
                        error.errno = code
                    else:
                        error.errno = None  # type: ignore[assignment]
                    with platforms.windows(), \
                            mock.patch.dict(sys.modules, {"msvcrt": FakeMsvcrt(lock_error=error)}):
                        self.assertFalse(locking.try_file_lock(handle, strict_non_contention=True))

    def test_a_windows_fault_that_is_not_contention_is_raised_in_strict_mode(self) -> None:
        handle = FakeHandle()
        error = _windows_other_error()
        with platforms.windows(), mock.patch.dict(sys.modules, {"msvcrt": FakeMsvcrt(lock_error=error)}):
            with self.assertRaises(OSError):
                locking.try_file_lock(handle, strict_non_contention=True)
            self.assertFalse(locking.try_file_lock(handle))

    def test_the_same_fault_is_only_busy_for_a_non_strict_caller(self) -> None:
        handle = FakeHandle()
        with platforms.windows(), \
                mock.patch.dict(sys.modules, {"msvcrt": FakeMsvcrt(lock_error=_windows_other_error())}):
            self.assertFalse(locking.try_file_lock(handle, strict_non_contention=False))


class CoordinationLockTests(unittest.TestCase):
    """The one lock every tool shares, so no two of them touch a movie at once."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="coordination_")
        self.target = Path(self._tmp.name) / "library"
        self.target.mkdir()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._remove_lock_file)
        self.slept: list[float] = []

    def _remove_lock_file(self) -> None:
        lock = locking.CoordinationLock(self.target, timeout_seconds=0.0)
        lock.path.unlink(missing_ok=True)

    def _no_sleeping(self) -> Any:
        """Record the backoff instead of paying for it; the deadline still bites."""
        return mock.patch.object(locking.time, "sleep", self.slept.append)

    def test_the_lock_is_taken_and_released(self) -> None:
        lock = locking.CoordinationLock(self.target, timeout_seconds=5.0)
        lock.acquire()
        self.assertTrue(lock.path.is_file())
        # Not read_bytes() while the lock is held: on Windows the byte-range
        # lock denies a second handle to the very byte it protects, so the byte
        # is measured by size now and by content once the holder is gone.
        self.assertEqual(lock.path.stat().st_size, 1,
                         "the first byte is materialised for the Windows range lock")
        lock.release()
        self.assertEqual(lock.path.read_bytes(), b"\0")
        # A second holder can take it immediately, so the release really unlocked.
        again = locking.CoordinationLock(self.target, timeout_seconds=5.0)
        again.acquire()
        again.release()

    def test_release_is_a_context_manager_and_is_idempotent(self) -> None:
        with locking.CoordinationLock(self.target, timeout_seconds=5.0) as lock:
            handle = lock._fh
            self.assertIsNotNone(handle)
        self.assertTrue(handle.closed, "__exit__ released the lock and closed the file")
        lock.release()  # already released; a second call must be a silent no-op
        self.assertIsNone(lock._fh)

    @needs_flock
    def test_a_second_holder_times_out_and_leaks_no_file_handle(self) -> None:
        """The failure mode a real sweep hits when the ingest hook is mid-run.

        Two assertions matter beyond the exception: the message has to name the
        wait and the lock file so an operator can find the holder, and the
        failed acquire must have closed its handle - a run that retries a
        contended lock for a thousand movies would otherwise end with a
        thousand open descriptors.
        """
        opened: list[Any] = []
        real_open = builtins.open

        def tracking_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            handle = real_open(file, *args, **kwargs)
            opened.append(handle)
            return handle

        with locking.CoordinationLock(self.target, timeout_seconds=5.0):
            contended = locking.CoordinationLock(self.target, timeout_seconds=0.3)
            with mock.patch.object(locking, "open", tracking_open, create=True), \
                    self._no_sleeping(), \
                    self.assertRaises(locking.LockTimeoutError) as caught:
                contended.acquire()
        self.assertEqual(len(opened), 1, "one attempt, one handle")
        self.assertIn("Timed out after 0.3s", str(caught.exception))
        self.assertIn(contended.path.name, str(caught.exception))
        self.assertIsNone(contended._fh, "a failed acquire must not keep the handle open")
        self.assertTrue(opened[0].closed,
                        "a sweep that retries a contended lock a thousand times must not end "
                        "with a thousand open descriptors")
        contended.release()  # and releasing afterwards is harmless
        self.assertTrue(self.slept, "a contended acquire backs off between attempts")
        self.assertTrue(all(delay == 0.1 for delay in self.slept))

    def test_a_lock_that_never_becomes_available_times_out_and_closes_its_handle(self) -> None:
        """The same guarantee, on every platform: a refused lock leaves nothing open.

        Contention is injected at ``try_file_lock`` rather than arranged with a
        second handle, so the timeout, the backoff and the handle cleanup are
        asserted on Windows and macOS runners too - where the POSIX flock
        behaviour the test above relies on does not exist.
        """
        opened: list[Any] = []
        real_open = builtins.open

        def tracking_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            handle = real_open(file, *args, **kwargs)
            opened.append(handle)
            return handle

        contended = locking.CoordinationLock(self.target, timeout_seconds=0.3)
        with mock.patch.object(locking, "open", tracking_open, create=True), \
                mock.patch.object(locking, "try_file_lock", lambda handle, **kw: False), \
                self._no_sleeping(), self.assertRaises(locking.LockTimeoutError):
            contended.acquire()
        self.assertIsNone(contended._fh)
        self.assertTrue(opened[0].closed, "the descriptor is closed on the way out of the failure")
        self.assertTrue(self.slept, "a contended acquire backs off between attempts")
        contended.release()

    def test_lock_timeout_error_is_a_timeout_error(self) -> None:
        """The cleaner catches the built-in ``TimeoutError``; the subclass keeps that true."""
        self.assertTrue(issubclass(locking.LockTimeoutError, TimeoutError))
        self.assertTrue(issubclass(locking.LockUnavailable, RuntimeError))

    def test_the_lock_file_is_keyed_by_the_normalized_target(self) -> None:
        """Every tool must arrive at the *same* file, or they do not contend.

        This is the whole cross-tool guarantee: the cleaner and the
        standardizer are separate programs started separately, and the only
        thing that stops them remuxing one movie at the same time is that they
        both hash the same normalized path.
        """
        first = locking.CoordinationLock(self.target)
        second = locking.CoordinationLock(str(self.target) + os.sep)
        third = locking.CoordinationLock(Path(str(self.target)))
        self.assertEqual(first.path, second.path)
        self.assertEqual(first.path, third.path)
        self.assertTrue(first.path.name.startswith(locking.STANDARDIZER_LOCK_NAME))

    def test_the_key_lives_in_the_temp_directory_beside_the_other_tools(self) -> None:
        self.assertEqual(locking.CoordinationLock(self.target).path.parent,
                         Path(tempfile.gettempdir()))

    def test_windows_targets_differing_only_in_case_share_one_lock(self) -> None:
        """Windows paths are case-insensitive; the lock key has to be too.

        ``organize run --source C:\\Media`` and a completion hook configured
        with ``c:\\media`` are the same library. Two lock files would mean two
        tools remuxing one movie.
        """
        with platforms.windows(), mock.patch.object(os.path, "normcase", ntpath.normcase):
            upper = locking.CoordinationLock(r"C:\Media\Movies")
            lower = locking.CoordinationLock(r"c:\media\movies")
        self.assertEqual(upper.path, lower.path)

    def test_a_negative_timeout_becomes_no_wait_at_all(self) -> None:
        lock = locking.CoordinationLock(self.target, timeout_seconds=-30.0)
        self.assertEqual(lock.timeout_seconds, 0.0)

    def test_windows_release_unlocks_the_range_and_closes_the_handle(self) -> None:
        fake = FakeMsvcrt()
        lock = locking.CoordinationLock(self.target, timeout_seconds=1.0)
        with platforms.windows(), mock.patch.dict(sys.modules, {"msvcrt": fake}):
            lock.acquire()
            handle = lock._fh
            fd = handle.fileno()
            lock.release()
        self.assertIsNone(lock._fh)
        self.assertTrue(handle.closed)
        self.assertIn(("unlock", fd, 1), fake.calls)

    def test_a_windows_release_that_fails_still_closes_the_handle(self) -> None:
        """Unlocking a range the OS already dropped must not strand the descriptor.

        ``release`` runs from ``__exit__``, so an exception escaping it would
        replace whatever the ``with`` body raised - the operator would see a
        locking error instead of the remux failure that actually happened.
        """
        fake = FakeMsvcrt(unlock_error=OSError("range already released"))
        lock = locking.CoordinationLock(self.target, timeout_seconds=1.0)
        with platforms.windows(), mock.patch.dict(sys.modules, {"msvcrt": fake}):
            lock.acquire()
            handle = lock._fh
            lock.release()  # must not raise
        self.assertIsNone(lock._fh)
        self.assertTrue(handle.closed)


class ExclusiveRunLockTests(unittest.TestCase):
    """The per-tool run lock: one sweep at a time, and a message that says whose."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="runlock_")
        self.path = Path(self._tmp.name) / "sub" / "run.lock"
        self.addCleanup(self._tmp.cleanup)
        self.slept: list[float] = []

    def _no_sleeping(self) -> Any:
        return mock.patch.object(locking.time, "sleep", self.slept.append)

    def test_the_holder_writes_who_it_is_and_when_it_started(self) -> None:
        """The file an operator cats when a sweep will not start.

        A crashed run leaves this behind, and "another run holds the lock" is
        only actionable if the lock says which pid and when.
        """
        with locking.ExclusiveRunLock(self.path, 1.0, busy_message="held by {path}"):
            self.assertTrue(self.path.is_file(), "the lock file exists while it is held")
        # Read after the release. The holder line outlives the holder - that is
        # the whole point of writing it - and on Windows a second handle cannot
        # read a byte the range lock is holding.
        content = self.path.read_text(encoding="utf-8")
        self.assertTrue(content.startswith(f"pid={os.getpid()} "), content)
        self.assertIn("started=", content)
        self.assertTrue(self.path.parent.is_dir(), "the lock's directory is created on demand")

    def test_a_stale_holder_line_is_replaced_not_appended(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("pid=1 started=1970-01-01T00:00:00+00:00\n", encoding="utf-8")
        with locking.ExclusiveRunLock(self.path, 1.0):
            pass
        lines = self.path.read_text(encoding="utf-8").splitlines()  # see above: not while held
        self.assertEqual(len(lines), 1, "a lock file must not accumulate dead holders")
        self.assertTrue(lines[0].startswith(f"pid={os.getpid()} "))

    def test_a_windows_run_lock_that_stays_busy_reports_the_tools_own_message(self) -> None:
        fake = FakeMsvcrt(busy=10_000, lock_error=_windows_busy_error())
        lock = locking.ExclusiveRunLock(self.path, 0.3, busy_message="another audit owns {path}")
        with platforms.windows(), mock.patch.dict(sys.modules, {"msvcrt": fake}), \
                self._no_sleeping(), self.assertRaises(locking.LockUnavailable) as caught:
            lock.__enter__()
        self.assertIn("another audit owns", str(caught.exception))
        self.assertIn(str(self.path), str(caught.exception))
        self.assertIsNone(lock.handle)

    @needs_flock
    def test_a_contended_run_lock_reports_the_tools_own_message(self) -> None:
        with locking.ExclusiveRunLock(self.path, 1.0):
            second = locking.ExclusiveRunLock(self.path, 0.05, busy_message="another audit owns {path}")
            with self._no_sleeping(), self.assertRaises(locking.LockUnavailable) as caught:
                second.__enter__()
        self.assertIn("another audit owns", str(caught.exception))
        self.assertIn(str(self.path), str(caught.exception))
        self.assertIsNone(second.handle, "the refused lock keeps no descriptor open")
        self.assertTrue(self.slept)
        self.assertTrue(all(delay == 0.2 for delay in self.slept))

    def test_exiting_a_lock_that_was_never_taken_is_harmless(self) -> None:
        lock = locking.ExclusiveRunLock(self.path, 1.0)
        lock.__exit__(None, None, None)  # no handle, no raise
        self.assertIsNone(lock.handle)

    def test_the_windows_lock_byte_is_materialised_once_no_matter_how_long_the_wait(self) -> None:
        """The scar, as a property: a contended wait must not grow the lock file.

        One of the two vendored copies of this class checked emptiness with
        ``seek(0)``/``tell()``, which in ``"a+"`` mode is *always* 0, so it
        appended a ``0`` on every retry - a lock held for ten minutes left a
        file of thousands of bytes behind. The guard is
        ``seek(0, SEEK_END)``/``tell() == 0``, and what makes it observable is
        the file size during the wait, not after it: ``__enter__`` truncates
        once it wins, so the finished file looks the same either way.
        """
        sizes: list[int] = []
        fake = FakeMsvcrt(busy=4)
        real_locking = fake.locking

        def measuring_locking(fd: int, mode: int, nbytes: int) -> None:
            if mode == fake.LK_NBLCK:
                sizes.append(self.path.stat().st_size if self.path.exists() else 0)
            real_locking(fd, mode, nbytes)

        fake.locking = measuring_locking  # type: ignore[method-assign]
        lock = locking.ExclusiveRunLock(self.path, 5.0)
        with platforms.windows(), mock.patch.dict(sys.modules, {"msvcrt": fake}), \
                self._no_sleeping(), lock:
            pass
        self.assertEqual(len(sizes), 5, "four refused attempts and then the one that won")
        self.assertEqual(set(sizes), {1},
                         "exactly one lock byte, however many retries it took")
        self.assertEqual(lock.path.read_text(encoding="utf-8").count("\n"), 1)

    def test_that_property_notices_the_unguarded_implementation(self) -> None:
        """Break the guard on purpose; the test above has to go red.

        Without this the size assertion would pass just as happily against a
        lock that never wrote a byte at all, and the property would be
        decoration. This is the mutation-test convention ``test_properties.py``
        established.
        """
        def unguarded(self: locking.ExclusiveRunLock) -> bool:
            """The version that shipped in one of the two copies."""
            assert self.handle is not None
            try:
                self.handle.write("0")
                self.handle.flush()
            except OSError:
                pass
            return locking.try_file_lock(self.handle, strict_non_contention=False)

        with mock.patch.object(locking.ExclusiveRunLock, "_try_lock", unguarded), \
                self.assertRaises(AssertionError):
            self.test_the_windows_lock_byte_is_materialised_once_no_matter_how_long_the_wait()

    def test_a_seek_that_fails_still_attempts_the_lock(self) -> None:
        """An unseekable handle must not turn into an unlockable one.

        The emptiness probe is a courtesy to the Windows range lock; if the
        filesystem refuses it, the lock attempt is still the thing that decides
        whether this run may proceed.
        """
        fake = FakeMsvcrt()
        lock = locking.ExclusiveRunLock(self.path, 1.0)
        with platforms.windows(), mock.patch.dict(sys.modules, {"msvcrt": fake}):
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            handle = FakeHandle(seek_error=OSError("not seekable"))
            lock.handle = handle
            self.assertTrue(lock._try_lock())
        self.assertEqual(handle.written, [], "nothing is materialised when emptiness is unknown")
        self.assertEqual([call[0] for call in fake.calls], ["lock"])

    def test_a_write_that_fails_does_not_stop_the_lock(self) -> None:
        fake = FakeMsvcrt()
        lock = locking.ExclusiveRunLock(self.path, 1.0)
        with platforms.windows(), mock.patch.dict(sys.modules, {"msvcrt": fake}):
            handle = FakeHandle(write_error=OSError("no space left on device"))
            lock.handle = handle
            self.assertTrue(lock._try_lock(), "the byte is a convenience; the lock is the contract")

    def test_windows_release_unlocks_and_swallows_a_failed_unlock(self) -> None:
        fake = FakeMsvcrt(unlock_error=OSError("already released"))
        lock = locking.ExclusiveRunLock(self.path, 1.0)
        handle = FakeHandle(size=1)
        lock.handle = handle
        with platforms.windows(), mock.patch.dict(sys.modules, {"msvcrt": fake}):
            lock.__exit__(None, None, None)  # must not raise
        self.assertIsNone(lock.handle)
        self.assertTrue(handle.closed)
        self.assertEqual([call[0] for call in fake.calls], ["unlock"])


if __name__ == "__main__":
    unittest.main()
