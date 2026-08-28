"""Tests for the pure classification helpers in ``mkv_track_cleaner.py``."""

from __future__ import annotations

import unittest

import mkv_track_cleaner as tc


class LanguageAndCommentaryTests(unittest.TestCase):
    def test_normalize_language(self) -> None:
        self.assertEqual(tc.normalize_language("fre"), "fr")
        self.assertEqual(tc.normalize_language("eng"), "en")
        self.assertEqual(tc.normalize_language("en"), "en")

    def test_matching_language(self) -> None:
        eng = {"id": 1, "type": "audio", "properties": {"language": "eng", "language_ietf": "en"}}
        self.assertTrue(tc.is_matching_language(eng, {"en", "eng"}))
        self.assertFalse(tc.is_matching_language(eng, {"fr"}))

    def test_sdh_subtitle_is_kept(self) -> None:
        sdh = {"type": "subtitles", "properties": {
            "language": "eng", "track_name": "English SDH",
            "flag_hearing_impaired": True, "flag_visual_impaired": True,
        }}
        self.assertFalse(tc.is_commentary_track(sdh, True))

    def test_commentary_and_dvs_dropped(self) -> None:
        comm = {"type": "audio", "properties": {"language": "eng", "track_name": "Director Commentary", "flag_commentary": True}}
        self.assertTrue(tc.is_commentary_track(comm, True))
        dvs = {"type": "audio", "properties": {"language": "eng", "track_name": "English Audio Description", "flag_visual_impaired": True}}
        self.assertTrue(tc.is_commentary_track(dvs, True))

    def test_forced_subtitle(self) -> None:
        forced = {"type": "subtitles", "properties": {"language": "eng", "track_name": "English Forced", "flag_forced": True}}
        self.assertTrue(tc.is_forced_subtitle(forced))


class AudioQualityTests(unittest.TestCase):
    def test_truehd_beats_aac(self) -> None:
        truehd = {"codec": "TrueHD", "properties": {"codec_id": "A_MLP", "audio_channels": 8, "track_name": "Atmos"}}
        aac = {"codec": "AAC", "properties": {"codec_id": "A_AAC", "audio_channels": 6}}
        self.assertGreater(tc.get_audio_quality_score(truehd), tc.get_audio_quality_score(aac))


class ProgressParsingTests(unittest.TestCase):
    def test_progress_forms(self) -> None:
        self.assertEqual(tc._parse_mkvmerge_progress("Progress: 45%"), 45)
        self.assertEqual(tc._parse_mkvmerge_progress("#GUI#progress 80%"), 80)
        self.assertEqual(tc._parse_mkvmerge_progress("#GUI#progress#parts=1/4"), 25)
        self.assertIsNone(tc._parse_mkvmerge_progress("hello"))


if __name__ == "__main__":
    unittest.main()
