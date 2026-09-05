"""The single-file build: does the whole toolkit still work as one file?

The toolkit's home is a NAS or a home server, and the install story there is
"copy one file next to your media". That build is only worth shipping if it is
the *same* toolkit, so these tests run the real archive as a subprocess and ask
it to do real work — including the thing most likely to break in a zipapp, and
the reason the toolkit needed a launcher abstraction at all: starting one of
its own tools as a child process when there is no script file on disk to point
an interpreter at.

They also hold two claims the README makes: the archive carries no third-party
code (every import in it is either the standard library or another member of
the archive), and it is byte-for-byte reproducible from the same source.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import tomllib
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from organizekit import VERSION  # noqa: E402
from organizekit.core import toolchain  # noqa: E402


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_pyz", REPO / "scripts" / "build_pyz.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()

_WORKSPACE: TemporaryDirectory | None = None
ARCHIVE: Path


def setUpModule() -> None:
    """Build the archive once; every test in this file runs that one file."""
    global _WORKSPACE, ARCHIVE
    _WORKSPACE = TemporaryDirectory()
    ARCHIVE = BUILDER.build(Path(_WORKSPACE.name) / "organize.pyz")


def tearDownModule() -> None:
    if _WORKSPACE is not None:
        _WORKSPACE.cleanup()


def run_archive(*args: str, cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ARCHIVE), *args],
        capture_output=True, encoding="utf-8", errors="replace",
        check=False, cwd=str(cwd) if cwd else None, timeout=timeout,
    )


class TheArchiveRunsTests(unittest.TestCase):
    def test_it_reports_the_same_version_as_the_package(self) -> None:
        result = run_archive("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(VERSION, result.stdout)

    def test_every_tool_passes_its_own_field_smoke_test_inside_the_archive(self) -> None:
        # `organize test` starts all nine tools as child processes. In a
        # checkout that is `python bitdepth.py --self-test`; here there is no
        # bitdepth.py on disk, so it has to re-enter the archive instead. If
        # the launcher is wrong, this is what says so.
        result = run_archive("test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ALL SELF-TESTS PASSED", result.stdout)
        self.assertNotIn("Missing file", result.stdout)

    def test_a_pipeline_step_really_runs_from_inside_the_archive(self) -> None:
        with TemporaryDirectory() as tmp:
            library = Path(tmp) / "lib" / "Some Movie (2020)"
            library.mkdir(parents=True)
            (library / "Some Movie (2020).mkv").write_bytes(b"\x00" * 4096)
            result = run_archive("run-tool", "pipeline.py",
                                 "--source", str(library.parent), "--steps", "auditor",
                                 cwd=Path(tmp))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RAN", result.stdout)
        self.assertIn("auditor", result.stdout)

    def test_the_offline_suite_is_not_pretended_to_be_there(self) -> None:
        result = run_archive("test", "--unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not part of the single-file build", result.stdout)

    def test_an_unknown_tool_is_refused(self) -> None:
        result = run_archive("run-tool", "os.py")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown tool", result.stderr)

    def test_run_tool_without_a_tool_explains_itself(self) -> None:
        result = run_archive("run-tool")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)


class WhatIsInTheArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        with zipfile.ZipFile(ARCHIVE) as bundle:
            self.names = bundle.namelist()

    def test_it_ships_exactly_what_the_wheel_ships(self) -> None:
        data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        modules = data["tool"]["setuptools"]["py-modules"]
        for module in modules:
            with self.subTest(module=module):
                self.assertIn(f"{module}.py", self.names)
        self.assertIn("__main__.py", self.names)
        self.assertIn("organizekit/core/toolchain.py", self.names)

    def test_it_does_not_ship_the_developer_equipment(self) -> None:
        for unwanted in ("tests/", "benchmarks/", "docs/", "__pycache__/", ".git"):
            with self.subTest(unwanted=unwanted):
                self.assertFalse([name for name in self.names if name.startswith(unwanted)])
        self.assertFalse([name for name in self.names if name.endswith(".pyc")])

    def test_it_carries_no_third_party_code(self) -> None:
        """Every import in the archive resolves to the stdlib or to the archive.

        This is the "zero runtime dependencies" claim, checked rather than
        repeated: a single `import requests` anywhere in the toolkit would make
        the single-file build a lie on a machine that has only Python.
        """
        with zipfile.ZipFile(ARCHIVE) as bundle:
            sources = {name: bundle.read(name).decode("utf-8")
                       for name in bundle.namelist() if name.endswith(".py")}
        shipped = {name[:-3].replace("/", ".") for name in sources}
        shipped |= {name.split("/")[0] for name in sources if "/" in name}

        offenders: list[str] = []
        for name, text in sources.items():
            for node in ast.walk(ast.parse(text, filename=name)):
                roots: list[str] = []
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = [node.module.split(".")[0]]
                for root in roots:
                    if root in sys.stdlib_module_names or root in shipped:
                        continue
                    offenders.append(f"{name}: {root}")
        self.assertEqual(offenders, [])

    def test_the_same_source_builds_the_same_bytes(self) -> None:
        # Sorted entries and a pinned timestamp: two builds of one source are
        # the same file, so a published checksum means something.
        with TemporaryDirectory() as tmp:
            rebuild = BUILDER.build(Path(tmp) / "again.pyz")
            self.assertEqual(rebuild.read_bytes(), ARCHIVE.read_bytes())


class LauncherRulesTests(unittest.TestCase):
    """The rules that make one toolkit work in two deployments.

    Exercised directly, without building anything: the archive case is what
    `TOOLS_DIR` pointing at a *file* means.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.archive = self.home / "organize.pyz"
        self.archive.write_bytes(b"not really a zip, but it is a file")

    def as_archive(self):
        return mock.patch.object(toolchain, "TOOLS_DIR", self.archive)

    def test_a_checkout_points_the_interpreter_at_the_script(self) -> None:
        command = toolchain.tool_command("bitdepth.py", ["--self-test"])
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[1]).name, "bitdepth.py")
        self.assertEqual(command[2:], ["--self-test"])
        self.assertIsNone(toolchain.zipapp_path())

    def test_the_archive_re_enters_itself(self) -> None:
        with self.as_archive():
            command = toolchain.tool_command("bitdepth.py", ["--self-test"])
        self.assertEqual(command, [sys.executable, str(self.archive),
                                   "run-tool", "bitdepth.py", "--self-test"])

    def test_a_script_dir_that_is_the_archive_means_the_archive(self) -> None:
        # jellyfin_one_shot's --script-dir defaults to "next to me", which
        # inside the archive is the archive; it must not become a path join.
        with self.as_archive():
            command = toolchain.tool_command("bitdepth.py", script_dir=self.archive)
            self.assertEqual(command[1:3], [str(self.archive), "run-tool"])
            self.assertTrue(toolchain.tool_is_available("bitdepth.py", script_dir=self.archive))
            self.assertFalse(toolchain.tool_is_available("not_a_tool.py"))
            self.assertEqual(toolchain.missing_tool_scripts(self.archive), [])

    def test_an_explicit_script_dir_still_wins(self) -> None:
        elsewhere = self.home / "toolkit"
        elsewhere.mkdir()
        (elsewhere / "bitdepth.py").write_text("", encoding="utf-8")
        with self.as_archive():
            command = toolchain.tool_command("bitdepth.py", script_dir=elsewhere)
            self.assertEqual(command, [sys.executable, str(elsewhere / "bitdepth.py")])
            self.assertTrue(toolchain.tool_is_available("bitdepth.py", script_dir=elsewhere))
            self.assertEqual(toolchain.child_cwd(elsewhere), elsewhere)

    def test_children_never_run_inside_the_archive(self) -> None:
        # A .pyz is a file: using it as a working directory raises
        # NotADirectoryError, and a log directory under it cannot be created.
        with self.as_archive():
            self.assertEqual(toolchain.tools_home(), self.home)
            self.assertEqual(toolchain.child_cwd(), self.home)
            self.assertEqual(toolchain.child_cwd(self.archive), self.home)
        self.assertTrue(toolchain.child_cwd().is_dir())


if __name__ == "__main__":
    unittest.main()
