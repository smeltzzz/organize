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
        return sorted(str(p.relative_to(self.library))
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


if __name__ == "__main__":
    unittest.main()
