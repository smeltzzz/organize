"""A fake ffprobe: a real executable, faithful enough to inspect a library with.

``bitdepth.py``'s verdicts are unit-tested against payload dictionaries, which
covers the classification rules and nothing around them - launching the
binary, parsing what comes back, containing a probe that fails, caching the
answer, and turning a library's worth of verdicts into exit codes a cron job
acts on. That plumbing is the part a mocked ``run_ffprobe`` never touches.

So a "movie" here carries its own ffprobe answer: one line of JSON followed by
padding. The fake reads that line and prints it, which means a test writes the
technical properties it wants to test and the unmodified tool discovers,
probes and classifies them through the real subprocess path.

Environment switches let a test make it misbehave:

* ``FAKE_FFPROBE_RC``      -> exit with this code, with a message on stderr
* ``FAKE_FFPROBE_GARBAGE`` -> print something that is not JSON
* ``FAKE_FFPROBE_SLEEP``   -> linger, so the caller's timeout fires
* ``FAKE_FFPROBE_VERSION_RC`` -> fail the ``-version`` handshake
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

VERSION_BANNER = "ffprobe version 7.1 Copyright (c) 2007-2025 the FFmpeg developers"


def video_stream(
    *,
    codec_name: str = "hevc",
    profile: str = "Main 10",
    pix_fmt: str = "yuv420p10le",
    bits_per_raw_sample: str | None = None,
    width: int = 1920,
    height: int = 1080,
    color_transfer: str = "bt709",
    color_primaries: str = "bt709",
    color_space: str = "bt709",
    side_data_list: list[dict] | None = None,
    duration: str = "7200.000000",
) -> dict:
    """One video stream in ffprobe's ``-show_entries`` shape."""
    stream: dict = {
        "index": 0,
        "codec_type": "video",
        "codec_name": codec_name,
        "profile": profile,
        "pix_fmt": pix_fmt,
        "width": width,
        "height": height,
        "color_transfer": color_transfer,
        "color_primaries": color_primaries,
        "color_space": color_space,
        "duration": duration,
        "disposition": {"attached_pic": 0},
    }
    if bits_per_raw_sample is not None:
        stream["bits_per_raw_sample"] = bits_per_raw_sample
    if side_data_list is not None:
        stream["side_data_list"] = side_data_list
    return stream


def cover_art_stream(*, width: int = 3000, height: int = 4500) -> dict:
    """The poster ffprobe also calls a video stream."""
    return {
        "index": 1,
        "codec_type": "video",
        "codec_name": "mjpeg",
        "pix_fmt": "yuvj420p",
        "width": width,
        "height": height,
        "disposition": {"attached_pic": 1},
    }


def audio_stream(*, codec_name: str = "eac3", channels: int = 6) -> dict:
    return {"index": 2, "codec_type": "audio", "codec_name": codec_name,
            "channels": channels}


def make_payload(*streams: dict, duration: str = "7200.000000",
                 size: str = "8388608") -> dict:
    return {
        "streams": list(streams) or [video_stream()],
        "format": {"duration": duration, "size": size, "bit_rate": "9000000"},
    }


# -- the shapes the classifier has to tell apart -----------------------------

def sdr_8bit() -> dict:
    """The HandBrake candidate: ordinary 8-bit SDR."""
    return make_payload(video_stream(
        codec_name="h264", profile="High", pix_fmt="yuv420p",
        bits_per_raw_sample="8",
    ))


def sdr_10bit() -> dict:
    return make_payload(video_stream(bits_per_raw_sample="10"))


def hdr10() -> dict:
    return make_payload(video_stream(
        pix_fmt="yuv420p10le", bits_per_raw_sample="10",
        color_transfer="smpte2084", color_primaries="bt2020",
        color_space="bt2020nc",
    ))


def hdr_8bit() -> dict:
    """Mis-tagged: PQ transfer on an 8-bit stream. Never an SDR candidate."""
    return make_payload(video_stream(
        codec_name="h264", profile="High", pix_fmt="yuv420p",
        bits_per_raw_sample="8", color_transfer="smpte2084",
        color_primaries="bt2020",
    ))


def unknown_depth() -> dict:
    """Nothing says how many bits: neither pixel format nor profile nor tag."""
    return make_payload(video_stream(
        codec_name="mpeg2video", profile="", pix_fmt="", bits_per_raw_sample=None,
    ))


def cover_art_only() -> dict:
    return make_payload(cover_art_stream(), audio_stream())


def write_movie(path: Path, payload: dict, size: int = 8 * 1024 * 1024) -> None:
    """Write a "movie": one JSON header line, then padding to ``size``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    path.write_bytes(header + b"\0" * max(0, size - len(header)))


def read_payload(path: Path) -> dict:
    with path.open("rb") as handle:
        return json.loads(handle.readline().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "-version" in args or "--version" in args:
        rc = int(os.environ.get("FAKE_FFPROBE_VERSION_RC", "0"))
        print(VERSION_BANNER if rc == 0 else "fake ffprobe is unwell")
        return rc

    sleep = float(os.environ.get("FAKE_FFPROBE_SLEEP", "0") or 0)
    if sleep:
        time.sleep(sleep)

    rc = int(os.environ.get("FAKE_FFPROBE_RC", "0"))
    if rc:
        print(f"{args[-1]}: Invalid data found when processing input", file=sys.stderr)
        return rc

    if os.environ.get("FAKE_FFPROBE_GARBAGE"):
        print("<!DOCTYPE html><html>this is not ffprobe output</html>")
        return 0

    target = Path(args[-1])
    try:
        payload = read_payload(target)
    except (OSError, ValueError) as exc:
        print(f"{target}: {exc}", file=sys.stderr)
        return 1
    # A movie may carry its own failure, so one file in a library can be
    # unreadable while the rest probe cleanly.
    failure = payload.get("_fail")
    if failure:
        print(f"{target}: {failure}", file=sys.stderr)
        return 1
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":  # pragma: no cover - executed as a child process
    raise SystemExit(main())
