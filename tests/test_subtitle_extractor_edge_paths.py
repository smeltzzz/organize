"""Degraded-path tests for ``subtitle_extractor.py``.

Two contracts carry this tool: an existing validated ``.eng.srt`` is never
overwritten, and provider quota is never spent on a movie that cannot receive
the result. The tests below pin the paths that only run when something is
missing, broken or hostile: a provider that answers HTML, a 429 with a bad
Retry-After header, a subtitle too large to decompress, a movie that changed
size mid-download, an unwritable library folder, an unreadable ledger. The
network is always a double; the file system is always real.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import subtitle_extractor as se  # noqa: E402

_VALID_SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello.\n\n2\n00:00:03,000 --> 00:00:04,000\nBye.\n"


def _http_error(code: int, body: bytes = b"", headers: dict | None = None,
                reason: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.example/x", code, "boom",
                                  headers if headers is not None else {}, io.BytesIO(body))


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="se_edge_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.library = self.root / "library"
        self.library.mkdir()

    def movie(self, name: str = "Movie (1999)", *, size: int = 200_000) -> Path:
        folder = self.library / name
        folder.mkdir(exist_ok=True)
        path = folder / f"{name}.mkv"
        path.write_bytes(b"\0" * size)
        return path

    def cfg(self, **kwargs: object) -> se.ExtractorConfig:
        base: dict = {
            "library": self.library,
            "log_file": None,
            "report_file": self.root / "report.txt",
            # The fixture movies are bytes, not gigabytes; eligibility is not
            # what these tests are about unless a test says so.
            "min_movie_size_mb": 0,
        }
        base.update(kwargs)
        return se.ExtractorConfig(**base)


class NumericCoercionTests(unittest.TestCase):
    def test_unusable_numbers_become_zero(self) -> None:
        self.assertEqual(0, se._nonnegative_int("not a number"))
        self.assertEqual(0, se._nonnegative_int(None))
        self.assertEqual(0.0, se._nonnegative_float("not a number"))
        self.assertEqual(0.0, se._nonnegative_float(None))
        self.assertEqual(3, se._nonnegative_int("3"))
        self.assertEqual(2.5, se._nonnegative_float("2.5"))

    def test_a_json_boolean_can_be_a_word(self) -> None:
        self.assertTrue(se._json_flag("yes"))
        self.assertFalse(se._json_flag("no"))
        self.assertTrue(se._json_flag(1))
        self.assertFalse(se._json_flag(0))


class DecodeTests(unittest.TestCase):
    def test_a_gzip_bomb_is_refused(self) -> None:
        blob = gzip.compress(b"A" * (se.MAX_SUBTITLE_BYTES + 1024))
        with self.assertRaises(ValueError) as caught:
            se.decode_subtitle_bytes(blob)
        self.assertIn("safety limit", str(caught.exception))

    def test_a_gzip_subtitle_is_decompressed(self) -> None:
        blob = gzip.compress(_VALID_SRT.encode("utf-8"))
        self.assertIn("Hello.", se.decode_subtitle_bytes(blob))

    def test_bytes_no_encoding_can_decode_are_replaced_not_raised(self) -> None:
        # 0x81 is undefined in cp1252 and not valid UTF-8: the caller wants a
        # string to explain a rejected download, never an exception.
        text = se.decode_subtitle_bytes(b"bad \x81 bytes")
        self.assertIn("bad", text)


class SidecarPresenceTests(_Case):
    def test_a_movie_with_no_sidecar_has_none(self) -> None:
        video = self.movie()
        self.assertIsNone(se.has_english_sidecar(video.parent, video.stem))

    def test_an_english_sidecar_is_found(self) -> None:
        video = self.movie()
        sidecar = video.parent / "Movie (1999).eng.srt"
        sidecar.write_text(_VALID_SRT, encoding="utf-8")
        self.assertEqual(sidecar, se.has_english_sidecar(video.parent, video.stem))

    def test_an_unlistable_folder_has_no_sidecar(self) -> None:
        with mock.patch.object(Path, "iterdir", side_effect=OSError("permission denied")):
            self.assertIsNone(se.has_english_sidecar(self.library, "Movie"))

    def test_a_foreign_language_name_is_not_an_english_sidecar(self) -> None:
        path = self.library / "Movie (1999).fra.srt"
        path.write_text(_VALID_SRT, encoding="utf-8")
        self.assertFalse(se.is_english_srt_sidecar(path, "Movie (1999)"))


class DiscoveryTests(_Case):
    def test_filters_are_counted_not_silent(self) -> None:
        (self.library / "Small (1999)").mkdir()
        (self.library / "Small (1999)" / "Small (1999).mkv").write_bytes(b"x")
        (self.library / "Sample (1999)").mkdir()
        (self.library / "Sample (1999)" / "sample.mkv").write_bytes(b"x" * 300_000)
        (self.library / ".hidden").mkdir()
        (self.library / ".hidden" / "Hidden (1999).mkv").write_bytes(b"x" * 300_000)
        (self.library / "Extras").mkdir()
        (self.library / "Extras" / "Extra (1999).mkv").write_bytes(b"x" * 300_000)
        (self.library / "Sci-Fi").mkdir()
        big = self.movie("Big (1999)")
        scan = se.discover_videos(self.library, 100_000)
        self.assertEqual([big], scan.videos)
        self.assertEqual(1, scan.below_min_size)
        self.assertEqual(1, scan.sample_named)

    def test_a_symlinked_movie_inside_the_library_is_skipped(self) -> None:
        outside = self.root / "outside.mkv"
        outside.write_bytes(b"x" * 300_000)
        (self.library / "Link (1999)").mkdir()
        (self.library / "Link (1999)" / "Link (1999).mkv").symlink_to(outside)
        self.assertEqual([], se.discover_videos(self.library, 100_000).videos)

    def test_a_file_that_vanishes_between_walk_and_stat_is_skipped(self) -> None:
        video = self.movie("Gone (1999)")
        real_stat = Path.stat

        def vanish(self: Path, **kwargs: object) -> os.stat_result:
            if self.name == video.name:
                raise OSError("vanished")
            return real_stat(self, **kwargs)

        with mock.patch.object(Path, "stat", vanish), \
                mock.patch.object(Path, "is_symlink", lambda self: False):
            self.assertEqual([], se.discover_videos(self.library, 100_000).videos)

    def test_a_non_video_file_is_ignored(self) -> None:
        (self.library / "Movie (1999)").mkdir(exist_ok=True)
        (self.library / "Movie (1999)" / "readme.txt").write_bytes(b"x")
        self.assertEqual([], se.discover_videos(self.library, 1).videos)


class LayoutTests(_Case):
    def test_a_file_directly_under_the_library_root_is_noncanonical(self) -> None:
        video = self.library / "Loose (1999).mkv"
        video.write_bytes(b"x")
        self.assertIn("directly under the library root",
                      se.canonical_movie_layout_issue(video, self.library) or "")

    def test_a_symlinked_movie_is_noncanonical(self) -> None:
        real = self.movie("Real (1999)")
        link = real.parent / "Link (1999).mkv"
        link.symlink_to(real)
        self.assertIn("not a regular non-symlink file",
                      se.canonical_movie_layout_issue(link, self.library) or "")

    def test_a_stem_that_does_not_match_the_folder_is_noncanonical(self) -> None:
        video = self.movie("Folder Name (1999)")
        wrong = video.parent / "Other Name (1999).mkv"
        wrong.write_bytes(b"x")
        self.assertIn("does not match its movie-folder name",
                      se.canonical_movie_layout_issue(wrong, self.library) or "")

    def test_two_movie_files_in_one_folder_are_noncanonical(self) -> None:
        video = self.movie("Double (1999)")
        (video.parent / "Double (1999).mp4").write_bytes(b"x")
        self.assertIn("expected one regular movie file",
                      se.canonical_movie_layout_issue(video, self.library) or "")

    def test_an_unreadable_movie_folder_is_noncanonical_not_fatal(self) -> None:
        video = self.movie("Locked (1999)")
        real_iterdir = Path.iterdir

        def deny(self: Path) -> object:
            if self == video.parent:
                raise OSError("permission denied")
            return real_iterdir(self)

        with mock.patch.object(Path, "iterdir", deny):
            issue = se.canonical_movie_layout_issue(video, self.library)
        self.assertIn("could not inspect movie folder", issue or "")

    def test_a_canonical_movie_has_no_issue(self) -> None:
        self.assertIsNone(se.canonical_movie_layout_issue(self.movie(), self.library))


class ExternalCommandTests(unittest.TestCase):
    def test_an_empty_command_is_refused_with_a_diagnostic(self) -> None:
        self.assertEqual((127, "", "could not run command: no program was given"),
                         se.run_external_command([]))

    def test_a_timeout_is_reported_as_124(self) -> None:
        import subprocess
        with mock.patch.object(se.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("mkvextract", 5)):
            rc, _out, err = se.run_external_command(["mkvextract"], timeout=5)
        self.assertEqual(124, rc)
        self.assertIn("timed out after 5s", err)

    def test_a_missing_binary_is_reported_as_127(self) -> None:
        with mock.patch.object(se.subprocess, "run", side_effect=OSError("ENOENT")):
            rc, _out, err = se.run_external_command(["mkvextract"])
        self.assertEqual(127, rc)
        self.assertIn("could not run mkvextract", err)

    def test_a_successful_run_decodes_its_output(self) -> None:
        proc = types.SimpleNamespace(returncode=0, stdout=b"out", stderr=b"err")
        with mock.patch.object(se.subprocess, "run", return_value=proc):
            self.assertEqual((0, "out", "err"), se.run_external_command(["mkvextract"]))

    def test_mkvtoolnix_is_found_by_explicit_path_then_on_path(self) -> None:
        binary = self._binary()
        self.assertEqual(str(binary), se.find_mkvtoolnix_binary("mkvmerge", str(binary)))
        with mock.patch.object(se.shutil, "which", return_value="/usr/bin/mkvmerge"):
            self.assertEqual("/usr/bin/mkvmerge", se.find_mkvtoolnix_binary("mkvmerge"))
        with mock.patch.object(se.shutil, "which", return_value=None):
            self.assertIsNone(se.find_mkvtoolnix_binary("mkvmerge", str(self.root_placeholder() / "nope")))

    def test_a_known_install_path_is_used_when_the_path_lookup_fails(self) -> None:
        binary = self._binary()
        with mock.patch.object(se.shutil, "which", return_value=None), \
                mock.patch.dict(se._MKVTOOLNIX_PATHS, {"mkvmerge": (str(binary),)}):
            self.assertEqual(str(binary), se.find_mkvtoolnix_binary("mkvmerge"))
        with mock.patch.object(se.shutil, "which", return_value=None):
            self.assertIsNone(se.find_mkvtoolnix_binary("mkvmerge"))

    def _binary(self) -> Path:
        binary = self.root_placeholder() / "mkvmerge"
        binary.write_bytes(b"#!/bin/sh\n")
        return binary

    def root_placeholder(self) -> Path:
        if not hasattr(self, "_root"):
            self._td = tempfile.TemporaryDirectory(prefix="se_bin_")
            self.addCleanup(self._td.cleanup)
            self._root = Path(self._td.name)
        return self._root


class SrtParsingTests(unittest.TestCase):
    def test_a_cue_without_a_timing_line_is_dropped(self) -> None:
        self.assertEqual([], se.parse_srt_cues("1\nnot a timing line\nHello\n"))

    def test_a_cue_with_a_bad_timing_line_is_dropped(self) -> None:
        self.assertEqual([], se.parse_srt_cues("1\nabc --> def\nHello\n"))

    def test_a_cue_with_no_body_is_dropped(self) -> None:
        self.assertEqual([], se.parse_srt_cues("1\n00:00:01,000 --> 00:00:02,000\n\n"))

    def test_a_valid_document_round_trips(self) -> None:
        cues = se.parse_srt_cues(_VALID_SRT)
        self.assertEqual(2, len(cues))
        self.assertEqual("Hello.", cues[0][2])

    def test_a_timestamp_that_is_not_a_clock_is_refused(self) -> None:
        self.assertIsNone(se._pad_srt_timestamp("no clock here"))
        self.assertIsNone(se._pad_srt_timestamp("1:2"))
        self.assertIsNone(se._pad_srt_timestamp("aa:bb:cc,000"))

    def test_a_dot_millisecond_separator_is_accepted(self) -> None:
        self.assertEqual("00:00:01,500", se._pad_srt_timestamp("00:00:01.5"))


class AssConversionTests(unittest.TestCase):
    def _ass(self, events: str, fmt: str = "Format: Layer, Start, End, Style, Text") -> str:
        return ("[Script Info]\nTitle: x\n\n[Events]\n"
                f"{fmt}\n{events}\n")

    def test_a_comment_line_is_dropped_and_a_dialogue_kept(self) -> None:
        document = self._ass("Dialogue: 0,0:00:01.00,0:00:02.00,Default,Hello there\n"
                             "Comment: 0,0:00:03.00,0:00:04.00,Default,Do not show")
        srt = se.ass_to_srt(document)
        self.assertIn("Hello there", srt)
        self.assertNotIn("Do not show", srt)

    def test_a_dialogue_before_any_format_line_is_dropped(self) -> None:
        document = ("[Events]\n"
                    "Dialogue: 0,0:00:01.00,0:00:02.00,Default,Hello\n")
        self.assertNotIn("Hello", se.ass_to_srt(document))

    def test_a_dialogue_with_too_few_columns_is_dropped(self) -> None:
        document = self._ass("Dialogue: 0,0:00:01.00,Default,Hello")
        self.assertNotIn("Hello", se.ass_to_srt(document))

    def test_a_dialogue_with_an_unreadable_timestamp_is_dropped(self) -> None:
        document = self._ass("Dialogue: 0,not-a-time,0:00:02.00,Default,Hello")
        self.assertNotIn("Hello", se.ass_to_srt(document))

    def test_a_dialogue_with_empty_text_is_dropped(self) -> None:
        document = self._ass("Dialogue: 0,0:00:01.00,0:00:02.00,Default,")
        self.assertNotIn("Dialogue", se.ass_to_srt(document))

    def test_a_non_events_section_is_ignored(self) -> None:
        document = "[Script Info]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,Hello\n"
        self.assertNotIn("Hello", se.ass_to_srt(document))


class VttConversionTests(unittest.TestCase):
    def test_a_bom_and_a_header_block_are_skipped(self) -> None:
        document = ("\ufeffWEBVTT\n\nNOTE something\n\n"
                    "00:01.000 --> 00:02.000\nHello\n")
        srt = se.vtt_to_srt(document)
        self.assertIn("Hello", srt)
        self.assertNotIn("WEBVTT", srt)

    def test_a_block_without_a_timing_line_is_skipped(self) -> None:
        self.assertNotIn("Hello", se.vtt_to_srt("WEBVTT\n\nHello\n"))

    def test_a_timing_line_without_a_recognizable_range_is_skipped(self) -> None:
        self.assertNotIn("Hello", se.vtt_to_srt("WEBVTT\n\nabc --> def\nHello\n"))

    def test_a_cue_with_no_text_is_skipped(self) -> None:
        self.assertNotIn("-->", se.vtt_to_srt("WEBVTT\n\n00:01.000 --> 00:02.000\n"))

    def test_a_cue_setting_after_the_timestamp_is_ignored(self) -> None:
        srt = se.vtt_to_srt("WEBVTT\n\n00:01.000 --> 00:02.000 align:start position:0%\nHello\n")
        self.assertIn("Hello", srt)

    def test_usf_tags_are_stripped(self) -> None:
        srt = se.vtt_to_srt("WEBVTT\n\n00:01.000 --> 00:02.000\n<v Speaker>Hello</v>\n")
        self.assertIn("Hello", srt)
        self.assertNotIn("<v", srt)


class CandidateParsingTests(unittest.TestCase):
    def _entry(self, **attributes: object) -> dict:
        base = {"files": [{"file_id": 42, "file_name": "Movie.eng.srt"}]}
        base.update(attributes)
        return {"type": "subtitle", "id": "7", "attributes": base}

    def test_a_non_mapping_entry_is_dropped(self) -> None:
        self.assertIsNone(se._candidate_from_entry("nope"))

    def test_a_non_subtitle_entry_is_dropped(self) -> None:
        self.assertIsNone(se._candidate_from_entry({"type": "movie"}))

    def test_missing_attributes_are_dropped(self) -> None:
        self.assertIsNone(se._candidate_from_entry({"type": "subtitle"}))

    def test_a_file_id_that_is_not_a_positive_integer_is_dropped(self) -> None:
        self.assertIsNone(se._candidate_from_entry(self._entry(files=[{"file_id": "abc"}])))
        self.assertIsNone(se._candidate_from_entry(self._entry(files=[{"file_id": 0}])))
        self.assertIsNone(se._candidate_from_entry(self._entry(files=["not-a-dict"])))
        self.assertIsNone(se._candidate_from_entry(self._entry(files=[])))

    def test_a_valid_entry_becomes_a_candidate(self) -> None:
        candidate = se._candidate_from_entry(self._entry(
            subtitle_id="9", language="en", moviehash_match="true", votes="4",
            ratings="7.5", download_count="120"))
        assert candidate is not None
        self.assertEqual(42, candidate.file_id)
        self.assertTrue(candidate.moviehash_match)
        self.assertEqual(4, candidate.votes)
        self.assertEqual(120, candidate.download_count)


class JsonDocumentTests(unittest.TestCase):
    def test_an_empty_answer_is_an_empty_object(self) -> None:
        self.assertEqual({}, se._json_document(b"", "GET /x"))

    def test_a_non_json_answer_names_the_endpoint(self) -> None:
        with self.assertRaises(se.OpenSubtitlesError) as caught:
            se._json_document(b"<html>hi</html>", "GET /x")
        self.assertIn("did not answer with JSON", str(caught.exception))

    def test_a_json_array_is_not_an_object(self) -> None:
        with self.assertRaises(se.OpenSubtitlesError) as caught:
            se._json_document(b"[]", "GET /x")
        self.assertIn("not an object", str(caught.exception))


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, _limit: int = -1) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class OpenSubtitlesRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = se.OpenSubtitlesClient("key", user_agent="tests")

    def test_a_transport_failure_is_reported_as_a_provider_error(self) -> None:
        with mock.patch.object(se, "urlopen", side_effect=urllib.error.URLError("no route")), \
                mock.patch.object(self.client, "_throttle"), self.assertRaises(se.OpenSubtitlesError) as caught:
            self.client._request("GET", "infos/formats")
        self.assertIn("could not reach OpenSubtitles", str(caught.exception))

    def test_an_http_error_becomes_the_providers_own_message(self) -> None:
        error = _http_error(401, json.dumps({"message": "bad key"}).encode())
        with mock.patch.object(se, "urlopen", side_effect=error), \
                mock.patch.object(self.client, "_throttle"), self.assertRaises(se.OpenSubtitlesError) as caught:
            self.client._request("GET", "infos/formats")
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertIn("bad key", str(caught.exception))

    def test_an_oversized_answer_is_refused_before_parsing(self) -> None:
        blob = b"x" * (se.OPENSUBTITLES_ANSWER_MAX_BYTES + 1)
        with mock.patch.object(se, "urlopen", return_value=_FakeResponse(blob)), \
                mock.patch.object(self.client, "_throttle"), self.assertRaises(se.OpenSubtitlesError) as caught:
            self.client._request("GET", "infos/formats")
        self.assertIn("oversized", str(caught.exception))

    def test_a_rate_limit_is_retried_then_reported(self) -> None:
        error = _http_error(429, headers={"Retry-After": "0"})
        with mock.patch.object(se, "urlopen", side_effect=error), \
                mock.patch.object(se.time, "sleep") as sleep, \
                mock.patch.object(self.client, "_throttle"), self.assertRaises(se.OpenSubtitlesError) as caught:
            self.client._request("GET", "infos/formats")
        self.assertIn("429", str(caught.exception))
        self.assertEqual(se.OPENSUBTITLES_MAX_ATTEMPTS - 1, sleep.call_count)

    def test_a_rate_limit_that_clears_is_served(self) -> None:
        error = _http_error(429, headers={"Retry-After": "1"})
        answers = [error, _FakeResponse(json.dumps({"data": []}).encode())]
        with mock.patch.object(se, "urlopen", side_effect=answers), \
                mock.patch.object(se.time, "sleep"), \
                mock.patch.object(self.client, "_throttle"):
            self.assertEqual({"data": []}, self.client._request("GET", "infos/formats"))

    def test_parameters_are_sorted_and_bodies_are_sent_as_json(self) -> None:
        seen: dict = {}

        def capture(request: object, timeout: float) -> _FakeResponse:
            seen["url"] = request.full_url
            seen["body"] = request.data
            seen["headers"] = dict(request.headers)
            return _FakeResponse(b"{}")

        with mock.patch.object(se, "urlopen", side_effect=capture), \
                mock.patch.object(self.client, "_throttle"):
            self.client._request("POST", "login", params={"b": "2", "a": "1"},
                                 body={"username": "u"}, token="tok")
        self.assertIn("a=1&b=2", seen["url"])
        self.assertEqual(b'{"username": "u"}', seen["body"])
        self.assertIn("Bearer tok", seen["headers"].get("Authorization", ""))


class HttpClientDetailsTests(unittest.TestCase):
    def test_a_json_error_body_is_used_as_the_detail(self) -> None:
        self.assertIn("bad key", se.OpenSubtitlesClient._http_error(
            _http_error(500, json.dumps({"error": "bad key"}).encode())))

    def test_an_unreadable_error_body_falls_back_to_the_reason(self) -> None:
        def unreadable(_size: int = -1) -> bytes:
            raise OSError("gone")

        error = types.SimpleNamespace(code=503, read=unreadable, reason="service unavailable")
        self.assertIn("service unavailable", se.OpenSubtitlesClient._http_error(error))

    def test_a_retry_after_header_wins(self) -> None:
        self.assertEqual(5.0, se.OpenSubtitlesClient._backoff_seconds(
            _http_error(429, headers={"Retry-After": "5"}), 1))

    def test_a_reset_timestamp_in_the_future_is_honoured(self) -> None:
        import time as time_module
        future = str(int(time_module.time()) + 3)
        seconds = se.OpenSubtitlesClient._backoff_seconds(
            _http_error(429, headers={"X-RateLimit-Reset": future}), 1)
        self.assertGreaterEqual(seconds, 1.0)
        self.assertLessEqual(seconds, se.OPENSUBTITLES_MAX_BACKOFF_SEC)

    def test_a_missing_header_mapping_falls_back_to_doubling(self) -> None:
        error = _http_error(429)
        error.headers = None  # AttributeError inside the header walk
        self.assertEqual(2.0, se.OpenSubtitlesClient._backoff_seconds(error, 1))
        self.assertEqual(16.0, se.OpenSubtitlesClient._backoff_seconds(error, 4))

    def test_backoff_is_capped(self) -> None:
        error = _http_error(429)
        error.headers = None
        self.assertEqual(se.OPENSUBTITLES_MAX_BACKOFF_SEC,
                         se.OpenSubtitlesClient._backoff_seconds(error, 20))


class LoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = se.OpenSubtitlesClient("key", username="u", password="p",
                                             user_agent="tests")

    def test_an_account_less_client_refuses_to_log_in(self) -> None:
        anonymous = se.OpenSubtitlesClient("key", user_agent="tests")
        with self.assertRaises(se.OpenSubtitlesError) as caught:
            anonymous.login()
        self.assertIn("OPENSUBTITLES_USERNAME", str(caught.exception))

    def test_a_login_without_a_token_is_refused(self) -> None:
        with mock.patch.object(self.client, "_request", return_value={}), \
                self.assertRaises(se.OpenSubtitlesError) as caught:
            self.client.login()
        self.assertIn("without a token", str(caught.exception))

    def test_a_login_stores_the_token_once(self) -> None:
        with mock.patch.object(self.client, "_request",
                               return_value={"token": " abc "}) as request:
            self.assertEqual("abc", self.client.login())
            self.assertEqual("abc", self.client.session_token())
        self.assertEqual(1, request.call_count)

    def test_a_refresh_forces_a_new_login(self) -> None:
        with mock.patch.object(self.client, "_request",
                               return_value={"token": "fresh"}) as request:
            self.client.login()
            self.assertEqual("fresh", self.client.session_token(refresh=True))
        self.assertEqual(2, request.call_count)

    def test_the_probe_reports_what_the_api_offers(self) -> None:
        with mock.patch.object(self.client, "_request",
                               return_value={"data": [{"format": "srt"}, {"format": "ass"}]}):
            self.assertIn("2 subtitle format(s)", self.client.probe())
        with mock.patch.object(self.client, "_request", return_value={"data": "no"}):
            self.assertEqual("the API answered", self.client.probe())


class MovieHashTests(_Case):
    def test_a_movie_below_the_provider_floor_is_refused(self) -> None:
        video = self.movie("Small (1999)", size=1024)
        with self.assertRaises(ValueError):
            se.moviehash_of_file(video)

    def test_an_unreadable_movie_is_an_oserror(self) -> None:
        with self.assertRaises(OSError):
            se.moviehash_of_file(self.library / "gone.mkv")

    def test_a_movie_that_changed_size_while_hashing_is_refused(self) -> None:
        video = self.movie("Shrink (1999)", size=300_000)
        real_open = Path.open

        class HalfReader:
            def __init__(self, handle: object) -> None:
                self._handle = handle

            def read(self, count: int = -1) -> bytes:
                return self._handle.read(count // 2)

            def seek(self, *args: object) -> int:
                return self._handle.seek(*args)

            def __enter__(self) -> HalfReader:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        def half_open(self: Path, *args: object, **kwargs: object) -> object:
            return HalfReader(real_open(self, *args, **kwargs))

        with mock.patch.object(Path, "open", half_open), self.assertRaises(ValueError) as caught:
            se.moviehash_of_file(video)
        self.assertIn("changed size", str(caught.exception))

    def test_a_normal_movie_hashes(self) -> None:
        video = self.movie("Hash (1999)", size=300_000)
        digest, size = se.moviehash_of_file(video)
        self.assertEqual(300_000, size)
        self.assertEqual(16, len(digest))


class FolderProbeTests(_Case):
    def test_a_writable_folder_accepts_files(self) -> None:
        self.assertTrue(se._folder_accepts_new_files(self.library))

    def test_a_folder_without_write_access_is_refused(self) -> None:
        def deny(path: Path, mode: int) -> bool:
            return False

        with mock.patch.object(se.os, "access", side_effect=deny), \
                mock.patch.object(Path, "open", side_effect=OSError("read-only")):
            self.assertFalse(se._folder_accepts_new_files(self.library))

    def test_a_probe_that_cannot_be_removed_still_counts_as_writable(self) -> None:
        def deny(path: Path, mode: int) -> bool:
            return False

        real_unlink = Path.unlink

        def stubborn(self: Path, **kwargs: object) -> None:
            if self.name.startswith(".organize-write-probe."):
                raise OSError("busy")
            real_unlink(self, **kwargs)

        with mock.patch.object(se.os, "access", side_effect=deny), \
                mock.patch.object(Path, "unlink", stubborn):
            self.assertTrue(se._folder_accepts_new_files(self.library))

    def test_an_os_access_error_falls_through_to_the_write_probe(self) -> None:
        with mock.patch.object(se.os, "access", side_effect=OSError("no access")):
            self.assertTrue(se._folder_accepts_new_files(self.library))


class LedgerTests(_Case):
    def test_a_missing_ledger_is_an_empty_one(self) -> None:
        self.assertEqual({"version": se.EXTRACTED_LEDGER_VERSION, "sidecars": {}},
                         se.load_extracted_ledger(self.root / "missing.json"))

    def test_a_damaged_ledger_is_an_empty_one(self) -> None:
        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        self.assertEqual({}, se.load_extracted_ledger(broken)["sidecars"])

    def test_a_ledger_without_a_sidecar_map_is_an_empty_one(self) -> None:
        wrong = self.root / "wrong.json"
        wrong.write_text(json.dumps({"sidecars": []}), encoding="utf-8")
        self.assertEqual({}, se.load_extracted_ledger(wrong)["sidecars"])

    def test_the_legacy_ledger_is_read_beside_the_current_name(self) -> None:
        target = self.root / "subtitle_extractor_extracted.json"
        (target.parent / se.LEGACY_EXTRACTED_LEDGER_NAME).write_text(
            json.dumps({"version": 1, "sidecars": {"a": 1}}), encoding="utf-8")
        with mock.patch.object(se, "extracted_ledger_path", return_value=target):
            ledger = se.load_extracted_ledger()
        self.assertEqual({"a": 1}, ledger["sidecars"])
        self.assertFalse(target.exists(), "reading a ledger never creates one")


class OpensubtitlesCheckTests(_Case):
    def test_a_working_provider_answers_true(self) -> None:
        with mock.patch.object(se, "OpenSubtitlesClient") as client:
            client.return_value.probe.return_value = "5 formats"
            ok, detail = se.opensubtitles_check("key")
        self.assertTrue(ok)
        self.assertEqual("5 formats", detail)

    def test_a_provider_failure_answers_false_with_the_reason(self) -> None:
        with mock.patch.object(se, "OpenSubtitlesClient") as client:
            client.return_value.probe.side_effect = se.OpenSubtitlesError("rate limited")
            ok, detail = se.opensubtitles_check("key")
        self.assertFalse(ok)
        self.assertIn("rate limited", detail)


class TriageTests(_Case):
    def test_a_layout_issue_settles_the_movie_before_any_read(self) -> None:
        video = self.library / "Loose (1999).mkv"
        video.write_bytes(b"x")
        triage = se.triage_movie(video, self.library)
        self.assertTrue(triage.layout_issue)
        self.assertFalse(triage.fetchable)

    def test_a_covered_movie_is_settled_without_a_snapshot(self) -> None:
        video = self.movie()
        (video.parent / "Movie (1999).eng.srt").write_text(_VALID_SRT, encoding="utf-8")
        triage = se.triage_movie(video, self.library)
        self.assertEqual("covered", triage.sidecar_status)
        self.assertIsNone(triage.snapshot)

    def test_an_unreadable_movie_is_an_error_triage_not_a_crash(self) -> None:
        video = self.movie()
        with mock.patch.object(se, "video_snapshot", side_effect=OSError("drive gone")):
            triage = se.triage_movie(video, self.library)
        self.assertIn("drive gone", triage.error)
        self.assertFalse(triage.fetchable)

    def test_a_fetchable_movie_carries_its_snapshot_and_key(self) -> None:
        video = self.movie()
        triage = se.triage_movie(video, self.library)
        self.assertTrue(triage.fetchable)
        self.assertIsNotNone(triage.snapshot)
        self.assertTrue(triage.key)


class SidecarInspectionTests(_Case):
    def test_a_canonical_valid_sidecar_means_covered(self) -> None:
        video = self.movie()
        sidecar = video.parent / "Movie (1999).eng.srt"
        sidecar.write_text(_VALID_SRT, encoding="utf-8")
        status, path, _detail, reason = se.inspect_existing_sidecars(video)
        self.assertEqual("covered", status)
        self.assertEqual(sidecar, path)
        self.assertEqual(se.REASON_COVERED, reason)

    def test_a_valid_sidecar_with_the_wrong_name_needs_review(self) -> None:
        video = self.movie()
        # English, but not the ".eng.srt" covering name the cleaner requires.
        (video.parent / "Movie (1999).English.srt").write_text(_VALID_SRT, encoding="utf-8")
        status, path, detail, reason = se.inspect_existing_sidecars(video)
        self.assertEqual("review", status)
        self.assertEqual(se.REASON_SIDECAR_NAME, reason)
        assert path is not None
        self.assertIn("rename or remove", detail)

    def test_a_broken_sidecar_needs_review_so_it_can_be_replaced(self) -> None:
        video = self.movie()
        (video.parent / "Movie (1999).eng.srt").write_bytes(b"not a subtitle")
        status, path, detail, reason = se.inspect_existing_sidecars(video)
        self.assertEqual("review", status)
        self.assertEqual(se.REASON_SIDECAR_UNUSABLE, reason)
        assert path is not None

    def test_a_sidecar_that_cannot_be_read_is_unusable_not_covered(self) -> None:
        video = self.movie()
        (video.parent / "Movie (1999).eng.srt").write_text(_VALID_SRT, encoding="utf-8")
        with mock.patch.object(Path, "read_bytes", side_effect=OSError("unreadable")):
            status, _path, _detail, reason = se.inspect_existing_sidecars(video)
        self.assertEqual("review", status)
        self.assertEqual(se.REASON_SIDECAR_UNUSABLE, reason)

    def test_a_symlink_occupying_the_canonical_name_needs_review(self) -> None:
        # The canonical path is taken by a symlink: the legacy promoter refuses
        # to write through it, and nothing may overwrite it silently.
        video = self.movie()
        target = self.root / "real.srt"
        target.write_text(_VALID_SRT, encoding="utf-8")
        (video.parent / "Movie (1999).eng.srt").symlink_to(target)
        status, _path, detail, reason = se.inspect_existing_sidecars(video)
        self.assertEqual("review", status)
        self.assertEqual(se.REASON_SIDECAR_NAME, reason)
        self.assertIn("occupied", detail)

    def test_an_unlistable_folder_is_missing_with_a_reason(self) -> None:
        video = self.movie()
        real_iterdir = Path.iterdir

        def deny(self: Path) -> object:
            if self == video.parent:
                raise OSError("permission denied")
            return real_iterdir(self)

        with mock.patch.object(Path, "iterdir", deny):
            status, _path, detail, _reason = se.inspect_existing_sidecars(video)
        self.assertEqual("missing", status)
        self.assertIn("could not inspect", detail)

    def test_no_sidecar_at_all_is_missing(self) -> None:
        status, path, _detail, reason = se.inspect_existing_sidecars(self.movie())
        self.assertEqual("missing", status)
        self.assertIsNone(path)
        self.assertEqual("", reason)


class ConfigValidationTests(_Case):
    def test_every_configuration_error_is_named(self) -> None:
        cfg = self.cfg(
            library=self.root / "missing", extract_min_cues=0, download_min_cues=0,
            download_limit=-1, download_timeout_seconds=0, workers=-1,
            min_movie_size_mb=-1, lock_timeout_seconds=-1, limit=-1,
        )
        errors = "\n".join(se.validate_config(cfg))
        for fragment in ("--source must be an existing non-symlink movie-library directory",
                         "--extract-min-cues must be at least 1",
                         "--download-min-cues must be at least 1",
                         "--download-limit must be zero (no run cap) or greater",
                         "--download-timeout must be greater than zero",
                         "--workers must be non-negative",
                         "--min-size, --lock-timeout, and --limit must be non-negative"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, errors)

    def test_artifacts_inside_the_media_library_are_refused(self) -> None:
        cfg = self.cfg(library=self.library, report_file=self.library / "report.txt",
                       log_file=self.library / "run.log")
        errors = "\n".join(se.validate_config(cfg))
        self.assertIn("--report must be outside the Jellyfin media library", errors)
        self.assertIn("--log must be outside the Jellyfin media library", errors)

    def test_a_good_config_has_no_errors(self) -> None:
        self.assertEqual([], se.validate_config(self.cfg()))


class ExtractionRunTests(_Case):
    """The run loop's four non-download outcomes and the quota notes."""

    def _run(self, cfg: se.ExtractorConfig, triages: dict[str, object],
             extra: dict | None = None) -> tuple[list[se.JobResult], dict]:
        with mock.patch.object(se, "triage_movie",
                               side_effect=lambda video, library: triages[video.name]), \
                mock.patch.object(se, "extract_embedded_english_srt",
                                  return_value=(extra or {}).get(
                                      "extract", se.ExtractionOutcome(
                                          ok=False, detail="no track",
                                          text_tracks=(), image_tracks=()))), \
                mock.patch.object(se, "download_hash_matched_srt",
                                  return_value=(extra or {}).get(
                                      "download", se.DownloadOutcome(unavailable_reason="off"))), \
                redirect_stdout(io.StringIO()):
            return se.extraction_run(cfg)

    def test_limit_zero_means_no_limit_and_limits_are_counted(self) -> None:
        video = self.movie()
        triage = se.Triage(video, sidecar_status="covered", existing=video,
                           sidecar_detail="have it")
        cfg = self.cfg(limit=0)
        results, summary = self._run(cfg, {video.name: triage})
        self.assertEqual([("have", se.REASON_COVERED)],
                         [(r.status, r.reason) for r in results])
        self.assertIsInstance(summary, dict)

    def test_a_layout_skip_is_reported_and_never_probed(self) -> None:
        video = self.movie()
        triage = se.Triage(video, layout_issue="noncanonical layout")
        results, _summary = self._run(self.cfg(), {video.name: triage})
        self.assertEqual("skip", results[0].status)
        self.assertEqual(se.REASON_LAYOUT, results[0].reason)

    def test_a_review_sidecar_is_reported_with_its_reason(self) -> None:
        video = self.movie()
        triage = se.Triage(video, sidecar_status="review", existing=video,
                           sidecar_detail="rename it", sidecar_reason=se.REASON_SIDECAR_NAME)
        results, _summary = self._run(self.cfg(), {video.name: triage})
        self.assertEqual("review", results[0].status)
        self.assertEqual(se.REASON_SIDECAR_NAME, results[0].reason)

    def test_an_unfetchable_movie_is_an_error_row(self) -> None:
        video = self.movie()
        triage = se.Triage(video, error="could not stat")
        results, _summary = self._run(self.cfg(), {video.name: triage})
        self.assertEqual("error", results[0].status)
        self.assertEqual(se.REASON_ERROR, results[0].reason)

    def test_a_limit_caps_the_run(self) -> None:
        first = self.movie("A (1999)")
        second = self.movie("B (2000)")
        triages = {v.name: se.Triage(v, layout_issue="noncanonical") for v in (first, second)}
        results, _summary = self._run(self.cfg(limit=1), triages)
        self.assertEqual(1, len(results))

    def test_a_run_that_filtered_everything_says_what_it_filtered(self) -> None:
        (self.library / "Small (1999)").mkdir()
        (self.library / "Small (1999)" / "Small (1999).mkv").write_bytes(b"x")
        (self.library / "Samples").mkdir()
        (self.library / "Samples" / "sample.mkv").write_bytes(b"x" * 300_000)
        out = io.StringIO()
        with redirect_stdout(out):
            results, _summary = se.extraction_run(self.cfg(min_movie_size_mb=1))
        self.assertEqual([], results)
        self.assertIn("Nothing eligible", out.getvalue())

    def test_a_missing_binary_is_noted_once_for_the_whole_run(self) -> None:
        video = self.movie()
        triage = se.Triage(video, sidecar_status="missing", snapshot=object(), key="k")
        outcome = se.ExtractionOutcome(ok=False, detail="mkvextract is not installed",
                                       unavailable_reason="mkvextract: not installed")
        with mock.patch.object(se, "triage_movie", return_value=triage), \
                mock.patch.object(se, "extract_embedded_english_srt", return_value=outcome), \
                mock.patch.object(se, "download_hash_matched_srt",
                                  return_value=se.DownloadOutcome(unavailable_reason="off")), \
                redirect_stdout(io.StringIO()) as out:
            se.extraction_run(self.cfg())
        self.assertIn("not installed", out.getvalue())


class MainTests(_Case):
    def test_the_self_test_flag_runs_the_checks(self) -> None:
        with mock.patch.object(se, "run_self_tests", return_value=0) as self_test, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, se.main(["--self-test"]))
        self_test.assert_called_once()

    def test_a_bad_configuration_is_exit_2(self) -> None:
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(2, se.main(["--source", str(self.root / "missing")]))
        self.assertIn("Configuration error", err.getvalue())

    def test_an_interrupt_is_exit_130(self) -> None:
        video = self.movie()
        with mock.patch.object(se, "extraction_run", side_effect=KeyboardInterrupt), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(130, se.main(["--source", str(self.library)]))
        self.assertTrue(video.exists())

    def test_an_unexpected_failure_is_exit_1_not_a_traceback(self) -> None:
        with mock.patch.object(se, "extraction_run", side_effect=RuntimeError("boom")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(1, se.main(["--source", str(self.library)]))

    def test_a_run_with_an_error_row_is_exit_1(self) -> None:
        video = self.movie()
        results = [se.JobResult(video, "error", "no snapshot", reason=se.REASON_ERROR)]
        with mock.patch.object(se, "extraction_run", return_value=(results, {})), \
                mock.patch.object(se, "write_report"), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(1, se.main(["--source", str(self.library)]))

    def test_a_clean_run_is_exit_0(self) -> None:
        with mock.patch.object(se, "extraction_run", return_value=([], {})), \
                mock.patch.object(se, "write_report"), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, se.main(["--source", str(self.library)]))
