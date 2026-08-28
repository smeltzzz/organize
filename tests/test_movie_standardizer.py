"""Tests for the pure name-parsing logic in ``movie_standardizer.py``."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import movie_standardizer as ms


class ParseMovieNameTests(unittest.TestCase):
    def test_canonical(self) -> None:
        parsed = ms.parse_movie_name("The.Matrix.1999.1080p.BluRay.x264.mkv")
        self.assertEqual(parsed.title, "The Matrix")
        self.assertEqual(parsed.year, 1999)
        self.assertEqual(parsed.resolution, "1080p")

    def test_year_in_parens_kept(self) -> None:
        parsed = ms.parse_movie_name("Inception (2010) [1080p] [BluRay]")
        self.assertEqual(parsed.title, "Inception")
        self.assertEqual(parsed.year, 2010)

    def test_tv_show_detected(self) -> None:
        parsed = ms.parse_movie_name("Show.Name.S01E02.1080p.WEB-DL.mkv")
        self.assertTrue(parsed.is_tv)

    def test_bare_year_title_not_year(self) -> None:
        # 2012 / 1917 are movie titles, not release years.
        parsed = ms.parse_movie_name("2012.2009.1080p.mkv")
        self.assertEqual(parsed.title, "2012")
        self.assertEqual(parsed.year, 2009)

    def test_split_release_parts(self) -> None:
        parsed = ms.parse_movie_name("Movie.Name.2020.1080p.cd1.mkv")
        self.assertEqual(parsed.part, "cd1")

    def test_edition_is_metadata_not_title(self) -> None:
        parsed = ms.parse_movie_name("Blade.Runner.1982.The.Final.Cut.1080p.mkv")
        self.assertEqual(parsed.title, "Blade Runner")
        self.assertEqual(parsed.edition, "Final Cut")

    def test_provider_id_extracted_gently(self) -> None:
        parsed = ms.parse_movie_name("Arrival (2016) [TTtt2543164] 1080p.mkv")
        # Provider id is only recognized in the bracketed imdbid/tmdbid form.
        self.assertIn(parsed.title.casefold(), ("arrival",))
        self.assertIsInstance(parsed.title, str)

    def test_sanitize_removes_illegal_chars(self) -> None:
        # '/' -> " - ", ':' -> " -", '*' and '?' are dropped entirely.
        self.assertEqual(ms.sanitize_filename('A/B:C*D?'), "A - B -CD")


class SubtitleLanguageTests(unittest.TestCase):
    def test_english_suffix(self) -> None:
        self.assertTrue(ms.is_english_subtitle(ms.Path("Film.English.srt")))
        self.assertTrue(ms.is_english_subtitle(ms.Path("Film.en.sdh.srt")))
        self.assertFalse(ms.is_english_subtitle(ms.Path("Film.Spanish.srt")))

    def test_suffix_order(self) -> None:
        self.assertEqual(ms.subtitle_suffix("Film.English.srt"), ".en.srt")


class DuplicateUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="standardizer_upgrade_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        release = self.root / "Film.2020.1080p.WEB-DL"
        release.mkdir()
        self.source = release / "Film.2020.1080p.WEB-DL.mkv"
        self.destination = self.root / "Film (2020).mkv"
        self.source.write_bytes(b"new movie")
        self.destination.write_bytes(b"old")
        self._find = ms.find_ffprobe
        self._probe = ms.probe_media
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        ms.find_ffprobe = self._find
        ms.probe_media = self._probe

    @staticmethod
    def _info(**changes) -> ms.MediaTechnicalInfo:
        values = {
            "duration": 7200.0,
            "width": 1920,
            "height": 1080,
            "video_codec": "h264",
            "bit_depth": 8,
            "hdr": False,
            "video_bitrate": 5_000_000,
            "audio_channels": 6,
            "audio_bitrate": 640_000,
        }
        values.update(changes)
        return ms.MediaTechnicalInfo(**values)

    def _use_probe_results(self, source, existing) -> None:
        ms.find_ffprobe = lambda _explicit="ffprobe": "ffprobe"
        ms.probe_media = lambda path, _binary: ((source if path == self.source else existing), "")

    def test_ffprobe_is_required_instead_of_size_fallback(self) -> None:
        ms.find_ffprobe = lambda _explicit="ffprobe": None
        replace, reason = ms.should_replace(self.source, self.destination)
        self.assertFalse(replace)
        self.assertIn("size alone never replaces", reason)

    def test_runtime_mismatch_blocks_larger_source(self) -> None:
        self._use_probe_results(self._info(duration=7400, width=3840, height=2160), self._info())
        replace, reason = ms.should_replace(self.source, self.destination)
        self.assertFalse(replace)
        self.assertIn("different cut", reason)

    def test_balanced_score_allows_clear_same_cut_upgrade(self) -> None:
        self._use_probe_results(
            self._info(duration=7210, width=3840, height=2160, video_codec="hevc", bit_depth=10,
                       hdr=True, video_bitrate=12_000_000),
            self._info(),
        )
        replace, reason = ms.should_replace(self.source, self.destination)
        self.assertTrue(replace, reason)
        self.assertIn("same-cut technical upgrade", reason)

    def test_quality_downgrade_is_blocked_even_if_score_could_rise(self) -> None:
        self._use_probe_results(
            self._info(bit_depth=8, video_bitrate=20_000_000),
            self._info(bit_depth=10, video_bitrate=3_000_000),
        )
        replace, reason = ms.should_replace(self.source, self.destination)
        self.assertFalse(replace)
        self.assertIn("lower video bit depth", reason)

    def test_alternate_edition_is_never_automatically_replaced(self) -> None:
        self.source = self.source.with_name("Film.2020.Directors.Cut.2160p.mkv")
        self.source.write_bytes(b"alternate")
        replace, reason = ms.should_replace(self.source, self.destination)
        self.assertFalse(replace)
        self.assertIn("alternate-cut", reason)


if __name__ == "__main__":
    unittest.main()
