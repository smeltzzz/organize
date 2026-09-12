"""Tests for embedded-subtitle extraction in ``subtitle_extractor.py``.

The whole suite is offline: ``subprocess.run`` is replaced with a fake that
serves a canned ``mkvmerge -J`` payload and writes a canned subtitle track, so
no MKVToolNix, no Tesseract, and no media file is needed.

The properties pinned here are the ones that decide whether a sidecar is
trustworthy: a movie's own track must only win when it is complete English
(never a forced/signs-only stream, never OCR noise), and a sidecar built that
way is recorded in the provenance ledger - while a sidecar that was already
there before this tool ran is never touched at all.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import unittest
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
        ok, reason = sx.extracted_subtitle_quality(
            self._cues("This is a line of English dialogue"))
        self.assertTrue(ok, reason)

    def test_signs_only_track_is_refused(self) -> None:
        ok, reason = sx.extracted_subtitle_quality(
            sx.render_srt_cues([("00:00:01,000", "00:00:02,000", "Only line")]))
        self.assertFalse(ok)
        self.assertIn("signs/songs-only", reason)

    def test_cyrillic_track_is_refused(self) -> None:
        ok, reason = sx.extracted_subtitle_quality(
            self._cues("Это предложение на русском языке"))
        self.assertFalse(ok)
        self.assertIn("not Latin-script", reason)

    def test_ocr_noise_is_refused(self) -> None:
        ok, reason = sx.extracted_subtitle_quality(
            self._cues("||| ~~~ ### ||| ~~~"), method="ocr")
        self.assertFalse(ok)
        self.assertIn("noise", reason)

    def test_word_salad_is_refused(self) -> None:
        ok, reason = sx.extracted_subtitle_quality(
            self._cues("Qwx zp vfg blrt mnk jklqwerty"))
        self.assertFalse(ok)
        self.assertIn("does not read as English", reason)

    def test_empty_text_is_refused(self) -> None:
        ok, _reason = sx.extracted_subtitle_quality("")
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


class OcrBackendTests(unittest.TestCase):
    def test_sup2srt_command(self) -> None:
        backend = sx.OcrBackend(sx.OCR_BACKEND_SUP2SRT, "sup2srt + Tesseract", ("sup2srt",),
                                frozenset({"PGS"}))
        self.assertEqual(
            backend.build_command(Path("/tmp/3.sup"), Path("/tmp/3.srt"), track_id=3, language="eng"),
            ["sup2srt", "-l", "eng", "-o",
             str(Path("/tmp/3.srt")), str(Path("/tmp/3.sup"))],
        )

    def test_pgsrip_command_and_language(self) -> None:
        backend = sx.OcrBackend(sx.OCR_BACKEND_PGSRIP, "pgsrip + Tesseract", ("pgsrip",),
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
        self.assertEqual(sx.OCR_BACKEND_AUTO_ORDER[0], sx.OCR_BACKEND_PGSRIP)
        self.assertIn(sx.OCR_BACKEND_PGSRIP, sx.OCR_BACKEND_CHOICES)

    def test_backend_that_renames_its_output_is_still_found(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            source = tmp / "track4.sup"
            source.write_bytes(b"pgs")
            expected = tmp / "track4.srt"
            self.assertIsNone(sx.find_sibling_srt(source, expected))
            renamed = tmp / "track4.eng.srt"
            renamed.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
            self.assertEqual(sx.find_sibling_srt(source, expected), renamed)
            expected.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
            self.assertEqual(sx.find_sibling_srt(source, expected), expected,
                             "the documented name always wins")

    def test_subtitleedit_writes_beside_its_input(self) -> None:
        backend = sx.OcrBackend(sx.OCR_BACKEND_SUBTITLEEDIT, "Subtitle Edit", ("SubtitleEdit",),
                                frozenset({"PGS", "VOBSUB"}), output_mode="sibling")
        argv = backend.build_command(Path("/tmp/3.sup"), Path("/tmp/out.srt"),
                                     track_id=3, language="eng")
        self.assertEqual(argv[:4],
                         ["SubtitleEdit", "/convert", str(Path("/tmp/3.sup")), "srt"])
        self.assertEqual(backend.result_path(Path("/tmp/3.sup"), Path("/tmp/out.srt")),
                         Path("/tmp/3.srt"))

    def test_custom_template_expands_placeholders(self) -> None:
        backend = sx.OcrBackend(sx.OCR_BACKEND_CUSTOM, "custom", ("/opt/ocr.sh",),
                                frozenset({"PGS"}), arg_template=("{input}", "{output}"))
        self.assertEqual(
            backend.build_command(Path("/tmp/3.sup"), Path("/tmp/o.srt"), track_id=3, language="en"),
            ["/opt/ocr.sh", str(Path("/tmp/3.sup")), str(Path("/tmp/o.srt"))],
        )

    def test_backend_refuses_tracks_it_cannot_read(self) -> None:
        backend = sx.OcrBackend(sx.OCR_BACKEND_SUP2SRT, "sup2srt", ("sup2srt",), frozenset({"PGS"}))
        vobsub = sx.EmbeddedSubtitleTrack(2, "S_VOBSUB", "eng", "", "image", ".idx")
        pgs = sx.EmbeddedSubtitleTrack(3, "S_HDMV/PGS", "eng", "", "image", ".sup")
        self.assertFalse(backend.supports_track(vobsub))
        self.assertTrue(backend.supports_track(pgs))

    def test_none_backend_is_reported_not_fatal(self) -> None:
        backend, note = sx.detect_ocr_backend(sx.OCR_BACKEND_NONE)
        self.assertIsNone(backend)
        self.assertIn("disabled", note)

    def test_custom_backend_without_a_binary_explains_itself(self) -> None:
        backend, note = sx.detect_ocr_backend(sx.OCR_BACKEND_CUSTOM, explicit_bin="",
                                               arg_template="{input} {output}")
        self.assertIsNone(backend)
        self.assertIn("--ocr-bin", note)


class ChoosingAnOcrBackendTests(unittest.TestCase):
    """Which program will OCR an image track, and what it says when none will.

    Four backends, four ways of being installed (a path handed in, a name on
    PATH, a known install location, a Windows .exe under mono) and a custom
    command an operator supplies themselves. None of it is fatal — an
    image-only movie is simply reported as needing attention — so what matters
    as much as the choice is that the note explains the fix.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="ocr_backend_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def program(self, name: str = "ocr-tool") -> Path:
        path = self.tmp / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        return path

    def nothing_on_path(self) -> None:
        patcher = mock.patch.object(sx.shutil, "which", lambda _name: None)
        patcher.start()
        self.addCleanup(patcher.stop)

        def _resolve_without_known(explicit: str, name: str, *search_paths: str) -> str | None:
            if explicit:
                pp = Path(explicit)
                if pp.is_file():
                    return str(pp)
                found = sx.shutil.which(explicit)
                if found:
                    return found
            found = sx.shutil.which(name)
            if found:
                return found
            return None

        patcher2 = mock.patch.object(sx, "_resolve_program", side_effect=_resolve_without_known)
        patcher2.start()
        self.addCleanup(patcher2.stop)
        patcher3 = mock.patch.object(sx, "_subtitleedit_program", lambda explicit="": None)
        patcher3.start()
        self.addCleanup(patcher3.stop)
        patcher4 = mock.patch.object(sx, "_pgstosrt_program", lambda explicit="": None)
        patcher4.start()
        self.addCleanup(patcher4.stop)

    def on_path(self, **programs: str) -> None:
        patcher = mock.patch.object(sx.shutil, "which", lambda name: programs.get(name))
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- how a program is found --------------------------------------------

    def test_a_path_handed_in_is_used_as_it_is(self) -> None:
        self.nothing_on_path()
        program = self.program("sup2srt")
        backend = sx.build_ocr_backend(sx.OCR_BACKEND_SUP2SRT, str(program))
        self.assertIsNotNone(backend)
        assert backend is not None
        self.assertEqual(backend.program, (str(program),))

    def test_a_name_handed_in_is_looked_up_on_the_path(self) -> None:
        self.on_path(**{"my-sup2srt": "/usr/local/bin/my-sup2srt"})
        backend = sx.build_ocr_backend(sx.OCR_BACKEND_SUP2SRT, "my-sup2srt")
        self.assertIsNotNone(backend)
        assert backend is not None
        self.assertEqual(backend.program, ("/usr/local/bin/my-sup2srt",))

    def test_a_known_install_location_is_tried_after_the_path(self) -> None:
        """Subtitle Edit and PgsToSrt are not usually on PATH at all."""
        patcher = mock.patch.object(sx.shutil, "which", lambda _name: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        installed = self.program("SubtitleEdit.exe")
        # _resolve_program is the shared lookup behind every backend; the
        # known-location list is what makes a GUI install work unattended.
        self.assertEqual(sx._resolve_program("", "SubtitleEdit", str(installed)),
                         str(installed))
        self.assertIsNone(sx._resolve_program("", "SubtitleEdit", str(self.tmp / "absent")))

    @unittest.skipIf(os.name == "nt", "the mono wrapper is the non-Windows branch")
    def test_a_windows_subtitle_edit_is_run_through_mono(self) -> None:
        """A .NET .exe is not directly runnable off Windows.

        This is where the mono wrapper used to be unreachable: the lookup
        knows Subtitle Edit's Linux install locations, so it always resolved
        the .exe first and the wrapper below it never ran.
        """
        self.on_path(mono="/usr/bin/mono")
        exe = self.program("SubtitleEdit.exe")
        backend = sx.build_ocr_backend(sx.OCR_BACKEND_SUBTITLEEDIT, str(exe))
        self.assertIsNotNone(backend)
        assert backend is not None
        self.assertEqual(backend.program, ("/usr/bin/mono", str(exe)))
        self.assertIn("VOBSUB", backend.supports)

    @unittest.skipIf(os.name == "nt", "the mono wrapper is the non-Windows branch")
    def test_subtitle_edit_without_mono_counts_as_not_installed(self) -> None:
        self.nothing_on_path()
        exe = self.program("SubtitleEdit.exe")
        self.assertIsNone(sx.build_ocr_backend(sx.OCR_BACKEND_SUBTITLEEDIT, str(exe)))

    def test_a_native_subtitle_edit_is_run_directly(self) -> None:
        self.on_path(SubtitleEdit="/usr/bin/SubtitleEdit", mono="/usr/bin/mono")
        backend = sx.build_ocr_backend(sx.OCR_BACKEND_SUBTITLEEDIT)
        assert backend is not None
        self.assertEqual(backend.program, ("/usr/bin/SubtitleEdit",))

    def test_pgsrip_and_pgstosrt_are_built_when_present(self) -> None:
        self.on_path(pgsrip="/usr/bin/pgsrip")
        pgsrip = sx.build_ocr_backend(sx.OCR_BACKEND_PGSRIP)
        self.assertIsNotNone(pgsrip)
        assert pgsrip is not None
        self.assertEqual(pgsrip.output_mode, "sibling", "pgsrip writes beside its input")

        dll = self.program("PgsToSrt.dll")
        self.on_path(dotnet="/usr/bin/dotnet")
        pgstosrt = sx.build_ocr_backend(sx.OCR_BACKEND_PGSTOSRT, str(dll))
        self.assertIsNotNone(pgstosrt)
        assert pgstosrt is not None
        self.assertIn(str(dll), pgstosrt.program)

    def test_a_backend_this_tool_does_not_know_is_not_built(self) -> None:
        self.assertIsNone(sx.build_ocr_backend("hand-typed"))

    # -- what detect_ocr_backend decides and says --------------------------

    def test_auto_takes_the_first_backend_it_finds(self) -> None:
        self.on_path(sup2srt="/usr/bin/sup2srt")
        backend, note = sx.detect_ocr_backend(sx.OCR_BACKEND_AUTO)
        self.assertIsNotNone(backend)
        assert backend is not None
        self.assertEqual(backend.key, sx.OCR_BACKEND_SUP2SRT)
        self.assertEqual(note, "")

    def test_auto_with_nothing_installed_names_the_install(self) -> None:
        self.nothing_on_path()
        backend, note = sx.detect_ocr_backend(sx.OCR_BACKEND_AUTO)
        self.assertIsNone(backend)
        self.assertIn("no image-subtitle OCR backend found", note)
        self.assertIn("pip install pgsrip", note)

    def test_a_named_backend_is_the_only_one_tried(self) -> None:
        self.on_path(sup2srt="/usr/bin/sup2srt")
        backend, note = sx.detect_ocr_backend(sx.OCR_BACKEND_PGSRIP)
        self.assertIsNone(backend, "asking for pgsrip must not silently use sup2srt")
        self.assertIn("--ocr-backend pgsrip was not found", note)

    def test_a_named_backend_that_is_installed_is_used(self) -> None:
        self.on_path(sup2srt="/usr/bin/sup2srt")
        backend, note = sx.detect_ocr_backend(sx.OCR_BACKEND_SUP2SRT)
        self.assertIsNotNone(backend)
        self.assertEqual(note, "")

    def test_a_backend_name_that_does_not_exist_is_refused(self) -> None:
        backend, note = sx.detect_ocr_backend("tesseract-by-hand")
        self.assertIsNone(backend)
        self.assertIn("unknown --ocr-backend", note)

    # -- the custom command an operator supplies ---------------------------

    def test_a_custom_command_needs_both_placeholders(self) -> None:
        """Naming only the input was accepted until this test was written.

        The message always said both were required, and it has to be: without
        {output} the tool cannot know where the OCR result landed, so every
        image track would fail minutes after the command was accepted.
        """
        program = self.program()
        for template in ("--in {input}", "--out {output}", "--quiet"):
            with self.subTest(template=template):
                backend, note = sx.detect_ocr_backend(
                    sx.OCR_BACKEND_CUSTOM, explicit_bin=str(program),
                    arg_template=template)
                self.assertIsNone(backend)
                self.assertIn("{input} and {output}", note)

    def test_a_custom_command_that_cannot_be_parsed_says_so(self) -> None:
        program = self.program()
        backend, note = sx.detect_ocr_backend(
            sx.OCR_BACKEND_CUSTOM, explicit_bin=str(program),
            arg_template='--in "{input} --out {output}')
        self.assertIsNone(backend)
        self.assertIn("--ocr-args could not be parsed", note)

    def test_a_valid_custom_command_becomes_the_backend(self) -> None:
        program = self.program()
        backend, note = sx.detect_ocr_backend(
            sx.OCR_BACKEND_CUSTOM, explicit_bin=str(program),
            arg_template="--in {input} --out {output}")
        self.assertEqual(note, "")
        assert backend is not None
        self.assertEqual(
            backend.build_command(Path("/tmp/3.sup"), Path("/tmp/3.srt"),
                                  track_id=3, language="eng"),
            [str(program), "--in", str(Path("/tmp/3.sup")), "--out", str(Path("/tmp/3.srt"))],
        )


class RunningTheOcrTests(unittest.TestCase):
    """OCR is a minutes-long call to somebody else's program.

    Whatever it does — write the file asked for, write one next to its input,
    fail loudly, fail silently — the caller gets back one boolean and one
    sentence it can put in the report.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="run_ocr_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.source = self.tmp / "track3.sup"
        self.source.write_bytes(b"pgs-bytes")
        self.output = self.tmp / "track3.srt"
        self.backend = sx.OcrBackend(sx.OCR_BACKEND_SUP2SRT, "sup2srt + Tesseract",
                                     ("sup2srt",), frozenset({"PGS"}))
        self.sibling_backend = sx.OcrBackend(
            sx.OCR_BACKEND_PGSRIP, "pgsrip + Tesseract", ("pgsrip",),
            frozenset({"PGS"}), output_mode="sibling")

    def command_that(self, rc: int = 0, writes: Path | None = None,
                     text: str = "1\n00:00:01,000 --> 00:00:02,000\nHi\n",
                     err: str = "") -> mock._patch:
        def fake(_command, timeout=0.0):
            if writes is not None:
                writes.write_text(text, encoding="utf-8")
            return rc, "", err

        return mock.patch.object(sx, "run_external_command", side_effect=fake)

    def test_output_where_it_was_asked_for_is_a_success(self) -> None:
        with self.command_that(writes=self.output):
            ok, detail = sx.run_ocr(self.backend, self.source, self.output)
        self.assertTrue(ok)
        self.assertEqual(detail, "")

    def test_output_written_beside_the_input_is_collected(self) -> None:
        """pgsrip and Subtitle Edit name the file themselves."""
        beside = self.tmp / "track3.eng.srt"
        with self.command_that(writes=beside):
            ok, detail = sx.run_ocr(self.sibling_backend, self.source, self.output)
        self.assertTrue(ok, detail)
        self.assertTrue(self.output.is_file(), "it ends up where the caller expects it")
        self.assertFalse(beside.exists())

    def test_output_that_cannot_be_collected_is_reported(self) -> None:
        beside = self.tmp / "track3.eng.srt"
        with self.command_that(writes=beside), \
                mock.patch.object(sx.shutil, "move", side_effect=OSError("read-only")):
            ok, detail = sx.run_ocr(self.sibling_backend, self.source, self.output)
        self.assertFalse(ok)
        self.assertIn("could not collect the OCR output", detail)

    def test_a_failing_backend_is_quoted_not_raised(self) -> None:
        with self.command_that(rc=3, err="tesseract: unknown language 'eng'"):
            ok, detail = sx.run_ocr(self.backend, self.source, self.output)
        self.assertFalse(ok)
        self.assertIn("sup2srt + Tesseract could not OCR this track (exit 3)", detail)
        self.assertIn("unknown language", detail)

    def test_a_backend_that_exits_clean_with_no_file_is_a_failure(self) -> None:
        with self.command_that(rc=0):
            ok, detail = sx.run_ocr(self.backend, self.source, self.output)
        self.assertFalse(ok)
        self.assertIn("could not OCR this track", detail)

    def test_an_empty_result_is_not_a_subtitle(self) -> None:
        with self.command_that(writes=self.output, text=""):
            ok, _detail = sx.run_ocr(self.backend, self.source, self.output)
        self.assertFalse(ok)


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

    def test_dry_run_does_not_spend_an_ocr_run(self) -> None:
        runner = FakeRunner(PGS_TRACKS)
        backend = sx.OcrBackend(sx.OCR_BACKEND_SUP2SRT, "sup2srt + Tesseract", ("sup2srt",),
                                frozenset({"PGS"}))
        with mock.patch.object(subprocess, "run", runner), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries), \
                mock.patch.object(sx, "detect_ocr_backend", return_value=(backend, "")):
            outcome = sx.extract_embedded_english_srt(
                self.movie, self.dest, sx.ExtractOptions(min_cues=2, dry_run=True))
        self.assertTrue(outcome.ok)
        self.assertFalse(any("tracks" in call for call in runner.calls),
                         "a dry run must not run mkvextract/OCR on an image track")
        self.assertFalse(self.dest.exists())

    def test_image_only_movie_without_ocr_falls_through(self) -> None:
        outcome = self._run(PGS_TRACKS, ocr_backend=sx.OCR_BACKEND_NONE)
        self.assertFalse(outcome.ok)
        self.assertFalse(self.dest.exists())
        self.assertIn("OCR is disabled", outcome.unavailable_reason or outcome.detail)

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

    Extraction is the only way this tool covers a movie — no provider, no
    quota, no network — but only if what comes out of the container is really the
    movie's English dialogue. Everything below is a way for that to not be
    true, and each one has to end with a reason a human can read and no file
    on disk.
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

    def run_with(self, runner: object, *, backend: object = None,
                 **options: object) -> sx.ExtractionOutcome:
        opts = sx.ExtractOptions(min_cues=2, **options)  # type: ignore[arg-type]
        patches = [mock.patch.object(subprocess, "run", runner),
                   mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries)]
        if backend is not None:
            patches.append(mock.patch.object(sx, "detect_ocr_backend",
                                             return_value=(backend, "")))
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
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

    # -- the image tracks, which cost minutes ------------------------------

    def ocr_backend(self) -> sx.OcrBackend:
        return sx.OcrBackend(sx.OCR_BACKEND_SUP2SRT, "sup2srt + Tesseract", ("sup2srt",),
                             frozenset({"PGS"}))

    def test_an_ocred_image_track_becomes_a_sidecar(self) -> None:
        text = sx.render_srt_cues([
            (f"00:00:{index:02d},000", f"00:00:{index:02d},900",
             "A whole line of English dialogue")
            for index in range(1, 30)
        ])

        def ocr(_backend, _source, output, **_kwargs):
            output.write_text(text, encoding="utf-8")
            return True, ""

        with mock.patch.object(sx, "run_ocr", side_effect=ocr):
            outcome = self.run_with(FakeRunner(PGS_TRACKS), backend=self.ocr_backend())
        self.assertTrue(outcome.ok, outcome.detail or outcome.unavailable_reason)
        self.assertEqual(outcome.method, "ocr")
        self.assertEqual(outcome.ocr_backend, "sup2srt + Tesseract")
        self.assertTrue(self.dest.is_file())

    def test_an_ocr_failure_is_carried_into_the_reason(self) -> None:
        with mock.patch.object(sx, "run_ocr",
                               return_value=(False, "sup2srt could not OCR this track (exit 1)")):
            outcome = self.run_with(FakeRunner(PGS_TRACKS), backend=self.ocr_backend())
        self.assertFalse(outcome.ok)
        self.assertIn("could not OCR this track", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_an_ocr_result_that_cannot_be_read_is_a_failure(self) -> None:
        """A backend that says it worked but wrote nowhere we can find."""
        with mock.patch.object(sx, "run_ocr", return_value=(True, "")):
            outcome = self.run_with(FakeRunner(PGS_TRACKS), backend=self.ocr_backend())
        self.assertFalse(outcome.ok)
        self.assertIn("could not read the OCR output", outcome.detail)

    def test_a_backend_that_cannot_read_the_codec_is_not_run(self) -> None:
        vobsub_only = sx.OcrBackend(sx.OCR_BACKEND_SUP2SRT, "sup2srt", ("sup2srt",),
                                    frozenset({"VOBSUB"}))
        with mock.patch.object(sx, "run_ocr") as run_ocr:
            outcome = self.run_with(FakeRunner(PGS_TRACKS), backend=vobsub_only)
        self.assertFalse(outcome.ok)
        self.assertIn("cannot OCR S_HDMV/PGS", outcome.detail)
        run_ocr.assert_not_called()


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
            results, summary = sx.extraction_run(self._config(ocr_backend=sx.OCR_BACKEND_NONE))
        self.assertEqual(len(results), 1)
        # No usable track and nothing to download: the movie is reported, not
        # silently dropped, and never counts as covered.
        self.assertEqual(results[0].reason, sx.REASON_NO_TRACK)
        self.assertEqual(results[0].status, "skip")
        self.assertEqual(int(summary["coverage_covered"]), 0)
        self.assertEqual(list(self.movie.parent.glob("*.srt")), [],
                         "nothing is written for a movie with no usable track")

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
            ok, reason = sx.extracted_subtitle_quality(
                "1\n00:00:01,000 --> 00:00:02,000\n" + "x" * 200 + "\n", min_cues=1)
        self.assertFalse(ok)
        self.assertIn("safety limit", reason)

    def test_a_track_that_did_not_convert_is_refused(self) -> None:
        ok, reason = sx.extracted_subtitle_quality("Dialogue: 0,0:00:01.50,...", min_cues=1)
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

    def test_an_image_only_movie_after_the_run_s_ocr_budget_is_spent(self) -> None:
        """The limit is per run, so the reason names the limit, not the tools."""
        with mock.patch.object(subprocess, "run", FakeRunner(PGS_TRACKS)), \
             mock.patch.object(sx, "find_mkvtoolnix_binary", fake_binaries):
            outcome = sx.extract_embedded_english_srt(
                self.movie, self.dest, sx.ExtractOptions(min_cues=2, ocr_allowed=False))
        self.assertFalse(outcome.ok)
        self.assertIn("per-run OCR limit was reached",
                      outcome.unavailable_reason or outcome.detail)
        self.assertFalse(self.dest.exists())


class OneTrackDirectlyTests(unittest.TestCase):
    """``_extract_one_track`` alone: the arms the caller normally prevents.

    The loop above it checks for a backend before it hands over an image
    track, and the outer entry point checks for MKVToolNix before it starts.
    These tests remove those guarantees, because a helper that trusts its
    caller is a helper that breaks the day the caller changes.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="one_track_direct_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.movie = self.tmp / "Fake (2021).mkv"
        self.movie.write_bytes(b"mkv-bytes")
        self.dest = self.tmp / "Fake (2021).eng.srt"

    def extract(self, track: sx.EmbeddedSubtitleTrack, *, backend: object = None,
                binary: object = fake_binaries) -> sx.ExtractionOutcome:
        with mock.patch.object(sx, "find_mkvtoolnix_binary", binary):
            return sx._extract_one_track(
                self.movie, self.movie, self.dest, track, self.tmp,
                sx.ExtractOptions(min_cues=2), backend=backend)  # type: ignore[arg-type]

    def test_without_mkvextract_nothing_is_attempted(self) -> None:
        track = sx.EmbeddedSubtitleTrack(2, "S_TEXT/UTF8", "eng", "English", "text", ".srt")
        outcome = self.extract(track, binary=lambda *_a, **_k: "")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.detail, "mkvextract is not installed")
        self.assertFalse(self.dest.exists())

    def test_an_image_track_with_no_backend_is_refused_not_attempted(self) -> None:
        track = sx.EmbeddedSubtitleTrack(4, "S_HDMV/PGS", "eng", "English", "image", ".sup")
        with mock.patch.object(subprocess, "run", FakeRunner(PGS_TRACKS)):
            outcome = self.extract(track, backend=None)
        self.assertFalse(outcome.ok)
        self.assertIn("no OCR backend is available", outcome.detail)
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
