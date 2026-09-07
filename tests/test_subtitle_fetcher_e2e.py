"""A whole fetcher run, from an uncovered library to a validated sidecar.

`subtitle_fetcher.py` is the largest tool here and the only one that reaches
the internet. Its planners are tested as pure functions and its provider
clients are tested against canned payloads, but `queue_run` — the 650-line
orchestrator that ties triage, quotas, provider calls, validation and the
report together — had almost no coverage, because reaching it needs a library
and a provider.

These tests supply both: a real movie library in a temporary directory, and
`tests/fake_provider.py` installed at `urllib.request.urlopen`, so the real
client builds the real request and the real run decides what to do with the
answer. What they pin down is the behaviour an operator depends on:

* a movie only ever ends up with a subtitle that was *validated*;
* a download reservation is spent at most once per movie per day, and
  survives into tomorrow's run through the ledger;
* a run that leaves movies uncovered says so in its exit code.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fake_provider as fake

import subtitle_fetcher as sf
from organizekit.core import LockTimeoutError

GOOD_RELEASE = "The.Dark.Knight.2008.1080p.BluRay.x264-GROUP"
BIG = 2 * 1024 * 1024

# Provider settings a developer may have exported; a run reads them.
ENV_KEYS = ("OPENSUBTITLES_API_KEY", "SUBDL_API_KEY", "OPENSUBTITLES_USERNAME",
            "OPENSUBTITLES_PASSWORD")

VALID_SIDECAR = (
    "1\n"
    "00:00:02,000 --> 00:00:05,000\n"
    "A subtitle that was already here.\n"
)


class FetcherRunFixture(unittest.TestCase):
    """A library of uncovered movies and a provider that answers."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sf_e2e_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()
        self.library = self.root / "Movies"
        self.library.mkdir()
        self.log = self.root / "out" / "fetcher.log"
        self.report = self.root / "out" / "report.txt"

        env = dict.fromkeys(ENV_KEYS, "")
        env["OPENSUBTITLES_API_KEY"] = "test-api-key"
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        # The client paces itself at one request per host per 1.1s and sleeps
        # between HTTP retries. Both are arithmetic tested elsewhere; here they
        # would only add wall-clock time.
        for name, value in (("REQUEST_GAP_SEC", 0.0), ("SCRAPE_REQUEST_GAP_SEC", 0.0)):
            gap = mock.patch.object(sf, name, value)
            gap.start()
            self.addCleanup(gap.stop)
        sleep = mock.patch.object(sf.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

        self.provider = fake.FakeOpenSubtitles(
            hash_results=[fake.subtitle(9001, GOOD_RELEASE, downloads=900,
                                        title="The Dark Knight", year=2008)],
        )

    # -- building a library -------------------------------------------------

    def movie(self, name: str = "The Dark Knight (2008)", size: int = BIG) -> Path:
        path = self.library / name / f"{name}.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.truncate(size)
        return path

    def sidecar_of(self, video: Path, suffix: str = ".eng.srt") -> Path:
        return video.with_name(video.stem + suffix)

    # -- running ------------------------------------------------------------

    def run_main(self, *extra: str, provider: object | None = None) -> int:
        argv = ["--source", str(self.library), "--log", str(self.log),
                "--report", str(self.report), "--min-size", "1",
                "--no-extract", "--scrape-daily-cap", "0", "--workers", "1",
                *extra]
        target = self.provider if provider is None else provider
        with mock.patch.object(sf.urllib.request, "urlopen", target), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = sf.main(argv)
        self.stdout, self.stderr = out.getvalue(), err.getvalue()
        return code

    # -- looking at the result ---------------------------------------------

    def report_text(self) -> str:
        return self.report.read_text(encoding="utf-8") if self.report.exists() else ""

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def ledger(self) -> dict:
        """The last durable quota checkpoint the run appended to the log."""
        events = [line.split(sf.LEDGER_EVENT, 1)[1].strip()
                  for line in self.log_text().splitlines() if sf.LEDGER_EVENT in line]
        if not events:
            return {}
        state = json.loads(events[-1])
        days = state.get("days") or {}
        return days.get(sf.utc_day(), {})

    def sidecars(self) -> list[str]:
        return sorted(p.relative_to(self.library).as_posix()
                      for p in self.library.rglob("*.srt"))


class CoveringAMovieTests(FetcherRunFixture):
    """The path that ends with a subtitle file on disk."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def test_a_hash_match_is_downloaded_and_written(self) -> None:
        self.assertEqual(self.run_main(), 0)
        sidecar = self.sidecar_of(self.video)
        self.assertTrue(sidecar.is_file())
        self.assertEqual(sidecar.read_text(encoding="utf-8"), fake.SRT_TEXT)

    def test_the_provider_was_asked_with_the_movie_hash(self) -> None:
        self.assertEqual(self.run_main(), 0)
        self.assertTrue(any("moviehash=" in path for _m, path in self.provider.calls))
        self.assertEqual(self.provider.count("/download"), 1)

    def test_the_download_is_recorded_in_the_ledger(self) -> None:
        self.assertEqual(self.run_main(), 0)
        ledger = self.ledger()
        self.assertEqual(ledger.get("opensubtitles_download_requests_reserved"), 1)
        self.assertEqual(ledger.get("opensubtitles_successful_downloads"), 1)

    def test_the_next_run_costs_no_provider_request(self) -> None:
        """Coverage is the point: a covered movie is never bought twice."""
        self.assertEqual(self.run_main(), 0)
        second = fake.FakeOpenSubtitles(hash_results=self.provider.hash_results)
        self.assertEqual(self.run_main(provider=second), 0)
        self.assertEqual(second.calls, [])

    def test_an_existing_valid_sidecar_is_left_exactly_as_it_is(self) -> None:
        self.sidecar_of(self.video).write_text(VALID_SIDECAR, encoding="utf-8")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.sidecar_of(self.video).read_text(encoding="utf-8"),
                         VALID_SIDECAR)
        self.assertEqual(self.provider.calls, [])

    def test_a_legacy_en_srt_is_promoted_rather_than_re_downloaded(self) -> None:
        self.sidecar_of(self.video, ".en.srt").write_text(VALID_SIDECAR, encoding="utf-8")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.sidecars(),
                         ["The Dark Knight (2008)/The Dark Knight (2008).eng.srt"])
        self.assertEqual(self.provider.calls, [])

    def test_the_report_and_the_log_say_what_happened(self) -> None:
        self.assertEqual(self.run_main(), 0)
        text = self.report_text()
        self.assertIn("The Dark Knight (2008)", text)
        self.assertIn("DOWNLOADED DURING THIS RUN", text)
        self.assertIn(GOOD_RELEASE, self.log_text(),
                      "the release that was chosen is on the record")


class WhatItRefusesToInstallTests(FetcherRunFixture):
    """A wrong subtitle is worse than no subtitle: every refusal, end to end."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def assert_uncovered(self, code: int = 1) -> None:
        self.assertEqual(self.sidecars(), [], "nothing was written")
        self.assertEqual(self.provider.count("/download"), 0, "no quota was spent")
        self.assertEqual(code, 1, "an uncovered movie is a non-zero exit")

    def test_a_release_with_no_bluray_keyword_is_not_auto_selected(self) -> None:
        self.provider.hash_results = [
            fake.subtitle(1, "The.Dark.Knight.2008.CAM.x264", title="The Dark Knight",
                          year=2008)]
        self.assert_uncovered(self.run_main())

    def test_a_machine_translated_subtitle_is_never_installed(self) -> None:
        self.provider.hash_results = [
            fake.subtitle(1, GOOD_RELEASE, machine_translated=True,
                          title="The Dark Knight", year=2008)]
        self.assert_uncovered(self.run_main())

    def test_a_non_english_subtitle_is_never_installed(self) -> None:
        self.provider.hash_results = [
            fake.subtitle(1, GOOD_RELEASE, language="es", title="The Dark Knight",
                          year=2008)]
        self.assert_uncovered(self.run_main())

    def test_a_provider_with_nothing_to_offer_leaves_the_movie_uncovered(self) -> None:
        self.provider.hash_results = []
        self.assert_uncovered(self.run_main())

    def test_a_payload_that_is_not_a_subtitle_is_rejected(self) -> None:
        """The bytes are checked after the download, not assumed."""
        self.provider.srt_text = "<html><body>Download limit reached</body></html>"
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])
        self.assertEqual(self.provider.count("/download"), 1,
                         "the reservation was still spent, and is still recorded")
        self.assertEqual(self.ledger().get("opensubtitles_download_requests_reserved"), 1)

    def test_a_non_https_download_link_is_refused(self) -> None:
        self.provider.download_link = "http://dl.opensubtitles.example/subtitle.srt"
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])

    def test_a_movie_replaced_mid_fetch_does_not_get_the_subtitle(self) -> None:
        """The subtitle was chosen for the bytes that were there when it started."""
        def rewrite_the_movie() -> None:
            with self.video.open("wb") as handle:
                handle.truncate(BIG + 4096)

        self.provider.on_download = rewrite_the_movie
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])
        self.assertIn("changed", self.report_text().casefold())

    def test_an_unreadable_existing_sidecar_is_held_for_review(self) -> None:
        """Overwriting it might destroy a hand-corrected file."""
        broken = self.sidecar_of(self.video)
        broken.write_text("this is not a subtitle at all", encoding="utf-8")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(broken.read_text(encoding="utf-8"),
                         "this is not a subtitle at all")
        self.assertEqual(self.provider.calls, [])
        self.assertIn("exists but is unusable", self.report_text())
        self.assertIn("delete it and re-run", self.report_text())


class QuotaTests(FetcherRunFixture):
    """The daily cap is a promise to the provider, kept across runs."""

    def setUp(self) -> None:
        super().setUp()
        self.first = self.movie("The Dark Knight (2008)")
        self.second = self.movie("Heat (1995)")
        self.provider.hash_results = [
            fake.subtitle(9001, GOOD_RELEASE, downloads=900,
                          title="The Dark Knight", year=2008),
        ]

    def test_the_cap_stops_the_run_before_the_second_download(self) -> None:
        self.assertEqual(self.run_main("--daily-cap", "1"), 1)
        self.assertEqual(self.provider.count("/download"), 1)
        self.assertEqual(self.ledger().get("opensubtitles_download_requests_reserved"), 1)

    def test_tomorrows_reservation_is_not_todays(self) -> None:
        """A second run on the same UTC day reads the ledger and defers."""
        self.assertEqual(self.run_main("--daily-cap", "1"), 1)
        second = fake.FakeOpenSubtitles(hash_results=self.provider.hash_results)
        self.assertEqual(self.run_main("--daily-cap", "1", provider=second), 1)
        self.assertEqual(second.count("/download"), 0)
        self.assertIn("cap", self.report_text().casefold())

    def test_a_deferred_movie_is_named_in_the_report(self) -> None:
        self.assertEqual(self.run_main("--daily-cap", "1"), 1)
        self.assertIn("Heat (1995)", self.report_text())

    def test_allow_missing_turns_an_uncovered_library_into_a_clean_exit(self) -> None:
        self.assertEqual(self.run_main("--daily-cap", "1", "--allow-missing"), 0)


class DryRunTests(FetcherRunFixture):
    """A rehearsal may search, but may not spend or write."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def test_nothing_is_downloaded_or_written(self) -> None:
        self.run_main("--dry-run", "--allow-missing")
        self.assertEqual(self.sidecars(), [])
        self.assertEqual(self.provider.count("/download"), 0)

    def test_the_candidate_it_would_have_taken_is_named(self) -> None:
        self.run_main("--dry-run", "--allow-missing")
        self.assertIn(GOOD_RELEASE, self.report_text())

    def test_no_reservation_is_recorded(self) -> None:
        self.run_main("--dry-run", "--allow-missing")
        self.assertEqual(self.ledger().get("opensubtitles_download_requests_reserved", 0), 0)


class ProviderFailureTests(FetcherRunFixture):
    """The provider is a service on the internet: it fails in every way."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def test_a_rate_limit_is_retried_and_then_succeeds(self) -> None:
        self.provider.fail("/subtitles", 429)
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.provider.count("/subtitles"), 2)
        self.assertTrue(self.sidecar_of(self.video).is_file())

    def test_a_provider_that_stays_down_does_not_take_the_run_with_it(self) -> None:
        self.provider.fail("/subtitles", 500, 500, 500, 500)
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])
        self.assertTrue(self.report.is_file(), "the report is still published")

    def test_an_error_is_not_forgiven_by_allow_missing(self) -> None:
        """--allow-missing accepts a gap in coverage, not a broken run."""
        self.provider.fail("/subtitles", 500, 500, 500, 500)
        self.assertEqual(self.run_main("--allow-missing"), 1)

    def test_a_network_error_is_reported_not_raised(self) -> None:
        self.provider.fail("/subtitles", ("urlerror", "connection refused"),
                           ("urlerror", "connection refused"),
                           ("urlerror", "connection refused"),
                           ("urlerror", "connection refused"))
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])

    def test_a_nonsense_response_body_is_reported_not_parsed(self) -> None:
        self.provider.fail("/subtitles", "garbage")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])

    def test_a_failed_download_leaves_no_partial_sidecar(self) -> None:
        self.provider.fail("/download", 503, 503, 503, 503)
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])


class LibraryShapeTests(FetcherRunFixture):
    """What counts as a movie to fetch for."""

    def test_a_small_file_is_not_a_movie(self) -> None:
        self.movie("Trailer (2008)", size=64 * 1024)
        self.assertEqual(self.run_main("--allow-missing"), 0)
        self.assertEqual(self.provider.calls, [])

    def test_the_limit_stops_after_n_movies(self) -> None:
        """One movie is looked at; the rest of the library is not touched."""
        self.movie("The Dark Knight (2008)")
        self.movie("Heat (1995)")
        self.assertEqual(self.run_main("--limit", "1", "--allow-missing"), 0)
        self.assertIn("Found 1 eligible movies", self.log_text())
        self.assertNotIn("The Dark Knight (2008)", self.report_text(),
                         "discovery is sorted, so only Heat was in this batch")

    def test_an_empty_library_is_a_clean_run(self) -> None:
        self.assertEqual(self.run_main(), 0)
        self.assertTrue(self.report.is_file())


class ConfigurationRefusalTests(FetcherRunFixture):
    """Settings that would make the run pointless or unsafe."""

    def refuse(self, *extra: str) -> str:
        self.assertEqual(self.run_main(*extra), 2)
        return self.stderr

    def test_a_missing_library_is_refused(self) -> None:
        self.library.rmdir()
        self.assertIn("--source", self.refuse())

    def test_user_auth_without_credentials_is_refused(self) -> None:
        self.assertIn("username and password",
                      self.refuse("--auth-mode", "user"))

    def test_no_provider_and_no_scraping_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {"OPENSUBTITLES_API_KEY": "",
                                          "SUBDL_API_KEY": ""}):
            said = self.refuse()
        self.assertIn("OPENSUBTITLES_API_KEY", said)

    def test_a_negative_worker_count_is_refused(self) -> None:
        self.assertIn("--workers", self.refuse("--workers", "-1"))


class RunEndingTests(FetcherRunFixture):
    """How the run ends, and what it tells whatever started it."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def test_ctrl_c_exits_130(self) -> None:
        with mock.patch.object(sf, "queue_run", side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_main(), 130)

    def test_an_unexpected_crash_leaves_through_an_exit_code(self) -> None:
        with mock.patch.object(sf, "queue_run", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_main(), 1)
        self.assertIn("boom", self.stderr)

    def test_another_fetcher_holding_the_lock_is_refused(self) -> None:
        with mock.patch.object(sf, "CoordinationLock",
                               side_effect=LockTimeoutError("held by pid 7")):
            self.assertEqual(self.run_main(), 1)

    def test_the_uncovered_count_is_explained_on_stderr(self) -> None:
        self.provider.hash_results = []
        self.assertEqual(self.run_main(), 1)
        self.assertIn("Coverage incomplete", self.stderr)
        self.assertIn("--allow-missing", self.stderr)


class SubdlFixture(FetcherRunFixture):
    """A run with both providers configured, answered by both fakes."""

    def setUp(self) -> None:
        super().setUp()
        key = mock.patch.dict(os.environ, {"SUBDL_API_KEY": "test-subdl-key"})
        key.start()
        self.addCleanup(key.stop)
        self.subdl = fake.FakeSubdl(
            release_results=[fake.subdl_subtitle("sub123", GOOD_RELEASE, downloads=400)],
        )
        self.provider.hash_results = []
        self.both = fake.FakeProviders(self.provider, self.subdl)

    def run_main(self, *extra: str, provider: object | None = None) -> int:
        return super().run_main(*extra, provider=self.both if provider is None else provider)


class SubdlTests(SubdlFixture):
    """SubDL is an equal source, metered separately, and gated on its score."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def test_subdl_covers_a_movie_opensubtitles_could_not(self) -> None:
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.sidecar_of(self.video).read_text(encoding="utf-8"),
                         fake.SRT_TEXT)
        self.assertEqual(self.subdl.count("download"), 1)

    def test_a_low_score_release_match_is_not_treated_as_one(self) -> None:
        """Below 0.80 the provider is not claiming this is the same release."""
        self.subdl.release_results = [
            fake.subdl_subtitle("sub123", GOOD_RELEASE, match_score=0.4)]
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])

    def test_subtitles_filed_under_a_different_movie_are_refused(self) -> None:
        """The provider's own identity record has to be this movie."""
        self.subdl.title = "Heat"
        self.subdl.year = 1995
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])
        self.assertEqual(self.subdl.count("download"), 0)

    def test_a_non_english_subdl_result_is_refused(self) -> None:
        self.subdl.release_results = [
            fake.subdl_subtitle("sub123", GOOD_RELEASE, language="es")]
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])

    def test_an_explicitly_non_srt_result_is_refused(self) -> None:
        self.subdl.release_results = [
            fake.subdl_subtitle("sub123", GOOD_RELEASE, media_format="ass")]
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])

    def test_the_search_cap_is_metered_apart_from_the_download_cap(self) -> None:
        """SubDL publishes two quotas; spending one must not spend the other."""
        self.assertEqual(self.run_main(), 0)
        ledger = self.ledger()
        self.assertEqual(ledger.get("subdl_search_requests_reserved"), 1)
        self.assertEqual(ledger.get("subdl_download_requests_reserved"), 1)

    def test_an_exhausted_search_cap_defers_instead_of_searching(self) -> None:
        self.assertEqual(self.run_main("--subdl-search-daily-cap", "1"), 0)
        self.movie("Heat (1995)")
        self.subdl.calls.clear()
        self.assertEqual(self.run_main("--subdl-search-daily-cap", "1"), 1)
        self.assertEqual(self.subdl.count("/files/search"), 0)
        self.assertIn("search cap", self.report_text().casefold())

    def test_an_exhausted_download_cap_defers_the_next_movie(self) -> None:
        """And it defers it *before* the lookup: a search it cannot use is waste."""
        self.movie("Heat (1995)")
        self.subdl.add_movie("Heat", 1995, release_results=[
            fake.subdl_subtitle("sub999", "Heat.1995.1080p.BluRay.x264-GROUP")])
        self.assertEqual(self.run_main("--subdl-daily-cap", "1"), 1)
        self.assertEqual(self.subdl.count("download"), 1)
        self.assertEqual(len(self.sidecars()), 1)
        self.assertIn("cap exhausted before lookup", self.report_text())

    def test_a_subdl_outage_does_not_stop_the_library(self) -> None:
        self.movie("Heat (1995)")
        self.subdl.fail("/files/search", 500, 500, 500, 500)
        self.assertEqual(self.run_main(), 1)
        self.assertTrue(self.report.is_file())

    def test_a_rejected_search_is_reported_as_an_error(self) -> None:
        self.subdl.fail("/files/search", ("rejected", "invalid api key"))
        self.assertEqual(self.run_main(), 1)
        self.assertIn("subdl", self.report_text().casefold())

    def test_a_dry_run_spends_neither_subdl_quota(self) -> None:
        self.assertEqual(self.run_main("--dry-run", "--allow-missing"), 0)
        self.assertEqual(self.sidecars(), [])
        self.assertEqual(self.subdl.count("download"), 0)
        self.assertEqual(self.ledger().get("subdl_download_requests_reserved", 0), 0)


class BothProvidersTests(SubdlFixture):
    """The pooling rule: whichever source has the better release, wins."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def test_the_most_downloaded_qualifying_release_wins_whoever_has_it(self) -> None:
        """Equal sources means exactly that: the provider is not the tiebreak."""
        self.provider.hash_results = [
            fake.subtitle(9001, GOOD_RELEASE, downloads=10,
                          title="The Dark Knight", year=2008)]
        self.subdl.release_results = [
            fake.subdl_subtitle("sub123", GOOD_RELEASE, downloads=400)]
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.subdl.count("download"), 1, "SubDL had the popular one")
        self.assertEqual(self.provider.count("/download"), 0)

    def test_and_the_same_rule_sends_the_job_to_opensubtitles(self) -> None:
        self.provider.hash_results = [
            fake.subtitle(9001, GOOD_RELEASE, downloads=9000,
                          title="The Dark Knight", year=2008)]
        self.subdl.release_results = [
            fake.subdl_subtitle("sub123", GOOD_RELEASE, downloads=400)]
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.provider.count("/download"), 1)
        self.assertEqual(self.subdl.count("download"), 0)

    def test_subdl_is_still_asked_when_opensubtitles_has_nothing(self) -> None:
        self.assertEqual(self.run_main(), 0)
        self.assertTrue(self.provider.count("/subtitles") >= 1)
        self.assertEqual(self.subdl.count("download"), 1)

    def test_with_opensubtitles_capped_the_run_falls_through_to_subdl(self) -> None:
        """One provider's exhausted quota is not the library's problem."""
        self.movie("Heat (1995)")
        self.provider.hash_results = [
            fake.subtitle(9001, GOOD_RELEASE, downloads=9000,
                          title="The Dark Knight", year=2008)]
        self.subdl.add_movie("Heat", 1995, release_results=[
            fake.subdl_subtitle("sub999", "Heat.1995.1080p.BluRay.x264-GROUP")])
        self.assertEqual(self.run_main("--daily-cap", "1"), 0)
        self.assertEqual(len(self.sidecars()), 2, "one from each provider")

    def test_the_report_names_both_sources(self) -> None:
        self.assertEqual(self.run_main(), 0)
        text = self.report_text()
        self.assertIn("OpenSubtitles", text)
        self.assertIn("SubDL", text)


class ScrapingTierFixture(FetcherRunFixture):
    """Both APIs empty, so the run falls through to the scraped sources."""

    def setUp(self) -> None:
        super().setUp()
        self.provider.hash_results = []
        self.provider.identity_results = []
        self.sites = fake.FakeSites(fake.subf2m_pages())
        self.all = fake.FakeProviders(self.provider, fake.FakeSubdl(), self.sites)
        self.video = self.movie()

    def run_main(self, *extra: str, provider: object | None = None) -> int:
        return super().run_main("--scrape-daily-cap", "50", *extra,
                                provider=self.all if provider is None else provider)


class ScrapingTierTests(ScrapingTierFixture):
    """The third tier: seven keyless sites, tried in order, one search each."""

    def test_a_scraped_site_can_cover_a_movie_no_api_could(self) -> None:
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.sidecar_of(self.video).read_text(encoding="utf-8"),
                         fake.SRT_TEXT)
        self.assertIn("Subf2m.co", self.report_text())

    def test_the_chain_stops_at_the_first_site_that_answers(self) -> None:
        """Six requests not made is six sites not annoyed."""
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.sites.hosts(), ["subf2m.co"])

    def test_when_a_site_has_nothing_the_next_one_is_asked(self) -> None:
        self.sites.pages = {}
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(len(self.sites.hosts()), 7, self.sites.hosts())
        self.assertEqual(self.sidecars(), [])
        self.assertIn("HELD FOR MANUAL REVIEW", self.report_text())

    def test_a_skipped_site_is_never_contacted(self) -> None:
        self.assertEqual(self.run_main("--skip-source", "subf2me"), 1)
        self.assertNotIn("subf2m.co", self.sites.hosts())
        self.assertEqual(self.sidecars(), [])

    def test_a_zero_cap_turns_the_whole_tier_off(self) -> None:
        self.assertEqual(self.run_main("--scrape-daily-cap", "0"), 1)
        self.assertEqual(self.sites.calls, [])

    def test_a_dry_run_asks_no_site_anything(self) -> None:
        self.assertEqual(self.run_main("--dry-run", "--allow-missing"), 0)
        self.assertEqual(self.sites.calls, [])
        self.assertEqual(self.sidecars(), [])

    def test_each_site_search_is_reserved_in_the_durable_ledger(self) -> None:
        """One reservation per source per movie, written before the request."""
        self.sites.pages = {}
        self.assertEqual(self.run_main(), 1)
        ledger = self.ledger()
        for key in sf.SCRAPE_PROVIDER_ORDER:
            self.assertEqual(ledger.get(f"{key}_search_requests_reserved"), 1, key)

    def test_yesterdays_reservations_are_still_spent_today(self) -> None:
        self.sites.pages = {}
        self.assertEqual(self.run_main("--scrape-daily-cap", "1"), 1)
        self.movie("Heat (1995)")
        self.sites.calls.clear()
        self.assertEqual(self.run_main("--scrape-daily-cap", "1"), 1)
        self.assertEqual(self.sites.calls, [], "every source's cap was already spent")

    def test_a_site_that_keeps_failing_is_dropped_for_the_rest_of_the_run(self) -> None:
        """Three hard failures and the breaker stops paying for that source."""
        self.sites.pages = {}
        for name in ("Heat (1995)", "Alien (1979)", "Jaws (1975)", "Up (2009)"):
            self.movie(name)
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sites.count("/subtitles/searchbytitle"), 3)
        self.assertIn("disabled", self.report_text())


if __name__ == "__main__":
    unittest.main()
