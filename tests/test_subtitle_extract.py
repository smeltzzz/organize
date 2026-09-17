"""Tests for embedded-subtitle extraction in ``subtitle_extractor.py``.

The whole suite is offline: ``subprocess.run`` is replaced with a fake that
serves a canned ``mkvmerge -J`` payload and writes a canned subtitle track, and
the OpenSubtitles HTTP layer is served from canned payloads, so no
MKVToolNix, no media file and no network is needed.

The properties pinned here are the ones that decide whether a sidecar is
trustworthy: a movie's own text track must only win when it is complete
English (never a forced/signs-only stream, never garbage), a sidecar built
that way - or downloaded by exact-hash match for an image-only movie - is
recorded in the provenance ledger, and a sidecar that was already there
before this tool ran is never touched at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

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


def good_download_srt(cues: int = 30) -> str:
    """English dialogue dense enough to pass the quality gate."""
    return sx.render_srt_cues([
        (f"00:00:{index % 60:02d},000", f"00:00:{index % 60:02d},900",
         "This is a line of English dialogue")
        for index in range(cues)
    ])


def canned_entries(*file_ids: int, **kwargs: object) -> list[dict]:
    """One plausible /subtitles answer: English SRT uploads for one hash."""
    return [{
        "id": 100 + index,
        "downloads": kwargs.pop("downloads", 500 - index * 10),
        "hearing_impaired": kwargs.pop("hearing_impaired", False),
        "files": [{"file_id": file_id, "file_name": f"movie.{file_id}.srt"}
                  for file_id in file_ids],
    } for index in range(len(file_ids))]


class FakeOsdbHttp:
    """Serves the OpenSubtitles v1 API and file downloads from canned payloads.

    Patches stand on top of the hermetic pin (which makes any call raise), so
    a test that wants the network fakes it here; a test that wants to prove a
    code path never reaches the network simply does not install this one.
    """

    def __init__(self, entries: list[dict] | None = None, srt_text: str | None = None,
                 statuses: dict[str, int] | None = None) -> None:
        self.entries = canned_entries(77, 78) if entries is None else entries
        self.srt_text = good_download_srt() if srt_text is None else srt_text
        self.statuses = statuses or {}
        self.calls: list[tuple[str, str]] = []
        self.login_count = 0
        self.failed_logins = 0  # how many logins answer 401 before succeeding
        self.bad_file_ids: set[int] = set()  # file ids whose file fails the gate

    # the same contract as subtitle_extractor.osdb_http
    def __call__(self, url: str, *, method: str = "GET", headers: dict | None = None,
                 body: bytes | None = None, timeout: float = 30.0):
        self.calls.append((method, url))
        for key, status in self.statuses.items():
            if key in url:
                return status, b"{}", "", {}
        if url.endswith("/subtitles") or "/subtitles?" in url:
            return 200, json.dumps({"data": self.entries}).encode("utf-8"), "", {}
        if url.endswith("/login"):
            self.login_count += 1
            if self.login_count <= self.failed_logins:
                return 401, b'{"message": "bad credentials"}', "", {}
            return 200, json.dumps({"token": "tok-1"}).encode("utf-8"), "", {}
        if url.endswith("/download"):
            file_id = int(json.loads(body).get("file_id", 0))
            if self.login_count == 0:
                # a real client cannot have called /download without logging in
                raise AssertionError("download was requested before login")
            return 200, json.dumps({"link": f"https://files.example/{file_id}.srt"}).encode(), "", {}
        # a temporary file URL: https://files.example/<id>.srt
        file_id = int(url.rsplit("/", 1)[1].split(".")[0])
        if file_id in self.bad_file_ids:
            return 200, (
                "1\n00:00:01,000 --> 00:00:02,000\nJust one lonely cue\n"
            ).encode("utf-8"), "", {}
        return 200, self.srt_text.encode("utf-8"), "", {}

    def url_kinds(self) -> list[str]:
        kinds = []
        for _method, url in self.calls:
            if "/subtitles" in url:
                kinds.append("search")
            elif "/login" in url:
                kinds.append("login")
            elif "/download" in url:
                kinds.append("download")
            else:
                kinds.append("file")
        return kinds



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
        ok, reason = sx.english_subtitle_quality(
            self._cues("This is a line of English dialogue"))
        self.assertTrue(ok, reason)

    def test_signs_only_track_is_refused(self) -> None:
        ok, reason = sx.english_subtitle_quality(
            sx.render_srt_cues([("00:00:01,000", "00:00:02,000", "Only line")]))
        self.assertFalse(ok)
        self.assertIn("signs/songs-only", reason)

    def test_cyrillic_track_is_refused(self) -> None:
        ok, reason = sx.english_subtitle_quality(
            self._cues("Это предложение на русском языке"))
        self.assertFalse(ok)
        self.assertIn("not Latin-script", reason)

    def test_word_salad_is_refused(self) -> None:
        ok, reason = sx.english_subtitle_quality(
            self._cues("Qwx zp vfg blrt mnk jklqwerty"))
        self.assertFalse(ok)
        self.assertIn("does not read as English", reason)

    def test_empty_text_is_refused(self) -> None:
        ok, _reason = sx.english_subtitle_quality("")
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

    def test_dry_run_of_an_image_only_movie_never_runs_mkvextract(self) -> None:
        runner = FakeRunner(PGS_TRACKS)
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            outcome = sx.extract_embedded_english_srt(
                self.movie, self.dest, sx.ExtractOptions(min_cues=2, dry_run=True))
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.image_only)
        self.assertFalse(any("tracks" in call for call in runner.calls),
                         "a dry run must not run mkvextract on an image track")
        self.assertFalse(self.dest.exists())

    def test_image_only_movie_is_flagged_for_the_hash_download(self) -> None:
        outcome = self._run(PGS_TRACKS)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.image_only, "image-only is what hands the movie to the download")
        self.assertIn("image-based", outcome.detail)
        self.assertFalse(self.dest.exists())

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
    """What happens between mkvextract and a sidecar, for one text track.

    The extraction path is fully local - no provider, no quota, no network -
    and only wins when what comes out of the container is really the movie's
    English dialogue. Everything below is a way for that to not be true, and
    each one has to end with a reason a human can read and no file on disk.
    (Image tracks never reach this path: they set the image_only verdict and
    are served by the OpenSubtitles download instead.)
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
    """The whole run: extraction is not a tier, it is the entire tool."""

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

    def test_a_movie_with_no_usable_track_needs_attention(self) -> None:
        runner = FakeRunner(PGS_TRACKS)
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            results, summary = sx.extraction_run(self._config())
        self.assertEqual(len(results), 1)
        # No usable text track and no OpenSubtitles account (the suite is
        # offline and unconfigured): the movie is reported, not silently
        # dropped, and never counts as covered.
        self.assertEqual(results[0].reason, sx.REASON_NO_TRACK)
        self.assertEqual(results[0].status, "skip")
        self.assertIn("image-based", results[0].detail)
        self.assertIn("no OpenSubtitles API key", results[0].detail)
        self.assertEqual(int(summary["coverage_covered"]), 0)
        self.assertEqual(int(summary["downloaded_from_opensubtitles"]), 0)
        self.assertEqual(list(self.movie.parent.glob("*.srt")), [],
                         "nothing is written for a movie with no usable track")

    def test_an_image_only_movie_is_downloaded_by_exact_hash(self) -> None:
        """The whole run for the one case extraction cannot serve."""
        runner = FakeRunner(PGS_TRACKS)
        http = FakeOsdbHttp()
        config = self._config(osdb_api_key="key", osdb_username="user",
                              osdb_password="pass")
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sx, "osdb_http", side_effect=http):
            results, summary = sx.extraction_run(config)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].reason, sx.REASON_DOWNLOADED)
        self.assertEqual(results[0].status, "downloaded")
        self.assertTrue(results[0].dest is not None and results[0].dest.is_file())
        self.assertEqual(int(summary["downloaded_from_opensubtitles"]), 1)
        self.assertEqual(int(summary["coverage_covered"]), 1,
                         "a downloaded sidecar counts as coverage")
        record = sx.find_extracted_record(results[0].dest,
                                          sx.sha256_text(results[0].dest.read_text(encoding="utf-8")))
        self.assertIsNotNone(record, "the download is recorded in the provenance ledger")

    def test_a_text_movie_never_reaches_the_network(self) -> None:
        """Extraction wins outright, so no search is even attempted."""
        runner = FakeRunner(TEXT_TRACKS)

        def explode(*_args, **_kwargs):
            raise AssertionError("a text-extractable movie must not touch OpenSubtitles")

        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sx, "osdb_http", side_effect=explode):
            results, _summary = sx.extraction_run(self._config(
                osdb_api_key="key", osdb_username="user", osdb_password="pass"))
        self.assertEqual(results[0].reason, sx.REASON_EXTRACTED)

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

    def test_report_names_what_was_extracted(self) -> None:
        runner = FakeRunner(TEXT_TRACKS)
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            results, summary = sx.extraction_run(self._config())
        text = sx.build_report(results, self._config(), summary)
        self.assertIn("EXTRACTED FROM THE MOVIE'S OWN EMBEDDED TRACK", text)
        self.assertIn("Extracted this run", text)


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
        self.assertTrue(record["extracted_utc"], "the record is stamped")

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
            ok, reason = sx.english_subtitle_quality(
                "1\n00:00:01,000 --> 00:00:02,000\n" + "x" * 200 + "\n", min_cues=1)
        self.assertFalse(ok)
        self.assertIn("safety limit", reason)

    def test_a_track_that_did_not_convert_is_refused(self) -> None:
        ok, reason = sx.english_subtitle_quality("Dialogue: 0,0:00:01.50,...", min_cues=1)
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

class OneTrackDirectlyTests(unittest.TestCase):
    """``_extract_one_track`` alone: the arms the caller normally prevents.

    The loop above it hands over text tracks only, and the outer entry point
    checks for MKVToolNix before it starts. These tests remove those
    guarantees, because a helper that trusts its caller is a helper that
    breaks the day the caller changes.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="one_track_direct_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.movie = self.tmp / "Fake (2021).mkv"
        self.movie.write_bytes(b"mkv-bytes")
        self.dest = self.tmp / "Fake (2021).eng.srt"

    def extract(self, track: sx.EmbeddedSubtitleTrack, *,
                binary: object = fake_binaries) -> sx.ExtractionOutcome:
        with mock.patch.object(sx, "find_mkvtoolnix_binary", binary):
            return sx._extract_one_track(
                self.movie, self.movie, self.dest, track, self.tmp,
                sx.ExtractOptions(min_cues=2))  # type: ignore[arg-type]

    def test_without_mkvextract_nothing_is_attempted(self) -> None:
        track = sx.EmbeddedSubtitleTrack(2, "S_TEXT/UTF8", "eng", "English", "text", ".srt")
        outcome = self.extract(track, binary=lambda *_a, **_k: "")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.detail, "mkvextract is not installed")
        self.assertFalse(self.dest.exists())

    def test_an_image_track_is_not_extracted_here(self) -> None:
        """Image tracks set the image_only verdict; they never reach this path."""
        track = sx.EmbeddedSubtitleTrack(4, "S_HDMV/PGS", "eng", "English", "image", ".sup")
        with mock.patch.object(subprocess, "run", FakeRunner(PGS_TRACKS)):
            outcome = self.extract(track)
        self.assertFalse(outcome.ok)
        self.assertIn("not extracted", outcome.detail)
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


class MovieHashTests(unittest.TestCase):
    """The OpenSubtitles v2 hash: MD5 over the leading bytes plus the size.

    A regression here makes every exact-hash search miss, and the report
    just says "no match" - so the spec is pinned against the algorithm,
    read straight from the service's documentation.
    """

    def _movie(self, body: bytes) -> Path:
        movie = self.tmp / "Fake (2021).mkv"
        movie.write_bytes(body)
        return movie

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="movie_hash_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def expected(self, body: bytes) -> str:
        return hashlib.md5(body + len(body).to_bytes(8, "little")).hexdigest()

    def test_a_small_file_hashes_its_whole_body_plus_its_size(self) -> None:
        body = b"small-body" * 10
        self.assertEqual(sx.compute_movie_hash(self._movie(body)), self.expected(body))

    def test_the_hash_is_a_lowercase_32_digit_hex(self) -> None:
        h = sx.compute_movie_hash(self._movie(b"abc"))
        self.assertEqual(len(h), 32)
        self.assertEqual(h, h.lower())
        int(h, 16)  # raises if it is not hex

    def test_two_files_of_the_same_bytes_but_different_sizes_differ(self) -> None:
        a = self._movie(b"same-leading-bytes")
        b = self.tmp / "Other (2022).mkv"
        b.write_bytes(b"same-leading-bytes!" + b"x" * 100)
        self.assertNotEqual(sx.compute_movie_hash(a), sx.compute_movie_hash(b))

    def test_a_big_file_hashes_only_its_head_plus_its_size(self) -> None:
        """Files over 64 MiB hash their first 64 KiB, not their whole body."""
        body = b"0123456789" * 1000  # 10 KiB, but "large" under the patched limit
        with mock.patch.object(sx, "OSDB_HASH_LARGE_FILE_BYTES", 64), \
                mock.patch.object(sx, "OSDB_HASH_HEAD_BYTES", 4):
            h = sx.compute_movie_hash(self._movie(body))
        # The appended size is the FILE's size, not the head's: that is what
        # distinguishes one release from another.
        self.assertEqual(h, hashlib.md5(body[:4] + len(body).to_bytes(8, "little")).hexdigest())

    def test_an_unreadable_movie_has_no_hash(self) -> None:
        self.assertIsNone(sx.compute_movie_hash(self.tmp / "absent.mkv"))


class OpenSubtitlesClientTests(unittest.TestCase):
    """The client against a canned API: what it asks, and what it says back."""

    def setUp(self) -> None:
        self.client = sx.OpenSubtitlesClient(api_key="key", username="user",
                                             password="pass")

    def test_search_builds_the_exact_hash_query(self) -> None:
        seen: list[tuple[str, str, dict]] = []

        def http(url, *, method="GET", headers=None, body=None, timeout=30.0):
            seen.append((method, url, dict(headers or {})))
            return 200, b'{"data": []}', "", {}

        with mock.patch.object(sx, "osdb_http", side_effect=http):
            entries, error = self.client.search_by_hash("a" * 32)
        self.assertEqual(entries, [])
        self.assertIsNone(error)
        self.assertEqual(seen[0][0], "GET")
        query = urllib.parse.parse_qs(seen[0][1].split("?", 1)[1])
        self.assertEqual(query["moviehash"], ["a" * 32])
        self.assertEqual(query["languages"], ["en"])
        self.assertEqual(query["format"], ["srt"])
        self.assertEqual(seen[0][2]["Api-Key"], "key")
        self.assertIn("User-Agent", seen[0][2])

    def test_search_empty_answer_is_no_match_not_an_error(self) -> None:
        with mock.patch.object(sx, "osdb_http",
                               return_value=(200, b'{"data": []}', "", {})):
            entries, error = self.client.search_by_hash("b" * 32)
        self.assertEqual(entries, [])
        self.assertIsNone(error)

    def test_search_network_failure_is_a_network_error(self) -> None:
        with mock.patch.object(sx, "osdb_http",
                               return_value=(0, b"", "could not reach OpenSubtitles (no route)", {})):
            entries, error = self.client.search_by_hash("c" * 32)
        self.assertEqual(entries, [])
        assert error is not None
        self.assertEqual(error.code, "network")
        self.assertIn("no route", error.message)

    def test_search_rejects_a_bad_api_key(self) -> None:
        with mock.patch.object(sx, "osdb_http", return_value=(401, b"{}", "", {})):
            _entries, error = self.client.search_by_hash("d" * 32)
        assert error is not None
        self.assertEqual(error.code, "auth")
        self.assertIn("rejected the API key", error.message)

    def test_search_stops_on_a_rate_limit(self) -> None:
        with mock.patch.object(sx, "osdb_http", return_value=(429, b"{}", "", {})):
            _entries, error = self.client.search_by_hash("e" * 32)
        assert error is not None
        self.assertEqual(error.code, "rate")
        self.assertIn("rate limit", error.message)

    def test_login_happens_once_for_many_downloads(self) -> None:
        http = FakeOsdbHttp()
        with mock.patch.object(sx, "osdb_http", side_effect=http):
            link1, err1 = self.client.download_link(77)
            link2, err2 = self.client.download_link(78)
        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertTrue(link1 and link2)
        self.assertEqual(http.login_count, 1)

    def test_a_stale_token_gets_one_relogin_then_works(self) -> None:
        """First /download 401s (stale token), the re-login fixes it."""
        http = FakeOsdbHttp()
        downloads_seen = {"n": 0}

        def flaky(url, **kwargs):
            if "/download" in url:
                downloads_seen["n"] += 1
                if downloads_seen["n"] == 1:
                    return 401, b"{}", "", {}
            return http(url, **kwargs)

        with mock.patch.object(sx, "osdb_http", side_effect=flaky):
            link, error = self.client.download_link(77)
        self.assertIsNone(error)
        self.assertTrue(link)
        self.assertEqual(downloads_seen["n"], 2, "one retry after the re-login")
        self.assertEqual(http.login_count, 2)  # initial login + one re-login

    def test_an_account_rejected_twice_is_an_auth_failure(self) -> None:
        http = FakeOsdbHttp()
        http.statuses["/download"] = 401
        with mock.patch.object(sx, "osdb_http", side_effect=http):
            _link, error = self.client.download_link(77)
        assert error is not None
        self.assertEqual(error.code, "auth")

    def test_fetch_rejects_a_file_bigger_than_the_cap(self) -> None:
        big = b"x" * (sx.OSDB_MAX_BODY_BYTES + 1)
        with mock.patch.object(sx, "osdb_http", return_value=(200, big, "", {})):
            data, error = self.client.fetch_subtitle("https://files.example/1.srt")
        self.assertIsNone(data)
        assert error is not None
        self.assertIn("safety limit", error.message)

    def test_fetch_passes_a_normal_file_through(self) -> None:
        with mock.patch.object(sx, "osdb_http", return_value=(200, b"1\\n00:00:01,000", "", {})):
            data, error = self.client.fetch_subtitle("https://files.example/1.srt")
        self.assertEqual(data, b"1\\n00:00:01,000")
        self.assertIsNone(error)


class ChooseDownloadCandidatesTests(unittest.TestCase):
    """Which of the several uploads of one hash gets tried, in what order."""

    def test_non_sdh_beats_sdh_and_more_downloads_beats_fewer(self) -> None:
        entries = [
            {"id": 1, "downloads": 900, "hearing_impaired": True,
             "files": [{"file_id": 11}]},
            {"id": 2, "downloads": 10, "hearing_impaired": False,
             "files": [{"file_id": 22}]},
            {"id": 3, "downloads": 500, "hearing_impaired": False,
             "files": [{"file_id": 33}]},
        ]
        self.assertEqual(sx.choose_download_candidates(entries),
                         [(3, 33), (2, 22), (1, 11)])

    def test_the_last_tie_is_broken_by_id_deterministically(self) -> None:
        entries = [
            {"id": 9, "downloads": 100, "files": [{"file_id": 91}]},
            {"id": 4, "downloads": 100, "files": [{"file_id": 41}]},
        ]
        self.assertEqual(sx.choose_download_candidates(entries), [(4, 41), (9, 91)])

    def test_at_most_three_candidates_are_returned(self) -> None:
        entries = [{"id": i, "downloads": 100, "files": [{"file_id": 100 + i}]}
                   for i in range(7)]
        self.assertEqual(len(sx.choose_download_candidates(entries)),
                         sx.OSDB_MAX_DOWNLOAD_CANDIDATES)

    def test_garbage_entries_are_ignored_not_raised(self) -> None:
        entries = [
            "not-a-dict",
            {"id": 1, "downloads": 100},                      # no files
            {"id": 2, "downloads": 100, "files": "nope"},     # files not a list
            {"id": 3, "downloads": 100, "files": [None, {"no_id": True},
                                                   {"file_id": 31}]},
        ]
        self.assertEqual(sx.choose_download_candidates(entries), [(3, 31)])


class DownloadExactHashTests(unittest.TestCase):
    """One image-only movie through the whole download pipeline."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="osdb_download_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._saved_ledger = os.environ.get(sx.EXTRACTED_LEDGER_ENV)
        os.environ[sx.EXTRACTED_LEDGER_ENV] = str(self.tmp / "extracted.json")
        self.addCleanup(self._restore_ledger_env)
        folder = self.tmp / "library" / "Fake (2021)"
        folder.mkdir(parents=True)
        self.movie = folder / "Fake (2021).mkv"
        self.movie.write_bytes(b"image-only-movie")
        self.dest = self.movie.with_name("Fake (2021).eng.srt")
        self.client = sx.OpenSubtitlesClient(api_key="key", username="user",
                                             password="pass")

    def _restore_ledger_env(self) -> None:
        if self._saved_ledger is None:
            os.environ.pop(sx.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sx.EXTRACTED_LEDGER_ENV] = self._saved_ledger

    def _download(self, client=None, *, dry_run: bool = False, **http_kwargs: object):
        http = FakeOsdbHttp(**http_kwargs)
        with mock.patch.object(sx, "osdb_http", side_effect=http):
            outcome = sx.download_exact_hash_subtitle(
                self.movie, self.dest, client if client is not None else self.client,
                min_cues=2, dry_run=dry_run)
        return outcome, http

    # -- the configuration branches -----------------------------------------

    def test_disabled_is_said_not_tried(self) -> None:
        http = FakeOsdbHttp()
        with mock.patch.object(sx, "osdb_http", side_effect=http):
            outcome = sx.download_exact_hash_subtitle(
                self.movie, self.dest, None, min_cues=2)
        self.assertFalse(outcome.ok)
        self.assertIn("disabled", outcome.unavailable_reason)
        self.assertEqual(http.calls, [], "disabled means no request at all")

    def test_missing_api_key_is_said_not_tried(self) -> None:
        http = FakeOsdbHttp()
        client = sx.OpenSubtitlesClient(api_key="", username="u", password="p")
        with mock.patch.object(sx, "osdb_http", side_effect=http):
            outcome = sx.download_exact_hash_subtitle(self.movie, self.dest, client, min_cues=2)
        self.assertFalse(outcome.ok)
        self.assertIn("API key", outcome.unavailable_reason)
        self.assertEqual(http.calls, [])

    def test_a_live_run_with_incomplete_credentials_is_said(self) -> None:
        http = FakeOsdbHttp()
        client = sx.OpenSubtitlesClient(api_key="key")  # no account
        with mock.patch.object(sx, "osdb_http", side_effect=http):
            outcome = sx.download_exact_hash_subtitle(self.movie, self.dest, client, min_cues=2)
        self.assertFalse(outcome.ok)
        self.assertIn("incomplete", outcome.unavailable_reason)
        self.assertIn("OPENSUBTITLES_USERNAME", outcome.unavailable_reason)
        self.assertEqual(http.calls, [], "it is known up front, so nothing is sent")

    def test_a_dry_run_with_only_a_key_still_searches(self) -> None:
        """A preview can show what a match would look like without an account."""
        http = FakeOsdbHttp()
        client = sx.OpenSubtitlesClient(api_key="key")
        with mock.patch.object(sx, "osdb_http", side_effect=http):
            outcome = sx.download_exact_hash_subtitle(
                self.movie, self.dest, client, min_cues=2, dry_run=True)
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertIn("would download", outcome.detail)
        self.assertEqual(http.url_kinds(), ["search"], "a dry run never downloads")
        self.assertFalse(self.dest.exists())

    # -- the happy path -------------------------------------------------------

    def test_an_exact_match_is_written_and_recorded(self) -> None:
        outcome, http = self._download()
        self.assertTrue(outcome.ok, outcome.detail or outcome.unavailable_reason)
        self.assertTrue(self.dest.is_file())
        written = self.dest.read_text(encoding="utf-8")
        self.assertEqual(written, sx.normalize_extracted_srt(good_download_srt()))
        self.assertEqual(http.url_kinds(), ["search", "login", "download", "file"])
        self.assertEqual(outcome.cue_count, 30)

        record = sx.find_extracted_record(
            self.dest, sx.sha256_text(written))
        self.assertIsNotNone(record, "a downloaded sidecar has provenance too")
        assert record is not None
        self.assertEqual(record["method"], "download")
        self.assertIsNone(record["track_id"], "no embedded track is involved")
        self.assertEqual(record["movie"], str(self.movie))
        self.assertEqual(record["download"]["provider"], "opensubtitles")
        self.assertEqual(record["download"]["movie_hash"],
                         sx.compute_movie_hash(self.movie))
        self.assertEqual(record["download"]["file_id"], 77)

    def test_the_first_candidate_is_preferred(self) -> None:
        """Both files are good; the better-ranked one wins and is the only one fetched."""
        outcome, http = self._download(entries=canned_entries(77, 78))
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertEqual(outcome.file_id, 77)
        self.assertEqual(http.url_kinds(), ["search", "login", "download", "file"])

    # -- the failures that end with no file -----------------------------------

    def test_no_exact_match_is_reported(self) -> None:
        outcome, http = self._download(entries=[])
        self.assertFalse(outcome.ok)
        self.assertIn("no exact-hash English SRT", outcome.detail)
        self.assertEqual(http.url_kinds(), ["search"])
        self.assertFalse(self.dest.exists())

    def test_a_match_that_fails_the_quality_gate_is_not_written(self) -> None:
        bad = "1\n00:00:01,000 --> 00:00:02,000\nJust one lonely cue\n"
        outcome, _http = self._download(srt_text=bad)
        self.assertFalse(outcome.ok)
        self.assertIn("quality gate", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_a_match_that_is_an_error_page_is_not_written(self) -> None:
        outcome, _http = self._download(srt_text="<!DOCTYPE html><html>nope</html>")
        self.assertFalse(outcome.ok)
        self.assertIn("quality gate", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_a_bad_file_moves_to_the_next_candidate(self) -> None:
        """Upload 77 is junk, upload 78 is the real subtitle: the second wins."""
        http = FakeOsdbHttp(entries=canned_entries(77, 78))
        http.bad_file_ids.add(77)
        with mock.patch.object(sx, "osdb_http", side_effect=http):
            outcome = sx.download_exact_hash_subtitle(
                self.movie, self.dest, self.client, min_cues=2)
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertEqual(outcome.file_id, 78)
        self.assertEqual(http.url_kinds(),
                         ["search", "login", "download", "file", "download", "file"])

    def test_every_match_bad_is_a_named_failure(self) -> None:
        http = FakeOsdbHttp(entries=canned_entries(77))
        http.bad_file_ids.add(77)
        with mock.patch.object(sx, "osdb_http", side_effect=http):
            outcome = sx.download_exact_hash_subtitle(
                self.movie, self.dest, self.client, min_cues=2)
        self.assertFalse(outcome.ok)
        self.assertIn("quality gate", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_a_sidecar_that_appears_during_download_is_kept(self) -> None:
        placed = "1\n00:00:01,000 --> 00:00:02,000\nPlaced by somebody else.\n"

        def slow(http, url, **kwargs):
            result = http(url, **kwargs)
            if url.endswith("/download"):
                self.dest.write_text(placed, encoding="utf-8")
            return result

        http = FakeOsdbHttp()
        with mock.patch.object(sx, "osdb_http", side_effect=lambda u, **k: slow(http, u, **k)):
            outcome = sx.download_exact_hash_subtitle(self.movie, self.dest, self.client,
                                                      min_cues=2)
        self.assertTrue(outcome.ok)
        self.assertIn("appeared during download", outcome.detail)
        self.assertEqual(self.dest.read_text(encoding="utf-8"), placed)

    # -- run state: when retrying cannot help ---------------------------------

    def test_a_rejected_credential_stops_the_rest_of_the_run(self) -> None:
        state = sx.OpenSubtitlesRunState()
        bad = FakeOsdbHttp()
        bad.statuses["/subtitles"] = 401
        good = FakeOsdbHttp()
        with mock.patch.object(sx, "osdb_http", side_effect=bad):
            first = sx.download_exact_hash_subtitle(self.movie, self.dest, self.client,
                                                    state=state, min_cues=2)
        self.assertFalse(first.ok)
        self.assertIn("rejected the API key", first.detail)
        self.assertTrue(state.stopped, "the run must not ask 899 more times")
        with mock.patch.object(sx, "osdb_http", side_effect=good):
            second = sx.download_exact_hash_subtitle(self.movie, self.dest, self.client,
                                                     state=state, min_cues=2)
        self.assertFalse(second.ok)
        self.assertEqual(second.unavailable_reason, state.stopped)
        self.assertEqual(good.calls, [], "a stopped run makes no requests")

    def test_repeated_network_failures_stop_the_rest_of_the_run(self) -> None:
        state = sx.OpenSubtitlesRunState()

        def dead(*_args, **_kwargs):
            return 0, b"", "could not reach OpenSubtitles (no route to host)", {}

        with mock.patch.object(sx, "osdb_http", side_effect=dead):
            for _attempt in range(sx.OSDB_MAX_CONSECUTIVE_NETWORK_FAILURES):
                sx.download_exact_hash_subtitle(self.movie, self.dest, self.client,
                                                state=state, min_cues=2)
        self.assertTrue(state.stopped, "three dead networks in a row is a dead network")
        self.assertIn("unreachable", state.stopped)
        with mock.patch.object(sx, "osdb_http", side_effect=FakeOsdbHttp()) as fresh:
            after = sx.download_exact_hash_subtitle(self.movie, self.dest, self.client,
                                                    state=state, min_cues=2)
        self.assertFalse(after.ok)
        self.assertEqual(fresh.call_count, 0, "a stopped run makes no requests")

    def test_one_failed_file_does_not_stop_the_run(self) -> None:
        """A per-movie failure is not a service verdict: the next movie still tries."""
        state = sx.OpenSubtitlesRunState()
        bad = FakeOsdbHttp(entries=[])
        with mock.patch.object(sx, "osdb_http", side_effect=bad):
            first = sx.download_exact_hash_subtitle(self.movie, self.dest, self.client,
                                                    state=state, min_cues=2)
        self.assertFalse(first.ok)
        self.assertFalse(state.stopped)
        with mock.patch.object(sx, "osdb_http", side_effect=FakeOsdbHttp()) as good:
            second = sx.download_exact_hash_subtitle(self.movie, self.dest, self.client,
                                                     state=state, min_cues=2)
        self.assertTrue(second.ok, second.detail)


if __name__ == "__main__":
    unittest.main()
