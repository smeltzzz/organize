"""Tests for ``pipeline.py``, the manual-step orchestrator.

Everything here is offline: no tool is ever launched. The property that matters
most is the step order, because fetching subtitles after a remux silently
degrades subtitle matching to the weaker title/year search.
"""

from __future__ import annotations

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
        cfg = pl.Config(library=self.library, nice=True, allow_hardlinked=True)
        command = pl.build_command(pl.STEPS["cleaner"], cfg)
        self.assertIn("--nice", command)
        self.assertIn("--allow-hardlinked", command)

    def test_flags_absent_by_default(self) -> None:
        command = pl.build_command(pl.STEPS["cleaner"], pl.Config(library=self.library))
        self.assertNotIn("--nice", command)
        self.assertNotIn("--allow-hardlinked", command)


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


if __name__ == "__main__":
    unittest.main()
