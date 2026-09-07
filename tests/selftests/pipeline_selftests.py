"""The offline self-tests lifted out of ``pipeline.py``.

These assertions used to ship inside the tool itself. They are unchanged; only
their address is different. Each function is rebound to the tool module's
namespace by :func:`bind_to_tool`, so a body that reads or patches a module
global (``globals()["_movie_upgrade_decision"] = ...``,  ``global CFG``)
affects the tool exactly as it did when it lived there.

``tests/test_selftests.py`` runs them as part of the normal unit suite.
"""

from __future__ import annotations

import os

import pipeline as tool
from organizekit.core import default_library_root
from tests.selftests import bind_to_tool

# The bodies below resolve their names in the tool's namespace. A few of the
# names they need had no other user in the tool once the self-tests moved out,
# and dead imports do not belong in a shipped file — so they are supplied from
# here, where the dependency is visible.
tool.default_library_root = default_library_root
tool.os = os


def run_self_tests() -> int:
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # The ordering is the whole point of this script.
    check(STEP_ORDER == ("fetcher", "cleaner", "10bit", "sync", "auditor"), "canonical step order")
    check(STEP_ORDER.index("fetcher") < STEP_ORDER.index("cleaner"),
          "subtitles must be fetched before the remux invalidates the moviehash")
    check(STEP_ORDER.index("sync") < STEP_ORDER.index("auditor"),
          "subtitle sync must finish before the audit sees the sidecars")

    # Order is preserved no matter how the user types the flag.
    for requested in (["auditor", "fetcher"], ["10bit", "cleaner", "fetcher"],
                      ["auditor"], ["cleaner", "10bit"]):
        resolved = resolve_steps(requested)
        check(resolved == tuple(k for k in STEP_ORDER if k in set(requested)),
              f"resolve_steps preserves canonical order for {requested}: {resolved}")
    check(resolve_steps([]) == (), "empty selection resolves to nothing")

    check(set(STEPS) == set(STEP_ORDER), "every step in the order has a definition")

    # Library resolution: flag wins, then the environment, then the documented
    # default. Docker and scheduled runs rely on the middle case.
    saved_env = {var: os.environ.pop(var, None)
                 for var in ("ORGANIZE_LIBRARY", "MOVIE_STD_TARGET")}
    try:
        check(resolve_library(None) == default_library_root(),
              "no flag and no env resolves to the platform default library")
        os.environ["MOVIE_STD_TARGET"] = str(Path("/media/movies"))
        check(resolve_library(None) == Path("/media/movies"),
              "MOVIE_STD_TARGET is honored when no --source flag is given")
        os.environ["ORGANIZE_LIBRARY"] = str(Path("/media/current"))
        check(resolve_library(None) == Path("/media/current"),
              "ORGANIZE_LIBRARY takes precedence over the legacy variable")
        check(resolve_library(Path("/srv/library")) == Path("/srv/library"),
              "an explicit --source flag beats the environment")
    finally:
        for var, value in saved_env.items():
            if value is not None:
                os.environ[var] = value
            else:
                os.environ.pop(var, None)
        else:
            os.environ.pop("MOVIE_STD_TARGET", None)

    # Each tool's library-root flag differs; getting these wrong silently
    # points a tool at the default path instead of the requested library.
    check(STEPS["fetcher"].root_flag == "--source", "fetcher uses --source")
    check(STEPS["cleaner"].root_flag == "--dir", "cleaner uses --dir")
    check(STEPS["10bit"].root_flag == "--source", "10bit uses --source")
    check(STEPS["sync"].root_flag == "--source", "sync uses --source")
    check(STEPS["auditor"].root_flag == "--source", "auditor uses --source")

    library = Path("/media/movies")
    base = [sys.executable, str(HERE / "subtitle_fetcher.py"), "--source", str(library)]
    check(build_command(STEPS["fetcher"], Config(library=library)) == base, "plain fetcher argv")
    check(build_command(STEPS["fetcher"], Config(library=library, dry_run=True))
          == base + ["--dry-run"], "dry-run flag")
    check(build_command(STEPS["fetcher"], Config(library=library, limit=5))
          == base + ["--limit", "5"], "limit flag")
    check(build_command(STEPS["cleaner"], Config(library=library, nice=True))
          == [sys.executable, str(HERE / "mkv_track_cleaner.py"), "--dir", str(library),
              "--nice"], "cleaner flags")
    # The auditor is already read-only, so it has no --dry-run to forward.
    check("--dry-run" not in build_command(STEPS["auditor"],
                                           Config(library=library, dry_run=True)),
          "auditor has no dry-run flag to forward")
    check("--limit" not in build_command(STEPS["auditor"], Config(library=library, limit=3)),
          "auditor has no limit flag to forward")

    # A missing script is reported as a skip, never a crash.
    ghost = Step(key="ghost", script="does-not-exist.py", title="ghost", root_flag="--source")
    check(prerequisite_issue(ghost) is not None, "missing script is skipped, not crashed")

    # The summary names what happened.
    summary = build_summary(
        Run(results=[
            StepResult("fetcher", "Fetch", "ran", returncode=0, seconds=1.5),
            StepResult("cleaner", "Clean", "skipped", detail="mkvmerge not found"),
        ], elapsed=2.0),
        Config(library=library),
    )
    check("SKIP" in summary and "mkvmerge not found" in summary, "summary reports a skip")
    check("RAN" in summary and "ok" in summary, "summary reports a successful run")
    failed_summary = build_summary(
        Run(results=[StepResult("fetcher", "Fetch", "ran", returncode=1, seconds=0.1)]),
        Config(library=library),
    )
    check("Failed steps : fetcher" in failed_summary, "summary reports failures")

    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print("  -", error)
        return 1
    print("SELF-TEST PASSED (step order + flag mapping + skip handling + summary)")
    return 0

# Rebind every moved function to the tool's namespace, then publish it back on
# the module so the bodies can call each other exactly as they used to.
run_self_tests = bind_to_tool(tool, run_self_tests)
tool.run_self_tests = run_self_tests
