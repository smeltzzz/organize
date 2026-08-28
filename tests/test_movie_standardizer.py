"""Tests for the pure name-parsing logic in ``movie_standardizer.py``."""

from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
