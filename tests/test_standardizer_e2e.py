"""A whole standardizer run, from a download folder to a Jellyfin library.

`movie_standardizer.py` is the tool that decides what a movie is *called* and
where it lives, and it is the first step of the pipeline — every later tool
works on the folders this one creates. Its parsing rules are heavily tested and
its deleting code has a suite of its own; the run itself was not tested at all.
Batch scanning a download folder, hardlinking a release into place, declining
what it will not touch, writing the report and the manifest, and choosing an
exit code: all of it needs a source tree, a target tree, and a filesystem that
supports hardlinks, so none of it had ever been executed by a test.

These tests drive `main(argv)` against two real directories and then look at
what is on disk. The invariant behind most of them is that **ingest is
additive**: a release is hardlinked into the library, the download folder is
left exactly as it was, and nothing that already existed in the library is
overwritten on a maybe.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hermetic

import movie_standardizer as ms

BIG = 8 * 1024 * 1024
SMALL = 512 * 1024

SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Every man dies. Not every man really lives.\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "They may take our lives, but they'll never take our freedom.\n"
)

# Every MOVIE_STD_* setting a developer may have exported: a run reads them,
# so the suite has to start from a known-empty environment.
ENV_KEYS = (
    "MOVIE_STD_TARGET", "MOVIE_STD_SOURCE", "MOVIE_STD_LOG", "MOVIE_STD_MIN_SIZE",
    "MOVIE_STD_REPORT", "MOVIE_STD_LOCK_TIMEOUT", "MOVIE_STD_FFPROBE",
    "MOVIE_STD_DEDUPLICATE", "MOVIE_STD_MAINTENANCE_MODE", "MOVIE_STD_QUARANTINE",
    "MOVIE_STD_MANIFEST", "MOVIE_STD_DRY_RUN",
)


def write_video(path: Path, size: int = BIG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)
    return path


def write_srt(path: Path, text: str = SRT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class StandardizerRunFixture(unittest.TestCase):
    """A download folder, an empty library, and one run of the real CLI."""

    def setUp(self) -> None:
        self._saved = (ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS)
        self._td = tempfile.TemporaryDirectory(prefix="ms_e2e_")
        self.root = Path(self._td.name).resolve()
        self.source = self.root / "final"
        self.source.mkdir()
        self.library = self.root / "Movies"
        self.library.mkdir()
        self.out = self.root / "out"
        self.log = self.out / "standardizer.log"
        self.report = self.out / "report.txt"
        self.manifest = self.out / "manifest.json"
        # setup_logging() installs a stdout handler on every run; keep the real
        # one so it can be put back when the test is done.
        self._logging = (ms.LOG.handlers[:], ms.LOG.propagate, ms.LOG.level)
        self._env = mock.patch.dict(os.environ, dict.fromkeys(ENV_KEYS, ""), clear=False)
        self._env.start()
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for handler in ms.LOG.handlers[:]:
            ms.LOG.removeHandler(handler)
            with contextlib.suppress(OSError):
                handler.close()
        ms.LOG.handlers, ms.LOG.propagate, ms.LOG.level = self._logging
        self._env.stop()
        ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS = self._saved
        self._td.cleanup()

    # -- running -----------------------------------------------------------

    def run_main(self, *extra: str, paths: tuple[str, ...] = ()) -> int:
        argv = ["--source", str(self.source), "--target", str(self.library),
                "--log", str(self.log), "--report", str(self.report),
                "--min-size", "1", *extra, *paths]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = ms.main(argv)
        self.stdout = out.getvalue()
        return code

    # -- looking at the result ---------------------------------------------

    def library_tree(self) -> list[str]:
        return sorted(p.relative_to(self.library).as_posix()
                      for p in self.library.rglob("*") if p.is_file())

    def source_tree(self) -> list[str]:
        return sorted(p.relative_to(self.source).as_posix()
                      for p in self.source.rglob("*") if p.is_file())

    def report_text(self) -> str:
        return self.report.read_text(encoding="utf-8") if self.report.exists() else ""

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def reasons(self) -> str:
        return " | ".join(event.get("reason", "") for event in ms.RUN_EVENTS)

    def release(self, folder: str, filename: str, size: int = BIG) -> Path:
        return write_video(self.source / folder / filename, size)


class HardlinkIngestTests(StandardizerRunFixture):
    """The happy path, and the invariant underneath it."""

    def setUp(self) -> None:
        super().setUp()
        self.src = self.release("The.Great.Escape.1963.1080p.BluRay.x264-GRP",
                                "the.great.escape.1963.1080p.bluray.x264-grp.mkv")

    def test_a_scene_release_becomes_a_canonical_folder_and_filename(self) -> None:
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.library_tree(),
                         ["The Great Escape (1963)/The Great Escape (1963).mkv"])

    def test_the_movie_is_hardlinked_and_the_download_is_untouched(self) -> None:
        """The seeding copy and the library copy are one file, not two."""
        before = self.source_tree()
        self.assertEqual(self.run_main(), 0)
        placed = self.library / "The Great Escape (1963)" / "The Great Escape (1963).mkv"
        self.assertTrue(placed.samefile(self.src), "hardlink, not copy")
        self.assertEqual(placed.stat().st_nlink, 2)
        self.assertEqual(self.source_tree(), before, "nothing left the download folder")

    def test_running_it_twice_changes_nothing(self) -> None:
        self.assertEqual(self.run_main(), 0)
        first = self.library_tree()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.library_tree(), first)
        self.assertIn("already in place", self.reasons())

    def test_an_english_sidecar_is_placed_under_the_canonical_name(self) -> None:
        write_srt(self.src.with_suffix(".eng.srt"))
        self.assertEqual(self.run_main(), 0)
        self.assertIn("The Great Escape (1963)/The Great Escape (1963).eng.srt",
                      self.library_tree())

    def test_two_valid_english_sidecars_are_left_for_a_human(self) -> None:
        """Picking one at random would put an unknown subtitle on the movie."""
        loose = write_video(self.source / "Rififi.1955.1080p.BluRay.x264-GRP.mkv")
        write_srt(loose.with_suffix(".eng.srt"))
        write_srt(loose.with_suffix(".en.srt"))
        self.assertEqual(self.run_main(), 0)
        self.assertIn("Rififi (1955)/Rififi (1955).mkv", self.library_tree())
        self.assertNotIn("Rififi (1955)/Rififi (1955).eng.srt", self.library_tree())
        self.assertIn("2 valid normal English SRT candidates", self.reasons())
        self.assertIn("subtitle ambiguity", self.report_text())

    def test_a_foreign_sidecar_is_not_placed(self) -> None:
        write_srt(self.src.parent / "The.Great.Escape.1963.spa.srt")
        self.assertEqual(self.run_main(), 0)
        self.assertNotIn("The Great Escape (1963)/The Great Escape (1963).eng.srt",
                         self.library_tree())

    def test_the_biggest_file_in_the_folder_is_the_movie(self) -> None:
        write_video(self.src.parent / "sample.mkv", SMALL)
        write_video(self.src.parent / "The.Great.Escape.1963.proof.mkv", 2 * 1024 * 1024)
        self.assertEqual(self.run_main(), 0)
        placed = self.library / "The Great Escape (1963)" / "The Great Escape (1963).mkv"
        self.assertTrue(placed.samefile(self.src))

    def test_a_box_set_folder_yields_one_folder_per_movie(self) -> None:
        folder = self.source / "Nolan Collection"
        write_video(folder / "Memento.2000.1080p.BluRay.x264.mkv")
        write_video(folder / "Insomnia.2002.1080p.BluRay.x264.mkv")
        self.assertEqual(self.run_main(), 0)
        self.assertIn("Memento (2000)/Memento (2000).mkv", self.library_tree())
        self.assertIn("Insomnia (2002)/Insomnia (2002).mkv", self.library_tree())


class WhatItRefusesToIngestTests(StandardizerRunFixture):
    """Everything the run leaves in the download folder, and says so."""

    def ingest_nothing(self, *extra: str) -> str:
        self.assertEqual(self.run_main(*extra), 0)
        self.assertEqual(self.library_tree(), [], "nothing was placed")
        return self.reasons()

    def test_a_tv_episode_is_not_a_movie(self) -> None:
        self.release("Some.Show.S01E02.1080p.WEB-DL", "Some.Show.S01E02.1080p.mkv")
        self.assertIn("looks like a TV show, not a movie", self.ingest_nothing())

    def test_a_loose_tv_episode_file_is_not_a_movie(self) -> None:
        write_video(self.source / "Some.Show.S01E02.1080p.WEB-DL.mkv")
        self.assertIn("looks like a TV episode, not a movie", self.ingest_nothing())

    def test_a_tv_category_skips_the_whole_run(self) -> None:
        """qBittorrent knows what it downloaded; believe it before parsing."""
        self.release("Movie.Night.2019.1080p", "Movie.Night.2019.1080p.mkv")
        self.assertEqual(self.run_main("--category", "tv-sonarr"), 0)
        self.assertEqual(self.library_tree(), [])

    def test_this_tool_never_transcodes(self) -> None:
        write_video(self.source / "Some.Movie.2019.1080p" / "Some.Movie.2019.avi")
        self.assertIn("never transcodes", self.ingest_nothing())

    def test_an_undersized_file_is_not_a_feature(self) -> None:
        self.release("Tiny.Movie.2019.1080p", "Tiny.Movie.2019.1080p.mkv", size=SMALL)
        self.assertIn("minimum", self.ingest_nothing())

    def test_a_multipart_release_is_not_one_complete_mkv(self) -> None:
        folder = self.source / "Long.Movie.1998.1080p"
        write_video(folder / "Long.Movie.1998.part1.mkv")
        write_video(folder / "Long.Movie.1998.part2.mkv")
        self.assertIn("multipart", self.ingest_nothing())

    def test_a_disc_rip_is_not_one_complete_mkv(self) -> None:
        write_video(self.source / "Old.Movie.1975" / "BDMV" / "STREAM" / "00001.m2ts")
        self.assertIn("disc structure", self.ingest_nothing())

    def test_a_folder_with_nothing_placeable_says_why(self) -> None:
        (self.source / "Empty.Movie.2019").mkdir()
        self.assertTrue(self.ingest_nothing())

    def test_an_incomplete_download_is_left_alone_and_not_reported(self) -> None:
        """A `.!qb` part file will disappear by itself; it is not a leftover."""
        write_video(self.source / "Movie.2019.1080p.mkv.!qB")
        self.assertEqual(self.ingest_nothing(), "")

    def test_the_file_handler_refuses_an_incomplete_download_too(self) -> None:
        """The batch scan filters these out; the placement path checks again.

        Two guards, because the two entry points are independent: a `.part`
        file that reached `handle_single_file` from anywhere is still a
        download in progress, and reporting it would fill the report with
        items that vanish on their own.
        """
        self.run_main()  # establish CFG the way a real run does
        partial = write_video(self.source / "Movie.2019.1080p.mkv.part")
        ms.RUN_EVENTS.clear()
        ms.handle_single_file(partial)
        self.assertEqual(self.reasons(), "")
        self.assertEqual(self.library_tree(), [])

    def test_a_symlinked_input_is_refused(self) -> None:
        real = write_video(self.root / "elsewhere" / "Movie.2019.1080p.mkv")
        link = self.source / "Movie.2019.1080p.mkv"
        try:
            link.symlink_to(real)
        except OSError:  # pragma: no cover - unprivileged Windows
            self.skipTest("symlinks are not available here")
        self.assertIn("symlink", self.ingest_nothing())

    def test_the_declines_are_named_in_the_report(self) -> None:
        self.release("Tiny.Movie.2019.1080p", "Tiny.Movie.2019.1080p.mkv", size=SMALL)
        self.assertEqual(self.run_main(), 0)
        text = self.report_text()
        self.assertIn("ITEMS LEFT IN SOURCE", text)
        self.assertIn("Tiny.Movie.2019.1080p", text)
        self.assertIn("smaller than the 1 MB minimum", text)


class ExistingLibraryTests(hermetic.HermeticToolsMixin, StandardizerRunFixture):
    """An occupied destination is the one place ingest could destroy data."""

    def setUp(self) -> None:
        super().setUp()
        self.existing = write_video(
            self.library / "The Great Escape (1963)" / "The Great Escape (1963).mkv",
            BIG // 2)
        self.incoming = self.release("The.Great.Escape.1963.2160p.BluRay.x265-GRP",
                                     "the.great.escape.1963.2160p.x265-grp.mkv")

    def test_an_unprovable_upgrade_keeps_the_existing_movie(self) -> None:
        """Without a working ffprobe there is no evidence, so nothing is replaced."""
        before = self.existing.stat()
        self.assertEqual(self.run_main("--ffprobe", str(self.root / "nope")), 0)
        after = self.existing.stat()
        self.assertEqual((after.st_size, after.st_ino), (before.st_size, before.st_ino))
        self.assertFalse(self.existing.samefile(self.incoming))

    def test_the_refusal_is_reported_rather_than_silent(self) -> None:
        self.assertEqual(self.run_main("--ffprobe", str(self.root / "nope")), 0)
        self.assertIn("conflict", self.reasons().lower() + self.report_text().lower())

    def test_an_existing_sidecar_is_never_overwritten(self) -> None:
        canonical = self.existing.with_name("The Great Escape (1963).eng.srt")
        write_srt(canonical, "1\n00:00:01,000 --> 00:00:02,000\nthe good one\n")
        write_srt(self.incoming.with_suffix(".eng.srt"))
        self.assertEqual(self.run_main("--ffprobe", str(self.root / "nope")), 0)
        self.assertIn("the good one", canonical.read_text(encoding="utf-8"))


class DryRunTests(StandardizerRunFixture):
    """A rehearsal has to be readable and has to write nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.release("Heat.1995.1080p.BluRay.x264-GRP", "Heat.1995.1080p.x264-grp.mkv")

    def test_nothing_is_placed(self) -> None:
        self.assertEqual(self.run_main("--dry-run"), 0)
        self.assertEqual(self.library_tree(), [])

    def test_what_would_have_happened_is_still_reported(self) -> None:
        self.assertEqual(self.run_main("--dry-run"), 0)
        self.assertIn("DRY-RUN", self.report_text())
        # The ledger's path column elides from the left, and a macOS temp
        # directory is 50 characters before the library even begins, so the
        # movie's name is asserted where it is never elided: the log.
        self.assertIn("Heat (1995)", self.log_text())
        self.assertIn("dry run", self.reasons())


class AutomatedInputTests(StandardizerRunFixture):
    """The qBittorrent hook: one path on the command line, not a scan."""

    def test_one_named_folder_is_organized(self) -> None:
        folder = self.source / "Alien.1979.1080p.BluRay.x264-GRP"
        write_video(folder / "Alien.1979.1080p.BluRay.x264-GRP.mkv")
        other = self.source / "Aliens.1986.1080p.BluRay.x264-GRP"
        write_video(other / "Aliens.1986.1080p.BluRay.x264-GRP.mkv")
        self.assertEqual(self.run_main(paths=(str(folder),)), 0)
        self.assertEqual(self.library_tree(), ["Alien (1979)/Alien (1979).mkv"],
                         "the rest of the download folder was not swept up")

    def test_a_path_inside_the_library_is_refused(self) -> None:
        """Otherwise the tool re-ingests its own output."""
        placed = write_video(self.library / "Alien (1979)" / "Alien (1979).mkv")
        self.assertEqual(self.run_main(paths=(str(placed.parent),)), 1)
        self.assertIn("inside the organized library", self.reasons())

    def test_a_path_that_does_not_exist_is_a_failure(self) -> None:
        self.assertEqual(self.run_main(paths=(str(self.source / "gone"),)), 1)
        self.assertIn("does not exist", self.reasons())

    def test_the_save_path_and_torrent_name_form_is_understood(self) -> None:
        """qBittorrent can hand over `%D %N` instead of `%F`."""
        folder = self.source / "Alien.1979.1080p.BluRay.x264-GRP"
        write_video(folder / "Alien.1979.1080p.BluRay.x264-GRP.mkv")
        self.assertEqual(self.run_main(paths=(str(self.source), folder.name)), 0)
        self.assertEqual(self.library_tree(), ["Alien (1979)/Alien (1979).mkv"])

    def test_a_named_movie_is_still_deduplicated_afterwards(self) -> None:
        """The hook run maintains the library it just wrote into."""
        write_video(self.library / "Alien (1979)" / "Alien (1979).mkv", BIG)
        write_video(self.library / "Alien 1979" / "Alien 1979.mkv", BIG // 2)
        folder = self.source / "Heat.1995.1080p.BluRay.x264-GRP"
        write_video(folder / "Heat.1995.1080p.BluRay.x264-GRP.mkv")
        self.assertEqual(self.run_main("--deduplicate", "--maintenance-mode", "DELETE",
                                       paths=(str(folder),)), 0)
        self.assertEqual(self.library_tree(),
                         ["Alien (1979)/Alien (1979).mkv", "Heat (1995)/Heat (1995).mkv"])


class BatchScanTests(StandardizerRunFixture):
    """Sweeping the download folder, including when it is not there."""

    def test_a_missing_download_folder_is_a_failed_run_not_a_crash(self) -> None:
        code = self.run_main("--source", str(self.root / "not-here"))
        self.assertEqual(code, 1)
        self.assertIn("source folder does not exist", self.reasons())

    def test_a_download_folder_that_cannot_be_listed_is_reported(self) -> None:
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            self.assertEqual(self.run_main(), 1)
        self.assertIn("denied", self.reasons())

    def test_every_release_in_the_folder_is_considered(self) -> None:
        self.release("Heat.1995.1080p.BluRay.x264-GRP", "Heat.1995.1080p.x264-grp.mkv")
        self.release("Alien.1979.1080p.BluRay.x264-GRP", "Alien.1979.1080p.x264-grp.mkv")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.library_tree(),
                         ["Alien (1979)/Alien (1979).mkv", "Heat (1995)/Heat (1995).mkv"])


class ConfigurationRefusalTests(StandardizerRunFixture):
    """Settings that would corrupt the library or feed the tool its own output."""

    def refuse(self, *extra: str) -> str:
        self.assertEqual(self.run_main(*extra), 2)
        return self.report_text() + self.reasons()

    def test_the_library_may_not_sit_inside_the_download_folder(self) -> None:
        nested = self.source / "Movies"
        nested.mkdir()
        self.assertIn("nested", self.refuse("--target", str(nested)))

    def test_source_and_target_may_not_be_the_same_directory(self) -> None:
        self.assertIn("different directories", self.refuse("--target", str(self.source)))

    def test_the_report_may_not_be_written_into_the_library(self) -> None:
        said = self.refuse("--report", str(self.library / "report.txt"))
        self.assertIn("outside --target", said)

    def test_the_log_may_not_be_written_into_the_download_folder(self) -> None:
        said = self.refuse("--log", str(self.source / "run.log"))
        self.assertIn("outside --source", said)

    def test_the_manifest_may_not_be_written_into_the_library(self) -> None:
        said = self.refuse("--manifest", str(self.library / "manifest.json"))
        self.assertIn("outside --target", said)

    def test_quarantine_mode_without_a_destination_is_refused(self) -> None:
        said = self.refuse("--deduplicate", "--maintenance-mode", "QUARANTINE")
        self.assertIn("requires --quarantine-dir", said)

    def test_the_quarantine_may_not_be_inside_the_library(self) -> None:
        said = self.refuse("--deduplicate", "--maintenance-mode", "QUARANTINE",
                           "--quarantine-dir", str(self.library / "quarantine"))
        self.assertIn("outside the organized library", said)

    def test_a_refused_run_places_nothing(self) -> None:
        self.release("Heat.1995.1080p.BluRay.x264-GRP", "Heat.1995.1080p.x264-grp.mkv")
        self.refuse("--target", str(self.source))
        self.assertEqual(self.library_tree(), [])


class DeduplicationWiringTests(StandardizerRunFixture):
    """Deduplication runs after the ingest, and only when asked."""

    def setUp(self) -> None:
        super().setUp()
        write_video(self.library / "Heat (1995)" / "Heat (1995).mkv", BIG)
        write_video(self.library / "Heat 1995" / "Heat 1995.mkv", BIG // 2)

    def test_it_is_off_unless_it_is_asked_for(self) -> None:
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.library_tree()), 2)

    def test_report_mode_names_the_duplicate_without_touching_it(self) -> None:
        self.assertEqual(self.run_main("--deduplicate"), 0)
        self.assertEqual(len(self.library_tree()), 2, "REPORT mode deletes nothing")
        self.assertIn("smaller duplicate folder", self.report_text())
        self.assertIn("duplicate identity", self.reasons())

    def test_delete_mode_removes_the_lesser_copy(self) -> None:
        self.assertEqual(self.run_main("--deduplicate", "--maintenance-mode", "DELETE"), 0)
        self.assertEqual(self.library_tree(), ["Heat (1995)/Heat (1995).mkv"])


class ManifestTests(StandardizerRunFixture):
    """The machine-readable record of what a run did."""

    def test_the_manifest_counts_match_the_events(self) -> None:
        self.release("Heat.1995.1080p.BluRay.x264-GRP", "Heat.1995.1080p.x264-grp.mkv")
        self.release("Tiny.Movie.2019.1080p", "Tiny.Movie.2019.1080p.mkv", size=SMALL)
        self.assertEqual(self.run_main("--manifest", str(self.manifest)), 0)
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["completed"], 1)
        self.assertEqual(payload["summary"]["skipped"], 1)
        self.assertEqual(len(payload["events"]), 2)
        self.assertEqual(payload["version"], ms.__version__)

    def test_no_manifest_is_written_unless_one_is_asked_for(self) -> None:
        self.release("Heat.1995.1080p.BluRay.x264-GRP", "Heat.1995.1080p.x264-grp.mkv")
        self.assertEqual(self.run_main(), 0)
        self.assertFalse(self.manifest.exists())


class RunEndingTests(StandardizerRunFixture):
    """How a run ends when something outside it goes wrong."""

    def setUp(self) -> None:
        super().setUp()
        self.release("Heat.1995.1080p.BluRay.x264-GRP", "Heat.1995.1080p.x264-grp.mkv")

    def test_another_organizer_holding_the_lock_is_not_a_crash(self) -> None:
        with mock.patch.object(ms, "CoordinationLock",
                               side_effect=ms.LockTimeoutError("held by pid 1234")):
            self.assertEqual(self.run_main(), 1)
        self.assertIn("held by pid 1234", self.reasons())
        self.assertEqual(self.library_tree(), [])

    def test_the_report_is_written_even_when_the_run_fails(self) -> None:
        with mock.patch.object(ms, "CoordinationLock",
                               side_effect=ms.LockTimeoutError("held")):
            self.run_main()
        self.assertTrue(self.report.is_file())

    def test_ctrl_c_exits_130(self) -> None:
        with mock.patch.object(ms, "batch_scan", side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_main(), 130)

    def test_an_unexpected_crash_leaves_through_an_exit_code(self) -> None:
        with mock.patch.object(ms, "batch_scan", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_main(), 1)

    def test_a_failure_during_ingest_is_reported_as_a_failed_run(self) -> None:
        """Exit 1 is what tells the operator to read the report."""
        with mock.patch.object(ms, "_create_hardlink",
                               side_effect=OSError("Invalid cross-device link")):
            self.assertEqual(self.run_main(), 1)
        self.assertIn("cross-device", self.reasons())
        self.assertEqual(self.library_tree(), [], "no partial file was left behind")


class SelfTestTests(hermetic.HermeticToolsMixin, StandardizerRunFixture):
    """`--self-test` is the field check on a machine with no test suite."""

    def test_it_passes_on_a_healthy_copy(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ms.main(["--self-test"])
        self.assertEqual(code, 0)
        self.assertIn("PASSED", out.getvalue())

    def test_it_notices_a_parser_that_stopped_finding_years(self) -> None:
        out = io.StringIO()
        with mock.patch.object(ms, "parse_movie_name",
                               return_value=ms.ParsedName(title="", year=None, raw="")), \
                contextlib.redirect_stdout(out):
            code = ms.main(["--self-test"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out.getvalue())


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
