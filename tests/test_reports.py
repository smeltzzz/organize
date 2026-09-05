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
import io
import tempfile
import unittest
from pathlib import Path

from reporttext import scorecard, section

import bitdepth as bit10
import library_auditor as la
import mkv_track_cleaner as tc
import movie_standardizer as ms
import pipeline as pl
import subtitle_fetcher as sf
from organizekit import core

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
                    self.assertLessEqual(len(line), core.REPORT_WIDTH, line)
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

    def test_a_deep_source_path_still_names_the_declined_file(self) -> None:
        """Platform-independent form of the failure CI saw on macos-latest.

        ``self.tmp`` is short on Linux and Windows, so the entry above fits
        there; a macOS temp dir (``/var/folders/.../T/...``) is long enough to
        overflow the entry line, and the old clip rendered it as
        ``.../final/Small...``.  Pinning a deep path makes the regression
        visible on every runner.
        """
        deep = self.tmp / "a-very-deeply-nested-incomplete-torrent-download" / "final"
        saved = (ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS)
        ms.CFG = ms.Config(source_dir=deep, target_dir=self.tmp / "lib",
                           log_file=None, report_file=self.tmp / "std_deep.txt")
        ms.RUN_SUMMARY = ms.RunSummary()
        ms.RUN_EVENTS = []
        try:
            ms.decline_source(deep / "Small.1995.mkv", "smaller than the 300 MB minimum")
            text = ms.build_report()
        finally:
            ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS = saved

        self.assertIn("Small.1995.mkv", section(text, "ITEMS LEFT IN SOURCE"))
        for line in text.splitlines():
            self.assertLessEqual(len(line), core.REPORT_WIDTH, line)


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





class ReportRendererTests(unittest.TestCase):
    """The shared renderer is what makes five tools' reports read alike.

    These pin the invariants every tool's report depends on: a boxed header,
    a scorecard whose counts sit in one right-aligned column, banners that
    delimit sections, and nothing that ever overflows the report width.
    """

    WIDTH = core.REPORT_WIDTH

    def test_header_is_a_box_the_exact_report_width(self) -> None:
        report = la.Report("TITLE", "subtitle")
        for line in report.render_header().splitlines():
            self.assertEqual(len(line), self.WIDTH)
        self.assertTrue(report.render_header().startswith("\u2554"))
        self.assertTrue(report.render_header().endswith("\u255d"))

    def test_long_metadata_wraps_inside_the_box(self) -> None:
        report = la.Report("T")
        report.meta("Ledger", "E:\\reports\\logs\\" + "x" * 200)
        for line in report.render_header().splitlines():
            self.assertEqual(len(line), self.WIDTH)

    def test_scorecard_counts_share_one_right_aligned_column(self) -> None:
        report = la.Report("T")
        report.scorecard([(3, "Needs a subtitle", "fix these"), (12, "Covered", "")])
        lines = [line for line in report.render().splitlines() if "Covered" in line or "Needs" in line]
        self.assertEqual(len(lines), 2)
        columns = {line.index("   ") for line in lines}
        self.assertEqual(len(columns), 1, lines)

    def test_no_rendered_line_exceeds_the_report_width(self) -> None:
        report = la.Report("T", "s")
        report.meta("Library", "E:\\" + "very-long-segment\\" * 12)
        report.scorecard([(1, "A very long scorecard label indeed", "a hint that is also long")])
        report.section("A SECTION TITLE", count=1, total=2, intro="word " * 200)
        report.subsection("A GROUP", count=1)
        report.entry("x" * 300, detail="y " * 200, ordinal=1)
        report.entry("short", detail="d", detail_column=40)
        report.table(["One", "Two"], [["a" * 200, "b" * 200]])
        report.footer(["z " * 200])
        for line in report.render().splitlines():
            self.assertLessEqual(len(line), self.WIDTH, line)

    def test_a_long_entry_path_keeps_its_file_name_whole(self) -> None:
        """An entry that overflows the width wraps; it is never ellipsised.

        Clipping was the original behaviour and it cost the one thing the line
        exists to name: on a macOS runner the standardizer's source paths run
        to ~90 columns, so ``/var/folders/.../Small.1995.mkv`` rendered as
        ``.../final/Small...`` and the test asserting the file name failed only
        on macOS.  Wrapping at separators keeps the name intact everywhere.
        """
        deep = "/var/folders/q7/" + "a" * 28 + "/T/ms_runstate_ab12cd34/final/Small.1995.mkv"
        report = la.Report("T")
        report.entry(deep, detail="smaller than the 300 MB minimum", ordinal=1)
        lines = report.render().splitlines()

        self.assertTrue(
            any(line.endswith("Small.1995.mkv") for line in lines),
            "\n".join(lines),
        )
        self.assertLessEqual(max(len(line) for line in lines), self.WIDTH)
        self.assertTrue(
            any("smaller than the 300 MB minimum" in line for line in lines),
            "\n".join(lines),
        )

    def test_path_wrapping_prefers_separators_over_splitting_names(self) -> None:
        wrapped = core.wrap_path_text("/aaa/bbb/ccc/ddd/Small.1995.mkv", 18)
        self.assertTrue(all(chunk in "/aaa/bbb/ccc/ddd/Small.1995.mkv" for chunk in wrapped))
        self.assertEqual("".join(wrapped).replace(" ", ""), "/aaa/bbb/ccc/ddd/Small.1995.mkv")
        self.assertIn("Small.1995.mkv", wrapped[-1])
        # A single component wider than the width still has to break somewhere.
        self.assertEqual(core.wrap_path_text("x" * 40, 16), ["x" * 16] * 2 + ["x" * 8])
        # Short text is untouched.
        self.assertEqual(core.wrap_path_text("Heat (1995).mkv", 40), ["Heat (1995).mkv"])

    def test_a_partial_run_never_reports_more_items_than_the_total(self) -> None:
        report = la.Report("T")
        report.section("GROUP", count=5, total=3)
        self.assertIn(" 5 ", report.render())
        self.assertNotIn("5 of 3", report.render())

    def test_entries_are_separated_by_a_blank_line(self) -> None:
        report = la.Report("T")
        report.entries([("first", "one"), ("second", "two")])
        body = report.render().splitlines()
        first = next(i for i, line in enumerate(body) if "first" in line)
        second = next(i for i, line in enumerate(body) if "second" in line)
        self.assertEqual(body[second - 1].strip(), "")
        self.assertGreater(second - first, 2)

    def test_table_columns_are_clipped_not_overflowed(self) -> None:
        report = la.Report("T", width=core.REPORT_MIN_WIDTH)
        report.table(["Folder", "Detail"], [["A" * 100, "B" * 100], ["short", "short"]])
        for line in report.render().splitlines():
            self.assertLessEqual(len(line), core.REPORT_MIN_WIDTH, line)

    def test_report_always_ends_with_exactly_one_newline(self) -> None:
        report = la.Report("T")
        report.paragraph("hello")
        self.assertTrue(report.render().endswith("hello\n"))


if __name__ == "__main__":
    unittest.main()
