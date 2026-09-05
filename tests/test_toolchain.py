"""The one step table, and the one argv builder both orchestrators call.

``pipeline.py`` and ``jellyfin_one_shot.py`` used to describe the same five
tools twice: two step tables, two sets of binary probes, two skip-reason
functions and six hand-written argv lists inside one 660-line function. The
copies disagreed - one-shot probed PATH while everything else asked the tool
that owns the binary - and every new flag had to be added in two places or it
silently applied to only one runner.

These tests pin the merged behaviour: the flags each tool actually receives,
the ones it must not receive, and the reasons a step is skipped.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from organizekit.core import toolchain as tc  # noqa: E402  (needs the path bootstrap)


class TheTableDescribesEveryTool(unittest.TestCase):
    def test_every_step_names_a_script_that_exists(self) -> None:
        for key in tc.STEP_ORDER:
            with self.subTest(step=key):
                self.assertTrue((tc.TOOLS_DIR / tc.STEPS[key].script).is_file())

    def test_every_step_can_be_run_and_described(self) -> None:
        """A step with no narrative would print an empty banner mid-run."""
        for key in tc.STEP_ORDER:
            step = tc.STEPS[key]
            with self.subTest(step=key):
                self.assertTrue(step.root_flag.startswith("--"))
                self.assertTrue(step.label)
                self.assertTrue(step.purpose)
                self.assertTrue(step.why_here)
                self.assertTrue(step.idle)
                self.assertTrue(step.tool_name)
                self.assertGreater(step.timeout_seconds, 0)

    def test_the_key_matches_its_entry(self) -> None:
        for key, step in tc.STEPS.items():
            self.assertEqual(key, step.key)


class BuildStepArgs(unittest.TestCase):
    """One builder, so a flag cannot reach one orchestrator and not the other."""

    def setUp(self) -> None:
        self.library = Path("/library")
        self.logs = Path("/logs")
        self.run_log = self.logs / "run.log"

    def _args(self, key: str, **kwargs: object) -> list[str]:
        return tc.build_step_args(
            tc.STEPS[key], library=self.library, report=Path(f"/stage/{key}.txt"),
            run_log=self.run_log, log_dir=self.logs, **kwargs,  # type: ignore[arg-type]
        )

    def _value(self, args: list[str], flag: str) -> str:
        return args[args.index(flag) + 1]

    def test_each_tool_gets_the_root_flag_it_actually_parses(self) -> None:
        # The cleaner is the odd one out: --dir, not --source.
        self.assertEqual("/library", self._value(self._args("cleaner"), "--dir"))
        self.assertNotIn("--source", self._args("cleaner"))
        for key in ("fetcher", "10bit", "sync", "auditor"):
            with self.subTest(step=key):
                self.assertEqual("/library", self._value(self._args(key), "--source"))

    def test_every_step_but_the_fetcher_writes_the_shared_run_log(self) -> None:
        for key in ("cleaner", "10bit", "sync", "auditor"):
            with self.subTest(step=key):
                self.assertEqual(str(self.run_log), self._value(self._args(key), "--log"))

    def test_the_fetcher_keeps_its_own_ledger(self) -> None:
        """Its log *is* its quota ledger: it parses the file back.

        Another tool's lines in that file are read as quota reservations, so
        the fetcher can never share the run log.
        """
        log = self._value(self._args("fetcher"), "--log")
        self.assertNotEqual(str(self.run_log), log)
        self.assertEqual(str(self.logs / "subtitle_fetcher_ledger.log"), log)

    def test_caches_live_under_the_log_dir_so_a_run_is_self_contained(self) -> None:
        self.assertEqual(str(self.logs / "mkv_track_cleaner_probe_cache.json"),
                         self._value(self._args("cleaner"), "--cache"))
        self.assertEqual(str(self.logs / "10bit_probe_cache.json"),
                         self._value(self._args("10bit"), "--cache"))
        # The sync tool spells the same idea differently.
        self.assertEqual(str(self.logs / "sync_state.json"),
                         self._value(self._args("sync"), "--sync-ledger"))
        self.assertNotIn("--cache", self._args("sync"))

    def test_the_auditor_takes_no_cache_and_no_dry_run(self) -> None:
        """It is read-only, and it has no flag for either."""
        args = self._args("auditor", dry_run=True, nice=True)
        self.assertNotIn("--cache", args)
        self.assertNotIn("--dry-run", args)
        self.assertNotIn("--nice", args)

    def test_dry_run_reaches_every_tool_that_understands_it(self) -> None:
        for key in ("fetcher", "cleaner", "10bit", "sync"):
            with self.subTest(step=key):
                self.assertIn("--dry-run", self._args(key, dry_run=True))
                self.assertNotIn("--dry-run", self._args(key))

    def test_nice_is_only_offered_to_the_tool_that_has_it(self) -> None:
        self.assertIn("--nice", self._args("cleaner", nice=True))
        for key in ("fetcher", "10bit", "sync", "auditor"):
            with self.subTest(step=key):
                self.assertNotIn("--nice", self._args(key, nice=True))

    def test_the_fetcher_is_capped_and_tolerates_a_missing_match(self) -> None:
        args = self._args("fetcher")
        self.assertIn("--allow-missing", args)
        self.assertEqual(str(tc.SCRAPING_DAILY_CAP), self._value(args, "--scrape-daily-cap"))

    def test_extra_flags_are_appended_verbatim(self) -> None:
        args = self._args("auditor", extra=("--fail-on-findings",))
        self.assertIn("--fail-on-findings", args)

    def test_without_a_log_dir_nothing_invents_a_path(self) -> None:
        """``pipeline.py`` calls tools with no run directory of its own."""
        args = tc.build_step_args(tc.STEPS["cleaner"], library=self.library,
                                  report=Path("/r.txt"), run_log=self.run_log)
        self.assertNotIn("--cache", args)


class SkipReasons(unittest.TestCase):
    ALL_PRESENT = {"mkvmerge": True, "ffprobe": True, "ffsubsync": True, "ffmpeg": True}

    def test_a_provisioned_machine_skips_nothing(self) -> None:
        for key in tc.STEP_ORDER:
            with self.subTest(step=key):
                self.assertIsNone(tc.step_skip_reason(key, self.ALL_PRESENT))

    def test_the_reason_names_the_binary_to_install(self) -> None:
        self.assertEqual("mkvmerge is not installed",
                         tc.step_skip_reason("cleaner", {**self.ALL_PRESENT, "mkvmerge": False}))
        self.assertEqual("ffprobe is not installed",
                         tc.step_skip_reason("10bit", {**self.ALL_PRESENT, "ffprobe": False}))

    def test_sync_needs_both_and_says_so(self) -> None:
        self.assertEqual("ffmpeg is not installed",
                         tc.step_skip_reason("sync", {**self.ALL_PRESENT, "ffmpeg": False}))
        self.assertEqual(
            "ffsubsync and ffmpeg are not installed",
            tc.step_skip_reason("sync", {**self.ALL_PRESENT, "ffsubsync": False, "ffmpeg": False}),
        )

    def test_the_steps_with_no_binary_are_never_skipped_for_one(self) -> None:
        """Fetching and auditing depend on nothing this machine can lack."""
        self.assertIsNone(tc.step_skip_reason("fetcher", {}))
        self.assertIsNone(tc.step_skip_reason("auditor", {}))

    def test_a_missing_script_is_a_skip_not_a_crash(self) -> None:
        ghost = tc.Step(key="ghost", script="does-not-exist.py", title="ghost",
                        root_flag="--source")
        self.assertIsNotNone(tc.prerequisite_issue(ghost))

    def test_missing_tool_scripts_lists_what_a_partial_copy_lacks(self) -> None:
        self.assertEqual([], tc.missing_tool_scripts(tc.TOOLS_DIR))
        self.assertEqual(list(tc.TOOL_SCRIPTS), tc.missing_tool_scripts(Path("/nowhere")))


if __name__ == "__main__":
    unittest.main()
