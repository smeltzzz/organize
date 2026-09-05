"""The fetcher's local pre-flight: parallel triage, serial spending.

Every movie the fetcher looks at is first answered locally - is its folder
canonical, does it already have a usable English sidecar, what is its identity.
That work is filesystem-bound and independent per movie, so it runs in a worker
pool. Everything downstream of it - the quota ledger, the provider tiers, the
downloads, the state checkpoints - stays on the single main thread.

These tests hold that line from both sides: the pool must produce exactly the
verdicts and exactly the output the serial run produced, and nothing that
spends a provider request may ever leave the main thread.
"""

from __future__ import annotations

import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import subtitle_fetcher as sf

SAMPLE_SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:03,000\n"
    "Hello there.\n"
    "\n"
    "2\n"
    "00:00:04,000 --> 00:00:06,500\n"
    "General Kenobi.\n"
)

# A numbered console/log line, without the timestamp the log prefixes it with.
LINE_RE = re.compile(r"\[\d+/\d+\] .*")


def make_movie(library: Path, title: str, *, sidecar: str | None = None,
               sidecar_text: str = SAMPLE_SRT) -> Path:
    folder = library / title
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"{title}.mkv"
    video.write_bytes(b"v" * 4096)
    if sidecar is not None:
        folder.joinpath(f"{title}{sidecar}").write_text(sidecar_text, encoding="utf-8")
    return video


class TriageMovieTests(unittest.TestCase):
    """One movie in, one verdict out - the same order of questions as the run."""

    def test_a_noncanonical_layout_is_decided_before_any_sidecar_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            library = Path(td)
            loose = library / "Loose Movie (2020).mkv"
            loose.write_bytes(b"v" * 4096)
            with mock.patch.object(sf, "inspect_existing_sidecars") as inspect:
                verdict = sf.triage_movie(loose, library)
            inspect.assert_not_called()
            self.assertIn("noncanonical layout", verdict.layout_issue)
            self.assertFalse(verdict.fetchable)

    def test_a_validated_sidecar_settles_the_movie_without_an_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            library = Path(td)
            video = make_movie(library, "Covered (2020)", sidecar=".eng.srt")
            verdict = sf.triage_movie(video, library)
            self.assertEqual(verdict.sidecar_status, "covered")
            self.assertEqual(verdict.existing, video.with_name("Covered (2020).eng.srt"))
            self.assertIsNone(verdict.snapshot)
            self.assertFalse(verdict.fetchable)

    def test_an_ambiguous_sidecar_is_held_for_review_with_its_reason(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            library = Path(td)
            video = make_movie(library, "Ambiguous (2020)", sidecar=".en.srt")
            # A legacy .en.srt alongside an occupied canonical name cannot be
            # promoted, which is the reviewable case.
            video.with_name("Ambiguous (2020).eng.srt").write_text("not a subtitle", encoding="utf-8")
            verdict = sf.triage_movie(video, library)
            self.assertEqual(verdict.sidecar_status, "review")
            self.assertEqual(verdict.sidecar_reason, sf.REASON_SIDECAR_NAME)
            self.assertFalse(verdict.fetchable)

    def test_a_missing_sidecar_carries_the_identity_the_run_will_spend_on(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            library = Path(td)
            video = make_movie(library, "Uncovered (2020)")
            verdict = sf.triage_movie(video, library)
            self.assertEqual(verdict.sidecar_status, "missing")
            self.assertTrue(verdict.fetchable)
            self.assertEqual(verdict.snapshot, sf.video_snapshot(video))
            self.assertEqual(verdict.key, sf.movie_key(video, sf.video_snapshot(video)))

    def test_a_legacy_sidecar_is_promoted_in_place_exactly_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            library = Path(td)
            video = make_movie(library, "Legacy (2020)", sidecar=".en.srt")
            verdict = sf.triage_movie(video, library)
            self.assertEqual(verdict.sidecar_status, "covered")
            self.assertTrue(video.with_name("Legacy (2020).eng.srt").is_file())
            self.assertFalse(video.with_name("Legacy (2020).en.srt").exists())

    def test_an_unreadable_identity_is_an_error_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            library = Path(td)
            video = make_movie(library, "Vanished (2020)")
            with mock.patch.object(sf, "video_snapshot", side_effect=OSError("gone")):
                verdict = sf.triage_movie(video, library)
            self.assertEqual(verdict.error, "gone")
            self.assertFalse(verdict.fetchable)


class TriageQueueTests(unittest.TestCase):
    """The pool in front of the loop: ordered, bounded, and fault-isolating."""

    def videos(self, count: int) -> list[Path]:
        return [Path(f"/library/Movie {n:02d}/Movie {n:02d}.mkv") for n in range(1, count + 1)]

    def test_verdicts_are_returned_in_input_order_however_they_finish(self) -> None:
        videos = self.videos(8)

        def slow_first(video: Path, library: Path) -> sf.Triage:
            # The first movie finishes last; ordering must not depend on it.
            if video == videos[0]:
                time.sleep(0.05)
            return sf.Triage(video, sidecar_detail=video.parent.name)

        with mock.patch.object(sf, "triage_movie", slow_first):
            queue = sf.TriageQueue(videos, Path("/library"), workers=4)
            seen = [queue.at(index).video for index in range(1, 9)]
        self.assertEqual(seen, videos)

    def test_the_pool_never_works_more_than_one_chunk_ahead(self) -> None:
        videos = self.videos(40)
        started: list[Path] = []
        lock = threading.Lock()

        def counted(video: Path, library: Path) -> sf.Triage:
            with lock:
                started.append(video)
            return sf.Triage(video)

        with mock.patch.object(sf, "triage_movie", counted):
            queue = sf.TriageQueue(videos, Path("/library"), workers=4, chunk=4)
            queue.at(1)
            self.assertEqual(len(started), 4, "asking for one movie triaged one chunk")
            for index in range(2, 5):
                queue.at(index)
            self.assertEqual(len(started), 4, "the rest of the chunk was already in hand")
            queue.at(5)
            self.assertEqual(len(started), 8)
            self.assertEqual(started, videos[:8])

    def test_a_short_final_chunk_is_handled(self) -> None:
        videos = self.videos(3)
        with mock.patch.object(sf, "triage_movie", lambda video, library: sf.Triage(video)):
            queue = sf.TriageQueue(videos, Path("/library"), workers=8, chunk=4)
            self.assertEqual([queue.at(i).video for i in (1, 2, 3)], videos)

    def test_one_movies_unexpected_failure_does_not_end_the_run(self) -> None:
        videos = self.videos(4)

        def explode_on_third(video: Path, library: Path) -> sf.Triage:
            if video == videos[2]:
                raise RuntimeError("the share went away")
            return sf.Triage(video)

        with mock.patch.object(sf, "triage_movie", explode_on_third):
            queue = sf.TriageQueue(videos, Path("/library"), workers=2, chunk=4)
            verdicts = [queue.at(index) for index in range(1, 5)]
        self.assertEqual([v.video for v in verdicts], videos)
        self.assertEqual(verdicts[2].error, "the share went away")
        self.assertFalse(verdicts[2].fetchable)
        self.assertEqual([v.error for v in verdicts if v.video != videos[2]], ["", "", ""])

    def test_ctrl_c_during_triage_still_stops_the_run(self) -> None:
        videos = self.videos(4)

        def interrupt(video: Path, library: Path) -> sf.Triage:
            raise KeyboardInterrupt

        with mock.patch.object(sf, "triage_movie", interrupt):
            queue = sf.TriageQueue(videos, Path("/library"), workers=2, chunk=4)
            with self.assertRaises(KeyboardInterrupt):
                queue.at(1)

    def test_one_worker_means_no_pool_at_all(self) -> None:
        videos = self.videos(4)
        threads: set[int] = set()

        def record(video: Path, library: Path) -> sf.Triage:
            threads.add(threading.get_ident())
            return sf.Triage(video)

        with mock.patch.object(sf, "triage_movie", record):
            queue = sf.TriageQueue(videos, Path("/library"), workers=1, chunk=4)
            queue.at(1)
        self.assertEqual(threads, {threading.get_ident()})


class OfflineTransport(sf.ScrapeTransport):
    """A transport that answers nothing, and remembers who asked."""

    def __init__(self) -> None:
        super().__init__(gap=0.0)
        self.threads: set[str] = set()

    def _record(self, url: str) -> bytes:
        self.threads.add(threading.current_thread().name)
        raise sf.ScrapeSourceError(f"offline: {url}")

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> bytes:
        return self._record(url)

    def post(self, url: str, form: dict[str, str], *, headers: dict[str, str] | None = None) -> bytes:
        return self._record(url)


class ParallelRunEquivalenceTests(unittest.TestCase):
    """A parallel run must be indistinguishable from the serial one it replaced."""

    TITLES = (
        "Alpha (2001)",     # covered
        "Bravo (2002)",     # legacy sidecar, promoted during triage
        "Charlie (2003)",   # review
        "Delta (2004)",     # covered
        "Echo (2005)",      # uncovered
        "Foxtrot (2006)",   # uncovered
    )

    def build_library(self, root: Path) -> Path:
        library = root / "library"
        make_movie(library, "Alpha (2001)", sidecar=".eng.srt")
        make_movie(library, "Bravo (2002)", sidecar=".en.srt")
        charlie = make_movie(library, "Charlie (2003)", sidecar=".en.srt")
        charlie.with_name("Charlie (2003).eng.srt").write_text("not a subtitle", encoding="utf-8")
        make_movie(library, "Delta (2004)", sidecar=".eng.srt")
        make_movie(library, "Echo (2005)")
        make_movie(library, "Foxtrot (2006)")
        # One movie loose at the library root: the noncanonical-layout branch.
        library.joinpath("Loose (2007).mkv").write_bytes(b"v" * 4096)
        return library

    def cfg(self, root: Path, **overrides: object) -> sf.QueueConfig:
        base: dict[str, object] = {
            "library": root / "library",
            "log_file": root / "fetcher.log",
            "report_file": root / "report.txt",
            "scrape_daily_cap": 20,
            "min_movie_size_mb": 0,
            "extract_embedded": False,
        }
        base.update(overrides)
        return sf.QueueConfig(**base)  # type: ignore[arg-type]

    def run_library(self, root: Path, *, workers: int) -> tuple[list[tuple[str, ...]], list[str], OfflineTransport]:
        self.build_library(root)
        transport = OfflineTransport()
        cfg = self.cfg(root, workers=workers)
        with mock.patch.object(sf, "make_scrape_transport", return_value=transport):
            results, _summary = sf.queue_run(cfg)
        rows = [
            (
                result.video.relative_to(cfg.library).as_posix(),
                result.status,
                result.detail,
                result.reason,
            )
            for result in results
        ]
        log_lines = LINE_RE.findall(cfg.log_file.read_text(encoding="utf-8"))
        return rows, log_lines, transport

    def test_eight_workers_produce_the_one_worker_result_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as td_serial, tempfile.TemporaryDirectory() as td_parallel:
            serial_rows, serial_lines, _ = self.run_library(Path(td_serial), workers=1)
            parallel_rows, parallel_lines, _ = self.run_library(Path(td_parallel), workers=8)
        self.assertEqual(parallel_rows, serial_rows)
        self.assertEqual(parallel_lines, serial_lines)
        statuses = {row[0]: row[1] for row in serial_rows}
        self.assertEqual(statuses["Alpha (2001)/Alpha (2001).mkv"], "have")
        self.assertEqual(statuses["Bravo (2002)/Bravo (2002).mkv"], "have")
        self.assertEqual(statuses["Charlie (2003)/Charlie (2003).mkv"], "review")
        self.assertEqual(statuses["Loose (2007).mkv"], "skip")

    def test_the_side_effects_of_triage_happen_under_the_pool_too(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.run_library(root, workers=8)
            bravo = root / "library" / "Bravo (2002)"
            self.assertTrue(bravo.joinpath("Bravo (2002).eng.srt").is_file())
            self.assertFalse(bravo.joinpath("Bravo (2002).en.srt").exists())

    def test_no_provider_is_ever_asked_from_a_worker_thread(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _rows, _lines, transport = self.run_library(Path(td), workers=8)
        self.assertTrue(transport.threads, "the run must actually have reached the scraping tier")
        self.assertEqual(transport.threads, {threading.current_thread().name})

    def test_a_run_that_stops_early_leaves_the_rest_of_the_library_unread(self) -> None:
        # No provider has any capacity, so the run breaks on the first
        # uncovered movie. Triage must not have read the whole library.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            library = root / "library"
            for n in range(1, 41):
                make_movie(library, f"Movie {n:02d} (20{n:02d})")
            triaged: list[Path] = []
            lock = threading.Lock()
            real = sf.triage_movie

            def counted(video: Path, lib: Path) -> sf.Triage:
                with lock:
                    triaged.append(video)
                return real(video, lib)

            cfg = self.cfg(root, workers=4, scrape_daily_cap=0)
            with mock.patch.object(sf, "triage_movie", counted):
                results, _summary = sf.queue_run(cfg)
        # The quota check fires before the first movie can be offered out, so
        # the run breaks with nothing recorded - and, crucially, having read at
        # most one lookahead chunk of the forty movie folders.
        self.assertEqual(results, [])
        self.assertLessEqual(len(triaged), sf.TRIAGE_LOOKAHEAD)
        self.assertGreaterEqual(len(triaged), 1)


class WorkersFlagTests(unittest.TestCase):
    def test_the_flag_reaches_the_config_and_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            library = root / "library"
            library.mkdir()
            cfg = sf.QueueConfig(library=library, log_file=None, report_file=root / "r.txt",
                                 scrape_daily_cap=20, workers=-1)
            self.assertTrue(
                any("--workers must be non-negative" in error
                    for error in sf.validate_compact_config(cfg)),
                sf.validate_compact_config(cfg),
            )
            self.assertEqual(sf.validate_compact_config(sf.QueueConfig(
                library=library, log_file=None, report_file=root / "r.txt",
                scrape_daily_cap=20, workers=4)), [])

    def test_zero_workers_means_decide_from_the_cpu_count(self) -> None:
        self.assertGreaterEqual(sf.resolve_workers(0, cap=sf.MAX_TRIAGE_WORKERS), 1)
        self.assertLessEqual(sf.resolve_workers(64, cap=sf.MAX_TRIAGE_WORKERS), sf.MAX_TRIAGE_WORKERS)
        self.assertEqual(sf.resolve_workers(4, items=2, cap=sf.MAX_TRIAGE_WORKERS), 2)


if __name__ == "__main__":
    unittest.main()
