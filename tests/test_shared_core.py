"""The shared core must stay shared.

Every tool in this repo used to be a standalone single file, which it achieved
by carrying its own copy of the shared helpers: report rendering, atomic
writes, locking, the subtitle contract, library-root resolution. That policy
cost 4,325 lines of literal duplication, and hand-enforcement failed in
exactly the way you would expect — ``atomic_write_text`` had drifted into two
implementations, and the safer one (it fsyncs the staged file before renaming,
so a report survives a power cut and not merely a process crash) was *not* the
one used by the tool that rewrites movie files.

The copies are gone. Every shared helper now lives exactly once in
``organizekit.core`` and the tools import it. These tests keep it that way:

* nothing may re-vendor a core helper into a tool,
* the durability guarantee is asserted on the single implementation,
* the two orchestrators must still agree on the load-bearing step order,
* and the prerequisite checks must still agree with each other.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from organizekit import core  # noqa: E402  (needs the path bootstrap above)

# Every module that used to vendor helpers, plus the two orchestrators.
TOOLS = (
    "bitdepth.py",
    "jellyfin_one_shot.py",
    "library_auditor.py",
    "mkv_track_cleaner.py",
    "movie_standardizer.py",
    "organize.py",
    "pipeline.py",
    "subtitle_fetcher.py",
    "sync_subtitles.py",
)

CORE_NAMES = frozenset(core.__all__)


def _top_level_definitions(path: Path) -> dict[str, ast.AST]:
    """Map every top-level def/class/assignment in ``path`` to its AST node."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, ast.AST] = {}
    for node in tree.body:
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name is None and isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        if name is not None:
            found[name] = node
    return found


class NothingMayReVendorTheCore(unittest.TestCase):
    """A helper that lives in the core may not be redefined by a tool.

    This is the structural replacement for the old "keep the seven copies
    byte-identical" rule. Drift is no longer detected after the fact; it is
    made unrepresentable, because a second definition fails this test.
    """

    def test_no_tool_redefines_a_core_helper(self) -> None:
        for tool in TOOLS:
            defined = _top_level_definitions(REPO / tool)
            clashes = sorted(CORE_NAMES & set(defined))
            with self.subTest(tool=tool):
                self.assertEqual(
                    [],
                    clashes,
                    f"{tool} defines {clashes}, which already exist in "
                    f"organizekit.core. Import them instead of copying them: "
                    f"a second definition is how atomic_write_text lost its "
                    f"fsync in five of six tools.",
                )

    def test_tools_bind_the_core_implementation_itself(self) -> None:
        """Importing must be by reference, not by re-assignment to a copy."""
        import bitdepth
        import library_auditor
        import mkv_track_cleaner
        import movie_standardizer
        import pipeline
        import subtitle_fetcher
        import sync_subtitles

        for module in (bitdepth, library_auditor, mkv_track_cleaner,
                       movie_standardizer, pipeline, subtitle_fetcher,
                       sync_subtitles):
            for name in ("Report", "resolve_library", "atomic_write_text"):
                bound = getattr(module, name, None)
                if bound is None:
                    continue  # a tool need not use every helper
                with self.subTest(tool=module.__name__, helper=name):
                    self.assertIs(bound, getattr(core, name))


class TheOneAtomicWriterIsDurable(unittest.TestCase):
    """The regression that motivated all of this, asserted once."""

    def test_atomic_write_text_fsyncs_before_publishing(self) -> None:
        source = ast.unparse(
            ast.parse((REPO / "organizekit" / "core" / "fsio.py").read_text(encoding="utf-8"))
        )
        self.assertIn("fsync", source)
        self.assertIn("os.replace", source)

    def test_atomic_write_text_publishes_the_exact_bytes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "report.txt"
            core.atomic_write_text(target, "line one\nline two\n")
            self.assertEqual("line one\nline two\n", target.read_text(encoding="utf-8"))
            # No staging debris is left behind by a successful publish.
            self.assertEqual(["report.txt"], sorted(p.name for p in Path(td).iterdir()))


class ExclusiveRunLockKeepsTheSaferWindowsGuard(unittest.TestCase):
    """The two copies of this lock had genuinely diverged.

    One materialised the Windows lock byte only when the file was empty; the
    other appended a ``0`` on *every* retry, growing the lock file for the
    lifetime of a contended wait. The surviving implementation is the guarded
    one, and the message each tool shows is a parameter rather than a reason
    to fork the class.
    """

    def test_contention_message_is_per_tool(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run.lock"
            with core.ExclusiveRunLock(path, 0.1, busy_message="held by {path}"):
                second = core.ExclusiveRunLock(path, 0.0, busy_message="held by {path}")
                if os.name == "nt":  # POSIX flock is per-process, not per-handle
                    with self.assertRaises(core.LockUnavailable) as caught:
                        second.__enter__()
                    self.assertIn(str(path), str(caught.exception))

    def test_windows_byte_is_written_once(self) -> None:
        source = ast.unparse(
            ast.parse((REPO / "organizekit" / "core" / "locking.py").read_text(encoding="utf-8"))
        )
        self.assertIn("if self.handle.tell() == 0:", source,
                      "the Windows lock byte must only be written when the file is empty")


class DotenvIsFoundFromWhereTheUserRuns(unittest.TestCase):
    """The .env used to be looked up beside the tool's own source file.

    Now that the loader lives in an installed package, it searches what the
    user actually ran: the entry-point script's directory, then the working
    directory, then the installation root.
    """

    def test_explicit_path_still_wins(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text("ORGANIZE_TEST_ONLY=1\n", encoding="utf-8")
            try:
                self.assertEqual({"ORGANIZE_TEST_ONLY": "1"}, core.load_dotenv(env))
            finally:
                os.environ.pop("ORGANIZE_TEST_ONLY", None)

    def test_candidates_include_cwd_and_the_entry_point(self) -> None:
        from organizekit.core.config import _dotenv_candidates

        candidates = [str(p) for p in _dotenv_candidates()]
        self.assertTrue(any(c.endswith(".env") for c in candidates))
        self.assertIn(str(Path.cwd() / ".env"), candidates)


class PipelineOrderIsLoadBearing(unittest.TestCase):
    """The subtitle fetch must precede the remux, forever.

    subtitle_fetcher.py searches OpenSubtitles by moviehash, computed from the
    file size plus the first and last 64 KiB. A remux rewrites those bytes, so
    a movie cleaned first can never reproduce its release hash and is silently
    demoted to the far weaker title/year search. The constraint was documented
    in three docstrings and enforced by nothing.
    """

    def test_fetcher_runs_before_cleaner(self) -> None:
        import pipeline

        order = pipeline.STEP_ORDER
        self.assertIn("fetcher", order)
        self.assertIn("cleaner", order)
        self.assertLess(
            order.index("fetcher"),
            order.index("cleaner"),
            "subtitle fetching MUST precede the remux: cleaning first destroys "
            "the OpenSubtitles moviehash and silently degrades every lookup.",
        )

    def test_sync_runs_after_cleaner_and_before_audit(self) -> None:
        import pipeline

        order = pipeline.STEP_ORDER
        self.assertLess(
            order.index("cleaner"),
            order.index("sync"),
            "syncing before the remux wastes the work: the remux republishes "
            "the movie the sidecar was aligned against.",
        )
        self.assertLess(
            order.index("sync"),
            order.index("auditor"),
            "the audit must see the finished sidecars.",
        )

    def test_one_shot_agrees_with_the_pipeline_order(self) -> None:
        """The two orchestrators must not disagree about the running order.

        They no longer *can*: the order, the scripts, the flags and the
        prerequisites are one table in organizekit.core.toolchain, and both
        orchestrators bind that object rather than a copy of it. Asserting
        identity (not equality) is what makes drift unrepresentable.
        """
        import jellyfin_one_shot
        import pipeline

        self.assertIs(pipeline.STEP_ORDER, core.STEP_ORDER)
        self.assertIs(jellyfin_one_shot.STEP_ORDER, core.STEP_ORDER)
        self.assertIs(pipeline.STEPS, core.STEPS)
        self.assertIs(jellyfin_one_shot.STEPS, core.STEPS)

    def test_the_step_table_is_the_only_list_of_the_tools(self) -> None:
        """Every script named in the table exists, and nothing else runs."""
        for key in core.STEP_ORDER:
            with self.subTest(step=key):
                self.assertTrue((REPO / core.STEPS[key].script).is_file())
        self.assertEqual(
            tuple(core.STEPS[key].script for key in core.STEP_ORDER),
            core.TOOL_SCRIPTS,
        )


class PrerequisiteChecksAgree(unittest.TestCase):
    """All three prerequisite surfaces must answer the same question.

    jellyfin_one_shot.py used a bare ``shutil.which("mkvmerge")`` while
    pipeline.py and organize.py doctor delegated to
    ``mkv_track_cleaner.resolve_mkvmerge_path()``, which also searches the
    standard install locations. The Windows MKVToolNix installer does not put
    itself on PATH, so a fully-provisioned machine had one-shot silently
    skipping the remux while doctor reported everything green.
    """

    def test_one_shot_delegates_binary_detection(self) -> None:
        import jellyfin_one_shot as one_shot
        import mkv_track_cleaner
        import sync_subtitles

        # mkvmerge: resolvable via the cleaner's resolver => one-shot agrees.
        try:
            mkv_track_cleaner.resolve_mkvmerge_path()
            expected_mkvmerge = True
        except (FileNotFoundError, OSError):
            expected_mkvmerge = False
        self.assertEqual(expected_mkvmerge, one_shot._mkvmerge_available())

        expected_ffsubsync = sync_subtitles.find_ffsubsync() is not None
        self.assertEqual(expected_ffsubsync, one_shot._ffsubsync_available())

    def test_one_shot_finds_binaries_off_PATH(self) -> None:
        """The regression itself: a binary present but not on PATH."""
        import tempfile

        import jellyfin_one_shot as one_shot
        import mkv_track_cleaner

        with tempfile.TemporaryDirectory() as td:
            # shutil.which() only accepts a PATHEXT extension on Windows, and
            # chmod is a no-op there, so the stub has to be named per platform.
            if os.name == "nt":
                fake = Path(td) / "mkvmerge.exe"
                fake.write_bytes(b"MZ")  # never executed, only located
            else:
                fake = Path(td) / "mkvmerge"
                fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                fake.chmod(0o755)

            original = mkv_track_cleaner.KNOWN_MKVMERGE_PATHS
            try:
                # Present in a known install location, absent from PATH.
                mkv_track_cleaner.KNOWN_MKVMERGE_PATHS = [str(fake)]
                self.assertTrue(
                    one_shot._mkvmerge_available(),
                    "one-shot must find mkvmerge in a standard install "
                    "location, not only on PATH",
                )
            finally:
                mkv_track_cleaner.KNOWN_MKVMERGE_PATHS = original


if __name__ == "__main__":
    unittest.main()
