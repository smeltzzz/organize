"""Tests for the pure helpers in ``subtitle_fetcher.py``."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import subtitle_fetcher as sf


class MovieHashTests(unittest.TestCase):
    def test_moviehash_of_large_file(self) -> None:
        # OpenSubtitles OSHash requires >= HASH_CHUNK * 2 bytes.
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(bytes(i & 0xFF for i in range(sf.MIN_HASH_SIZE)))
            path = Path(fh.name)
        try:
            digest = sf.moviehash(path)
            self.assertEqual(len(digest), 16)
            self.assertTrue(all(c in "0123456789abcdef" for c in digest))
        finally:
            path.unlink(missing_ok=True)

    def test_moviehash_too_small_raises(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"tiny")
            path = Path(fh.name)
        try:
            with self.assertRaises(ValueError):
                sf.moviehash(path)
        finally:
            path.unlink(missing_ok=True)


class SnapshotTests(unittest.TestCase):
    def test_path_norm_equivalence(self) -> None:
        # Matches the standardizer/cleaner path normalisation contract exactly.
        self.assertEqual(sf.path_norm(Path("/tmp/./a/../a/x.mkv")), sf.path_norm("/tmp/a/x.mkv"))


if __name__ == "__main__":
    unittest.main()
