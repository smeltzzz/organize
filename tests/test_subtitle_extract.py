"""Tests for embedded-subtitle extraction in ``subtitle_fetcher.py``.

The whole suite is offline: ``subprocess.run`` is replaced with a fake that
serves a canned ``mkvmerge -J`` payload and writes a canned subtitle track, so
no MKVToolNix, no Tesseract, no API key, and no media file is needed.

The properties pinned here are the ones that decide whether a sidecar is
trustworthy: a movie's own track must only win when it is complete English
(never a forced/signs-only stream, never OCR noise), and a sidecar built that
way must never be handed to ffsubsync, because it is already frame-accurate.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import subtitle_fetcher as sf
import sync_subtitles as ss



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
        converted = sf.ass_to_srt(ASS_TRACK)
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
        self.assertIn("SSA cue", sf.ass_to_srt(ssa))

    def test_webvtt_conversion(self) -> None:
        vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello VTT\n\n"
               "00:00:04.000 --> 00:00:05.500 align:start\nSecond VTT\n")
        converted = sf.vtt_to_srt(vtt)
        self.assertIn("00:00:01,000 --> 00:00:03,000", converted)
        self.assertIn("Hello VTT", converted)
        self.assertIn("Second VTT", converted)

    def test_normalization_renumbers_and_drops_cr(self) -> None:
        messy = ("5\r\n00:00:01,000 --> 00:00:02,000\r\nfirst\r\n\r\n"
                 "9\r\n00:00:03,000 --> 00:00:04,000\r\nsecond\r\n")
        fixed = sf.normalize_extracted_srt(messy)
        self.assertTrue(fixed.startswith("1\n00:00:01,000 --> 00:00:02,000\nfirst\n\n2\n"))
        self.assertNotIn("\r", fixed)

    def test_empty_ass_yields_no_cues(self) -> None:
        self.assertEqual(sf.ass_to_srt("[Script Info]\n"), "")


class QualityGateTests(unittest.TestCase):
    def _cues(self, body: str, count: int = 30) -> str:
        return sf.render_srt_cues([
            (f"00:00:{index % 60:02d},000", f"00:00:{index % 60:02d},900", body)
            for index in range(count)
        ])

    def test_complete_english_track_passes(self) -> None:
        ok, reason = sf.extracted_subtitle_quality(
            self._cues("This is a line of English dialogue"))
        self.assertTrue(ok, reason)

    def test_signs_only_track_is_refused(self) -> None:
        ok, reason = sf.extracted_subtitle_quality(
            sf.render_srt_cues([("00:00:01,000", "00:00:02,000", "Only line")]))
        self.assertFalse(ok)
        self.assertIn("signs/songs-only", reason)

    def test_cyrillic_track_is_refused(self) -> None:
        ok, reason = sf.extracted_subtitle_quality(
            self._cues("Это предложение на русском языке"))
        self.assertFalse(ok)
        self.assertIn("not Latin-script", reason)

    def test_ocr_noise_is_refused(self) -> None:
        ok, reason = sf.extracted_subtitle_quality(
            self._cues("||| ~~~ ### ||| ~~~"), method="ocr")
        self.assertFalse(ok)
        self.assertIn("noise", reason)

    def test_word_salad_is_refused(self) -> None:
        ok, reason = sf.extracted_subtitle_quality(
            self._cues("Qwx zp vfg blrt mnk jklqwerty"))
        self.assertFalse(ok)
        self.assertIn("does not read as English", reason)

    def test_empty_text_is_refused(self) -> None:
        ok, _reason = sf.extracted_subtitle_quality("")
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
        picked = sf.classify_embedded_subtitle_tracks(tracks)
        self.assertEqual([item.track_id for item in picked], [3, 2, 8])
        self.assertTrue(picked[0].sdh)
        self.assertEqual(picked[0].kind, "text")
        self.assertEqual(picked[1].kind, "image")

    def test_forced_name_is_excluded_without_a_flag(self) -> None:
        tracks = [self._track(2, "S_TEXT/UTF8", language="eng", track_name="English (Forced)")]
        self.assertEqual(sf.classify_embedded_subtitle_tracks(tracks), [])

    def test_untagged_english_name_counts_as_english(self) -> None:
        tracks = [self._track(2, "S_TEXT/UTF8", language="und", track_name="English")]
        self.assertEqual(len(sf.classify_embedded_subtitle_tracks(tracks)), 1)

    def test_unsupported_codec_is_skipped(self) -> None:
        tracks = [self._track(2, "S_TEXT/X_UNKNOWN", language="eng")]
        self.assertEqual(sf.classify_embedded_subtitle_tracks(tracks), [])

    def test_no_english_track_at_all(self) -> None:
        tracks = [self._track(2, "S_TEXT/UTF8", language="spa", track_name="Spanish")]
        self.assertEqual(sf.classify_embedded_subtitle_tracks(tracks), [])


class OcrBackendTests(unittest.TestCase):
    def test_sup2srt_command(self) -> None:
        backend = sf.OcrBackend(sf.OCR_BACKEND_SUP2SRT, "sup2srt + Tesseract", ("sup2srt",),
                                frozenset({"PGS"}))
        self.assertEqual(
            backend.build_command(Path("/tmp/3.sup"), Path("/tmp/3.srt"), track_id=3, language="eng"),
            ["sup2srt", "-l", "eng", "-o",
             str(Path("/tmp/3.srt")), str(Path("/tmp/3.sup"))],
        )

    def test_pgsrip_command_and_language(self) -> None:
        backend = sf.OcrBackend(sf.OCR_BACKEND_PGSRIP, "pgsrip + Tesseract", ("pgsrip",),
                                frozenset({"PGS"}), output_mode="sibling")
        # pgsrip filters by language itself and writes beside the input.
        self.assertEqual(
            backend.build_command(Path("/tmp/4.sup"), Path("/tmp/4.srt"),
                                  track_id=4, language="eng"),
            ["pgsrip", "-l", "en", str(Path("/tmp/4.sup"))],
        )
        self.assertEqual(backend.result_path(Path("/tmp/4.sup"), Path("/tmp/4.srt")),
                         Path("/tmp/4.srt"))

    def test_pgsrip_is_tried_first_when_auto_detecting(self) -> None:
        self.assertEqual(sf.OCR_BACKEND_AUTO_ORDER[0], sf.OCR_BACKEND_PGSRIP)
        self.assertIn(sf.OCR_BACKEND_PGSRIP, sf.OCR_BACKEND_CHOICES)

    def test_backend_that_renames_its_output_is_still_found(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            source = tmp / "track4.sup"
            source.write_bytes(b"pgs")
            expected = tmp / "track4.srt"
            self.assertIsNone(sf.find_sibling_srt(source, expected))
            renamed = tmp / "track4.eng.srt"
            renamed.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
            self.assertEqual(sf.find_sibling_srt(source, expected), renamed)
            expected.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
            self.assertEqual(sf.find_sibling_srt(source, expected), expected,
                             "the documented name always wins")

    def test_subtitleedit_writes_beside_its_input(self) -> None:
        backend = sf.OcrBackend(sf.OCR_BACKEND_SUBTITLEEDIT, "Subtitle Edit", ("SubtitleEdit",),
                                frozenset({"PGS", "VOBSUB"}), output_mode="sibling")
        argv = backend.build_command(Path("/tmp/3.sup"), Path("/tmp/out.srt"),
                                     track_id=3, language="eng")
        self.assertEqual(argv[:4],
                         ["SubtitleEdit", "/convert", str(Path("/tmp/3.sup")), "srt"])
        self.assertEqual(backend.result_path(Path("/tmp/3.sup"), Path("/tmp/out.srt")),
                         Path("/tmp/3.srt"))

    def test_custom_template_expands_placeholders(self) -> None:
        backend = sf.OcrBackend(sf.OCR_BACKEND_CUSTOM, "custom", ("/opt/ocr.sh",),
                                frozenset({"PGS"}), arg_template=("{input}", "{output}"))
        self.assertEqual(
            backend.build_command(Path("/tmp/3.sup"), Path("/tmp/o.srt"), track_id=3, language="en"),
            ["/opt/ocr.sh", str(Path("/tmp/3.sup")), str(Path("/tmp/o.srt"))],
        )

    def test_backend_refuses_tracks_it_cannot_read(self) -> None:
        backend = sf.OcrBackend(sf.OCR_BACKEND_SUP2SRT, "sup2srt", ("sup2srt",), frozenset({"PGS"}))
        vobsub = sf.EmbeddedSubtitleTrack(2, "S_VOBSUB", "eng", "", "image", ".idx")
        pgs = sf.EmbeddedSubtitleTrack(3, "S_HDMV/PGS", "eng", "", "image", ".sup")
        self.assertFalse(backend.supports_track(vobsub))
        self.assertTrue(backend.supports_track(pgs))

    def test_none_backend_is_reported_not_fatal(self) -> None:
        backend, note = sf.detect_ocr_backend(sf.OCR_BACKEND_NONE)
        self.assertIsNone(backend)
        self.assertIn("disabled", note)

    def test_custom_backend_without_a_binary_explains_itself(self) -> None:
        backend, note = sf.detect_ocr_backend(sf.OCR_BACKEND_CUSTOM, explicit_bin="",
                                               arg_template="{input} {output}")
        self.assertIsNone(backend)
        self.assertIn("--ocr-bin", note)


class ExtractionRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="extract_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._saved_ledger = os.environ.get(sf.EXTRACTED_LEDGER_ENV)
        os.environ[sf.EXTRACTED_LEDGER_ENV] = str(self.tmp / "extracted.json")
        self.addCleanup(self._restore_ledger_env)
        library = self.tmp / "library"
        movie_dir = library / "Fake (2021)"
        movie_dir.mkdir(parents=True)
        self.movie = movie_dir / "Fake (2021).mkv"
        self.movie.write_bytes(b"mkv-bytes")
        self.dest = self.movie.with_name("Fake (2021).eng.srt")

    def _restore_ledger_env(self) -> None:
        if self._saved_ledger is None:
            os.environ.pop(sf.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sf.EXTRACTED_LEDGER_ENV] = self._saved_ledger

    def _run(self, tracks: dict, **options: object) -> sf.ExtractionOutcome:
        runner = FakeRunner(tracks)
        opts = sf.ExtractOptions(min_cues=2, **options)  # type: ignore[arg-type]
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sf, "find_mkvtoolnix_binary", fake_binaries):
            return sf.extract_embedded_english_srt(self.movie, self.dest, opts)

    def test_text_track_is_extracted_and_recorded(self) -> None:
        outcome = self._run(TEXT_TRACKS)
        self.assertTrue(outcome.ok, outcome.detail or outcome.unavailable_reason)
        self.assertEqual(outcome.method, "text")
        self.assertEqual(outcome.cue_count, 2)
        self.assertTrue(self.dest.is_file())
        self.assertEqual(self.dest.read_text(encoding="utf-8"), sf.ass_to_srt(ASS_TRACK))
        record = sf.find_extracted_record(self.dest, sf.sha256_text(sf.ass_to_srt(ASS_TRACK)))
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["track_id"], 2)
        self.assertEqual(record["method"], "text")

    def test_dry_run_writes_nothing(self) -> None:
        outcome = self._run(TEXT_TRACKS, dry_run=True)
        self.assertTrue(outcome.ok)
        self.assertFalse(self.dest.exists(), "a preview must not create a sidecar")

    def test_dry_run_does_not_spend_an_ocr_run(self) -> None:
        runner = FakeRunner(PGS_TRACKS)
        backend = sf.OcrBackend(sf.OCR_BACKEND_SUP2SRT, "sup2srt + Tesseract", ("sup2srt",),
                                frozenset({"PGS"}))
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sf, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sf, "detect_ocr_backend", return_value=(backend, "")):
            outcome = sf.extract_embedded_english_srt(
                self.movie, self.dest, sf.ExtractOptions(min_cues=2, dry_run=True))
        self.assertTrue(outcome.ok)
        self.assertFalse(any("tracks" in call for call in runner.calls),
                         "a dry run must not run mkvextract/OCR on an image track")
        self.assertFalse(self.dest.exists())

    def test_image_only_movie_without_ocr_falls_through(self) -> None:
        outcome = self._run(PGS_TRACKS, ocr_backend=sf.OCR_BACKEND_NONE)
        self.assertFalse(outcome.ok)
        self.assertFalse(self.dest.exists())
        self.assertIn("OCR is disabled", outcome.unavailable_reason or outcome.detail)

    def test_movie_without_an_english_track_falls_through(self) -> None:
        outcome = self._run({"tracks": [
            {"id": 1, "type": "audio", "properties": {"codec_id": "A_AC3", "language": "eng"}}]})
        self.assertFalse(outcome.ok)
        self.assertIn("no English subtitle track", outcome.unavailable_reason)

    def test_missing_mkvtoolnix_names_the_install(self) -> None:
        with mock.patch.object(sf, "find_mkvtoolnix_binary", lambda *_args, **_kwargs: None):
            outcome = sf.extract_embedded_english_srt(
                self.movie, self.dest, sf.ExtractOptions(min_cues=2))
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
                mock.patch.object(sf, "find_mkvtoolnix_binary", fake_binaries):
            outcome = sf.extract_embedded_english_srt(
                self.movie, self.dest, sf.ExtractOptions(min_cues=10))
        self.assertFalse(outcome.ok)
        self.assertIn("signs/songs-only", outcome.detail)
        self.assertFalse(self.dest.exists())


class RunIntegrationTests(unittest.TestCase):
    """Extraction happens inside queue_run, before every provider tier."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="queue_extract_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._saved_ledger = os.environ.get(sf.EXTRACTED_LEDGER_ENV)
        os.environ[sf.EXTRACTED_LEDGER_ENV] = str(self.tmp / "extracted.json")
        self.addCleanup(self._restore_env)
        self.library = self.tmp / "library"
        movie_dir = self.library / "Fake (2021)"
        movie_dir.mkdir(parents=True)
        self.movie = movie_dir / "Fake (2021).mkv"
        self.movie.write_bytes(b"mkv-bytes")

    def _restore_env(self) -> None:
        if self._saved_ledger is None:
            os.environ.pop(sf.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sf.EXTRACTED_LEDGER_ENV] = self._saved_ledger

    def _config(self, **overrides: object) -> sf.QueueConfig:
        base: dict[str, object] = {
            "library": self.library,
            "log_file": self.tmp / "fetcher.log",
            "report_file": self.tmp / "fetcher_report.txt",
            "scrape_daily_cap": 0,  # no network, no scraping tier
            "extract_min_cues": 2,
            "daily_cap": 200,
            "min_movie_size_mb": 0,  # the fixtures are a few bytes, not 300 MB
        }
        base.update(overrides)
        return sf.QueueConfig(**base)  # type: ignore[arg-type]

    def test_an_extracted_movie_is_covered_without_any_provider(self) -> None:
        runner = FakeRunner(TEXT_TRACKS)
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sf, "find_mkvtoolnix_binary", fake_binaries):
            results, summary = sf.queue_run(self._config())
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.reason, sf.REASON_EXTRACTED)
        self.assertEqual(result.status, "extracted")
        self.assertTrue(result.dest is not None and result.dest.is_file())
        self.assertEqual(int(summary["extracted_from_embedded"]), 1)
        self.assertEqual(int(summary["coverage_covered"]), 1, "extraction counts as coverage")
        self.assertEqual(int(summary["coverage_total"]), 1)

    def test_no_extract_leaves_the_movie_to_the_sources(self) -> None:
        runner = FakeRunner(TEXT_TRACKS)
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sf, "find_mkvtoolnix_binary", fake_binaries):
            results, summary = sf.queue_run(self._config(extract_embedded=False))
        self.assertTrue(all(result.reason != sf.REASON_EXTRACTED for result in results))
        self.assertFalse(runner.calls, "with --no-extract the movie is never probed")
        # With no provider configured the only cover for this movie is its own
        # track, so turning extraction off leaves it waiting for a source.
        self.assertIn(str(self.movie),
                      [str(item) for item in summary.get("deferred_videos") or []])

    def test_report_names_what_was_extracted(self) -> None:
        runner = FakeRunner(TEXT_TRACKS)
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sf, "find_mkvtoolnix_binary", fake_binaries):
            results, summary = sf.queue_run(self._config())
        text = sf.build_report(results, self._config(), summary)
        self.assertIn("EXTRACTED FROM THE MOVIE'S OWN EMBEDDED TRACK", text)
        self.assertIn("Extracted from the movie", text)


class SyncSkipTests(unittest.TestCase):
    """A sidecar built from the movie's own track must not be re-synced."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sync_extract_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._saved_ledger = os.environ.get(sf.EXTRACTED_LEDGER_ENV)
        os.environ[sf.EXTRACTED_LEDGER_ENV] = str(self.tmp / "extracted.json")
        self.addCleanup(self._restore_env)
        folder = self.tmp / "library" / "Fake (2021)"
        folder.mkdir(parents=True)
        self.video = folder / "Fake (2021).mkv"
        self.video.write_bytes(b"mkv-bytes")
        self.srt = folder / "Fake (2021).eng.srt"
        self.body = sf.render_srt_cues([
            (f"00:00:{index:02d},000", f"00:00:{index:02d},900", "A line of dialogue")
            for index in range(1, 12)
        ])
        # Bytes, and LF on purpose: the fetcher writes sidecars with
        # newline="\n" and records the hash of those exact bytes, so a text-mode
        # write (CRLF on Windows) would break the match for the wrong reason.
        self.srt.write_bytes(self.body.encode("utf-8"))

    def _restore_env(self) -> None:
        if self._saved_ledger is None:
            os.environ.pop(sf.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sf.EXTRACTED_LEDGER_ENV] = self._saved_ledger

    def _sync_one(self) -> ss.SyncResult:
        cfg = ss.Config(library=self.tmp / "library", log_file=self.tmp / "sync.log",
                        report_file=self.tmp / "sync_report.txt")
        job = ss.Job(srt=self.srt, video=self.video)
        features = ss.FfsubsyncFeatures()
        return ss.sync_one(job, cfg, "ffsubsync", features)

    def test_extracted_sidecar_is_not_synced(self) -> None:
        track = sf.EmbeddedSubtitleTrack(2, "S_TEXT/ASS", "eng", "English", "text", ".ass")
        self.assertTrue(sf.record_extracted_sidecar(
            self.video, self.srt, track=track, method="text", cue_count=11,
            sha256=sf.sha256_text(self.body)))
        with mock.patch.object(ss, "run_ffsubsync", side_effect=AssertionError("ffsubsync ran")):
            result = self._sync_one()
        self.assertEqual(result.status, ss.STATUS_EXTRACTED)
        self.assertIn("frame-accurate", result.detail)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), self.body, "file untouched")

    def test_a_replaced_sidecar_is_synced_normally(self) -> None:
        track = sf.EmbeddedSubtitleTrack(2, "S_TEXT/ASS", "eng", "English", "text", ".ass")
        sf.record_extracted_sidecar(self.video, self.srt, track=track, method="text",
                                    cue_count=11, sha256=sf.sha256_text(self.body))
        # The sidecar was replaced by a download: it is no longer the extracted copy.
        self.srt.write_bytes((self.body + "\n").encode("utf-8"))
        with mock.patch.object(ss, "run_ffsubsync", return_value=(1, "", "boom")) as run:
            result = self._sync_one()
        self.assertNotEqual(result.status, ss.STATUS_EXTRACTED)
        self.assertTrue(run.called, "a replaced sidecar is measured like any other")

    def test_report_counts_extracted_sidecars(self) -> None:
        track = sf.EmbeddedSubtitleTrack(2, "S_TEXT/ASS", "eng", "English", "text", ".ass")
        sf.record_extracted_sidecar(self.video, self.srt, track=track, method="text",
                                    cue_count=11, sha256=sf.sha256_text(self.body))
        with mock.patch.object(ss, "run_ffsubsync", side_effect=AssertionError("ffsubsync ran")):
            result = self._sync_one()
        cfg = ss.Config(library=self.tmp / "library", log_file=self.tmp / "sync.log",
                        report_file=self.tmp / "sync_report.txt")
        text = ss.build_report([result], cfg, video_count=1, ffsubsync_info="ffsubsync 1.0",
                               features=ss.FfsubsyncFeatures(), elapsed_sec=0.1, truncated=False)
        self.assertIn("EXTRACTED FROM THE MOVIE (SYNC NOT NEEDED)", text)
        self.assertIn("Extracted (not synced)", text)


if __name__ == "__main__":
    unittest.main()
