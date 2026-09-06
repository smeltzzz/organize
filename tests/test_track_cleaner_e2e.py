"""End-to-end runs of ``mkv_track_cleaner.main()`` against a fake mkvmerge.

Everything here is real except the multiplexer: real argument parsing, real
directory scan, real coordination and single-instance locks, real subprocess
launch and progress parsing, real transaction journal, real verification, real
``os.replace``, real report and log files. ``tests/fake_mkvmerge.py`` stands in
for MKVToolNix and is invoked as an actual child process.

That combination is what these tests are for: the in-process suites prove the
decisions, and this one proves the plumbing around them - that a run started
from the command line ends with the right files on disk, the right exit code,
and nothing left behind.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fake_mkvmerge as fake

import mkv_track_cleaner as tc

# The fake is launched through a shebang wrapper, so these tests are POSIX-only.
# The in-process suites cover the same decisions on every platform.
WINDOWS = os.name == "nt"

GOOD_SRT = (
    "1\n00:00:01,000 --> 00:00:04,000\nHello.\n\n"
    "2\n00:00:05,000 --> 00:00:08,000\nGoodbye.\n\n"
)


def dirty_movie_spec() -> dict:
    """A typical post-download MKV: one keeper plus ballast."""
    return fake.make_spec([
        fake.video_track(),
        fake.audio_track(default=True),
        fake.audio_track(name="Director Commentary", codec="AC-3",
                         codec_id="A_AC3", channels=2, commentary=True),
        fake.audio_track(language="fra", name="French", codec="DTS",
                         codec_id="A_DTS", channels=6),
        fake.subtitle_track(),
        fake.subtitle_track(language="fra", name="French"),
    ])


@unittest.skipIf(WINDOWS, "the fake mkvmerge is launched through a POSIX shebang")
class CleanerRunFixture(unittest.TestCase):
    """One dirty movie, one fake multiplexer, one command line - and no tests.

    The fixture is its own class so that the suites below do not inherit each
    other: a subclass of a suite silently re-runs every test in it, which buys
    nothing and costs seconds on every developer's machine.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="tc_e2e_")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.library = self.tmp / "Movies"
        self.folder = self.library / "Film (2000)"
        self.folder.mkdir(parents=True)
        self.movie = self.folder / "Film (2000).mkv"
        fake.write_movie(self.movie, dirty_movie_spec())
        self.log = self.tmp / "out" / "cleaner.log"
        self.report = self.tmp / "out" / "cleaner_report.txt"
        self.cache = self.tmp / "out" / "cache.json"
        self.state_db = self.tmp / "out" / "state.db"
        self.mkvmerge = self._install_fake_mkvmerge()

        # main() installs signal handlers and leaves a console behind; put the
        # process back exactly as it was so the rest of the suite is unaffected.
        self._console = tc._console
        self._handlers = {sig: signal.getsignal(sig)
                          for sig in (signal.SIGINT, signal.SIGTERM)
                          if hasattr(signal, sig.name)}
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._console = self._console
        tc._target_root = None
        tc._interrupt_requested = False
        tc._active_temp_file = None
        for sig, handler in self._handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)

    def _install_fake_mkvmerge(self) -> Path:
        path = self.tmp / "mkvmerge"
        path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.path.insert(0, {str(Path(fake.__file__).parent)!r})\n"
            "import fake_mkvmerge\n"
            "sys.exit(fake_mkvmerge.main())\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    # -- helpers -----------------------------------------------------------

    def _run(self, *extra: str, env: dict[str, str] | None = None) -> int:
        # --state-db is not optional here: without it a real run publishes its
        # verdicts to the default location, which is the developer's own
        # ~/.local/state/organize/state.db. This suite touches nothing outside
        # its temporary directory.
        argv = ["--dir", str(self.library), "--log", str(self.log),
                "--report", str(self.report), "--cache", str(self.cache),
                "--state-db", str(self.state_db),
                "--mkvmerge", str(self.mkvmerge), "--no-color", *extra]
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(os.environ, env or {}):
            return tc.main(argv)

    def _report_text(self) -> str:
        return self.report.read_text(encoding="utf-8")

    def _tracks(self, path: Path) -> list[tuple[str, str]]:
        return [(track["type"], track["properties"].get("track_name", ""))
                for track in fake.read_spec(path)["tracks"]]

    def _leftovers(self) -> list[str]:
        return sorted(p.name for p in self.folder.iterdir()
                      if p.name.startswith((tc.TEMP_PREFIX, tc.TRANSACTION_MARKER)))


class EndToEndRunTests(CleanerRunFixture):
    """A run started from the command line: the files, the exit code, the debris."""

    def test_a_dry_run_changes_nothing(self) -> None:
        before = self.movie.read_bytes()
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertEqual(self.movie.read_bytes(), before)
        self.assertIn("DRY RUN", self._report_text().upper())
        self.assertFalse((self.library / tc.LOCK_FILENAME).exists(),
                         "a dry run takes no single-instance lock")

    def test_a_real_run_keeps_one_english_audio_track(self) -> None:
        self.assertEqual(self._run(), 0)
        tracks = self._tracks(self.movie)
        self.assertEqual([kind for kind, _ in tracks].count("audio"), 1)
        self.assertEqual([name for kind, name in tracks if kind == "audio"],
                         ["English TrueHD 7.1"], "the commentary and the French dub are gone")
        self.assertEqual([name for kind, name in tracks if kind == "video"], [""],
                         "the video track is never touched")
        self.assertEqual([name for kind, name in tracks if kind == "subtitles"],
                         ["English"], "English subs stay when there is no .eng.srt sidecar")
        self.assertEqual(self._leftovers(), [], "no staging file, no journal")
        self.assertFalse((self.library / tc.LOCK_FILENAME).exists(), "the lock is released")
        self.assertIn("Film (2000).mkv", self._report_text())
        self.assertTrue(self.log.is_file())

    def test_the_second_run_finds_nothing_to_do(self) -> None:
        self.assertEqual(self._run(), 0)
        cleaned = self.movie.read_bytes()
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.movie.read_bytes(), cleaned,
                         "an already-clean movie is not remuxed again")
        self.assertIn("ALREADY CLEAN", self._report_text().upper())

    def test_an_external_sidecar_lets_the_embedded_subtitles_go(self) -> None:
        (self.folder / "Film (2000).eng.srt").write_text(GOOD_SRT, encoding="utf-8")
        self.assertEqual(self._run(), 0)
        kinds = [kind for kind, _ in self._tracks(self.movie)]
        self.assertNotIn("subtitles", kinds,
                         "a validated sidecar makes the embedded subs redundant")

    def test_the_metadata_cache_is_written_and_reused(self) -> None:
        self.assertEqual(self._run(), 0)
        self.assertTrue(self.cache.is_file())
        entries = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertTrue(entries, "the probe result is remembered")
        self.assertEqual(self._run(), 0)
        self.assertIn("reused", self.log.read_text(encoding="utf-8"))

    def test_no_cache_writes_no_cache_file(self) -> None:
        self.assertEqual(self._run("--no-cache"), 0)
        self.assertFalse(self.cache.exists())

    def test_limit_stops_after_the_first_movie(self) -> None:
        second = self.library / "Other (2001)"
        second.mkdir()
        fake.write_movie(second / "Other (2001).mkv", dirty_movie_spec())
        self.assertEqual(self._run("--limit", "1"), 0)
        self.assertIn("1   Movies scanned", self._report_text())
        self.assertIn("1   Cleaned / remuxed", self._report_text())

    def test_only_selects_a_single_movie(self) -> None:
        second = self.library / "Other (2001)"
        second.mkdir()
        untouched = second / "Other (2001).mkv"
        fake.write_movie(untouched, dirty_movie_spec())
        before = untouched.read_bytes()
        self.assertEqual(self._run("--only", str(self.movie)), 0)
        self.assertEqual(untouched.read_bytes(), before)
        self.assertEqual(len([k for k, _ in self._tracks(self.movie) if k == "audio"]), 1)

    def test_only_outside_the_library_is_refused(self) -> None:
        outside = self.tmp / "Elsewhere (2002).mkv"
        fake.write_movie(outside, dirty_movie_spec())
        self.assertEqual(self._run("--only", str(outside)), 1)
        self.assertIn("must be inside --dir", self.log.read_text(encoding="utf-8"))

    def test_min_size_skips_small_files(self) -> None:
        self.assertEqual(self._run("--min-size", "1024"), 0)
        self.assertIn("No MKV files found", self.log.read_text(encoding="utf-8"))

    # -- the ways a run can go wrong ---------------------------------------

    def test_a_failing_mkvmerge_is_an_error_not_a_loss(self) -> None:
        before = self.movie.read_bytes()
        self.assertEqual(self._run(env={"FAKE_MKVMERGE_RC": "2"}), 1)
        self.assertEqual(self.movie.read_bytes(), before)
        self.assertEqual(self._leftovers(), [], "the failed attempt is swept up")
        self.assertIn("ERRORS", self._report_text().upper())

    def test_a_truncated_remux_is_rejected(self) -> None:
        before = self.movie.read_bytes()
        self.assertEqual(self._run(env={"FAKE_MKVMERGE_TRUNCATE": "1"}), 1)
        self.assertEqual(self.movie.read_bytes(), before,
                         "verification refuses to promote a short file")
        self.assertEqual(self._leftovers(), [])

    def test_a_missing_mkvmerge_stops_the_run(self) -> None:
        argv = ["--dir", str(self.library), "--log", str(self.log),
                "--report", str(self.report), "--mkvmerge", str(self.tmp / "nope")]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tc.main(argv), 1)

    def test_a_missing_library_stops_the_run(self) -> None:
        argv = ["--dir", str(self.tmp / "gone"), "--log", str(self.log),
                "--report", str(self.report), "--mkvmerge", str(self.mkvmerge)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tc.main(argv), 1)

    def test_a_report_inside_the_library_is_refused(self) -> None:
        argv = ["--dir", str(self.library), "--log", str(self.log),
                "--report", str(self.library / "report.txt"),
                "--mkvmerge", str(self.mkvmerge)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tc.main(argv), 2)

    def test_a_live_lock_holder_keeps_the_second_instance_out(self) -> None:
        lock = self.library / tc.LOCK_FILENAME
        lock.write_text(f"{tc._this_hostname()}\n{os.getpid()}\n0\n", encoding="utf-8")
        self.assertEqual(self._run(), 1)
        self.assertTrue(lock.exists(), "the other instance's lock is left alone")

    def test_a_stale_lock_is_reclaimed(self) -> None:
        lock = self.library / tc.LOCK_FILENAME
        dead = self._dead_pid()
        lock.write_text(f"{tc._this_hostname()}\n{dead}\n0\n", encoding="utf-8")
        self.assertEqual(self._run(), 0)
        self.assertFalse(lock.exists())
        self.assertIn("stale lock", self.log.read_text(encoding="utf-8").lower())

    def _dead_pid(self) -> int:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the child never returns
            os._exit(0)
        os.waitpid(pid, 0)
        return pid

    def test_an_interrupt_reports_and_exits_130(self) -> None:
        real_process = tc.process_mkv

        def interrupting(*args, **kwargs):
            tc.request_interrupt()
            return real_process(*args, **kwargs)

        with mock.patch.object(tc, "process_mkv", interrupting):
            self.assertEqual(self._run(), 130)
        self.assertIn("INTERRUPT", self._report_text().upper())

    def test_a_hardlinked_movie_is_deferred_not_remuxed(self) -> None:
        seed = self.tmp / "seed.mkv"
        os.link(self.movie, seed)
        before = self.movie.read_bytes()
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.movie.read_bytes(), before,
                         "remuxing would break the seeding hardlink")
        self.assertEqual(seed.read_bytes(), before)
        self.assertIn("HARDLINK", self._report_text().upper())

    def test_the_self_test_flag_runs_the_bundled_checks(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tc.main(["--self-test"]), 0)


class WhatTheRunTellsStatusTests(CleanerRunFixture):
    """The verdicts a real run leaves behind for ``organize status``.

    Until this existed the command printed ``Remux  not recorded yet`` forever:
    a library could be completely remuxed and the summary would never say so,
    because the one tool that knew never wrote it down.
    """

    def verdicts(self) -> dict[str, tuple[str, str]]:
        from organizekit.core import KIND_REMUX, open_state

        store = open_state(self.state_db, tool="tests")
        self.addCleanup(store.close)
        return {
            Path(key).name: (row.verdict, row.detail)
            for (key, _), row in store.verdicts(KIND_REMUX).items()
        }

    def test_a_cleaned_movie_is_recorded_as_cleaned(self) -> None:
        self.assertEqual(self._run(), 0)
        verdict, detail = self.verdicts()[self.movie.name]
        self.assertEqual(verdict, tc.STATUS_CLEANED)
        self.assertIn("kept", detail)

    def test_the_verdict_describes_the_remuxed_bytes_not_the_old_ones(self) -> None:
        """Written after the swap: `organize status` must not call it stale."""
        from organizekit.core import KIND_REMUX, open_state, path_norm

        self.assertEqual(self._run(), 0)
        store = open_state(self.state_db, tool="tests")
        self.addCleanup(store.close)
        row = store.verdicts(KIND_REMUX)[(path_norm(self.movie), KIND_REMUX)]
        info = self.movie.stat()
        self.assertTrue(row.is_current_for(info.st_size, info.st_mtime_ns))

    def test_the_second_run_records_that_there_was_nothing_to_do(self) -> None:
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.verdicts()[self.movie.name][0], tc.STATUS_ALREADY_CLEAN)

    def test_a_seeding_movie_is_recorded_as_deferred_not_done(self) -> None:
        os.link(self.movie, self.tmp / "seed.mkv")
        self.assertEqual(self._run(), 0)
        verdict, detail = self.verdicts()[self.movie.name]
        self.assertEqual(verdict, tc.STATUS_DEFERRED)
        self.assertNotIn(verdict, tc.SETTLED_REMUX, "it still has to be remuxed later")
        self.assertIn("hardlink", detail)

    def test_a_failed_remux_is_recorded_as_failed(self) -> None:
        self.assertEqual(self._run(env={"FAKE_MKVMERGE_RC": "2"}), 1)
        self.assertEqual(self.verdicts()[self.movie.name][0], tc.STATUS_FAILED)

    def test_a_dry_run_publishes_nothing(self) -> None:
        """It measured nothing about the bytes on disk, so it claims nothing."""
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertFalse(self.state_db.exists())

    def test_no_state_publishes_nothing(self) -> None:
        self.assertEqual(self._run("--no-state"), 0)
        self.assertFalse(self.state_db.exists())
        self.assertNotEqual(self.movie.read_bytes(), b"", "the run itself still happened")

    def test_a_state_db_inside_the_library_is_refused(self) -> None:
        self.state_db = self.library / "state.db"
        self.assertEqual(self._run(), 2)

    def test_an_interrupted_run_keeps_the_verdicts_it_already_wrote(self) -> None:
        """Per movie, not per run: hours of work must not vanish at Ctrl-C."""
        second = self.library / "Other (2001)"
        second.mkdir()
        fake.write_movie(second / "Other (2001).mkv", dirty_movie_spec())
        real_process = tc.process_mkv
        seen: list[str] = []

        def interrupting(*args, **kwargs):
            seen.append(kwargs["mkv_path"].name)
            if len(seen) == 2:
                tc.request_interrupt()
            return real_process(*args, **kwargs)

        with mock.patch.object(tc, "process_mkv", interrupting):
            self.assertEqual(self._run(), 130)
        recorded = self.verdicts()
        self.assertEqual(len(recorded), 1, "only the movie that finished")
        self.assertEqual(next(iter(recorded.values()))[0], tc.STATUS_CLEANED)


@unittest.skipIf(WINDOWS, "the fake mkvmerge is launched through a POSIX shebang")
class MkvmergeSubprocessTests(unittest.TestCase):
    """``_run_mkvmerge`` itself: progress parsing, exit codes, child tracking."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="tc_proc_")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.movie = self.tmp / "Film (2000).mkv"
        fake.write_movie(self.movie, dirty_movie_spec())
        self.binary = EndToEndRunTests._install_fake_mkvmerge(self)  # type: ignore[arg-type]
        self.addCleanup(setattr, tc, "_interrupt_requested", False)

    def test_version_is_read_from_the_binary(self) -> None:
        self.assertEqual(tc.get_mkvmerge_version(str(self.binary)), fake.VERSION_BANNER)

    def test_identification_json_round_trips(self) -> None:
        rc, out, _err = tc._run_mkvmerge([str(self.binary), "-J", str(self.movie)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)["tracks"]), 6)

    def test_progress_is_parsed_from_the_live_stream(self) -> None:
        seen: list[int] = []
        rc, out, err = tc._run_mkvmerge(
            [str(self.binary), "-o", str(self.tmp / "out.mkv"),
             "--audio-tracks", "1", str(self.movie)],
            on_progress=seen.append,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [0, 25, 50, 75, 100])
        self.assertEqual(out, err, "live mode folds stderr into stdout")
        self.assertIsNone(tc._active_proc, "the finished child is forgotten")

    def test_a_progress_callback_that_raises_does_not_break_the_remux(self) -> None:
        def explode(_pct: int) -> None:
            raise RuntimeError("the console blew up")

        rc, _out, _err = tc._run_mkvmerge(
            [str(self.binary), "-o", str(self.tmp / "out.mkv"),
             "--audio-tracks", "1", str(self.movie)],
            on_progress=explode,
        )
        self.assertEqual(rc, 0, "a broken display must not fail a good remux")

    def test_a_nonzero_exit_is_reported_verbatim(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_MKVMERGE_RC": "2"}):
            rc, out, _err = tc._run_mkvmerge(
                [str(self.binary), "-o", str(self.tmp / "out.mkv"),
                 "--audio-tracks", "1", str(self.movie)],
                on_progress=lambda pct: None,
            )
        self.assertEqual(rc, 2)
        summary = tc._summarize_mkvmerge_failure(out, rc)
        self.assertIn("demuxer", summary)
        self.assertNotIn("#GUI#", summary, "progress noise is stripped from the reason")

    def test_an_interrupt_requested_mid_run_is_raised_after_the_child_exits(self) -> None:
        tc.request_interrupt()
        with self.assertRaises(KeyboardInterrupt):
            tc._run_mkvmerge([str(self.binary), "-J", str(self.movie)])
        self.assertIsNone(tc._active_proc)


if __name__ == "__main__":
    unittest.main()
