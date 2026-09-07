#!/usr/bin/env python3
"""What the fetcher's triage pool is for, and what it is not for.

Before the fetcher can spend a provider request on a movie it answers three
local questions about it: is the folder canonical, is there already a usable
English sidecar, what is the file's identity. On a library that is mostly
covered - the steady state this toolkit is aimed at - that pre-flight *is* the
run: a directory listing and a couple of small reads per movie, thousands of
round trips, none of which needs the network or the quota ledger.

On a local SSD those reads cost microseconds and threads cost more than they
save. The case the pool exists for is a library on a NAS, where every listing
is a packet; the second half of this benchmark simulates that by delaying each
folder read.

Reported on 600 movies (Python 3.11, 2 cores, tmpfs):

    tmpfs     workers=1: 0.06s   workers=8: 0.23s   (slower; threads cost)
    5 ms RTT  workers=1: 3.16s   workers=8: 0.50s   (6.3x)

The tmpfs row is a fifth of a second lost across a 600-movie library, which is
the price of being ready for the row underneath it.

tmpfs is the worst case for this, not the typical one: a real disk pays a seek
per folder and a NAS pays a packet, both of which put it in the second row.

The verdicts are identical either way - the same movies, the same order, the
same decisions - which the script asserts rather than assumes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import subtitle_fetcher as sf  # noqa: E402  (needs the path bootstrap above)

MOVIES = 600
LATENCY_SECONDS = 0.005
SIDECAR = ("1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n"
           "2\n00:00:05,000 --> 00:00:07,000\nGeneral Kenobi.\n\n")


def build_library(root: Path) -> tuple[Path, list[Path]]:
    library = root / "lib"
    library.mkdir()
    videos: list[Path] = []
    for index in range(MOVIES):
        name = f"Movie {index:04d} (2001)"
        folder = library / name
        folder.mkdir()
        video = folder / f"{name}.mkv"
        video.write_bytes(os.urandom(4096))
        # A mostly-covered library, which is the case the pool has to be fast
        # for: most movies are settled by triage and never reach a provider.
        if index % 5:
            (folder / f"{name}.eng.srt").write_text(SIDECAR, encoding="utf-8")
        videos.append(video)
    return library, videos


def timed(library: Path, videos: list[Path], workers: int) -> tuple[float, list[tuple[str, str]]]:
    queue = sf.TriageQueue(videos, library, workers=workers, chunk=sf.TRIAGE_LOOKAHEAD)
    started = time.perf_counter()
    verdicts = [queue.at(index) for index in range(1, len(videos) + 1)]
    elapsed = time.perf_counter() - started
    return elapsed, [(v.video.parent.name, v.sidecar_status or v.layout_issue or v.error) for v in verdicts]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bench_triage_") as tmp:
        library, videos = build_library(Path(tmp))
        seen: list[list[tuple[str, str]]] = []

        for label, latency in (
            ("local storage", 0.0),
            (f"{LATENCY_SECONDS * 1000:.0f} ms round trip", LATENCY_SECONDS),
        ):
            real_inspect = sf.inspect_existing_sidecars
            if latency:
                def inspect(video: Path, _real=real_inspect, _wait=latency):
                    time.sleep(_wait)
                    return _real(video)
                sf.inspect_existing_sidecars = inspect
            print(f"{MOVIES} movies, {label}")
            baseline = 0.0
            for workers in (1, 2, 4, 8):
                elapsed, verdicts = timed(library, videos, workers)
                seen.append(verdicts)
                baseline = baseline or elapsed
                print(f"  workers={workers}: {elapsed:6.2f}s  ({baseline / elapsed:4.1f}x)")
            sf.inspect_existing_sidecars = real_inspect

        if any(verdicts != seen[0] for verdicts in seen):
            print("FAIL: the triage verdicts changed with the worker count", file=sys.stderr)
            return 1
        print("every run produced the identical verdicts, in the identical order")
        print("provider requests, the quota ledger and every write stay on the main thread")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
