"""The offline self-tests lifted out of ``movie_standardizer.py``.

These assertions used to ship inside the tool itself. They are unchanged; only
their address is different. Each function is rebound to the tool module's
namespace by :func:`bind_to_tool`, so a body that reads or patches a module
global (``globals()["_movie_upgrade_decision"] = ...``,  ``global CFG``)
affects the tool exactly as it did when it lived there.

``tests/test_selftests.py`` runs them as part of the normal unit suite.
"""

from __future__ import annotations

import movie_standardizer as tool
from tests.selftests import bind_to_tool


def _assert_eq(actual, expected, label: str, errors: list[str]) -> None:
    if actual != expected:
        errors.append(f"{label}: got {actual!r} expected {expected!r}")


def run_canonical_self_tests() -> int:
    """Exercise the exact canonical-output contract in isolated temp folders."""
    global CFG, RUN_SUMMARY
    original_cfg = CFG
    root = Path(tempfile.mkdtemp(prefix="ms_canonical_"))
    src, dst = root / "source", root / "final_organized"
    src.mkdir()
    dst.mkdir()
    errors: list[str] = []
    try:
        # report_file=None / log_file=None on purpose: the self-test must not
        # scatter files under the host's own reports directory or CWD.
        CFG = Config(
            source_dir=src,
            target_dir=dst,
            log_file=None,
            report_file=None,
            min_movie_size_mb=0,
            copy_extras=False,
            copy_artwork=False,
            run_cleanup_on_target=False,
            enable_deduplication=False,
        )
        RUN_SUMMARY = RunSummary()
        setup_logging(CFG)

        release = src / "Example.Film.2020.1080p.WEB-DL"
        release.mkdir()
        (release / "Example.Film.2020.1080p.WEB-DL.mkv").write_bytes(b"movie")
        (release / "Example.Film.2020.English.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nEnglish\n", encoding="utf-8"
        )
        (release / "Example.Film.2020.en.forced.ass").write_text("forced", encoding="utf-8")
        (release / "Example.Film.2020.Spanish.srt").write_text("spanish", encoding="utf-8")
        (release / "poster.jpg").write_bytes(b"art")
        (release / "Example.Film.2020-trailer.mkv").write_bytes(b"trailer")
        handle_directory(release)
        output_dir = dst / "Example Film (2020)"
        _assert_eq(
            sorted(path.name for path in output_dir.iterdir()) if output_dir.exists() else [],
            [
                f"Example Film (2020){EXTERNAL_SRT_SUFFIX}",
                "Example Film (2020).mkv",
            ],
            "exact canonical output",
            errors,
        )

        dual = src / "Dual.Film.2021"
        dual.mkdir()
        (dual / "Dual.Film.2021.720p.mkv").write_bytes(b"a" * 10)
        (dual / "Dual.Film.2021.1080p.mkv").write_bytes(b"b" * 20)
        handle_directory(dual)
        dual_out = dst / "Dual Film (2021)" / "Dual Film (2021).mkv"
        _assert_eq(dual_out.read_bytes() if dual_out.exists() else b"", b"b" * 20, "largest MKV only", errors)

        (src / "Unsupported.Film.2022.mp4").write_bytes(b"mp4")
        handle_single_file(src / "Unsupported.Film.2022.mp4")
        parts = src / "Parts"
        parts.mkdir()
        (parts / "Parts.Film.2023.cd1.mkv").write_bytes(b"one")
        (parts / "Parts.Film.2023.cd2.mkv").write_bytes(b"two")
        handle_directory(parts)
        disc = src / "Disc"
        (disc / "BDMV" / "STREAM").mkdir(parents=True)
        (disc / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"disc")
        handle_directory(disc)
        if (dst / "Unsupported Film (2022)").exists() or (dst / "Parts Film (2023)").exists() or (dst / "Disc").exists():
            errors.append("unsupported MP4, multipart, or disc release was emitted")

        _assert_eq(is_english_subtitle(Path("Film.English.srt")), True, "english subtitle", errors)
        _assert_eq(is_english_subtitle(Path("Film.en.sdh.srt")), True, "english SDH subtitle", errors)
        _assert_eq(is_english_subtitle(Path("Film.Spanish.srt")), False, "non-English subtitle", errors)
        _assert_eq(parse_movie_name("The.Matrix.1999.1080p.mkv").file_stem(), "The Matrix (1999)", "canonical filename", errors)

        guard_src = src / "Guard.2019.mkv"
        guard_src.write_bytes(b"source-replacement")
        guard_dest = dst / "Guard (2019)" / "Guard (2019).mkv"
        guard_dest.parent.mkdir()
        guard_dest.write_bytes(b"destination")
        real_replace = os.replace
        real_upgrade_decision = globals()["_movie_upgrade_decision"]
        try:
            # This test isolates atomic activation failure. Duplicate identity
            # and quality policy is covered separately by the unit suite.
            globals()["_movie_upgrade_decision"] = lambda *_args: (True, "self-test upgrade")
            os.replace = lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("locked"))
            if process_file_action(guard_src, guard_dest):
                errors.append("locked destination replacement unexpectedly succeeded")
        finally:
            os.replace = real_replace
            globals()["_movie_upgrade_decision"] = real_upgrade_decision
        _assert_eq(guard_src.read_bytes(), b"source-replacement", "failed replacement keeps source", errors)
        _assert_eq(guard_dest.read_bytes(), b"destination", "failed replacement keeps destination", errors)
    finally:
        CFG = original_cfg
        shutil.rmtree(root, ignore_errors=True)

    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print("  -", error)
        return 1
    print("SELF-TEST PASSED (canonical MKV + English subtitles + skip and safety guards)")
    return 0

# Rebind every moved function to the tool's namespace, then publish it back on
# the module so the bodies can call each other exactly as they used to.
_assert_eq = bind_to_tool(tool, _assert_eq)
tool._assert_eq = _assert_eq
run_canonical_self_tests = bind_to_tool(tool, run_canonical_self_tests)
tool.run_canonical_self_tests = run_canonical_self_tests
