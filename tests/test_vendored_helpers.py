"""The vendored helpers must not drift apart.

Every tool in this repo is a standalone single file, which it achieves by
carrying its own copy of the shared helpers (report rendering, atomic writes,
locking, the subtitle contract). CONTRIBUTING.md states the rule plainly:

    If you change a vendored helper, keep the other tools' copies
    byte-identical so they cannot drift apart.

That rule was being enforced by hand, and hand-enforcement failed. Before these
tests existed, ``atomic_write_text`` had drifted into two materially different
implementations: ``subtitle_fetcher.py`` fsynced the staged file before
renaming it (so a report survives power loss, not merely a process crash) while
the other five copies did not. The safer version had been written once and
never propagated, leaving the tool that rewrites movie files with the weaker
writer.

These tests compare the copies against each other by AST, so formatting and
comments may differ per file but behaviour cannot.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Every tool that vendors helpers. organize.py is the thin CLI front end and
# jellyfin_one_shot.py is an orchestrator; neither vendors the report stack.
TOOLS = (
    "bitdepth.py",
    "library_auditor.py",
    "mkv_track_cleaner.py",
    "movie_standardizer.py",
    "pipeline.py",
    "subtitle_fetcher.py",
    "sync_subtitles.py",
)


def _top_level(path: Path) -> dict[str, ast.AST]:
    """Map every top-level def/class in ``path`` to its AST node."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def _normalized(node: ast.AST) -> str:
    """A comparable dump of ``node`` with docstrings and positions stripped.

    Docstrings are excluded deliberately: a helper may explain itself in terms
    of the tool it lives in. Only executable behaviour has to match.
    """
    clone = ast.parse(ast.unparse(node)).body[0]
    for sub in ast.walk(clone):
        if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module):
            body = getattr(sub, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                sub.body = body[1:] or [ast.Pass()]
    return ast.dump(clone, annotate_fields=True)


class VendoredHelpersAgree(unittest.TestCase):
    """Helpers sharing a name across tools must share an implementation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.symbols = {tool: _top_level(REPO / tool) for tool in TOOLS}

    def test_shared_helpers_are_behaviourally_identical(self) -> None:
        shared = (
            "atomic_write_text",
            "clip_text",
            "wrap_text",
            "wrap_path_text",
            "_pack_on_separators",
            "print_text",
            "enable_utf8_stdio",
            "path_norm",
            "path_is_within",
            "try_file_lock",
            "Report",
            "normalize_srt_newlines",
            "decode_srt_bytes",
            "srt_looks_valid",
            "validate_srt_sidecar",
            "exact_external_english_srt_path",
            "legacy_external_english_srt_path",
            "promote_legacy_external_english_srt",
        )
        for name in shared:
            copies = {
                tool: _normalized(syms[name])
                for tool, syms in self.symbols.items()
                if name in syms
            }
            if len(copies) < 2:
                continue
            with self.subTest(helper=name):
                reference_tool, reference = next(iter(copies.items()))
                for tool, dumped in copies.items():
                    self.assertEqual(
                        reference,
                        dumped,
                        f"{name}() in {tool} has drifted from the copy in "
                        f"{reference_tool}. Vendored helpers must stay "
                        f"behaviourally identical across every tool.",
                    )

    def test_atomic_write_text_is_durable_everywhere(self) -> None:
        """Every copy must fsync before renaming, not just some of them.

        This is the specific regression that motivated the whole suite: a
        report published without fsync can survive a process crash but not a
        power cut, because os.replace may land while the bytes it points at
        are still only in the page cache.
        """
        for tool, syms in self.symbols.items():
            node = syms.get("atomic_write_text")
            if node is None:
                continue
            with self.subTest(tool=tool):
                source = ast.unparse(node)
                self.assertIn(
                    "fsync",
                    source,
                    f"atomic_write_text() in {tool} does not fsync the staged "
                    f"file before publishing it.",
                )
                self.assertIn(
                    "os.replace",
                    source,
                    f"atomic_write_text() in {tool} does not publish via the "
                    f"atomic os.replace.",
                )


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
        """The two orchestrators must not disagree about the running order."""
        import jellyfin_one_shot
        import pipeline

        self.assertEqual(
            tuple(pipeline.STEP_ORDER),
            tuple(jellyfin_one_shot.STEP_ORDER),
            "jellyfin_one_shot.py and pipeline.py define the toolchain order "
            "separately; they have drifted apart.",
        )


if __name__ == "__main__":
    unittest.main()


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
