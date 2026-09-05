"""The worker pool the library tools share.

Three tools do one independent unit of work per movie and were bound by
something that is not the CPU. This module is what makes them parallel, so its
guarantees have to be exact rather than approximately true:

* ``--workers 1`` is a real escape hatch - the work runs inline, in order, in
  the calling thread, with no executor involved at all;
* ``map_ordered`` returns results in *input* order however they finish, which
  is what lets the auditor stay byte-identical while running in parallel;
* one failing item is captured as data, never allowed to abort a sweep that has
  already done real work.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from organizekit.core import parallel  # noqa: E402  (needs the path bootstrap)


class ResolveWorkers(unittest.TestCase):
    def test_zero_asks_the_machine(self) -> None:
        self.assertGreaterEqual(parallel.resolve_workers(0), 1)

    def test_never_more_workers_than_jobs(self) -> None:
        self.assertEqual(3, parallel.resolve_workers(16, items=3))
        self.assertEqual(1, parallel.resolve_workers(16, items=1))

    def test_the_cap_is_a_cap(self) -> None:
        self.assertEqual(4, parallel.resolve_workers(99, items=100, cap=4))

    def test_no_items_means_no_clamp(self) -> None:
        """A caller that only wants the number for a banner passes no count."""
        self.assertEqual(4, parallel.resolve_workers(4, cap=8))

    def test_negative_is_treated_as_automatic(self) -> None:
        self.assertGreaterEqual(parallel.resolve_workers(-5), 1)

    def test_describe_is_honest_about_serial(self) -> None:
        self.assertEqual("1 (serial)", parallel.describe_workers(1))
        self.assertIn("parallel", parallel.describe_workers(4, "sidecar"))


class SerialPathIsTrulySerial(unittest.TestCase):
    """`--workers 1` must be indistinguishable from the loop it replaced."""

    def test_work_runs_in_the_calling_thread(self) -> None:
        main = threading.current_thread()
        threads = [o.value for o in parallel.map_ordered(
            [1, 2, 3], lambda _i: threading.current_thread(), workers=1)]
        self.assertTrue(all(t is main for t in threads))

    def test_one_job_never_starts_a_pool(self) -> None:
        main = threading.current_thread()
        (outcome,) = parallel.map_ordered(
            ["only"], lambda _i: threading.current_thread(), workers=8)
        self.assertIs(main, outcome.value)

    def test_order_is_input_order(self) -> None:
        seen: list[int] = []
        parallel.map_ordered([3, 1, 2], seen.append, workers=1)
        self.assertEqual([3, 1, 2], seen)

    def test_no_items_no_work(self) -> None:
        self.assertEqual([], parallel.map_ordered([], lambda x: x, workers=4))


class OrderedResultsSurviveParallelism(unittest.TestCase):
    def test_results_come_back_in_input_order(self) -> None:
        """The whole point: the slowest job first must still report first."""
        def work(item: int) -> int:
            time.sleep(0.02 if item == 0 else 0.0)
            return item * 10

        outcomes = parallel.map_ordered(list(range(6)), work, workers=4)
        self.assertEqual(list(range(6)), [o.index for o in outcomes])
        self.assertEqual([0, 10, 20, 30, 40, 50], [o.value for o in outcomes])

    def test_the_item_travels_with_its_result(self) -> None:
        outcomes = parallel.map_ordered(["a", "b"], str.upper, workers=2)
        self.assertEqual([("a", "A"), ("b", "B")],
                         [(o.item, o.value) for o in outcomes])


class WorkReallyOverlaps(unittest.TestCase):
    def test_four_workers_run_four_jobs_at_once(self) -> None:
        """A barrier that only clears if the jobs are genuinely concurrent.

        Without real parallelism this times out rather than passing slowly, so
        the test cannot silently degrade into asserting nothing.
        """
        barrier = threading.Barrier(4, timeout=10)

        def work(_item: int) -> int:
            return barrier.wait()

        outcomes = parallel.map_ordered(list(range(4)), work, workers=4)
        self.assertTrue(all(o.ok for o in outcomes),
                        "jobs did not run concurrently: the barrier timed out")


class FailuresAreDataNotCrashes(unittest.TestCase):
    def test_one_bad_item_does_not_stop_the_sweep(self) -> None:
        def work(item: int) -> int:
            if item == 2:
                raise ValueError("unreadable movie")
            return item

        outcomes = parallel.map_ordered([1, 2, 3], work, workers=2)
        self.assertEqual([True, False, True], [o.ok for o in outcomes])
        self.assertIsInstance(outcomes[1].error, ValueError)
        self.assertEqual([1, None, 3], [o.value for o in outcomes])

    def test_the_same_holds_serially(self) -> None:
        outcomes = parallel.map_ordered(
            [1, 2], lambda i: 1 / (i - 1), workers=1)
        self.assertIsInstance(outcomes[0].error, ZeroDivisionError)
        self.assertTrue(outcomes[1].ok)


class CompletionOrder(unittest.TestCase):
    def test_everything_is_yielded_exactly_once(self) -> None:
        outcomes = list(parallel.iter_completed(list(range(20)), lambda i: i, workers=4))
        self.assertEqual(set(range(20)), {o.index for o in outcomes})
        self.assertEqual(20, len(outcomes))

    def test_serial_completion_is_input_order(self) -> None:
        outcomes = list(parallel.iter_completed([5, 6, 7], lambda i: i, workers=1))
        self.assertEqual([5, 6, 7], [o.item for o in outcomes])

    def test_a_fast_job_does_not_wait_for_a_slow_one(self) -> None:
        def work(item: int) -> int:
            time.sleep(0.25 if item == 0 else 0.0)
            return item

        first = next(iter(parallel.iter_completed([0, 1], work, workers=2)))
        self.assertEqual(1, first.item, "results must arrive as they finish")


class InterruptionStaysAnInterruption(unittest.TestCase):
    def test_ctrl_c_propagates_to_the_tool(self) -> None:
        """Each tool writes its partial report in a KeyboardInterrupt handler.

        Swallowing it here would turn Ctrl-C into a silent full-speed run.
        """
        def work(item: int) -> int:
            if item == 0:
                raise KeyboardInterrupt
            return item

        with self.assertRaises(KeyboardInterrupt):
            parallel.map_ordered(list(range(4)), work, workers=2)

        with self.assertRaises(KeyboardInterrupt):
            parallel.map_ordered([0], work, workers=1)


if __name__ == "__main__":
    unittest.main()
