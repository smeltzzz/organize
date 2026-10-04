"""The CLI and orchestration layer's answer to a bad day.

``organize.py``, ``pipeline.py``, ``__main__.py`` and the archive builder are
the surfaces a user meets first and the ones that must never show a traceback:
a missing tool, a child that will not launch, a command that was mistyped, a
step list that names nothing. Each test pins the *decision* - the exit code,
the message on the right stream, the step that was skipped rather than run.
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import organize
import pipeline

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class _Result:
    """The part of ``subprocess.CompletedProcess`` this code reads."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = organize.main(argv)
    return code, out.getvalue(), err.getvalue()


class CliRenderingEdgeTests(unittest.TestCase):
    """The helpers around the CLI degrade rather than crash on a bare console."""

    def test_the_ascii_banner_is_used_when_the_console_cannot_take_the_glyphs(self) -> None:
        """cp437 pipes cannot encode the block banner; the fallback is the
        banner, not a UnicodeEncodeError before any work starts."""
        out = io.StringIO()
        with mock.patch.object(organize, "_SUPPORTS_UNICODE", False), \
                contextlib.redirect_stdout(out):
            organize.print_hero_banner()
        self.assertIn("ORGANIZE", out.getvalue())
        self.assertIn("=" * 72, out.getvalue())

    def test_the_colour_helpers_pass_their_text_through(self) -> None:
        self.assertIn("hello", organize.blue("hello"))
        self.assertIn("hello", organize.magenta("hello"))

    def test_a_binary_that_answers_with_nothing_reports_nothing(self) -> None:
        with mock.patch.object(organize.subprocess, "run", return_value=_Result(0, "", "")):
            self.assertEqual("", organize.get_binary_version("/usr/bin/nothing"))

    def test_the_source_root_default_is_platform_aware(self) -> None:
        """A POSIX host must never be pointed at a literal ``E:\\torrents``."""
        with mock.patch.object(organize, "load_dotenv"), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(organize.os, "name", "nt"), \
                mock.patch.object(organize, "Path", _PureWindowsPathForTest):
            self.assertEqual(r"E:\torrents\final", str(organize._resolve_source_path(None)))

    def test_ffmpeg_present_is_reported_as_ok(self) -> None:
        import audio_standardizer as aus

        with mock.patch.object(aus, "find_ffmpeg", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(aus, "binary_works", return_value=True), \
                mock.patch.object(organize, "get_binary_version", return_value="ffmpeg 7"):
            check = organize.check_ffmpeg(None)
        self.assertEqual("ok", check.status)

    def test_the_alternative_wiring_is_named_as_such(self) -> None:
        from organizekit.core import playbackchain as pc

        with mock.patch.object(pc, "resolve_wiring", return_value=pc.WIRING_TV_ARC):
            check = organize.check_playback_chain(None)
        self.assertIn("alternative wiring", check.message)

    def test_subtitle_states_are_empty_without_the_auditor(self) -> None:
        """`status` may not take the whole command down because one sibling
        refuses to import; the mapping degrades to 'nothing to map'."""
        with mock.patch.dict(sys.modules, {"library_auditor": None}):
            self.assertEqual({}, organize._subtitle_states())


class _PureWindowsPathForTest(pathlib.PureWindowsPath):
    """A Windows path that can be built (and compared) on any host."""


class DelegateEdgeTests(unittest.TestCase):
    """Delegation is a process launch, and a launch can fail in three ways."""

    def test_a_missing_tool_is_named_rather_than_crashing(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        from organizekit import core

        with mock.patch.object(core, "tool_is_available", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = organize.delegate_to_script("bitdepth.py", [])
        self.assertEqual(2, code)
        self.assertIn("bitdepth.py not found", err.getvalue())

    def test_a_child_that_cannot_be_launched_is_reported_not_raised(self) -> None:
        from organizekit import core

        with mock.patch.object(core, "tool_is_available", return_value=True), \
                mock.patch.object(core, "tool_command", return_value=["python3", "x.py"]), \
                mock.patch.object(core, "tools_home", return_value=REPO), \
                mock.patch.object(organize.subprocess, "run",
                                  side_effect=OSError("no such interpreter")):
            code, _out, err = _run_main(["probe", "--self-test"])
        self.assertEqual(2, code)
        self.assertIn("Error launching", err)

    def test_an_interrupt_during_a_child_is_reported_as_130(self) -> None:
        from organizekit import core

        with mock.patch.object(core, "tool_is_available", return_value=True), \
                mock.patch.object(core, "tool_command", return_value=["python3", "x.py"]), \
                mock.patch.object(core, "tools_home", return_value=REPO), \
                mock.patch.object(organize.subprocess, "run", side_effect=KeyboardInterrupt):
            code = organize.delegate_to_script("bitdepth.py", [])
        self.assertEqual(130, code)

    def test_a_successful_child_returns_its_exit_code(self) -> None:
        from organizekit import core

        with mock.patch.object(core, "tool_is_available", return_value=True), \
                mock.patch.object(core, "tool_command", return_value=["python3", "x.py"]), \
                mock.patch.object(core, "tools_home", return_value=REPO), \
                mock.patch.object(organize.subprocess, "run", return_value=_Result(7)):
            self.assertEqual(7, organize.delegate_to_script("bitdepth.py", []))


class SelfTestRunnerTests(unittest.TestCase):
    """`organize test` runs every tool's own smoke test and has to survive the
    two answers that are not success: a tool that is not there and a tool that
    fails with output worth showing."""

    def test_a_missing_tool_is_counted_as_a_failure(self) -> None:
        from organizekit import core

        with mock.patch.object(core, "tool_is_available", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            code = organize.run_all_self_tests()
        self.assertEqual(1, code)

    def test_a_failing_tool_has_its_output_quoted_and_the_run_fails(self) -> None:
        from organizekit import core

        with mock.patch.object(core, "tool_is_available", return_value=True), \
                mock.patch.object(core, "tool_command", return_value=["python3", "x.py"]), \
                mock.patch.object(core, "tools_home", return_value=REPO), \
                mock.patch.object(organize.subprocess, "run",
                                  return_value=_Result(1, "line one\nline two", "boom")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = organize.run_all_self_tests()
        self.assertEqual(1, code)
        self.assertIn("line two", out.getvalue())
        self.assertIn("boom", out.getvalue())

    def test_the_unit_runner_says_so_when_it_is_not_in_a_checkout(self) -> None:
        """An installed package used to hand the operator an ImportError
        traceback from unittest's discoverer."""
        from organizekit import core

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(core, "tools_home", return_value=Path(td)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = organize.run_unit_tests()
        self.assertEqual(0, code)
        self.assertIn("not part of the single-file build", out.getvalue())

    def test_a_checkout_runs_the_suite_in_place_and_returns_its_code(self) -> None:
        from organizekit import core

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(core, "tools_home", return_value=Path(td)), \
                mock.patch.object(organize.subprocess, "run", return_value=_Result(0)) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            (Path(td) / "tests").mkdir()
            code = organize.run_unit_tests()
        self.assertEqual(0, code)
        self.assertIn("unittest", " ".join(run.call_args.args[0]))

    def test_the_internal_self_test_confirms_the_pipeline_is_reachable(self) -> None:
        code, out, _err = _run_main(["--internal-self-test"])
        self.assertEqual(0, code)
        self.assertIn("internal self-test: OK", out)


class CommandDispatchTests(unittest.TestCase):
    """Every documented alias must reach the script it names.

    The dispatch table is the CLI's contract with the docs; a script renamed in
    one place and not the other is a command that silently does nothing.
    """

    def test_each_alias_delegates_to_its_script(self) -> None:
        expected = {
            "run": "pipeline.py",
            "pipeline": "pipeline.py",
            "standardize": "movie_standardizer.py",
            "std": "movie_standardizer.py",
            "extract": "subtitle_extractor.py",
            "audio": "audio_standardizer.py",
            "clean": "mkv_track_cleaner.py",
            "remux": "mkv_track_cleaner.py",
            "10bit": "bitdepth.py",
            "probe": "bitdepth.py",
            "audit": "library_auditor.py",
        }
        for command, script in expected.items():
            with self.subTest(command=command):
                calls: list[tuple[str, list[str]]] = []

                def record(script_name: str, args: object, _calls: list = calls) -> int:
                    _calls.append((script_name, list(args)))  # type: ignore[arg-type]
                    return 0

                with mock.patch.object(organize, "delegate_to_script", side_effect=record):
                    code = organize.main([command, "--dry-run"])
                self.assertEqual(0, code)
                self.assertEqual([(script, ["--dry-run"])], calls)

    def test_the_test_command_runs_the_smoke_tests_and_optionally_the_unit_suite(self) -> None:
        with mock.patch.object(organize, "run_all_self_tests", return_value=0) as smoke, \
                mock.patch.object(organize, "run_unit_tests", return_value=0) as unit:
            self.assertEqual(0, organize.main(["test"]))
            smoke.assert_called_once()
            unit.assert_not_called()
        with mock.patch.object(organize, "run_all_self_tests", return_value=0), \
                mock.patch.object(organize, "run_unit_tests", return_value=1):
            self.assertEqual(1, organize.main(["test", "--unit"]))

    def test_help_and_version_stay_off_the_dispatch_table(self) -> None:
        code, out, _err = _run_main(["--help"])
        self.assertEqual(0, code)
        self.assertIn("Unified Jellyfin", out)
        code, out, _err = _run_main(["--version"])
        self.assertEqual(0, code)
        self.assertIn(organize.VERSION, out)

    def test_stdio_reconfiguration_failures_are_survivable(self) -> None:
        class Stubborn:
            def reconfigure(self, **kwargs: object) -> None:
                raise OSError("detached")

        with mock.patch.object(organize.sys, "stdout", Stubborn()), \
                mock.patch.object(organize.sys, "stderr", Stubborn()):
            organize._reconfigure_stdio_for_windows()


class StatusScanEdgeTests(unittest.TestCase):
    """`status` joins a live scan with the cache, and the join has to survive
    the shapes a real library contains: a folder that is not one movie, and a
    movie that cannot be stat()ed."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="cli_status_")
        self.addCleanup(self._td.cleanup)
        self.library = Path(self._td.name) / "lib"
        self.library.mkdir(parents=True)

    def _movie(self, title: str, name: str | None = None) -> Path:
        folder = self.library / title
        folder.mkdir(parents=True, exist_ok=True)
        movie = folder / (name or f"{title}.mkv")
        movie.write_bytes(b"x" * 128)
        return movie

    def _status(self, *extra: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return organize.main(["status", "--library", str(self.library), "--no-state", *extra])

    def test_a_folder_holding_several_movies_is_left_out_of_the_stamp_map(self) -> None:
        folder = self.library / "Multi (2000)"
        folder.mkdir()
        (folder / "Multi (2000) - 1080p.mkv").write_bytes(b"x" * 64)
        (folder / "Multi (2000) - 2160p.mkv").write_bytes(b"x" * 64)
        self._movie("Single (2001)")
        self.assertEqual(0, self._status())

    def test_a_movie_deleted_mid_scan_is_not_an_error(self) -> None:
        """The scan lists a movie, something removes it before the stamp is
        taken, and `status` still answers - with an unstamped row."""
        import library_auditor

        self._movie("Single (2001)")
        self._movie("Other (2002)")
        real_audit = library_auditor.audit_library

        def audit_then_delete(cfg: object) -> object:
            audit = real_audit(cfg)  # type: ignore[arg-type]
            for item in audit.folders:
                if len(item.movie_files) == 1:
                    (item.folder / item.movie_files[0].name).unlink()
                    break
            return audit

        with mock.patch.object(library_auditor, "audit_library", audit_then_delete):
            self.assertEqual(0, self._status())

    def test_a_library_that_is_not_there_exits_2(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            code = organize.main(["status", "--library", str(self.library / "missing")])
        self.assertEqual(2, code)


class PipelineEdgeTests(unittest.TestCase):
    """The pipeline reports what it can and cannot run; it never guesses."""

    def test_listing_the_steps_names_every_step_and_its_state(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pipeline.main(["--list-steps"])
        self.assertEqual(0, code)
        for key in pipeline.STEP_ORDER:
            self.assertIn(key, out.getvalue())
        self.assertTrue("ready" in out.getvalue() or "blocked:" in out.getvalue())

    def test_an_unknown_step_is_refused_with_the_known_ones(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = pipeline.main(["--steps", "nope"])
        self.assertEqual(2, code)
        self.assertIn("Unknown step(s): nope", err.getvalue())

    def test_selecting_no_steps_is_refused(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = pipeline.main(["--steps", " , "])
        self.assertEqual(2, code)
        self.assertIn("No steps selected", err.getvalue())

    def test_a_missing_library_is_refused_before_anything_runs(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = pipeline.main(["--steps", "auditor", "--source",
                                  str(Path(tempfile.gettempdir()) / "no-such-organize-lib")])
        self.assertEqual(2, code)
        self.assertIn("does not exist", err.getvalue())

    def test_a_step_that_cannot_launch_is_reported_as_missing(self) -> None:
        import organizekit.core as core

        step = pipeline.STEPS["auditor"]
        cfg = pipeline.Config(library=REPO, steps=["auditor"])
        with mock.patch.object(core, "prerequisite_issue", return_value=None), \
                mock.patch.object(pipeline.subprocess, "run",
                                  side_effect=OSError("exec format error")), \
                contextlib.redirect_stdout(io.StringIO()):
            result = pipeline.run_step(step, cfg)
        self.assertEqual("missing", result.status)
        self.assertIn("could not launch", result.detail)

    def test_the_pipelines_own_smoke_test_pins_the_load_bearing_order(self) -> None:
        """``tests/selftests`` rebinds ``pipeline.run_self_tests`` at import, so
        the shipped definition is exercised through a fresh load of the file -
        the same source the archive ships, lines attributed to it as they run."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("pipeline_from_source", REPO / "pipeline.py")
        assert spec is not None and spec.loader is not None
        fresh = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = fresh
        spec.loader.exec_module(fresh)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fresh.run_self_tests()
        self.assertEqual(0, code)
        self.assertIn("SELF-TEST PASSED", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fresh.main(["--self-test"])
        self.assertEqual(0, code)
        self.assertIn("SELF-TEST PASSED", out.getvalue())


class ZipappBuilderTests(unittest.TestCase):
    """The archive is the install story for a machine with nothing on it, so
    its build must fail loudly when a declared module is not there rather than
    shipping an archive that cannot start."""

    def _load(self) -> types.ModuleType:
        import importlib.util

        path = REPO / "scripts" / "build_pyz.py"
        spec = importlib.util.spec_from_file_location("build_pyz_under_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_main_builds_an_archive_and_reports_its_size(self) -> None:
        build_pyz = self._load()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "organize.pyz"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = build_pyz.main(["--output", str(target)])
            self.assertEqual(0, code)
            self.assertTrue(target.is_file())
            self.assertIn("organize.pyz", out.getvalue())

    def _root(self, td: str, *, modules: tuple[str, ...], packages: tuple[str, ...],
              entry: bool = True) -> Path:
        root = Path(td) / "root"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            "[tool.setuptools]\n"
            f"py-modules = [{', '.join(repr(m) for m in modules)}]\n"
            f"packages = [{', '.join(repr(p) for p in packages)}]\n",
            encoding="utf-8",
        )
        for module in modules:
            (root / f"{module}.py").write_text("x = 1\n", encoding="utf-8")
        for package in packages:
            (root / package).mkdir()
            (root / package / "__init__.py").write_text("", encoding="utf-8")
        if entry:
            (root / "__main__.py").write_text("print('hi')\n", encoding="utf-8")
        return root

    def test_a_module_the_wheel_ships_but_the_tree_lacks_stops_the_build(self) -> None:
        build_pyz = self._load()
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td, modules=("present",), packages=())
            (root / "pyproject.toml").write_text(
                '[tool.setuptools]\npy-modules = ["absent"]\npackages = []\n', encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                build_pyz.stage(root, root)
            self.assertIn("absent.py", str(caught.exception))

    def test_a_package_the_wheel_ships_but_the_tree_lacks_stops_the_build(self) -> None:
        build_pyz = self._load()
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td, modules=(), packages=("gone",))
            import shutil

            shutil.rmtree(root / "gone")
            (root / "pyproject.toml").write_text(
                '[tool.setuptools]\npy-modules = []\npackages = ["gone"]\n', encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                build_pyz.stage(root, root)
            self.assertIn("gone", str(caught.exception))

    def test_a_tree_without_the_archive_entry_point_stops_the_build(self) -> None:
        build_pyz = self._load()
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td, modules=(), packages=(), entry=False)
            with self.assertRaises(SystemExit) as caught:
                build_pyz.stage(root, root)
            self.assertIn("__main__.py", str(caught.exception))


class SingleFileEntryPointTests(unittest.TestCase):
    """``__main__.py`` is how the archive runs a tool: it must refuse a command
    it does not know and pass through exactly the argv the tool expects."""

    def setUp(self) -> None:
        import importlib.util

        path = REPO / "__main__.py"
        spec = importlib.util.spec_from_file_location("organize_zipapp_entry", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.main = module

    def test_no_tool_named_is_a_usage_error(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = self.main.run_tool([])
        self.assertEqual(2, code)
        self.assertIn("usage:", err.getvalue())

    def test_an_unknown_tool_is_refused_with_the_list(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = self.main.run_tool(["--help"])
        self.assertEqual(2, code)
        self.assertIn("unknown tool", err.getvalue())

    def test_a_known_tool_is_imported_by_module_name_and_given_its_argv(self) -> None:
        """The child must see exactly the argv it would have seen as a script
        on disk - the tool's own parser prints the program name from it."""
        module = types.ModuleType("bitdepth")
        seen: dict[str, object] = {}

        def fake_main() -> int:
            seen["argv"] = list(sys.argv)
            return 5

        module.main = fake_main  # type: ignore[attr-defined]
        original = sys.argv
        try:
            with mock.patch.dict(sys.modules, {"bitdepth": module}):
                code = self.main.run_tool(["bitdepth.py", "--self-test"])
        finally:
            sys.argv = original
        self.assertEqual(5, code)
        self.assertEqual(["bitdepth.py", "--self-test"], seen["argv"])

    def test_the_entry_point_dispatches_its_verb(self) -> None:
        with mock.patch.object(self.main, "run_tool", return_value=3) as run:
            self.assertEqual(3, self.main.main([self.main.RUN_TOOL_VERB, "bitdepth.py"]))
        run.assert_called_once_with(["bitdepth.py"])

    def test_without_the_verb_the_cli_gets_the_argv(self) -> None:
        seen: list[list[str]] = []
        fake_organize = types.ModuleType("organize")
        fake_organize.main = lambda argv=None: seen.append(list(argv or [])) or 0  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"organize": fake_organize}):
            original = sys.argv
            try:
                code = self.main.main(["doctor"])
            finally:
                sys.argv = original
        self.assertEqual(0, code)
        self.assertEqual([["doctor"]], seen)


class BenchmarkVerdictTests(unittest.TestCase):
    """Both benchmarks end with an assertion about themselves; the failure
    branch is the one that has to print and exit non-zero rather than pass."""

    def _load(self, filename: str) -> types.ModuleType:
        import importlib.util

        path = REPO / "benchmarks" / filename
        spec = importlib.util.spec_from_file_location(f"bench_{filename}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_the_audit_benchmark_reports_changed_verdicts_as_a_failure(self) -> None:
        import library_auditor as la

        module = self._load("bench_audit_workers.py")
        module.FOLDERS = 2
        module.LATENCY_SECONDS = 0.0
        calls = {"n": 0}

        def timed(library: Path, workers: int) -> tuple[float, list[tuple[str, str]]]:
            calls["n"] += 1
            return 0.01, [("Folder", f"state-{calls['n']}")]

        self.addCleanup(setattr, la.log, "stream", la.log.stream)
        self.addCleanup(setattr, la.log, "file", la.log.file)
        err = io.StringIO()
        with mock.patch.object(module, "timed", timed), \
                mock.patch.object(module, "build_library", return_value=Path(".")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = module.main()
        self.assertEqual(1, code)
        self.assertIn("changed with the worker count", err.getvalue())

    def test_the_triage_benchmark_reports_changed_verdicts_as_a_failure(self) -> None:
        module = self._load("bench_triage_workers.py")
        module.MOVIES = 2
        module.LATENCY_SECONDS = 0.0
        calls = {"n": 0}

        def timed(library: Path, videos: list, workers: int) -> tuple[float, list[tuple[str, str]]]:
            calls["n"] += 1
            return 0.01, [("Movie", f"state-{calls['n']}")]

        err = io.StringIO()
        with mock.patch.object(module, "timed", timed), \
                mock.patch.object(module, "build_library", return_value=(Path("."), [])), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = module.main()
        self.assertEqual(1, code)
        self.assertIn("changed with the worker count", err.getvalue())


if __name__ == "__main__":
    unittest.main()
