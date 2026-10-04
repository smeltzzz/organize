"""A fake ``ffmpeg`` for the audio standardizer's end-to-end tests.

Same contract as ``fake_ffprobe.py``: a real executable the tool spawns (via
``tests/fakebin.py``), never a mock, so argument quoting, exit codes and
buffering all get exercised exactly as in the field.

What it does with a real transcode invocation: it reads the *input* fake
movie (the JSON-header format ``fake_ffprobe.write_movie`` writes), appends
one Dolby audio stream built from the ``-c:a:N <codec>`` / ``-ac:a:N`` /
``-b:a:N`` output options it was handed (``ac3`` under the tv-arc wiring,
``eac3`` on the default soundbar-hdmi-in wiring), and writes the *output*
fake movie in the same format. The tool's own verification pass then
ffprobes that output (through the fake ffprobe) and sees exactly what a
real ffmpeg would have produced: every original stream, plus the appended
Dolby track.

Environment knobs for the failure branches:

* ``FAKE_FFMPEG_RC`` — exit with this code for any transcode invocation
  (``-version`` keeps working; the tool refuses an ffmpeg that will not run
  at all earlier than that).
* ``FAKE_FFMPEG_NO_OUTPUT`` — exit 0 but write nothing (interrupted encode).
* ``FAKE_FFMPEG_WRONG_TRACK`` — append a track of a different codec than
  asked for (verification must refuse the publish).
* ``FAKE_FFMPEG_WRONG_RATE`` — append the track at a sample rate other than the
  ``-ar:a:N`` that was asked for.
* ``FAKE_FFMPEG_IGNORING_DISPOSITION`` — leave every original stream's default
  flag alone and do not set the new track's, i.e. behave like an ffmpeg that
  silently dropped the ``-disposition`` options.
* ``FAKE_FFMPEG_LOG`` — append every full argv as one JSON line to this file,
  so tests can assert the exact command the tool built.

The ``-disposition`` options ARE honoured, in command order and per stream
specifier, because a double that ignored them would hide exactly the drift the
tool's verification pass exists to catch: ``-disposition:a 0`` followed by
``-disposition:a:N default`` is how the appended Dolby track is made the one a
player picks, and a fake that always wrote ``default: 1`` on the new track
while leaving the lossless master default-flagged too would let a build that
dropped both options pass every test.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

VERSION_BANNER = "ffmpeg version 7.1 Copyright (c) 2000-2025 the FFmpeg developers"


def _disposition_options(args: list[str]) -> list[tuple[str, str]]:
    """Every ``-disposition[:spec] value`` pair, in command order.

    Order matters: real ffmpeg applies output options left to right, so the
    LAST one matching a stream is the one that sticks. That is what makes
    ``-disposition:a 0`` (clear every audio stream) followed by
    ``-disposition:a:N default`` (this one is the default) work at all.
    """
    found: list[tuple[str, str]] = []
    for i, arg in enumerate(args):
        if arg.startswith("-disposition") and i + 1 < len(args):
            found.append((arg[len("-disposition"):], args[i + 1]))
    return found


def _spec_matches(spec: str, kind: str, index: int) -> bool:
    """Does ``spec`` (":a", ":a:1", "") select output stream ``index`` of ``kind``?"""
    if not spec:
        return True
    parts = [part for part in spec.split(":") if part != ""]
    if not parts:
        return True
    if parts[0] not in ("a", "audio"):
        return False
    if len(parts) == 1:
        return kind == "audio"
    try:
        return kind == "audio" and int(parts[1]) == index
    except ValueError:
        return False


def _apply_dispositions(streams: list[dict], options: list[tuple[str, str]]) -> None:
    """Set each output stream's disposition from the options that select it."""
    audio_index = 0
    for stream in streams:
        kind = str(stream.get("codec_type") or "")
        if kind != "audio":
            continue
        selected = None
        for spec, value in options:
            if _spec_matches(spec, kind, audio_index):
                selected = value  # last match wins, as in ffmpeg
        if selected is not None:
            # ffmpeg's `-disposition` replaces the whole set: "0" clears every
            # flag, a bare name leaves only that one set.
            value = selected.strip().lstrip("+")
            stream["disposition"] = {} if value == "0" else {value: 1}
        audio_index += 1


def _output_option(args: list[str], prefix: str) -> str | None:
    """The value of an output option, joined ("-b:a:1640k") or split ("-b:a:1", "640k")."""
    for i, arg in enumerate(args):
        if arg.startswith(prefix):
            rest = arg[len(prefix):]
            if rest:
                return rest
            if i + 1 < len(args):
                return args[i + 1]
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

    # Find which output audio index the Dolby options target: -c:a:N <codec>.
    wanted_codec = None
    ac3_index = None
    for i, arg in enumerate(args):
        if arg.startswith("-c:a:") and i + 1 < len(args):
            ac3_index = arg.rsplit(":", 1)[-1]
            wanted_codec = args[i + 1]
    if ac3_index is None or wanted_codec not in ("ac3", "eac3"):
        print("expected -c:a:N ac3|eac3 options in the fake transcode", file=sys.stderr)
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

    if os.environ.get("FAKE_FFMPEG_WRONG_RATE"):
        sample_rate = 44100 if sample_rate != 44100 else 48000

    next_index = max((int(s.get("index", 0)) for s in payload.get("streams", [])), default=0) + 1
    wrong = bool(os.environ.get("FAKE_FFMPEG_WRONG_TRACK"))
    appended = {
        "index": next_index,
        "codec_type": "audio",
        "codec_name": "dts" if wrong else wanted_codec,
        "channels": channels,
        "sample_rate": sample_rate,
        "bit_rate": bitrate.rstrip("k") + "000" if bitrate.endswith("k") else bitrate,
        "tags": {"language": language, "title": title},
        "disposition": {"default": 0},
    }
    payload = dict(payload)
    streams = list(payload.get("streams", [])) + [appended]
    if not os.environ.get("FAKE_FFMPEG_IGNORING_DISPOSITION"):
        _apply_dispositions(streams, _disposition_options(args))
    payload["streams"] = streams
    write_movie(dst, payload, size=1024 * 1024)
    return 0


if __name__ == "__main__":  # pragma: no cover - executed as a child process
    raise SystemExit(main())
