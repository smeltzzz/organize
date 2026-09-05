"""Unit tests for ``organize status``: the live scan joined with the cache."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import organize
from organizekit.core import KIND_BITDEPTH, KIND_REMUX, KIND_SYNC, open_state

SRT = "1\n00:00:01,000 --> 00:00:02,000\nhello\n\n"


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.library = self.root / "lib"
        self.library.mkdir()
        self.db = self.root / "state.db"
        self.addCleanup(self._tmp.cleanup)

    def _movie(self, title: str, *, sidecar: str | None = None, size: int = 4096) -> Path:
        folder = self.library / title
        folder.mkdir(parents=True, exist_ok=True)
        movie = folder / f"{title}.mkv"
        movie.write_bytes(b"x" * size)
        if sidecar is not None:
            (folder / sidecar).write_text(SRT, encoding="utf-8")
        return movie

    def _run(self, *args: str) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = organize.main(["status", "--library", str(self.library),
                                  "--state-db", str(self.db), *args])
        return code, buf.getvalue()

    def _record(self, movie: Path, kind: str, verdict: str) -> None:
        with open_state(self.db, tool="tests") as store:
            store.record(movie, kind, verdict)

    # -- the live half -----------------------------------------------------

    def test_layout_and_subtitles_come_from_a_live_scan(self) -> None:
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self._movie("Bravo (2002)")
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("2 movie(s)", out)
        self.assertIn("1 CANONICAL_MKV", out)
        self.assertIn("1 MISSING_SIDECAR", out)
        self.assertIn("1 present", out)
        self.assertIn("1 missing", out)

    def test_a_new_sidecar_is_visible_on_the_next_status_without_any_tool_running(self) -> None:
        # The point of re-scanning instead of trusting the cache: a user who
        # drops a sidecar in by hand sees it immediately.
        movie = self._movie("Alpha (2001)")
        self.assertIn("1 missing", self._run()[1])
        (movie.parent / "Alpha (2001).eng.srt").write_text(SRT, encoding="utf-8")
        self.assertIn("1 present", self._run()[1])

    def test_missing_library_exits_2(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = organize.main(["status", "--library", str(self.root / "nope")])
        self.assertEqual(code, 2)
        self.assertIn("Library not found", err.getvalue())

    def test_status_never_writes_to_the_library(self) -> None:
        movie = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        before = sorted(p.name for p in movie.parent.iterdir())
        stamp = movie.stat().st_mtime_ns
        self._run()
        self.assertEqual(sorted(p.name for p in movie.parent.iterdir()), before)
        self.assertEqual(movie.stat().st_mtime_ns, stamp)

    def test_a_legacy_sidecar_is_promoted_exactly_as_audit_does(self) -> None:
        # status runs the auditor rather than a second, subtly different scan,
        # so it inherits the auditor's single side effect - and nothing else.
        movie = self._movie("Alpha (2001)", sidecar="Alpha (2001).en.srt")
        self.assertIn("1 present", self._run()[1])
        self.assertTrue((movie.parent / "Alpha (2001).eng.srt").is_file())
        self.assertFalse((movie.parent / "Alpha (2001).en.srt").exists())

    def test_the_scan_log_is_hidden_unless_verbose(self) -> None:
        self._movie("Bravo (2002)")
        self.assertNotIn("MISSING_SIDECAR: Bravo (2002)", self._run()[1])
        self.assertIn("MISSING_SIDECAR: Bravo (2002)", self._run("--verbose")[1])

    # -- the cached half ---------------------------------------------------

    def test_cached_verdicts_are_shown_per_step(self) -> None:
        alpha = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self._record(alpha, KIND_BITDEPTH, "SKIP_HDR")
        self._record(alpha, KIND_SYNC, "synced")
        out = self._run()[1]
        self.assertIn("1 SKIP_HDR", out)
        self.assertIn("1 synced", out)

    def test_a_verdict_about_changed_bytes_is_reported_unknown(self) -> None:
        alpha = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self._record(alpha, KIND_BITDEPTH, "SKIP_HDR")
        alpha.write_bytes(b"y" * 9000)  # a remux happened behind our back
        out = self._run()[1]
        self.assertNotIn("SKIP_HDR", out)
        self.assertIn("1 stale", out)

    def test_a_step_with_no_data_is_named_rather_than_counted(self) -> None:
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        out = self._run()[1]
        self.assertIn("Remux     not recorded yet", out)
        self.assertIn("not counted", out)

    def test_no_state_hides_the_cache_entirely(self) -> None:
        alpha = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self._record(alpha, KIND_BITDEPTH, "SKIP_HDR")
        out = self._run("--no-state")[1]
        self.assertNotIn("SKIP_HDR", out)
        self.assertIn("State cache disabled", out)
        self.assertIn("1 CANONICAL_MKV", out)  # the live half still works

    def test_status_refreshes_the_cache_for_the_movies_it_scanned(self) -> None:
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self._run()
        with open_state(self.db, tool="tests") as store:
            self.assertEqual(len(store.movies()), 1)
            self.assertTrue(any(v.verdict == "CANONICAL_MKV" for v in store.verdicts().values()))

    def test_a_deleted_movie_stops_being_reported(self) -> None:
        alpha = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self._record(alpha, KIND_BITDEPTH, "SKIP_HDR")
        self._run()
        for path in sorted(alpha.parent.iterdir()):
            path.unlink()
        alpha.parent.rmdir()
        out = self._run()[1]
        self.assertIn("0 movie(s)", out)
        self.assertNotIn("SKIP_HDR", out)

    # -- the arithmetic ----------------------------------------------------

    def _summary(self, folders, verdicts=None, stamps=None) -> organize.LibraryStatus:
        import library_auditor

        audit = library_auditor.Audit(source_dir=self.library, folders=folders)
        return organize.collect_status(audit, verdicts or {}, stamps or {})

    def _folder(self, title: str, state: str) -> object:
        import library_auditor

        return library_auditor.FolderAudit(
            folder=self.library / title,
            state=state,
            movie_files=[library_auditor.MovieFile(f"{title}.mkv", ".mkv", 1024)],
        )

    def test_settled_requires_every_recorded_step_to_agree(self) -> None:
        alpha = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        bravo = self._movie("Bravo (2002)", sidecar="Bravo (2002).eng.srt")
        for movie, sync in ((alpha, "synced"), (bravo, "review")):
            self._record(movie, KIND_BITDEPTH, "SKIP_HDR")
            self._record(movie, KIND_SYNC, sync)
        out = self._run()[1]
        self.assertIn("Nothing to do for 1 movie(s)", out)
        self.assertIn("the next pass will touch 1", out)

    def test_a_folder_without_a_single_movie_file_counts_as_layout_only(self) -> None:
        import library_auditor

        empty = library_auditor.FolderAudit(
            folder=self.library / "Empty", state="NO_DIRECT_MOVIE_FILE", movie_files=[],
        )
        status = self._summary([empty, self._folder("Alpha (2001)", "CANONICAL_MKV")])
        self.assertEqual(status.movies, 1)
        self.assertEqual(status.steps[0].counts["NO_DIRECT_MOVIE_FILE"], 1)

    def test_any_current_remux_verdict_settles_the_remux_question(self) -> None:
        from organizekit.core import Verdict, path_norm

        key = path_norm(self.library / "Alpha (2001)" / "Alpha (2001).mkv")
        verdict = Verdict(path_key=key, kind=KIND_REMUX, verdict="ALREADY_CLEAN",
                          size=1, mtime_ns=2)
        status = self._summary(
            [self._folder("Alpha (2001)", "CANONICAL_MKV")],
            verdicts={(key, KIND_REMUX): verdict},
            stamps={key: (1, 2)},
        )
        remux = next(step for step in status.steps if step.label == "Remux")
        self.assertEqual(remux.counts, {"ALREADY_CLEAN": 1})
        self.assertEqual(remux.settled, 1)

    def test_pending_never_goes_negative(self) -> None:
        status = organize.LibraryStatus(
            library=self.library, movies=0, total_bytes=0, steps=(), settled=5,
        )
        self.assertEqual(status.pending, 0)

    def test_human_bytes_reads_like_the_reports(self) -> None:
        self.assertEqual(organize.human_bytes(512), "512 B")
        self.assertEqual(organize.human_bytes(1024), "1.0 KiB")
        self.assertEqual(organize.human_bytes(3 * 1024**4), "3.0 TiB")
        self.assertEqual(organize.human_bytes(2048 * 1024**4), "2048.0 TiB")

    def test_format_status_is_plain_text(self) -> None:
        status = self._summary([self._folder("Alpha (2001)", "CANONICAL_MKV")])
        lines = organize.format_status(status)
        self.assertTrue(lines[0].startswith("Library"))
        self.assertTrue(any("Nothing to do for" in line for line in lines))
        self.assertFalse(any("\x1b[" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
