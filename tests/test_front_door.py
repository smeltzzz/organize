"""The two front doors: the single-file archive's entry point and ``organize.py``.

Both of these are the part of the toolkit an operator actually touches, and
both are the part the rest of the suite reaches only from the inside: the unit
tests import the tools and call their functions, so nothing ever walked the
``python organize.pyz ...`` dispatcher or the ``organize test`` / ``organize
clean`` delegation table until now.

``__main__.py`` is the archive's whole reason for existing. Out of a checkout a
step is started as ``[python, bitdepth.py, ...]``; inside the archive there is
no such file, so the archive re-enters itself with ``run-tool bitdepth.py``.
That verb takes the module name from a command line, which makes its allowlist
a security boundary and its argv rewriting the difference between a child that
sees the arguments it was given and one that sees the archive's. The file's own
docstring claims the normal test suite exercises this dispatch; these tests are
what makes that claim true.

``organize.py``'s job is to be a launcher whose exit status can be trusted: a
cron hook or a qBittorrent "run on completion" script reads it and nothing
else, so a failing step must not be reported as success, ``Ctrl-C`` must be
130, and an unlaunchable tool must be a clean error rather than a traceback.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import platforms

import organize
from organizekit import core
from organizekit.core import toolchain

REPO = Path(__file__).resolve().parent.parent


def _load_archive_entry_point() -> types.ModuleType:
    """Load ``__main__.py`` as an ordinary module.

    It cannot be imported by name - ``__main__`` is already taken by the test
    runner - but its file is the file the archive carries, so loading it by
    path measures and tests exactly what ships.
    """
    spec = importlib.util.spec_from_file_location("organize_archive_entry", REPO / "__main__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENTRY = _load_archive_entry_point()


class _StubTool:
    """A stand-in for one tool module, recording what the dispatcher handed it."""

    def __init__(self, code: object = 0) -> None:
        self.code = code
        self.seen_argv: list[str] | None = None

    def module(self, name: str) -> types.ModuleType:
        stub = types.ModuleType(name)

        def main(argv: object = None) -> object:
            # A tool's main() defaults to sys.argv[1:], so what it can see is
            # sys.argv itself - which is exactly what the dispatcher must set.
            self.seen_argv = list(sys.argv)
            return self.code

        stub.main = main  # type: ignore[attr-defined]
        return stub


class ArchiveEntryPointTests(unittest.TestCase):
    """``python organize.pyz [run-tool <script.py>] ...``."""

    def test_run_tool_without_a_script_exits_2_and_names_what_it_can_run(self) -> None:
        """A bare ``run-tool`` is a typo, and the answer has to be actionable.

        The archive is copied to a NAS and run by hand; there is no man page
        beside it. Exit 2 is argparse's "you used me wrong", and the tool list
        on stderr is the only documentation the operator will get.
        """
        err = io.StringIO()
        with redirect_stderr(err):
            code = ENTRY.run_tool([])
        self.assertEqual(code, 2)
        printed = err.getvalue()
        self.assertIn("usage: run-tool <tool.py> [args...]", printed)
        for script in ENTRY.RUNNABLE:
            self.assertIn(script, printed, f"{script} is runnable but the usage line omits it")

    def test_run_tool_refuses_a_name_that_is_not_one_of_its_own_tools(self) -> None:
        """The verb's argument comes from a command line, so it is an allowlist.

        ``importlib.import_module`` on an operator-supplied string would run
        whatever module that string names - ``run-tool os`` or a path with
        ``../`` in it - inside the archive's own interpreter. The refusal must
        happen *before* the import, so the assertion is that nothing was
        imported at all.
        """
        for name in ("os", "json", "bitdepth", "../organize.py", "organize.exe"):
            with self.subTest(name=name):
                err = io.StringIO()
                with mock.patch("importlib.import_module") as importer, redirect_stderr(err):
                    code = ENTRY.run_tool([name, "--self-test"])
                self.assertEqual(code, 2)
                importer.assert_not_called()
                self.assertIn(f"unknown tool: {name}", err.getvalue())

    def test_every_step_the_pipeline_can_run_is_dispatchable_from_the_archive(self) -> None:
        """The allowlist and the step table are written in different files.

        ``tool_command`` builds ``run-tool <script>`` for every step; if a step
        is ever added to ``STEP_ORDER`` without landing in ``RUNNABLE``, the
        checkout keeps working and only the single-file build fails - at the
        moment a NAS with no other copy tries to run that step.
        """
        self.assertTrue(set(toolchain.TOOL_SCRIPTS).issubset(set(ENTRY.RUNNABLE)))
        for extra in ("organize.py", "pipeline.py", "movie_standardizer.py"):
            self.assertIn(extra, ENTRY.RUNNABLE)

    def test_run_tool_gives_the_child_the_argv_it_would_have_seen_on_disk(self) -> None:
        """``sys.argv`` is rewritten to the script's own name and arguments.

        A tool's parser prints ``sys.argv[0]`` as its program name and reads
        ``sys.argv[1:]`` when main() is called with no argument. Inside the
        archive argv[0] is ``organize.pyz`` and argv[1] is ``run-tool``, so
        without the rewrite every child would see the launcher's arguments and
        print the wrong name in its own help and error messages.
        """
        stub = _StubTool(code=0)
        with mock.patch.dict(sys.modules, {"pipeline": stub.module("pipeline")}):
            code = ENTRY.run_tool(["pipeline.py", "--dry-run", "--source", "/media/in"])
        self.assertEqual(code, 0)
        self.assertEqual(stub.seen_argv, ["pipeline.py", "--dry-run", "--source", "/media/in"])

    def test_a_tools_exit_status_is_reported_unchanged(self) -> None:
        """The archive must not swallow a step's failure.

        A pipeline that spawns its steps through the archive reads this return
        value; flattening it to 0 would turn a failed remux into a green run.
        """
        stub = _StubTool(code=3)
        with mock.patch.dict(sys.modules, {"bitdepth": stub.module("bitdepth")}):
            code = ENTRY.run_tool(["bitdepth.py", "--self-test"])
        self.assertEqual(code, 3)

    def test_a_tool_that_returns_nothing_is_success_not_a_crash(self) -> None:
        """``int(main() or 0)``: a tool that just returns is a tool that passed.

        Several of the tools' main() functions end without an explicit status.
        ``int(None)`` would raise, and the archive would report a failure for a
        step that did its work.
        """
        stub = _StubTool(code=None)
        with mock.patch.dict(sys.modules, {"library_auditor": stub.module("library_auditor")}):
            code = ENTRY.run_tool(["library_auditor.py", "--self-test"])
        self.assertEqual(code, 0)

    def test_main_dispatches_the_run_tool_verb_and_nothing_else(self) -> None:
        stub = _StubTool(code=0)
        with mock.patch.dict(sys.modules, {"pipeline": stub.module("pipeline")}):
            code = ENTRY.main(["run-tool", "pipeline.py", "--help"])
        self.assertEqual(code, 0)
        self.assertEqual(stub.seen_argv, ["pipeline.py", "--help"])

    def test_without_the_verb_the_archive_is_the_organize_cli(self) -> None:
        """``python organize.pyz status ...`` has to behave like ``organize status``.

        Asserted on a real CLI call rather than a stub: the same version string
        the installed console script prints, and argv[0] renamed so the child's
        own ``--help`` says ``organize`` and not ``organize.pyz``.
        """
        out = io.StringIO()
        with redirect_stdout(out):
            code = ENTRY.main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(f"organize {organize.VERSION}", out.getvalue())
        self.assertEqual(sys.argv[0], "organize")

    def test_main_falls_back_to_the_real_command_line(self) -> None:
        """The archive is started by a shell, so argv is the usual source."""
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["organize.pyz", "--version"]), redirect_stdout(out):
            code = ENTRY.main()
        self.assertEqual(code, 0)
        self.assertIn(f"organize {organize.VERSION}", out.getvalue())

    def test_the_cli_exit_status_survives_the_archive(self) -> None:
        """``organize`` answers 2 for an unknown command; the archive must too."""
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = ENTRY.main(["no-such-command"])
        self.assertEqual(code, 2)
        self.assertIn("Unknown command", err.getvalue())


class DelegationTests(unittest.TestCase):
    """``organize <step>`` starts that step as its own process."""

    def _delegate(self, *args: str) -> tuple[int, subprocess.CompletedProcess[str] | None]:
        captured: list[subprocess.CompletedProcess[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            proc = subprocess.CompletedProcess(cmd, 0)
            proc.returncode = 4  # type: ignore[misc]
            captured.append(proc)  # type: ignore[arg-type]
            return proc  # type: ignore[return-value]

        with mock.patch.object(subprocess, "run", fake_run), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = organize.main(list(args))
        return code, captured[0] if captured else None

    def test_a_step_is_started_with_its_own_script_and_the_remaining_arguments(self) -> None:
        code, proc = self._delegate("clean", "--dry-run", "--dir", "/media/lib")
        self.assertIsNotNone(proc)
        assert proc is not None
        self.assertEqual(proc.args, [sys.executable, str(REPO / "mkv_track_cleaner.py"),
                                     "--dry-run", "--dir", "/media/lib"])
        self.assertEqual(code, 4, "the front door must report the child's own status")

    def test_every_alias_reaches_the_tool_it_is_documented_to_reach(self) -> None:
        """The command table is what the README's examples depend on.

        A wrong mapping here does not fail loudly - ``organize audio`` running
        the cleaner would look like a working command until it touched a
        library.
        """
        expected = {
            "run": "pipeline.py", "pipeline": "pipeline.py",
            "standardize": "movie_standardizer.py", "std": "movie_standardizer.py",
            "extract": "subtitle_extractor.py", "extract-subs": "subtitle_extractor.py",
            "audio": "audio_standardizer.py", "audiofit": "audio_standardizer.py",
            "ac3": "audio_standardizer.py",
            "clean": "mkv_track_cleaner.py", "remux": "mkv_track_cleaner.py",
            "10bit": "bitdepth.py", "probe": "bitdepth.py",
            "audit": "library_auditor.py",
        }
        for command, script in expected.items():
            with self.subTest(command=command):
                launched: list[tuple[str, list[str]]] = []

                def record(name: str, args: object, _seen: list = launched) -> int:
                    _seen.append((name, list(args)))  # type: ignore[arg-type]
                    return 0

                with mock.patch.object(organize, "delegate_to_script", record), \
                        redirect_stdout(io.StringIO()):
                    organize.main([command, "--dry-run"])
                self.assertEqual(launched, [(script, ["--dry-run"])])

    def test_a_real_delegation_actually_starts_a_child_process(self) -> None:
        """One delegation is run for real: the plumbing, not just the table.

        ``--help`` is answered by the child's own parser and costs no media
        binaries, so this is hermetic while still proving the interpreter, the
        script path and the working directory are right.
        """
        with redirect_stdout(io.StringIO()):
            code = organize.main(["probe", "--help"])
        self.assertEqual(code, 0)

    def test_a_missing_tool_is_reported_as_2_without_starting_anything(self) -> None:
        err = io.StringIO()
        with mock.patch.object(core, "tool_is_available", lambda script: False), \
                mock.patch.object(subprocess, "run", mock.Mock()) as run, \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = organize.main(["clean", "--dry-run"])
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("mkv_track_cleaner.py not found", err.getvalue())

    def test_ctrl_c_while_a_step_runs_is_130(self) -> None:
        """130 is the shell's convention for "killed by SIGINT".

        A wrapper that restarts a failed step must be able to tell an operator
        who pressed Ctrl-C from a step that broke; reporting 0 or 1 for the
        interrupt would make the two indistinguishable.
        """
        out = io.StringIO()

        def interrupted(cmd: object, **kwargs: object) -> None:
            raise KeyboardInterrupt

        with mock.patch.object(subprocess, "run", interrupted), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = organize.main(["run", "--dry-run"])
        self.assertEqual(code, 130)
        self.assertIn("Interrupted by user.", out.getvalue())

    def test_a_tool_that_cannot_be_launched_at_all_is_a_clean_error(self) -> None:
        """No traceback: the operator gets a sentence and exit 2."""
        err = io.StringIO()

        def unlaunchable(cmd: object, **kwargs: object) -> None:
            raise OSError("interpreter vanished")

        with mock.patch.object(subprocess, "run", unlaunchable), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = organize.main(["audit", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("Error launching library_auditor.py: interpreter vanished", err.getvalue())


class SelfTestRunnerTests(unittest.TestCase):
    """``organize test`` - the command whose only job is to report failures."""

    def _run_self_tests(self, *, codes: dict[str, int], stdout: str = "", stderr: str = "",
                        available: object = None) -> tuple[int, str]:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            script = Path(cmd[1]).name if len(cmd) > 1 else "?"
            proc = subprocess.CompletedProcess(cmd, codes.get(script, 0))
            proc.stdout = stdout  # type: ignore[misc]
            proc.stderr = stderr  # type: ignore[misc]
            return proc

        patches = [mock.patch.object(subprocess, "run", fake_run)]
        if available is not None:
            patches.append(mock.patch.object(core, "tool_is_available", available))
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(redirect_stdout(out))
            code = organize.run_all_self_tests()
        return code, out.getvalue()

    def test_passing_self_tests_are_reported_as_passing(self) -> None:
        code, printed = self._run_self_tests(codes={})
        self.assertEqual(code, 0)
        self.assertIn("ALL SELF-TESTS PASSED", printed)
        for script in ("organize.py", "mkv_track_cleaner.py", "pipeline.py"):
            self.assertIn(f"{script:<24}", printed)

    def test_a_failing_self_test_prints_the_childs_own_output_and_exits_1(self) -> None:
        """The failure rendering had never been exercised - only the pass path.

        ``organize test`` exists so an operator can ask "is this machine able
        to run the toolkit?" and get a diagnosis. A FAILED row with no detail
        is the same as no answer at all, so the child's last stdout and stderr
        lines are part of the contract, as is the non-zero exit a wrapper reads.
        """
        code, printed = self._run_self_tests(
            codes={"mkv_track_cleaner.py": 1},
            stdout="noise\nFAIL: the audio ranking disagrees\ntrailing",
            stderr="Traceback (most recent call last):\n  AssertionError",
        )
        self.assertEqual(code, 1)
        self.assertIn("mkv_track_cleaner.py", printed)
        self.assertIn("FAILED (exit code 1)", printed)
        self.assertIn("FAIL: the audio ranking disagrees", printed)
        self.assertIn("AssertionError", printed)
        self.assertIn("1 SELF-TEST(S) FAILED", printed)
        self.assertNotIn("ALL SELF-TESTS PASSED", printed)

    def test_only_the_tail_of_a_noisy_child_is_echoed(self) -> None:
        """Six lines, because the last lines are the ones that name the failure."""
        loud = "\n".join(f"line {index}" for index in range(1, 41))
        code, printed = self._run_self_tests(codes={"bitdepth.py": 2}, stdout=loud)
        self.assertEqual(code, 1)
        self.assertIn("line 40", printed)
        self.assertNotIn("line 1\n", printed)

    def test_a_missing_tool_is_a_failure_not_a_skip(self) -> None:
        """An installation that lost a tool must say so, loudly.

        ``organize test`` is what someone runs after a partial copy to a NAS;
        silently skipping the missing script would report ALL PASSED for a
        toolkit that cannot run.
        """
        code, printed = self._run_self_tests(
            codes={}, available=lambda script: script != "pipeline.py",
        )
        self.assertEqual(code, 1)
        self.assertIn("pipeline.py", printed)
        self.assertIn("Missing file!", printed)
        self.assertIn("1 SELF-TEST(S) FAILED", printed)

    def test_the_internal_self_test_checks_that_the_front_door_can_find_its_steps(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = organize.main(["--internal-self-test"])
        self.assertEqual(code, 0)
        self.assertIn("organize.py internal self-test: OK", out.getvalue())

    def test_the_unit_test_command_is_the_one_the_docs_and_ci_run(self) -> None:
        """``organize test --unit`` must not invent its own discovery flags.

        README, docs/development.md and the CI workflow all quote this exact
        command; a launcher that ran something narrower would pass while the
        real suite failed.
        """
        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.append(list(cmd))
            captured.append([str(kwargs.get("cwd"))])
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(subprocess, "run", fake_run), redirect_stdout(io.StringIO()):
            code = organize.run_unit_tests()
        self.assertEqual(code, 0)
        self.assertEqual(captured[0], [sys.executable, "-m", "unittest", "discover",
                                       "-s", "tests", "-p", "test_*.py"])
        self.assertEqual(Path(captured[1][0]), REPO)

    def test_the_unit_tests_status_is_propagated(self) -> None:
        captured: list[int] = []

        def fake_run(cmd: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
            proc = subprocess.CompletedProcess(cmd, 1)
            captured.append(1)
            return proc

        with mock.patch.object(subprocess, "run", fake_run), redirect_stdout(io.StringIO()):
            code = organize.run_unit_tests()
        self.assertEqual(code, 1)
        self.assertEqual(captured, [1])

    def test_an_installation_without_the_suite_says_so_instead_of_crashing(self) -> None:
        """The scar: an installed package reached discovery and printed an ImportError.

        The suite is developer equipment - it is in neither the wheel nor the
        zipapp - so asking for it there has to answer in prose and exit 0,
        because the toolkit itself is fine.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with mock.patch.object(core, "tools_home", lambda: Path(tmp)), \
                    mock.patch.object(subprocess, "run", mock.Mock()) as run, \
                    redirect_stdout(out):
                code = organize.run_unit_tests()
        self.assertEqual(code, 0)
        run.assert_not_called()
        printed = out.getvalue()
        self.assertIn("not part of the single-file build or the installed package", printed)
        self.assertIn("python3 -m unittest discover -s tests", printed)


class OrganizeTestCommandTests(unittest.TestCase):
    """``organize test`` and ``organize test --unit``."""

    def test_the_test_command_runs_the_self_tests_and_reports_their_status(self) -> None:
        self_tests = mock.Mock(return_value=0)
        unit_tests = mock.Mock(return_value=0)
        with mock.patch.object(organize, "run_all_self_tests", self_tests), \
                mock.patch.object(organize, "run_unit_tests", unit_tests), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(organize.main(["test"]), 0)
        self.assertEqual(self_tests.call_count, 1)
        self.assertEqual(unit_tests.call_count, 0, "--unit was not asked for")

    def test_the_unit_flag_adds_the_repository_suite(self) -> None:
        for flag in ("--unit", "-u"):
            with self.subTest(flag=flag):
                unit_tests = mock.Mock(return_value=0)
                with mock.patch.object(organize, "run_all_self_tests", lambda: 0), \
                        mock.patch.object(organize, "run_unit_tests", unit_tests), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(organize.main(["test", flag]), 0)
                self.assertEqual(unit_tests.call_count, 1)

    def test_a_failed_self_test_is_not_rescued_by_passing_unit_tests(self) -> None:
        """``code or run_unit_tests()``: the first failure is the answer.

        Running the unit suite after a self-test failure is deliberate - it is
        the extra evidence - but it must not be allowed to overwrite a non-zero
        status with its own zero, or ``organize test --unit`` would report
        success for a toolkit whose self-tests failed.
        """
        with mock.patch.object(organize, "run_all_self_tests", lambda: 1), \
                mock.patch.object(organize, "run_unit_tests", lambda: 0), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(organize.main(["test", "--unit"]), 1)

    def test_a_failed_unit_suite_is_reported_even_when_the_self_tests_passed(self) -> None:
        with mock.patch.object(organize, "run_all_self_tests", lambda: 0), \
                mock.patch.object(organize, "run_unit_tests", lambda: 1), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(organize.main(["tests", "--unit"]), 1)


class FrontDoorRenderingTests(unittest.TestCase):
    """The parts of the front door that answer to what the machine can print."""

    def test_the_hero_banner_falls_back_to_ascii_on_a_console_that_cannot_encode_it(self) -> None:
        """The scar: cp437/cp1252 Windows pipes raised UnicodeEncodeError.

        ``organize doctor`` and ``organize test`` died on windows-latest before
        printing a single diagnostic, because the banner is the first thing
        drawn. The ASCII banner must carry the same name and no box drawing.
        """
        out = io.StringIO()
        with mock.patch.object(organize, "_SUPPORTS_UNICODE", False), redirect_stdout(out):
            organize.print_hero_banner()
        printed = out.getvalue()
        self.assertIn("=" * 72, printed)
        self.assertIn("ORGANIZE — Jellyfin Movie Management Toolkit", printed)
        self.assertNotIn("█", printed)
        self.assertIn(f"v{organize.VERSION}", printed)

    def test_the_unicode_banner_is_the_default_where_it_can_be_encoded(self) -> None:
        out = io.StringIO()
        with mock.patch.object(organize, "_SUPPORTS_UNICODE", True), redirect_stdout(out):
            organize.print_hero_banner()
        printed = out.getvalue()
        self.assertIn("█", printed)
        self.assertNotIn("=" * 72, printed)
        self.assertIn(f"v{organize.VERSION}", printed)

    def test_sizes_above_a_terabyte_read_like_the_reports(self) -> None:
        """``status`` prints one library size; TiB is where the loop stops.

        Without the final ``unit == "TiB"`` guard a multi-TiB library divided
        its way off the end of the unit list and printed nothing at all.
        """
        self.assertEqual(organize.human_bytes(1024 ** 4), "1.0 TiB")
        self.assertEqual(organize.human_bytes(int(7.5 * 1024 ** 4)), "7.5 TiB")
        self.assertEqual(organize.human_bytes(1024 ** 5), "1024.0 TiB")
        self.assertEqual(organize.human_bytes(512), "512 B")

    def test_a_binary_version_is_read_from_whichever_stream_answered(self) -> None:
        """Some tools print ``--version`` on stderr; doctor shows either."""
        proc = subprocess.CompletedProcess(["mkvmerge", "--version"], 0)
        proc.stdout = ""
        proc.stderr = "mkvmerge v70.0.0 'Caught A Fire' 64-bit\nmore lines"
        with mock.patch.object(subprocess, "run", lambda *a, **k: proc):
            self.assertEqual(organize.get_binary_version("mkvmerge", "--version"),
                             "mkvmerge v70.0.0 'Caught A Fire' 64-bit")

    def test_a_binary_that_prints_nothing_reports_an_empty_version(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe", "-version"], 0)
        proc.stdout = "\n  \n"
        proc.stderr = ""
        with mock.patch.object(subprocess, "run", lambda *a, **k: proc):
            self.assertEqual(organize.get_binary_version("ffprobe", "-version"), "")

    def test_doctor_reports_ffmpeg_as_ok_when_the_standardizer_can_run_it(self) -> None:
        """The mirror image of the hermetic case: a provisioned machine says so.

        Every other test in the suite pins ffmpeg to "absent", so the OK row -
        the one an operator who did install FFmpeg expects to see - was never
        rendered.
        """
        import audio_standardizer as aus

        ctx = organize.DoctorContext(library=REPO, source=REPO)
        with mock.patch.object(aus, "find_ffmpeg", lambda explicit=None: "/usr/bin/ffmpeg"), \
                mock.patch.object(aus, "binary_works", lambda binary: True), \
                mock.patch.object(organize, "get_binary_version", lambda b, f: "ffmpeg version 6.0"):
            check = organize.check_ffmpeg(ctx)
        self.assertEqual(check.status, "ok")
        self.assertIn("ffmpeg version 6.0", check.message)
        self.assertEqual(check.detail, "/usr/bin/ffmpeg")

    def test_doctor_describes_the_arc_wiring_as_the_alternative_chain(self) -> None:
        """Two wirings are supported and the row has to say which one it found.

        The hardware doc is explicit that HDMI IN is the default and ARC is the
        fallback; a doctor row that describes the wrong topology sends someone
        chasing the wrong cable.
        """
        from organizekit.core import playbackchain as pc

        ctx = organize.DoctorContext(library=REPO, source=REPO)
        with mock.patch.object(pc, "resolve_wiring", lambda explicit=None: pc.WIRING_TV_ARC):
            check = organize.check_playback_chain(ctx)
        self.assertEqual(check.status, "ok")
        self.assertIn("ARC/optical", check.message)
        self.assertIn(pc.SINK.model, check.message)

        with mock.patch.object(pc, "resolve_wiring",
                               lambda explicit=None: pc.WIRING_SOUNDBAR_HDMI_IN):
            default = organize.check_playback_chain(ctx)
        self.assertIn("(HDMI IN)", default.message)
        self.assertNotIn("ARC/optical", default.message)

    def test_the_source_root_default_is_platform_aware(self) -> None:
        """A POSIX machine must never be warned about a literal ``E:\\torrents``.

        The library resolver's platform default has a test that answers
        whichever platform it happens to run on, which on the coverage runner
        means the Windows half is never measured - and the Windows half is the
        one the README documents. Both halves are asserted here explicitly.
        """
        saved = os.environ.pop("MOVIE_STD_SOURCE", None)
        self.addCleanup(lambda: os.environ.__setitem__("MOVIE_STD_SOURCE", saved)
                        if saved is not None else os.environ.pop("MOVIE_STD_SOURCE", None))
        with platforms.windows():
            self.assertEqual(organize._resolve_source_path(None), Path(r"E:\torrents\final"))
        with platforms.posix():
            self.assertEqual(organize._resolve_source_path(None),
                             Path.home() / "torrents" / "final")
        with platforms.windows():
            self.assertEqual(organize._resolve_source_path(Path("/explicit")), Path("/explicit"))
            os.environ["MOVIE_STD_SOURCE"] = "/from/env"
            self.assertEqual(organize._resolve_source_path(None), Path("/from/env"))
            # A whitespace-only --source is no source at all: the resolver
            # falls through to the default rather than handing back a Path
            # built from three spaces (the inline copy this replaced did).
            os.environ.pop("MOVIE_STD_SOURCE", None)
            self.assertEqual(organize._resolve_source_path(Path("   ")),
                             Path(r"E:\torrents\final"))

    def test_status_works_when_the_auditor_cannot_be_imported(self) -> None:
        """``_subtitle_states`` degrades to "no mapping", never to a crash.

        ``organize status`` joins a live audit with the cache; if the auditor
        module is unavailable the subtitle column has nothing to say, but the
        command still has to answer - an exception here would take the whole
        dashboard down for a cosmetic column.
        """
        with mock.patch.dict(sys.modules, {"library_auditor": None}):
            self.assertEqual(organize._subtitle_states(), {})
        real = organize._subtitle_states()
        self.assertTrue(real, "with the auditor present there are states to map")

    def test_a_stream_that_cannot_be_reconfigured_does_not_stop_the_cli(self) -> None:
        """A closed or detached stdout is a real state, and main() starts there.

        ``_reconfigure_stdio_for_windows`` runs before a single argument is
        read; if it raised, ``organize --version`` under a detached console
        (a Windows service, ``nohup`` with stdout closed) would die before
        doing anything.
        """
        class Broken:
            def reconfigure(self, **kwargs: object) -> None:
                raise ValueError("I/O operation on closed file")

            def write(self, text: str) -> int:
                return len(text)

            def flush(self) -> None:
                return None

        with mock.patch.object(sys, "stdout", Broken()), mock.patch.object(sys, "stderr", Broken()):
            organize._reconfigure_stdio_for_windows()  # must not raise

    def test_a_stream_without_reconfigure_is_left_alone(self) -> None:
        class Plain:
            def __init__(self) -> None:
                self.written: list[str] = []

            def write(self, text: str) -> int:
                self.written.append(text)
                return len(text)

            def flush(self) -> None:
                return None

        stream = Plain()
        with mock.patch.object(sys, "stdout", stream), mock.patch.object(sys, "stderr", stream):
            organize._reconfigure_stdio_for_windows()
        self.assertEqual(stream.written, [])


class StatusStampTests(unittest.TestCase):
    """The (size, mtime) stamps ``status`` takes to decide if a verdict is stale."""

    SRT = "1\n00:00:01,000 --> 00:00:02,000\nhello\n\n"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.library = self.root / "lib"
        self.library.mkdir()
        self.db = self.root / "state.db"
        self.addCleanup(self._tmp.cleanup)

    def _movie(self, title: str, *, files: tuple[str, ...] = (".mkv",)) -> Path:
        folder = self.library / title
        folder.mkdir(parents=True, exist_ok=True)
        first = folder / f"{title}{files[0]}"
        first.write_bytes(b"x" * 4096)
        for suffix in files[1:]:
            (folder / f"{title}{suffix}").write_bytes(b"y" * 2048)
        return first

    def _steps(self, *args: str) -> tuple[int, dict[str, dict], str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = organize.main(["status", "--library", str(self.library),
                                  "--state-db", str(self.db), "--json", *args])
        document = json.loads(out.getvalue())
        return code, {step["id"]: step for step in document["steps"]}, out.getvalue()

    def test_a_movie_that_vanishes_mid_scan_never_lends_its_verdict_to_the_next_pass(self) -> None:
        """The race the stamps exist for, run as a real filesystem race.

        The scan and the stamps are two separate walks, so a movie can go
        between them - a torrent client deleting a finished download, an
        operator moving a file. A cached verdict is only trustworthy while the
        bytes it was measured on are still there, so when the stamp cannot be
        taken the answer has to be reported as *stale* and never as a settled
        step. The alternative is `status` telling someone their library is
        finished on the strength of a movie that no longer exists.
        """
        import library_auditor

        movie = self._movie("Gone (2020)")
        with core.open_state(self.db, tool="tests") as store:
            store.record(movie, core.KIND_REMUX, "cleaned")

        code, steps, _ = self._steps()
        self.assertEqual(code, 0)
        self.assertEqual(steps["remux"]["counts"], {"cleaned": 1}, "sanity: the verdict is live")
        self.assertEqual(steps["remux"]["stale"], 0)

        real_audit = library_auditor.audit_library

        def audit_then_vanish(cfg: object) -> object:
            audit = real_audit(cfg)  # type: ignore[arg-type]
            movie.unlink()  # the race: gone after the scan, before the stamps
            return audit

        with mock.patch.object(library_auditor, "audit_library", audit_then_vanish):
            code, steps, raw = self._steps()
        self.assertEqual(code, 0, "an unreadable movie is a stale verdict, not a failed command")
        self.assertEqual(steps["remux"]["stale"], 1)
        self.assertEqual(steps["remux"]["counts"], {}, "an unverifiable stamp proves nothing")
        self.assertEqual(steps["remux"]["settled"], 0)
        self.assertEqual(json.loads(raw)["pending"], 1)

    def test_a_folder_with_more_than_one_video_file_is_a_layout_defect_not_a_movie(self) -> None:
        """Two features in one folder: nothing to key a per-movie verdict on.

        The stamps loop skips it for the same reason the summary does -
        guessing which of the two files a cached verdict described would report
        a movie as finished on the strength of its sibling's history.
        """
        folder = self._movie("Twins (2020)", files=(".mkv", ".mp4")).parent
        with core.open_state(self.db, tool="tests") as store:
            store.record(folder / "Twins (2020).mkv", core.KIND_REMUX, "cleaned")
        code, steps, raw = self._steps()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(raw)["movies"], 0, "a two-file folder is not a movie")
        self.assertEqual(steps["remux"]["counts"], {})
        self.assertEqual(steps["remux"]["recorded"], False)
        self.assertEqual(steps["layout"]["counts"], {"MULTIPLE_DIRECT_MOVIE_FILES": 1})


class ToolchainDeploymentTests(unittest.TestCase):
    """The archive-side branches of the deployment helpers."""

    def test_an_unimportable_script_name_is_unavailable_rather_than_fatal(self) -> None:
        """In the archive ``tool_is_available`` asks the importer, and importers raise.

        ``find_spec`` answers ``ValueError`` for a name that is not a legal
        module name; the whole point of the helper is that a missing tool is a
        skipped step, so it has to come back as False.
        """
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "organize.pyz"
            archive.write_bytes(b"not a real archive")
            with mock.patch.object(toolchain, "TOOLS_DIR", archive):
                self.assertIsNotNone(toolchain.zipapp_path())
                self.assertFalse(toolchain.tool_is_available("not a module.py"))
                self.assertFalse(toolchain.tool_is_available("10bit.py"))

    def test_a_script_name_the_importer_cannot_resolve_is_unavailable_not_fatal(self) -> None:
        """Two ways ``find_spec`` refuses, and both have to answer False.

        A name with a dot in it ("``movie.standardizer.py``") makes the importer
        look for a package that is not there, and a module that is already in
        ``sys.modules`` without a ``__spec__`` - the shape a partially
        initialized or hand-built module has - makes it raise outright. Either
        way the answer is "this deployment cannot run that script", because a
        prerequisite check that raises takes the pipeline down with it.
        """
        import types

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "organize.pyz"
            archive.write_bytes(b"not a real archive")
            specless = types.ModuleType("specless")
            specless.__spec__ = None  # type: ignore[assignment]
            with mock.patch.object(toolchain, "TOOLS_DIR", archive), \
                    mock.patch.dict(sys.modules, {"specless": specless}):
                self.assertFalse(toolchain.tool_is_available("movie.standardizer.py"))
                self.assertFalse(toolchain.tool_is_available("specless.py"))

    def test_out_of_the_archive_availability_is_a_file_on_disk(self) -> None:
        self.assertTrue(toolchain.tool_is_available("mkv_track_cleaner.py"))
        self.assertFalse(toolchain.tool_is_available("no_such_tool.py"))

    def test_the_child_working_directory_is_where_the_tools_are(self) -> None:
        self.assertEqual(toolchain.child_cwd(), toolchain.tools_home())
        self.assertEqual(toolchain.tools_home(), REPO)


if __name__ == "__main__":
    unittest.main()
