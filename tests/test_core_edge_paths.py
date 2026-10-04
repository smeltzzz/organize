"""The degraded paths of the shared core: what it does when things go wrong.

``organizekit.core`` is the one place every tool's durability, locking and
report guarantees are implemented, and the interesting half of each of those
guarantees is the failure half: a lock that cannot be taken, a cache write that
fails, a sidecar that cannot be read, a console that refuses to be reconfigured.

Each test here pins one of those answers to a *stated* promise rather than to
the particular statement that implements it. A lock that cannot prove it is
alone must refuse; a filesystem primitive that fails must leave the previous
bytes in place and no debris behind; a derived cache that breaks must degrade
to "no answer" and never to a wrong one.
"""

from __future__ import annotations

import contextlib
import errno
import io
import os
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from organizekit.core import config as config_mod
from organizekit.core import console as console_mod
from organizekit.core import fsio, locking, playbackchain, probecache, report, smoke, state, subtitles, text

WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# locking.py
# ---------------------------------------------------------------------------


class _FakeHandle:
    """The sliver of a file handle ``msvcrt``-style locking touches."""

    def __init__(self, *, data: bytes = b"", fail_seek: bool = False,
                 fail_write: bool = False, unlock_error: bool = False) -> None:
        self._data = data
        self._pos = 0
        self.fail_seek = fail_seek
        self.fail_write = fail_write
        self.unlock_error = unlock_error
        self.closed = False
        self.written: list[bytes | str] = []

    def seek(self, offset: int, whence: int = 0) -> int:
        if self.fail_seek:
            raise OSError(errno.EINVAL, "seek refused")
        if whence == os.SEEK_END:
            self._pos = len(self._data) + offset
        else:
            self._pos = offset
        return self._pos

    def tell(self) -> int:
        return self._pos

    def write(self, data: bytes | str) -> int:
        if self.fail_write:
            raise OSError(errno.EIO, "write refused")
        self.written.append(data)
        self._data += data if isinstance(data, bytes) else data.encode()
        return len(data)

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        self.closed = True


def _fake_fcntl(error: OSError | None = None) -> types.ModuleType:
    """A stand-in ``fcntl`` that either takes the lock or raises ``error``."""
    module = types.ModuleType("fcntl")
    module.LOCK_EX = 2  # type: ignore[attr-defined]
    module.LOCK_NB = 4  # type: ignore[attr-defined]
    module.LOCK_UN = 8  # type: ignore[attr-defined]

    def flock(fd: int, operation: int) -> None:
        if error is not None:
            raise error

    module.flock = flock  # type: ignore[attr-defined]
    return module


def _fake_msvcrt(behaviour: object = "ok") -> types.ModuleType:
    """A stand-in for the Windows-only ``msvcrt`` module.

    ``behaviour`` is ``"ok"``, ``"busy"`` (raises the expected contention
    error) or a string errno name for a genuine failure.
    """
    module = types.ModuleType("msvcrt")
    module.LK_NBLCK = 1  # type: ignore[attr-defined]
    module.LK_UNLCK = 2  # type: ignore[attr-defined]

    def locking(handle: object, mode: int, length: int) -> None:
        if behaviour == "ok":
            return
        exc = OSError(errno.EAGAIN, "held by another process")
        if behaviour == "busy":
            raise exc
        if behaviour != "ok":
            raise OSError(getattr(errno, str(behaviour)), "a real error")

    module.locking = locking  # type: ignore[attr-defined]
    return module


@contextlib.contextmanager
def _as_windows(fake_msvcrt: object):
    """Run a block with ``os.name`` claiming Windows and a fake ``msvcrt``."""
    with (
        mock.patch.object(locking.os, "name", "nt"),
        mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
    ):
        yield


class FileLockContractTests(unittest.TestCase):
    """``try_file_lock`` is the one primitive the whole locking story rests on.

    Its contract: ``True`` when taken, ``False`` when someone else holds it,
    and - in strict mode - a raised error for anything that is *not*
    contention. Swallowing a real error as "busy" is how a fail-closed lock
    turns into a fail-open one.
    """

    def test_non_strict_treats_any_oserror_as_contention(self) -> None:
        """The historical per-tool lock retried every failure until timeout."""
        with mock.patch.dict(sys.modules, {"fcntl": _fake_fcntl(OSError(errno.EBADF, "bad handle"))}), \
                tempfile.TemporaryFile() as handle:
            self.assertFalse(locking.try_file_lock(handle))

    def test_strict_contention_is_still_just_contention(self) -> None:
        with mock.patch.dict(sys.modules, {"fcntl": _fake_fcntl(OSError(errno.EAGAIN, "held"))}), \
                tempfile.TemporaryFile() as handle:
            self.assertFalse(locking.try_file_lock(handle, strict_non_contention=True))

    def test_strict_mode_refuses_to_pretend_a_real_error_is_contention(self) -> None:
        with mock.patch.dict(sys.modules,
                              {"fcntl": _fake_fcntl(OSError(errno.EPERM, "not permitted"))}), \
                tempfile.TemporaryFile() as handle, self.assertRaises(OSError):
            locking.try_file_lock(handle, strict_non_contention=True)

    def test_windows_takes_the_lock_when_it_can(self) -> None:
        handle = _FakeHandle()
        with _as_windows(_fake_msvcrt("ok")):
            self.assertTrue(locking.try_file_lock(handle))

    def test_windows_non_strict_reports_a_busy_console_as_busy(self) -> None:
        with _as_windows(_fake_msvcrt("busy")):
            self.assertFalse(locking.try_file_lock(_FakeHandle()))

    def test_windows_strict_reraises_an_errno_it_does_not_understand(self) -> None:
        """EBADF is a broken handle, not a held lock; retrying it for a minute
        is exactly the fail-open bug strict mode exists to stop."""
        with _as_windows(_fake_msvcrt("EBADF")), self.assertRaises(OSError):
            locking.try_file_lock(_FakeHandle(), strict_non_contention=True)

    def test_windows_strict_contention_codes_are_busy(self) -> None:
        with _as_windows(_fake_msvcrt("busy")):
            self.assertFalse(locking.try_file_lock(_FakeHandle(), strict_non_contention=True))


class CoordinationLockTests(unittest.TestCase):
    """Two tools must never remux the same movie at once.

    The lock is fail-closed: a holder that cannot be excluded is a run that
    must not start, and a timeout must release the file handle it opened so
    the next run can try again.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="core_lock_")
        self.addCleanup(self._td.cleanup)
        self.target = Path(self._td.name) / "library"

    def test_an_acquired_lock_excludes_a_second_one_until_release(self) -> None:
        first = locking.CoordinationLock(self.target, timeout_seconds=1.0)
        second = locking.CoordinationLock(self.target, timeout_seconds=0.0)
        first.acquire()
        self.addCleanup(first.release)
        with self.assertRaises(locking.LockTimeoutError):
            second.acquire()
        first.release()
        third = locking.CoordinationLock(self.target, timeout_seconds=1.0)
        third.acquire()
        third.release()

    def test_a_timeout_does_not_leak_the_handle_it_opened(self) -> None:
        holder = locking.CoordinationLock(self.target, timeout_seconds=1.0)
        holder.acquire()
        self.addCleanup(holder.release)
        loser = locking.CoordinationLock(self.target, timeout_seconds=0.0)
        with self.assertRaises(locking.LockTimeoutError):
            loser.acquire()
        # A held handle is what makes the next attempt fail on Windows too:
        # nothing can delete or replace a file with an open handle.
        self.assertIsNone(loser._fh)
        holder.release()

    def test_an_interrupt_while_waiting_closes_the_handle(self) -> None:
        """Ctrl-C during the wait must not leave a lock file open behind it."""
        with mock.patch.object(locking, "try_file_lock", side_effect=KeyboardInterrupt):
            lock = locking.CoordinationLock(self.target, timeout_seconds=1.0)
            with self.assertRaises(KeyboardInterrupt):
                lock.acquire()
        self.assertIsNone(lock._fh)

    def test_release_without_acquire_is_a_no_op(self) -> None:
        locking.CoordinationLock(self.target).release()

    def test_release_unlocks_and_closes_the_handle(self) -> None:
        lock = locking.CoordinationLock(self.target, timeout_seconds=1.0)
        lock.acquire()
        handle = lock._fh
        lock.release()
        self.assertIsNone(lock._fh)
        self.assertTrue(handle.closed)

    def test_a_windows_release_never_raises_over_a_refusing_console(self) -> None:
        """An un-unlockable range must still close the handle: the run is over
        either way, and a held handle outlives the process on Windows."""
        handle = _FakeHandle()
        lock = locking.CoordinationLock(self.target, timeout_seconds=1.0)
        lock._fh = handle

        def locking_call(*args: object, **kwargs: object) -> None:
            raise OSError(errno.EINVAL, "cannot unlock")

        fake = _fake_msvcrt("ok")
        fake.locking = locking_call  # type: ignore[attr-defined]
        with _as_windows(fake):
            lock.release()
        self.assertIsNone(lock._fh)
        self.assertTrue(handle.closed)


class ExclusiveRunLockTests(unittest.TestCase):
    """One run per library at a time, and the Windows details that make it so."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="core_runlock_")
        self.addCleanup(self._td.cleanup)
        self.path = Path(self._td.name) / "run.lock"

    def test_a_busy_lock_times_out_with_the_path_in_the_message(self) -> None:
        holder = locking.ExclusiveRunLock(self.path, 5.0, busy_message="held by {path}")
        holder.__enter__()
        self.addCleanup(holder.__exit__, None, None, None)
        loser = locking.ExclusiveRunLock(self.path, 0.05, busy_message="held by {path}")
        with self.assertRaises(locking.LockUnavailable) as caught:
            loser.__enter__()
        self.assertIn(str(self.path), str(caught.exception))
        self.assertIsNone(loser.handle, "the losing handle must not be left open")

    def test_exit_without_enter_is_a_no_op(self) -> None:
        locking.ExclusiveRunLock(self.path, 0.0).__exit__(None, None, None)

    def test_the_windows_lock_byte_is_materialised_but_never_grown(self) -> None:
        """The bug this guards: appending a byte on *every* retry grew the lock
        file for the whole contended wait."""
        handle = _FakeHandle()
        lock = locking.ExclusiveRunLock(self.path, 0.0)
        lock.handle = handle
        with _as_windows(_fake_msvcrt("ok")), \
                mock.patch.object(locking, "try_file_lock", return_value=True):
            self.assertTrue(lock._try_lock())
            self.assertEqual(["0"], handle.written)
            self.assertTrue(lock._try_lock())
        self.assertEqual(["0"], handle.written, "an existing byte is never rewritten")

    def test_a_windows_handle_that_cannot_be_probed_is_not_treated_as_empty(self) -> None:
        """If the size cannot be read, the byte is not written: a blind write is
        the growth bug again."""
        handle = _FakeHandle(fail_seek=True)
        lock = locking.ExclusiveRunLock(self.path, 0.0)
        lock.handle = handle
        with _as_windows(_fake_msvcrt("ok")), \
                mock.patch.object(locking, "try_file_lock", return_value=True):
            self.assertTrue(lock._try_lock())
        self.assertEqual([], handle.written)

    def test_a_windows_byte_that_cannot_be_written_does_not_stop_the_lock(self) -> None:
        handle = _FakeHandle(fail_write=True)
        lock = locking.ExclusiveRunLock(self.path, 0.0)
        lock.handle = handle
        with _as_windows(_fake_msvcrt("ok")), \
                mock.patch.object(locking, "try_file_lock", return_value=True):
            self.assertTrue(lock._try_lock())

    def test_a_windows_exit_unlocks_even_when_the_unlock_fails(self) -> None:
        handle = _FakeHandle(unlock_error=True)
        lock = locking.ExclusiveRunLock(self.path, 0.0)
        lock.handle = handle

        def locking_call(*args: object, **kwargs: object) -> None:
            raise OSError(errno.EINVAL, "cannot unlock")

        fake = _fake_msvcrt("ok")
        fake.locking = locking_call  # type: ignore[attr-defined]
        with _as_windows(fake):
            lock.__exit__(None, None, None)
        self.assertIsNone(lock.handle)
        self.assertTrue(handle.closed)

    def test_the_windows_exit_unlocks_the_range_it_took(self) -> None:
        handle = _FakeHandle()
        lock = locking.ExclusiveRunLock(self.path, 0.0)
        lock.handle = handle
        with _as_windows(_fake_msvcrt("ok")):
            lock.__exit__(None, None, None)
        self.assertTrue(handle.closed)
        self.assertIsNone(lock.handle)


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------


class _FakeWindowsPath(pathlib.PureWindowsPath):
    """``PureWindowsPath`` plus the one probe ``default_reports_root`` makes.

    The real Windows branch returns ``Path(r"E:\\...")``, which cannot even be
    constructed on POSIX - so the branch is exercised through the pure path
    flavour and answers the drive question from a settable set.
    """

    @classmethod
    def home(cls) -> _FakeWindowsPath:
        return cls(r"C:\Users\tester")

    def exists(self) -> bool:
        return False


class DotenvEdgeCaseTests(unittest.TestCase):
    """A typo in a config file must not stop a maintenance run.

    So every malformed shape is *skipped*, real environment variables still
    win, and a file that is not there or not readable is simply not a source.
    """

    def _load(self, body: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text(body, encoding="utf-8")
            return config_mod.load_dotenv(env)

    def test_comments_and_malformed_lines_are_skipped(self) -> None:
        loaded = self._load(
            "# a comment\n"
            "\n"
            "no-equals-sign\n"
            "= orphan value\n"
            "export ORGANIZE_EXPORTED=yes\n"
            'ORGANIZE_QUOTED="quoted value"\n'
            "ORGANIZE_SINGLE='single'\n"
        )
        self.assertNotIn("no-equals-sign", loaded)
        self.assertEqual("yes", loaded["ORGANIZE_EXPORTED"])
        self.assertEqual("quoted value", loaded["ORGANIZE_QUOTED"])
        self.assertEqual("single", loaded["ORGANIZE_SINGLE"])
        for key in ("ORGANIZE_EXPORTED", "ORGANIZE_QUOTED", "ORGANIZE_SINGLE"):
            os.environ.pop(key, None)

    def test_a_real_environment_variable_still_wins(self) -> None:
        with mock.patch.dict(os.environ, {"ORGANIZE_WINS": "exported"}):
            loaded = self._load("ORGANIZE_WINS=from-file\n")
            self.assertEqual("from-file", loaded["ORGANIZE_WINS"])
            self.assertEqual("exported", os.environ["ORGANIZE_WINS"],
                             "an explicit export beats a stale file")

    def test_an_unreadable_file_is_not_a_source(self) -> None:
        missing = Path(tempfile.gettempdir()) / "organize-no-such-env-file"
        self.assertEqual({}, config_mod.load_dotenv(missing))

    def test_candidate_directories_that_raise_are_skipped(self) -> None:
        """A CWD that cannot be resolved (deleted, unreadable) is skipped
        rather than fatal: resolution is best-effort by design."""
        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("no")

        with mock.patch.object(Path, "cwd", boom), \
                mock.patch.object(Path, "resolve", boom):
            config_mod._dotenv_candidates()

    def test_the_installation_root_is_searched_in_both_layouts(self) -> None:
        seen = config_mod._dotenv_candidates()
        self.assertTrue(any(p.parent == config_mod.Path(__file__).resolve().parents[2] for p in seen),
                        "the source layout's repository root must be a candidate")


class PlatformDefaultTests(unittest.TestCase):
    """The defaults are per-platform for a reason: hardcoding the Windows path
    made a POSIX run create literal ``E:\\torrents\\...`` directories in the
    working directory."""

    def test_windows_library_default_is_the_documented_layout(self) -> None:
        with mock.patch.object(config_mod.os, "name", "nt"), \
                mock.patch.object(config_mod, "Path", _FakeWindowsPath):
            self.assertEqual(r"E:\torrents\final_organized",
                             str(config_mod.default_library_root()))

    def test_windows_reports_default_uses_the_documented_drive_when_it_exists(self) -> None:
        with mock.patch.object(config_mod.os, "name", "nt"), \
                mock.patch.object(config_mod, "Path", _FakeWindowsPath), \
                mock.patch.object(_FakeWindowsPath, "exists", lambda self: True):
            self.assertEqual(r"E:\torrents\tools\ReportsAndLogs",
                             str(config_mod.default_reports_root()))

    def test_windows_reports_default_falls_back_without_the_drive(self) -> None:
        """A Windows machine with no E: drive used to get a default nothing
        could be written to, and every tool then exited 2 on its own report."""
        with mock.patch.object(config_mod.os, "name", "nt"), \
                mock.patch.object(config_mod, "Path", _FakeWindowsPath), \
                mock.patch.object(_FakeWindowsPath, "exists", lambda self: False), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}):
            self.assertEqual(r"C:\Users\u\AppData\Local\organize",
                             str(config_mod.default_reports_root()))

    def test_posix_state_home_is_honoured(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-state"}):
            self.assertEqual(Path("/tmp/xdg-state") / "organize", config_mod.default_reports_root())

    def test_origin_is_named_for_every_source(self) -> None:
        with mock.patch.dict(os.environ, {"ORGANIZE_LIBRARY": "/tmp/organize-lib"}):
            self.assertEqual("--source", config_mod.describe_library_origin("/tmp/explicit"))
            self.assertEqual("ORGANIZE_LIBRARY", config_mod.describe_library_origin(None))
            self.assertEqual(Path("/tmp/organize-lib"), config_mod.resolve_library(None))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn("default library root", config_mod.describe_library_origin(None))


# ---------------------------------------------------------------------------
# fsio.py
# ---------------------------------------------------------------------------


class AtomicWriteFailureTests(unittest.TestCase):
    """Publishing a report is the last thing a run does; it may fail, but it
    may not take the previous report with it or leave staging debris."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="core_fsio_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def _stages(self) -> list[Path]:
        return [p for p in self.root.iterdir() if p.name.startswith(".")]

    def test_create_if_absent_never_clobbers_and_leaves_no_debris(self) -> None:
        dest = self.root / "movie.eng.srt"
        dest.write_text("the existing sidecar\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            fsio.atomic_write_text(dest, "a downloaded sidecar\n", replace=False)
        self.assertEqual("the existing sidecar\n", dest.read_text(encoding="utf-8"))
        self.assertEqual([], self._stages())

    def test_a_stage_that_cannot_be_cleaned_still_reports_the_collision(self) -> None:
        dest = self.root / "movie.eng.srt"
        dest.write_text("existing\n", encoding="utf-8")
        with mock.patch.object(Path, "unlink", side_effect=OSError(errno.EPERM, "no")), \
                self.assertRaises(FileExistsError):
            fsio.atomic_write_text(dest, "new\n", replace=False)

    def test_a_failed_publish_keeps_the_previous_bytes(self) -> None:
        dest = self.root / "report.txt"
        dest.write_text("old\n", encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError(errno.ENOSPC, "full")), self.assertRaises(OSError):
            fsio.atomic_write_text(dest, "new\n")
        self.assertEqual("old\n", dest.read_text(encoding="utf-8"))
        self.assertEqual([], self._stages())

    def test_an_uncleanable_stage_does_not_mask_the_write_error(self) -> None:
        dest = self.root / "report.txt"
        with mock.patch("os.replace", side_effect=OSError(errno.ENOSPC, "full")), \
                mock.patch.object(Path, "unlink", side_effect=OSError(errno.EPERM, "no")), self.assertRaises(OSError):
            fsio.atomic_write_text(dest, "new\n")

    def test_a_link_publish_that_cannot_remove_its_stage_still_succeeds(self) -> None:
        """The link already published the bytes; a leftover second name for the
        same inode is harmless and must not become the caller's error."""
        dest = self.root / "movie.eng.srt"
        stage_unlinks = {"count": 0}
        real_unlink = Path.unlink

        def unlink(self: Path, *args: object, **kwargs: object) -> None:
            stage_unlinks["count"] += 1
            raise OSError(errno.EPERM, "no")

        with mock.patch.object(Path, "unlink", unlink):
            fsio.atomic_write_text(dest, "sidecar\n", replace=False)
        self.assertEqual("sidecar\n", dest.read_text(encoding="utf-8"))
        self.assertGreater(stage_unlinks["count"], 0)
        for leftover in self._stages():
            real_unlink(leftover, missing_ok=True)

    def test_a_snapshot_of_a_missing_file_never_reads_as_unchanged(self) -> None:
        """'I cannot prove it is unchanged' must mean 'refuse to replace'."""
        self.assertFalse(fsio.source_snapshot_matches(self.root / "gone.mkv", {"size": 0}))
        self.assertFalse(fsio.source_snapshot_matches(self.root / "gone.mkv", {}))

    def test_a_snapshot_is_refused_when_a_named_field_is_missing(self) -> None:
        movie = self.root / "movie.mkv"
        movie.write_bytes(b"bytes")
        snapshot = fsio.source_snapshot(movie)
        for missing in ("size", "mtime_ns", "device", "inode", "identity"):
            partial = {k: v for k, v in snapshot.items() if k != missing}
            self.assertFalse(fsio.source_snapshot_matches(movie, partial), missing)

    def test_sha256_file_reads_every_chunk(self) -> None:
        import hashlib

        payload = os.urandom(3 * 1024 * 1024)  # more than one read() chunk
        movie = self.root / "big.mkv"
        movie.write_bytes(payload)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), fsio.sha256_file(movie))


# ---------------------------------------------------------------------------
# text.py
# ---------------------------------------------------------------------------


class TextLayoutEdgeTests(unittest.TestCase):
    """The clipping rules are what keep a report inside its width budget."""

    def test_a_nonpositive_width_yields_nothing(self) -> None:
        self.assertEqual("", text.clip_text("anything", 0))
        self.assertEqual("", text.clip_text("anything", -5))

    def test_a_width_at_or_below_the_marker_is_a_hard_cut(self) -> None:
        self.assertEqual("ab", text.clip_text("abcdef", 2, ellipsis="..."))

    def test_wrapping_preserves_explicit_blank_lines(self) -> None:
        self.assertEqual(["one", "", "two"], text.wrap_text("one\n\ntwo", 40))

    def test_path_wrapping_keeps_separators_and_blank_lines(self) -> None:
        long_path = "/media/movies/" + "a-very-long-movie-name/" * 3
        wrapped = text.wrap_path_text(long_path, 40)
        self.assertTrue(all(len(line) <= 40 for line in wrapped))
        multi = text.wrap_path_text("/media/movies/" + "x" * 60 + "\n\n/more/movies", 40)
        self.assertIn("", multi)
        # A component that fits is emitted with its trailing spaces trimmed.
        self.assertEqual(["Movies/Alpha (2001)  "], text.wrap_path_text("Movies/Alpha (2001)  ", 80))
        self.assertEqual(["short"], text.wrap_path_text("short", 80))


# ---------------------------------------------------------------------------
# smoke.py
# ---------------------------------------------------------------------------


class FieldSmokeTestResultTests(unittest.TestCase):
    """A smoke test that crashes has still answered the question."""

    def _run(self, checks: list[tuple[str, object]]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = smoke.run_field_smoke_test("probe.py", checks)  # type: ignore[arg-type]
        return rc, out.getvalue()

    def test_every_check_passing_exits_zero(self) -> None:
        rc, output = self._run([("fine", lambda: True)])
        self.assertEqual(0, rc)
        self.assertIn("SELF-TEST PASSED", output)

    def test_a_crashing_check_is_a_named_failure_not_a_traceback(self) -> None:
        def explode() -> bool:
            raise RuntimeError("no such binary")

        rc, output = self._run([("fine", lambda: True), ("boom", explode)])
        self.assertEqual(1, rc)
        self.assertIn("FAIL", output)
        self.assertIn("RuntimeError: no such binary", output)
        self.assertIn("boom", output)


# ---------------------------------------------------------------------------
# console.py
# ---------------------------------------------------------------------------


class ConsoleFallbackTests(unittest.TestCase):
    """A console that cannot be reconfigured or cannot take VT mode degrades;
    it never takes the run down with it."""

    def test_a_stream_that_refuses_reconfiguration_is_left_alone(self) -> None:
        class Stubborn:
            def reconfigure(self, **kwargs: object) -> None:
                raise ValueError("detached")

        with mock.patch.object(console_mod.sys, "stdout", Stubborn()), \
                mock.patch.object(console_mod.sys, "stderr", Stubborn()):
            console_mod.enable_utf8_stdio()

    def test_windows_without_ctypes_support_gets_no_colour(self) -> None:
        with mock.patch.object(console_mod.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": types.SimpleNamespace()}):
            self.assertFalse(console_mod.enable_windows_vt())

    def test_a_console_without_a_console_mode_gets_no_colour(self) -> None:
        class Kernel32:
            def GetStdHandle(self, which: int) -> int:
                return 7

            def GetConsoleMode(self, handle: int, mode: object) -> bool:
                return False

        fake = types.SimpleNamespace(
            windll=types.SimpleNamespace(kernel32=Kernel32()),
            c_uint32=lambda: types.SimpleNamespace(value=0),
            byref=lambda obj: obj,
        )
        with mock.patch.object(console_mod.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": fake}):
            self.assertFalse(console_mod.enable_windows_vt())


# ---------------------------------------------------------------------------
# state.py
# ---------------------------------------------------------------------------


class _FailingSqlite:
    """A connection proxy that raises ``sqlite3.Error`` for chosen statements.

    Every other call is forwarded untouched, so the failure stays inside the
    one statement under test.
    """

    def __init__(self, wrapped: sqlite3.Connection, *, fail_startswith: tuple[str, ...] = (),
                 fail_exact: tuple[str, ...] = (),
                 fail_many_startswith: tuple[str, ...] = ()) -> None:
        self._wrapped = wrapped
        self._fail_startswith = tuple(s.upper() for s in fail_startswith)
        self._fail_exact = tuple(s.upper() for s in fail_exact)
        self._fail_many_startswith = tuple(s.upper() for s in fail_many_startswith)
        self.seen: list[str] = []
        self.executed = 0

    def _refuses(self, sql: str, patterns: tuple[str, ...]) -> bool:
        upper = sql.strip().upper()
        return any(upper.startswith(pattern) for pattern in patterns)

    def execute(self, sql: str, *args: object, **kwargs: object) -> object:
        self.seen.append(sql.strip().upper())
        if self._refuses(sql, self._fail_startswith) or sql.strip().upper() in self._fail_exact:
            raise sqlite3.OperationalError("injected failure")
        self.executed += 1
        return self._wrapped.execute(sql, *args, **kwargs)

    def executemany(self, sql: str, *args: object, **kwargs: object) -> object:
        if self._refuses(sql, self._fail_many_startswith):
            raise sqlite3.OperationalError("injected failure")
        return self._wrapped.executemany(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


class StateStoreDegradationTests(unittest.TestCase):
    """The state cache is derived: it may lose data, but it may never hand back
    an answer about bytes that are not the ones on disk, and a cache write may
    never fail a run."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="core_state_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db = self.root / "state.db"
        self.movie = self.root / "lib" / "Alpha (2001)" / "Alpha (2001).mkv"
        self.movie.parent.mkdir(parents=True)
        self.movie.write_bytes(b"x" * 1024)
        self.store = state.StateStore(self.db, tool="tests")
        self.addCleanup(self.store.close)

    def _proxy(self, *, fail_startswith: tuple[str, ...] = (),
               fail_exact: tuple[str, ...] = (),
               fail_many_startswith: tuple[str, ...] = ()) -> _FailingSqlite:
        """Swap the live connection for one that refuses selected statements.

        ``sqlite3.Connection`` attributes are read-only, so the failure is
        injected by replacing the store's handle rather than patching a method
        on the C object.
        """
        proxy = _FailingSqlite(self.store._db, fail_startswith=fail_startswith,
                               fail_exact=fail_exact, fail_many_startswith=fail_many_startswith)
        self.store._db = proxy  # type: ignore[assignment]
        return proxy

    def test_closing_twice_is_harmless(self) -> None:
        self.store.close()
        self.store.close()

    def test_a_begin_that_fails_degrades_to_autocommit_rather_than_dropping_the_write(self) -> None:
        proxy = self._proxy(fail_startswith=("BEGIN",))
        self.store.record(self.movie, state.KIND_REMUX, "cleaned", "kept 1 track")
        self.assertGreater(proxy.executed, 0)
        self.assertEqual("cleaned", self.store.verdicts(state.KIND_REMUX)[(state.path_norm(self.movie), state.KIND_REMUX)].verdict)

    def test_a_commit_that_fails_is_rolled_back_so_the_next_write_can_start(self) -> None:
        proxy = self._proxy(fail_exact=("COMMIT",))
        self.store.record(self.movie, state.KIND_REMUX, "cleaned")
        self.assertIn("ROLLBACK", proxy.seen,
                      "a failed COMMIT leaves a transaction nobody else can open")

    def test_a_body_error_rolls_back_and_propagates(self) -> None:
        with self.assertRaises(RuntimeError), self.store._write() as db:
            db.execute("INSERT INTO event (ts, tool, path_key, kind, detail) VALUES (?,?,?,?,?)",
                       (state._now(), "tests", "", "x", ""))
            raise RuntimeError("body exploded")
        # The connection is clean again: the next write works.
        self.store.note("after")

    def test_an_unstattable_movie_is_recorded_as_unknown_not_as_a_size(self) -> None:
        """A row with no stamp is what makes a verdict read as stale, so an
        unreadable movie degrades to 'unknown' rather than a wrong answer."""
        col = self.root / "lib" / "Beta (2002)" / "Beta (2002).mkv"
        col.parent.mkdir(parents=True)
        col.write_bytes(b"y" * 10)
        snapshot = col.stat()
        col.unlink()
        key = self.store.see_movie(col)
        self.assertIsNotNone(key)
        stored = self.store.movies()[state.path_norm(col)]
        self.assertIsNone(stored.size, "a movie that cannot be stat()ed is recorded unstamped")
        self.assertEqual(10, snapshot.st_size)

    def test_pruning_a_cache_that_has_nothing_to_prune_is_a_no_op(self) -> None:
        self.assertEqual(0, self.store.forget_missing({state.path_norm(self.movie)}))

    def test_pruning_survives_a_delete_that_cannot_run(self) -> None:
        self.store.see_movie(self.movie)
        self._proxy(fail_many_startswith=("DELETE FROM MOVIE",))
        gone = self.store.forget_missing(set())
        self.assertEqual(1, gone)

    def test_a_record_for_a_vanished_file_is_kept_with_an_unknown_stamp(self) -> None:
        gone = self.root / "lib" / "Gamma (2003).mkv"
        self.store.record(gone, state.KIND_SUBTITLE, "missing")
        verdict = self.store.verdicts(state.KIND_SUBTITLE)[(state.path_norm(gone), state.KIND_SUBTITLE)]
        self.assertEqual("missing", verdict.verdict)
        self.assertIsNone(verdict.size, "an unstamped verdict reads as stale, never as current")

    def test_a_batch_record_that_cannot_run_does_not_raise(self) -> None:
        self._proxy(fail_many_startswith=("INSERT INTO VERDICT",))
        self.assertEqual(1, self.store.record_many([(self.movie, state.KIND_REMUX, "cleaned", "")]))
        self.assertEqual({}, self.store.verdicts(state.KIND_REMUX),
                         "a batch that could not run writes nothing, and says nothing")

    def test_a_batch_of_seen_movies_survives_a_locked_database(self) -> None:
        self._proxy(fail_many_startswith=("INSERT INTO MOVIE",))
        keys = self.store.see_movies([(self.movie, None)])
        self.assertEqual([state.path_norm(self.movie)], keys)

    def test_a_batch_of_seen_movies_writes_one_row_per_movie(self) -> None:
        second = self.root / "lib" / "Beta (2002).mkv"
        keys = self.store.see_movies([(self.movie, None), (second, self.root)])
        self.assertEqual(2, len(keys))
        self.assertEqual(2, len(self.store.movies()))

    def test_a_quota_cannot_be_overspent(self) -> None:
        self.assertTrue(self.store.reserve_quota("opensubtitles", "2026-10-04", 2))
        self.assertTrue(self.store.reserve_quota("opensubtitles", "2026-10-04", 2, count=1))
        self.assertFalse(self.store.reserve_quota("opensubtitles", "2026-10-04", 2))
        self.assertEqual(2, self.store.quota_used("opensubtitles", "2026-10-04"))

    def test_a_refused_quota_reservation_does_not_consume_the_budget(self) -> None:
        self.store.reserve_quota("opensubtitles", "2026-10-04", 1)
        self.assertFalse(self.store.reserve_quota("opensubtitles", "2026-10-04", 1))
        self.assertEqual(1, self.store.quota_used("opensubtitles", "2026-10-04"))

    def test_a_quota_read_that_cannot_run_reads_as_zero(self) -> None:
        self._proxy(fail_startswith=("SELECT USED FROM QUOTA",))
        self.assertEqual(0, self.store.quota_used("opensubtitles", "2026-10-04"))

    def test_a_quota_write_that_cannot_run_is_refused_not_reported_granted(self) -> None:
        self._proxy(fail_startswith=("INSERT INTO QUOTA",))
        self.assertFalse(self.store.reserve_quota("opensubtitles", "2026-10-04", 5))

    def test_an_event_that_cannot_be_written_is_not_an_error(self) -> None:
        self._proxy(fail_startswith=("INSERT INTO EVENT",))
        self.store.note("something happened", movie=self.movie)

    def test_event_pruning_survives_a_locked_database(self) -> None:
        self._proxy(fail_startswith=("DELETE FROM EVENT",))
        self.store.prune_events(keep=1)


# ---------------------------------------------------------------------------
# subtitles.py
# ---------------------------------------------------------------------------


class SidecarValidationTests(unittest.TestCase):
    """The subtitle contract: only a real, readable, cue-bearing regular file
    may count as covering a movie. Everything else is reported as a refusal
    with a reason a report line can carry."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="core_subs_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def test_a_path_that_cannot_be_stat_ed_is_refused(self) -> None:
        ok, reason = subtitles.validate_srt_sidecar(self.root / "gone.srt")
        self.assertFalse(ok)
        self.assertIn("could not stat subtitle", reason)

    def test_a_symlink_is_refused_even_when_it_points_at_a_valid_file(self) -> None:
        real = self.root / "real.srt"
        real.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        link = self.root / "link.srt"
        try:
            link.symlink_to(real)
        except OSError:
            self.skipTest("this filesystem cannot make symlinks")
        ok, reason = subtitles.validate_srt_sidecar(link)
        self.assertFalse(ok)
        self.assertIn("not a regular file", reason)

    def test_an_oversized_file_is_refused_by_the_safety_limit(self) -> None:
        huge = self.root / "huge.srt"
        with mock.patch.object(Path, "stat", lambda self, **kw: types.SimpleNamespace(
                st_mode=0o100644, st_size=subtitles.EXTERNAL_SRT_MAX_BYTES + 1)):
            ok, reason = subtitles.validate_srt_sidecar(huge)
        self.assertFalse(ok)
        self.assertIn("safety limit", reason)

    def test_a_file_that_cannot_be_read_is_refused(self) -> None:
        unreadable = self.root / "locked.srt"
        unreadable.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        with mock.patch.object(Path, "read_bytes", side_effect=OSError(errno.EACCES, "denied")):
            ok, reason = subtitles.validate_srt_sidecar(unreadable)
        self.assertFalse(ok)
        self.assertIn("could not read subtitle", reason)

    def test_a_binary_file_is_refused_as_an_unsupported_encoding(self) -> None:
        binary = self.root / "binary.srt"
        binary.write_bytes(b"\xff\xfe\x00\x01 not text at all")
        ok, reason = subtitles.validate_srt_sidecar(binary)
        self.assertFalse(ok)
        self.assertTrue("unsupported text encoding" in reason or "no valid SRT cue" in reason)


class LegacySidecarPromotionTests(unittest.TestCase):
    """Promoting ``.en.srt`` to ``.eng.srt`` must never overwrite work that
    arrived concurrently: the publish is create-if-absent, and a refusal is
    reported instead of guessed at."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="core_promote_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.movie = self.root / "Film (2000).mkv"
        self.movie.write_bytes(b"movie")
        self.canonical = self.root / "Film (2000).eng.srt"
        self.legacy = self.root / "Film (2000).en.srt"

    def _write_legacy(self, body: str = "1\n00:00:01,000 --> 00:00:02,000\nHi.\n") -> None:
        self.legacy.write_text(body, encoding="utf-8")

    def test_an_existing_valid_canonical_sidecar_wins(self) -> None:
        self.canonical.write_text("1\n00:00:01,000 --> 00:00:02,000\nExisting.\n", encoding="utf-8")
        path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual(self.canonical, path)
        self.assertEqual("", reason)

    def test_a_canonical_path_occupied_by_a_directory_is_refused(self) -> None:
        self.canonical.mkdir()
        path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(path)
        self.assertIn("occupied", reason)

    def test_an_inspectable_canonical_path_that_raises_is_refused(self) -> None:
        def boom(self: Path) -> bool:
            raise OSError(errno.EACCES, "denied")

        with mock.patch.object(Path, "exists", boom):
            path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(path)
        self.assertIn("could not inspect canonical sidecar", reason)

    def test_an_inspectable_legacy_path_that_raises_is_refused(self) -> None:
        real_exists = Path.exists

        def sometimes(self: Path) -> bool:
            if self.name == "Film (2000).en.srt":
                raise OSError(errno.EACCES, "denied")
            return real_exists(self)

        with mock.patch.object(Path, "exists", sometimes):
            path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(path)
        self.assertIn("could not inspect legacy sidecar", reason)

    def test_a_legacy_that_is_a_symlink_is_not_promoted(self) -> None:
        real = self.root / "elsewhere.srt"
        real.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        try:
            self.legacy.symlink_to(real)
        except OSError:
            self.skipTest("this filesystem cannot make symlinks")
        path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(path)
        self.assertIn("absent", reason)

    def test_a_legacy_that_arrives_after_the_check_loses_the_race_safely(self) -> None:
        self._write_legacy()

        def publishing_link(src: object, dst: object, **kwargs: object) -> None:
            Path(dst).write_text("someone else's sidecar\n", encoding="utf-8")
            raise FileExistsError(errno.EEXIST, "exists")

        with mock.patch("os.link", publishing_link):
            path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(path)
        self.assertIn("appeared concurrently", reason)
        self.assertEqual("someone else's sidecar\n", self.canonical.read_text(encoding="utf-8"))
        self.assertTrue(self.legacy.exists(), "the legacy file is left for a human to look at")

    def test_a_filesystem_without_hardlinks_falls_back_to_an_atomic_rename(self) -> None:
        """FAT32 and some SMB shares cannot link at all; the guarantee must be
        as good as the filesystem allows rather than silently absent."""
        self._write_legacy()
        with mock.patch("os.link", side_effect=OSError(errno.EPERM, "operation not permitted")):
            path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual("", reason)
        self.assertTrue(self.canonical.exists())
        self.assertFalse(self.legacy.exists())

    def test_a_real_link_error_that_is_not_missing_support_is_reported(self) -> None:
        self._write_legacy()
        with mock.patch("os.link", side_effect=OSError(errno.ENOSPC, "no space")):
            path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(path)
        self.assertIn("could not promote", reason)

    def test_a_rename_fallback_that_fails_reports_the_reason(self) -> None:
        self._write_legacy()
        with mock.patch("os.link", side_effect=OSError(errno.EPERM, "not supported")), \
                mock.patch("os.replace", side_effect=OSError(errno.EACCES, "denied")):
            path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(path)
        self.assertIn("could not rename", reason)

    def test_a_rename_fallback_never_clobbers_a_sidecar_that_appeared(self) -> None:
        self._write_legacy()

        def late_arrival(src: object, dst: object, **kwargs: object) -> None:
            Path(dst).write_text("arrived meanwhile\n", encoding="utf-8")
            raise OSError(errno.EPERM, "not supported")

        with mock.patch("os.link", late_arrival):
            path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(path)
        self.assertIn("occupied", reason)
        self.assertEqual("arrived meanwhile\n", self.canonical.read_text(encoding="utf-8"))

    def test_a_legacy_that_cannot_be_unlinked_is_left_as_a_harmless_duplicate(self) -> None:
        self._write_legacy()
        with mock.patch.object(Path, "unlink", side_effect=OSError(errno.EPERM, "no")):
            path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual(self.canonical, path)
        self.assertEqual("", reason)

    def test_a_valid_legacy_sidecar_is_promoted_without_changing_its_bytes(self) -> None:
        self._write_legacy()
        path, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual(self.canonical, path)
        self.assertEqual("", reason)
        self.assertFalse(self.legacy.exists())
        self.assertEqual("1\n00:00:01,000 --> 00:00:02,000\nHi.\n",
                         self.canonical.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# report.py
# ---------------------------------------------------------------------------


class ReportLayoutEdgeTests(unittest.TestCase):
    """Rendering must never distort the number a reader is looking at."""

    def test_a_meta_label_without_a_value_still_gets_a_row(self) -> None:
        rendered = (report.Report("Cover", width=64)
                    .metas([("Library", ""), ("Mode", "dry run")])
                    .render())
        self.assertIn("Library", rendered)

    def test_a_title_line_without_a_right_hand_column_is_clipped_to_the_span(self) -> None:
        rendered = report.Report("Cover", width=64).title_line("x" * 200).render()
        self.assertTrue(all(len(line) <= 64 for line in rendered.splitlines()))

    def test_a_right_hand_column_that_would_collide_is_clipped_as_one_string(self) -> None:
        rendered = (report.Report("Cover", width=64)
                    .title_line("a title that is far too long for the space", right="12 movies")
                    .render())
        self.assertTrue(all(len(line) <= 64 for line in rendered.splitlines()))
        self.assertIn("12 movies", rendered)

    def test_a_right_hand_column_with_room_is_pushed_to_the_margin(self) -> None:
        rendered = report.Report("Cover", width=72).title_line("Items", right="3").render()
        self.assertTrue(any(line.rstrip().endswith(" 3") for line in rendered.splitlines()))

    def test_a_section_whose_header_does_not_fit_degrades_to_one_line(self) -> None:
        rendered = (report.Report("Cover", width=64)
                    .section("A title so long the two rules would collide with the tally",
                             count=3)
                    .render())
        self.assertIn("A title so long", rendered)

    def test_a_subsection_whose_header_does_not_fit_degrades_to_one_line(self) -> None:
        rendered = (report.Report("Cover", width=64)
                    .subsection("Another title wide enough that the fill would go negative",
                                count=12)
                    .render())
        self.assertIn("Another title", rendered)

    def test_an_entry_marked_with_a_tag_uses_it_instead_of_a_number(self) -> None:
        rendered = (report.Report("Cover", width=80)
                    .entry("Film (2000).mkv", marker="SKIP")
                    .render())
        self.assertIn("SKIP", rendered)

    def test_a_table_with_no_columns_renders_nothing(self) -> None:
        rendered = report.Report("Cover", width=80).table([], []).render()
        self.assertNotIn("Headers", rendered)

    def test_a_table_that_cannot_shrink_further_still_renders(self) -> None:
        headers = ["a", "b", "c"]
        rows = [["q" * 80, "r" * 80, "s" * 80]]
        rendered = report.Report("Cover", width=64).table(headers, rows).render()
        self.assertTrue(any("a" in line for line in rendered.splitlines()))


# ---------------------------------------------------------------------------
# toolchain.py
# ---------------------------------------------------------------------------


class SiblingToolDegradationTests(unittest.TestCase):
    """Every prerequisite answer comes from the tool that runs the binary. If a
    sibling cannot even be imported that is not the caller's problem: the
    answer degrades to the plain PATH lookup instead of taking the run down."""

    def test_an_unimportable_sibling_degrades_to_path_for_every_check(self) -> None:
        from organizekit.core import toolchain

        boom = types.ModuleType("subtitle_extractor")

        def explode(*args: object, **kwargs: object) -> None:
            raise ImportError("no sibling here")

        boom.find_mkvtoolnix_binary = explode  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"subtitle_extractor": boom}), \
                mock.patch.object(toolchain.shutil, "which", return_value=None):
            self.assertFalse(toolchain.mkvtoolnix_installed())
            self.assertFalse(toolchain.mkvextract_installed())
        with mock.patch.dict(sys.modules, {"subtitle_extractor": boom}), \
                mock.patch.object(toolchain.shutil, "which", return_value="/usr/bin/x"):
            self.assertTrue(toolchain.mkvtoolnix_installed())
            self.assertTrue(toolchain.mkvextract_installed())

    def test_the_cleaner_and_inspector_degrade_to_path_too(self) -> None:
        from organizekit.core import toolchain

        def explode(*args: object, **kwargs: object) -> None:
            raise ImportError("no sibling here")

        cleaner = types.ModuleType("mkv_track_cleaner")
        cleaner.resolve_mkvmerge_path = explode  # type: ignore[attr-defined]
        inspector = types.ModuleType("bitdepth")
        inspector.find_ffprobe = explode  # type: ignore[attr-defined]
        audio = types.ModuleType("audio_standardizer")
        audio.find_ffprobe = explode  # type: ignore[attr-defined]
        audio.find_ffmpeg = explode  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"mkv_track_cleaner": cleaner, "bitdepth": inspector,
                                           "audio_standardizer": audio}), \
                mock.patch.object(toolchain.shutil, "which", return_value=None):
            self.assertFalse(toolchain.mkvmerge_installed())
            self.assertFalse(toolchain.ffprobe_installed())
            self.assertFalse(toolchain.ffmpeg_installed())
        with mock.patch.dict(sys.modules, {"mkv_track_cleaner": cleaner, "bitdepth": inspector,
                                           "audio_standardizer": audio}), \
                mock.patch.object(toolchain.shutil, "which", return_value="/usr/bin/x"):
            self.assertTrue(toolchain.mkvmerge_installed())
            self.assertTrue(toolchain.ffprobe_installed())
            self.assertTrue(toolchain.ffmpeg_installed())

    def test_a_prerequisite_probe_that_crashes_blocks_the_step(self) -> None:
        """A probe that raises has answered 'cannot run this step' - and the
        message has to say why rather than being empty."""
        from organizekit.core import toolchain

        step = toolchain.STEPS["cleaner"]

        def explode() -> bool:
            raise RuntimeError("probe exploded")

        with mock.patch.dict(toolchain.PREREQUISITES, {"cleaner": (explode, "cleaner missing")}):
            self.assertEqual("cleaner missing", toolchain.prerequisite_issue(step))

    def test_a_name_that_cannot_be_imported_is_not_available_in_the_archive(self) -> None:
        from organizekit.core import toolchain

        with mock.patch.object(toolchain, "zipapp_path", return_value=Path("/tmp/x.pyz")), \
                mock.patch.object(toolchain.importlib.util, "find_spec",
                                  side_effect=ValueError("bad module name")):
            self.assertFalse(toolchain.tool_is_available("not-a-module.pp"))


# ---------------------------------------------------------------------------
# probecache.py
# ---------------------------------------------------------------------------


class ProbeCacheDegradationTests(unittest.TestCase):
    """A cache that is corrupt, unreadable or unwritable is a cache miss. It is
    never a failed run and never a wrong payload."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="core_probe_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def test_a_json_cache_holding_something_else_is_a_miss(self) -> None:
        path = self.root / "cache.json"
        path.write_text("[1, 2, 3]\n", encoding="utf-8")
        self.assertEqual({}, probecache._JsonBackend(path, "tool", 1).load())
        path.write_text('{"schema": 2, "tool": "tool", "entries": {}}\n', encoding="utf-8")
        self.assertEqual({}, probecache._JsonBackend(path, "tool", 1).load())
        path.write_text('{"schema": 1, "tool": "other", "entries": {}}\n', encoding="utf-8")
        self.assertEqual({}, probecache._JsonBackend(path, "tool", 1).load())
        path.write_text('{"schema": 1, "tool": "tool", "entries": "nope"}\n', encoding="utf-8")
        self.assertEqual({}, probecache._JsonBackend(path, "tool", 1).load())

    def test_non_dict_entries_are_dropped_not_carried(self) -> None:
        path = self.root / "cache.json"
        path.write_text('{"schema": 1, "tool": "tool", "entries": {"a": 3, "b": {"x": 1}}}\n',
                        encoding="utf-8")
        self.assertEqual({"b": {"x": 1}}, probecache._JsonBackend(path, "tool", 1).load())

    def test_a_json_cache_that_cannot_be_written_is_not_an_error(self) -> None:
        path = self.root / "cache.json"
        with mock.patch("os.replace", side_effect=OSError(errno.ENOSPC, "full")):
            probecache._JsonBackend(path, "tool", 1).save({"a": {"x": 1}})
        self.assertFalse(path.exists())

    def test_a_sqlite_cache_that_is_not_a_database_is_a_miss(self) -> None:
        path = self.root / "state.db"
        path.write_bytes(b"this is not sqlite")
        self.assertEqual({}, probecache._SqliteBackend(path, "tool").load())

    def test_a_missing_sqlite_cache_is_not_created_by_a_read(self) -> None:
        path = self.root / "state.db"
        self.assertEqual({}, probecache._SqliteBackend(path, "tool").load())
        self.assertFalse(path.exists(), "a read must not bring a database into being")

    def test_a_payload_that_cannot_be_decoded_is_a_miss_for_that_entry_only(self) -> None:
        path = self.root / "state.db"
        backend = probecache._SqliteBackend(path, "tool")
        backend.save({"good": {"size": 1, "mtime_ns": 2, "payload": {"ok": True}},
                      "nodict": {"size": 1, "mtime_ns": 2, "payload": "not a mapping"},
                      "bad": {"size": 1, "mtime_ns": 2, "payload": {"ok": True}}})
        db = sqlite3.connect(str(path))
        with contextlib.closing(db):
            db.execute("UPDATE probe SET payload=? WHERE path_key=?", ("{not json", "bad"))
            db.commit()
        loaded = backend.load()
        self.assertEqual({"ok": True}, loaded["good"]["payload"])
        self.assertNotIn("bad", loaded, "an undecodable payload is a miss, not a crash")
        self.assertNotIn("nodict", loaded)

    def test_a_row_read_that_fails_is_a_miss(self) -> None:
        path = self.root / "state.db"
        backend = probecache._SqliteBackend(path, "tool")
        backend.save({"a": {"size": 1, "mtime_ns": 2, "payload": {"ok": True}}})
        real_connect = backend._connect
        with mock.patch.object(probecache._SqliteBackend, "_connect",
                               side_effect=sqlite3.OperationalError("no such table")):
            self.assertEqual({}, backend.load())
        self.assertTrue(real_connect)

    def test_a_save_that_times_out_is_rolled_back_and_swallowed(self) -> None:
        path = self.root / "state.db"
        backend = probecache._SqliteBackend(path, "tool")
        with mock.patch.object(probecache._SqliteBackend, "_connect",
                               side_effect=sqlite3.OperationalError("database is locked")):
            backend.save({"a": {"size": 1, "mtime_ns": 2, "payload": {}}})

    def test_a_save_whose_insert_fails_rolls_back_without_raising(self) -> None:
        path = self.root / "state.db"
        backend = probecache._SqliteBackend(path, "tool")
        real_connect = probecache._SqliteBackend._connect

        class Failing:
            def __init__(self, wrapped: sqlite3.Connection) -> None:
                self._wrapped = wrapped

            def __getattr__(self, name: str) -> object:
                return getattr(self._wrapped, name)

            def executemany(self, *args: object, **kwargs: object) -> None:
                raise sqlite3.OperationalError("disk I/O error")

        def connect(self: object) -> object:
            return Failing(real_connect(self))  # type: ignore[arg-type]

        with mock.patch.object(probecache._SqliteBackend, "_connect", connect):
            backend.save({"a": {"size": 1, "mtime_ns": 2, "payload": {}}})


# ---------------------------------------------------------------------------
# playbackchain.py
# ---------------------------------------------------------------------------


class AudioSegmentClassificationTests(unittest.TestCase):
    def test_blank_text_classifies_as_nothing(self) -> None:
        self.assertIsNone(playbackchain._classify_audio_segment("   "))


if __name__ == "__main__":
    unittest.main()
