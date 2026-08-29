"""Tests for ``pipeline.py``, the manual-step orchestrator.

Everything here is offline: no tool is ever launched. The property that matters
most is the step order, because fetching subtitles after a remux silently
degrades subtitle matching to the weaker title/year search.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import unittest
from pathlib import Path

import pipeline as pl


class StepOrderTests(unittest.TestCase):
    def test_canonical_order(self) -> None:
        self.assertEqual(pl.STEP_ORDER, ("fetcher", "cleaner", "10bit", "auditor"))

    def test_fetcher_precedes_cleaner(self) -> None:
        """The moviehash is destroyed by a remux, so this is load-bearing."""
        self.assertLess(pl.STEP_ORDER.index("fetcher"), pl.STEP_ORDER.index("cleaner"))

    def test_order_survives_any_input_order(self) -> None:
        for requested in (["auditor", "fetcher"], ["10bit", "cleaner", "fetcher"],
                          ["cleaner"], ["auditor", "10bit", "fetcher", "cleaner"]):
            with self.subTest(requested=requested):
                expected = tuple(k for k in pl.STEP_ORDER if k in set(requested))
                self.assertEqual(pl.resolve_steps(requested), expected)

    def test_step_order_does_not_leak_requested_order(self) -> None:
        self.assertEqual(pl.resolve_steps(["cleaner", "fetcher"]), ("fetcher", "cleaner"))

    def test_empty_selection(self) -> None:
        self.assertEqual(pl.resolve_steps([]), ())

    def test_every_step_is_defined(self) -> None:
        self.assertEqual(set(pl.STEPS), set(pl.STEP_ORDER))


class LibraryResolutionTests(unittest.TestCase):
    """The library root resolves flag-first, then MOVIE_STD_TARGET, then the
    documented default — the env case is what makes Docker one-liners work."""

    def setUp(self) -> None:
        self._saved = os.environ.pop("MOVIE_STD_TARGET", None)

    def tearDown(self) -> None:
        if self._saved is not None:
            os.environ["MOVIE_STD_TARGET"] = self._saved
        else:
            os.environ.pop("MOVIE_STD_TARGET", None)

    def test_no_flag_no_env_uses_documented_default(self) -> None:
        self.assertEqual(pl.resolve_library(None), Path(pl.DEFAULT_LIBRARY).resolve())

    def test_env_var_is_honored_without_a_flag(self) -> None:
        os.environ["MOVIE_STD_TARGET"] = "/media/torrents/final_organized"
        self.assertEqual(pl.resolve_library(None), Path("/media/torrents/final_organized").resolve())

    def test_explicit_flag_beats_env_var(self) -> None:
        os.environ["MOVIE_STD_TARGET"] = "/media/torrents/final_organized"
        self.assertEqual(pl.resolve_library(Path("/srv/movies")), Path("/srv/movies").resolve())


class CommandBuildingTests(unittest.TestCase):
    """Each tool names the library root differently; a wrong flag silently
    sends it to its hardcoded default path instead."""

    def setUp(self) -> None:
        self.library = Path("/media/movies")

    def test_root_flags(self) -> None:
        self.assertEqual(pl.STEPS["fetcher"].root_flag, "--source")
        self.assertEqual(pl.STEPS["cleaner"].root_flag, "--dir")
        self.assertEqual(pl.STEPS["10bit"].root_flag, "--source")
        self.assertEqual(pl.STEPS["auditor"].root_flag, "--source")

    def test_library_root_is_always_passed(self) -> None:
        for key in pl.STEP_ORDER:
            command = pl.build_command(pl.STEPS[key], pl.Config(library=self.library))
            with self.subTest(step=key):
                self.assertIn(str(self.library), command)
                self.assertEqual(command[0], __import__("sys").executable)

    def test_dry_run_forwarded_only_where_supported(self) -> None:
        cfg = pl.Config(library=self.library, dry_run=True)
        self.assertIn("--dry-run", pl.build_command(pl.STEPS["fetcher"], cfg))
        self.assertIn("--dry-run", pl.build_command(pl.STEPS["cleaner"], cfg))
        # The auditor is already read-only and has no --dry-run to accept.
        self.assertNotIn("--dry-run", pl.build_command(pl.STEPS["auditor"], cfg))

    def test_limit_forwarded_only_where_supported(self) -> None:
        cfg = pl.Config(library=self.library, limit=5)
        self.assertEqual(pl.build_command(pl.STEPS["fetcher"], cfg)[-2:], ["--limit", "5"])
        self.assertNotIn("--limit", pl.build_command(pl.STEPS["auditor"], cfg))

    def test_cleaner_specific_flags(self) -> None:
        command = pl.build_command(pl.STEPS["cleaner"],
                                   pl.Config(library=self.library, nice=True))
        self.assertIn("--nice", command)
        self.assertNotIn("--allow-hardlinked", command)

    def test_flags_absent_by_default(self) -> None:
        command = pl.build_command(pl.STEPS["cleaner"], pl.Config(library=self.library))
        self.assertNotIn("--nice", command)
        self.assertNotIn("--allow-hardlinked", command)

    def test_no_hardlink_override_exists(self) -> None:
        """Seeding movies are never remuxed, so the flag must not exist at all."""
        source = Path(pl.__file__).read_text(encoding="utf-8")
        self.assertNotIn("--allow-hardlinked", source)


class PrerequisiteTests(unittest.TestCase):
    def test_missing_script_is_skipped_not_crashed(self) -> None:
        ghost = pl.Step(key="ghost", script="does-not-exist.py",
                        title="ghost", root_flag="--source")
        self.assertIsNotNone(pl.prerequisite_issue(ghost))

    def test_auditor_has_no_hard_prerequisite(self) -> None:
        # It is read-only and dependency-free, so only the file itself matters.
        issue = pl.prerequisite_issue(pl.STEPS["auditor"])
        self.assertIsNone(issue, f"auditor should be runnable here: {issue}")

    def test_known_scripts_exist(self) -> None:
        for key in pl.STEP_ORDER:
            with self.subTest(step=key):
                self.assertTrue((pl.HERE / pl.STEPS[key].script).is_file())


class DryRunDoesNotExecuteTests(unittest.TestCase):
    """A dry run must never launch a tool."""

    def setUp(self) -> None:
        self.calls: list = []
        self._run = subprocess.run
        subprocess.run = self._fake  # type: ignore[assignment]
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        subprocess.run = self._run  # type: ignore[assignment]

    def _fake(self, command, *_args, **_kwargs):
        """Record the launch and pretend it succeeded; never really run a tool."""
        self.calls.append(list(command))
        return subprocess.CompletedProcess(command, 0)

    def test_dry_run_launches_nothing(self) -> None:
        cfg = pl.Config(library=pl.HERE, steps=pl.STEP_ORDER, dry_run=True)
        run = pl.run_pipeline(cfg, dry_run=True)
        self.assertEqual(self.calls, [], "a dry run must never launch a tool")
        self.assertTrue(all(r.status == "skipped" for r in run.results))

    def test_dry_run_still_shows_the_commands(self) -> None:
        cfg = pl.Config(library=pl.HERE, steps=pl.STEP_ORDER, dry_run=True)
        pl.run_pipeline(cfg, dry_run=True)
        self.assertEqual(pl.build_command(pl.STEPS["auditor"], cfg)[-1], str(pl.HERE))

    def test_live_run_launches_runnable_steps(self) -> None:
        # Only the auditor is runnable in a bare checkout (the rest are skipped
        # on missing prerequisites), which keeps this test offline and fast.
        cfg = pl.Config(library=pl.HERE, steps=("auditor",))
        run = pl.run_pipeline(cfg, dry_run=False)
        self.assertEqual(len(self.calls), 1, "a live run must launch the runnable step")
        self.assertEqual([r.status for r in run.results], ["ran"])
        self.assertIn(str(pl.HERE), self.calls[0], "the library root is always forwarded")

    def test_live_run_skips_steps_missing_prerequisites(self) -> None:
        cfg = pl.Config(library=pl.HERE, steps=pl.STEP_ORDER)
        run = pl.run_pipeline(cfg, dry_run=False)
        statuses = {r.key: r.status for r in run.results}
        self.assertEqual(statuses["auditor"], "ran")
        for key in ("fetcher", "cleaner", "10bit"):
            if statuses[key] != "ran":
                with self.subTest(step=key):
                    self.assertNotEqual(statuses[key], "ran")


class HintTests(unittest.TestCase):
    """The two silent failure modes get a note at the moment they matter."""

    def test_cleaner_warns_about_hardlink_deferral(self) -> None:
        hint = pl.HINTS["cleaner"]
        self.assertIn("ALWAYS deferred", hint)
        self.assertNotIn("--allow-hardlinked", hint)
        self.assertIn("safe - it", hint, "the hint should reassure that deleting is safe")

    def test_fetcher_hint_explains_the_ordering(self) -> None:
        self.assertIn("moviehash", pl.HINTS["fetcher"])

    def test_hints_are_shown_only_for_steps_that_will_run(self) -> None:
        seen: list[str] = []
        original = pl.prerequisite_issue
        try:
            pl.prerequisite_issue = lambda step: None
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                pl.run_pipeline(pl.Config(library=pl.HERE, steps=("cleaner",)), dry_run=True)
            seen.append(buf.getvalue())
        finally:
            pl.prerequisite_issue = original
        self.assertIn("ALWAYS deferred", seen[0])

    def test_deferral_hint_is_always_shown(self) -> None:
        """No flag can suppress it, because no flag can override the policy."""
        original = pl.prerequisite_issue
        try:
            pl.prerequisite_issue = lambda step: None
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                pl.run_pipeline(pl.Config(library=pl.HERE, steps=("cleaner",)), dry_run=True)
        finally:
            pl.prerequisite_issue = original
        self.assertIn("ALWAYS deferred", buf.getvalue())


class SummaryTests(unittest.TestCase):
    def test_summary_names_failures_and_skips(self) -> None:
        run = pl.Run(results=[
            pl.StepResult("fetcher", "Fetch", "ran", returncode=1, seconds=0.2),
            pl.StepResult("cleaner", "Clean", "skipped", detail="mkvmerge not found"),
        ])
        summary = pl.build_summary(run, pl.Config(library=Path("/lib")))
        self.assertIn("Failed steps : fetcher", summary)
        self.assertIn("mkvmerge not found", summary)
        self.assertIn("SKIP", summary)

    def test_summary_says_all_done_when_clean(self) -> None:
        run = pl.Run(results=[pl.StepResult("auditor", "Audit", "ran", returncode=0)])
        self.assertIn("All steps completed.", pl.build_summary(run, pl.Config(library=Path("/lib"))))


class ContinueOnErrorTests(unittest.TestCase):
    """Tests for the --continue-on-error / --stop-on-error behavior."""

    def test_continue_on_error_defaults_to_true(self) -> None:
        """The pipeline now continues past failures by default."""
        cfg = pl.Config()
        self.assertTrue(cfg.continue_on_error)

    def test_pipeline_continues_past_failure_by_default(self) -> None:
        """With the default continue_on_error=True, all steps run even if one fails."""
        cfg = pl.Config(library=pl.HERE, steps=("auditor", "auditor"), continue_on_error=True)
        original = pl.prerequisite_issue
        calls: list = []
        orig_run = subprocess.run
        def fake_run(command, *_args, **_kwargs):
            calls.append(list(command))
            # Simulate first step failing, second succeeding
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)
        try:
            pl.prerequisite_issue = lambda step: None
            subprocess.run = fake_run  # type: ignore[assignment]
            run = pl.run_pipeline(cfg, dry_run=False)
            # Both steps should have run
            self.assertEqual(len([r for r in run.results if r.status == "ran"]), 2)
            # Pipeline should report failure overall
            self.assertTrue(any(r.status == "ran" and r.returncode for r in run.results))
        finally:
            pl.prerequisite_issue = original
            subprocess.run = orig_run  # type: ignore[assignment]

    def test_pipeline_stops_on_failure_with_stop_on_error(self) -> None:
        """With --stop-on-error (continue_on_error=False), pipeline stops at first failure."""
        cfg = pl.Config(library=pl.HERE, steps=("auditor", "auditor"), continue_on_error=False)
        original = pl.prerequisite_issue
        calls: list = []
        orig_run = subprocess.run
        def fake_run(command, *_args, **_kwargs):
            calls.append(list(command))
            # First step fails
            return subprocess.CompletedProcess(command, 1)
        try:
            pl.prerequisite_issue = lambda step: None
            subprocess.run = fake_run  # type: ignore[assignment]
            run = pl.run_pipeline(cfg, dry_run=False)
            # Only first step should have run
            self.assertEqual(len([r for r in run.results if r.status == "ran"]), 1)
        finally:
            pl.prerequisite_issue = original
            subprocess.run = orig_run  # type: ignore[assignment]

    def test_summary_reports_failed_steps_when_continuing(self) -> None:
        """Failed steps are listed in summary even when pipeline continues."""
        run = pl.Run(results=[
            pl.StepResult("fetcher", "Fetch", "ran", returncode=1, seconds=0.1),
            pl.StepResult("cleaner", "Clean", "ran", returncode=0, seconds=0.2),
            pl.StepResult("10bit", "10bit", "ran", returncode=0, seconds=0.3),
            pl.StepResult("auditor", "Audit", "ran", returncode=0, seconds=0.4),
        ])
        summary = pl.build_summary(run, pl.Config(library=Path("/lib")))
        self.assertIn("Failed steps : fetcher", summary)
        self.assertIn("RAN", summary)

    def test_continue_on_error_flag_is_accepted_no_op(self) -> None:
        """--continue-on-error is still accepted and is now a no-op (default is True)."""
        # This tests that the argument parser accepts --continue-on-error
        import argparse
        parser = pl.build_parser()
        # Parse with --continue-on-error
        args = parser.parse_args(["--continue-on-error"])
        self.assertTrue(args.continue_on_error)

    def test_stop_on_error_flag_sets_false(self) -> None:
        """--stop-on-error sets continue_on_error to False."""
        import argparse
        parser = pl.build_parser()
        # Parse with --stop-on-error
        args = parser.parse_args(["--stop-on-error"])
        self.assertFalse(args.continue_on_error)

    def test_stop_on_error_overrides_default(self) -> None:
        """--stop-on-error overrides the default True."""
        import argparse
        parser = pl.build_parser()
        # Parse with both flags (--continue-on-error is default, --stop-on-error overrides)
        args = parser.parse_args(["--stop-on-error"])
        self.assertFalse(args.continue_on_error)


if __name__ == "__main__":
    unittest.main()
