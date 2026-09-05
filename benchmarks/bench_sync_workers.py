#!/usr/bin/env python3
"""How much does parallel measurement buy `sync_subtitles.py`?

ffsubsync is the slowest thing the toolchain does: it decodes a movie's audio
and correlates it against the subtitle, taking tens of seconds to minutes per
movie. That cost is what this benchmark stands in for - a fake ffsubsync that
sleeps for a fixed time and writes a plausible corrected sidecar - so the
number produced here is the *scheduling* win, with no ffmpeg on the machine and
no library required.

Reported on 8 movies at 0.5 s per sync (Python 3.11, 2 cores):

    workers=1:   4.26s
    workers=2:   2.19s
    workers=4:   1.19s   (3.6x)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOVIES = 8
SECONDS_PER_SYNC = 0.5

FAKE_FFSUBSYNC = '''#!/usr/bin/env python3
import os, sys, time
args = sys.argv[1:]
if "--help" in args or "-h" in args:
    print("usage: ffs [--strict] [--skip-sync-on-low-quality] [-o SRTOUT]")
    raise SystemExit(0)
if "--version" in args:
    print("ffsubsync 0.4.25")
    raise SystemExit(0)
time.sleep(float(os.environ.get("FAKE_SYNC_SECONDS", "0.5")))
open(args[args.index("-o") + 1], "w", encoding="utf-8").write(
    "1\\n00:00:02,500 --> 00:00:04,000\\nHello there.\\n\\n"
    "2\\n00:00:06,000 --> 00:00:08,000\\nGeneral Kenobi.\\n\\n")
print("INFO: score: 551.000", file=sys.stderr)
print("INFO: offset seconds: -3.950", file=sys.stderr)
print("INFO: framerate scale factor: 1.000", file=sys.stderr)
'''

SIDECAR = ("1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n"
           "2\n00:00:05,000 --> 00:00:07,000\nGeneral Kenobi.\n\n")


def build_library(root: Path) -> tuple[Path, Path]:
    """A fresh library each run: the previous run rewrote every sidecar."""
    library, output = root / "lib", root / "out"
    shutil.rmtree(library, ignore_errors=True)
    library.mkdir(parents=True)
    output.mkdir(exist_ok=True)
    for index in range(MOVIES):
        name = f"Film {index:02d} (20{index:02d})"
        folder = library / name
        folder.mkdir()
        (folder / f"{name}.mkv").write_bytes(b"\x1a\x45\xdf\xa3" + os.urandom(2048))
        (folder / f"{name}.eng.srt").write_text(SIDECAR, encoding="utf-8")
    return library, output


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bench_sync_") as tmp:
        root = Path(tmp)
        binaries = root / "bin"
        binaries.mkdir()
        ffsubsync = binaries / "fake_ffsubsync"
        ffsubsync.write_text(FAKE_FFSUBSYNC, encoding="utf-8")
        ffsubsync.chmod(0o755)
        # sync_subtitles refuses to run without ffmpeg, which the fake never
        # calls; a stub satisfies the check without installing FFmpeg.
        ffmpeg = binaries / "ffmpeg"
        ffmpeg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        ffmpeg.chmod(0o755)

        env = dict(os.environ,
                   FAKE_SYNC_SECONDS=str(SECONDS_PER_SYNC),
                   PATH=f"{binaries}{os.pathsep}{os.environ['PATH']}")
        print(f"{MOVIES} movies, {SECONDS_PER_SYNC:g}s per ffsubsync run")
        baseline = 0.0
        for workers in (1, 2, 4):
            library, output = build_library(root)
            started = time.perf_counter()
            proc = subprocess.run(
                [sys.executable, str(REPO / "sync_subtitles.py"),
                 "--source", str(library),
                 "--log", str(output / "sync.log"),
                 "--report", str(output / "sync_report.txt"),
                 "--sync-ledger", str(output / "sync_state.json"),
                 "--ffsubsync", str(ffsubsync),
                 "--workers", str(workers)],
                capture_output=True, text=True, env=env, cwd=str(REPO))
            elapsed = time.perf_counter() - started
            baseline = baseline or elapsed
            synced = proc.stdout.count("SYNCED")
            print(f"  workers={workers}: {elapsed:6.2f}s  ({baseline / elapsed:4.1f}x)  "
                  f"exit={proc.returncode}  synced={synced}/{MOVIES}")
            if proc.returncode != 0 or synced != MOVIES:
                print(proc.stdout[-2000:], file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
