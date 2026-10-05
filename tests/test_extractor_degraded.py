"""The subtitle extractor's conversions, refusals and provider protocol.

``subtitle_extractor.py`` writes a file the rest of the toolkit treats as
authoritative: the cleaner strips every embedded subtitle on the strength of it,
and Jellyfin direct-plays it. So the interesting failures here are the ones that
would publish a subtitle nobody can use, or refuse to publish one that is fine.

Three groups:

* **Conversions.** An embedded track arrives as ASS, SSA, WebVTT or USF and has
  to leave as canonical SRT. A cue with an unreadable timestamp, an empty body, a
  ``Comment`` line, a block with no timing, or a WebVTT header is dropped rather
  than written out as a broken cue - a malformed sidecar is worse than no
  sidecar, because the cleaner will then remove the good embedded track.
* **Refusals.** A gzipped payload that decompresses to more than the ceiling, a
  movie that is not a regular non-symlink file, a folder that cannot be listed, a
  layout that breaks the one-MKV-per-folder contract.
* **The provider protocol.** Every OpenSubtitles answer that is not the one that
  was hoped for: not JSON, a JSON array, an HTTP error with and without a
  message, a 429 with and without a ``Retry-After``, a login that returns no
  token, a download that returns no link, a token that expired mid-run, a file
  that changed size while it was being hashed. Nothing here touches the network:
  ``urlopen`` is the fake transport from ``tests/fakeprovider.py``.
"""

from __future__ import annotations

import gzip
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from fakeprovider import (
    SRT_PAYLOAD,
    FakeTransport,
    download_answer,
    http_error,
    provider_entry,
    search_answer,
)

import subtitle_extractor as sx

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class TempFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="extractor_degraded_")
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        self.library = self.root / "Movies"
        self.library.mkdir()

    def movie(self, title: str = "Film (2020)", size: int = 4 * 1024 * 1024) -> Path:
        folder = self.library / title
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{title}.mkv"
        with path.open("wb") as handle:
            handle.truncate(size)
        return path


class SrtParsingTests(unittest.TestCase):
    """``parse_srt_cues``: what may be re-rendered into a published sidecar."""

    def test_a_well_formed_cue_is_parsed(self) -> None:
        cues = sx.parse_srt_cues("1\n00:00:01,000 --> 00:00:02,500\nHello.\n\n")
        self.assertEqual(cues, [("00:00:01,000", "00:00:02,500", "Hello.")])

    def test_a_block_with_no_timing_is_dropped(self) -> None:
        self.assertEqual(sx.parse_srt_cues("1\njust some text\n\n"), [])

    def test_a_block_whose_timing_is_not_a_timing_is_dropped(self) -> None:
        """``-->`` with junk either side is a truncated download, not a cue."""
        self.assertEqual(sx.parse_srt_cues("1\nfoo --> bar\nHello.\n\n"), [])

    def test_a_cue_with_nothing_in_it_is_dropped(self) -> None:
        self.assertEqual(sx.parse_srt_cues("1\n00:00:01,000 --> 00:00:02,000\n\n"), [])

    def test_a_cue_whose_timestamp_cannot_be_padded_is_dropped(self) -> None:
        self.assertEqual(sx.parse_srt_cues("1\n1:2 --> 3:4\nHello.\n\n"), [])

    def test_a_dot_millisecond_separator_is_normalised_to_a_comma(self) -> None:
        cues = sx.parse_srt_cues("1\n00:00:01.000 --> 00:00:02.500\nHello.\n\n")
        self.assertEqual(cues, [("00:00:01,000", "00:00:02,500", "Hello.")])

    def test_a_short_millisecond_field_is_padded_not_truncated(self) -> None:
        self.assertEqual(sx._pad_srt_timestamp("00:00:01,5"), "00:00:01,500")

    def test_a_timestamp_without_a_clock_is_not_a_timestamp(self) -> None:
        self.assertIsNone(sx._pad_srt_timestamp(",500"))
        self.assertIsNone(sx._pad_srt_timestamp("01:02"))
        self.assertIsNone(sx._pad_srt_timestamp("aa:bb:cc,500"))

    def test_cues_are_renumbered_from_one_when_rendered(self) -> None:
        text = "7\n00:00:01,000 --> 00:00:02,000\nOne.\n\n99\n00:00:03,000 --> 00:00:04,000\nTwo.\n\n"
        rendered = sx.normalize_extracted_srt(text)
        self.assertTrue(rendered.startswith("1\n"))
        self.assertIn("\n2\n", rendered)

    def test_carriage_returns_are_normalised(self) -> None:
        rendered = sx.normalize_extracted_srt("1\r\n00:00:01,000 --> 00:00:02,000\r\nHi.\r\n\r\n")
        self.assertNotIn("\r", rendered)
        self.assertIn("Hi.", rendered)


class AssConversionTests(unittest.TestCase):
    HEADER = ("[Script Info]\nTitle: demo\n\n[V4+ Styles]\nFormat: Name, Fontname\n"
              "Style: Default,Arial\n\n[Events]\n"
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")

    def test_a_dialogue_line_becomes_a_cue(self) -> None:
        text = self.HEADER + "Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,Hello there\n"
        self.assertEqual(sx.parse_srt_cues(sx.ass_to_srt(text)),
                         [("00:00:01,500", "00:00:03,000", "Hello there")])

    def test_a_comment_line_is_never_shown(self) -> None:
        """``Comment`` is the format's non-displaying note."""
        text = (self.HEADER
                + "Comment: 0,0:00:09.00,0:00:10.00,Default,,0,0,0,,translator note\n"
                + "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Real line\n")
        rendered = sx.ass_to_srt(text)
        self.assertNotIn("translator note", rendered)
        self.assertIn("Real line", rendered)

    def test_an_unreadable_timestamp_drops_the_cue(self) -> None:
        text = self.HEADER + "Dialogue: 0,not-a-time,0:00:03.00,Default,,0,0,0,,Hello\n"
        self.assertEqual(sx.ass_to_srt(text), "")

    def test_a_line_with_no_text_in_it_is_dropped(self) -> None:
        text = self.HEADER + "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\i1}\\N\n"
        self.assertEqual(sx.ass_to_srt(text), "")

    def test_a_row_that_does_not_fill_the_format_columns_is_dropped(self) -> None:
        text = self.HEADER + "Dialogue: 0,0:00:01.00\n"
        self.assertEqual(sx.ass_to_srt(text), "")

    def test_an_event_section_without_a_format_line_is_not_guessed_at(self) -> None:
        """The column order differs between SSA v4 and ASS v4+."""
        text = ("[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello\n")
        self.assertEqual(sx.ass_to_srt(text), "")

    def test_lines_outside_the_events_section_are_ignored(self) -> None:
        text = ("[Script Info]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Not an event\n"
                + self.HEADER.split("[Events]")[1].join(["[Events]", ""])
                + "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                + "Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,Real event\n")
        rendered = sx.ass_to_srt(text)
        self.assertNotIn("Not an event", rendered)
        self.assertIn("Real event", rendered)

    def test_an_ass_timestamp_that_is_not_a_timestamp_is_none(self) -> None:
        self.assertIsNone(sx._ass_timestamp_to_srt("nonsense"))
        self.assertEqual(sx._ass_timestamp_to_srt("1:02:03.45"), "01:02:03,450")

    def test_styling_is_stripped_and_line_breaks_become_newlines(self) -> None:
        text = self.HEADER + "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\i1}Hello\\Nthere\\h\n"
        self.assertEqual(sx.parse_srt_cues(sx.ass_to_srt(text))[0][2], "Hello\nthere")


class VttConversionTests(unittest.TestCase):
    def test_a_well_formed_document_is_converted(self) -> None:
        text = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello.\n\n"
                "00:00:04.000 --> 00:00:05.000\nSecond.\n")
        self.assertEqual(len(sx.parse_srt_cues(sx.vtt_to_srt(text))), 2)

    def test_a_timestamp_without_hours_is_padded(self) -> None:
        """WebVTT allows ``MM:SS.mmm``; SRT requires the hours."""
        text = "WEBVTT\n\n01:02.500 --> 01:04.000\nHello.\n"
        self.assertEqual(sx.parse_srt_cues(sx.vtt_to_srt(text))[0][0], "00:01:02,500")

    def test_a_byte_order_mark_is_stripped(self) -> None:
        text = "\ufeffWEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello.\n"
        self.assertIn("Hello.", sx.vtt_to_srt(text))

    def test_header_and_note_blocks_are_not_cues(self) -> None:
        text = ("WEBVTT\n\nNOTE\nthis is a note\n\nSTYLE\n::cue { color: white }\n\n"
                "REGION\nid: r1\n\n00:00:01.000 --> 00:00:02.000\nReal cue.\n")
        rendered = sx.vtt_to_srt(text)
        self.assertNotIn("this is a note", rendered)
        self.assertNotIn("::cue", rendered)
        self.assertIn("Real cue.", rendered)

    def test_a_block_with_no_timing_is_dropped(self) -> None:
        self.assertEqual(sx.vtt_to_srt("WEBVTT\n\njust some text\n"), "")

    def test_a_timing_line_the_regex_cannot_read_is_dropped(self) -> None:
        self.assertEqual(sx.vtt_to_srt("WEBVTT\n\nfoo --> bar\nHello.\n"), "")

    def test_a_cue_whose_timestamps_are_unreadable_is_dropped(self) -> None:
        self.assertEqual(sx.vtt_to_srt("WEBVTT\n\n1:2 --> 3:4\nHello.\n"), "")

    def test_a_cue_with_settings_after_the_end_timestamp_is_still_read(self) -> None:
        text = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000 position:10% align:start\nHello.\n"
        self.assertIn("Hello.", sx.vtt_to_srt(text))

    def test_a_cue_with_no_text_is_dropped(self) -> None:
        self.assertEqual(sx.vtt_to_srt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n\n"), "")

    def test_a_cue_identifier_line_is_not_part_of_the_text(self) -> None:
        text = "WEBVTT\n\nintro-1\n00:00:01.000 --> 00:00:02.000\nHello.\n"
        cues = sx.parse_srt_cues(sx.vtt_to_srt(text))
        self.assertEqual(cues, [("00:00:01,000", "00:00:02,000", "Hello.")])


class PayloadDecodingTests(TempFixture):
    def test_a_gzipped_payload_is_inflated(self) -> None:
        text = "1\n00:00:01,000 --> 00:00:02,000\nHello.\n\n"
        with io.BytesIO() as buffer:
            with gzip.GzipFile(fileobj=buffer, mode="wb") as archive:
                archive.write(text.encode("utf-8"))
            self.assertEqual(sx.decode_subtitle_bytes(buffer.getvalue()), text)

    def test_a_gzipped_payload_that_inflates_past_the_ceiling_is_refused(self) -> None:
        """A decompression bomb is the reason the ceiling is checked after inflating.

        The bytes on the wire can be tiny and the subtitle enormous; reading it
        to find out is what the limit exists to prevent.
        """
        payload = b"1\n00:00:01,000 --> 00:00:02,000\n" + b"x" * (sx.MAX_SUBTITLE_BYTES + 2)
        with io.BytesIO() as buffer:
            with gzip.GzipFile(fileobj=buffer, mode="wb") as archive:
                archive.write(payload)
            with self.assertRaises(ValueError) as caught:
                sx.decode_subtitle_bytes(buffer.getvalue())
        self.assertIn("exceeds safety limit", str(caught.exception))

    def test_bytes_in_no_known_encoding_are_still_returned_as_text(self) -> None:
        """The caller inspects a rejected download to explain why it was rejected.

        Returning ``None`` here - which the shared ``decode_srt_bytes`` does -
        would leave the report with no reason at all.
        """
        decoded = sx.decode_subtitle_bytes(b"\x81\x81 not a subtitle \x81")
        self.assertIsInstance(decoded, str)
        self.assertIn("not a subtitle", decoded)

    def test_a_number_that_is_not_a_number_is_zero(self) -> None:
        for value in (None, "nonsense", [], {}):
            with self.subTest(value=value):
                self.assertEqual(sx._nonnegative_int(value), 0)
                self.assertEqual(sx._nonnegative_float(value), 0.0)

    def test_a_negative_number_is_clamped_to_zero(self) -> None:
        self.assertEqual(sx._nonnegative_int(-5), 0)
        self.assertEqual(sx._nonnegative_float(-5.5), 0.0)

    def test_a_ratio_of_an_empty_text_is_zero_not_a_division_error(self) -> None:
        self.assertEqual(sx.non_latin_ratio(""), 0.0)
        self.assertEqual(sx.non_latin_ratio("   "), 0.0)


class SidecarAndLayoutTests(TempFixture):
    def test_a_snapshot_of_a_symlinked_movie_is_refused(self) -> None:
        """The provider transaction publishes a file beside the movie.

        Through a symlink that write lands outside the library, so the identity
        check refuses the shape before anything is fetched.
        """
        real = self.movie()
        link = self.root / "link.mkv"
        link.symlink_to(real)
        with self.assertRaises(OSError) as caught:
            sx.video_snapshot(link)
        self.assertIn("not a regular non-symlink movie file", str(caught.exception))

    def test_a_snapshot_of_a_real_movie_carries_its_identity(self) -> None:
        movie = self.movie()
        snapshot = sx.video_snapshot(movie)
        info = movie.stat()
        self.assertEqual((snapshot.size, snapshot.mtime_ns, snapshot.inode),
                         (info.st_size, info.st_mtime_ns, info.st_ino))

    def test_only_a_sidecar_named_for_this_movie_counts(self) -> None:
        movie = self.movie()
        other = movie.with_name("Other (2019).eng.srt")
        other.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n\n", encoding="utf-8")
        self.assertFalse(sx.is_english_srt_sidecar(other, "Film (2020)"))
        mine = movie.with_name("Film (2020).eng.srt")
        mine.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n\n", encoding="utf-8")
        self.assertTrue(sx.is_english_srt_sidecar(mine, "Film (2020)"))
        self.assertEqual(sx.has_english_sidecar(movie.parent, "Film (2020)"), mine)

    def test_a_sidecar_that_is_not_an_srt_does_not_count(self) -> None:
        movie = self.movie()
        sub = movie.with_name("Film (2020).eng.sub")
        sub.write_text("x", encoding="utf-8")
        self.assertFalse(sx.is_english_srt_sidecar(sub, "Film (2020)"))

    def test_a_sidecar_that_is_not_english_does_not_count(self) -> None:
        movie = self.movie()
        foreign = movie.with_name("Film (2020).fra.srt")
        foreign.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n\n", encoding="utf-8")
        self.assertFalse(sx.is_english_srt_sidecar(foreign, "Film (2020)"))
        self.assertIsNone(sx.has_english_sidecar(movie.parent, "Film (2020)"))

    def test_a_folder_that_cannot_be_listed_has_no_sidecar(self) -> None:
        """The caller then fetches one; a crash here would end the run instead."""
        movie = self.movie()
        with mock.patch.object(Path, "iterdir", side_effect=OSError("share went away")):
            self.assertIsNone(sx.has_english_sidecar(movie.parent, "Film (2020)"))

    def test_a_movie_directly_under_the_library_root_is_not_canonical(self) -> None:
        loose = self.library / "Film (2020).mkv"
        loose.write_bytes(b"x")
        self.assertIn("directly under the library root",
                      sx.canonical_movie_layout_issue(loose, self.library))

    def test_a_symlinked_movie_is_not_canonical(self) -> None:
        real = self.movie()
        folder = self.library / "Link (2021)"
        folder.mkdir()
        link = folder / "Link (2021).mkv"
        link.symlink_to(real)
        self.assertIn("not a regular non-symlink file",
                      sx.canonical_movie_layout_issue(link, self.library))

    def test_a_stem_that_does_not_match_its_folder_is_not_canonical(self) -> None:
        self.movie()
        odd = self.library / "Film (2020)" / "Something.Else.mkv"
        odd.write_bytes(b"x" * 1024)
        self.assertIn("movie stem does not match its movie-folder name",
                      sx.canonical_movie_layout_issue(odd, self.library))

    def test_two_features_in_one_folder_are_not_canonical(self) -> None:
        movie = self.movie()
        second = movie.with_name("Film (2020).mp4")
        second.write_bytes(b"y" * 1024)
        self.assertIn("expected one regular movie file in movie folder, found 2",
                      sx.canonical_movie_layout_issue(movie, self.library))

    def test_a_folder_that_cannot_be_inspected_is_not_canonical(self) -> None:
        movie = self.movie()
        with mock.patch.object(Path, "iterdir", side_effect=OSError("share went away")):
            self.assertIn("could not inspect movie folder",
                          sx.canonical_movie_layout_issue(movie, self.library))

    def test_a_canonical_movie_has_no_issue(self) -> None:
        self.assertIsNone(sx.canonical_movie_layout_issue(self.movie(), self.library))


class DiscoveryTests(TempFixture):
    def test_a_movie_is_found_and_a_sample_is_counted_as_filtered(self) -> None:
        movie = self.movie()
        sample = self.library / "Film (2020)" / "Film (2020)-sample.mkv"
        sample.write_bytes(b"x" * 4096)
        scan = sx.discover_videos(self.library, min_bytes=1024)
        self.assertEqual(scan.videos, [movie])
        self.assertEqual(scan.sample_named, 1, "the report has to say a sample was filtered")

    def test_a_movie_below_the_size_floor_is_counted_as_filtered(self) -> None:
        small = self.library / "Tiny (2020)" / "Tiny (2020).mkv"
        small.parent.mkdir()
        small.write_bytes(b"x" * 10)
        scan = sx.discover_videos(self.library, min_bytes=1024)
        self.assertEqual(scan.videos, [])
        self.assertEqual(scan.below_min_size, 1)

    def test_a_file_that_cannot_be_stat_ed_is_skipped(self) -> None:
        movie = self.movie()
        real_stat = Path.stat

        def flaky(path: Path, **kwargs: object) -> object:
            if path == movie and not kwargs:
                raise OSError("share went away")
            return real_stat(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(Path, "stat", flaky):
            self.assertEqual(sx.discover_videos(self.library, min_bytes=1024).videos, [])

    def test_a_symlinked_video_is_skipped(self) -> None:
        real = self.movie()
        link = self.library / "Film (2020)" / "linked.mkv"
        link.symlink_to(real)
        scan = sx.discover_videos(self.library, min_bytes=1024)
        self.assertEqual(scan.videos, [real])

    def test_non_video_files_and_dotfiles_are_skipped(self) -> None:
        movie = self.movie()
        (self.library / "Film (2020)" / ".hidden.mkv").write_bytes(b"x" * 4096)
        (self.library / "Film (2020)" / "notes.txt").write_text("x", encoding="utf-8")
        self.assertEqual(sx.discover_videos(self.library, min_bytes=1024).videos, [movie])

    def test_extras_and_disc_folders_are_not_walked(self) -> None:
        movie = self.movie()
        (self.library / "Film (2020)" / "Extras").mkdir()
        (self.library / "Film (2020)" / "Extras" / "making-of.mkv").write_bytes(b"x" * 4096)
        self.assertEqual(sx.discover_videos(self.library, min_bytes=1024).videos, [movie])


class BinaryLookupTests(TempFixture):
    def test_an_explicit_path_that_exists_is_used(self) -> None:
        binary = self.root / "mkvextract"
        binary.write_bytes(b"#!/bin/sh\n")
        self.assertEqual(sx.find_mkvtoolnix_binary("mkvextract", str(binary)), str(binary))

    def test_an_explicit_name_is_looked_up_on_the_path(self) -> None:
        with mock.patch.object(sx.shutil, "which", lambda name: f"/opt/{name}"):
            self.assertEqual(sx.find_mkvtoolnix_binary("mkvmerge", "mkvmerge"), "/opt/mkvmerge")

    def test_an_explicit_path_that_does_not_exist_falls_back_to_the_path_lookup(self) -> None:
        with mock.patch.object(sx.shutil, "which", lambda name: None):
            self.assertIsNone(sx.find_mkvtoolnix_binary("mkvextract", str(self.root / "nope")))

    def test_a_standard_install_directory_is_searched_when_the_path_has_nothing(self) -> None:
        """MKVToolNix's Windows installer does not put itself on PATH."""
        install = self.root / "mkvmerge.exe"
        install.write_bytes(b"MZ")
        with mock.patch.object(sx.shutil, "which", lambda name: None), \
                mock.patch.dict(sx._MKVTOOLNIX_PATHS, {"mkvmerge": (str(install),)}, clear=True):
            self.assertEqual(sx.find_mkvtoolnix_binary("mkvmerge"), str(install))

    def test_nothing_found_anywhere_is_none(self) -> None:
        with mock.patch.object(sx.shutil, "which", lambda name: None), \
                mock.patch.dict(sx._MKVTOOLNIX_PATHS, {}, clear=True):
            self.assertIsNone(sx.find_mkvtoolnix_binary("mkvmerge"))


class ExternalCommandTests(TempFixture):
    def test_an_empty_command_is_a_failure_not_an_index_error(self) -> None:
        """The contract is that this function never raises."""
        self.assertEqual(sx.run_external_command([]),
                         (127, "", "could not run command: no program was given"))

    def test_a_command_that_times_out_answers_124(self) -> None:
        """124 is ``timeout``'s own code, and the caller prints the wait."""
        with mock.patch.object(sx.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["mkvextract"], 30)):
            self.assertEqual(sx.run_external_command(["mkvextract", "x"], timeout=30),
                             (124, "", "timed out after 30s"))

    def test_a_program_that_cannot_be_launched_answers_127(self) -> None:
        with mock.patch.object(sx.subprocess, "run", side_effect=OSError("no such file")):
            rc, out, err = sx.run_external_command(["mkvextract", "x"])
        self.assertEqual((rc, out), (127, ""))
        self.assertIn("could not run mkvextract", err)

    def test_a_successful_command_returns_its_streams_as_text(self) -> None:
        completed = subprocess.CompletedProcess(["mkvmerge"], 0, stdout=b"out\n", stderr=b"err\n")
        with mock.patch.object(sx.subprocess, "run", lambda *a, **k: completed):
            self.assertEqual(sx.run_external_command(["mkvmerge"]), (0, "out\n", "err\n"))

    def test_a_command_with_no_output_says_so(self) -> None:
        """An empty tail would leave the report reading "failed: "."""
        self.assertEqual(sx._command_tail("   \n\n"), "no output")
        self.assertEqual(sx._command_tail("one\ntwo\nthree\nfour"), "two | three | four")


class TrackLabelTests(unittest.TestCase):
    def test_an_sdh_track_says_so_in_the_label_the_report_prints(self) -> None:
        """A hearing-impaired track is not the plain English one the tool wants.

        The label is what the report shows when it explains why a track was
        passed over, so the distinction has to be visible there.
        """
        track = sx.EmbeddedSubtitleTrack(track_id=2, codec_id="S_TEXT/UTF8", language="eng",
                                         name="English", kind="text", extension=".srt", sdh=True)
        self.assertIn("SDH", track.label)
        self.assertIn("track 2", track.label)
        plain = sx.EmbeddedSubtitleTrack(track_id=3, codec_id="S_TEXT/UTF8", language="eng",
                                         name="", kind="text", extension=".srt")
        self.assertNotIn("SDH", plain.label)


class ProviderAnswerTests(unittest.TestCase):
    def test_an_answer_that_is_not_json_is_an_error_naming_the_call(self) -> None:
        with self.assertRaises(sx.OpenSubtitlesError) as caught:
            sx._json_document(b"<html>rate limited</html>", "the search")
        self.assertIn("the search did not answer with JSON", str(caught.exception))

    def test_an_answer_that_is_a_json_array_is_an_error(self) -> None:
        """The API answers with objects; an array is a proxy or a CDN talking."""
        with self.assertRaises(sx.OpenSubtitlesError) as caught:
            sx._json_document(b"[]", "the download request")
        self.assertIn("answered with a JSON list, not an object", str(caught.exception))

    def test_an_empty_answer_is_an_empty_document(self) -> None:
        self.assertEqual(sx._json_document(b"   ", "the search"), {})

    def test_an_entry_that_is_not_an_object_is_dropped(self) -> None:
        self.assertIsNone(sx._candidate_from_entry("not an entry"))

    def test_an_entry_that_is_not_a_subtitle_is_dropped(self) -> None:
        entry = provider_entry(11)
        entry["type"] = "movie"
        self.assertIsNone(sx._candidate_from_entry(entry))

    def test_an_entry_without_attributes_is_dropped(self) -> None:
        self.assertIsNone(sx._candidate_from_entry({"type": "subtitle"}))

    def test_an_entry_with_no_usable_file_id_is_dropped(self) -> None:
        entry = provider_entry(11)
        entry["attributes"]["files"] = ["not a file", {"file_id": "nonsense"}, {"file_id": 0}]
        self.assertIsNone(sx._candidate_from_entry(entry))

    def test_the_first_usable_file_in_an_entry_is_the_one_used(self) -> None:
        entry = provider_entry(11)
        entry["attributes"]["files"] = [{"file_id": "nonsense"},
                                        {"file_id": 42, "file_name": "second.srt"}]
        candidate = sx._candidate_from_entry(entry)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.file_id, 42)
        self.assertEqual(candidate.file_name, "second.srt")


class MoviehashTests(TempFixture):
    def test_a_file_that_cannot_be_read_is_an_error_not_a_hash(self) -> None:
        with self.assertRaises(OSError) as caught:
            sx.moviehash_of_file(self.root / "absent.mkv")
        self.assertIn("could not read the movie's size", str(caught.exception))

    def test_a_read_that_fails_halfway_is_an_error_not_a_hash(self) -> None:
        movie = self.movie(size=1024 * 1024)
        with mock.patch.object(Path, "open", side_effect=OSError("share went away")), \
                self.assertRaises(OSError) as caught:
            sx.moviehash_of_file(movie)
        self.assertIn("could not read the movie", str(caught.exception))

    def test_a_file_that_shrinks_while_it_is_being_hashed_is_refused(self) -> None:
        """A hash of a moving target matches nothing, and silently wastes a request.

        The provider's whole value here is that it matched *this file*; a hash
        computed from half of one file and half of another is worse than no hash.
        """
        movie = self.movie(size=1024 * 1024)
        real_open = Path.open

        def shrinking(path: Path, *args: object, **kwargs: object) -> object:
            handle = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
            movie_path = path
            del movie_path
            real_read = handle.read

            def read(size: int = -1) -> bytes:
                return real_read(1) if size > 64 else real_read(size)

            handle.read = read  # type: ignore[method-assign]
            return handle

        with mock.patch.object(Path, "open", shrinking), self.assertRaises(ValueError) as caught:
            sx.moviehash_of_file(movie)
        self.assertIn("changed size while it was being hashed", str(caught.exception))

    def test_a_file_below_the_provider_floor_is_refused(self) -> None:
        small = self.movie("Tiny (2020)", size=1024)
        with self.assertRaises(ValueError) as caught:
            sx.moviehash_of_file(small)
        self.assertIn("OpenSubtitles hashes need at least", str(caught.exception))


def client(**kwargs: object) -> sx.OpenSubtitlesClient:
    settings: dict[str, object] = {"user_agent": "organizekit v4.0.0", "timeout_seconds": 1.0}
    settings.update(kwargs)
    return sx.OpenSubtitlesClient("test-key", **settings)  # type: ignore[arg-type]


class ClientProtocolTests(unittest.TestCase):
    """The provider conversation, with ``urlopen`` replaced by the fake transport."""

    def test_a_login_without_an_account_says_which_variables_to_set(self) -> None:
        with self.assertRaises(sx.OpenSubtitlesError) as caught:
            client().login()
        message = str(caught.exception)
        self.assertIn("OPENSUBTITLES_USERNAME", message)
        self.assertIn("OPENSUBTITLES_PASSWORD", message)

    def test_a_login_that_returns_no_token_is_an_error(self) -> None:
        """A token nobody can use would make every later call fail anonymously."""
        transport = FakeTransport(json.dumps({"status": 200}).encode("utf-8"))
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError) as caught:
            client(username="u", password="p").login()
        self.assertIn("accepted the login without a token", str(caught.exception))

    def test_a_successful_login_is_reused_for_the_rest_of_the_run(self) -> None:
        transport = FakeTransport(json.dumps({"token": "abc123"}).encode("utf-8"))
        account = client(username="u", password="p")
        with mock.patch.object(sx, "urlopen", transport):
            self.assertEqual(account.session_token(), "abc123")
            self.assertEqual(account.session_token(), "abc123")
        self.assertEqual(len(transport.requests), 1, "one login per run, not one per call")

    def test_a_refresh_logs_in_again(self) -> None:
        transport = FakeTransport(json.dumps({"token": "first"}).encode("utf-8"),
                                  json.dumps({"token": "second"}).encode("utf-8"))
        account = client(username="u", password="p")
        with mock.patch.object(sx, "urlopen", transport):
            self.assertEqual(account.session_token(), "first")
            self.assertEqual(account.session_token(refresh=True), "second")
        self.assertEqual(len(transport.requests), 2)

    def test_an_http_error_carries_the_providers_own_message(self) -> None:
        transport = FakeTransport(http_error(403, b'{"message": "Invalid API key"}'))
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError) as caught:
            client().search_by_movie_hash("0" * 16)
        self.assertIn("OpenSubtitles answered HTTP 403: Invalid API key", str(caught.exception))

    def test_an_http_error_with_no_body_falls_back_to_its_reason(self) -> None:
        transport = FakeTransport(http_error(500))
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError) as caught:
            client().search_by_movie_hash("0" * 16)
        self.assertIn("OpenSubtitles answered HTTP 500", str(caught.exception))

    def test_an_http_error_whose_body_cannot_be_read_is_still_reported(self) -> None:
        broken = http_error(502, b"")
        with mock.patch.object(broken, "read", side_effect=OSError("connection reset")):
            self.assertIn("HTTP 502", sx.OpenSubtitlesClient._http_error(broken))

    def test_a_network_that_cannot_be_reached_is_an_error_not_a_retry_forever(self) -> None:
        transport = FakeTransport(URLError("no route to host"))
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError) as caught:
            client().search_by_movie_hash("0" * 16)
        self.assertIn("could not reach OpenSubtitles", str(caught.exception))

    def test_an_oversized_answer_is_refused(self) -> None:
        """The read is bounded, so a huge document is caught before it is parsed."""
        huge = b"x" * (sx.OPENSUBTITLES_ANSWER_MAX_BYTES + 2)
        transport = FakeTransport(huge)
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError) as caught:
            client().search_by_movie_hash("0" * 16)
        self.assertIn("oversized document", str(caught.exception))

    def test_the_providers_own_retry_after_is_honoured(self) -> None:
        error = http_error(429, b'{"message": "rate limit"}', {"Retry-After": "7"})
        self.assertEqual(sx.OpenSubtitlesClient._backoff_seconds(error, 1), 7.0)

    def test_a_retry_after_nobody_can_read_falls_back_to_doubling(self) -> None:
        for headers, attempt, expected in (({}, 1, 2.0), ({}, 3, 8.0),
                                           ({"Retry-After": "soon"}, 2, 4.0)):
            with self.subTest(headers=headers, attempt=attempt):
                error = http_error(429, b"", headers)
                self.assertEqual(sx.OpenSubtitlesClient._backoff_seconds(error, attempt), expected)

    def test_the_backoff_is_capped(self) -> None:
        error = http_error(429, b"", {"Retry-After": "999999"})
        self.assertEqual(sx.OpenSubtitlesClient._backoff_seconds(error, 1),
                         sx.OPENSUBTITLES_MAX_BACKOFF_SEC)

    def test_a_ratelimit_reset_in_the_past_is_not_a_wait(self) -> None:
        """``X-RateLimit-Reset`` is an epoch second; an old one must not be a delay."""
        error = http_error(429, b"", {"X-RateLimit-Reset": "1"})
        self.assertEqual(sx.OpenSubtitlesClient._backoff_seconds(error, 2), 4.0)

    def test_headers_that_cannot_be_read_do_not_stop_the_backoff(self) -> None:
        """A proxy can hand back a headers object that is not a mapping at all."""
        class BadHeaders:
            def get(self, name: str, default: object = None) -> object:
                raise AttributeError("this response has no headers")

        error = HTTPError("https://api.opensubtitles.com/api/v1/subtitles", 429, "error",
                          BadHeaders(), io.BytesIO(b""))  # type: ignore[arg-type]
        self.assertEqual(sx.OpenSubtitlesClient._backoff_seconds(error, 1), 2.0)

    def test_a_download_answer_with_no_link_is_an_error(self) -> None:
        """No link means no subtitle, and the run must not report success."""
        transport = FakeTransport(json.dumps({"remaining": 3}).encode("utf-8"))
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError) as caught:
            client().download_link(9001)
        self.assertIn("without a link", str(caught.exception))

    def test_an_expired_token_is_refreshed_once_and_the_download_retried(self) -> None:
        """A token lasts about a day; a run can outlive it.

        One fresh login, never a retry loop against the credentials endpoint -
        which is rate limited precisely because of that.
        """
        transport = FakeTransport(
            http_error(401, b'{"message": "Token expired"}'),
            json.dumps({"token": "fresh"}).encode("utf-8"),
            download_answer(),
        )
        account = client(username="u", password="p")
        account._token = "stale"
        with mock.patch.object(sx, "urlopen", transport):
            outcome = account.download_link(9001)
        self.assertIn("dl.opensubtitles.com", outcome.link)
        self.assertEqual(account._token, "fresh")
        self.assertEqual(len(transport.requests), 3)

    def test_an_expired_token_without_an_account_is_not_retried(self) -> None:
        transport = FakeTransport(http_error(401, b'{"message": "Token expired"}'))
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError):
            client().download_link(9001)
        self.assertEqual(len(transport.requests), 1)

    def test_the_doctor_probe_reports_the_key_works(self) -> None:
        transport = FakeTransport(json.dumps({"data": ["srt", "ass", "vtt"]}).encode("utf-8"))
        with mock.patch.object(sx, "urlopen", transport):
            self.assertEqual(client().probe(), "3 subtitle format(s) offered")

    def test_the_doctor_probe_answers_even_when_the_shape_is_unexpected(self) -> None:
        transport = FakeTransport(json.dumps({"data": "not a list"}).encode("utf-8"))
        with mock.patch.object(sx, "urlopen", transport):
            self.assertEqual(client().probe(), "the API answered")

    def test_a_subtitle_fetch_is_bounded_and_https_only(self) -> None:
        transport = FakeTransport(SRT_PAYLOAD)
        with mock.patch.object(sx, "urlopen", transport):
            self.assertEqual(client().fetch("https://dl.opensubtitles.com/x.srt"), SRT_PAYLOAD)

    def test_a_foreign_fetch_link_is_refused_before_it_is_read(self) -> None:
        """A link the provider did not sign is a redirect to somebody else's host."""
        transport = FakeTransport(SRT_PAYLOAD)
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError):
            client().fetch("http://example.invalid/subtitle.srt")
        self.assertEqual(transport.requests, [], "nothing was requested")

    def test_a_fetch_that_fails_over_http_reports_the_status(self) -> None:
        transport = FakeTransport(http_error(404, b'{"message": "gone"}'))
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError) as caught:
            client().fetch("https://dl.opensubtitles.com/x.srt")
        self.assertIn("HTTP 404", str(caught.exception))

    def test_a_fetch_that_cannot_reach_the_cdn_reports_that(self) -> None:
        transport = FakeTransport(URLError("no route"))
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError) as caught:
            client().fetch("https://dl.opensubtitles.com/x.srt")
        self.assertIn("could not download the subtitle", str(caught.exception))

    def test_an_oversized_subtitle_is_refused(self) -> None:
        transport = FakeTransport(b"x" * (sx.MAX_SUBTITLE_BYTES + 2))
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError) as caught:
            client().fetch("https://dl.opensubtitles.com/x.srt")
        self.assertIn("safety limit", str(caught.exception))

    def test_a_search_answer_is_turned_into_candidates(self) -> None:
        transport = FakeTransport(search_answer(provider_entry(9001), provider_entry(0)))
        with mock.patch.object(sx, "urlopen", transport):
            candidates = client().search_by_movie_hash("0" * 16)
        self.assertEqual([candidate.file_id for candidate in candidates], [9001],
                         "an entry with no usable file is dropped, not repaired")

    def test_a_search_answer_with_no_data_block_is_an_error(self) -> None:
        transport = FakeTransport(json.dumps({"total_count": 0}).encode("utf-8"))
        with mock.patch.object(sx, "urlopen", transport), self.assertRaises(sx.OpenSubtitlesError):
            client().search_by_movie_hash("0" * 16)

    def test_a_rate_limited_search_waits_and_is_retried(self) -> None:
        transport = FakeTransport(http_error(429, b'{"message": "slow down"}',
                                             {"Retry-After": "1"}),
                                  search_answer(provider_entry(9001)))
        slept: list[float] = []
        with mock.patch.object(sx, "urlopen", transport), \
                mock.patch.object(sx.time, "sleep", slept.append):
            candidates = client().search_by_movie_hash("0" * 16)
        self.assertEqual(len(candidates), 1)
        self.assertIn(1.0, slept)

    def test_a_rate_limit_that_never_lets_up_is_reported_once(self) -> None:
        transport = FakeTransport(*[http_error(429, b"", {"Retry-After": "1"})]
                                  * (sx.OPENSUBTITLES_MAX_ATTEMPTS + 1))
        with mock.patch.object(sx, "urlopen", transport), \
                mock.patch.object(sx.time, "sleep", lambda seconds: None), \
                self.assertRaises(sx.OpenSubtitlesError) as caught:
            client().search_by_movie_hash("0" * 16)
        self.assertIn("kept answering HTTP 429", str(caught.exception))

    def test_the_throttle_keeps_a_polite_gap_between_requests(self) -> None:
        """The provider allows five requests a second per IP."""
        account = client()
        account._last_request_at = time.monotonic()
        slept: list[float] = []
        with mock.patch.object(sx.time, "sleep", slept.append), \
                mock.patch.object(sx.time, "monotonic", lambda: account._last_request_at):
            account._throttle()
        self.assertEqual(len(slept), 1)
        self.assertLessEqual(slept[0], sx.OPENSUBTITLES_MIN_INTERVAL_SEC)


if __name__ == "__main__":
    unittest.main()
