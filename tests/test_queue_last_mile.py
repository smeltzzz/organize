"""The queue's last mile: the branches a normal bad day does not reach.

`tests/test_queue_failures_e2e.py` covers the failures a run meets in the
field — a provider that is down, a movie that changes, a filename with no
identity. What is left after it are the arms that only fire when something
*inside* the run is inconsistent: a pick that names a provider this run has no
client for, a scraped payload that grew between validation and writing, a
source whose daily cap was spent by an earlier run while its neighbours still
have quota.

They are all one line each, and each one is the difference between "this movie
was skipped, here is why" and a traceback that ends a 900-movie nightly job.
So they are driven through the real `queue_run`, with the smallest possible
lie told to the run: one patched planner call, one shrunken constant, one
pre-spent ledger.
"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fake_provider as fake
from test_subtitle_fetcher_e2e import (
    GOOD_RELEASE,
    FetcherRunFixture,
    ScrapingTierFixture,
    SubdlFixture,
)

import subtitle_fetcher as sf


def picked(file_id: int | str, provider: str) -> tuple[sf.Candidate, str, str, str]:
    """What `pick_pooled_candidates` returns when a candidate wins."""
    candidate = sf.Candidate(
        file_id=file_id, release=GOOD_RELEASE, moviehash_match=True, downloads=900,
        votes=12, rating=8.5, trusted=True, hearing_impaired=False,
        machine_translated=False, ai_translated=False, foreign_parts_only=False,
        language="en", feature_title="The Dark Knight", feature_year=2008,
    )
    return candidate, provider, "moviehash", ""


class WhenThePickCannotBeDownloadedTests(FetcherRunFixture):
    """A pick that names a provider this run cannot ask.

    The planner and the download leg are separate; the download leg re-checks
    what it was handed rather than assuming the planner agreed with the
    configuration. Each mismatch has to become this movie's error, and the run
    has to reach the end.
    """

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def run_with_pick(self, pick: tuple, **kwargs: Any) -> int:
        with mock.patch.object(sf, "pick_pooled_candidates", return_value=pick):
            return self.run_main(**kwargs)

    def test_a_subdl_pick_in_a_run_with_no_subdl_key(self) -> None:
        code = self.run_with_pick(picked("subdl:sub123", sf.PROVIDER_SUBDL))
        self.assertEqual(code, 1)
        self.assertIn("SubDL client is unavailable", self.report_text())
        self.assertEqual(self.sidecars(), [])

    def test_an_opensubtitles_pick_whose_file_id_is_not_a_number(self) -> None:
        code = self.run_with_pick(picked("nine-thousand", sf.PROVIDER_OPENSUBTITLES))
        self.assertEqual(code, 1)
        self.assertIn("invalid file identifier", self.report_text())
        self.assertEqual(self.provider.count("/download"), 0)
        self.assertEqual(self.sidecars(), [])


class WhenTheSubdlReferenceIsMissingTests(SubdlFixture):
    """SubDL candidates carry their download reference in a side table."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def test_a_pick_with_no_entry_in_that_table_is_an_error(self) -> None:
        pick = picked("subdl:not-in-the-table", sf.PROVIDER_SUBDL)
        with mock.patch.object(sf, "pick_pooled_candidates", return_value=pick):
            code = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("download reference is missing", self.report_text())
        self.assertEqual(self.subdl.count("download"), 0)
        self.assertEqual(self.sidecars(), [])


class WritingAScrapedSubtitleTests(ScrapingTierFixture):
    """The scraped tier hands over bytes, not a URL — and they are re-checked.

    The chain already validated this payload, but between that verdict and the
    file appearing beside the movie the same three questions apply as for a
    download: is it still small enough, is it still this movie, and did
    somebody else write the sidecar first.
    """

    def test_a_payload_over_the_size_limit_is_never_written(self) -> None:
        with mock.patch.object(sf, "MAX_SUBTITLE_BYTES", 10):
            code = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("safety limit", self.report_text())
        self.assertEqual(self.sidecars(), [])

    def test_a_movie_that_changed_during_the_lookup_gets_no_sidecar(self) -> None:
        """True while the movie is hashed, false by the time bytes are written."""
        real = sf.video_snapshot_matches
        seen: list[int] = []

        def changed_after_the_search(video: Path, snapshot: Any) -> bool:
            seen.append(1)
            return real(video, snapshot) if len(seen) == 1 else False

        with mock.patch.object(sf, "video_snapshot_matches", changed_after_the_search):
            code = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("movie changed during subtitle lookup", self.report_text())
        self.assertEqual(self.sidecars(), [])

    def test_a_sidecar_that_appeared_meanwhile_is_left_alone(self) -> None:
        """Another actor won the race; this run reports the movie as covered."""
        real = sf.atomic_write_text

        def refuse_the_sidecar(path: Path, text: str, **kwargs: Any) -> None:
            if path.name.endswith(".eng.srt"):
                raise FileExistsError("someone else got there first")
            real(path, text, **kwargs)

        with mock.patch.object(sf, "atomic_write_text", refuse_the_sidecar):
            code = self.run_main()
        self.assertEqual(code, 0, "someone else's sidecar still covers the movie")
        self.assertIn("preserved the existing sidecar", self.log_text())
        self.assertIn("The Dark Knight (2008).eng.srt", self.report_text())


class SpentSearchCapsTests(unittest.TestCase):
    """The durable half of a source's daily search cap.

    The chain counts searches in memory, but the promise to the site is kept
    in the ledger: the reservation is written and fsynced *before* the request
    leaves, so an interrupted run still spends it. This is that second check,
    the one that decides based on what is on disk rather than what this
    process remembers.
    """

    def cfg(self) -> sf.QueueConfig:
        return sf.QueueConfig(library=Path("/library"), log_file=None,
                              report_file=Path("/logs/report.txt"), scrape_daily_cap=2)

    def test_a_source_whose_reservations_are_already_spent_is_refused(self) -> None:
        cfg = self.cfg()
        field = sf.provider_reservation_field(sf.PROVIDER_SUBF2ME)
        ledger: dict[str, int] = {field: 2}
        chain = sf.build_scrape_chain(cfg, ledger, sf.new_state(cfg.library))
        assert chain is not None and chain.reserve_cb is not None
        with self.assertRaises(sf.SourceUnavailable) as caught:
            chain.reserve_cb(sf.PROVIDER_SUBF2ME)
        self.assertIn("daily search cap exhausted (2/2)", str(caught.exception))
        self.assertEqual(ledger[field], 2, "a refused search reserves nothing")

    def test_a_source_with_quota_left_reserves_before_it_asks(self) -> None:
        cfg = self.cfg()
        field = sf.provider_reservation_field(sf.PROVIDER_SUBF2ME)
        ledger: dict[str, int] = {field: 1}
        chain = sf.build_scrape_chain(cfg, ledger, sf.new_state(cfg.library))
        assert chain is not None and chain.reserve_cb is not None
        chain.reserve_cb(sf.PROVIDER_SUBF2ME)
        self.assertEqual(ledger[field], 2)


class TheOcrBudgetTests(FetcherRunFixture):
    """``--ocr-limit`` counts OCR jobs, and only OCR jobs.

    OCR of an image track takes minutes per movie, so a nightly run is allowed
    to do a few and then stop. The counter therefore has to move when a track
    was OCR'd and stay still when the movie's text track was simply copied.
    """

    def setUp(self) -> None:
        super().setUp()
        self.first = self.movie("Alien (1979)")
        self.second = self.movie("The Dark Knight (2008)")
        self.allowed: list[bool] = []

    def extraction(self, *methods: str) -> Any:
        """A fake extractor that succeeds with the given method per movie."""
        outcomes = list(methods)

        def fake_extract(video: Path, dest: Path, options: Any = None,
                         log_file: Path | None = None) -> sf.ExtractionOutcome:
            self.allowed.append(bool(options.ocr_allowed))
            method = outcomes.pop(0) if outcomes else "text"
            dest.write_text(fake.SRT_TEXT, encoding="utf-8")
            return sf.ExtractionOutcome(
                ok=True, method=method, cue_count=2, dest=dest,
                detail=f"extracted the embedded track from {video.name}",
            )

        return fake_extract

    def run_extracting(self, *extra: str, methods: tuple[str, ...]) -> int:
        argv = ["--source", str(self.library), "--log", str(self.log),
                "--report", str(self.report), "--min-size", "1",
                "--scrape-daily-cap", "0", "--workers", "1", *extra]
        with mock.patch.object(sf.urllib.request, "urlopen", self.provider), \
             mock.patch.object(sf, "extract_embedded_english_srt", self.extraction(*methods)), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            return sf.main(argv)

    def test_an_ocr_job_spends_the_budget_for_the_next_movie(self) -> None:
        self.assertEqual(self.run_extracting("--ocr-limit", "1", methods=("ocr", "ocr")), 0)
        self.assertEqual(self.allowed, [True, False],
                         "the second movie must be told the OCR budget is gone")

    def test_a_copied_text_track_costs_nothing(self) -> None:
        self.assertEqual(self.run_extracting("--ocr-limit", "1", methods=("text", "text")), 0)
        self.assertEqual(self.allowed, [True, True])


class WhyNoProviderAnsweredTests(FetcherRunFixture):
    """The review detail is assembled from one phrase per tier.

    A movie held for review is a movie a human has to decide about, and the
    only thing they get is that sentence. Every tier that was offered the
    movie and produced nothing has to contribute its reason to it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()

    def test_a_moviehash_match_nobody_should_use_says_why(self) -> None:
        """Tier 1 found the movie, and refused every file it found."""
        self.provider.hash_results = [
            fake.subtitle(9001, GOOD_RELEASE, downloads=900, title="The Dark Knight",
                          year=2008, machine_translated=True),
        ]
        self.provider.identity_results = []
        self.assertEqual(self.run_main(), 1)
        report = self.report_text()
        self.assertIn("HELD FOR MANUAL REVIEW", report)
        self.assertIn("moviehash", report.casefold())
        self.assertEqual(self.sidecars(), [])

    def test_a_title_match_nobody_should_use_says_why_too(self) -> None:
        """Tier 2 found the movie by title, and refused every file it found."""
        self.provider.hash_results = []
        self.provider.identity_results = [
            fake.subtitle(9002, GOOD_RELEASE, moviehash_match=False, downloads=900,
                          title="The Dark Knight", year=2008, machine_translated=True),
        ]
        self.assertEqual(self.run_main(), 1)
        self.assertIn("HELD FOR MANUAL REVIEW", self.report_text())
        self.assertEqual(self.sidecars(), [])


class WhenThePoolRefusesEveryProviderTests(SubdlFixture):
    """Two providers answered, and the pooled selector took neither.

    A tie between two equal sources, or two releases that each provider was
    willing to offer but that neither names this movie, is deliberately not
    resolved by a provider default — it is held for review. What is pinned
    here is that the *reason* survives all the way into the review detail, for
    both tiers, rather than the movie being held with an empty explanation.
    """

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()
        self.provider.hash_results = [
            fake.subtitle(9001, GOOD_RELEASE, downloads=900,
                          title="The Dark Knight", year=2008),
        ]
        self.subdl.release_results = [fake.subdl_subtitle("sub123", GOOD_RELEASE)]

    def test_the_pooled_refusal_is_what_the_operator_reads(self) -> None:
        refused = (None, "", "",
                   "no release met the selection policy on either provider "
                   "(OpenSubtitles; SubDL rejected)")
        with mock.patch.object(sf, "pick_pooled_candidates", return_value=refused):
            code = self.run_main()
        self.assertEqual(code, 1)
        report = self.report_text()
        self.assertIn("HELD FOR MANUAL REVIEW", report)
        # The renderer wraps the detail, so compare on collapsed whitespace.
        flat = " ".join(report.split())
        self.assertEqual(flat.count("no release met the selection policy on either provider"), 2,
                         "both tiers contributed their refusal to the review detail")
        self.assertEqual(self.provider.count("/download"), 0)
        self.assertEqual(self.subdl.count("download"), 0)
        self.assertEqual(self.sidecars(), [])


class WhenOpenSubtitlesIsNotAskedTests(ScrapingTierFixture):
    """A configured provider that is skipped still owes the report a reason."""

    def setUp(self) -> None:
        super().setUp()
        # Two movies: the first spends the single OpenSubtitles download, the
        # second reaches the title/year tier with the cap already gone.
        self.covered = self.movie("Alien (1979)")
        self.provider.hash_results = [
            fake.subtitle(9001, "Alien.1979.1080p.BluRay.x264-GROUP", downloads=900,
                          title="Alien", year=1979),
        ]
        self.sites.pages = {}

    def test_an_exhausted_daily_cap_is_named_in_the_review_detail(self) -> None:
        self.assertEqual(self.run_main("--daily-cap", "1"), 1)
        report = self.report_text()
        self.assertIn("OpenSubtitles: daily download cap exhausted", report)
        self.assertIn("The Dark Knight (2008)", report)


class CheckpointWritingTests(unittest.TestCase):
    """The ledger checkpoint is written whole or not at all."""

    def test_a_staging_file_that_cannot_be_removed_is_not_an_error(self) -> None:
        """``os.replace`` already renamed it; the tidy-up is best effort."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="checkpoint_") as tmpdir:
            target = Path(tmpdir) / "state.json"
            real_unlink = Path.unlink

            def refuse_staging(self_path: Path, **kwargs: object) -> None:
                if self_path.name.startswith(".state.json.partial."):
                    raise OSError("the filesystem said no")
                real_unlink(self_path, **kwargs)  # type: ignore[arg-type]

            with mock.patch.object(Path, "unlink", refuse_staging):
                sf.atomic_write_json(target, {"movies": {}})
            self.assertEqual(target.read_text(encoding="utf-8").strip(), '{\n  "movies": {}\n}')
            self.assertEqual(sorted(os.listdir(tmpdir)), ["state.json"])


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
