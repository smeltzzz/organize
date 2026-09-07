"""Run every tool's moved self-test suite as part of the offline unit suite.

The bodies live in ``tests/selftests/`` and are rebound to their tool's
namespace (see that package's docstring). Each returns a process exit code, so
the assertion here is simply that it is zero — the individual failures are
printed by the suite itself, which is what made them useful in the field.

Two things this buys beyond tidiness:

* the assertions now count towards coverage of the tools, instead of being
  production lines that the unit suite never executes;
* they can no longer silently rot, because CI runs them on every push across
  three operating systems and three Python versions.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from types import ModuleType

from tests.selftests import (
    bitdepth_selftests,
    jellyfin_one_shot_selftests,
    library_auditor_selftests,
    mkv_track_cleaner_selftests,
    movie_standardizer_selftests,
    pipeline_selftests,
    subtitle_fetcher_selftests,
    sync_subtitles_selftests,
)

SUITES = (
    ("bitdepth", bitdepth_selftests.run_self_tests),
    ("jellyfin_one_shot", jellyfin_one_shot_selftests.run_self_tests),
    ("library_auditor", library_auditor_selftests.run_self_tests),
    ("mkv_track_cleaner", mkv_track_cleaner_selftests.run_self_tests),
    ("movie_standardizer", movie_standardizer_selftests.run_canonical_self_tests),
    ("pipeline", pipeline_selftests.run_self_tests),
    # run_self_tests drives the scraping and extraction sub-suites itself,
    # collecting into the same error list, so it is the only entry point.
    ("subtitle_fetcher", subtitle_fetcher_selftests.run_self_tests),
    ("sync_subtitles", sync_subtitles_selftests.run_self_tests),
)


#: The field smoke test each tool still ships, by module name and the label
#: its own suite prints. Rebinding (above) replaces ``tool.run_self_tests``
#: with the moved suite for the rest of this process, so these bodies are
#: reachable only from a clean import — see ShippedFieldSmokeTests.
SHIPPED_SMOKE_TESTS = (
    ("bitdepth", "run_self_tests"),
    ("library_auditor", "run_self_tests"),
    ("mkv_track_cleaner", "run_self_tests"),
    ("movie_standardizer", "run_canonical_self_tests"),
    ("subtitle_fetcher", "run_self_tests"),
    ("sync_subtitles", "run_self_tests"),
)


def load_pristine(module_name: str) -> ModuleType:
    """Import a second, unrebound copy of a tool module from its own file.

    ``tests/selftests`` assigns each moved suite over the tool's
    ``run_self_tests``, which is what makes the moved bodies run under the unit
    suite — and what hides the shipped ``--self-test`` body from it. Executing
    the file again under a different module name gives the real thing back;
    coverage still attributes the lines to the tool, because it is the same
    file.
    """
    path = Path(__file__).resolve().parent.parent / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"{module_name}__pristine", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``dataclasses`` resolves string annotations through ``sys.modules``, so
    # the copy has to be registered while its body runs. It is removed again
    # immediately: nothing else may import a second copy of a tool by accident.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class ShippedFieldSmokeTests(unittest.TestCase):
    """``--self-test`` is what an operator runs on the NAS. It has to work.

    It is also the one piece of every tool that the rest of the suite cannot
    reach: importing ``tests.selftests`` rebinds ``tool.run_self_tests`` to the
    moved suite, so from then on the shipped body is shadowed. Nothing but
    ``organize.py test`` — a subprocess, invisible to coverage — ever ran it.
    A field check nobody executes is a field check nobody can trust.
    """

    def test_every_tool_ships_a_field_smoke_test_that_passes(self) -> None:
        for module_name, attribute in SHIPPED_SMOKE_TESTS:
            with self.subTest(tool=module_name):
                tool = load_pristine(module_name)
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                    code = getattr(tool, attribute)()
                printed = captured.getvalue()
                self.assertEqual(0, code, f"{module_name} --self-test failed:\n{printed}")
                self.assertIn("SELF-TEST PASSED", printed)
                self.assertIn(f"{module_name}.py", printed)


class MovedSelfTestsStillPass(unittest.TestCase):
    """Each tool's own suite, unchanged, run from its new home."""

    def test_every_tool_suite_reports_success(self) -> None:
        for name, suite in SUITES:
            with self.subTest(tool=name):
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                    code = suite()
                self.assertEqual(
                    0, code,
                    f"{name}'s self-test suite failed:\n{captured.getvalue()}",
                )


if __name__ == "__main__":
    unittest.main()
