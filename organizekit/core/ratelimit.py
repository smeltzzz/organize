"""Per-host rate limiting: one token bucket per server being asked politely.

Why this exists
---------------
Every provider in this toolkit publishes a rate limit — "no more than one
request per second" — and the tools honoured it the cheapest way possible: a
``_last_call`` timestamp per client object, and a sleep before every request.
That is correct for a single host and wrong for everything else, in two ways
that cost real time and real correctness:

1. **One shared throttle serialised unrelated servers.** The subtitle fetcher's
   scraping tier drives seven different sites through *one* transport object,
   so a request to subf2m waited a full second because the previous request
   went to podnapisi. Nobody was protected by that wait; the run was simply an
   hour longer on a large library. A bucket per host removes it, without ever
   letting a single host be hit faster than its documented limit.
2. **A timestamp cannot be shared between threads.** Two workers checking
   ``now - last > gap`` at the same moment both conclude "yes" and both fire.
   Taking a token is atomic — under the lock, the caller either gets a token or
   is told exactly how long to wait — so N workers pace a host correctly
   without any of them being able to overspend.

The bucket also carries a *penalty* channel: when a provider answers 429 with
``Retry-After: 30``, that is information about the **host**, not about the one
request that happened to be unlucky. ``penalize(30)`` delays every future
caller of that host, which is what the header actually asked for.

Design notes
------------
- **Waiting happens outside the lock, and the wait is reserved.** A caller that
  must wait subtracts its token immediately (the bucket goes into debt) and
  then sleeps. Two callers therefore get *different* wake-up times rather than
  the same one — no thundering herd, and the long-run rate is exactly the
  configured rate.
- **The clock and the sleep are injectable**, so tests can prove the pacing
  arithmetic without spending the wall-clock time it describes. Left alone,
  both are looked up on the ``time`` module at call time, so a test that
  patches ``time.sleep`` (as several already do) keeps working.
- **A gap of zero means unlimited**, because that is what tests and offline
  runs want and an explicit ``if gap:`` at every call site is worse.
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable
from threading import Lock

__all__ = [
    "BucketRegistry",
    "TokenBucket",
    "host_key",
]


def host_key(url: str) -> str:
    """The bucket key for a URL: its lowercase host, port included.

    Politeness is owed to a server, not to a path or a provider name — two
    sources on the same host share one budget, and one source spread over an
    API host and a download host is two independent budgets, which is exactly
    how the servers themselves see it.
    """
    try:
        netloc = urllib.parse.urlsplit(url).netloc
    except ValueError:
        netloc = ""
    host = (netloc or "").rsplit("@", 1)[-1].strip().casefold()
    return host or "unknown-host"


class TokenBucket:
    """Paces one host: at most ``rate`` requests per second, burst ``capacity``.

    ``take()`` blocks for exactly as long as the configured rate requires and
    returns the seconds it slept, so callers can log or assert on it.
    """

    def __init__(
        self,
        *,
        rate: float | None = None,
        gap: float | None = None,
        capacity: float = 1.0,
        name: str = "",
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if rate is None and gap is None:
            raise ValueError("a token bucket needs either a rate or a gap")
        if rate is None:
            # gap <= 0 is "no limit", which is a rate of infinity.
            rate = (1.0 / gap) if (gap or 0.0) > 0 else float("inf")
        self.name = name
        self.rate = float(rate)
        self.capacity = max(1.0, float(capacity))
        self._clock = clock
        self._sleep = sleep
        self._lock = Lock()
        self._tokens = self.capacity
        self._updated = self._now()

    # -- injectable time ---------------------------------------------------

    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.monotonic()

    def _wait(self, seconds: float) -> None:
        if self._sleep is not None:
            self._sleep(seconds)
        else:
            # Resolved on the module at call time, so a test that patches
            # ``time.sleep`` still controls this bucket.
            time.sleep(seconds)

    @property
    def unlimited(self) -> bool:
        return self.rate == float("inf")

    # -- internals ---------------------------------------------------------

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    def _reserve(self, tokens: float) -> float:
        """Claim ``tokens`` and return how long the caller must wait for them."""
        with self._lock:
            now = self._now()
            self._refill(now)
            deficit = tokens - self._tokens
            # The tokens are subtracted whether or not they exist yet: the
            # bucket goes into debt, so the *next* caller is quoted a longer
            # wait instead of the same one.
            self._tokens -= tokens
            return max(0.0, deficit / self.rate) if deficit > 0 else 0.0

    # -- public API --------------------------------------------------------

    def take(self, tokens: float = 1.0) -> float:
        """Block until this host may be asked again. Returns seconds slept."""
        if self.unlimited:
            return 0.0
        wait = self._reserve(tokens)
        if wait > 0:
            self._wait(wait)
        return wait

    def try_take(self, tokens: float = 1.0) -> bool:
        """Take a token only if one is available right now; never sleeps."""
        if self.unlimited:
            return True
        with self._lock:
            self._refill(self._now())
            if self._tokens < tokens:
                return False
            self._tokens -= tokens
            return True

    def available(self) -> float:
        """Tokens available right now (negative while the bucket is in debt)."""
        if self.unlimited:
            return float("inf")
        with self._lock:
            self._refill(self._now())
            return self._tokens

    def penalize(self, seconds: float) -> float:
        """Hold every caller of this host back for at least ``seconds``.

        This is what a ``Retry-After`` header means. It never *shortens* an
        existing wait: a bucket already 30 s in debt stays 30 s in debt when a
        5 s penalty arrives.
        """
        if self.unlimited or seconds <= 0:
            return 0.0
        with self._lock:
            now = self._now()
            self._refill(now)
            current = max(0.0, (1.0 - self._tokens) / self.rate)
            delay = max(current, float(seconds))
            self._tokens = 1.0 - delay * self.rate
            return delay


class BucketRegistry:
    """One bucket per key, created on first use, all sharing one rate.

    Deliberately *not* a module-level global: a registry belongs to the client
    that owns the connections, so tests stay hermetic and two configurations
    can coexist in one process. Threads share it safely.
    """

    def __init__(
        self,
        *,
        rate: float | None = None,
        gap: float | None = None,
        capacity: float = 1.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._rate = rate
        self._gap = gap
        self._capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._lock = Lock()
        self._buckets: dict[str, TokenBucket] = {}

    def bucket(self, key: str) -> TokenBucket:
        with self._lock:
            found = self._buckets.get(key)
            if found is None:
                found = TokenBucket(
                    rate=self._rate, gap=self._gap, capacity=self._capacity,
                    name=key, clock=self._clock, sleep=self._sleep,
                )
                self._buckets[key] = found
            return found

    def take(self, key: str, tokens: float = 1.0) -> float:
        return self.bucket(key).take(tokens)

    def penalize(self, key: str, seconds: float) -> float:
        return self.bucket(key).penalize(seconds)

    def keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._buckets))
