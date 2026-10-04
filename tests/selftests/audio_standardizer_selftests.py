"""The offline self-tests for ``audio_standardizer.py``.

Pattern: identical to the other moved suites — assertions live here, are
rebound to the tool's namespace by :func:`bind_to_tool`, and run as part of
``tests/test_selftests.py``. No media binaries, no fixtures on disk: every
check feeds the planner a hand-built ffprobe payload.
"""

from __future__ import annotations

from typing import Any

import audio_standardizer as tool
from tests.selftests import bind_to_tool


def _assert(cond: bool, msg: str, errors: list[str]) -> None:
    if not cond:
        errors.append(msg)


def _payload(*audio_streams: dict[str, Any], duration: str = "7200.000000") -> dict[str, Any]:
    streams = [{"index": 0, "codec_type": "video", "codec_name": "hevc"}]
    for position, stream in enumerate(audio_streams, start=1):
        entry = {"index": position, "codec_type": "audio", "channels": 6,
                 "tags": {"language": "eng"}, "disposition": {"default": position == 1}}
        entry.update(stream)
        streams.append(entry)
    return {"streams": streams, "format": {"duration": duration, "size": "8388608"}}


def run_self_tests() -> int:
    errors: list[str] = []
    base_cfg = Config(dry_run=True)

    # -- classification of the planner --------------------------------------
    truehd_only = _payload({"codec_name": "truehd", "channels": 8})
    v = plan_for_payload("m.mkv", truehd_only, base_cfg)
    _assert(v.status == STATUS_PLANNED, f"TrueHD-only plans a transcode, got {v.status}", errors)
    _assert(v.target is not None and v.target.codec == "eac3"
            and v.target.channels == FFMPEG_DOLBY_ENCODE_MAX_CHANNELS,
            "a 7.1 TrueHD folds to the Dolby Digital Plus 5.1 its encoder can "
            "actually write on the default wiring", errors)
    _assert(v.target is not None and v.target.bitrate == "640k",
            "the synthesis ceiling is 640 kbps", errors)
    v_arc = plan_for_payload("m.mkv", truehd_only, Config(dry_run=True, wiring=WIRING_TV_ARC))
    _assert(v_arc.target is not None and v_arc.target.codec == "ac3" and v_arc.target.channels == 6,
            "the tv-arc alternative still targets AC-3 5.1", errors)
    _assert(v.source_stream == 1, "the transcode reads the TrueHD stream", errors)

    eac3 = _payload({"codec_name": "eac3", "channels": 6})
    v = plan_for_payload("m.mkv", eac3, base_cfg)
    _assert(v.status == STATUS_NATIVE, f"E-AC-3 is done, got {v.status}", errors)

    ac3 = _payload({"codec_name": "ac3", "channels": 6})
    v = plan_for_payload("m.mkv", ac3, base_cfg)
    _assert(v.status == STATUS_NATIVE, f"AC-3 is done, got {v.status}", errors)

    # A lossless master + an existing chain-native track: nothing to do — the
    # cleaner will keep the AC-3 and drop the master.
    both = _payload({"codec_name": "truehd", "channels": 8}, {"codec_name": "ac3", "channels": 6})
    v = plan_for_payload("m.mkv", both, base_cfg)
    _assert(v.status == STATUS_NATIVE, f"an existing AC-3 settles a TrueHD file, got {v.status}", errors)

    dts = _payload({"codec_name": "dts", "channels": 6})
    v = plan_for_payload("m.mkv", dts, base_cfg)
    _assert(v.status == STATUS_DTS, f"base DTS accepted by default, got {v.status}", errors)
    v = plan_for_payload("m.mkv", dts, Config(dry_run=True, dts_passthrough_ok=False))
    _assert(v.status == STATUS_PLANNED, "--no-dts-passthrough transcodes base DTS", errors)

    flac = _payload({"codec_name": "flac", "channels": 6})
    v = plan_for_payload("m.mkv", flac, base_cfg)
    _assert(v.status == STATUS_PCM,
            f"multichannel FLAC plays as multichannel PCM on the DEFAULT "
            f"soundbar-hdmi-in wiring, got {v.status}", errors)
    v = plan_for_payload("m.mkv", flac, Config(dry_run=True, wiring=WIRING_TV_ARC))
    _assert(v.status == STATUS_PLANNED,
            f"the explicit tv-arc alternative makes multichannel FLAC a "
            f"transcode candidate, got {v.status}", errors)

    # A track title never decides the codec: an actual E-AC-3 stream titled
    # like a lossless master stays chain-native (the false positive this
    # suite's regression tests pin).
    titled = _payload({"codec_name": "eac3", "profile": "unknown", "channels": 6,
                       "tags": {"language": "eng", "title": "TrueHD 7.1"}})
    v = plan_for_payload("m.mkv", titled, base_cfg)
    _assert(v.status == STATUS_NATIVE,
            f"an E-AC-3 stream titled 'TrueHD 7.1' must stay native, got {v.status}", errors)
    _assert(v.target is None, "a chain-native E-AC-3 stream gets no AC-3 target", errors)

    dtshd = _payload({"codec_name": "dts", "profile": "DTS-HD MA", "channels": 8})
    v = plan_for_payload("m.mkv", dtshd, base_cfg)
    _assert(v.status == STATUS_PLANNED, "DTS-HD MA plans a transcode", errors)

    unknown = _payload({"codec_name": "gsm_ms", "channels": 2})
    v = plan_for_payload("m.mkv", unknown, base_cfg)
    _assert(v.status == STATUS_REVIEW, "unknown audio is reviewed, never touched", errors)

    commentary = _payload(
        {"codec_name": "truehd", "channels": 8},
        {"codec_name": "eac3", "channels": 2, "tags": {"language": "eng", "title": "Director Commentary"}},
    )
    v = plan_for_payload("m.mkv", commentary, base_cfg)
    _assert(v.status == STATUS_PLANNED and v.source_stream == 1,
            "commentary is never the transcode source", errors)

    foreign = _payload({"codec_name": "truehd", "channels": 8, "tags": {"language": "jpn"}},
                       {"codec_name": "dts", "channels": 6, "tags": {"language": "eng"}})
    # A Japanese film: the TrueHD (native) is the source; the English DTS dub
    # must not win the pool even though DTS is the more playable codec.
    v = plan_for_payload("m.mkv", foreign, base_cfg)
    _assert(v.status == STATUS_PLANNED and v.source_stream == 1 and v.source_lang == "jpn",
            "the native language pool picks the transcode source, not codec playability",
            errors)

    # -- the ffmpeg command is the one contract the verification checks -----
    v = plan_for_payload("m.mkv", truehd_only, Config(dry_run=False))
    cmd = build_ffmpeg_command(Config(ffmpeg="ffmpeg"), Path("/lib/M/m.mkv"),
                             Path("/lib/M/.m.audiofit-1.tmp.mkv"), v, total_audio_streams=1)
    _assert("-map" in cmd and "0:1" in cmd, "the command maps the TrueHD source stream", errors)
    _assert("eac3" in cmd, "the command encodes Dolby Digital Plus on the default wiring", errors)
    _assert("640k" in cmd, "the command uses the 640 kbps ceiling bitrate", errors)
    _assert(cmd[-1].endswith(".tmp.mkv"), "the command writes the temp file", errors)
    _assert("language=eng" in cmd, "the appended track keeps the source language tag", errors)

    # -- verification: only a strict superset may publish --------------------
    # The appended track's shape is DERIVED from the planned target, never
    # hardcoded: when 8.4.0 capped the Dolby targets at the encoder's 5.1, a
    # literal here would have "verified" a file the tool could no longer make.
    t = v.target
    added = {"index": 2, "codec_type": "audio", "codec_name": t.codec,
             "channels": t.channels}
    good_new = dict(truehd_only)
    good_new["streams"] = list(truehd_only["streams"]) + [added]
    # patch the probe of the produced file: the verifier reads run_ffprobe
    original_run_ffprobe = run_ffprobe
    try:
        def fake_run(binary: str, file_path: Path, cfg: Config) -> dict[str, Any]:
            return good_new
        globals()["run_ffprobe"] = fake_run
        ok, why = verify_output(Path("/tmp/out.mkv"), v, Config(), truehd_only)
        _assert(ok, f"a clean superset verifies ({why})", errors)

        bad_new = dict(truehd_only)  # nothing appended
        globals()["run_ffprobe"] = lambda b, p, c: bad_new
        ok, why = verify_output(Path("/tmp/out.mkv"), v, Config(), truehd_only)
        _assert(not ok, "a file with no new track is refused", errors)

        wrong = dict(truehd_only)
        wrong["streams"] = list(truehd_only["streams"]) + [dict(added, codec_name="dts")]
        globals()["run_ffprobe"] = lambda b, p, c: wrong
        ok, why = verify_output(Path("/tmp/out.mkv"), v, Config(), truehd_only)
        _assert(not ok, "an appended track of the wrong codec is refused", errors)

        drifted = dict(truehd_only)
        drifted["streams"] = list(truehd_only["streams"]) + [added]
        drifted["format"] = {"duration": "10.000000"}
        globals()["run_ffprobe"] = lambda b, p, c: drifted
        ok, why = verify_output(Path("/tmp/out.mkv"), v, Config(), truehd_only)
        _assert(not ok, "a duration drift beyond tolerance is refused", errors)
    finally:
        globals()["run_ffprobe"] = original_run_ffprobe

    # -- discovery hygiene ----------------------------------------------------
    _assert(is_junk_name("sample-clip.mkv"), "sample names stay out of the scan", errors)
    _assert(not is_junk_name("The Sampler (2012).mkv"), "a real title is not a sample", errors)
    _assert(is_skipped_dir("Extras"), "extras folders stay out", errors)

    if errors:
        print("SELF-TEST FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print("SELF-TEST PASSED (chain classification + planning + command + verification)")
    return 0


_assert = bind_to_tool(tool, _assert)
tool._assert = _assert
_payload = bind_to_tool(tool, _payload)
tool._payload = _payload
run_self_tests = bind_to_tool(tool, run_self_tests)
tool.run_self_tests = run_self_tests
