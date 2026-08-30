"""Tests for the pure helpers in ``subtitle_fetcher.py``."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import subtitle_fetcher as sf
from reporttext import scorecard, section


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
                capture_output=True, env=env, timeout=120,
                # The child pins its stdio to UTF-8 (its report is full of
                # box-drawing characters), so the parent must not decode with
                # the locale encoding - cp1252 on Windows turns those bytes
                # into a UnicodeDecodeError.
                encoding="utf-8", errors="replace",
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
            self.assertEqual(scorecard(text)["NEED A SUBTITLE"], 1)
            self.assertIn("ERRORS", text)

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


class ReportOrganizationTests(unittest.TestCase):
    """The report exists to answer two questions: what needs a subtitle, what has one.

    Before this, every movie was dumped into one flat list tagged with a status
    word, and the reader had to reconstruct the grouping themselves. These
    tests pin the grouping: covered movies and sidecar names in one place,
    every movie that still needs a subtitle in another, split by what to do.
    """

    def setUp(self) -> None:
        self.library = Path("/library")

    def video(self, name: str) -> Path:
        return self.library / name / f"{name}.mkv"

    def sidecar(self, name: str) -> Path:
        return self.library / name / f"{name}.eng.srt"

    def config(self, **overrides: object) -> sf.QueueConfig:
        base: dict[str, object] = {
            "library": self.library,
            "log_file": Path("/logs/subtitle_fetcher.log"),
            "report_file": Path("/logs/subtitle_fetcher_report.txt"),
            "daily_cap": 200,
        }
        base.update(overrides)
        return sf.QueueConfig(**base)  # type: ignore[arg-type]

    def summary(self, results: list[sf.JobResult], **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "utc_day": "2026-08-29",
            "daily_cap": 200,
            "download_requests_reserved": 0,
            "successful_downloads": 0,
            "quota_reached": False,
            "deferred_remaining": 0,
            "ledger_log": "/logs/subtitle_fetcher.log",
            "movies_discovered": len(results),
            "deferred_videos": [],
        }
        base.update(overrides)
        return base

    def report(self, results: list[sf.JobResult], summary: dict[str, object]) -> str:
        return sf.build_report(results, self.config(), summary)

    def test_covered_movies_are_listed_with_their_sidecar_name(self) -> None:
        results = [sf.JobResult(self.video("Dune (2021)"), "have", "validated exact .eng.srt",
                                self.sidecar("Dune (2021)"), reason=sf.REASON_COVERED)]
        text = self.report(results, self.summary(results))

        covered = section(text, "MOVIES THAT ALREADY HAVE AN EXTERNAL .eng.srt")
        self.assertIn("Dune (2021)", covered)
        self.assertIn("Dune (2021).eng.srt", covered)
        self.assertNotIn("Dune (2021)", section(text, "MOVIES THAT NEED A SUBTITLE"))
        self.assertEqual(scorecard(text)["Already have .eng.srt"], 1)
        self.assertEqual(scorecard(text)["NEED A SUBTITLE"], 0)

    def test_movies_needing_a_subtitle_are_grouped_by_the_fix(self) -> None:
        results = [
            sf.JobResult(self.video("Broken (2009)"), "review", "unusable sidecar",
                         reason=sf.REASON_SIDECAR_UNUSABLE),
            sf.JobResult(self.video("Heat (1995)"), "skip", "no usable English moviehash match",
                         reason=sf.REASON_NO_MATCH),
            sf.JobResult(self.video("Loose"), "skip", "noncanonical layout", reason=sf.REASON_LAYOUT),
        ]
        text = self.report(results, self.summary(results))
        needs = section(text, "MOVIES THAT NEED A SUBTITLE")

        for title in ("SIDECAR EXISTS BUT IS UNUSABLE", "LIBRARY LAYOUT MUST BE FIXED FIRST",
                      "NO MATCHING SUBTITLE ON OPENSUBTITLES"):
            self.assertIn(title, needs)
        for movie in ("Broken (2009)", "Heat (1995)", "Loose"):
            self.assertIn(movie, needs)
        self.assertEqual(scorecard(text)["NEED A SUBTITLE"], 3)

    def test_the_cheapest_fix_is_named_first(self) -> None:
        """A broken sidecar is a two-second fix; a provider miss is not."""
        results = [
            sf.JobResult(self.video("Heat (1995)"), "skip", "no match", reason=sf.REASON_NO_MATCH),
            sf.JobResult(self.video("Broken (2009)"), "review", "unusable", reason=sf.REASON_SIDECAR_UNUSABLE),
        ]
        text = self.report(results, self.summary(results))
        self.assertIn("Start here:", text)
        self.assertLess(text.index("SIDECAR EXISTS BUT IS UNUSABLE"),
                        text.index("NO MATCHING SUBTITLE ON OPENSUBTITLES"))

    def test_movies_cut_off_by_the_quota_are_named_not_just_counted(self) -> None:
        results: list[sf.JobResult] = []
        summary = self.summary(results, deferred_remaining=2,
                               deferred_videos=[self.video("Zodiac (2007)"), self.video("Prisoners (2013)")],
                               movies_discovered=2)
        text = self.report(results, summary)

        deferred = section(text, "MOVIES THAT NEED A SUBTITLE")
        self.assertIn("DEFERRED TO THE NEXT UTC DAY", deferred)
        self.assertIn("Zodiac (2007)", deferred)
        self.assertIn("Prisoners (2013)", deferred)
        self.assertEqual(scorecard(text)["NEED A SUBTITLE"], 2)
        self.assertEqual(scorecard(text)["Movies in the library"], 2)

    def test_downloaded_movies_get_their_own_section(self) -> None:
        results = [sf.JobResult(self.video("Oppenheimer (2023)"), "download", "method=hash",
                                self.sidecar("Oppenheimer (2023)"), reason=sf.REASON_DOWNLOADED)]
        text = self.report(results, self.summary(results, successful_downloads=1))

        downloaded = section(text, "DOWNLOADED DURING THIS RUN")
        self.assertIn("Oppenheimer (2023).eng.srt", downloaded)
        self.assertEqual(scorecard(text)["NEED A SUBTITLE"], 0)

    def test_empty_groups_are_not_rendered(self) -> None:
        results = [sf.JobResult(self.video("Dune (2021)"), "have", "validated exact .eng.srt",
                                self.sidecar("Dune (2021)"), reason=sf.REASON_COVERED)]
        text = self.report(results, self.summary(results))

        self.assertNotIn("ERRORS", text)
        self.assertNotIn("DEFERRED TO THE NEXT UTC DAY", text)
        self.assertIn("None. Every movie already has a validated external English subtitle.", text)

    def test_every_line_fits_the_report_width(self) -> None:
        """A report that overflows its own rules is not a report anybody reads."""
        long_name = "An Unreasonably Long Movie Title That Keeps Going (1999)"
        results = [
            sf.JobResult(self.video(long_name), "have", "validated exact .eng.srt",
                         self.sidecar(long_name), reason=sf.REASON_COVERED),
            sf.JobResult(self.video("Broken (2009)"), "review", "x" * 400, reason=sf.REASON_SIDECAR_UNUSABLE),
        ]
        for line in self.report(results, self.summary(results)).splitlines():
            self.assertLessEqual(len(line), sf.Report("").width, line)


class SubdlIntegrationTests(unittest.TestCase):
    def test_subdl_client_empty_key(self) -> None:
        client = sf.SubdlClient("")
        identity = sf.MovieIdentity(title="Inception", year=2010, normalized_title="inception")
        cands, urls = client.search_identity(identity)
        self.assertEqual(cands, [])
        self.assertEqual(urls, {})
