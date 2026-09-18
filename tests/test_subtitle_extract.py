"""Tests for embedded-subtitle extraction in ``subtitle_extractor.py``.

The whole suite is offline: ``subprocess.run`` is replaced with a fake that
serves a canned ``mkvmerge -J`` payload and writes a canned subtitle track, and
``urlopen`` is replaced with a fake transport that records requests and serves
canned provider answers. No MKVToolNix, no media file and no network are
needed.

The properties pinned here are the ones that decide whether a sidecar is
trustworthy: a movie's own *text* track must only win when it is complete
English (never a forced/signs-only stream), the OpenSubtitles tier must only
ever install a subtitle the provider matched to this exact file hash, and a
sidecar built either way is recorded in the provenance ledger - while a sidecar
that was already there before this tool ran is never touched at all.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fakeprovider import (
    SRT_PAYLOAD,
    FakeTransport,
    download_answer,
    http_error,
    provider_entry,
    search_answer,
)

import subtitle_extractor as sx

ASS_TRACK = (
    "[Script Info]\nTitle: demo\n\n[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,"
    "100,0,0,1,2,2,2,10,10,10,1\n\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,{\\i1}Hello there\\NGeneral Kenobi\n"
    "Dialogue: 0,0:00:05.00,0:00:06.25,Default,,0,0,0,,Second line\n"
    "Comment: 0,0:00:09.00,0:00:10.00,Default,,0,0,0,,not shown\n"
)

USF_TRACK = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<USFSubtitles>\n"
    '  <subtitle start="00:00:01.000" end="00:00:03.000">'
    "<text>Hello <b>USF</b> &amp; friends</text></subtitle>\n"
    '  <subtitle start="00:00:04.000" end="00:00:05.000">'
    "<text>Second<br/>line</text></subtitle>\n"
    '  <subtitle start="00:00:06.000" end="00:00:07.000"><text>   </text></subtitle>\n'
    '  <subtitle start="whenever" end="00:00:09.000"><text>no timing</text></subtitle>\n'
    "</USFSubtitles>\n"
)

PGS_TRACKS = {
    "tracks": [
        {"id": 0, "type": "video", "properties": {"codec_id": "V_MPEGH/ISO/HEVC"}},
        {"id": 4, "type": "subtitles", "properties": {"codec_id": "S_HDMV/PGS", "language": "eng"}},
    ]
}

TEXT_TRACKS = {
    "tracks": [
        {"id": 1, "type": "audio", "properties": {"codec_id": "A_TRUEHD", "language": "eng"}},
        {"id": 2, "type": "subtitles",
         "properties": {"codec_id": "S_TEXT/ASS", "language": "eng", "track_name": "English"}},
    ]
}


def fake_binaries(name: str, explicit: str | None = None) -> str:
    return f"fake-{name}"


class FakeRunner:
    """Serves ``mkvmerge -J`` and ``mkvextract tracks`` from canned payloads."""

    def __init__(self, tracks: dict, payload: str = ASS_TRACK) -> None:
        self.tracks = tracks
        self.payload = payload
        self.calls: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        argv = [str(part) for part in command]
        self.calls.append(argv)
        if "-J" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.tracks).encode("utf-8"), b"")
        if len(argv) > 1 and argv[1] == "tracks":
            target = argv[-1].split(":", 1)[1]
            # Byte-exact, like mkvextract: no newline translation anywhere.
            Path(target).write_bytes(self.payload.encode("utf-8"))
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


class ConversionTests(unittest.TestCase):
    def test_ass_timings_and_styling(self) -> None:
        converted = sx.ass_to_srt(ASS_TRACK)
        self.assertIn("00:00:01,500 --> 00:00:03,000", converted)
        self.assertIn("Hello there\nGeneral Kenobi", converted, "override block and \\N handled")
        self.assertIn("Second line", converted)
        self.assertNotIn("not shown", converted, "Comment lines are not cues")
        self.assertTrue(converted.startswith("1\n"), "cues are renumbered from 1")

    def test_ssa_v4_column_order(self) -> None:
        ssa = (
            "[Script Info]\n\n[V4 Styles]\nFormat: Name, Fontname\nStyle: Default,Arial\n\n"
            "[Events]\nFormat: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: Marked=0,0:00:02.00,0:00:04.00,Default,,0,0,0,,SSA cue\n"
        )
        self.assertIn("SSA cue", sx.ass_to_srt(ssa))

    def test_webvtt_conversion(self) -> None:
        vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello VTT\n\n"
               "00:00:04.000 --> 00:00:05.500 align:start\nSecond VTT\n")
        converted = sx.vtt_to_srt(vtt)
        self.assertIn("00:00:01,000 --> 00:00:03,000", converted)
        self.assertIn("Hello VTT", converted)
        self.assertIn("Second VTT", converted)

    def test_normalization_renumbers_and_drops_cr(self) -> None:
        messy = ("5\r\n00:00:01,000 --> 00:00:02,000\r\nfirst\r\n\r\n"
                 "9\r\n00:00:03,000 --> 00:00:04,000\r\nsecond\r\n")
        fixed = sx.normalize_extracted_srt(messy)
        self.assertTrue(fixed.startswith("1\n00:00:01,000 --> 00:00:02,000\nfirst\n\n2\n"))
        self.assertNotIn("\r", fixed)

    def test_empty_ass_yields_no_cues(self) -> None:
        self.assertEqual(sx.ass_to_srt("[Script Info]\n"), "")

    def test_usf_xml_becomes_srt(self) -> None:
        """USF is rare enough to be forgotten and simple enough to convert."""
        converted = sx.usf_to_srt(USF_TRACK)
        self.assertIn("00:00:01,000 --> 00:00:03,000", converted)
        self.assertIn("Hello USF & friends", converted, "tags stripped, entities decoded")
        self.assertIn("Secondline", converted)
        self.assertTrue(converted.startswith("1\n"))

    def test_usf_cues_with_nothing_in_them_are_dropped(self) -> None:
        converted = sx.usf_to_srt(USF_TRACK)
        self.assertNotIn("00:00:06,000", converted, "a whitespace-only cue is not a cue")

    def test_a_usf_cue_with_an_unreadable_time_is_skipped(self) -> None:
        self.assertNotIn("no timing", sx.usf_to_srt(USF_TRACK))

    def test_usf_that_is_not_usf_yields_no_cues(self) -> None:
        self.assertEqual(sx.usf_to_srt("<html><body>nope</body></html>"), "")


class QualityGateTests(unittest.TestCase):
    def _cues(self, body: str, count: int = 30) -> str:
        return sx.render_srt_cues([
            (f"00:00:{index % 60:02d},000", f"00:00:{index % 60:02d},900", body)
            for index in range(count)
        ])

    def test_complete_english_track_passes(self) -> None:
        ok, reason = sx.subtitle_quality(
            self._cues("This is a line of English dialogue"))
        self.assertTrue(ok, reason)

    def test_signs_only_track_is_refused(self) -> None:
        ok, reason = sx.subtitle_quality(
            sx.render_srt_cues([("00:00:01,000", "00:00:02,000", "Only line")]))
        self.assertFalse(ok)
        self.assertIn("signs/songs-only", reason)

    def test_cyrillic_track_is_refused(self) -> None:
        ok, reason = sx.subtitle_quality(
            self._cues("Это предложение на русском языке"))
        self.assertFalse(ok)
        self.assertIn("not Latin-script", reason)

    def test_word_salad_is_refused(self) -> None:
        ok, reason = sx.subtitle_quality(
            self._cues("Qwx zp vfg blrt mnk jklqwerty"))
        self.assertFalse(ok)
        self.assertIn("does not read as English", reason)

    def test_empty_text_is_refused(self) -> None:
        ok, _reason = sx.subtitle_quality("")
        self.assertFalse(ok)


class TrackClassificationTests(unittest.TestCase):
    def _track(self, track_id: int, codec: str, **props: object) -> dict:
        properties = {"codec_id": codec}
        properties.update(props)  # type: ignore[arg-type]
        return {"id": track_id, "type": "subtitles", "properties": properties}

    def test_english_text_beats_image_and_excludes_the_rest(self) -> None:
        tracks = [
            self._track(2, "S_HDMV/PGS", language="eng", track_name="English"),
            self._track(3, "S_TEXT/ASS", language="eng", track_name="English (SDH)",
                        flag_hearing_impaired=True),
            self._track(4, "S_TEXT/UTF8", language="fre", track_name="French"),
            self._track(5, "S_TEXT/UTF8", language="eng", track_name="English forced",
                        flag_forced=True),
            self._track(6, "S_TEXT/UTF8", language="eng", track_name="Commentary"),
            {"id": 7, "type": "audio", "properties": {"codec_id": "A_AC3", "language": "eng"}},
            self._track(8, "S_VOBSUB", language="und", track_name="English"),
            self._track(9, "S_KATE", language="eng"),
        ]
        picked = sx.classify_embedded_subtitle_tracks(tracks)
        self.assertEqual([item.track_id for item in picked], [3, 2, 8])
        self.assertTrue(picked[0].sdh)
        self.assertEqual(picked[0].kind, "text")
        self.assertEqual(picked[1].kind, "image")

    def test_forced_name_is_excluded_without_a_flag(self) -> None:
        tracks = [self._track(2, "S_TEXT/UTF8", language="eng", track_name="English (Forced)")]
        self.assertEqual(sx.classify_embedded_subtitle_tracks(tracks), [])

    def test_untagged_english_name_counts_as_english(self) -> None:
        tracks = [self._track(2, "S_TEXT/UTF8", language="und", track_name="English")]
        self.assertEqual(len(sx.classify_embedded_subtitle_tracks(tracks)), 1)

    def test_unsupported_codec_is_skipped(self) -> None:
        tracks = [self._track(2, "S_TEXT/X_UNKNOWN", language="eng")]
        self.assertEqual(sx.classify_embedded_subtitle_tracks(tracks), [])

    def test_no_english_track_at_all(self) -> None:
        tracks = [self._track(2, "S_TEXT/UTF8", language="spa", track_name="Spanish")]
        self.assertEqual(sx.classify_embedded_subtitle_tracks(tracks), [])


class CommandLineTests(unittest.TestCase):
    """Flags are taken as typed, and a bad number is reported, never repaired.

    A negative ``--download-limit`` quietly meaning "no cap" would spend
    provider quota the operator was trying to bound, so the clamp that used to
    hide these is gone and ``validate_config`` - whose checks for exactly these
    ranges already existed - is the single gate.
    """

    def config(self, *argv: str) -> sx.ExtractorConfig:
        parser = sx.build_parser()
        return sx.extractor_config_from_args(
            parser.parse_args(["--source", tempfile.gettempdir(), *argv]))

    def test_the_defaults_are_offline_and_uncapped(self) -> None:
        cfg = self.config()
        self.assertTrue(cfg.download_enabled)
        self.assertEqual(cfg.download_limit, 0)
        self.assertEqual(sx.validate_config(cfg), [], "the defaults must validate")

    def test_a_negative_download_limit_is_reported_not_clamped(self) -> None:
        cfg = self.config("--download-limit", "-1")
        self.assertEqual(cfg.download_limit, -1, "the value is not silently rewritten")
        self.assertTrue(any("--download-limit" in error for error in sx.validate_config(cfg)))

    def test_a_zero_cue_floor_is_reported_not_clamped(self) -> None:
        for flag in ("--download-min-cues", "--extract-min-cues"):
            with self.subTest(flag=flag):
                cfg = self.config(flag, "0")
                self.assertTrue(any(flag in error for error in sx.validate_config(cfg)))

    def test_a_nonpositive_timeout_is_reported_not_clamped(self) -> None:
        cfg = self.config("--download-timeout", "0")
        self.assertTrue(any("--download-timeout" in error for error in sx.validate_config(cfg)))

    def test_the_no_download_switch_disables_the_tier(self) -> None:
        cfg = self.config("--no-download")
        self.assertFalse(cfg.download_enabled)
        self.assertFalse(cfg.download_options().enabled)

    def test_credentials_come_from_the_environment_and_nowhere_else(self) -> None:
        parser = sx.build_parser()
        flags = [flag for action in parser._actions for flag in action.option_strings]
        self.assertNotIn("--opensubtitles-key", flags,
                         "a key on the command line would land in shell history")
        saved = os.environ.get("OPENSUBTITLES_API_KEY")
        os.environ["OPENSUBTITLES_API_KEY"] = "  padded-key  "
        try:
            cfg = sx.extractor_config_from_args(parser.parse_args(
                ["--source", tempfile.gettempdir()]))
        finally:
            if saved is None:
                os.environ.pop("OPENSUBTITLES_API_KEY", None)
            else:
                os.environ["OPENSUBTITLES_API_KEY"] = saved
        self.assertEqual(cfg.opensubtitles_api_key, "padded-key")


class MoviehashTests(unittest.TestCase):
    """The exact-file identity every provider lookup is keyed on."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="moviehash_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_the_hash_is_the_size_plus_the_two_chunk_sums(self) -> None:
        # An all-zero file of exactly the minimum size: no chunk contributes,
        # so the hash is the file size, and the size comes back with it.
        movie = self.tmp / "Fake (2021).mkv"
        movie.write_bytes(b"\0" * (2 * 65536))
        self.assertEqual(sx.moviehash_of_file(movie), ("0000000000020000", 2 * 65536))

    def test_a_patterned_file_matches_the_documented_algorithm(self) -> None:
        """The reference implementation, written out the long way."""
        movie = self.tmp / "Fake (2021).mkv"
        movie.write_bytes(bytes(range(256)) * 4096)  # 1 MiB, well over the floor

        def reference(path: Path) -> str:
            with path.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                total = size
                for _ in range(2):
                    values = struct.unpack(f"<{65536 // 8}Q", handle.read(65536))
                    total = (total + sum(values)) & 0xFFFFFFFFFFFFFFFF
                return f"{total:016x}"

        self.assertEqual(sx.moviehash_of_file(movie)[0], reference(movie))

    def test_a_file_below_the_floor_is_refused(self) -> None:
        movie = self.tmp / "Tiny.mkv"
        movie.write_bytes(b"x" * 1024)
        with self.assertRaises(ValueError):
            sx.moviehash_of_file(movie)

    def test_a_missing_file_is_an_error_not_a_hash(self) -> None:
        with self.assertRaises(OSError):
            sx.moviehash_of_file(self.tmp / "gone.mkv")


class ProviderAnswerTests(unittest.TestCase):
    """What the API's answer means, with no HTTP involved."""

    def test_a_hash_matched_english_subtitle_is_chosen(self) -> None:
        candidates = sx.candidates_from_search(
            json.loads(search_answer(provider_entry(11))), "test")
        chosen, refusal = sx.choose_hash_match(candidates)
        self.assertEqual(refusal, "")
        assert chosen is not None
        self.assertEqual(chosen.file_id, 11)
        self.assertTrue(chosen.is_english)

    def test_a_subtitle_that_is_not_a_hash_match_is_never_chosen(self) -> None:
        candidates = sx.candidates_from_search(
            json.loads(search_answer(provider_entry(11, hash_match=False))), "test")
        chosen, refusal = sx.choose_hash_match(candidates)
        self.assertIsNone(chosen)
        self.assertIn("hash", refusal)

    def test_a_plain_dialogue_subtitle_beats_a_hearing_impaired_one(self) -> None:
        candidates = sx.candidates_from_search(json.loads(search_answer(
            provider_entry(11, hearing_impaired=True, downloads=999),
            provider_entry(12, hearing_impaired=False, downloads=1),
        )), "test")
        chosen, _ = sx.choose_hash_match(candidates)
        assert chosen is not None
        self.assertEqual(chosen.file_id, 12)

    def test_forced_partial_and_machine_translated_subtitles_are_refused(self) -> None:
        for kwargs in ({"foreign_parts_only": True}, {"machine_translated": True},
                       {"ai_translated": True}):
            with self.subTest(**kwargs):
                candidates = sx.candidates_from_search(
                    json.loads(search_answer(provider_entry(11, **kwargs))), "test")
                chosen, refusal = sx.choose_hash_match(candidates)
                self.assertIsNone(chosen)
                self.assertTrue(refusal)

    def test_a_subtitle_tagged_as_another_language_is_refused(self) -> None:
        candidates = sx.candidates_from_search(
            json.loads(search_answer(provider_entry(11, language="es"))), "test")
        chosen, refusal = sx.choose_hash_match(candidates)
        self.assertIsNone(chosen)
        self.assertIn("English", refusal)

    def test_an_entry_without_a_usable_file_id_is_dropped(self) -> None:
        document = json.loads(search_answer(provider_entry(11, included=False)))
        self.assertEqual(sx.candidates_from_search(document, "test"), [])

    def test_a_document_without_a_subtitle_list_is_an_error(self) -> None:
        with self.assertRaises(sx.OpenSubtitlesError):
            sx.candidates_from_search({"total_count": 0}, "test")

    def test_word_shaped_flags_are_read_as_flags(self) -> None:
        entry = provider_entry(11)
        entry["attributes"]["hearing_impaired"] = "true"
        entry["attributes"]["moviehash_match"] = "TRUE"
        candidates = sx.candidates_from_search(json.loads(search_answer(entry)), "test")
        self.assertTrue(candidates[0].hearing_impaired)
        self.assertTrue(candidates[0].moviehash_match)


class OpenSubtitlesClientTests(unittest.TestCase):
    """What actually leaves the machine: URL, params, headers, body."""

    def setUp(self) -> None:
        self._saved_sleep = None
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        os.environ.pop("OPENSUBTITLES_API_KEY", None)

    def client(self, **kwargs: object) -> sx.OpenSubtitlesClient:
        return sx.OpenSubtitlesClient("test-key", user_agent="organizekit v4.0.0",
                                      **kwargs)  # type: ignore[arg-type]

    def test_the_search_sends_the_hash_and_asks_for_hash_matches_only(self) -> None:
        transport = FakeTransport(search_answer(provider_entry(11)))
        with mock.patch.object(sx, "urlopen", transport):
            found = self.client().search_by_movie_hash(
                "8e245d9679d31e12", file_name="Fake (2021).mkv")
        self.assertEqual(len(found), 1)
        url = transport.urls()[0]
        self.assertIn("moviehash=8e245d9679d31e12", url)
        self.assertIn("moviehash_match=only", url)
        self.assertIn("languages=en", url)
        self.assertIn("query=Fake", url)
        self.assertNotIn("moviebytesize", url)
        self.assertEqual(transport.headers(0)["api-key"], "test-key")
        self.assertEqual(transport.headers(0)["user-agent"], "organizekit v4.0.0")

    def test_the_download_posts_the_file_id_with_both_headers(self) -> None:
        transport = FakeTransport(download_answer())
        with mock.patch.object(sx, "urlopen", transport), \
                mock.patch.object(sx.OpenSubtitlesClient, "session_token",
                                  return_value="session-token"):
            download = self.client(username="user", password="pass").download_link(11)
        self.assertEqual(download.link, "https://dl.opensubtitles.com/download/abc/Fake.2021.srt")
        self.assertEqual(download.remaining, 4)
        request = transport.requests[0]
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"file_id": 11, "sub_format": "srt"})
        self.assertEqual(transport.headers(0)["authorization"], "Bearer session-token")

    def test_a_foreign_download_link_is_refused_before_it_is_read(self) -> None:
        for link in ("https://evil.example/x.srt", "http://opensubtitles.com/x.srt",
                     "https://opensubtitles.com.evil.example/x.srt"):
            with self.subTest(link=link), self.assertRaises(sx.OpenSubtitlesError):
                sx._require_provider_link(link)

    def test_a_rate_limited_request_waits_and_is_retried(self) -> None:
        transport = FakeTransport(http_error(429, b'{"message": "API rate limit exceeded"}',
                                             {"Retry-After": "1"}),
                                  search_answer(provider_entry(11)))
        with mock.patch.object(sx, "urlopen", transport), \
                mock.patch.object(sx.time, "sleep") as sleep:
            found = self.client().search_by_movie_hash("8e245d9679d31e12")
        self.assertEqual(len(found), 1)
        self.assertEqual(len(transport.requests), 2, "the same request was retried once")
        # The provider's own Retry-After is what the retry waited on. (With a
        # mocked clock the 0.25 s floor adds a second, tiny sleep, because no
        # real time passed; in a live run the backoff already covers it.)
        self.assertIn(mock.call(1.0), sleep.call_args_list)

    def test_a_spent_allowance_is_recognized_by_its_status_and_words(self) -> None:
        self.assertTrue(sx._looks_like_quota_error(
            "OpenSubtitles answered HTTP 406: You have downloaded your allowed 5 subtitles for 24h"))
        self.assertTrue(sx._looks_like_quota_error("HTTP 403: download limit reached"))
        self.assertFalse(sx._looks_like_quota_error("OpenSubtitles answered HTTP 500"))

    def test_a_rate_limit_that_never_lets_up_is_reported_once(self) -> None:
        """Three refusals end the request; the message says what happened."""
        transport = FakeTransport(*[http_error(429, b'{"message": "rate limited"}',
                                              {"Retry-After": "1"})] * 3)
        with mock.patch.object(sx, "urlopen", transport), \
                mock.patch.object(sx.time, "sleep"), \
                self.assertRaises(sx.OpenSubtitlesError) as caught:
            self.client().search_by_movie_hash("8e245d9679d31e12")
        self.assertIn("kept answering HTTP 429", str(caught.exception))
        self.assertEqual(len(transport.requests), 3, "the retry count is bounded")

    def test_a_rejected_request_still_counts_for_the_throttle(self) -> None:
        """A refused request spends the provider's rate budget like any other."""
        transport = FakeTransport(http_error(500, b'{"message": "boom"}'))
        client = self.client()
        with mock.patch.object(sx, "urlopen", transport), \
                self.assertRaises(sx.OpenSubtitlesError):
            client.search_by_movie_hash("8e245d9679d31e12")
        self.assertGreater(client._last_request_at, 0.0,
                           "the next request must be spaced from this one too")

    def test_a_bad_hash_is_refused_before_any_request(self) -> None:
        transport = FakeTransport()
        with mock.patch.object(sx, "urlopen", transport), \
                self.assertRaises(sx.OpenSubtitlesError):
            self.client().search_by_movie_hash("not-a-hash")
        self.assertEqual(transport.requests, [])


class DownloadTierTests(unittest.TestCase):
    """The image-only tier end to end: hash, search, download, gate, ledger."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="download_tier_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._saved_ledger = os.environ.get(sx.EXTRACTED_LEDGER_ENV)
        os.environ[sx.EXTRACTED_LEDGER_ENV] = str(self.tmp / "extracted.json")
        self.addCleanup(self._restore_ledger_env)
        movie_dir = self.tmp / "library" / "Fake (2021)"
        movie_dir.mkdir(parents=True)
        self.movie = movie_dir / "Fake (2021).mkv"
        # At least the provider's 128 KiB floor, so it can be hashed at all.
        self.movie.write_bytes(b"\0" * (2 * 65536))
        self.dest = self.movie.with_name("Fake (2021).eng.srt")

    def _restore_ledger_env(self) -> None:
        if self._saved_ledger is None:
            os.environ.pop(sx.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sx.EXTRACTED_LEDGER_ENV] = self._saved_ledger

    def options(self, **overrides: object) -> sx.DownloadOptions:
        base: dict[str, object] = {"api_key": "test-key", "min_cues": 2}
        base.update(overrides)
        return sx.DownloadOptions(**base)  # type: ignore[arg-type]

    def test_an_exact_hash_match_becomes_the_sidecar_beside_the_movie(self) -> None:
        transport = FakeTransport(search_answer(provider_entry(11)), download_answer(), SRT_PAYLOAD)
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertTrue(self.dest.is_file())
        self.assertEqual(self.dest.parent, self.movie.parent)
        self.assertEqual(self.dest.name, "Fake (2021).eng.srt")
        written = self.dest.read_text(encoding="utf-8")
        self.assertIn("A line of English dialogue", written)
        self.assertEqual(outcome.cue_count, 2)
        self.assertEqual(outcome.movie_hash, sx.moviehash_of_file(self.movie)[0])
        self.assertEqual(transport.urls()[0].split("/api/v1/")[1].split("?")[0], "subtitles")

    def test_every_accepted_payload_becomes_a_sidecar_the_toolkit_accepts(self) -> None:
        """The bytes this tier installs must pass the toolkit's own validator.

        A sidecar that the next run reads as invalid would be re-reported (or
        worse, replaced by a hand edit), so the encoding and newline handling
        here is not cosmetic: whatever the provider serves, the file that lands
        beside the movie has to look like every other sidecar.
        """
        payloads = {
            "utf-8, LF": SRT_PAYLOAD,
            "utf-8 with a BOM": b"\xef\xbb\xbf" + SRT_PAYLOAD,
            "cp1252 with an accented name": SRT_PAYLOAD.replace(
                b"A line of English dialogue", b"Rene\xe9 and Zoe\xe9 speak"),
            "CRLF line endings": SRT_PAYLOAD.replace(b"\n", b"\r\n"),
            "surrounded by whitespace": b"\n\n  " + SRT_PAYLOAD + b"\n\n",
        }
        for label, payload in payloads.items():
            with self.subTest(payload=label):
                transport = FakeTransport(search_answer(provider_entry(11)),
                                          download_answer(), payload)
                with mock.patch.object(sx, "urlopen", transport):
                    outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
                self.assertTrue(outcome.ok, outcome.detail)
                ok, reason = sx.validate_srt_sidecar(self.dest)
                self.assertTrue(ok, f"{label}: the written sidecar is invalid ({reason})")
                raw = self.dest.read_bytes()
                self.assertNotIn(b"\r", raw, f"{label}: newlines are canonical")
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{label}: no BOM is written")
                self.dest.unlink()

    def test_the_download_is_recorded_in_the_provenance_ledger(self) -> None:
        transport = FakeTransport(search_answer(provider_entry(11)), download_answer(), SRT_PAYLOAD)
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
        assert outcome.dest is not None
        record = sx.find_extracted_record(outcome.dest, sx.sha256_text(
            self.dest.read_text(encoding="utf-8")))
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["method"], "download")
        self.assertEqual(record["provider"], "opensubtitles")
        self.assertEqual(record["moviehash"], outcome.movie_hash)
        self.assertEqual(record["file_id"], 11)
        self.assertNotIn("ocr_backend", record)

    def test_no_api_key_means_no_request_at_all(self) -> None:
        transport = FakeTransport()
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(
                self.movie, self.dest, self.options(api_key=""))
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.available)
        self.assertIn("API key", outcome.unavailable_reason)
        self.assertEqual(transport.requests, [])
        self.assertFalse(self.dest.exists())

    def test_an_existing_sidecar_is_never_replaced(self) -> None:
        self.dest.write_text("hand-made", encoding="utf-8")
        transport = FakeTransport()
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.covered_by_other)
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "hand-made")
        self.assertEqual(transport.requests, [], "nothing was asked of the provider")

    def test_a_download_that_fails_the_gate_is_not_installed(self) -> None:
        junk = b"1\n00:00:01,000 --> 00:00:02,000\njust one cue\n"
        transport = FakeTransport(search_answer(provider_entry(11)), download_answer(), junk)
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, sx.REASON_DOWNLOAD_FAILED)
        self.assertIn("signs/songs-only", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_a_search_with_no_hash_match_reports_that_specific_fix(self) -> None:
        transport = FakeTransport(search_answer(provider_entry(11, hash_match=False)))
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, sx.REASON_NO_HASH_MATCH)
        self.assertIn("hash", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_a_spent_allowance_is_its_own_reason(self) -> None:
        transport = FakeTransport(
            search_answer(provider_entry(11)),
            http_error(406, b'{"message": "You have downloaded your allowed 5 subtitles for 24h"}'),
        )
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, sx.REASON_QUOTA_SPENT)
        self.assertFalse(self.dest.exists())

    def test_a_quota_refusal_on_the_search_is_still_a_quota_refusal(self) -> None:
        """The API can refuse the search itself; the bucket must be the same."""
        transport = FakeTransport(http_error(
            406, b'{"message": "You have downloaded your allowed 5 subtitles for 24h"}'))
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, sx.REASON_QUOTA_SPENT)
        self.assertFalse(self.dest.exists())

    def test_an_unwritable_movie_folder_is_refused_before_any_spend(self) -> None:
        """A folder that cannot take the sidecar must not cost a download."""
        os.chmod(self.movie.parent, 0o500)
        self.addCleanup(os.chmod, self.movie.parent, 0o700)
        if os.access(self.movie.parent, os.W_OK):  # e.g. running as root
            self.skipTest("this account can write to a read-only folder")
        transport = FakeTransport()  # any request fails the test
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.available)
        self.assertEqual(outcome.reason, sx.REASON_DOWNLOAD_FAILED)
        self.assertIn("not writable", outcome.unavailable_reason)
        self.assertEqual(transport.requests, [], "the provider was never asked")
        self.assertEqual(list(self.movie.parent.glob(".organize-write-probe*")), [],
                         "the probe cleans up after itself")

    def test_a_dry_run_names_the_match_and_spends_nothing(self) -> None:
        transport = FakeTransport(search_answer(provider_entry(11)))
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(
                self.movie, self.dest, self.options(dry_run=True))
        self.assertTrue(outcome.ok)
        self.assertIn("would download", outcome.detail)
        self.assertEqual(len(transport.requests), 1, "a preview searches but never downloads")
        self.assertFalse(self.dest.exists())

    def test_a_movie_too_small_to_hash_is_reported_before_any_request(self) -> None:
        small = self.movie.with_name("Small.mkv")
        small.write_bytes(b"tiny")
        transport = FakeTransport()
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(small, self.dest, self.options())
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.available)
        self.assertIn("could not hash", outcome.unavailable_reason)
        self.assertEqual(transport.requests, [])


class ExtractionRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="extract_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._saved_ledger = os.environ.get(sx.EXTRACTED_LEDGER_ENV)
        os.environ[sx.EXTRACTED_LEDGER_ENV] = str(self.tmp / "extracted.json")
        self.addCleanup(self._restore_ledger_env)
        library = self.tmp / "library"
        movie_dir = library / "Fake (2021)"
        movie_dir.mkdir(parents=True)
        self.movie = movie_dir / "Fake (2021).mkv"
        self.movie.write_bytes(b"mkv-bytes")
        self.dest = self.movie.with_name("Fake (2021).eng.srt")

    def _restore_ledger_env(self) -> None:
        if self._saved_ledger is None:
            os.environ.pop(sx.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sx.EXTRACTED_LEDGER_ENV] = self._saved_ledger

    def _run(self, tracks: dict, **options: object) -> sx.ExtractionOutcome:
        runner = FakeRunner(tracks)
        opts = sx.ExtractOptions(min_cues=2, **options)  # type: ignore[arg-type]
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            return sx.extract_embedded_english_srt(self.movie, self.dest, opts)

    def test_text_track_is_extracted_and_recorded(self) -> None:
        outcome = self._run(TEXT_TRACKS)
        self.assertTrue(outcome.ok, outcome.detail or outcome.unavailable_reason)
        self.assertEqual(outcome.method, "text")
        self.assertEqual(outcome.cue_count, 2)
        self.assertTrue(self.dest.is_file())
        self.assertEqual(self.dest.read_text(encoding="utf-8"), sx.ass_to_srt(ASS_TRACK))
        record = sx.find_extracted_record(self.dest, sx.sha256_text(sx.ass_to_srt(ASS_TRACK)))
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["track_id"], 2)
        self.assertEqual(record["method"], "text")

    def test_dry_run_writes_nothing(self) -> None:
        outcome = self._run(TEXT_TRACKS, dry_run=True)
        self.assertTrue(outcome.ok)
        self.assertFalse(self.dest.exists(), "a preview must not create a sidecar")

    def test_an_image_only_movie_reports_its_image_tracks(self) -> None:
        """It is not extracted, but it is exactly what earns a hash lookup."""
        runner = FakeRunner(PGS_TRACKS)
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            outcome = sx.extract_embedded_english_srt(
                self.movie, self.dest, sx.ExtractOptions(min_cues=2))
        self.assertFalse(outcome.ok)
        self.assertFalse(self.dest.exists())
        self.assertEqual(outcome.text_tracks, ())
        self.assertEqual([track.codec_id for track in outcome.image_tracks],
                         ["S_HDMV/PGS"])
        self.assertIn("image-based", outcome.detail)
        self.assertFalse(any("tracks" in call for call in runner.calls[1:]),
                         "an image track is never extracted")

    def test_movie_without_an_english_track_falls_through(self) -> None:
        outcome = self._run({"tracks": [
            {"id": 1, "type": "audio", "properties": {"codec_id": "A_AC3", "language": "eng"}}]})
        self.assertFalse(outcome.ok)
        self.assertIn("no English subtitle track", outcome.unavailable_reason)

    def test_missing_mkvtoolnix_names_the_install(self) -> None:
        with mock.patch.object(sx, "find_mkvtoolnix_binary", lambda *_args, **_kwargs: None):
            outcome = sx.extract_embedded_english_srt(
                self.movie, self.dest, sx.ExtractOptions(min_cues=2))
        self.assertIn("MKVToolNix", outcome.unavailable_reason)

    def test_an_existing_sidecar_is_never_overwritten(self) -> None:
        self.dest.write_text("untouched", encoding="utf-8")
        outcome = self._run(TEXT_TRACKS)
        self.assertFalse(outcome.ok)
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "untouched")

    def test_a_track_that_fails_the_quality_gate_is_rejected(self) -> None:
        # One cue only: gate refuses it as signs/songs-only and nothing is written.
        runner = FakeRunner(TEXT_TRACKS, payload=(
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Only one line\n"))
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            outcome = sx.extract_embedded_english_srt(
                self.movie, self.dest, sx.ExtractOptions(min_cues=10))
        self.assertFalse(outcome.ok)
        self.assertIn("signs/songs-only", outcome.detail)
        self.assertFalse(self.dest.exists())


class OneTrackAtATimeTests(unittest.TestCase):
    """What happens between mkvextract and a sidecar, for one track.

    Text extraction is local and free, and for most movies it is the whole
    story - but only if what comes out of the container is really the movie's
    English dialogue. Everything below is a way for that to not be true, and
    each one has to end with a reason a human can read and no file on disk.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="one_track_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._saved_ledger = os.environ.get(sx.EXTRACTED_LEDGER_ENV)
        os.environ[sx.EXTRACTED_LEDGER_ENV] = str(self.tmp / "extracted.json")
        self.addCleanup(self._restore_ledger_env)
        movie_dir = self.tmp / "library" / "Fake (2021)"
        movie_dir.mkdir(parents=True)
        self.movie = movie_dir / "Fake (2021).mkv"
        self.movie.write_bytes(b"mkv-bytes")
        self.dest = self.movie.with_name("Fake (2021).eng.srt")

    def _restore_ledger_env(self) -> None:
        if self._saved_ledger is None:
            os.environ.pop(sx.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sx.EXTRACTED_LEDGER_ENV] = self._saved_ledger

    @staticmethod
    def tracks_of(codec: str) -> dict:
        return {"tracks": [
            {"id": 3, "type": "subtitles",
             "properties": {"codec_id": codec, "language": "eng", "track_name": "English"}},
        ]}

    def run_with(self, runner: object, **options: object) -> sx.ExtractionOutcome:
        opts = sx.ExtractOptions(min_cues=2, **options)  # type: ignore[arg-type]
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            return sx.extract_embedded_english_srt(self.movie, self.dest, opts)

    # -- the formats a container can hold ----------------------------------

    def test_a_webvtt_track_becomes_a_sidecar(self) -> None:
        vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nA line of dialogue here\n\n"
               "00:00:04.000 --> 00:00:06.000\nAnd another one after it\n")
        outcome = self.run_with(FakeRunner(self.tracks_of("S_TEXT/WEBVTT"), payload=vtt))
        self.assertTrue(outcome.ok, outcome.detail or outcome.unavailable_reason)
        self.assertIn("A line of dialogue here", self.dest.read_text(encoding="utf-8"))

    def test_a_usf_track_becomes_a_sidecar(self) -> None:
        outcome = self.run_with(FakeRunner(self.tracks_of("S_TEXT/USF"), payload=USF_TRACK))
        self.assertTrue(outcome.ok, outcome.detail or outcome.unavailable_reason)
        self.assertIn("Hello USF & friends", self.dest.read_text(encoding="utf-8"))

    def test_an_srt_track_is_renumbered_rather_than_converted(self) -> None:
        messy = ("7\r\n00:00:01,000 --> 00:00:02,000\r\nfirst line of speech\r\n\r\n"
                 "9\r\n00:00:03,000 --> 00:00:04,000\r\nsecond line of speech\r\n")
        outcome = self.run_with(FakeRunner(self.tracks_of("S_TEXT/UTF8"), payload=messy))
        self.assertTrue(outcome.ok, outcome.detail or outcome.unavailable_reason)
        written = self.dest.read_text(encoding="utf-8")
        self.assertTrue(written.startswith("1\n"))
        self.assertNotIn("\r", written)

    def test_a_byte_order_mark_does_not_become_part_of_the_first_cue(self) -> None:
        payload = ("\ufeff1\n00:00:01,000 --> 00:00:02,000\nfirst line of speech\n\n"
                   "2\n00:00:03,000 --> 00:00:04,000\nsecond line of speech\n")
        outcome = self.run_with(FakeRunner(self.tracks_of("S_TEXT/UTF8"), payload=payload))
        self.assertTrue(outcome.ok, outcome.detail or outcome.unavailable_reason)
        self.assertTrue(self.dest.read_text(encoding="utf-8").startswith("1\n"))

    # -- the ways the container step fails ---------------------------------

    def test_mkvextract_failing_is_reported_with_its_own_words(self) -> None:
        class Failing(FakeRunner):
            def __call__(self, command, **kwargs):
                argv = [str(part) for part in command]
                if len(argv) > 1 and argv[1] == "tracks":
                    self.calls.append(argv)
                    return subprocess.CompletedProcess(argv, 2, b"", b"error: no such track")
                return super().__call__(command, **kwargs)

        outcome = self.run_with(Failing(self.tracks_of("S_TEXT/ASS")))
        self.assertFalse(outcome.ok)
        self.assertIn("mkvextract failed (exit 2)", outcome.detail)
        self.assertIn("no such track", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_a_track_that_was_never_written_is_not_guessed_at(self) -> None:
        """mkvextract can exit 0 and produce nothing on a damaged file."""
        class Silent(FakeRunner):
            def __call__(self, command, **kwargs):
                argv = [str(part) for part in command]
                if len(argv) > 1 and argv[1] == "tracks":
                    self.calls.append(argv)
                    return subprocess.CompletedProcess(argv, 0, b"", b"")
                return super().__call__(command, **kwargs)

        outcome = self.run_with(Silent(self.tracks_of("S_TEXT/ASS")))
        self.assertFalse(outcome.ok)
        self.assertIn("could not read the extracted track", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_a_track_that_converts_to_nothing_is_not_a_subtitle(self) -> None:
        outcome = self.run_with(
            FakeRunner(self.tracks_of("S_TEXT/ASS"), payload="[Script Info]\nTitle: empty\n"))
        self.assertFalse(outcome.ok)
        self.assertIn("no subtitle cues", outcome.detail)
        self.assertFalse(self.dest.exists())

    # -- the ways publishing fails -----------------------------------------

    def test_a_sidecar_that_appears_during_extraction_is_kept(self) -> None:
        """Create-only, exactly like a download: the other file wins.

        The check at the start of extraction is not enough — a download, a
        second run or a human can put the file there while mkvextract is
        working — so the publish itself has to be create-only.
        """
        placed = "1\n00:00:01,000 --> 00:00:02,000\nPlaced by somebody else.\n"

        class Interfering(FakeRunner):
            def __call__(inner, command, **kwargs):  # noqa: N805 - fake, not a method
                result = super().__call__(command, **kwargs)
                argv = [str(part) for part in command]
                if len(argv) > 1 and argv[1] == "tracks":
                    self.dest.write_text(placed, encoding="utf-8")
                return result

        outcome = self.run_with(Interfering(self.tracks_of("S_TEXT/ASS")))
        self.assertTrue(outcome.ok, "the movie is covered, just not by us")
        self.assertIn("appeared during extraction", outcome.detail)
        self.assertEqual(self.dest.read_text(encoding="utf-8"), placed,
                         "the file that got there first is never overwritten")

    def test_a_sidecar_that_cannot_be_written_is_reported(self) -> None:
        with mock.patch.object(sx, "atomic_write_text",
                               side_effect=OSError("read-only file system")):
            outcome = self.run_with(FakeRunner(self.tracks_of("S_TEXT/ASS")))
        self.assertFalse(outcome.ok)
        self.assertIn("could not write the extracted sidecar", outcome.detail)
        self.assertIn("read-only file system", outcome.detail)

class RunIntegrationTests(unittest.TestCase):
    """The whole run, tier by tier: text first, exact-hash lookup last."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="queue_extract_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._saved_ledger = os.environ.get(sx.EXTRACTED_LEDGER_ENV)
        os.environ[sx.EXTRACTED_LEDGER_ENV] = str(self.tmp / "extracted.json")
        self.addCleanup(self._restore_env)
        self.library = self.tmp / "library"
        movie_dir = self.library / "Fake (2021)"
        movie_dir.mkdir(parents=True)
        self.movie = movie_dir / "Fake (2021).mkv"
        self.movie.write_bytes(b"mkv-bytes")

    def _restore_env(self) -> None:
        if self._saved_ledger is None:
            os.environ.pop(sx.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sx.EXTRACTED_LEDGER_ENV] = self._saved_ledger

    def _config(self, **overrides: object) -> sx.ExtractorConfig:
        base: dict[str, object] = {
            "library": self.library,
            "log_file": self.tmp / "extractor.log",
            "report_file": self.tmp / "extractor_report.txt",
            "extract_min_cues": 2,
            "min_movie_size_mb": 0,  # the fixtures are a few bytes, not 300 MB
        }
        base.update(overrides)
        return sx.ExtractorConfig(**base)  # type: ignore[arg-type]

    def test_an_extracted_movie_is_covered_by_its_own_track(self) -> None:
        runner = FakeRunner(TEXT_TRACKS)
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            results, summary = sx.extraction_run(self._config())
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.reason, sx.REASON_EXTRACTED)
        self.assertEqual(result.status, "extracted")
        self.assertTrue(result.dest is not None and result.dest.is_file())
        self.assertEqual(int(summary["extracted_from_embedded"]), 1)
        self.assertEqual(int(summary["coverage_covered"]), 1, "extraction counts as coverage")
        self.assertEqual(int(summary["coverage_total"]), 1)

    def test_an_image_only_movie_needs_attention_without_a_key(self) -> None:
        runner = FakeRunner(PGS_TRACKS)
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            results, summary = sx.extraction_run(self._config())
        self.assertEqual(len(results), 1)
        # Image-only, and no API key: the lookup cannot run, so the movie is
        # reported in the bucket that names the fix (configure the key) rather
        # than in "there is no usable track" - the tracks are usable, just not
        # as text. It never counts as covered.
        self.assertEqual(results[0].reason, sx.REASON_IMAGE_ONLY)
        self.assertEqual(results[0].status, "skip")
        self.assertIn("API key", results[0].detail)
        self.assertEqual(int(summary["coverage_covered"]), 0)
        self.assertEqual(int(summary["image_only_movies"]), 1)
        self.assertEqual(list(self.movie.parent.glob("*.srt")), [],
                         "nothing is written when the lookup cannot run")

    def test_a_dry_run_names_the_track_it_would_use_and_writes_nothing(self) -> None:
        """The whole run, in dry-run: nothing is written, the movie is named."""
        runner = FakeRunner(TEXT_TRACKS)
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            results, _summary = sx.extraction_run(self._config(dry_run=True))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "dry-run")
        self.assertIn("embedded", results[0].detail)
        self.assertEqual(list(self.movie.parent.glob("*.srt")), [],
                         "a dry run writes no sidecar")

    def test_no_download_says_so_even_with_a_key_configured(self) -> None:
        """--no-download is a decision, not a missing key: say which it was."""
        transport = FakeTransport()  # any request fails the test
        with mock.patch.object(subprocess, "run", FakeRunner(PGS_TRACKS)), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sx, "urlopen", transport):
            results, _summary = sx.extraction_run(self._config(
                opensubtitles_api_key="test-key", download_enabled=False))
        self.assertEqual(results[0].reason, sx.REASON_IMAGE_ONLY)
        self.assertIn("--no-download", results[0].detail or "")
        self.assertEqual(transport.requests, [], "a disabled tier makes no requests")

    def test_a_text_track_beats_the_provider_tier(self) -> None:
        """Text extraction is local: with a text track present, no lookup runs."""
        combined = {"tracks": TEXT_TRACKS["tracks"] + PGS_TRACKS["tracks"]}
        transport = FakeTransport()  # any request raises
        with mock.patch.object(subprocess, "run", FakeRunner(combined)), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sx, "urlopen", transport):
            results, _summary = sx.extraction_run(self._config(opensubtitles_api_key="test-key", download_min_cues=2))
        self.assertEqual(results[0].reason, sx.REASON_EXTRACTED)
        self.assertEqual(transport.requests, [],
                         "the provider must not be asked when a text track exists")

    def test_an_image_only_movie_is_covered_by_an_exact_hash_download(self) -> None:
        """The whole run, both tiers: bitmaps go to the provider by file hash."""
        # Big enough for the provider's 128 KiB hashing floor.
        self.movie.write_bytes(b"\0" * (2 * 65536))
        transport = FakeTransport(search_answer(provider_entry(11)), download_answer(), SRT_PAYLOAD)
        with mock.patch.object(subprocess, "run", FakeRunner(PGS_TRACKS)), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sx, "urlopen", transport):
            results, summary = sx.extraction_run(self._config(opensubtitles_api_key="test-key", download_min_cues=2))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].reason, sx.REASON_DOWNLOADED)
        self.assertEqual(results[0].status, "downloaded")
        self.assertTrue(results[0].dest is not None and results[0].dest.is_file())
        self.assertEqual(int(summary["downloaded_from_opensubtitles"]), 1)
        self.assertEqual(int(summary["coverage_covered"]), 1, "a download counts as coverage")
        search_url = transport.urls()[0]
        self.assertIn("moviehash=0000000000020000", search_url)
        text = sx.build_report(results, self._config(), summary)
        self.assertIn("DOWNLOADED FROM OPENSUBTITLES BY EXACT MOVIEHASH", text)

    def test_a_quota_refusal_on_the_first_search_stops_the_run_asking(self) -> None:
        """One refusal, however it arrives, ends the tier for the whole run."""
        self.movie.write_bytes(b"\0" * (2 * 65536))
        second_dir = self.library / "Other (2019)"
        second_dir.mkdir()
        (second_dir / "Other (2019).mkv").write_bytes(b"\0" * (2 * 65536))
        transport = FakeTransport(http_error(
            406, b'{"message": "You have downloaded your allowed 5 subtitles for 24h"}'))
        with mock.patch.object(subprocess, "run", FakeRunner(PGS_TRACKS)), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sx, "urlopen", transport):
            results, _summary = sx.extraction_run(self._config(
                opensubtitles_api_key="test-key", download_min_cues=2))
        self.assertEqual({result.reason for result in results}, {sx.REASON_QUOTA_SPENT})
        self.assertEqual(len(transport.requests), 1,
                         "the second movie was never asked about")

    def test_a_spent_allowance_stops_the_run_from_asking_again(self) -> None:
        """One refusal ends the tier for the run; later movies are reported."""
        self.movie.write_bytes(b"\0" * (2 * 65536))
        second_dir = self.library / "Other (2019)"
        second_dir.mkdir()
        (second_dir / "Other (2019).mkv").write_bytes(b"\0" * (2 * 65536))
        transport = FakeTransport(
            search_answer(provider_entry(11)), download_answer(remaining=0), SRT_PAYLOAD,
        )
        with mock.patch.object(subprocess, "run", FakeRunner(PGS_TRACKS)), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sx, "urlopen", transport):
            results, _summary = sx.extraction_run(self._config(opensubtitles_api_key="test-key", download_min_cues=2))
        by_name = {result.video.parent.name: result.reason for result in results}
        self.assertEqual(by_name["Fake (2021)"], sx.REASON_DOWNLOADED)
        self.assertEqual(by_name["Other (2019)"], sx.REASON_QUOTA_SPENT)
        self.assertEqual(len(transport.requests), 3,
                         "one search, one download, and nothing for the second movie")

    def test_a_run_level_download_limit_gets_its_own_explanation(self) -> None:
        """The cap is this run's, not the provider's - so the fix is a re-run."""
        self.movie.write_bytes(b"\0" * (2 * 65536))
        second_dir = self.library / "Other (2019)"
        second_dir.mkdir()
        (second_dir / "Other (2019).mkv").write_bytes(b"\0" * (2 * 65536))
        transport = FakeTransport(
            search_answer(provider_entry(11)), download_answer(), SRT_PAYLOAD,
        )
        with mock.patch.object(subprocess, "run", FakeRunner(PGS_TRACKS)), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sx, "urlopen", transport):
            results, _summary = sx.extraction_run(self._config(
                opensubtitles_api_key="test-key", download_min_cues=2, download_limit=1))
        by_name = {result.video.parent.name: result for result in results}
        self.assertEqual(by_name["Fake (2021)"].reason, sx.REASON_DOWNLOADED)
        limited = by_name["Other (2019)"]
        self.assertEqual(limited.reason, sx.REASON_IMAGE_ONLY)
        self.assertIn("--download-limit", limited.detail or "")
        self.assertNotIn("--no-download", limited.detail or "")
        self.assertEqual(len(transport.requests), 3,
                         "the second movie must not reach the provider at all")

    def test_a_dry_run_previews_exactly_what_the_live_run_would_do(self) -> None:
        """The report calls the preview a forecast, cap included: two image-only
        movies and room for one download means one preview, one capped movie."""
        self.movie.write_bytes(b"\0" * (2 * 65536))
        second_dir = self.library / "Other (2019)"
        second_dir.mkdir()
        (second_dir / "Other (2019).mkv").write_bytes(b"\0" * (2 * 65536))
        transport = FakeTransport(search_answer(provider_entry(11)))
        with mock.patch.object(subprocess, "run", FakeRunner(PGS_TRACKS)), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sx, "urlopen", transport):
            results, summary = sx.extraction_run(self._config(
                opensubtitles_api_key="test-key", download_min_cues=2,
                download_limit=1, dry_run=True))
        by_name = {result.video.parent.name: result for result in results}
        self.assertEqual(by_name["Fake (2021)"].reason, sx.REASON_DRY_RUN)
        self.assertIn("would download", by_name["Fake (2021)"].detail or "")
        self.assertIn("OpenSubtitles", by_name["Fake (2021)"].detail or "")
        self.assertEqual(by_name["Other (2019)"].reason, sx.REASON_IMAGE_ONLY)
        self.assertIn("--download-limit", by_name["Other (2019)"].detail or "")
        self.assertEqual(len(transport.requests), 1,
                         "the preview searches once and downloads nothing")
        self.assertEqual(int(summary["image_only_movies"]), 2,
                         "both movies are image-only, whatever became of them")
        self.assertEqual(list(self.library.rglob("*.srt")), [],
                         "a preview never writes a sidecar")

    def test_the_image_only_tally_survives_a_failed_download(self) -> None:
        """The tally is about the library, not about how the tier behaved."""
        self.movie.write_bytes(b"\0" * (2 * 65536))
        transport = FakeTransport(search_answer(provider_entry(11)),
                                  http_error(406, b'{"message": "quota exhausted"}'))
        with mock.patch.object(subprocess, "run", FakeRunner(PGS_TRACKS)), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sx, "urlopen", transport):
            results, summary = sx.extraction_run(self._config(
                opensubtitles_api_key="test-key", download_min_cues=2))
        self.assertEqual(results[0].reason, sx.REASON_QUOTA_SPENT)
        self.assertEqual(int(summary["image_only_movies"]), 1)

    def test_report_names_what_was_extracted(self) -> None:
        runner = FakeRunner(TEXT_TRACKS)
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            results, summary = sx.extraction_run(self._config())
        text = sx.build_report(results, self._config(), summary)
        self.assertIn("EXTRACTED FROM THE MOVIE'S OWN EMBEDDED TRACK", text)
        self.assertIn("Extracted this run", text)


class NothingInspectedTests(unittest.TestCase):
    """A library the filters emptied must not be reported as a green pass.

    `--min-size` (300 MB by default) and the sample-name rule are both silent
    filters: a walk that removes every file leaves the run with nothing to do,
    and "nothing to do" used to be rendered as the *success* sentence -
    "Nothing to do: every one of the 0 movie(s) in the library has a validated
    external English .eng.srt", next to "0/0 (100.0%) COVERAGE". The operator
    read that as "your library is fine" when the truth was "your library was
    never looked at".
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="nothing_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.library = self.tmp / "library"
        folder = self.library / "Tiny (2020)"
        folder.mkdir(parents=True)
        self.movie = folder / "Tiny (2020).mkv"
        self.movie.write_bytes(b"x" * 4096)
        self.min_size_mb = self.movie.stat().st_size  # a little over 4 kB

    def scan(self, min_size_mb: float) -> sx.LibraryScan:
        return sx.discover_videos(self.library, int(min_size_mb * 1024 * 1024))

    def test_a_filtered_scan_reports_what_it_removed(self) -> None:
        scan = self.scan(self.min_size_mb * 2 + 1)
        self.assertEqual(scan.videos, [])
        self.assertEqual(scan.below_min_size, 1)
        self.assertEqual(scan.sample_named, 0)

    def test_sample_named_files_are_counted_not_just_dropped(self) -> None:
        (self.movie.parent / "Tiny (2020).sample.mkv").write_bytes(b"y" * 4096)
        scan = self.scan(0)
        self.assertEqual([path.name for path in scan.videos], [self.movie.name])
        self.assertEqual(scan.sample_named, 1, "the sample is skipped, and reported as skipped")

    def test_an_unfiltered_scan_counts_nothing_removed(self) -> None:
        scan = self.scan(0)
        self.assertEqual(scan.videos, [self.movie])
        self.assertEqual((scan.below_min_size, scan.sample_named), (0, 0))

    def test_the_report_says_the_library_was_not_inspected(self) -> None:
        cfg = sx.ExtractorConfig(library=self.library, report_file=self.tmp / "r.txt",
                                 log_file=self.tmp / "r.log",
                                 min_movie_size_mb=self.min_size_mb * 2 + 1)
        summary = {"movies_discovered": 0, "files_below_min_size": 1, "files_sample_named": 0,
                   "coverage_covered": 0, "coverage_total": 0,
                   "extracted_from_embedded": 0, "downloaded_from_opensubtitles": 0,
                   "log_file": str(cfg.log_file)}
        text = sx.build_report([], cfg, summary)
        self.assertIn("Nothing was inspected", text)
        self.assertIn("--min-size", text)
        self.assertIn("Movies inspected", text)
        self.assertNotIn("Nothing to do", text, "the success sentence is not a filter report")
        self.assertNotIn("already has a validated external English", text)
        self.assertNotIn("Coverage this run: 0 of 0", text)

    def test_an_empty_library_is_named_as_empty(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        cfg = sx.ExtractorConfig(library=empty, report_file=self.tmp / "r.txt",
                                 log_file=self.tmp / "r.log", min_movie_size_mb=0)
        summary = {"movies_discovered": 0, "files_below_min_size": 0, "files_sample_named": 0,
                   "coverage_covered": 0, "coverage_total": 0,
                   "extracted_from_embedded": 0, "downloaded_from_opensubtitles": 0,
                   "log_file": str(cfg.log_file)}
        text = sx.build_report([], cfg, summary)
        self.assertIn("No movie files were found", text)
        self.assertNotIn("Nothing to do", text)
        self.assertNotIn("Nothing was inspected", text,
                         "an empty library is not a filtering result")

    def test_a_covered_library_still_gets_the_success_sentence(self) -> None:
        """The fix must not silence the true case it replaced."""
        video = self.movie
        results = [sx.JobResult(video, "have", "validated exact .eng.srt",
                                video.with_suffix(".eng.srt"), reason=sx.REASON_COVERED)]
        cfg = sx.ExtractorConfig(library=self.library, report_file=self.tmp / "r.txt",
                                 log_file=self.tmp / "r.log", min_movie_size_mb=0)
        summary = {"movies_discovered": 1, "files_below_min_size": 0, "files_sample_named": 0,
                   "coverage_covered": 1, "coverage_total": 1,
                   "extracted_from_embedded": 0, "downloaded_from_opensubtitles": 0,
                   "log_file": str(cfg.log_file)}
        text = sx.build_report(results, cfg, summary)
        self.assertIn("Nothing to do: every one of the 1 movie(s)", text)
        self.assertNotIn("Nothing was inspected", text)

    def test_the_run_logs_a_warning_when_the_filters_emptied_the_library(self) -> None:
        """The console line, not just the report - this is what a cron job sees."""
        cfg = sx.ExtractorConfig(library=self.library, report_file=self.tmp / "r.txt",
                                 log_file=self.tmp / "run.log", min_movie_size_mb=5000)
        with mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            results, summary = sx.extraction_run(cfg)
        self.assertEqual(results, [])
        self.assertEqual(int(summary["files_below_min_size"]), 1)
        logged = (self.tmp / "run.log").read_text(encoding="utf-8")
        self.assertIn("Nothing eligible", logged)
        self.assertIn("smaller than --min-size", logged)


class ProvenanceLedgerTests(unittest.TestCase):
    """What the extraction ledger says about a sidecar, and what it refuses to.

    The ledger is the durable answer to "did this tool write this sidecar?".
    A sidecar the extractor just wrote is recorded against the movie, the track
    and the exact bytes; every other sidecar - placed by hand, carried over
    from an earlier era, or edited afterwards - has no record, and the tool
    says so rather than guessing.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="ledger_extract_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._saved_ledger = os.environ.get(sx.EXTRACTED_LEDGER_ENV)
        os.environ[sx.EXTRACTED_LEDGER_ENV] = str(self.tmp / "extracted.json")
        self.addCleanup(self._restore_env)
        folder = self.tmp / "library" / "Fake (2021)"
        folder.mkdir(parents=True)
        self.video = folder / "Fake (2021).mkv"
        self.video.write_bytes(b"mkv-bytes")
        self.srt = folder / "Fake (2021).eng.srt"
        self.body = sx.render_srt_cues([
            (f"00:00:{index:02d},000", f"00:00:{index:02d},900", "A line of dialogue")
            for index in range(1, 12)
        ])
        # Bytes, and LF on purpose: the extractor writes sidecars with
        # newline="\n" and records the hash of those exact bytes, so a text-mode
        # write (CRLF on Windows) would break the match for the wrong reason.
        self.srt.write_bytes(self.body.encode("utf-8"))
        self.track = sx.EmbeddedSubtitleTrack(2, "S_TEXT/ASS", "eng", "English", "text", ".ass")
        self.sha = sx.sha256_text(self.body)

    def _restore_env(self) -> None:
        if self._saved_ledger is None:
            os.environ.pop(sx.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sx.EXTRACTED_LEDGER_ENV] = self._saved_ledger

    def _record_extraction(self) -> None:
        self.assertTrue(sx.record_extracted_sidecar(
            self.video, self.srt, track=self.track, method="text", cue_count=11,
            sha256=self.sha))

    def test_a_sidecar_with_no_extraction_record_has_no_provenance(self) -> None:
        self.assertIsNone(sx.find_extracted_record(self.srt, self.sha))
        self.assertEqual(self.srt.read_text(encoding="utf-8"), self.body, "file untouched")

    def test_a_fresh_extraction_is_recorded_against_its_movie_and_track(self) -> None:
        self._record_extraction()
        record = sx.find_extracted_record(self.srt, self.sha)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["movie"], str(self.video))
        self.assertEqual(record["track_id"], 2)
        self.assertEqual(record["codec_id"], "S_TEXT/ASS")
        self.assertEqual(record["method"], "text")
        self.assertEqual(record["cue_count"], 11)
        self.assertTrue(record["recorded_utc"], "the record is stamped")

    def test_a_replaced_sidecar_is_no_longer_the_extraction(self) -> None:
        self._record_extraction()
        # The sidecar was replaced by a hand edit: its bytes no longer match the
        # provenance record, so it is not the extracted copy any more.
        self.srt.write_bytes((self.body + "\n").encode("utf-8"))
        replaced_sha = sx.sha256_text(self.body + "\n")
        self.assertIsNone(sx.find_extracted_record(self.srt, replaced_sha),
                          "a replaced sidecar must not inherit the old record")
        # Without a sha to compare, the record is still there by path - which is
        # exactly why every caller that matters passes the sha.
        self.assertIsNotNone(sx.find_extracted_record(self.srt))

    def test_a_damaged_ledger_reads_as_an_empty_one(self) -> None:
        self._record_extraction()
        Path(os.environ[sx.EXTRACTED_LEDGER_ENV]).write_text("{not json", encoding="utf-8")
        self.assertEqual(sx.load_extracted_ledger(), {"version": sx.EXTRACTED_LEDGER_VERSION,
                                                      "sidecars": {}})
        self.assertIsNone(sx.find_extracted_record(self.srt, self.sha))


class WhatTheContainerClaimsTests(unittest.TestCase):
    """mkvmerge's JSON is a report about a file someone else made.

    Matroska flags come in a modern and a legacy spelling, and are sometimes
    strings rather than booleans; a track id is sometimes not a number at all.
    None of that may become an exception, and none of it may quietly turn a
    commentary or signs-only stream into this movie's English sidecar.
    """

    @staticmethod
    def track(**props: object) -> dict:
        base = {"codec_id": "S_TEXT/UTF8", "language": "eng"}
        base.update(props)
        return {"id": 2, "type": "subtitles", "properties": base}

    def test_a_flag_written_as_a_word_is_still_a_flag(self) -> None:
        for spelling in ("1", "true", "TRUE", " yes "):
            with self.subTest(value=spelling):
                self.assertTrue(sx.subtitle_track_is_forced(self.track(flag_forced=spelling)))

    def test_a_string_that_is_not_a_flag_is_not_true(self) -> None:
        self.assertFalse(sx.subtitle_track_is_forced(self.track(flag_forced="no")))

    def test_the_commentary_flag_is_read_as_well_as_the_name(self) -> None:
        self.assertTrue(sx.subtitle_track_is_commentary(self.track(flag_commentary=True)))
        self.assertTrue(sx.subtitle_track_is_commentary(
            self.track(track_name="Director's commentary")))
        self.assertFalse(sx.subtitle_track_is_commentary(self.track(track_name="English")))

    def test_the_hearing_impaired_flag_is_read_as_well_as_the_name(self) -> None:
        self.assertTrue(sx.subtitle_track_is_sdh(self.track(flag_hearing_impaired=True)))

    def test_a_track_with_an_unusable_id_is_not_a_candidate(self) -> None:
        """An id that is not a number cannot be handed to mkvextract."""
        tracks = [{"id": "two", "type": "subtitles",
                   "properties": {"codec_id": "S_TEXT/UTF8", "language": "eng"}}]
        self.assertEqual(sx.classify_embedded_subtitle_tracks(tracks), [])


class ProbingTheContainerTests(unittest.TestCase):
    """Three ways ``mkvmerge -J`` can answer that are not track information."""

    def probe(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> tuple:
        completed = subprocess.CompletedProcess(["mkvmerge"], returncode, stdout, stderr)
        with mock.patch.object(subprocess, "run", return_value=completed):
            return sx.probe_embedded_subtitle_tracks(Path("/library/Fake (2021).mkv"), "mkvmerge")

    def test_a_movie_mkvmerge_cannot_read(self) -> None:
        tracks, reason = self.probe(returncode=2, stderr=b"Error: no EBML head found")
        self.assertIsNone(tracks)
        self.assertIn("could not read the movie (exit 2)", reason)
        self.assertIn("no EBML head found", reason)

    def test_an_answer_that_is_not_json(self) -> None:
        tracks, reason = self.probe(stdout=b"mkvmerge v82 ('Ridin')")
        self.assertIsNone(tracks)
        self.assertIn("unreadable track information", reason)

    def test_an_answer_with_no_track_list(self) -> None:
        tracks, reason = self.probe(stdout=b'{"container": {"recognized": true}}')
        self.assertIsNone(tracks)
        self.assertIn("no tracks", reason)

    def test_entries_that_are_not_objects_are_dropped(self) -> None:
        tracks, reason = self.probe(stdout=b'{"tracks": ["surprise", {"id": 1}]}')
        self.assertEqual(tracks, [{"id": 1}])
        self.assertEqual(reason, "")


class ExtractionQualityGateTests(unittest.TestCase):
    """The two refusals that only an extracted track can trigger."""

    def test_a_track_bigger_than_the_safety_limit_is_refused(self) -> None:
        with mock.patch.object(sx, "MAX_SUBTITLE_BYTES", 64):
            ok, reason = sx.subtitle_quality(
                "1\n00:00:01,000 --> 00:00:02,000\n" + "x" * 200 + "\n", min_cues=1)
        self.assertFalse(ok)
        self.assertIn("safety limit", reason)

    def test_a_track_that_did_not_convert_is_refused(self) -> None:
        ok, reason = sx.subtitle_quality("Dialogue: 0,0:00:01.50,...", min_cues=1)
        self.assertFalse(ok)
        self.assertIn("did not convert to valid SRT cues", reason)

    def test_an_outcome_that_names_no_obstacle_was_possible(self) -> None:
        self.assertTrue(sx.ExtractionOutcome().available)
        self.assertFalse(sx.ExtractionOutcome(unavailable_reason="no MKVToolNix").available)


class WhenExtractionCannotBeAttemptedTests(unittest.TestCase):
    """Extraction that never starts still has to say why, in one sentence."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="no_extract_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        movie_dir = self.tmp / "library" / "Fake (2021)"
        movie_dir.mkdir(parents=True)
        self.movie = movie_dir / "Fake (2021).mkv"
        self.movie.write_bytes(b"mkv-bytes")
        self.dest = self.movie.with_name("Fake (2021).eng.srt")

    def test_extraction_switched_off_is_not_an_error(self) -> None:
        outcome = sx.extract_embedded_english_srt(
            self.movie, self.dest, sx.ExtractOptions(enabled=False))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.unavailable_reason, "embedded extraction is disabled")
        self.assertFalse(outcome.available)

    def test_a_container_that_cannot_be_probed_is_reported_not_guessed(self) -> None:
        with mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
             mock.patch.object(sx, "probe_embedded_subtitle_tracks",
                               return_value=(None, "mkvmerge reported no tracks")):
            outcome = sx.extract_embedded_english_srt(
                self.movie, self.dest, sx.ExtractOptions())
        self.assertFalse(outcome.ok)
        self.assertIn("could not read the movie's tracks", outcome.unavailable_reason)
        self.assertIn("mkvmerge reported no tracks", outcome.unavailable_reason)
        self.assertFalse(self.dest.exists())

    def test_an_image_only_movie_says_text_tracks_are_what_it_wants(self) -> None:
        """The movie is not "unsubtitleable": it is bitmap-subtitled, and that
        is what the provider tier exists for - so the detail says so."""
        with mock.patch.object(subprocess, "run", FakeRunner(PGS_TRACKS)), \
             mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            outcome = sx.extract_embedded_english_srt(
                self.movie, self.dest, sx.ExtractOptions(min_cues=2))
        self.assertFalse(outcome.ok)
        self.assertIn("image-based", outcome.detail)
        self.assertEqual(len(outcome.image_tracks), 1)
        self.assertFalse(self.dest.exists())


class OneTrackDirectlyTests(unittest.TestCase):
    """``_extract_one_track`` alone: the arms the caller normally prevents.

    The outer entry point checks for MKVToolNix and only ever hands over text
    tracks. These tests remove those guarantees, because a helper that trusts
    its caller is a helper that breaks the day the caller changes.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="one_track_direct_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.movie = self.tmp / "Fake (2021).mkv"
        self.movie.write_bytes(b"mkv-bytes")
        self.dest = self.tmp / "Fake (2021).eng.srt"

    def extract(self, track: sx.EmbeddedSubtitleTrack,
                binary: object = fake_binaries) -> sx.ExtractionOutcome:
        with mock.patch.object(sx, "find_mkvtoolnix_binary", binary):
            return sx._extract_one_track(
                self.movie, self.movie, self.dest, track, self.tmp,
                sx.ExtractOptions(min_cues=2))

    def test_without_mkvextract_nothing_is_attempted(self) -> None:
        track = sx.EmbeddedSubtitleTrack(2, "S_TEXT/UTF8", "eng", "English", "text", ".srt")
        outcome = self.extract(track, binary=lambda *_a, **_k: "")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.detail, "mkvextract is not installed")
        self.assertFalse(self.dest.exists())

    def test_an_image_track_is_never_extracted(self) -> None:
        """_extract_one_track refuses bitmaps outright, even if a caller asks."""
        track = sx.EmbeddedSubtitleTrack(4, "S_HDMV/PGS", "eng", "English", "image", ".sup")
        runner = FakeRunner(PGS_TRACKS)
        with mock.patch.object(subprocess, "run", runner):
            outcome = self.extract(track)
        self.assertFalse(outcome.ok)
        self.assertIn("only text-based subtitle tracks are extracted", outcome.detail)
        self.assertEqual(runner.calls, [], "nothing is run for an image track")
        self.assertFalse(self.dest.exists())


class TheBytesThatCameOutTests(unittest.TestCase):
    """mkvextract writes a file; what is in it is another question."""

    class BytesRunner:
        """Like FakeRunner, but the extracted track is raw bytes."""

        def __init__(self, tracks: dict, payload: bytes) -> None:
            self.tracks = tracks
            self.payload = payload

        def __call__(self, command, **_kwargs):
            argv = [str(part) for part in command]
            if "-J" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(self.tracks).encode("utf-8"), b"")
            if len(argv) > 1 and argv[1] == "tracks":
                Path(argv[-1].split(":", 1)[1]).write_bytes(self.payload)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="track_bytes_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._saved_ledger = os.environ.get(sx.EXTRACTED_LEDGER_ENV)
        os.environ[sx.EXTRACTED_LEDGER_ENV] = str(self.tmp / "extracted.json")
        self.addCleanup(self._restore_ledger_env)
        movie_dir = self.tmp / "library" / "Fake (2021)"
        movie_dir.mkdir(parents=True)
        self.movie = movie_dir / "Fake (2021).mkv"
        self.movie.write_bytes(b"mkv-bytes")
        self.dest = self.movie.with_name("Fake (2021).eng.srt")

    def _restore_ledger_env(self) -> None:
        if self._saved_ledger is None:
            os.environ.pop(sx.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sx.EXTRACTED_LEDGER_ENV] = self._saved_ledger

    def run_with(self, payload: bytes) -> sx.ExtractionOutcome:
        tracks = {"tracks": [{"id": 3, "type": "subtitles", "properties": {
            "codec_id": "S_TEXT/UTF8", "language": "eng", "track_name": "English"}}]}
        with mock.patch.object(subprocess, "run", self.BytesRunner(tracks, payload)), \
             mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            return sx.extract_embedded_english_srt(
                self.movie, self.dest, sx.ExtractOptions(min_cues=2))

    def test_a_track_that_is_not_readable_text_is_refused(self) -> None:
        """A gzip header on a Matroska track is nonsense, and unpacking it
        fails; that is a refusal with a reason, not a traceback."""
        outcome = self.run_with(b"\x1f\x8b" + b"\x00" * 64)
        self.assertFalse(outcome.ok)
        self.assertIn("not readable text", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_a_second_byte_order_mark_is_stripped_too(self) -> None:
        """utf-8-sig removes one BOM; a doubled one must not reach the file."""
        srt = ("1\n00:00:01,000 --> 00:00:02,000\nfirst line of speech\n\n"
               "2\n00:00:03,000 --> 00:00:04,000\nsecond line of speech\n")
        outcome = self.run_with("\ufeff\ufeff".encode("utf-8") + srt.encode("utf-8"))
        self.assertTrue(outcome.ok, outcome.detail or outcome.unavailable_reason)
        written = self.dest.read_text(encoding="utf-8")
        self.assertFalse(written.startswith("\ufeff"), repr(written[:10]))
        self.assertTrue(written.startswith("1\n"))


if __name__ == "__main__":
    unittest.main()
