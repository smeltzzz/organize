#!/usr/bin/env python3
"""What overlapping the two tier-1 provider lookups is worth - and when.

Every movie that reaches the fetcher's API tier with the title/year fallback
enabled is offered to both providers: OpenSubtitles for an exact moviehash
match, SubDL for a scored release-name match. Their answers are pooled, so both
lookups happen for every movie - one of them just used to wait for the other.
They are two companies, two connections, two separate per-host rate limits;
nothing about asking them in sequence was ever required by either of them.

This measures the loop rather than a model of it: two real HTTP servers on
loopback, the tool's real per-host token buckets, and the real one-worker
executor the fetcher uses. Each server sleeps for the round trip before
answering.

The answer depends on one ratio - the provider's round trip against the
per-host gap - and it is worth being blunt about it:

    round trip vs gap   what dominates            overlapping is worth
    much smaller        the 1.1 s courtesy gap    nothing, measurably
    about half          the two are comparable    a little
    larger              the providers themselves  up to 2x

Measured, 20 movies, 0.10 s host gap (Python 3.11, loopback):

    round trip   serial   overlapped
      0.02 s      1.94 s     1.92 s     1.0x   pacing-bound: no change
      0.05 s      2.02 s     1.95 s     1.0x   pacing-bound: no change
      0.10 s      4.02 s     2.05 s     2.0x   provider-bound: halved
      0.30 s     12.04 s     6.13 s     2.0x   provider-bound: halved

The crossover is where the two round trips stop fitting inside one gap, i.e. at
a round trip of about half the gap. Scaled to the shipped 1.1 s per-host gap
that is a provider taking 0.55 s to answer: below it - a healthy provider at
0.25 s - this changes nothing, and saying so plainly is the point of the
script. What it removes is the case where the run actually hurts: when a
provider is slow or timing out, the *other* provider's lookup no longer queues
behind it and the tier costs one round trip instead of two. The gaps are
unchanged either way, which the script asserts before it prints.

Run it yourself:

    .venv/bin/python benchmarks/bench_provider_overlap.py --movies 20
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from organizekit.core import BucketRegistry, host_key, nethttp  # noqa: E402

RTT = 0.05


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def log_message(self, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        time.sleep(RTT)  # the round trip to a provider on another continent
        body = b'{"data": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}/subtitles"


class Provider:
    """One provider: its own host, its own bucket, its own gap."""

    def __init__(self, url: str, buckets: BucketRegistry) -> None:
        self.url = url
        self._buckets = buckets
        self.times: list[float] = []

    def search(self, movie: int) -> bytes:
        self._buckets.take(host_key(self.url))
        self.times.append(time.perf_counter())
        with urllib.request.urlopen(f"{self.url}?movie={movie}", timeout=10) as response:  # nosec B310
            return response.read()


def worst_gap(provider: Provider) -> float:
    """The closest two requests to one host ever came. Never below the gap."""
    deltas = [later - earlier
              for earlier, later in zip(provider.times, provider.times[1:], strict=False)]
    return min(deltas) if deltas else float("inf")


def gaps_respected(provider: Provider, gap: float) -> bool:
    """Every request to one host is still at least ``gap`` after the last.

    The tolerance is scheduler noise, not slack in the rule: a sleep can return
    a hair early, and this runs on a machine with other things to do.
    """
    return worst_gap(provider) >= gap * 0.95


def run(movies: int, gap: float, *, overlapped: bool) -> tuple[float, Provider, Provider]:
    buckets = BucketRegistry(gap=gap)
    open_url, subdl_url = URLS
    opensubtitles = Provider(open_url, buckets)
    subdl = Provider(subdl_url, buckets)
    pool = ThreadPoolExecutor(max_workers=1) if overlapped else None
    started = time.perf_counter()
    for movie in range(movies):
        if pool is None:
            opensubtitles.search(movie)
            subdl.search(movie)
        else:
            in_flight = pool.submit(opensubtitles.search, movie)
            subdl.search(movie)
            in_flight.result()
    elapsed = time.perf_counter() - started
    if pool is not None:
        pool.shutdown(wait=True)
    return elapsed, opensubtitles, subdl


def main() -> int:
    global RTT, URLS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--movies", type=int, default=20)
    parser.add_argument("--gap", type=float, default=0.10,
                        help="per-host request gap (shipped: 1.1 s)")
    parser.add_argument("--rtt", type=float, nargs="*", default=[0.02, 0.05, 0.10, 0.30],
                        help="round trips to sweep (shipped gap is 1.1 s, so 0.25 s "
                             "of real latency is the 0.02 row here)")
    args = parser.parse_args()

    first, first_thread, open_url = start_server()
    second, second_thread, subdl_url = start_server()
    URLS = (open_url, subdl_url)
    nethttp.ensure_pooled_opener()
    print(f"\n{args.movies} movies, {args.gap:.2f} s host gap")
    print("    round trip   serial   overlapped")
    try:
        for rtt in args.rtt:
            RTT = rtt
            serial, open_serial, subdl_serial = run(args.movies, args.gap, overlapped=False)
            overlapped, open_par, subdl_par = run(args.movies, args.gap, overlapped=True)
            print(f"      {rtt:.2f} s     {serial:5.2f} s    {overlapped:5.2f} s"
                  f"     {serial / overlapped:.1f}x")
            for label, provider in (("OpenSubtitles", open_serial), ("SubDL", subdl_serial),
                                    ("OpenSubtitles", open_par), ("SubDL", subdl_par)):
                if not gaps_respected(provider, args.gap):
                    print(f"    !! {label} was asked faster than its own rate limit: "
                          f"{worst_gap(provider):.4f} s apart, gap is {args.gap:.4f} s")
                    return 1
    finally:
        for server, thread in ((first, first_thread), (second, second_thread)):
            server.shutdown()
            server.server_close()
            thread.join(5)
        nethttp.reset_pooled_opener()

    print("\n    every provider still paced to one request per gap, both ways.")
    print("    The ratio that decides this is round trip vs gap: at the shipped")
    print("    1.1 s gap a healthy 0.25 s provider is the top row - no change -")
    print("    and a provider that has gone slow is the bottom one.\n")
    return 0


URLS: tuple[str, str] = ("", "")

if __name__ == "__main__":
    raise SystemExit(main())
