"""A fake ``ffmpeg`` for the audio standardizer's end-to-end tests.

Same contract as ``fake_ffprobe.py``: a real executable the tool spawns (via
``tests/fakebin.py``), never a mock, so argument quoting, exit codes and
buffering all get exercised exactly as in the field.

What it does with a real transcode invocation: it reads the *input* fake
movie (the JSON-header format ``fake_ffprobe.write_movie`` writes), appends
one AC-3 audio stream built from the ``-c:a:N ac3`` / ``-ac:a:N`` /
``-b:a:N`` output options it was handed, and writes the *output* fake movie
in the same format. The tool's own verification pass then ffprobes that
output (through the fake ffprobe) and sees exactly what a real ffmpeg would
have produced: every original stream, plus the appended AC-3.

Environment knobs for the failure branches:

* ``FAKE_FFMPEG_RC`` — exit with this code for any transcode invocation
  (``-version`` keeps working; the tool refuses an ffmpeg that will not run
  at all earlier than that).
* ``FAKE_FFMPEG_NO_OUTPUT`` — exit 0 but write nothing (interrupted encode).
* ``FAKE_FFMPEG_WRONG_TRACK`` — append a non-AC-3 track (verification must
  refuse the publish).
* ``FAKE_FFMPEG_LOG`` — append every full argv as one JSON line to this file,
  so tests can assert the exact command the tool built.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

VERSION_BANNER = "ffmpeg version 7.1 Copyright (c) 2000-2025 the FFmpeg developers"


def _output_option(args: list[str], prefix: str) -> str | None:
    for arg in args:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "-version" in args or "--version" in args:
        print(VERSION_BANNER)
        return 0

    log_path = os.environ.get("FAKE_FFMPEG_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(args) + "\n")

    try:
        from fake_ffprobe import read_payload, write_movie
    except ImportError:  # pragma: no cover - fakebin puts this dir on sys.path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from fake_ffprobe import read_payload, write_movie

    rc = int(os.environ.get("FAKE_FFMPEG_RC", "0"))
    if rc:
        print("Conversion failed!", file=sys.stderr)
        return rc

    # ffmpeg invocation shape: [tool args] -i INPUT [more args] OUTPUT
    try:
        src = Path(args[args.index("-i") + 1])
    except (ValueError, IndexError):
        print("no -i INPUT given", file=sys.stderr)
        return 2
    dst = Path(args[-1])
    if str(dst) == str(src) or dst.name.startswith("-"):
        print("OUTPUT is not a plain path", file=sys.stderr)
        return 2

    try:
        payload = read_payload(src)
    except (OSError, ValueError) as exc:
        print(f"{src}: {exc}", file=sys.stderr)
        return 1

    if os.environ.get("FAKE_FFMPEG_NO_OUTPUT"):
        print("encoded 0 bytes (simulated abort)")
        return 0

    # Find which output audio index the AC-3 options target: -c:a:N.
    ac3_index = None
    for arg in args:
        if arg.startswith("-c:a:"):
            ac3_index = arg.rsplit(":", 1)[-1]
    if ac3_index is None:
        print("expected -c:a:N ac3 options in the fake transcode", file=sys.stderr)
        return 2
    suffix = f"a:{ac3_index}"
    bitrate = _output_option(args, f"-b:{suffix}") or "640k"
    channels = int(_output_option(args, f"-ac:{suffix}") or "6")
    sample_rate = int(_output_option(args, f"-ar:{suffix}") or "48000")
    language = "eng"
    title = ""
    for i, arg in enumerate(args):
        if arg.startswith(f"-metadata:s:{suffix}") and i + 1 < len(args):
            key, _, value = args[i + 1].partition("=")
            if key == "language":
                language = value
            elif key == "title":
                title = value

    next_index = max((int(s.get("index", 0)) for s in payload.get("streams", [])), default=0) + 1
    wrong = bool(os.environ.get("FAKE_FFMPEG_WRONG_TRACK"))
    appended = {
        "index": next_index,
        "codec_type": "audio",
        "codec_name": "dts" if wrong else "ac3",
        "channels": channels,
        "sample_rate": sample_rate,
        "bit_rate": bitrate.rstrip("k") + "000" if bitrate.endswith("k") else bitrate,
        "tags": {"language": language, "title": title},
        "disposition": {"default": 1},
    }
    payload = dict(payload)
    payload["streams"] = list(payload.get("streams", [])) + [appended]
    write_movie(dst, payload, size=1024 * 1024)
    return 0


if __name__ == "__main__":  # pragma: no cover - executed as a child process
    raise SystemExit(main())
