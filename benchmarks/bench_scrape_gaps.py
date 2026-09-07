#!/usr/bin/env python3
"""What per-host rate limiting is worth to the subtitle fetcher's scraping tier.

The scraping tier drives seven different sites through one transport object.
Until now that transport held a single "last request" timestamp, so every
request waited a full second after the previous one *whoever it was sent to* —
a request to subf2m waited because the previous request went to podnapisi.
Nobody was protected by that wait: they are seven separate servers with seven
separate limits.

This measures the difference on the request pattern a real pass produces: for
each movie the chain searches source after source until one answers, and a
source that answers is then asked for the subtitle page and the file (same
host, so those *do* pay the gap, and still do).

Two runs, identical work:

    shared gap  — the previous behaviour, reconstructed here exactly
    per host    — the shipped behaviour (organizekit.core.ratelimit)

Both are driven by a fake clock, so the script reports the seconds a real run
would spend sleeping without spending them. The wall-clock section at the end
re-runs a scaled-down version for real, as a sanity check that the arithmetic
describes something that actually happens.

Reported for 200 movies, 7 sources, 1,800 requests (Python 3.11):

    shared gap : 1,800.0 s of throttling   (30.0 min)
    per host   :   571.0 s of throttling   ( 9.5 min)   3.2x less waiting

The floor is real work, not slack: the busiest single host is asked 258 times
in that pass and is still paced to one request per second, which the script
asserts before printing anything.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import subtitle_fetcher as sf  # noqa: E402  (needs the path bootstrap above)
from organizekit.core import BucketRegistry, host_key  # noqa: E402

MOVIES = 200
GAP = sf.SCRAPE_REQUEST_GAP_SEC

#: The seven scraped sources, in chain order, and the request each pass makes.
HOSTS = (
    "subf2m.co", "www.podnapisi.net", "www.addic7ed.com", "api.subsource.net",
    "subsunacs.net", "yifysubtitles.ch", "subs.sab.bz",
)


class FakeClock:
    """Only moves when something sleeps: exact, and instant."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class SharedGapThrottle:
    """The previous implementation, kept here so the comparison is honest."""

    def __init__(self, gap: float, clock: FakeClock) -> None:
        self.gap = gap
        self.clock = clock
        self._last = 0.0

    def take(self, _url: str) -> None:
        wait = self.gap - (self.clock.time() - self._last)
        if wait > 0:
            self.clock.sleep(wait)
        self._last = self.clock.time()


def request_urls(movies: int) -> list[str]:
    """The URL sequence one pass of the scraping tier produces.

    Every movie is offered to every source (the common case for the tier: it
    exists because the API providers already missed), and the source that
    answers costs two more requests on its own host.
    """
    urls: list[str] = []
    for index in range(movies):
        for host in HOSTS:
            urls.append(f"https://{host}/search?q=movie{index}")
        answering = HOSTS[index % len(HOSTS)]
        urls.append(f"https://{answering}/subtitle/{index}")
        urls.append(f"https://{answering}/download/{index}")
    return urls


def simulate(urls: list[str], *, per_host: bool) -> float:
    clock = FakeClock()
    if per_host:
        registry = BucketRegistry(gap=GAP, clock=clock.time, sleep=clock.sleep)
        for url in urls:
            registry.take(host_key(url))
    else:
        throttle = SharedGapThrottle(GAP, clock)
        for url in urls:
            throttle.take(url)
    return clock.now


def wall_clock(urls: list[str], gap: float) -> tuple[float, float]:
    """Re-run a scaled-down version against the real clock."""
    shared = SharedGapThrottle(gap, clock=_RealClock())
    started = time.perf_counter()
    for url in urls:
        shared.take(url)
    shared_elapsed = time.perf_counter() - started

    registry = BucketRegistry(gap=gap)
    started = time.perf_counter()
    for url in urls:
        registry.take(host_key(url))
    return shared_elapsed, time.perf_counter() - started


class _RealClock:
    def time(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def main() -> int:
    urls = request_urls(MOVIES)
    print(f"{MOVIES} movies, {len(HOSTS)} sources, {len(urls)} requests, gap {GAP:.1f}s per host")

    shared = simulate(urls, per_host=False)
    per_host = simulate(urls, per_host=True)
    print(f"  shared gap : {shared:9.1f}s of throttling  ({shared / 60:5.1f} min)")
    print(f"  per host   : {per_host:9.1f}s of throttling  ({per_host / 60:5.1f} min)"
          f"   {shared / per_host:.1f}x less waiting")

    # Every host must still be paced: the win has to come from removing waits
    # nobody was owed, not from being impolite.
    floor = max(sum(1 for url in urls if host_key(url) == host) - 1 for host in HOSTS) * GAP
    if per_host < floor:
        print(f"FAIL: per-host pacing dropped below the per-site limit ({floor:.1f}s)",
              file=sys.stderr)
        return 1
    print(f"  busiest single host would still be paced to {floor:.1f}s of gaps")

    scaled_gap = 0.002
    small = request_urls(20)
    shared_real, per_host_real = wall_clock(small, scaled_gap)
    print(f"wall clock, 20 movies at a {scaled_gap * 1000:.0f} ms gap "
          f"(the same shape, 500x faster to run):")
    print(f"  shared gap : {shared_real:.3f}s")
    print(f"  per host   : {per_host_real:.3f}s   {shared_real / max(per_host_real, 1e-9):.1f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
