"""Crash safety: pull the plug at every dangerous instant and check the library.

This repo's central promise is that a power cut, a `Ctrl-C`, or a killed
process can never cost you a movie. Until now that promise was argued in
prose and in code comments. This suite executes it.

The method is fault injection, not narration: for each point where a tool is
mid-transaction — after the journal is written, after the remux finishes,
after verification, between staging and `os.replace` — the process is
"killed" (an exception raised from the exact call the crash would interrupt),
and then the two questions that actually matter are asked of the filesystem:

1. **Is anything lost or half-written right now?** The original must be intact
   and byte-identical, or already fully replaced. There is no third state.
2. **Does the next run clean it up?** A crash may leave staging files behind;
   what it may not do is leave them behind *forever*, or promote something that
   was never verified.

``mkv_track_cleaner.py`` gets the most attention here because it is the only
tool that rewrites and deletes movie files.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import library_auditor
import mkv_track_cleaner as tc
import sync_subtitles as ss
from organizekit import core

# Both must clear the tool's own truncation guards: an output under 1 KiB, or
# under half the source, is rejected as a botched remux before it is promoted.
MOVIE_BYTES = b"ORIGINAL-MOVIE-BYTES" * 400   # 8000 bytes
REMUXED_BYTES = b"REMUXED-MOVIE-BYTES-" * 300  # 6000 bytes, a plausible saving
SRT = "1\n00:00:05,000 --> 00:00:07,000\nHello.\n\n"
SHIFTED_SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello.\n\n"

SOURCE_INFO = {
    "container": {"recognized": True, "supported": True,
                  "properties": {"duration": 6_000_000_000_000}},
    "tracks": [
        {"id": 0, "type": "video", "codec": "AVC/H.264/MPEG-4p10", "properties": {
            "codec_id": "V_MPEG4/ISO/AVC", "pixel_dimensions": "1920x1080",
            "display_dimensions": "1920x1080", "tag_number_of_frames": "144000",
            "flag_default": True}},
        {"id": 1, "type": "audio", "codec": "TrueHD", "properties": {
            "codec_id": "A_TRUEHD", "language": "eng", "language_ietf": "en",
            "track_name": "English TrueHD 7.1", "audio_channels": 8,
            "audio_sampling_frequency": 48000, "flag_default": True}},
        {"id": 2, "type": "audio", "codec": "AC-3", "properties": {
            "codec_id": "A_AC3", "language": "eng", "language_ietf": "en",
            "track_name": "Director Commentary", "audio_channels": 2,
            "audio_sampling_frequency": 48000, "flag_commentary": True}},
    ],
    "attachments": [], "chapters": [],
}
OUTPUT_INFO = {
    "container": SOURCE_INFO["container"],
    "tracks": SOURCE_INFO["tracks"][:2],
    "attachments": [], "chapters": [],
}


class Crash(BaseException):
    """The power cut, simulated faithfully.

    Deliberately **not** an ``Exception``: a real power cut runs no handler at
    all, and every one of these tools wraps its work in ``except Exception`` to
    turn a bad movie into a reported error rather than a dead run. Raising an
    ordinary exception would therefore test the error path, not the crash path
    - the tool would tidy up on its way out and the filesystem would never see
    the state a crash actually leaves behind.
    """


class RemuxCrashTests(unittest.TestCase):
    """Kill the remux at each step of its transaction and inspect the library."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="crash_remux_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.movie = self.root / "Film (2020).mkv"
        self.movie.write_bytes(MOVIE_BYTES)
        os.utime(self.movie, (1_600_000_000, 1_600_000_000))
        self.remux_calls = 0

        def fake_mkvmerge(cmd, on_progress=None):
            if "-J" in cmd:
                target = Path(cmd[cmd.index("-J") + 1])
                info = OUTPUT_INFO if target.name.startswith(tc.TEMP_PREFIX) else SOURCE_INFO
                return 0, json.dumps(info), ""
            self.remux_calls += 1
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_bytes(REMUXED_BYTES)
            return 0, "", ""

        self._real_mkvmerge = tc._run_mkvmerge
        self._real_root = tc._target_root
        tc._run_mkvmerge = fake_mkvmerge
        tc._target_root = None
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._run_mkvmerge = self._real_mkvmerge
        tc._target_root = self._real_root
        tc._active_temp_file = None

    # -- helpers -----------------------------------------------------------

    def _stats(self) -> dict:
        return {"cleaned": [], "already_clean": [], "skipped_no_english": [],
                "skipped_layout": [], "deferred_hardlinked": [], "errors": [],
                "remux_without_srt": [], "total_scanned": 0,
                "total_space_saved_bytes": 0}

    def _process(self, expect_crash: bool = False) -> dict:
        stats = self._stats()
        with contextlib.redirect_stdout(io.StringIO()):
            if expect_crash:
                with self.assertRaises(Crash):
                    tc.process_mkv(self.movie, stats, "mkvmerge", dry_run=False,
                                   log_file_path=None)
            else:
                tc.process_mkv(self.movie, stats, "mkvmerge", dry_run=False,
                               log_file_path=None)
        return stats

    def _artifacts(self) -> tuple[list[Path], list[Path]]:
        temps = sorted(p for p in self.root.iterdir() if p.name.startswith(tc.TEMP_PREFIX))
        journals = sorted(p for p in self.root.iterdir()
                          if p.name.startswith(tc.TRANSACTION_MARKER))
        return temps, journals

    def _recover(self, *, aged: bool = True) -> int:
        """Run orphan recovery, optionally as if the crash were minutes ago.

        The staging file is aged by moving the *clock*, not by back-dating the
        file with ``utime``: the journal fingerprints the temp by mtime, so
        touching it would fail the tamper check for the wrong reason and hide
        whatever the recovery logic would really have done.
        """
        elapsed = tc.ORPHAN_MIN_AGE_SECONDS + 60 if aged else 0.0
        later = time.time() + elapsed
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(tc.time, "time", lambda: later):
            return tc.cleanup_orphan_temps(self.root, "mkvmerge", log_file_path=None)

    def _assert_original_intact(self) -> None:
        self.assertTrue(self.movie.is_file(), "the movie must never disappear")
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES,
                         "a crashed remux must leave the original byte-identical")

    # -- the crash matrix --------------------------------------------------

    def test_crash_while_remuxing_leaves_the_original_untouched(self) -> None:
        def crashing(cmd, on_progress=None):
            if "-J" in cmd:
                return 0, json.dumps(SOURCE_INFO), ""
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"half a movie")
            raise Crash("power cut mid-remux")

        tc._run_mkvmerge = crashing
        self._process(expect_crash=True)
        self._assert_original_intact()
        temps, journals = self._artifacts()
        self.assertEqual(len(temps), 1, "the half-written output is a staging file")
        self.assertEqual(len(journals), 1, "and the journal records the transaction")
        self.assertEqual(tc.read_transaction(journals[0])["phase"], "remuxing")

    def test_the_next_run_cleans_up_after_that_crash(self) -> None:
        self.test_crash_while_remuxing_leaves_the_original_untouched()
        self.assertEqual(self._recover(), 1)
        self.assertEqual(self._artifacts(), ([], []),
                         "no staging file may survive a run that saw an intact original")
        self._assert_original_intact()

    def test_a_fresh_staging_file_is_left_alone_by_recovery(self) -> None:
        """A concurrent, still-running remux must not be swept out from under."""
        self.test_crash_while_remuxing_leaves_the_original_untouched()
        self.assertEqual(self._recover(aged=False), 0,
                         "younger than the orphan age: not abandoned")
        temps, journals = self._artifacts()
        self.assertEqual((len(temps), len(journals)), (1, 1))

    def test_crash_after_verification_before_the_swap(self) -> None:
        # The most dangerous instant: a fully verified replacement exists but
        # the original has not been replaced yet.
        with mock.patch.object(tc, "safe_replace", side_effect=Crash("power cut mid-swap")):
            self._process(expect_crash=True)
        self._assert_original_intact()
        temps, journals = self._artifacts()
        self.assertEqual(len(temps), 1)
        journal = tc.read_transaction(journals[0])
        self.assertEqual(journal["phase"], "verified")
        self.assertIn("temp_snapshot", journal)

        # Recovery sees an intact original, so it discards the replacement
        # rather than swapping in work nobody asked it to finish.
        self.assertEqual(self._recover(), 1)
        self.assertEqual(self._artifacts(), ([], []))
        self._assert_original_intact()

    def test_a_verified_remux_whose_original_vanished_is_recovered(self) -> None:
        """The one case where recovery promotes: journal-proven and re-verified."""
        with mock.patch.object(tc, "safe_replace", side_effect=Crash("power cut mid-swap")):
            self._process(expect_crash=True)
        self.movie.unlink()  # the swap half-happened, or an operator intervened
        self.assertEqual(self._recover(), 1)
        self.assertEqual(self.movie.read_bytes(), REMUXED_BYTES)
        self.assertEqual(self._artifacts(), ([], []))

    def test_an_unverified_remux_is_never_promoted(self) -> None:
        """A recognisable MKV is not evidence that it passed the checks."""
        def crashing(cmd, on_progress=None):
            if "-J" in cmd:
                return 0, json.dumps(SOURCE_INFO), ""
            Path(cmd[cmd.index("-o") + 1]).write_bytes(REMUXED_BYTES)
            raise Crash("power cut before verification")

        tc._run_mkvmerge = crashing
        self._process(expect_crash=True)
        self.movie.unlink()
        self.assertEqual(self._recover(), 0, "nothing may be promoted on a hunch")
        temps, _journals = self._artifacts()
        self.assertEqual(len(temps), 1, "it is kept for manual review, not deleted")
        self.assertFalse(self.movie.exists())

    def test_a_verified_temp_that_changed_afterwards_is_not_promoted(self) -> None:
        with mock.patch.object(tc, "safe_replace", side_effect=Crash("power cut mid-swap")):
            self._process(expect_crash=True)
        temps, _journals = self._artifacts()
        temps[0].write_bytes(REMUXED_BYTES + b"tampered")
        self.movie.unlink()
        self.assertEqual(self._recover(), 0)
        self.assertTrue(temps[0].exists())
        self.assertFalse(self.movie.exists(), "a changed temp is never swapped in")

    def test_a_stale_journal_beside_an_intact_original_is_removed(self) -> None:
        """The crash-after-replace case: the swap happened, the cleanup did not."""
        with mock.patch.object(tc, "safe_delete", side_effect=Crash("power cut after swap")):
            self._process(expect_crash=True)
        self.assertEqual(self.movie.read_bytes(), REMUXED_BYTES, "the swap did complete")
        temps, journals = self._artifacts()
        self.assertEqual(temps, [], "the temp became the movie")
        self.assertEqual(len(journals), 1)
        self.assertEqual(self._recover(), 1)
        self.assertEqual(self._artifacts(), ([], []))
        self.assertEqual(self.movie.read_bytes(), REMUXED_BYTES)

    def test_recovery_is_idempotent(self) -> None:
        self.test_crash_after_verification_before_the_swap()
        self.assertEqual(self._recover(), 0, "a clean library gives recovery nothing to do")
        self._assert_original_intact()

    def test_a_completed_remux_leaves_nothing_behind(self) -> None:
        stats = self._process()
        self.assertEqual([item["name"] for item in stats["cleaned"]], ["Film (2020).mkv"])
        self.assertEqual(self.movie.read_bytes(), REMUXED_BYTES)
        self.assertEqual(self._artifacts(), ([], []))

    def test_ctrl_c_cleans_up_after_itself(self) -> None:
        """Ctrl-C is not a power cut: the handler does get to run."""
        def interrupted(cmd, on_progress=None):
            if "-J" in cmd:
                return 0, json.dumps(SOURCE_INFO), ""
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"partial")
            raise KeyboardInterrupt

        tc._run_mkvmerge = interrupted
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            tc.process_mkv(self.movie, self._stats(), "mkvmerge", dry_run=False,
                           log_file_path=None)
        self._assert_original_intact()
        self.assertEqual(self._artifacts(), ([], []),
                         "an interrupt that reaches the handler leaves nothing behind")

    def test_an_in_process_error_is_reported_and_tidied(self) -> None:
        """The other half of the contract: a bad movie is an error, not a crash."""
        def failing(cmd, on_progress=None):
            if "-J" in cmd:
                return 0, json.dumps(SOURCE_INFO), ""
            raise RuntimeError("mkvmerge exploded")

        tc._run_mkvmerge = failing
        stats = self._process()
        self.assertEqual(len(stats["errors"]), 1)
        self._assert_original_intact()
        self.assertEqual(self._artifacts(), ([], []))

    def test_the_tool_tracks_the_staging_file_it_is_writing(self) -> None:
        # What the signal handler deletes when the run is killed between files.
        def crashing(cmd, on_progress=None):
            if "-J" in cmd:
                return 0, json.dumps(SOURCE_INFO), ""
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"partial")
            raise Crash("power cut")

        tc._run_mkvmerge = crashing
        self._process(expect_crash=True)
        in_flight = tc._active_temp_file
        self.assertIsNotNone(in_flight, "the tool knows which file is in flight")
        self.assertTrue(Path(in_flight).exists())
        tc.safe_delete(Path(in_flight))
        self.assertFalse(Path(in_flight).exists())
        self._assert_original_intact()


class MaliciousJournalTests(unittest.TestCase):
    """Recovery reads a file from the media volume; it must not trust it."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="crash_journal_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.library = self.root / "lib"
        self.library.mkdir()
        self.outside = self.root / "precious.mkv"
        self.outside.write_bytes(b"do not touch me")

    def _plant(self, source_name: str, *, phase: str = "verified") -> tuple[Path, Path]:
        token = "a" * 32
        temp = self.library / f"{tc.TEMP_PREFIX}{token}__Film (2020).mkv"
        temp.write_bytes(REMUXED_BYTES)
        # Age the temp first, then fingerprint it: the journal must be
        # internally consistent so that the only thing recovery can object to
        # is the hostile ``source_name`` under test.
        old = time.time() - (tc.ORPHAN_MIN_AGE_SECONDS + 60)
        os.utime(temp, (old, old))
        journal = tc._transaction_journal_path(self.library, token)
        journal.write_text(json.dumps({
            "schema": tc.TRANSACTION_SCHEMA_VERSION, "token": token, "phase": phase,
            "source_name": source_name, "temp_name": temp.name,
            "source_path": str(self.library / source_name),
            "temp_snapshot": tc._source_snapshot(temp),
            "verification_plan": {},
        }), encoding="utf-8")
        os.utime(journal, (old, old))
        return temp, journal

    def _recover(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return tc.cleanup_orphan_temps(self.library, "mkvmerge", log_file_path=None)

    def test_a_journal_naming_a_parent_directory_is_refused(self) -> None:
        temp, _journal = self._plant("../precious.mkv")
        self.assertEqual(self._recover(), 0)
        self.assertEqual(self.outside.read_bytes(), b"do not touch me")
        self.assertTrue(temp.exists(), "the temp is preserved, not acted on")

    def test_a_journal_naming_an_absolute_path_is_refused(self) -> None:
        temp, _journal = self._plant(str(self.outside))
        self.assertEqual(self._recover(), 0)
        self.assertEqual(self.outside.read_bytes(), b"do not touch me")
        self.assertTrue(temp.exists())

    def test_a_journal_whose_token_does_not_match_its_temp_is_refused(self) -> None:
        temp, journal = self._plant("Film (2020).mkv")
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload["token"] = "b" * 32
        journal.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self._recover(), 0)
        self.assertTrue(temp.exists())

    def test_a_legacy_temp_with_no_journal_is_kept_when_the_original_is_gone(self) -> None:
        legacy = self.library / f"{tc.TEMP_PREFIX}Film (2020).mkv"
        legacy.write_bytes(REMUXED_BYTES)
        old = time.time() - (tc.ORPHAN_MIN_AGE_SECONDS + 60)
        os.utime(legacy, (old, old))
        self.assertEqual(self._recover(), 0)
        self.assertTrue(legacy.exists(), "unexplained data is never deleted")

    def test_a_legacy_temp_beside_an_intact_original_is_removed(self) -> None:
        original = self.library / "Film (2020).mkv"
        original.write_bytes(MOVIE_BYTES)
        legacy = self.library / f"{tc.TEMP_PREFIX}Film (2020).mkv"
        legacy.write_bytes(REMUXED_BYTES)
        old = time.time() - (tc.ORPHAN_MIN_AGE_SECONDS + 60)
        os.utime(legacy, (old, old))
        self.assertEqual(self._recover(), 1)
        self.assertFalse(legacy.exists())
        self.assertEqual(original.read_bytes(), MOVIE_BYTES)


class DurableWriteTests(unittest.TestCase):
    """Interrupt the durable writers themselves: the readers must never see a
    half-written file, and no temp file may be left behind."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="crash_write_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _stray_temps(self) -> list[str]:
        return [p.name for p in self.root.iterdir() if ".tmp" in p.name]

    # -- core.atomic_write_text -------------------------------------------

    def test_a_crash_before_replace_leaves_the_previous_content(self) -> None:
        target = self.root / "report.txt"
        core.atomic_write_text(target, "the good report")
        with mock.patch("organizekit.core.fsio.os.replace", side_effect=Crash("power cut")), \
                self.assertRaises(Crash):
            core.atomic_write_text(target, "the interrupted report")
        self.assertEqual(target.read_text(encoding="utf-8"), "the good report")

    def test_a_crash_during_fsync_leaves_the_previous_content(self) -> None:
        target = self.root / "report.txt"
        core.atomic_write_text(target, "the good report")
        with mock.patch("organizekit.core.fsio.os.fsync", side_effect=Crash("power cut")), \
                self.assertRaises(Crash):
            core.atomic_write_text(target, "the interrupted report")
        self.assertEqual(target.read_text(encoding="utf-8"), "the good report")

    def test_the_first_write_is_all_or_nothing(self) -> None:
        target = self.root / "new.txt"
        with mock.patch("organizekit.core.fsio.os.replace", side_effect=Crash("power cut")), \
                self.assertRaises(Crash):
            core.atomic_write_text(target, "never landed")
        self.assertFalse(target.exists(), "a reader must not find a half-written report")

    # -- the remux journal -------------------------------------------------

    def test_a_crashed_journal_write_keeps_the_previous_journal(self) -> None:
        journal = self.root / ".track_cleaner.abc.json"
        tc.write_transaction(journal, {"schema": tc.TRANSACTION_SCHEMA_VERSION,
                                       "token": "a" * 32, "phase": "remuxing"})
        with mock.patch("mkv_track_cleaner.os.replace", side_effect=Crash("power cut")), \
                self.assertRaises(Crash):
            tc.write_transaction(journal, {"schema": tc.TRANSACTION_SCHEMA_VERSION,
                                           "token": "a" * 32, "phase": "verified"})
        self.assertEqual(tc.read_transaction(journal)["phase"], "remuxing")
        self.assertEqual(self._stray_temps(), [],
                         "the interrupted write must clean up its own scratch file")

    def test_a_journal_interrupted_mid_fsync_is_not_readable_as_verified(self) -> None:
        journal = self.root / ".track_cleaner.def.json"
        with mock.patch("mkv_track_cleaner.os.fsync", side_effect=Crash("power cut")), \
                self.assertRaises(Crash):
            tc.write_transaction(journal, {"schema": tc.TRANSACTION_SCHEMA_VERSION,
                                           "token": "a" * 32, "phase": "verified"})
        self.assertIsNone(tc.read_transaction(journal),
                          "no journal at all is safe; a partial one would not be")
        self.assertEqual(self._stray_temps(), [])

    def test_a_truncated_journal_is_read_as_no_journal(self) -> None:
        journal = self.root / ".track_cleaner.ghi.json"
        journal.write_text('{"schema": 1, "token": "aaa', encoding="utf-8")
        self.assertIsNone(tc.read_transaction(journal))


class SidecarCrashTests(unittest.TestCase):
    """The sidecar half of the promise: a subtitle is never lost to a crash.

    ``sync_subtitles`` rewrites a working subtitle in place when ffsubsync
    finds a trustworthy correction. These tests kill it mid-rewrite and check
    that the subtitle the user already had is still there and still plays.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="crash_srt_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.folder = self.root / "Film (2000)"
        self.folder.mkdir()
        self.movie = self.folder / "Film (2000).mkv"
        self.movie.write_bytes(b"fake video" * 100)
        self.srt = self.folder / "Film (2000).eng.srt"
        self.srt.write_text(SRT, encoding="utf-8")
        self.cfg = ss.Config(library=self.root, log_file=self.root / "s.log",
                             report_file=self.root / "s.txt",
                             sync_ledger=self.root / "ledger.json",
                             use_state=False)
        self.features = ss.FfsubsyncFeatures(True, True, True)

    def _ffsubsync(self, *, crash_after_write: bool = False):
        """A stand-in ffsubsync that measures a large, trustworthy correction."""
        def run(cfg, command):
            command = list(map(str, command))
            Path(command[command.index("-o") + 1]).write_text(SHIFTED_SRT, encoding="utf-8")
            if crash_after_write:
                raise Crash("power cut while ffsubsync was running")
            return 0, "", "\n".join([
                "INFO: score: 40.000",
                "INFO: offset seconds: -4.000",
                "INFO: framerate scale factor: 1.000",
            ])
        return run

    def _sync(self, run, *, expect_crash: bool = True):
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(ss, "run_ffsubsync", run):
            if expect_crash:
                with self.assertRaises(Crash):
                    ss.sync_one(ss.Job(srt=self.srt, video=self.movie), self.cfg,
                                "fake-ffsubsync", self.features)
                return None
            return ss.sync_one(ss.Job(srt=self.srt, video=self.movie), self.cfg,
                               "fake-ffsubsync", self.features)

    def _staging(self) -> list[Path]:
        return sorted(p for p in self.folder.iterdir()
                      if p.name.startswith(ss.STAGING_PREFIX))

    def _assert_subtitle_survived(self) -> None:
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SRT,
                         "the subtitle the user already had must be byte-identical")
        ok, reason = core.validate_srt_sidecar(self.srt)
        self.assertTrue(ok, f"and still usable ({reason})")

    def test_the_uninterrupted_sync_does_replace_the_sidecar(self) -> None:
        # The control: without a crash this run rewrites the file, so the
        # tests below are really about the interruption and not about a
        # code path that never writes.
        result = self._sync(self._ffsubsync(), expect_crash=False)
        self.assertEqual(result.status, ss.STATUS_SYNCED)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SHIFTED_SRT)
        self.assertEqual(self._staging(), [], "staging is consumed by the swap")

    def test_a_crash_while_ffsubsync_runs_leaves_the_subtitle_alone(self) -> None:
        self._sync(self._ffsubsync(crash_after_write=True))
        self._assert_subtitle_survived()

    def test_a_crash_during_the_swap_leaves_the_subtitle_alone(self) -> None:
        with mock.patch("sync_subtitles.os.replace", side_effect=Crash("power cut mid-swap")):
            self._sync(self._ffsubsync())
        self._assert_subtitle_survived()

    def test_the_debris_a_crash_leaves_is_invisible_to_the_other_tools(self) -> None:
        """A crash strands a staging file. It must not look like a subtitle."""
        self._sync(self._ffsubsync(crash_after_write=True))
        stranded = self._staging()
        self.assertEqual(len(stranded), 1, "the crash did strand a staging file")

        # Nothing may mistake it for the movie's subtitle: not this tool's own
        # discovery, and not the auditor that decides what still needs fetching.
        jobs, skipped, _videos = ss.discover_jobs(self.root)
        self.assertEqual([job.srt for job in jobs], [self.srt])
        self.assertEqual(skipped, [])
        self.assertTrue(ss.is_junk_filename(stranded[0].name))

        audit = library_auditor.classify_folder(self.folder)
        self.assertEqual([movie.name for movie in audit.movie_files], [self.movie.name])
        self.assertNotIn(stranded[0].name, audit.detail)

    def test_a_failed_sync_restores_the_entry_time_bytes(self) -> None:
        """Not a crash, but the same promise: a bad sync gives the file back."""
        def hopeless(cfg, command):
            command = list(map(str, command))
            Path(command[command.index("-o") + 1]).write_text("garbage", encoding="utf-8")
            return 1, "", "ERROR: ffsubsync failed to sync the input"

        with mock.patch.object(ss, "_refetch_sidecar",
                               lambda *a, **k: (False, "", "no candidates")):
            result = self._sync(hopeless, expect_crash=False)
        self.assertIn(result.status, {ss.STATUS_FAILED, ss.STATUS_REVIEW})
        self._assert_subtitle_survived()


if __name__ == "__main__":
    unittest.main()
