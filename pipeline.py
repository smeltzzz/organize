#!/usr/bin/env python3
"""Run the manual half of the Jellyfin movie pipeline in the right order.

``movie_standardizer.py`` is the qBittorrent completion hook and runs by itself
the moment a download stops, so it is deliberately not part of this sweep. What
is left — fetching subtitles, cleaning tracks, checking bit depth, auditing the
library — is four separate commands, and the order between the first two is
load-bearing:

    subtitle_fetcher.py   MUST run before mkv_track_cleaner.py

``subtitle_fetcher.py`` searches OpenSubtitles by moviehash, which is the file
size plus the sum of the first and last 64 KiB. A remux rewrites those bytes, so
any movie cleaned first can never reproduce its release hash again and is
silently demoted to the much weaker title/year search. Running the four scripts
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
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from common import Report, enable_utf8_stdio, print_text

VERSION = "1.0.0"

HERE = Path(__file__).resolve().parent
DEFAULT_LIBRARY = r"E:\torrents\final_organized"

# The canonical order. Index order is the execution order; do not reorder
# without re-reading the moviehash note in the module docstring.
STEP_ORDER = ("fetcher", "cleaner", "10bit", "auditor")


@dataclass(frozen=True)
class Step:
    key: str
    script: str
    title: str
    # The flag each tool uses for the movie-library root; they are not uniform.
    root_flag: str
    supports_dry_run: bool = True
    supports_limit: bool = True
    supports_nice: bool = False


STEPS: dict[str, Step] = {
    "fetcher": Step(
        key="fetcher", script="subtitle_fetcher.py", title="Fetch English SRT subtitles",
        root_flag="--source",
    ),
    "cleaner": Step(
        key="cleaner", script="mkv_track_cleaner.py", title="Clean MKV tracks (remux)",
        root_flag="--dir", supports_nice=True,
    ),
    "10bit": Step(
        key="10bit", script="10bit.py", title="Check 8-bit vs 10-bit / HDR",
        root_flag="--source",
    ),
    "auditor": Step(
        key="auditor", script="library_auditor.py", title="Audit library layout",
        root_flag="--source", supports_dry_run=False, supports_limit=False,
    ),
}


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
        "cleaned - there is no override. If this step cleans nothing, open E:\\torrents\\final: "
        "qBittorrent's default seed-limit action only pauses the torrent and leaves the file, so "
        "the link count never drops and the deferral never clears. Delete the source (safe - it "
        "is a hardlink, so your library copy keeps the data) or set qBittorrent to remove the "
        "content when seeding stops."
    ),
    "fetcher": (
        "Runs before the cleaner on purpose: a remux rewrites the OpenSubtitles moviehash, "
        "so cleaning first would force the weaker title/year search."
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
# Prerequisites
# ---------------------------------------------------------------------------

def _api_key_present() -> bool:
    try:
        import subtitle_fetcher as sf
    except Exception:
        return False
    return bool(os.environ.get("OPENSUBTITLES_API_KEY") or sf.OPENSUBTITLES_API_KEY)


def _mkvmerge_present() -> bool:
    try:
        import mkv_track_cleaner as tc
        tc.resolve_mkvmerge_path()
        return True
    except Exception:
        return shutil.which("mkvmerge") is not None


def _ffprobe_present() -> bool:
    try:
        import _10bit  # type: ignore  # module name starts with a digit
        return _10bit.find_ffprobe() is not None
    except Exception:
        pass
    try:
        import importlib

        probe = importlib.import_module("10bit")
        return probe.find_ffprobe() is not None
    except Exception:
        return shutil.which("ffprobe") is not None


PREREQUISITES: dict[str, tuple[Callable[[], bool], str]] = {
    "fetcher": (
        _api_key_present,
        "no OpenSubtitles API key; set OPENSUBTITLES_API_KEY to enable fetching",
    ),
    "cleaner": (
        _mkvmerge_present,
        "mkvmerge (MKVToolNix) not found on PATH or in the standard install locations",
    ),
    "10bit": (
        _ffprobe_present,
        "ffprobe (FFmpeg) not found on PATH or in the standard install locations",
    ),
}


def prerequisite_issue(step: Step) -> str | None:
    """Return a reason to skip ``step``, or ``None`` when it can run."""
    script_path = HERE / step.script
    if not script_path.is_file():
        return f"{step.script} is missing from this directory"
    check, reason = PREREQUISITES.get(step.key, (lambda: True, ""))
    try:
        if not check():
            return reason
    except Exception:
        return reason or "prerequisite check failed"
    return None


def build_command(step: Step, cfg: Config) -> list[str]:
    """Build the argv for one step, mirroring what you would type by hand."""
    command = [sys.executable, str(HERE / step.script), step.root_flag, str(cfg.library)]
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
        completed = subprocess.run(command, cwd=str(HERE), check=False)
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


def resolve_library(cli_source: Path | None) -> Path:
    """Resolve the library root: explicit flag, then MOVIE_STD_TARGET, then default.

    Docker deployments point ``MOVIE_STD_TARGET`` at the container mount, so
    honoring it here lets ``docker compose run --rm organize run`` work without
    retyping ``--source`` on every invocation.
    """
    if cli_source is not None:
        return cli_source.expanduser().resolve()
    return Path(os.environ.get("MOVIE_STD_TARGET") or DEFAULT_LIBRARY).expanduser().resolve()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Run the manual Jellyfin movie steps in the correct order: "
                     "subtitles, then track cleaning, then bit depth, then audit."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("movie_standardizer.py is the qBittorrent completion hook and is not part of\n"
                "this sweep. Subtitles are fetched before the remux because a remux rewrites\n"
                "the OpenSubtitles moviehash and would force a weaker title/year search."),
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
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # The ordering is the whole point of this script.
    check(STEP_ORDER == ("fetcher", "cleaner", "10bit", "auditor"), "canonical step order")
    check(STEP_ORDER.index("fetcher") < STEP_ORDER.index("cleaner"),
          "subtitles must be fetched before the remux invalidates the moviehash")

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
    saved_target = os.environ.pop("MOVIE_STD_TARGET", None)
    try:
        check(resolve_library(None) == Path(DEFAULT_LIBRARY).resolve(),
              "no flag and no env resolves to the documented default library")
        os.environ["MOVIE_STD_TARGET"] = str(Path("/media/movies"))
        check(resolve_library(None) == Path("/media/movies").resolve(),
              "MOVIE_STD_TARGET is honored when no --source flag is given")
        check(resolve_library(Path("/srv/library")) == Path("/srv/library").resolve(),
              "an explicit --source flag beats MOVIE_STD_TARGET")
    finally:
        if saved_target is not None:
            os.environ["MOVIE_STD_TARGET"] = saved_target
        else:
            os.environ.pop("MOVIE_STD_TARGET", None)

    # Each tool's library-root flag differs; getting these wrong silently
    # points a tool at the default path instead of the requested library.
    check(STEPS["fetcher"].root_flag == "--source", "fetcher uses --source")
    check(STEPS["cleaner"].root_flag == "--dir", "cleaner uses --dir")
    check(STEPS["10bit"].root_flag == "--source", "10bit uses --source")
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


if __name__ == "__main__":
    raise SystemExit(main())
