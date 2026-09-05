"""The offline self-tests lifted out of ``library_auditor.py``.

These assertions used to ship inside the tool itself. They are unchanged; only
their address is different. Each function is rebound to the tool module's
namespace by :func:`bind_to_tool`, so a body that reads or patches a module
global (``globals()["_movie_upgrade_decision"] = ...``,  ``global CFG``)
affects the tool exactly as it did when it lived there.

``tests/test_selftests.py`` runs them as part of the normal unit suite.
"""

from __future__ import annotations

import shutil

import library_auditor as tool
from tests.selftests import bind_to_tool

# The bodies below resolve their names in the tool's namespace. A few of the
# names they need had no other user in the tool once the self-tests moved out,
# and dead imports do not belong in a shipped file — so they are supplied from
# here, where the dependency is visible.
tool.shutil = shutil


def run_self_tests() -> int:
    errors: list[str] = []
    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    check(is_junk_filename("movie.mkv.!qB"), "torrent temporary suffix")
    root = Path(tempfile.mkdtemp(prefix="jellyfin_auditor_"))
    try:
        library, output = root / "library", root / "reports"
        library.mkdir()
        output.mkdir()
        valid_srt = "1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n"
        (library / "Movie One (2020)").mkdir()
        (library / "Movie One (2020)" / "Movie One (2020).mkv").write_bytes(b"mkv")
        (library / "Movie One (2020)" / f"Movie One (2020){EXTERNAL_SRT_SUFFIX}").write_text(valid_srt, encoding="utf-8")
        (library / "Movie One (2020)" / "Featurettes").mkdir()
        (library / "Movie One (2020)" / "Featurettes" / "making-of.mp4").write_bytes(b"extra")
        (library / "Legacy (1999)").mkdir()
        (library / "Legacy (1999)" / "Legacy (1999).AVI").write_bytes(b"avi")
        (library / "Multiple (2001)").mkdir()
        (library / "Multiple (2001)" / "Multiple (2001).mkv").write_bytes(b"mkv")
        (library / "Multiple (2001)" / "Multiple (2001).mp4").write_bytes(b"mp4")
        (library / "No Movie (2002)").mkdir()
        (library / "No Movie (2002)" / f"No Movie (2002){EXTERNAL_SRT_SUFFIX}").write_text("sub", encoding="utf-8")
        (library / "Stem Mismatch (2003)").mkdir()
        (library / "Stem Mismatch (2003)" / "wrong-name.mkv").write_bytes(b"mkv")
        # A forced/flagged English SRT is not the canonical plain .eng.srt.
        (library / "Sidecar Mismatch (2004)").mkdir()
        (library / "Sidecar Mismatch (2004)" / "Sidecar Mismatch (2004).mkv").write_bytes(b"mkv")
        (library / "Sidecar Mismatch (2004)" / "Sidecar Mismatch (2004).eng.forced.srt").write_text(valid_srt, encoding="utf-8")
        (library / "Sdh Cover (2007)").mkdir()
        (library / "Sdh Cover (2007)" / "Sdh Cover (2007).mkv").write_bytes(b"mkv")
        (library / "Sdh Cover (2007)" / "Sdh Cover (2007).eng.sdh.srt").write_text(valid_srt, encoding="utf-8")
        # A correctly named sidecar whose contents are unusable is a real defect:
        # nothing downstream will replace a subtitle it believes is present.
        (library / "Broken Subs (2005)").mkdir()
        (library / "Broken Subs (2005)" / "Broken Subs (2005).mkv").write_bytes(b"mkv")
        (library / "Broken Subs (2005)" / f"Broken Subs (2005){EXTERNAL_SRT_SUFFIX}").write_text("sub", encoding="utf-8")
        # A validated legacy .en.srt is promoted to .eng.srt during the audit.
        (library / "Legacy En (2006)").mkdir()
        (library / "Legacy En (2006)" / "Legacy En (2006).mkv").write_bytes(b"mkv")
        (library / "Legacy En (2006)" / f"Legacy En (2006){LEGACY_EXTERNAL_SRT_SUFFIX}").write_text(valid_srt, encoding="utf-8")
        cfg = Config(source_dir=library, log_file=output / "audit.log", report_file=output / "report.txt", lock_timeout_seconds=0)
        audit = audit_library(cfg)
        states = {item.folder.name: item.state for item in audit.folders}
        check(states == {
            "Legacy (1999)": "SINGLE_OTHER_CONTAINER",
            "Movie One (2020)": "CANONICAL_MKV",
            "Multiple (2001)": "MULTIPLE_DIRECT_MOVIE_FILES",
            "No Movie (2002)": "NO_DIRECT_MOVIE_FILE",
            "Stem Mismatch (2003)": "MKV_STEM_MISMATCH",
            "Sidecar Mismatch (2004)": "NONCANONICAL_SIDECAR",
            "Broken Subs (2005)": "INVALID_SIDECAR",
            "Legacy En (2006)": "CANONICAL_MKV",
            "Sdh Cover (2007)": "CANONICAL_MKV",
        }, f"folder states {states}")
        check(
            (library / "Legacy En (2006)" / f"Legacy En (2006){EXTERNAL_SRT_SUFFIX}").is_file(),
            "legacy .en.srt was promoted to .eng.srt",
        )
        check(
            not (library / "Legacy En (2006)" / f"Legacy En (2006){LEGACY_EXTERNAL_SRT_SUFFIX}").exists(),
            "legacy .en.srt removed after promote",
        )
        report = build_report(audit, cfg)
        check(f"Movie One (2020){EXTERNAL_SRT_SUFFIX}" not in report and "making-of.mp4" not in report, "non-direct media leaked into report")
        # The scorecard is the contract: a right-aligned count, three spaces,
        # then the label. Asserting on it keeps the report honest about what a
        # reader sees at a glance.
        check("   1   MKV stem mismatch" in report and "   1   Noncanonical SRT" in report,
              "canonical exception counts")
        check("   1   Invalid Eng SRT" in report and "MOVIES WITH NO USABLE EXTERNAL ENGLISH SRT" in report,
              "unusable sidecar reported as actionable")
        atomic_write_text(cfg.report_file, report)
        check(cfg.report_file.read_text(encoding="utf-8") == report, "saved report differs")
        check(not list(output.glob("*.json")), "JSON output exists")
        check(bool(validate_config(Config(source_dir=library, log_file=output / "audit.log", report_file=library / "bad.txt", lock_timeout_seconds=0))), "report within library accepted")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("SELF-TEST PASSED (direct folders + types + single report)")
    return 0

# Rebind every moved function to the tool's namespace, then publish it back on
# the module so the bodies can call each other exactly as they used to.
run_self_tests = bind_to_tool(tool, run_self_tests)
tool.run_self_tests = run_self_tests
