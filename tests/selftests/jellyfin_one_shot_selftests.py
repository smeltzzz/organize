"""The offline self-tests lifted out of ``jellyfin_one_shot.py``.

These assertions used to ship inside the tool itself. They are unchanged; only
their address is different. Each function is rebound to the tool module's
namespace by :func:`bind_to_tool`, so a body that reads or patches a module
global (``globals()["_movie_upgrade_decision"] = ...``,  ``global CFG``)
affects the tool exactly as it did when it lived there.

``tests/test_selftests.py`` runs them as part of the normal unit suite.
"""

from __future__ import annotations

import jellyfin_one_shot as tool
from organizekit.core import default_library_root
from tests.selftests import bind_to_tool

# The bodies below resolve their names in the tool's namespace. A few of the
# names they need had no other user in the tool once the self-tests moved out,
# and dead imports do not belong in a shipped file — so they are supplied from
# here, where the dependency is visible.
tool.default_library_root = default_library_root


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------
def run_self_tests() -> int:
    """Run offline self-tests for the one-shot completer."""
    errors = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="one_shot_test_") as tmpdir:
        tmp_path = Path(tmpdir)

        # Create a dummy runtime_log for testing
        test_runtime_log = tmp_path / "test.log"
        test_runtime_log.touch()

        # Test 1: machine-readable summary line (current auditor format)
        report0 = tmp_path / "report0.txt"
        report0.write_text("""\
╔═ JELLYFIN MOVIE LIBRARY AUDIT ═╗
  ────────────────────────────────
     7   Canonical MKV            one MKV + a validated .eng.srt
     0   Missing Eng SRT          run subtitle_fetcher.py
     7   Folders checked          every top-level folder in the library
  ────────────────────────────────
  AUDIT SUMMARY: canonical=7; total=7; pct=100.0%
""", encoding="utf-8")
        covered, total = parse_auditor_coverage(test_runtime_log, report0)
        check(covered == 7 and total == 7, f"Summary-line parsing: got {covered}/{total}")

        # Test 2: Parse scorecard format (older auditor reports)
        report1 = tmp_path / "report1.txt"
        report1.write_text("""
   42/42 (100.0%)  COVERAGE: movies with a validated English SRT
   42  Already have .eng.srt
   42  Movies in the library
""")
        covered, total = parse_auditor_coverage(test_runtime_log, report1)
        check(covered == 42 and total == 42, f"Percentage line parsing: got {covered}/{total}")

        # Test 3: Parse "Canonical MKV" scorecard format
        report2 = tmp_path / "report2.txt"
        report2.write_text("""
   42  Canonical MKV
   42  Folders checked
""")
        covered, total = parse_auditor_coverage(test_runtime_log, report2)
        check(covered == 42 and total == 42, f"Canonical MKV parsing: got {covered}/{total}")

        # Test 4: Parse fetcher-style coverage line
        report3 = tmp_path / "report3.txt"
        report3.write_text("Coverage this run: 5 of 9 movie(s) (55.6%) end with a validated external English SRT.")
        covered, total = parse_auditor_coverage(test_runtime_log, report3)
        check(covered == 5 and total == 9, f"Coverage-line parsing: got {covered}/{total}")

        # Test 5: Non-existent report
        covered, total = parse_auditor_coverage(test_runtime_log, tmp_path / "nonexistent.txt")
        check(covered is None and total is None, "Non-existent report should return None")

        # Test 6: Unparseable report
        report4 = tmp_path / "report4.txt"
        report4.write_text("nothing in here looks like a report\n")
        covered, total = parse_auditor_coverage(test_runtime_log, report4)
        check(covered is None and total is None, "Unparseable report should return None")

        # Test 7: Library complete check
        check(is_library_complete(42, 42), "42/42 should be complete")
        check(not is_library_complete(41, 42), "41/42 should not be complete")
        check(not is_library_complete(None, 42), "None coverage should not be complete")
        check(not is_library_complete(0, 0), "0/0 should not be complete (empty library)")

        # Test 8: Log dir inside source detection
        source = tmp_path / "library"
        source.mkdir()
        check(log_dir_inside_source(source / "logs", source), "log dir inside library detected")
        check(log_dir_inside_source(source, source), "log dir equal to library detected")
        check(not log_dir_inside_source(tmp_path / "elsewhere", source), "sibling log dir accepted")

        # Test 9: Missing tool script detection
        check(
            "subtitle_fetcher.py" in missing_tool_scripts(tmp_path),
            "missing tool script detected",
        )

        # Test 10: Bounded transcript tailing
        transcript = tmp_path / "transcript.log"
        for i in range(5):
            tail_to_file(transcript, "\n".join(f"line {i}-{j}" for j in range(10)), max_lines=15)
        lines = transcript.read_text(encoding="utf-8").splitlines()
        check(len(lines) == 15, f"transcript bounded to {max(0, 15)} lines, got {len(lines)}")
        check(lines[-1] == "line 4-9", "transcript keeps the newest lines")

        # Test 11: UTC midnight wait (just check it doesn't crash)
        # We don't actually wait in tests
        check(callable(wait_for_utc_midnight), "UTC wait function exists and is callable")

        # Test 12: Library root resolution — flag > MOVIE_STD_TARGET > default,
        # the same ladder every sibling tool walks.
        saved_env = {var: os.environ.pop(var, None)
                     for var in ("ORGANIZE_LIBRARY", "MOVIE_STD_TARGET")}
        try:
            check(resolve_library(None) == default_library_root(),
                  "library falls back to the platform default")
            check(describe_library_origin(None) == f"the default library root ({default_library_root()})",
                  "default provenance is reported")

            os.environ["MOVIE_STD_TARGET"] = str(tmp_path / "env_library")
            check(resolve_library(None) == tmp_path / "env_library",
                  "MOVIE_STD_TARGET overrides the default")
            check(describe_library_origin(None) == "MOVIE_STD_TARGET",
                  "MOVIE_STD_TARGET provenance is reported")

            os.environ["ORGANIZE_LIBRARY"] = str(tmp_path / "current_library")
            check(resolve_library(None) == tmp_path / "current_library",
                  "ORGANIZE_LIBRARY takes precedence over the legacy variable")
            check(describe_library_origin(None) == "ORGANIZE_LIBRARY",
                  "ORGANIZE_LIBRARY provenance is reported")

            explicit = tmp_path / "explicit_library"
            check(resolve_library(explicit) == explicit,
                  "--source beats the environment")
            check(describe_library_origin(explicit) == "--source",
                  "--source provenance is reported")
        finally:
            for var, value in saved_env.items():
                if value is not None:
                    os.environ[var] = value
                else:
                    os.environ.pop(var, None)

    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("SELF-TEST PASSED (coverage parsing, completeness check, validation, library resolution)")
    return 0

# Rebind every moved function to the tool's namespace, then publish it back on
# the module so the bodies can call each other exactly as they used to.
run_self_tests = bind_to_tool(tool, run_self_tests)
tool.run_self_tests = run_self_tests
