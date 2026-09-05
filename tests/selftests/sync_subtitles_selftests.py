"""The offline self-tests lifted out of ``sync_subtitles.py``.

These assertions used to ship inside the tool itself. They are unchanged; only
their address is different. Each function is rebound to the tool module's
namespace by :func:`bind_to_tool`, so a body that reads or patches a module
global (``globals()["_movie_upgrade_decision"] = ...``,  ``global CFG``)
affects the tool exactly as it did when it lived there.

``tests/test_selftests.py`` runs them as part of the normal unit suite.
"""

from __future__ import annotations

import tempfile

import sync_subtitles as tool
from organizekit.core import EXTERNAL_SRT_SUFFIX, REPORT_WIDTH
from tests.selftests import bind_to_tool

# The bodies below resolve their names in the tool's namespace. A few of the
# names they need had no other user in the tool once the self-tests moved out,
# and dead imports do not belong in a shipped file — so they are supplied from
# here, where the dependency is visible.
tool.tempfile = tempfile
tool.EXTERNAL_SRT_SUFFIX = EXTERNAL_SRT_SUFFIX
tool.REPORT_WIDTH = REPORT_WIDTH


def run_self_tests() -> int:
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # -- log parsing ------------------------------------------------------
    rich = (
        "           INFO     score: 551.000                              ffsubsync.py:255\n"
        "           INFO     offset seconds: -3.950                      ffsubsync.py:256\n"
        "           INFO     framerate scale factor: 1.000               ffsubsync.py:257\n"
        "           INFO     writing output to out.srt                   ffsubsync.py:350\n"
    )
    parsed = parse_ffsubsync_output(rich)
    check(abs(parsed.offset_seconds - -3.950) < 1e-9, f"rich offset {parsed.offset_seconds}")
    check(abs(parsed.scale_factor - 1.0) < 1e-9, f"rich scale {parsed.scale_factor}")
    check(abs(parsed.score - 551.0) < 1e-9, f"rich score {parsed.score}")
    check(not parsed.failed_marker and not parsed.leaving_unmodified, "rich has no failure markers")

    plain = (
        "INFO:ffsubsync:score: 12.345\n"
        "INFO:ffsubsync:offset seconds: 2.5\n"
        "INFO:ffsubsync:framerate scale factor: 1.042\n"
    )
    p2 = parse_ffsubsync_output(plain)
    check(p2.offset_seconds == 2.5 and p2.scale_factor == 1.042 and p2.score == 12.345,
          f"plain parsing {p2}")

    p3 = parse_ffsubsync_output("hello world\nno numbers here\n")
    check(p3.offset_seconds is None and p3.scale_factor is None and p3.score is None,
          "unparseable text yields None")

    p4 = parse_ffsubsync_output("offset seconds: 1.0\nERROR:ffsubsync:failed to sync x.srt\n")
    check(p4.failed_marker, "failure marker detected")

    p5 = parse_ffsubsync_output("offset seconds: 1.0\noffset seconds: 2.0\n")
    check(p5.offset_seconds == 2.0, "last measurement wins")

    p6 = parse_ffsubsync_output("WARNING: low-quality alignment; leaving subtitles unmodified\n")
    check(p6.leaving_unmodified, "quality-gate marker detected")

    # -- feature flag parsing ---------------------------------------------
    feats = parse_feature_flags("usage: ffs [--strict] [--skip-sync-on-low-quality] ...")
    check(feats.strict and feats.quality_gate and feats.help_ok, "both flags detected")
    feats_old = parse_feature_flags("usage: ffs [-o SRTOUT] [--encoding ENCODING]")
    check(not feats_old.strict and not feats_old.quality_gate, "older release has neither flag")

    # -- command building ---------------------------------------------------
    cmd = build_ffsubsync_command("ffs", Path("v.mkv"), Path("s.srt"), Path("st.srt"))
    check(cmd == ["ffs", str(Path("v.mkv")), "-i", str(Path("s.srt")),
                  "-o", str(Path("st.srt")), "--output-encoding", "utf-8"],
          f"plain argv {cmd}")
    cmd2 = build_ffsubsync_command("ffs", Path("v.mkv"), Path("s.srt"), Path("st.srt"),
                                   FfsubsyncFeatures(strict=True, quality_gate=True, help_ok=True))
    check(cmd2[-2:] == ["--strict", "--skip-sync-on-low-quality"], f"flag argv {cmd2}")
    cmd3 = build_ffsubsync_command("ffs", Path("v.mkv"), Path("s.srt"), Path("st.srt"),
                                   FfsubsyncFeatures())
    check("--strict" not in cmd3 and "--skip-sync-on-low-quality" not in cmd3,
          "no optional flags when unsupported")

    # -- decision table ------------------------------------------------------
    cfg = Config(library=Path("/lib"), log_file=Path("/out/sync_subtitles.log"),
                 report_file=Path("/out/sync_subtitles_report.txt"))
    ok_parsed = ParsedSync(score=551.0, offset_seconds=-3.95, scale_factor=1.0)
    check(classify_outcome(1, True, True, "", ok_parsed, cfg)[0] == STATUS_FAILED,
          "non-zero exit is a failure even with output")
    check(classify_outcome(0, False, False, "", ok_parsed, cfg)[0] == STATUS_FAILED,
          "missing output is a failure")
    check(classify_outcome(0, True, False, "no valid cue", ok_parsed, cfg)[0] == STATUS_FAILED,
          "invalid output is a failure")
    gate = ParsedSync(score=5.0, offset_seconds=1.0, scale_factor=1.0, leaving_unmodified=True)
    check(classify_outcome(0, True, True, "", gate, cfg)[0] == STATUS_REVIEW,
          "ffsubsync quality gate refusal is a review")
    check(classify_outcome(0, True, True, "", ParsedSync(), cfg)[0] == STATUS_REVIEW,
          "unmeasured offset is a review, not a replace")
    check(classify_outcome(0, True, True, "", ParsedSync(score=-12.0, offset_seconds=1.0,
                                                         scale_factor=1.0), cfg)[0] == STATUS_REVIEW,
          "anti-correlated score is a review")
    big = ParsedSync(score=10.0, offset_seconds=45.0, scale_factor=1.0)
    check(classify_outcome(0, True, True, "", big, cfg)[0] == STATUS_REVIEW,
          "offset beyond the trust window is a review")
    tiny = ParsedSync(score=10.0, offset_seconds=0.02, scale_factor=1.0)
    check(classify_outcome(0, True, True, "", tiny, cfg)[0] == STATUS_IN_SYNC,
          "sub-threshold offset with scale 1.0 is in sync")
    fps_fix = ParsedSync(score=10.0, offset_seconds=0.02, scale_factor=1.041667)
    check(classify_outcome(0, True, True, "", fps_fix, cfg)[0] == STATUS_SYNCED,
          "a real framerate correction is applied even with a tiny offset")
    check(classify_outcome(0, True, True, "", ok_parsed, cfg)[0] == STATUS_SYNCED,
          "trusted drift is applied")

    # -- staging name ---------------------------------------------------------
    staged_name = f"{STAGING_PREFIX}{os.getpid()}.{uuid.uuid4().hex}.srt"
    check(staged_name.startswith("."), "staging file is dot-prefixed")
    check(staged_name.endswith(".srt"), "staging file keeps the .srt extension")
    check(is_junk_filename(staged_name), "staging file is junk to the other tools")

    # -- discovery ------------------------------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="sync_selftest_"))
    try:
        film = tmp / "Film (2000)"
        film.mkdir()
        (film / "Film (2000).mkv").write_bytes(b"fake video")
        (film / "Film (2000).eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHello.\n", encoding="utf-8")
        orphan = tmp / "Orphan (2001)"
        orphan.mkdir()
        (orphan / "Orphan (2001).eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nAlone.\n", encoding="utf-8")
        multi = tmp / "Dual (2002)"
        multi.mkdir()
        (multi / "Dual (2002).mkv").write_bytes(b"mkv")
        (multi / "Dual (2002).mp4").write_bytes(b"mp4")
        (multi / "Dual (2002).eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nTwo.\n", encoding="utf-8")
        (film / ".hidden.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nJunk.\n", encoding="utf-8")
        (film / "Film (2000).eng.srt.tmp").write_text("1\n00:00:01,000 --> 00:00:02,000\nJunk.\n",
                                                      encoding="utf-8")
        (film / "sample.mkv").write_bytes(b"sample video")
        (film / "sample.mkv.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nSample.\n",
                                             encoding="utf-8")
        jobs, skips, video_count = discover_jobs(tmp)
        check(len(jobs) == 3, f"jobs {len(jobs)}")
        check({j.srt.name for j in jobs} ==
              {"Film (2000).eng.srt", "Dual (2002).eng.srt", "sample.mkv.srt"},
              f"job names {[j.srt.name for j in jobs]}")
        dual = next(j for j in jobs if j.srt.name == "Dual (2002).eng.srt")
        check(dual.video.name == "Dual (2002).mkv", "mkv preferred over mp4")
        sample = next(j for j in jobs if j.srt.name == "sample.mkv.srt")
        check(sample.video.name == "sample.mkv", "plain-stem sidecar pairs with its video")
        check(len(skips) == 1 and skips[0].srt.name == "Orphan (2001).eng.srt",
              f"skips {[(s.srt.name, s.detail) for s in skips]}")
        check(video_count == 4, f"video count {video_count}")
    finally:
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)

    # -- report rendering -------------------------------------------------------
    results = [
        SyncResult(srt=Path("/lib/Film (2000)/Film (2000).eng.srt"),
                   video=Path("/lib/Film (2000)/Film (2000).mkv"),
                   status=STATUS_SYNCED, detail="offset -3.950s",
                   offset_seconds=-3.95, scale_factor=1.0, score=551.0, seconds=12.3,
                   original_sha="a" * 64, new_sha="b" * 64),
        SyncResult(srt=Path("/lib/Review (2001)/Review (2001).eng.srt"),
                   video=Path("/lib/Review (2001)/Review (2001).mkv"),
                   status=STATUS_REVIEW, detail="offset +45.0s beyond window"),
        SyncResult(srt=Path("/lib/Broken (2002)/Broken (2002).eng.srt"),
                   video=Path("/lib/Broken (2002)/Broken (2002).mkv"),
                   status=STATUS_FAILED, detail="ffsubsync exited with code 1",
                   error_tail="ffmpeg not found"),
        SyncResult(srt=Path("/lib/Skipped (2003)/Skipped (2003).eng.srt"),
                   video=None, status=STATUS_SKIPPED, detail="no matching movie file"),
        SyncResult(srt=Path("/lib/Fine (2004)/Fine (2004).eng.srt"),
                   video=Path("/lib/Fine (2004)/Fine (2004).mkv"),
                   status=STATUS_IN_SYNC, detail="already aligned (offset +0.020s)"),
    ]
    text = build_report(results, cfg, video_count=5, ffsubsync_info="ffs ffsubsync 0.5.1",
                        features=FfsubsyncFeatures(strict=True, quality_gate=True, help_ok=True),
                        elapsed_sec=12.3, truncated=False)
    lines = text.splitlines()
    check(text.endswith("\n"), "report ends with a newline")
    check(all(not line.endswith(" ") for line in lines), "no trailing whitespace")
    check(all(len(line) <= REPORT_WIDTH for line in lines), "every line fits the page width")
    check("JELLYFIN SUBTITLE SYNCHRONIZER" in text, "title present")
    for title in ("SUBTITLES HELD FOR REVIEW", "FAILED SYNC ATTEMPTS",
                  "SUBTITLES SYNCED (TIMING CORRECTED)", "SKIPPED (NOTHING SYNCED)",
                  "ALREADY IN SYNC"):
        check(title in text, f"section {title} present")
    review_pos = text.index("SUBTITLES HELD FOR REVIEW")
    failed_pos = text.index("FAILED SYNC ATTEMPTS")
    synced_pos = text.index("SUBTITLES SYNCED")
    check(review_pos < failed_pos < synced_pos, "urgency order: review, failed, synced")

    # -- exit codes ---------------------------------------------------------------
    check(exit_code_for([results[0]], cfg) == 0, "all synced is 0")
    check(exit_code_for([results[2]], cfg) == 1, "a failure is 1")
    check(exit_code_for([results[1]], cfg) == 0, "a review alone is 0 without the flag")
    strict_cfg = Config(library=Path("/lib"), log_file=Path("/out/x.log"),
                        report_file=Path("/out/x.txt"), fail_on_review=True)
    check(exit_code_for([results[1]], strict_cfg) == 3, "a review is 3 with --fail-on-review")
    check(exit_code_for([results[1], results[2]], strict_cfg) == 1, "failure dominates review")

    # -- lock identity ----------------------------------------------------------------
    lock = CoordinationLock(Path("/some/library"))
    check(lock.path.name.startswith(".movie_standardizer.lock."),
          "shares the standardizer coordination lock key")

    # -- constants ---------------------------------------------------------------------
    check(EXTERNAL_SRT_SUFFIX == ".eng.srt", "canonical sidecar suffix")
    check(Path(LOG_FILE).parent == default_tool_dir("sync_subtitles"),
          "log defaults under the platform reports root")
    check(Path(REPORT_FILE).parent == default_tool_dir("sync_subtitles"),
          "report defaults under the platform reports root")

    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print("  -", error)
        return 1
    print("SELF-TEST PASSED (parse + flags + argv + decision table + discovery + report + exit codes)")
    return 0

# Rebind every moved function to the tool's namespace, then publish it back on
# the module so the bodies can call each other exactly as they used to.
run_self_tests = bind_to_tool(tool, run_self_tests)
tool.run_self_tests = run_self_tests
