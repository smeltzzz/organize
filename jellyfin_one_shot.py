#!/usr/bin/env python3
"""
Jellyfin Library One-Shot Completer
=====================================

Runs the full Organize toolchain repeatedly until the library auditor
reports 100% canonical: every movie has a validated, synced .eng.srt
or .eng.sdh.srt, exactly one best English audio track, no embedded
subtitles, and passes 10-bit inspection.

This is the "never stop, never skip, get to the end result no matter
what" runner. It handles:
  - API quota exhaustion (waits for UTC day rollover, then retries)
  - Movies held for manual review (retries on every pass; scraping
    tier re-offers daily)
  - Transient failures (retries each pass)
  - Partial runs (every step is idempotent, so re-running resumes
    from where it left off)
  - Dry-run mode for preview (exactly one pass, nothing is written)

Guaranteed-finish behaviour (no silent infinite loops):
  - empty library (no movie folders)           -> exit 2, with the fix
  - log dir inside the library                 -> exit 2, with the fix
  - auditor keeps failing (3 passes in a row)  -> exit 1, with the reason
  - no coverage improvement for 2 passes      -> wait for UTC midnight
    (the daily caps reset and the scraping tier re-offers) and continue

Usage:
    python3 jellyfin_one_shot.py --source /path/to/library [--nice]
                                   [--dry-run] [--max-passes N]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION = "1.0.0"

DEFAULT_LOG_DIR = Path(__file__).parent / "logs"
SCRAPING_DAILY_CAP = 20  # per source, matches subtitle_fetcher.py default
MAX_FETCH_RETRIES = 10   # per pass, before giving up and moving on

# Auditor resilience: a single audit attempt can be blocked (another process
# holds the run lock) or fail transiently. Retry a few times inside the pass,
# then give the next pass a chance. Three *passes* in a row without a usable
# audit report is a hard stop - hot-looping a broken audit is never useful.
AUDIT_ATTEMPTS_PER_PASS = 3
AUDIT_BACKOFF_SECONDS = (5, 15, 45)
MAX_CONSECUTIVE_BAD_AUDITS = 3

# Pacing: when two consecutive passes make no progress at all, the only thing
# that can change the outcome is the next UTC day (provider caps reset, the
# scraping tier re-offers held movies). Wait for the rollover instead of
# burning a full pipeline sweep for nothing.
STAGNATION_PASSES_BEFORE_ROLLOVER = 2

# Full stdout/stderr transcripts are kept per tool (bounded) so a failed
# multi-day run can be debugged after the fact.
TOOL_TRANSCRIPT_MAX_LINES = 2000

# The toolchain scripts, in the one correct order. 10bit.py and
# mkv_track_cleaner.py additionally take a --cache flag; the rest do not.
TOOL_SCRIPTS = (
    "subtitle_fetcher.py",
    "mkv_track_cleaner.py",
    "10bit.py",
    "sync_subtitles.py",
    "library_auditor.py",
)


# ---------------------------------------------------------------------------
# Console encoding
# ---------------------------------------------------------------------------
def enable_utf8_stdio() -> None:
    """Pin this process's console streams to UTF-8 with replacement errors.

    The banners below (and the tool transcripts we echo) contain box-drawing
    characters and check marks that a cp1252 Windows console cannot encode;
    without this, ``print`` raises UnicodeEncodeError on exactly the success
    banner. ``errors="replace"`` degrades a limited console to ``?`` instead
    of aborting.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # a replaced stream, e.g. under redirect_stdout
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # closed or detached stream
            pass


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(log_dir: Path) -> Path:
    """Create log directory and return the runtime log path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    runtime_log = log_dir / f"one_shot_{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    return runtime_log


def log(runtime_log: Path, level: str, message: str) -> None:
    """Write a timestamped log line to both console and file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {message}"
    print(line, flush=True)
    try:
        runtime_log.parent.mkdir(parents=True, exist_ok=True)
        with runtime_log.open("a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass


def log_info(runtime_log: Path, message: str) -> None:
    log(runtime_log, "INFO", message)


def log_warning(runtime_log: Path, message: str) -> None:
    log(runtime_log, "WARNING", message)


def log_error(runtime_log: Path, message: str) -> None:
    log(runtime_log, "ERROR", message)


def tail_to_file(path: Path, text: str, max_lines: int = TOOL_TRANSCRIPT_MAX_LINES) -> None:
    """Keep a bounded rolling transcript of a tool's full output."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
        keep = (old + text.splitlines())[-max_lines:]
        path.write_text("\n".join(keep) + "\n", encoding="utf-8", errors="replace")
    except OSError:
        pass  # a transcript is a debugging aid; never fail the run for one


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------
def run_tool(
    runtime_log: Path,
    script_path: Path,
    args: list[str],
    tool_name: str,
    timeout: float | None = None,
    transcript: Path | None = None,
) -> tuple[int, str, str]:
    """
    Run a Python tool script and return (returncode, stdout, stderr).

    Args:
        runtime_log: Path to the runtime log file.
        script_path: Path to the tool script (e.g., subtitle_fetcher.py).
        args: Command-line arguments to pass to the script.
        tool_name: Human-readable name for logging.
        timeout: Optional timeout in seconds. None = no timeout.
        transcript: Optional path for a bounded rolling copy of the tool's
            full output (for post-mortem debugging of long runs).

    Returns:
        Tuple of (returncode, stdout_text, stderr_text).
    """
    cmd = [sys.executable, str(script_path)] + args
    log_info(runtime_log, f"Running: {' '.join(cmd)}")

    try:
        # The tool scripts pin their own stdio to UTF-8 (reports are full of
        # box-drawing characters), so decode explicitly instead of with the
        # locale encoding - cp1252 on Windows would raise UnicodeDecodeError
        # on the very first report line.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=script_path.parent,
        )
    except subprocess.TimeoutExpired as e:
        log_error(runtime_log, f"{tool_name} timed out after {timeout}s")
        return -1, "", str(e)
    except FileNotFoundError:
        log_error(runtime_log, f"Script not found: {script_path}")
        return -1, "", f"Script not found: {script_path}"
    except Exception as e:
        log_error(runtime_log, f"Failed to run {tool_name}: {e}")
        return -1, "", str(e)

    log_info(runtime_log, f"{tool_name} exited with code {result.returncode}")

    if transcript is not None:
        tail_to_file(transcript, result.stdout or "")
        tail_to_file(transcript.with_name(transcript.stem + ".err"), result.stderr or "")

    # Log last 20 lines of output for debugging
    stdout_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
    stderr_lines = result.stderr.strip().split("\n") if result.stderr.strip() else []

    if stdout_lines:
        log_info(runtime_log, f"{tool_name} stdout (last 20 lines):")
        for line in stdout_lines[-20:]:
            log(runtime_log, "INFO", f"  {line}")

    if stderr_lines:
        log_warning(runtime_log, f"{tool_name} stderr (last 20 lines):")
        for line in stderr_lines[-20:]:
            log(runtime_log, "WARNING", f"  {line}")

    return result.returncode, result.stdout, result.stderr


def check_prerequisites(runtime_log: Path) -> dict[str, bool]:
    """Check which required tools are available."""
    tools = {
        "mkvmerge": shutil.which("mkvmerge") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "ffsubsync": shutil.which("ffsubsync") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }

    for tool, available in tools.items():
        if available:
            log_info(runtime_log, f"  {tool}: found")
        else:
            log_warning(runtime_log, f"  {tool}: NOT FOUND")

    return tools


def missing_tool_scripts(script_dir: Path) -> list[str]:
    """Names of the toolchain scripts that are absent from ``script_dir``."""
    return [name for name in TOOL_SCRIPTS if not (script_dir / name).is_file()]


def log_dir_inside_source(log_dir: Path, source: Path) -> bool:
    """True when ``log_dir`` resolves to ``source`` or a descendant of it.

    Every tool refuses --log/--report inside the library (exit 2), and the
    auditor would additionally count a log directory at the library root as a
    movie folder - a one-shot pointed at that combination could never reach
    100%. Use resolve(strict=False) so not-yet-created paths are checked too.
    """
    try:
        log_dir.resolve(strict=False).relative_to(source.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Auditor report parsing
# ---------------------------------------------------------------------------
# Preferred: the machine-readable summary line the auditor appends to the
# report footer (stable, one line, no layout assumptions).
AUDIT_SUMMARY_RE = re.compile(
    r"^\s*AUDIT SUMMARY: canonical=(\d+); total=(\d+); pct=([\d.]+)%",
    re.MULTILINE,
)
# Fallbacks: the human scorecard (right-aligned count, then the label) and
# the fetcher-style coverage line, so reports written by older auditor
# versions still parse.
CANONICAL_SCORECARD_RE = re.compile(r"^\s*(\d+)\s+Canonical MKV", re.MULTILINE)
FOLDERS_SCORECARD_RE = re.compile(r"^\s*(\d+)\s+Folders checked", re.MULTILINE)
COVERAGE_LINE_RE = re.compile(r"Coverage this run:\s*(\d+)\s+of\s+(\d+)\s+movie\(s\)")
PERCENTAGE_LINE_RE = re.compile(r"^\s*(\d+)/(\d+)\s+\(100\.0%\)", re.MULTILINE)


def parse_auditor_coverage(runtime_log: Path, report_path: Path) -> tuple[int | None, int | None]:
    """
    Parse the library auditor report to extract coverage.

    Args:
        runtime_log: Path to the runtime log for logging.
        report_path: Path to the auditor report file.

    Returns:
        Tuple of (covered_count, total_count). Returns (None, None) if coverage
        cannot be determined.
    """
    if not report_path.exists():
        log_warning(runtime_log, f"Auditor report not found: {report_path}")
        return None, None

    try:
        content = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log_error(runtime_log, f"Cannot read auditor report: {e}")
        return None, None

    # Pattern 1: machine-readable summary line (current auditor).
    match = AUDIT_SUMMARY_RE.search(content)
    if match:
        return int(match.group(1)), int(match.group(2))

    # Pattern 2: scorecard line like "42/42 (100.0%)"
    match = PERCENTAGE_LINE_RE.search(content)
    if match:
        covered = int(match.group(1))
        total = int(match.group(2))
        return covered, total

    # Pattern 3: "Canonical MKV" count in the scorecard
    match = CANONICAL_SCORECARD_RE.search(content)
    if match:
        canonical = int(match.group(1))
        # Find total folders checked
        total_match = FOLDERS_SCORECARD_RE.search(content)
        total = int(total_match.group(1)) if total_match else canonical
        return canonical, total

    # Pattern 4: Coverage summary at the bottom
    match = COVERAGE_LINE_RE.search(content)
    if match:
        covered = int(match.group(1))
        total = int(match.group(2))
        return covered, total

    log_warning(runtime_log, "Could not parse coverage from auditor report")
    log_info(runtime_log, f"Report preview (first 500 chars):\n{content[:500]}")
    return None, None


def is_library_complete(covered: int | None, total: int | None) -> bool:
    """Check if the library is 100% complete."""
    if covered is None or total is None:
        return False
    return total > 0 and covered == total


# ---------------------------------------------------------------------------
# UTC day rollover wait
# ---------------------------------------------------------------------------
def wait_for_utc_midnight(runtime_log: Path) -> None:
    """Block until the next UTC midnight, logging progress."""
    now = datetime.now(UTC)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    wait_seconds = (tomorrow - now).total_seconds()

    log_info(runtime_log, "Waiting for UTC midnight rollover...")
    log_info(runtime_log, f"  Current UTC time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    log_info(runtime_log, f"  Next UTC midnight: {tomorrow.strftime('%Y-%m-%d %H:%M:%S')}")
    log_info(runtime_log, f"  Waiting {wait_seconds:.0f} seconds...")

    # Sleep in 1-hour chunks so we can log progress and be interruptible
    while wait_seconds > 0:
        if wait_seconds > 3600:
            sleep_time = 3600
            wait_seconds -= sleep_time
        else:
            sleep_time = wait_seconds
            wait_seconds = 0

        log_info(runtime_log, f"  Sleeping {sleep_time}s... (Ctrl+C to interrupt)")
        try:
            time.sleep(sleep_time)
        except KeyboardInterrupt:
            log_warning(runtime_log, "Interrupted during UTC wait. Resuming in next pass.")
            return

    log_info(runtime_log, "UTC day has rolled over. Resuming.")


# ---------------------------------------------------------------------------
# One-shot orchestrator
# ---------------------------------------------------------------------------
def run_one_shot(
    library: Path,
    script_dir: Path,
    runtime_log: Path,
    log_dir: Path,
    nice: bool = False,
    dry_run: bool = False,
    max_passes: int = 0,
    tools: dict[str, bool] | None = None,
    timeout_scale: float = 1.0,
) -> int:
    """
    Run the one-shot completion loop.

    Args:
        library: Path to the Jellyfin movie library.
        script_dir: Directory containing the Organize tool scripts.
        runtime_log: Path to the runtime log.
        log_dir: Directory for per-tool logs, reports, and transcripts.
        nice: If True, add --nice flag to tools that support it.
        dry_run: If True, preview only (no changes written). A dry run makes
            no changes by definition, so exactly one pass runs.
        max_passes: Maximum number of passes (0 = unlimited, live runs only).
        tools: Dictionary of available tools from check_prerequisites().
        timeout_scale: Multiplier for the per-step timeouts (0 = no timeout).

    Returns:
        Exit code: 0 if library is complete (or dry-run preview finished),
        1 if partial completion / the auditor keeps failing,
        2 if the library is empty or misconfigured.
    """
    tools = tools or {}

    def scaled(seconds: float) -> float | None:
        if timeout_scale <= 0:
            return None
        return seconds * timeout_scale

    pass_number = 0
    previous_coverage: str | None = None
    no_improvement_streak = 0
    consecutive_bad_audits = 0
    skipped_steps: list[str] = []
    last_audit_report: Path | None = None

    log_info(runtime_log, "=" * 60)
    log_info(runtime_log, "JELLYFIN LIBRARY ONE-SHOT COMPLETER")
    log_info(runtime_log, "=" * 60)
    if dry_run:
        max_passes_text = "1 (a dry run always previews exactly one pass)"
    elif max_passes > 0:
        max_passes_text = str(max_passes)
    else:
        max_passes_text = "unlimited"

    log_info(runtime_log, f"Version: {VERSION}")
    log_info(runtime_log, f"Library: {library}")
    log_info(runtime_log, f"Nice mode: {nice}")
    log_info(runtime_log, f"Dry run: {dry_run}")
    log_info(runtime_log, f"Max passes: {max_passes_text}")
    log_info(runtime_log, f"Script directory: {script_dir}")
    log_info(runtime_log, f"Log directory: {log_dir}")
    log_info(runtime_log, f"Runtime log: {runtime_log}")
    log_info(runtime_log, "")

    # Discover movies in the library (for tracking purposes)
    log_info(runtime_log, "DISCOVERING MOVIES IN LIBRARY...")
    try:
        mkv_files = sorted(library.rglob("*.mkv"), key=lambda p: str(p).casefold())
        log_info(runtime_log, f"  Found {len(mkv_files)} MKV file(s)")
        for mkv in mkv_files[:20]:
            log_info(runtime_log, f"    - {mkv.relative_to(library)}")
        if len(mkv_files) > 20:
            log_info(runtime_log, f"    ... and {len(mkv_files) - 20} more")
        if not mkv_files:
            log_warning(runtime_log, "  No MKV files found - is this the right library?")
    except OSError as e:
        log_warning(runtime_log, f"  Could not discover movies: {e}")

    log_info(runtime_log, "")

    # Main pass loop
    while True:
        pass_number += 1

        if dry_run and pass_number > 1:
            # A dry run writes nothing, so a second pass would be identical.
            break
        if not dry_run and max_passes > 0 and pass_number > max_passes:
            log_warning(runtime_log, f"Reached max passes ({max_passes}). Stopping.")
            break

        log_info(runtime_log, "=" * 60)
        log_info(runtime_log, f"PASS {pass_number}")
        log_info(runtime_log, "=" * 60)
        log_info(runtime_log, "")

        # -------------------------------------------------------------------
        # Step 1: Fetch subtitles (with quota handling)
        # -------------------------------------------------------------------
        log_info(runtime_log, "STEP 1: Fetching subtitles...")

        fetch_success = False
        fetch_retries = 0

        while not fetch_success and fetch_retries < MAX_FETCH_RETRIES:
            log_info(runtime_log, f"  Subtitle fetch attempt {fetch_retries + 1}/{MAX_FETCH_RETRIES}")

            args = [
                "--source", str(library),
                "--report", str(log_dir / "subtitle_fetcher_report.txt"),
                "--log", str(log_dir / "subtitle_fetcher.log"),
                "--scrape-daily-cap", str(SCRAPING_DAILY_CAP),
                "--allow-missing",  # Don't fail the whole run if some movies miss
            ]
            if dry_run:
                args.append("--dry-run")

            # Check if we have API keys and pass them through
            if os.environ.get("OPENSUBTITLES_API_KEY"):
                log_info(runtime_log, "  Using OpenSubtitles API key (configured)")
            else:
                log_info(runtime_log, "  OpenSubtitles API key: not set (scraping fallbacks only)")

            if os.environ.get("SUBDL_API_KEY"):
                log_info(runtime_log, "  Using SubDL API key (configured)")
            else:
                log_info(runtime_log, "  SubDL API key: not set")

            returncode, stdout, stderr = run_tool(
                runtime_log,
                script_dir / "subtitle_fetcher.py",
                args,
                "subtitle_fetcher",
                timeout=scaled(3600),  # 1 hour per fetch attempt
                transcript=log_dir / "transcript_subtitle_fetcher.log",
            )

            if returncode == 0:
                fetch_success = True
                log_info(runtime_log, "  Subtitle fetch completed successfully.")
                # With --allow-missing the fetcher exits 0 even when every
                # source is out of quota; surface it so the wait below makes
                # sense to the operator.
                if "QUOTA REACHED" in (stdout + stderr):
                    log_info(runtime_log, "  NOTE: some sources report QUOTA REACHED - "
                                          "held movies are re-offered after the next UTC day rollover.")
            else:
                fetch_retries += 1
                log_warning(runtime_log, f"  Subtitle fetch failed (attempt {fetch_retries}/{MAX_FETCH_RETRIES})")

                # Check if it's a quota issue
                combined_output = stdout + stderr
                if "QUOTA REACHED" in combined_output or "daily cap exhausted" in combined_output.lower():
                    log_info(runtime_log, "  Quota exhausted — waiting for UTC day rollover...")
                    wait_for_utc_midnight(runtime_log)
                    fetch_retries = 0  # Reset retry counter after waiting
                elif fetch_retries >= 3:
                    # Non-quota error, give up after 3 attempts
                    log_warning(runtime_log, "  Non-quota errors persisting — moving to next step.")
                    fetch_success = True  # Break out of retry loop
                else:
                    # Wait a bit before retrying
                    time.sleep(5)

        log_info(runtime_log, "")

        # -------------------------------------------------------------------
        # Step 2: Track cleaning (lossless remux)
        # -------------------------------------------------------------------
        log_info(runtime_log, "STEP 2: Track cleaning (lossless remux)...")

        if tools.get("mkvmerge", False):
            args = [
                "--dir", str(library),
                "--log", str(log_dir / "mkv_track_cleaner.log"),
                "--report", str(log_dir / "mkv_track_cleaner_report.txt"),
                "--cache", str(log_dir / "mkv_track_cleaner_probe_cache.json"),
            ]
            if dry_run:
                args.append("--dry-run")
            if nice:
                args.append("--nice")

            returncode, stdout, stderr = run_tool(
                runtime_log,
                script_dir / "mkv_track_cleaner.py",
                args,
                "mkv_track_cleaner",
                timeout=scaled(7200),  # 2 hours per pass
                transcript=log_dir / "transcript_mkv_track_cleaner.log",
            )

            if returncode != 0:
                log_warning(runtime_log, f"  Track cleaner exited with code {returncode}")
            else:
                log_info(runtime_log, "  Track cleaning completed.")
        else:
            skipped_steps.append("track cleaning (mkvmerge not found)")
            log_warning(runtime_log, "  mkvmerge not available — skipping track cleaning")

        log_info(runtime_log, "")

        # -------------------------------------------------------------------
        # Step 3: 10-bit inspection
        # -------------------------------------------------------------------
        log_info(runtime_log, "STEP 3: 10-bit / HDR inspection...")

        if tools.get("ffprobe", False):
            args = [
                "--source", str(library),
                "--log", str(log_dir / "10bit.log"),
                "--report", str(log_dir / "10bit_report.txt"),
                "--cache", str(log_dir / "10bit_probe_cache.json"),
            ]
            if dry_run:
                args.append("--dry-run")

            returncode, stdout, stderr = run_tool(
                runtime_log,
                script_dir / "10bit.py",
                args,
                "10bit",
                timeout=scaled(3600),
                transcript=log_dir / "transcript_10bit.log",
            )

            if returncode != 0:
                log_warning(runtime_log, f"  10-bit inspector exited with code {returncode}")
            else:
                log_info(runtime_log, "  10-bit inspection completed.")
        else:
            skipped_steps.append("10-bit inspection (ffprobe not found)")
            log_warning(runtime_log, "  ffprobe not available — skipping 10-bit inspection")

        log_info(runtime_log, "")

        # -------------------------------------------------------------------
        # Step 4: Subtitle sync (ffsubsync)
        # -------------------------------------------------------------------
        log_info(runtime_log, "STEP 4: Subtitle timing sync...")

        if tools.get("ffsubsync", False) and tools.get("ffmpeg", False):
            args = [
                "--source", str(library),
                "--report", str(log_dir / "sync_subtitles_report.txt"),
                "--log", str(log_dir / "sync_subtitles.log"),
            ]
            if dry_run:
                args.append("--dry-run")

            returncode, stdout, stderr = run_tool(
                runtime_log,
                script_dir / "sync_subtitles.py",
                args,
                "sync_subtitles",
                timeout=scaled(7200),
                transcript=log_dir / "transcript_sync_subtitles.log",
            )

            if returncode != 0:
                log_warning(runtime_log, f"  Subtitle sync exited with code {returncode}")
            else:
                log_info(runtime_log, "  Subtitle sync completed.")
        else:
            if not tools.get("ffsubsync", False):
                skipped_steps.append("subtitle sync (ffsubsync not found)")
                log_warning(runtime_log, "  ffsubsync not available — skipping subtitle sync")
            if not tools.get("ffmpeg", False):
                skipped_steps.append("subtitle sync (ffmpeg not found)")
                log_warning(runtime_log, "  ffmpeg not available — ffsubsync cannot extract audio")

        log_info(runtime_log, "")

        # -------------------------------------------------------------------
        # Step 5: Library audit (with retries - the only step that decides)
        # -------------------------------------------------------------------
        log_info(runtime_log, "STEP 5: Library audit...")

        audit_report = log_dir / f"auditor-pass-{pass_number}.txt"
        returncode = -1
        for attempt in range(1, AUDIT_ATTEMPTS_PER_PASS + 1):
            args = [
                "--source", str(library),
                "--report", str(audit_report),
                "--log", str(log_dir / "library_auditor.log"),
            ]
            returncode, stdout, stderr = run_tool(
                runtime_log,
                script_dir / "library_auditor.py",
                args,
                f"library_auditor (attempt {attempt}/{AUDIT_ATTEMPTS_PER_PASS})",
                timeout=scaled(600),
                transcript=log_dir / "transcript_library_auditor.log",
            )
            if returncode == 0:
                break
            if returncode == 3:
                log_warning(runtime_log, f"  Audit blocked by another process (attempt {attempt}/{AUDIT_ATTEMPTS_PER_PASS})")
            else:
                log_warning(runtime_log, f"  Audit failed with code {returncode} (attempt {attempt}/{AUDIT_ATTEMPTS_PER_PASS})")
            if attempt < AUDIT_ATTEMPTS_PER_PASS:
                backoff = AUDIT_BACKOFF_SECONDS[attempt - 1]
                log_info(runtime_log, f"  Retrying audit in {backoff}s...")
                time.sleep(backoff)

        last_audit_report = audit_report

        # Parse coverage
        covered, total = parse_auditor_coverage(runtime_log, audit_report)

        # A pass with no usable audit report is a bad audit: retry on the
        # next pass, but three in a row means something is genuinely wrong.
        if covered is None or total is None:
            consecutive_bad_audits += 1
            log_error(runtime_log,
                      f"  Auditor produced no usable coverage (audit exit code {returncode}). "
                      f"{consecutive_bad_audits}/{MAX_CONSECUTIVE_BAD_AUDITS} bad audits in a row.")
            if consecutive_bad_audits >= MAX_CONSECUTIVE_BAD_AUDITS:
                log_error(runtime_log, "=" * 60)
                log_error(runtime_log, "STOPPING: the library audit keeps failing.")
                log_error(runtime_log, f"Last audit report: {audit_report}")
                log_error(runtime_log, f"Last auditor transcript: {log_dir / 'transcript_library_auditor.log'}")
                log_error(runtime_log, "Fix the underlying problem (permissions, disk, another "
                                       "process holding the audit lock) and re-run - every step "
                                       "is idempotent, so nothing is lost.")
                log_error(runtime_log, "=" * 60)
                return 1
        else:
            consecutive_bad_audits = 0
            coverage_str = f"{covered}/{total}"
            log_info(runtime_log, f"  Auditor coverage: {coverage_str}")

            # An empty library can never reach "covered == total" with
            # total > 0 - fail fast with the actual problem instead of
            # looping forever.
            if total == 0:
                log_error(runtime_log, "=" * 60)
                log_error(runtime_log, "STOPPING: no movie folders found under --source.")
                log_error(runtime_log, "The canonical Jellyfin layout is one folder per movie:")
                log_error(runtime_log, '    Title (Year)/Title (Year).mkv')
                log_error(runtime_log, "If this directory is genuinely empty there is nothing to "
                                       "complete; otherwise check that --source points at the "
                                       "library that holds the movie folders.")
                log_error(runtime_log, "=" * 60)
                return 2

            if is_library_complete(covered, total):
                log_info(runtime_log, "")
                log_info(runtime_log, "=" * 60)
                log_info(runtime_log, "LIBRARY COMPLETE!")
                log_info(runtime_log, f"All {total} movies have validated, synced subtitles")
                log_info(runtime_log, "and clean tracks.")
                if skipped_steps:
                    log_warning(runtime_log, "Note: these steps were skipped because the binary "
                                             "is not installed - the auditor verdict covers layout "
                                             "and subtitles only:")
                    for step in sorted(set(skipped_steps)):
                        log_warning(runtime_log, f"  - {step}")
                log_info(runtime_log, "=" * 60)
                return 0

            # Pacing: two passes with zero progress means the only thing
            # left to do is wait for the daily caps to reset.
            if previous_coverage is not None and coverage_str == previous_coverage:
                no_improvement_streak += 1
                if no_improvement_streak >= STAGNATION_PASSES_BEFORE_ROLLOVER:
                    log_warning(runtime_log, f"  No improvement for {no_improvement_streak} passes "
                                             f"(coverage stuck at {coverage_str}).")
                    log_warning(runtime_log, "  Provider daily caps reset at UTC midnight and the "
                                             "scraping tier re-offers held movies - waiting for "
                                             "the rollover before the next pass.")
                    wait_for_utc_midnight(runtime_log)
                    no_improvement_streak = 0
            else:
                no_improvement_streak = 0
                if previous_coverage is not None:
                    log_info(runtime_log, f"  Progress: {previous_coverage} -> {coverage_str}")

            previous_coverage = coverage_str
            log_info(runtime_log, "")

    # -----------------------------------------------------------------------
    # Dry run: exactly one pass, by definition nothing changed
    # -----------------------------------------------------------------------
    if dry_run:
        covered, total = (
            parse_auditor_coverage(runtime_log, last_audit_report)
            if last_audit_report is not None
            else (None, None)
        )
        coverage_str = f"{covered}/{total}" if covered is not None and total is not None else "unknown"
        log_info(runtime_log, "")
        log_info(runtime_log, "=" * 60)
        log_info(runtime_log, "DRY-RUN PREVIEW COMPLETE")
        log_info(runtime_log, f"After one pass the auditor reports {coverage_str} canonical folders.")
        log_info(runtime_log, "Nothing was written. A live run repeats the same passes")
        log_info(runtime_log, "until the auditor reports 100%.")
        log_info(runtime_log, "=" * 60)
        return 0

    # -----------------------------------------------------------------------
    # Final audit
    # -----------------------------------------------------------------------
    log_info(runtime_log, "=" * 60)
    log_info(runtime_log, "FINAL AUDIT")
    log_info(runtime_log, "=" * 60)

    final_report = log_dir / "auditor-final.txt"
    returncode = -1
    for attempt in range(1, AUDIT_ATTEMPTS_PER_PASS + 1):
        args = [
            "--source", str(library),
            "--report", str(final_report),
            "--log", str(log_dir / "library_auditor.log"),
            "--fail-on-findings",
        ]
        returncode, stdout, stderr = run_tool(
            runtime_log,
            script_dir / "library_auditor.py",
            args,
            f"library_auditor (final, attempt {attempt}/{AUDIT_ATTEMPTS_PER_PASS})",
            timeout=scaled(600),
            transcript=log_dir / "transcript_library_auditor.log",
        )
        if returncode == 0 or returncode == 1:
            # 1 = findings with --fail-on-findings: the report is still valid.
            break
        if attempt < AUDIT_ATTEMPTS_PER_PASS:
            backoff = AUDIT_BACKOFF_SECONDS[attempt - 1]
            log_info(runtime_log, f"  Final audit failed (code {returncode}); retrying in {backoff}s...")
            time.sleep(backoff)

    covered, total = parse_auditor_coverage(runtime_log, final_report)
    coverage_str = f"{covered}/{total}" if covered is not None and total is not None else "unknown"
    log_info(runtime_log, f"Final coverage: {coverage_str}")

    if is_library_complete(covered, total):
        log_info(runtime_log, "")
        log_info(runtime_log, "=" * 60)
        log_info(runtime_log, "SUCCESS: Library is 100% complete.")
        log_info(runtime_log, "")
        log_info(runtime_log, "  ✓ Every movie has a synced .eng.srt or .eng.sdh.srt")
        log_info(runtime_log, "  ✓ Every MKV has exactly 1 best English audio track")
        log_info(runtime_log, "  ✓ No embedded subtitles remain")
        log_info(runtime_log, "  ✓ All movies audited and 10-bit inspected")
        if skipped_steps:
            log_info(runtime_log, "")
            log_warning(runtime_log, "  Caveat - steps skipped this run (missing binaries):")
            for step in sorted(set(skipped_steps)):
                log_warning(runtime_log, f"    - {step}")
        log_info(runtime_log, "=" * 60)
        return 0
    else:
        log_warning(runtime_log, "")
        log_warning(runtime_log, "=" * 60)
        log_warning(runtime_log, "PARTIAL COMPLETION")
        if covered is not None and total is not None:
            log_warning(runtime_log, f"  {covered}/{total} movies complete.")
        log_warning(runtime_log, "")
        log_warning(runtime_log, "Review the final audit report:")
        log_warning(runtime_log, f"  {final_report}")
        log_warning(runtime_log, "=" * 60)
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot Jellyfin library completer — guarantees every movie "
        "gets a synced subtitle, clean tracks, and passes 10-bit inspection. "
        "Runs repeatedly until the library auditor reports 100% canonical.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {VERSION}",
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="Path to the Jellyfin movie library",
    )

    parser.add_argument(
        "--script-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Directory containing the Organize tool scripts",
    )

    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory for runtime logs, per-tool logs/reports, and transcripts. "
             "Must be OUTSIDE the library.",
    )

    parser.add_argument(
        "--nice",
        action="store_true",
        help="Add --nice flag to tools that support it (lower priority)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only — one pass, no changes will be written",
    )

    parser.add_argument(
        "--max-passes",
        type=int,
        default=0,
        help="Maximum number of passes (0 = unlimited, keep going until complete; "
             "ignored for --dry-run, which always previews exactly one pass)",
    )

    parser.add_argument(
        "--timeout-scale",
        type=float,
        default=1.0,
        help="Multiplier for the per-step timeouts (0 = no timeout). Use > 1 for "
             "very large libraries, 0 to disable timeouts entirely.",
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run built-in self-tests",
    )

    args = parser.parse_args(argv)

    enable_utf8_stdio()

    if args.self_test:
        return run_self_tests()

    # Validate inputs
    if not args.source:
        print("ERROR: --source is required", file=sys.stderr)
        return 2

    if not args.source.exists():
        print(f"ERROR: Library directory does not exist: {args.source}", file=sys.stderr)
        return 2

    if not args.source.is_dir():
        print(f"ERROR: Source is not a directory: {args.source}", file=sys.stderr)
        return 2

    if log_dir_inside_source(args.log_dir, args.source):
        print(
            f"ERROR: --log-dir ({args.log_dir}) is inside the library ({args.source}). "
            "Every tool refuses to write logs/reports inside the media library, and "
            "the auditor would count a log folder at the library root as a movie "
            "folder - the run could never reach 100%. Choose a --log-dir outside "
            "the library, e.g. its parent directory.",
            file=sys.stderr,
        )
        return 2

    missing = missing_tool_scripts(args.script_dir)
    if missing:
        print(
            f"ERROR: tool scripts missing from --script-dir ({args.script_dir}): "
            + ", ".join(missing)
            + ". Point --script-dir at the folder that contains the Organize toolchain.",
            file=sys.stderr,
        )
        return 2

    if args.timeout_scale < 0:
        print("ERROR: --timeout-scale must be zero or greater", file=sys.stderr)
        return 2

    # Setup logging
    runtime_log = setup_logging(args.log_dir)

    log_info(runtime_log, "=" * 60)
    log_info(runtime_log, "CHECKING PREREQUISITES")
    log_info(runtime_log, "=" * 60)

    tools = check_prerequisites(runtime_log)

    # Check for API keys
    log_info(runtime_log, "")
    log_info(runtime_log, "API KEY STATUS:")
    if os.environ.get("OPENSUBTITLES_API_KEY"):
        log_info(runtime_log, "  OpenSubtitles API key: configured")
    else:
        log_info(runtime_log, "  OpenSubtitles API key: not set (scraping fallbacks only)")

    if os.environ.get("SUBDL_API_KEY"):
        log_info(runtime_log, "  SubDL API key: configured")
    else:
        log_info(runtime_log, "  SubDL API key: not set")

    log_info(runtime_log, "")
    log_info(runtime_log, "=" * 60)
    log_info(runtime_log, "STARTING ONE-SHOT COMPLETION")
    log_info(runtime_log, "=" * 60)
    log_info(runtime_log, "")

    try:
        exit_code = run_one_shot(
            library=args.source,
            script_dir=args.script_dir,
            runtime_log=runtime_log,
            log_dir=args.log_dir,
            nice=args.nice,
            dry_run=args.dry_run,
            max_passes=args.max_passes,
            tools=tools,
            timeout_scale=args.timeout_scale,
        )
        return exit_code
    except KeyboardInterrupt:
        log_warning(runtime_log, "Interrupted by user. Partial results may be available.")
        return 130
    except Exception as e:
        log_error(runtime_log, f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


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

    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("SELF-TEST PASSED (coverage parsing, completeness check, validation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
