"""The two build-and-run entry points that are not tools: the pipeline and the archive.

``pipeline.py`` is the one runner. It starts each step as its own process, so
its own failure modes are about *starting* things: a step whose binary is
missing, a step that cannot be launched at all, a step name nobody defined, and
a library that is not there. Each of those has to be a sentence and an exit code
a wrapper can act on, never a traceback.

``scripts/build_pyz.py`` is the single-file build the README's install story
depends on ("copy one file next to your media"). Its module list comes from
``pyproject.toml`` so the wheel and the archive cannot drift - which makes the
three guards here the interesting part: a declared module or package that is not
on disk, and a missing ``__main__.py``, must stop the build rather than produce
an archive that quietly lacks a tool.

The two benchmarks end by asserting their own invariant - that the answer does
not change with the worker count - and the branch where that assertion *fails*
had never been executed. A benchmark that reports a speed-up by quietly changing
its verdicts is worse than no benchmark, so the failure path is the one worth
proving works.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import pipeline as pl
from organizekit.core import toolchain

REPO = Path(__file__).resolve().parents[1]
BENCHMARKS = REPO / "benchmarks"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def pristine_module(path: Path, name: str) -> types.ModuleType:
    """Load a file as its own module, without touching the one the suite shares.

    ``tests/selftests`` rebinds each tool's ``--self-test`` entry point on the
    imported module, so the shipped field smoke test is unreachable through it.
    A second copy gives the shipped one back.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def load_builder() -> types.ModuleType:
    return pristine_module(REPO / "scripts" / "build_pyz.py", "build_pyz_for_test")


class StepLaunchTests(unittest.TestCase):
    """``run_step``: what the pipeline reports when a step cannot be started."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pipeline_gates_")
        self.library = Path(self._tmp.name) / "Movies"
        self.library.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def test_a_step_that_cannot_be_launched_is_reported_missing(self) -> None:
        """An interpreter that will not start is not the step's failure.

        The status is its own value ("missing", not "ran" with a non-zero code)
        because the summary has to tell "this step broke" apart from "this step
        was never started" - and ``--continue-on-error`` keeps the run going
        either way.
        """
        cfg = pl.Config(library=self.library, steps=("cleaner",))
        with mock.patch.object(pl.subprocess, "run",
                               side_effect=OSError("interpreter vanished")), \
                mock.patch.object(pl, "prerequisite_issue", lambda step: None), \
                redirect_stdout(io.StringIO()):
            result = pl.run_step(pl.STEPS["cleaner"], cfg)
        self.assertEqual(result.status, "missing")
        self.assertIn("could not launch: interpreter vanished", result.detail)
        self.assertIsNone(result.returncode)

    def test_a_step_that_runs_reports_its_own_exit_code(self) -> None:
        cfg = pl.Config(library=self.library, steps=("cleaner",))
        completed = subprocess.CompletedProcess(["python3", "mkv_track_cleaner.py"], 3)
        with mock.patch.object(pl.subprocess, "run", lambda *a, **k: completed), \
                mock.patch.object(pl, "prerequisite_issue", lambda step: None), \
                redirect_stdout(io.StringIO()):
            result = pl.run_step(pl.STEPS["cleaner"], cfg)
        self.assertEqual(result.status, "ran")
        self.assertEqual(result.returncode, 3)

    def test_a_step_with_an_unmet_prerequisite_is_skipped_before_anything_starts(self) -> None:
        """The prerequisite answer is injected, not read off the host.

        A machine that has MKVToolNix installed - the shape this toolkit is
        written for, and one of the CI jobs - *meets* the extractor's
        prerequisite, so a test that relied on the binary being absent would
        launch the step there and assert nothing about skipping. What is under
        test is the ordering: the check happens before the child is built, so a
        step that cannot run never starts one, and the reason it gives is the
        prerequisite's own wording.
        """
        cfg = pl.Config(library=self.library, steps=("extractor",))
        with mock.patch.object(pl.subprocess, "run", mock.Mock()) as run, \
                mock.patch.object(pl, "prerequisite_issue",
                                  lambda step: "mkvextract was not found"), \
                redirect_stdout(io.StringIO()):
            result = pl.run_step(pl.STEPS["extractor"], cfg)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.detail, "mkvextract was not found")
        run.assert_not_called()


class PipelineCommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pipeline_cli_")
        self.library = Path(self._tmp.name) / "Movies"
        self.library.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pl.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_list_steps_says_what_each_step_needs_and_whether_this_machine_has_it(self) -> None:
        """The first thing somebody runs on a new NAS."""
        code, printed, _ = self._main("--list-steps")
        self.assertEqual(code, 0)
        for key in toolchain.STEP_ORDER:
            self.assertIn(key, printed)
        self.assertTrue(any("ready" in line or "blocked:" in line
                            for line in printed.splitlines()), printed)

    def test_an_unknown_step_name_is_a_usage_error_that_lists_the_known_ones(self) -> None:
        code, _out, err = self._main("--steps", "cleanup")
        self.assertEqual(code, 2)
        self.assertIn("Unknown step(s): cleanup", err)
        self.assertIn(", ".join(toolchain.STEP_ORDER), err)

    def test_an_empty_step_selection_is_a_usage_error_not_an_empty_run(self) -> None:
        """A run that did nothing and reported success is the worst outcome."""
        code, _out, err = self._main("--steps", " , ")
        self.assertEqual(code, 2)
        self.assertIn("No steps selected.", err)

    def test_a_library_that_does_not_exist_is_refused_before_any_step_starts(self) -> None:
        code, _out, err = self._main("--source", str(self.library / "nope"), "--steps", "cleaner")
        self.assertEqual(code, 2)
        self.assertIn("Library directory does not exist", err)

    def test_a_dry_run_names_the_commands_it_would_have_started(self) -> None:
        """A dry run has to print the exact argv, quoting and all.

        This is what somebody copies into a cron job or a qBittorrent hook, so a
        path with a space in it has to arrive quoted.
        """
        spaced = self.library.parent / "My Movies"
        spaced.mkdir()
        with mock.patch.object(pl, "prerequisite_issue", lambda step: None):
            code, printed, _ = self._main("--source", str(spaced), "--steps", "cleaner",
                                          "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("mkv_track_cleaner.py", printed)
        self.assertIn(f'"{spaced}"', printed, "a path with a space is quoted")
        self.assertIn("--dry-run", printed)
        self.assertIn("pipeline dry-run", printed)

    def test_the_shipped_self_test_asserts_the_step_order_it_depends_on(self) -> None:
        """Extraction must precede the remux, forever, and the smoke test says so.

        Run against a pristine copy of the module: ``tests/selftests`` rebinds
        this entry point on the shared one, and the shipped field smoke test is
        what a NAS actually runs.
        """
        module = pristine_module(REPO / "pipeline.py", "pipeline_pristine")
        with redirect_stdout(io.StringIO()) as out:
            code = module.run_self_tests()
        printed = out.getvalue()
        self.assertEqual(code, 0, printed)
        self.assertIn("subtitles are extracted before the remux", printed.lower())
        self.assertIn("the audit is the last step", printed.lower())
        self.assertIn("SELF-TEST PASSED", printed)

    def test_the_self_test_flag_reaches_the_shipped_smoke_test(self) -> None:
        module = pristine_module(REPO / "pipeline.py", "pipeline_pristine_main")
        with redirect_stdout(io.StringIO()) as out:
            code = module.main(["--self-test"])
        self.assertEqual(code, 0)
        self.assertIn("pipeline.py", out.getvalue())


class ArchiveBuildTests(unittest.TestCase):
    """``scripts/build_pyz.py``: the one-file install the README promises."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="build_pyz_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.builder = load_builder()

    def _fake_repo(self, *, modules: list[str] = (), packages: list[str] = (),
                   entry: bool = True) -> Path:
        root = self.root / "repo"
        root.mkdir(exist_ok=True)
        # JSON's array syntax is valid TOML, so the list is written with json
        # rather than with a Python repr that would need unquoting.
        body = ("[tool.setuptools]\n"
                f"py-modules = {json.dumps(list(modules))}\n"
                f"packages = {json.dumps(list(packages))}\n")
        (root / "pyproject.toml").write_text(body, encoding="utf-8")
        for module in modules:
            (root / f"{module}.py").write_text("# a tool\n", encoding="utf-8")
        for package in packages:
            (root / package).mkdir(exist_ok=True)
            (root / package / "__init__.py").write_text("", encoding="utf-8")
        if entry:
            (root / "__main__.py").write_text("# the entry point\n", encoding="utf-8")
        return root

    def test_a_declared_module_that_is_not_on_disk_stops_the_build(self) -> None:
        """An archive missing a tool still runs - and fails on the step that needs it.

        The list comes from ``pyproject.toml``, the same list the wheel ships, so
        a rename that lands in one place and not the other has to be caught at
        build time rather than on somebody's NAS.
        """
        root = self._fake_repo(modules=["mkv_track_cleaner"])
        (root / "mkv_track_cleaner.py").unlink()
        staging = self.root / "staging"
        staging.mkdir()
        with self.assertRaises(SystemExit) as caught:
            self.builder.stage(root, staging)
        self.assertIn("pyproject declares mkv_track_cleaner.py but it does not exist",
                      str(caught.exception))

    def test_a_declared_package_that_is_not_on_disk_stops_the_build(self) -> None:
        root = self._fake_repo(packages=["organizekit"])
        shutil.rmtree(root / "organizekit")
        staging = self.root / "staging"
        staging.mkdir()
        with self.assertRaises(SystemExit) as caught:
            self.builder.stage(root, staging)
        self.assertIn("pyproject declares package organizekit but it does not exist",
                      str(caught.exception))

    def test_a_missing_entry_point_stops_the_build(self) -> None:
        """Without ``__main__.py`` the archive is a zip nobody can run."""
        root = self._fake_repo(entry=False)
        staging = self.root / "staging"
        staging.mkdir()
        with self.assertRaises(SystemExit) as caught:
            self.builder.stage(root, staging)
        self.assertIn("__main__.py (the archive's entry point) is missing", str(caught.exception))

    def test_a_staged_tree_carries_the_modules_the_packages_and_the_entry_point(self) -> None:
        root = self._fake_repo(modules=["organize"], packages=["organizekit"])
        staging = self.root / "staging"
        staging.mkdir()
        copied = self.builder.stage(root, staging)
        self.assertEqual({path.name for path in copied}, {"organize.py", "__init__.py",
                                                          "__main__.py"})
        self.assertTrue((staging / "__main__.py").is_file())
        self.assertTrue((staging / "organizekit" / "__init__.py").is_file())

    def test_the_build_command_reports_the_archive_it_wrote(self) -> None:
        output = self.root / "dist" / "organize.pyz"
        with redirect_stdout(io.StringIO()) as out:
            code = self.builder.main(["--output", str(output)])
        self.assertEqual(code, 0)
        self.assertTrue(output.is_file())
        printed = out.getvalue()
        self.assertIn(str(output), printed)
        self.assertIn("KiB", printed)
        self.assertIn("modules", printed)
        self.assertIn(f"python3 {output.name} --help", printed)

    def test_the_real_build_carries_every_tool_the_wheel_ships(self) -> None:
        """The two builds read one list, so this is the drift check that matters."""
        import zipfile

        output = self.root / "dist" / "real.pyz"
        self.builder.build(output)
        with zipfile.ZipFile(output) as bundle:
            names = set(bundle.namelist())
        modules, packages = self.builder.shipped_modules(REPO / "pyproject.toml")
        for module in modules:
            self.assertIn(f"{module}.py", names)
        for package in packages:
            self.assertTrue(any(name.startswith(package.replace(".", "/") + "/")
                                for name in names), package)
        self.assertIn("__main__.py", names)
        self.assertFalse(any(name.startswith("tests/") for name in names),
                         "the offline suite is developer equipment, not a deployment")


class BenchmarkInvariantTests(unittest.TestCase):
    """The benchmarks' own assertion, in the direction that fails."""

    def _load(self, filename: str) -> types.ModuleType:
        return pristine_module(BENCHMARKS / filename, f"benchmark_{Path(filename).stem}_mutated")

    def test_the_audit_benchmark_fails_when_the_verdicts_depend_on_the_worker_count(self) -> None:
        """The numbers are only worth quoting if the answer did not change.

        A parallel audit that reordered or re-classified folders would still
        print a speed-up, so the script compares every worker count's verdicts
        and exits 1 on a difference. That exit is the property; this proves it
        fires.
        """
        import library_auditor as la

        module = self._load("bench_audit_workers.py")
        module.FOLDERS = 2
        module.LATENCY_SECONDS = 0.0
        self.addCleanup(setattr, la.log, "stream", la.log.stream)
        self.addCleanup(setattr, la.log, "file", la.log.file)

        real_timed = module.timed
        calls = {"n": 0}

        def shifting(library: Path, workers: int) -> tuple[float, list]:
            elapsed, states = real_timed(library, workers)
            calls["n"] += 1
            if calls["n"] > 1:
                states = [(name, "SOMETHING_ELSE") for name, _ in states]
            return elapsed, states

        err = io.StringIO()
        with mock.patch.object(module, "timed", shifting), \
                contextlib.redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = module.main()
        self.assertEqual(code, 1)
        self.assertIn("FAIL: the audit changed with the worker count", err.getvalue())

    def test_the_triage_benchmark_fails_when_the_verdicts_depend_on_the_worker_count(self) -> None:
        module = self._load("bench_triage_workers.py")
        module.MOVIES = 2
        module.LATENCY_SECONDS = 0.0

        real_timed = module.timed
        calls = {"n": 0}

        def shifting(*args: object, **kwargs: object) -> tuple:
            elapsed, verdicts = real_timed(*args, **kwargs)  # type: ignore[arg-type]
            calls["n"] += 1
            if calls["n"] > 1:
                verdicts = [(name, "changed") for name, _ in verdicts]
            return elapsed, verdicts

        err = io.StringIO()
        with mock.patch.object(module, "timed", shifting), \
                contextlib.redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = module.main()
        self.assertEqual(code, 1)
        self.assertIn("FAIL: the triage verdicts changed with the worker count", err.getvalue())

    def test_both_benchmarks_still_pass_on_an_unchanged_library(self) -> None:
        """The failure above has to be about the mutation, not about the harness."""
        import library_auditor as la

        module = self._load("bench_audit_workers.py")
        module.FOLDERS = 2
        module.LATENCY_SECONDS = 0.0
        self.addCleanup(setattr, la.log, "stream", la.log.stream)
        self.addCleanup(setattr, la.log, "file", la.log.file)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(module.main(), 0)
        self.assertIn("identical audit", out.getvalue())


if __name__ == "__main__":
    unittest.main()
