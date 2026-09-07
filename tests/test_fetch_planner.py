"""The fetcher's spending decisions, as a table.

Before a single request leaves the process, two pure functions decide whether
a movie costs anything today: ``plan_from_history`` reads the durable ledger
record, and ``plan_sources`` reads the daily quota counters. Both used to be
inline in ``queue_run``'s 700-line per-movie loop, where the only way to
exercise them was to run the whole fetcher against live providers - which is
exactly why the retry economy (the rules that stop a movie burning quota it
already burned) had no direct tests at all.

They are ordinary functions of their inputs, so the interesting cases fit in
a table.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import subtitle_fetcher as sf

TODAY = "2026-09-05"
YESTERDAY = "2026-09-04"
SCRAPE_KEYS = sf.SCRAPE_PROVIDER_ORDER
BOTH_APIS = (sf.PROVIDER_OPENSUBTITLES, sf.PROVIDER_SUBDL)


def record(**fields: object) -> dict:
    base: dict = {"path": "/library/Film (2020)/Film (2020).mkv", "status": "pending",
                  "attempts": 0}
    base.update(fields)
    return base


class HistoryPlanTests(unittest.TestCase):
    """What a movie's own record says about spending requests on it today."""

    def plan(self, rec: dict, **overrides: object) -> sf.HistoryPlan:
        options: dict = {
            "today": TODAY,
            "retry_no_match": False,
            "identity_fallback": True,
            "scrape_keys": SCRAPE_KEYS,
            "active_providers": BOTH_APIS,
        }
        options.update(overrides)
        return sf.plan_from_history(rec, **options)  # type: ignore[arg-type]

    # -- the ordinary answer -----------------------------------------------

    def test_a_new_movie_is_fetched(self) -> None:
        plan = self.plan(record())
        self.assertTrue(plan.fetch)
        self.assertEqual((plan.detail, plan.reason), ("", ""))

    def test_a_movie_that_errored_before_is_tried_again(self) -> None:
        self.assertTrue(self.plan(record(status="error")).fetch)

    def test_a_downloaded_movie_is_not_gated_here(self) -> None:
        # Coverage is decided by the sidecar on disk, not by the record; this
        # planner must not invent a second opinion about it.
        self.assertTrue(self.plan(record(status="downloaded")).fetch)

    # -- the scraping retry economy ----------------------------------------

    def test_scraping_exhausted_today_is_not_offered_twice(self) -> None:
        plan = self.plan(record(status="no_match", scrape_failed=True,
                                scrape_failed_utc_day=TODAY))
        self.assertEqual(plan.action, "skip")
        self.assertEqual(plan.reason, sf.REASON_QUOTA)
        self.assertIn("next UTC day", plan.detail)
        self.assertTrue(plan.scrape_tried_today)
        self.assertFalse(plan.scrape_retry_today)

    def test_scraping_exhausted_yesterday_goes_straight_back_to_scraping(self) -> None:
        plan = self.plan(record(status="no_match", scrape_failed=True,
                                scrape_failed_utc_day=YESTERDAY))
        self.assertTrue(plan.fetch)
        self.assertTrue(plan.scrape_retry_today)
        self.assertFalse(plan.scrape_tried_today)

    def test_a_scraping_retry_needs_the_identity_fallback(self) -> None:
        plan = self.plan(record(status="no_match", scrape_failed=True,
                                scrape_failed_utc_day=YESTERDAY),
                         identity_fallback=False)
        self.assertFalse(plan.scrape_retry_today, "strict-hash runs never scrape")

    def test_a_scraping_retry_needs_at_least_one_enabled_source(self) -> None:
        plan = self.plan(record(status="no_match", scrape_failed=True,
                                scrape_failed_utc_day=YESTERDAY),
                         scrape_keys=())
        self.assertFalse(plan.scrape_retry_today)

    def test_a_covered_movie_that_exhausted_scraping_today_is_still_fetchable(self) -> None:
        # The today-gate applies only to the two statuses it was written for;
        # a pending movie is not held back by an old scraping failure.
        plan = self.plan(record(status="pending", scrape_failed=True,
                                scrape_failed_utc_day=TODAY))
        self.assertTrue(plan.fetch)

    # -- the no-match hold --------------------------------------------------

    def test_a_strict_no_match_is_not_re_searched(self) -> None:
        plan = self.plan(record(status="no_match"), identity_fallback=False)
        self.assertEqual((plan.action, plan.reason), ("skip", sf.REASON_NO_MATCH))
        self.assertIn("moviehash", plan.detail)

    def test_retry_no_match_reopens_it(self) -> None:
        self.assertTrue(self.plan(record(status="no_match"), identity_fallback=False,
                                  retry_no_match=True).fetch)

    def test_the_identity_fallback_reopens_it(self) -> None:
        self.assertTrue(self.plan(record(status="no_match")).fetch)

    # -- the manual-review hold --------------------------------------------

    def test_a_deliberate_review_hold_is_honoured(self) -> None:
        plan = self.plan(record(status="manual_review",
                                providers_checked=list(BOTH_APIS), scrape_checked=True))
        self.assertEqual((plan.action, plan.reason), ("review", sf.REASON_REVIEW))
        self.assertIn("held for review", plan.detail)

    def test_retry_no_match_overrides_the_review_hold(self) -> None:
        plan = self.plan(record(status="manual_review",
                                providers_checked=list(BOTH_APIS), scrape_checked=True),
                         retry_no_match=True)
        self.assertTrue(plan.fetch)

    def test_a_newly_configured_provider_reopens_the_hold(self) -> None:
        plan = self.plan(record(status="manual_review",
                                providers_checked=[sf.PROVIDER_OPENSUBTITLES],
                                scrape_checked=True))
        self.assertTrue(plan.fetch, "SubDL has never seen this movie")

    def test_the_scraping_tier_reopens_a_legacy_hold_once(self) -> None:
        plan = self.plan(record(status="manual_review", providers_checked=list(BOTH_APIS)))
        self.assertTrue(plan.fetch, "the scraping tier is new to this record")

    def test_a_hold_already_offered_to_every_source_stays_held(self) -> None:
        plan = self.plan(record(status="manual_review", providers_checked=list(BOTH_APIS),
                                scrape_checked=True), scrape_keys=())
        self.assertEqual(plan.action, "review")

    # -- the reservation hold -----------------------------------------------

    def test_a_download_reserved_today_waits_for_the_next_day(self) -> None:
        plan = self.plan(record(status="reserved", updated_utc=f"{TODAY}T09:15:00Z"))
        self.assertEqual((plan.action, plan.reason), ("skip", sf.REASON_QUOTA))
        self.assertIn("already reserved today", plan.detail)

    def test_a_reservation_from_an_earlier_day_is_retried(self) -> None:
        plan = self.plan(record(status="reserved", updated_utc=f"{YESTERDAY}T23:59:00Z"))
        self.assertTrue(plan.fetch, "a stranded reservation must not strand the movie")

    def test_a_reservation_with_no_timestamp_is_retried(self) -> None:
        self.assertTrue(self.plan(record(status="reserved")).fetch)

    # -- garbage in the ledger ----------------------------------------------

    def test_an_unreadable_providers_list_is_treated_as_legacy(self) -> None:
        plan = self.plan(record(status="manual_review", providers_checked="opensubtitles",
                                scrape_checked=True), scrape_keys=())
        self.assertTrue(plan.fetch, "a string is not a list: fall back to the legacy rule")

    def test_a_missing_status_is_pending(self) -> None:
        self.assertTrue(self.plan({"path": "/library/x/x.mkv"}).fetch)


class HasNewProviderTests(unittest.TestCase):
    def check(self, rec: dict, *, providers=BOTH_APIS, scrape=SCRAPE_KEYS) -> bool:
        return sf.has_new_provider(rec, active_providers=providers, scrape_keys=scrape)

    def test_a_record_listing_every_active_provider_has_nothing_new(self) -> None:
        self.assertFalse(self.check(record(providers_checked=list(BOTH_APIS),
                                           scrape_checked=True), scrape=()))

    def test_an_unchecked_scraping_tier_counts_as_new(self) -> None:
        self.assertTrue(self.check(record(providers_checked=list(BOTH_APIS))))

    def test_a_legacy_record_sees_subdl_as_new(self) -> None:
        self.assertTrue(self.check(record(), providers=BOTH_APIS, scrape=()))

    def test_a_legacy_record_with_only_opensubtitles_configured_has_nothing_new(self) -> None:
        self.assertFalse(self.check(record(scrape_checked=True),
                                    providers=(sf.PROVIDER_OPENSUBTITLES,), scrape=()))

    def test_a_legacy_record_that_never_saw_the_scraping_tier_is_revisited(self) -> None:
        """No providers_checked list at all: the seven sites are new to it."""
        self.assertTrue(self.check(record(), providers=(sf.PROVIDER_OPENSUBTITLES,)))

    def test_an_empty_history_list_makes_every_provider_new(self) -> None:
        self.assertTrue(self.check(record(providers_checked=[]), scrape=()))


class SourcePlanTests(unittest.TestCase):
    """Which tiers may be asked, given the day's durable reservations."""

    def config(self, **overrides: object) -> sf.QueueConfig:
        base: dict = {"library": Path("/library"), "log_file": None,
                      "report_file": Path("/logs/r.txt"), "daily_cap": 200,
                      "subdl_daily_cap": 100, "subdl_search_daily_cap": 100,
                      "scrape_daily_cap": 20}
        base.update(overrides)
        return sf.QueueConfig(**base)  # type: ignore[arg-type]

    def plan(self, ledger: dict | None = None, *, history: sf.HistoryPlan | None = None,
             cfg: sf.QueueConfig | None = None, has_open: bool = True,
             has_subdl: bool = True, has_scrape_chain: bool = True) -> sf.SourcePlan:
        return sf.plan_sources(
            cfg or self.config(), ledger or {}, history or sf.HistoryPlan(),
            has_open=has_open, has_subdl=has_subdl,
            has_scrape_chain=has_scrape_chain, scrape_keys=SCRAPE_KEYS,
        )

    def test_everything_configured_and_funded_is_available(self) -> None:
        plan = self.plan()
        self.assertEqual(
            (plan.open_available, plan.subdl_available, plan.scrape_available),
            (True, True, True),
        )
        self.assertTrue(plan.open_tier and plan.subdl_tier)
        self.assertFalse(plan.exhausted)

    def test_a_provider_with_no_key_is_not_available(self) -> None:
        plan = self.plan(has_open=False)
        self.assertFalse(plan.open_available)
        self.assertTrue(plan.subdl_available)

    def test_an_exhausted_download_cap_closes_that_provider_only(self) -> None:
        plan = self.plan({"opensubtitles_download_requests_reserved": 200})
        self.assertFalse(plan.open_available)
        self.assertTrue(plan.subdl_available, "one provider's cap is not the other's")

    def test_an_exhausted_subdl_search_cap_closes_subdl(self) -> None:
        plan = self.plan({"subdl_search_requests_reserved": 100})
        self.assertFalse(plan.subdl_available,
                         "SubDL needs a search before it can download")

    def test_the_strict_run_disables_everything_but_opensubtitles(self) -> None:
        plan = self.plan(cfg=self.config(identity_fallback=False))
        self.assertTrue(plan.open_available, "the exact moviehash route survives")
        self.assertFalse(plan.subdl_available, "SubDL has no byte-exact route")
        self.assertFalse(plan.scrape_available)

    def test_a_dry_run_has_no_scrape_chain(self) -> None:
        plan = self.plan(has_scrape_chain=False)
        self.assertFalse(plan.scrape_available,
                         "a dry run must not spend a scraping search")

    def test_scraping_stays_open_while_any_single_source_has_capacity(self) -> None:
        ledger = {f"{key}_search_requests_reserved": 20 for key in SCRAPE_KEYS[:-1]}
        self.assertTrue(self.plan(ledger).scrape_available)

    def test_every_scraping_source_exhausted_closes_the_tier(self) -> None:
        ledger = {f"{key}_search_requests_reserved": 20 for key in SCRAPE_KEYS}
        self.assertFalse(self.plan(ledger).scrape_available)

    def test_the_wallet_is_empty_only_when_every_source_is(self) -> None:
        ledger = {"opensubtitles_download_requests_reserved": 200,
                  "subdl_download_requests_reserved": 100}
        ledger.update({f"{key}_search_requests_reserved": 20 for key in SCRAPE_KEYS})
        plan = self.plan(ledger)
        self.assertTrue(plan.exhausted)

    def test_a_scraping_retry_funds_the_apis_but_does_not_ask_them(self) -> None:
        history = sf.HistoryPlan(scrape_retry_today=True)
        plan = self.plan(history=history)
        self.assertFalse(plan.api_tiers_allowed)
        self.assertFalse(plan.open_tier, "the API tiers already missed for this movie")
        self.assertFalse(plan.subdl_tier)
        self.assertTrue(plan.open_available,
                        "still funded, so the run does not stop for an empty wallet")
        self.assertFalse(plan.exhausted)

    def test_a_movie_that_exhausted_scraping_today_still_asks_the_apis(self) -> None:
        history = sf.HistoryPlan(scrape_retry_today=True, scrape_tried_today=True)
        plan = self.plan(history=history)
        self.assertTrue(plan.api_tiers_allowed)
        self.assertTrue(plan.open_tier)


class MissReasonTests(unittest.TestCase):
    """The phrases a review hold is assembled from.

    When nothing is found, the movie's detail line is the only thing telling
    the operator what to do about it — wait for tomorrow's quota, turn a
    switch back on, or go and find the subtitle by hand. These phrases used to
    be built by if/elif chains three levels deep inside the per-movie loop.
    """

    def config(self, **overrides: object) -> sf.QueueConfig:
        base: dict = {"library": Path("/library"), "log_file": None,
                      "report_file": Path("/logs/r.txt"), "daily_cap": 200,
                      "subdl_daily_cap": 100, "subdl_search_daily_cap": 100}
        base.update(overrides)
        return sf.QueueConfig(**base)  # type: ignore[arg-type]

    # -- OpenSubtitles: two ways to be unavailable -------------------------

    def test_a_scraping_retry_says_so_rather_than_blaming_the_cap(self) -> None:
        self.assertEqual(
            sf.opensubtitles_unavailable_reason(api_tiers_allowed=False),
            "OpenSubtitles: not re-queried on a scraping retry (known API miss)",
        )

    def test_otherwise_a_configured_provider_is_out_of_quota(self) -> None:
        self.assertEqual(
            sf.opensubtitles_unavailable_reason(api_tiers_allowed=True),
            "OpenSubtitles: daily download cap exhausted",
        )

    # -- SubDL: four, and their order is the point -------------------------

    def subdl(self, ledger: dict | None = None, *, api_tiers_allowed: bool = True,
              **cfg_overrides: object) -> str:
        return sf.subdl_unavailable_reason(
            self.config(**cfg_overrides), ledger or {}, api_tiers_allowed=api_tiers_allowed,
        )

    def test_a_scraping_retry_explains_the_miss_even_with_quota_to_spare(self) -> None:
        self.assertEqual(
            self.subdl(api_tiers_allowed=False),
            "SubDL: not re-queried on a scraping retry (known API miss)",
        )

    def test_a_spent_download_cap_is_reported_before_the_search_cap(self) -> None:
        """No point spending a search on a subtitle that cannot be downloaded today."""
        spent = {"subdl_download_requests_reserved": 100, "subdl_search_requests_reserved": 100}
        self.assertEqual(self.subdl(spent), "SubDL: daily download cap exhausted")

    def test_a_spent_search_cap_is_its_own_sentence(self) -> None:
        self.assertEqual(
            self.subdl({"subdl_search_requests_reserved": 100}),
            "SubDL: daily search cap exhausted",
        )

    def test_a_funded_provider_that_was_not_asked_was_switched_off(self) -> None:
        """The last case is not a quota problem, and must not read like one."""
        self.assertEqual(self.subdl(), "SubDL: identity fallback disabled")

    # -- deferral is a different disposition from review -------------------

    def test_a_cap_reached_before_the_lookup_defers_to_the_next_day(self) -> None:
        self.assertEqual(
            sf.subdl_defer_detail(self.config(), {"subdl_download_requests_reserved": 100}),
            "SubDL daily download cap exhausted before lookup; deferred to the next UTC day",
        )
        self.assertEqual(
            sf.subdl_defer_detail(self.config(), {"subdl_search_requests_reserved": 100}),
            "SubDL daily search cap exhausted before lookup; deferred to the next UTC day",
        )

    def test_every_reason_names_its_provider(self) -> None:
        """A detail line concatenates these; an unattributed clause is useless."""
        reasons = [
            sf.opensubtitles_unavailable_reason(api_tiers_allowed=True),
            sf.opensubtitles_unavailable_reason(api_tiers_allowed=False),
            self.subdl(), self.subdl(api_tiers_allowed=False),
            self.subdl({"subdl_download_requests_reserved": 100}),
            self.subdl({"subdl_search_requests_reserved": 100}),
        ]
        for reason in reasons:
            self.assertTrue(reason.startswith(("OpenSubtitles:", "SubDL:")), reason)


class ScrapeCandidateTests(unittest.TestCase):
    """A scraping hit presented as the Candidate everything downstream reads."""

    IDENTITY = sf.MovieIdentity(title="Arrival", year=2016, normalized_title="arrival")

    def convert(self, **overrides: object) -> tuple[sf.Candidate, str]:
        fields: dict = {"provider": "subf2me", "file_id": "9911", "release": "Arrival.2016.BluRay",
                        "feature_title": "Arrival", "feature_year": 2016, "downloads": 42,
                        "rating": 4.5, "hearing_impaired": True}
        fields.update(overrides)
        return sf.candidate_from_scrape(sf.ScrapeCandidate(**fields), "subf2me", self.IDENTITY)

    def test_the_id_carries_its_source_so_a_bad_pick_can_be_traced(self) -> None:
        candidate, _ = self.convert()
        self.assertEqual(candidate.file_id, "scrape:subf2me:9911")

    def test_a_scraped_candidate_never_claims_a_hash_match_or_provider_trust(self) -> None:
        """The chain validated bytes; that is a weaker claim than provider metadata."""
        candidate, _ = self.convert()
        self.assertFalse(candidate.moviehash_match)
        self.assertFalse(candidate.trusted)
        self.assertFalse(candidate.machine_translated or candidate.ai_translated)
        self.assertEqual(candidate.votes, 0)
        self.assertEqual(candidate.language, "en")

    def test_popularity_signals_survive_the_conversion(self) -> None:
        candidate, _ = self.convert()
        self.assertEqual((candidate.downloads, candidate.rating), (42, 4.5))
        self.assertTrue(candidate.hearing_impaired)

    def test_a_source_that_names_no_feature_borrows_the_movie_identity(self) -> None:
        candidate, _ = self.convert(feature_title="", feature_year=0)
        self.assertEqual((candidate.feature_title, candidate.feature_year), ("Arrival", 2016))

    def test_the_reason_names_the_source_in_words_an_operator_reads(self) -> None:
        _, reason = self.convert()
        self.assertIn(sf.scrape_provider_label("subf2me"), reason)
        self.assertIn("validated as an English SRT", reason)


class SelectionNoteTests(unittest.TestCase):
    """The one line that records why this subtitle, for this movie."""

    def note(self, **overrides: object) -> str:
        fields: dict = {"file_id": 7, "release": "Arrival.2016.BluRay.x264", "moviehash_match": True,
                        "downloads": 900, "votes": 12, "rating": 8.5, "trusted": True,
                        "hearing_impaired": False, "machine_translated": False,
                        "ai_translated": False, "foreign_parts_only": False, "language": "en"}
        fields.update(overrides)
        return sf.selection_note(
            sf.Candidate(**fields), provider=sf.PROVIDER_OPENSUBTITLES,
            method="hash", reason="moviehash match",
        )

    def test_it_records_the_tier_the_provider_and_the_file(self) -> None:
        note = self.note()
        self.assertIn("provider=OpenSubtitles", note)
        self.assertIn("method=hash", note)
        self.assertIn("id=7", note)
        self.assertIn("trusted=yes", note)
        self.assertIn("rating=8.5/12", note)
        self.assertIn("moviehash match", note)
        self.assertIn("Arrival.2016.BluRay.x264", note)

    def test_an_untrusted_pick_says_no_rather_than_omitting_the_field(self) -> None:
        self.assertIn("trusted=no", self.note(trusted=False))

    def test_a_nameless_release_still_reads_as_a_sentence(self) -> None:
        self.assertTrue(self.note(release="").endswith("unnamed release"))


class RunSummaryTests(unittest.TestCase):
    """The closing tallies, computed from a ledger instead of from a library."""

    def config(self, **overrides: object) -> sf.QueueConfig:
        base: dict = {"library": Path("/library"), "log_file": Path("/logs/ledger.json"),
                      "report_file": Path("/logs/r.txt"), "daily_cap": 200,
                      "subdl_daily_cap": 100, "subdl_search_daily_cap": 100,
                      "scrape_daily_cap": 20}
        base.update(overrides)
        return sf.QueueConfig(**base)  # type: ignore[arg-type]

    def ledger(self, **counters: int) -> dict:
        ledger = sf.day_ledger(sf.new_state(Path("/library")), TODAY)
        ledger.update(counters)
        return ledger

    def result(self, name: str, status: str, reason: str) -> sf.JobResult:
        return sf.JobResult(Path(f"/library/{name}/{name}.mkv"), status, "", reason=reason)

    def summary(self, *, results: list | None = None, cfg: sf.QueueConfig | None = None,
                ledger: dict | None = None, **overrides: object) -> dict:
        options: dict = {"today": TODAY, "total": 3,
                         "active_providers": BOTH_APIS, "scrape_keys": SCRAPE_KEYS,
                         "scrape_status": {}, "deferred_remaining": 0, "deferred_videos": []}
        options.update(overrides)
        return sf.run_summary(
            cfg or self.config(), ledger if ledger is not None else self.ledger(),
            results if results is not None else [], **options,  # type: ignore[arg-type]
        )

    # -- capacity: what decides "come back tomorrow" -----------------------

    def test_a_fresh_day_has_capacity_everywhere(self) -> None:
        self.assertFalse(self.summary()["quota_reached"])

    def test_subdl_download_allowance_without_search_allowance_is_not_capacity(self) -> None:
        spent = self.ledger(subdl_search_requests_reserved=100,
                            opensubtitles_download_requests_reserved=200)
        available = sf.providers_with_capacity(
            self.config(scrape_daily_cap=0), spent,
            active_providers=BOTH_APIS, scrape_keys=(),
        )
        self.assertEqual(available, [])

    def test_a_disabled_identity_fallback_takes_subdl_out_of_the_tally(self) -> None:
        available = sf.providers_with_capacity(
            self.config(identity_fallback=False), self.ledger(),
            active_providers=BOTH_APIS, scrape_keys=(),
        )
        self.assertEqual(available, [sf.PROVIDER_OPENSUBTITLES])

    def test_one_scraping_source_with_capacity_keeps_the_run_alive(self) -> None:
        spent = self.ledger(opensubtitles_download_requests_reserved=200,
                            subdl_download_requests_reserved=100)
        summary = self.summary(ledger=spent)
        self.assertFalse(summary["quota_reached"], "the scraping sources are untouched")

    def test_everything_spent_is_reported_as_quota_reached(self) -> None:
        spent = self.ledger(opensubtitles_download_requests_reserved=200,
                            subdl_download_requests_reserved=100)
        spent.update({f"{key}_search_requests_reserved": 20 for key in SCRAPE_KEYS})
        self.assertTrue(self.summary(ledger=spent)["quota_reached"])

    # -- coverage: the product promise -------------------------------------

    def test_coverage_counts_outcomes_not_work(self) -> None:
        results = [
            self.result("A (2001)", "have", sf.REASON_COVERED),
            self.result("B (2002)", "download", sf.REASON_DOWNLOADED),
            self.result("C (2003)", "extracted", sf.REASON_EXTRACTED),
            self.result("D (2004)", "review", sf.REASON_REVIEW),
            self.result("E (2005)", "error", sf.REASON_ERROR),
        ]
        self.assertEqual(sf.coverage_count(results, dry_run=False), 3)

    def test_a_dry_run_counts_what_it_would_have_fetched(self) -> None:
        results = [self.result("A (2001)", "dry-run", sf.REASON_DRY_RUN)]
        self.assertEqual(sf.coverage_count(results, dry_run=True), 1)
        self.assertEqual(sf.coverage_count(results, dry_run=False), 0,
                         "a real run must never count a would-be download as coverage")

    def test_coverage_is_reported_against_everything_discovered(self) -> None:
        summary = self.summary(
            results=[self.result("A (2001)", "have", sf.REASON_COVERED)], total=9,
        )
        self.assertEqual((summary["coverage_covered"], summary["coverage_total"]), (1, 9))
        self.assertEqual(summary["movies_discovered"], 9)

    # -- the fields consumers read -----------------------------------------

    def test_the_legacy_fields_stay_opensubtitles_values(self) -> None:
        """Written before there was a second provider; readers still exist."""
        ledger = self.ledger(opensubtitles_download_requests_reserved=4,
                             opensubtitles_successful_downloads=3,
                             successful_downloads=3,
                             subdl_download_requests_reserved=9,
                             subdl_successful_downloads=8)
        summary = self.summary(ledger=ledger)
        self.assertEqual(summary["daily_cap"], 200)
        self.assertEqual(summary["download_requests_reserved"], 4)
        self.assertEqual(summary["successful_downloads"], 3)
        self.assertEqual(summary["subdl_download_requests_reserved"], 9)
        self.assertEqual(summary["subdl_successful_downloads"], 8)

    def test_the_scraping_tallies_are_per_source(self) -> None:
        key = SCRAPE_KEYS[0]
        ledger = self.ledger(**{f"{key}_search_requests_reserved": 3,
                                sf.provider_success_field(key): 1})
        summary = self.summary(ledger=ledger, scrape_keys=(key,),
                               scrape_status={key: "ok"})
        self.assertEqual(summary["scrape_search_requests_reserved"], {key: 3})
        self.assertEqual(summary["scrape_successful_downloads"], {key: 1})
        self.assertEqual(summary["scrape_sources_enabled"], [key])
        self.assertEqual(summary["scrape_sources_status"], {key: "ok"})

    def test_a_cut_short_run_names_the_movies_it_never_reached(self) -> None:
        deferred = [Path("/library/D (2004)/D (2004).mkv")]
        summary = self.summary(deferred_remaining=1, deferred_videos=deferred)
        self.assertEqual(summary["deferred_remaining"], 1)
        self.assertEqual(summary["deferred_videos"], deferred)

    def test_the_summary_says_which_day_it_belongs_to(self) -> None:
        self.assertEqual(self.summary()["utc_day"], TODAY)
        self.assertEqual(self.summary()["ledger_log"], str(Path("/logs/ledger.json")))


if __name__ == "__main__":
    unittest.main()
