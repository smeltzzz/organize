"""Edge paths of the movie library organizer: name parsing corners, scan
classification, and the destructive-maintenance decisions a real library hits.

Every test here exists because the branch it pins was a scar: an extension the
parser must not eat (``(500) Days of Summer``), a scan that must not follow a
symlink out of the torrent folder, a duplicate sweep that must not delete
unique data. Where a branch ends in a filesystem change, the assertion is on
the bytes/on-disk state a user would see, not on the log text.
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import movie_standardizer as ms  # noqa: E402


class _RunState(unittest.TestCase):
    """Isolate the module-level run state and point CFG at a scratch tree."""

    def setUp(self) -> None:
        self._cfg, self._summary, self._events = ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS
        self._td = tempfile.TemporaryDirectory(prefix="ms_edge_")
        self.root = Path(self._td.name)
        self.source = self.root / "source"
        self.lib = self.root / "library"
        self.source.mkdir()
        self.lib.mkdir()
        ms.CFG = ms.Config(
            target_dir=self.lib,
            source_dir=self.source,
            log_file=None,
            report_file=None,
            maintenance_mode="REPORT",
        )
        ms.RUN_SUMMARY = ms.RunSummary()
        ms.RUN_EVENTS = []
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS = self._cfg, self._summary, self._events
        self._td.cleanup()

    def cfg(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(ms.CFG, key, value)


class TagBlockParsingTests(unittest.TestCase):
    """Scene-tag detection must keep numbers that are part of a title."""

    def test_a_bracketed_year_is_not_a_tag_block(self) -> None:
        self.assertFalse(ms._is_tag_block("(1999)"))

    def test_bracketed_numbers_are_titles_not_tags(self) -> None:
        # (500) Days of Summer / 13 Going on 30 / [9]
        self.assertFalse(ms._is_tag_block("(500)"))
        self.assertFalse(ms._is_tag_block("[9]"))

    def test_real_release_tags_are_blocks(self) -> None:
        self.assertTrue(ms._is_tag_block("[x265]"))
        self.assertTrue(ms._is_tag_block("(1080p BluRay)"))
        self.assertTrue(ms._is_tag_block("[]"))

    def test_a_year_ending_title_is_not_eaten(self) -> None:
        parsed = ms.parse_movie_name("(500).Days.of.Summer.2009.1080p.mkv")
        self.assertIn("500", parsed.title)
        self.assertEqual(2009, parsed.year)


class PartAndProviderTests(unittest.TestCase):
    """Multipart positions must survive, and provider IDs must leave the title."""

    def test_each_stack_marker_maps_to_a_canonical_part(self) -> None:
        self.assertEqual("cd2", ms.parse_movie_name("Film.1999.CD2.mkv").part)
        self.assertEqual("cd1", ms.parse_movie_name("Film.1999.Pt1.mkv").part)
        self.assertEqual("disc1", ms.parse_movie_name("Film.1999.Disk1.mkv").part)
        self.assertEqual("disc3", ms.parse_movie_name("Film.1999.Disc3.mkv").part)

    def test_a_title_integral_part_is_not_a_stack_position(self) -> None:
        # "Harry Potter and the Deathly Hallows Part 2" is one movie.
        self.assertIsNone(ms.parse_movie_name("Film.Part.2.2019.1080p.mkv").part)

    def test_a_provider_id_leaves_the_title(self) -> None:
        parsed = ms.parse_movie_name("Arrival.[tmdbid-329865].2016.1080p.mkv")
        self.assertEqual("tmdbid-329865", parsed.provider_id)
        self.assertNotIn("tmdbid", parsed.title)

    def test_no_provider_id_means_no_change(self) -> None:
        self.assertEqual(("Film.mkv", None), ms._extract_provider_id("Film.mkv"))


class ResolutionAndEditionTests(unittest.TestCase):
    def test_resolution_labels_are_normalized(self) -> None:
        self.assertEqual("2160p", ms._extract_resolution_label("Film.4k.mkv"))
        self.assertEqual("1080i", ms._extract_resolution_label("Film.1080i.mkv"))
        self.assertIsNone(ms._extract_resolution_label("Film.mkv"))

    def test_three_d_labels_match_jellyfins_form(self) -> None:
        self.assertEqual("3D_HSBS", ms._extract_3d_label("Film.3D.HSBS.mkv"))
        self.assertIsNone(ms._extract_3d_label("Film.2019.mkv"))

    def test_year_scoring_prefers_the_bracketed_release_year(self) -> None:
        name = "Wonder Woman 1984 (2020) 1080p"
        best = max(re.finditer(r"((?:18|19|20)\d{2})", name),
                   key=lambda m: ms._score_year_match(name, m))
        self.assertEqual("2020", best.group(0))
        self.assertEqual(("Wonder Woman 1984", 2020), ms._pick_year("Wonder Woman 1984 (2020) 1080p"))

    def test_a_resolution_is_never_read_as_a_year(self) -> None:
        # "2160p" is four digits in the year shape; treating it as a year is
        # how "Film 2160p" becomes "Film (2160)".
        match = re.search(r"(2160)", "Film.2160p.mkv")
        self.assertEqual(-10_000, ms._score_year_match("Film.2160p.mkv", match))
        self.assertEqual(("Film 2160p.mkv", None), ms._pick_year("Film 2160p.mkv"))

    def test_a_year_that_is_the_title_is_not_taken_as_the_release_year(self) -> None:
        # 2012 (2009): the first number is the title, the second the year.
        parsed = ms.parse_movie_name("2012.2009.1080p.mkv")
        self.assertEqual("2012", parsed.title)
        self.assertEqual(2009, parsed.year)

    def test_no_year_leaves_the_name_alone(self) -> None:
        self.assertEqual(("Some Film", None), ms._pick_year("Some Film"))


class TitleCasingTests(unittest.TestCase):
    def test_website_prefixes_are_stripped_repeatedly(self) -> None:
        self.assertEqual("Film 1999", ms._strip_website_prefix("[rarbg.to] www.x.to - Film 1999"))

    def test_scene_tags_peel_off_the_end_of_a_title(self) -> None:
        self.assertEqual("Some Film", ms._strip_trailing_tags("Some Film 1080p BluRay x265"))

    def test_leading_tag_blocks_peel_off_but_titles_in_parentheses_stay(self) -> None:
        self.assertEqual("Some Film", ms._strip_leading_tag_blocks("[x265] Some Film"))
        self.assertIn("500", ms._strip_leading_tag_blocks("(500) Days of Summer"))

    def test_codec_group_pairs_peel_together(self) -> None:
        # "x264-SPARKS" is one tag pair, not a title.
        self.assertEqual("Some Film", ms._strip_trailing_tags("Some Film x264-SPARKS"))

    def test_honorifics_keep_their_period(self) -> None:
        self.assertEqual("Dr. Strangelove", ms._clean_separators("Dr. Strangelove"))

    def test_stylized_tokens_are_left_alone(self) -> None:
        self.assertTrue(ms._is_stylized_token("Se7en"))
        self.assertTrue(ms._is_stylized_token("iPhone"))
        self.assertTrue(ms._is_stylized_token("McConaughey"))
        self.assertFalse(ms._is_stylized_token("Seven"))

    def test_custom_title_case_handles_hyphens_minor_words_and_rec(self) -> None:
        self.assertEqual("Spider-Man", ms.custom_title_case("spider-man"))
        self.assertEqual("The Matrix", ms.custom_title_case("the matrix"))
        self.assertEqual("Attack on Titan", ms.custom_title_case("attack on titan"))
        self.assertEqual("[REC]", ms.custom_title_case("[rec]"))

    def test_sanitize_defuses_reserved_names_and_blanks(self) -> None:
        self.assertEqual("_CON", ms.sanitize_filename("CON"))
        self.assertEqual("Unknown", ms.sanitize_filename("   "))
        self.assertEqual('He said \'hi\'', ms.sanitize_filename('He said "hi"'))

    def test_sanitize_keeps_names_within_a_component_limit(self) -> None:
        long_name = "Film " + "x" * 300
        self.assertLessEqual(len(ms.sanitize_filename(long_name).encode("utf-8")), 200)


class SubtitleValidationTests(unittest.TestCase):
    """Only a playable normal-English SRT may enter the library."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="ms_sub_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _srt(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_valid_eng_srt_passes(self) -> None:
        path = self._srt("Film.eng.srt", "1\n00:00:01,000 --> 00:00:02,000\nHi.\n")
        ok, reason = ms.is_valid_plain_english_srt(path)
        self.assertTrue(ok, reason)

    def test_the_legacy_en_suffix_is_still_admitted(self) -> None:
        # Releases that predate the canonical ".eng.srt" tag hand the promoter
        # a ".en.srt"; refusing it would strand an otherwise normal subtitle.
        path = self._srt("Film.en.srt", "1\n00:00:01,000 --> 00:00:02,000\nHi.\n")
        ok, reason = ms.is_valid_plain_english_srt(path)
        self.assertTrue(ok, reason)

    def test_a_non_english_subtitle_is_refused(self) -> None:
        path = self._srt("Film.fra.srt", "1\n00:00:01,000 --> 00:00:02,000\nBonjour.\n")
        ok, reason = ms.is_valid_plain_english_srt(path)
        self.assertFalse(ok)
        self.assertIn("English", reason)

    def test_a_word_document_named_srt_is_refused(self) -> None:
        path = self._srt("Film.eng.srt", "this is not a subtitle at all")
        ok, reason = ms.is_valid_plain_english_srt(path)
        self.assertFalse(ok)
        self.assertIn("cue", reason)

    def test_an_empty_subtitle_is_refused(self) -> None:
        path = self._srt("Film.eng.srt", "")
        ok, reason = ms.is_valid_plain_english_srt(path)
        self.assertFalse(ok)
        self.assertIn("size", reason)

    def test_a_non_srt_extension_is_refused_before_reading(self) -> None:
        path = self._srt("Film.eng.vtt", "1\n00:00:01.000 --> 00:00:02.000\nHi.\n")
        ok, reason = ms.is_valid_plain_english_srt(path)
        self.assertFalse(ok)
        self.assertEqual("not an SRT", reason)

    def test_an_unstattable_path_is_refused(self) -> None:
        ok, reason = ms.is_valid_plain_english_srt(self.root / "missing.eng.srt")
        self.assertFalse(ok)
        self.assertIn("could not stat", reason)

    def test_known_scene_suffixes_are_stripped_before_matching(self) -> None:
        parsed = ms.parse_movie_name("Film.1999.1080p.mkv")
        self.assertEqual("Film", parsed.title)


class ScanTreeTests(_RunState):
    """The scan classifies what a torrent actually ships — and refuses to
    follow links out of the download folder."""

    def test_a_symlinked_video_inside_the_folder_is_skipped(self) -> None:
        folder = self.source / "Film (1999)"
        folder.mkdir()
        real = self.root / "outside.mkv"
        real.write_bytes(b"x" * (ms.CFG.min_movie_bytes + 1))
        (folder / "Film.1999.mkv").symlink_to(real)
        scan = ms.scan_tree(folder)
        self.assertEqual([], scan.videos, "a symlink would smuggle an outside file into the library")

    def test_small_videos_and_extras_are_not_features(self) -> None:
        folder = self.source / "Film (1999)"
        folder.mkdir()
        (folder / "Film.1999.mkv").write_bytes(b"x" * (ms.CFG.min_movie_bytes + 1))
        (folder / "sample.mkv").write_bytes(b"tiny")
        scan = ms.scan_tree(folder)
        self.assertEqual(["Film.1999.mkv"], [f.path.name for f in scan.videos])
        self.assertEqual({"sample.mkv"}, {f.path.name for f in scan.extras})

    def test_trailer_names_are_extras_even_when_large(self) -> None:
        folder = self.source / "Film (1999)"
        folder.mkdir()
        trailer = folder / "Film.1999.trailer.mkv"
        trailer.write_bytes(b"x" * (ms.CFG.min_movie_bytes + 1))
        self.assertTrue(ms.is_extra_video(trailer, root=folder))

    def test_jellyfin_extra_suffixes_apply_only_in_jellyfin_mode(self) -> None:
        folder = self.source / "Film (1999)"
        folder.mkdir()
        clip = folder / "Film.1999.clip.mkv"
        clip.write_bytes(b"x")
        self.assertFalse(ms.is_extra_video(clip, root=folder))
        self.cfg(jellyfin_mode=True)
        self.assertTrue(ms.is_extra_video(clip, root=folder), "Jellyfin's own extra suffixes must win")

    def test_subtitles_in_an_extras_folder_are_not_sidecars(self) -> None:
        folder = self.source / "Film (1999)"
        (folder / "Extras").mkdir(parents=True)
        (folder / "Extras" / "deleted.eng.srt").write_text("x", encoding="utf-8")
        scan = ms.scan_tree(folder)
        self.assertEqual([], scan.subtitles, "an extras subtitle is not the feature's sidecar")

    def test_artwork_is_classified_as_artwork(self) -> None:
        folder = self.source / "Film (1999)"
        folder.mkdir()
        (folder / "poster.jpg").write_bytes(b"jpeg")
        (folder / "backdrop-1.jpg").write_bytes(b"jpeg")
        self.assertTrue(ms.is_artwork_file(folder / "poster.jpg"))
        self.assertTrue(ms.is_artwork_file(folder / "backdrop-1.jpg"))
        self.assertFalse(ms.is_artwork_file(folder / "shot.jpg"))
        self.assertEqual(2, len(ms.scan_tree(folder).artwork))

    def test_a_disc_structure_keeps_its_video_files_as_disc_files(self) -> None:
        # Inside a disc tree a video file is a stream, never a "feature" that
        # could be renamed or replaced: the branch keeps it labelled.
        folder = self.source / "Film (1999)"
        (folder / "BDMV" / "STREAM").mkdir(parents=True)
        (folder / "BDMV" / "STREAM" / "00001.mkv").write_bytes(b"x")
        self.assertTrue(ms.path_has_disc_structure(folder))
        scan = ms.scan_tree(folder)
        self.assertTrue(scan.is_disc)
        self.assertEqual(["disc-file"], [f.kind for f in scan.files])

    def test_a_disc_structure_is_found_one_folder_deep(self) -> None:
        folder = self.source / "Film (1999)"
        (folder / "disc" / "VIDEO_TS").mkdir(parents=True)
        self.assertTrue(ms.path_has_disc_structure(folder))

    def test_an_empty_folder_is_not_a_disc(self) -> None:
        folder = self.source / "Film (1999)"
        folder.mkdir()
        self.assertFalse(ms.path_has_disc_structure(folder))
        self.assertFalse(ms.path_has_disc_structure(folder / "missing"))

    def test_junk_names_are_skipped_entirely(self) -> None:
        self.assertTrue(ms.is_skipped_junk_name(".DS_Store"))
        self.assertTrue(ms.is_skipped_junk_name("movie.part"))
        self.assertFalse(ms.is_skipped_junk_name("movie.mkv"))

    def test_generic_stems_are_recognized(self) -> None:
        self.assertTrue(ms.stem_is_generic("movie"))
        self.assertTrue(ms.stem_is_generic("cd1"))
        self.assertFalse(ms.stem_is_generic("The.Matrix.1999"))
        self.assertTrue(ms.folder_name_is_generic("downloads"))
        self.assertFalse(ms.folder_name_is_generic("The Matrix (1999)"))


class VideoIdentityTests(_RunState):
    def test_a_useful_filename_wins_over_its_folder(self) -> None:
        video = self.source / "The.Matrix.1999.1080p.mkv"
        parsed = ms.parse_video_identity(video, fallback=self.source / "The Matrix (1999)")
        self.assertEqual("The Matrix", parsed.title)
        self.assertEqual(1999, parsed.year)

    def test_a_generic_filename_borrows_the_folder_name(self) -> None:
        parsed = ms.parse_video_identity(
            self.source / "movie.mkv", fallback=self.source / "The Matrix (1999)",
        )
        self.assertEqual("The Matrix", parsed.title)
        self.assertEqual(1999, parsed.year)

    def test_a_generic_folder_never_renames_the_movie(self) -> None:
        parsed = ms.parse_video_identity(
            self.source / "The.Matrix.1999.mkv", fallback=self.source / "downloads",
        )
        self.assertEqual("The Matrix", parsed.title)

    def test_a_tv_name_is_returned_as_tv(self) -> None:
        parsed = ms.parse_video_identity(self.source / "Show.S01E02.1080p.mkv")
        self.assertTrue(parsed.is_tv)


class SubtitleMatchingTests(_RunState):
    """In a multi-video folder each subtitle must land on the right film."""

    def _sub(self, path: Path, size: int = 100) -> ms.ScannedFile:
        return ms.ScannedFile(path, size, "subtitle")

    def test_a_matching_numbered_srt_is_kept(self) -> None:
        video = self.source / "Film.1999.mkv"
        subs = [self._sub(self.source / "Film.1999.eng.srt")]
        hits = ms.match_subtitles_for_video(video, ms.parse_movie_name(video.name), subs, multi=True)
        self.assertEqual(subs, hits)

    def test_a_subtitle_named_after_the_video_stem_is_kept(self) -> None:
        video = self.source / "Film.1999.1080p.mkv"
        sub = self._sub(self.source / "Film.1999.1080p.en.srt")
        hits = ms.match_subtitles_for_video(video, ms.parse_movie_name(video.name), [sub], multi=True)
        self.assertEqual([sub], hits)

    def test_a_subtitle_in_the_movies_own_folder_is_kept(self) -> None:
        folder = self.source / "Film.1999"
        folder.mkdir()
        video = folder / "feature.mkv"
        sub = self._sub(folder / "anything.srt")
        hits = ms.match_subtitles_for_video(video, ms.parse_movie_name("Film.1999.mkv"), [sub], multi=True)
        self.assertEqual([sub], hits)

    def test_a_subtitle_for_a_different_film_is_dropped(self) -> None:
        video = self.source / "Film.1999.mkv"
        other = self._sub(self.source / "Other.Movie.2001.eng.srt")
        hits = ms.match_subtitles_for_video(video, ms.parse_movie_name(video.name), [other], multi=True)
        self.assertEqual([], hits)

    def test_a_single_video_folder_keeps_every_subtitle(self) -> None:
        video = self.source / "Film.1999.mkv"
        subs = [self._sub(self.source / "whatever.srt")]
        self.assertEqual(subs, ms.match_subtitles_for_video(
            video, ms.parse_movie_name(video.name), subs, multi=False))


class DisposalTests(_RunState):
    """Maintenance candidates are reported, quarantined or deleted — never
    half-moved, and never deleted before the replacement is in place."""

    def _candidate(self) -> Path:
        path = self.source / "dup" / "Film.1999.extra.mkv"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"data")
        return path

    def test_report_mode_touches_nothing(self) -> None:
        candidate = self._candidate()
        outcome = ms.dispose_candidate(candidate, action="test", reason="dup")
        self.assertEqual("reported", outcome)
        self.assertTrue(candidate.exists())
        self.assertEqual(1, ms.RUN_SUMMARY.reported)

    def test_dry_run_says_would_delete_and_touches_nothing(self) -> None:
        candidate = self._candidate()
        self.cfg(maintenance_mode="DELETE", dry_run=True)
        outcome = ms.dispose_candidate(candidate, action="test", reason="dup")
        self.assertEqual("reported", outcome)
        self.assertTrue(candidate.exists())

    def test_quarantine_moves_the_candidate_out_of_the_library(self) -> None:
        candidate = self._candidate()
        quarantine = self.root / "quarantine"
        self.cfg(maintenance_mode="QUARANTINE", quarantine_dir=quarantine)
        outcome = ms.dispose_candidate(candidate, action="test", reason="dup")
        self.assertEqual("quarantined", outcome)
        self.assertFalse(candidate.exists())
        self.assertTrue((quarantine / candidate.name).exists())

    def test_quarantine_never_overwrites_an_earlier_candidate(self) -> None:
        candidate = self._candidate()
        quarantine = self.root / "quarantine"
        earlier = quarantine / candidate.name
        earlier.parent.mkdir(parents=True)
        earlier.write_bytes(b"the first one")
        self.cfg(maintenance_mode="QUARANTINE", quarantine_dir=quarantine)
        ms.dispose_candidate(candidate, action="test", reason="dup")
        self.assertEqual(b"the first one", earlier.read_bytes())
        conflicts = list(earlier.parent.glob("*.conflict.*"))
        self.assertEqual(1, len(conflicts), "the second copy must land beside, not on top")

    def test_a_failed_quarantine_is_recorded_and_leaves_the_file(self) -> None:
        candidate = self._candidate()
        self.cfg(maintenance_mode="QUARANTINE", quarantine_dir=self.root / "quarantine")
        with mock.patch.object(ms.shutil, "move", side_effect=OSError("cross-device")):
            outcome = ms.dispose_candidate(candidate, action="test", reason="dup")
        self.assertEqual("failed", outcome)
        self.assertTrue(candidate.exists())
        self.assertEqual(1, ms.RUN_SUMMARY.failed)

    def test_delete_removes_a_directory_tree(self) -> None:
        folder = self.source / "dup-folder"
        (folder / "sub").mkdir(parents=True)
        (folder / "sub" / "file.txt").write_bytes(b"x")
        self.cfg(maintenance_mode="DELETE")
        outcome = ms.dispose_candidate(folder, action="test", reason="dup")
        self.assertEqual("deleted", outcome)
        self.assertFalse(folder.exists())

    def test_a_failed_delete_is_recorded_and_leaves_the_file(self) -> None:
        candidate = self._candidate()
        self.cfg(maintenance_mode="DELETE")
        with mock.patch.object(Path, "unlink", side_effect=OSError("read-only")):
            outcome = ms.dispose_candidate(candidate, action="test", reason="dup")
        self.assertEqual("failed", outcome)
        self.assertTrue(candidate.exists())

    def test_an_unknown_mode_is_a_programming_error(self) -> None:
        self.cfg(maintenance_mode="PURGE")
        with self.assertRaises(ValueError):
            ms.dispose_candidate(self._candidate(), action="test", reason="dup")

    def test_quarantine_without_a_directory_refuses(self) -> None:
        self.cfg(maintenance_mode="QUARANTINE", quarantine_dir=None)
        with self.assertRaises(ValueError):
            ms.dispose_candidate(self._candidate(), action="test", reason="dup")


class ReplacementDecisionTests(_RunState):
    """One movie, one canonical name; the newest download wins."""

    def _pair(self, incoming: str, existing: str) -> tuple[Path, Path]:
        src = self.source / incoming
        dest = self.lib / "Film (1999)" / existing
        src.write_bytes(b"new bytes")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"old bytes")
        return src, dest

    def test_a_matching_movie_replaces_the_existing_cut(self) -> None:
        src, dest = self._pair("The.Matrix.1999.Directors.Cut.1080p.mkv", "The Matrix (1999).mkv")
        replace, reason = ms.should_replace(src, dest)
        self.assertTrue(replace, reason)

    def test_a_multipart_position_must_agree(self) -> None:
        src, dest = self._pair("The.Matrix.1999.CD2.mkv", "The.Matrix.1999.CD1.mkv")
        replace, reason = ms.should_replace(src, dest)
        self.assertFalse(replace)
        self.assertIn("part", reason)

    def test_a_different_title_never_replaces(self) -> None:
        src, dest = self._pair("Other.Film.1999.mkv", "The Matrix (1999).mkv")
        replace, reason = ms.should_replace(src, dest)
        self.assertFalse(replace)
        self.assertIn("identities differ", reason)

    def test_a_different_year_never_replaces(self) -> None:
        src, dest = self._pair("The.Matrix.2003.mkv", "The Matrix (1999).mkv")
        self.assertFalse(ms.should_replace(src, dest)[0])

    def test_a_missing_destination_is_placed(self) -> None:
        self.assertEqual((True, "missing"), ms.should_replace(self.source / "x.mkv", self.lib / "x.mkv"))

    def test_the_same_file_is_left_alone(self) -> None:
        src = self.source / "Film.1999.mkv"
        src.write_bytes(b"x")
        self.assertEqual((False, "same-file"), ms.should_replace(src, src))

    def test_an_already_linked_pair_is_left_alone(self) -> None:
        src = self.source / "Film.1999.mkv"
        src.write_bytes(b"x")
        dest = self.lib / "Film (1999).mkv"
        os.link(src, dest)
        # The same inode under a second name is the same file: replacing it
        # would be a no-op, and relinking would only churn the library.
        self.assertIn(ms.should_replace(src, dest), {(False, "same-file"), (False, "already-linked")})

    def test_sidecars_keep_the_size_rule(self) -> None:
        small = self.source / "Film.eng.srt"
        small.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        big = self.lib / "Film.eng.srt"
        big.write_text("x" * 100, encoding="utf-8")
        replace, reason = ms.should_replace(small, big)
        self.assertFalse(replace)
        self.assertIn("dest-larger", reason)
        equal = self.lib / "Film.other.srt"
        equal.write_text(small.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual((False, "same-size-exists"), ms.should_replace(small, equal))

    def test_an_mp4_covering_the_same_movie_is_found(self) -> None:
        dest = self.lib / "Film (1999)" / "Film (1999).mkv"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"mkv")
        other = dest.with_suffix(".mp4")
        other.write_bytes(b"mp4")
        self.assertEqual(other, ms.existing_other_container(dest))
        self.assertIsNone(ms.existing_other_container(self.source / "Film.eng.srt"))

    def test_process_file_action_refuses_a_vanished_source(self) -> None:
        dest = self.lib / "Film (1999).mkv"
        ok = ms.process_file_action(self.source / "gone.mkv", dest)
        self.assertFalse(ok)
        self.assertEqual(1, ms.RUN_SUMMARY.failed)
        self.assertFalse(dest.exists())

    def test_process_file_action_reports_already_in_place(self) -> None:
        src = self.source / "Film.1999.mkv"
        src.write_bytes(b"x")
        with mock.patch.object(ms, "paths_equal", return_value=True):
            ok = ms.process_file_action(src, self.lib / "Film (1999).mkv")
        self.assertTrue(ok)
        self.assertEqual(1, ms.RUN_SUMMARY.skipped)


class InputPathResolutionTests(_RunState):
    """qBittorrent passes %F or %D %N, and both must resolve."""

    def test_no_arguments_means_no_input(self) -> None:
        self.assertIsNone(ms.resolve_input_path([]))
        self.assertIsNone(ms.resolve_input_path(["", ""]))

    def test_one_argument_is_used_as_is(self) -> None:
        self.assertEqual(Path("/a/movie.mkv"), ms.resolve_input_path(["/a/movie.mkv"]))

    def test_save_path_plus_name_resolves_inside_the_save_path(self) -> None:
        save = self.source / "saves"
        item = save / "Film.1999"
        item.mkdir(parents=True)
        resolved = ms.resolve_input_path([str(save), "Film.1999"])
        self.assertEqual(item, resolved)

    def test_name_plus_content_path_resolves_to_the_content(self) -> None:
        content = self.source / "Film.1999.mkv"
        content.write_bytes(b"x")
        resolved = ms.resolve_input_path(["Film.1999.mkv", str(content)])
        self.assertEqual(content, resolved)

    def test_both_paths_existing_prefers_the_specific_one(self) -> None:
        save = self.source / "saves"
        save.mkdir()
        nested = save / "Film.1999.mkv"
        nested.write_bytes(b"x")
        resolved = ms.resolve_input_path([str(save), str(nested)])
        self.assertEqual(nested, resolved)

    def test_unresolvable_paths_fall_back_to_the_first(self) -> None:
        self.assertEqual(Path("/nope/a"), ms.resolve_input_path(["/nope/a", "/nope/b"]))


class ValidationTests(_RunState):
    def test_nested_source_and_target_are_refused(self) -> None:
        self.cfg(target_dir=self.source / "library")
        errors = ms.validate_config(ms.CFG)
        self.assertTrue(any("nested" in e for e in errors), errors)

    def test_a_same_path_pair_is_refused_once(self) -> None:
        self.cfg(target_dir=self.source)
        errors = ms.validate_config(ms.CFG)
        self.assertEqual(1, sum("must be different" in e for e in errors), errors)

    def test_a_filesystem_probe_failure_is_an_error_not_a_crash(self) -> None:
        with mock.patch.object(ms, "_filesystem_device", side_effect=OSError("no such mount")):
            errors = ms.validate_config(ms.CFG)
        self.assertTrue(any("Could not verify" in e for e in errors), errors)

    def test_a_target_that_is_a_file_is_refused(self) -> None:
        target = self.root / "not-a-dir"
        target.write_bytes(b"x")
        self.cfg(target_dir=target)
        errors = ms.validate_config(ms.CFG)
        self.assertTrue(any("not a directory" in e for e in errors), errors)

    def test_documented_option_placement_rules_are_enforced(self) -> None:
        self.cfg(
            maintenance_mode="QUARANTINE",
            quarantine_dir=self.lib / "q",
            manifest_file=self.lib / "manifest.json",
            report_file=self.source / "report.txt",
            log_file=self.lib / "run.log",
        )
        errors = ms.validate_config(ms.CFG)
        blob = "\n".join(errors)
        self.assertIn("--quarantine-dir must be outside the organized library", blob)
        self.assertIn("--manifest must be outside --target", blob)
        self.assertIn("--report must be outside --source", blob)
        self.assertIn("--log must be outside --target", blob)

    def test_a_bad_maintenance_mode_is_reported(self) -> None:
        self.cfg(maintenance_mode="NOPE")
        errors = ms.validate_config(ms.CFG)
        self.assertTrue(any("Unsupported maintenance mode" in e for e in errors), errors)

    def test_a_quarantine_mode_without_a_directory_is_reported(self) -> None:
        self.cfg(maintenance_mode="QUARANTINE", quarantine_dir=None)
        errors = ms.validate_config(ms.CFG)
        self.assertTrue(any("requires --quarantine-dir" in e for e in errors), errors)

    def test_negative_numbers_are_reported(self) -> None:
        self.cfg(min_movie_size_mb=-1, lock_timeout_seconds=-1)
        errors = ms.validate_config(ms.CFG)
        self.assertIn("--min-size must be zero or greater", errors)
        self.assertIn("--lock-timeout must be zero or greater", errors)

    def test_automated_input_rejects_missing_symlink_and_library_paths(self) -> None:
        self.assertIn("does not exist", ms.validate_automated_input(self.source / "gone", ms.CFG))
        real = self.source / "Film.1999.mkv"
        real.write_bytes(b"x")
        link = self.source / "link.mkv"
        link.symlink_to(real)
        self.assertIn("symlink", ms.validate_automated_input(link, ms.CFG))
        inside = self.lib / "Film.1999.mkv"
        inside.write_bytes(b"x")
        self.assertIn("organized library", ms.validate_automated_input(inside, ms.CFG))

    def test_automated_input_rejects_a_non_video_file(self) -> None:
        junk = self.source / "readme.txt"
        junk.write_bytes(b"x")
        self.assertIn("movie file", ms.validate_automated_input(junk, ms.CFG))

    def test_automated_input_probe_failures_are_returned_not_raised(self) -> None:
        with mock.patch.object(Path, "exists", side_effect=OSError("stale handle")):
            reason = ms.validate_automated_input(self.source / "x.mkv", ms.CFG)
        self.assertIn("could not validate", reason or "")


class DeduplicationTests(_RunState):
    """Dedup may only remove what it can prove is a leftover."""

    def _movie_folder(self, folder: str, video: str | None, size: int, *, extra: str | None = None) -> Path:
        path = self.lib / folder
        path.mkdir(parents=True, exist_ok=True)
        if video:
            (path / video).write_bytes(b"x" * size)
        if extra:
            (path / extra).write_bytes(b"unique data")
        return path

    def test_a_clearly_smaller_duplicate_folder_is_deleted_smallest_first(self) -> None:
        keep = self._movie_folder("Film (1999)", "Film.1999.mkv", 10_000)
        loser = self._movie_folder("Film.1999.1080p", "Film.1999.1080p.mkv", 1_000)
        self.cfg(enable_deduplication=True, maintenance_mode="DELETE", create_subfolders=True)
        ms.deduplicate_movies(self.lib)
        self.assertTrue(keep.exists())
        self.assertFalse(loser.exists())

    def test_two_near_equal_folders_are_left_for_a_human(self) -> None:
        first = self._movie_folder("Film (1999)", "Film.1999.mkv", 10_000)
        second = self._movie_folder("Film.1999.1080p", "Film.1999.1080p.mkv", 9_900)
        self.cfg(enable_deduplication=True, maintenance_mode="DELETE", create_subfolders=True)
        ms.deduplicate_movies(self.lib)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists(), "within the dedup margin nothing may be deleted")
        self.assertEqual(0, ms.RUN_SUMMARY.deleted)

    def test_a_video_less_folder_that_still_holds_files_is_kept(self) -> None:
        self._movie_folder("Film (1999)", "Film.1999.mkv", 10_000)
        shell = self._movie_folder("Film.1999.1080p", None, 0, extra="Film.eng.srt")
        self.cfg(enable_deduplication=True, maintenance_mode="DELETE", create_subfolders=True)
        ms.deduplicate_movies(self.lib)
        self.assertTrue(shell.exists(), "a folder with unique sidecars is not an empty shell")

    def test_a_provably_empty_duplicate_folder_is_deleted(self) -> None:
        self._movie_folder("Film (1999)", "Film.1999.mkv", 10_000)
        shell = self._movie_folder("Film.1999.1080p", None, 0)
        self.cfg(enable_deduplication=True, maintenance_mode="DELETE", create_subfolders=True)
        ms.deduplicate_movies(self.lib)
        self.assertFalse(shell.exists())

    def test_a_jellyfin_multi_version_folder_freezes_the_whole_group(self) -> None:
        # "Title (1999)/Title (1999) - Extended.mkv" + "... - Theatrical.mkv"
        # is Jellyfin's documented multi-version layout: collapsing the group
        # would silently pick one cut for the user.
        first = self._movie_folder("The Matrix (1999)", "The Matrix (1999) - Extended.mkv", 10_000)
        (first / "The Matrix (1999) - Theatrical.mkv").write_bytes(b"x" * 9_000)
        second = self._movie_folder("The.Matrix.1999.1080p", "The.Matrix.1999.1080p.mkv", 1_000)
        self.cfg(enable_deduplication=True, maintenance_mode="DELETE", create_subfolders=True,
                 jellyfin_mode=True)
        ms.deduplicate_movies(self.lib)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists(), "Jellyfin's own multi-version layout must be respected")

    def test_a_hardlinked_leftover_tree_is_removed(self) -> None:
        keep = self._movie_folder("Film (1999)", "Film.1999.mkv", 10_000)
        older = self.lib / "Film.1999.1080p"
        older.mkdir()
        os.link(keep / "Film.1999.mkv", older / "Film.1999.1080p.mkv")
        self.cfg(enable_deduplication=True, maintenance_mode="DELETE", create_subfolders=True)
        ms.deduplicate_movies(self.lib)
        self.assertTrue(keep.exists())
        self.assertFalse(older.exists(), "a same-inode copy holds no extra bytes")

    def test_flat_layout_dedup_deletes_the_smaller_file_and_its_subtitle(self) -> None:
        keep = self.lib / "Film.1999.1080p.mkv"
        keep.write_bytes(b"x" * 10_000)
        loser = self.lib / "Film.1999.720p.mkv"
        loser.write_bytes(b"x" * 1_000)
        sidecar = self.lib / "Film.1999.720p.eng.srt"
        sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        self.cfg(enable_deduplication=True, maintenance_mode="DELETE", create_subfolders=False)
        ms.deduplicate_movies(self.lib)
        self.assertTrue(keep.exists())
        self.assertFalse(loser.exists())
        self.assertFalse(sidecar.exists(), "the deleted file's own subtitle is not left orphaned")

    def test_flat_layout_near_equal_files_are_left_alone(self) -> None:
        first = self.lib / "Film.1999.1080p.mkv"
        first.write_bytes(b"x" * 10_000)
        second = self.lib / "Film.1999.720p.mkv"
        second.write_bytes(b"x" * 9_900)
        self.cfg(enable_deduplication=True, maintenance_mode="DELETE", create_subfolders=False)
        ms.deduplicate_movies(self.lib)
        self.assertTrue(second.exists())

    def test_dedup_can_be_switched_off(self) -> None:
        loser = self._movie_folder("Film.1999.1080p", "Film.1999.1080p.mkv", 1)
        self._movie_folder("Film (1999)", "Film.1999.mkv", 10_000)
        self.cfg(enable_deduplication=False, maintenance_mode="DELETE", create_subfolders=True)
        ms.deduplicate_movies(self.lib)
        self.assertTrue(loser.exists())

    def test_a_missing_target_is_a_warning_not_a_crash(self) -> None:
        self.cfg(enable_deduplication=True)
        ms.deduplicate_movies(self.lib / "missing")
        self.assertEqual([], ms.RUN_EVENTS)

    def test_an_unlistable_target_is_a_recorded_error(self) -> None:
        self.cfg(enable_deduplication=True, create_subfolders=True)
        with mock.patch.object(Path, "iterdir", side_effect=OSError("permission denied")), \
                mock.patch.object(ms.LOG, "error") as log_error:
            ms.deduplicate_movies(self.lib)
        self.assertTrue(log_error.called)


class CliAndEnvironmentTests(_RunState):
    def test_environment_variables_populate_the_config(self) -> None:
        env = {
            "MOVIE_STD_TARGET": str(self.lib),
            "MOVIE_STD_SOURCE": str(self.source),
            "MOVIE_STD_MIN_SIZE": "12.5",
            "MOVIE_STD_DEDUPLICATE": "yes",
            "MOVIE_STD_DRY_RUN": "true",
            "MOVIE_STD_MAINTENANCE_MODE": "delete",
        }
        cfg = ms.Config(log_file=None, report_file=None)
        with mock.patch.dict(os.environ, env, clear=False):
            ms.apply_env(cfg)
        self.assertEqual(self.lib, cfg.target_dir)
        self.assertEqual(12.5, cfg.min_movie_size_mb)
        self.assertTrue(cfg.enable_deduplication)
        self.assertTrue(cfg.dry_run)
        self.assertEqual("delete", cfg.maintenance_mode)

    def test_args_take_precedence_and_are_uppercased(self) -> None:
        args = ms.build_parser().parse_args([
            str(self.source), "--target", str(self.lib), "--dry-run",
            "--maintenance-mode", "QUARANTINE",
        ])
        cfg = ms.cfg_from_args(args)
        self.assertEqual(self.lib, cfg.target_dir)
        self.assertTrue(cfg.dry_run)
        self.assertEqual("QUARANTINE", cfg.maintenance_mode)

    def test_the_batch_scanner_records_a_missing_source(self) -> None:
        ms.batch_scan(self.source / "gone")
        self.assertEqual(1, ms.RUN_SUMMARY.failed)
        self.assertEqual("failed", ms.RUN_EVENTS[0]["status"])

    def test_the_batch_scanner_records_an_unlistable_source(self) -> None:
        with mock.patch.object(Path, "iterdir", side_effect=OSError("permission denied")):
            ms.batch_scan(self.source)
        self.assertEqual(1, ms.RUN_SUMMARY.failed)

    def test_the_batch_scanner_orders_by_mtime_and_skips_junk(self) -> None:
        older = self.source / "Older.1999"
        newer = self.source / "Newer.2001"
        older.mkdir()
        newer.mkdir()
        os.utime(older, (100, 100))
        os.utime(newer, (200, 200))
        (self.source / "junk.part").write_bytes(b"x")
        seen: list[str] = []
        with mock.patch.object(ms, "handle_item", side_effect=lambda item: seen.append(item.name)), \
                mock.patch.object(ms, "LIVE"):
            ms.batch_scan(self.source)
        self.assertEqual(["Older.1999", "Newer.2001"], seen)

    def test_a_rejected_input_is_a_failure_exit_code(self) -> None:
        args = ms.build_parser().parse_args([
            str(self.source / "gone.mkv"), "--target", str(self.lib), "--source", str(self.source),
        ])
        with mock.patch.object(ms, "write_report"), mock.patch.object(ms, "write_manifest"), \
                redirect_stdout(io.StringIO()):
            code = ms.run(args)
        self.assertEqual(1, code)

    def test_a_run_with_an_empty_source_exits_zero(self) -> None:
        args = ms.build_parser().parse_args([
            "--target", str(self.lib), "--source", str(self.source),
        ])
        with mock.patch.object(ms, "write_report"), mock.patch.object(ms, "write_manifest"), \
                redirect_stdout(io.StringIO()):
            code = ms.run(args)
        self.assertEqual(0, code)

    def test_main_propagates_an_interrupt_as_130(self) -> None:
        with mock.patch.object(ms, "run", side_effect=KeyboardInterrupt), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(130, ms.main(["--source", str(self.source)]))

    def test_main_reports_an_unexpected_crash_as_1(self) -> None:
        with mock.patch.object(ms, "run", side_effect=RuntimeError("boom")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(1, ms.main(["--source", str(self.source)]))

    def test_the_self_test_flag_runs_the_smoke_checks(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            code = ms.main(["--self-test"])
        self.assertEqual(0, code, out.getvalue())
        self.assertIn("SELF-TEST PASSED", out.getvalue())
