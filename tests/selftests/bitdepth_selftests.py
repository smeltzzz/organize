"""The offline self-tests lifted out of ``bitdepth.py``.

These assertions used to ship inside the tool itself. They are unchanged; only
their address is different. Each function is rebound to the tool module's
namespace by :func:`bind_to_tool`, so a body that reads or patches a module
global (``globals()["_movie_upgrade_decision"] = ...``,  ``global CFG``)
affects the tool exactly as it did when it lived there.

``tests/test_selftests.py`` runs them as part of the normal unit suite.
"""

from __future__ import annotations

import bitdepth as tool
from tests.selftests import bind_to_tool


def _assert(cond: bool, msg: str, errors: list[str]) -> None:
    if not cond:
        errors.append(msg)


def run_self_tests() -> int:
    errors: list[str] = []

    def probe(name: str, stream: dict[str, Any], fmt: dict[str, Any] | None = None) -> ProbeResult:
        payload: dict[str, Any] = {"streams": [stream]}
        if fmt is not None:
            payload["format"] = fmt
        return result_from_probe(name, payload, size_bytes=1_000_000)

    sdr8 = probe("a.mkv", {
        "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920, "height": 1080,
        "color_transfer": "bt709", "color_primaries": "bt709",
        "disposition": {"attached_pic": 0},
    })
    _assert(sdr8.status == STATUS_QUEUE, f"8-bit SDR should queue, got {sdr8.status}", errors)
    _assert(sdr8.bit_depth == 8, f"8-bit depth, got {sdr8.bit_depth}", errors)
    _assert(not sdr8.hdr, "8-bit SDR must not be HDR", errors)

    sdr10 = probe("b.mkv", {
        "codec_name": "hevc", "profile": "Main 10", "pix_fmt": "yuv420p10le",
        "width": 1920, "height": 1080, "color_transfer": "bt709",
        "disposition": {"attached_pic": 0},
    })
    _assert(sdr10.status == STATUS_SKIP_SDR, f"10-bit SDR should skip, got {sdr10.status}", errors)
    _assert(sdr10.bit_depth == 10, f"10-bit, got {sdr10.bit_depth}", errors)

    hdr10 = probe("c.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p10le", "bits_per_raw_sample": "10",
        "width": 3840, "height": 2160,
        "color_transfer": "smpte2084", "color_primaries": "bt2020",
        "side_data_list": [
            {"side_data_type": "Mastering display metadata"},
            {"side_data_type": "Content light level metadata"},
        ],
        "disposition": {"attached_pic": 0},
    })
    _assert(hdr10.status == STATUS_SKIP_HDR, f"HDR10 should keep, got {hdr10.status}", errors)
    _assert("HDR10" in hdr10.hdr_flavors, f"flavors {hdr10.hdr_flavors}", errors)

    dv = probe("d.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p10le",
        "width": 3840, "height": 2160,
        "side_data_list": [{"side_data_type": "DOVI configuration record"}],
        "disposition": {"attached_pic": 0},
    })
    _assert(dv.status == STATUS_SKIP_HDR, f"DV should keep, got {dv.status}", errors)
    _assert("Dolby Vision" in dv.hdr_flavors, f"DV flavors {dv.hdr_flavors}", errors)

    hlg = probe("e.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p10le",
        "color_transfer": "arib-std-b67", "color_primaries": "bt2020",
        "disposition": {"attached_pic": 0},
    })
    _assert(hlg.status == STATUS_SKIP_HDR and "HLG" in hlg.hdr_flavors, f"HLG {hlg}", errors)

    plus = probe("f.mkv", {
        "codec_name": "hevc", "pix_fmt": "p010le",
        "color_transfer": "smpte2084",
        "side_data_list": [{"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}],
        "disposition": {"attached_pic": 0},
    })
    _assert("HDR10+" in plus.hdr_flavors, f"HDR10+ flavors {plus.hdr_flavors}", errors)
    _assert(plus.status == STATUS_SKIP_HDR, "HDR10+ keep", errors)

    # The original script's worst bug: BT.2020 primaries ≠ HDR
    wcg = probe("g.mkv", {
        "codec_name": "h264", "pix_fmt": "yuv420p",
        "color_transfer": "bt709", "color_primaries": "bt2020",
        "disposition": {"attached_pic": 0},
    })
    _assert(wcg.status == STATUS_QUEUE, f"WCG SDR 8-bit should QUEUE, got {wcg.status}", errors)
    _assert(not wcg.hdr, "BT.2020 + bt709 is not HDR", errors)
    _assert("WCG" in wcg.info, f"should mention WCG: {wcg.info}", errors)

    # The original's other bug: 8-bit + PQ dumped into the SDR HandBrake queue
    bad = probe("h.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p",
        "color_transfer": "smpte2084", "color_primaries": "bt2020",
        "disposition": {"attached_pic": 0},
    })
    _assert(bad.status == STATUS_REVIEW_8BIT_HDR, f"8-bit HDR must REVIEW, got {bad.status}", errors)

    # Cover art must not win
    mixed = result_from_probe("i.mkv", {"streams": [
        {"codec_name": "mjpeg", "pix_fmt": "yuvj420p", "width": 600, "height": 900,
         "disposition": {"attached_pic": 1}},
        {"codec_name": "hevc", "pix_fmt": "yuv420p10le", "width": 1920, "height": 800,
         "color_transfer": "bt709", "disposition": {"attached_pic": 0}},
    ]})
    _assert(mixed.bit_depth == 10 and mixed.status == STATUS_SKIP_SDR, f"cover-art mix {mixed}", errors)

    # bits_per_raw_sample wins over a misleading 8-bit-looking default
    raw = probe("j.mkv", {
        "codec_name": "ffv1", "pix_fmt": "something_custom",
        "bits_per_raw_sample": "12",
        "disposition": {"attached_pic": 0},
    })
    _assert(raw.bit_depth == 12 and raw.status == STATUS_SKIP_SDR, f"12-bit raw {raw}", errors)

    unknown = probe("k.mkv", {
        "codec_name": "unknown", "pix_fmt": "custom_layout", "profile": "",
        "width": 1920, "height": 1080, "disposition": {"attached_pic": 0},
    })
    _assert(unknown.status == STATUS_REVIEW_UNKNOWN_DEPTH, f"unknown depth must review, got {unknown.status}", errors)
    _assert(unknown.bit_depth is None, f"unknown depth should remain None, got {unknown.bit_depth}", errors)

    multi_feature = result_from_probe("l.mkv", {"streams": [
        {"index": 0, "codec_type": "video", "pix_fmt": "yuv420p10le", "width": 64, "height": 64, "disposition": {"attached_pic": 0}},
        {"index": 1, "codec_type": "video", "pix_fmt": "yuv420p", "width": 1920, "height": 1080, "disposition": {"attached_pic": 0}},
        {"index": 2, "codec_type": "video", "pix_fmt": "yuv420p", "width": 4000, "height": 4000, "disposition": {"attached_pic": 1}},
    ]})
    _assert(multi_feature.status == STATUS_QUEUE and multi_feature.width == 1920, f"main feature selection {multi_feature}", errors)

    matroska_cover = result_from_probe("cover.mkv", {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p10le",
         "width": 1920, "height": 804},
        {"index": 2, "codec_type": "video", "codec_name": "mjpeg", "pix_fmt": "yuvj420p",
         "bits_per_raw_sample": "8", "width": 2000, "height": 3000,
         "tags": {"filename": "cover.jpg", "mimetype": "image/jpeg"}},
    ]})
    _assert(matroska_cover.status == STATUS_SKIP_SDR and matroska_cover.width == 1920,
            f"Matroska MIME-tagged cover excluded {matroska_cover}", errors)

    _assert(bit_depth_from_pix_fmt("yuv420p") == 8, "yuv420p", errors)
    _assert(bit_depth_from_pix_fmt("yuv420p10le") == 10, "p10le", errors)
    _assert(bit_depth_from_pix_fmt("p010le") == 10, "p010", errors)
    _assert(bit_depth_from_pix_fmt("yuv420p12le") == 12, "p12", errors)

    # Discovery skips extras / samples
    tmp = Path(tempfile.mkdtemp(prefix="hb_ins_"))
    try:
        movie = tmp / "Movie (1999)"
        extra = movie / "Featurettes"
        extra.mkdir(parents=True)
        (movie / "Movie (1999).mkv").write_bytes(b"x" * (120 * 1024 * 1024))
        (extra / "Making-Of.mkv").write_bytes(b"y" * (120 * 1024 * 1024))
        (movie / "Movie (1999)-sample.mkv").write_bytes(b"z" * (120 * 1024 * 1024))
        cfg = Config(source_dir=tmp, min_file_size_mb=100)
        found = discover_videos(tmp, cfg)
        names = {p.name for p in found}
        _assert(names == {"Movie (1999).mkv"}, f"canonical discovery {names}", errors)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Report includes errors (original dropped them)
    fake_cfg = Config(source_dir=Path("X:\\lib"))
    text = build_report([
        ProbeResult("a.mkv", STATUS_QUEUE, CATEGORY_LABELS[STATUS_QUEUE], "info", size_bytes=1),
        ProbeResult("e.mkv", STATUS_ERROR, CATEGORY_LABELS[STATUS_ERROR], "boom", error="boom"),
    ], fake_cfg, 1.0)
    _assert("UNREADABLE" in text and "boom" in text, "report must include errors", errors)
    _assert("QUEUE FOR HANDBRAKE" in text, "report queue heading", errors)

    report_dir = Path(tempfile.mkdtemp(prefix="hb_report_"))
    try:
        report_cfg = Config(source_dir=Path("X:\\lib"), report_file=report_dir / "report.txt")
        _assert(write_report([sdr8], report_cfg, 0.1), "atomic report write", errors)
        _assert(report_cfg.report_file.is_file(), "single report exists", errors)
        # The default cache lives in the tool's own output dir, so the report
        # directory still holds exactly one artifact.
        _assert(not list(report_dir.glob("*.json")), "no JSON side output", errors)
        _assert(not list(report_dir.glob("*.tmp")), "no staged report remains", errors)

        # Probe cache: reused only while size and mtime agree, and a cache that
        # cannot be read is a miss rather than an error.
        cache_path = report_dir / "probe_cache.json"
        cache = MediaProbeCache(cache_path, tool="10bit")
        _assert(cache.get("movie.mkv", 100, 5) is None, "cold cache is a miss", errors)
        cache.put("movie.mkv", 100, 5, {"streams": []})
        _assert(cache.get("movie.mkv", 100, 5) == {"streams": []}, "warm cache is a hit", errors)
        _assert(cache.get("movie.mkv", 101, 5) is None, "size change invalidates", errors)
        _assert(cache.get("movie.mkv", 100, 6) is None, "mtime change invalidates", errors)
        cache.save()
        reloaded = MediaProbeCache(cache_path, tool="10bit")
        _assert(reloaded.get("movie.mkv", 100, 5) == {"streams": []}, "cache survives a reload", errors)
        _assert(MediaProbeCache(cache_path, tool="other").get("movie.mkv", 100, 5) is None,
                "a different tool's cache is not reused", errors)
        cache_path.write_text("{not json", encoding="utf-8")
        _assert(MediaProbeCache(cache_path, tool="10bit").get("movie.mkv", 100, 5) is None,
                "corrupt cache degrades to a miss", errors)
        disabled = MediaProbeCache(report_dir / "unused.json", tool="10bit", enabled=False)
        disabled.put("movie.mkv", 1, 1, {"streams": []})
        _assert(disabled.get("movie.mkv", 1, 1) is None, "--no-cache stores nothing", errors)
        _assert(not (report_dir / "unused.json").exists(), "--no-cache writes no file", errors)
        cache_path.unlink()
    finally:
        shutil.rmtree(report_dir, ignore_errors=True)

    if errors:
        print("SELF-TEST FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print("SELF-TEST PASSED (fail-closed classification + HDR rules + discovery + single report)")
    return 0

# Rebind every moved function to the tool's namespace, then publish it back on
# the module so the bodies can call each other exactly as they used to.
_assert = bind_to_tool(tool, _assert)
tool._assert = _assert
run_self_tests = bind_to_tool(tool, run_self_tests)
tool.run_self_tests = run_self_tests
