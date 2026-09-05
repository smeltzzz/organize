"""A fake mkvmerge: a real executable, faithful enough to remux against.

The track cleaner's most valuable code is the part that shells out to
mkvmerge, parses its progress, verifies its output and swaps the result in.
Mocking ``_run_mkvmerge`` in-process tests everything *except* that part, so
this module is the other half: a small program that speaks enough of the
mkvmerge command line to be driven by the unmodified tool, launched as a real
subprocess through the real ``subprocess.Popen`` path.

A "movie" here is a text header line of identification JSON followed by
padding, so a file carries its own track list: a remux writes a new header
describing exactly the tracks it kept, which means ``os.replace`` moves the
metadata with the bytes and a second pass sees a genuinely cleaner file.

Supported surface (everything the cleaner actually sends):

* ``--version``                     -> a version banner
* ``-J FILE``                       -> that file's identification JSON
* ``-o OUT [flags] SOURCE``         -> remux, honouring ``--audio-tracks``,
  ``--subtitle-tracks``, ``--no-subtitles``, ``--default-track-flag`` and
  ``--forced-display-flag``
* ``--gui-mode``                    -> ``#GUI#progress N%`` lines on stdout

Environment switches let a test make it misbehave:

* ``FAKE_MKVMERGE_RC``      -> exit with this code after printing an error
* ``FAKE_MKVMERGE_TRUNCATE``-> write a believable but tiny output file
* ``FAKE_MKVMERGE_SLEEP``   -> seconds to linger mid-remux
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

VERSION_BANNER = "mkvmerge v80.0.0 ('Roundabout') 64-bit"


def make_spec(tracks: list[dict], *, duration_ns: int = 6_000_000_000_000) -> dict:
    """Build an identification document in mkvmerge's JSON shape."""
    return {
        "container": {"recognized": True, "supported": True,
                      "properties": {"duration": duration_ns}},
        "tracks": [dict(track, id=index) for index, track in enumerate(tracks)],
        "attachments": [],
        "chapters": [],
    }


def video_track(*, frames: int = 144_000) -> dict:
    return {"type": "video", "codec": "AVC/H.264/MPEG-4p10", "properties": {
        "codec_id": "V_MPEG4/ISO/AVC", "pixel_dimensions": "1920x1080",
        "display_dimensions": "1920x1080", "tag_number_of_frames": str(frames),
        "flag_default": True}}


def audio_track(*, language: str = "eng", name: str = "English TrueHD 7.1",
                codec: str = "TrueHD", codec_id: str = "A_TRUEHD",
                channels: int = 8, default: bool = False,
                commentary: bool = False) -> dict:
    properties = {"codec_id": codec_id, "language": language,
                  "language_ietf": language[:2], "track_name": name,
                  "audio_channels": channels, "audio_sampling_frequency": 48000}
    if default:
        properties["flag_default"] = True
    if commentary:
        properties["flag_commentary"] = True
    return {"type": "audio", "codec": codec, "properties": properties}


def subtitle_track(*, language: str = "eng", name: str = "English",
                   codec: str = "SubRip/SRT", codec_id: str = "S_TEXT/UTF8",
                   forced: bool = False) -> dict:
    properties = {"codec_id": codec_id, "language": language,
                  "language_ietf": language[:2], "track_name": name}
    if forced:
        properties["flag_forced"] = True
    return {"type": "subtitles", "codec": codec, "properties": properties}


def write_movie(path: Path, spec: dict, size: int = 8192) -> None:
    """Write a "movie": one JSON header line, then padding to ``size``."""
    header = json.dumps(spec, separators=(",", ":")).encode("utf-8") + b"\n"
    path.write_bytes(header + b"\0" * max(0, size - len(header)))


def read_spec(path: Path) -> dict:
    with path.open("rb") as handle:
        return json.loads(handle.readline().decode("utf-8"))


def _ids(value: str) -> set[int]:
    return {int(part) for part in value.split(",") if part.strip()}


def _remux(argv: list[str]) -> int:
    output: Path | None = None
    source: Path | None = None
    keep_audio: set[int] | None = None
    keep_subs: set[int] | None = None
    no_subtitles = False
    default_flags: dict[int, bool] = {}
    forced_flags: dict[int, bool] = {}

    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "-o":
            output = Path(argv[index + 1])
            index += 2
        elif argument == "--audio-tracks":
            keep_audio = _ids(argv[index + 1])
            index += 2
        elif argument == "--subtitle-tracks":
            keep_subs = _ids(argv[index + 1])
            index += 2
        elif argument == "--no-subtitles":
            no_subtitles = True
            index += 1
        elif argument in {"--default-track-flag", "--forced-display-flag"}:
            track, _, value = argv[index + 1].partition(":")
            target = default_flags if argument == "--default-track-flag" else forced_flags
            target[int(track)] = value.strip() == "1"
            index += 2
        elif argument in {"--gui-mode", "--quiet"}:
            index += 1
        elif argument.startswith("-"):
            index += 2  # an option this fake does not model: skip its value
        else:
            source = Path(argument)
            index += 1

    if output is None or source is None:
        print("Error: no output or source file given", file=sys.stderr)
        return 2
    if not source.is_file():
        print(f"Error: The file '{source}' could not be opened for reading: "
              "no such file or directory.", file=sys.stderr)
        return 2

    spec = read_spec(source)
    kept: list[dict] = []
    for track in spec["tracks"]:
        kind, track_id = track["type"], int(track["id"])
        if kind == "audio" and keep_audio is not None and track_id not in keep_audio:
            continue
        if kind == "subtitles" and (no_subtitles or
                                    (keep_subs is not None and track_id not in keep_subs)):
            continue
        properties = dict(track["properties"])
        if track_id in default_flags:
            properties["flag_default"] = default_flags[track_id]
        if track_id in forced_flags:
            properties["flag_forced"] = forced_flags[track_id]
        kept.append(dict(track, properties=properties))

    if "--gui-mode" in argv:
        for percent in (0, 25, 50, 75, 100):
            print(f"#GUI#progress {percent}%", flush=True)
            time.sleep(float(os.environ.get("FAKE_MKVMERGE_SLEEP", "0")) / 5)

    failure_code = int(os.environ.get("FAKE_MKVMERGE_RC", "0"))
    if failure_code:
        print("Error: The demuxer for the file could not be created.", flush=True)
        print("mkvmerge: aborting.", file=sys.stderr, flush=True)
        return failure_code

    size = 512 if os.environ.get("FAKE_MKVMERGE_TRUNCATE") else int(source.stat().st_size * 0.75)
    write_movie(output, make_spec(kept, duration_ns=spec["container"]["properties"]["duration"]),
                size=size)
    print("Multiplexing took 0 seconds.", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(VERSION_BANNER)
        return 0
    if "-J" in argv:
        target = Path(argv[argv.index("-J") + 1])
        if not target.is_file():
            print(f"Error: The file '{target}' could not be opened for reading.",
                  file=sys.stderr)
            return 2
        try:
            print(json.dumps(read_spec(target)))
        except (OSError, ValueError):
            print(json.dumps({"container": {"recognized": False, "supported": False},
                              "tracks": [], "attachments": [], "chapters": []}))
        return 0
    return _remux(argv)


if __name__ == "__main__":
    sys.exit(main())
