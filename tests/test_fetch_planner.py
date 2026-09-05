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


if __name__ == "__main__":
    unittest.main()
