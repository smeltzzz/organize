"""The fetcher's last mile: the branches nothing else in the suite reaches.

`subtitle_fetcher.py` sat at 94% line coverage with the remaining ~200 lines
scattered in ones and twos across forty functions — the refusal arms of pure
helpers, the malformed-document paths in the format converters, the
"MKVToolNix answered something impossible" arms of the extraction path, and
the last few error returns in the queue. None of them is a block worth its own
phase, and every one of them is a line that only ever runs on a bad day.

That is exactly the argument for testing them: a branch that only executes
when something has already gone wrong is a branch nobody notices is broken.
What each test below pins is the *refusal* — no crash, no half-written
sidecar, one sentence a report can print.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import subtitle_fetcher as sf

SRT = "1\n00:00:01,000 --> 00:00:04,000\nHello.\n"


class TimestampPaddingTests(unittest.TestCase):
    """Every timestamp this tool writes is padded to ``HH:MM:SS,mmm``."""

    def test_a_well_formed_timestamp_is_normalised(self) -> None:
        self.assertEqual(sf._pad_srt_timestamp("1:02:03.4"), "01:02:03,400")

    def test_a_timestamp_with_no_milliseconds_field_is_refused(self) -> None:
        self.assertIsNone(sf._pad_srt_timestamp("000102"))

    def test_a_clock_that_is_not_three_parts_is_refused(self) -> None:
        self.assertIsNone(sf._pad_srt_timestamp("02:03,400"))

    def test_a_clock_that_is_not_numeric_is_refused(self) -> None:
        self.assertIsNone(sf._pad_srt_timestamp("aa:bb:cc,400"))


class SrtCueParsingTests(unittest.TestCase):
    """Anything this tool re-renders is parsed first; junk blocks are dropped."""

    def test_a_block_with_no_timing_line_is_dropped(self) -> None:
        self.assertEqual(sf.parse_srt_cues("1\njust a note\n"), [])

    def test_a_block_whose_timing_line_is_malformed_is_dropped(self) -> None:
        self.assertEqual(sf.parse_srt_cues("1\n0:1 --> 0:2\nHello\n"), [])

    def test_a_cue_with_no_text_is_dropped(self) -> None:
        self.assertEqual(sf.parse_srt_cues("1\n00:00:01,000 --> 00:00:02,000\n"), [])

    def test_a_cue_whose_timestamp_will_not_pad_is_dropped(self) -> None:
        with mock.patch.object(sf, "_pad_srt_timestamp", return_value=None):
            self.assertEqual(sf.parse_srt_cues(SRT), [])

    def test_a_good_cue_survives_and_is_renumbered(self) -> None:
        text = "7\n00:00:01,000 --> 00:00:04,000\nHello.\n"
        self.assertEqual(sf.normalize_extracted_srt(text), SRT)


class AssConversionTests(unittest.TestCase):
    """ASS/SSA is a styling format; only complete Dialogue rows become cues."""

    HEADER = "[Script Info]\nTitle: x\n\n[Events]\nFormat: Layer, Start, End, Style, Text\n"

    def convert(self, body: str) -> str:
        return sf.ass_to_srt(self.HEADER + body)

    def test_a_dialogue_row_becomes_a_cue(self) -> None:
        out = self.convert("Dialogue: 0,0:00:01.00,0:00:04.00,Default,{\\i1}Hello.\n")
        self.assertEqual(out, "1\n00:00:01,000 --> 00:00:04,000\nHello.\n")

    def test_a_comment_row_is_not_a_cue(self) -> None:
        self.assertEqual(self.convert("Comment: 0,0:00:01.00,0:00:04.00,Default,Note\n"), "")

    def test_a_line_that_is_neither_dialogue_nor_comment_is_ignored(self) -> None:
        self.assertEqual(self.convert("Something: else\n"), "")

    def test_dialogue_before_a_format_line_is_ignored(self) -> None:
        """Column order differs between SSA and ASS, so guessing is not allowed."""
        text = "[Events]\nDialogue: 0,0:00:01.00,0:00:04.00,Default,Hello.\n"
        self.assertEqual(sf.ass_to_srt(text), "")

    def test_a_row_with_fewer_columns_than_the_format_promises_is_dropped(self) -> None:
        self.assertEqual(self.convert("Dialogue: 0,0:00:01.00\n"), "")

    def test_a_row_with_an_unreadable_timestamp_is_dropped(self) -> None:
        self.assertEqual(self.convert("Dialogue: 0,later,much later,Default,Hello.\n"), "")

    def test_a_row_that_is_pure_styling_is_dropped(self) -> None:
        self.assertEqual(self.convert("Dialogue: 0,0:00:01.00,0:00:04.00,Default,{\\pos(1,2)}\n"), "")


class VttConversionTests(unittest.TestCase):
    """WebVTT carries cue settings, notes and regions SRT has no place for."""

    def test_a_cue_survives_the_conversion(self) -> None:
        out = sf.vtt_to_srt("WEBVTT\n\n00:00:01.000 --> 00:00:04.000 line:0%\nHello.\n")
        self.assertEqual(out, "1\n00:00:01,000 --> 00:00:04,000\nHello.\n")

    def test_a_byte_order_mark_does_not_hide_the_header(self) -> None:
        out = sf.vtt_to_srt("\ufeffWEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHello.\n")
        self.assertEqual(out, "1\n00:00:01,000 --> 00:00:04,000\nHello.\n")

    def test_a_block_with_no_timing_line_is_dropped(self) -> None:
        self.assertEqual(sf.vtt_to_srt("WEBVTT\n\nan orphan caption\n"), "")

    def test_a_timing_line_that_is_not_a_timing_line_is_dropped(self) -> None:
        self.assertEqual(sf.vtt_to_srt("WEBVTT\n\n1 --> 2\nHello.\n"), "")

    def test_a_cue_whose_timestamp_will_not_pad_is_dropped(self) -> None:
        with mock.patch.object(sf, "_pad_srt_timestamp", return_value=None):
            self.assertEqual(sf.vtt_to_srt("WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHi\n"), "")

    def test_a_cue_with_a_timing_line_and_nothing_else_is_dropped(self) -> None:
        self.assertEqual(sf.vtt_to_srt("WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n"), "")

    def test_notes_regions_and_styles_are_not_cues(self) -> None:
        for block in ("NOTE what follows", "STYLE ::cue {}", "REGION id:x"):
            with self.subTest(block=block):
                self.assertEqual(sf.vtt_to_srt(f"WEBVTT\n\n{block}\n"), "")


class SubtitleTextGuardTests(unittest.TestCase):
    """The cheap guards in front of every payload this tool accepts."""

    def test_an_empty_document_is_not_an_srt(self) -> None:
        self.assertFalse(sf.looks_like_srt_text(""))

    def test_a_document_past_the_size_ceiling_is_not_read(self) -> None:
        self.assertFalse(sf.looks_like_srt_text("x" * (4 * 1024 * 1024 + 1)))

    def test_empty_bytes_are_not_a_subtitle(self) -> None:
        self.assertFalse(sf.valid_srt_bytes(b""))

    def test_bytes_past_the_size_ceiling_are_not_a_subtitle(self) -> None:
        self.assertFalse(sf.valid_srt_bytes(b"x" * (4 * 1024 * 1024 + 1)))

    def test_bytes_that_are_not_text_at_all_are_not_a_subtitle(self) -> None:
        self.assertFalse(sf.valid_srt_bytes(b"\x00\x81\xfe" * 40))

    def test_a_short_line_is_never_called_cyrillic(self) -> None:
        """Under eight letters there is nothing to be confident about."""
        self.assertFalse(sf.mostly_cyrillic("Да"))

    def test_a_cyrillic_subtitle_is_recognised(self) -> None:
        self.assertTrue(sf.mostly_cyrillic("Здравейте всички приятели"))

    def test_two_empty_titles_do_not_match(self) -> None:
        self.assertFalse(sf.titles_match("", "The Dark Knight"))

    def test_a_title_of_pure_punctuation_scores_zero(self) -> None:
        self.assertEqual(sf.title_similarity("!!!", "The Dark Knight"), 0.0)

    def test_a_relative_url_is_joined_to_the_site_root(self) -> None:
        self.assertEqual(sf.absolute_url("https://example.test", "subs/1.zip"),
                         "https://example.test/subs/1.zip")

    def test_an_absolute_url_is_left_alone(self) -> None:
        self.assertEqual(sf.absolute_url("https://example.test", "https://cdn.test/x"),
                         "https://cdn.test/x")


class SidecarIdentityTests(unittest.TestCase):
    def test_a_covering_sidecar_is_recognised_by_name(self) -> None:
        movie = Path("/library/Movie (2020)/Movie (2020).mkv")
        self.assertTrue(sf.is_covering_english_sidecar(
            movie.with_name("Movie (2020).eng.srt"), movie))

    def test_another_movies_sidecar_is_not_covering(self) -> None:
        movie = Path("/library/Movie (2020)/Movie (2020).mkv")
        self.assertFalse(sf.is_covering_english_sidecar(
            movie.with_name("Other (2019).eng.srt"), movie))


class VideoIdentityTests(unittest.TestCase):
    """The moviehash transaction is guarded by a no-follow file snapshot."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="fetcher_snapshot_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.movie = self.root / "Movie (2020).mkv"
        self.movie.write_bytes(b"x" * 4096)

    def test_a_symlinked_movie_has_no_usable_identity(self) -> None:
        link = self.root / "link.mkv"
        link.symlink_to(self.movie)
        with self.assertRaises(OSError):
            sf.video_snapshot(link)

    def test_a_movie_that_disappeared_does_not_match_its_snapshot(self) -> None:
        snapshot = sf.video_snapshot(self.movie)
        self.movie.unlink()
        self.assertFalse(sf.video_snapshot_matches(self.movie, snapshot))

    def test_a_file_too_small_to_hash_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sf.moviehash_bytes(b"x" * 16)


class NumberCoercionTests(unittest.TestCase):
    """Provider payloads are JSON from someone else's database."""

    def test_a_number_is_truthy_when_it_is_not_zero(self) -> None:
        self.assertTrue(sf.as_bool(1))
        self.assertTrue(sf.as_bool(0.5))
        self.assertFalse(sf.as_bool(0))

    def test_a_word_is_read_as_a_flag(self) -> None:
        self.assertTrue(sf.as_bool("yes"))
        self.assertTrue(sf.as_bool(" TRUE "))
        self.assertFalse(sf.as_bool("nope"))
        self.assertFalse(sf.as_bool(None))

    def test_a_missing_or_unreadable_count_is_zero(self) -> None:
        self.assertEqual(sf._nonnegative_int(None), 0)
        self.assertEqual(sf._nonnegative_int("many"), 0)
        self.assertEqual(sf._nonnegative_int(-4), 0)

    def test_a_missing_or_unreadable_score_is_zero(self) -> None:
        self.assertEqual(sf._nonnegative_float(None), 0.0)
        self.assertEqual(sf._nonnegative_float("high"), 0.0)
        self.assertEqual(sf._nonnegative_float(-1.5), 0.0)

    def test_bytes_that_decode_to_nothing_readable_still_return_text(self) -> None:
        """The caller quotes the rejected payload back at the operator."""
        self.assertIn("\ufffd", sf.decode_subtitle_bytes(b"\xff\x81\xfe"))


class ProviderVocabularyTests(unittest.TestCase):
    """A provider key this build does not know is a defect, not a bad day."""

    def setUp(self) -> None:
        self.cfg = sf.QueueConfig(library=Path("/library"), log_file=None,
                                  report_file=Path("/tmp/report.txt"), scrape_daily_cap=5)

    def test_each_known_provider_has_a_cap_a_field_and_a_label(self) -> None:
        for provider in (sf.PROVIDER_OPENSUBTITLES, sf.PROVIDER_SUBDL,
                         sf.scrape_provider_keys()[0]):
            with self.subTest(provider=provider):
                self.assertIsInstance(sf.provider_daily_cap(self.cfg, provider), int)
                self.assertTrue(sf.provider_reservation_field(provider))
                self.assertTrue(sf.provider_success_field(provider))
                self.assertTrue(sf.provider_label(provider))

    def test_an_unknown_provider_raises_rather_than_guessing_a_cap(self) -> None:
        with self.assertRaises(ValueError):
            sf.provider_daily_cap(self.cfg, "nope")
        with self.assertRaises(ValueError):
            sf.provider_reservation_field("nope")
        with self.assertRaises(ValueError):
            sf.provider_success_field("nope")

    def test_a_provider_with_no_label_is_printed_as_its_key(self) -> None:
        self.assertEqual(sf.provider_label("nope"), "nope")

    def test_scraping_is_off_when_every_source_is_skipped(self) -> None:
        cfg = sf.QueueConfig(library=Path("/library"), log_file=None,
                             report_file=Path("/tmp/report.txt"), scrape_daily_cap=5,
                             skip_sources=sf.scrape_provider_keys())
        self.assertFalse(sf.scrape_sources_enabled(cfg))

    def test_a_zero_cap_disables_the_whole_scraping_tier(self) -> None:
        cfg = sf.QueueConfig(library=Path("/library"), log_file=None,
                             report_file=Path("/tmp/report.txt"), scrape_daily_cap=0)
        self.assertEqual(sf.active_scrape_sources(cfg), ())


class DailyCapTests(unittest.TestCase):
    """A cap the provider never promised is refused at startup, not at run time."""

    def test_an_unknown_auth_mode_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sf.resolve_daily_cap("telepathy", 0)

    def test_a_cap_below_one_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sf.resolve_daily_cap(sf.AUTH_MODE_USER, -1)

    def test_a_cap_above_the_documented_limit_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sf.resolve_daily_cap(sf.AUTH_MODE_USER, sf.USER_DAILY_CAP + 1)

    def test_a_subdl_cap_below_one_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sf.resolve_subdl_daily_cap(-1)

    def test_a_scrape_cap_below_zero_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sf.resolve_scrape_daily_cap(-1)

    def test_zero_means_the_documented_default(self) -> None:
        self.assertEqual(sf.resolve_daily_cap(sf.AUTH_MODE_USER, 0), sf.USER_DAILY_CAP)
        self.assertEqual(sf.resolve_subdl_daily_cap(0), sf.SUBDL_DEFAULT_DAILY_CAP)
        self.assertEqual(sf.resolve_scrape_daily_cap(None), sf.SCRAPE_DEFAULT_SEARCH_DAILY_CAP)


class LedgerCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="fetcher_ledger_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_no_log_file_means_no_checkpoint_and_no_error(self) -> None:
        state = {"library": str(self.root), "days": {}, "movies": {}, "_dirty_movies": {"a"}}
        sf.persist_state(state, None)
        self.assertEqual(state["_dirty_movies"], {"a"})

    def test_a_checkpoint_that_cannot_be_written_stops_the_run(self) -> None:
        """Silently continuing would re-spend today's allowance tomorrow."""
        state = {"library": str(self.root), "days": {}, "movies": {}, "_dirty_movies": set()}
        with mock.patch.object(Path, "open", side_effect=OSError("read-only")), \
             self.assertRaises(RuntimeError) as caught:
            sf.persist_state(state, self.root / "run.log")
        self.assertIn("could not persist", str(caught.exception))

    def test_a_counter_that_is_not_a_number_is_reset_to_zero(self) -> None:
        state = {"days": {"2026-01-01": {"opensubtitles_download_requests_reserved": "lots"}}}
        ledger = sf.day_ledger(state, "2026-01-01")
        self.assertEqual(ledger["opensubtitles_download_requests_reserved"], 0)


class ExtractionLedgerTests(unittest.TestCase):
    """The record of "this sidecar came from the movie" is best effort."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="fetcher_extracted_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.ledger = self.root / "extracted.json"
        self.video = self.root / "Movie (2020).mkv"
        self.video.write_bytes(b"x" * 2048)
        self.sidecar = self.root / "Movie (2020).eng.srt"
        self.sidecar.write_text(SRT, encoding="utf-8")
        self.track = sf.EmbeddedSubtitleTrack(
            track_id=3, codec_id="S_TEXT/UTF8", language="eng", name="English",
            kind="text", extension=".srt", sdh=True,
        )

    def _record(self, **kwargs) -> bool:
        return sf.record_extracted_sidecar(
            self.video, self.sidecar, track=self.track, method="text",
            cue_count=42, sha256="abc", path=self.ledger, **kwargs)

    def test_a_damaged_ledger_reads_as_an_empty_one(self) -> None:
        self.ledger.write_text("[]", encoding="utf-8")
        self.assertEqual(sf.load_extracted_ledger(self.ledger)["sidecars"], {})

    def test_a_ledger_whose_sidecars_field_is_the_wrong_shape_reads_as_empty(self) -> None:
        self.ledger.write_text(json.dumps({"version": 1, "sidecars": []}), encoding="utf-8")
        self.assertEqual(sf.load_extracted_ledger(self.ledger)["sidecars"], {})

    def test_a_recorded_sidecar_names_the_track_it_came_from(self) -> None:
        self.assertTrue(self._record())
        entry = next(iter(sf.load_extracted_ledger(self.ledger)["sidecars"].values()))
        self.assertEqual(entry["track_id"], 3)
        self.assertEqual(entry["movie_size"], 2048)
        self.assertEqual(entry["cue_count"], 42)

    def test_a_movie_that_cannot_be_stat_ed_is_recorded_with_no_size(self) -> None:
        self.video.unlink()
        self.assertTrue(self._record())
        entry = next(iter(sf.load_extracted_ledger(self.ledger)["sidecars"].values()))
        self.assertEqual((entry["movie_size"], entry["movie_mtime_ns"]), (0, 0))

    def test_a_ledger_that_cannot_be_written_is_reported_not_raised(self) -> None:
        with mock.patch.object(sf, "atomic_write_json", side_effect=OSError("read-only")):
            self.assertFalse(self._record())

    def test_the_sdh_flag_is_part_of_the_tracks_label(self) -> None:
        self.assertIn("SDH", self.track.label)


class OcrBackendTests(unittest.TestCase):
    def test_an_unknown_backend_key_is_a_defect_not_a_bad_day(self) -> None:
        backend = sf.OcrBackend(key="telepathy", label="Telepathy",
                                program=("telepathy",), supports=frozenset({"image"}))
        with self.assertRaises(ValueError):
            backend.build_command(Path("in.sup"), Path("out.srt"), track_id=0, language="eng")

    def test_pgstosrt_without_dotnet_is_not_available(self) -> None:
        with mock.patch.object(sf.shutil, "which", return_value=None), \
             mock.patch.object(Path, "is_file", return_value=True):
            self.assertIsNone(sf._pgstosrt_program("/opt/PgsToSrt.dll"))

    def test_an_ocr_output_directory_that_cannot_be_listed_yields_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "track3.sup"
            source.write_bytes(b"x")
            expected = Path(td) / "track3.srt"
            with mock.patch.object(Path, "glob", side_effect=OSError("gone")):
                self.assertIsNone(sf.find_sibling_srt(source, expected))


class FieldSmokeTestTests(unittest.TestCase):
    """``subtitle_fetcher.py --self-test`` is the copy-on-a-NAS check.

    The body itself is executed by ``tests/test_selftests.py``, from a clean
    import — ``tests/selftests`` rebinds ``sf.run_self_tests`` to the moved
    suite for the rest of this process, so the shipped one is unreachable from
    here. What is pinned here is the routing: ``--self-test`` must not touch a
    library, a report or a network.
    """

    def test_main_routes_self_test_without_touching_a_library(self) -> None:
        with mock.patch.object(sf, "run_self_tests", return_value=0) as smoke:
            self.assertEqual(sf.main(["--self-test"]), 0)
        smoke.assert_called_once_with()


class ZipPayloadTests(unittest.TestCase):
    """A provider zip is opened defensively: it is a file from the internet."""

    @staticmethod
    def _zip(name: str = "movie.srt", text: str = SRT) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name, text)
        return buffer.getvalue()

    def test_a_member_that_reads_longer_than_it_declared_is_refused(self) -> None:
        """The central directory can lie; the bytes that came out are what counts."""
        payload = self._zip()
        self.assertLess(len(payload), 600)
        with mock.patch.object(sf.zipfile.ZipFile, "open",
                               return_value=io.BytesIO(b"x" * 1000)), \
             self.assertRaises(RuntimeError) as caught:
            sf.decode_subdl_srt_payload(payload, 600)
        self.assertIn("safety limit", str(caught.exception))

    def test_an_archive_with_no_srt_says_so(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            sf.decode_subdl_srt_payload(self._zip(name="readme.txt"), 1024 * 1024)
        self.assertIn("no usable .srt", str(caught.exception))

    def test_an_unreadable_archive_is_one_sentence_not_a_traceback(self) -> None:
        payload = bytearray(self._zip())
        payload[40:60] = b"\x00" * 20
        with self.assertRaises(RuntimeError) as caught:
            sf.decode_subdl_srt_payload(bytes(payload), 1024 * 1024)
        self.assertIn("could not be read safely", str(caught.exception))

    def test_a_payload_bigger_than_the_limit_is_refused_unopened(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            sf.decode_subdl_srt_payload(b"x" * 100, 10)
        self.assertIn("safety limit", str(caught.exception))


class ProviderPayloadTests(unittest.TestCase):
    """Every provider document is remote input, and some of it is nonsense.

    These are the "the API answered with a shape nobody documented" arms: a
    subtitle list that is not a list, a record that is not an object, a
    download reference that cannot be turned into a URL. Each one has to end
    in *no candidate*, never in a guess.
    """

    IDENTITY = sf.MovieIdentity(title="Dune", year=1984, normalized_title="dune")

    def feature(self, **overrides: object) -> dict:
        record = {"name": "Dune", "year": 1984, "type": "movie", "imdb_id": "tt0087182"}
        record.update(overrides)
        return record

    # -- OpenSubtitles ------------------------------------------------------

    def test_a_search_result_that_is_not_an_object_is_skipped(self) -> None:
        self.assertEqual(sf.parse_candidates({"data": ["not a record"]}), [])

    def test_a_search_result_with_no_files_is_skipped(self) -> None:
        payload = {"data": [{"attributes": {"release": "Dune", "files": []}}]}
        self.assertEqual(sf.parse_candidates(payload), [])

    def test_a_file_record_with_no_file_id_is_skipped(self) -> None:
        payload = {"data": [{"attributes": {"files": [{"file_name": "dune.srt"}]}}]}
        self.assertEqual(sf.parse_candidates(payload), [])

    def test_feature_details_that_are_not_an_object_are_ignored(self) -> None:
        payload = {"data": [{"attributes": {
            "feature_details": "Dune (1984)",
            "files": [{"file_id": 7, "file_name": "dune.srt"}],
        }}]}
        found = sf.parse_candidates(payload)
        self.assertEqual([c.file_id for c in found], [7])
        self.assertEqual(found[0].feature_title, "")

    # -- SubDL identity -----------------------------------------------------

    def test_the_filename_route_will_not_fall_back_to_a_title_result(self) -> None:
        """``require_match`` means the match record or nothing."""
        payload = {"results": [self.feature()]}
        self.assertIsNone(sf._subdl_exact_feature(payload, self.IDENTITY, require_match=True))

    def test_the_title_route_reads_the_match_record_when_results_are_empty(self) -> None:
        payload = {"results": [], "match": self.feature()}
        self.assertEqual(sf._subdl_exact_feature(payload, self.IDENTITY),
                         ("Dune", 1984, 87182))

    def test_a_response_with_no_identity_at_all_matches_nothing(self) -> None:
        self.assertIsNone(sf._subdl_exact_feature({"results": "Dune"}, self.IDENTITY))

    # -- SubDL download references -----------------------------------------

    def test_an_unsafe_url_is_survivable_when_the_identifier_is_usable(self) -> None:
        """The v2 endpoint is built locally, so a bad response URL is dropped."""
        reference = sf._subdl_candidate_reference(
            {"n_id": "sub123", "url": "http://evil.example/x.srt"}, {})
        self.assertIsNotNone(reference)
        candidate_id, download = reference  # type: ignore[misc]
        self.assertEqual(candidate_id, "subdl:sub123")
        self.assertEqual(download.url, "")

    def test_an_unsafe_url_and_no_identifier_is_no_candidate(self) -> None:
        self.assertIsNone(sf._subdl_candidate_reference(
            {"url": "http://evil.example/x.srt"}, {}))

    def test_a_record_with_neither_reference_is_no_candidate(self) -> None:
        self.assertIsNone(sf._subdl_candidate_reference({"release_name": "Dune"}, {}))

    def test_a_download_answer_with_no_url_anywhere_is_not_a_redirect(self) -> None:
        self.assertIsNone(sf.subdl_download_redirect_url(
            json.dumps({"data": {"note": "queued"}}).encode("utf-8")))

    # -- SubDL search payloads ---------------------------------------------

    def client(self) -> sf.SubdlClient:
        return sf.SubdlClient("test-subdl-key")

    def parse(self, payload: dict) -> tuple[list, dict]:
        return self.client()._parse_search_payload(payload, self.IDENTITY)

    def test_a_subtitle_list_that_is_not_a_list_yields_nothing(self) -> None:
        payload = {"results": [self.feature()], "subtitles": {"one": "dune.srt"}}
        self.assertEqual(self.parse(payload), ([], {}))

    def test_a_subtitle_entry_that_is_not_an_object_is_skipped(self) -> None:
        payload = {"results": [self.feature()], "subtitles": ["dune.srt"]}
        self.assertEqual(self.parse(payload), ([], {}))

    def test_an_english_subtitle_with_nothing_to_download_is_not_a_candidate(self) -> None:
        payload = {"results": [self.feature()],
                   "subtitles": [{"language": "EN", "name": "dune.srt"}]}
        self.assertEqual(self.parse(payload), ([], {}))

    def test_a_subtitle_in_another_language_is_not_a_candidate(self) -> None:
        payload = {"results": [self.feature()],
                   "subtitles": [{"language": "FR", "n_id": "sub9", "name": "dune.srt"}]}
        self.assertEqual(self.parse(payload), ([], {}))

    def test_a_filename_search_with_no_key_never_leaves_the_machine(self) -> None:
        client = sf.SubdlClient("")
        with mock.patch.object(sf.SubdlClient, "_request_json") as request:
            self.assertEqual(client.search_filename("Dune (1984).mkv", self.IDENTITY), ([], {}))
        request.assert_not_called()

    def test_a_filename_that_is_not_a_filename_is_never_sent(self) -> None:
        with mock.patch.object(sf.SubdlClient, "_request_json") as request:
            for bad in ("", "   ", "movies/", "Dune\x00.mkv", "x" * 513 + ".mkv"):
                with self.subTest(filename=bad):
                    self.assertEqual(
                        self.client().search_filename(bad, self.IDENTITY), ([], {}))
        request.assert_not_called()


class DownloadUrlTests(unittest.TestCase):
    """A SubDL download URL is dereferenced by urllib, so it is vetted first."""

    def test_a_port_that_is_not_a_number_is_unsafe(self) -> None:
        """``urlsplit`` parses lazily: the port only fails when it is read."""
        with self.assertRaises(ValueError) as caught:
            sf.normalize_subdl_download_url("https://dl.subdl.com:notaport/subtitle/x.srt")
        self.assertIn("unsafe download URL", str(caught.exception))

    def test_a_port_that_is_not_https_is_unsafe(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe download URL"):
            sf.normalize_subdl_download_url("https://dl.subdl.com:8080/subtitle/x.srt")

    def test_credentials_in_the_url_are_unsafe(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe download URL"):
            sf.normalize_subdl_download_url("https://user:pw@dl.subdl.com/subtitle/x.srt")

    def test_a_host_that_is_not_the_download_host_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside dl.subdl.com"):
            sf.normalize_subdl_download_url("//attacker.invalid/subtitle/x.srt")

    def test_a_network_path_url_is_never_read_as_a_relative_one(self) -> None:
        """``///x`` splits to an empty host, and would otherwise be joined to
        the download host as if the provider had sent a plain path."""
        with self.assertRaisesRegex(ValueError, "invalid relative download URL"):
            sf.normalize_subdl_download_url("///subtitle/x.srt")

    def test_an_empty_url_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty download URL"):
            sf.normalize_subdl_download_url("")

    def test_the_documented_relative_shape_is_accepted(self) -> None:
        self.assertEqual(sf.normalize_subdl_download_url("/subtitle/x.srt"),
                         "https://dl.subdl.com/subtitle/x.srt")


class LibraryReadingTests(unittest.TestCase):
    """Reading the library is I/O, and I/O is allowed to fail."""

    def test_the_minimum_movie_size_is_stated_in_megabytes_and_used_in_bytes(self) -> None:
        cfg = sf.Config(library=Path("/library"), log_file=None,
                        report_file=Path("/logs/r.txt"), min_movie_size_mb=50)
        self.assertEqual(cfg.min_bytes, 50 * 1024 * 1024)

    def test_a_movie_that_shrinks_while_it_is_hashed_is_an_error(self) -> None:
        """The OSHash is a sum over fixed-size blocks; a short read is not one."""
        with self.assertRaises(ValueError) as caught:
            sf._sum_u64_le(io.BytesIO(b"12345"), 16)
        self.assertIn("short read while hashing", str(caught.exception))

    def test_a_folder_that_cannot_be_listed_has_no_english_sidecar(self) -> None:
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("nope")):
            self.assertIsNone(sf.has_english_sidecar(Path("/library/Dune (1984)"), "Dune (1984)"))

    def test_a_title_that_is_all_punctuation_is_not_an_identity(self) -> None:
        """``normalize_title`` can empty a name that the pattern accepted."""
        self.assertIsNone(sf.movie_identity_from_video(Path("!!! (2008).mkv")))


class PolicyAndBannerTests(unittest.TestCase):
    """The lines at the top of every report that say what this run can do.

    They are the operator's only warning that a run is weaker than they think
    — title/year matching switched off, one provider missing, no OCR for an
    image-only movie — so each combination has to render, not raise.
    """

    def cfg(self, **kwargs: object) -> sf.QueueConfig:
        base: dict = {"library": Path("/library"), "log_file": None,
                      "report_file": Path("/logs/report.txt"), "scrape_daily_cap": 0}
        base.update(kwargs)
        return sf.QueueConfig(**base)  # type: ignore[arg-type]

    def test_no_provider_and_no_fallback_says_so(self) -> None:
        self.assertEqual(sf.provider_policy_text(self.cfg(identity_fallback=False)),
                         "title/year fallback disabled")

    def test_a_hash_only_run_names_the_limitation(self) -> None:
        text = sf.provider_policy_text(self.cfg(identity_fallback=False, api_key="k"))
        self.assertIn("exact moviehash matching only", text)

    def test_subdl_alone_names_the_missing_moviehash_provider(self) -> None:
        text = sf.provider_policy_text(self.cfg(identity_fallback=True, subdl_api_key="k"))
        self.assertIn("SubDL only", text)
        self.assertIn("no exact moviehash provider", text)

    def test_nothing_configured_at_all_is_still_a_sentence(self) -> None:
        self.assertEqual(sf.provider_policy_text(self.cfg(identity_fallback=True)),
                         "no source configured")

    def test_a_run_with_scraping_switched_off_says_so_in_the_quota_line(self) -> None:
        text = sf.report_provider_quota_text(
            self.cfg(api_key="k", scrape_daily_cap=1),
            {"scrape_sources_enabled": [], "scrape_search_daily_cap": 20},
        )
        self.assertIn("scraping sources not configured for this run", text)

    def test_the_extraction_banner_names_the_ocr_backend(self) -> None:
        backend = sf.OcrBackend("sup2srt", "sup2srt + Tesseract", ("sup2srt",),
                                frozenset({"PGS"}))
        with mock.patch.object(sf, "find_mkvtoolnix_binary", lambda *_a, **_k: "mkvmerge"), \
             mock.patch.object(sf, "detect_ocr_backend", return_value=(backend, "")):
            text = sf.extract_banner_text(self.cfg(extract_embedded=True))
        self.assertIn("text tracks (SRT/SSA/ASS) with mkvextract", text)
        self.assertIn("image tracks (PGS/VobSub) with sup2srt + Tesseract", text)

    def test_the_extraction_banner_names_the_missing_ocr_backend(self) -> None:
        with mock.patch.object(sf, "find_mkvtoolnix_binary", lambda *_a, **_k: "mkvmerge"), \
             mock.patch.object(sf, "detect_ocr_backend",
                               return_value=(None, "install Tesseract to OCR image tracks")):
            text = sf.extract_banner_text(self.cfg(extract_embedded=True))
        self.assertIn("image tracks (PGS/VobSub) skipped: install Tesseract", text)


class ReportGroupingTests(unittest.TestCase):
    """Nothing a run produced may be missing from the report it writes."""

    def test_a_movie_stored_at_the_library_root_is_named_by_its_file(self) -> None:
        library = Path("/library")
        self.assertEqual(sf.movie_label(library / "Dune (2021).mkv", library), "Dune (2021).mkv")
        self.assertEqual(sf.movie_label(library / "Dune (2021)" / "Dune (2021).mkv", library),
                         "Dune (2021)")

    def test_a_path_outside_the_library_keeps_its_full_name(self) -> None:
        self.assertEqual(sf.relative_text(Path("/elsewhere/Dune (2021).mkv"), Path("/library")),
                         "/elsewhere/Dune (2021).mkv")

    def test_a_reason_nobody_knows_about_still_reaches_the_report(self) -> None:
        """A future status must show up as an error, never be dropped."""
        result = sf.JobResult(Path("/library/Dune (2021)/Dune (2021).mkv"), "skip",
                              "something new happened", reason="a_reason_from_the_future")
        buckets, *_rest = sf.group_results([result], {})
        self.assertEqual(buckets[sf.REASON_ERROR],
                         [(result.video, "something new happened")])


if __name__ == "__main__":
    unittest.main()
