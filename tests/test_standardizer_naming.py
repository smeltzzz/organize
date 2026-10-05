"""What the standardizer decides a movie is *called*.

Every folder and file name in the library is produced by these functions, so
they are user-facing output as much as any report: a name that loses a year,
keeps a scene tag, or mangles a stylized title is a movie Jellyfin cannot match
and an operator has to rename by hand.

The rules under test are the ones that only fire on awkward releases - the
reason the parser is this size:

* a bracketed block is peeled only when every token in it is a release tag, so
  ``(500) Days of Summer`` and ``(1999)`` keep their digits while ``[1080p
  BluRay]`` goes;
* a bare number is never a tag, and a year is never a title unless the whole
  title is a year;
* ``Se7en``, ``iPhone``, ``McConaughey``, ``WALL-E`` and ``[REC]`` survive
  title-casing;
* a lone ``hi`` is Hindi, and only beside another language does it mean
  hearing-impaired - Jellyfin's own documented ambiguity;
* a name that has to be truncated is truncated on a character boundary, because
  a split UTF-8 sequence is not a filename;
* a disc structure (``BDMV``) is not one complete MKV and must never be
  hardlinked into the library as if it were.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import movie_standardizer as ms

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

GOOD_SRT = "1\n00:00:01,000 --> 00:00:04,000\nHello.\n\n"


class ConfiguredTests(unittest.TestCase):
    """A case that can flip the two config switches the naming rules read."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ms_naming_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._cfg = ms.CFG
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        ms.CFG = self._cfg

    def configure(self, **kwargs: object) -> None:
        ms.CFG = replace(ms.CFG, **kwargs)  # type: ignore[arg-type]


class JunkAndSizeTests(ConfiguredTests):
    def test_a_size_the_filesystem_will_not_give_is_zero_not_a_crash(self) -> None:
        """A scan reads sizes for every candidate; one unreadable file is a skip."""
        self.assertEqual(ms.file_size(self.root / "absent.mkv"), 0)

    def test_incomplete_downloads_and_dotfiles_are_junk(self) -> None:
        for name in ("movie.mkv.part", "movie.!qb", "movie.crdownload", ".hidden.mkv"):
            with self.subTest(name=name):
                self.assertTrue(ms.is_skipped_junk_name(name))

    def test_a_real_movie_is_not_junk(self) -> None:
        self.assertFalse(ms.is_skipped_junk_name("Film (2020).mkv"))


class ParsedNameShapeTests(ConfiguredTests):
    def test_a_folder_name_is_the_title_and_the_year(self) -> None:
        parsed = ms.parse_movie_name("The.Matrix.1999.1080p.BluRay.x264.mkv")
        self.assertEqual(parsed.folder_name, "The Matrix (1999)")

    def test_an_edition_is_a_tag_only_when_the_configuration_asks_for_one(self) -> None:
        """Two conventions exist and the switch decides which the library gets.

        Plex-style ``{edition-Final Cut}`` folders and Jellyfin version labels
        cannot both be right for one library, so the folder name carries the
        edition only outside Jellyfin mode.
        """
        parsed = ms.parse_movie_name("Blade.Runner.1982.The.Final.Cut.1080p.mkv")
        self.assertEqual(parsed.edition, "Final Cut")
        self.configure(include_edition_tag=True, jellyfin_mode=False)
        self.assertEqual(parsed.folder_name, "Blade Runner (1982) {edition-Final Cut}")
        self.configure(include_edition_tag=True, jellyfin_mode=True)
        self.assertEqual(parsed.folder_name, "Blade Runner (1982)")
        self.configure(include_edition_tag=False, jellyfin_mode=False)
        self.assertEqual(parsed.folder_name, "Blade Runner (1982)")

    def test_a_version_label_is_the_jellyfin_side_of_the_same_decision(self) -> None:
        parsed = ms.parse_movie_name("Blade.Runner.1982.The.Final.Cut.1080p.3D.mkv")
        self.configure(jellyfin_mode=True)
        label = parsed.version_label
        self.assertIsNotNone(label)
        self.assertIn("Final Cut", label)
        self.assertIn("1080p", label)
        self.configure(jellyfin_mode=False)
        self.assertIsNone(parsed.version_label)

    def test_a_movie_with_no_edition_or_resolution_has_no_version_label(self) -> None:
        self.configure(jellyfin_mode=True)
        self.assertIsNone(ms.parse_movie_name("The.Matrix.1999.mkv").version_label)

    def test_a_split_release_keeps_its_part_in_the_file_stem_only(self) -> None:
        """One folder, two files: the part belongs in the stem, not the folder."""
        parsed = ms.parse_movie_name("Movie.Name.2020.1080p.cd1.mkv")
        self.assertEqual(parsed.part, "cd1")
        self.assertEqual(parsed.folder_name, "Movie Name (2020)")
        self.assertEqual(parsed.file_stem(), "Movie Name (2020)-cd1")
        self.assertEqual(parsed.file_stem("cd2"), "Movie Name (2020)-cd2")

    def test_a_movie_with_no_part_has_a_stem_equal_to_its_folder(self) -> None:
        parsed = ms.parse_movie_name("The.Matrix.1999.1080p.mkv")
        self.assertEqual(parsed.file_stem(), parsed.folder_name)

    def test_the_edition_is_not_part_of_a_movies_identity(self) -> None:
        """Every cut of a title *is* that title.

        Filing cuts as separate movies is how a library ends up with two copies
        of one film and Jellyfin guessing which to show.
        """
        theatrical = ms.parse_movie_name("Blade.Runner.1982.Theatrical.1080p.mkv")
        final_cut = ms.parse_movie_name("Blade.Runner.1982.The.Final.Cut.1080p.mkv")
        self.assertEqual(theatrical.identity, final_cut.identity)

    def test_an_empty_name_is_not_a_tv_show(self) -> None:
        self.assertFalse(ms.is_tv_show(""))

    def test_a_movie_that_looks_like_an_episode_pattern_is_not_tv(self) -> None:
        for name in ("Se7en.1995.1080p.mkv", "S.W.A.T.2003.mkv"):
            with self.subTest(name=name):
                self.assertFalse(ms.is_tv_show(name))

    def test_a_real_episode_pattern_is_tv(self) -> None:
        self.assertTrue(ms.is_tv_show("Show.Name.S01E02.1080p.WEB-DL.mkv"))


class TagBlockTests(ConfiguredTests):
    def test_an_empty_bracket_is_a_tag(self) -> None:
        self.assertTrue(ms._is_tag_block("[]"))
        self.assertTrue(ms._is_tag_block("( )"))

    def test_a_year_in_brackets_is_not_a_tag(self) -> None:
        """``(1999)`` is the release year; peeling it would lose the year."""
        self.assertFalse(ms._is_tag_block("(1999)"))
        self.assertFalse(ms._is_tag_block("[2049]"))

    def test_a_number_in_brackets_is_a_title_not_a_tag(self) -> None:
        for block in ("(500)", "(13)", "[9]"):
            with self.subTest(block=block):
                self.assertFalse(ms._is_tag_block(block))

    def test_a_block_of_release_tags_is_a_tag(self) -> None:
        for block in ("[1080p BluRay]", "(x264)", "[DTS 6ch]", "{720p}"):
            with self.subTest(block=block):
                self.assertTrue(ms._is_tag_block(block))

    def test_a_block_of_ordinary_words_is_part_of_the_title(self) -> None:
        self.assertFalse(ms._is_tag_block("(The Final Cut)"))
        self.assertFalse(ms._is_tag_block("[Days of Summer]"))

    def test_a_weak_tag_counts_only_after_a_strong_one(self) -> None:
        """``web`` and ``hd`` are English words as often as they are tags.

        Peeling them unconditionally is how a title loses its last word, so the
        rule is that a weak token is only a tag once a strong one has been seen.
        """
        self.assertTrue(ms._is_tag_token("web"))
        self.assertFalse(ms._is_tag_token("web", allow_weak=False))
        self.assertTrue(ms._is_tag_token("bluray", allow_weak=False))

    def test_a_token_that_is_only_punctuation_is_a_tag(self) -> None:
        self.assertTrue(ms._is_tag_token("[]"))
        self.assertTrue(ms._is_tag_token("..."))

    def test_a_known_tag_and_a_channel_count_are_tags(self) -> None:
        for token in ("1080p", "bluray", "6ch", "dd5.1", "r00"):
            with self.subTest(token=token):
                self.assertTrue(ms._is_tag_token(token))

    def test_a_block_of_nothing_but_punctuation_is_a_tag(self) -> None:
        """``[!!!]`` carries no title information, so it peels like any other tag."""
        self.assertTrue(ms._is_tag_block("[!!!]"))

    def test_leading_tag_blocks_are_peeled_and_titles_are_not(self) -> None:
        self.assertEqual(ms._strip_leading_tag_blocks("[1080p] The Matrix"), "The Matrix")
        self.assertEqual(ms._strip_leading_tag_blocks("{x264} _ Movie"), "Movie")
        self.assertEqual(ms._strip_leading_tag_blocks("(1080p) The Matrix"), "The Matrix")
        self.assertEqual(ms._strip_leading_tag_blocks("(500) Days of Summer"),
                         "(500) Days of Summer")

    def test_trailing_tag_blocks_are_peeled_one_after_another(self) -> None:
        self.assertEqual(ms._strip_trailing_tags("The Matrix [1080p] [BluRay]"), "The Matrix")
        self.assertEqual(ms._strip_trailing_tags("The Matrix (1999)"), "The Matrix (1999)")

    def test_bare_trailing_tags_are_peeled_strong_ones_first(self) -> None:
        """No brackets: the tokens themselves have to be recognised as tags.

        A weak token (``web``, ``hd``) is only peeled once a strong one has been
        seen, which is what keeps "The Web" a title and "The Matrix 1080p web"
        a title plus two tags.
        """
        self.assertEqual(ms._strip_trailing_tags("The Matrix 1080p BluRay"), "The Matrix")
        self.assertEqual(ms._strip_trailing_tags("The Matrix web 1080p"), "The Matrix",
                         "a weak token goes once a strong one has been seen to its right")
        self.assertEqual(ms._strip_trailing_tags("The Matrix Web"), "The Matrix Web",
                         "a lone weak token is a word, not a tag")

    def test_a_hyphenated_scene_suffix_is_peeled_from_the_last_word(self) -> None:
        self.assertEqual(ms._strip_trailing_tags("The Matrix x264-WiKi"), "The Matrix")

    def test_a_website_prefix_is_peeled_even_when_it_repeats(self) -> None:
        """A release group's banner is sometimes stamped on twice."""
        self.assertEqual(ms._strip_website_prefix("www.Example.com - Example.Com - The Matrix"),
                         "The Matrix")

    def test_a_part_token_is_normalised_to_the_family_the_library_uses(self) -> None:
        for name, expected in (("Movie cd1", "cd1"), ("Movie CD2", "cd2"),
                               ("Movie disc1", "disc1"), ("Movie disk2", "disc2"),
                               ("Movie dvd1", "dvd1"), ("Movie pt2", "cd2")):
            with self.subTest(name=name):
                _cleaned, part = ms._extract_part(name)
                self.assertEqual(part, expected)

    def test_a_title_integral_part_is_not_a_split_release(self) -> None:
        """``Deathly Hallows Part 2`` is a distinct movie, not the second disc."""
        name = "Harry Potter and the Deathly Hallows Part 2"
        cleaned, part = ms._extract_part(name)
        self.assertIsNone(part)
        self.assertEqual(cleaned, name)

    def test_a_part_beside_a_disc_cue_is_a_split_release(self) -> None:
        """``part`` alone is a title word; ``disc`` beside it makes it a stack."""
        cleaned, part = ms._extract_part("Movie disc part2")
        self.assertEqual(part, "cd2")
        self.assertEqual(cleaned, "Movie disc")

    def test_a_part_with_no_disc_cue_anywhere_is_left_in_the_title(self) -> None:
        cleaned, part = ms._extract_part("Movie part2 cd1")
        self.assertIsNone(part, "``cd1`` has no word boundary, so it is not a disc cue")
        self.assertEqual(cleaned, "Movie part2 cd1")

    def test_an_inner_tag_block_is_dropped_from_the_middle_of_a_title(self) -> None:
        """``Movie [x264] Name`` is one title with a tag stamped in the middle."""
        parsed = ms.parse_movie_name("Movie [x264] Name 2020.mkv")
        self.assertEqual(parsed.title, "Movie Name")
        self.assertEqual(parsed.year, 2020)

    def test_an_inner_block_that_is_a_title_is_kept(self) -> None:
        parsed = ms.parse_movie_name("The (500) Days 2009.mkv")
        self.assertIn("500", parsed.title)

    def test_a_bracketed_provider_id_is_lifted_out_of_the_title(self) -> None:
        """Jellyfin reads ``[imdbid-tt...]``; a title containing it does not match."""
        cleaned, provider = ms._extract_provider_id("Arrival (2016) [imdbid-tt2543164] 1080p")
        self.assertEqual(provider, "imdbid-tt2543164")
        self.assertNotIn("tt2543164", cleaned)
        self.assertIn("Arrival", cleaned)

    def test_a_name_without_a_provider_id_is_unchanged(self) -> None:
        cleaned, provider = ms._extract_provider_id("Arrival (2016) 1080p")
        self.assertIsNone(provider)
        self.assertEqual(cleaned, "Arrival (2016) 1080p")


class YearScoringTests(ConfiguredTests):
    def _score(self, name: str, pattern: str = r"(?<!\d)((?:18|19|20)\d{2})(?!\d)") -> int:
        """Score the first four-digit year-looking token in ``name``.

        The scanner's own regex already refuses the worst false positives, so
        the guard inside the scorer is exercised with a plainer match - which is
        the shape it has to survive if that regex is ever loosened.
        """
        import re

        match = re.search(pattern, name)
        assert match is not None, name
        return ms._score_year_match(name, match)

    def test_a_year_outside_the_plausible_range_is_rejected(self) -> None:
        """``is_valid_year`` bounds it: 2160 is a resolution, not a release year."""
        self.assertLess(self._score("Movie 2160 x264", r"(\d{4})"), -9000)

    def test_a_year_immediately_followed_by_a_scan_line_is_rejected(self) -> None:
        self.assertLess(self._score("Movie 1999p x264"), -9000)

    def test_a_year_in_parentheses_outscores_a_bare_one(self) -> None:
        self.assertGreater(self._score("Movie (1999)"), self._score("Movie 1999"))

    def test_a_year_followed_by_release_tags_outscores_one_followed_by_nothing(self) -> None:
        self.assertGreater(self._score("Movie 1999 1080p BluRay"), self._score("Movie 1999"))

    def test_a_year_as_the_first_token_is_likely_the_title(self) -> None:
        """2012, 1917 and 1984 are movies, not release years."""
        self.assertLess(self._score("2012 1080p"), self._score("Movie 2012 1080p"))
        self.assertEqual(ms.parse_movie_name("2012.2009.1080p.mkv").title, "2012")
        self.assertEqual(ms.parse_movie_name("2012.2009.1080p.mkv").year, 2009)

    def test_words_after_the_year_that_look_like_a_title_push_it_back(self) -> None:
        """``2001 A Space Odyssey 1968``: 2001 is the title, 1968 is the year."""
        parsed = ms.parse_movie_name("2001.A.Space.Odyssey.1968.1080p.mkv")
        self.assertEqual(parsed.year, 1968)
        self.assertIn("2001", parsed.title)

    def test_a_name_that_is_only_tags_and_a_year_keeps_the_year(self) -> None:
        """Nothing survived the peel but the year, so the year is what identifies it.

        The folder is ``Unknown (1984)`` rather than ``Unknown``: a year in the
        name is still enough for a human to find it, and the movie is placed
        rather than declined. (The branch below this one in ``parse_movie_name``
        that would have made the year the *title* cannot be reached, because
        ``sanitize_filename`` answers "Unknown" for an empty name first - see the
        notes on this PR.)
        """
        parsed = ms.parse_movie_name("[1080p] [BluRay] 1984.mkv")
        self.assertEqual(parsed.year, 1984)
        self.assertEqual(parsed.folder_name, "Unknown (1984)")

    def test_a_name_that_reduces_to_nothing_at_all_is_still_placeable(self) -> None:
        parsed = ms.parse_movie_name("[1080p][BluRay][x264].mkv")
        self.assertEqual(parsed.folder_name, "Unknown")
        self.assertNotIn("/", parsed.folder_name)


class TitleCasingTests(ConfiguredTests):
    def test_a_stylized_token_is_recognised_and_left_alone_mid_title(self) -> None:
        """``Se7en``, ``iPhone`` and ``McConaughey`` are spelled that way on purpose.

        The generic title-caser would produce "Se7En"/"Iphone"/"Mcconaughey",
        which is a name no metadata provider matches.
        """
        for word in ("Se7en", "iPhone", "McConaughey", "eXistenZ"):
            with self.subTest(word=word):
                self.assertTrue(ms._is_stylized_token(word))
        self.assertEqual(ms.custom_title_case("the Se7en"), "The Se7en")
        self.assertEqual(ms.custom_title_case("the iPhone"), "The iPhone")
        self.assertEqual(ms.custom_title_case("matthew McConaughey"), "Matthew McConaughey")

    def test_a_stylized_word_that_starts_the_title_gets_its_first_letter_raised(self) -> None:
        """A title cannot begin with a lower-case letter, whatever the brand says."""
        self.assertEqual(ms.custom_title_case("iPhone"), "IPhone")
        self.assertEqual(ms.custom_title_case("eXistenZ"), "EXistenZ")

    def test_an_acronym_is_upper_cased(self) -> None:
        self.assertEqual(ms.custom_title_case("the bbc story"), "The BBC Story")

    def test_a_roman_numeral_is_upper_cased_but_an_ambiguous_word_is_not(self) -> None:
        self.assertEqual(ms.custom_title_case("part ii"), "Part II")

    def test_an_empty_word_is_returned_unchanged(self) -> None:
        self.assertEqual(ms._title_case_word("", 0, 1, False), "")

    def test_a_contraction_is_cased_once_and_keeps_its_apostrophe(self) -> None:
        """An all-caps release name must not stay all-caps through an apostrophe."""
        self.assertEqual(ms._title_case_word("don't", 1, 3, False), "Don't")
        self.assertEqual(ms.custom_title_case("DON'T"), "Don't")
        # The head of an apostrophised word is raised and the tail is lowered, so
        # an Irish surname arrives as "O'briens": the shape survives, the second
        # capital does not. Asserted as shipped, not as ideal.
        self.assertEqual(ms.custom_title_case("THE O'BRIENS"), "The O'briens")

    def test_a_hyphenated_word_cases_both_halves(self) -> None:
        self.assertEqual(ms.custom_title_case("well-known"), "Well-Known")

    def test_a_minor_word_stays_lower_except_at_the_ends(self) -> None:
        self.assertEqual(ms.custom_title_case("the lord of the rings"),
                         "The Lord of the Rings")

    def test_a_short_bracket_title_is_kept_verbatim(self) -> None:
        """``[REC]`` is the film's actual name, in brackets, upper case."""
        self.assertEqual(ms.custom_title_case("[rec]"), "[REC]")
        self.assertEqual(ms.custom_title_case("[rec]2"), "[REC]2")

    def test_an_all_caps_release_name_is_cased_not_shouted(self) -> None:
        self.assertEqual(ms.custom_title_case("THE GREAT ESCAPE"), "The Great Escape")

    def test_a_word_after_a_colon_starts_a_new_phrase(self) -> None:
        self.assertEqual(ms.custom_title_case("mad max: fury road"), "Mad Max: Fury Road")

    def test_brackets_around_a_word_survive_the_casing(self) -> None:
        self.assertEqual(ms.custom_title_case("(the) matrix"), "(The) Matrix")
        self.assertEqual(ms.custom_title_case("matrix, (the)"), "Matrix, (The)")

    def test_a_number_forces_the_next_word_to_capitalise(self) -> None:
        self.assertEqual(ms.custom_title_case("blade runner 2049 black"),
                         "Blade Runner 2049 Black")

    def test_an_empty_title_stays_empty(self) -> None:
        self.assertEqual(ms.custom_title_case("   "), "")


class SanitizeTests(ConfiguredTests):
    def test_illegal_characters_become_the_documented_replacements(self) -> None:
        self.assertEqual(ms.sanitize_filename("A:B"), "A -B")
        self.assertEqual(ms.sanitize_filename("A/B"), "A - B")
        self.assertNotIn("|", ms.sanitize_filename("A|B"))
        self.assertNotIn('"', ms.sanitize_filename('A"B'))

    def test_a_windows_device_name_is_prefixed_rather_than_lost(self) -> None:
        """``CON``, ``NUL`` and ``PRN`` cannot be file names on Windows.

        The library lives on a NAS that a Windows machine may mount, and a name
        that is legal on Linux makes the whole folder unreadable there.
        """
        for name in ("CON", "nul", "PRN.mkv"):
            with self.subTest(name=name):
                self.assertTrue(ms.sanitize_filename(name).startswith("_"))

    def test_a_name_longer_than_the_filesystem_allows_is_cut_on_a_character(self) -> None:
        """A truncated UTF-8 sequence is not a filename, and 255 bytes is the limit.

        Cutting at 200 bytes leaves room for the folder name, the extension and
        the suffixes the other tools add.
        """
        long_name = "Ünïcödé" * 60
        sanitized = ms.sanitize_filename(long_name)
        self.assertLessEqual(len(sanitized.encode("utf-8")), 200)
        self.assertTrue(sanitized, "the name still says something")
        sanitized.encode("utf-8").decode("utf-8")  # must not raise
        self.assertFalse(sanitized.endswith((" ", ".")),
                         "a trailing space or dot is illegal on Windows")

    def test_a_name_that_sanitizes_to_nothing_becomes_unknown(self) -> None:
        self.assertEqual(ms.sanitize_filename("***"), "Unknown")


class SubtitleSuffixTests(ConfiguredTests):
    def test_english_collapses_to_the_canonical_library_tag(self) -> None:
        for name in ("movie.en.srt", "movie.eng.srt", "movie.english.srt"):
            with self.subTest(name=name):
                self.assertEqual(ms.subtitle_suffix(name), ".eng.srt")

    def test_a_lone_hi_is_hindi(self) -> None:
        """Jellyfin's documented ambiguity, resolved the way the docs say."""
        self.assertEqual(ms.subtitle_suffix("movie.hi.srt"), ".hi.srt")

    def test_hi_beside_another_language_is_hearing_impaired(self) -> None:
        self.assertEqual(ms.subtitle_suffix("movie.en.hi.srt"), ".eng.sdh.srt")

    def test_flags_come_after_languages_and_the_extension_is_lower_cased(self) -> None:
        self.assertEqual(ms.subtitle_suffix("Movie.English.Forced.SRT"), ".eng.forced.srt")

    def test_a_subtitle_with_no_language_or_flag_keeps_only_its_extension(self) -> None:
        self.assertEqual(ms.subtitle_suffix("movie.srt"), ".srt")
        self.assertEqual(ms.subtitle_suffix("movie.sub"), ".sub")

    def test_a_duplicated_language_is_listed_once(self) -> None:
        self.assertEqual(ms.subtitle_suffix("movie.eng.en.srt"), ".eng.srt")

    def test_an_english_sidecar_is_recognised_by_its_suffix(self) -> None:
        self.assertTrue(ms.is_english_subtitle(Path("movie.eng.srt")))
        self.assertFalse(ms.is_english_subtitle(Path("movie.fra.srt")))


class SidecarValidationTests(ConfiguredTests):
    """``is_valid_plain_english_srt``: what may be hardlinked into the library."""

    def _sidecar(self, name: str = "Movie (2020).eng.srt", body: str | None = GOOD_SRT) -> Path:
        path = self.root / name
        path.write_text(body if body is not None else "", encoding="utf-8")
        return path

    def test_a_valid_canonical_sidecar_is_accepted(self) -> None:
        ok, reason = ms.is_valid_plain_english_srt(self._sidecar())
        self.assertTrue(ok)
        self.assertEqual(reason, "validated normal English SRT")

    def test_a_valid_legacy_sidecar_is_accepted_for_the_promote_step_to_rename(self) -> None:
        ok, _ = ms.is_valid_plain_english_srt(self._sidecar("Movie (2020).en.srt"))
        self.assertTrue(ok)

    def test_a_non_srt_is_refused(self) -> None:
        path = self.root / "Movie (2020).eng.sub"
        path.write_text("x", encoding="utf-8")
        self.assertEqual(ms.is_valid_plain_english_srt(path), (False, "not an SRT"))

    def test_a_sidecar_that_cannot_be_stat_ed_is_refused(self) -> None:
        ok, reason = ms.is_valid_plain_english_srt(self.root / "absent.eng.srt")
        self.assertFalse(ok)
        self.assertIn("could not stat subtitle", reason)

    def test_a_symlinked_sidecar_is_refused(self) -> None:
        """Placement hardlinks what it validated; through a symlink that lands elsewhere."""
        real = self.root / "real.srt"
        real.write_text(GOOD_SRT, encoding="utf-8")
        link = self.root / "Movie (2020).eng.srt"
        link.symlink_to(real)
        ok, reason = ms.is_valid_plain_english_srt(link)
        self.assertFalse(ok)
        self.assertIn("not a regular non-symlink file", reason)

    def test_an_empty_or_oversized_sidecar_is_refused(self) -> None:
        empty = self._sidecar("Movie (2020).eng.srt", body="")
        self.assertEqual(ms.is_valid_plain_english_srt(empty)[1], "subtitle size is unsafe")
        huge = self.root / "Movie (2021).eng.srt"
        with huge.open("wb") as handle:
            handle.truncate(ms.EXTERNAL_SRT_MAX_BYTES + 1)
        self.assertEqual(ms.is_valid_plain_english_srt(huge)[1], "subtitle size is unsafe")

    def test_a_sidecar_that_cannot_be_read_is_refused(self) -> None:
        path = self._sidecar()
        with mock.patch.object(Path, "read_bytes", side_effect=OSError("permission denied")):
            ok, reason = ms.is_valid_plain_english_srt(path)
        self.assertFalse(ok)
        self.assertIn("could not read subtitle", reason)

    def test_a_sidecar_with_no_cue_in_it_is_refused(self) -> None:
        """An HTML error page is the shape this really arrives in."""
        page = self._sidecar("Movie (2020).eng.srt", body="<html><body>404</body></html>")
        ok, reason = ms.is_valid_plain_english_srt(page)
        self.assertFalse(ok)
        self.assertIn("does not contain a valid numbered SRT cue", reason)

    def test_a_sidecar_that_is_not_text_is_refused(self) -> None:
        binary = self.root / "Movie (2020).eng.srt"
        binary.write_bytes(b"\x81\x81\x81\x81")
        ok, reason = ms.is_valid_plain_english_srt(binary)
        self.assertFalse(ok)
        self.assertIn("does not contain a valid numbered SRT cue", reason)

    def test_a_flagged_sidecar_is_not_a_plain_english_one(self) -> None:
        """``.eng.sdh.srt`` is a real sidecar, but not the one direct play wants."""
        sdh = self._sidecar("Movie (2020).eng.sdh.srt")
        ok, reason = ms.is_valid_plain_english_srt(sdh)
        self.assertFalse(ok)
        self.assertIn("not a normal English SRT", reason)


class DiscAndExtraTests(ConfiguredTests):
    def test_a_disc_folder_name_is_recognised_whatever_its_case(self) -> None:
        for name in ("BDMV", "bdmv", "VIDEO_TS", " video_ts "):
            with self.subTest(name=name):
                self.assertTrue(ms.is_disc_folder_name(name))
        self.assertFalse(ms.is_disc_folder_name("Movies"))

    def test_a_disc_structure_at_the_top_of_a_release_is_detected(self) -> None:
        (self.root / "BDMV").mkdir()
        self.assertTrue(ms.path_has_disc_structure(self.root))

    def test_a_disc_structure_one_folder_down_is_detected(self) -> None:
        """``Movie/Anything/BDMV`` is how a real disc rip arrives."""
        (self.root / "disc1" / "BDMV").mkdir(parents=True)
        self.assertTrue(ms.path_has_disc_structure(self.root))

    def test_a_release_with_no_disc_structure_is_not_one(self) -> None:
        (self.root / "Movie.2020.1080p.mkv").write_bytes(b"x")
        self.assertFalse(ms.path_has_disc_structure(self.root))

    def test_a_path_that_is_not_a_directory_has_no_disc_structure(self) -> None:
        movie = self.root / "Movie.2020.1080p.mkv"
        movie.write_bytes(b"x")
        self.assertFalse(ms.path_has_disc_structure(movie))

    def test_a_folder_that_cannot_be_listed_is_not_a_disc(self) -> None:
        """The caller declines a disc rip; it must not crash on an unreadable one."""
        with mock.patch.object(Path, "iterdir", side_effect=OSError("share went away")):
            self.assertFalse(ms.path_has_disc_structure(self.root))

    def test_a_share_that_goes_away_between_the_two_listings_is_not_a_disc(self) -> None:
        """The wrapper scan lists the folder a second time; a NAS can drop it."""
        real_iterdir = Path.iterdir
        calls = {"n": 0}

        def flaky(path: Path) -> object:
            if path == self.root:
                calls["n"] += 1
                if calls["n"] > 1:
                    raise OSError("share went away")
            return real_iterdir(path)

        with mock.patch.object(Path, "iterdir", flaky):
            self.assertFalse(ms.path_has_disc_structure(self.root))

    def test_a_child_folder_that_cannot_be_listed_does_not_stop_the_check(self) -> None:
        """A disc rip can be beside a folder the share will not open.

        The wrapper scan is what finds ``Movie/Anything/BDMV``, so one unreadable
        child must cost that child and not the answer about the release.
        """
        (self.root / "readable").mkdir()
        unreadable = self.root / "unreadable"
        unreadable.mkdir()
        with_disc = self.root / "disc1" / "BDMV"
        with_disc.mkdir(parents=True)
        real_iterdir = Path.iterdir

        def flaky(path: Path) -> object:
            if path == unreadable:
                raise OSError("permission denied")
            return real_iterdir(path)

        with mock.patch.object(Path, "iterdir", flaky):
            self.assertTrue(ms.path_has_disc_structure(self.root))
        with mock.patch.object(Path, "iterdir", flaky):
            self.assertFalse(ms.path_has_disc_structure(unreadable.parent / "nothing"))

    def test_a_jellyfin_extra_suffix_makes_a_video_an_extra(self) -> None:
        """Jellyfin's own ``-trailer`` / ``-sample`` naming convention.

        A library in Jellyfin mode has already been arranged with those
        suffixes, so a video carrying one is an extra wherever it is found -
        placing it as a feature would give the movie two videos.
        """
        self.configure(jellyfin_mode=True)
        for name in ("Movie (2020)-trailer.mkv", "Movie (2020) sample.mkv",
                     "Movie (2020)-featurette.mkv"):
            with self.subTest(name=name):
                self.assertTrue(ms.is_extra_video(self.root / name))
        self.assertFalse(ms.is_extra_video(self.root / "Movie (2020).mkv"))

    def test_an_extra_token_anywhere_in_the_name_declines_the_video(self) -> None:
        """Conservative on purpose - and it costs a real title. Pinned as shipped.

        ``trailer``, ``sample`` and ``teaser`` match anywhere between
        non-letters, so "Trailer Park Boys" is declined as an extra instead of
        being placed. Nothing is lost by it: the decline is recorded with its
        reason, the download stays in the source, and the report says what to do.
        Narrowing the rule would change what a library ingests, so it belongs in
        its own change with its own evidence - not in a coverage PR. This test
        exists so the behaviour is visible rather than incidental.
        """
        self.configure(jellyfin_mode=False)
        self.assertTrue(ms.is_extra_video(self.root / "Trailer.Park.Boys.2006.mkv"))
        self.assertTrue(ms.is_extra_video(self.root / "Sample People 2020.mkv"))
        self.assertFalse(ms.is_extra_video(self.root / "The.Matrix.1999.1080p.mkv"))

    def test_a_video_in_an_extras_folder_is_an_extra(self) -> None:
        self.configure(jellyfin_mode=False)
        self.assertTrue(ms.is_extra_video(self.root / "Extras" / "movie.mkv", root=self.root))

    def test_a_video_named_like_a_sample_is_an_extra(self) -> None:
        self.configure(jellyfin_mode=False)
        self.assertTrue(ms.is_extra_video(self.root / "Movie-sample.mkv"))

    def test_a_feature_film_is_not_an_extra(self) -> None:
        self.configure(jellyfin_mode=False)
        self.assertFalse(ms.is_extra_video(self.root / "Movie (2020).mkv"))


class GenericStemTests(ConfiguredTests):
    def test_a_split_release_with_no_title_is_generic(self) -> None:
        """``cd1.mkv`` in a folder tells you nothing about the movie."""
        for stem in ("cd1", "Disc 2", "part1", "movie cd1"):
            with self.subTest(stem=stem):
                self.assertTrue(ms.stem_is_generic(stem))

    def test_a_title_with_a_year_is_not_generic_even_with_a_part(self) -> None:
        self.assertFalse(ms.stem_is_generic("The.Matrix.1999.cd1"))

    def test_a_real_title_is_not_generic(self) -> None:
        self.assertFalse(ms.stem_is_generic("The.Matrix.1999.1080p"))

    def test_a_disc_structure_stem_is_generic(self) -> None:
        for stem in ("movie", "video", "feature", "mainmovie"):
            with self.subTest(stem=stem):
                self.assertTrue(ms.stem_is_generic(stem))

    def test_a_downloader_folder_name_never_overrides_the_video_identity(self) -> None:
        """``download``, ``torrent`` and ``unsorted`` say nothing about the film."""
        for name in ("download", "torrent", "unsorted", "qbittorrent"):
            with self.subTest(name=name):
                self.assertTrue(ms.folder_name_is_generic(name))
        self.assertFalse(ms.folder_name_is_generic("The Matrix"))

    def test_a_tv_named_video_keeps_its_own_identity(self) -> None:
        """The folder is never consulted for an episode: it is not a movie at all."""
        episode = self.root / "Show.Name" / "Show.Name.S01E02.1080p.mkv"
        parsed = ms.parse_video_identity(episode, fallback=self.root / "Show.Name")
        self.assertTrue(parsed.is_tv)
        self.assertEqual(parsed.raw, episode.name)

    def test_a_movie_named_only_by_its_folder_uses_the_folder(self) -> None:
        video = self.root / "The.Great.Escape.1963.1080p" / "movie.mkv"
        parsed = ms.parse_video_identity(video, fallback=video.parent)
        self.assertEqual(parsed.title, "The Great Escape")
        self.assertEqual(parsed.year, 1963)


if __name__ == "__main__":
    unittest.main()
