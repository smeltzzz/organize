"""``movie_standardizer.py`` when the filesystem answers badly, and when it must not act.

This is the tool that moves files, so every ``except OSError`` here is a
decision about whether to act on information it does not have. Each of these
tests pins the conservative side of that decision:

* a name token is only peeled when the rules prove it is a tag - a bare number
  stays in a title, and a weak token like "WEB" is never peeled on its own;
* a path that cannot be ``stat``ed is skipped, never guessed at, and a special
  file in a download folder is reported rather than ingested;
* a dry run stages nothing, not even a temporary file;
* when ``samefile()`` cannot answer, a duplicate is **not** assumed to be the
  same inode: the size-margin rule applies and an ambiguous pair is kept;
* a rollback that itself fails is logged, the outcome is recorded as failed, and
  the old container is still on disk;
* a log handler that cannot be closed does not stop the run.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_standardizer_e2e import StandardizerRunFixture, write_video

import movie_standardizer as ms

WINDOWS = os.name == "nt"


class FaultyPath(type(Path())):
    """A ``Path`` whose reads fail on demand, and whose children inherit it.

    ``iterdir()`` and ``with_name()`` hand back objects of the caller's own
    class, so a subclass can fail one specific read without patching ``Path`` for
    every tool in the same process.
    """

    fail_is_file: frozenset[str] = frozenset()

    def is_file(self) -> bool:
        if self.name in type(self).fail_is_file:
            raise OSError(5, "Input/output error", self.name)
        return super().is_file()


class TagTokenTests(unittest.TestCase):
    """Which trailing tokens are release tags, and which are part of a title."""

    def test_a_channel_count_is_a_tag(self) -> None:
        """``5ch``/``7ch`` are not in the token table; the shape rule has to catch them.

        A channel count left in a title becomes part of the movie's name in Jellyfin,
        so "Film 7ch" is a search nobody finds.
        """
        for token in ("6ch", "2ch", "5ch", "7ch", "0ch"):
            with self.subTest(token=token):
                self.assertTrue(ms._is_tag_token(token))

    def test_a_joined_audio_tag_is_a_tag(self) -> None:
        """``dd51`` and ``dts5.1`` are how a scene release spells Dolby 5.1."""
        for token in ("dd51", "ddp5.1", "dts5.1", "truehd71", "eac320"):
            with self.subTest(token=token):
                self.assertTrue(ms._is_tag_token(token))

    def test_a_bare_number_is_never_a_tag(self) -> None:
        """2049, 500, 13 are titles: peeling them would rename the movie."""
        for token in ("2049", "500", "13", "1"):
            with self.subTest(token=token):
                self.assertFalse(ms._is_tag_token(token))

    def test_an_empty_token_is_a_tag_so_the_peel_can_finish(self) -> None:
        self.assertTrue(ms._is_tag_token(""))
        self.assertTrue(ms._is_tag_token("[]"))

    def test_a_weak_token_is_only_peeled_after_a_strong_one(self) -> None:
        """``WEB`` alone may be part of a title; after ``x264`` it is a tag.

        The tail is peeled from the end, and a weak token is only trusted once a
        strong tag has been seen - otherwise "Silk Web" or "Web of Spider" loses
        the word that names the film.
        """
        self.assertEqual(ms._strip_trailing_tags("Movie dd51 x264"), "Movie")
        self.assertEqual(ms._strip_trailing_tags("Silk Web"), "Silk Web")
        self.assertEqual(ms._strip_trailing_tags("Some Movie 1080p WEB"), "Some Movie 1080p WEB")

    def test_a_hyphenated_tag_the_peeler_leaves_alone_is_still_peeled(self) -> None:
        """``10-bit`` and ``blu-ray`` look hyphen-joined but are single tags.

        The peeler only splits a tail it recognises as ``tag-tag``; these are in the
        token table instead, so the tail walk has to take them itself - and once a
        strong tag like that is gone, a weak one behind it ("WEB") goes too.
        """
        for tail in ("10-bit", "blu-ray", "true-hd", "vc-1"):
            with self.subTest(tail=tail):
                self.assertEqual(ms._strip_trailing_tags(f"Movie {tail}"), "Movie")
        self.assertEqual(ms._strip_trailing_tags("Movie WEB 10-bit"), "Movie")

    def test_a_release_tail_comes_off_a_scene_name(self) -> None:
        parsed = ms.parse_movie_name("Some.Movie.1080p.WEB.x264-GRP")
        self.assertEqual(parsed.title, "Some Movie")
        self.assertEqual(parsed.resolution, "1080p")


class ScanTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="ms_scan_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        # The fixture-sized files in these tests are far below the 300 MB default
        # a real library uses; the floor is not what is under test here.
        patch = mock.patch.object(ms, "CFG", ms.Config(min_movie_size_mb=1))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_dangling_symlink_is_skipped_not_followed(self) -> None:
        """``os.link()`` follows symlinks, so one is never ingested - or stat'ed twice.

        A leftover link in a download folder whose target is gone must not stop the
        scan of the real files beside it.
        """
        release = self.root / "Movie.2001.1080p.x264-GRP"
        release.mkdir()
        video = release / "movie.2001.1080p.x264-grp.mkv"
        write_video(video, 8 * 1024 * 1024)
        (release / "gone.mkv").symlink_to(self.root / "nowhere" / "gone.mkv")
        # Torrent debris the scan must not classify, stat or ingest.
        (release / "movie.2001.1080p.x264-grp.mkv.!qb").write_bytes(b"partial")
        (release / "Thumbs.db").write_bytes(b"junk")

        scan = ms.scan_tree(release)

        self.assertEqual([f.path.name for f in scan.videos], [video.name])
        self.assertEqual([f.path.name for f in scan.files], [video.name],
                         "a partial download and Thumbs.db are not part of the release")
        self.assertFalse((self.root / "nowhere").exists(), "nothing was created for the link")

    def test_a_file_that_goes_mid_scan_is_skipped_not_fatal(self) -> None:
        """A torrent client deletes finished files while the batch is being read.

        The scan is thousands of stats; one vanished file must cost that file only,
        not the folder it sits in and not the run.
        """
        release = self.root / "Movie.2001.1080p.x264-GRP"
        release.mkdir()
        keeper = release / "movie.2001.1080p.x264-grp.mkv"
        write_video(keeper, 8 * 1024 * 1024)
        write_video(release / "vanished.mkv", 8 * 1024 * 1024)

        real_stat = Path.stat

        def stat_without_the_vanished_file(self: Path, *a: object, **kw: object):
            if Path(self).name == "vanished.mkv":
                raise OSError(2, "No such file or directory")
            return real_stat(self, *a, **kw)  # type: ignore[arg-type]

        with mock.patch.object(Path, "stat", stat_without_the_vanished_file):
            scan = ms.scan_tree(release)

        self.assertEqual([f.path.name for f in scan.videos], [keeper.name])


class MultiVersionFolderTests(unittest.TestCase):
    """Jellyfin's own multi-version layout, which deduplication must never collapse."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="ms_multiver_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        patch = mock.patch.object(ms, "CFG", ms.Config(jellyfin_mode=True, min_movie_size_mb=0))
        patch.start()
        self.addCleanup(patch.stop)

    def folder(self, name: str, *videos: str) -> Path:
        folder = self.root / name
        folder.mkdir()
        for video in videos:
            write_video(folder / video, 1024)
        return folder

    def test_a_folder_with_one_feature_is_not_a_multi_version_folder(self) -> None:
        folder = self.folder("Film (2020)", "Film (2020).mkv")
        self.assertFalse(ms._is_jellyfin_multi_version_folder(folder))

    def test_the_detection_is_switched_off_outside_jellyfin_mode(self) -> None:
        folder = self.folder("Film (2020)", "Film (2020) - 1080p.mkv", "Film (2020) - 2160p.mkv")
        with mock.patch.object(ms, "CFG", ms.Config(jellyfin_mode=False)):
            self.assertFalse(ms._is_jellyfin_multi_version_folder(folder))

    def test_an_extra_beside_the_feature_does_not_make_it_multi_version(self) -> None:
        folder = self.folder("Film (2020)", "Film (2020).mkv")
        write_video(folder / "Film (2020)-trailer.mkv", 1024)
        self.assertFalse(ms._is_jellyfin_multi_version_folder(folder))

    def test_two_features_in_one_folder_are_a_multi_version_set(self) -> None:
        """Jellyfin's own layout: collapsing it would delete a version somebody kept."""
        folder = self.folder("Film (2020)", "Film (2020) - 1080p.mkv", "Film (2020) - 2160p.mkv")
        self.assertTrue(ms._is_jellyfin_multi_version_folder(folder))


class SubtitleMatchTests(unittest.TestCase):
    def test_a_sidecar_named_exactly_like_its_video_is_matched(self) -> None:
        """The cheapest and surest match: same stem, same folder."""
        video = Path("/downloads/Movie.2001/movie.2001.mkv")
        parsed = ms.parse_movie_name("movie.2001")
        subs = [ms.ScannedFile(Path("/downloads/Movie.2001/movie.2001.srt"), 1024, "subtitle"),
                ms.ScannedFile(Path("/downloads/Movie.2001/movie.2001.eng.forced.srt"), 512,
                               "subtitle")]
        hits = ms.match_subtitles_for_video(video, parsed, subs, multi=True)
        self.assertEqual([hit.path.name for hit in hits],
                         ["movie.2001.srt", "movie.2001.eng.forced.srt"])

    def test_a_release_with_one_video_takes_every_sidecar_it_ships_with(self) -> None:
        """Nothing to disambiguate, so nothing is left behind in the download."""
        video = Path("/downloads/Movie.2001/movie.2001.mkv")
        parsed = ms.parse_movie_name("movie.2001")
        subs = [ms.ScannedFile(Path("/downloads/Movie.2001/whatever.srt"), 1024, "subtitle")]
        self.assertEqual(ms.match_subtitles_for_video(video, parsed, subs, multi=False), subs)

    def test_a_sidecar_in_a_folder_named_after_the_movie_is_matched(self) -> None:
        """Releases ship subtitles in ``Subs/`` under names that say nothing.

        The folder is the evidence then: a sidecar whose ancestor directory is named
        like this movie belongs to it, and leaving it behind means the ingest
        publishes a movie with no subtitle the extractor then has to fetch.
        """
        video = Path("/downloads/Movie.2001/movie.2001.mkv")
        parsed = ms.parse_movie_name("movie.2001")
        subs = [ms.ScannedFile(Path("/downloads/Movie.2001/Subs/english.srt"), 1024, "subtitle"),
                ms.ScannedFile(Path("/downloads/Other.1999/Subs/english.srt"), 1024, "subtitle")]
        hits = ms.match_subtitles_for_video(video, parsed, subs, multi=True)
        self.assertEqual([hit.path.as_posix() for hit in hits],
                         ["/downloads/Movie.2001/Subs/english.srt"],
                         "another movie's subtitles stay with that movie")


class DryRunReplaceTests(unittest.TestCase):
    def test_the_write_helper_refuses_to_produce_anything_in_a_dry_run(self) -> None:
        """``_replace_with`` is the last thing between a decision and a written file.

        Its only caller today checks ``CFG.dry_run`` before it gets here, so this is
        the guard behind the guard: a future caller that forgets still writes
        nothing. A preview that staged a temporary file would leave debris in the
        library, and a preview that published one would not be a preview.
        """
        with tempfile.TemporaryDirectory(prefix="ms_dryrun_") as td:
            dest = Path(td) / "Movies" / "Film (2020)" / "Film (2020).mkv"
            with mock.patch.object(ms, "CFG", ms.Config(dry_run=True)), \
                    self.assertLogs(ms.LOG, level="INFO") as captured:
                ok = ms._replace_with("hardlink /src/film.mkv", dest,
                                      lambda tmp: self.fail("a dry run must not produce"))
            self.assertTrue(ok)
            self.assertFalse(dest.exists(), "no movie was published")
            self.assertFalse(dest.parent.exists(), "a preview does not even build the folder")
            self.assertEqual(list(Path(td).rglob("*")), [],
                             "no staging file, no folder: nothing at all was written")
            self.assertIn("[DRY-RUN]", "\n".join(captured.output))


class RivalContainerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="ms_rival_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.folder = self.root / "Film (2020)"
        self.folder.mkdir()
        FaultyPath.fail_is_file = frozenset()

    def test_an_unreadable_sibling_is_not_mistaken_for_a_rival_to_delete(self) -> None:
        """The rival is the old container this run is allowed to remove.

        Guessing at a file that cannot be read would mean unlinking something the
        tool could not identify, so an unreadable sibling is skipped and the answer
        is "no rival" - the placement still happens and that file is untouched.
        """
        dest = FaultyPath(self.folder / "Film (2020).mkv")
        unreadable = self.folder / "Film (2020).mp4"
        unreadable.write_bytes(b"x" * 1024)
        FaultyPath.fail_is_file = frozenset({"Film (2020).mp4"})

        rival = ms.existing_other_container(dest)

        self.assertIsNone(rival)
        self.assertTrue(unreadable.is_file(), "the unreadable sibling was not removed")

    def test_a_readable_rival_is_still_found(self) -> None:
        dest = FaultyPath(self.folder / "Film (2020).mkv")
        rival = self.folder / "Film (2020).mp4"
        rival.write_bytes(b"x" * 1024)
        self.assertEqual(ms.existing_other_container(dest), rival)


class LoggingSetupTests(unittest.TestCase):
    def test_a_handler_that_cannot_be_closed_does_not_stop_the_run(self) -> None:
        """A log on a share that went away must not take the ingest with it."""
        saved = (ms.LOG.handlers[:], ms.LOG.propagate, ms.LOG.level)
        self.addCleanup(self._restore, saved)
        stubborn = mock.Mock(spec=logging.Handler)
        stubborn.close.side_effect = OSError(5, "Input/output error")
        ms.LOG.addHandler(stubborn)

        ms.setup_logging(ms.Config())

        stubborn.close.assert_called_once_with()
        self.assertNotIn(stubborn, ms.LOG.handlers)
        self.assertTrue(ms.LOG.handlers, "the run still has somewhere to log")

    @staticmethod
    def _restore(saved: tuple) -> None:
        for handler in ms.LOG.handlers[:]:
            ms.LOG.removeHandler(handler)
        ms.LOG.handlers, ms.LOG.propagate, ms.LOG.level = saved


class RunFaultTests(StandardizerRunFixture):
    """A whole run with broken things in the download folder and in the library."""

    def test_special_files_and_dead_links_are_reported_and_the_run_continues(self) -> None:
        """A FIFO or a leftover symlink is not a movie, and must not stop the batch.

        ``batch_scan`` orders items by mtime first: an item that cannot be stat'ed
        sorts as the oldest rather than raising, and ``handle_item`` then reports
        what it found instead of ingesting it.
        """
        self.release("The.Great.Escape.1963.1080p.BluRay.x264-GRP",
                     "the.great.escape.1963.1080p.bluray.x264-grp.mkv")
        (self.source / "gone.mkv").symlink_to(self.source / "nowhere" / "gone.mkv")
        vanished = self.source / "vanished.mkv"
        vanished.write_bytes(b"x" * 1024)
        if not WINDOWS:
            os.mkfifo(self.source / "pipe.mkv")

        real_stat = Path.stat

        def stat_without_the_vanished_item(self: Path, *a: object, **kw: object):
            # The item is listed, then goes: a torrent client cleaning up behind
            # itself while the batch is being ordered.
            if Path(self).name == "vanished.mkv":
                raise OSError(2, "No such file or directory")
            return real_stat(self, *a, **kw)  # type: ignore[arg-type]

        with mock.patch.object(Path, "stat", stat_without_the_vanished_item):
            code = self.run_main()

        self.assertEqual(code, 0)
        logged = self.log_text()
        self.assertIn("Skipping symlinked input", logged)
        self.assertIn("Path does not exist", logged)
        if not WINDOWS:
            self.assertIn("Not a file or directory", logged)
        self.assertIn("The Great Escape (1963)/The Great Escape (1963).mkv", self.library_tree())

    def test_an_unprovable_duplicate_is_kept_rather_than_deleted(self) -> None:
        """``samefile()`` failing means "unknown", not "different, so delete it".

        Two hardlinked copies of one movie are safe to collapse: same inode, same
        bytes. When the question cannot be answered the tool falls back to the size
        margin, and a pair within it is left for a human - because the alternative
        is deleting a movie on the strength of a filesystem error.
        """
        keep = self.library / "Film (2020)"
        keep.mkdir()
        keeper = keep / "Film (2020).mkv"
        write_video(keeper, 8 * 1024 * 1024)
        dupe = self.library / "Film 2020"
        dupe.mkdir()
        duplicate = dupe / "Film 2020.mkv"
        os.link(keeper, duplicate)  # genuinely the same inode
        self.assertEqual(keeper.stat().st_ino, duplicate.stat().st_ino)

        real_samefile = Path.samefile

        def broken_samefile(self: Path, other: object) -> bool:
            if str(self).endswith(".mkv") and str(other).endswith(".mkv"):
                raise OSError(5, "Input/output error")
            return real_samefile(self, other)  # type: ignore[arg-type]

        with mock.patch.object(Path, "samefile", broken_samefile):
            code = self.run_main("--deduplicate", "--maintenance-mode", "DELETE")

        self.assertEqual(code, 0)
        self.assertIn("Leaving ambiguous duplicate", self.log_text())
        self.assertTrue(duplicate.is_file(), "the unprovable duplicate survived")
        self.assertTrue(keeper.is_file())

    def test_a_tv_shaped_folder_is_never_grouped_as_a_movie_duplicate(self) -> None:
        """Deduplication deletes; a TV folder is not this tool's to judge.

        ``--allow-tv`` decides whether TV is *ingested*, but the duplicate scan runs
        over whatever is already in the library, and grouping an episode folder with
        a movie of a similar name would put a DELETE mode to work on data the tool
        was never asked about.
        """
        show = self.library / "Show S01E02"
        show.mkdir()
        write_video(show / "Show S01E02.mkv", 8 * 1024 * 1024)
        film = self.library / "Film (2020)"
        film.mkdir()
        write_video(film / "Film (2020).mkv", 8 * 1024 * 1024)

        code = self.run_main("--deduplicate", "--maintenance-mode", "DELETE")

        self.assertEqual(code, 0)
        self.assertTrue((show / "Show S01E02.mkv").is_file(), "the episode was not touched")
        self.assertTrue((film / "Film (2020).mkv").is_file())
        self.assertNotIn("Duplicate group", self.log_text())

    def test_a_flat_library_leaves_a_lone_file_and_a_tv_shaped_file_alone(self) -> None:
        """The same two refusals on the no-subfolders layout."""
        lone = self.library / "Solo (2011).mkv"
        write_video(lone, 8 * 1024 * 1024)
        episode = self.library / "Show.S01E02.mkv"
        write_video(episode, 8 * 1024 * 1024)

        real_cfg_from_args = ms.cfg_from_args

        def flat_config(args: object) -> ms.Config:
            return dataclasses.replace(real_cfg_from_args(args), create_subfolders=False)

        with mock.patch.object(ms, "cfg_from_args", flat_config):
            code = self.run_main("--deduplicate", "--maintenance-mode", "DELETE")

        self.assertEqual(code, 0)
        self.assertTrue(lone.is_file(), "a file with no twin is not a duplicate")
        self.assertTrue(episode.is_file(), "and an episode is not a movie duplicate")
        self.assertNotIn("Duplicate files", self.log_text())

    def test_the_same_pair_is_collapsed_when_the_inode_check_answers(self) -> None:
        """The contrast the test above depends on: a proven hardlink is removed."""
        keep = self.library / "Film (2020)"
        keep.mkdir()
        keeper = keep / "Film (2020).mkv"
        write_video(keeper, 8 * 1024 * 1024)
        dupe = self.library / "Film 2020"
        dupe.mkdir()
        duplicate = dupe / "Film 2020.mkv"
        os.link(keeper, duplicate)

        code = self.run_main("--deduplicate", "--maintenance-mode", "DELETE")

        self.assertEqual(code, 0)
        self.assertFalse(duplicate.exists(), "a proven same-inode duplicate is safe to drop")
        self.assertTrue(keeper.is_file())

    def test_a_rollback_that_fails_is_logged_and_leaves_the_old_container_alone(self) -> None:
        """Container replacement verifies before it removes; if the undo fails, say so.

        The invariant is that a failed replacement never leaves the library with no
        movie: the old MP4 stays, the outcome is recorded as failed, and the log
        names the file that could not be rolled back.
        """
        src = self.release("Film.2020.1080p.x264-GRP", "film.2020.1080p.x264-grp.mkv")
        folder = self.library / "Film (2020)"
        folder.mkdir(parents=True)
        old = folder / "Film (2020).mp4"
        write_video(old, 4 * 1024 * 1024)
        dest = folder / "Film (2020).mkv"

        real_samefile = Path.samefile
        real_unlink = Path.unlink

        def verification_fails(self: Path, other: object) -> bool:
            # Only the post-placement verification is broken: the rollback still has
            # to be able to prove dest and src are the same file before it unlinks.
            if Path(self) == src and Path(other) == dest:
                return False
            return real_samefile(self, other)  # type: ignore[arg-type]

        def refusing_unlink(self: Path, *args: object, **kwargs: object) -> None:
            if Path(self) == dest:
                raise OSError(16, "Device or resource busy")
            return real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(ms, "CFG", ms.Config(target_dir=self.library,
                                                    source_dir=self.source)), \
                mock.patch.object(Path, "samefile", verification_fails), \
                mock.patch.object(Path, "unlink", refusing_unlink), \
                self.assertLogs(ms.LOG, level="ERROR") as captured:
            ok = ms.process_file_action(src, dest)

        logged = "\n".join(captured.output)
        self.assertFalse(ok)
        self.assertIn("Could not roll back new container", logged)
        self.assertIn("failed", logged)
        self.assertTrue(old.is_file(), "the container being replaced is still there")
        self.assertIn("failed", " ".join(str(event.get("status", "")) for event in ms.RUN_EVENTS))

    def test_the_library_sweep_runs_before_the_ingest_when_it_is_switched_on(self) -> None:
        """``run_cleanup_on_target`` is a Config field: prove the run honours it.

        The sweep has to happen before the batch, or extras that this very run
        ingested would be judged by the next one.
        """
        extras = self.library / "Film (2020)" / "Extras"
        extras.mkdir(parents=True)
        write_video(extras / "making-of.mkv", 1024)
        self.release("The.Great.Escape.1963.1080p.BluRay.x264-GRP",
                     "the.great.escape.1963.1080p.bluray.x264-grp.mkv")
        real_cfg_from_args = ms.cfg_from_args
        with mock.patch.object(ms, "cfg_from_args",
                               lambda args: dataclasses.replace(
                                   real_cfg_from_args(args), run_cleanup_on_target=True)):
            code = self.run_main("--maintenance-mode", "DELETE")
        self.assertEqual(code, 0)
        self.assertIn("Target extras cleanup", self.log_text())
        self.assertNotIn("Film (2020)/Extras/making-of.mkv", self.library_tree())
        self.assertIn("The Great Escape (1963)/The Great Escape (1963).mkv", self.library_tree())

    def test_a_flat_library_deduplicates_files_and_keeps_an_unprovable_pair(self) -> None:
        """The same conservative rule on the no-subfolders layout."""
        keeper = self.library / "Film (2020).mkv"
        write_video(keeper, 8 * 1024 * 1024)
        duplicate = self.library / "Film 2020.mkv"
        os.link(keeper, duplicate)

        real_cfg_from_args = ms.cfg_from_args

        def flat_config(args: object) -> ms.Config:
            return dataclasses.replace(real_cfg_from_args(args), create_subfolders=False)

        real_samefile = Path.samefile

        def broken_samefile(self: Path, other: object) -> bool:
            if str(self).endswith(".mkv") and str(other).endswith(".mkv"):
                raise OSError(5, "Input/output error")
            return real_samefile(self, other)  # type: ignore[arg-type]

        with mock.patch.object(ms, "cfg_from_args", flat_config), \
                mock.patch.object(Path, "samefile", broken_samefile):
            code = self.run_main("--deduplicate", "--maintenance-mode", "DELETE")

        self.assertEqual(code, 0)
        self.assertIn("Leaving ambiguous duplicate file", self.log_text())
        self.assertTrue(duplicate.is_file())
        self.assertTrue(keeper.is_file())


if __name__ == "__main__":
    unittest.main()
