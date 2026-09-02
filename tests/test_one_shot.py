"""Tests for ``jellyfin_one_shot.py``, the one-shot library completer.

Everything here is offline: ``run_tool`` is replaced with a deterministic
fake so no tool subprocess is ever launched, and every file the runner
writes goes to a temp directory. The properties pinned here are the ones
that decide whether a run reaches its end state: coverage parsing must
agree with the auditor's actual report format, and none of the edge
cases (dry run, empty library, broken auditor, log dir inside the
library) may loop forever.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jellyfin_one_shot as js
import library_auditor as la


# ---------------------------------------------------------------------------
# Coverage parsing
# ---------------------------------------------------------------------------
class ParseAuditorCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="one_shot_parse_")
        self.tmp = Path(self._td.name)
        self.runtime_log = self.tmp / "test.log"
        self.runtime_log.touch()

    def tearDown(self) -> None:
        self._td.cleanup()

    def _report(self, name: str, content: str) -> Path:
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_machine_readable_summary_line(self) -> None:
        report = self._report("r.txt", "  AUDIT SUMMARY: canonical=7; total=9; pct=77.8%\n")
        self.assertEqual(js.parse_auditor_coverage(self.runtime_log, report), (7, 9))

    def test_summary_line_beats_scorecard(self) -> None:
        # A summary line and a scorecard that disagree: the machine line
        # is the contract, so it wins.
        report = self._report(
            "r.txt",
            "   3   Canonical MKV\n"
            "   3   Folders checked\n"
            "  AUDIT SUMMARY: canonical=2; total=5; pct=40.0%\n",
        )
        self.assertEqual(js.parse_auditor_coverage(self.runtime_log, report), (2, 5))

    def test_percentage_line(self) -> None:
        report = self._report("r.txt", "   42/42 (100.0%)  COVERAGE: movies with a validated English SRT\n")
        self.assertEqual(js.parse_auditor_coverage(self.runtime_log, report), (42, 42))

    def test_canonical_scorecard(self) -> None:
        report = self._report("r.txt", "   42  Canonical MKV\n   42  Folders checked\n")
        self.assertEqual(js.parse_auditor_coverage(self.runtime_log, report), (42, 42))

    def test_coverage_line(self) -> None:
        report = self._report(
            "r.txt",
            "Coverage this run: 5 of 9 movie(s) (55.6%) end with a validated external English SRT.\n",
        )
        self.assertEqual(js.parse_auditor_coverage(self.runtime_log, report), (5, 9))

    def test_missing_report(self) -> None:
        self.assertEqual(js.parse_auditor_coverage(self.runtime_log, self.tmp / "nope.txt"), (None, None))

    def test_unparseable_report(self) -> None:
        report = self._report("r.txt", "nothing in here looks like a report\n")
        self.assertEqual(js.parse_auditor_coverage(self.runtime_log, report), (None, None))

    def test_real_auditor_report_round_trip(self) -> None:
        """The one parser contract that matters: the one-shot must read the
        report that library_auditor.py actually renders, complete or not."""
        library = self.tmp / "lib"
        (library / "Good (2020)").mkdir(parents=True)
        (library / "Good (2020)" / "Good (2020).mkv").write_bytes(b"mkv")
        (library / "Good (2020)" / "Good (2020).eng.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
        (library / "Bare (2021)").mkdir()
        (library / "Bare (2021)" / "Bare (2021).mkv").write_bytes(b"mkv")

        cfg = la.Config(source_dir=library, log_file=self.tmp / "a.log",
                        report_file=self.tmp / "a.txt", lock_timeout_seconds=0)
        audit = la.audit_library(cfg)
        report = self.tmp / "auditor_report.txt"
        report.write_text(la.build_report(audit, cfg), encoding="utf-8")

        covered, total = js.parse_auditor_coverage(self.runtime_log, report)
        self.assertEqual((covered, total), (1, 2))
        self.assertFalse(js.is_library_complete(covered, total))

        # Remove the sidecar-less movie: the same report path must now
        # parse as 100%.
        (library / "Bare (2021)" / "Bare (2021).mkv").unlink()
        (library / "Bare (2021)").rmdir()
        audit2 = la.audit_library(cfg)
        report.write_text(la.build_report(audit2, cfg), encoding="utf-8")
        covered, total = js.parse_auditor_coverage(self.runtime_log, report)
        self.assertEqual((covered, total), (1, 1))
        self.assertTrue(js.is_library_complete(covered, total))


class IsLibraryCompleteTests(unittest.TestCase):
    def test_complete(self) -> None:
        self.assertTrue(js.is_library_complete(42, 42))

    def test_partial(self) -> None:
        self.assertFalse(js.is_library_complete(41, 42))

    def test_unknown(self) -> None:
        self.assertFalse(js.is_library_complete(None, 42))
        self.assertFalse(js.is_library_complete(42, None))

    def test_empty_is_not_complete(self) -> None:
        # 0/0 is handled by the runner as a misconfiguration, not as
        # success: is_library_complete stays strict.
        self.assertFalse(js.is_library_complete(0, 0))


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="one_shot_val_")
        self.tmp = Path(self._td.name)
        self.source = self.tmp / "library"
        self.source.mkdir()

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_log_dir_inside_source(self) -> None:
        self.assertTrue(js.log_dir_inside_source(self.source / "logs", self.source))

    def test_log_dir_equal_to_source(self) -> None:
        self.assertTrue(js.log_dir_inside_source(self.source, self.source))

    def test_log_dir_sibling(self) -> None:
        self.assertFalse(js.log_dir_inside_source(self.tmp / "elsewhere", self.source))

    def test_log_dir_not_yet_created(self) -> None:
        self.assertTrue(js.log_dir_inside_source(self.source / "new" / "logs", self.source))

    def test_missing_tool_scripts(self) -> None:
        self.assertEqual(set(js.missing_tool_scripts(self.tmp)), set(js.TOOL_SCRIPTS))
        for name in js.TOOL_SCRIPTS:
            (self.tmp / name).write_text("# fake tool\n", encoding="utf-8")
        self.assertEqual(js.missing_tool_scripts(self.tmp), [])


class TailToFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="one_shot_tail_")
        self.tmp = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_bounded_and_keeps_newest(self) -> None:
        target = self.tmp / "t.log"
        for i in range(5):
            js.tail_to_file(target, "\n".join(f"line {i}-{j}" for j in range(10)), max_lines=15)
        # 5 appends of 10 lines keep the last 15: i=4 all ten, plus i=3's j>=5.
        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 15)
        self.assertEqual(lines[-1], "line 4-9")
        self.assertEqual(lines[0], "line 3-5")


# ---------------------------------------------------------------------------
# Orchestrator behaviour (run_tool faked, sleeps faked)
# ---------------------------------------------------------------------------
COMPLETE_REPORT = "  AUDIT SUMMARY: canonical=2; total=2; pct=100.0%\n"
PARTIAL_REPORT = "  AUDIT SUMMARY: canonical=1; total=2; pct=50.0%\n"
EMPTY_REPORT = "  AUDIT SUMMARY: canonical=0; total=0; pct=100.0%\n"


class FakeToolRunner:
    """Deterministic stand-in for run_tool.

    The subtitle fetcher succeeds; the auditor writes the configured
    report to the --report path and honours the auditor's exit-code
    contract (0 healthy / 0 with findings unless --fail-on-findings,
    1 with findings and --fail-on-findings, configurable override).
    """

    def __init__(
        self,
        report: str = PARTIAL_REPORT,
        auditor_rc: int | None = None,
        fetcher_rc: int = 0,
        write_report: bool = True,
    ) -> None:
        self.report = report
        self.auditor_rc = auditor_rc
        self.fetcher_rc = fetcher_rc
        self.write_report = write_report
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(
        self,
        runtime_log: Path,
        script_path: Path,
        args: list[str],
        tool_name: str,
        timeout: float | None = None,
        transcript: Path | None = None,
    ) -> tuple[int, str, str]:
        self.calls.append((script_path.name, list(args)))
        if script_path.name == "subtitle_fetcher.py":
            return self.fetcher_rc, "fetcher ok\n", ""
        if script_path.name == "library_auditor.py":
            if self.write_report:
                report_arg = args[args.index("--report") + 1]
                Path(report_arg).parent.mkdir(parents=True, exist_ok=True)
                Path(report_arg).write_text(self.report, encoding="utf-8")
            rc = self.auditor_rc
            if rc is None:
                complete = "pct=100.0%" in self.report
                rc = 1 if ("--fail-on-findings" in args and not complete) else 0
            return rc, "", ""
        return 0, "", ""


class RunOneShotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="one_shot_run_")
        self.tmp = Path(self._td.name)
        self.library = self.tmp / "lib"
        self.library.mkdir()
        self.log_dir = self.tmp / "logs"
        self.runtime_log = self.log_dir / "one_shot_test.log"
        self.tools = {"mkvmerge": False, "ffprobe": False, "ffsubsync": False, "ffmpeg": False}

    def tearDown(self) -> None:
        self._td.cleanup()

    def _run(self, fake: FakeToolRunner, **kwargs: object) -> int:
        tools = kwargs.pop("tools", self.tools)
        with mock.patch.object(js, "run_tool", fake), \
                mock.patch.object(js.time, "sleep", lambda _s: None), \
                mock.patch.object(js, "wait_for_utc_midnight", lambda _log: None):
            return js.run_one_shot(
                library=self.library,
                script_dir=Path(js.__file__).parent,
                runtime_log=self.runtime_log,
                log_dir=self.log_dir,
                tools=tools,
                **kwargs,
            )

    def _steps_called(self, fake: FakeToolRunner) -> list[str]:
        return [name for name, _args in fake.calls]

    def test_complete_library_is_left_alone_by_the_preflight_audit(self) -> None:
        # The auditor is the verdict this runner chases and the cheapest step
        # by far, so a finished library is audited and then left alone.
        fake = FakeToolRunner(report=COMPLETE_REPORT)
        code = self._run(fake, dry_run=False)
        self.assertEqual(code, 0)
        self.assertEqual(self._steps_called(fake), ["library_auditor.py"])
        text = self.runtime_log.read_text(encoding="utf-8")
        self.assertIn("LIBRARY ALREADY COMPLETE", text)
        self.assertIn("--force-pass", text)

    def test_force_pass_runs_the_sweep_even_when_complete(self) -> None:
        fake = FakeToolRunner(report=COMPLETE_REPORT)
        code = self._run(fake, dry_run=False, force_pass=True)
        self.assertEqual(code, 0)
        self.assertEqual(self._steps_called(fake), ["subtitle_fetcher.py", "library_auditor.py"])

    def test_preflight_does_not_shorten_a_dry_run(self) -> None:
        # A dry run previews exactly one pass; skipping it would preview nothing.
        fake = FakeToolRunner(report=COMPLETE_REPORT)
        code = self._run(fake, dry_run=True, force_pass=False)
        self.assertEqual(code, 0)
        self.assertEqual(self._steps_called(fake), ["subtitle_fetcher.py", "library_auditor.py"])

    def test_partial_library_still_runs_a_full_pass(self) -> None:
        fake = FakeToolRunner(report=PARTIAL_REPORT)
        code = self._run(fake, dry_run=False, max_passes=1)
        self.assertEqual(code, 1)
        fetch_calls = [n for n, _a in fake.calls if n == "subtitle_fetcher.py"]
        self.assertEqual(len(fetch_calls), 1, "an incomplete library is never left alone")
        text = self.runtime_log.read_text(encoding="utf-8")
        self.assertIn("Pre-flight coverage: 1/2", text)

    def test_preflight_empty_library_exits_2_without_a_sweep(self) -> None:
        fake = FakeToolRunner(report=EMPTY_REPORT)
        code = self._run(fake, dry_run=False, max_passes=5)
        self.assertEqual(code, 2)
        self.assertEqual(self._steps_called(fake), ["library_auditor.py"])

    def test_unusable_preflight_falls_through_to_a_full_pass(self) -> None:
        # A blocked or broken audit is not a verdict: the pass loop's own
        # retry and bad-audit accounting applies.
        fake = FakeToolRunner(report="", auditor_rc=2, write_report=False)
        code = self._run(fake, dry_run=False, max_passes=1)
        self.assertEqual(code, 1)
        fetch_calls = [n for n, _a in fake.calls if n == "subtitle_fetcher.py"]
        self.assertEqual(len(fetch_calls), 1, "no verdict means the sweep still runs")

    def test_dry_run_is_exactly_one_pass_and_exits_0(self) -> None:
        fake = FakeToolRunner(report=PARTIAL_REPORT)
        code = self._run(fake, dry_run=True, max_passes=0)
        self.assertEqual(code, 0, "a dry-run preview is a success even when incomplete")
        fetch_calls = [args for name, args in fake.calls if name == "subtitle_fetcher.py"]
        audit_calls = [args for name, args in fake.calls if name == "library_auditor.py"]
        self.assertEqual(len(fetch_calls), 1, "dry run must not loop passes")
        self.assertEqual(len(audit_calls), 1)
        self.assertIn("--dry-run", fetch_calls[0])
        # The auditor is read-only; it takes no --dry-run flag.
        self.assertNotIn("--dry-run", audit_calls[0])

    def test_sync_step_pins_its_ledger_under_the_log_dir(self) -> None:
        # Every artifact of a run lives under --log-dir, so the remembered
        # sync verdicts do too.
        fake = FakeToolRunner(report=PARTIAL_REPORT)
        self._run(fake, dry_run=True, tools={"ffsubsync": True, "ffmpeg": True})
        _name, args = next((n, a) for n, a in fake.calls if n == "sync_subtitles.py")
        self.assertIn(str(self.log_dir / "sync_state.json"),
                      args[args.index("--sync-ledger") + 1:])

    def test_dry_run_pins_log_report_and_transcript_paths(self) -> None:
        fake = FakeToolRunner(report=PARTIAL_REPORT)
        self._run(fake, dry_run=True)
        _name, fetch_args = next((n, a) for n, a in fake.calls if n == "subtitle_fetcher.py")
        self.assertIn(str(self.log_dir / "subtitle_fetcher_report.txt"), fetch_args)
        self.assertIn(str(self.log_dir / "subtitle_fetcher.log"), fetch_args)
        self.assertIn("--allow-missing", fetch_args)
        self.assertIn("--scrape-daily-cap", fetch_args)

    def test_partial_library_respects_max_passes_and_exits_1(self) -> None:
        fake = FakeToolRunner(report=PARTIAL_REPORT)
        code = self._run(fake, dry_run=False, max_passes=2)
        self.assertEqual(code, 1)
        # Pre-flight, then two passes, then the final audit.
        audit_calls = [args for name, args in fake.calls if name == "library_auditor.py"]
        self.assertEqual(len(audit_calls), 4)
        self.assertTrue(any("--fail-on-findings" in args for args in audit_calls),
                        "the final audit uses the fail gate")
        self.assertFalse(any("--fail-on-findings" in args for args in audit_calls[:3]),
                         "the pre-flight and per-pass audits do not use the fail gate")

    def test_empty_library_exits_2_with_no_loop(self) -> None:
        fake = FakeToolRunner(report=EMPTY_REPORT)
        code = self._run(fake, dry_run=False, max_passes=5, force_pass=True)
        self.assertEqual(code, 2)
        # The empty verdict comes from the first pass; no further passes.
        self.assertEqual(len([n for n, _a in fake.calls if n == "subtitle_fetcher.py"]), 1)

    def test_persistently_failing_auditor_exits_1(self) -> None:
        # rc 2, no report written: the worst audit case. Three bad passes
        # in a row must hard-stop instead of hot-looping.
        fake = FakeToolRunner(report="", auditor_rc=2, write_report=False)
        code = self._run(fake, dry_run=False, max_passes=10)
        self.assertEqual(code, 1)
        # One pre-flight attempt (which cannot produce a verdict) plus three
        # bad passes, each of AUDIT_ATTEMPTS_PER_PASS tries.
        self.assertEqual(len([n for n, _a in fake.calls if n == "library_auditor.py"]),
                         1 + js.AUDIT_ATTEMPTS_PER_PASS * js.MAX_CONSECUTIVE_BAD_AUDITS)

    def test_auditor_lock_contention_is_retried_within_the_pass(self) -> None:
        # The auditor is blocked (rc 3, another process holds the lock) for
        # the first AUDIT_ATTEMPTS_PER_PASS attempts, then succeeds and
        # writes a complete report. The pass must retry through the lock
        # contention and the run must complete (exit 0), not treat the
        # blocked attempts as permanent failures.
        audit_attempts = {"n": 0}

        def flaky(runtime_log, script_path, args, tool_name, **kw):
            if script_path.name == "library_auditor.py":
                audit_attempts["n"] += 1
                if audit_attempts["n"] <= js.AUDIT_ATTEMPTS_PER_PASS:
                    return 3, "", "locked"
                report_arg = args[args.index("--report") + 1]
                Path(report_arg).parent.mkdir(parents=True, exist_ok=True)
                Path(report_arg).write_text(COMPLETE_REPORT, encoding="utf-8")
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(js, "run_tool", flaky), \
                mock.patch.object(js.time, "sleep", lambda _s: None), \
                mock.patch.object(js, "wait_for_utc_midnight", lambda _log: None):
            code = js.run_one_shot(
                library=self.library,
                script_dir=Path(js.__file__).parent,
                runtime_log=self.runtime_log,
                log_dir=self.log_dir,
                tools=self.tools,
                max_passes=10,
            )
        self.assertEqual(code, 0)
        self.assertEqual(audit_attempts["n"], js.AUDIT_ATTEMPTS_PER_PASS + 1)

    def test_stagnation_waits_for_utc_rollover(self) -> None:
        fake = FakeToolRunner(report=PARTIAL_REPORT)
        with mock.patch.object(js, "run_tool", fake), \
                mock.patch.object(js.time, "sleep", lambda _s: None), \
                mock.patch.object(js, "wait_for_utc_midnight", return_value=None) as w:
            js.run_one_shot(
                library=self.library,
                script_dir=Path(js.__file__).parent,
                runtime_log=self.runtime_log,
                log_dir=self.log_dir,
                tools=self.tools,
                max_passes=js.STAGNATION_PASSES_BEFORE_ROLLOVER + 1,
            )
        # Pass 1 sets the baseline; passes 2 and 3 show no improvement, so
        # the rollover wait fires once before pass 3.
        self.assertEqual(w.call_count, 1)

    def test_progress_resets_the_stagnation_counter(self) -> None:
        reports = iter([PARTIAL_REPORT, COMPLETE_REPORT, PARTIAL_REPORT])
        fake = FakeToolRunner(report=PARTIAL_REPORT)

        def runner(runtime_log, script_path, args, tool_name, **kw):
            if script_path.name == "library_auditor.py":
                fake.report = next(reports)
            return fake(runtime_log, script_path, args, tool_name, **kw)

        with mock.patch.object(js, "run_tool", runner), \
                mock.patch.object(js.time, "sleep", lambda _s: None), \
                mock.patch.object(js, "wait_for_utc_midnight", return_value=None) as w:
            code = js.run_one_shot(
                library=self.library,
                script_dir=Path(js.__file__).parent,
                runtime_log=self.runtime_log,
                log_dir=self.log_dir,
                tools=self.tools,
                max_passes=5,
            )
        self.assertEqual(code, 0, "the complete report on pass 2 must end the run")
        self.assertEqual(w.call_count, 0, "improved coverage must not wait for midnight")


# ---------------------------------------------------------------------------
# CLI validation (no subprocesses: main() rejects before any run)
# ---------------------------------------------------------------------------
class MainValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="one_shot_main_")
        self.tmp = Path(self._td.name)
        self.library = self.tmp / "lib"
        self.library.mkdir()

    def tearDown(self) -> None:
        self._td.cleanup()

    def _script_dir(self) -> Path:
        return Path(js.__file__).resolve().parent

    def test_missing_source_flag(self) -> None:
        # There is no "missing" source any more: the library root resolves
        # like every sibling tool (default, then MOVIE_STD_TARGET). Pin the
        # default to an absent path so the run still refuses with exit 2.
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(js, "DEFAULT_LIBRARY", str(self.tmp / "absent")):
            self.assertEqual(js.main([]), 2)

    def test_nonexistent_source(self) -> None:
        self.assertEqual(js.main(["--source", str(self.tmp / "nope")]), 2)

    def test_source_is_a_file(self) -> None:
        f = self.tmp / "file.txt"
        f.write_text("x", encoding="utf-8")
        self.assertEqual(js.main(["--source", str(f)]), 2)

    def test_log_dir_inside_source(self) -> None:
        code = js.main([
            "--source", str(self.library),
            "--log-dir", str(self.library / "logs"),
        ])
        self.assertEqual(code, 2)
        self.assertFalse((self.library / "logs").exists(),
                         "validation must not create the log dir before rejecting it")

    def test_missing_tool_scripts(self) -> None:
        code = js.main([
            "--source", str(self.library),
            "--log-dir", str(self.tmp / "logs"),
            "--script-dir", str(self.tmp),
        ])
        self.assertEqual(code, 2)

    def test_negative_timeout_scale(self) -> None:
        code = js.main([
            "--source", str(self.library),
            "--log-dir", str(self.tmp / "logs"),
            "--timeout-scale", "-1",
        ])
        self.assertEqual(code, 2)

    def test_version_flag(self) -> None:
        import contextlib
        import io

        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(out):
            js.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(js.VERSION, out.getvalue())

    def test_self_test_passes(self) -> None:
        self.assertEqual(js.main(["--self-test"]), 0)


# ---------------------------------------------------------------------------
# Library root resolution
# ---------------------------------------------------------------------------
class LibraryResolutionTests(unittest.TestCase):
    """The one-shot resolves its library root exactly like the sibling tools:
    flag first, then MOVIE_STD_TARGET, then the documented default — so a bare
    run finishes the same library everything else maintains."""

    def setUp(self) -> None:
        self._saved = os.environ.pop("MOVIE_STD_TARGET", None)

    def tearDown(self) -> None:
        if self._saved is not None:
            os.environ["MOVIE_STD_TARGET"] = self._saved
        else:
            os.environ.pop("MOVIE_STD_TARGET", None)

    def test_no_flag_no_env_uses_documented_default(self) -> None:
        self.assertEqual(js.resolve_library(None), Path(js.DEFAULT_LIBRARY))
        self.assertEqual(js.DEFAULT_LIBRARY, r"E:\torrents\final_organized",
                         "the default must stay identical to the sibling tools")

    def test_env_var_is_honored_without_a_flag(self) -> None:
        os.environ["MOVIE_STD_TARGET"] = "/media/torrents/final_organized"
        self.assertEqual(js.resolve_library(None), Path("/media/torrents/final_organized"))

    def test_explicit_flag_beats_env_var(self) -> None:
        os.environ["MOVIE_STD_TARGET"] = "/media/torrents/final_organized"
        self.assertEqual(js.resolve_library(Path("/srv/movies")), Path("/srv/movies"))

    def test_origin_is_reported_for_every_source(self) -> None:
        self.assertEqual(
            js.describe_library_origin(None),
            f"the default library root ({js.DEFAULT_LIBRARY})",
        )
        os.environ["MOVIE_STD_TARGET"] = "/media/torrents/final_organized"
        self.assertEqual(js.describe_library_origin(None), "MOVIE_STD_TARGET")
        self.assertEqual(js.describe_library_origin(Path("/srv/movies")), "--source")

    def test_bare_run_rejects_a_default_library_that_is_not_there(self) -> None:
        """A bare run on a machine without the default path must still fail
        loudly (exit 2) and say where the path came from."""
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(js, "DEFAULT_LIBRARY", "/nonexistent/library"), \
                mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(js.main([]), 2)
        self.assertIn("resolved from the default library root", err.getvalue())


if __name__ == "__main__":
    unittest.main()
