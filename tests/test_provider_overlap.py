"""Tier 1 asks two providers at once, and spends money on one thread.

Every movie that reaches the API tier with the title/year fallback enabled is
offered to *both* providers: OpenSubtitles for an exact moviehash match, SubDL
for a scored release-name match. Their answers are pooled and the better one
wins, so the second lookup was never conditional on the first — it just waited
for it, on a different connection, to a different company, behind a different
rate limit.

One of the two now runs on a single background worker. These tests hold the
line that makes that safe: the run must be *indistinguishable* from the serial
one, and everything durable — SubDL's persisted search reservation, the ledger,
the downloads, the state checkpoints — must still happen on the main thread, in
library order. The overlap is two waits becoming one wait, and nothing else.
"""

from __future__ import annotations

import re
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import subtitle_fetcher as sf

# A numbered console/log line, without the timestamp the log prefixes it with.
LINE_RE = re.compile(r"\[\d+/\d+\] .*")

RELEASE = "Dune.Part.Two.2024.1080p.BluRay.x264"


def make_movie(library: Path, title: str) -> Path:
    """A movie big enough to have a moviehash, in a canonical folder."""
    folder = library / title
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"{title}.mkv"
    video.write_bytes(b"x" * sf.MIN_HASH_SIZE)
    return video


def open_candidate(title: str, year: int, *, downloads: int = 900) -> sf.Candidate:
    return sf.Candidate(
        file_id=9001, release=f"{title.replace(' ', '.')}.{year}.1080p.BluRay.x264",
        moviehash_match=True, downloads=downloads, votes=10, rating=8.0, trusted=True,
        hearing_impaired=False, machine_translated=False, ai_translated=False,
        foreign_parts_only=False, language="en", feature_title=title, feature_year=year,
    )


def subdl_candidate(title: str, year: int, *, downloads: int = 400) -> sf.Candidate:
    return sf.Candidate(
        file_id="subdl:sub-1:file-1",
        release=f"{title.replace(' ', '.')}.{year}.1080p.BluRay.x264",
        moviehash_match=False, downloads=downloads, votes=0, rating=0.0, trusted=False,
        hearing_impaired=False, machine_translated=False, ai_translated=False,
        foreign_parts_only=False, language="en", feature_title=title, feature_year=year,
        subdl_match_score=0.95,
    )


class OverlapFixture(unittest.TestCase):
    """A library both providers are configured for."""

    TITLES = ("Dune Part Two (2024)", "Heat (1995)", "Arrival (2016)")

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="overlap_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()
        self.threads_before = threading.active_count()
        gap = mock.patch.object(sf, "REQUEST_GAP_SEC", 0.0)
        gap.start()
        self.addCleanup(gap.stop)

    def build_library(self, *, count: int = 3) -> Path:
        library = self.root / "library"
        for title in self.TITLES[:count]:
            make_movie(library, title)
        return library

    def cfg(self, library: Path, **overrides: Any) -> sf.QueueConfig:
        base: dict[str, Any] = {
            "library": library,
            "log_file": self.root / "fetcher.log",
            "report_file": self.root / "report.txt",
            "api_key": "open-key",
            "subdl_api_key": "subdl-key",
            "daily_cap": 10,
            "subdl_daily_cap": 10,
            "subdl_search_daily_cap": 10,
            "scrape_daily_cap": 0,
            "min_movie_size_mb": 0,
            "extract_embedded": False,
            "dry_run": True,  # decide everything, download nothing
            "workers": 8,
        }
        base.update(overrides)
        return sf.QueueConfig(**base)

    def log_lines(self, cfg: sf.QueueConfig) -> list[str]:
        return LINE_RE.findall(cfg.log_file.read_text(encoding="utf-8"))

    def assertNoStrayThreads(self) -> None:  # noqa: N802 - unittest's spelling
        for _ in range(50):
            if threading.active_count() <= self.threads_before:
                return
            threading.Event().wait(0.02)
        self.fail(f"{threading.active_count() - self.threads_before} thread(s) outlived the run")


class BothLookupsAtOnceTests(OverlapFixture):
    def test_the_two_searches_are_genuinely_in_flight_together(self) -> None:
        """A barrier only opens when both sides reach it. Serially it cannot."""
        barrier = threading.Barrier(2, timeout=10)
        where: dict[str, str] = {}

        def open_search(_self: object, **_kwargs: object) -> list[sf.Candidate]:
            where["opensubtitles"] = threading.current_thread().name
            barrier.wait()
            return []

        def subdl_search(_self: object, *_args: object, **_kwargs: object) -> tuple[list, dict]:
            where["subdl"] = threading.current_thread().name
            barrier.wait()
            return [], {}

        library = self.build_library(count=1)
        with (
            mock.patch.object(sf.OpenSubtitlesClient, "search", open_search),
            mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
            mock.patch.object(sf.SubdlClient, "search_filename", subdl_search),
            mock.patch.object(sf.SubdlClient, "search_identity", return_value=([], {})),
        ):
            sf.queue_run(self.cfg(library))  # a timeout here means they were serial

        self.assertEqual(where["subdl"], threading.current_thread().name)
        self.assertNotEqual(where["opensubtitles"], threading.current_thread().name)
        self.assertTrue(where["opensubtitles"].startswith("fetch-provider"))
        self.assertNoStrayThreads()

    def test_the_background_lookup_is_asked_exactly_what_the_serial_one_asks(self) -> None:
        asked: dict[int, list[tuple[str, str]]] = {1: [], 8: []}

        for workers in (1, 8):
            def search(_self: object, *, movie_hash: str, query: str,
                       _workers: int = workers) -> list[sf.Candidate]:
                asked[_workers].append((movie_hash, query))
                return []

            with tempfile.TemporaryDirectory() as td:
                library = Path(td) / "library"
                for title in self.TITLES:
                    make_movie(library, title)
                cfg = self.cfg(library, workers=workers,
                               log_file=Path(td) / "fetcher.log",
                               report_file=Path(td) / "report.txt")
                with (
                    mock.patch.object(sf.OpenSubtitlesClient, "search", search),
                    mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
                    mock.patch.object(sf.SubdlClient, "search_filename", return_value=([], {})),
                    mock.patch.object(sf.SubdlClient, "search_identity", return_value=([], {})),
                ):
                    sf.queue_run(cfg)

        self.assertEqual(len(asked[8]), len(self.TITLES))
        self.assertEqual(asked[8], asked[1], "same moviehash, same query, same order")

    def test_the_worker_is_shut_down_when_the_run_ends(self) -> None:
        library = self.build_library()
        with (
            mock.patch.object(sf.OpenSubtitlesClient, "search", return_value=[]),
            mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
            mock.patch.object(sf.SubdlClient, "search_filename", return_value=([], {})),
            mock.patch.object(sf.SubdlClient, "search_identity", return_value=([], {})),
        ):
            sf.queue_run(self.cfg(library))
        self.assertNoStrayThreads()

    def test_one_lookup_at_a_time_per_provider(self) -> None:
        """The pool is one worker wide: a run never has two hash searches open."""
        live = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def open_search(_self: object, **_kwargs: object) -> list[sf.Candidate]:
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            threading.Event().wait(0.01)
            with lock:
                live["now"] -= 1
            return []

        library = self.build_library()
        with (
            mock.patch.object(sf.OpenSubtitlesClient, "search", open_search),
            mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
            mock.patch.object(sf.SubdlClient, "search_filename", return_value=([], {})),
            mock.patch.object(sf.SubdlClient, "search_identity", return_value=([], {})),
        ):
            sf.queue_run(self.cfg(library))
        self.assertEqual(live["peak"], 1)


class SameRunEitherWayTests(OverlapFixture):
    """The overlapped run must be the serial run, byte for byte."""

    def run_both_ways(self, **patches: Any) -> tuple[tuple, tuple]:
        outcomes = []
        dispatched = {1: 0, 8: 0}
        real_start = sf.SearchPool.start

        def counting_start(pool: sf.SearchPool, work: Any) -> Any:
            future = real_start(pool, work)
            if future is not None:
                dispatched[workers] += 1
            return future

        for workers in (1, 8):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                library = root / "library"
                for title in self.TITLES:
                    make_movie(library, title)
                cfg = sf.QueueConfig(
                    library=library, log_file=root / "fetcher.log",
                    report_file=root / "report.txt", api_key="open-key",
                    subdl_api_key="subdl-key", daily_cap=10, subdl_daily_cap=10,
                    subdl_search_daily_cap=10, scrape_daily_cap=0, min_movie_size_mb=0,
                    extract_embedded=False, dry_run=True, workers=workers,
                )
                with mock.patch.object(sf, "REQUEST_GAP_SEC", 0.0), \
                        mock.patch.object(sf.SearchPool, "start", counting_start), \
                        mock.patch.multiple(sf.OpenSubtitlesClient, **patches["open"]), \
                        mock.patch.multiple(sf.SubdlClient, **patches["subdl"]):
                    results, summary = sf.queue_run(cfg)
                rows = tuple(
                    (result.video.name, result.status, result.detail, result.reason)
                    for result in results
                )
                lines = tuple(LINE_RE.findall(cfg.log_file.read_text(encoding="utf-8")))
                # Everything in the summary except the paths, which are this
                # temporary directory's and say nothing about the two runs.
                counts = tuple(sorted(
                    ((key, value) for key, value in summary.items()
                     if not (isinstance(value, str) and str(root) in value)),
                    key=str,
                ))
                outcomes.append((rows, lines, counts))
        # Guard against the comparison quietly becoming serial-vs-serial.
        self.assertEqual(dispatched[1], 0, "--workers 1 dispatched a background lookup")
        self.assertEqual(dispatched[8], len(self.TITLES),
                         "the overlapped run did not overlap")
        return outcomes[0], outcomes[1]

    def test_a_run_where_opensubtitles_wins(self) -> None:
        serial, overlapped = self.run_both_ways(
            open={
                "search": lambda _self, **_kw: [open_candidate("Dune Part Two", 2024)],
                "search_identity": lambda _self, _identity: [],
            },
            subdl={
                "search_filename": lambda _self, _name, _identity: ([], {}),
                "search_identity": lambda _self, _identity: ([], {}),
            },
        )
        self.assertEqual(overlapped, serial)

    def test_a_run_where_subdl_wins(self) -> None:
        serial, overlapped = self.run_both_ways(
            open={
                "search": lambda _self, **_kw: [],
                "search_identity": lambda _self, _identity: [],
            },
            subdl={
                "search_filename": lambda _self, _name, identity: (
                    [subdl_candidate(identity.title, identity.year)],
                    {"subdl:sub-1:file-1": sf.SubdlDownload(n_id="sub-1")},
                ),
                "search_identity": lambda _self, _identity: ([], {}),
            },
        )
        self.assertEqual(overlapped, serial)

    def test_a_run_where_neither_provider_has_anything(self) -> None:
        serial, overlapped = self.run_both_ways(
            open={
                "search": lambda _self, **_kw: [],
                "search_identity": lambda _self, _identity: [],
            },
            subdl={
                "search_filename": lambda _self, _name, _identity: ([], {}),
                "search_identity": lambda _self, _identity: ([], {}),
            },
        )
        self.assertEqual(overlapped, serial)

    def test_a_run_where_the_hash_lookup_fails(self) -> None:
        """Only the *order* of the fallback line may differ, so compare sorted."""
        def failing(_self: object, **_kwargs: object) -> list[sf.Candidate]:
            raise RuntimeError("provider unavailable")

        serial, overlapped = self.run_both_ways(
            open={"search": failing, "search_identity": lambda _self, _identity: []},
            subdl={
                "search_filename": lambda _self, _name, identity: (
                    [subdl_candidate(identity.title, identity.year)],
                    {"subdl:sub-1:file-1": sf.SubdlDownload(n_id="sub-1")},
                ),
                "search_identity": lambda _self, _identity: ([], {}),
            },
        )
        self.assertEqual(overlapped[0], serial[0], "same verdict for every movie")
        self.assertEqual(sorted(overlapped[1]), sorted(serial[1]), "same log lines")
        self.assertEqual(overlapped[2], serial[2], "same summary")


class NothingDurableLeavesTheMainThreadTests(OverlapFixture):
    def test_every_checkpoint_is_written_by_the_main_thread(self) -> None:
        seen: set[str] = set()
        real_persist = sf.persist_state

        def persist(state: dict, log_file: Path) -> None:
            seen.add(threading.current_thread().name)
            real_persist(state, log_file)

        library = self.build_library()
        with (
            mock.patch.object(sf, "persist_state", persist),
            mock.patch.object(sf.OpenSubtitlesClient, "search", return_value=[]),
            mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
            mock.patch.object(sf.SubdlClient, "search_filename", return_value=([], {})),
            mock.patch.object(sf.SubdlClient, "search_identity", return_value=([], {})),
        ):
            sf.queue_run(self.cfg(library))
        self.assertEqual(seen, {threading.current_thread().name})

    def test_the_subdl_reservation_is_taken_on_the_main_thread(self) -> None:
        """It is persisted with fsync before each attempt; it cannot race."""
        seen: list[str] = []

        def search_filename(self: sf.SubdlClient, _name: str, _identity: object) -> tuple[list, dict]:
            if self._before_search_request is not None:
                self._before_search_request()
            seen.append(threading.current_thread().name)
            return [], {}

        library = self.build_library()
        with (
            mock.patch.object(sf.OpenSubtitlesClient, "search", return_value=[]),
            mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
            mock.patch.object(sf.SubdlClient, "search_filename", search_filename),
            mock.patch.object(sf.SubdlClient, "search_identity", return_value=([], {})),
        ):
            _results, summary = sf.queue_run(self.cfg(library))
        self.assertEqual(set(seen), {threading.current_thread().name})
        self.assertEqual(summary["subdl_search_requests_reserved"], len(self.TITLES))

    def abandon_after_dispatch(self, failure: Exception) -> tuple[list[sf.JobResult], list]:
        """Run a library where every movie is abandoned once SubDL is asked.

        The hash lookup for that movie is already in flight when it happens, so
        this is the case where a discarded answer could run on into the next
        movie's turn - one movie's request overlapping another movie's, which
        is the ordering the whole tier is built on.
        """
        in_flight: set[str] = set()
        strays: list[tuple[str, frozenset[str]]] = []
        lock = threading.Lock()

        def slow_open(_self: object, *, movie_hash: str, query: str) -> list[sf.Candidate]:
            with lock:
                in_flight.add(query)
            threading.Event().wait(0.05)
            with lock:
                in_flight.discard(query)
            return []

        def failing_subdl(_self: object, name: str, _identity: object) -> tuple[list, dict]:
            with lock:
                stray = frozenset(in_flight - {Path(name).stem})
            if stray:
                strays.append((name, stray))
            raise failure

        library = self.build_library()
        with (
            mock.patch.object(sf.OpenSubtitlesClient, "search", slow_open),
            mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
            mock.patch.object(sf.SubdlClient, "search_filename", failing_subdl),
            mock.patch.object(sf.SubdlClient, "search_identity", return_value=([], {})),
        ):
            results, _summary = sf.queue_run(self.cfg(library))
        self.assertNoStrayThreads()
        return results, strays

    def test_an_exhausted_cap_does_not_leave_the_lookup_running(self) -> None:
        results, strays = self.abandon_after_dispatch(
            sf.SubdlSearchQuotaExhausted("SubDL daily search cap exhausted"),
        )
        self.assertEqual([result.reason for result in results],
                         [sf.REASON_QUOTA] * len(self.TITLES))
        self.assertEqual(strays, [], "another movie's lookup was still in flight")

    def test_a_subdl_error_does_not_leave_the_lookup_running(self) -> None:
        results, strays = self.abandon_after_dispatch(RuntimeError("SubDL is down"))
        self.assertEqual([result.reason for result in results],
                         [sf.REASON_ERROR] * len(self.TITLES))
        self.assertEqual(strays, [], "another movie's lookup was still in flight")

    def test_an_unexpected_failure_still_stops_the_run(self) -> None:
        """Only RuntimeError/ValueError are provider misses; the rest is a bug."""
        def broken(_self: object, **_kwargs: object) -> list[sf.Candidate]:
            raise KeyError("unexpected")

        library = self.build_library(count=1)
        with (
            mock.patch.object(sf.OpenSubtitlesClient, "search", broken),
            mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
            mock.patch.object(sf.SubdlClient, "search_filename", return_value=([], {})),
            mock.patch.object(sf.SubdlClient, "search_identity", return_value=([], {})),
            self.assertRaises(KeyError),
        ):
            sf.queue_run(self.cfg(library))
        self.assertNoStrayThreads()


class WhenTheOverlapIsRefusedTests(OverlapFixture):
    """Cases where the second lookup is not certain, so nothing is dispatched."""

    def record_thread(self) -> tuple[Any, list[str]]:
        seen: list[str] = []

        def search(_self: object, **_kwargs: object) -> list[sf.Candidate]:
            seen.append(threading.current_thread().name)
            return []

        return search, seen

    def run_with(self, cfg: sf.QueueConfig, search: Any) -> None:
        with (
            mock.patch.object(sf.OpenSubtitlesClient, "search", search),
            mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
            mock.patch.object(sf.SubdlClient, "search_filename", return_value=([], {})),
            mock.patch.object(sf.SubdlClient, "search_identity", return_value=([], {})),
        ):
            sf.queue_run(cfg)

    def test_workers_one_keeps_every_request_on_this_thread(self) -> None:
        search, seen = self.record_thread()
        self.run_with(self.cfg(self.build_library(), workers=1), search)
        self.assertEqual(set(seen), {threading.current_thread().name})
        self.assertNoStrayThreads()

    def test_a_run_with_only_opensubtitles_starts_no_worker(self) -> None:
        search, seen = self.record_thread()
        self.run_with(self.cfg(self.build_library(), subdl_api_key=""), search)
        self.assertEqual(set(seen), {threading.current_thread().name})
        self.assertNoStrayThreads()

    def test_without_the_title_year_fallback_the_hash_lookup_stands_alone(self) -> None:
        """A hash hit is then the final answer, so SubDL may never be asked."""
        search, seen = self.record_thread()
        self.run_with(self.cfg(self.build_library(), identity_fallback=False), search)
        self.assertEqual(set(seen), {threading.current_thread().name})

    def test_a_movie_without_a_canonical_identity_is_not_overlapped(self) -> None:
        """No Title (Year) means no SubDL release lookup to overlap with."""
        library = self.root / "odd"
        folder = library / "movie"
        folder.mkdir(parents=True)
        (folder / "movie.mkv").write_bytes(b"x" * sf.MIN_HASH_SIZE)
        search, seen = self.record_thread()
        self.run_with(self.cfg(library), search)
        self.assertEqual(set(seen), {threading.current_thread().name})


class TheEnablingRuleTests(OverlapFixture):
    """When a worker is started at all, and that it is stopped again."""

    def pools_for(self, **overrides: Any) -> list[tuple[sf.SearchPool, bool]]:
        built: list[tuple[sf.SearchPool, bool]] = []
        real_init = sf.SearchPool.__init__

        def spy(pool: sf.SearchPool, *, enabled: bool) -> None:
            real_init(pool, enabled=enabled)
            built.append((pool, enabled))

        library = self.build_library(count=1)
        with (
            mock.patch.object(sf.SearchPool, "__init__", spy),
            mock.patch.object(sf.OpenSubtitlesClient, "search", return_value=[]),
            mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
            mock.patch.object(sf.SubdlClient, "search_filename", return_value=([], {})),
            mock.patch.object(sf.SubdlClient, "search_identity", return_value=([], {})),
        ):
            sf.queue_run(self.cfg(library, **overrides))
        self.assertEqual(len(built), 1, "one pool per run")
        return built

    def enabled_for(self, **overrides: Any) -> bool:
        return self.pools_for(**overrides)[0][1]

    def test_both_providers_and_the_fallback_means_a_worker(self) -> None:
        self.assertTrue(self.enabled_for())

    def test_serial_by_request(self) -> None:
        self.assertFalse(self.enabled_for(workers=1))

    def test_one_provider_has_nothing_to_overlap_with(self) -> None:
        self.assertFalse(self.enabled_for(subdl_api_key=""))
        self.assertFalse(self.enabled_for(api_key=""))

    def test_without_the_fallback_subdl_may_never_be_asked(self) -> None:
        self.assertFalse(self.enabled_for(identity_fallback=False))

    def test_the_run_closes_the_pool_it_opened(self) -> None:
        pool, enabled = self.pools_for()[0]
        self.assertTrue(enabled)
        self.assertFalse(pool.enabled, "the worker outlived the run")


class WhenSubdlHasNothingLeftToSpendTests(OverlapFixture):
    """A configured provider with no capacity is not a second lookup."""

    def test_an_exhausted_download_cap_keeps_the_lookup_on_this_thread(self) -> None:
        seen: list[str] = []

        def search(_self: object, **_kwargs: object) -> list[sf.Candidate]:
            seen.append(threading.current_thread().name)
            return []

        library = self.build_library()
        with (
            mock.patch.object(sf.OpenSubtitlesClient, "search", search),
            mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
            mock.patch.object(
                sf.SubdlClient, "search_filename",
                side_effect=AssertionError("SubDL has no capacity and must not be asked"),
            ),
            mock.patch.object(sf.SubdlClient, "search_identity", return_value=([], {})),
        ):
            sf.queue_run(self.cfg(library, subdl_daily_cap=0))
        self.assertTrue(seen, "the run must have reached the API tier")
        self.assertEqual(set(seen), {threading.current_thread().name})
        self.assertNoStrayThreads()


class TheSharedVerdictTests(unittest.TestCase):
    """Both paths judge the hash search with the same function, on purpose."""

    def cfg(self) -> sf.Config:
        return sf.Config(library=Path("/library"))

    def identity(self) -> sf.MovieIdentity:
        return sf.MovieIdentity(title="Dune Part Two", year=2024,
                                normalized_title="dune part two")

    def test_a_qualifying_candidate_is_picked_and_explained(self) -> None:
        pick, reason = sf.judge_moviehash_candidates(
            [open_candidate("Dune Part Two", 2024)], self.cfg(), self.identity(),
        )
        self.assertIsNotNone(pick)
        self.assertIn("moviehash match", reason)
        self.assertIn("highest download count", reason)

    def test_nothing_usable_is_explained_the_same_way_as_an_empty_answer(self) -> None:
        empty = sf.judge_moviehash_candidates([], self.cfg(), self.identity())
        wrong_movie = sf.judge_moviehash_candidates(
            [open_candidate("Heat", 1995)], self.cfg(), self.identity(),
        )
        self.assertEqual(empty[0], None)
        self.assertEqual(wrong_movie[0], None)
        self.assertEqual(empty[1], wrong_movie[1])
        self.assertIn("no usable Blu-ray English moviehash-matched human SRT", empty[1])


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
