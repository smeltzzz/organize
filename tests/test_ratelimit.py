"""Unit tests for ``organizekit/core/ratelimit.py``.

Every timing assertion here runs on a fake clock: the point is to prove the
pacing arithmetic, and a test that proves it by actually sleeping is both slow
and flaky. The one test that uses real threads asserts on *reserved waits*,
not on wall-clock time, for the same reason.
"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from organizekit.core import BucketRegistry, TokenBucket, host_key


class FakeClock:
    """A monotonic clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TokenBucketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()

    def _bucket(self, **kw: object) -> TokenBucket:
        kw.setdefault("gap", 1.0)
        return TokenBucket(clock=self.clock.time, sleep=self.clock.sleep, **kw)  # type: ignore[arg-type]

    def test_the_first_request_never_waits(self) -> None:
        self.assertEqual(self._bucket().take(), 0.0)

    def test_back_to_back_requests_are_paced_by_the_gap(self) -> None:
        bucket = self._bucket(gap=1.1)
        self.assertEqual(bucket.take(), 0.0)
        self.assertAlmostEqual(bucket.take(), 1.1)
        self.assertAlmostEqual(bucket.take(), 1.1)

    def test_time_spent_elsewhere_counts_towards_the_gap(self) -> None:
        # The request itself took 0.8 s, so only 0.2 s of politeness is left.
        bucket = self._bucket()
        bucket.take()
        self.clock.advance(0.8)
        self.assertAlmostEqual(bucket.take(), 0.2)

    def test_a_long_pause_does_not_bank_unlimited_credit(self) -> None:
        bucket = self._bucket(capacity=1.0)
        bucket.take()
        self.clock.advance(3600.0)
        self.assertEqual(bucket.take(), 0.0)
        self.assertAlmostEqual(bucket.take(), 1.0, msg="capacity 1 means no burst")

    def test_capacity_allows_a_bounded_burst(self) -> None:
        bucket = self._bucket(capacity=3.0)
        self.assertEqual([bucket.take() for _ in range(3)], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(bucket.take(), 1.0)

    def test_a_zero_gap_is_unlimited(self) -> None:
        bucket = self._bucket(gap=0.0)
        self.assertTrue(bucket.unlimited)
        self.assertEqual([bucket.take() for _ in range(5)], [0.0] * 5)
        self.assertEqual(self.clock.slept, [])

    def test_rate_and_gap_are_two_ways_to_say_the_same_thing(self) -> None:
        self.assertEqual(TokenBucket(rate=2.0).rate, 2.0)
        self.assertEqual(TokenBucket(gap=0.5).rate, 2.0)
        with self.assertRaises(ValueError):
            TokenBucket()

    def test_try_take_never_sleeps(self) -> None:
        bucket = self._bucket()
        self.assertTrue(bucket.try_take())
        self.assertFalse(bucket.try_take())
        self.assertEqual(self.clock.slept, [])
        self.clock.advance(1.0)
        self.assertTrue(bucket.try_take())

    def test_available_reports_the_debt(self) -> None:
        bucket = self._bucket()
        bucket.take()
        bucket.take()  # goes one token into debt, having slept for it
        self.assertAlmostEqual(bucket.available(), 0.0, places=6)

    # -- penalties ---------------------------------------------------------

    def test_penalize_holds_the_next_caller_back(self) -> None:
        bucket = self._bucket()
        self.assertAlmostEqual(bucket.penalize(30.0), 30.0)
        self.assertAlmostEqual(bucket.take(), 30.0)

    def test_penalize_never_shortens_an_existing_wait(self) -> None:
        bucket = self._bucket()
        bucket.penalize(30.0)
        self.assertAlmostEqual(bucket.penalize(5.0), 30.0)
        self.assertAlmostEqual(bucket.take(), 30.0)

    def test_penalize_applies_to_every_caller_of_that_host(self) -> None:
        # A Retry-After is information about the server, so a second caller
        # must not sail past it.
        bucket = self._bucket()
        bucket.take()
        bucket.penalize(10.0)
        self.assertAlmostEqual(bucket.take(), 10.0)
        self.assertAlmostEqual(bucket.take(), 1.0)

    def test_a_zero_or_negative_penalty_does_nothing(self) -> None:
        bucket = self._bucket()
        self.assertEqual(bucket.penalize(0.0), 0.0)
        self.assertEqual(bucket.penalize(-5.0), 0.0)
        self.assertEqual(bucket.take(), 0.0)

    # -- concurrency -------------------------------------------------------

    def test_concurrent_takers_are_quoted_different_waits(self) -> None:
        """The property a ``last_call`` timestamp cannot provide.

        Four threads asking at the same instant must be spaced 0, 1, 2, 3 gaps
        apart - not all told "you may go now" because each read the same
        timestamp before any of them wrote it back.
        """
        bucket = TokenBucket(gap=1.0, clock=lambda: 0.0, sleep=lambda _s: None)
        start = Barrier(4)

        def ask() -> float:
            start.wait(timeout=5)
            return bucket.take()

        with ThreadPoolExecutor(max_workers=4) as pool:
            waits = sorted(pool.map(lambda _i: ask(), range(4)))
        self.assertEqual([round(w, 6) for w in waits], [0.0, 1.0, 2.0, 3.0])

    def test_the_long_run_rate_is_the_configured_rate(self) -> None:
        bucket = self._bucket(gap=0.25)
        for _ in range(20):
            bucket.take()
        self.assertAlmostEqual(self.clock.now - 1000.0, 19 * 0.25, places=6)


class HostKeyTests(unittest.TestCase):
    def test_host_is_lowercased_and_keeps_its_port(self) -> None:
        self.assertEqual(host_key("https://Example.COM/a/b?c=d"), "example.com")
        self.assertEqual(host_key("https://example.com:8443/x"), "example.com:8443")

    def test_credentials_are_not_part_of_the_key(self) -> None:
        self.assertEqual(host_key("https://user:pw@example.com/x"), "example.com")

    def test_two_paths_on_one_host_share_a_key(self) -> None:
        self.assertEqual(host_key("https://a.example.com/search"),
                         host_key("https://a.example.com/download/1"))

    def test_subdomains_are_separate_servers(self) -> None:
        self.assertNotEqual(host_key("https://api.example.com/x"),
                            host_key("https://dl.example.com/x"))

    def test_an_unparseable_url_still_yields_a_key(self) -> None:
        self.assertEqual(host_key("not a url"), "unknown-host")
        self.assertEqual(host_key(""), "unknown-host")


class BucketRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.registry = BucketRegistry(gap=1.0, clock=self.clock.time, sleep=self.clock.sleep)

    def test_one_bucket_per_key_created_on_first_use(self) -> None:
        self.assertEqual(self.registry.keys(), ())
        first = self.registry.bucket("a.example.com")
        self.assertIs(first, self.registry.bucket("a.example.com"))
        self.registry.bucket("b.example.com")
        self.assertEqual(self.registry.keys(), ("a.example.com", "b.example.com"))

    def test_different_hosts_do_not_wait_for_each_other(self) -> None:
        # The whole point of the change: seven scraped sources are seven
        # servers, and a request to one must not be delayed by a request to
        # another.
        waits = [self.registry.take(f"host{index}.example.com") for index in range(7)]
        self.assertEqual(waits, [0.0] * 7)
        self.assertEqual(self.clock.slept, [])

    def test_the_same_host_is_still_paced(self) -> None:
        self.assertEqual(self.registry.take("one.example.com"), 0.0)
        self.assertAlmostEqual(self.registry.take("one.example.com"), 1.0)

    def test_penalizing_one_host_leaves_the_others_alone(self) -> None:
        self.registry.penalize("slow.example.com", 30.0)
        self.assertAlmostEqual(self.registry.take("slow.example.com"), 30.0)
        self.assertEqual(self.registry.take("fast.example.com"), 0.0)

    def test_registries_are_independent_of_each_other(self) -> None:
        other = BucketRegistry(gap=1.0, clock=self.clock.time, sleep=self.clock.sleep)
        self.registry.take("shared.example.com")
        self.assertEqual(other.take("shared.example.com"), 0.0,
                         "a registry belongs to its client; there is no global state")


if __name__ == "__main__":
    unittest.main()
