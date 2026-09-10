"""The one step table, and the prerequisite checks that guard it.

``pipeline.py`` and a second runner (deleted in 4.0.0) used to describe the
same five tools twice: two step tables, two sets of binary probes, two
skip-reason functions and six hand-written argv lists inside one 660-line
function. The copies disagreed - the second runner probed PATH while
everything else asked the tool that owns the binary - and every new flag had
to be added in two places or it silently applied to only one runner.

These tests pin the merged behaviour: the table names scripts that exist,
each step carries the flag spelling its tool actually parses, and a step
whose binary is missing is skipped with a reason instead of crashing the run.
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

    def test_the_key_matches_its_entry(self) -> None:
        for key, step in tc.STEPS.items():
            self.assertEqual(key, step.key)

    def test_every_step_spells_its_root_flag_the_way_its_tool_parses_it(self) -> None:
        # The cleaner is the odd one out: --dir, not --source.
        self.assertEqual(tc.STEPS["cleaner"].root_flag, "--dir")
        for key in ("extractor", "10bit", "sync", "auditor"):
            with self.subTest(step=key):
                self.assertEqual(tc.STEPS[key].root_flag, "--source")

    def test_every_step_has_a_title_and_a_script_name(self) -> None:
        for key in tc.STEP_ORDER:
            step = tc.STEPS[key]
            with self.subTest(step=key):
                self.assertTrue(step.title)
                self.assertTrue(step.script.endswith(".py"))


class Prerequisites(unittest.TestCase):
    def test_a_missing_script_is_a_skip_not_a_crash(self) -> None:
        ghost = tc.Step(key="ghost", script="does-not-exist.py", title="ghost",
                        root_flag="--source")
        self.assertIsNotNone(tc.prerequisite_issue(ghost))

    def test_the_tool_scripts_list_matches_the_table(self) -> None:
        self.assertEqual(
            tuple(tc.STEPS[key].script for key in tc.STEP_ORDER),
            tc.TOOL_SCRIPTS,
        )


if __name__ == "__main__":
    unittest.main()
