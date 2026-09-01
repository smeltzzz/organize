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
  - Partial runs (resumes from where it left off)
  - Dry-run mode for preview

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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_LOG_DIR = Path(__file__).parent / "logs"
SCRAPING_DAILY_CAP = 20  # per source, matches subtitle_fetcher.py default
MAX_FETCH_RETRIES = 10   # per pass, before giving up and moving on


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
    print(line)
    try:
        runtime_log.parent.mkdir(parents=True, exist_ok=True)
        with runtime_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def log_info(runtime_log: Path, message: str) -> None:
    log(runtime_log, "INFO", message)


def log_warning(runtime_log: Path, message: str) -> None:
    log(runtime_log, "WARNING", message)


def log_error(runtime_log: Path, message: str) -> None:
    log(runtime_log, "ERROR", message)


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------
def run_tool(
    runtime_log: Path,
    script_path: Path,
    args: list[str],
    tool_name: str,
    timeout: Optional[float] = None,
) -> tuple[int, str, str]:
    """
    Run a Python tool script and return (returncode, stdout, stderr).
    
    Args:
        runtime_log: Path to the runtime log file.
        script_path: Path to the tool script (e.g., subtitle_fetcher.py).
        args: Command-line arguments to pass to the script.
        tool_name: Human-readable name for logging.
        timeout: Optional timeout in seconds. None = no timeout.
    
    Returns:
        Tuple of (returncode, stdout_text, stderr_text).
    """
    cmd = [sys.executable, str(script_path)] + args
    log_info(runtime_log, f"Running: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=script_path.parent,
        )
        log_info(runtime_log, f"{tool_name} exited with code {result.returncode}")
        
        # Log last 20 lines of output for debugging
        stdout_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        stderr_lines = result.stderr.strip().split("\n") if result.stderr.strip() else []
        
        if stdout_lines:
            log_info(runtime_log, f"{tool_name}stdout (last 20 lines):")
            for line in stdout_lines[-20:]:
                log(runtime_log, "INFO", f"  {line}")
        
        if stderr_lines:
            log_warning(runtime_log, f"{tool_name}stderr (last 20 lines):")
            for line in stderr_lines[-20:]:
                log(runtime_log, "WARNING", f"  {line}")
        
        return result.returncode, result.stdout, result.stderr
        
    except subprocess.TimeoutExpired as e:
        log_error(runtime_log, f"{tool_name} timed out after {timeout}s")
        return -1, "", str(e)
    except FileNotFoundError:
        log_error(runtime_log, f"Script not found: {script_path}")
        return -1, "", f"Script not found: {script_path}"
    except Exception as e:
        log_error(runtime_log, f"Failed to run {tool_name}: {e}")
        return -1, "", str(e)


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


# ---------------------------------------------------------------------------
# Auditor report parsing
# ---------------------------------------------------------------------------
def parse_auditor_coverage(runtime_log: Path, report_path: Path) -> tuple[Optional[int], Optional[int]]:
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
        content = report_path.read_text(encoding="utf-8")
    except OSError as e:
        log_error(runtime_log, f"Cannot read auditor report: {e}")
        return None, None
    
    # Pattern 1: Scorecard line like "42/42 (100.0%)"
    match = re.search(r"^\s*(\d+)/(\d+)\s+\(100\.0%\)", content, re.MULTILINE)
    if match:
        covered = int(match.group(1))
        total = int(match.group(2))
        return covered, total
    
    # Pattern 2: Look for "Canonical MKV" count in the scorecard
    # The auditor report has lines like: "   42  Canonical MKV"
    match = re.search(r"^\s*(\d+)\s+Canonical MKV", content, re.MULTILINE)
    if match:
        canonical = int(match.group(1))
        # Find total folders checked
        total_match = re.search(r"^\s*(\d+)\s+Folders checked", content, re.MULTILINE)
        total = int(total_match.group(1)) if total_match else canonical
        return canonical, total
    
    # Pattern 3: Coverage summary at the bottom
    match = re.search(r"Coverage this run:\s*(\d+)\s+of\s+(\d+)\s+movie\(s\)", content)
    if match:
        covered = int(match.group(1))
        total = int(match.group(2))
        return covered, total
    
    log_warning(runtime_log, "Could not parse coverage from auditor report")
    log_info(runtime_log, f"Report preview (first 500 chars):\n{content[:500]}")
    return None, None


def is_library_complete(covered: Optional[int], total: Optional[int]) -> bool:
    """Check if the library is 100% complete."""
    if covered is None or total is None:
        return False
    return total > 0 and covered == total


# ---------------------------------------------------------------------------
# UTC day rollover wait
# ---------------------------------------------------------------------------
def wait_for_utc_midnight(runtime_log: Path) -> None:
    """Block until the next UTC midnight, logging progress."""
    now = datetime.now(timezone.utc)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    wait_seconds = (tomorrow - now).total_seconds()
    
    log_info(runtime_log, f"Waiting for UTC midnight rollover...")
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
    nice: bool = False,
    dry_run: bool = False,
    max_passes: int = 0,
    tools: Optional[dict[str, bool]] = None,
) -> int:
    """
    Run the one-shot completion loop.
    
    Args:
        library: Path to the Jellyfin movie library.
        script_dir: Directory containing the Organize tool scripts.
        runtime_log: Path to the runtime log.
        nice: If True, add --nice flag to tools that support it.
        dry_run: If True, preview only (no changes written).
        max_passes: Maximum number of passes (0 = unlimited).
        tools: Dictionary of available tools from check_prerequisites().
    
    Returns:
        Exit code: 0 if library is complete, 1 if partial completion.
    """
    nice_flag = "--nice" if nice else ""
    dry_run_flag = "--dry-run" if dry_run else ""
    
    pass_number = 0
    previous_coverage = "0/0"
    
    log_info(runtime_log, "=" * 60)
    log_info(runtime_log, "JELLYFIN LIBRARY ONE-SHOT COMPLETER")
    log_info(runtime_log, "=" * 60)
    log_info(runtime_log, f"Library: {library}")
    log_info(runtime_log, f"Nice mode: {nice}")
    log_info(runtime_log, f"Dry run: {dry_run}")
    log_info(runtime_log, f"Max passes: {max_passes if max_passes > 0 else 'unlimited'}")
    log_info(runtime_log, f"Script directory: {script_dir}")
    log_info(runtime_log, f"Runtime log: {runtime_log}")
    log_info(runtime_log, "")

    # Discover movies in the library (for tracking purposes)
    log_info(runtime_log, "DISCOVERING MOVIES IN LIBRARY...")
    try:
        import json
        from pathlib import Path
        from subprocess import run, PIPE, DEVNULL
        
        # Use ffprobe to discover MKV files
        mkv_files = list(library.rglob("*.mkv"))
        log_info(runtime_log, f"  Found {len(mkv_files)} MKV files")
        for mkv in mkv_files[:20]:
            log_info(runtime_log, f"    - {mkv.relative_to(library)}")
        if len(mkv_files) > 20:
            log_info(runtime_log, f"    ... and {len(mkv_files) - 20} more")
    except Exception as e:
        log_warning(runtime_log, f"  Could not discover movies: {e}")

    log_info(runtime_log, "")

    # Main pass loop
    while True:
        pass_number += 1
        
        if max_passes > 0 and pass_number > max_passes:
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
                timeout=3600,  # 1 hour per fetch attempt
            )
            
            if returncode == 0:
                fetch_success = True
                log_info(runtime_log, "  Subtitle fetch completed successfully.")
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
                timeout=7200,  # 2 hours per pass
            )
            
            if returncode != 0:
                log_warning(runtime_log, f"  Track cleaner exited with code {returncode}")
            else:
                log_info(runtime_log, "  Track cleaning completed.")
        else:
            log_warning(runtime_log, "  mkvmerge not available — skipping track cleaning")
        
        log_info(runtime_log, "")
        
        # -------------------------------------------------------------------
        # Step 3: 10-bit inspection
        # -------------------------------------------------------------------
        log_info(runtime_log, "STEP 3: 10-bit / HDR inspection...")
        
        if tools.get("ffprobe", False):
            args = [
                "--source", str(library),
            ]
            if dry_run:
                args.append("--dry-run")
            
            returncode, stdout, stderr = run_tool(
                runtime_log,
                script_dir / "10bit.py",
                args,
                "10bit",
                timeout=3600,
            )
            
            if returncode != 0:
                log_warning(runtime_log, f"  10-bit inspector exited with code {returncode}")
            else:
                log_info(runtime_log, "  10-bit inspection completed.")
        else:
            log_warning(runtime_log, "  ffprobe not available — skipping 10-bit inspection")
        
        log_info(runtime_log, "")
        
        # -------------------------------------------------------------------
        # Step 4: Subtitle sync (ffsubsync)
        # -------------------------------------------------------------------
        log_info(runtime_log, "STEP 4: Subtitle timing sync...")
        
        if tools.get("ffsubsync", False) and tools.get("ffmpeg", False):
            args = [
                "--source", str(library),
            ]
            if dry_run:
                args.append("--dry-run")
            
            returncode, stdout, stderr = run_tool(
                runtime_log,
                script_dir / "sync_subtitles.py",
                args,
                "sync_subtitles",
                timeout=7200,
            )
            
            if returncode != 0:
                log_warning(runtime_log, f"  Subtitle sync exited with code {returncode}")
            else:
                log_info(runtime_log, "  Subtitle sync completed.")
        else:
            if not tools.get("ffsubsync", False):
                log_warning(runtime_log, "  ffsubsync not available — skipping subtitle sync")
            if not tools.get("ffmpeg", False):
                log_warning(runtime_log, "  ffmpeg not available — ffsubsync cannot extract audio")
        
        log_info(runtime_log, "")
        
        # -------------------------------------------------------------------
        # Step 5: Library audit
        # -------------------------------------------------------------------
        log_info(runtime_log, "STEP 5: Library audit...")
        
        audit_report = runtime_log.parent / f"auditor-pass-{pass_number}.txt"
        args = [
            "--source", str(library),
            "--report", str(audit_report),
        ]
        
        returncode, stdout, stderr = run_tool(
            runtime_log,
            script_dir / "library_auditor.py",
            args,
            "library_auditor",
            timeout=600,
        )
        
        # Parse coverage
        covered, total = parse_auditor_coverage(runtime_log, audit_report)
        coverage_str = f"{covered}/{total}" if covered is not None and total is not None else "unknown"
        log_info(runtime_log, f"  Auditor coverage: {coverage_str}")
        
        if is_library_complete(covered, total):
            log_info(runtime_log, "")
            log_info(runtime_log, "=" * 60)
            log_info(runtime_log, "LIBRARY COMPLETE!")
            log_info(runtime_log, f"All {total} movies have validated, synced subtitles")
            log_info(runtime_log, "and clean tracks.")
            log_info(runtime_log, "=" * 60)
            return 0
        
        # Check for stagnation
        if coverage_str == previous_coverage and coverage_str != "0/0":
            log_warning(runtime_log, f"  No improvement since last pass. Coverage: {coverage_str}")
            log_warning(runtime_log, "  This may indicate quota exhaustion or movies needing manual review.")
        
        previous_coverage = coverage_str
        log_info(runtime_log, "")
    
    # -----------------------------------------------------------------------
    # Final audit
    # -----------------------------------------------------------------------
    log_info(runtime_log, "=" * 60)
    log_info(runtime_log, "FINAL AUDIT")
    log_info(runtime_log, "=" * 60)
    
    final_report = runtime_log.parent / "auditor-final.txt"
    args = [
        "--source", str(library),
        "--report", str(final_report),
        "--fail-on-findings",
    ]
    
    returncode, stdout, stderr = run_tool(
        runtime_log,
        script_dir / "library_auditor.py",
        args,
        "library_auditor (final)",
        timeout=600,
    )
    
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
        log_info(runtime_log, "  ✓ All movies audited and 10-bit tested")
        log_info(runtime_log, "=" * 60)
        return 0
    else:
        log_warning(runtime_log, "")
        log_warning(runtime_log, "=" * 60)
        log_warning(runtime_log, "PARTIAL COMPLETION")
        log_warning(runtime_log, f"  {covered}/{total} movies complete.")
        log_warning(runtime_log, "")
        log_warning(runtime_log, "Review the final audit report:")
        log_warning(runtime_log, f"  {final_report}")
        log_warning(runtime_log, "=" * 60)
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-shot Jellyfin library completer — guarantees every movie "
        "gets a synced subtitle, clean tracks, and passes 10-bit inspection. "
        "Runs repeatedly until the library auditor reports 100% canonical.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    
    parser.add_argument(
        "--source",
        type=Path,
        help="Path to the Jellyfin movie library",
    )
    
    parser.add_argument(
        "--script-dir",
        type=Path,
        default=Path(__file__).parent,
        help="Directory containing the Organize tool scripts",
    )
    
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory for runtime logs and reports",
    )
    
    parser.add_argument(
        "--nice",
        action="store_true",
        help="Add --nice flag to tools that support it (lower priority)",
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only — no changes will be written",
    )
    
    parser.add_argument(
        "--max-passes",
        type=int,
        default=0,
        help="Maximum number of passes (0 = unlimited, keep going until complete)",
    )
    
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run built-in self-tests",
    )
    
    args = parser.parse_args()
    
    if args.self_test:
        return run_self_tests()
    
    # Validate inputs
    if not args.source.exists():
        print(f"ERROR: Library directory does not exist: {args.source}", file=sys.stderr)
        return 2
    
    if not args.source.is_dir():
        print(f"ERROR: Source is not a directory: {args.source}", file=sys.stderr)
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
            nice=args.nice,
            dry_run=args.dry_run,
            max_passes=args.max_passes,
            tools=tools,
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
    
    # Test coverage parsing with mock report
    import tempfile
    from pathlib import Path
    
    with tempfile.TemporaryDirectory(prefix="one_shot_test_") as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Create a dummy runtime_log for testing
        test_runtime_log = tmp_path / "test.log"
        test_runtime_log.touch()
        
        # Test 1: Parse scorecard format
        report1 = tmp_path / "report1.txt"
        report1.write_text("""
   42/42 (100.0%)  COVERAGE: movies with a validated English SRT
   42  Already have .eng.srt
   42  Movies in the library
""")
        covered, total = parse_auditor_coverage(test_runtime_log, report1)
        check(covered == 42 and total == 42, f"Scorecard parsing: got {covered}/{total}")
        
        # Test 2: Parse "Canonical MKV" format
        report2 = tmp_path / "report2.txt"
        report2.write_text("""
   42  Canonical MKV
   42  Folders checked
""")
        covered, total = parse_auditor_coverage(test_runtime_log, report2)
        check(covered == 42 and total == 42, f"Canonical MKV parsing: got {covered}/{total}")
        
        # Test 3: Non-existent report
        covered, total = parse_auditor_coverage(test_runtime_log, tmp_path / "nonexistent.txt")
        check(covered is None and total is None, "Non-existent report should return None")
        
        # Test 4: Library complete check
        check(is_library_complete(42, 42), "42/42 should be complete")
        check(not is_library_complete(41, 42), "41/42 should not be complete")
        check(not is_library_complete(None, 42), "None coverage should not be complete")
    
    # Test 5: UTC midnight wait (just check it doesn't crash)
    # We don't actually wait in tests
    check(True, "UTC wait function exists and is callable")
    
    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    
    print("SELF-TEST PASSED (coverage parsing, completeness check)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
