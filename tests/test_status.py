"""Unit tests for ``organize status``: the live scan joined with the cache."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import organize
from organizekit.core import KIND_BITDEPTH, KIND_REMUX, open_state

SRT = "1\n00:00:01,000 --> 00:00:02,000\nhello\n\n"


class StatusFixture:
    """The library-on-disk fixture both renderings of `status` are tested on."""

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


class StatusTests(StatusFixture, unittest.TestCase):
    """The printed report."""

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
        self._record(alpha, KIND_REMUX, "cleaned")
        out = self._run()[1]
        self.assertIn("1 SKIP_HDR", out)
        self.assertIn("1 cleaned", out)

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

    def test_settled_requires_every_recorded_step_to_agree(self) -> None:
        alpha = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        bravo = self._movie("Bravo (2002)", sidecar="Bravo (2002).eng.srt")
        for movie, remux in ((alpha, "cleaned"), (bravo, "deferred")):
            self._record(movie, KIND_BITDEPTH, "SKIP_HDR")
            self._record(movie, KIND_REMUX, remux)
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

    def _remux_status(self, verdict: str):
        from organizekit.core import Verdict, path_norm

        key = path_norm(self.library / "Alpha (2001)" / "Alpha (2001).mkv")
        stored = Verdict(path_key=key, kind=KIND_REMUX, verdict=verdict, size=1, mtime_ns=2)
        status = self._summary(
            [self._folder("Alpha (2001)", "CANONICAL_MKV")],
            verdicts={(key, KIND_REMUX): stored},
            stamps={key: (1, 2)},
        )
        return next(step for step in status.steps if step.label == "Remux")

    def test_a_finished_remux_verdict_settles_the_remux_question(self) -> None:
        import mkv_track_cleaner as remux_mod

        for verdict in sorted(remux_mod.SETTLED_REMUX):
            with self.subTest(verdict=verdict):
                remux = self._remux_status(verdict)
                self.assertEqual(remux.counts, {verdict: 1})
                self.assertEqual(remux.settled, 1)

    def test_a_movie_the_cleaner_could_not_finish_is_still_pending(self) -> None:
        """Deferred, layout-blocked and failed are all work that is still to do."""
        import mkv_track_cleaner as remux_mod

        for verdict in (remux_mod.STATUS_DEFERRED, remux_mod.STATUS_SKIPPED_LAYOUT,
                        remux_mod.STATUS_FAILED):
            with self.subTest(verdict=verdict):
                remux = self._remux_status(verdict)
                self.assertEqual(remux.counts, {verdict: 1})
                self.assertEqual(remux.settled, 0)

    def test_the_status_line_uses_the_cleaners_own_vocabulary(self) -> None:
        """A status line that disagrees with the tool about "done" is worse than none."""
        import mkv_track_cleaner as remux_mod

        self.assertEqual(
            remux_mod.SETTLED_REMUX | {remux_mod.STATUS_DEFERRED,
                                       remux_mod.STATUS_SKIPPED_LAYOUT,
                                       remux_mod.STATUS_FAILED},
            {status for _, status in remux_mod.VERDICT_BUCKETS},
        )

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


class StatusJsonTests(StatusFixture, unittest.TestCase):
    """`status --json`: the same summary, for something that is not a person."""

    def _json(self, *args: str) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = organize.main(["status", "--library", str(self.library),
                                  "--state-db", str(self.db), "--json", *args])
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_stdout_is_nothing_but_the_document(self) -> None:
        """The scan progress line would otherwise sit in front of the JSON."""
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        code, document, err = self._json()
        self.assertEqual(code, 0)
        self.assertEqual(document["command"], "status")
        self.assertIn("Scanning", err)

    def test_the_envelope_matches_every_other_json_command(self) -> None:
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        document = self._json()[1]
        self.assertEqual(document["schema"], organize.JSON_SCHEMA)
        self.assertEqual(document["tool"], "organize")
        self.assertEqual(document["version"], organize.VERSION)

    def test_top_level_keys_are_the_documented_ones(self) -> None:
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self.assertEqual(
            sorted(self._json()[1]),
            ["command", "error", "exit_code", "library", "movies", "pending", "schema",
             "settled", "state_cache", "steps", "tool", "total_bytes", "version"],
        )

    def test_the_numbers_are_the_ones_the_report_prints(self) -> None:
        alpha = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self._movie("Bravo (2002)")
        self._record(alpha, KIND_BITDEPTH, "SKIP_HDR")
        code, document, _ = self._json()
        human = self._run()[1]
        self.assertEqual(code, 0)
        self.assertEqual(document["movies"], 2)
        self.assertEqual(document["total_bytes"], 8192)
        self.assertIn(f"Nothing to do for {document['settled']} movie(s)", human)
        self.assertIn(f"the next pass will touch {document['pending']}", human)

    def test_one_row_per_step_with_a_slug_id(self) -> None:
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        steps = self._json()[1]["steps"]
        self.assertEqual([step["id"] for step in steps],
                         ["layout", "subtitles", "remux", "bit-depth"])
        self.assertEqual([step["label"] for step in steps],
                         ["Layout", "Subtitles", "Remux", "Bit depth"])

    def test_counts_are_reported_per_step(self) -> None:
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self._movie("Bravo (2002)")
        steps = {step["id"]: step for step in self._json()[1]["steps"]}
        self.assertEqual(steps["layout"]["counts"],
                         {"CANONICAL_MKV": 1, "MISSING_SIDECAR": 1})
        self.assertEqual(steps["subtitles"]["counts"], {"missing": 1, "present": 1})

    def test_recorded_tells_nothing_to_do_apart_from_nobody_measured(self) -> None:
        """The footnote the printed report spells out, as a field."""
        alpha = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        steps = {step["id"]: step for step in self._json()[1]["steps"]}
        self.assertFalse(steps["bit-depth"]["recorded"])
        self.assertEqual(steps["bit-depth"]["unmeasured"], 1)

        self._record(alpha, KIND_BITDEPTH, "SKIP_HDR")
        steps = {step["id"]: step for step in self._json()[1]["steps"]}
        self.assertTrue(steps["bit-depth"]["recorded"])
        self.assertEqual(steps["bit-depth"]["counts"], {"SKIP_HDR": 1})
        self.assertEqual(steps["bit-depth"]["unmeasured"], 0)

    def test_a_stale_verdict_is_counted_as_stale_not_as_an_answer(self) -> None:
        alpha = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self._record(alpha, KIND_REMUX, "cleaned")
        alpha.write_bytes(b"y" * 8192)  # the bytes the verdict described are gone
        remux = {step["id"]: step for step in self._json()[1]["steps"]}["remux"]
        self.assertEqual(remux["stale"], 1)
        self.assertEqual(remux["counts"], {})

    def test_the_state_cache_reports_whether_it_was_used(self) -> None:
        alpha = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self._record(alpha, KIND_BITDEPTH, "SKIP_HDR")
        self.assertEqual(self._json()[1]["state_cache"], {"enabled": True, "measured": True})
        self.assertEqual(self._json("--no-state")[1]["state_cache"],
                         {"enabled": False, "measured": False})

    def test_a_missing_library_is_reported_as_json_not_a_bare_stderr_line(self) -> None:
        """A caller that asked for a document must not have to parse two formats."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = organize.main(["status", "--library", str(self.root / "gone"), "--json"])
        document = json.loads(out.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(document["exit_code"], 2)
        self.assertEqual(document["error"]["kind"], "library-not-found")
        self.assertIn("gone", document["error"]["message"])
        self.assertEqual(document["steps"], [])
        self.assertEqual(document["movies"], 0)

    def test_a_failing_scan_is_reported_as_json_too(self) -> None:
        import library_auditor

        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        out = io.StringIO()
        with patch.object(library_auditor, "audit_library", side_effect=RuntimeError("disk gone")), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = organize.main(["status", "--library", str(self.library),
                                  "--state-db", str(self.db), "--json"])
        document = json.loads(out.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(document["error"]["kind"], "scan-failed")
        self.assertIn("disk gone", document["error"]["message"])

    def test_a_healthy_run_carries_no_error(self) -> None:
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        document = self._json()[1]
        self.assertIsNone(document["error"])
        self.assertEqual(document["exit_code"], 0)

    def test_two_runs_over_an_unchanged_library_are_byte_identical(self) -> None:
        """No scan duration in the document, so a cron job can diff it."""
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        first, second = self._json()[1], self._json()[1]
        self.assertEqual(json.dumps(first), json.dumps(second))
        self.assertNotIn("elapsed", json.dumps(first))

    def test_the_document_is_serialisable_without_a_custom_encoder(self) -> None:
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        status = self._summary([self._folder("Alpha (2001)", "CANONICAL_MKV")])
        json.dumps(organize.status_document(status, state_enabled=True))

    def test_verbose_keeps_the_scan_log_off_stdout(self) -> None:
        self._movie("Bravo (2002)")  # a non-canonical folder the auditor narrates
        code, document, err = self._json("--verbose")
        self.assertEqual(code, 0)
        self.assertEqual(document["movies"], 1)
        self.assertTrue(err.strip(), "the scan log should still be shown, on stderr")

    def test_both_parsers_describe_the_same_status_flags(self) -> None:
        top = organize.build_parser()
        action = next(a for a in top._subparsers._group_actions if "status" in a.choices)  # noqa: SLF001
        advertised = {opt for act in action.choices["status"]._actions for opt in act.option_strings}  # noqa: SLF001
        dispatched = {opt for act in organize.add_status_arguments(
            organize.argparse.ArgumentParser(prog="organize status"))._actions  # noqa: SLF001
            for opt in act.option_strings}
        self.assertEqual(advertised, dispatched)


if __name__ == "__main__":
    unittest.main()
