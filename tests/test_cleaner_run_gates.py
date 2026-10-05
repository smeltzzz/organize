"""The cleaner's command-line gates: what stops a run before it touches a movie.

``main()`` is where the tool meets an operator's arguments, another tool's lock,
the machine's console and the operator's Ctrl-C. The interesting branches are
the ones that refuse to start or that stop cleanly:

* a negative lock timeout is a usage error, not a wait;
* the library-wide coordination lock with ``movie_standardizer.py`` is taken
  *before* the scan, and a lock that cannot be taken ends the run with an exit
  code a wrapper can act on;
* ``--only`` is validated before anything is read - a path outside the library,
  or one that is not a regular MKV, is refused rather than silently ignored;
* a directory the walk cannot read is logged and the scan continues;
* one movie raising an unexpected exception becomes one error row and a non-zero
  exit, not a dead queue;
* Ctrl-C between movies ends the run at 130 with a report and no staging file
  left behind.

These run the real ``main()`` in process, against a faked multiplexer, so the
argument parsing, the lock protocol, the report and the exit codes are all the
shipped ones.
"""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import fake_mkvmerge as fake

import mkv_track_cleaner as tc
from organizekit.core.locking import LockTimeoutError

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

MOVIE_BYTES = b"ORIGINAL-MOVIE-BYTES" * 400
REMUXED_BYTES = b"REMUXED-MOVIE-BYTES-" * 300

DIRTY = fake.make_spec([
    fake.video_track(),
    fake.audio_track(default=True),
    fake.audio_track(name="Director Commentary", codec="AC-3", codec_id="A_AC3",
                     channels=2, commentary=True),
])
CLEAN = fake.make_spec([fake.video_track(), fake.audio_track(default=True)])


class ReconfigurableTTY(io.StringIO):
    """A terminal that also has the ``reconfigure`` a real stream has."""

    encoding = "utf-8"

    def __init__(self, *, reconfigure_error: Exception | None = None) -> None:
        super().__init__()
        self.reconfigure_error = reconfigure_error
        self.reconfigured: list[dict] = []

    def isatty(self) -> bool:
        return True

    def reconfigure(self, **kwargs: object) -> None:
        self.reconfigured.append(dict(kwargs))
        if self.reconfigure_error is not None:
            raise self.reconfigure_error


class CleanerRunHarness(unittest.TestCase):
    """One library, one faked multiplexer, and the real ``main()``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cleaner_run_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.library = self.root / "library"
        self.folder = self.library / "Film (2020)"
        self.folder.mkdir(parents=True)
        self.movie = self.folder / "Film (2020).mkv"
        self.movie.write_bytes(MOVIE_BYTES)
        self.log = self.root / "out" / "cleaner.log"
        self.report = self.root / "out" / "cleaner_report.txt"
        self.state_db = self.root / "out" / "state.db"
        self.info = DIRTY
        self.output = CLEAN
        self.remuxes = 0

        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
                    if hasattr(signal, sig.name)}
        self.addCleanup(self._restore, handlers)
        for name, value in (("_run_mkvmerge", self._fake_mkvmerge),
                            ("resolve_mkvmerge_path", lambda custom=None: "mkvmerge"),
                            ("get_mkvmerge_version", lambda binary: fake.VERSION_BANNER)):
            patch = mock.patch.object(tc, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def _restore(self, handlers: dict) -> None:
        tc._console = None
        tc._target_root = None
        tc._interrupt_requested = False
        tc._active_temp_file = None
        for sig, handler in handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    def _fake_mkvmerge(self, cmd: list[str], on_progress=None) -> tuple[int, str, str]:
        if "-J" in cmd:
            target = Path(cmd[cmd.index("-J") + 1])
            payload = self.output if target.name.startswith(tc.TEMP_PREFIX) else self.info
            return 0, json.dumps(payload), ""
        self.remuxes += 1
        Path(cmd[cmd.index("-o") + 1]).write_bytes(REMUXED_BYTES)
        return 0, "", ""

    def run_main(self, *extra: str, stream: object | None = None,
                 env: dict[str, str] | None = None, color: bool = False) -> tuple[int, str]:
        out = stream if stream is not None else ReconfigurableTTY()
        argv = ["--dir", str(self.library), "--log", str(self.log),
                "--report", str(self.report), "--state-db", str(self.state_db),
                *([] if color else ["--no-color"]), *extra]
        with redirect_stdout(out), mock.patch.dict(os.environ, env or {}):
            code = tc.main(argv)
        return code, out.getvalue() if hasattr(out, "getvalue") else ""

    def report_text(self) -> str:
        return self.report.read_text(encoding="utf-8") if self.report.exists() else ""

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.folder.iterdir()
                      if p.name.startswith((tc.TEMP_PREFIX, tc.TRANSACTION_MARKER)))



class SweepInterruptTests(CleanerRunHarness):
    """Ctrl-C during a sweep: stop, kill the child, and still publish the decisions.

    A remux pass runs for hours, so an interrupt is a normal event, not a crash.
    What has to survive it is the record: every movie already decided is in the
    report, the mkvmerge child is dead rather than orphaned writing into the
    library, and the exit code tells a scheduler the run was cut short.
    """

    def second_movie(self) -> Path:
        folder = self.library / "Second (2021)"
        folder.mkdir()
        movie = folder / "Second (2021).mkv"
        movie.write_bytes(MOVIE_BYTES)
        return movie

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_an_interrupt_during_a_movie_lets_it_finish_and_stops_before_the_next(self) -> None:
        """The handler sets a flag; it does not raise into a running remux.

        Killing mkvmerge mid-write is how a library ends up with a truncated movie,
        so the sweep finishes the movie it is holding and then stops at the top of
        the next iteration: one decision published, nothing half-written, exit 130.
        """
        self.second_movie()
        real_process = tc.process_mkv
        calls: list[str] = []

        def process_then_stop(**kwargs: object) -> None:
            calls.append(str(kwargs["mkv_path"]))
            real_process(**kwargs)  # type: ignore[arg-type]
            tc._interrupt_requested = True  # what the SIGINT handler does

        with mock.patch.object(tc, "process_mkv", process_then_stop):
            code, _out = self.run_main()

        self.assertEqual(code, 130)
        self.assertEqual(len(calls), 1, "the queue stopped before the second movie")
        self.assertEqual(self.leftovers(), [], "and the first one left nothing behind")
        self.assertTrue(self.report_text(), "what it decided is still published")

    def test_an_interrupt_between_movies_still_kills_the_child_and_reports(self) -> None:
        """The interrupt lands while a verdict is being published, not inside a remux."""
        self.second_movie()
        with mock.patch.object(tc, "publish_remux_verdict", side_effect=KeyboardInterrupt), \
                mock.patch.object(tc, "_kill_active_child") as killed:
            code, _out = self.run_main()

        self.assertEqual(code, 130)
        killed.assert_called_once_with()
        self.assertTrue(tc._interrupt_requested)
        report = self.report_text()
        self.assertTrue(report, "an interrupted sweep still publishes what it decided")
        self.assertIn("interrupt", report.lower())
        self.assertIn("Execution interrupted by user", self.log_text())



class ArgumentGateTests(CleanerRunHarness):
    def test_a_negative_coordination_timeout_is_a_usage_error(self) -> None:
        """A negative wait is not a wait; argparse answers before any lock is taken."""
        out = ReconfigurableTTY()
        with redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            tc.main(["--dir", str(self.library), "--standardizer-lock-timeout", "-1",
                     "--no-color"])
        self.assertEqual(caught.exception.code, 2)

    def test_the_run_pins_its_own_output_to_utf8(self) -> None:
        """The scar: a cp1252 console raised on the report's box drawing."""
        out = ReconfigurableTTY()
        code, _ = self.run_main("--dry-run", stream=out)
        self.assertEqual(code, 0)
        self.assertTrue(out.reconfigured)
        self.assertEqual(out.reconfigured[0], {"encoding": "utf-8", "errors": "replace"})

    def test_a_console_that_cannot_be_reconfigured_still_runs(self) -> None:
        out = ReconfigurableTTY(reconfigure_error=ValueError("I/O operation on closed file"))
        code, _ = self.run_main("--dry-run", stream=out)
        self.assertEqual(code, 0, "a detached console costs the pinning, not the run")

    def test_a_run_with_no_state_database_still_checks_its_output_paths(self) -> None:
        """``--state-db`` is optional, and the path checks skip what was not given."""
        out = ReconfigurableTTY()
        with redirect_stdout(out):
            code = tc.main(["--dir", str(self.library), "--log", str(self.log),
                            "--report", str(self.report), "--no-state", "--no-color", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(self.state_db.exists(), "no cache file is created for a run that asked for none")

    def test_a_report_inside_the_library_is_refused(self) -> None:
        """A report in the library would be indexed as a movie folder by the auditor."""
        out = ReconfigurableTTY()
        with redirect_stdout(out):
            code = tc.main(["--dir", str(self.library),
                            "--report", str(self.library / "report.txt"),
                            "--no-state", "--no-color"])
        self.assertEqual(code, 2)
        self.assertFalse((self.library / "report.txt").exists())

    def test_a_run_that_cannot_find_mkvmerge_exits_1_and_says_why(self) -> None:
        with mock.patch.object(tc, "resolve_mkvmerge_path",
                               mock.Mock(side_effect=FileNotFoundError("Install MKVToolNix"))):
            code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.remuxes, 0)
        self.assertIn("Install MKVToolNix", self.log.read_text(encoding="utf-8"))

    def test_a_library_that_does_not_exist_exits_1(self) -> None:
        code, _ = self.run_main("--dir", str(self.root / "nope"))
        self.assertEqual(code, 1)
        self.assertEqual(self.remuxes, 0)


class CoordinationTests(CleanerRunHarness):
    def test_a_run_starts_only_after_the_standardizers_lock_is_taken(self) -> None:
        """The cleaner and the ingest hook must never be inside one library together.

        The hook hardlinks a new movie into place; the cleaner replaces movies
        in situ. Overlapping them is how a placement lands on a file that is
        mid-remux, so the lock is taken before the scan, not before the write.
        """
        code, _ = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("coordination lock acquired", self.log.read_text(encoding="utf-8"))

    def test_a_lock_that_cannot_be_taken_ends_the_run_before_any_scanning(self) -> None:
        def refuse(self: object) -> None:
            raise LockTimeoutError("Timed out after 60.0s waiting for library coordination lock")

        with mock.patch.object(tc.CoordinationLock, "acquire", refuse):
            code, _ = self.run_main("--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(self.remuxes, 0, "nothing was scanned, let alone remuxed")
        logged = self.log.read_text(encoding="utf-8")
        self.assertIn("Could not acquire movie_standardizer.py coordination lock", logged)
        self.assertIn("Timed out after 60.0s", logged)
        self.assertFalse(self.report.exists(),
                         "the run never started, so it writes no report of its own")

    def test_an_operating_system_that_refuses_the_lock_also_ends_the_run(self) -> None:
        def refuse(self: object) -> None:
            raise OSError("the share will not open a lock file")

        with mock.patch.object(tc.CoordinationLock, "acquire", refuse):
            code, _ = self.run_main("--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(self.remuxes, 0)


class OnlySelectionTests(CleanerRunHarness):
    def test_only_a_movie_outside_the_library_is_refused_as_a_fatal_error(self) -> None:
        """``--only`` narrows a run; it must not widen it to another directory."""
        outside = self.root / "elsewhere" / "Other (2001).mkv"
        outside.parent.mkdir()
        outside.write_bytes(MOVIE_BYTES)
        code, _ = self.run_main("--only", str(outside), "--no-state")
        self.assertEqual(code, 1)
        self.assertEqual(self.remuxes, 0)
        self.assertIn("--only movie must be inside --dir", self.report_text())
        self.assertEqual(outside.read_bytes(), MOVIE_BYTES)

    def test_only_something_that_is_not_a_movie_is_refused(self) -> None:
        sidecar = self.folder / "Film (2020).eng.srt"
        sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi.\n\n", encoding="utf-8")
        code, _ = self.run_main("--only", str(sidecar), "--no-state")
        self.assertEqual(code, 1)
        self.assertIn("--only must name an existing regular MKV or MP4", self.report_text())
        self.assertEqual(self.remuxes, 0)

    def test_only_a_movie_the_scan_did_not_find_is_refused_rather_than_ignored(self) -> None:
        """A path that is a real MKV inside the library but not a movie.

        This is the case the second check exists for: an extra, a sample, or a
        movie in a folder the scan skips. The operator asked for one specific
        file; a run that reports success having done nothing is the worst
        possible answer, so the disagreement between "you named it" and "I did
        not find it" is fatal.
        """
        extras = self.folder / "Extras"
        extras.mkdir()
        extra = extras / "making-of.mkv"
        extra.write_bytes(MOVIE_BYTES)
        code, _ = self.run_main("--only", str(extra), "--no-state")
        self.assertEqual(code, 1)
        self.assertIn("--only MKV was not discovered under --dir", self.report_text())
        self.assertEqual(extra.read_bytes(), MOVIE_BYTES, "the extra was not remuxed either")
        self.assertEqual(self.remuxes, 0)

    def test_only_a_path_that_resolves_to_nothing_is_refused(self) -> None:
        """A dangling symlink resolves to a path that is not a file."""
        link = self.folder / "Linked (2020).mkv"
        link.symlink_to(self.root / "gone.mkv")
        code, _ = self.run_main("--only", str(link), "--no-state")
        self.assertEqual(code, 1)
        self.assertIn("--only must name an existing regular MKV or MP4", self.report_text())
        self.assertEqual(self.remuxes, 0)

    def test_only_a_movie_inside_the_library_is_the_only_one_processed(self) -> None:
        second = self.library / "Other (2001)"
        second.mkdir()
        untouched = second / "Other (2001).mkv"
        untouched.write_bytes(MOVIE_BYTES)
        code, _ = self.run_main("--only", str(self.movie))
        self.assertEqual(code, 0)
        self.assertEqual(self.movie.read_bytes(), REMUXED_BYTES)
        self.assertEqual(untouched.read_bytes(), MOVIE_BYTES)


class FailureDuringRunTests(CleanerRunHarness):
    def test_a_directory_the_walk_cannot_read_is_logged_and_the_scan_continues(self) -> None:
        """A share with one locked folder still has movies in the others."""
        locked = self.library / "Locked (2002)"
        locked.mkdir()
        real_scandir = os.scandir

        def scandir(path: object) -> object:
            if str(path) == str(locked):
                raise OSError(13, "permission denied", str(locked))
            return real_scandir(path)  # type: ignore[arg-type]

        with mock.patch.object(tc.os, "scandir", scandir):
            code, _ = self.run_main("--dry-run")
        self.assertEqual(code, 0, "one unreadable folder is a warning, not a failed run")
        logged = self.log.read_text(encoding="utf-8")
        self.assertIn("Could not access directory", logged)
        self.assertIn(str(locked), logged)

    def test_one_movie_that_raises_becomes_one_error_row_and_a_non_zero_exit(self) -> None:
        """The queue survives a bad movie, and the exit code says the run was not clean."""
        def exploding(**kwargs: object) -> None:
            raise RuntimeError("something nobody anticipated")

        with mock.patch.object(tc, "process_mkv", exploding):
            code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("something nobody anticipated", self.report_text())
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES)
        self.assertEqual(self.leftovers(), [])

    def test_a_second_movie_is_still_processed_after_the_first_one_raises(self) -> None:
        second = self.library / "Other (2001)"
        second.mkdir()
        (second / "Other (2001).mkv").write_bytes(MOVIE_BYTES)
        seen: list[str] = []

        def explodes_on_the_first(**kwargs: object) -> None:
            path = Path(str(kwargs.get("mkv_path")))
            seen.append(path.name)
            if path.name == "Film (2020).mkv":
                raise RuntimeError("the first one is bad")

        with mock.patch.object(tc, "process_mkv", explodes_on_the_first):
            code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(sorted(seen), ["Film (2020).mkv", "Other (2001).mkv"],
                         "the queue did not stop at the bad movie")

    def test_a_system_exit_from_a_step_is_not_turned_into_an_error_row(self) -> None:
        def exiting(**kwargs: object) -> None:
            raise SystemExit(3)

        with mock.patch.object(tc, "process_mkv", exiting), self.assertRaises(SystemExit):
            self.run_main()

    def test_a_state_cache_that_refuses_a_note_does_not_fail_the_run(self) -> None:
        """The verdicts are already published; the note is decoration."""
        from organizekit.core.state import StateStore

        def exploding(self: object, *args: object, **kwargs: object) -> None:
            raise RuntimeError("the cache is locked")

        with mock.patch.object(StateStore, "note", exploding):
            code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("state cache note not written", self.log.read_text(encoding="utf-8"))

    def test_the_run_publishes_verdicts_even_when_a_movie_failed(self) -> None:
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.movie.read_bytes(), REMUXED_BYTES)
        self.assertIn("verdict(s) recorded for `organize status`",
                      self.log.read_text(encoding="utf-8"))


class WallOfAudioTests(CleanerRunHarness):
    """A movie with a dozen audio tracks is a real release, not an error."""

    def test_the_removed_track_list_is_truncated_in_the_log(self) -> None:
        """Twelve dropped tracks are one line with a count, not twelve lines.

        The per-movie detail is what an operator reads to check the decision; a
        wall of near-identical lines hides the one track that was kept.
        """
        tracks = [fake.video_track(), fake.audio_track(default=True)]
        tracks += [fake.audio_track(name=f"Commentary {n}", codec="AC-3", codec_id="A_AC3",
                                    channels=2) for n in range(2, 12)]
        self.info = fake.make_spec(tracks)
        code, _out = self.run_main()
        logged = self.log.read_text(encoding="utf-8")
        self.assertEqual(code, 0, logged[-800:])
        self.assertIn("Removing 10 Audio Track(s):", logged)
        self.assertIn("+2 more", logged)
        self.assertEqual(self.remuxes, 1)


class MidRemuxInterruptTests(CleanerRunHarness):
    def test_a_control_c_during_the_remux_leaves_no_debris_behind(self) -> None:
        """The staging file and its journal are removed on the way out.

        An interrupted mkvmerge leaves a partial container in the library's own
        folder; if it survived, the next sweep (or Jellyfin) would see a movie that
        is half a file. The exception is re-raised so the run exits 130 and says it
        was interrupted rather than reporting a clean pass.
        """
        real_fake = self._fake_mkvmerge

        def interrupted_remux(cmd: list[str], on_progress=None):
            if "-J" not in cmd:
                output = Path(cmd[cmd.index("-o") + 1])
                output.write_bytes(b"half a movie")  # what mkvmerge would leave
                raise KeyboardInterrupt
            return real_fake(cmd, on_progress)

        with mock.patch.object(tc, "_run_mkvmerge", interrupted_remux):
            code, _out = self.run_main()

        self.assertEqual(code, 130)
        self.assertEqual(self.leftovers(), [], "no staging file or journal survived")
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES, "the movie was not touched")
        self.assertIn("INTERRUPTED by user", self.report_text())


class SystemExitTests(CleanerRunHarness):
    def test_a_system_exit_inside_a_movie_is_not_swallowed_as_a_movie_error(self) -> None:
        """``SystemExit`` means "stop the process", not "this one movie failed".

        The per-movie handler catches ``Exception`` so one bad movie cannot end a
        sweep, and ``SystemExit`` derives from ``BaseException`` for exactly that
        reason: swallowing it would turn a shutdown request into a fake error row
        and keep remuxing for hours after the operator asked it to stop.
        """
        def abort(*_a: object, **_kw: object) -> None:
            raise SystemExit(3)

        with (mock.patch.object(tc, "verify_remux_output", abort),
              self.assertRaises(SystemExit) as ctx):
            self.run_main()

        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES, "the movie was not replaced")
        self.assertFalse(self.report.exists(), "no report claims a decision was made")
        # The staging file and its journal stay: they are the evidence the next
        # sweep's crash recovery replays, and deleting them here would hide a
        # transaction that was open when the process was told to stop.
        leftovers = self.leftovers()
        self.assertTrue(any(name.endswith(".json") for name in leftovers), leftovers)
        self.assertTrue(any(name.startswith(tc.TEMP_PREFIX) for name in leftovers), leftovers)


class InterruptTests(CleanerRunHarness):
    def test_the_signal_handler_asked_for_by_the_run_is_the_tools_own(self) -> None:
        """``main`` installs a handler; this proves what it does when it fires."""
        installed: dict[int, object] = {}
        real_signal = signal.signal

        def recording(signum: int, handler: object) -> object:
            installed[signum] = handler
            return real_signal(signum, handler)

        with mock.patch.object(tc.signal, "signal", recording):
            code, _ = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(signal.SIGINT, installed)
        handler = installed[signal.SIGINT]
        assert callable(handler)
        tc._interrupt_requested = False
        with mock.patch.object(tc, "_kill_active_child", lambda: None):
            handler(signal.SIGINT, None)
        self.assertTrue(tc._interrupt_requested,
                        "the handler sets the flag the file loop checks between movies")

    def test_ctrl_c_between_movies_ends_the_run_at_130_with_a_report(self) -> None:
        """130 is what a wrapper reads as "interrupted", and the report still exists.

        A sweep that has been running for hours must leave behind what it
        already decided, so the report is written on the way out and the
        run status says it was interrupted.
        """
        staging = self.root / f"{tc.TEMP_PREFIX}{'0' * 32}__Film (2020).mkv"

        def interrupted(**kwargs: object) -> None:
            staging.write_bytes(b"half a movie")
            tc._active_temp_file = staging
            raise KeyboardInterrupt

        with mock.patch.object(tc, "process_mkv", interrupted):
            code, _ = self.run_main()
        self.assertEqual(code, 130)
        self.assertFalse(staging.exists(), "the in-flight staging file is deleted on the way out")
        self.assertIn("INTERRUPTED by user", self.report_text())
        self.assertIn("Execution interrupted by user", self.log.read_text(encoding="utf-8"))
        self.assertEqual(self.movie.read_bytes(), MOVIE_BYTES)

    def test_an_interrupt_before_the_first_movie_stops_the_scan_immediately(self) -> None:
        tc._interrupt_requested = True
        code, _ = self.run_main("--dry-run")
        self.assertEqual(code, 130)
        self.assertEqual(self.remuxes, 0)

    def test_a_child_process_is_killed_when_the_run_is_interrupted(self) -> None:
        killed: list[bool] = []
        with mock.patch.object(tc, "_kill_active_child", lambda: killed.append(True)), \
                mock.patch.object(tc, "process_mkv", mock.Mock(side_effect=KeyboardInterrupt)):
            code, _ = self.run_main()
        self.assertEqual(code, 130)
        self.assertEqual(killed, [True], "the multiplexer is not left running behind the sweep")

    def test_a_fatal_error_still_writes_a_report_and_exits_1(self) -> None:
        """The last-resort handler: the report is the run's only durable answer."""
        def exploding(*args: object, **kwargs: object) -> object:
            raise RuntimeError("the library vanished mid-scan")

        with mock.patch.object(tc, "discover_mkv_files", exploding):
            code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("the library vanished mid-scan", self.report_text())


class BannerTests(CleanerRunHarness):
    def test_the_startup_banner_states_the_policy_the_run_is_about_to_apply(self) -> None:
        code, drawn = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        plain = drawn
        self.assertIn("DRY-RUN", plain)
        self.assertIn("mkvmerge", plain)
        self.assertIn("Process priority", plain)
        self.assertIn("Hardlinked movies", plain)

    def test_the_lossless_line_is_emphasised_on_a_console_that_takes_colour(self) -> None:
        """The one line the banner colours is the promise the tool is making."""
        out = ReconfigurableTTY()
        code, _ = self.run_main("--nice", stream=out, env={"FORCE_COLOR": "1"}, color=True)
        self.assertEqual(code, 0)
        drawn = out.getvalue()
        lossless = [line for line in drawn.splitlines() if "LOSSLESS" in line]
        self.assertTrue(lossless, drawn[:400])
        self.assertIn("\033[", lossless[0], "the lossless line is the coloured one")
        self.assertIn("nice +10", drawn, "--nice is reported in the banner")


if __name__ == "__main__":
    unittest.main()
