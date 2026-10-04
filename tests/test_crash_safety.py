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

import mkv_track_cleaner as tc
from organizekit import core

# Both must clear the tool's own truncation guards: an output under 1 KiB, or
# under half the source, is rejected as a botched remux before it is promoted.
MOVIE_BYTES = b"ORIGINAL-MOVIE-BYTES" * 400   # 8000 bytes
REMUXED_BYTES = b"REMUXED-MOVIE-BYTES-" * 300  # 6000 bytes, a plausible saving

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
                "remux_without_srt": [], "keeper_needs_audiofit": [],
                "total_scanned": 0,
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
            "temp_snapshot": tc.source_snapshot(temp),
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


# ---------------------------------------------------------------------------
# audio_standardizer.py: the other tool that replaces a movie file
# ---------------------------------------------------------------------------
#
# The remuxer gets a transaction journal because it deletes the superseded MP4
# in a second named step. ``audio_standardizer.py`` has no journal: its whole
# transaction is one temp file and one ``os.replace``, so the questions are
# narrower but the same — is the original intact at every instant, is the temp
# always swept, can the debris ever be mistaken for a movie, and does the tool
# still refuse to publish over something that is not the file it planned
# against. Until now none of that was executed; it was only asserted in the
# module docstring.

def _audiofit_payload(*streams: dict, duration: str = "7200.000000") -> dict:
    """An ffprobe payload: one hevc video stream plus ``streams`` audio."""
    out = [{"index": 0, "codec_type": "video", "codec_name": "hevc"}]
    for position, stream in enumerate(streams, start=1):
        entry = {"index": position, "codec_type": "audio", "channels": 8,
                 "sample_rate": "48000", "tags": {"language": "eng"},
                 "disposition": {"default": position == 1}}
        entry.update(stream)
        out.append(entry)
    return {"streams": out, "format": {"duration": duration, "size": "8388608"}}


#: A 7.1 lossless master — the shape audiofit exists to convert.
AUDIOFIT_TRUEHD = _audiofit_payload({"codec_name": "truehd", "channels": 8})


@unittest.skipIf(os.name == "nt", "the fakes are launched through a POSIX shebang")
class AudiofitCrashTests(unittest.TestCase):
    """Kill the transcode at each dangerous instant and inspect the library."""

    def setUp(self) -> None:
        import fake_ffprobe as fakeff
        import fakebin

        import audio_standardizer as aus
        self.aus = aus
        self.fakeff = fakeff

        self._td = tempfile.TemporaryDirectory(prefix="crash_audiofit_")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.library = self.tmp / "Movies"
        self.folder = self.library / "Film (2020)"
        self.folder.mkdir(parents=True)
        self.movie = self.folder / "Film (2020).mkv"
        fakeff.write_movie(self.movie, AUDIOFIT_TRUEHD, size=2 * 1024 * 1024)
        self.log = self.tmp / "out" / "audiofit.log"
        self.report = self.tmp / "out" / "audiofit_report.txt"
        self.state_db = self.tmp / "out" / "state.db"
        self.ffmpeg = fakebin.install_python_shim(self.tmp / "bin", "ffmpeg", "fake_ffmpeg")
        self.ffprobe = fakebin.install_python_shim(self.tmp / "bin", "ffprobe", "fake_ffprobe")
        self._saved_log_file = aus.log.file
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        self.aus.log.file = self._saved_log_file

    # -- helpers -----------------------------------------------------------

    def _argv(self, *extra: str) -> list[str]:
        return ["--source", str(self.library), "--log", str(self.log),
                "--report", str(self.report), "--state-db", str(self.state_db),
                "--ffprobe", str(self.ffprobe), "--ffmpeg", str(self.ffmpeg),
                "--workers", "1", *extra]

    def _run(self, *extra: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return self.aus.main(self._argv(*extra))

    def _crash_run(self) -> None:
        """Run to a power cut. The log is a console log; pin it like _run does."""
        with contextlib.redirect_stdout(io.StringIO()):
            self.aus.main(self._argv())

    def _temps(self) -> list[Path]:
        """Every audiofit staging file anywhere under the library."""
        return sorted(self.library.rglob("*.audiofit-*.tmp.mkv"))

    def _is_transcode(self, cmd: object) -> bool:
        """True for the ffmpeg invocation that writes the staging file."""
        return any(".audiofit-" in str(part) for part in cmd or ())

    def _after_encode(self, act) -> object:
        """Wrap ``subprocess.run`` so ``act`` fires once the encode has landed.

        That is the window between "the new file exists and verified" and
        "``os.replace`` publishes it" — the instant the remuxer guards with its
        source snapshot, and the one an ingest hook landing a better release
        would choose.
        """
        real_run = self.aus.subprocess.run
        fired = []

        def wrapped(cmd, *args, **kwargs):
            result = real_run(cmd, *args, **kwargs)
            if self._is_transcode(cmd) and not fired:
                fired.append(cmd)
                act()
            return result

        return mock.patch.object(self.aus.subprocess, "run", wrapped)

    def _assert_untouched(self, before: bytes) -> None:
        self.assertTrue(self.movie.is_file(), "the movie must never disappear")
        self.assertEqual(self.movie.read_bytes(), before,
                         "a crashed transcode must leave the original byte-identical")

    # -- the crash matrix --------------------------------------------------

    def test_crash_during_the_encode_leaves_the_original_untouched(self) -> None:
        before = self.movie.read_bytes()
        real_run = self.aus.subprocess.run

        def crashing(cmd, *args, **kwargs):
            if self._is_transcode(cmd):
                # The staging file exists and is half-written when power goes.
                Path(cmd[-1]).write_bytes(b"PARTIAL")
                raise Crash("power cut mid-encode")
            return real_run(cmd, *args, **kwargs)

        with mock.patch.object(self.aus.subprocess, "run", crashing), \
                self.assertRaises(Crash):
            self._crash_run()
        self._assert_untouched(before)
        self.assertEqual(self._temps(), [], "a crashed encode must sweep its own staging file")

    def test_crash_between_verification_and_the_publish_leaves_the_original(self) -> None:
        before = self.movie.read_bytes()
        real_replace = os.replace

        def crashing_replace(src, dst, **kwargs):
            # Only the publish of the verified staging file over the movie.
            if ".audiofit-" in str(src):
                raise Crash("power cut between verification and the swap")
            return real_replace(src, dst, **kwargs)

        with mock.patch.object(self.aus.os, "replace", crashing_replace), \
                self.assertRaises(Crash):
            self._crash_run()
        self._assert_untouched(before)
        # ``finally`` runs for a BaseException too, so this crash sweeps its own
        # staging file. Only a SIGKILL or a real power cut can leave one behind,
        # and the next run's startup sweep is what covers that (below).
        self.assertEqual(self._temps(), [], "the publish crash still sweeps its staging file")
        self.assertEqual(self._run(), 0, "and the library is simply tried again")

    def test_the_staging_file_can_never_be_mistaken_for_a_movie(self) -> None:
        """Half-written debris must not be discoverable as a movie by any tool.

        The staging name is dot-prefixed and carries the tool's marker, which
        is the whole reason a crash mid-encode cannot leave something that a
        media server, the auditor or the remuxer would happily play or clean.
        """
        stray = self.folder / ".Film (2020).audiofit-424242.tmp.mkv"
        stray.write_bytes(b"PARTIAL")
        cfg = self.aus.Config(source_dir=self.library, min_file_size_mb=0)
        found = self.aus.discover_videos(self.library, cfg)
        self.assertNotIn(stray, found, "audiofit must not probe its own debris")
        self.assertEqual(found, [self.movie], "only the real movie is discovered")
        self.assertTrue(self.aus.is_junk_name(stray.name),
                        "the marker name is junk by the same rule that hides it")
        self.assertEqual(self._run(), 0)
        self.assertFalse(stray.exists(), "the next run sweeps the stale staging file")
        self.assertTrue(self.movie.is_file())

    def test_a_movie_replaced_while_the_encode_ran_is_not_clobbered(self) -> None:
        """The ingest hook lands a NEW release between the probe and the publish.

        ``movie_standardizer.py`` runs from the torrent client on completion, so
        a better release can be hardlinked onto this exact path while audiofit
        is still encoding the old one. ``os.replace`` would then destroy the
        fresh ingest and publish a track built from the bytes it replaced — and
        report success, because verification had already passed against the old
        file. The remuxer refuses this swap (``source_snapshot_matches`` before
        its own ``safe_replace``); this tool must too.
        """
        def ingest_a_better_release() -> None:
            self.fakeff.write_movie(self.movie, AUDIOFIT_TRUEHD, size=9 * 1024 * 1024)
            os.utime(self.movie, (1_700_000_000, 1_700_000_000))

        with self._after_encode(ingest_a_better_release):
            code = self._run()
        ingested = self.movie.read_bytes()
        self.assertEqual(len(ingested), 9 * 1024 * 1024,
                         "the freshly ingested movie must survive audiofit's publish")
        self.assertNotEqual(code, 0, "a refused publish is an error, not a success")
        self.assertIn("source changed while transcoding", self.report.read_text(encoding="utf-8"))
        self.assertEqual(self._temps(), [], "the refused staging file is still swept")

    def test_a_movie_that_becomes_seeded_mid_run_is_deferred_at_the_publish(self) -> None:
        """"Seeding torrents are never touched" includes the instant of the swap.

        A movie that was single-linked when it was planned can be hardlinked to
        a seed by the time its encode finishes. Deferring only at plan time left
        the publish to fork the library copy from the copy still being served.
        """
        seeds = self.tmp / "torrents"
        seeds.mkdir()

        def start_seeding() -> None:
            os.link(self.movie, seeds / self.movie.name)

        with self._after_encode(start_seeding):
            code = self._run()
        self.assertEqual(code, 0, "deferring is not an error: nothing was lost")
        self.assertEqual(self.movie.stat().st_nlink, 2, "the library copy keeps its seed link")
        self.assertEqual(self.fakeff.read_payload(self.movie)["streams"][-1]["codec_name"],
                         "truehd", "the movie was left exactly as it was planned")
        self.assertIn(self.aus.STATUS_DEFERRED, self.report.read_text(encoding="utf-8"))
        self.assertIn("hardlinks at publish time", self.report.read_text(encoding="utf-8"))
        self.assertEqual(self._temps(), [])

    def test_the_run_lock_survives_a_crash(self) -> None:
        """A killed run must not lock the library out of the next one."""
        real_run = self.aus.subprocess.run

        def crashing(cmd, *args, **kwargs):
            if self._is_transcode(cmd):
                raise Crash("power cut")
            return real_run(cmd, *args, **kwargs)

        with mock.patch.object(self.aus.subprocess, "run", crashing), \
                self.assertRaises(Crash):
            self._crash_run()
        lock_path = self.aus.run_lock_path(self.library)
        with lock_path.open("a+", encoding="utf-8") as handle:
            self.assertTrue(core.try_file_lock(handle, strict_non_contention=False),
                            "the crashed run must have released its lock")
        self.assertEqual(self._run(), 0, "and the next run starts normally")


if __name__ == "__main__":
    unittest.main()
