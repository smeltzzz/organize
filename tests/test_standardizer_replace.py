"""Replacement of a previously organized movie by a matching new download.

The canonical movie name identifies the title, year and (when preserved in the
name) the edition. A new download with that identity wins regardless of file
size or technical quality. The seed stays untouched; the library gets a staged
hardlink. Unrelated titles, versions, sidecars and failed writes stay put.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import movie_standardizer as ms


class ReplacementFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS
        self._td = tempfile.TemporaryDirectory(prefix="ms_replace_")
        self.root = Path(self._td.name)
        ms.CFG = ms.Config(
            source_dir=self.root / "final", target_dir=self.root / "Movies",
            log_file=None, report_file=None,
        )
        ms.RUN_SUMMARY = ms.RunSummary()
        ms.RUN_EVENTS = []
        self._logging = ms.LOG.handlers[:], ms.LOG.propagate
        ms.LOG.handlers = [logging.NullHandler()]
        ms.LOG.propagate = False
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for handler in ms.LOG.handlers[:]:
            ms.LOG.removeHandler(handler)
            with contextlib.suppress(OSError):
                handler.close()
        ms.LOG.handlers, ms.LOG.propagate = self._logging
        ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS = self._saved
        self._td.cleanup()

    def file(self, relative: str, content: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def pair(self, *, extension: str = ".mkv") -> tuple[Path, Path]:
        src = self.file(f"final/Film.2020.1080p.WEB/Film.2020.1080p.WEB{extension}", b"new")
        dest = self.file(f"Movies/Film (2020)/Film (2020){extension}", b"old movie")
        return src, dest


class ShouldReplaceTests(ReplacementFixture):
    def test_a_missing_movie_is_placed(self) -> None:
        src = self.file("final/Film.2020.mkv", b"new")
        self.assertEqual(ms.should_replace(src, self.root / "Movies/Film (2020)/Film (2020).mkv"),
                         (True, "missing"))

    def test_the_same_download_is_not_relinked(self) -> None:
        src, dest = self.pair()
        self.assertEqual(ms.should_replace(src, src), (False, "same-file"))
        dest.unlink()
        dest.hardlink_to(src)
        self.assertIn(ms.should_replace(src, dest)[1], {"same-file", "already-linked"})
        self.assertFalse(ms.should_replace(src, dest)[0])

    def test_equal_smaller_and_larger_downloads_all_replace_the_matching_movie(self) -> None:
        src, dest = self.pair()
        for contents in (b"x", b"123456789", b"x" * 500):
            with self.subTest(size=len(contents)):
                src.write_bytes(contents)
                with mock.patch("subprocess.run", side_effect=AssertionError("no probe needed")), \
                        mock.patch.object(ms.shutil, "which", side_effect=AssertionError("no binary needed")):
                    allowed, reason = ms.should_replace(src, dest)
                self.assertTrue(allowed, reason)
                self.assertIn("latest download", reason)

    def test_a_different_title_or_year_is_not_the_same_movie(self) -> None:
        _, dest = self.pair()
        for name in ("Other.Film.2020.mkv", "Film.1970.mkv"):
            with self.subTest(name=name):
                src = self.file(f"final/{name}", b"not the same movie")
                self.assertEqual(ms.should_replace(src, dest)[0], False)
                self.assertIn("title/year", ms.should_replace(src, dest)[1])

    def test_the_release_folder_is_used_when_the_video_name_is_generic(self) -> None:
        _, dest = self.pair()
        src = self.file("final/Film.2020.1080p/movie.mkv", b"new")
        self.assertTrue(ms.should_replace(src, dest)[0])

    def test_a_distinct_edition_or_3d_version_cannot_replace_a_plain_movie(self) -> None:
        _, dest = self.pair()
        for name in ("Film.2020.Directors.Cut.mkv", "Film.2020.3D_HSBS.mkv"):
            with self.subTest(name=name):
                src = self.file(f"final/{name}", b"different version")
                allowed, reason = ms.should_replace(src, dest)
                self.assertFalse(allowed)
                self.assertIn("alternate-cut/version", reason)

    def test_a_named_edition_can_replace_the_same_named_edition(self) -> None:
        src = self.file("final/Film.2020.Extended.mkv", b"new extended")
        dest = self.file("Movies/Film (2020) {edition-Extended}/Film (2020) {edition-Extended}.mkv", b"old extended")
        self.assertTrue(ms.should_replace(src, dest)[0])
        plain = self.file("final/Film.2020.mkv", b"theatrical")
        self.assertFalse(ms.should_replace(plain, dest)[0])

    def test_a_matching_movie_in_another_container_is_still_the_same_movie(self) -> None:
        src, _ = self.pair(extension=".mp4")
        old = self.file("Movies/Film (2020)/Film (2020).mkv", b"old")
        self.assertTrue(ms.should_replace(src, old)[0])

    def test_a_sidecar_retains_its_size_rule(self) -> None:
        dest = self.file("Movies/Film (2020)/Film (2020).eng.srt", b"123")
        src = self.file("final/Film.2020.eng.srt", b"1")
        self.assertFalse(ms.should_replace(src, dest)[0])
        src.write_bytes(b"1234")
        self.assertTrue(ms.should_replace(src, dest)[0])


class PlacementTests(ReplacementFixture):
    def test_a_new_download_replaces_the_library_hardlink_and_keeps_its_seed(self) -> None:
        src, dest = self.pair()
        old_inode = dest.stat().st_ino
        self.assertTrue(ms.process_file_action(src, dest))
        self.assertEqual(dest.read_bytes(), b"new")
        self.assertTrue(dest.samefile(src))
        self.assertNotEqual(dest.stat().st_ino, old_inode)
        self.assertEqual(src.read_bytes(), b"new")
        self.assertEqual(ms.RUN_SUMMARY.completed, 1)
        self.assertIn("replaced", ms.RUN_EVENTS[-1]["reason"])

    def test_reingesting_the_same_download_changes_nothing(self) -> None:
        src, dest = self.pair()
        ms.process_file_action(src, dest)
        inode = dest.stat().st_ino
        self.assertTrue(ms.process_file_action(src, dest))
        self.assertEqual(dest.stat().st_ino, inode)
        self.assertEqual(ms.RUN_SUMMARY.completed, 1)
        self.assertEqual(ms.RUN_SUMMARY.skipped, 1)

    def test_a_conflicting_movie_is_skipped_without_placing_its_sidecar(self) -> None:
        _, dest = self.pair()
        src = self.file("final/Other.Film.2020.mkv", b"different")
        self.assertFalse(ms.process_file_action(src, dest))
        self.assertEqual(dest.read_bytes(), b"old movie")
        self.assertEqual(ms.RUN_SUMMARY.skipped, 1)

    def test_a_dry_run_reports_replacement_but_leaves_both_files_alone(self) -> None:
        src, dest = self.pair()
        ms.CFG.dry_run = True
        self.assertTrue(ms.process_file_action(src, dest))
        self.assertEqual(dest.read_bytes(), b"old movie")
        self.assertFalse(dest.samefile(src))
        self.assertIn("latest download", ms.RUN_EVENTS[-1]["reason"])
        self.assertEqual(ms.RUN_SUMMARY.reported, 1)

    def test_a_failed_hardlink_keeps_the_library_movie(self) -> None:
        src, dest = self.pair()
        with mock.patch.object(ms, "_create_hardlink", side_effect=OSError("no link")):
            self.assertFalse(ms.process_file_action(src, dest))
        self.assertEqual(dest.read_bytes(), b"old movie")
        self.assertEqual(src.read_bytes(), b"new")
        self.assertEqual(list(dest.parent.glob(".*partial*")), [])
        self.assertEqual(ms.RUN_SUMMARY.failed, 1)

    def test_a_failed_atomic_swap_keeps_the_library_movie(self) -> None:
        src, dest = self.pair()
        with mock.patch.object(ms.os, "replace", side_effect=PermissionError("locked")):
            self.assertFalse(ms.process_file_action(src, dest))
        self.assertEqual(dest.read_bytes(), b"old movie")
        self.assertEqual(src.read_bytes(), b"new")
        self.assertEqual(list(dest.parent.glob(".*partial*")), [])

    def test_the_latest_container_replaces_the_previous_one_in_both_directions(self) -> None:
        for extension, old_extension in ((".mp4", ".mkv"), (".mkv", ".mp4")):
            with self.subTest(extension=extension):
                src = self.file(f"final/Film.2020.WEB{extension}", b"new " + extension.encode())
                dest = self.root / f"Movies/Film (2020)/Film (2020){extension}"
                old = self.file(f"Movies/Film (2020)/Film (2020){old_extension}", b"old")
                self.assertTrue(ms.process_file_action(src, dest))
                self.assertFalse(old.exists())
                self.assertTrue(dest.samefile(src))
                self.assertEqual(sorted(p.suffix for p in dest.parent.iterdir()), [extension])
                dest.unlink()

    def test_cross_container_replacement_keeps_the_old_torrent_seed(self) -> None:
        src = self.file("final/Film.2020.WEB.mp4", b"new")
        dest = self.root / "Movies/Film (2020)/Film (2020).mp4"
        old = self.file("Movies/Film (2020)/Film (2020).mkv", b"old")
        old_seed = self.root / "final/Film.2020.BluRay.mkv"
        old_seed.hardlink_to(old)
        self.assertTrue(ms.process_file_action(src, dest))
        self.assertTrue(dest.samefile(src))
        self.assertFalse(old.exists())
        self.assertEqual(old_seed.read_bytes(), b"old")

    def test_cross_container_dry_run_keeps_the_old_movie(self) -> None:
        src = self.file("final/Film.2020.WEB.mp4", b"new")
        dest = self.root / "Movies/Film (2020)/Film (2020).mp4"
        old = self.file("Movies/Film (2020)/Film (2020).mkv", b"old")
        ms.CFG.dry_run = True
        self.assertTrue(ms.process_file_action(src, dest))
        self.assertEqual(old.read_bytes(), b"old")
        self.assertFalse(dest.exists())
        self.assertIn("latest download", ms.RUN_EVENTS[-1]["reason"])

    def test_cross_container_failed_link_or_swap_does_not_remove_the_old_movie(self) -> None:
        src = self.file("final/Film.2020.WEB.mp4", b"new")
        dest = self.root / "Movies/Film (2020)/Film (2020).mp4"
        old = self.file("Movies/Film (2020)/Film (2020).mkv", b"old")
        for target, attribute, error in ((ms, "_create_hardlink", OSError("no link")),
                                         (ms.os, "replace", PermissionError("locked"))):
            with self.subTest(attribute=attribute), mock.patch.object(target, attribute, side_effect=error):
                self.assertFalse(ms.process_file_action(src, dest))
                self.assertEqual(old.read_bytes(), b"old")
                self.assertFalse(dest.exists())
                self.assertEqual(list(dest.parent.glob(".*partial*")), [])

    def test_cross_container_unlink_failure_rolls_back_the_new_movie(self) -> None:
        src = self.file("final/Film.2020.WEB.mp4", b"new")
        dest = self.root / "Movies/Film (2020)/Film (2020).mp4"
        old = self.file("Movies/Film (2020)/Film (2020).mkv", b"old")
        real_unlink = Path.unlink

        def refuse_old(path: Path, *args, **kwargs) -> None:
            if path == old:
                raise PermissionError("old container locked")
            real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=refuse_old):
            self.assertFalse(ms.process_file_action(src, dest))
        self.assertEqual(old.read_bytes(), b"old")
        self.assertFalse(dest.exists(), "the new link was rolled back")
        self.assertEqual(src.read_bytes(), b"new")
        self.assertEqual(ms.RUN_SUMMARY.failed, 1)

    def test_a_changed_old_container_is_not_deleted(self) -> None:
        src = self.file("final/Film.2020.WEB.mp4", b"new")
        dest = self.root / "Movies/Film (2020)/Film (2020).mp4"
        old = self.file("Movies/Film (2020)/Film (2020).mkv", b"old")
        real_replace = ms.os.replace

        def changed_old(tmp: str, target: str) -> None:
            real_replace(tmp, target)
            old.write_bytes(b"changed after identity check")

        with mock.patch.object(ms.os, "replace", side_effect=changed_old):
            self.assertFalse(ms.process_file_action(src, dest))
        self.assertEqual(old.read_bytes(), b"changed after identity check")
        self.assertFalse(dest.exists())

    def test_a_vanished_old_container_does_not_leave_the_library_empty(self) -> None:
        src = self.file("final/Film.2020.WEB.mp4", b"new")
        dest = self.root / "Movies/Film (2020)/Film (2020).mp4"
        old = self.file("Movies/Film (2020)/Film (2020).mkv", b"old")
        real_replace = ms.os.replace

        def vanished_old(tmp: str, target: str) -> None:
            real_replace(tmp, target)
            old.unlink()

        with mock.patch.object(ms.os, "replace", side_effect=vanished_old):
            self.assertFalse(ms.process_file_action(src, dest))
        self.assertFalse(old.exists())
        self.assertTrue(dest.samefile(src), "the verified new movie must not be rolled back")
        self.assertEqual(ms.RUN_SUMMARY.failed, 1)

    def test_two_preexisting_containers_are_not_silently_collapsed(self) -> None:
        src, dest = self.pair(extension=".mkv")
        rival = self.file("Movies/Film (2020)/Film (2020).mp4", b"other")
        self.assertFalse(ms.process_file_action(src, dest))
        self.assertEqual((dest.read_bytes(), rival.read_bytes()), (b"old movie", b"other"))
        self.assertIn("both movie containers", ms.RUN_EVENTS[-1]["reason"])


if __name__ == "__main__":
    unittest.main()
