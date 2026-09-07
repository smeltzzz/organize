"""What `pip install` gets, checked without building anything.

Packaging fails quietly. A tool added at the repository root and not added to
`py-modules` still works in the checkout, still works in the zipapp - the
builder reads the same list - and is simply absent from the wheel, where the
first symptom is an ImportError on somebody else's machine. A support file the
test suite imports and the sdist does not carry produces a distribution whose
tests cannot run, which is worse than one that ships no tests at all.

So these read the declarations - `pyproject.toml`, `MANIFEST.in` - and hold
them against the files on disk. They are offline and take milliseconds; the
end-to-end proof (build both artifacts, install the wheel, run the sdist's own
suite) is the release checklist in `docs/development.md`.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

import organize
from organizekit import VERSION

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PROJECT = PYPROJECT["project"]
SETUPTOOLS = PYPROJECT["tool"]["setuptools"]

# Root-level `.py` files that are deliberately not wheel modules.
NOT_A_TOOL = {
    "__main__.py",  # the zipapp's entry point; a wheel has a console script instead
}


class WhatTheWheelCarriesTests(unittest.TestCase):
    def test_every_tool_at_the_root_is_declared(self) -> None:
        """The drift a new tool causes: it works in the checkout and is absent from the wheel."""
        on_disk = {p.stem for p in ROOT.glob("*.py") if p.name not in NOT_A_TOOL}
        self.assertEqual(on_disk, set(SETUPTOOLS["py-modules"]))

    def test_nothing_is_declared_that_does_not_exist(self) -> None:
        for module in SETUPTOOLS["py-modules"]:
            with self.subTest(module=module):
                self.assertTrue((ROOT / f"{module}.py").is_file())
        for package in SETUPTOOLS["packages"]:
            with self.subTest(package=package):
                self.assertTrue((ROOT / Path(*package.split("."))).is_dir())

    def test_every_shared_subpackage_is_declared(self) -> None:
        """`packages` is not recursive: an un-listed subpackage ships as nothing."""
        found = {
            ".".join(p.parent.relative_to(ROOT).parts)
            for p in ROOT.glob("organizekit/**/__init__.py")
        }
        self.assertEqual(found, set(SETUPTOOLS["packages"]))

    def test_the_command_is_organize_whatever_the_distribution_is_called(self) -> None:
        """`organize` was taken on PyPI in 2011; the command people type is not."""
        self.assertEqual(list(PROJECT["scripts"]), ["organize"])
        self.assertEqual(PROJECT["scripts"]["organize"], "organize:main")

    def test_the_console_script_points_at_something_callable(self) -> None:
        module, _, attribute = PROJECT["scripts"]["organize"].partition(":")
        self.assertEqual(module, organize.__name__)
        self.assertTrue(callable(getattr(organize, attribute)))

    def test_the_wheel_has_no_third_party_dependencies(self) -> None:
        """The promise on the front page, and the reason the zipapp can exist."""
        self.assertEqual(PROJECT.get("dependencies", []), [])


class OneVersionTests(unittest.TestCase):
    def test_the_version_is_read_from_the_package_not_repeated(self) -> None:
        self.assertEqual(PROJECT["dynamic"], ["version"])
        self.assertEqual(SETUPTOOLS["dynamic"]["version"], {"attr": "organizekit.VERSION"})
        self.assertNotIn("version", PROJECT)

    def test_the_installed_command_would_report_that_version(self) -> None:
        proc = subprocess.run([sys.executable, str(ROOT / "organize.py"), "--version"],
                              capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(proc.stdout.strip(), f"organize {VERSION}")

    def test_the_python_floor_matches_the_one_the_doctor_enforces(self) -> None:
        """A wheel that installs on a Python `doctor` then fails is a trap."""
        self.assertEqual(PROJECT["requires-python"], ">=3.11")
        classifiers = [c for c in PROJECT["classifiers"] if c.startswith("Programming Language :: Python :: 3.")]
        self.assertEqual(classifiers[0], "Programming Language :: Python :: 3.11")


class WhatTheSourceDistributionCarriesTests(unittest.TestCase):
    """An sdist that ships half a test suite is worse than one that ships none."""

    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    def directives(self, keyword: str) -> list[str]:
        return [
            argument
            for line in self.manifest.splitlines()
            if line.split(" ")[0] == keyword
            for argument in line.split(" ")[1:]
        ]

    def test_the_whole_test_suite_travels_with_its_fixtures(self) -> None:
        """setuptools ships `tests/test_*.py` by default and leaves the imports behind."""
        self.assertIn("tests", self.directives("graft"))
        support = {p.name for p in (ROOT / "tests").glob("*.py") if not p.name.startswith("test_")}
        self.assertTrue(support, "if the fixtures moved, this test is checking nothing")
        self.assertTrue((ROOT / "tests" / "selftests").is_dir())

    def test_the_zipapp_entry_point_travels(self) -> None:
        """`tests/test_zipapp.py` builds the archive; without this it cannot."""
        self.assertIn("__main__.py", self.directives("include"))
        self.assertIn("scripts/build_pyz.py", self.directives("include"))

    def test_the_documentation_the_readme_links_to_travels(self) -> None:
        """`tests/test_docs.py` checks every relative link, and runs from the sdist."""
        self.assertIn("docs", self.directives("graft"))
        for name in ("CHANGELOG.md", "OVERHAUL.md", ".env.example"):
            with self.subTest(name=name):
                self.assertIn(name, self.directives("include"))

    def test_nothing_is_shipped_that_is_not_there(self) -> None:
        """A stale `include` is silent: the file just does not appear in the sdist."""
        for name in self.directives("include"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())
        for name in self.directives("graft"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_dir())

    def test_caches_are_kept_out(self) -> None:
        self.assertIn("__pycache__", self.directives("global-exclude"))


if __name__ == "__main__":
    unittest.main()
