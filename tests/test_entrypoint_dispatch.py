"""The archive entry point must preserve the normal CLI and child-process contract."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]


def load_entrypoint() -> ModuleType:
    """Load the zipapp dispatcher without executing its script guard."""
    spec = importlib.util.spec_from_file_location("organize_archive_entry", REPO / "__main__.py")
    if spec is None or spec.loader is None:
        raise AssertionError("the archive entry point must be loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArchiveEntryPointTests(unittest.TestCase):
    def test_missing_child_name_shows_usage_and_exits_as_a_usage_error(self) -> None:
        """A malformed zipapp dispatch must not import an arbitrary module."""
        entry = load_entrypoint()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = entry.run_tool([])
        self.assertEqual(status, 2)
        self.assertIn("usage: run-tool", stderr.getvalue())
        self.assertIn("bitdepth.py", stderr.getvalue())

    def test_unlisted_child_name_is_rejected_before_import(self) -> None:
        """The archive only dispatches its reviewed runnable scripts."""
        entry = load_entrypoint()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                mock.patch.object(entry.importlib, "import_module") as importer:
            status = entry.run_tool(["untrusted.py", "--anything"])
        self.assertEqual(status, 2)
        self.assertIn("unknown tool: untrusted.py", stderr.getvalue())
        importer.assert_not_called()

    def test_child_arguments_and_exit_status_match_a_standalone_script(self) -> None:
        """A zipapp child sees its own argv and its real exit status."""
        entry = load_entrypoint()
        child = SimpleNamespace(main=mock.Mock(side_effect=[7, None]))
        with mock.patch.object(entry.importlib, "import_module", return_value=child) as importer, \
                mock.patch.object(entry.sys, "argv", ["organize.pyz", "old"]):
            self.assertEqual(entry.run_tool(["bitdepth.py", "--self-test"]), 7)
            self.assertEqual(entry.sys.argv, ["bitdepth.py", "--self-test"])
            self.assertEqual(entry.run_tool(["pipeline.py"]), 0)
            self.assertEqual(entry.sys.argv, ["pipeline.py"])
        self.assertEqual(importer.call_args_list[0].args, ("bitdepth",))
        self.assertEqual(importer.call_args_list[1].args, ("pipeline",))

    def test_hidden_dispatch_verb_reaches_the_child_runner(self) -> None:
        """The step runner re-enters the archive rather than inlining a tool."""
        entry = load_entrypoint()
        with mock.patch.object(entry, "run_tool", return_value=9) as run_tool:
            self.assertEqual(entry.main(["run-tool", "mkv_track_cleaner.py", "--dry-run"]), 9)
        run_tool.assert_called_once_with(["mkv_track_cleaner.py", "--dry-run"])

    def test_normal_arguments_still_reach_the_organize_cli(self) -> None:
        """Invoking the archive without its private verb is the regular CLI."""
        entry = load_entrypoint()
        cli = SimpleNamespace(main=mock.Mock(return_value=None))
        original = ["organize.pyz", "doctor", "--json"]
        with mock.patch.dict(sys.modules, {"organize": cli}), \
                mock.patch.object(entry.sys, "argv", original):
            self.assertEqual(entry.main(), 0)
            self.assertEqual(entry.sys.argv, ["organize", "doctor", "--json"])
        cli.main.assert_called_once_with(["doctor", "--json"])


if __name__ == "__main__":
    unittest.main()
