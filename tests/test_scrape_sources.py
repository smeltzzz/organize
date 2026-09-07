"""The scraped sources: seven sites, no API, and no promises.

The API providers answer with JSON they document. These seven do not: they are
HTML pages built for humans, and they change without telling anybody. That is
the whole reason this tier is written the way it is — every adapter parses
defensively, every failure is one source's failure rather than the run's, and
a source that keeps surprising the parser is switched off for the rest of the
run.

None of that had tests at the level where it happens. The chain wiring was
covered end to end, but the parsers themselves — the code that decides that
this row is an English subtitle and that one is a 40%-translated draft — were
only ever run against one happy-path page per site.

So these tests hand the adapters the pages a site actually serves on a bad
day: an empty result set, a layout that moved, a row with no download link, a
year that is not a number, a payload that is not a zip, a search page for a
different film with a similar name. What is pinned is not the parse but the
refusal: nothing here may end with a subtitle that is not English, complete
and for this movie, and nothing here may raise into the run.
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.parse
import zipfile
from typing import Any
from unittest import mock

import subtitle_fetcher as sf

IDENTITY = sf.SourceIdentity("The Dark Knight", 2008, sf.normalize_title("The Dark Knight"))

SRT = ("1\n00:00:01,000 --> 00:00:04,000\nYou either die a hero.\n\n"
       "2\n00:00:05,000 --> 00:00:08,000\nOr you live long enough.\n")


def zipped(name: str = "movie.utf8.srt", text: str = SRT) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


class Pages(sf.ScrapeTransport):
    """A transport that serves canned pages and records what was asked for.

    Keyed by URL fragment; anything not listed answers 404, which is what a
    site with nothing for this movie looks like from the outside.
    """

    def __init__(self, pages: dict[str, Any] | None = None) -> None:
        super().__init__(gap=0.0, sleep=lambda _s: None, clock=lambda: 0.0)
        self.pages = dict(pages or {})
        self.requests: list[str] = []

    def _open(self, url: str, data: bytes | None, headers: dict[str, str]) -> bytes:
        self.requests.append(url)
        for fragment, body in self.pages.items():
            if fragment in url:
                if isinstance(body, Exception):
                    raise body
                return body if isinstance(body, bytes) else str(body).encode("utf-8")
        raise sf.ScrapeSourceError(f"HTTP 404 for {urllib.parse.urlsplit(url).path}")

    def asked(self, fragment: str) -> int:
        return sum(1 for url in self.requests if fragment in url)


class WhatTheTransportSaysWentWrongTests(unittest.TestCase):
    """Every network outcome becomes one sentence a report can print."""

    def setUp(self) -> None:
        self.transport = sf.ScrapeTransport(gap=0.0, sleep=lambda _s: None, clock=lambda: 0.0)

    def urlopen(self, result: Any) -> mock._patch:
        def opener(_request, timeout=None):
            if isinstance(result, Exception):
                raise result
            return result

        return mock.patch.object(sf.urllib.request, "urlopen", side_effect=opener)

    def response(self, body: bytes, status: int = 200) -> Any:
        class Response:
            def __init__(self) -> None:
                self.status = status

            def read(self, amount: int | None = None) -> bytes:
                return body[:amount] if amount is not None else body

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

        return Response()

    def test_a_clean_page_comes_back_as_bytes(self) -> None:
        with self.urlopen(self.response(b"<html>hi</html>")):
            self.assertEqual(self.transport.get("https://example.test/x"), b"<html>hi</html>")

    def test_a_non_2xx_status_is_a_source_error_naming_the_path(self) -> None:
        """Some sites answer 503 with a body rather than raising an HTTPError."""
        with (self.urlopen(self.response(b"busy", status=503)),
              self.assertRaises(sf.ScrapeSourceError) as caught):
            self.transport.get("https://example.test/subtitles/search?q=secret")
        self.assertIn("HTTP 503 for /subtitles/search", str(caught.exception))

    def test_an_http_error_is_a_source_error(self) -> None:
        error = urllib.error.HTTPError("https://example.test/p", 404, "Not Found", None, None)
        with self.urlopen(error), self.assertRaises(sf.ScrapeSourceError) as caught:
            self.transport.get("https://example.test/p")
        self.assertIn("HTTP 404 for /p", str(caught.exception))

    def test_a_network_error_names_the_host_not_the_query(self) -> None:
        with (self.urlopen(urllib.error.URLError("name or service not known")),
              self.assertRaises(sf.ScrapeSourceError) as caught):
            self.transport.get("https://example.test/s?q=The+Dark+Knight")
        message = str(caught.exception)
        self.assertIn("network error for example.test", message)
        self.assertNotIn("Dark", message)

    def test_a_timeout_is_a_transport_error(self) -> None:
        with (self.urlopen(TimeoutError("timed out")),
              self.assertRaises(sf.ScrapeSourceError) as caught):
            self.transport.get("https://example.test/p")
        self.assertIn("transport error for example.test", str(caught.exception))

    def test_a_page_larger_than_the_limit_is_refused(self) -> None:
        """An unbounded read of an untrusted host is how a run runs out of memory."""
        with (self.urlopen(self.response(b"x" * (sf.SCRAPE_MAX_RESPONSE_BYTES + 1))),
              self.assertRaises(sf.ScrapeSourceError) as caught):
            self.transport.get("https://example.test/p")
        self.assertIn("exceeds the size limit", str(caught.exception))


class DecodingWhatCameBackTests(unittest.TestCase):
    """Scraped subtitles arrive in whatever encoding the uploader used."""

    def test_utf8_with_a_byte_order_mark(self) -> None:
        self.assertEqual(sf.decode_scrape_subtitle_bytes("\ufeffHola".encode()), "Hola")

    def test_windows_1252_is_the_last_resort(self) -> None:
        self.assertEqual(sf.decode_scrape_subtitle_bytes(b"Caf\x92s"), "Caf\u2019s")

    def test_text_that_already_holds_a_replacement_character_is_not_trusted(self) -> None:
        """A U+FFFD in the utf-8 reading means the guess was wrong, not that the text has one."""
        decoded = sf.decode_scrape_subtitle_bytes("Caf\ufffds".encode())
        self.assertNotIn("\ufffd", decoded)

    def test_bytes_that_are_not_text_at_all_are_refused(self) -> None:
        with self.assertRaises(sf.ScrapeSourceError):
            sf.decode_scrape_subtitle_bytes(b"\x81\x8d\xff\xfe\x00")


class PodnapisiTests(unittest.TestCase):
    """Podnapisi answers JSON, which can still be the wrong JSON."""

    def setUp(self) -> None:
        self.source = sf.PodnapisiSource()

    def payload(self, data: Any, page: Any = 1, all_pages: Any = 1) -> str:
        return json.dumps({"data": data, "page": page, "all_pages": all_pages})

    def entry(self, pid: int = 11, year: Any = 2008, title: str = "The Dark Knight",
              releases: list[str] | None = None) -> dict[str, Any]:
        return {"id": pid, "movie": {"title": title, "year": year},
                "releases": releases if releases is not None else ["The.Dark.Knight.1080p.BluRay"]}

    def search(self, *payloads: str) -> list[sf.ScrapeCandidate]:
        bodies = list(payloads)
        transport = Pages()
        transport._open = lambda url, data, headers: bodies.pop(0).encode("utf-8")  # type: ignore[method-assign]
        return self.source.search(IDENTITY, transport)

    def test_a_matching_entry_becomes_a_candidate(self) -> None:
        found = self.search(self.payload([self.entry()]))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].release, "The.Dark.Knight.1080p.BluRay")
        self.assertEqual(found[0].feature_year, 2008)

    def test_a_payload_that_is_not_a_list_is_a_source_error(self) -> None:
        with self.assertRaises(sf.ScrapeSourceError):
            self.search(self.payload({"id": 1}))

    def test_entries_that_are_not_objects_are_skipped(self) -> None:
        self.assertEqual(self.search(self.payload(["nonsense", 7, None])), [])

    def test_the_same_film_listed_twice_is_one_candidate(self) -> None:
        found = self.search(self.payload([self.entry(), self.entry()]))
        self.assertEqual(len(found), 1)

    def test_an_entry_for_another_year_is_dropped(self) -> None:
        self.assertEqual(self.search(self.payload([self.entry(year=1995)])), [])

    def test_a_year_that_is_not_a_number_is_treated_as_unknown(self) -> None:
        """Unknown is not "wrong": the ranking below can still reject it."""
        found = self.search(self.payload([self.entry(year="MMVIII")]))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].feature_year, 0)

    def test_a_second_page_is_read_when_the_site_says_there_is_one(self) -> None:
        found = self.search(
            self.payload([self.entry(pid=1)], page=1, all_pages=2),
            self.payload([self.entry(pid=2)], page=2, all_pages=2),
        )
        self.assertEqual([c.file_id for c in found], ["1", "2"])

    def test_pagination_fields_that_make_no_sense_stop_the_walk(self) -> None:
        found = self.search(self.payload([self.entry(pid=1)], page="?", all_pages="lots"))
        self.assertEqual(len(found), 1, "one page was read and no second was asked for")

    def test_a_site_that_lists_everything_is_cut_off(self) -> None:
        many = [self.entry(pid=index) for index in range(50)]
        found = self.search(self.payload(many, page=1, all_pages=1))
        self.assertEqual(len(found), sf.SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2)

    def test_an_entry_with_no_release_name_is_still_usable(self) -> None:
        found = self.search(self.payload([self.entry(releases=[])]))
        self.assertEqual(found[0].release, "")


ADDIC7ED_SEARCH = '<html><a href="movie/4321">The Dark Knight</a></html>'


def addic7ed_movie_page(rows: str, title: str = "The Dark Knight", year: str = "2008") -> str:
    return (
        f'<html><body><a href="/show/9">show</a><span>{title} ({year})</span> <small>movie</small>'
        f"{rows}</body></html>"
    )


def addic7ed_row(language: str = "English", status: str = "Completed",
                 link: str = "/original/4321/1", downloads: str = "1200",
                 version: str = "1080p BluRay") -> str:
    """The layout in use today: language cell, status cell, download anchor."""
    return (
        f"Version {version}, 1.2MB"
        f'<td class="language">{language}</td>'
        f'<td class="Completed">{status}</td><td>{downloads} Downloads</td>'
        f'<a href="{link}"><strong>most updated</strong></a>'
    )


def addic7ed_row_one_cell(language: str = "English", status: str = "Completed",
                          link: str = "/original/4321/1", downloads: str = "1200") -> str:
    """An older shape: everything crammed into the language cell."""
    return (
        "Version 1080p BluRay, 1.2MB"
        f'<td class="language">{language}<span>{status}</span> {downloads} Downloads</td>'
        f'<a href="{link}"><strong>Download</strong></a>'
    )


class Addic7edTests(unittest.TestCase):
    """Addic7ed's movie page is a table that only sometimes means what it says."""

    def setUp(self) -> None:
        self.source = sf.Addic7edSource()

    def search(self, search_page: str, movie_page: str | None = None) -> list[sf.ScrapeCandidate]:
        pages: dict[str, Any] = {"srch.php": search_page}
        if movie_page is not None:
            pages["movie/4321"] = movie_page
        self.transport = Pages(pages)
        return self.source.search(IDENTITY, self.transport)

    def test_a_completed_english_row_becomes_a_candidate(self) -> None:
        found = self.search(ADDIC7ED_SEARCH, addic7ed_movie_page(addic7ed_row()))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].file_id, "/original/4321/1")
        self.assertEqual(found[0].release, "1080p BluRay")
        self.assertEqual(found[0].downloads, 1200)
        self.assertEqual(found[0].feature_title, "The Dark Knight")
        self.assertEqual(found[0].feature_year, 2008)
        self.assertIn("/show/9", str(found[0].extra.get("referer")))

    def test_a_search_with_no_results_asks_for_nothing_else(self) -> None:
        """The empty search page still carries movie links in its sidebar."""
        page = ('<html><b> 0 results found </b>'
                '<div class="sidebar"><a href="movie/4321">Popular movie</a></div></html>')
        self.assertEqual(self.search(page), [])
        self.assertEqual(self.transport.asked("movie/"), 0)

    def test_a_search_page_with_no_movie_links_is_not_parsed_further(self) -> None:
        self.assertEqual(self.search("<html>something else entirely</html>"), [])
        self.assertEqual(self.transport.asked("movie/"), 0)

    def test_a_partially_translated_row_is_not_offered(self) -> None:
        """"80% Completed" is a draft, and its download link does not work."""
        rows = addic7ed_row(status="80% Completed")
        self.assertEqual(self.search(ADDIC7ED_SEARCH, addic7ed_movie_page(rows)), [])

    def test_a_bare_percentage_is_read_as_a_draft_too(self) -> None:
        rows = addic7ed_row(status="80%")
        self.assertEqual(self.search(ADDIC7ED_SEARCH, addic7ed_movie_page(rows)), [])

    def test_a_row_that_never_says_completed_is_not_offered(self) -> None:
        rows = addic7ed_row(status="Working")
        self.assertEqual(self.search(ADDIC7ED_SEARCH, addic7ed_movie_page(rows)), [])

    def test_the_older_all_in_one_cell_layout_is_still_read(self) -> None:
        """Language, status and count in one cell: the shape before the split."""
        found = self.search(ADDIC7ED_SEARCH, addic7ed_movie_page(addic7ed_row_one_cell()))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].downloads, 1200)

    def test_a_draft_in_the_all_in_one_cell_layout_is_not_offered(self) -> None:
        rows = addic7ed_row_one_cell(status="80% Completed")
        self.assertEqual(self.search(ADDIC7ED_SEARCH, addic7ed_movie_page(rows)), [])

    def test_a_row_in_another_language_is_not_offered(self) -> None:
        rows = addic7ed_row(language="Spanish")
        self.assertEqual(self.search(ADDIC7ED_SEARCH, addic7ed_movie_page(rows)), [])

    def test_a_row_that_lists_the_count_before_the_status_is_still_english(self) -> None:
        """The language is the language cell, not "everything before Completed"."""
        rows = ('Version 1080p BluRay, 1.2MB<td class="language">English</td>'
                '<td>1200 Downloads</td><td class="Completed">Completed</td>'
                '<a href="/original/4321/1"><strong>Download</strong></a>')
        found = self.search(ADDIC7ED_SEARCH, addic7ed_movie_page(rows))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].downloads, 1200)

    def test_a_row_with_no_download_link_is_not_offered(self) -> None:
        rows = ('Version 1080p BluRay, 1.2MB<td class="language">English</td>'
                '<td class="Completed">Completed</td><td>5 Downloads</td>')
        self.assertEqual(self.search(ADDIC7ED_SEARCH, addic7ed_movie_page(rows)), [])

    def test_a_hearing_impaired_row_is_labelled_as_one(self) -> None:
        rows = addic7ed_row(language="English (hearing impaired)")
        found = self.search(ADDIC7ED_SEARCH, addic7ed_movie_page(rows))
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].hearing_impaired)

    def test_a_movie_page_with_no_readable_header_falls_back_to_the_movie_asked_for(self) -> None:
        page = f'<a href="/show/9">show</a>{addic7ed_row()}'
        found = self.search(ADDIC7ED_SEARCH, page)
        self.assertEqual(found[0].feature_title, "The Dark Knight")
        self.assertEqual(found[0].feature_year, 2008)

    def test_a_page_full_of_rows_is_cut_off(self) -> None:
        rows = "".join(addic7ed_row(link=f"/original/4321/{index}") for index in range(20))
        found = self.search(ADDIC7ED_SEARCH, addic7ed_movie_page(rows))
        self.assertEqual(len(found), sf.SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2)

    def test_the_download_carries_the_referer_the_search_recorded(self) -> None:
        """Addic7ed serves an error page to a download with no Referer."""
        candidate = sf.ScrapeCandidate(provider=sf.PROVIDER_ADDIC7ED, file_id="/original/4321/1",
                                       extra={"referer": "https://www.addic7ed.com/show/9"})
        transport = Pages({"/original/4321/1": SRT})
        self.assertEqual(self.source.fetch(candidate, transport).decode("utf-8"), SRT)

    def test_a_download_that_is_not_a_subtitle_is_refused(self) -> None:
        candidate = sf.ScrapeCandidate(provider=sf.PROVIDER_ADDIC7ED, file_id="/original/4321/1")
        transport = Pages({"/original/4321/1": "<html>too many downloads today</html>"})
        with self.assertRaises(sf.CandidateRejected) as caught:
            self.source.fetch(candidate, transport)
        self.assertIn("not a valid SRT", str(caught.exception))


def subsource_movie_page(*ids: int) -> str:
    rows = "".join(f'<a href="/subtitle/the-dark-knight/english/{n}">English</a>' for n in ids)
    return f"<html><body>{rows}</body></html>"


class SubSourceTests(unittest.TestCase):
    """SubSource has guessable URLs, which is a shortcut and a trap."""

    def setUp(self) -> None:
        self.source = sf.SubSourceSource()

    def search(self, pages: dict[str, Any]) -> list[sf.ScrapeCandidate]:
        self.transport = Pages(pages)
        return self.source.search(IDENTITY, self.transport)

    def test_the_guessed_movie_url_is_tried_first(self) -> None:
        found = self.search({"/subtitles/the-dark-knight-2008": subsource_movie_page(1, 2)})
        self.assertEqual(len(found), 2)
        self.assertEqual(self.transport.asked("/search?"), 0, "no search was needed")

    def test_the_same_file_listed_twice_is_one_candidate(self) -> None:
        found = self.search({"/subtitles/the-dark-knight-2008": subsource_movie_page(1, 1, 2)})
        self.assertEqual(len(found), 2)

    def test_a_guess_that_misses_falls_back_to_the_search_page(self) -> None:
        found = self.search({
            "/search?q=": '<a href="/subtitles/the-dark-knight-2008">The Dark Knight</a>',
            "/subtitles/the-dark-knight-2008": subsource_movie_page(7),
        })
        self.assertEqual([c.file_id for c in found],
                         ["/subtitle/the-dark-knight/english/7"])

    def test_a_guess_that_answers_with_an_empty_page_falls_back_too(self) -> None:
        pages = {
            "/subtitles/the-dark-knight-2008": "<html>no subtitles yet</html>",
            "/search?q=": '<a href="/subtitles/the-dark-knight-batman-2008">Batman</a>',
            "/subtitles/the-dark-knight-batman-2008": subsource_movie_page(3),
        }
        found = self.search(pages)
        self.assertEqual(len(found), 1, "a close-enough title from the search page is used")

    def test_a_movie_page_that_lists_everything_is_cut_off(self) -> None:
        page = subsource_movie_page(*range(50))
        found = self.search({"/subtitles/the-dark-knight-2008": page})
        self.assertEqual(len(found), sf.SCRAPE_MAX_CANDIDATES_PER_SOURCE)

    def flaky_first(self, pages: dict[str, Any]) -> Pages:
        """A transport whose first request fails — the guessed URL and the
        search hit are the same page, so this is the only way to reach the
        search-page branch for a movie SubSource really does have."""
        class FlakyFirst(Pages):
            def _open(self, url: str, data: bytes | None, headers: dict[str, str]) -> bytes:
                if not self.requests:
                    self.requests.append(url)
                    raise sf.ScrapeSourceError("HTTP 502 for /subtitles/the-dark-knight-2008")
                return super()._open(url, data, headers)

        return FlakyFirst(pages)

    def test_a_guess_that_fails_once_is_still_read_from_the_search_page(self) -> None:
        self.transport = self.flaky_first({
            "/search?q=": '<a href="/subtitles/the-dark-knight-2008">The Dark Knight</a>',
            "/subtitles/the-dark-knight-2008": subsource_movie_page(5),
        })
        found = self.source.search(IDENTITY, self.transport)
        self.assertEqual([c.file_id for c in found], ["/subtitle/the-dark-knight/english/5"])

    def test_a_second_movie_page_that_is_down_costs_only_its_own_rows(self) -> None:
        self.transport = self.flaky_first({
            "/search?q=": ('<a href="/subtitles/the-dark-knight-2008">one</a>'
                           '<a href="/subtitles/the-dark-knight-imax-2008">two</a>'),
            "/subtitles/the-dark-knight-2008": subsource_movie_page(1),
            # the imax page is a 404 from this transport: one source, one
            # broken page, and the rows that did load still count.
        })
        found = self.source.search(IDENTITY, self.transport)
        self.assertEqual([c.file_id for c in found], ["/subtitle/the-dark-knight/english/1"])

    def test_a_search_hit_for_a_different_film_is_ignored(self) -> None:
        found = self.search({
            "/search?q=": '<a href="/subtitles/the-hangover-2008">The Hangover</a>',
            "/subtitles/the-hangover-2008": subsource_movie_page(9),
        })
        self.assertEqual(found, [])

    def test_a_search_hit_for_another_year_is_ignored(self) -> None:
        found = self.search({
            "/search?q=": '<a href="/subtitles/the-dark-knight-1995">The Dark Knight</a>',
            "/subtitles/the-dark-knight-1995": subsource_movie_page(9),
        })
        self.assertEqual(found, [])

    def test_a_page_that_will_not_load_is_skipped_rather_than_fatal(self) -> None:
        found = self.search({
            "/search?q=": ('<a href="/subtitles/the-dark-knight-2008">a</a>'
                           '<a href="/subtitles/the-dark-knight-returns-2008">b</a>'),
            "/subtitles/the-dark-knight-2008": subsource_movie_page(1),
            "/subtitles/the-dark-knight-returns-2008": sf.ScrapeSourceError("HTTP 500"),
        })
        self.assertEqual(len(found), 1)

    # -- the download leg ---------------------------------------------------

    def fetch(self, pages: dict[str, Any]) -> bytes:
        candidate = sf.ScrapeCandidate(provider=sf.PROVIDER_SUBSOURCE,
                                       file_id="/subtitle/the-dark-knight/english/7")
        return self.source.fetch(candidate, Pages(pages))

    def test_the_api_link_on_the_file_page_is_followed(self) -> None:
        link = "https://api.subsource.net/v1/subtitle/download/abc123"
        self.assertEqual(self.fetch({"/subtitle/the-dark-knight/english/7": f'<a href="{link}">get</a>',
                                     "api.subsource.net": SRT.encode("utf-8")}),
                         SRT.encode("utf-8"))

    def test_a_zipped_answer_is_unpacked(self) -> None:
        link = "https://api.subsource.net/v1/subtitle/download/abc123"
        raw = self.fetch({"/subtitle/the-dark-knight/english/7": f'<a href="{link}">get</a>',
                          "api.subsource.net": zipped()})
        self.assertEqual(raw.decode("utf-8"), SRT)

    def test_a_file_page_with_no_download_link_is_a_source_error(self) -> None:
        with self.assertRaises(sf.ScrapeSourceError):
            self.fetch({"/subtitle/the-dark-knight/english/7": "<html>login required</html>"})

    def test_an_answer_that_is_not_a_subtitle_is_refused(self) -> None:
        link = "https://api.subsource.net/v1/subtitle/download/abc123"
        with self.assertRaises(sf.CandidateRejected):
            self.fetch({"/subtitle/the-dark-knight/english/7": f'<a href="{link}">get</a>',
                        "api.subsource.net": b"<html>not a subtitle</html>"})


def yify_row(language: str = "English", rating: str = "5", href: str = "/subtitles/tdk-en-1",
             data_id: str = "1") -> str:
    return (
        f'<tr data-id="{data_id}">'
        f'<td class="rating-cell">{rating}</td>'
        f'<span class="sub-lang">{language}</span>'
        f'<a href="{href}">download</a>'
        "</tr>"
    )


def yify_card(title: str = "The Dark Knight", year: str = "2008",
              href: str = "/movie-imdb/tt0468569") -> str:
    return (f'<div class="media-body"><a href="{href}">'
            f'<h3 itemprop="name">{title}</h3></a>'
            f'<span class="movinfo-section">{year}</span></div>')


class YifySearchTests(unittest.TestCase):
    """YIFY's search page is a wall of cards; only complete ones count."""

    def setUp(self) -> None:
        self.source = sf.YifySubtitlesSource()

    def search(self, page: str) -> list[sf.ScrapeCandidate]:
        return self.source.search(IDENTITY, Pages({"/search?q=": page}))

    def test_a_complete_card_becomes_a_candidate(self) -> None:
        found = self.search(yify_card())
        self.assertEqual([c.file_id for c in found], ["/movie-imdb/tt0468569"])
        self.assertEqual(found[0].feature_year, 2008)

    def test_a_card_with_no_year_on_it_is_skipped(self) -> None:
        card = ('<div class="media-body"><a href="/movie-imdb/tt1">'
                '<h3 itemprop="name">The Dark Knight</h3></a></div>')
        self.assertEqual(self.search(card), [])

    def test_the_same_movie_carded_twice_is_one_candidate(self) -> None:
        self.assertEqual(len(self.search(yify_card() + yify_card())), 1)

    def test_a_search_page_that_lists_everything_is_cut_off(self) -> None:
        cards = "".join(yify_card(href=f"/movie-imdb/tt{n}") for n in range(200))
        self.assertEqual(len(self.search(cards)), sf.SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2)


class YifySubtitlesTests(unittest.TestCase):
    """YIFY's movie page ranks its own subtitles; the highest rated English wins."""

    def setUp(self) -> None:
        self.source = sf.YifySubtitlesSource()
        self.candidate = sf.ScrapeCandidate(provider=sf.PROVIDER_YIFY,
                                            file_id="/movie-imdb/tt0468569")

    def fetch(self, rows: str, download: Any = None) -> bytes:
        pages: dict[str, Any] = {"/movie-imdb/tt0468569": f"<table>{rows}</table>"}
        if download is not None:
            pages["/subtitle/"] = download
        self.transport = Pages(pages)
        return self.source.fetch(self.candidate, self.transport)

    def test_the_best_rated_english_row_is_downloaded(self) -> None:
        """Best, not last: the highest rated row wins wherever it sits."""
        rows = (yify_row(rating="9", href="/subtitles/high", data_id="1")
                + yify_row(rating="3", href="/subtitles/low", data_id="2"))
        raw = self.fetch(rows, download=zipped())
        self.assertEqual(raw.decode("utf-8"), SRT)
        self.assertTrue(any("/subtitle/high.zip" in url for url in self.transport.requests),
                        self.transport.requests)

    def test_rows_in_other_languages_are_not_considered(self) -> None:
        rows = yify_row(language="French", rating="9", href="/subtitles/fr")
        with self.assertRaises(sf.CandidateRejected):
            self.fetch(rows)

    def test_a_negatively_rated_row_is_not_considered(self) -> None:
        """A downvoted subtitle is refused even when it is the only one."""
        with self.assertRaises(sf.CandidateRejected):
            self.fetch(yify_row(rating="-2"), download=zipped())

    def test_a_row_with_no_link_is_not_considered(self) -> None:
        rows = ('<tr data-id="1"><td class="rating-cell">7</td>'
                '<span class="sub-lang">English</span></tr>')
        with self.assertRaises(sf.CandidateRejected):
            self.fetch(rows)

    def test_a_row_with_no_rating_at_all_still_counts(self) -> None:
        rows = ('<tr data-id="1"><span class="sub-lang">English</span>'
                '<a href="/subtitles/plain">get</a></tr>')
        self.assertEqual(self.fetch(rows, download=zipped()).decode("utf-8"), SRT)

    def test_a_page_with_no_english_rows_is_a_refusal_not_a_crash(self) -> None:
        with self.assertRaises(sf.CandidateRejected):
            self.fetch("<tr><td>nothing here</td></tr>")


class Subf2mTests(unittest.TestCase):
    """Subf2m: a title search, a movie page, a download page, then a zip.

    Four requests deep, and each of the three pages can have moved. What is
    pinned here is that a layout change is a *refusal*, not a wrong subtitle.
    """

    def setUp(self) -> None:
        self.source = sf.Subf2meSource()

    def search(self, page: str) -> list[sf.ScrapeCandidate]:
        self.transport = Pages({"/subtitles/searchbytitle": page})
        return self.source.search(IDENTITY, self.transport)

    def test_a_result_for_this_year_becomes_a_candidate(self) -> None:
        page = ('<div class="search-result"><ul>'
                '<li><a href="/subtitles/the-dark-knight">The Dark Knight (2008)</a></li>'
                "</ul></div>")
        found = self.search(page)
        self.assertEqual([c.file_id for c in found], ["/subtitles/the-dark-knight"])
        self.assertEqual(found[0].feature_year, 2008)

    def test_a_page_with_no_result_block_yields_nothing(self) -> None:
        self.assertEqual(self.search("<html><body>nothing today</body></html>"), [])

    def test_a_result_block_with_no_list_is_still_read(self) -> None:
        """The site sometimes drops the <ul>; the first 4 kB is scanned instead."""
        page = ('<div class="search-result">'
                '<a href="/subtitles/the-dark-knight">The Dark Knight (2008)</a>')
        self.assertEqual(len(self.search(page)), 1)

    def test_a_result_for_another_year_is_not_a_candidate(self) -> None:
        page = ('<div class="search-result"><ul>'
                '<li><a href="/subtitles/batman-begins">Batman Begins (2005)</a></li>'
                "</ul></div>")
        self.assertEqual(self.search(page), [])

    def test_a_site_that_lists_everything_is_cut_off(self) -> None:
        rows = "".join(
            f'<li><a href="/subtitles/copy-{n}">The Dark Knight (2008)</a></li>'
            for n in range(200)
        )
        found = self.search(f'<div class="search-result"><ul>{rows}</ul></div>')
        self.assertEqual(len(found), sf.SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2)

    def _fetch(self, pages: dict[str, Any]) -> bytes:
        self.transport = Pages(pages)
        candidate = sf.ScrapeCandidate(provider=sf.PROVIDER_SUBF2ME,
                                       file_id="/subtitles/the-dark-knight")
        return self.source.fetch(candidate, self.transport)

    def test_the_first_download_row_leads_to_the_zip(self) -> None:
        raw = self._fetch({
            "/subtitles/the-dark-knight/en": (
                '<li class="item"><a class="download icon-download" '
                'href="/download/1">get</a></li>'),
            "/download/1": '<div class="download"><a href="/dl/1">download</a></div>',
            "/dl/1": zipped(),
        })
        self.assertEqual(raw.decode("utf-8"), SRT)

    def test_a_movie_page_with_no_download_rows_is_a_source_error(self) -> None:
        with self.assertRaises(sf.ScrapeSourceError) as caught:
            self._fetch({"/subtitles/the-dark-knight/en": "<div>no rows</div>"})
        self.assertIn("no download rows", str(caught.exception))

    def test_a_download_page_with_no_link_is_a_source_error(self) -> None:
        with self.assertRaises(sf.ScrapeSourceError) as caught:
            self._fetch({
                "/subtitles/the-dark-knight/en": (
                    '<li class="item"><a class="download" href="/subtitles/x/english/1">get</a></li>'),
                "/subtitles/x/english/1": "<div class=\"download\">gone</div>",
            })
        self.assertIn("no download link", str(caught.exception))


class SubsunacsTests(unittest.TestCase):
    """Subsunacs cannot scope a search to English, so the page is re-checked.

    The catalogue is Bulgarian. Every guard here exists to stop a Bulgarian
    subtitle — or the right title from the wrong year — becoming this movie's
    English sidecar.
    """

    def setUp(self) -> None:
        self.source = sf.SubsunacsSource()
        self.candidate = sf.ScrapeCandidate(
            provider=sf.PROVIDER_SUBSUNACS, file_id="/subtitles/dark-knight-1/",
            release="The Dark Knight", feature_title="The Dark Knight", feature_year=2008)

    def test_a_result_row_becomes_a_candidate(self) -> None:
        page = ('<a href="/subtitles/dark-knight-1/">The Dark Knight</a> <span>(2008)</span>')
        found = self.source.search(IDENTITY, Pages({"/search.php": page}))
        self.assertEqual([c.file_id for c in found], ["/subtitles/dark-knight-1/"])

    def test_the_same_row_twice_is_one_candidate(self) -> None:
        row = '<a href="/subtitles/dark-knight-1/">The Dark Knight</a> <span>(2008)</span>'
        found = self.source.search(IDENTITY, Pages({"/search.php": row + row}))
        self.assertEqual(len(found), 1)

    def test_a_search_that_lists_everything_is_cut_off(self) -> None:
        rows = "".join(
            f'<a href="/subtitles/copy-{n}/">The Dark Knight</a> <span>(2008)</span>'
            for n in range(200))
        found = self.source.search(IDENTITY, Pages({"/search.php": rows}))
        self.assertEqual(len(found), sf.SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2)

    def _page(self, *, language: str = "Английски", title: str = "The Dark Knight",
              year: int = 2008, entry: bool = True) -> str:
        entry_html = ('<a href="/getentry.php?id=1&amp;ei=0">download</a>' if entry else "")
        return (f"<h1>{title} ({year})</h1>Език: {language} / 2008{entry_html}")

    def _fetch(self, page: str, payload: Any = None) -> bytes:
        pages: dict[str, Any] = {"/subtitles/dark-knight-1/": page}
        pages["/getentry.php"] = SRT.encode("utf-8") if payload is None else payload
        return self.source.fetch(self.candidate, Pages(pages))

    def test_an_english_page_downloads_the_archive_entry(self) -> None:
        self.assertEqual(self._fetch(self._page()).decode("utf-8"), SRT)

    def test_a_bulgarian_subtitle_is_refused_by_its_own_page(self) -> None:
        with self.assertRaises(sf.CandidateRejected) as caught:
            self._fetch(self._page(language="Български"))
        self.assertIn("not English", str(caught.exception))

    def test_a_page_for_another_year_is_refused(self) -> None:
        with self.assertRaises(sf.CandidateRejected) as caught:
            self._fetch(self._page(year=2005))
        self.assertIn("year does not match", str(caught.exception))

    def test_a_page_for_another_film_is_refused(self) -> None:
        with self.assertRaises(sf.CandidateRejected) as caught:
            self._fetch(self._page(title="The Hangover"))
        self.assertIn("title does not match", str(caught.exception))

    def test_a_page_with_no_archive_entry_is_a_source_error(self) -> None:
        with self.assertRaises(sf.ScrapeSourceError) as caught:
            self._fetch(self._page(entry=False))
        self.assertIn("no archive entry", str(caught.exception))

    def test_a_payload_that_is_not_an_srt_is_refused(self) -> None:
        with self.assertRaises(sf.CandidateRejected) as caught:
            self._fetch(self._page(), payload=b"<html>not a subtitle</html>")
        self.assertIn("not a valid SRT", str(caught.exception))


class SubsSabTests(unittest.TestCase):
    """Subs.sab.bz publishes no language metadata, so the bytes are judged."""

    def setUp(self) -> None:
        self.source = sf.SubsSabSource()
        self.candidate = sf.ScrapeCandidate(provider=sf.PROVIDER_SUBSAB, file_id="42")

    def search(self, page: str) -> list[sf.ScrapeCandidate]:
        return self.source.search(IDENTITY, Pages({"/index.php": page}))

    def test_an_attachment_row_becomes_a_candidate(self) -> None:
        page = '<a href="index.php?act=download&attach_id=42">The Dark Knight (2008)</a>'
        found = self.search(page)
        self.assertEqual([c.file_id for c in found], ["42"])
        self.assertEqual(found[0].feature_year, 2008)

    def test_the_same_attachment_twice_is_one_candidate(self) -> None:
        row = '<a href="index.php?attach_id=42">The Dark Knight (2008)</a>'
        self.assertEqual(len(self.search(row + row)), 1)

    def test_a_row_with_no_readable_title_falls_back_to_the_movie_asked_for(self) -> None:
        found = self.search('<a href="index.php?attach_id=7">download</a>')
        self.assertEqual(found[0].feature_title, IDENTITY.title)
        self.assertEqual(found[0].feature_year, IDENTITY.year)

    def test_a_page_full_of_attachments_is_cut_off(self) -> None:
        rows = "".join(f'<a href="index.php?attach_id={n}">x (2008)</a>' for n in range(200))
        self.assertEqual(len(self.search(rows)), sf.SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2)

    def _fetch(self, payload: bytes) -> bytes:
        return self.source.fetch(self.candidate, Pages({"attach_id=42": payload}))

    def test_an_english_srt_is_accepted(self) -> None:
        self.assertEqual(self._fetch(SRT.encode("utf-8")).decode("utf-8"), SRT)

    def test_a_payload_that_is_not_text_is_refused(self) -> None:
        with self.assertRaises(sf.CandidateRejected) as caught:
            self._fetch(b"\x00\x81\xfe" * 40)
        self.assertIn("not text", str(caught.exception))

    def test_a_cyrillic_subtitle_is_refused(self) -> None:
        cyrillic = SRT.replace("You either die a hero.", "Или умираш като герой") \
                      .replace("Or you live long enough.", "или живееш достатъчно дълго")
        with self.assertRaises(sf.CandidateRejected) as caught:
            self._fetch(cyrillic.encode("utf-8"))
        self.assertIn("Cyrillic", str(caught.exception))

    def test_a_payload_that_is_not_an_srt_is_refused(self) -> None:
        with self.assertRaises(sf.CandidateRejected) as caught:
            self._fetch(b"<html>not a subtitle</html>")
        self.assertIn("not a valid SRT", str(caught.exception))


class TheChainDecidesWhoToTrustTests(unittest.TestCase):
    """One source's bad day is not the tier's, and not the run's."""

    def chain(self, **kwargs: Any) -> sf.ScrapeChain:
        return sf.ScrapeChain(keys=(sf.PROVIDER_SUBF2ME, sf.PROVIDER_PODNAPISI),
                              transport=Pages(), search_caps={}, **kwargs)

    def source_raising(self, exc: Exception) -> mock._patch:
        class Broken(sf.BaseSource):
            key = sf.PROVIDER_SUBF2ME
            label = "Subf2m.co"

            def search(self, identity, t):  # noqa: ANN001, ARG002
                raise exc

            def fetch(self, candidate, t):  # noqa: ANN001, ARG002
                raise exc

        return mock.patch.dict(sf.SCRAPE_SOURCES, {sf.PROVIDER_SUBF2ME: Broken()})

    def test_a_site_that_is_down_is_a_source_being_unavailable(self) -> None:
        chain = self.chain()
        with (self.source_raising(sf.ScrapeSourceError("HTTP 503 for /search")),
              self.assertRaises(sf.SourceUnavailable) as caught):
            chain.search(sf.PROVIDER_SUBF2ME, IDENTITY)
        self.assertIn("HTTP 503", str(caught.exception))
        self.assertEqual(chain.health[sf.PROVIDER_SUBF2ME].hard_failures, 1)

    def test_three_hard_failures_switch_the_source_off(self) -> None:
        chain = self.chain()
        with self.source_raising(sf.ScrapeSourceError("HTTP 503 for /search")):
            for _ in range(sf.BREAKER_HARD_FAILURES):
                with self.assertRaises(sf.SourceUnavailable):
                    chain.search(sf.PROVIDER_SUBF2ME, IDENTITY)
        self.assertTrue(chain.health[sf.PROVIDER_SUBF2ME].disabled)
        self.assertIn("consecutive hard failures", chain.status()[sf.PROVIDER_SUBF2ME])

    def test_a_layout_the_parser_did_not_expect_is_counted_separately(self) -> None:
        """A crash in an adapter is a parse failure, not a site outage."""
        chain = self.chain()
        with self.source_raising(AttributeError("'NoneType' object has no attribute 'group'")):
            for _ in range(sf.BREAKER_PARSE_FAILURES):
                with self.assertRaises(sf.SourceUnavailable) as caught:
                    chain.search(sf.PROVIDER_SUBF2ME, IDENTITY)
        self.assertIn("unparseable response", str(caught.exception))
        self.assertTrue(chain.health[sf.PROVIDER_SUBF2ME].disabled)
        self.assertIn("repeated parse failures", chain.status()[sf.PROVIDER_SUBF2ME])

    def test_a_download_that_is_not_a_subtitle_is_the_candidate_s_fault(self) -> None:
        """A refused candidate must not count against the source's health."""
        class NotASubtitle(sf.BaseSource):
            key = sf.PROVIDER_SUBF2ME
            label = "Subf2m.co"

            def fetch(self, candidate, t):  # noqa: ANN001, ARG002
                return b"<html>an error page</html>"

        chain = self.chain()
        candidate = sf.ScrapeCandidate(provider=sf.PROVIDER_SUBF2ME, file_id="1")
        with (mock.patch.dict(sf.SCRAPE_SOURCES, {sf.PROVIDER_SUBF2ME: NotASubtitle()}),
              self.assertRaises(sf.CandidateRejected)):
            chain.fetch(sf.PROVIDER_SUBF2ME, candidate)
        self.assertEqual(chain.health[sf.PROVIDER_SUBF2ME].hard_failures, 0)

    def test_a_source_that_refuses_its_own_candidate_stays_healthy(self) -> None:
        chain = self.chain()
        candidate = sf.ScrapeCandidate(provider=sf.PROVIDER_SUBF2ME, file_id="1")
        with (self.source_raising(sf.CandidateRejected("bulgarian, not english")),
              self.assertRaises(sf.CandidateRejected)):
            chain.fetch(sf.PROVIDER_SUBF2ME, candidate)
        health = chain.health[sf.PROVIDER_SUBF2ME]
        self.assertEqual((health.hard_failures, health.parse_failures), (0, 0))

    def test_a_failed_download_counts_against_the_source(self) -> None:
        chain = self.chain()
        candidate = sf.ScrapeCandidate(provider=sf.PROVIDER_SUBF2ME, file_id="1")
        with (self.source_raising(sf.ScrapeSourceError("HTTP 500 for /dl")),
              self.assertRaises(sf.SourceUnavailable)):
            chain.fetch(sf.PROVIDER_SUBF2ME, candidate)
        self.assertEqual(chain.health[sf.PROVIDER_SUBF2ME].hard_failures, 1)

    def test_a_source_nobody_configured_is_not_searched(self) -> None:
        chain = self.chain()
        with self.assertRaises(ValueError):
            chain.search("a-site-that-does-not-exist", IDENTITY)

    def test_downloading_from_a_source_nobody_configured_is_refused(self) -> None:
        chain = self.chain()
        candidate = sf.ScrapeCandidate(provider="a-site-that-does-not-exist", file_id="1")
        with self.assertRaises(ValueError) as caught:
            chain.fetch("a-site-that-does-not-exist", candidate)
        self.assertIn("unknown scraped source", str(caught.exception))

    def test_a_source_switched_off_this_run_is_not_downloaded_from(self) -> None:
        """The breaker covers the download leg too, not only the search leg."""
        chain = self.chain()
        chain.health[sf.PROVIDER_SUBF2ME].disabled_reason = "3 consecutive hard failures"
        candidate = sf.ScrapeCandidate(provider=sf.PROVIDER_SUBF2ME, file_id="1")
        with self.assertRaises(sf.SourceUnavailable) as caught:
            chain.fetch(sf.PROVIDER_SUBF2ME, candidate)
        self.assertIn("source disabled this run", str(caught.exception))
        self.assertIn("3 consecutive hard failures", str(caught.exception))

    def test_a_download_leg_that_crashes_the_adapter_is_a_parse_failure(self) -> None:
        chain = self.chain()
        candidate = sf.ScrapeCandidate(provider=sf.PROVIDER_SUBF2ME, file_id="1")
        with (self.source_raising(AttributeError("'NoneType' has no attribute 'group'")),
              self.assertRaises(sf.SourceUnavailable) as caught):
            chain.fetch(sf.PROVIDER_SUBF2ME, candidate)
        self.assertIn("unparseable response", str(caught.exception))
        health = chain.health[sf.PROVIDER_SUBF2ME]
        self.assertEqual((health.parse_failures, health.hard_failures), (1, 0))

    def test_a_disabled_source_drops_out_of_the_enabled_list(self) -> None:
        chain = self.chain()
        self.assertEqual(chain.enabled_keys(), [sf.PROVIDER_SUBF2ME, sf.PROVIDER_PODNAPISI])
        chain.health[sf.PROVIDER_SUBF2ME].disabled_reason = "3 consecutive hard failures"
        self.assertEqual(chain.enabled_keys(), [sf.PROVIDER_PODNAPISI])

    def test_the_daily_cap_stops_a_search_before_it_leaves(self) -> None:
        chain = sf.ScrapeChain(keys=(sf.PROVIDER_SUBF2ME,), transport=Pages(),
                               search_caps={sf.PROVIDER_SUBF2ME: 1},
                               reserved={sf.PROVIDER_SUBF2ME: 1})
        with self.assertRaises(sf.SourceUnavailable) as caught:
            chain.search(sf.PROVIDER_SUBF2ME, IDENTITY)
        self.assertIn("daily search cap", str(caught.exception))


class WalkingTheChainTests(unittest.TestCase):
    """`run_scrape_chain` is the order, the reasons, and the first good answer."""

    def setUp(self) -> None:
        self.reasons: list[tuple[str, str]] = []

    def walk(self, chain: sf.ScrapeChain, keys: tuple[str, ...]) -> tuple[Any, str, Any]:
        return sf.run_scrape_chain(
            IDENTITY, keys=keys, chain=chain,
            on_reason=lambda key, why: self.reasons.append((key, why)),
        )

    def chain_of(self, sources: dict[str, sf.BaseSource]) -> tuple[sf.ScrapeChain, mock._patch]:
        keys = tuple(sources)
        chain = sf.ScrapeChain(keys=keys, transport=Pages())
        return chain, mock.patch.dict(sf.SCRAPE_SOURCES, sources)

    def source(self, key: str, *, candidates: list[sf.ScrapeCandidate] | None = None,
               payload: Any = None) -> sf.BaseSource:
        class Canned(sf.BaseSource):
            def __init__(self) -> None:
                self.key = key
                self.label = key

            def search(self, identity, t):  # noqa: ANN001, ARG002
                return list(candidates or [])

            def fetch(self, candidate, t):  # noqa: ANN001, ARG002
                if isinstance(payload, Exception):
                    raise payload
                return payload if payload is not None else SRT.encode("utf-8")

        return Canned()

    def candidate(self, key: str, file_id: str = "1") -> sf.ScrapeCandidate:
        return sf.ScrapeCandidate(provider=key, file_id=file_id,
                                  release="The.Dark.Knight.2008.1080p.BluRay",
                                  feature_title="The Dark Knight", feature_year=2008)

    def test_the_first_source_with_an_answer_ends_the_walk(self) -> None:
        first, second = sf.PROVIDER_SUBF2ME, sf.PROVIDER_PODNAPISI
        chain, patch = self.chain_of({
            first: self.source(first, candidates=[self.candidate(first)]),
            second: self.source(second, candidates=[self.candidate(second)]),
        })
        with patch:
            cand, key, raw = self.walk(chain, (first, second))
        self.assertEqual(key, first)
        assert cand is not None and raw is not None
        self.assertEqual(raw.decode("utf-8"), SRT)
        self.assertEqual(self.reasons, [], "nothing to explain when the first source answers")

    def test_a_disabled_source_is_reported_and_skipped(self) -> None:
        key = sf.PROVIDER_SUBF2ME
        chain, patch = self.chain_of({key: self.source(key, candidates=[self.candidate(key)])})
        chain.health[key].disabled_reason = "3 consecutive hard failures"
        with patch:
            cand, _key, _raw = self.walk(chain, (key,))
        self.assertIsNone(cand)
        self.assertEqual(self.reasons, [(key, "disabled: 3 consecutive hard failures")])

    def test_a_source_that_is_not_in_this_run_is_reported_as_not_enabled(self) -> None:
        chain = sf.ScrapeChain(keys=(sf.PROVIDER_SUBF2ME,), transport=Pages())
        cand, _key, _raw = self.walk(chain, (sf.PROVIDER_PODNAPISI,))
        self.assertIsNone(cand)
        self.assertEqual(self.reasons, [(sf.PROVIDER_PODNAPISI, "not enabled")])

    def test_a_source_with_nothing_for_this_movie_says_so(self) -> None:
        key = sf.PROVIDER_SUBF2ME
        chain, patch = self.chain_of({key: self.source(key, candidates=[])})
        with patch:
            cand, _key, _raw = self.walk(chain, (key,))
        self.assertIsNone(cand)
        self.assertEqual(self.reasons, [(key, "no matching English subtitle")])

    def test_a_refused_candidate_moves_on_to_the_next_one(self) -> None:
        key = sf.PROVIDER_SUBF2ME
        cands = [self.candidate(key, "1"), self.candidate(key, "2")]

        class SecondOneWorks(sf.BaseSource):
            def __init__(self) -> None:
                self.key = key
                self.label = key

            def search(self, identity, t):  # noqa: ANN001, ARG002
                return cands

            def fetch(self, candidate, t):  # noqa: ANN001, ARG002
                if candidate.file_id == "1":
                    return b"<html>an error page</html>"
                return SRT.encode("utf-8")

        chain = sf.ScrapeChain(keys=(key,), transport=Pages())
        with mock.patch.dict(sf.SCRAPE_SOURCES, {key: SecondOneWorks()}):
            cand, _key, raw = self.walk(chain, (key,))
        assert cand is not None and raw is not None
        self.assertEqual(cand.file_id, "2")
        self.assertEqual(self.reasons, [(key, "candidate refused: payload is not a valid SRT")])

    def test_a_source_that_breaks_mid_download_is_left_for_the_next_one(self) -> None:
        first, second = sf.PROVIDER_SUBF2ME, sf.PROVIDER_PODNAPISI
        chain, patch = self.chain_of({
            first: self.source(first, candidates=[self.candidate(first), self.candidate(first, "2")],
                               payload=sf.ScrapeSourceError("HTTP 503 for /dl")),
            second: self.source(second, candidates=[self.candidate(second)]),
        })
        with patch:
            cand, key, _raw = self.walk(chain, (first, second))
        assert cand is not None
        self.assertEqual(key, second)
        self.assertEqual(len(self.reasons), 1, "the broken source is only reported once")
        self.assertIn("HTTP 503", self.reasons[0][1])

    def test_every_candidate_failing_is_explained(self) -> None:
        key = sf.PROVIDER_SUBF2ME
        chain, patch = self.chain_of({
            key: self.source(key, candidates=[self.candidate(key)],
                             payload=b"<html>an error page</html>"),
        })
        with patch:
            cand, _key, _raw = self.walk(chain, (key,))
        self.assertIsNone(cand)
        self.assertIn("none produced a valid English SRT", self.reasons[-1][1])


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
