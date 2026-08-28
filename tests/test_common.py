"""Unit tests for the shared ``common`` infrastructure module.

Runs with only the standard library:

    python3 -m unittest discover -s tests
    # or, with pytest installed:
    pytest tests/test_common.py
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from common import (
    CoordinationLock,
    LockTimeoutError,
    STANDARDIZER_LOCK_NAME,
    atomic_write_text,
    path_is_within,
    path_norm,
    paths_equal,
)


class AtomicWriteTextTests(unittest.TestCase):
    def test_writes_and_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "report.txt"
            atomic_write_text(target, "hello")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")
            atomic_write_text(target, "world")
            self.assertEqual(target.read_text(encoding="utf-8"), "world")

    def test_creates_parents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "a" / "b" / "report.txt"
            atomic_write_text(target, "x")
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding="utf-8"), "x")

    def test_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "report.txt"
            atomic_write_text(target, "data")
            self.assertEqual([p.name for p in Path(td).iterdir()], ["report.txt"])


class PathHelpersTests(unittest.TestCase):
    def test_path_is_within(self) -> None:
        root = Path("/data/library")
        self.assertTrue(path_is_within(Path("/data/library/movie"), root))
        self.assertTrue(path_is_within(root, root))
        self.assertFalse(path_is_within(Path("/data/other/movie"), root))
        self.assertFalse(path_is_within(Path("/other"), root))

    def test_path_norm_and_equal(self) -> None:
        left = Path("/tmp/./media/../media/film.mkv")
        right = Path("/tmp/media/film.mkv")
        # normpath collapses the .. and duplicate segments.
        self.assertEqual(path_norm(left), path_norm(right))

    def test_paths_equal_samefile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a.mkv"
            f.write_bytes(b"x")
            link = Path(td) / "b.mkv"
            try:
                link.hardlink_to(f)
            except OSError:
                self.skipTest("hardlink not supported on this filesystem")
            self.assertTrue(paths_equal(f, link))


class CoordinationLockTests(unittest.TestCase):
    def test_acquire_release_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with CoordinationLock(Path(td) / "lib", timeout_seconds=10.0):
                pass  # no exception means the lock was taken and released cleanly

    def test_path_is_deterministic(self) -> None:
        # The lock path must be identical for identical normalized targets so the
        # standardizer, cleaner and subtitle fetcher contend on the same file.
        # normpath collapses "." and ".."; on Windows normcase also lower-cases,
        # which is exactly what makes the tools agree on a shared key.
        lock_a = CoordinationLock(Path("/Data/./Library"))
        lock_b = CoordinationLock("/Data/Library")
        self.assertEqual(lock_a.path, lock_b.path)
        self.assertIn(STANDARDIZER_LOCK_NAME, lock_a.path.name)

    def test_times_out_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with CoordinationLock(Path(td) / "lib", timeout_seconds=10.0) as held:
                blocker = CoordinationLock(Path(td) / "lib", timeout_seconds=0.1)
                # On POSIX, flock conflicts across two separate open file
                # descriptions even in the same process; on Windows msvcrt
                # byte-range locks do the same.
                try:
                    with self.assertRaises(LockTimeoutError):
                        blocker.acquire()
                finally:
                    blocker.release()
            _ = held


if __name__ == "__main__":
    unittest.main()
