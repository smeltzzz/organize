"""End-to-end runs of ``bitdepth.py`` against a fake ffprobe.

The inspector is read-only, which makes it easy to mistake for low-risk code:
nothing it does can lose a file. What it produces is a *worklist* - "send
these movies through HandBrake" - and acting on a wrong row means re-encoding
an HDR master into SDR by hand. The verdicts themselves are unit-tested
against payload dictionaries; everything around them was not tested at all.

This suite runs `main()` from an argv, with a real child process
(`tests/fake_ffprobe.py`) standing in for FFmpeg: real discovery, real worker
pool, real probe cache, real report, real state cache, real exit codes.

The exit codes are the point of several of these. `--fail-if-queue`,
`--fail-if-review` and `--fail-if-error` are how a scheduled run tells a human
something needs attention, and a wrong code is either a silent library
problem or a nightly job that cries wolf.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fake_ffprobe as fake

import bitdepth as bd

# The fake is launched through a shebang wrapper, so these tests are POSIX-only.
# The in-process suites cover the classification on every platform.
WINDOWS = os.name == "nt"


@unittest.skipIf(WINDOWS, "the fake ffprobe is launched through a POSIX shebang")
class InspectorRunFixture(unittest.TestCase):
    """A small library with one movie of each kind, and a fake FFmpeg."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="bd_e2e_")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.library = self.tmp / "Movies"
        self.library.mkdir()
        self.log = self.tmp / "out" / "bitdepth.log"
        self.report = self.tmp / "out" / "bitdepth_report.txt"
        self.cache = self.tmp / "out" / "probe_cache.json"
        self.state_db = self.tmp / "out" / "state.db"
        self.ffprobe = self._install_fake_ffprobe()
        self._saved_log_file = bd.log.file
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        bd.log.file = self._saved_log_file

    def _install_fake_ffprobe(self) -> Path:
        path = self.tmp / "ffprobe"
        path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.path.insert(0, {str(Path(fake.__file__).parent)!r})\n"
            "import fake_ffprobe\n"
            "sys.exit(fake_ffprobe.main())\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def movie(self, name: str, payload: dict, size: int = 2 * 1024 * 1024) -> Path:
        path = self.library / name / f"{name}.mkv"
        fake.write_movie(path, payload, size=size)
        return path

    def _run(self, *extra: str, env: dict[str, str] | None = None) -> int:
        argv = ["--source", str(self.library), "--log", str(self.log),
                "--report", str(self.report), "--cache", str(self.cache),
                "--state-db", str(self.state_db),
                "--ffprobe", str(self.ffprobe), "--workers", "2", *extra]
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(os.environ, env or {}):
            return bd.main(argv)

    def report_text(self) -> str:
        return self.report.read_text(encoding="utf-8")

    def verdicts(self) -> dict[str, str]:
        """Movie name -> the verdict the run recorded for it.

        Read back out of the shared state cache rather than scraped from the
        report: it is the same per-movie status the report groups by, and a
        renderer change should not break a test about classification.
        """
        from organizekit.core import KIND_BITDEPTH, open_state

        store = open_state(self.state_db, tool="tests")
        self.addCleanup(store.close)
        return {Path(key).name: row.verdict
                for (key, _), row in store.verdicts(KIND_BITDEPTH).items()}


class WhatTheInspectorFilesTests(InspectorRunFixture):
    """One library, one pass, six verdicts."""

    def setUp(self) -> None:
        super().setUp()
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        self.movie("High Bit SDR (2002)", fake.sdr_10bit())
        self.movie("Real HDR (2003)", fake.hdr10())
        self.movie("Mislabelled HDR (2004)", fake.hdr_8bit())
        self.movie("Mystery Depth (2005)", fake.unknown_depth())

    def test_each_movie_lands_in_the_right_queue(self) -> None:
        self.assertEqual(self._run(), 0)
        rows = self.verdicts()
        self.assertEqual(rows["Plain SDR (2001).mkv"], bd.STATUS_QUEUE)
        self.assertEqual(rows["High Bit SDR (2002).mkv"], bd.STATUS_SKIP_SDR)
        self.assertEqual(rows["Real HDR (2003).mkv"], bd.STATUS_SKIP_HDR)

    def test_a_mislabelled_hdr_file_is_never_queued_as_sdr(self) -> None:
        """8-bit with a PQ transfer is broken metadata, not a re-encode job."""
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.verdicts()["Mislabelled HDR (2004).mkv"],
                         bd.STATUS_REVIEW_8BIT_HDR)

    def test_an_unknown_bit_depth_waits_for_a_human(self) -> None:
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.verdicts()["Mystery Depth (2005).mkv"],
                         bd.STATUS_REVIEW_UNKNOWN_DEPTH)

    def test_an_unreadable_movie_is_an_error_row_not_a_dead_run(self) -> None:
        self.movie("Corrupt (2006)", {"_fail": "moov atom not found"})
        self.assertEqual(self._run(), 0)
        rows = self.verdicts()
        self.assertEqual(rows["Corrupt (2006).mkv"], bd.STATUS_ERROR)
        self.assertEqual(rows["Plain SDR (2001).mkv"], bd.STATUS_QUEUE,
                         "the rest of the library was still inspected")

    def test_a_cover_art_only_file_is_an_error_not_a_verdict(self) -> None:
        self.movie("Art Only (2007)", fake.cover_art_only())
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.verdicts()["Art Only (2007).mkv"], bd.STATUS_ERROR)

    def test_the_movies_are_named_in_the_report(self) -> None:
        self.assertEqual(self._run(), 0)
        text = self.report_text()
        for name in ("Plain SDR (2001)", "Real HDR (2003)", "Mystery Depth (2005)"):
            self.assertIn(name, text)
        self.assertTrue(self.log.is_file(), "the run log is written outside the library")


class ExitCodeTests(InspectorRunFixture):
    """What a scheduled run tells the machine that started it."""

    def test_a_clean_library_exits_zero(self) -> None:
        self.movie("High Bit SDR (2002)", fake.sdr_10bit())
        self.assertEqual(self._run("--fail-if-queue", "--fail-if-review",
                                   "--fail-if-error"), 0)

    def test_queued_movies_can_fail_the_run(self) -> None:
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        self.assertEqual(self._run(), 0, "silent by default")
        self.assertEqual(self._run("--fail-if-queue"), 3)

    def test_review_movies_can_fail_the_run(self) -> None:
        self.movie("Mislabelled HDR (2004)", fake.hdr_8bit())
        self.assertEqual(self._run("--fail-if-review"), 4)

    def test_errors_can_fail_the_run(self) -> None:
        self.movie("Corrupt (2006)", {"_fail": "moov atom not found"})
        self.assertEqual(self._run("--fail-if-error"), 5)

    def test_an_error_outranks_a_review_which_outranks_a_queue(self) -> None:
        """One code has to be chosen; the most serious finding wins."""
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        self.movie("Mislabelled HDR (2004)", fake.hdr_8bit())
        self.movie("Corrupt (2006)", {"_fail": "moov atom not found"})
        self.assertEqual(
            self._run("--fail-if-queue", "--fail-if-review", "--fail-if-error"), 5)
        self.assertEqual(self._run("--fail-if-queue", "--fail-if-review"), 4)
        self.assertEqual(self._run("--fail-if-queue"), 3)

    def test_a_missing_library_is_a_configuration_error(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            code = bd.main(["--source", str(self.tmp / "gone"),
                            "--log", str(self.log), "--report", str(self.report),
                            "--ffprobe", str(self.ffprobe), "--no-state"])
        self.assertEqual(code, 2)

    def test_an_ffprobe_that_will_not_run_stops_the_run(self) -> None:
        """Better no report than a report full of ERROR rows."""
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        self.assertEqual(self._run(env={"FAKE_FFPROBE_VERSION_RC": "1"}), 2)
        self.assertFalse(self.report.exists())

    def test_a_second_inspector_is_kept_out(self) -> None:
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        from organizekit.core import ExclusiveRunLock

        with ExclusiveRunLock(bd.run_lock_path(self.library), 1.0):
            self.assertEqual(self._run("--lock-timeout", "0"), 3)


class ProbeFailureTests(InspectorRunFixture):
    """Every way FFmpeg can be useless, contained per movie."""

    def setUp(self) -> None:
        super().setUp()
        self.movie("Plain SDR (2001)", fake.sdr_8bit())

    def test_a_nonzero_exit_becomes_that_movie_s_error(self) -> None:
        self.assertEqual(self._run(env={"FAKE_FFPROBE_RC": "1"}), 0)
        self.assertIn("Invalid data found", self.report_text())

    def test_garbage_on_stdout_is_reported_not_parsed(self) -> None:
        self.assertEqual(self._run(env={"FAKE_FFPROBE_GARBAGE": "1"}), 0)
        self.assertIn("invalid JSON", self.report_text())

    def test_a_hung_probe_gives_up_and_says_so(self) -> None:
        self.assertEqual(
            self._run("--timeout", "0.2", env={"FAKE_FFPROBE_SLEEP": "3"}), 0)
        self.assertIn("timed out", self.report_text())

    def test_a_failed_probe_is_never_cached(self) -> None:
        """A transient failure must not become a sticky verdict for that movie."""
        self.assertEqual(self._run(env={"FAKE_FFPROBE_RC": "1"}), 0)
        entries = (json.loads(self.cache.read_text(encoding="utf-8"))["entries"]
                   if self.cache.exists() else {})
        self.assertEqual(entries, {}, "nothing usable was learned, so nothing is kept")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.verdicts()["Plain SDR (2001).mkv"], bd.STATUS_QUEUE,
                         "the next run reaches the real answer")


class ProbeCacheTests(InspectorRunFixture):
    """The cache exists so the second sweep over an unchanged library is free."""

    def setUp(self) -> None:
        super().setUp()
        self.film = self.movie("Plain SDR (2001)", fake.sdr_8bit())

    def test_the_second_run_reuses_the_stored_probe(self) -> None:
        self.assertEqual(self._run(), 0)
        self.assertTrue(self.cache.is_file())
        self.assertEqual(self._run(), 0)
        self.assertIn("1 reused", self.log.read_text(encoding="utf-8"))

    def test_a_changed_movie_is_probed_again(self) -> None:
        self.assertEqual(self._run(), 0)
        fake.write_movie(self.film, fake.hdr10(), size=3 * 1024 * 1024)
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.verdicts()["Plain SDR (2001).mkv"], bd.STATUS_SKIP_HDR,
                         "the cache is keyed on size and mtime, so this is re-read")

    def test_no_cache_writes_nothing_and_reuses_nothing(self) -> None:
        self.assertEqual(self._run("--no-cache"), 0)
        self.assertFalse(self.cache.exists())

    def test_a_corrupt_cache_is_a_miss_not_a_crash(self) -> None:
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text("{not json at all", encoding="utf-8")
        self.assertEqual(self._run(), 0)
        self.assertIn("QUEUE", self.report_text().upper())


class WhatTheRunTellsStatusTests(InspectorRunFixture):
    """The verdicts `organize status` reads back."""

    def test_each_verdict_is_recorded(self) -> None:
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        self.movie("Real HDR (2003)", fake.hdr10())
        self.assertEqual(self._run(), 0)
        verdicts = self.verdicts()
        self.assertEqual(len(verdicts), 2)
        self.assertNotEqual(verdicts["Plain SDR (2001).mkv"],
                            verdicts["Real HDR (2003).mkv"])

    def test_no_state_records_nothing(self) -> None:
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        self.assertEqual(self._run("--no-state"), 0)
        self.assertFalse(self.state_db.exists())

    def test_a_state_db_inside_the_library_is_refused(self) -> None:
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        with contextlib.redirect_stdout(io.StringIO()):
            code = bd.main(["--source", str(self.library), "--log", str(self.log),
                            "--report", str(self.report), "--cache", str(self.cache),
                            "--state-db", str(self.library / "state.db"),
                            "--ffprobe", str(self.ffprobe)])
        self.assertEqual(code, 2, "no tool writes its bookkeeping into the library")


class InterruptedAndBrokenRunTests(InspectorRunFixture):
    """Ways a run ends early, and what survives it."""

    def setUp(self) -> None:
        super().setUp()
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        self.movie("Real HDR (2003)", fake.hdr10())

    def test_ctrl_c_publishes_what_was_already_learned(self) -> None:
        """Half a library inspected is still worth reporting."""
        real = bd.iter_completed

        def stop_after_one(items, fn, **kwargs):
            for index, outcome in enumerate(real(items, fn, **kwargs)):
                if index:
                    raise KeyboardInterrupt
                yield outcome

        with mock.patch.object(bd, "iter_completed", stop_after_one):
            self.assertEqual(self._run(), 0)
        self.assertIn("Interrupted", self.log.read_text(encoding="utf-8"))
        self.assertEqual(len(self.verdicts()), 1, "the finished movie was kept")
        self.assertTrue(self.report.is_file())

    def test_a_report_that_cannot_be_written_fails_the_run(self) -> None:
        """The report is the only output; a run that lost it did not succeed."""
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.report.mkdir()
        self.assertEqual(self._run(), 2)

    def test_an_unexpected_crash_still_leaves_through_an_exit_code(self) -> None:
        with mock.patch.object(bd, "scan", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self._run(), 1)
        self.assertIn("boom", err.getvalue(), "and says what went wrong")

    def test_ctrl_c_before_the_scan_starts_exits_130(self) -> None:
        with mock.patch.object(bd, "scan", side_effect=KeyboardInterrupt):
            self.assertEqual(self._run(), 130)


class ConfigurationRefusalTests(InspectorRunFixture):
    """Settings that would put tool output inside the library, or make no sense."""

    def refuse(self, *extra: str) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = bd.main(["--source", str(self.library), "--log", str(self.log),
                            "--report", str(self.report), "--cache", str(self.cache),
                            "--ffprobe", str(self.ffprobe), "--no-state", *extra])
        self.assertEqual(code, 2)
        return stdout.getvalue()

    def test_the_report_may_not_live_in_the_library(self) -> None:
        said = self.refuse("--report", str(self.library / "report.txt"))
        self.assertIn("outside --source", said)

    def test_the_log_may_not_live_in_the_library(self) -> None:
        said = self.refuse("--log", str(self.library / "run.log"))
        self.assertIn("outside --source", said)

    def test_the_probe_cache_may_not_live_in_the_library(self) -> None:
        said = self.refuse("--cache", str(self.library / "cache.json"))
        self.assertIn("outside --source", said)

    def test_the_log_and_the_report_may_not_be_the_same_file(self) -> None:
        said = self.refuse("--report", str(self.log))
        self.assertIn("different files", said)

    def test_nonsense_numbers_are_refused(self) -> None:
        self.assertIn("--min-size", self.refuse("--min-size", "-1"))
        self.assertIn("--timeout", self.refuse("--timeout", "0"))
        self.assertIn("--lock-timeout", self.refuse("--lock-timeout", "-5"))


class SelfTestTests(InspectorRunFixture):
    """`--self-test` is the field check on a machine with no test suite."""

    def self_test(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bd.main(["--self-test"])
        return code, out.getvalue()

    def test_it_passes_on_a_healthy_copy(self) -> None:
        code, said = self.self_test()
        self.assertEqual(code, 0)
        self.assertIn("PASSED", said)

    def test_it_notices_a_classifier_that_stopped_protecting_hdr(self) -> None:
        """Otherwise it is a green light that means nothing."""
        with mock.patch.object(bd, "classify_hdr",
                               return_value=(False, [], "everything is SDR now")):
            code, said = self.self_test()
        self.assertEqual(code, 1)
        self.assertIn("FAILED", said)


class DiscoveryTests(InspectorRunFixture):
    """What counts as a movie, before anything is probed."""

    def test_a_dry_run_lists_without_probing(self) -> None:
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertFalse(self.report.exists(), "a dry run publishes no report")
        self.assertIn("not probing", self.log.read_text(encoding="utf-8"))

    def test_an_empty_library_still_publishes_a_report(self) -> None:
        """A report that says "nothing here" is different from no report at all."""
        self.assertEqual(self._run(), 0)
        self.assertTrue(self.report.is_file())

    def test_small_files_are_not_movies(self) -> None:
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        self.movie("Sample (2001)", fake.sdr_8bit(), size=64 * 1024)
        self.assertEqual(self._run("--min-size", "1"), 0)
        self.assertNotIn("Sample (2001)", self.report_text())

    def test_extras_folders_are_not_movies(self) -> None:
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        fake.write_movie(self.library / "Plain SDR (2001)" / "Featurettes" /
                         "Making Of.mkv", fake.sdr_8bit())
        self.assertEqual(self._run(), 0)
        self.assertNotIn("Making Of", self.report_text())


if __name__ == "__main__":
    unittest.main()
