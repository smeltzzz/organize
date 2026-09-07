#!/usr/bin/env python3
"""What connection reuse is worth to the subtitle fetcher.

urllib closes the socket after every response. The fetcher asks the same three
or four hosts for thousands of things in a row, so every request pays for a
fresh TCP handshake and, on https, a fresh TLS handshake as well. This measures
what the pool in ``organizekit.core.nethttp`` removes.

Two numbers are reported, and they are honest about different things:

    connections accepted   counted by the server itself, so it cannot be
                           argued with: 200 requests either open 200 sockets
                           or they open 1

    seconds                loopback, which is the *floor*: there is no network
                           latency here and no TLS, so the saving measured is
                           only Python's own connect/close cost

The second run adds a deliberate delay to every accept, which is what a real
handshake is: one round trip for TCP, two more for TLS. At 40 ms of round trip
(a typical trans-Atlantic provider) that is roughly 120 ms per new connection,
and it is paid once per request today and once per *run* with the pool.

Reported for 200 requests to one host (Python 3.11, loopback):

    stock urllib   200 connections    0.10 s
    pooled           1 connection     0.04 s     2.5x less time, no handshakes

    with a simulated 120 ms handshake
    stock urllib   200 connections   24.26 s
    pooled           1 connection     0.17 s     24.1 s of handshaking, gone

Run it yourself:

    .venv/bin/python benchmarks/bench_keepalive.py --requests 200
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from organizekit.core import nethttp  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Without this the server's header and body writes wait on each other's
    # ACKs and every reused connection appears to cost 40 ms. Real servers set
    # TCP_NODELAY; a benchmark that forgets to is measuring the benchmark.
    disable_nagle_algorithm = True

    def log_message(self, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        body = b'{"data": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CountingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    handshake_delay = 0.0

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.connections = 0

    def get_request(self) -> tuple[socket.socket, object]:
        result = super().get_request()
        self.connections += 1
        if self.handshake_delay:
            # Stands in for the round trips a real TCP+TLS handshake costs.
            time.sleep(self.handshake_delay)
        return result


def measure(opener: urllib.request.OpenerDirector, requests: int,
            handshake_delay: float) -> tuple[int, float]:
    server = CountingServer(("127.0.0.1", 0), Handler)
    server.handshake_delay = handshake_delay
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/subtitles"
    try:
        started = time.perf_counter()
        for _ in range(requests):
            with opener.open(url, timeout=10) as response:
                response.read()
        elapsed = time.perf_counter() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    return server.connections, elapsed


def report(label: str, connections: int, elapsed: float) -> None:
    print(f"    {label:<14} {connections:>5} connections   {elapsed:7.2f} s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--handshake-ms", type=float, default=120.0,
                        help="simulated cost of opening a connection (TCP+TLS round trips)")
    args = parser.parse_args()

    print(f"\n{args.requests} requests to one host, loopback, no simulated handshake")
    stock = measure(urllib.request.build_opener(), args.requests, 0.0)
    report("stock urllib", *stock)
    pool = nethttp.ConnectionPool()
    pooled = measure(nethttp.build_pooled_opener(pool), args.requests, 0.0)
    pool.close_all()
    report("pooled", *pooled)
    if pooled[1] > 0:
        print(f"    {stock[1] / pooled[1]:.1f}x less time, "
              f"{stock[0] - pooled[0]} handshakes avoided")

    delay = args.handshake_ms / 1000.0
    print(f"\nthe same, with a {args.handshake_ms:.0f} ms handshake on every new connection")
    stock = measure(urllib.request.build_opener(), args.requests, delay)
    report("stock urllib", *stock)
    pool = nethttp.ConnectionPool()
    pooled = measure(nethttp.build_pooled_opener(pool), args.requests, delay)
    pool.close_all()
    report("pooled", *pooled)
    if pooled[1] > 0:
        print(f"    {stock[1] / pooled[1]:.1f}x less time, "
              f"{(stock[1] - pooled[1]):.1f} s of handshaking not done\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
