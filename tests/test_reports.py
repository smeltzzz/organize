"""Every tool's report is rendered by the same shared layout.

The reports are the only thing a scheduled run leaves behind, and they used to
look nothing alike: five different separators, five different label paddings,
and no consistent place to look for "what needs my attention". These tests hold
the package-wide contract - one boxed header, one scorecard, one section style,
and nothing that overflows the page - so a new tool cannot drift away from it.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib
import io
import tempfile
import unittest
from pathlib import Path

import common
from reporttext import scorecard, section

bit10 = importlib.import_module("10bit")
import library_auditor as la
import mkv_track_cleaner as tc
import movie_standardizer as ms
import pipeline as pl
import subtitle_fetcher as sf

HEAVY = "\u2550"


class _SampleReports(unittest.TestCase):
    """Builds one non-empty report per tool, with no media and no binaries."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="reports_test_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    # -- individual tools ---------------------------------------------------
    def subtitle_report(self) -> str:
        library = self.tmp / "lib"
        video = library / "Dune (2021)" / "Dune (2021).mkv"
        sidecar = library / "Dune (2021)" / "Dune (2021).eng.srt"
        results = [
            sf.JobResult(video, "have", "validated exact .eng.srt", sidecar, reason=sf.REASON_COVERED),
            sf.JobResult(library / "Heat (1995)" / "Heat (1995).mkv", "skip",
                         "no usable English moviehash-matched human SRT", reason=sf.REASON_NO_MATCH),
        ]
        cfg = sf.QueueConfig(library=library, log_file=self.tmp / "fetch.log",
                             report_file=self.tmp / "fetch_report.txt")
        summary = {"utc_day": "2026-08-29", "daily_cap": 200, "download_requests_reserved": 0,
                   "successful_downloads": 0, "quota_reached": False, "deferred_remaining": 0,
                   "ledger_log": str(self.tmp / "fetch.log"), "movies_discovered": 2,
                   "deferred_videos": []}
        return sf.build_report(results, cfg, summary)

    def auditor_report(self) -> str:
        folders = [
            la.FolderAudit(self.tmp / "Covered (2010)", "CANONICAL_MKV",
                           [la.MovieFile("Covered (2010).mkv", ".mkv", 10)]),
            la.FolderAudit(self.tmp / "Bare (2008)", "MISSING_SIDECAR",
                           [la.MovieFile("Bare (2008).mkv", ".mkv", 10)], "no English .eng.srt sidecar"),
        ]
        audit = la.Audit(self.tmp, folders, elapsed_sec=0.01)
        return la.build_report(audit, la.Config(source_dir=self.tmp, report_file=self.tmp / "audit.txt"))

    def inspector_report(self) -> str:
        results = [
            bit10.ProbeResult(path="/movies/Old (2004)/Old (2004).mkv", status=bit10.STATUS_QUEUE,
                              category=bit10.CATEGORY_LABELS[bit10.STATUS_QUEUE],
                              info="H.264 yuv420p", size_bytes=1000, duration_sec=100, bit_depth=8),
            bit10.ProbeResult(path="/movies/HDR (2021)/HDR (2021).mkv", status=bit10.STATUS_SKIP_HDR,
                              category=bit10.CATEGORY_LABELS[bit10.STATUS_SKIP_HDR],
                              info="HEVC yuv420p10le", size_bytes=2000, duration_sec=100, bit_depth=10,
                              hdr=True, hdr_flavors=["HDR10"]),
        ]
        cfg = bit10.Config(source_dir=Path("/movies"), report_file=self.tmp / "10bit.txt")
        return bit10.build_report(results, cfg, 1.5)

    def cleaner_report(self) -> str:
        stats = {
            "start_time": datetime.datetime.now(), "total_scanned": 1,
            "cleaned": [{"name": "Heat (1995).mkv", "kept_audio": "Track 1: English",
                         "removed_audio_desc": ["Track 2: Commentary"], "kept_subs_count": 0,
                         "kept_subs_desc": [], "removed_subs_count": 0, "removed_subs_desc": []}],
            "already_clean": ["Dune (2021).mkv"], "skipped_no_english": [], "skipped_layout": [],
            "deferred_hardlinked": [], "errors": [], "remux_without_srt": [], "diagnostics": [],
            "total_space_saved_bytes": 0,
        }
        with contextlib.redirect_stdout(io.StringIO()):
            return tc.generate_and_save_report(stats, dry_run=True,
                                               report_file=str(self.tmp / "cleaner.txt"),
                                               log_file_path=None)

    def standardizer_report(self) -> str:
        saved = (ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS)
        ms.CFG = ms.Config(source_dir=self.tmp / "final", target_dir=self.tmp / "lib",
                           log_file=None, report_file=self.tmp / "std.txt")
        ms.RUN_SUMMARY = ms.RunSummary()
        ms.RUN_EVENTS = []
        try:
            ms.record_outcome("completed", "HARDLINK", src=self.tmp / "final" / "Heat.1995.mkv",
                              dest=self.tmp / "lib" / "Heat (1995)" / "Heat (1995).mkv",
                              reason="verified hardlink")
            ms.decline_source(self.tmp / "final" / "Small.1995.mkv", "smaller than the 300 MB minimum")
            return ms.build_report()
        finally:
            ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS = saved

    def pipeline_report(self) -> str:
        run = pl.Run(results=[pl.StepResult("fetcher", "Fetch subtitles", "ran", returncode=0, seconds=1.0)])
        run.elapsed = 1.0
        return pl.build_summary(run, pl.Config(library=self.tmp / "lib"))

    def all_reports(self) -> dict[str, str]:
        return {
            "subtitle_fetcher": self.subtitle_report(),
            "library_auditor": self.auditor_report(),
            "10bit": self.inspector_report(),
            "mkv_track_cleaner": self.cleaner_report(),
            "movie_standardizer": self.standardizer_report(),
            "pipeline": self.pipeline_report(),
        }


class SharedLayoutTests(_SampleReports):
    def test_every_report_is_boxed_and_fits_the_page(self) -> None:
        for name, text in self.all_reports().items():
            with self.subTest(tool=name):
                self.assertTrue(text.startswith("\u2554"), text.splitlines()[0])
                for line in text.splitlines():
                    self.assertLessEqual(len(line), common.REPORT_WIDTH, line)
                self.assertTrue(text.endswith("\n"))

    def test_every_report_opens_with_a_scorecard(self) -> None:
        """The counts are the first thing a reader should be able to scan."""
        for name, text in self.all_reports().items():
            with self.subTest(tool=name):
                self.assertTrue(scorecard(text), name)

    def test_every_report_has_a_titled_section(self) -> None:
        for name, text in self.all_reports().items():
            with self.subTest(tool=name):
                self.assertIn(HEAVY * 2 + " ", text, name)

    def test_no_report_line_carries_trailing_whitespace(self) -> None:
        for name, text in self.all_reports().items():
            with self.subTest(tool=name):
                for line in text.splitlines():
                    self.assertEqual(line, line.rstrip(), line)


class SubtitleReportContentTests(_SampleReports):
    """The fetcher report must answer both questions the operator asks."""

    def test_covered_and_needing_movies_are_in_separate_sections(self) -> None:
        text = self.subtitle_report()
        needs = section(text, "MOVIES THAT NEED A SUBTITLE")
        covered = section(text, "MOVIES THAT ALREADY HAVE AN EXTERNAL .eng.srt")

        self.assertIn("Heat (1995)", needs)
        self.assertNotIn("Dune (2021)", needs)
        self.assertIn("Dune (2021).eng.srt", covered)
        self.assertNotIn("Heat (1995)", covered)
        self.assertEqual(scorecard(text)["Already have .eng.srt"], 1)
        self.assertEqual(scorecard(text)["NEED A SUBTITLE"], 1)


class InspectorReportOrderTests(_SampleReports):
    """The re-encode queue is the point of the inspector, so it leads."""

    def test_queue_precedes_the_do_not_touch_groups(self) -> None:
        text = self.inspector_report()
        self.assertLess(text.index("QUEUE FOR HANDBRAKE"), text.index("NATIVE HDR (KEEP"))
        self.assertLess(text.index("NATIVE HDR (KEEP"), text.index("HIGH BIT-DEPTH SDR (SKIP"))
        self.assertIn("Old (2004).mkv", section(text, "QUEUE FOR HANDBRAKE (8-BIT SDR)"))


class StandardizerReportContentTests(_SampleReports):
    def test_declined_items_lead_the_report(self) -> None:
        text = self.standardizer_report()
        self.assertLess(text.index("ITEMS LEFT IN SOURCE"), text.index("ORGANIZED INTO THE LIBRARY"))
        self.assertIn("Small.1995.mkv", section(text, "ITEMS LEFT IN SOURCE"))


class HostileConsoleEncodingTests(unittest.TestCase):
    """A console that cannot encode box-drawing must not abort a run.

    CI caught this on Windows and macOS: the reports are UTF-8, but a captured
    child stream is decoded with the *locale* encoding (cp1252 on Windows, and
    ASCII on a runner with no locale set), which turned the box-drawing bytes
    into a ``UnicodeDecodeError`` in the parent and a ``UnicodeEncodeError`` in
    the child.  The contract now is that every tool pins its own stdio to UTF-8
    with ``errors="replace"``, and every caller that captures a child decodes it
    as UTF-8 - so the console may degrade, but the run and the report file do
    not.
    """

    def test_fetcher_survives_an_ascii_console_and_keeps_the_file_utf8(self) -> None:
        import os
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            movie = root / "lib" / "Covered Movie (2020)"
            movie.mkdir(parents=True)
            (movie / "Covered Movie (2020).mkv").write_bytes(b"x")
            (movie / "Covered Movie (2020).eng.srt").write_text(
                "1\n00:00:00,000 --> 00:00:04,000\nHi.\n", encoding="utf-8")
            report = root / "report.txt"
            env = dict(os.environ, OPENSUBTITLES_API_KEY="test-key-not-used",
                       PYTHONIOENCODING="ascii")
            proc = subprocess.run(
                [sys.executable, "subtitle_fetcher.py", "--source", str(root / "lib"),
                 "--log", str(root / "fetch.log"), "--report", str(report), "--min-size", "0"],
                capture_output=True, encoding="utf-8", errors="replace", env=env,
                timeout=120, cwd=Path(__file__).resolve().parent.parent,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout[-800:] + proc.stderr[-800:])
            self.assertNotIn("Traceback", proc.stderr, proc.stderr[-800:])
            # The file is written UTF-8 regardless of what the console could show.
            text = report.read_bytes().decode("utf-8")
            self.assertIn(HEAVY * 2, text)
            self.assertIn("Covered Movie (2020).eng.srt", text)


if __name__ == "__main__":
    unittest.main()
