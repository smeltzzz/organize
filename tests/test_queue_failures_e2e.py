"""One movie's bad day is not the run's: the queue's failure branches.

`queue_run` is the orchestrator, and most of what it contains is not the happy
path — it is the twenty-odd places where something goes wrong for *one* movie
and the run has to decide what that movie's status is, write it down, and move
on to the next one. A nightly job over 900 movies is only useful if it is
impossible for movie 40 to end the run.

The end-to-end suite covered the paths where a provider says no. These cover
the paths where something breaks: a movie that cannot be hashed, a movie that
changes while it is being hashed, a title search that goes down between the two
providers, a crash inside the scraped tier, a subtitle that appears from
somewhere else mid-download. Each test asks the same two questions: what
happened to *this* movie, and did the next movie still get its subtitle?

They also cover the two ways the run can be told to be stricter — no title/year
fallback, and a filename it cannot read an identity out of — because both end
in a refusal that has to be explained to a human rather than retried forever.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import fake_provider as fake
from test_subtitle_fetcher_e2e import (
    GOOD_RELEASE,
    FetcherRunFixture,
    ScrapingTierFixture,
    SubdlFixture,
)

import subtitle_fetcher as sf


class RememberedStatusMixin:
    """What the run wrote down about a movie, which is what tomorrow reads."""

    def remembered(self, video) -> dict:
        """The last durable record for ``video`` in the append-only ledger."""
        found: dict = {}
        for line in self.log_text().splitlines():  # type: ignore[attr-defined]
            if sf.LEDGER_EVENT not in line:
                continue
            payload = json.loads(line.split(sf.LEDGER_EVENT, 1)[1].strip())
            for record in (payload.get("movies") or {}).values():
                if record.get("path") == str(video):
                    found = record
        return found


class TheQueueKeepsGoingTests(RememberedStatusMixin, FetcherRunFixture):
    """Two movies, and the first one is trouble."""

    def setUp(self) -> None:
        super().setUp()
        self.first = self.movie("Alien (1979)")
        self.second = self.movie()  # The Dark Knight (2008), which the fake answers
        self.provider.hash_results = [
            fake.subtitle(9001, GOOD_RELEASE, downloads=900,
                          title="The Dark Knight", year=2008)]

    def hash_raising(self, exc: Exception) -> mock._patch:
        """Make the moviehash fail for the first movie only."""
        real = sf.moviehash

        def fake_hash(video, *args, **kwargs):
            if video == self.first:
                raise exc
            return real(video, *args, **kwargs)

        return mock.patch.object(sf, "moviehash", side_effect=fake_hash)

    def test_a_movie_too_small_to_hash_does_not_stop_the_queue(self) -> None:
        """`moviehash` raises ValueError below its minimum size.

        The size gate ran at scan time, so this means the file was truncated
        in between — a local problem with one movie, not a reason to stop.
        """
        with self.hash_raising(ValueError("file too small for moviehash")):
            self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), ["The Dark Knight (2008)/The Dark Knight (2008).eng.srt"])
        self.assertIn("file too small for moviehash", self.report_text())

    def test_a_movie_that_changes_while_it_is_hashed_is_not_matched(self) -> None:
        """The hash and the file it identifies have to be the same file."""
        real = sf.moviehash

        def hash_then_change(video, *args, **kwargs):
            digest = real(video, *args, **kwargs)
            if video == self.first:
                with video.open("ab") as handle:
                    handle.write(b"the movie was replaced mid-run")
            return digest

        with mock.patch.object(sf, "moviehash", side_effect=hash_then_change):
            self.assertEqual(self.run_main(), 1)
        self.assertIn("movie changed while calculating moviehash", self.report_text())
        self.assertEqual(self.sidecars(), ["The Dark Knight (2008)/The Dark Knight (2008).eng.srt"])

    def test_a_movie_that_vanishes_before_it_is_read_is_an_error(self) -> None:
        """Triage snapshots the file; if that fails, the movie is not fetchable.

        A scan and the work that follows it are minutes apart on a real
        library, and in between a movie can be moved, deleted or unmounted.
        The snapshot is what every provider transaction is fenced by, so
        failing to take one is the end of the road for that movie only.
        """
        real = sf.video_snapshot

        def vanishing(path, *args, **kwargs):
            if path == self.first:
                raise OSError("no such file or directory")
            return real(path, *args, **kwargs)

        with mock.patch.object(sf, "video_snapshot", side_effect=vanishing):
            self.assertEqual(self.run_main(), 1)
        self.assertIn("no such file or directory", self.report_text())
        self.assertEqual(self.sidecars(), ["The Dark Knight (2008)/The Dark Knight (2008).eng.srt"])


class WhenTheHashFindsNothingTests(FetcherRunFixture):
    """What the run does when the byte-exact route comes back empty."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()
        self.provider.hash_results = []
        self.provider.identity_results = [
            fake.subtitle(9003, GOOD_RELEASE, moviehash_match=False, downloads=700,
                          title="The Dark Knight", year=2008)]

    def test_with_the_fallback_off_a_missing_hash_match_is_a_no_match(self) -> None:
        self.assertEqual(self.run_main("--no-identity-fallback"), 1)
        self.assertEqual(self.sidecars(), [])
        self.assertIn("moviehash-matched", self.report_text())
        self.assertEqual(self.provider.count("query="), 1, "only the hash search was paid for")

    def test_with_the_fallback_off_a_hash_match_still_stands_alone(self) -> None:
        self.provider.hash_results = [
            fake.subtitle(9001, GOOD_RELEASE, downloads=900,
                          title="The Dark Knight", year=2008)]
        self.assertEqual(self.run_main("--no-identity-fallback"), 0)
        self.assertEqual(self.sidecar_of(self.video).read_text(encoding="utf-8"),
                         fake.SRT_TEXT)

    def test_a_filename_with_no_identity_in_it_is_held_for_review(self) -> None:
        """Without `Title (Year)` there is nothing to search by, and guessing is worse."""
        self.movie("dark knight 1080p")
        self.video.parent.rename(self.video.parent.with_name("gone"))
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])
        self.assertIn("canonical Title (Year)", self.report_text())


class WhenTheTitleSearchGoesDownTests(SubdlFixture):
    """The second tier is two providers; one of them failing is not the answer."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()
        self.provider.hash_results = []
        self.provider.identity_results = [
            fake.subtitle(9003, GOOD_RELEASE, moviehash_match=False,
                          title="The Dark Knight", year=2008)]
        # "Knight+2008" appears only in the title/year query; the hash search
        # sends the filename, where the year is parenthesised.
        self.provider.fail("Knight+2008", *([500] * 8))

    def test_the_other_provider_still_covers_the_movie(self) -> None:
        self.assertEqual(self.run_main(provider=self.both), 0)
        self.assertEqual(self.sidecar_of(self.video).read_text(encoding="utf-8"),
                         fake.SRT_TEXT)
        self.assertEqual(self.subdl.count("download"), 1)

    def test_the_failure_is_still_written_down(self) -> None:
        self.assertEqual(self.run_main(provider=self.both), 0)
        self.assertIn("OpenSubtitles", self.log_text())

    def test_a_subdl_title_search_that_fails_is_an_error_for_that_movie(self) -> None:
        self.subdl.release_results = []
        self.subdl.fail("/subtitles/search", *([500] * 8))
        self.assertEqual(self.run_main(provider=self.both), 1)
        self.assertEqual(self.sidecars(), [])
        self.assertIn("SubDL lookup failed", self.report_text())


class WhenTheOnlyProviderGoesDownTests(RememberedStatusMixin, FetcherRunFixture):
    """With nowhere else to ask, the same outage is an error for that movie."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()
        self.provider.hash_results = []
        self.provider.fail("Knight+2008", *([500] * 8))

    def test_a_title_search_that_never_answers_is_an_error(self) -> None:
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])
        self.assertIn("HTTP 500", self.report_text())

    def test_the_movie_is_filed_as_an_error_not_as_reviewed(self) -> None:
        """The distinction is what tomorrow does: an error is retried, a review is not."""
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.remembered(self.video).get("status"), "error")
        report = self.report_text()
        self.assertIn("ERRORS", report)
        self.assertNotIn("HELD FOR MANUAL REVIEW", report)


class WhenTheScrapedTierMisbehavesTests(ScrapingTierFixture):
    """The free sources are the least trustworthy code path in the tool."""

    def test_a_crash_in_the_chain_costs_the_movie_not_the_run(self) -> None:
        """A bug in an adapter must not be able to end a 900-movie night."""
        # Movies are taken in sorted order, so "Heat (1995)" is the one the
        # crash lands on; the sources only have pages for the other movie.
        self.movie("Heat (1995)")
        real = sf.run_scrape_chain
        seen: list[int] = []

        def crash_once(*args, **kwargs):
            if not seen:
                seen.append(1)
                raise RuntimeError("the adapter exploded")
            return real(*args, **kwargs)

        with mock.patch.object(sf, "run_scrape_chain", side_effect=crash_once):
            self.assertEqual(self.run_main(), 1)
        self.assertIn("RuntimeError: the adapter exploded", self.report_text())
        self.assertEqual(self.sidecars(),
                         ["The Dark Knight (2008)/The Dark Knight (2008).eng.srt"])

    def test_bytes_the_chain_should_have_rejected_are_rejected_here(self) -> None:
        """The install step re-checks the payload the adapter handed back.

        Every source validates what it downloaded, so this branch is only
        reachable when one of them is wrong — which is exactly when it
        matters, because these bytes are about to become a movie's subtitle.
        """
        candidate = sf.ScrapeCandidate(
            provider=sf.PROVIDER_SUBF2ME, file_id="/dl/12345", release=GOOD_RELEASE,
            feature_title="The Dark Knight", feature_year=2008,
        )
        with mock.patch.object(
            sf, "run_scrape_chain",
            return_value=(candidate, sf.PROVIDER_SUBF2ME, b"<html>not a subtitle</html>"),
        ):
            self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sidecars(), [])
        self.assertIn("not a valid SRT subtitle", self.report_text())


class WhatTheLedgerRemembersTests(RememberedStatusMixin, FetcherRunFixture):
    """How a movie ended has to outlive the run that ended it.

    The ledger is append-only and each checkpoint carries only the records
    that changed since the last one, which is what keeps a 900-movie night's
    log small. A movie that gets as far as a download is checkpointed twice:
    once to reserve the request before it is spent, and once for the outcome.

    Writing this test is what found the bug the second half of that sentence
    describes: the reservation checkpoint empties the changed-record set, and
    recording an outcome does not put the record back into it, so every
    outcome after a reservation was dropped and the durable ledger was left
    claiming the download was still reserved. The same re-marking the scraping
    branch already did is now done here too.
    """

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def test_a_downloaded_movie_is_remembered_as_downloaded(self) -> None:
        self.assertEqual(self.run_main(), 0)
        record = self.remembered(self.video)
        self.assertEqual(record.get("status"), "downloaded")
        self.assertEqual(record.get("sidecar"), str(self.sidecar_of(self.video)))

    def test_a_download_that_failed_is_remembered_as_an_error(self) -> None:
        self.provider.fail("/download", *([429] * 6))
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.remembered(self.video).get("status"), "error")

    def test_the_reservation_is_still_written_before_the_request(self) -> None:
        """The first checkpoint is the one that must survive a crash."""
        self.provider.fail(fake.DOWNLOAD_HOST, "urlerror", "urlerror", "urlerror",
                           "urlerror", "urlerror", "urlerror")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.ledger().get("opensubtitles_download_requests_reserved"), 1)


class WhenTheSubtitleArrivesFromSomewhereElseTests(RememberedStatusMixin, FetcherRunFixture):
    """Another process, another tool, or a human, mid-download."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def test_a_sidecar_that_appears_during_the_download_wins(self) -> None:
        """The download is create-only: what is already there is never clobbered."""
        placed = "1\n00:00:01,000 --> 00:00:02,000\nPlaced by hand.\n"

        def place_a_sidecar() -> None:
            self.sidecar_of(self.video).write_text(placed, encoding="utf-8")

        self.provider.on_download = place_a_sidecar
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.sidecar_of(self.video).read_text(encoding="utf-8"), placed)
        self.assertIn("preserved the existing sidecar", self.log_text())
        # And it is remembered as covered, so tomorrow's run does not pay to
        # fetch a subtitle this movie already has.
        self.assertEqual(self.remembered(self.video).get("status"), "have")

    def test_nothing_of_the_download_is_left_in_the_folder(self) -> None:
        self.provider.on_download = lambda: self.sidecar_of(self.video).write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nPlaced by hand.\n", encoding="utf-8")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(
            sorted(p.name for p in self.video.parent.iterdir()),
            ["The Dark Knight (2008).eng.srt", "The Dark Knight (2008).mkv"],
        )


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
