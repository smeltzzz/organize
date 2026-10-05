"""The track cleaner's guards: the checks that decide *not* to touch a movie.

``mkv_track_cleaner.py`` is the only tool in the toolkit that rewrites and
deletes movie files, so almost all of its interesting behaviour is refusal.
Every test here injects the condition a guard exists for and asserts the
observable outcome - the reason a report prints, the file that was left alone,
the exit status a wrapper reads - rather than the internal call that produced
it.

The guards under test are the ones that only run when something is already
wrong: a multiplexer that answers with a code nobody handles, a remux whose
output no longer matches the plan it was built from, a lock file left behind by
a machine that rebooted, a sidecar that changed while it was being read, a
child process that has to be killed on the way out of a failure, and the
Windows-only paths - creation timestamps, priority class, ``OpenProcess`` -
that a POSIX coverage runner would otherwise never measure.
"""

from __future__ import annotations

import ctypes
import errno
import io
import os
import sys
import tempfile
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

MOVIE_BYTES = b"ORIGINAL-MOVIE-BYTES" * 400
REMUXED_BYTES = b"REMUXED-MOVIE-BYTES-" * 300

GOOD_SRT = "1\n00:00:01,000 --> 00:00:04,000\nHello.\n\n"


def dirty_spec() -> dict:
    return fake.make_spec([
        fake.video_track(),
        fake.audio_track(default=True),
        fake.audio_track(name="Director Commentary", codec="AC-3", codec_id="A_AC3",
                         channels=2, commentary=True),
        fake.subtitle_track(),
    ])


def clean_spec() -> dict:
    return fake.make_spec([fake.video_track(), fake.audio_track(default=True)])


class QuietTestCase(unittest.TestCase):
    """A case whose subjects print: everything they say is captured, not lost."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cleaner_guard_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # The cleaner appends through one module-level file object closed at
        # exit; on Windows an open handle makes the tree undeletable and the test
        # dies in teardown instead of reporting what it measured.
        self.addCleanup(tc.close_log_fp)
        capture = redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)


class MultiplexerResolutionTests(QuietTestCase):
    """Finding mkvmerge, and answering honestly when it cannot be found."""

    def test_an_explicit_path_that_is_not_on_path_is_still_used(self) -> None:
        """MKVToolNix's Windows installer does not put itself on PATH.

        ``--mkvmerge C:\\Program Files\\MKVToolNix\\mkvmerge.exe`` is the
        documented workaround, and it has to work when ``which`` cannot see it.
        """
        binary = self.root / "mkvmerge"
        binary.write_bytes(b"#!/bin/sh\n")
        with mock.patch.object(tc.shutil, "which", lambda name: None):
            self.assertEqual(tc.resolve_mkvmerge_path(str(binary)), str(binary))

    def test_an_explicit_path_that_does_not_exist_is_an_error_naming_it(self) -> None:
        with mock.patch.object(tc.shutil, "which", lambda name: None), \
                self.assertRaises(FileNotFoundError) as caught:
            tc.resolve_mkvmerge_path(str(self.root / "nope"))
        self.assertIn(str(self.root / "nope"), str(caught.exception))

    def test_the_standard_install_locations_are_searched_when_path_has_nothing(self) -> None:
        known = self.root / "mkvmerge-known"
        known.write_bytes(b"#!/bin/sh\n")
        with mock.patch.object(tc.shutil, "which", lambda name: None), \
                mock.patch.object(tc, "KNOWN_MKVMERGE_PATHS", (str(known),)):
            self.assertEqual(tc.resolve_mkvmerge_path(None), str(known))

    def test_a_known_location_found_through_which_is_used(self) -> None:
        with mock.patch.object(tc.shutil, "which",
                               lambda name: "/opt/mkvtoolnix/mkvmerge" if "mkvmerge" in name else None), \
                mock.patch.object(tc, "KNOWN_MKVMERGE_PATHS", ("/opt/mkvtoolnix/mkvmerge",)):
            self.assertEqual(tc.resolve_mkvmerge_path(None), "/opt/mkvtoolnix/mkvmerge")

    def test_no_multiplexer_anywhere_says_how_to_get_one(self) -> None:
        with mock.patch.object(tc.shutil, "which", lambda name: None), \
                mock.patch.object(tc, "KNOWN_MKVMERGE_PATHS", ()), \
                self.assertRaises(FileNotFoundError) as caught:
            tc.resolve_mkvmerge_path(None)
        self.assertIn("Install MKVToolNix or pass --mkvmerge", str(caught.exception))

    def test_a_version_probe_that_answers_with_a_failure_code_reports_unknown(self) -> None:
        """The banner is cosmetic; a run must not die for it."""
        with mock.patch.object(tc, "_run_mkvmerge", lambda cmd, on_progress=None: (2, "", "boom")):
            self.assertEqual(tc.get_mkvmerge_version("mkvmerge"), "unknown version")

    def test_a_version_probe_that_cannot_run_at_all_reports_unknown(self) -> None:
        with mock.patch.object(tc, "_run_mkvmerge",
                               mock.Mock(side_effect=OSError("no such file"))):
            self.assertEqual(tc.get_mkvmerge_version("mkvmerge"), "unknown version")

    def test_a_version_probe_printing_nothing_reports_unknown(self) -> None:
        with mock.patch.object(tc, "_run_mkvmerge", lambda cmd, on_progress=None: (0, "", "")):
            self.assertEqual(tc.get_mkvmerge_version("mkvmerge"), "unknown version")

    def test_ctrl_c_during_a_version_probe_is_not_swallowed(self) -> None:
        """An interrupt is the operator, not a probe failure."""
        with mock.patch.object(tc, "_run_mkvmerge", mock.Mock(side_effect=KeyboardInterrupt)), \
                self.assertRaises(KeyboardInterrupt):
            tc.get_mkvmerge_version("mkvmerge")

    def test_the_first_line_of_the_banner_is_the_version(self) -> None:
        with mock.patch.object(tc, "_run_mkvmerge",
                               lambda cmd, on_progress=None: (0, fake.VERSION_BANNER + "\nmore", "")):
            self.assertEqual(tc.get_mkvmerge_version("mkvmerge"), fake.VERSION_BANNER)


class ProgressAndFailureTextTests(QuietTestCase):
    """What the multiplexer's output is turned into for a human."""

    def test_a_duration_that_is_not_a_number_renders_as_zero(self) -> None:
        for value in (None, "soon", float("nan")):
            with self.subTest(value=value):
                self.assertEqual(tc.format_duration(value), "0s")

    def test_an_infinite_duration_does_not_overflow_the_render(self) -> None:
        self.assertIn("s", tc.format_duration(float("inf")))

    def test_an_estimate_needs_something_to_extrapolate_from(self) -> None:
        self.assertIsNone(tc._eta_seconds(0.0, 0, 0, 0, 0), "no elapsed time, no estimate")
        self.assertIsNone(tc._eta_seconds(5.0, 0, 0, 0, 0), "nothing done yet, nothing to divide by")

    def test_an_estimate_from_bytes_is_the_remaining_bytes_at_the_observed_rate(self) -> None:
        self.assertAlmostEqual(tc._eta_seconds(10.0, 100, 400, 0, 0), 30.0)
        self.assertEqual(tc._eta_seconds(10.0, 400, 400, 0, 0), 0.0, "finished means no wait")

    def test_a_run_with_no_sizes_estimates_from_the_file_count(self) -> None:
        """The fallback that keeps an ETA on screen when the sizes are unknown."""
        self.assertAlmostEqual(tc._eta_seconds(10.0, 0, 0, 2, 10), 40.0)
        self.assertEqual(tc._eta_seconds(10.0, 0, 0, 10, 10), 0.0)

    def test_gui_mode_progress_is_read_in_every_form_mkvmerge_emits(self) -> None:
        self.assertEqual(tc._parse_mkvmerge_progress("#GUI#progress 42%"), 42)
        self.assertEqual(tc._parse_mkvmerge_progress("# GUI # progress#percent=17"), 17)
        self.assertEqual(tc._parse_mkvmerge_progress("#GUI#progress#parts=3/4"), 75)
        self.assertEqual(tc._parse_mkvmerge_progress("Progress: 17%"), 17)

    def test_a_parts_fraction_with_no_total_is_not_a_percentage(self) -> None:
        self.assertIsNone(tc._parse_mkvmerge_progress("#GUI#progress #parts=3/0"))

    def test_a_line_that_is_not_progress_answers_nothing(self) -> None:
        self.assertIsNone(tc._parse_mkvmerge_progress(""))
        self.assertIsNone(tc._parse_mkvmerge_progress("mkvmerge v80.0.0"))

    def test_a_percentage_outside_the_range_is_clamped(self) -> None:
        """A bar drawn from 140% would run off the end of the terminal."""
        self.assertEqual(tc._parse_mkvmerge_progress("Progress: 140%"), 100)
        self.assertEqual(tc._parse_mkvmerge_progress("#GUI#progress#percent=900"), 100)
        self.assertIsNone(tc._parse_mkvmerge_progress("#GUI#progress -5%"),
                          "a negative percentage is not progress at all")

    def test_a_failure_with_no_output_is_summarised_by_its_code(self) -> None:
        self.assertEqual(tc._summarize_mkvmerge_failure("   ", 2), "mkvmerge remux failed with code 2")

    def test_a_gui_mode_error_is_lifted_out_of_the_noise(self) -> None:
        """``--gui-mode`` is how the tool runs it, so the real error is wrapped."""
        output = ("#GUI#progress 10%\n"
                  "#GUI#error#message=The track 1 is not a supported codec.\n"
                  "Progress: 10%\n")
        self.assertEqual(tc._summarize_mkvmerge_failure(output, 1),
                         "ERROR: The track 1 is not a supported codec.")

    def test_a_plain_failure_keeps_its_own_lines_and_drops_the_progress(self) -> None:
        output = "\n".join(["", "Error: unsupported codec", "#GUI#progress 55%", "  "])
        self.assertEqual(tc._summarize_mkvmerge_failure(output, 1), "Error: unsupported codec")

    def test_a_very_chatty_failure_is_cut_to_a_report_line(self) -> None:
        summary = tc._summarize_mkvmerge_failure("x" * 5000, 1)
        self.assertLessEqual(len(summary), 500)
        self.assertTrue(summary.endswith("..."))


class WindowsTimestampTests(QuietTestCase):
    """The creation date a remux must not reset, and the priority it must lower.

    Both are Windows-only and both are marked in the source as untestable from
    here. They are not: ``os.name`` is the switch and ``ctypes.windll`` is the
    only thing missing, so a recording double for kernel32 exercises the real
    conversion arithmetic and the real ``finally`` that closes the handle.
    """

    def _stat(self, ctime_ns: int = 1_600_000_000_000_000_000) -> os.stat_result:
        movie = self.root / "Film (2020).mkv"
        movie.write_bytes(MOVIE_BYTES)
        info = movie.stat()
        return SimpleNamespace(st_atime=info.st_atime, st_mtime=info.st_mtime,
                               st_atime_ns=info.st_atime_ns, st_mtime_ns=info.st_mtime_ns,
                               st_ctime=ctime_ns / 1e9, st_ctime_ns=ctime_ns)  # type: ignore[return-value]

    def test_the_filetime_conversion_matches_the_windows_epoch(self) -> None:
        """1970-01-01 is 11644473600 seconds after 1601-01-01, in 100ns ticks."""
        low, high = tc._unix_ns_to_filetime(0)
        self.assertEqual((high << 32) + low, 11_644_473_600 * 10_000_000)

    def test_a_timestamp_before_the_windows_epoch_is_clamped_not_wrapped(self) -> None:
        """A negative FILETIME would be a date in the 58th millennium.

        A filesystem that reports a nonsense creation time must not put one in
        the file's metadata.
        """
        self.assertEqual(tc._unix_ns_to_filetime(-12_000_000_000 * 10_000_000_000), (0, 0))

    def test_the_creation_time_is_restored_from_the_original_stat(self) -> None:
        calls: dict[str, list] = {"create": [], "set": [], "close": []}

        def create_file(name: str, access: int, share: int, _sa: object, mode: int,
                        flags: int, _template: object) -> int:
            calls["create"].append((name, access, mode))
            return 4242

        def set_file_time(handle: int, creation: object, _a: object, _w: object) -> int:
            calls["set"].append((handle, (creation.dwLowDateTime, creation.dwHighDateTime)))
            return 1

        kernel32 = SimpleNamespace(CreateFileW=create_file, SetFileTime=set_file_time,
                                   CloseHandle=lambda handle: calls["close"].append(handle))
        stat = self._stat()
        # ``byref`` hands the callee a CArgObject a Python double cannot read
        # through; passing the structure itself is the same call as far as the
        # code under test is concerned.
        with platforms.windows(), \
                mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel32),
                                  create=True), \
                mock.patch.object(ctypes, "byref", lambda obj: obj):
            tc._restore_windows_ctime(self.root / "Film (2020).mkv", stat)
        expected = tc._unix_ns_to_filetime(stat.st_ctime_ns)
        self.assertEqual(calls["create"], [(str(self.root / "Film (2020).mkv"), 0x0100, 3)],
                         "opened for write-attributes only, and only if it already exists")
        self.assertEqual(calls["set"], [(4242, expected)])
        self.assertEqual(calls["close"], [4242], "the handle is always released")

    def test_the_handle_is_closed_even_when_setting_the_time_fails(self) -> None:
        """On Windows an open handle means nothing can delete or replace the file.

        The movie has just been swapped into place; leaking a handle here would
        leave the library locked and the next run unable to touch it.
        """
        closed: list[int] = []

        def set_file_time(handle: int, *_args: object) -> int:
            raise ctypes.ArgumentError(None, "bad FILETIME")

        kernel32 = SimpleNamespace(CreateFileW=lambda *args: 99, SetFileTime=set_file_time,
                                   CloseHandle=closed.append)
        with platforms.windows(), \
                mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel32),
                                  create=True), \
                mock.patch.object(ctypes, "byref", lambda obj: obj):
            tc._restore_windows_ctime(self.root / "Film (2020).mkv", self._stat())
        self.assertEqual(closed, [99])

    def test_a_file_that_cannot_be_opened_leaves_the_timestamp_alone(self) -> None:
        set_calls: list[int] = []
        kernel32 = SimpleNamespace(
            CreateFileW=lambda *args: ctypes.c_void_p(-1).value,
            SetFileTime=lambda *args: set_calls.append(1),
            CloseHandle=lambda handle: None,
        )
        with platforms.windows(), \
                mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel32),
                                  create=True):
            tc._restore_windows_ctime(self.root / "Film (2020).mkv", self._stat())
        self.assertEqual(set_calls, [], "an invalid handle is not passed on to SetFileTime")

    def test_a_host_without_kernel32_skips_the_creation_time_quietly(self) -> None:
        with platforms.windows(), mock.patch.object(ctypes, "windll", None, create=True):
            tc._restore_windows_ctime(self.root / "Film (2020).mkv", self._stat())

    def test_off_windows_nothing_is_attempted(self) -> None:
        windll = mock.Mock()
        with platforms.posix(), mock.patch.object(ctypes, "windll", windll, create=True):
            tc._restore_windows_ctime(self.root / "Film (2020).mkv", self._stat())
        windll.assert_not_called()
        self.assertEqual(os.name, "posix", "the host platform is put back the way it was")

    def test_a_stat_without_nanosecond_fields_is_applied_in_whole_seconds(self) -> None:
        """Not every ``stat_result`` a caller can hand over carries ``*_ns``.

        A journal replayed from an older schema, or a probe payload rebuilt by
        hand, has only the float pair; the remux has already succeeded at that
        point and a timestamp is not worth failing it.
        """
        movie = self.root / "Film (2020).mkv"
        movie.write_bytes(MOVIE_BYTES)
        info = movie.stat()
        legacy = SimpleNamespace(st_atime=info.st_atime, st_mtime=info.st_mtime)
        calls: list[object] = []
        with mock.patch.object(tc.os, "utime", lambda path, times: calls.append(times)), \
                mock.patch.object(tc, "_restore_windows_ctime", lambda *a: None):
            tc.restore_file_times(movie, legacy)  # type: ignore[arg-type]
        self.assertEqual(calls, [(info.st_atime, info.st_mtime)])

    def test_a_timestamp_the_filesystem_refuses_entirely_is_not_fatal(self) -> None:
        movie = self.root / "Film (2020).mkv"
        movie.write_bytes(MOVIE_BYTES)
        with mock.patch.object(tc.os, "utime", side_effect=OSError("out of range")), \
                mock.patch.object(tc, "_restore_windows_ctime", lambda *a: None):
            tc.restore_file_times(movie, movie.stat())  # must not raise

    def test_the_windows_priority_class_is_lowered_and_the_banner_says_so(self) -> None:
        """A sweep must not starve a Jellyfin transcode running beside it.

        The returned string is printed in the run banner, so it is the
        operator's only evidence about what priority six hours of remuxing ran
        at.
        """
        set_calls: list[tuple] = []
        kernel32 = SimpleNamespace(
            GetCurrentProcess=lambda: 1, GetCurrentThread=lambda: 2,
            SetPriorityClass=lambda handle, cls: set_calls.append(("process", cls)) or True,
            SetThreadPriority=lambda handle, prio: set_calls.append(("thread", prio)),
        )
        with platforms.windows(), mock.patch.object(ctypes, "WinDLL",
                                                    lambda name, **kw: kernel32, create=True):
            self.assertEqual(tc.apply_low_priority(), "below-normal (Windows)")
        self.assertEqual(set_calls, [("process", 0x00004000)])

    def test_a_process_priority_the_console_refuses_falls_back_to_the_thread(self) -> None:
        set_calls: list[tuple] = []
        kernel32 = SimpleNamespace(
            GetCurrentProcess=lambda: 1, GetCurrentThread=lambda: 2,
            SetPriorityClass=lambda handle, cls: False,
            SetThreadPriority=lambda handle, prio: set_calls.append(prio) or True,
        )
        with platforms.windows(), mock.patch.object(ctypes, "WinDLL",
                                                    lambda name, **kw: kernel32, create=True):
            self.assertEqual(tc.apply_low_priority(), "thread below-normal (Windows)")
        self.assertEqual(set_calls, [-1])

    def test_a_priority_nobody_will_change_is_reported_with_the_windows_error(self) -> None:
        kernel32 = SimpleNamespace(
            GetCurrentProcess=lambda: 1, GetCurrentThread=lambda: 2,
            SetPriorityClass=lambda handle, cls: False,
            SetThreadPriority=lambda handle, prio: False,
        )
        with platforms.windows(), mock.patch.object(ctypes, "WinDLL",
                                                    lambda name, **kw: kernel32, create=True), \
                mock.patch.object(ctypes, "get_last_error", lambda: 5, create=True):
            self.assertEqual(tc.apply_low_priority(), "unchanged (Windows error 5)")

    def test_a_priority_call_that_raises_still_returns_a_banner_string(self) -> None:
        def exploding(name: str, **kwargs: object) -> object:
            raise OSError("kernel32 is not what it used to be")

        with platforms.windows(), mock.patch.object(ctypes, "WinDLL", exploding, create=True):
            self.assertIn("unchanged", tc.apply_low_priority())

    def test_on_posix_the_sweep_nices_itself(self) -> None:
        nice = mock.Mock()
        with platforms.posix(), mock.patch.object(tc.os, "nice", nice, create=True):
            self.assertEqual(tc.apply_low_priority(), "nice +10")
        nice.assert_called_once_with(10)

    def test_a_nice_that_is_not_permitted_says_so_instead_of_failing(self) -> None:
        with platforms.posix(), mock.patch.object(tc.os, "nice", create=True,
                                                  new=mock.Mock(side_effect=PermissionError)):
            self.assertEqual(tc.apply_low_priority(), "unchanged (nice: permission denied)")

    def test_a_platform_without_nice_at_all_says_so(self) -> None:
        with platforms.posix(), mock.patch.object(
                tc.os, "nice", create=True, new=mock.Mock(side_effect=OSError("no"))):
            self.assertEqual(tc.apply_low_priority(), "unchanged (no)")


class TrackClassificationTests(QuietTestCase):
    """Which tracks are ballast, and which of them must survive."""

    def test_an_isolated_score_is_commentary_and_so_goes(self) -> None:
        for name in ("Isolated Score", "Music & Effects", "Score Only", "Isolated Music"):
            with self.subTest(name=name):
                self.assertTrue(tc.is_commentary_name(name))

    def test_a_descriptive_audio_track_is_commentary_and_so_goes(self) -> None:
        for name in ("Audio Description", "Described Video", "DVS", "Visual Description"):
            with self.subTest(name=name):
                self.assertTrue(tc.is_commentary_name(name))

    def test_a_directors_cut_is_a_feature_not_a_commentary(self) -> None:
        """The scar shape: "Director" is in both titles and only one is ballast.

        Dropping a Director's Cut would delete the version somebody downloaded
        on purpose.
        """
        for name in ("Director's Cut", "Directors Cut", "Producer's Version", "Theatrical Edition"):
            with self.subTest(name=name):
                self.assertFalse(tc.is_commentary_name(name))

    def test_bare_description_is_commentary_on_audio_and_not_on_subtitles(self) -> None:
        self.assertTrue(tc.is_commentary_name("English Description"))
        self.assertFalse(tc.is_commentary_name("English Description", track_type="subtitles"),
                         "an SDH subtitle's description is a feature, not ballast")

    def test_an_empty_track_name_is_never_commentary(self) -> None:
        self.assertFalse(tc.is_commentary_name(""))

    def test_a_missing_track_matches_no_language(self) -> None:
        self.assertFalse(tc.is_matching_language(None, {"en"}))
        self.assertFalse(tc.is_matching_language({"properties": {}}, {"en"}))

    def test_a_missing_track_is_never_a_named_dub(self) -> None:
        self.assertFalse(tc.is_named_dub_track(None))
        self.assertFalse(tc.is_named_dub_track({"properties": {}}))

    def test_a_language_is_not_a_dub_unless_the_file_says_so(self) -> None:
        spanish = {"properties": {"track_name": "Spanish", "language": "spa"}}
        self.assertFalse(tc.is_named_dub_track(spanish))
        self.assertTrue(tc.is_named_dub_track(
            {"properties": {"track_name": "Spanish Dub", "language": "spa"}}))
        self.assertTrue(tc.is_named_dub_track({"properties": {"track_name": "Voice-over"}}))

    def test_a_hardlink_count_the_filesystem_will_not_give_is_read_as_one(self) -> None:
        """One link means "nobody is seeding this", so the movie may be rewritten.

        The guard exists to *refuse* a seeded movie, so an unreadable count has
        to answer the permissive value only because the alternative - refusing
        everything a filesystem will not describe - would leave whole libraries
        untouched.
        """
        self.assertEqual(tc.hardlink_count(self.root / "absent.mkv"), 1)
        with mock.patch.object(Path, "stat", mock.Mock(side_effect=AttributeError("no st_nlink"))):
            self.assertEqual(tc.hardlink_count(self.root), 1)

    def test_a_real_second_hardlink_is_reported_as_such(self) -> None:
        movie = self.root / "Film (2020).mkv"
        movie.write_bytes(MOVIE_BYTES)
        os.link(movie, self.root / "seed-copy.mkv")
        self.assertEqual(tc.hardlink_count(movie), 2)

    def test_a_directory_sync_that_cannot_be_done_is_skipped(self) -> None:
        """Durability is best-effort; the journal write itself is not."""
        with platforms.windows():
            tc._fsync_directory(self.root)  # returns immediately on Windows
        with mock.patch.object(tc.os, "open", side_effect=OSError("no directory handles here")):
            tc._fsync_directory(self.root)
        close = mock.Mock()
        with mock.patch.object(tc.os, "open", lambda *a, **k: 3), \
                mock.patch.object(tc.os, "fsync", side_effect=OSError("not a syncable fd")), \
                mock.patch.object(tc.os, "close", close):
            tc._fsync_directory(self.root)
        close.assert_called_once_with(3)

    def test_a_descriptor_that_cannot_be_closed_does_not_fail_the_journal_write(self) -> None:
        with mock.patch.object(tc.os, "open", lambda *a, **k: 3), \
                mock.patch.object(tc.os, "fsync", lambda fd: None), \
                mock.patch.object(tc.os, "close", side_effect=OSError("bad descriptor")):
            tc._fsync_directory(self.root)  # must not raise

    def test_an_empty_sidecar_record_never_matches(self) -> None:
        for record in ({}, {"valid": False}, {"valid": True}, None):
            with self.subTest(record=record):
                self.assertFalse(tc.external_srt_snapshot_matches(record))

    def test_a_temp_name_that_carries_no_transaction_token_is_not_a_journal_owner(self) -> None:
        self.assertIsNone(tc._transaction_token_from_temp_name("Film (2020).mkv"))
        self.assertIsNone(tc._transaction_token_from_temp_name(f"{tc.TEMP_PREFIX}Film (2020).mkv"))
        self.assertIsNone(tc._transaction_token_from_temp_name(f"{tc.TEMP_PREFIX}zz__Film.mkv"))
        token = "0" * 32
        self.assertEqual(tc._transaction_token_from_temp_name(f"{tc.TEMP_PREFIX}{token}__Film.mkv"),
                         token)

    def test_the_aac_seven_to_eight_channel_report_difference_is_accepted(self) -> None:
        """MKVToolNix 100 re-reports an AAC 7.1 stream as 8 channels after a remux.

        The exception is deliberately as narrow as the observed bug: same
        codec_id, expected 7, actual 8, and *every other* fingerprint field
        identical. Anything wider would let a genuinely different audio track
        through the verification that decides whether a movie may be replaced.
        """
        expected = {"type": "audio", "codec_id": "A_AAC", "channels": 7, "language": "eng"}
        actual = dict(expected, channels=8)
        self.assertTrue(tc.retained_audio_fingerprint_matches(actual, expected))

    def test_every_other_difference_in_the_retained_audio_is_refused(self) -> None:
        expected = {"type": "audio", "codec_id": "A_AAC", "channels": 7, "language": "eng"}
        for change in ({"channels": 6}, {"codec_id": "A_AC3"}, {"language": "fra"},
                       {"type": "video"}):
            with self.subTest(change=change):
                self.assertFalse(
                    tc.retained_audio_fingerprint_matches(dict(expected, **change), expected))

    def test_a_fingerprint_that_is_not_a_mapping_is_refused(self) -> None:
        self.assertFalse(tc.retained_audio_fingerprint_matches([], {}))
        self.assertFalse(tc.retained_audio_fingerprint_matches({}, None))


class SidecarValidationTests(QuietTestCase):
    """The cleaner's own read of the one sidecar allowed to replace embeds."""

    def _movie(self, name: str = "Film (2020)") -> Path:
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        movie = folder / f"{name}.mkv"
        movie.write_bytes(MOVIE_BYTES)
        return movie

    def test_a_symlinked_sidecar_is_refused(self) -> None:
        movie = self._movie()
        real = movie.with_name("real.srt")
        real.write_text(GOOD_SRT, encoding="utf-8")
        movie.with_name("Film (2020).eng.srt").symlink_to(real)
        ok, reason, snapshot = tc._validate_srt_file(movie.with_name("Film (2020).eng.srt"))
        self.assertFalse(ok)
        self.assertIn("not a regular non-symlink file", reason)
        self.assertIsNone(snapshot)

    def test_an_empty_sidecar_is_refused(self) -> None:
        movie = self._movie()
        sidecar = movie.with_name("Film (2020).eng.srt")
        sidecar.write_bytes(b"")
        ok, reason, _ = tc._validate_srt_file(sidecar)
        self.assertFalse(ok)
        self.assertIn("is empty", reason)

    def test_an_oversized_sidecar_is_refused(self) -> None:
        movie = self._movie()
        sidecar = movie.with_name("Film (2020).eng.srt")
        with sidecar.open("wb") as handle:
            handle.write(b"x" * (tc.EXTERNAL_SRT_MAX_BYTES + 1))
        ok, reason, _ = tc._validate_srt_file(sidecar)
        self.assertFalse(ok)
        self.assertIn("safety limit", reason)

    def test_a_sidecar_that_cannot_be_read_is_refused_with_the_os_reason(self) -> None:
        movie = self._movie()
        sidecar = movie.with_name("Film (2020).eng.srt")
        sidecar.write_text(GOOD_SRT, encoding="utf-8")
        with mock.patch.object(Path, "read_bytes",
                               side_effect=OSError(errno.EACCES, "permission denied")):
            ok, reason, _ = tc._validate_srt_file(sidecar)
        self.assertFalse(ok)
        self.assertIn("could not read external SRT", reason)

    def test_a_sidecar_that_cannot_be_stat_ed_is_refused(self) -> None:
        ok, reason, _ = tc._validate_srt_file(self.root / "absent.eng.srt")
        self.assertFalse(ok)
        self.assertIn("could not stat external SRT", reason)

    def test_a_sidecar_that_is_not_text_is_refused(self) -> None:
        movie = self._movie()
        sidecar = movie.with_name("Film (2020).eng.srt")
        sidecar.write_bytes(b"\x81\x81\x81\x81")
        ok, reason, _ = tc._validate_srt_file(sidecar)
        self.assertFalse(ok)
        self.assertIn("unsupported text encoding", reason)

    def test_a_sidecar_that_changes_while_it_is_being_validated_is_refused(self) -> None:
        """The read is not atomic, so the identity is checked on both sides of it.

        A sidecar being rewritten by the extractor while the cleaner reads it
        would otherwise be validated from one set of bytes and snapshotted from
        another - and the post-remux re-check would then compare against
        something that never existed.
        """
        movie = self._movie()
        sidecar = movie.with_name("Film (2020).eng.srt")
        sidecar.write_text(GOOD_SRT, encoding="utf-8")
        real_read = Path.read_bytes
        calls = {"n": 0}

        def read_and_rewrite(path: Path) -> bytes:
            data = real_read(path)
            calls["n"] += 1
            if calls["n"] == 1:
                # The extractor publishing over the sidecar mid-read: same
                # bytes, a different mtime, so the identity digest differs.
                os.utime(path, (1_000_000_000, 1_000_000_000))
            return data

        with mock.patch.object(Path, "read_bytes", read_and_rewrite):
            ok, reason, _ = tc._validate_srt_file(sidecar)
        self.assertFalse(ok)
        self.assertIn("changed while being validated", reason)

    def test_a_sidecar_that_cannot_be_re_stat_ed_is_refused(self) -> None:
        movie = self._movie()
        sidecar = movie.with_name("Film (2020).eng.srt")
        sidecar.write_text(GOOD_SRT, encoding="utf-8")
        calls = {"n": 0}
        real_stat = Path.stat

        def flaky_stat(path: Path, **kwargs: object) -> object:
            if path == sidecar:
                calls["n"] += 1
                # The first two are the initial stat and is_symlink's lstat;
                # the third is the re-check after the read, which is the one
                # under test.
                if calls["n"] > 2:
                    raise OSError("share went away")
            return real_stat(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(Path, "stat", flaky_stat):
            ok, reason, _ = tc._validate_srt_file(sidecar)
        self.assertFalse(ok)
        self.assertIn("could not re-stat external SRT after reading", reason)

    def test_a_valid_sidecar_is_accepted_with_a_snapshot_of_its_bytes(self) -> None:
        movie = self._movie()
        sidecar = movie.with_name("Film (2020).eng.srt")
        sidecar.write_text(GOOD_SRT, encoding="utf-8")
        ok, reason, snapshot = tc._validate_srt_file(sidecar)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertRegex(snapshot["sha256"], r"^[0-9a-f]{64}$")

    def test_a_legacy_sidecar_that_cannot_be_promoted_stops_the_run_touching_the_movie(self) -> None:
        """A broken ``.en.srt`` is authoritative: the embeds must not be dropped.

        The cleaner strips every embedded subtitle once a validated sidecar
        exists. If a legacy sidecar is present but cannot be promoted *and*
        cannot be validated, stripping the embeds on the strength of a file
        nobody can read would leave the movie with no subtitles at all - so the
        whole movie is skipped and the report says why.
        """
        movie = self._movie()
        occupied = movie.with_name("Film (2020).eng.srt")
        occupied.mkdir()  # the canonical name is held by a directory
        movie.with_name("Film (2020).en.srt").write_text(GOOD_SRT, encoding="utf-8")
        result = tc.validate_exact_external_english_srt(movie)
        self.assertFalse(result["valid"])
        self.assertIn("could not be promoted", result["reason"])

    def test_a_broken_canonical_sidecar_does_not_hide_a_valid_sdh_one(self) -> None:
        """The reason the covering list is walked instead of short-circuited."""
        movie = self._movie()
        movie.with_name("Film (2020).eng.srt").write_bytes(b"\x81\x81\x81\x81")
        sdh = movie.with_name("Film (2020).eng.sdh.srt")
        sdh.write_text(GOOD_SRT, encoding="utf-8")
        result = tc.validate_exact_external_english_srt(movie)
        self.assertTrue(result["valid"])
        self.assertEqual(Path(result["path"]), sdh,
                         "the record points at the sidecar that was actually validated")

    def test_no_sidecar_at_all_reads_as_absent_not_as_unusable(self) -> None:
        movie = self._movie()
        result = tc.validate_exact_external_english_srt(movie)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "external SRT is absent")


class DurableSwapTests(QuietTestCase):
    """``safe_replace`` and the journal write behind it."""

    def test_a_replace_that_is_refused_is_retried_and_then_succeeds(self) -> None:
        """A Windows indexer or antivirus holding the destination is transient."""
        src = self.root / "temp_clean_x.mkv"
        src.write_bytes(REMUXED_BYTES)
        dst = self.root / "Film (2020).mkv"
        dst.write_bytes(MOVIE_BYTES)
        attempts = {"n": 0}
        real_replace = os.replace

        def flaky_replace(a: str, b: str, **kwargs: object) -> None:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PermissionError("another process has the file open")
            real_replace(a, b, **kwargs)  # type: ignore[arg-type]

        slept: list[float] = []
        with mock.patch.object(tc.os, "replace", flaky_replace), \
                mock.patch.object(tc.time, "sleep", slept.append):
            self.assertTrue(tc.safe_replace(src, dst, max_retries=5, initial_delay=0.01))
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(slept, [0.01, 0.015], "the backoff grows between attempts")
        self.assertEqual(dst.read_bytes(), REMUXED_BYTES)
        self.assertFalse(src.exists(), "the staging file became the movie")

    def test_a_replace_that_is_refused_every_time_raises_rather_than_lying(self) -> None:
        src = self.root / "temp_clean_x.mkv"
        src.write_bytes(REMUXED_BYTES)
        dst = self.root / "Film (2020).mkv"
        dst.write_bytes(MOVIE_BYTES)
        with mock.patch.object(tc.os, "replace",
                               side_effect=PermissionError("held open")), \
                mock.patch.object(tc.time, "sleep", lambda seconds: None), self.assertRaises(PermissionError):
            tc.safe_replace(src, dst, max_retries=3, initial_delay=0.0)
        self.assertEqual(dst.read_bytes(), MOVIE_BYTES,
                         "the original is untouched when the swap could not happen")
        self.assertTrue(src.is_file(), "and the verified remux is still there for a human")

    def test_a_journal_write_that_fails_removes_its_own_staging_file(self) -> None:
        journal = self.root / ".track_cleaner.abc.json"
        with mock.patch.object(tc.os, "replace", side_effect=OSError("read-only")), \
                self.assertRaises(OSError):
            tc.write_transaction(journal, {"schema": tc.TRANSACTION_SCHEMA_VERSION})
        self.assertFalse(journal.exists())
        self.assertEqual([p.name for p in self.root.iterdir()], [],
                         "no half-written journal and no staging debris")

    def test_a_staging_file_that_cannot_be_removed_does_not_replace_the_real_error(self) -> None:
        journal = self.root / ".track_cleaner.abc.json"
        with mock.patch.object(tc.os, "replace", side_effect=OSError("read-only")), \
                mock.patch.object(Path, "unlink", side_effect=OSError("share went away")), \
                self.assertRaises(OSError) as caught:
            tc.write_transaction(journal, {"schema": tc.TRANSACTION_SCHEMA_VERSION})
        self.assertIn("read-only", str(caught.exception))


class InterruptHandlingTests(QuietTestCase):
    """The second Ctrl-C, which is the last code that runs before ``os._exit``."""

    def setUp(self) -> None:
        super().setUp()
        self._interrupt = tc._interrupt_requested
        self._temp = tc._active_temp_file
        self._console = tc._console
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._interrupt_requested = self._interrupt
        tc._active_temp_file = self._temp
        tc._console = self._console

    def test_a_second_ctrl_c_deletes_the_staging_file_and_exits(self) -> None:
        """The operator has now asked twice; the run stops where it stands.

        ``os._exit`` is deliberate - no handler, no flush, no finally - so the
        only cleanup that will ever happen is the two lines before it: the
        in-flight staging file and the log handle. Skipping either leaves a
        movie-sized temp file on the media volume or an unclosed log.
        """
        temp = self.root / f"{tc.TEMP_PREFIX}{'0' * 32}__Film (2020).mkv"
        temp.write_bytes(REMUXED_BYTES)
        tc._active_temp_file = temp
        tc._interrupt_requested = True
        exited: list[int] = []
        close_log = mock.Mock()
        with mock.patch.object(tc, "close_log_fp", close_log), \
                mock.patch.object(tc.os, "_exit", exited.append):
            tc.request_interrupt()
        self.assertEqual(exited, [1])
        close_log.assert_called_once_with()
        self.assertFalse(temp.exists(), "the staging file is gone")

    def test_a_cleanup_that_fails_on_the_way_out_does_not_stop_the_exit(self) -> None:
        temp = self.root / f"{tc.TEMP_PREFIX}{'0' * 32}__Film (2020).mkv"
        temp.write_bytes(REMUXED_BYTES)
        tc._active_temp_file = temp
        tc._interrupt_requested = True
        exited: list[int] = []

        def exploding() -> None:
            raise RuntimeError("the log handle is already gone")

        with mock.patch.object(tc, "safe_delete", mock.Mock(side_effect=RuntimeError("no"))), \
                mock.patch.object(tc, "close_log_fp", exploding), \
                mock.patch.object(tc.os, "_exit", exited.append):
            tc.request_interrupt()
        self.assertEqual(exited, [1], "the exit happens whatever the cleanup did")

    def test_the_first_ctrl_c_tells_the_console_to_commit_its_line(self) -> None:
        console = mock.Mock()
        tc._console = console
        tc._interrupt_requested = False
        with mock.patch.object(tc, "_kill_active_child", lambda: None), \
                mock.patch.object(tc.os, "_exit", mock.Mock()) as die:
            tc.request_interrupt()
        die.assert_not_called()
        console.finish_progress.assert_called_once_with()
        self.assertTrue(tc._interrupt_requested)

    def test_a_console_that_cannot_finish_its_line_does_not_stop_the_interrupt(self) -> None:
        console = mock.Mock()
        console.finish_progress.side_effect = ValueError("I/O on a closed stream")
        tc._console = console
        tc._interrupt_requested = False
        with mock.patch.object(tc, "_kill_active_child", lambda: None), \
                mock.patch.object(tc.os, "_exit", mock.Mock()) as die:
            tc.request_interrupt()
        die.assert_not_called()
        self.assertTrue(tc._interrupt_requested)


class FakePopen:
    """The child-process surface ``_run_mkvmerge`` uses, and nothing more."""

    def __init__(self, *, stdout: object = None, returncode: int = 0,
                 poll: object = None, kill_error: Exception | None = None,
                 wait_error: Exception | None = None) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self._poll = poll
        self.kill_error = kill_error
        self.wait_error = wait_error
        self.killed = False
        self.waits = 0
        self.communicated = False

    def communicate(self) -> tuple[str, str]:
        self.communicated = True
        return ("", "")

    def poll(self) -> object:
        return self._poll() if callable(self._poll) else self._poll

    def kill(self) -> None:
        if self.kill_error is not None:
            raise self.kill_error
        self.killed = True

    def wait(self) -> int:
        self.waits += 1
        if self.wait_error is not None:
            raise self.wait_error
        return self.returncode


class FakeStdout:
    """A child's stdout, served in chunks, that can fail part-way through."""

    def __init__(self, chunks: list[str], *, fail_after: int | None = None) -> None:
        self.chunks = list(chunks)
        self.fail_after = fail_after
        self.reads = 0
        self.closed = False

    def read(self, size: int) -> str:
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise ValueError("the pipe closed while we were reading it")
        return self.chunks.pop(0) if self.chunks else ""

    def close(self) -> None:
        self.closed = True


class RemuxChildProcessTests(QuietTestCase):
    """``_run_mkvmerge``: the hours-long child, and every way it can go wrong."""

    def setUp(self) -> None:
        super().setUp()
        self._proc = tc._active_proc
        self._interrupt = tc._interrupt_requested
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._active_proc = self._proc
        tc._interrupt_requested = self._interrupt

    def test_a_live_run_with_no_readable_stdout_still_reports_the_exit_code(self) -> None:
        """A child that produced no pipe is not a crash; its status is the answer."""
        proc = FakePopen(stdout=None, returncode=2)
        with mock.patch.object(tc.subprocess, "Popen", lambda **kwargs: proc):
            rc, out, err = tc._run_mkvmerge(["mkvmerge", "-o", "x", "y"], on_progress=lambda p: None)
        self.assertEqual((rc, out, err), (2, "", ""))
        self.assertEqual(proc.waits, 1)

    def test_progress_is_parsed_out_of_a_chunked_stream(self) -> None:
        """mkvmerge writes progress in fragments; the reader reassembles lines."""
        seen: list[int] = []
        stdout = FakeStdout(["#GUI#prog", "ress 25%\n#GUI#progress 75%\r\n"])
        proc = FakePopen(stdout=stdout, returncode=0)
        with mock.patch.object(tc.subprocess, "Popen", lambda **kwargs: proc):
            rc, out, _err = tc._run_mkvmerge(["mkvmerge", "--gui-mode"],
                                             on_progress=seen.append)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [25, 75])
        self.assertIn("#GUI#progress 25%", out)
        self.assertTrue(stdout.closed, "the pipe is closed even on the happy path")

    def test_a_progress_callback_that_raises_does_not_interrupt_the_remux(self) -> None:
        """A display callback is not part of the transaction.

        Six hours into a remux, a console that has gone away must cost the
        progress line and nothing else.
        """
        stdout = FakeStdout(["#GUI#progress 50%\n"])
        proc = FakePopen(stdout=stdout, returncode=0)

        def exploding(_percent: int) -> None:
            raise ValueError("I/O operation on closed file")

        with mock.patch.object(tc.subprocess, "Popen", lambda **kwargs: proc):
            rc, out, _ = tc._run_mkvmerge(["mkvmerge", "--gui-mode"], on_progress=exploding)
        self.assertEqual(rc, 0)
        self.assertIn("50%", out)

    def test_an_interrupt_during_a_plain_run_is_raised_after_the_child_is_reaped(self) -> None:
        """The flag is checked after ``communicate``: a Ctrl-C must not be lost.

        Without this the run would carry on to the next movie as though nothing
        had happened, because the signal arrived between files rather than
        during one.
        """
        tc._interrupt_requested = True
        proc = FakePopen(returncode=0)
        with mock.patch.object(tc.subprocess, "Popen", lambda **kwargs: proc), \
                self.assertRaises(KeyboardInterrupt):
            tc._run_mkvmerge(["mkvmerge", "-J", "movie.mkv"])

    def test_an_interrupt_during_a_live_run_is_raised_too(self) -> None:
        tc._interrupt_requested = True
        proc = FakePopen(stdout=FakeStdout(["#GUI#progress 10%\n"]), returncode=0)
        with mock.patch.object(tc.subprocess, "Popen", lambda **kwargs: proc), \
                self.assertRaises(KeyboardInterrupt):
            tc._run_mkvmerge(["mkvmerge", "--gui-mode"], on_progress=lambda p: None)
        self.assertTrue(proc.killed, "the multiplexer is not left running behind us")

    def test_a_pipe_that_fails_mid_remux_kills_the_child_and_propagates_the_failure(self) -> None:
        stdout = FakeStdout(["#GUI#progress 10%\n"], fail_after=1)
        proc = FakePopen(stdout=stdout, poll=None, returncode=1)
        with mock.patch.object(tc.subprocess, "Popen", lambda **kwargs: proc), \
                self.assertRaises(ValueError):
            tc._run_mkvmerge(["mkvmerge", "--gui-mode"], on_progress=lambda p: None)
        self.assertTrue(proc.killed)
        self.assertTrue(stdout.closed)
        self.assertEqual(proc.waits, 1, "the child is reaped, not left as a zombie")

    def test_a_child_that_cannot_be_killed_does_not_replace_the_real_error(self) -> None:
        """An exception is already propagating; a second one would hide it."""
        stdout = FakeStdout(["#GUI#progress 10%\n"], fail_after=1)
        proc = FakePopen(stdout=stdout, poll=None,
                         kill_error=OSError("process already gone"),
                         wait_error=OSError("no child processes"))
        with mock.patch.object(tc.subprocess, "Popen", lambda **kwargs: proc), \
                self.assertRaises(ValueError) as caught:
            tc._run_mkvmerge(["mkvmerge", "--gui-mode"], on_progress=lambda p: None)
        self.assertIn("pipe closed", str(caught.exception))

    def test_a_finished_child_is_not_killed_on_the_way_out(self) -> None:
        stdout = FakeStdout(["#GUI#progress 10%\n"], fail_after=1)
        proc = FakePopen(stdout=stdout, poll=0, returncode=0)
        with mock.patch.object(tc.subprocess, "Popen", lambda **kwargs: proc), \
                self.assertRaises(ValueError):
            tc._run_mkvmerge(["mkvmerge", "--gui-mode"], on_progress=lambda p: None)
        self.assertFalse(proc.killed, "poll() answered: the child already exited")

    def test_the_active_child_is_cleared_when_the_call_ends(self) -> None:
        """``_kill_active_child`` reads this from the signal handler."""
        proc = FakePopen(returncode=0)
        with mock.patch.object(tc.subprocess, "Popen", lambda **kwargs: proc):
            tc._run_mkvmerge(["mkvmerge", "-J", "movie.mkv"])
            self.assertIsNone(tc._active_proc)

    def test_killing_a_child_that_is_already_gone_is_not_fatal(self) -> None:
        tc._active_proc = FakePopen(poll=lambda: None, kill_error=OSError("gone"))
        tc._kill_active_child()  # must not raise

    def test_killing_nothing_is_not_fatal(self) -> None:
        tc._active_proc = None
        tc._kill_active_child()


if __name__ == "__main__":
    unittest.main()
