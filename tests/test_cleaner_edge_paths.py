"""The cleaner's degraded paths: crashes, orphans, locks and interrupts.

The remuxer is the tool that rewrites people's movie files, so every failure
here is about the *safety* half of the promise rather than the feature half: a
journal that cannot be written means the movie is skipped, an interrupted
transaction is either recovered from a journal or left for a human, and a lock
that cannot be proven stale means this run does not start.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import io
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import mkv_track_cleaner as tc  # noqa: E402


def _empty_stats() -> dict:
    return {
        "start_time": datetime.datetime.now(),
        "total_scanned": 0,
        "cleaned": [], "already_clean": [], "skipped_no_english": [],
        "skipped_layout": [], "deferred_hardlinked": [], "errors": [],
        "remux_without_srt": [], "keeper_needs_audiofit": [], "diagnostics": [],
        "total_space_saved_bytes": 0,
    }


def _movie_info(*tracks: dict, recognized: bool = True) -> dict:
    return {"container": {"recognized": recognized, "supported": recognized,
                          "properties": {"duration": 6_000_000_000_000}},
            "tracks": list(tracks), "attachments": [], "chapters": []}


def _audio(track_id: int, language: str = "eng", *, name: str = "", codec: str = "TrueHD") -> dict:
    return {"type": "audio", "codec": codec, "id": track_id,
            "properties": {"codec_id": "A_TRUEHD", "language": language, "track_name": name,
                           "audio_channels": 8, "flag_default": track_id == 0}}


def _sub(track_id: int, language: str = "eng") -> dict:
    return {"type": "subtitles", "codec": "SubRip/SRT", "id": track_id,
            "properties": {"codec_id": "S_TEXT/UTF8", "language": language}}


class _CleanerCase(unittest.TestCase):
    """A canonical movie folder and a stubbed multiplexer."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="tc_edge_")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self._root = tc._target_root
        self._run = tc._run_mkvmerge
        self._console = tc._console
        self._interrupt = tc._interrupt_requested
        self._active_temp = tc._active_temp_file
        tc._target_root = self.tmp
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._target_root = self._root
        tc._run_mkvmerge = self._run
        tc._console = self._console
        tc._interrupt_requested = self._interrupt
        tc._active_temp_file = self._active_temp

    def movie(self, name: str = "Film (2000)") -> Path:
        folder = self.tmp / name
        folder.mkdir(exist_ok=True)
        movie = folder / f"{name}.mkv"
        movie.write_bytes(b"x" * 4096)
        return movie

    def stub_identify(self, info: object) -> list[list[str]]:
        """Answer ``-J`` with ``info``; refuse anything else."""
        calls: list[list[str]] = []
        payload = info if isinstance(info, str) else json.dumps(info)

        def runner(argv: list[str], *args: object, **kwargs: object) -> tuple[int, str, str]:
            calls.append(list(argv))
            if "-J" in argv:
                return 0, payload, ""
            return 0, "", ""

        tc._run_mkvmerge = runner
        return calls

    def process(self, movie: Path, **kwargs: object) -> dict:
        stats = _empty_stats()
        with contextlib.redirect_stdout(io.StringIO()):
            tc.process_mkv(movie, stats, "stub-mkvmerge", log_file_path=None, **kwargs)
        return stats

    def leftovers(self, movie: Path) -> list[str]:
        return sorted(p.name for p in movie.parent.iterdir()
                      if p.name.startswith((tc.TEMP_PREFIX, tc.TRANSACTION_MARKER)))


class ProcessMkvDegradedTests(_CleanerCase):
    """Every way metadata inspection can fail, and what each one leaves behind:
    an error row, an untouched movie, and no staging debris."""

    def test_a_mkvmerge_failure_is_an_error_row_and_no_remux(self) -> None:
        movie = self.movie()
        before = movie.read_bytes()

        def failing(argv: list[str], *a: object, **k: object) -> tuple[int, str, str]:
            return 2, "", "mkvmerge: cannot open file"

        tc._run_mkvmerge = failing
        stats = self.process(movie)
        self.assertEqual(1, len(stats["errors"]))
        self.assertIn("cannot open file", stats["errors"][0]["error"])
        self.assertEqual(before, movie.read_bytes())
        self.assertEqual([], self.leftovers(movie))

    def test_invalid_json_metadata_is_an_error_row(self) -> None:
        movie = self.movie()
        self.stub_identify("{not json")
        stats = self.process(movie)
        self.assertEqual(1, len(stats["errors"]))
        self.assertIn("Invalid mkvmerge JSON", stats["errors"][0]["error"])

    def test_an_unrecognized_container_is_refused(self) -> None:
        movie = self.movie()
        self.stub_identify(_movie_info(recognized=False))
        stats = self.process(movie)
        self.assertIn("not a recognized", stats["errors"][0]["error"])

    def test_an_exception_during_inspection_is_one_error_row(self) -> None:
        movie = self.movie()

        def exploding(argv: list[str], *a: object, **k: object) -> tuple[int, str, str]:
            raise RuntimeError("the multiplexer went away")

        tc._run_mkvmerge = exploding
        stats = self.process(movie)
        self.assertIn("Metadata inspection exception", stats["errors"][0]["error"])

    def test_an_unusable_sidecar_skips_the_movie(self) -> None:
        movie = self.movie()
        (movie.parent / "Film (2000).eng.srt").write_text("this is not an srt\n", encoding="utf-8")
        self.stub_identify(_movie_info(_audio(1)))
        stats = self.process(movie)
        self.assertEqual(1, len(stats["skipped_sidecar"]))
        self.assertEqual([], stats["errors"])
        self.assertEqual([], self.leftovers(movie))

    def test_a_movie_with_nothing_retainable_is_skipped_with_its_reason(self) -> None:
        movie = self.movie()
        self.stub_identify(_movie_info(_sub(2)))
        stats = self.process(movie)
        self.assertEqual(1, len(stats["skipped_no_english"]))
        self.assertEqual([], stats["errors"])

    def test_a_dry_run_reports_the_plan_and_writes_nothing(self) -> None:
        movie = self.movie()
        (movie.parent / "Film (2000).eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        calls = self.stub_identify(_movie_info(_audio(1), _sub(2)))
        before = movie.read_bytes()
        stats = self.process(movie, dry_run=True)
        self.assertEqual(1, len(stats["cleaned"]))
        self.assertTrue(stats["cleaned"][0]["kept_audio"])
        self.assertEqual(before, movie.read_bytes())
        self.assertEqual(["-J", str(movie)], calls[0][1:])
        self.assertEqual([], self.leftovers(movie))

    def test_a_disk_with_no_room_is_an_error_before_any_journal(self) -> None:
        movie = self.movie()
        (movie.parent / "Film (2000).eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        self.stub_identify(_movie_info(_audio(1), _sub(2)))
        with mock.patch.object(tc, "check_free_space", return_value=(False, 10, 999, None)):
            stats = self.process(movie)
        self.assertEqual(1, len(stats["errors"]))
        self.assertIn("not enough free disk space", stats["errors"][0]["error"])
        self.assertEqual([], self.leftovers(movie))

    def test_a_journal_that_cannot_be_written_skips_the_movie(self) -> None:
        """Without a journal there is no crash recovery, so the movie is left
        completely alone rather than remuxed without one."""
        movie = self.movie()
        (movie.parent / "Film (2000).eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        self.stub_identify(_movie_info(_audio(1), _sub(2)))
        before = movie.read_bytes()
        with mock.patch.object(tc, "write_transaction", side_effect=OSError("read-only volume")):
            stats = self.process(movie)
        self.assertEqual(1, len(stats["errors"]))
        self.assertIn("could not create remux transaction journal", stats["errors"][0]["error"])
        self.assertEqual(before, movie.read_bytes())
        self.assertEqual([], self.leftovers(movie))

    def test_an_unexpected_exception_is_one_error_row_and_no_debris(self) -> None:
        movie = self.movie()
        (movie.parent / "Film (2000).eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        self.stub_identify(_movie_info(_audio(1), _sub(2)))
        with mock.patch.object(tc, "verify_remux_output", side_effect=RuntimeError("probe bug")):
            stats = self.process(movie)
        self.assertEqual(1, len(stats["errors"]))
        self.assertIn("probe bug", stats["errors"][0]["error"])
        self.assertEqual([], self.leftovers(movie))
        self.assertIsNone(tc._active_temp_file)

    def test_an_interrupt_during_the_remux_cleans_up_and_propagates(self) -> None:
        movie = self.movie()
        (movie.parent / "Film (2000).eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        self.stub_identify(_movie_info(_audio(1), _sub(2)))
        seen: dict = {}

        def raise_after_journal(journal: Path, payload: dict) -> None:
            tc.write_transaction(journal, payload)
            seen["temp"] = payload["temp_name"]

        with mock.patch.object(tc, "write_transaction", raise_after_journal), \
                mock.patch.object(tc, "_run_mkvmerge",
                                  side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.process(movie)
        self.assertEqual([], self.leftovers(movie), "an interrupted transaction leaves no debris")
        self.assertIsNone(tc._active_temp_file)

    def test_a_layout_issue_skips_before_any_inspection(self) -> None:
        folder = self.tmp / "Wrong (2000)"
        folder.mkdir()
        movie = folder / "Other name.mkv"
        movie.write_bytes(b"x" * 4096)
        calls = self.stub_identify(_movie_info(_audio(1), _sub(2)))
        stats = self.process(movie)
        self.assertEqual(1, len(stats["skipped_layout"]))
        self.assertEqual([], calls, "a skipped movie is never inspected")


class OrphanRecoveryTests(_CleanerCase):
    """A run that died leaves two shapes behind: a legacy temp with no journal,
    and a journaled transaction. Only the second can ever be promoted, and only
    when the journal proves it was verified against unchanged bytes."""

    def _journal(self, temp: Path, **overrides: object) -> Path:
        token = temp.name[len(tc.TEMP_PREFIX):].split("__", 1)[0]
        journal_path = temp.parent / f"{tc.TRANSACTION_MARKER}{token}{tc.TRANSACTION_JOURNAL_SUFFIX}"
        if temp.exists():
            # Freeze it first: the sweep ages corpses back to the epoch before it
            # consults the journal, and a snapshot must describe what the sweep sees.
            os.utime(temp, (0.0, 0.0))
            temp_snapshot = tc.source_snapshot(temp)
        else:
            temp_snapshot = {"size": 0, "mtime_ns": 0, "device": 0, "inode": 0,
                             "identity": ""}
        payload: dict = {
            "schema": tc.TRANSACTION_SCHEMA_VERSION,
            "token": token,
            "phase": "verified",
            "source_name": temp.name.split("__", 1)[1],
            "temp_name": temp.name,
            "source_snapshot": {"size": 1},
            "temp_snapshot": temp_snapshot,
            "verification_plan": {"baseline": {}},
        }
        payload.update(overrides)
        journal_path.write_text(json.dumps(payload), encoding="utf-8")
        return journal_path

    def _temp(self, name: str = "Film (2000).mkv", token: str = "a" * 32) -> Path:
        folder = self.tmp / "Film (2000)"
        folder.mkdir(exist_ok=True)
        return folder / f"{tc.TEMP_PREFIX}{token}__{name}"

    def _sweep(self) -> int:
        for artifact in self.tmp.rglob("*"):
            if artifact.is_file() and artifact.name.startswith(
                    (tc.TEMP_PREFIX, tc.TRANSACTION_MARKER)):
                with contextlib.suppress(OSError):
                    os.utime(artifact, (0.0, 0.0))
        with contextlib.redirect_stdout(io.StringIO()):
            return tc.cleanup_orphan_temps(self.tmp, "stub-mkvmerge", log_file_path=None)

    def test_a_legacy_orphan_beside_an_intact_original_is_removed(self) -> None:
        original = self.movie()
        legacy = original.parent / f"{tc.TEMP_PREFIX}{original.name}"
        legacy.write_bytes(b"half a remux")
        self.assertEqual(1, self._sweep())
        self.assertFalse(legacy.exists())
        self.assertTrue(original.exists())

    def test_a_lone_legacy_orphan_is_left_for_a_human(self) -> None:
        legacy = self.tmp / "Film (2000)" / f"{tc.TEMP_PREFIX}Film (2000).mkv"
        legacy.parent.mkdir()
        legacy.write_bytes(b"a remux with no original")
        self.assertEqual(0, self._sweep())
        self.assertTrue(legacy.exists(), "never promote a temp that no journal proves")

    def test_a_fresh_temp_is_not_touched(self) -> None:
        """A live sibling run's staging file is younger than the corpse age."""
        temp = self._temp()
        temp.write_bytes(b"still being written")
        with contextlib.redirect_stdout(io.StringIO()):
            handled = tc.cleanup_orphan_temps(self.tmp, "stub-mkvmerge", log_file_path=None)
        self.assertEqual(0, handled)
        self.assertTrue(temp.exists())

    def test_a_journaled_temp_beside_an_intact_original_is_cleaned_up(self) -> None:
        original = self.movie()
        temp = self._temp()
        temp.write_bytes(b"abandoned")
        journal = self._journal(temp)
        self.assertEqual(1, self._sweep())
        self.assertFalse(temp.exists())
        self.assertFalse(journal.exists())
        self.assertTrue(original.exists())

    def test_a_temp_without_a_valid_journal_is_preserved(self) -> None:
        temp = self._temp()
        temp.write_bytes(b"no journal here")
        self.assertEqual(0, self._sweep())
        self.assertTrue(temp.exists())

    def test_a_temp_whose_journal_names_a_different_temp_is_preserved(self) -> None:
        temp = self._temp()
        temp.write_bytes(b"bytes")
        self._journal(temp, temp_name="something-else.mkv")
        self.assertEqual(0, self._sweep())
        self.assertTrue(temp.exists())

    def test_an_unverified_orphan_is_never_promoted(self) -> None:
        temp = self._temp()
        temp.write_bytes(b"bytes")
        self._journal(temp, phase="remuxing")
        self.assertEqual(0, self._sweep())
        self.assertTrue(temp.exists())

    def test_a_verified_temp_whose_bytes_changed_is_preserved(self) -> None:
        temp = self._temp()
        temp.write_bytes(b"bytes")
        self._journal(temp, temp_snapshot={"size": 1, "mtime_ns": 1, "device": 1, "inode": 1,
                                           "identity": "not-this-file"})
        self.assertEqual(0, self._sweep())
        self.assertTrue(temp.exists())

    def test_a_verified_temp_without_a_plan_is_preserved(self) -> None:
        temp = self._temp()
        temp.write_bytes(b"bytes")
        self._journal(temp, verification_plan="not a mapping")
        self.assertEqual(0, self._sweep())
        self.assertTrue(temp.exists())

    def test_a_conversion_orphan_is_left_for_manual_review(self) -> None:
        """An MKV temp must never be renamed onto an absent ``.mp4`` name: that
        would put Matroska bytes behind an MP4 extension."""
        temp = self._temp(name="Film (2000).mp4")
        temp.write_bytes(b"bytes")
        self._journal(temp, output_name="Film (2000).mkv")
        self.assertEqual(0, self._sweep())
        self.assertTrue(temp.exists())

    def test_a_verified_orphan_is_promoted_back_to_its_source_name(self) -> None:
        temp = self._temp()
        temp.write_bytes(b"a fully verified remux")
        self._journal(temp)
        with mock.patch.object(tc, "verify_remux_output", return_value=(True, "", {})):
            self.assertEqual(1, self._sweep())
        self.assertFalse(temp.exists())
        recovered = temp.parent / "Film (2000).mkv"
        self.assertEqual(b"a fully verified remux", recovered.read_bytes())

    def test_a_failed_reverification_preserves_the_orphan(self) -> None:
        temp = self._temp()
        temp.write_bytes(b"bytes")
        self._journal(temp)
        with mock.patch.object(tc, "verify_remux_output", return_value=(False, "trailing junk", {})):
            self.assertEqual(0, self._sweep())
        self.assertTrue(temp.exists())

    def test_a_recovery_that_raises_leaves_the_orphan_and_keeps_sweeping(self) -> None:
        temp = self._temp()
        temp.write_bytes(b"bytes")
        self._journal(temp)
        with mock.patch.object(tc, "verify_remux_output", side_effect=RuntimeError("probe died")):
            self.assertEqual(0, self._sweep())
        self.assertTrue(temp.exists())

    def test_a_verified_orphan_with_a_changed_external_srt_is_preserved(self) -> None:
        temp = self._temp()
        temp.write_bytes(b"bytes")
        srt = temp.parent / "Film (2000).eng.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        self._journal(temp, external_srt={
            "path": str(srt), "size": 1, "mtime_ns": 1, "identity": "stale",
        })
        self.assertEqual(0, self._sweep())
        self.assertTrue(temp.exists())

    def test_a_stale_journal_beside_an_intact_original_is_removed(self) -> None:
        original = self.movie()
        temp = self._temp()
        journal = self._journal(temp)  # journal exists, temp never did
        self.assertEqual(1, self._sweep())
        self.assertFalse(journal.exists())
        self.assertTrue(original.exists())

    def test_a_suspected_mp4_mkv_pair_is_left_alone(self) -> None:
        folder = self.tmp / "Film (2000)"
        folder.mkdir(exist_ok=True)
        mkv = folder / "Film (2000).mkv"
        mkv.write_bytes(b"verified conversion output")
        mp4 = folder / "Film (2000).mp4"
        mp4.write_bytes(b"the original")
        token = "b" * 32
        journal = folder / f"{tc.TRANSACTION_MARKER}{token}{tc.TRANSACTION_JOURNAL_SUFFIX}"
        journal.write_text(json.dumps({
            "schema": tc.TRANSACTION_SCHEMA_VERSION, "token": token, "phase": "verified",
            "source_name": mp4.name, "temp_name": f"{tc.TEMP_PREFIX}{token}__{mp4.name}",
            "output_name": mkv.name, "temp_snapshot": {"size": 1, "mtime_ns": 1,
                                                       "device": 1, "inode": 1, "identity": "x"},
        }), encoding="utf-8")
        self.assertEqual(0, self._sweep())
        self.assertTrue(journal.exists(), "a pair nobody can prove must be left for a human")
        self.assertTrue(mp4.exists())

    def test_an_interrupt_stops_the_sweep_before_anything_is_touched(self) -> None:
        original = self.movie()
        legacy = original.parent / f"{tc.TEMP_PREFIX}{original.name}"
        legacy.write_bytes(b"bytes")
        tc._interrupt_requested = True
        try:
            self.assertEqual(0, self._sweep())
        finally:
            tc._interrupt_requested = False
        self.assertTrue(legacy.exists())

    def test_a_sweep_with_nothing_to_do_says_so_and_returns_zero(self) -> None:
        self.movie()
        self.assertEqual(0, self._sweep())


class SingleInstanceLockTests(_CleanerCase):
    """The run lock is fail-closed: an unreadable, foreign or live lock means
    this run does not start, and only a provably dead same-host lock is stale."""

    def _lock(self, body: str) -> Path:
        path = self.tmp / "run.lock"
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_fresh_lock_is_taken_and_names_this_host_and_pid(self) -> None:
        path = self.tmp / "run.lock"
        self.assertTrue(tc.acquire_lock(path, log_file_path=None))
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(3, len(lines))
        self.assertEqual(str(os.getpid()), lines[1])

    def test_an_unreadable_lock_means_another_instance(self) -> None:
        path = self._lock("not a pid at all\n")
        self.assertFalse(tc.acquire_lock(path, log_file_path=None))

    def test_a_lock_from_another_host_is_never_broken(self) -> None:
        path = self._lock(f"some-other-nas\n{os.getpid()}\n0\n")
        self.assertFalse(tc.acquire_lock(path, log_file_path=None))
        self.assertTrue(path.exists())

    def test_a_lock_held_by_a_live_process_is_respected(self) -> None:
        path = self._lock(f"{tc._this_hostname()}\n{os.getpid()}\n0\n")
        self.assertFalse(tc.acquire_lock(path, log_file_path=None))
        self.assertTrue(path.exists())

    def test_a_same_host_lock_with_a_dead_pid_is_stale_and_reclaimed(self) -> None:
        path = self._lock(f"{tc._this_hostname()}\n999999999\n0\n")
        self.assertTrue(tc.acquire_lock(path, log_file_path=None))

    def test_a_lock_written_by_the_old_single_line_format_is_understood(self) -> None:
        path = self._lock("999999999\n")
        self.assertTrue(tc.acquire_lock(path, log_file_path=None))

    def test_a_lock_that_cannot_be_created_is_refused(self) -> None:
        path = self.tmp / "run.lock"
        with mock.patch.object(tc.os, "open", side_effect=OSError(errno.EROFS, "read-only")):
            self.assertFalse(tc.acquire_lock(path, log_file_path=None))

    def test_a_stale_lock_that_cannot_be_removed_is_refused(self) -> None:
        path = self._lock(f"{tc._this_hostname()}\n999999999\n0\n")
        with mock.patch.object(Path, "unlink", side_effect=OSError(errno.EACCES, "denied")):
            self.assertFalse(tc.acquire_lock(path, log_file_path=None))

    def test_an_unexpected_error_while_locking_fails_closed(self) -> None:
        path = self.tmp / "run.lock"
        with mock.patch.object(tc, "_this_hostname", side_effect=RuntimeError("no gethostname")):
            self.assertFalse(tc.acquire_lock(path, log_file_path=None))

    def test_releasing_a_lock_that_is_already_gone_is_harmless(self) -> None:
        tc.release_lock(self.tmp / "never-existed.lock")

    def test_pid_liveness_answers_safely(self) -> None:
        self.assertTrue(tc._pid_alive("not a pid"), "an unparseable pid must not look dead")
        self.assertFalse(tc._pid_alive(0))
        self.assertFalse(tc._pid_alive(999999999))
        self.assertTrue(tc._pid_alive(os.getpid()))
        with mock.patch.object(tc.os, "kill", side_effect=PermissionError("not mine")):
            self.assertTrue(tc._pid_alive(os.getpid()), "someone else's process is still alive")


class _FakeCtypes:
    """Enough ``ctypes`` for the Windows-only branches, on any host."""

    class Structure:
        _fields_: list = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args

    @staticmethod
    def byref(obj: object) -> object:
        return obj

    @staticmethod
    def c_void_p(value: int) -> types.SimpleNamespace:
        return types.SimpleNamespace(value=value)

    @staticmethod
    def c_int(value: int) -> int:
        return value

    def __init__(self, kernel32: object, *, WinDLL: object = None) -> None:
        self.windll = types.SimpleNamespace(kernel32=kernel32)
        if WinDLL is not None:
            self.WinDLL = WinDLL
        self.wintypes = types.SimpleNamespace(DWORD=int, HANDLE=int, BOOL=int)


class WindowsBranchTests(_CleanerCase):
    """The Windows-only code paths are simulated, not skipped: they are the
    creation-time and priority handling a Windows host actually runs."""

    def _kernel32(self) -> types.SimpleNamespace:
        calls: list = []
        return types.SimpleNamespace(
            calls=calls,
            CreateFileW=lambda *a: 42,
            SetFileTime=lambda handle, *a: calls.append(("SetFileTime", handle)) or True,
            CloseHandle=lambda handle: calls.append(("CloseHandle", handle)),
            OpenProcess=lambda access, inherit, pid: 0 if pid == 12345 else 77,
        )

    def test_the_windows_creation_time_is_restored_with_the_same_handle_closed(self) -> None:
        kernel32 = self._kernel32()
        stat = types.SimpleNamespace(st_ctime_ns=1_600_000_000_000_000_000, st_ctime=1_600_000_000.0)
        path = pathlib.PurePosixPath("/media/movie.mkv")
        with mock.patch.object(tc.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": _FakeCtypes(kernel32)}):
            tc._restore_windows_ctime(path, stat)
        self.assertIn(("SetFileTime", 42), kernel32.calls)
        self.assertIn(("CloseHandle", 42), kernel32.calls)

    def test_a_windows_file_that_cannot_be_opened_is_left_alone(self) -> None:
        kernel32 = self._kernel32()
        kernel32.CreateFileW = lambda *a: 0
        with mock.patch.object(tc.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": _FakeCtypes(kernel32)}):
            tc._restore_windows_ctime(pathlib.PurePosixPath("/media/movie.mkv"),
                                      types.SimpleNamespace(st_ctime_ns=0, st_ctime=0.0))
        self.assertNotIn(("SetFileTime", 42), kernel32.calls)

    def test_a_ctypes_that_raises_is_swallowed(self) -> None:
        """A creation timestamp nobody can restore is cosmetic."""
        kernel32 = self._kernel32()
        kernel32.SetFileTime = lambda *a: (_ for _ in ()).throw(OSError("bad call"))
        with mock.patch.object(tc.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": _FakeCtypes(kernel32)}):
            tc._restore_windows_ctime(pathlib.PurePosixPath("/media/movie.mkv"),
                                      types.SimpleNamespace(st_ctime_ns=0, st_ctime=0.0))

    def test_a_negative_filetime_is_clamped(self) -> None:
        low, high = tc._unix_ns_to_filetime(-10**30)
        self.assertEqual(0, low)
        self.assertEqual(0, high)

    def test_restoring_times_on_a_stat_without_nanoseconds_falls_back(self) -> None:
        movie = self.movie()
        stat = types.SimpleNamespace(st_atime=1.0, st_mtime=2.0)
        tc.restore_file_times(movie, stat)
        info = movie.stat()
        self.assertEqual(2, int(info.st_mtime))

    def test_a_timestamp_the_filesystem_rejects_is_not_a_failed_remux(self) -> None:
        with mock.patch.object(tc.os, "utime", side_effect=OverflowError("too big")):
            tc.restore_file_times(Path("movie.mkv"), types.SimpleNamespace(
                st_atime_ns=1, st_mtime_ns=2))

    def test_windows_priority_is_lowered_when_the_api_allows_it(self) -> None:
        calls: list = []

        class WinDLL:
            def __init__(self, name: str, use_last_error: bool = False) -> None:
                self.GetCurrentProcess = lambda: 1
                self.SetPriorityClass = lambda h, c: calls.append(("class", c)) or True
                self.GetCurrentThread = lambda: 2
                self.SetThreadPriority = lambda h, p: calls.append(("thread", p)) or True

        fake_ctypes = _FakeCtypes(types.SimpleNamespace(), WinDLL=WinDLL)
        with mock.patch.object(tc.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            self.assertEqual("below-normal (Windows)", tc.apply_low_priority())
        self.assertEqual([("class", 0x00004000)], calls)

    def test_windows_priority_falls_back_to_the_thread_then_reports_the_error(self) -> None:
        class WinDLL:
            def __init__(self, name: str, use_last_error: bool = False) -> None:
                self.GetCurrentProcess = lambda: 1
                self.SetPriorityClass = lambda h, c: False
                self.GetCurrentThread = lambda: 2
                self.SetThreadPriority = lambda h, p: True

        with mock.patch.object(tc.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": _FakeCtypes(types.SimpleNamespace(), WinDLL=WinDLL)}):
            self.assertEqual("thread below-normal (Windows)", tc.apply_low_priority())

    def test_windows_priority_that_fails_entirely_says_so(self) -> None:
        class WinDLL:
            def __init__(self, name: str, use_last_error: bool = False) -> None:
                self.GetCurrentProcess = lambda: 1
                self.SetPriorityClass = lambda h, c: False
                self.GetCurrentThread = lambda: 2
                self.SetThreadPriority = lambda h, p: False

        with mock.patch.object(tc.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": _FakeCtypes(types.SimpleNamespace(), WinDLL=WinDLL)}):
            self.assertTrue(tc.apply_low_priority().startswith("unchanged (Windows error"))

    def test_a_ctypes_that_cannot_be_loaded_is_reported_not_raised(self) -> None:
        with mock.patch.object(tc.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": types.SimpleNamespace()}):
            self.assertTrue(tc.apply_low_priority().startswith("unchanged"))

    def test_windows_pid_liveness_never_guesses_wildly(self) -> None:
        kernel32 = self._kernel32()
        with mock.patch.object(tc.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": _FakeCtypes(kernel32)}):
            self.assertFalse(tc._pid_alive(12345), "a process that cannot be opened is gone")
            self.assertTrue(tc._pid_alive(4242))

    def test_windows_pid_liveness_fails_safe_when_ctypes_breaks(self) -> None:
        kernel32 = self._kernel32()
        kernel32.OpenProcess = lambda *a: (_ for _ in ()).throw(OSError("bad"))
        with mock.patch.object(tc.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": _FakeCtypes(kernel32)}):
            self.assertTrue(tc._pid_alive(4242), "assume alive: a skipped run beats two writers")


class InterruptTests(_CleanerCase):
    """Ctrl-C has to stop the run without leaving a half-written temp behind,
    and a second Ctrl-C must exit even if the cleanup cannot run."""

    def test_a_second_interrupt_exits_after_attempting_cleanup(self) -> None:
        temp = self.tmp / f"{tc.TEMP_PREFIX}film.mkv"
        temp.write_bytes(b"half written")
        tc._active_temp_file = temp
        tc._interrupt_requested = True
        try:
            with mock.patch.object(tc.os, "_exit", side_effect=SystemExit(1)) as exit_call, \
                    self.assertRaises(SystemExit):
                tc.request_interrupt()
            exit_call.assert_called_once_with(1)
            self.assertFalse(temp.exists(), "the second Ctrl-C still tries to delete the temp")
        finally:
            tc._interrupt_requested = False
            tc._active_temp_file = None

    def test_a_second_interrupt_survives_a_failing_cleanup(self) -> None:
        tc._interrupt_requested = True
        try:
            with mock.patch.object(tc, "safe_delete", side_effect=OSError("no")), \
                    mock.patch.object(tc.os, "_exit", side_effect=SystemExit(1)), \
                    mock.patch.object(tc, "_active_temp_file", Path("x")), \
                    self.assertRaises(SystemExit):
                tc.request_interrupt()
        finally:
            tc._interrupt_requested = False

    def test_the_first_interrupt_marks_the_run_and_finishes_the_display(self) -> None:
        console = mock.Mock()
        tc._console = console
        tc._interrupt_requested = False
        try:
            tc.request_interrupt()
            self.assertTrue(tc._interrupt_requested)
            console.finish_progress.assert_called_once()
        finally:
            tc._interrupt_requested = False
            tc._console = self._console

    def test_the_child_process_is_killed_when_one_is_running(self) -> None:
        class Proc:
            killed = False

            def poll(self) -> None:
                return None

            def kill(self) -> None:
                Proc.killed = True

        proc = Proc()
        with mock.patch.object(tc, "_active_proc", proc):
            tc._kill_active_child()
        self.assertTrue(Proc.killed)

    def test_killing_a_child_that_already_exited_is_harmless(self) -> None:
        class Proc:
            def poll(self) -> int:
                return 0

            def kill(self) -> None:
                raise AssertionError("must not kill an exited child")

        with mock.patch.object(tc, "_active_proc", Proc()):
            tc._kill_active_child()


class LiveConsoleTests(unittest.TestCase):
    """The renderer off a terminal: one line per decision, never a bar fight."""

    def setUp(self) -> None:
        self.out = io.StringIO()
        self._redirect = contextlib.redirect_stdout(self.out)
        self._redirect.__enter__()
        self.addCleanup(self._redirect.__exit__, None, None, None)
        self.console = tc.LiveConsole(use_color=False)
        self.console.is_tty = False

    def test_a_file_line_is_printed_once_off_a_terminal(self) -> None:
        self.console.begin_file("[1/1]", "Film (2000).mkv", 1024)
        self.assertIn("Film (2000).mkv", self.out.getvalue())

    def test_an_inline_suffix_is_printed_off_a_terminal(self) -> None:
        self.console.begin_file("[1/1]", "Film (2000).mkv", 1024)
        self.console.end_file_inline("replaced", kind="success")
        self.assertIn("replaced", self.out.getvalue())

    def test_progress_is_reported_once_per_bucket_off_a_terminal(self) -> None:
        self.console.begin_file("[1/1]", "Film (2000).mkv", 1024)
        self.console.remux_progress(10, started=0.0)
        self.console.remux_progress(11, started=0.0)
        text = self.out.getvalue()
        self.assertEqual(1, text.count("-> remux"), "same bucket, no second line")

    def test_a_progress_line_estimates_the_remaining_time(self) -> None:
        self.console.begin_file("[1/1]", "Film (2000).mkv", 1024)
        self.console.remux_progress(50, started=time_monotonic_minus(10.0))
        self.assertIn("left", self.out.getvalue())

    def test_an_out_of_range_percent_is_clamped(self) -> None:
        self.console.begin_file("[1/1]", "Film (2000).mkv", 1024)
        self.console.remux_progress(140, started=0.0)
        self.assertIn("100%", self.out.getvalue())

    def test_a_progress_message_is_throttled(self) -> None:
        self.console.progress_message("working")
        first = self.out.getvalue()
        self.console.progress_message("working again")
        self.assertEqual(first, self.out.getvalue(), "two messages within 2s print once")

    def test_details_after_a_pending_file_line_are_separated(self) -> None:
        self.console.begin_file("[1/1]", "Film (2000).mkv", 1024)
        self.console._file_line_pending = True
        self.console.mark_details()
        self.assertFalse(self.console._file_line_pending)

    def test_a_log_line_carries_its_level(self) -> None:
        self.console.log_line("12:00:00", "WARNING", "something odd")
        self.assertIn("[WARNING]", self.out.getvalue())

    def test_details_are_indented_under_the_file_line(self) -> None:
        self.console.begin_file("[1/1]", "Film (2000).mkv", 1024)
        self.console.detail("  -> removing 2 audio tracks  ")
        self.assertIn("removing 2 audio tracks", self.out.getvalue())

    def test_a_committed_line_after_progress_does_not_draw_a_bar(self) -> None:
        self.console._progress_active = True
        self.console.finish_progress()
        self.assertFalse(self.console._progress_active)


def time_monotonic_minus(seconds: float) -> float:
    import time

    return time.monotonic() - seconds


if __name__ == "__main__":
    unittest.main()
