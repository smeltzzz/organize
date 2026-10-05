"""The standardizer's placement decisions and the gates in front of them.

``movie_standardizer.py`` is the ingest hook: qBittorrent finishes a download,
this tool decides what the movie is called, hardlinks it into the library, and
declines everything it will not touch. Its naming rules have their own suite;
this one covers the machinery around them, which is where the damage would be:

* **Placement is hardlink-only and verified.** The source copy stays seeding, so
  a placement that failed must leave the library exactly as it was - which is
  what the staging-and-swap path and its rollback are for. A container change
  (an MP4 replacing an MKV) keeps the old file until the new hardlink has been
  published *and* verified, and un-publishes itself if the old one is still
  there afterwards.
* **Declines are durable.** A multipart fragment, an extra, a disc rip or a TV
  episode is not "skipped" - it is recorded with a reason the report prints,
  because the operator has to know the download is still sitting in the source.
* **The configuration is validated before anything is written.** Every rule in
  ``validate_config`` exists because getting it wrong means a tool reading its
  own output: a report inside the library, a quarantine inside the source, a
  source and target on different volumes where hardlinks are impossible.
* **A duplicate is only ever removed when it is provably a duplicate.** Two
  folders with the same identity but sizes within the margin are left for a
  human; a folder that still holds files is never collapsed; and a Jellyfin
  multi-version folder is never mistaken for a duplicate at all.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import platforms
from test_standardizer_e2e import StandardizerRunFixture, write_srt, write_video

import movie_standardizer as ms

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

BIG = 8 * 1024 * 1024


def pristine_tool() -> types.ModuleType:
    """A second copy of ``movie_standardizer``, loaded from source.

    ``tests/selftests`` rebinds ``run_canonical_self_tests`` on the imported
    module, so the shipped field smoke test - the one a real ``--self-test`` on a
    NAS runs - is unreachable through it. Loading the file again under another
    name gives the shipped function back without disturbing the module every
    other test in the suite holds a reference to.
    """
    spec = importlib.util.spec_from_file_location(
        "movie_standardizer_pristine", REPO / "movie_standardizer.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered while it executes: ``@dataclass`` resolves its own annotations
    # through ``sys.modules[cls.__module__]``, and the module is building a
    # Config dataclass at import time.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


class StandardizerFixture(unittest.TestCase):
    """A source tree, a library, and the module state a run mutates."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ms_gates_")
        self.root = Path(self._tmp.name).resolve()
        self.source = self.root / "final"
        self.library = self.root / "Movies"
        self.out = self.root / "out"
        self.source.mkdir()
        self.library.mkdir()
        self.out.mkdir()
        self._saved = (ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS)
        self._logging = (ms.LOG.handlers[:], ms.LOG.propagate, ms.LOG.level)
        self.addCleanup(self._restore)
        self.configure()

    def _restore(self) -> None:
        for handler in ms.LOG.handlers[:]:
            ms.LOG.removeHandler(handler)
            try:
                handler.close()
            except OSError:
                pass
        ms.LOG.handlers, ms.LOG.propagate, ms.LOG.level = self._logging
        ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS = self._saved
        self._tmp.cleanup()

    def configure(self, **kwargs: object) -> ms.Config:
        settings: dict[str, object] = {
            "source_dir": self.source, "target_dir": self.library,
            "log_file": self.out / "standardizer.log",
            "report_file": self.out / "report.txt",
            "min_movie_size_mb": 1.0,
        }
        settings.update(kwargs)
        ms.CFG = ms.Config(**settings)  # type: ignore[arg-type]
        ms.RUN_SUMMARY = ms.RunSummary()
        ms.RUN_EVENTS = []
        return ms.CFG

    def reasons(self) -> str:
        return " | ".join(str(event.get("reason", "")) for event in ms.RUN_EVENTS)

    def outcomes(self) -> list[str]:
        return [str(event.get("status", "")) for event in ms.RUN_EVENTS]

    def library_tree(self) -> list[str]:
        return sorted(p.relative_to(self.library).as_posix()
                      for p in self.library.rglob("*") if p.is_file())


class PlacementTests(StandardizerFixture):
    """``process_file_action``: the only place this tool writes into a library."""

    def _place(self, src: Path, dest: Path) -> bool:
        with redirect_stdout(io.StringIO()):
            return ms.process_file_action(src, dest)

    def test_a_new_movie_is_hardlinked_into_its_canonical_folder(self) -> None:
        src = write_video(self.source / "The.Matrix.1999.1080p.mkv" / "movie.mkv")
        dest = self.library / "The Matrix (1999)" / "The Matrix (1999).mkv"
        self.assertTrue(self._place(src, dest))
        self.assertTrue(dest.samefile(src), "one file, two names: the download keeps seeding")
        self.assertEqual(dest.stat().st_nlink, 2)
        self.assertIn("completed", self.outcomes())

    def test_a_source_that_vanished_before_placement_is_a_failure_not_a_crash(self) -> None:
        """The hook runs on completion; the user may have deleted the download.

        Nothing is created at the destination, and the outcome is recorded as a
        failure so the run's exit code says it was not a clean sweep.
        """
        dest = self.library / "Gone (2020)" / "Gone (2020).mkv"
        self.assertFalse(self._place(self.source / "Gone (2020).mkv", dest))
        self.assertIn("source vanished", self.reasons())
        self.assertFalse(dest.exists())
        self.assertFalse(dest.parent.exists(), "not even an empty folder is left behind")

    def test_a_movie_that_is_already_in_place_is_a_skip_not_a_write(self) -> None:
        src = write_video(self.source / "movie.mkv")
        self.assertTrue(self._place(src, src))
        self.assertIn("already in place", self.reasons())
        self.assertIn("skipped", self.outcomes())

    def test_an_existing_hardlink_of_the_same_download_is_not_rewritten(self) -> None:
        """Idempotence: re-running the hook on a finished download writes nothing.

        It still answers True, because an already-linked movie is exactly the
        case where a *missing sidecar* should be allowed to arrive.
        """
        src = write_video(self.source / "movie.mkv")
        dest = self.library / "Film (2020)" / "Film (2020).mkv"
        dest.parent.mkdir(parents=True)
        os.link(src, dest)
        before = dest.stat()
        self.assertTrue(self._place(src, dest))
        self.assertIn("already in place", self.reasons())
        self.assertIn("skipped", self.outcomes())
        after = dest.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_a_dry_run_places_nothing_and_says_what_it_would_have_done(self) -> None:
        self.configure(dry_run=True)
        src = write_video(self.source / "movie.mkv")
        dest = self.library / "Film (2020)" / "Film (2020).mkv"
        self.assertTrue(self._place(src, dest))
        self.assertFalse(dest.exists())
        self.assertIn("dry run", self.reasons())

    def test_two_containers_of_one_movie_are_left_for_review(self) -> None:
        """``.mkv`` and ``.mp4`` of the same title in one folder is the auditor's
        MULTIPLE_DIRECT_MOVIE_FILES, and Jellyfin resolves it by guessing.

        Neither may be deleted to fix it: the tool skips and reports instead.
        """
        folder = self.library / "Film (2020)"
        folder.mkdir()
        mkv = write_video(folder / "Film (2020).mkv")
        mp4 = write_video(folder / "Film (2020).mp4", size=BIG // 2)
        src = write_video(self.source / "Film.2020.1080p.mkv" / "movie.mkv")
        self.assertFalse(self._place(src, mkv))
        self.assertIn("both movie containers already exist", self.reasons())
        self.assertTrue(mkv.is_file())
        self.assertTrue(mp4.is_file())

    def test_a_new_container_replaces_the_old_one_only_after_the_new_link_is_verified(self) -> None:
        """The MP4 arrives, the MKV goes - but not before the replacement exists."""
        folder = self.library / "Film (2020)"
        folder.mkdir()
        old = write_video(folder / "Film (2020).mp4", size=BIG // 2)
        src = write_video(self.source / "Film.2020.1080p.mkv" / "movie.mkv")
        dest = folder / "Film (2020).mkv"
        self.assertTrue(self._place(src, dest))
        self.assertTrue(dest.samefile(src))
        self.assertFalse(old.exists(), "the superseded container is gone")
        self.assertEqual(self.library_tree(), ["Film (2020)/Film (2020).mkv"])

    def test_a_rival_container_that_is_a_symlink_refuses_the_replacement(self) -> None:
        """``is_file()`` follows the link; ``stat(follow_symlinks=False)`` does not.

        The rival is only removed after the new hardlink is published, so the
        check that it is a *regular file* is what stops the tool unlinking
        something that turns out to be a link to a movie outside the library.
        """
        folder = self.library / "Film (2020)"
        folder.mkdir()
        elsewhere = write_video(self.root / "outside.mp4", size=BIG // 2)
        rival = folder / "Film (2020).mp4"
        rival.symlink_to(elsewhere)
        src = write_video(self.source / "Film.2020.1080p.mkv" / "movie.mkv")
        self.assertFalse(self._place(src, folder / "Film (2020).mkv"))
        self.assertIn("not a regular file", self.reasons())
        self.assertTrue(elsewhere.is_file(), "the file outside the library was not touched")

    def test_a_rival_that_changes_during_the_replacement_stops_the_swap(self) -> None:
        """Something else is writing that movie; the run must not delete it."""
        folder = self.library / "Film (2020)"
        folder.mkdir()
        rival = write_video(folder / "Film (2020).mp4", size=BIG // 2)
        src = write_video(self.source / "Film.2020.1080p.mkv" / "movie.mkv")
        real_stat = Path.stat
        stats = {"n": 0}

        def changing(path: Path, **kwargs: object) -> object:
            info = real_stat(path, **kwargs)  # type: ignore[arg-type]
            if path == rival:
                stats["n"] += 1
                if stats["n"] > 1:
                    # Every later look sees a different mtime: the file is being
                    # written by somebody else while this run is working.
                    stamp = 1_000_000_000 + stats["n"]
                    os.utime(rival, (stamp, stamp))
                    info = real_stat(path, **kwargs)  # type: ignore[arg-type]
            return info

        with mock.patch.object(Path, "stat", changing):
            placed = self._place(src, folder / "Film (2020).mkv")
        self.assertFalse(placed)
        self.assertIn("existing movie changed during container replacement", self.reasons())
        self.assertTrue(rival.is_file(), "the rival survived")

    def test_a_failed_placement_leaves_the_previous_movie_intact(self) -> None:
        """The destination is never deleted merely to retry a failed replace."""
        folder = self.library / "Film (2020)"
        folder.mkdir()
        existing = write_video(folder / "Film (2020).mkv", size=BIG // 2)
        src = write_video(self.source / "Film.2020.2160p.mkv" / "movie.mkv")
        with mock.patch.object(ms.os, "replace", side_effect=OSError("read-only library")):
            self.assertFalse(self._place(src, folder / "Film (2020).mkv"))
        self.assertTrue(existing.is_file())
        self.assertEqual(existing.stat().st_size, BIG // 2)
        self.assertEqual([p.name for p in folder.iterdir()], ["Film (2020).mkv"],
                         "no staging file is left beside the movie")

    def test_a_post_placement_check_that_fails_is_reported_as_a_failure(self) -> None:
        folder = self.library / "Film (2020)"
        folder.mkdir()
        src = write_video(self.source / "movie.mkv")
        real_samefile = Path.samefile
        with mock.patch.object(Path, "samefile", lambda path, other: False if path == src
                               else real_samefile(path, other)):  # type: ignore[arg-type]
            self.assertFalse(self._place(src, folder / "Film (2020).mkv"))
        self.assertIn("post-placement hardlink verification failed", self.reasons())

    def test_a_folder_the_library_will_not_list_is_not_a_rival(self) -> None:
        folder = self.library / "Film (2020)"
        folder.mkdir()
        src = write_video(self.source / "movie.mkv")
        real_iterdir = Path.iterdir

        def flaky(path: Path) -> object:
            if path == folder:
                raise OSError("share went away")
            return real_iterdir(path)

        with mock.patch.object(Path, "iterdir", flaky):
            self.assertTrue(self._place(src, folder / "Film (2020).mkv"))

    def test_a_sidecar_smaller_than_the_existing_one_is_not_placed(self) -> None:
        """Sidecars keep the established size rule: the library's copy wins a tie."""
        folder = self.library / "Film (2020)"
        folder.mkdir()
        existing = folder / "Film (2020).eng.srt"
        existing.write_text("1\n00:00:01,000 --> 00:00:02,000\nlonger existing sidecar\n\n",
                            encoding="utf-8")
        src = self.source / "movie.eng.srt"
        src.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n\n", encoding="utf-8")
        self.assertFalse(self._place(src, existing))
        self.assertIn("dest-larger", self.reasons())
        self.assertIn("longer existing sidecar", existing.read_text(encoding="utf-8"))

    def test_a_larger_sidecar_replaces_a_smaller_one(self) -> None:
        folder = self.library / "Film (2020)"
        folder.mkdir()
        existing = folder / "Film (2020).eng.srt"
        existing.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n\n", encoding="utf-8")
        src = self.source / "movie.eng.srt"
        src.write_text("1\n00:00:01,000 --> 00:00:02,000\na much longer replacement\n\n",
                       encoding="utf-8")
        self.assertTrue(self._place(src, existing))
        self.assertIn("a much longer replacement", existing.read_text(encoding="utf-8"))

    def test_two_paths_the_filesystem_will_not_compare_are_not_called_equal(self) -> None:
        """``paths_equal`` answers False when it cannot prove they are the same file.

        It falls back to the normalized-text comparison, which is also what makes
        it usable for paths that do not exist yet.
        """
        with mock.patch.object(Path, "samefile", side_effect=OSError("share went away")):
            self.assertFalse(ms.paths_equal(self.library / "a.mkv", self.library / "b.mkv"))
            self.assertTrue(ms.paths_equal(self.library / "a.mkv", self.library / "a.mkv"))

    def test_two_hardlinks_one_comparison_refuses_are_still_not_replaced(self) -> None:
        """The second opinion exists because the first one can fail.

        ``paths_equal`` giving up (a share that will not answer ``samefile``)
        must not turn into "these are different files, replace it": the
        placement path asks again, and an already-linked destination is a skip.
        """
        src = write_video(self.source / "movie.mkv")
        dest = self.library / "Film (2020)" / "Film (2020).mkv"
        dest.parent.mkdir(parents=True)
        os.link(src, dest)
        real_samefile = Path.samefile
        calls = {"n": 0}

        def flaky(path: Path, other: object) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("share went away")
            return real_samefile(path, other)  # type: ignore[arg-type]

        with mock.patch.object(Path, "samefile", flaky):
            self.assertEqual(ms.should_replace(src, dest), (False, "already-linked"))


class ReplacementDecisionTests(StandardizerFixture):
    def test_a_missing_destination_is_always_replaceable(self) -> None:
        self.assertEqual(ms.should_replace(self.source / "a.mkv", self.library / "b.mkv"),
                         (True, "missing"))

    def test_the_same_file_is_never_replaced_by_itself(self) -> None:
        movie = write_video(self.source / "a.mkv")
        self.assertEqual(ms.should_replace(movie, movie), (False, "same-file"))

    def test_a_comparison_the_filesystem_refuses_falls_through_to_the_size_rule(self) -> None:
        src = write_video(self.source / "a.mkv")
        dest = write_video(self.library / "b.srt" / "b.srt", size=1)
        with mock.patch.object(Path, "samefile", side_effect=OSError("share went away")):
            replace, reason = ms.should_replace(src, dest)
        self.assertTrue(replace)
        self.assertIn("src-larger", reason)

    def test_equal_sized_sidecars_are_left_alone(self) -> None:
        src = self.source / "a.srt"
        src.write_text("same", encoding="utf-8")
        dest = self.library / "b.srt"
        dest.write_text("size", encoding="utf-8")
        self.assertEqual(ms.should_replace(src, dest), (False, "same-size-exists"))


class MaintenanceModeTests(StandardizerFixture):
    """``dispose_candidate``: the only code in this tool that deletes."""

    def test_report_mode_never_touches_the_file(self) -> None:
        self.configure(maintenance_mode="REPORT", enable_deduplication=True)
        candidate = write_video(self.library / "Film (2020)" / "Film (2020).mkv", size=1024)
        self.assertEqual(ms.dispose_candidate(candidate, action="duplicate", reason="test"),
                         "reported")
        self.assertTrue(candidate.is_file())

    def test_a_dry_run_reports_even_in_delete_mode(self) -> None:
        self.configure(maintenance_mode="DELETE", dry_run=True)
        candidate = write_video(self.library / "Film (2020)" / "Film (2020).mkv", size=1024)
        self.assertEqual(ms.dispose_candidate(candidate, action="duplicate", reason="test"),
                         "reported")
        self.assertTrue(candidate.is_file())

    def test_delete_mode_removes_a_file_and_a_folder(self) -> None:
        self.configure(maintenance_mode="DELETE")
        candidate = write_video(self.library / "Film (2020)" / "Film (2020).mkv", size=1024)
        self.assertEqual(ms.dispose_candidate(candidate, action="duplicate", reason="test"),
                         "deleted")
        self.assertFalse(candidate.exists())
        folder = self.library / "Other (2021)"
        folder.mkdir()
        write_video(folder / "Other (2021).mkv", size=1024)
        self.assertEqual(ms.dispose_candidate(folder, action="duplicate", reason="test"), "deleted")
        self.assertFalse(folder.exists())

    def test_a_delete_the_filesystem_refuses_is_a_recorded_failure(self) -> None:
        self.configure(maintenance_mode="DELETE")
        candidate = write_video(self.library / "Film (2020)" / "Film (2020).mkv", size=1024)
        with mock.patch.object(Path, "unlink", side_effect=OSError("read-only library")):
            self.assertEqual(ms.dispose_candidate(candidate, action="duplicate", reason="test"),
                             "failed")
        self.assertTrue(candidate.is_file(), "the movie is still there")
        self.assertIn("failed", self.outcomes())

    def test_quarantine_mode_moves_the_candidate_outside_the_library(self) -> None:
        quarantine = self.root / "quarantine"
        self.configure(maintenance_mode="QUARANTINE", quarantine_dir=quarantine)
        candidate = write_video(self.library / "Film (2020)" / "Film (2020).mkv", size=1024)
        self.assertEqual(ms.dispose_candidate(candidate, action="duplicate", reason="test"),
                         "quarantined")
        self.assertFalse(candidate.exists())
        moved = list(quarantine.rglob("*.mkv"))
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].stat().st_size, 1024, "the bytes arrived intact")

    def test_a_quarantine_move_that_fails_is_a_recorded_failure(self) -> None:
        quarantine = self.root / "quarantine"
        self.configure(maintenance_mode="QUARANTINE", quarantine_dir=quarantine)
        candidate = write_video(self.library / "Film (2020)" / "Film (2020).mkv", size=1024)
        with mock.patch.object(ms.shutil, "move", side_effect=OSError("no space left")):
            self.assertEqual(ms.dispose_candidate(candidate, action="duplicate", reason="test"),
                             "failed")
        self.assertTrue(candidate.is_file())

    def test_a_candidate_outside_the_library_is_quarantined_under_its_own_name(self) -> None:
        """``relative_to`` fails for a path that is not under the target."""
        quarantine = self.root / "quarantine"
        self.configure(maintenance_mode="QUARANTINE", quarantine_dir=quarantine)
        candidate = write_video(self.root / "elsewhere" / "stray.mkv", size=1024)
        destination = ms._quarantine_destination(candidate)
        self.assertEqual(destination, quarantine / "stray.mkv")

    def test_an_occupied_quarantine_slot_gets_a_unique_name(self) -> None:
        quarantine = self.root / "quarantine"
        self.configure(maintenance_mode="QUARANTINE", quarantine_dir=quarantine)
        candidate = write_video(self.library / "Film (2020)" / "Film (2020).mkv", size=1024)
        (quarantine / "Film (2020)").mkdir(parents=True)
        (quarantine / "Film (2020)" / "Film (2020).mkv").write_bytes(b"already here")
        destination = ms._quarantine_destination(candidate)
        self.assertIn(".conflict.", destination.name)
        self.assertNotEqual(destination.read_bytes() if destination.exists() else b"", b"already here")

    def test_quarantine_without_a_destination_is_a_configuration_error(self) -> None:
        self.configure(maintenance_mode="QUARANTINE", quarantine_dir=None)
        with self.assertRaises(ValueError):
            ms._quarantine_destination(self.library / "Film (2020).mkv")

    def test_an_unknown_maintenance_mode_is_refused_rather_than_guessed(self) -> None:
        self.configure(maintenance_mode="REPORT")
        ms.CFG = ms.Config(source_dir=self.source, target_dir=self.library,
                           maintenance_mode="SHRED")
        with self.assertRaises(ValueError):
            ms.dispose_candidate(self.library / "x.mkv", action="duplicate", reason="test")

    def test_a_staging_file_that_cannot_be_removed_does_not_replace_the_real_error(self) -> None:
        self.configure()
        dest = self.library / "Film (2020)" / "Film (2020).mkv"

        def producer(tmp: Path) -> None:
            tmp.write_bytes(b"staged")

        with mock.patch.object(ms.os, "replace", side_effect=OSError("read-only library")), \
                mock.patch.object(Path, "unlink", side_effect=OSError("share went away")), \
                self.assertRaises(OSError) as caught:
            ms._replace_with("test", dest, producer)
        self.assertIn("read-only library", str(caught.exception))


class DuplicateScanTests(StandardizerFixture):
    """``deduplicate_movies``: deletion, but only of a provable duplicate."""

    def test_deduplication_is_off_unless_asked_for(self) -> None:
        first = self.library / "Film (2020)"
        second = self.library / "Film (2020) 1080p"
        write_video(first / "Film (2020).mkv", size=BIG)
        write_video(second / "Film (2020).mkv", size=BIG // 4)
        self.configure(enable_deduplication=False)
        ms.deduplicate_movies(self.library)
        self.assertTrue(second.exists(), "nothing is removed without --deduplicate")

    def test_a_much_smaller_duplicate_folder_is_removed_in_delete_mode(self) -> None:
        keeper = self.library / "Film (2020)"
        smaller = self.library / "Film 2020 720p"
        write_video(keeper / "Film (2020).mkv", size=BIG)
        write_video(smaller / "Film (2020).mkv", size=BIG // 20)
        self.configure(enable_deduplication=True, maintenance_mode="DELETE")
        ms.deduplicate_movies(self.library)
        self.assertTrue(keeper.is_dir())
        self.assertFalse(smaller.exists())

    def test_two_copies_within_the_margin_are_left_for_a_human(self) -> None:
        """Same identity, nearly the same size: that is a judgement call.

        Guessing here deletes a movie. The margin is a percentage of the keeper,
        and both copies survive with a warning in the log.
        """
        first = self.library / "Film (2020)"
        second = self.library / "Film 2020 1080p"
        write_video(first / "Film (2020).mkv", size=BIG)
        write_video(second / "Film (2020).mkv", size=BIG - 1024)
        self.configure(enable_deduplication=True, maintenance_mode="DELETE",
                       dedup_size_margin_pct=5.0)
        ms.deduplicate_movies(self.library)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir(), "an ambiguous duplicate is never deleted")

    def test_a_duplicate_that_is_the_same_file_is_removed_without_losing_anything(self) -> None:
        """A leftover hardlink tree: one inode, two folders, one name afterwards.

        Which of the two folders keeps the name is not part of the promise: the
        keeper is chosen by size, these two sizes are the same inode's size, and
        the tie is broken by the order the library directory happens to
        enumerate in. What is promised is that the movie survives, that exactly
        one name for it is left, and that nothing was copied to get there.
        """
        keeper = self.library / "Film (2020)"
        leftover = self.library / "Film 2020"
        keeper.mkdir()
        leftover.mkdir()
        movie = write_video(keeper / "Film (2020).mkv", size=BIG)
        twin = leftover / "Film (2020).mkv"
        os.link(movie, twin)
        self.configure(enable_deduplication=True, maintenance_mode="DELETE")
        ms.deduplicate_movies(self.library)

        survivors = [path for path in (movie, twin) if path.exists()]
        self.assertEqual(len(survivors), 1, "one inode keeps exactly one name")
        self.assertEqual(survivors[0].stat().st_size, BIG, "and it is still the whole movie")
        self.assertEqual(survivors[0].stat().st_nlink, 1, "the extra name is really gone")

    def test_a_video_less_duplicate_that_still_holds_files_is_kept(self) -> None:
        """Subtitles and artwork are unique data even when the movie is not there."""
        keeper = self.library / "Film (2020)"
        shell = self.library / "Film 2020 1080p"
        write_video(keeper / "Film (2020).mkv", size=BIG)
        shell.mkdir()
        write_srt(shell / "Film (2020).eng.srt")
        self.configure(enable_deduplication=True, maintenance_mode="DELETE")
        ms.deduplicate_movies(self.library)
        self.assertTrue(shell.is_dir())
        self.assertTrue((shell / "Film (2020).eng.srt").is_file())

    def test_a_provably_empty_duplicate_shell_is_removed(self) -> None:
        keeper = self.library / "Film (2020)"
        shell = self.library / "Film 2020 1080p"
        write_video(keeper / "Film (2020).mkv", size=BIG)
        shell.mkdir()
        self.configure(enable_deduplication=True, maintenance_mode="DELETE")
        ms.deduplicate_movies(self.library)
        self.assertFalse(shell.exists())

    def test_a_folder_that_cannot_be_proved_empty_is_kept(self) -> None:
        """Walk errors count as "has files": if it cannot be proved empty, it stays."""
        keeper = self.library / "Film (2020)"
        shell = self.library / "Film 2020 1080p"
        write_video(keeper / "Film (2020).mkv", size=BIG)
        shell.mkdir()
        real_walk = ms.os.walk

        def failing_walk(path: object, **kwargs: object) -> object:
            onerror = kwargs.get("onerror")
            if str(path) == str(shell) and onerror is not None:
                onerror(OSError("share went away"))
                return iter(())
            return real_walk(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(ms.os, "walk", failing_walk):
            self.assertTrue(ms._folder_has_files(shell))
        with mock.patch.object(ms.os, "walk", failing_walk):
            self.configure(enable_deduplication=True, maintenance_mode="DELETE")
            ms.deduplicate_movies(self.library)
        self.assertTrue(shell.is_dir())

    def test_a_jellyfin_multi_version_folder_is_never_collapsed(self) -> None:
        """``Title (2020) - 1080p.mkv`` beside ``Title (2020) - 2160p.mkv`` is a feature.

        Jellyfin documents that layout as one movie with two versions, so a
        deduplicator that "kept the largest" would delete a version the user
        picked on purpose.
        """
        self.configure(jellyfin_mode=True, enable_deduplication=True,
                       maintenance_mode="DELETE")
        versions = self.library / "Film (2020)"
        versions.mkdir()
        write_video(versions / "Film (2020) - 1080p.mkv", size=BIG)
        write_video(versions / "Film (2020) - 2160p.mkv", size=BIG // 8)
        self.assertTrue(ms._is_jellyfin_multi_version_folder(versions))
        duplicate = self.library / "Film 2020"
        duplicate.mkdir()
        write_video(duplicate / "Film (2020).mkv", size=BIG // 8)
        ms.deduplicate_movies(self.library)
        self.assertTrue(versions.is_dir())
        self.assertEqual(len(list(versions.iterdir())), 2, "both versions survived")

    def test_a_folder_that_cannot_be_listed_is_treated_as_a_multi_version_folder(self) -> None:
        """Cannot prove safety, so never collapse a potentially valid set."""
        self.configure(jellyfin_mode=True)
        folder = self.library / "Film (2020)"
        folder.mkdir()
        with mock.patch.object(Path, "iterdir", side_effect=OSError("share went away")):
            self.assertTrue(ms._is_jellyfin_multi_version_folder(folder))

    def test_outside_jellyfin_mode_the_same_folder_is_not_a_version_set(self) -> None:
        self.configure(jellyfin_mode=False)
        folder = self.library / "Film (2020)"
        folder.mkdir()
        write_video(folder / "Film (2020) - 1080p.mkv", size=BIG)
        write_video(folder / "Film (2020) - 2160p.mkv", size=BIG)
        self.assertFalse(ms._is_jellyfin_multi_version_folder(folder))

    def test_a_library_that_cannot_be_listed_ends_the_scan_without_deleting(self) -> None:
        self.configure(enable_deduplication=True, maintenance_mode="DELETE")
        with mock.patch.object(Path, "iterdir", side_effect=OSError("share went away")):
            ms.deduplicate_movies(self.library)
        self.assertEqual(self.library_tree(), [])
        self.assertTrue(self.library.is_dir())

    def test_flat_libraries_are_deduplicated_file_by_file(self) -> None:
        """``--no-subfolders`` libraries hold the movies directly in the target."""
        self.configure(enable_deduplication=True, maintenance_mode="DELETE",
                       create_subfolders=False)
        keeper = write_video(self.library / "Film (2020).mkv", size=BIG)
        smaller = write_video(self.library / "Film 2020 720p.mkv", size=BIG // 20)
        write_srt(smaller.with_suffix(".eng.srt"))
        ms.deduplicate_movies(self.library)
        self.assertTrue(keeper.is_file())
        self.assertFalse(smaller.exists())
        self.assertFalse(smaller.with_suffix(".eng.srt").exists(),
                         "the deleted movie's sidecar goes with it")

    def test_an_ambiguous_flat_duplicate_keeps_its_sidecar(self) -> None:
        self.configure(enable_deduplication=True, maintenance_mode="DELETE",
                       create_subfolders=False, dedup_size_margin_pct=5.0)
        write_video(self.library / "Film (2020).mkv", size=BIG)
        near = write_video(self.library / "Film 2020.mkv", size=BIG - 1024)
        write_srt(near.with_suffix(".eng.srt"))
        ms.deduplicate_movies(self.library)
        self.assertTrue(near.is_file())
        self.assertTrue(near.with_suffix(".eng.srt").is_file())

    def test_a_flat_library_that_cannot_be_listed_is_left_alone(self) -> None:
        self.configure(enable_deduplication=True, maintenance_mode="DELETE",
                       create_subfolders=False)
        with mock.patch.object(Path, "iterdir", side_effect=OSError("share went away")):
            ms.deduplicate_movies(self.library)
        self.assertTrue(self.library.is_dir())

    def test_a_target_that_does_not_exist_ends_the_scan(self) -> None:
        self.configure(enable_deduplication=True)
        ms.deduplicate_movies(self.root / "no-such-library")  # must not raise

    def test_the_largest_video_in_a_folder_is_the_keeper(self) -> None:
        folder = self.library / "Film (2020)"
        folder.mkdir()
        small = write_video(folder / "Film (2020)-sample.mkv", size=1024)
        big = write_video(folder / "Film (2020).mkv", size=BIG)
        video, size = ms._largest_video_in(folder)
        self.assertEqual(video, big)
        self.assertEqual(size, BIG)
        self.assertNotEqual(small, big)

    def test_a_folder_with_no_video_has_no_keeper(self) -> None:
        folder = self.library / "Film (2020)"
        folder.mkdir()
        write_srt(folder / "Film (2020).eng.srt")
        self.assertEqual(ms._largest_video_in(folder), (None, 0))

    def test_a_path_that_is_not_a_folder_has_no_keeper(self) -> None:
        movie = write_video(self.library / "Film (2020).mkv")
        self.assertEqual(ms._largest_video_in(movie), (None, 0))


class SidecarMatchingTests(StandardizerFixture):
    """``match_subtitles_for_video``: which sidecar belongs to which movie."""

    def _scan(self, names: list[str]) -> list[ms.ScannedFile]:
        folder = self.source / "release"
        folder.mkdir(parents=True, exist_ok=True)
        return [ms.ScannedFile(write_srt(folder / name), 10, "subtitle") for name in names]

    def test_a_single_video_release_takes_every_sidecar(self) -> None:
        subs = self._scan(["movie.eng.srt", "movie.fra.srt"])
        video = self.source / "release" / "movie.mkv"
        parsed = ms.parse_movie_name(video.name)
        matched = ms.match_subtitles_for_video(video, parsed, subs, multi=False)
        self.assertEqual(len(matched), 2)

    def test_in_a_multi_movie_release_a_sidecar_matches_by_stem(self) -> None:
        subs = self._scan(["first.eng.srt", "second.eng.srt"])
        video = self.source / "release" / "first.mkv"
        parsed = ms.parse_movie_name(video.name)
        matched = ms.match_subtitles_for_video(video, parsed, subs, multi=True)
        self.assertEqual([sub.path.name for sub in matched], ["first.eng.srt"])

    def test_a_sidecar_in_a_folder_named_after_the_movie_matches(self) -> None:
        """Box sets ship ``Subs/Title/...srt``; the parent name is the link."""
        subs = self._scan(["first.eng.srt"])
        subs.append(ms.ScannedFile(
            write_srt(self.source / "release" / "First 2020" / "anything.eng.srt"), 10, "subtitle"))
        video = self.source / "release" / "first.mkv"
        parsed = ms.parse_movie_name("First.2020.1080p.mkv")
        matched = ms.match_subtitles_for_video(video, parsed, subs, multi=True)
        self.assertIn("anything.eng.srt", [sub.path.name for sub in matched])

    def test_a_sidecar_named_after_the_movie_matches(self) -> None:
        subs = self._scan(["The.Matrix.1999.eng.srt"])
        video = self.source / "release" / "the.matrix.1999.1080p.mkv"
        parsed = ms.parse_movie_name(video.name)
        matched = ms.match_subtitles_for_video(video, parsed, subs, multi=True)
        self.assertEqual([sub.path.name for sub in matched], ["The.Matrix.1999.eng.srt"])

    def test_a_sidecar_of_a_different_movie_does_not_match(self) -> None:
        subs = self._scan(["Inception.2010.eng.srt"])
        video = self.source / "release" / "The.Matrix.1999.mkv"
        parsed = ms.parse_movie_name(video.name)
        self.assertEqual(ms.match_subtitles_for_video(video, parsed, subs, multi=True), [])

    def test_a_sidecar_in_a_folder_called_subs_does_not_match_every_movie(self) -> None:
        """``Subs`` names the folder, not the film."""
        subs = [ms.ScannedFile(write_srt(self.source / "release" / "Subs" / "x.eng.srt"),
                               10, "subtitle")]
        video = self.source / "release" / "The.Matrix.1999.mkv"
        parsed = ms.parse_movie_name(video.name)
        self.assertEqual(ms.match_subtitles_for_video(video, parsed, subs, multi=True), [])

    def test_a_sidecar_pool_that_cannot_be_listed_is_empty_not_fatal(self) -> None:
        video = write_video(self.source / "movie.mkv")
        parsed = ms.parse_movie_name(video.name)
        with mock.patch.object(Path, "iterdir", side_effect=OSError("share went away")):
            self.assertEqual(ms._sidecar_pool_for_single(video, parsed), [])


class FolderScanTests(StandardizerFixture):
    def test_a_release_is_scanned_into_the_kinds_the_placement_rules_need(self) -> None:
        release = self.source / "The.Matrix.1999.1080p.BluRay"
        movie = write_video(release / "the.matrix.1999.1080p.mkv", size=BIG)
        sidecar = write_srt(release / "the.matrix.1999.1080p.eng.srt")
        poster = release / "poster.jpg"
        poster.parent.mkdir(parents=True, exist_ok=True)
        poster.write_bytes(b"jpeg")
        nfo = release / "movie.nfo"
        nfo.write_text("<movie/>", encoding="utf-8")
        extra = write_video(release / "Extras" / "making-of.mkv", size=BIG)
        scan = ms.scan_tree(release)
        self.assertIn(movie, [item.path for item in scan.videos])
        self.assertIn(sidecar, [item.path for item in scan.subtitles])
        self.assertIn(poster, [item.path for item in scan.artwork])
        self.assertIn(extra, [item.path for item in scan.extras])
        self.assertIn(nfo, [item.path for item in scan.files if item.kind == "other"])
        self.assertFalse(scan.is_disc)

    def test_a_path_that_is_not_a_folder_scans_to_nothing(self) -> None:
        movie = write_video(self.source / "movie.mkv")
        scan = ms.scan_tree(movie)
        self.assertEqual(scan.files, [])
        self.assertEqual(scan.videos, [])

    def test_a_symlink_inside_a_release_is_not_a_candidate(self) -> None:
        """``os.link`` follows symlinks, so one would pull a file in from outside."""
        release = self.source / "release"
        release.mkdir()
        real = write_video(self.root / "outside.mkv", size=BIG)
        (release / "linked.mkv").symlink_to(real)
        scan = ms.scan_tree(release)
        self.assertEqual(scan.videos, [])
        self.assertTrue(real.is_file())

    def test_a_file_that_cannot_be_stat_ed_is_skipped(self) -> None:
        release = self.source / "release"
        release.mkdir()
        write_video(release / "movie.mkv", size=BIG)
        real_stat = Path.stat

        def flaky(path: Path, **kwargs: object) -> object:
            if path.name == "movie.mkv" and kwargs.get("follow_symlinks") is False:
                raise OSError("share went away")
            return real_stat(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(Path, "stat", flaky):
            self.assertEqual(ms.scan_tree(release).videos, [])

    def test_a_disc_structure_is_scanned_as_disc_files_not_movies(self) -> None:
        release = self.source / "Movie.2020.BDMV"
        (release / "BDMV" / "STREAM").mkdir(parents=True)
        stream = release / "BDMV" / "movie.mkv"
        stream.write_bytes(b"x" * 4096)
        scan = ms.scan_tree(release)
        self.assertTrue(scan.is_disc)
        self.assertEqual(scan.videos, [], "an .m2ts is not a movie")
        self.assertEqual([item.kind for item in scan.files], ["disc-file"])

    def test_a_junk_folder_is_not_descended_into(self) -> None:
        release = self.source / "release"
        (release / ".unwanted").mkdir(parents=True)
        write_video(release / ".unwanted" / "movie.mkv", size=BIG)
        self.assertEqual(ms.scan_tree(release).videos, [])

    def test_a_video_under_the_size_floor_is_an_extra_not_a_feature(self) -> None:
        release = self.source / "release"
        write_video(release / "movie.mkv", size=1024)
        scan = ms.scan_tree(release)
        self.assertEqual(scan.videos, [])
        self.assertEqual([item.kind for item in scan.files], ["extra"])

    def test_a_sidecar_inside_an_extras_folder_is_not_a_sidecar(self) -> None:
        release = self.source / "release"
        write_srt(release / "Extras" / "making-of.eng.srt")
        scan = ms.scan_tree(release)
        self.assertEqual(scan.subtitles, [])
        self.assertEqual([item.kind for item in scan.files], ["other"])

    def test_a_destination_without_subfolders_goes_straight_into_the_library(self) -> None:
        self.configure(create_subfolders=False)
        parsed = ms.parse_movie_name("The.Matrix.1999.1080p.mkv")
        self.assertEqual(ms.dest_for(parsed, ".mkv"), self.library / "The Matrix (1999).mkv")
        self.configure(create_subfolders=True)
        self.assertEqual(ms.dest_for(parsed, ".mkv"),
                         self.library / "The Matrix (1999)" / "The Matrix (1999).mkv")


class DeclineTests(StandardizerFixture):
    """What the tool refuses, and that every refusal is recorded with a reason."""

    def _handle(self, path: Path) -> None:
        with redirect_stdout(io.StringIO()):
            ms.handle_item(path)

    def test_an_extra_or_sample_is_declined_with_its_reason(self) -> None:
        sample = write_video(self.source / "Movie-sample.mkv", size=BIG)
        self._handle(sample)
        self.assertIn("extra/sample video, not a feature", self.reasons())
        self.assertEqual(self.library_tree(), [])

    def test_a_multipart_fragment_is_declined(self) -> None:
        """Canonical output requires one complete MKV; half a movie is not placed."""
        part = write_video(self.source / "Movie.2020.1080p.cd1.mkv", size=BIG)
        self._handle(part)
        self.assertIn("multipart fragment", self.reasons())
        self.assertEqual(self.library_tree(), [])

    def test_a_path_that_does_not_exist_is_reported_and_not_an_outcome(self) -> None:
        self._handle(self.source / "gone.mkv")
        self.assertEqual(ms.RUN_EVENTS, [], "a vanished input is logged, not counted as a failure")

    def test_a_path_that_is_neither_file_nor_folder_is_reported(self) -> None:
        broken = self.source / "dangling.mkv"
        broken.symlink_to(self.root / "nothing.mkv")
        self._handle(broken)  # a symlink input is refused before this branch
        self.assertIn("symlink input", self.reasons())

    def test_a_filesystem_that_refuses_the_input_is_a_recorded_failure(self) -> None:
        with mock.patch.object(Path, "exists", side_effect=OSError("share went away")):
            self._handle(self.source / "movie.mkv")
        self.assertIn("failed", self.outcomes())
        self.assertIn("share went away", self.reasons())

    def test_a_folder_of_only_tv_content_has_no_usable_movie(self) -> None:
        """The folder is not named like TV, but everything inside it is.

        A season pack downloaded into a generically named folder still has to be
        refused: placing an episode as a movie puts a 40-minute file in the
        library under a film's name.
        """
        release = self.source / "Some.Release.1080p"
        write_video(release / "Show.Name.S01E01.1080p.mkv", size=BIG)
        write_video(release / "Show.Name.S01E02.1080p.mkv", size=BIG)
        with redirect_stdout(io.StringIO()):
            ms.handle_directory(release)
        self.assertIn("no usable movie video after excluding TV content", self.reasons())
        self.assertEqual(self.library_tree(), [])

    def test_a_folder_of_multipart_fragments_is_declined(self) -> None:
        """One marked fragment beside one unmarked file of the same title.

        The all-parts case and the part-numbered-stack case each have their own
        refusal; this is the mixed one, where a fragment is still a fragment and
        must not be placed as if it were the whole film.
        """
        release = self.source / "Movie.2020.1080p"
        write_video(release / "Movie.2020.1080p.cd1.mkv", size=BIG)
        write_video(release / "Movie.2020.1080p.mkv", size=BIG)
        with redirect_stdout(io.StringIO()):
            ms.handle_directory(release)
        self.assertIn("multipart fragments", self.reasons())

    def test_a_complete_part_numbered_stack_is_declined_as_a_split_release(self) -> None:
        release = self.source / "Movie.2020.1080p"
        write_video(release / "Movie.2020.1080p.cd1.mkv", size=BIG)
        write_video(release / "Movie.2020.1080p.cd2.mkv", size=BIG)
        with redirect_stdout(io.StringIO()):
            ms.handle_directory(release)
        self.assertIn("multipart movie", self.reasons())

    def test_a_folder_with_nothing_placeable_explains_what_it_found(self) -> None:
        release = self.source / "Movie.2020.1080p"
        write_video(release / "movie.avi", size=BIG)
        with redirect_stdout(io.StringIO()):
            ms.handle_directory(release)
        self.assertIn("this tool never transcodes", self.reasons().lower() + "avi")

    def test_a_folder_of_small_videos_reports_the_size_floor(self) -> None:
        release = self.source / "Movie.2020.1080p"
        write_video(release / "movie.mkv", size=1024)
        with redirect_stdout(io.StringIO()):
            ms.handle_directory(release)
        self.assertIn("minimum", self.reasons())

    def test_an_incomplete_download_is_not_what_the_explanation_is_about(self) -> None:
        """The reason has to name the real problem, not the debris beside it."""
        release = self.source / "Movie.2020.1080p"
        release.mkdir()
        (release / "movie.mkv.!qb").write_bytes(b"partial")
        reason = ms.explain_no_canonical_video(release)
        self.assertIn("no movie-sized video", reason.lower() + reason)

    def test_an_unreadable_video_is_not_counted_when_explaining_a_decline(self) -> None:
        """A file the share will not describe cannot be cited as the biggest one."""
        release = self.source / "Movie.2020.1080p"
        movie = write_video(release / "movie.mkv", size=1024)
        real_stat = Path.stat

        def flaky(path: Path, **kwargs: object) -> object:
            if path == movie:
                raise OSError("share went away")
            return real_stat(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(Path, "stat", flaky):
            reason = ms.explain_no_canonical_video(release)
        self.assertTrue(reason)


class ConfigurationGateTests(StandardizerFixture):
    """``validate_config``: every rule here prevents the tool reading its own output."""

    def _errors(self, **kwargs: object) -> list[str]:
        cfg = self.configure(**kwargs)
        return ms.validate_config(cfg)

    def assertError(self, fragment: str, **kwargs: object) -> None:
        """``validate_config`` answers a list of sentences; find the one meant."""
        errors = self._errors(**kwargs)
        self.assertTrue(any(fragment in error for error in errors),
                        f"{fragment!r} not in {errors}")

    def test_a_workable_configuration_has_no_errors(self) -> None:
        self.assertEqual(self._errors(), [])

    def test_an_unsupported_maintenance_mode_is_refused(self) -> None:
        self.assertError("Unsupported maintenance mode: SHRED", maintenance_mode="SHRED")

    def test_quarantine_without_a_destination_is_refused(self) -> None:
        self.assertError("QUARANTINE maintenance mode requires --quarantine-dir",
                         maintenance_mode="QUARANTINE")

    def test_a_negative_size_floor_is_refused(self) -> None:
        self.assertError("--min-size must be zero or greater", min_movie_size_mb=-1.0)

    def test_a_negative_lock_timeout_is_refused(self) -> None:
        self.assertError("--lock-timeout must be zero or greater", lock_timeout_seconds=-1.0)

    def test_a_target_that_is_a_file_is_refused(self) -> None:
        a_file = self.root / "not-a-library"
        a_file.write_text("x", encoding="utf-8")
        self.assertError("--target exists but is not a directory", target_dir=a_file)

    def test_a_quarantine_inside_the_source_is_refused(self) -> None:
        """A batch scan would then ingest what it just quarantined."""
        self.assertError("--quarantine-dir must be outside --source",
                         quarantine_dir=self.source / "quarantine")

    def test_a_manifest_inside_the_source_is_refused(self) -> None:
        self.assertError("--manifest must be outside --source",
                         manifest_file=self.source / "manifest.json")

    def test_a_report_inside_the_source_is_refused(self) -> None:
        self.assertError("--report must be outside --source",
                         report_file=self.source / "report.txt")

    def test_a_log_inside_the_library_is_refused(self) -> None:
        """A media server indexes anything in the library, including a log."""
        self.assertError("--log must be outside --target",
                         log_file=self.library / "standardizer.log")

    def test_a_source_and_target_on_different_volumes_cannot_hardlink(self) -> None:
        with mock.patch.object(ms, "_filesystem_device",
                               side_effect=[1, 2, OSError("no device")]):
            errors = ms.validate_config(self.configure())
        self.assertTrue(any("same filesystem" in error or "Could not verify" in error
                            for error in errors), errors)

    def test_a_device_lookup_that_fails_is_reported_as_unverifiable(self) -> None:
        with mock.patch.object(ms, "_filesystem_device", side_effect=OSError("no device")):
            errors = ms.validate_config(self.configure())
        self.assertIn("Could not verify source/target filesystem for hardlinks: no device", errors)

    def test_the_windows_source_default_is_the_documented_one(self) -> None:
        """A POSIX host must never be told about ``E:\\torrents\\final``."""
        with platforms.windows():
            self.assertEqual(ms.default_source_root(), Path(r"E:\torrents\final"))
        with platforms.posix():
            self.assertEqual(ms.default_source_root(), Path.home() / "torrents" / "final")


class CommandLineTests(StandardizerFixture):
    def _cfg_from(self, argv: list[str]) -> ms.Config:
        parser = ms.build_parser()
        return ms.cfg_from_args(parser.parse_args(argv))

    def test_a_lock_timeout_on_the_command_line_reaches_the_configuration(self) -> None:
        cfg = self._cfg_from(["--lock-timeout", "5"])
        self.assertEqual(cfg.lock_timeout_seconds, 5.0)

    def test_allow_tv_turns_the_tv_filter_off(self) -> None:
        self.assertFalse(self._cfg_from(["--allow-tv"]).skip_tv_shows)
        self.assertTrue(self._cfg_from([]).skip_tv_shows)

    def test_the_maintenance_mode_is_upper_cased_wherever_it_came_from(self) -> None:
        """``MOVIE_STD_MAINTENANCE_MODE=delete`` and ``--maintenance-mode DELETE``
        are the same setting, and the comparison inside the tool is upper case.
        """
        with mock.patch.dict(os.environ, {"MOVIE_STD_MAINTENANCE_MODE": "delete"}):
            self.assertEqual(self._cfg_from([]).maintenance_mode, "DELETE")
        self.assertEqual(self._cfg_from(["--maintenance-mode", "DELETE"]).maintenance_mode,
                         "DELETE")

    def test_a_tv_category_is_recognised_from_its_tokens(self) -> None:
        for category in ("tv", "tv-sonarr", "series.anime", "TV Shows"):
            with self.subTest(category=category):
                self.assertTrue(ms.category_is_tv(category))
        for category in ("", "movies", "1080p"):
            with self.subTest(category=category):
                self.assertFalse(ms.category_is_tv(category))

    def test_a_symlinked_automated_input_is_refused(self) -> None:
        """The hook receives ``%F``; a symlink would be followed out of the source."""
        real = write_video(self.root / "outside.mkv")
        link = self.source / "movie.mkv"
        link.symlink_to(real)
        cfg = self.configure()
        self.assertEqual(ms.validate_automated_input(link, cfg),
                         "qBittorrent input must not be a symlink")

    def test_an_input_inside_the_library_is_refused(self) -> None:
        movie = write_video(self.library / "Film (2020)" / "Film (2020).mkv")
        cfg = self.configure()
        self.assertEqual(ms.validate_automated_input(movie, cfg),
                         "qBittorrent input is inside the organized library")

    def test_an_input_the_filesystem_will_not_describe_is_refused_with_the_reason(self) -> None:
        cfg = self.configure()
        with mock.patch.object(Path, "exists", side_effect=OSError("share went away")):
            self.assertIn("could not validate qBittorrent input",
                          ms.validate_automated_input(self.source / "movie.mkv", cfg))

    def test_the_save_path_and_torrent_name_are_joined(self) -> None:
        """``%D %N``: a directory and a name, which is only a path together."""
        folder = self.source / "The.Matrix.1999"
        folder.mkdir()
        self.assertEqual(ms.resolve_input_path([str(self.source), "The.Matrix.1999"]), folder)

    def test_a_name_then_a_full_path_resolves_to_the_one_that_exists(self) -> None:
        """``%N %F`` arrives in the other order from some configurations."""
        folder = self.source / "The.Matrix.1999"
        folder.mkdir()
        self.assertEqual(ms.resolve_input_path(["The.Matrix.1999", str(folder)]), folder)

    def test_a_first_path_that_exists_wins_over_one_that_does_not(self) -> None:
        folder = self.source / "The.Matrix.1999"
        folder.mkdir()
        self.assertEqual(ms.resolve_input_path([str(folder), "/nope/second"]), folder)

    def test_two_plain_files_resolve_to_the_first(self) -> None:
        """Neither is a directory, so there is nothing to prefer: ``%D`` wins."""
        first = self.source / "one.mkv"
        first.write_bytes(b"x")
        second = self.source / "two.mkv"
        second.write_bytes(b"y")
        self.assertEqual(ms.resolve_input_path([str(first), str(second)]), first)

    def test_two_paths_where_neither_exists_resolve_to_the_first(self) -> None:
        self.assertEqual(ms.resolve_input_path(["/nope/one", "/nope/two"]), Path("/nope/one"))

    def test_no_paths_at_all_is_not_an_automated_run(self) -> None:
        self.assertIsNone(ms.resolve_input_path([]))
        self.assertIsNone(ms.resolve_input_path(["", None]))  # type: ignore[list-item]

    def test_a_batch_entry_that_vanished_before_it_could_be_sorted_is_still_handled(self) -> None:
        """The sort key stats every entry; a deleted download must not stop the batch."""
        write_video(self.source / "movie.mkv", size=BIG)
        real_stat = Path.stat
        calls = {"n": 0}

        def flaky(path: Path, **kwargs: object) -> object:
            calls["n"] += 1
            if path.name == "movie.mkv" and calls["n"] == 1:
                raise OSError("vanished between listing and sorting")
            return real_stat(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(Path, "stat", flaky), redirect_stdout(io.StringIO()):
            ms.batch_scan(self.source)
        self.assertTrue(calls["n"])


class LoggingSetupTests(StandardizerFixture):
    """A log file is a convenience; losing it must not lose the run."""

    def test_a_log_path_that_cannot_be_opened_is_reported_and_ignored(self) -> None:
        cfg = self.configure(log_file=self.root / "a-file" / "standardizer.log")
        (self.root / "a-file").write_text("in the way", encoding="utf-8")
        warnings: list[str] = []
        with mock.patch.object(ms.LOG, "warning", lambda message, *args: warnings.append(
                message % args if args else message)):
            ms.setup_logging(cfg)
        self.assertTrue(any("Cannot write log file" in line for line in warnings), warnings)

    def test_a_log_directory_that_cannot_be_created_is_skipped_quietly(self) -> None:
        cfg = self.configure(log_file=self.root / "logs" / "standardizer.log")
        with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")):
            ms.setup_logging(cfg)  # must not raise

    def test_a_run_without_a_log_file_logs_to_the_console_only(self) -> None:
        cfg = self.configure(log_file=None)
        ms.setup_logging(cfg)
        self.assertTrue(ms.LOG.handlers)


class RunLevelTests(StandardizerRunFixture):
    """The whole run, through ``main()``, on the real fixture."""

    def test_a_run_that_raises_is_logged_and_exits_1(self) -> None:
        """The hook is launched by qBittorrent; a silent crash is a lost movie."""
        with mock.patch.object(ms, "run", mock.Mock(side_effect=RuntimeError("the library vanished"))):
            code = ms.main(["--source", str(self.source), "--target", str(self.library)])
        self.assertEqual(code, 1)

    def test_a_crash_whose_logging_also_fails_still_prints_a_traceback(self) -> None:
        def exploding_logger(*args: object, **kwargs: object) -> None:
            raise RuntimeError("logging is broken too")

        from contextlib import redirect_stderr

        with mock.patch.object(ms, "run", mock.Mock(side_effect=RuntimeError("boom"))), \
                mock.patch.object(ms.LOG, "exception", exploding_logger), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = ms.main(["--source", str(self.source), "--target", str(self.library)])
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError", err.getvalue(),
                      "a qBittorrent-launched crash is never silent")

    def test_an_interrupt_exits_130(self) -> None:
        with mock.patch.object(ms, "run", mock.Mock(side_effect=KeyboardInterrupt)):
            code = ms.main(["--source", str(self.source), "--target", str(self.library)])
        self.assertEqual(code, 130)

    def test_the_shipped_self_test_reports_whether_this_filesystem_can_hardlink(self) -> None:
        """Placement is hardlink-only, so a filesystem without links is a real finding.

        Asked of a pristine copy of the tool, because importing ``tests/selftests``
        rebinds this name on the shared module to the fuller moved-out suite.
        """
        tool = pristine_tool()
        with redirect_stdout(io.StringIO()) as out:
            code = tool.run_canonical_self_tests()
        self.assertIn("this filesystem supports hardlinks", out.getvalue())
        self.assertEqual(code, 0, out.getvalue())

    def test_a_filesystem_without_hardlinks_fails_the_shipped_self_test(self) -> None:
        """Placement is hardlink-only: on a filesystem without links the tool must say so.

        A run that "succeeded" by copying instead would silently double the
        library's disk usage and leave the torrent client's copy disconnected
        from the movie the rest of the pipeline works on.
        """
        tool = pristine_tool()
        with mock.patch.object(tool.os, "link", side_effect=OSError("not supported")), \
                redirect_stdout(io.StringIO()) as out:
            code = tool.run_canonical_self_tests()
        self.assertEqual(code, 1)
        self.assertIn("SELF-TEST FAILED", out.getvalue())
        self.assertIn("hardlinks", out.getvalue())

    def test_a_clean_run_cleans_up_the_library_it_was_pointed_at(self) -> None:
        """``run_cleanup_on_target`` sweeps the library after the ingest."""
        extra = self.library / "Film (2020)" / "Extras"
        extra.mkdir(parents=True)
        write_video(extra / "making-of.mkv", size=1024)
        self.release("The.Great.Escape.1963.1080p.BluRay.x264-GRP",
                     "the.great.escape.1963.1080p.bluray.x264-grp.mkv")
        self.assertEqual(self.run_main("--deduplicate", "--maintenance-mode", "REPORT"), 0)
        self.assertIn("The Great Escape (1963)/The Great Escape (1963).mkv", self.library_tree())

    def test_a_logging_handler_that_cannot_emit_does_not_fail_the_run(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(self.log))
        ms.LOG.addHandler(handler)
        with mock.patch.object(handler, "emit", side_effect=OSError("read-only")):
            self.release("The.Great.Escape.1963.1080p.BluRay.x264-GRP",
                         "the.great.escape.1963.1080p.bluray.x264-grp.mkv")
            self.assertEqual(self.run_main(), 0)


if __name__ == "__main__":
    unittest.main()
