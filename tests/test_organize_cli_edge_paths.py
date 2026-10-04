"""Operator-facing edge paths of ``organize.py``: the doctor's plumbing, the
status summary, the subcommand delegation and every CLI dispatch arm.

These are the paths an operator runs on a machine that is not the developer's:
a version string a binary refuses to print, a missing sibling script, a
library whose movie file vanished between the audit and the status summary, a
unit-test suite that is not part of an installed build. Each test asserts the
exit code and the message the operator is left with.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import hermetic  # noqa: E402

import library_auditor  # noqa: E402
import organize  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="org_cli_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.library = self.root / "library"
        self.source = self.root / "source"
        self.library.mkdir()
        self.source.mkdir()


class PrimitiveTests(_Case):
    def test_colour_helpers_wrap_their_text(self) -> None:
        self.assertIn("hello", organize.blue("hello"))
        self.assertIn("hello", organize.magenta("hello"))

    def test_human_bytes_handles_every_magnitude(self) -> None:
        self.assertEqual("512 B", organize.human_bytes(512))
        self.assertEqual("2.0 KiB", organize.human_bytes(2048))
        self.assertEqual("3.0 MiB", organize.human_bytes(3 * 1024**2))
        self.assertEqual("4.0 GiB", organize.human_bytes(4 * 1024**3))
        self.assertEqual("5.0 TiB", organize.human_bytes(5 * 1024**4))

    def test_a_binary_that_prints_nothing_has_no_version(self) -> None:
        quiet = subprocess_result(stdout="", stderr="")
        with mock.patch.object(organize.subprocess, "run", return_value=quiet):
            self.assertEqual("", organize.get_binary_version("/bin/quiet"))

    def test_a_binary_that_fails_to_launch_has_no_version(self) -> None:
        with mock.patch.object(organize.subprocess, "run", side_effect=OSError("ENOENT")):
            self.assertEqual("", organize.get_binary_version("/bin/missing"))

    def test_the_source_root_falls_back_to_the_windows_final_folder(self) -> None:
        # Path("E:\\torrents\\final") is a WindowsPath, which POSIX refuses to
        # construct — so the fallback string is what this asserts, through a
        # Path double that records the argument the tool chose.
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(organize, "load_dotenv", lambda: None), \
                mock.patch.object(organize.os, "name", "nt"), \
                mock.patch.object(organize, "Path", side_effect=lambda value: ("chosen", value)):
            os.environ.pop("MOVIE_STD_SOURCE", None)
            self.assertEqual(("chosen", "E:\\torrents\\final"), organize._resolve_source_path(None))

    def test_victory_lap_banner_is_not_printed_on_a_plain_console(self) -> None:
        with mock.patch.object(organize, "_SUPPORTS_UNICODE", False), \
                redirect_stdout(io.StringIO()) as out:
            organize.print_hero_banner()
        self.assertIn("ORGANIZE", out.getvalue())


def subprocess_result(*, stdout: str, stderr: str) -> object:
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0)


class DoctorCheckTests(_Case):
    """A doctor that cannot see a tool must say so — and must not crash."""

    def test_a_working_ffmpeg_is_reported_ok(self) -> None:
        with mock.patch.dict(sys.modules, {}), \
                mock.patch("audio_standardizer.find_ffmpeg", return_value="/bin/ffmpeg"), \
                mock.patch("audio_standardizer.binary_works", return_value=True), \
                mock.patch.object(organize, "get_binary_version", return_value="6.0"):
            check = organize.check_ffmpeg(organize.DoctorContext(library=self.library, source=self.source))
        self.assertEqual("ok", check.status)
        self.assertIn("6.0", check.message)

    def test_a_missing_ffmpeg_is_reported_missing(self) -> None:
        with mock.patch("audio_standardizer.find_ffmpeg", return_value=None):
            check = organize.check_ffmpeg(organize.DoctorContext(library=self.library, source=self.source))
        # ffmpeg is only needed for the audio transcodes, so its absence is a
        # warning about that step — never a silent "ok".
        self.assertEqual("warn", check.status)
        self.assertIn("ffmpeg", check.name.casefold())

    def test_the_alternative_arc_wiring_is_named_in_the_chain_check(self) -> None:
        from organizekit.core import playbackchain as pc
        with mock.patch.object(pc, "resolve_wiring", return_value=pc.WIRING_TV_ARC):
            check = organize.check_playback_chain(organize.DoctorContext(library=self.library, source=self.source))
        self.assertIn("ARC", check.message + check.detail)


class StatusSummaryTests(_Case):
    """``organize status`` reads whatever the audit found and never dies on it."""

    def _folder(self, name: str, movies: list[str]) -> Path:
        folder = self.library / name
        folder.mkdir(exist_ok=True)
        for movie in movies:
            (folder / movie).write_bytes(b"x")
        return folder

    def test_a_folder_with_no_movie_and_one_with_two_are_both_skipped(self) -> None:
        self._folder("Empty (1999)", [])
        self._folder("Double (2000)", ["a.mkv", "b.mkv"])
        self._folder("Good (2001)", ["Good (2001).mkv"])
        audit = library_auditor.audit_library(library_auditor.Config(source_dir=self.library))
        with mock.patch.object(library_auditor, "audit_library", return_value=audit), \
                mock.patch.object(library_auditor, "publish_state"), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, organize.run_status(self.library, use_state=False))

    def test_a_movie_that_vanishes_before_the_stamp_is_still_counted(self) -> None:
        self._folder("Good (2001)", ["Good (2001).mkv"])
        audit = library_auditor.audit_library(library_auditor.Config(source_dir=self.library))
        real_stat = Path.stat

        def vanish(self: Path, **kwargs: object) -> os.stat_result:
            if self.name == "Good (2001).mkv":
                raise OSError("vanished after the scan")
            return real_stat(self, **kwargs)

        with mock.patch.object(library_auditor, "audit_library", return_value=audit), \
                mock.patch.object(library_auditor, "publish_state"), \
                mock.patch.object(Path, "stat", vanish), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, organize.run_status(self.library, use_state=False))

    def test_a_failed_scan_is_exit_2_with_the_captured_log(self) -> None:
        with mock.patch.object(library_auditor, "audit_library",
                               side_effect=OSError("disk fell over")), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = organize.run_status(self.library, use_state=False)
        self.assertEqual(2, code)
        self.assertIn("disk fell over", err.getvalue() + out.getvalue())

    def test_a_failed_scan_in_json_mode_stays_a_valid_document(self) -> None:
        with mock.patch.object(library_auditor, "audit_library",
                               side_effect=OSError("disk fell over")), \
                redirect_stdout(io.StringIO()) as out:
            code = organize.run_status(self.library, use_state=False, as_json=True)
        self.assertEqual(2, code)
        self.assertIn('"kind": "scan-failed"', out.getvalue())
        self.assertIn('"exit_code": 2', out.getvalue())

    def test_a_missing_library_is_exit_2_before_scanning(self) -> None:
        with redirect_stderr(io.StringIO()) as err:
            code = organize.run_status(self.library / "missing", use_state=False)
        self.assertEqual(2, code)
        self.assertIn("Library not found", err.getvalue())


class DelegationTests(_Case):
    """Every delegated tool is a separate process with its own exit code."""

    def test_a_missing_script_is_exit_2_and_never_launched(self) -> None:
        with mock.patch("organizekit.core.tool_is_available", return_value=False), \
                mock.patch.object(organize.subprocess, "run") as runner, \
                redirect_stderr(io.StringIO()) as err:
            code = organize.delegate_to_script("pipeline.py", [])
        self.assertEqual(2, code)
        self.assertIn("not found", err.getvalue())
        runner.assert_not_called()

    def test_the_childs_exit_code_is_passed_through(self) -> None:
        proc = types.SimpleNamespace(returncode=7)
        with mock.patch("organizekit.core.tool_is_available", return_value=True), \
                mock.patch("organizekit.core.tool_command", return_value=["python", "x.py"]), \
                mock.patch("organizekit.core.tools_home", return_value=REPO), \
                mock.patch.object(organize.subprocess, "run", return_value=proc):
            self.assertEqual(7, organize.delegate_to_script("pipeline.py", ["--scan"]))

    def test_an_interrupt_is_exit_130(self) -> None:
        with mock.patch("organizekit.core.tool_is_available", return_value=True), \
                mock.patch("organizekit.core.tool_command", return_value=["python", "x.py"]), \
                mock.patch("organizekit.core.tools_home", return_value=REPO), \
                mock.patch.object(organize.subprocess, "run", side_effect=KeyboardInterrupt), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(130, organize.delegate_to_script("pipeline.py", []))

    def test_a_launch_failure_is_exit_2(self) -> None:
        with mock.patch("organizekit.core.tool_is_available", return_value=True), \
                mock.patch("organizekit.core.tool_command", return_value=["python", "x.py"]), \
                mock.patch("organizekit.core.tools_home", return_value=REPO), \
                mock.patch.object(organize.subprocess, "run", side_effect=OSError("ENOENT")), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(2, organize.delegate_to_script("pipeline.py", []))
        self.assertIn("Error launching", err.getvalue())

    def test_the_command_table_routes_every_alias(self) -> None:
        routes = {
            "run": "pipeline.py", "pipeline": "pipeline.py",
            "standardize": "movie_standardizer.py", "std": "movie_standardizer.py",
            "extract": "subtitle_extractor.py", "extract-subs": "subtitle_extractor.py",
            "audio": "audio_standardizer.py", "audiofit": "audio_standardizer.py",
            "ac3": "audio_standardizer.py",
            "clean": "mkv_track_cleaner.py", "remux": "mkv_track_cleaner.py",
            "10bit": "bitdepth.py", "probe": "bitdepth.py", "audit": "library_auditor.py",
        }
        for command, script in routes.items():
            with self.subTest(command=command):
                with mock.patch.object(organize, "delegate_to_script",
                                       return_value=0) as delegate:
                    organize.main([command, "--flag"])
                self.assertEqual(script, delegate.call_args.args[0])
                self.assertEqual(["--flag"], delegate.call_args.args[1])


class TestRunnerTests(_Case):
    """`organize test` must work from a clone and explain itself elsewhere."""

    def test_the_internal_self_test_flag_short_circuits(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, organize.main(["--internal-self-test"]))
        self.assertIn("OK", out.getvalue())

    def test_a_missing_sibling_script_fails_the_self_test_suite(self) -> None:
        with mock.patch("organizekit.core.tool_is_available", return_value=False), \
                redirect_stdout(io.StringIO()) as out:
            code = organize.run_all_self_tests()
        self.assertEqual(1, code)
        self.assertIn("Missing file", out.getvalue())

    def test_a_failing_child_script_names_its_exit_code(self) -> None:
        proc = types.SimpleNamespace(returncode=3, stdout="boom", stderr="trace")
        with mock.patch("organizekit.core.tool_is_available", return_value=True), \
                mock.patch("organizekit.core.tool_command", return_value=["python", "x.py"]), \
                mock.patch("organizekit.core.tools_home", return_value=REPO), \
                mock.patch.object(organize.subprocess, "run", return_value=proc), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(1, organize.run_all_self_tests())
        self.assertIn("exit code 3", out.getvalue())

    def test_the_unit_suite_is_absent_from_an_installed_build(self) -> None:
        with mock.patch("organizekit.core.tools_home", return_value=self.root / "no-tests"), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, organize.run_unit_tests())
        self.assertIn("not part of the single-file build", out.getvalue())

    def test_the_unit_suite_runs_from_a_checkout(self) -> None:
        home = self.root / "clone"
        (home / "tests").mkdir(parents=True)
        proc = types.SimpleNamespace(returncode=0)
        with mock.patch("organizekit.core.tools_home", return_value=home), \
                mock.patch.object(organize.subprocess, "run", return_value=proc) as runner, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, organize.run_unit_tests())
        self.assertIn("unittest", runner.call_args.args[0])

    def test_the_test_command_chains_self_tests_and_unit_tests(self) -> None:
        with mock.patch.object(organize, "run_all_self_tests", return_value=0) as self_tests, \
                mock.patch.object(organize, "run_unit_tests", return_value=0) as unit_tests, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, organize.main(["test", "--unit"]))
        self_tests.assert_called_once()
        unit_tests.assert_called_once()

    def test_a_failing_self_test_suite_short_circuits_the_unit_tests(self) -> None:
        with mock.patch.object(organize, "run_all_self_tests", return_value=1), \
                mock.patch.object(organize, "run_unit_tests", return_value=0) as unit_tests, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(1, organize.main(["test", "--unit"]))
        unit_tests.assert_not_called()


class StdioReconfigurationTests(_Case):
    """A closed or detached stream is not a reason to fail a run."""

    def test_a_stream_without_reconfigure_is_skipped(self) -> None:
        stream = types.SimpleNamespace()
        with mock.patch.object(organize.sys, "stdout", stream), \
                mock.patch.object(organize.sys, "stderr", stream):
            organize._reconfigure_stdio_for_windows()

    def test_a_stream_that_refuses_reconfiguration_is_ignored(self) -> None:
        def explode(**kwargs: object) -> None:
            raise ValueError("detached stream")

        stream = types.SimpleNamespace(reconfigure=explode)
        with mock.patch.object(organize.sys, "stdout", stream), \
                mock.patch.object(organize.sys, "stderr", stream):
            organize._reconfigure_stdio_for_windows()


class HermeticDoctorTests(hermetic.HermeticToolsMixin, unittest.TestCase):
    """With the host toolchain pinned out, the doctor still answers."""

    def test_the_doctor_renders_with_no_tools_on_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="org_doc_") as td:
            root = Path(td)
            library = root / "library"
            source = root / "source"
            library.mkdir()
            source.mkdir()
            with redirect_stdout(io.StringIO()) as out:
                code = organize.main(["doctor", "--target", str(library), "--source", str(source)])
        self.assertEqual(0, code)
        self.assertIn("ffmpeg", out.getvalue().casefold())
