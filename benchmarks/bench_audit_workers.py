#!/usr/bin/env python3
"""What the auditor's parallelism is for, and what it is not for.

The audit is thousands of directory reads and almost no computation. On a local
SSD `stat()` costs microseconds and threads cost more than they save, so the
honest local answer is "no faster". The case it exists for is a library on a
NAS, where every folder is a network round trip; the second half of this
benchmark simulates that by delaying each folder read.

Reported on 600 folders (Python 3.11, 2 cores, tmpfs):

    tmpfs     workers=1: 0.05s   workers=8: 0.14s   (no win; threads cost)
    5 ms RTT  workers=1: 3.13s   workers=8: 0.40s   (7.8x)

tmpfs is the worst case for this, not the typical one: a real disk pays a seek
per folder and a NAS pays a packet, both of which put it in the second row.

The audit itself is identical either way - the same folders in the same order -
which the script asserts rather than assumes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import library_auditor as la  # noqa: E402  (needs the path bootstrap above)

FOLDERS = 600
LATENCY_SECONDS = 0.005
SIDECAR = ("1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n"
           "2\n00:00:05,000 --> 00:00:07,000\nGeneral Kenobi.\n\n")


def build_library(root: Path) -> Path:
    library = root / "lib"
    library.mkdir()
    for index in range(FOLDERS):
        name = f"Movie {index:04d} (2001)"
        folder = library / name
        folder.mkdir()
        (folder / f"{name}.mkv").write_bytes(os.urandom(4096))
        if index % 3:
            (folder / f"{name}.eng.srt").write_text(SIDECAR, encoding="utf-8")
    return library


def timed(library: Path, workers: int) -> tuple[float, list[tuple[str, str]]]:
    started = time.perf_counter()
    audit = la.audit_library(la.Config(source_dir=library, workers=workers))
    elapsed = time.perf_counter() - started
    return elapsed, [(item.folder.name, item.state) for item in audit.folders]


def main() -> int:
    la.log = lambda *args, **kwargs: None  # the benchmark is the output here
    with tempfile.TemporaryDirectory(prefix="bench_audit_") as tmp:
        library = build_library(Path(tmp))
        verdicts: list[list[tuple[str, str]]] = []

        for label, latency in (("local storage", 0.0), (f"{LATENCY_SECONDS * 1000:.0f} ms round trip", LATENCY_SECONDS)):
            real_classify = la.classify_folder
            if latency:
                def classify(folder: Path, _real=real_classify, _wait=latency):
                    time.sleep(_wait)
                    return _real(folder)
                la.classify_folder = classify
            print(f"{FOLDERS} folders, {label}")
            baseline = 0.0
            for workers in (1, 2, 4, 8):
                elapsed, states = timed(library, workers)
                verdicts.append(states)
                baseline = baseline or elapsed
                print(f"  workers={workers}: {elapsed:6.2f}s  ({baseline / elapsed:4.1f}x)")
            la.classify_folder = real_classify

        if any(states != verdicts[0] for states in verdicts):
            print("FAIL: the audit changed with the worker count", file=sys.stderr)
            return 1
        print("every run produced the identical audit, in the identical order")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
