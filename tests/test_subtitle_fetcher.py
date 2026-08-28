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


class PerMovieFailureIsolationTests(unittest.TestCase):
    """One bad movie must never abort the rest of the library.

    The per-movie handler around the hash/search step caught only
    ``RuntimeError``, but ``moviehash()`` raises ``ValueError`` for a file below
    ``MIN_HASH_SIZE`` and ``decode_subtitle_bytes()`` raises it for a subtitle
    that decompresses past ``MAX_SUBTITLE_BYTES``. Either one escaped as an
    uncaught traceback that killed the whole run, so every remaining movie went
    unfetched.
    """

    def test_undersized_movie_is_recorded_not_fatal(self) -> None:
        """End to end: a 3-byte MKV yields a per-movie error and exit 0.

        ``--min-size 0`` lets the stub past the size gate so the hash is
        attempted. No network call happens: the hash fails before the client is
        used, which keeps this test hermetic.
        """
        import os
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            movie_dir = root / "lib" / "Tiny Movie (2020)"
            movie_dir.mkdir(parents=True)
            (movie_dir / "Tiny Movie (2020).mkv").write_bytes(b"mkv")
            report = root / "report.txt"
            env = dict(os.environ, OPENSUBTITLES_API_KEY="test-key-not-used")
            proc = subprocess.run(
                [sys.executable, "subtitle_fetcher.py", "--source", str(root / "lib"),
                 "--log", str(root / "fetch.log"), "--report", str(report), "--min-size", "0"],
                capture_output=True, text=True, env=env, timeout=120,
                cwd=Path(__file__).resolve().parent.parent,
            )

            # Exit 1 is correct here: the tool reports "there were errors".
            # The bug was that it got there by crashing instead of by recording
            # the failure, so the distinguishing assertions are the absence of a
            # traceback and the presence of a per-movie error in the report.
            self.assertNotIn("Traceback", proc.stderr, proc.stderr[-800:])
            self.assertEqual(proc.returncode, 1, proc.stdout[-800:])
            text = report.read_text(encoding="utf-8")
            self.assertIn("too small to hash", text)
            self.assertIn("Errors                : 1", text)

    def test_oversized_decompressed_subtitle_raises_value_error(self) -> None:
        """The provider payload case the download handler must also survive."""
        import gzip

        bomb = gzip.compress(b"x" * (sf.MAX_SUBTITLE_BYTES + 1))
        with self.assertRaises(ValueError):
            sf.decode_subtitle_bytes(bomb)

    def test_download_handler_catches_value_error(self) -> None:
        """Pin the fix: the download site handles ValueError, not just RuntimeError."""
        import inspect

        source = inspect.getsource(sf.queue_run)
        self.assertIn("except (RuntimeError, ValueError) as exc:", source)
        # Two sites were affected: the hash/search step and the download step.
        self.assertEqual(source.count("except (RuntimeError, ValueError) as exc:"), 2)


if __name__ == "__main__":
    unittest.main()
