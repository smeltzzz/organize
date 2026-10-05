"""The cleaner's recovery half: verification, stale locks, and orphaned staging.

``tests/test_crash_safety.py`` pulls the plug at every dangerous instant and
asks whether a movie survived. These tests ask the other question, about the
machinery that runs *afterwards* and *beforehand*:

* **Verification.** A remux is promoted over the original only if its output
  still matches the plan it was built from - one audio track, the same audio
  fingerprint, the same subtitle set, the same attachments, chapters and video
  frame counts, and a duration that did not grow or collapse. Each refusal
  below is a movie that was *not* replaced, and the reason it was not is the
  one the report prints.
* **The single-instance lock.** A machine that rebooted mid-sweep leaves a lock
  file behind. Reclaiming it requires knowing the holder is dead; guessing wrong
  means two processes remuxing one library. The Windows ``OpenProcess`` half of
  that decision had never been measured.
* **Orphan recovery.** A staging file with a journal is a transaction somebody
  did not finish. The rule is that only a journal-proven, re-verified remux may
  be promoted - and everything else is *preserved for a human*, never deleted
  on a hunch. Most of these branches are the preserved ones.
"""

from __future__ import annotations

import ctypes
import errno
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fake_mkvmerge as fake
import platforms

import mkv_track_cleaner as tc

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

MOVIE_BYTES = b"ORIGINAL-MOVIE-BYTES" * 400   # 8000 bytes
REMUXED_BYTES = b"REMUXED-MOVIE-BYTES-" * 300  # 6000 bytes

SOURCE_INFO = fake.make_spec([
    fake.video_track(),
    fake.audio_track(default=True),
    fake.audio_track(name="Director Commentary", codec="AC-3", codec_id="A_AC3",
                     channels=2, commentary=True),
])
OUTPUT_INFO = fake.make_spec([fake.video_track(), fake.audio_track(default=True)])


class QuietTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cleaner_recovery_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # The cleaner appends through one module-level file object closed at
        # exit; on Windows an open handle makes the tree undeletable and the test
        # dies in teardown instead of reporting what it measured.
        self.addCleanup(tc.close_log_fp)
        capture = redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def _movie_folder(self, name: str = "Film (2020)") -> Path:
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        movie = folder / f"{name}.mkv"
        movie.write_bytes(MOVIE_BYTES)
        return movie


def plan_for(source: dict, output: dict) -> dict:
    """The verification plan the cleaner builds before it remuxes."""
    return {
        "source_size": len(MOVIE_BYTES),
        "audio": tc.track_fingerprint(
            next(t for t in output["tracks"] if t["type"] == "audio")),
        "subtitles": [],
        "preserved_tracks": {"video": tc._fingerprint_list(
            [t for t in output["tracks"] if t["type"] == "video"])},
        "attachment_count": len(source.get("attachments") or []),
        "chapter_entries": tc._chapter_entry_count(source),
        "video_frame_counts": tc._video_frame_counts(output["tracks"]),
        "source_duration_ns": (source.get("container") or {}).get("properties", {}).get("duration"),
    }


class RemuxVerificationTests(QuietTestCase):
    """``verify_remux_output``: the gate between a staging file and a movie."""

    def setUp(self) -> None:
        super().setUp()
        self.temp = self.root / f"{tc.TEMP_PREFIX}{'0' * 32}__Film (2020).mkv"
        self.temp.write_bytes(REMUXED_BYTES)
        self.plan = plan_for(SOURCE_INFO, OUTPUT_INFO)

    def _verify(self, info: dict | None = None, plan: dict | None = None,
                rc: int = 0, stdout: str | None = None, stderr: str = "") -> tuple:
        payload = json.dumps(OUTPUT_INFO if info is None else info)
        with mock.patch.object(tc, "_run_mkvmerge",
                               lambda cmd, on_progress=None: (rc, payload if stdout is None
                                                              else stdout, stderr)):
            return tc.verify_remux_output(self.temp, "mkvmerge", plan or self.plan)

    def test_a_remux_that_matches_its_plan_is_accepted(self) -> None:
        ok, reason, info = self._verify()
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")
        self.assertEqual(info, OUTPUT_INFO)

    def test_a_remux_that_cannot_be_inspected_is_refused(self) -> None:
        """Fail closed: a verification that could not run is one that did not pass.

        This is the branch that decides whether a movie gets replaced, so
        "the multiplexer exploded" has to read as "refused", not "probably
        fine".
        """
        with mock.patch.object(tc, "_run_mkvmerge",
                               mock.Mock(side_effect=RuntimeError("mkvmerge exploded"))):
            ok, reason, info = tc.verify_remux_output(self.temp, "mkvmerge", self.plan)
        self.assertFalse(ok)
        self.assertIn("could not re-inspect remuxed file", reason)
        self.assertIsNone(info)

    def test_an_inspection_that_answerss_with_a_failure_code_is_refused_with_its_stderr(self) -> None:
        ok, reason, _ = self._verify(rc=2, stdout="", stderr="Error: not a Matroska file")
        self.assertFalse(ok)
        self.assertIn("remuxed file inspection failed (code 2)", reason)
        self.assertIn("not a Matroska file", reason)

    def test_metadata_that_is_not_json_is_refused(self) -> None:
        ok, reason, _ = self._verify(stdout="this is not json at all")
        self.assertFalse(ok)
        self.assertIn("could not parse remuxed file metadata", reason)

    def test_a_staging_file_that_vanished_is_refused(self) -> None:
        self.temp.unlink()
        ok, reason, _ = self._verify()
        self.assertFalse(ok)
        self.assertIn("could not stat remuxed file", reason)

    def test_an_output_that_is_not_a_supported_container_is_refused(self) -> None:
        broken = dict(OUTPUT_INFO, container={"recognized": False, "supported": False})
        ok, reason, _ = self._verify(info=broken)
        self.assertFalse(ok)
        self.assertIn("not a recognized/supported media container", reason)

    def test_a_truncated_output_is_refused(self) -> None:
        self.temp.write_bytes(b"half a movie")
        ok, reason, _ = self._verify()
        self.assertFalse(ok)
        self.assertIn("remuxed file is tiny", reason)

    def test_an_output_that_shrank_too_far_is_refused(self) -> None:
        """Removing two audio tracks saves megabytes, not most of the file."""
        self.temp.write_bytes(b"x" * 2000)  # over the 1 KiB floor, under half the source
        ok, reason, _ = self._verify()
        self.assertFalse(ok)
        self.assertIn("shrank too much", reason)
        self.assertIn("refusing to replace original", reason)

    def test_an_output_with_the_wrong_number_of_audio_tracks_is_refused(self) -> None:
        for tracks, expected in ((OUTPUT_INFO["tracks"][:1], "found 0"),
                                 (SOURCE_INFO["tracks"], "found 2")):
            with self.subTest(count=expected):
                info = dict(OUTPUT_INFO, tracks=tracks)
                ok, reason, _ = self._verify(info=info)
                self.assertFalse(ok)
                self.assertIn(f"expected exactly 1 audio track in output, {expected}", reason)

    def test_an_output_whose_kept_audio_is_not_the_selected_audio_is_refused(self) -> None:
        """The whole point of the remux: keep *this* track, not merely *a* track."""
        wrong = dict(OUTPUT_INFO, tracks=[
            fake.video_track(), fake.audio_track(name="French", language="fra", codec="DTS",
                                                 codec_id="A_DTS", channels=6),
        ])
        wrong["tracks"] = [dict(track, id=index) for index, track in enumerate(wrong["tracks"])]
        ok, reason, _ = self._verify(info=wrong)
        self.assertFalse(ok)
        self.assertIn("retained audio fingerprint differs", reason)

    def test_an_output_that_gained_a_subtitle_track_is_refused(self) -> None:
        gained = dict(OUTPUT_INFO, tracks=[*OUTPUT_INFO["tracks"], fake.subtitle_track()])
        gained["tracks"] = [dict(track, id=index) for index, track in enumerate(gained["tracks"])]
        ok, reason, _ = self._verify(info=gained)
        self.assertFalse(ok)
        self.assertIn("retained subtitle fingerprints differ", reason)

    def test_an_output_whose_video_track_is_not_the_source_video_is_refused(self) -> None:
        """The video track is never supposed to be touched by this tool.

        The fingerprint deliberately excludes track ids and statistics tags -
        mkvmerge renumbers and regenerates those - so what has to differ for
        this refusal to fire is the codec itself.
        """
        different = dict(fake.video_track()["properties"], codec_id="V_MPEGH/ISO/HEVC")
        changed = dict(OUTPUT_INFO, tracks=[
            dict(fake.video_track(), codec="HEVC/H.265", properties=different),
            OUTPUT_INFO["tracks"][1],
        ])
        changed["tracks"] = [dict(track, id=index) for index, track in enumerate(changed["tracks"])]
        ok, reason, _ = self._verify(info=changed)
        self.assertFalse(ok)
        self.assertIn("video track fingerprints changed during remux", reason)

    def test_an_output_that_lost_an_attachment_is_refused(self) -> None:
        source = dict(SOURCE_INFO, attachments=[{"file_name": "cover.jpg", "size": 100}])
        plan = plan_for(source, OUTPUT_INFO)
        plan["attachment_count"] = 1
        ok, reason, _ = self._verify(plan=plan)
        self.assertFalse(ok)
        self.assertIn("attachment count changed during remux", reason)

    def test_an_output_that_lost_its_chapters_is_refused(self) -> None:
        plan = dict(self.plan, chapter_entries=4)
        ok, reason, _ = self._verify(plan=plan)
        self.assertFalse(ok)
        self.assertIn("chapter count changed during remux", reason)

    def test_a_source_frame_count_missing_from_the_output_is_refused(self) -> None:
        plan = dict(self.plan, video_frame_counts=[144_000, 12_000])
        ok, reason, _ = self._verify(plan=plan)
        self.assertFalse(ok)
        self.assertIn("source video frame count is absent from the remuxed output", reason)

    def test_a_frame_count_the_multiplexer_materialised_is_not_a_refusal(self) -> None:
        """MKVToolNix can add a NumberOfFrames tag the source never carried.

        Only counts *present in the source* are a verification signal; an
        output-only value is informational, and refusing it would leave every
        such movie permanently uncleanable.
        """
        plan = dict(self.plan, video_frame_counts=[None, 144_000])
        ok, reason, _ = self._verify(plan=plan)
        self.assertTrue(ok, reason)

    def test_a_duration_that_grew_is_refused(self) -> None:
        grown = json.loads(json.dumps(OUTPUT_INFO))
        grown["container"]["properties"]["duration"] = SOURCE_INFO["container"]["properties"]["duration"] * 2
        ok, reason, _ = self._verify(info=grown)
        self.assertFalse(ok)
        self.assertIn("duration grew during remux", reason)

    def test_a_duration_that_collapsed_without_frame_counts_is_refused(self) -> None:
        short = json.loads(json.dumps(OUTPUT_INFO))
        short["container"]["properties"]["duration"] = 1_000_000_000
        plan = dict(self.plan, video_frame_counts=[])
        ok, reason, _ = self._verify(info=short, plan=plan)
        self.assertFalse(ok)
        self.assertIn("duration shrank too much during remux", reason)

    def test_a_shorter_duration_is_accepted_when_the_frame_counts_confirm_the_video(self) -> None:
        """Removing padded commentary can legitimately shorten the container."""
        short = json.loads(json.dumps(OUTPUT_INFO))
        short["container"]["properties"]["duration"] = 5_900_000_000_000
        ok, reason, _ = self._verify(info=short)
        self.assertTrue(ok, reason)

    def test_a_duration_that_is_not_a_number_is_ignored_rather_than_fatal(self) -> None:
        """Probe payloads carry strings and nulls; a report line must not die on one."""
        for value in ("not a number", None, {}):
            with self.subTest(value=value):
                odd = json.loads(json.dumps(OUTPUT_INFO))
                odd["container"]["properties"]["duration"] = value
                plan = dict(self.plan, source_duration_ns=6_000_000_000_000)
                ok, _reason, _ = self._verify(info=odd, plan=plan)
                self.assertTrue(ok)


class PidAliveTests(QuietTestCase):
    """Is the process named in a lock file still running?"""

    def test_a_pid_that_is_not_a_number_is_treated_as_alive(self) -> None:
        for value in (None, "not-a-pid", float("nan")):
            with self.subTest(value=value):
                self.assertTrue(tc._pid_alive(value))

    def test_a_non_positive_pid_is_not_a_process(self) -> None:
        self.assertFalse(tc._pid_alive(0))
        self.assertFalse(tc._pid_alive(-1))

    def test_a_pid_nobody_can_signal_is_dead(self) -> None:
        with mock.patch.object(tc.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(tc._pid_alive(4_000_000))

    def test_a_pid_that_refuses_to_be_signalled_is_alive(self) -> None:
        """EPERM means the process exists and belongs to somebody else.

        ``os.kill(pid, 0)`` is the POSIX probe; on Windows the same function
        goes through OpenProcess instead (covered below), so the signalling half
        has to be asked on a POSIX host.
        """
        with platforms.posix(), mock.patch.object(tc.os, "kill", side_effect=PermissionError):
            self.assertTrue(tc._pid_alive(1))

    def test_a_kill_that_fails_for_any_other_reason_is_treated_as_alive(self) -> None:
        for error in (OSError("nope"), OverflowError("pid too large"), ValueError("bad pid")):
            with self.subTest(error=error), platforms.posix(), \
                    mock.patch.object(tc.os, "kill", side_effect=error):
                self.assertTrue(tc._pid_alive(12345))

    def test_this_process_is_alive(self) -> None:
        self.assertTrue(tc._pid_alive(os.getpid()))

    def test_windows_asks_the_process_table_rather_than_signalling(self) -> None:
        """``os.kill(pid, 0)`` on Windows is a terminate, not a probe.

        So the check goes through ``OpenProcess`` with QUERY_LIMITED_INFORMATION
        and closes the handle it got back - a leaked handle per checked lock
        would be a leaked kernel object for the life of the sweep.
        """
        calls: list[tuple] = []
        kernel32 = SimpleNamespace(
            OpenProcess=lambda access, inherit, pid: calls.append(("open", access, pid)) or 777,
            CloseHandle=lambda handle: calls.append(("close", handle)),
        )
        with platforms.windows(), mock.patch.object(ctypes, "windll",
                                                    SimpleNamespace(kernel32=kernel32),
                                                    create=True):
            self.assertTrue(tc._pid_alive(4321))
        self.assertEqual(calls, [("open", 0x1000, 4321), ("close", 777)])

    def test_a_windows_pid_with_no_handle_is_dead(self) -> None:
        kernel32 = SimpleNamespace(OpenProcess=lambda *args: 0,
                                   CloseHandle=lambda handle: None)
        with platforms.windows(), mock.patch.object(ctypes, "windll",
                                                    SimpleNamespace(kernel32=kernel32),
                                                    create=True):
            self.assertFalse(tc._pid_alive(4321))

    def test_a_windows_probe_that_raises_assumes_the_other_run_is_alive(self) -> None:
        """Fail safe in the expensive direction: a skipped run, never two writers."""
        def exploding(*args: object, **kwargs: object) -> object:
            raise ctypes.ArgumentError(None, "bad handle")

        kernel32 = SimpleNamespace(OpenProcess=exploding, CloseHandle=lambda handle: None)
        with platforms.windows(), mock.patch.object(ctypes, "windll",
                                                    SimpleNamespace(kernel32=kernel32),
                                                    create=True):
            self.assertTrue(tc._pid_alive(4321))

    def test_a_windows_host_without_a_kernel32_falls_through_to_the_posix_probe(self) -> None:
        with platforms.windows(), mock.patch.object(ctypes, "windll", None, create=True), \
                mock.patch.object(tc.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(tc._pid_alive(4321))


class SingleInstanceLockTests(QuietTestCase):
    """``acquire_lock``: two sweeps in one library is how a movie gets destroyed."""

    def setUp(self) -> None:
        super().setUp()
        self.lock = self.root / tc.LOCK_FILENAME
        self._hostname = tc.socket.gethostname()

    def _write_lock(self, body: str) -> None:
        self.lock.write_text(body, encoding="utf-8")

    def test_a_free_lock_is_taken_and_names_its_holder(self) -> None:
        self.assertTrue(tc.acquire_lock(self.lock, log_file_path=None))
        lines = self.lock.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], tc._this_hostname())
        self.assertEqual(lines[1], str(os.getpid()))

    def test_a_lock_held_by_this_live_process_is_refused(self) -> None:
        self._write_lock(f"{self._hostname}\n{os.getpid()}\n{time.time()}\n")
        self.assertFalse(tc.acquire_lock(self.lock, log_file_path=None))
        self.assertIn(str(os.getpid()), self.lock.read_text(encoding="utf-8"),
                      "the live holder's lock file is left alone")

    def test_a_lock_left_by_a_dead_process_on_this_host_is_reclaimed(self) -> None:
        """The reboot case: the sweep died and its lock file outlived it.

        Without this the library could never be cleaned again until somebody
        found and deleted a dotfile by hand - and the tool would not say why.
        """
        self._write_lock(f"{self._hostname}\n4000000\n{time.time()}\n")
        with mock.patch.object(tc, "_pid_alive", lambda pid: False):
            self.assertTrue(tc.acquire_lock(self.lock, log_file_path=None))
        self.assertEqual(self.lock.read_text(encoding="utf-8").splitlines()[1], str(os.getpid()),
                         "the reclaimed lock names this run")

    def test_a_legacy_lock_file_with_only_a_pid_is_read_as_this_host(self) -> None:
        """Older builds wrote just the pid; those files are still out there."""
        self._write_lock("4000000\n")
        with mock.patch.object(tc, "_pid_alive", lambda pid: False):
            self.assertTrue(tc.acquire_lock(self.lock, log_file_path=None))

    def test_a_lock_held_on_another_machine_is_never_reclaimed(self) -> None:
        """A pid means nothing across hosts: 4321 is somebody else's process.

        This is the NAS case the toolkit is built for - two machines, one share.
        Reclaiming that lock would put two remuxes on one movie.
        """
        self._write_lock("some-other-nas\n4321\n1234567890\n")
        log = self.root / "lock.log"
        with mock.patch.object(tc, "_pid_alive", mock.Mock(return_value=False)):
            self.assertFalse(tc.acquire_lock(self.lock, log_file_path=str(log)))
        self.assertIn("some-other-nas", self.lock.read_text(encoding="utf-8"),
                      "the other host's lock is untouched")
        self.assertIn("on host 'some-other-nas'", log.read_text(encoding="utf-8"))

    def test_a_lock_file_nobody_can_read_is_treated_as_held(self) -> None:
        self._write_lock("4000000\n")
        with mock.patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            self.assertFalse(tc.acquire_lock(self.lock, log_file_path=None))

    def test_a_lock_file_that_is_not_text_at_all_is_treated_as_held(self) -> None:
        """An empty or garbage lock file proves nothing, so it is not reclaimed."""
        self._write_lock("\n\n\n")
        self.assertFalse(tc.acquire_lock(self.lock, log_file_path=None))

    def test_a_stale_lock_that_cannot_be_removed_is_refused(self) -> None:
        self._write_lock(f"{self._hostname}\n4000000\n1\n")
        with mock.patch.object(tc, "_pid_alive", lambda pid: False), \
                mock.patch.object(Path, "unlink", side_effect=OSError("read-only share")):
            self.assertFalse(tc.acquire_lock(self.lock, log_file_path=None))

    def test_a_lock_directory_nobody_can_write_is_an_error_not_a_crash(self) -> None:
        with mock.patch.object(tc.os, "open", side_effect=OSError(errno.EACCES, "denied")):
            self.assertFalse(tc.acquire_lock(self.lock, log_file_path=None))

    def test_anything_unexpected_while_taking_the_lock_means_the_run_does_not_start(self) -> None:
        """Fail closed: an exception here must not become two writers."""
        def exploding(*args: object, **kwargs: object) -> object:
            raise RuntimeError("something nobody anticipated")

        with mock.patch.object(tc.os, "open", exploding):
            self.assertFalse(tc.acquire_lock(self.lock, log_file_path=None))

    def test_releasing_a_lock_that_is_already_gone_is_not_an_error(self) -> None:
        tc.release_lock(self.lock)  # never taken
        self.assertTrue(tc.acquire_lock(self.lock, log_file_path=None))
        tc.release_lock(self.lock)
        self.assertFalse(self.lock.exists())

    def test_a_lock_that_cannot_be_released_is_left_for_the_next_run_to_reclaim(self) -> None:
        self.assertTrue(tc.acquire_lock(self.lock, log_file_path=None))
        with mock.patch.object(Path, "unlink", side_effect=OSError("read-only share")):
            tc.release_lock(self.lock)  # must not raise
        self.assertTrue(self.lock.is_file())


class OrphanRecoveryTests(QuietTestCase):
    """``cleanup_orphan_temps``: what a sweep does with somebody else's debris."""

    def setUp(self) -> None:
        super().setUp()
        self._interrupt = tc._interrupt_requested
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._interrupt_requested = self._interrupt

    def _journal(self, token: str, snapshot_of: Path | None = None,
                 **overrides: object) -> Path:
        """A journal for one interrupted transaction.

        ``snapshot_of`` is the staging file the journal vouches for: without a
        matching snapshot the sweep stops one branch earlier ("changed verified
        temp") and never reaches the case under test.
        """
        payload = {
            "schema": tc.TRANSACTION_SCHEMA_VERSION,
            "token": token,
            "phase": "verified",
            "source_path": str(self.root / "Film (2020).mkv"),
            "source_name": "Film (2020).mkv",
            "temp_name": f"{tc.TEMP_PREFIX}{token}__Film (2020).mkv",
            "source_snapshot": {},
            "temp_snapshot": tc.source_snapshot(snapshot_of) if snapshot_of else {},
            "verification_plan": plan_for(SOURCE_INFO, OUTPUT_INFO),
        }
        payload.update(overrides)
        path = tc._transaction_journal_path(self.root, token)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _temp(self, token: str, *, body: bytes = REMUXED_BYTES, age: bool = True) -> Path:
        temp = self.root / f"{tc.TEMP_PREFIX}{token}__Film (2020).mkv"
        temp.write_bytes(body)
        if age:
            old = time.time() - tc.ORPHAN_MIN_AGE_SECONDS - 60
            os.utime(temp, (old, old))
        return temp

    def _sweep(self) -> int:
        return tc.cleanup_orphan_temps(self.root, "mkvmerge", log_file_path=None)

    def _artifacts(self) -> tuple[list[str], list[str]]:
        temps = sorted(p.name for p in self.root.iterdir() if p.name.startswith(tc.TEMP_PREFIX))
        journals = sorted(p.name for p in self.root.iterdir()
                          if p.name.startswith(tc.TRANSACTION_MARKER))
        return temps, journals

    def test_a_conversion_temp_whose_destination_is_named_is_left_for_a_human(self) -> None:
        """An MKV temp must never be renamed onto an absent ``.mp4`` name.

        Doing that would put Matroska bytes behind an MP4 extension, which a
        media server will then fail to play - a worse outcome than an orphan
        file somebody has to delete.
        """
        token = "a" * 32
        temp = self._temp(token)
        self._journal(token, output_name="Film (2020).mkv", phase="verified")
        self.assertEqual(self._sweep(), 0, "nothing was recovered")
        self.assertTrue(temp.is_file(), "and nothing was deleted")

    def test_a_verified_temp_with_an_incomplete_journal_is_left_for_a_human(self) -> None:
        """No plan in the journal means nothing can be re-verified against."""
        token = "b" * 32
        temp = self._temp(token)
        journal = self._journal(token, snapshot_of=temp)
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload.pop("verification_plan")
        journal.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self._sweep(), 0)
        self.assertTrue(temp.is_file())

    def test_a_verified_temp_whose_sidecar_changed_is_left_for_a_human(self) -> None:
        """The remux was verified *with* that sidecar; a different one invalidates it."""
        movie = self._movie_folder()
        sidecar = movie.with_name("Film (2020).eng.srt")
        sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello.\n\n", encoding="utf-8")
        record = tc.validate_exact_external_english_srt(movie)
        self.assertTrue(record["valid"])
        token = "c" * 32
        temp = self._temp(token)
        self._journal(token, snapshot_of=temp, external_srt=record)
        movie.unlink()  # the swap half-happened
        sidecar.write_text("1\n00:00:09,000 --> 00:00:10,000\nDifferent.\n\n", encoding="utf-8")
        self.assertEqual(self._sweep(), 0)
        self.assertTrue(temp.is_file(), "the verified remux is not promoted against a new sidecar")

    def test_a_verified_temp_that_fails_re_verification_is_left_for_a_human(self) -> None:
        token = "d" * 32
        temp = self._temp(token)
        self._journal(token, snapshot_of=temp)
        (self.root / "Film (2020).mkv").unlink(missing_ok=True)
        with mock.patch.object(tc, "verify_remux_output",
                               lambda path, binary, plan: (False, "the audio changed", None)):
            self.assertEqual(self._sweep(), 0)
        self.assertTrue(temp.is_file())

    def test_a_recovery_that_raises_leaves_the_file_and_the_sweep_continues(self) -> None:
        """One unrecoverable orphan must not stop the rest of the library being swept."""
        first, second = "e" * 32, "f" * 32
        temp = self._temp(first)
        self._journal(first, snapshot_of=temp)
        self._temp(second, body=b"legacy junk")
        with mock.patch.object(tc, "verify_remux_output",
                               mock.Mock(side_effect=RuntimeError("multiplexer gone"))):
            self.assertEqual(self._sweep(), 0)
        self.assertEqual(len(self._artifacts()[0]), 2, "both orphans are still there for a human")

    def test_an_interrupt_stops_the_sweep_where_it_stands(self) -> None:
        token = "0" * 32
        temp = self._temp(token)
        self._journal(token, snapshot_of=temp)
        (self.root / "Film (2020).mkv").unlink(missing_ok=True)
        tc._interrupt_requested = True
        self.assertEqual(self._sweep(), 0)
        self.assertTrue((self.root / f"{tc.TEMP_PREFIX}{token}__Film (2020).mkv").is_file(),
                        "an interrupted sweep promotes nothing")

    def test_a_temp_that_cannot_be_stat_ed_is_skipped_not_fatal(self) -> None:
        token = "1" * 32
        temp = self._temp(token)
        self._journal(token, snapshot_of=temp)
        real_stat = Path.stat

        def flaky(path: Path, **kwargs: object) -> object:
            if path == temp:
                raise OSError("share went away")
            return real_stat(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(Path, "stat", flaky):
            self.assertEqual(self._sweep(), 0)
        self.assertTrue(temp.is_file())

    def test_an_interrupt_during_the_journal_pass_stops_it_too(self) -> None:
        """The second walk has its own break: a Ctrl-C between files is honoured."""
        movie = self._movie_folder()
        token = "2" * 32
        self._journal(token, temp_name=f"{tc.TEMP_PREFIX}{token}__Film (2020).mkv")
        tc._interrupt_requested = True
        self.assertEqual(self._sweep(), 0)
        self.assertEqual(len(self._artifacts()[1]), 1, "the stale journal is left for the next run")
        self.assertTrue(movie.is_file())

    def test_an_interrupted_conversion_is_finished_when_the_journal_proves_the_output(self) -> None:
        """The crash landed between publishing the MKV and removing the MP4.

        The journal's snapshot identifies the verified output, so the stale MP4
        and the journal can both go - and the library ends up with one MKV.
        """
        token = "3" * 32
        mp4 = self.root / "Film (2020).mp4"
        mp4.write_bytes(MOVIE_BYTES)
        output = self.root / "Film (2020).mkv"
        output.write_bytes(REMUXED_BYTES)
        snapshot = tc.source_snapshot(output)
        self._journal(token, output_name="Film (2020).mkv",
                      temp_name=f"{tc.TEMP_PREFIX}{token}__Film (2020).mp4",
                      source_name="Film (2020).mp4", temp_snapshot=snapshot)
        self.assertEqual(self._sweep(), 2, "the MP4 and the journal were both handled")
        self.assertFalse(mp4.exists())
        self.assertEqual(output.read_bytes(), REMUXED_BYTES)
        self.assertEqual(self._artifacts()[1], [])

    def test_a_conversion_whose_output_does_not_match_the_journal_is_left_alone(self) -> None:
        """An MKV beside an MP4 that the journal cannot vouch for is a pair a human named."""
        token = "4" * 32
        mp4 = self.root / "Film (2020).mp4"
        mp4.write_bytes(MOVIE_BYTES)
        output = self.root / "Film (2020).mkv"
        output.write_bytes(b"somebody else's cut")
        self._journal(token, output_name="Film (2020).mkv",
                      temp_name=f"{tc.TEMP_PREFIX}{token}__Film (2020).mp4",
                      source_name="Film (2020).mp4",
                      temp_snapshot=tc.source_snapshot(mp4))
        self.assertEqual(self._sweep(), 0)
        self.assertTrue(mp4.is_file(), "neither half of the pair was touched")
        self.assertTrue(output.is_file())

    def test_a_stale_journal_beside_the_finished_mkv_is_removed(self) -> None:
        token = "5" * 32
        output = self.root / "Film (2020).mkv"
        output.write_bytes(REMUXED_BYTES)
        self._journal(token, output_name="Film (2020).mkv",
                      temp_name=f"{tc.TEMP_PREFIX}{token}__Film (2020).mp4",
                      source_name="Film (2020).mp4",
                      temp_snapshot=tc.source_snapshot(output))
        self.assertEqual(self._sweep(), 1)
        self.assertEqual(self._artifacts()[1], [])
        self.assertEqual(output.read_bytes(), REMUXED_BYTES)


class LayoutContractTests(QuietTestCase):
    """``canonical_movie_layout_issue``: the shape the cleaner requires."""

    def _canonical(self, name: str = "Film (2020)") -> Path:
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        movie = folder / f"{name}.mkv"
        movie.write_bytes(MOVIE_BYTES)
        return movie

    def test_a_canonical_movie_has_no_issue(self) -> None:
        movie = self._canonical()
        self.assertIsNone(tc.canonical_movie_layout_issue(movie, self.root))

    def test_a_movie_sitting_in_the_library_root_is_refused(self) -> None:
        movie = self.root / "Film (2020).mkv"
        movie.write_bytes(MOVIE_BYTES)
        self.assertIn("directly under the library root",
                      tc.canonical_movie_layout_issue(movie, self.root))

    def test_a_symlinked_movie_is_refused(self) -> None:
        real = self._canonical()
        folder = self.root / "Link (2020)"
        folder.mkdir()
        link = folder / "Link (2020).mkv"
        link.symlink_to(real)
        self.assertIn("not a regular non-symlink file",
                      tc.canonical_movie_layout_issue(link, self.root))
        self.assertTrue(real.is_file())

    def test_a_stem_that_does_not_match_its_folder_is_refused(self) -> None:
        movie = self._canonical()
        odd = self.root / "Film (2020)" / "Something.Else.1080p.mkv"
        odd.write_bytes(MOVIE_BYTES)
        self.assertIn("movie stem does not match its movie-folder name",
                      tc.canonical_movie_layout_issue(odd, self.root))
        self.assertTrue(movie.is_file())

    def test_two_features_in_one_folder_are_refused(self) -> None:
        self._canonical()
        second = self.root / "Film (2020)" / "Film (2020).mp4"
        second.write_bytes(b"y" * 4096)
        issue = tc.canonical_movie_layout_issue(self.root / "Film (2020)" / "Film (2020).mkv",
                                                self.root)
        self.assertIn("expected one regular movie file in movie folder, found 2", issue)

    def test_a_folder_that_cannot_be_listed_is_refused_with_the_os_reason(self) -> None:
        movie = self._canonical()
        with mock.patch.object(Path, "iterdir", side_effect=OSError("share went away")):
            issue = tc.canonical_movie_layout_issue(movie, self.root)
        self.assertIn("could not inspect movie folder", issue)

    def test_a_sidecar_beside_the_movie_is_not_a_second_feature(self) -> None:
        movie = self._canonical()
        movie.with_name("Film (2020).eng.srt").write_text("x", encoding="utf-8")
        self.assertIsNone(tc.canonical_movie_layout_issue(movie, self.root))

    def test_a_staging_file_beside_the_movie_is_not_a_second_feature(self) -> None:
        """The cleaner's own temp prefix must not disqualify the movie it is for."""
        movie = self._canonical()
        (self.root / "Film (2020)" / f"{tc.TEMP_PREFIX}{'0' * 32}__Film (2020).mkv").write_bytes(b"x")
        self.assertIsNone(tc.canonical_movie_layout_issue(movie, self.root))

    def test_a_sample_beside_the_movie_is_not_a_second_feature(self) -> None:
        movie = self._canonical()
        (self.root / "Film (2020)" / "Film (2020)-sample.mkv").write_bytes(b"x" * 1024)
        self.assertIsNone(tc.canonical_movie_layout_issue(movie, self.root))

    def test_an_extras_folder_is_not_part_of_the_library(self) -> None:
        extras = self.root / "Film (2020)" / "Extras"
        extras.mkdir(parents=True)
        self.assertTrue(tc._in_extra_dir(extras / "behind the scenes.mkv", self.root))

    def test_a_folder_outside_the_scan_root_is_still_checked_for_extras(self) -> None:
        """``relative_to`` fails for a path outside the root; the name is used instead."""
        outside = Path("/somewhere/else/Extras/movie.mkv")
        self.assertTrue(tc._in_extra_dir(outside, self.root))
        self.assertFalse(tc._in_extra_dir(Path("/somewhere/else/movies/movie.mkv"), self.root))


class DiscoveryTests(QuietTestCase):
    """``discover_mkv_files``: what a sweep will and will not pick up."""

    def _discover(self, **kwargs: object) -> list[Path]:
        found, sizes, total = tc.discover_mkv_files(self.root, None, **kwargs)  # type: ignore[arg-type]
        self.assertEqual(total, sum(sizes))
        return found

    def test_movies_in_canonical_folders_are_found(self) -> None:
        first = self.root / "Alpha (2001)" / "Alpha (2001).mkv"
        first.parent.mkdir(parents=True)
        first.write_bytes(b"x" * 2048)
        second = self.root / "Bravo (2002)" / "Bravo (2002).mp4"
        second.parent.mkdir()
        second.write_bytes(b"y" * 1024)
        self.assertEqual(sorted(self._discover()), sorted([first, second]))

    def test_extras_and_hidden_folders_are_skipped_by_default(self) -> None:
        (self.root / "Alpha (2001)").mkdir(parents=True)
        (self.root / "Alpha (2001)" / "Alpha (2001).mkv").write_bytes(b"x")
        (self.root / "Extras").mkdir()
        (self.root / "Extras" / "trailer.mkv").write_bytes(b"x")
        (self.root / ".hidden").mkdir()
        (self.root / ".hidden" / "movie.mkv").write_bytes(b"x")
        found = self._discover()
        self.assertEqual([path.name for path in found], ["Alpha (2001).mkv"])

    def test_extras_are_included_when_the_run_asks_for_them(self) -> None:
        (self.root / "Alpha (2001)").mkdir(parents=True)
        (self.root / "Alpha (2001)" / "Alpha (2001).mkv").write_bytes(b"x")
        (self.root / "Extras").mkdir()
        extra = self.root / "Extras" / "making-of.mkv"
        extra.write_bytes(b"x")
        self.assertIn(extra, self._discover(skip_extras=False),
                      "with --include-extras the folder is walked and its videos are found")

    def test_samples_staging_files_and_other_containers_are_skipped(self) -> None:
        folder = self.root / "Alpha (2001)"
        folder.mkdir()
        movie = folder / "Alpha (2001).mkv"
        movie.write_bytes(b"x")
        (folder / "Alpha (2001)-sample.mkv").write_bytes(b"x")
        (folder / f"{tc.TEMP_PREFIX}{'0' * 32}__Alpha (2001).mkv").write_bytes(b"x")
        (folder / ".Alpha (2001).mkv").write_bytes(b"x")
        (folder / "Alpha (2001).avi").write_bytes(b"x")
        self.assertEqual(self._discover(), [movie])

    def test_a_movie_below_the_size_floor_is_skipped(self) -> None:
        folder = self.root / "Alpha (2001)"
        folder.mkdir()
        small = folder / "Alpha (2001).mkv"
        small.write_bytes(b"x" * 1024)
        self.assertEqual(self._discover(min_size=2 * 1024 * 1024), [])
        self.assertEqual(self._discover(min_size=0), [small])

    def test_a_file_that_cannot_be_stat_ed_is_counted_as_zero_bytes(self) -> None:
        """A size the filesystem will not give is not a reason to skip the movie."""
        folder = self.root / "Alpha (2001)"
        folder.mkdir()
        movie = folder / "Alpha (2001).mkv"
        movie.write_bytes(b"x" * 1024)
        real_stat = Path.stat

        def flaky(path: Path, **kwargs: object) -> object:
            if path == movie:
                raise OSError("share went away")
            return real_stat(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(Path, "stat", flaky):
            self.assertEqual(self._discover(), [movie])

    def test_a_directory_the_walk_cannot_read_is_reported_and_the_scan_continues(self) -> None:
        """A share with one unreadable folder still has movies in the others.

        ``os.walk`` hands the failure to ``onerror`` and keeps going, so the
        caller decides what to say - and the run must not lose the rest of the
        library because one folder was locked.
        """
        readable = self.root / "Alpha (2001)"
        readable.mkdir()
        movie = readable / "Alpha (2001).mkv"
        movie.write_bytes(b"x")
        locked = self.root / "Locked (2002)"
        locked.mkdir()
        real_scandir = os.scandir
        seen: list[str] = []

        def scandir(path: object) -> object:
            if str(path) == str(locked):
                raise OSError(errno.EACCES, "permission denied", str(locked))
            return real_scandir(path)  # type: ignore[arg-type]

        with mock.patch.object(tc.os, "scandir", scandir):
            found = tc.discover_mkv_files(
                self.root, None, onerror=lambda err: seen.append(str(err.filename)))[0]
        self.assertEqual(found, [movie])
        self.assertEqual(seen, [str(locked)], "the unreadable folder was reported, not swallowed")

    def test_an_interrupt_stops_the_scan(self) -> None:
        folder = self.root / "Alpha (2001)"
        folder.mkdir()
        (folder / "Alpha (2001).mkv").write_bytes(b"x")
        tc._interrupt_requested = True
        self.addCleanup(setattr, tc, "_interrupt_requested", False)
        self.assertEqual(self._discover(), [])

    def test_the_display_name_is_relative_to_the_library_root(self) -> None:
        movie = self.root / "Alpha (2001)" / "Alpha (2001).mkv"
        with mock.patch.object(tc, "_target_root", self.root):
            self.assertEqual(tc._rel_display_name(movie), str(Path("Alpha (2001)") / movie.name))

    def test_a_movie_outside_the_scan_root_is_shown_by_its_name(self) -> None:
        with mock.patch.object(tc, "_target_root", self.root / "library"):
            self.assertEqual(tc._rel_display_name(self.root / "Film (2020).mkv"),
                             "Film (2020).mkv")


if __name__ == "__main__":
    unittest.main()
