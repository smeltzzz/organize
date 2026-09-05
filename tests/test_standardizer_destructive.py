"""The standardizer's deleting code, exercised on real directory trees.

``movie_standardizer.py`` is the only tool that removes whole folders: it
de-duplicates a library after a hardlink ingest and sweeps extras out of the
target. Everything it deletes is, by definition, something the user already
has - so the interesting behaviour is not what it removes but what it
*refuses* to remove: an ambiguous near-match, a folder that still holds
unique files, a Jellyfin multi-version set, an extras folder that turns out to
contain the feature.

These tests build the trees on disk and check the files afterwards, in all
three maintenance modes (REPORT, QUARANTINE, DELETE).
"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path

import movie_standardizer as ms

BIG = 8 * 1024 * 1024
SMALL = 1 * 1024 * 1024


def write_video(path: Path, size: int = BIG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)
    return path


class _StandardizerFixture(unittest.TestCase):
    """Own the module-level run state the standardizer mutates."""

    maintenance_mode = "DELETE"

    def setUp(self) -> None:
        self._saved = (ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS)
        self._td = tempfile.TemporaryDirectory(prefix="ms_destructive_")
        self.root = Path(self._td.name)
        self.library = self.root / "Movies"
        self.library.mkdir()
        self.quarantine = self.root / "quarantine"
        ms.CFG = ms.Config(
            source_dir=self.root / "final",
            target_dir=self.library,
            log_file=None,
            report_file=self.root / "out" / "report.txt",
            quarantine_dir=self.quarantine,
            maintenance_mode=self.maintenance_mode,
            # Deduplication ships off by default (it deletes); these tests are
            # about what it does when an operator turns it on.
            enable_deduplication=True,
            min_movie_size_mb=2,
        )
        ms.RUN_SUMMARY = ms.RunSummary()
        ms.RUN_EVENTS = []
        # These runs log every refusal by design; keep that off the test output
        # without silencing the logger itself, which assertLogs still needs.
        self._logging = (ms.LOG.handlers[:], ms.LOG.propagate)
        ms.LOG.handlers = [logging.NullHandler()]
        ms.LOG.propagate = False
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        ms.LOG.handlers, ms.LOG.propagate = self._logging
        ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS = self._saved
        self._td.cleanup()

    def _folders(self) -> list[str]:
        return sorted(p.name for p in self.library.iterdir() if p.is_dir())

    def _reasons(self) -> str:
        return " | ".join(event.get("reason", "") for event in ms.RUN_EVENTS)


class DisposeCandidateTests(_StandardizerFixture):
    """One decision point governs every deletion: prove each mode of it."""

    def setUp(self) -> None:
        super().setUp()
        self.candidate = write_video(self.library / "Film (2020)" / "Film (2020).mkv", SMALL)
        self.folder = self.candidate.parent

    def test_report_mode_deletes_nothing(self) -> None:
        ms.CFG.maintenance_mode = "REPORT"
        outcome = ms.dispose_candidate(self.folder, action="duplicate", reason="smaller copy")
        self.assertEqual(outcome, "reported")
        self.assertTrue(self.candidate.is_file())
        self.assertEqual(ms.RUN_SUMMARY.reported, 1)

    def test_a_dry_run_overrides_delete_mode(self) -> None:
        ms.CFG.dry_run = True
        outcome = ms.dispose_candidate(self.folder, action="duplicate", reason="smaller copy")
        self.assertEqual(outcome, "reported")
        self.assertTrue(self.candidate.is_file())

    def test_quarantine_moves_the_tree_out_of_the_library(self) -> None:
        ms.CFG.maintenance_mode = "QUARANTINE"
        outcome = ms.dispose_candidate(self.folder, action="duplicate", reason="smaller copy")
        self.assertEqual(outcome, "quarantined")
        self.assertFalse(self.folder.exists())
        moved = self.quarantine / "Film (2020)" / "Film (2020).mkv"
        self.assertTrue(moved.is_file(), "the data is moved aside, not destroyed")
        self.assertEqual(ms.RUN_SUMMARY.quarantined, 1)

    def test_quarantine_never_overwrites_an_earlier_quarantine(self) -> None:
        ms.CFG.maintenance_mode = "QUARANTINE"
        ms.dispose_candidate(self.folder, action="duplicate", reason="first")
        write_video(self.library / "Film (2020)" / "Film (2020).mkv", SMALL)
        ms.dispose_candidate(self.folder, action="duplicate", reason="second")
        kept = sorted(p.name for p in self.quarantine.iterdir())
        self.assertEqual(len(kept), 2, f"both quarantined copies survive: {kept}")

    def test_quarantine_without_a_destination_is_refused(self) -> None:
        ms.CFG.maintenance_mode = "QUARANTINE"
        ms.CFG.quarantine_dir = None
        with self.assertRaises(ValueError):
            ms.dispose_candidate(self.folder, action="duplicate", reason="smaller copy")
        self.assertTrue(self.candidate.is_file())

    def test_delete_mode_removes_the_tree(self) -> None:
        outcome = ms.dispose_candidate(self.folder, action="duplicate", reason="smaller copy")
        self.assertEqual(outcome, "deleted")
        self.assertFalse(self.folder.exists())
        self.assertEqual(ms.RUN_SUMMARY.deleted, 1)

    def test_a_failed_delete_is_recorded_not_swallowed(self) -> None:
        missing = self.library / "Gone (1999)"
        outcome = ms.dispose_candidate(missing, action="duplicate", reason="smaller copy")
        self.assertEqual(outcome, "failed")
        self.assertEqual(ms.RUN_SUMMARY.failed, 1)

    def test_an_unknown_mode_is_a_hard_error(self) -> None:
        ms.CFG.maintenance_mode = "SHRED"
        with self.assertRaises(ValueError):
            ms.dispose_candidate(self.folder, action="duplicate", reason="smaller copy")
        self.assertTrue(self.candidate.is_file(), "an unrecognised policy deletes nothing")


class DeduplicateFoldersTests(_StandardizerFixture):
    """Folder-per-movie libraries: the Jellyfin layout this repo targets."""

    def _movie(self, folder_name: str, file_name: str, size: int) -> Path:
        return write_video(self.library / folder_name / file_name, size)

    def test_the_smaller_copy_goes_and_the_bigger_one_stays(self) -> None:
        keeper = self._movie("Film (2020)", "Film (2020).mkv", BIG)
        self._movie("Film.2020.720p", "Film.2020.720p.mkv", SMALL)
        ms.deduplicate_movies(self.library)
        self.assertEqual(self._folders(), ["Film (2020)"])
        self.assertTrue(keeper.is_file())

    def test_a_near_identical_size_is_too_ambiguous_to_delete(self) -> None:
        self._movie("Film (2020)", "Film (2020).mkv", BIG)
        self._movie("Film.2020.1080p", "Film.2020.1080p.mkv", int(BIG * 0.95))
        ms.deduplicate_movies(self.library)
        self.assertEqual(len(self._folders()), 2, "within the margin, keep both and say so")
        self.assertIn("ambiguous", self._captured_warning())

    def test_different_years_are_different_films(self) -> None:
        self._movie("Film (2020)", "Film (2020).mkv", BIG)
        self._movie("Film (1978)", "Film (1978).mkv", SMALL)
        ms.deduplicate_movies(self.library)
        self.assertEqual(len(self._folders()), 2, "a remake is not a duplicate")

    def test_a_hardlinked_second_copy_is_dropped_whatever_its_size(self) -> None:
        keeper = self._movie("Film (2020)", "Film (2020).mkv", BIG)
        clone_folder = self.library / "Film.2020.1080p.WEB"
        clone_folder.mkdir()
        os.link(keeper, clone_folder / "Film.2020.1080p.WEB.mkv")
        ms.deduplicate_movies(self.library)
        self.assertEqual(self._folders(), ["Film (2020)"],
                         "the same inode twice is a leftover, not a second copy")
        self.assertTrue(keeper.is_file())

    def test_a_video_less_duplicate_that_still_holds_files_is_kept(self) -> None:
        self._movie("Film (2020)", "Film (2020).mkv", BIG)
        shell = self.library / "Film.2020.1080p"
        shell.mkdir()
        (shell / "Film.2020.1080p.eng.srt").write_text("1\n", encoding="utf-8")
        ms.deduplicate_movies(self.library)
        self.assertEqual(len(self._folders()), 2, "a subtitle nobody else has is unique data")

    def test_an_empty_duplicate_shell_is_removed(self) -> None:
        self._movie("Film (2020)", "Film (2020).mkv", BIG)
        (self.library / "Film.2020.1080p").mkdir()
        ms.deduplicate_movies(self.library)
        self.assertEqual(self._folders(), ["Film (2020)"])

    def test_a_jellyfin_multi_version_set_is_never_collapsed(self) -> None:
        ms.CFG.jellyfin_mode = True
        versions = self.library / "Film (2020)"
        write_video(versions / "Film (2020) - Theatrical.mkv", BIG)
        write_video(versions / "Film (2020) - Directors Cut.mkv", SMALL)
        self._movie("Film.2020.1080p", "Film.2020.1080p.mkv", SMALL)
        ms.deduplicate_movies(self.library)
        self.assertEqual(len(self._folders()), 2)
        self.assertTrue((versions / "Film (2020) - Theatrical.mkv").is_file())

    def test_dedup_can_be_switched_off(self) -> None:
        ms.CFG.enable_deduplication = False
        self._movie("Film (2020)", "Film (2020).mkv", BIG)
        self._movie("Film.2020.720p", "Film.2020.720p.mkv", SMALL)
        ms.deduplicate_movies(self.library)
        self.assertEqual(len(self._folders()), 2)

    def test_a_missing_target_is_not_an_error(self) -> None:
        ms.deduplicate_movies(self.root / "nowhere")  # must not raise

    def test_report_mode_lists_the_duplicate_without_touching_it(self) -> None:
        ms.CFG.maintenance_mode = "REPORT"
        self._movie("Film (2020)", "Film (2020).mkv", BIG)
        self._movie("Film.2020.720p", "Film.2020.720p.mkv", SMALL)
        ms.deduplicate_movies(self.library)
        self.assertEqual(len(self._folders()), 2)
        self.assertEqual(ms.RUN_SUMMARY.reported, 1)
        self.assertIn("duplicate identity", self._reasons())

    def _captured_warning(self) -> str:
        with self.assertLogs(ms.LOG, level="WARNING") as captured:
            ms.deduplicate_movies(self.library)
        return "\n".join(captured.output).lower()


class DeduplicateFlatFilesTests(_StandardizerFixture):
    """The flat-library variant, where the duplicates are files, not folders."""

    def setUp(self) -> None:
        super().setUp()
        ms.CFG.create_subfolders = False

    def _videos(self) -> list[str]:
        return sorted(p.name for p in self.library.iterdir() if p.suffix == ".mkv")

    def test_the_smaller_file_goes(self) -> None:
        write_video(self.library / "Film (2020).mkv", BIG)
        write_video(self.library / "Film.2020.720p.mkv", SMALL)
        ms.deduplicate_movies(self.library)
        self.assertEqual(self._videos(), ["Film (2020).mkv"])

    def test_the_deleted_file_takes_its_own_sidecars_with_it(self) -> None:
        write_video(self.library / "Film (2020).mkv", BIG)
        write_video(self.library / "Film.2020.720p.mkv", SMALL)
        orphan = self.library / "Film.2020.720p.eng.srt"
        orphan.write_text("1\n", encoding="utf-8")
        keeper_sidecar = self.library / "Film (2020).eng.srt"
        keeper_sidecar.write_text("1\n", encoding="utf-8")
        ms.deduplicate_movies(self.library)
        self.assertFalse(orphan.exists(), "a subtitle for a deleted file is dead weight")
        self.assertTrue(keeper_sidecar.is_file(), "the keeper's subtitle is untouched")

    def test_an_ambiguous_pair_is_left_alone(self) -> None:
        write_video(self.library / "Film (2020).mkv", BIG)
        write_video(self.library / "Film.2020.1080p.mkv", int(BIG * 0.98))
        ms.deduplicate_movies(self.library)
        self.assertEqual(len(self._videos()), 2)


class CleanExistingExtrasTests(_StandardizerFixture):
    def test_an_extras_folder_without_a_movie_is_removed(self) -> None:
        folder = self.library / "Film (2020)"
        write_video(folder / "Film (2020).mkv", BIG)
        write_video(folder / "Featurettes" / "Making Of.mkv", SMALL)
        ms.clean_existing_extras(self.library)
        self.assertFalse((folder / "Featurettes").exists())
        self.assertTrue((folder / "Film (2020).mkv").is_file())

    def test_an_extras_folder_that_actually_holds_the_movie_is_kept(self) -> None:
        folder = self.library / "Film (2020)" / "Extras"
        write_video(folder / "Film (2020).mkv", BIG)
        ms.clean_existing_extras(self.library)
        self.assertTrue(folder.exists(), "a feature-sized video vetoes the folder's name")

    def test_a_subtitle_folder_is_never_treated_as_an_extra(self) -> None:
        subs = self.library / "Film (2020)" / "Subs"
        subs.mkdir(parents=True)
        (subs / "Film (2020).eng.srt").write_text("1\n", encoding="utf-8")
        ms.clean_existing_extras(self.library)
        self.assertTrue(subs.is_dir())

    def test_a_sample_file_beside_the_movie_is_removed(self) -> None:
        folder = self.library / "Film (2020)"
        write_video(folder / "Film (2020).mkv", BIG)
        sample = write_video(folder / "Film-sample.mkv", SMALL)
        ms.clean_existing_extras(self.library)
        self.assertFalse(sample.exists())
        self.assertTrue((folder / "Film (2020).mkv").is_file())

    def test_a_feature_sized_extra_is_kept_for_review(self) -> None:
        folder = self.library / "Film (2020)"
        write_video(folder / "Film (2020).mkv", BIG)
        big_sample = write_video(folder / "Film-sample.mkv", BIG)
        ms.clean_existing_extras(self.library)
        self.assertTrue(big_sample.is_file(),
                        "too big to be a sample: never deleted on the name alone")

    def test_a_missing_target_is_not_an_error(self) -> None:
        ms.clean_existing_extras(self.root / "nowhere")  # must not raise


if __name__ == "__main__":
    unittest.main()
