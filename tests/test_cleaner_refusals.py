"""What the cleaner does with a movie it will not touch, and the report that says why.

``process_mkv`` has one job with two halves: decide, and then either rewrite the
movie or explain itself. The explaining half is the part that only runs when
something is already wrong - a source that cannot be stat'ed, a container
mkvmerge does not recognise, metadata that is not JSON, a layout that breaks the
one-MKV-per-folder contract, a film with no retainable audio - and it is the
half an operator actually reads, in the report and on the console line.

Each refusal is asserted three ways, because all three are the contract:

1. the movie's bytes are unchanged and no staging file or journal is left;
2. the run's own bucket records the movie and the reason, which is what the
   report and ``organize status`` are built from;
3. the reason is printed - once with a console installed and once without, since
   those are two different renderings of the same sentence and only one of them
   reaches a log file.

Also here: the report's own failure paths (a report directory nobody can write
must not lose the run), and the last few defensive branches in the discovery
sweep and the lock reclaim.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import fake_mkvmerge as fake

import mkv_track_cleaner as tc

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_UNSET = object()

MOVIE_BYTES = b"ORIGINAL-MOVIE-BYTES" * 400   # 8000 bytes
REMUXED_BYTES = b"REMUXED-MOVIE-BYTES-" * 300  # 6000 bytes

DIRTY = fake.make_spec([
    fake.video_track(),
    fake.audio_track(default=True),
    fake.audio_track(name="Director Commentary", codec="AC-3", codec_id="A_AC3",
                     channels=2, commentary=True),
])
CLEAN_OUTPUT = fake.make_spec([fake.video_track(), fake.audio_track(default=True)])
COMMENTARY_ONLY = fake.make_spec([
    fake.video_track(),
    fake.audio_track(name="Director Commentary", codec="AC-3", codec_id="A_AC3",
                     channels=2, commentary=True),
    fake.audio_track(name="Audio Description", codec="AC-3", codec_id="A_AC3", channels=2),
])
FRENCH_FILM = fake.make_spec([
    fake.video_track(),
    fake.audio_track(name="French", language="fra", codec="DTS", codec_id="A_DTS",
                     channels=6, default=True),
    fake.audio_track(name="French", language="fra", codec="AC-3", codec_id="A_AC3",
                     channels=2),
])
FRENCH_KEPT = fake.make_spec([
    fake.video_track(),
    fake.audio_track(name="French", language="fra", codec="DTS", codec_id="A_DTS",
                     channels=6, default=True),
])
GOOD_SRT = "1\n00:00:01,000 --> 00:00:04,000\nHello.\n\n2\n00:00:05,000 --> 00:00:08,000\nBye.\n\n"


def stats() -> dict:
    return {"start_time": datetime.datetime.now(),
            "cleaned": [], "already_clean": [], "skipped_no_english": [], "skipped_layout": [],
            "skipped_sidecar": [], "deferred_hardlinked": [], "errors": [],
            "remux_without_srt": [], "keeper_needs_audiofit": [], "diagnostics": [],
            "total_scanned": 0, "total_space_saved_bytes": 0}


class ProcessMkvRefusalTests(unittest.TestCase):
    """One movie, one injected fault, and the three things that must be true."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cleaner_refusal_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.library = self.root / "library"
        self.folder = self.library / "Film (2020)"
        self.folder.mkdir(parents=True)
        self.movie = self.folder / "Film (2020).mkv"
        self.movie.write_bytes(MOVIE_BYTES)
        self.log = self.root / "out" / "cleaner.log"
        self._console = tc._console
        self._target_root = tc._target_root
        self._temp = tc._active_temp_file
        self._interrupt = tc._interrupt_requested
        self.addCleanup(self._restore)
        self.info = DIRTY
        self.output = CLEAN_OUTPUT
        self.rc = 0
        self.stdout_override: str | None = None
        self.stderr = ""
        self.remux_raises: Exception | None = None
        self.inspect_raises: Exception | None = None
        self.remuxes = 0
        patch = mock.patch.object(tc, "_run_mkvmerge", self._fake_mkvmerge)
        patch.start()
        self.addCleanup(patch.stop)

    def _restore(self) -> None:
        tc._console = self._console
        tc._target_root = self._target_root
        tc._active_temp_file = self._temp
        tc._interrupt_requested = self._interrupt

    def _fake_mkvmerge(self, cmd: list[str], on_progress=None) -> tuple[int, str, str]:
        if "-J" not in cmd and self.remux_raises is not None:
            raise self.remux_raises
        if "-J" in cmd:
            if self.inspect_raises is not None:
                raise self.inspect_raises
            target = Path(cmd[cmd.index("-J") + 1])
            payload = self.output if target.name.startswith(tc.TEMP_PREFIX) else self.info
            stdout = json.dumps(payload) if self.stdout_override is None else self.stdout_override
            return self.rc, stdout, self.stderr
        self.remuxes += 1
        Path(cmd[cmd.index("-o") + 1]).write_bytes(REMUXED_BYTES)
        return 0, "", ""

    # -- the harness -------------------------------------------------------

    def _process(self, *, console: bool, dry_run: bool = False,
                 library_root: Path | None | object = _UNSET) -> tuple[dict, str, str]:
        """Run one movie through the real ``process_mkv``, twice-rendered.

        Returns the run's stats, whatever the console drew, and the log file.
        ``library_root=None`` runs the tool the way a caller that has no library
        contract does, which is the only way to reach the refusals that sit
        behind the layout check.
        """
        drawn = io.StringIO()
        tc._target_root = self.library if library_root is _UNSET else library_root  # type: ignore[assignment]
        if console:
            with redirect_stdout(drawn):
                tc._console = tc.LiveConsole(use_color=False)
                result = self._call(dry_run)
                console_text = drawn.getvalue()
            tc._console = None
        else:
            with redirect_stdout(drawn):
                result = self._call(dry_run)
                console_text = drawn.getvalue()
        logged = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        return result, console_text, logged

    def _call(self, dry_run: bool) -> dict:
        collected = stats()
        tc.process_mkv(self.movie, collected, "mkvmerge", dry_run=dry_run,
                       log_file_path=str(self.log), file_index=1, file_total=1)
        return collected

    def _artifacts(self) -> list[str]:
        return sorted(p.name for p in self.folder.iterdir()
                      if p.name.startswith((tc.TEMP_PREFIX, tc.TRANSACTION_MARKER)))

    # -- refusals ----------------------------------------------------------

    def test_a_movie_that_cannot_be_stat_ed_is_refused_without_a_snapshot(self) -> None:
        """The remux is verified against a snapshot of the source taken first.

        With no snapshot there is nothing to compare the output to, so the only
        safe answer is to leave the movie alone - even though the file is
        plainly there and mkvmerge might have read it fine.
        """
        real_stat = Path.stat

        def unstatable(path: Path, **kwargs: object) -> object:
            if path == self.movie:
                raise OSError("share went away")
            return real_stat(path, **kwargs)  # type: ignore[arg-type]

        for console in (False, True):
            with self.subTest(console=console), mock.patch.object(Path, "stat", unstatable):
                # No library root: the layout check would otherwise report this
                # as "not a regular file" and never reach the snapshot refusal.
                collected, drawn, logged = self._process(console=console, library_root=None)
            self.assertIn("could not stat source file", collected["errors"][0]["error"])
            self.assertIn("refusing to remux without a stable source snapshot",
                          collected["errors"][0]["error"])
            self.assertEqual(self._artifacts(), [])
            if console:
                self.assertIn("could not stat source file", drawn)
            else:
                self.assertIn("could not stat source file", logged)

    def test_a_movie_outside_the_canonical_layout_is_skipped(self) -> None:
        """One MKV per folder, named like the folder: the contract the report states.

        Remuxing a movie whose folder holds two features would pick one of them
        to replace and leave the library in a state nobody asked for, so the
        layout is checked before anything is read.
        """
        second = self.folder / "Film (2020).mp4"
        second.write_bytes(b"y" * 4096)
        for console in (False, True):
            with self.subTest(console=console):
                collected, drawn, logged = self._process(console=console)
            entry = collected["skipped_layout"][0]
            self.assertIn("expected one regular movie file in movie folder, found 2",
                          entry["reason"])
            self.assertTrue(second.is_file(), "the second feature was not touched either")
            self.assertEqual(self._artifacts(), [])
            self.assertIn("noncanonical layout", (drawn or logged))
        second.unlink()

    def test_a_metadata_read_that_fails_is_an_error_row_carrying_mkvmerges_own_words(self) -> None:
        self.rc = 2
        self.stderr = "Error: 'Film (2020).mkv' is not a Matroska file."
        for console in (False, True):
            with self.subTest(console=console):
                collected, drawn, logged = self._process(console=console)
            self.assertIn("is not a Matroska file", collected["errors"][0]["error"])
            self.assertEqual(self._artifacts(), [])
            self.assertIn("Metadata inspection failed", logged)
            self.assertIn("is not a Matroska file", drawn if console else logged)

    def test_a_container_mkvmerge_does_not_support_is_an_error_row(self) -> None:
        self.info = dict(DIRTY, container={"recognized": True, "supported": False,
                                           "properties": {}})
        for console in (False, True):
            with self.subTest(console=console):
                collected, drawn, logged = self._process(console=console)
            self.assertIn("not a recognized/supported media container",
                          collected["errors"][0]["error"])
            self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES)

    def test_metadata_that_is_not_json_is_an_error_row(self) -> None:
        """A truncated payload is a real answer from a dying multiplexer."""
        self.stdout_override = '{"container": {"recognized": true, '
        for console in (False, True):
            with self.subTest(console=console):
                collected, drawn, logged = self._process(console=console)
            self.assertIn("Invalid mkvmerge JSON", collected["errors"][0]["error"])
            self.assertEqual(self._artifacts(), [])

    def test_an_inspection_that_raises_is_one_error_row_and_not_the_end_of_the_queue(self) -> None:
        self.inspect_raises = RuntimeError("mkvmerge exploded")
        for console in (False, True):
            with self.subTest(console=console):
                collected, drawn, logged = self._process(console=console)
            self.assertIn("Metadata inspection exception", collected["errors"][0]["error"])
            self.assertIn("mkvmerge exploded", collected["errors"][0]["error"])

    def test_an_interrupt_during_inspection_is_not_reported_as_a_bad_movie(self) -> None:
        """Ctrl-C is the operator, not a fault in the file."""
        def interrupting(cmd: list[str], on_progress=None) -> tuple[int, str, str]:
            raise KeyboardInterrupt

        with mock.patch.object(tc, "_run_mkvmerge", interrupting), \
                redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            tc.process_mkv(self.movie, stats(), "mkvmerge", log_file_path=str(self.log))
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES)
        self.assertEqual(self._artifacts(), [])

    def test_a_film_with_no_retainable_audio_is_skipped_with_the_reason(self) -> None:
        """Policy, not a fault: every track is commentary or descriptive audio.

        Keeping the commentary because it is the only track left would produce a
        movie nobody can watch, so the file is left exactly as it is and the
        report names it.
        """
        self.info = COMMENTARY_ONLY
        self.output = COMMENTARY_ONLY
        for console in (False, True):
            with self.subTest(console=console):
                collected, drawn, logged = self._process(console=console)
            entry = collected["skipped_no_english"][0]
            self.assertIn("every audio track is commentary/descriptive or a titled dub",
                          entry["reason"])
            self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES)
            self.assertIn("commentary", (drawn or logged))

    def test_a_film_with_no_audio_at_all_is_skipped(self) -> None:
        self.info = fake.make_spec([fake.video_track()])
        collected, _drawn, _logged = self._process(console=False)
        self.assertEqual(collected["skipped_no_english"][0]["reason"], "no audio track to retain")

    def test_a_foreign_film_with_a_validated_sidecar_keeps_its_own_audio(self) -> None:
        """The one case where a non-English keeper is the right answer.

        A French film with an English ``.eng.srt`` beside it is watchable: the
        sidecar carries the language, so the tool keeps the best French track,
        strips the embedded subtitles the sidecar replaces, and says so in the
        log rather than silently doing something that looks like a mistake.
        """
        self.info = FRENCH_FILM
        self.output = FRENCH_KEPT
        sidecar = self.folder / "Film (2020).eng.srt"
        sidecar.write_text(GOOD_SRT, encoding="utf-8")
        collected, _drawn, logged = self._process(console=False)
        self.assertEqual([entry["name"] for entry in collected["cleaned"]], ["Film (2020).mkv"])
        self.assertIn("Foreign / non-English audio film with validated external English SRT",
                      logged)
        self.assertEqual(self.movie.read_bytes(), REMUXED_BYTES)

    def test_a_dry_run_reports_the_decision_and_changes_nothing(self) -> None:
        collected, _drawn, logged = self._process(console=False, dry_run=True)
        self.assertEqual([entry["name"] for entry in collected["cleaned"]], ["Film (2020).mkv"])
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES)
        self.assertEqual(self.remuxes, 0)
        self.assertEqual(self._artifacts(), [])
        self.assertIn("DRY", logged.upper())

    def test_a_seeded_movie_is_deferred_and_never_remuxed(self) -> None:
        """The hard policy: there is no flag that forces this.

        qBittorrent's "stop seeding" only pauses the torrent, so the source copy
        can stay hardlinked forever; rewriting the file would change what every
        peer is serving.
        """
        os.link(self.movie, self.root / "seed-copy.mkv")
        collected, drawn, logged = self._process(console=True)
        self.assertEqual(collected["deferred_hardlinked"][0]["hardlinks"], 2)
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES)
        self.assertEqual(self.remuxes, 0)
        self.assertIn("hardlinks", drawn)
        collected, _drawn, logged = self._process(console=False)
        self.assertIn("There is no flag to force it", logged)

    def test_a_movie_that_is_already_clean_is_not_remuxed_again(self) -> None:
        self.info = CLEAN_OUTPUT
        self.output = CLEAN_OUTPUT
        collected, _drawn, _logged = self._process(console=False)
        self.assertEqual(collected["already_clean"], ["Film (2020).mkv"])
        self.assertEqual(self.remuxes, 0)
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES)

    def test_a_verification_failure_leaves_the_original_and_records_a_diagnostic(self) -> None:
        """The remux happened; the promotion did not. That distinction is the tool."""
        with mock.patch.object(tc, "verify_remux_output",
                               lambda path, binary, plan: (False, "the audio changed", None)):
            collected, _drawn, logged = self._process(console=False)
        self.assertEqual(self.remuxes, 1, "the remux really ran")
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES, "and the movie was not replaced")
        self.assertEqual(self._artifacts(), [], "the staging file and journal were cleaned up")
        self.assertIn("Post-remux verification failed: the audio changed",
                      collected["errors"][0]["error"])
        self.assertIn("Verification diagnostic", logged)

    def test_a_source_that_changed_during_the_remux_is_not_replaced(self) -> None:
        """The ingest hook can land a better release while a sweep is mid-encode."""
        real_verify = tc.verify_remux_output

        def verified_then_changed(path: Path, binary: str, plan: dict) -> tuple:
            # The remux is finished and verified; the source is replaced a
            # moment later, which is exactly the window the re-check covers.
            outcome = real_verify(path, binary, plan)
            os.utime(self.movie, (1_000_000_000, 1_000_000_000))
            return outcome

        with mock.patch.object(tc, "verify_remux_output", verified_then_changed):
            collected, _drawn, logged = self._process(console=False)
        self.assertIn("source changed while remuxing; refusing to replace it",
                      collected["errors"][0]["error"])
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES)
        self.assertEqual(self._artifacts(), [])

    def test_a_sidecar_that_changed_during_the_remux_is_not_replaced(self) -> None:
        """The remux was verified with that sidecar; a different one invalidates it."""
        sidecar = self.folder / "Film (2020).eng.srt"
        sidecar.write_text(GOOD_SRT, encoding="utf-8")
        real_verify = tc.verify_remux_output

        def verified_then_rewritten(path: Path, binary: str, plan: dict) -> tuple:
            outcome = real_verify(path, binary, plan)
            sidecar.write_text("1\n00:00:09,000 --> 00:00:10,000\nDifferent.\n\n",
                               encoding="utf-8")
            return outcome

        with mock.patch.object(tc, "verify_remux_output", verified_then_rewritten):
            collected, _drawn, _logged = self._process(console=False)
        self.assertIn("validated external SRT changed or became invalid while remuxing",
                      collected["errors"][0]["error"])
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES)

    def test_an_unexpected_failure_after_the_journal_is_written_is_still_one_error_row(self) -> None:
        """One bad movie is reported and the queue moves on - and nothing is left behind."""
        self.remux_raises = None
        with mock.patch.object(tc, "safe_replace",
                               mock.Mock(side_effect=RuntimeError("the volume vanished"))):
            collected, _drawn, logged = self._process(console=False)
        self.assertIn("the volume vanished", collected["errors"][0]["error"])
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES)
        self.assertEqual(self._artifacts(), [], "the transaction was cleaned up on the way out")
        self.assertIn("Exception processing", logged)


class ReportFailureTests(unittest.TestCase):
    """The report is the run's only durable answer; writing it must not fail the run."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cleaner_report_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.report = self.root / "out" / "cleaner_report.txt"
        capture = redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def _stats_with(self, **buckets: object) -> dict:
        collected = stats()
        collected.update(buckets)
        return collected

    def test_a_layout_skip_is_its_own_report_section_with_the_fix_in_it(self) -> None:
        """A skipped movie has to say what to do about it, not just that it was skipped."""
        collected = self._stats_with(skipped_layout=[
            {"name": "Film (2020).mkv", "reason": "noncanonical layout: found 2"}])
        text = tc.generate_and_save_report(collected, dry_run=False,
                                           report_file=str(self.report), log_file_path=None)
        self.assertIn("SKIPPED (LAYOUT CONTRACT)", text)
        self.assertIn("Run movie_standardizer.py", text)
        self.assertIn("Film (2020).mkv", text)
        self.assertIn("noncanonical layout: found 2", text)
        self.assertEqual(self.report.read_text(encoding="utf-8"), text)

    def test_a_film_with_no_retainable_audio_is_listed_with_its_reason(self) -> None:
        collected = self._stats_with(skipped_no_english=[
            {"name": "Le Film (1999).mkv", "reason": "every audio track is commentary"}])
        text = tc.generate_and_save_report(collected, dry_run=False,
                                           report_file=str(self.report), log_file_path=None)
        self.assertIn("SKIPPED (FOREIGN / NO ENGLISH AUDIO)", text)
        self.assertIn("Le Film (1999).mkv", text)
        self.assertIn("every audio track is commentary", text)

    def test_a_broken_sidecar_gets_its_own_section(self) -> None:
        collected = self._stats_with(skipped_sidecar=[
            {"name": "Film (2020).mkv", "reason": "external SRT is empty"}])
        text = tc.generate_and_save_report(collected, dry_run=False,
                                           report_file=str(self.report), log_file_path=None)
        self.assertIn("SKIPPED (BROKEN .ENG.SRT)", text)
        self.assertIn("external SRT is empty", text)

    def test_a_report_directory_nobody_can_write_is_an_error_line_not_a_crash(self) -> None:
        """The sweep has already finished; losing the report must not lose the run.

        ``generate_and_save_report`` is called from ``main``'s finally-shaped
        paths, including the fatal-error one, so a raise here would replace
        whatever went wrong with an OSError about the report.
        """
        log = self.root / "report.log"
        with mock.patch.object(tc.os, "replace", side_effect=OSError("read-only share")):
            text = tc.generate_and_save_report(stats(), dry_run=False,
                                               report_file=str(self.report),
                                               log_file_path=str(log))
        self.assertTrue(text, "the rendered report is still returned to the caller")
        self.assertIn("Failed to save summary report", log.read_text(encoding="utf-8"))

    def test_a_staging_report_that_cannot_be_removed_does_not_fail_the_publish(self) -> None:
        with mock.patch.object(Path, "unlink", side_effect=OSError("share went away")):
            text = tc.generate_and_save_report(stats(), dry_run=False,
                                               report_file=str(self.report), log_file_path=None)
        self.assertEqual(self.report.read_text(encoding="utf-8"), text)

    def test_a_deferred_movie_explains_that_there_is_no_flag_to_force_it(self) -> None:
        collected = self._stats_with(deferred_hardlinked=[
            {"name": "Film (2020).mkv", "hardlinks": 2}])
        text = tc.generate_and_save_report(collected, dry_run=False,
                                           report_file=str(self.report), log_file_path=None)
        self.assertIn("2 hardlinks", text)
        self.assertIn("no flag to force it", text)


class LeftoverBranchTests(unittest.TestCase):
    """The last few defensive branches, each with the condition that reaches it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cleaner_leftover_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        capture = redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_a_replace_given_no_attempts_at_all_reports_failure(self) -> None:
        """``max_retries=0`` is a legal argument and must not raise or lie."""
        src = self.root / "a.mkv"
        src.write_bytes(b"x")
        self.assertFalse(tc.safe_replace(src, self.root / "b.mkv", max_retries=0))
        self.assertTrue(src.is_file(), "nothing was moved")

    def test_an_interrupt_during_verification_is_not_a_verification_failure(self) -> None:
        with mock.patch.object(tc, "_run_mkvmerge", mock.Mock(side_effect=KeyboardInterrupt)), \
                self.assertRaises(KeyboardInterrupt):
            tc.verify_remux_output(self.root / "temp.mkv", "mkvmerge", {})

    def test_a_lock_reclaimed_twice_in_a_row_gives_up_instead_of_spinning(self) -> None:
        """Two runs can both find the same stale lock; the second one must lose cleanly.

        The loop is bounded at two attempts precisely so a race for a reclaimed
        lock ends in "this run does not start" rather than in an unbounded
        delete/create fight over a file on a network share.
        """
        lock = self.root / tc.LOCK_FILENAME

        def always_taken(*args: object, **kwargs: object) -> int:
            raise FileExistsError("already there")

        attempts = {"n": 0}

        def counted_unlink(path: Path, missing_ok: bool = False) -> None:
            attempts["n"] += 1

        lock.write_text(f"{tc._this_hostname()}\n4000000\n1\n", encoding="utf-8")
        with mock.patch.object(tc.os, "open", always_taken), \
                mock.patch.object(tc, "_pid_alive", lambda pid: False), \
                mock.patch.object(Path, "unlink", counted_unlink):
            self.assertFalse(tc.acquire_lock(lock, log_file_path=None))
        self.assertEqual(attempts["n"], 2, "two reclaims, then it stops")

    def test_an_interrupt_during_a_sweep_of_orphans_is_honoured_between_files(self) -> None:
        """A Ctrl-C between two orphans stops the sweep rather than finishing it."""
        for index in range(2):
            legacy = self.root / f"{tc.TEMP_PREFIX}Film (202{index}).mkv"
            legacy.write_bytes(b"junk")
            original = self.root / f"Film (202{index}).mkv"
            original.write_bytes(MOVIE_BYTES)
            old = time.time() - tc.ORPHAN_MIN_AGE_SECONDS - 60
            os.utime(legacy, (old, old))

        real_delete = tc.safe_delete

        def delete_and_interrupt(path: Path, **kwargs: object) -> None:
            real_delete(path, **kwargs)
            tc._interrupt_requested = True

        try:
            with mock.patch.object(tc, "safe_delete", delete_and_interrupt):
                handled = tc.cleanup_orphan_temps(self.root, "mkvmerge", log_file_path=None)
        finally:
            tc._interrupt_requested = False
        self.assertEqual(handled, 1, "the first orphan was reclaimed and then the sweep stopped")
        self.assertEqual(len([p for p in self.root.iterdir()
                              if p.name.startswith(tc.TEMP_PREFIX)]), 1)

    def test_an_interrupt_during_orphan_recovery_is_raised_not_swallowed(self) -> None:
        """The recovery loop catches broad exceptions; an interrupt is not one of them."""
        token = "9" * 32
        temp = self.root / f"{tc.TEMP_PREFIX}{token}__Film (2020).mkv"
        temp.write_bytes(REMUXED_BYTES)
        old = time.time() - tc.ORPHAN_MIN_AGE_SECONDS - 60
        os.utime(temp, (old, old))
        journal = tc._transaction_journal_path(self.root, token)
        journal.write_text(json.dumps({
            "schema": tc.TRANSACTION_SCHEMA_VERSION, "token": token, "phase": "verified",
            "source_name": "Film (2020).mkv", "temp_name": temp.name,
            "temp_snapshot": tc.source_snapshot(temp), "verification_plan": {},
        }), encoding="utf-8")
        with mock.patch.object(tc, "verify_remux_output",
                               mock.Mock(side_effect=KeyboardInterrupt)), \
                self.assertRaises(KeyboardInterrupt):
            tc.cleanup_orphan_temps(self.root, "mkvmerge", log_file_path=None)
        self.assertTrue(temp.is_file(), "an interrupted recovery promotes nothing")

    def test_an_interrupt_during_the_scan_stops_it_between_files(self) -> None:
        folder = self.root / "library" / "Alpha (2001)"
        folder.mkdir(parents=True)
        first = folder / "Alpha (2001).mkv"
        first.write_bytes(b"x")
        (folder / "Alpha (2001)-2.mkv").write_bytes(b"y")
        real_stat = Path.stat
        seen = {"n": 0}

        def stat_then_interrupt(path: Path, **kwargs: object) -> object:
            result = real_stat(path, **kwargs)  # type: ignore[arg-type]
            seen["n"] += 1
            tc._interrupt_requested = True
            return result

        try:
            with mock.patch.object(Path, "stat", stat_then_interrupt):
                found, _sizes, _total = tc.discover_mkv_files(self.root, None)
        finally:
            tc._interrupt_requested = False
        self.assertEqual(len(found), 1, "the scan stopped after the file it was on")

    def test_a_status_detail_for_an_entry_that_says_nothing_is_empty(self) -> None:
        for entry in ({}, {"name": "Film (2020).mkv"}, "not a mapping", None):
            with self.subTest(entry=entry):
                self.assertEqual(tc._verdict_detail(entry), "")

    def test_a_gui_progress_line_with_a_zero_denominator_is_not_a_percentage(self) -> None:
        self.assertIsNone(tc._parse_mkvmerge_progress("#GUI#progress#parts=3/0"))

    def test_a_gui_line_that_is_neither_progress_nor_a_message_is_dropped_from_the_summary(self) -> None:
        """``--gui-mode`` emits bookkeeping lines; only the error is worth printing."""
        summary = tc._summarize_mkvmerge_failure(
            "#GUI#progress 10%\n#GUI#message#progress\nError: the real problem\n", 1)
        self.assertEqual(summary, "Error: the real problem")


if __name__ == "__main__":
    unittest.main()
