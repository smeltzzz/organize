#!/usr/bin/env python3
"""Run the manual half of the Jellyfin movie pipeline in the right order.

``movie_standardizer.py`` is the qBittorrent completion hook and runs by itself
the moment a download stops, so it is deliberately not part of this sweep. What
is left — fetching subtitles, cleaning tracks, checking bit depth, syncing
subtitle timing, auditing the library — is five separate commands, and the
order between the first two is load-bearing:

    subtitle_fetcher.py   MUST run before mkv_track_cleaner.py

``subtitle_fetcher.py`` searches OpenSubtitles by moviehash, which is the file
size plus the sum of the first and last 64 KiB. It can then use SubDL's
score-gated release-aware fallback. A remux rewrites those bytes, so any movie cleaned
first can never reproduce its release hash and is silently demoted to the much
weaker title/year search. ``sync_subtitles.py`` runs last of the content
steps, just before the audit: it rewrites subtitle bytes only (never movie
bytes), so the moviehash is undisturbed - but the audit must see the finished
sidecars. Running the five scripts
by hand makes that easy to get wrong on a busy day; this script cannot get it
wrong.

Each tool runs as its own subprocess so it keeps its own locks, logs and
reports exactly as it would standalone. Steps whose prerequisites are missing
are skipped with a clear reason rather than crashing the run — no API key means
no fetching, no mkvmerge means no cleaning, no ffprobe means no bit-depth scan.

    python pipeline.py --dry-run
    python pipeline.py --source "E:\\torrents\\final_organized"
    python pipeline.py --steps fetcher,auditor
    python pipeline.py --self-test

Stdlib only. No Python packages required.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Shared implementation: everything imported here is defined exactly once,
# in organizekit/core/. See tests/test_shared_core.py for the rule that
# keeps it that way.
from organizekit.core import (
    STEP_ORDER,
    STEPS,
    Report,
    Step,
    child_cwd,
    enable_utf8_stdio,
    prerequisite_issue,
    print_text,
    resolve_library,
    run_field_smoke_test,
    tool_command,
    tool_is_available,
)

# The binary probes, under the names this module has always used for them.
# They are re-exported rather than wrapped so that patching one here patches
# the one the prerequisite table actually calls.
from organizekit.core import api_key_present as _api_key_present  # noqa: F401
from organizekit.core import ffsubsync_ready as _ffsubsync_present  # noqa: F401

VERSION = "1.0.0"

HERE = Path(__file__).resolve().parent
DEFAULT_LIBRARY = str(resolve_library())

@dataclass
class Config:
    library: Path = Path(DEFAULT_LIBRARY)
    steps: tuple[str, ...] = STEP_ORDER
    dry_run: bool = False
    limit: int = 0
    nice: bool = False
    continue_on_error: bool = True

# Shown before a step runs, because the failure mode is silent and easy to
# misread as "nothing to do".
HINTS: dict[str, str] = {
    "cleaner": (
        "Movies still hardlinked to their qBittorrent source are ALWAYS deferred, never "
        "cleaned - there is no override. If this step cleans nothing, open your completed-download "
        "folder (the standardizer's source root): "
        "qBittorrent's default seed-limit action only pauses the torrent and leaves the file, so "
        "the link count never drops and the deferral never clears. Delete the source (safe - it "
        "is a hardlink, so your library copy keeps the data) or set qBittorrent to remove the "
        "content when seeding stops."
    ),
    "fetcher": (
        "Runs before the cleaner on purpose: a remux rewrites the OpenSubtitles moviehash, "
        "so cleaning first would force the weaker title/year search."
    ),
    "sync": (
        "Runs after every other tool on purpose: it rewrites subtitle bytes, never movie "
        "bytes, so the moviehash is undisturbed - but it must finish before the audit so the "
        "audit sees the finished sidecars. A bad sync is worse than none: untrusted alignments "
        "are held for review, never applied."
    ),
}

@dataclass
class StepResult:
    key: str
    title: str
    status: str          # ran | skipped | missing
    returncode: int | None = None
    seconds: float = 0.0
    detail: str = ""

@dataclass
class Run:
    results: list[StepResult] = field(default_factory=list)
    elapsed: float = 0.0

# ---------------------------------------------------------------------------
# Calling a step
#
# The step table, the prerequisite checks and the long-run argv builder all
# live in organizekit.core.toolchain: jellyfin_one_shot.py runs the same five
# tools and must not be able to disagree with this file about any of it.
# ---------------------------------------------------------------------------

def build_command(step: Step, cfg: Config) -> list[str]:
    """Build the argv for one step, mirroring what you would type by hand."""
    command = tool_command(step.script, [step.root_flag, str(cfg.library)])
    if cfg.dry_run and step.supports_dry_run:
        command.append("--dry-run")
    if cfg.limit and step.supports_limit:
        command.extend(["--limit", str(cfg.limit)])
    if cfg.nice and step.supports_nice:
        command.append("--nice")
    return command

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run_step(step: Step, cfg: Config, dry_run_pipeline: bool = False) -> StepResult:
    issue = prerequisite_issue(step)
    if issue is not None:
        return StepResult(step.key, step.title, "skipped", detail=issue)

    command = build_command(step, cfg)
    print(f"\n{'=' * 78}\nSTEP {step.key}: {step.title}\n{'=' * 78}", flush=True)
    hint = HINTS.get(step.key)
    if hint:
        print(f"  note: {hint}", flush=True)
    if dry_run_pipeline:
        print("  would run: " + " ".join(f'"{part}"' if " " in part else part
                                         for part in command), flush=True)
        return StepResult(step.key, step.title, "skipped", detail="pipeline dry-run")

    print("  " + " ".join(f'"{part}"' if " " in part else part for part in command), flush=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=str(child_cwd()), check=False)
        code: int | None = completed.returncode
    except OSError as exc:
        return StepResult(step.key, step.title, "missing", detail=f"could not launch: {exc}")
    return StepResult(step.key, step.title, "ran", returncode=code,
                      seconds=time.monotonic() - started)

def run_pipeline(cfg: Config, dry_run: bool = False) -> Run:
    started = time.monotonic()
    run = Run()
    for key in cfg.steps:
        step = STEPS[key]
        result = run_step(step, cfg, dry_run_pipeline=dry_run)
        run.results.append(result)
        if result.status == "ran" and result.returncode:
            print(f"\n  STEP {key} exited with code {result.returncode}.", flush=True)
            if not cfg.continue_on_error:
                print("  Stopping. Use --continue-on-error (the default) to run the remaining steps anyway.",
                      flush=True)
                break
    run.elapsed = time.monotonic() - started
    return run

def build_summary(run: Run, cfg: Config) -> str:
    """Render the pipeline summary with the same layout as every tool's report."""
    label = {"ran": "RAN", "skipped": "SKIP", "missing": "MISSING"}
    failed = [r.key for r in run.results if r.status == "ran" and r.returncode]
    skipped = [r.key for r in run.results if r.status != "ran"]
    succeeded = [r.key for r in run.results if r.status == "ran" and not r.returncode]

    report = Report(
        "JELLYFIN MOVIE PIPELINE SUMMARY",
        "Every tool, in the order that keeps exact-hash subtitle matching possible",
    )
    report.metas([
        ("Library", cfg.library),
        ("Steps", ", ".join(cfg.steps) or "(none selected)"),
        ("Mode", "DRY-RUN (showing commands only)" if cfg.dry_run else "LIVE"),
        ("Elapsed", f"{run.elapsed:.1f}s"),
    ])
    report.blank()
    report.scorecard([
        (len(succeeded), "Completed", "exited 0"),
        (len(failed), "Failed", "exited non-zero; read that tool's report"),
        (len(skipped), "Not run", "skipped or a required binary is missing"),
        (len(run.results), "Steps attempted", "in the canonical order"),
    ])
    if failed:
        report.paragraph(f"Start here: {len(failed)} step(s) failed \u00b7 their own report files "
                         "carry the per-movie detail.")

    report.section("STEP RESULTS", count=len(run.results),
                   intro="In pipeline order. Each tool writes its own report and log.")
    if not run.results:
        report.paragraph("No steps ran.")
    else:
        report.table(
            ["Status", "Step", "Tool", "Outcome"],
            [[label.get(r.status, r.status.upper()), r.key, r.title,
              (f"ok ({r.seconds:.1f}s)" if not r.returncode else f"exit {r.returncode}")
              if r.status == "ran" else r.detail]
             for r in run.results],
            aligns="<<<<",
        )

    closing: list[str] = []
    if failed:
        closing.append(f"Failed steps : {', '.join(failed)}")
    if skipped:
        closing.append(f"Not run      : {', '.join(skipped)}")
    if not failed and not skipped:
        closing.append("All steps completed.")
    report.footer(closing)
    return report.render()

def resolve_steps(requested: Sequence[str]) -> tuple[str, ...]:
    """Keep the canonical order regardless of the order flags were supplied in."""
    chosen = set(requested)
    return tuple(key for key in STEP_ORDER if key in chosen)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Run the manual Jellyfin movie steps in the correct order: "
                     "subtitles, then track cleaning, then bit depth, then subtitle sync, then audit."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("movie_standardizer.py is the qBittorrent completion hook and is not part of\n"
                "this sweep. Subtitles are fetched before the remux because a remux rewrites\n"
                "the OpenSubtitles moviehash and would force a weaker title/year search.\n"
                "Subtitle sync (ffsubsync) runs just before the audit: it only rewrites\n"
                "subtitle bytes, so the audit sees the finished sidecars."),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--source", type=Path, default=None,
                        help=f"Jellyfin movie-library root (default: {DEFAULT_LIBRARY}, or MOVIE_STD_TARGET when set)")
    parser.add_argument("--steps", default=",".join(STEP_ORDER),
                        help=f"Comma-separated subset of: {', '.join(STEP_ORDER)}")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the commands that would run, without running them")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="Pass --limit N to the steps that support it (0 = all)")
    parser.add_argument("--nice", action="store_true",
                        help="Lower remux priority so track cleaning does not starve Jellyfin")
    parser.add_argument("--continue-on-error", dest="continue_on_error", action="store_true", default=True,
                        help="Keep going after a step fails instead of stopping (default)")
    parser.add_argument("--stop-on-error", dest="continue_on_error", action="store_false",
                        help="Stop the pipeline on the first step failure")
    parser.add_argument("--list-steps", action="store_true",
                        help="Print the steps, what needs to be installed, and exit")
    parser.add_argument("--self-test", action="store_true")
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    enable_utf8_stdio()
    args = build_parser().parse_args(argv)

    if args.self_test:
        return run_self_tests()
    if args.list_steps:
        for key in STEP_ORDER:
            step = STEPS[key]
            issue = prerequisite_issue(step)
            state = f"blocked: {issue}" if issue else "ready"
            print(f"  {key:<9} {step.title:<32} {state}")
        return 0

    requested = [part.strip() for part in str(args.steps).split(",") if part.strip()]
    unknown = [key for key in requested if key not in STEPS]
    if unknown:
        print(f"Unknown step(s): {', '.join(unknown)}. Known: {', '.join(STEP_ORDER)}",
              file=sys.stderr)
        return 2
    if not requested:
        print("No steps selected.", file=sys.stderr)
        return 2

    cfg = Config(
        library=resolve_library(args.source),
        steps=resolve_steps(requested),
        dry_run=bool(args.dry_run),
        limit=max(0, int(args.limit)),
        nice=bool(args.nice),
        continue_on_error=bool(args.continue_on_error),
    )
    if not cfg.library.is_dir():
        print(f"Library directory does not exist: {cfg.library}", file=sys.stderr)
        return 2

    run = run_pipeline(cfg, dry_run=cfg.dry_run)
    print()
    print_text(build_summary(run, cfg))
    return 1 if any(r.status == "ran" and r.returncode for r in run.results) else 0

# ---------------------------------------------------------------------------
# SELF-TEST  (offline; never launches a tool)
# ---------------------------------------------------------------------------


def run_self_tests() -> int:
    """Field smoke test: is the step order intact in this copy?

    The order between the fetcher and the cleaner is the single load-bearing
    invariant of the whole toolkit — a remux rewrites the bytes OpenSubtitles
    hashes — so it is worth asserting even in a 5-second smoke test.
    """
    def fetch_precedes_remux() -> bool:
        return STEP_ORDER.index("fetcher") < STEP_ORDER.index("cleaner")

    def sync_precedes_audit() -> bool:
        return (STEP_ORDER.index("cleaner") < STEP_ORDER.index("sync")
                < STEP_ORDER.index("auditor"))

    def every_step_is_defined() -> bool:
        return set(STEPS) == set(STEP_ORDER)

    def the_tools_are_present() -> bool:
        return all(tool_is_available(STEPS[key].script) for key in STEP_ORDER)

    return run_field_smoke_test("pipeline.py", [
        ("subtitles are fetched before the remux", fetch_precedes_remux),
        ("sync runs after the remux, before the audit", sync_precedes_audit),
        ("every ordered step has a definition", every_step_is_defined),
        ("every tool script is present", the_tools_are_present),
    ])

if __name__ == "__main__":
    raise SystemExit(main())
