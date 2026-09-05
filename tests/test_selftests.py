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
import io
import unittest

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
