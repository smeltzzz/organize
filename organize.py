#!/usr/bin/env python3
"""
Organize — The Definitive Media Management Toolkit for Jellyfin
===============================================================
A unified CLI, system doctor, and orchestrator for the dependency-free
Python 3.11+ Jellyfin movie library toolkit.

Commands:
    doctor       Diagnose environment, external binaries, paths, and hardlink capability
    run          Run the automated maintenance pipeline (subtitles -> remux -> 10-bit -> audit)
    standardize  Rename and hardlink completed downloads into Title (Year)/Title (Year).mkv
    subtitles    Fetch validated English human UTF-8 SRT sidecars from OpenSubtitles
    clean        Lossless remux: keep single best English audio, strip commentary/DVS
    10bit        ffprobe inspection: queue 8-bit SDR for HandBrake; protect HDR & 10-bit
    audit        Read-only health check of library layout, MKV naming, and subtitle sidecars
    test         Run the test suite across all tools

Quickstart:
    python organize.py doctor           # Check if your system is ready
    python organize.py run --dry-run    # Preview the pipeline commands
    python organize.py run              # Run the full pipeline

Zero runtime dependencies. Standard library only.
"""

from __future__ import annotations

import argparse
import importlib
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

HERE = Path(__file__).resolve().parent

# Ensure the workspace/script directory is always on sys.path
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

VERSION = "3.1.0"

# ANSI styling helpers (with safe fallbacks)
_SUPPORTS_COLOR = (
    sys.stdout.isatty()
    and not os.environ.get("NO_COLOR")
    and os.environ.get("TERM") != "dumb"
)
_SUPPORTS_UNICODE = False
try:
    _encoding = (sys.stdout.encoding or "").casefold()
    _SUPPORTS_UNICODE = "utf-8" in _encoding or "utf8" in _encoding
except Exception:
    pass

# On Windows 10/11, enable VT mode for ANSI color support in cmd/powershell
if os.name == "nt" and _SUPPORTS_COLOR:
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


def _c(text: str, code: str) -> str:
    """Format text with ANSI color code if supported."""
    return f"\033[{code}m{text}\033[0m" if _SUPPORTS_COLOR else text


def bold(text: str) -> str:
    return _c(text, "1")


def dim(text: str) -> str:
    return _c(text, "2")


def cyan(text: str) -> str:
    return _c(text, "36")


def green(text: str) -> str:
    return _c(text, "32")


def yellow(text: str) -> str:
    return _c(text, "33")


def red(text: str) -> str:
    return _c(text, "31")


def blue(text: str) -> str:
    return _c(text, "34")


def magenta(text: str) -> str:
    return _c(text, "35")


# Symbols
SYM_OK = green("✔") if _SUPPORTS_UNICODE else green("[OK]")
SYM_WARN = yellow("⚠") if _SUPPORTS_UNICODE else yellow("[WARN]")
SYM_FAIL = red("✖") if _SUPPORTS_UNICODE else red("[FAIL]")
SYM_ARROW = cyan("➜") if _SUPPORTS_UNICODE else cyan("->")
SYM_BULLET = dim("•") if _SUPPORTS_UNICODE else dim("*")
SYM_STAR = yellow("★") if _SUPPORTS_UNICODE else yellow("*")

# Horizontal rule character. U+2500 (BOX DRAWINGS LIGHT HORIZONTAL) is not
# encodable in the cp1252/cp437 code pages that Windows defaults stdout to for
# non-interactive pipes, so printing it there raises UnicodeEncodeError (this
# broke `organize.py doctor` / `organize.py test` in CI on windows-latest).
# Fall back to ASCII when the output encoding cannot represent it.
HRULE = "─" if _SUPPORTS_UNICODE else "-"


# =============================================================================
# BANNER & DASHBOARD
# =============================================================================


def print_hero_banner() -> None:
    """Render a clean, high-impact terminal hero banner."""
    title = (
        "  ██████╗ ██████╗  ██████╗  █████╗ ███╗   ██╗██╗███████╗███████╗\n"
        " ██╔═══██╗██╔══██╗██╔════╝ ██╔══██╗████╗  ██║██║╚══███╔╝██╔════╝\n"
        " ██║   ██║██████╔╝██║  ███╗███████║██╔██╗ ██║██║  ███╔╝ █████╗  \n"
        " ██║   ██║██╔══██╗██║   ██║██╔══██║██║╚██╗██║██║ ███╔╝  ██╔══╝  \n"
        " ╚██████╔╝██║  ██║╚██████╔╝██║  ██║██║ ╚████║██║███████╗███████╗\n"
        "  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝╚══════╝╚══════╝"
    )
    if _SUPPORTS_UNICODE:
        print(cyan(title))
    else:
        print("=" * 72)
        print("  ORGANIZE — Jellyfin Movie Management Toolkit")
        print("=" * 72)

    subtitle = f"  ORGANIZE — The Definitive Media Management Toolkit for Jellyfin & Plex  {dim(f'v{VERSION}')}"
    print(subtitle)
    print(dim("  Zero third-party Python dependencies • Pure Python 3.11+ • Safe by default"))
    print()


def print_dashboard() -> None:
    """Print the interactive overview dashboard when run with no arguments."""
    print_hero_banner()

    print(bold("  WORKFLOW PIPELINE:"))
    print(f"    {cyan('1. standardize')} {SYM_ARROW} qBittorrent completion hook: hardlinks & names into Title (Year)")
    print(f"    {cyan('2. subtitles')}   {SYM_ARROW} OpenSubtitles OSHash fetcher: exact English UTF-8 SRT (pre-remux)")
    print(f"    {cyan('3. clean')}       {SYM_ARROW} MKVToolNix lossless remux: keeps 1 audio, drops commentary & bloat")
    print(f"    {cyan('4. 10bit')}       {SYM_ARROW} FFprobe inspection: queue 8-bit SDR for HandBrake, protect native HDR")
    print(f"    {cyan('5. audit')}       {SYM_ARROW} Read-only health check: verifies container, naming, and SRT health")
    print()

    print(bold("  QUICK COMMANDS:"))
    print(f"    {green('python organize.py doctor')}             Run comprehensive environment & prerequisite diagnostics")
    print(f"    {green('python organize.py run')}                Run manual maintenance pipeline (steps 2 -> 3 -> 4 -> 5)")
    print(f"    {green('python organize.py run --dry-run')}      Preview pipeline commands without executing")
    print(f"    {green('python organize.py standardize [PATH]')} Standardize a specific torrent download or batch scan")
    print(f"    {green('python organize.py audit')}              Audit current library layout and subtitle coverage")
    print(f"    {green('python organize.py test')}               Run built-in test suite (all self-tests + unit tests)")
    print()

    print(dim("  Type ") + bold("python organize.py <command> --help") + dim(" for specific command options."))
    print()


# =============================================================================
# DOCTOR — ENVIRONMENT & PREREQUISITE DIAGNOSTICS
# =============================================================================


@dataclass
class DiagnosticCheck:
    name: str
    status: str  # ok | warn | fail
    message: str
    detail: str = ""
    remedy: str = ""


def get_binary_version(binary_path: str, flag: str = "-version") -> str:
    """Attempt to extract version string from a binary."""
    try:
        proc = subprocess.run(
            [binary_path, flag],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        first_line = (proc.stdout or proc.stderr or "").strip().splitlines()
        return first_line[0] if first_line else ""
    except Exception:
        return ""


def run_doctor(library_path: Path | None = None, source_path: Path | None = None) -> int:
    """Run full system diagnostics and print a beautiful scorecard."""
    print_hero_banner()
    print(bold("  SYSTEM & PREREQUISITE DIAGNOSTICS (DOCTOR)"))
    print("  " + HRULE * 68)

    checks: list[DiagnosticCheck] = []

    # 1. Python runtime
    py_ver = sys.version_info
    py_ver_str = f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}"
    if py_ver >= (3, 11):
        checks.append(DiagnosticCheck(
            name="Python Runtime",
            status="ok",
            message=f"Python {py_ver_str} ({platform.python_implementation()} {platform.architecture()[0]})",
            detail=sys.executable,
        ))
    else:
        checks.append(DiagnosticCheck(
            name="Python Runtime",
            status="fail",
            message=f"Python {py_ver_str} is below required Python 3.11+",
            remedy="Upgrade to Python 3.11 or newer: https://www.python.org/downloads/",
        ))

    # 2. Operating System
    os_desc = f"{platform.system()} {platform.release()} ({platform.machine()})"
    checks.append(DiagnosticCheck(
        name="Operating System",
        status="ok",
        message=os_desc,
    ))

    # 3. MKVToolNix (mkvmerge) — needed by mkv_track_cleaner.py
    try:
        import mkv_track_cleaner as tc
        mkvmerge_bin = tc.resolve_mkvmerge_path()
        mkvmerge_ver = tc.get_mkvmerge_version(mkvmerge_bin)
        checks.append(DiagnosticCheck(
            name="MKVToolNix (mkvmerge)",
            status="ok",
            message=f"Found: {mkvmerge_ver or 'mkvmerge'}",
            detail=mkvmerge_bin,
        ))
    except Exception as exc:
        remedy_msg = (
            "Windows: winget install MKVToolNix.MKVToolNix or https://mkvtoolnix.download/\n"
            "Debian/Ubuntu: sudo apt install -y mkvtoolnix\n"
            "macOS: brew install mkvtoolnix"
        )
        checks.append(DiagnosticCheck(
            name="MKVToolNix (mkvmerge)",
            status="warn",
            message="Not found on PATH or standard install paths",
            detail="Track cleaner step will be skipped until mkvmerge is installed",
            remedy=remedy_msg,
        ))

    # 4. FFmpeg (ffprobe) — needed by 10bit.py and standardizer duplicate check
    try:
        probe_mod = importlib.import_module("10bit")
        ffprobe_bin = probe_mod.find_ffprobe()
        if ffprobe_bin and probe_mod.ffprobe_works(ffprobe_bin):
            ver_text = get_binary_version(ffprobe_bin, "-version")
            checks.append(DiagnosticCheck(
                name="FFmpeg (ffprobe)",
                status="ok",
                message=f"Found: {ver_text or 'ffprobe'}",
                detail=ffprobe_bin,
            ))
        else:
            raise FileNotFoundError("ffprobe not found or not working")
    except Exception:
        remedy_msg = (
            "Windows: winget install Gyan.FFmpeg or drop ffprobe.exe in C:\\ffmpeg\\bin\\\n"
            "Debian/Ubuntu: sudo apt install -y ffmpeg\n"
            "macOS: brew install ffmpeg"
        )
        checks.append(DiagnosticCheck(
            name="FFmpeg (ffprobe)",
            status="warn",
            message="Not found on PATH or standard install paths",
            detail="10-bit bit depth scanning and technical quality upgrades will be skipped",
            remedy=remedy_msg,
        ))

    # 5. OpenSubtitles API Configuration
    try:
        import subtitle_fetcher as sf
        api_key = os.environ.get("OPENSUBTITLES_API_KEY") or sf.OPENSUBTITLES_API_KEY
    except Exception:
        api_key = None
    if api_key:
        masked = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "***"
        checks.append(DiagnosticCheck(
            name="OpenSubtitles API Key",
            status="ok",
            message=f"Configured ({masked})",
            detail="Allows automated English SRT downloads using release moviehash",
        ))
    else:
        checks.append(DiagnosticCheck(
            name="OpenSubtitles API Key",
            status="warn",
            message="OPENSUBTITLES_API_KEY environment variable is not set",
            detail="Subtitle fetching step will be skipped until an API key is configured",
            remedy=(
                "Get a free consumer key at: https://www.opensubtitles.com/en/consumers\n"
                "Windows (PowerShell): [Environment]::SetEnvironmentVariable('OPENSUBTITLES_API_KEY', 'your-key', 'User')\n"
                "Linux/macOS (bash): export OPENSUBTITLES_API_KEY='your-key'"
            ),
        ))

    # 6. Library directories and Hardlink check
    lib_path = library_path or Path(os.environ.get("MOVIE_STD_TARGET", r"E:\torrents\final_organized"))
    src_path = source_path or Path(os.environ.get("MOVIE_STD_SOURCE", r"E:\torrents\final"))

    # Library directory check
    if lib_path.exists() and lib_path.is_dir():
        checks.append(DiagnosticCheck(
            name="Library Directory",
            status="ok",
            message=f"Accessible: {lib_path}",
        ))
    else:
        checks.append(DiagnosticCheck(
            name="Library Directory",
            status="warn",
            message=f"Path not found: {lib_path}",
            detail="This is the target folder where organized movies will be placed",
            remedy=f"Create it, set MOVIE_STD_TARGET, or pass --target: mkdir {lib_path}",
        ))

    # Source directory check
    if src_path.exists() and src_path.is_dir():
        checks.append(DiagnosticCheck(
            name="Download Source Dir",
            status="ok",
            message=f"Accessible: {src_path}",
        ))
    else:
        checks.append(DiagnosticCheck(
            name="Download Source Dir",
            status="warn",
            message=f"Path not found: {src_path}",
            detail="This is where qBittorrent saves completed downloads",
            remedy=f"Create it, set MOVIE_STD_SOURCE, or pass --source: mkdir {src_path}",
        ))

    # Hardlink filesystem compatibility check (crucial invariant)
    if lib_path.exists() and src_path.exists():
        try:
            lib_dev = lib_path.stat().st_dev
            src_dev = src_path.stat().st_dev
            if lib_dev == src_dev:
                checks.append(DiagnosticCheck(
                    name="Hardlink Compatibility",
                    status="ok",
                    message="Source and library share the same filesystem volume (device ID match)",
                    detail="Hardlinks (os.link) work with 0 extra disk space and safe uninterrupted seeding",
                ))
            else:
                checks.append(DiagnosticCheck(
                    name="Hardlink Compatibility",
                    status="fail",
                    message="Source and library are on DIFFERENT filesystems / drives!",
                    detail=f"Source dev={src_dev}, Target dev={lib_dev}. movie_standardizer requires same volume.",
                    remedy=(
                        "Hardlink-only placement cannot cross disk drives or separate filesystem mounts.\n"
                        "Configure qBittorrent downloads to reside on the same drive/volume as your library."
                    ),
                ))
        except Exception as exc:
            checks.append(DiagnosticCheck(
                name="Hardlink Compatibility",
                status="warn",
                message=f"Could not inspect filesystem devices: {exc}",
            ))

    # Display checks
    total_ok = sum(1 for c in checks if c.status == "ok")
    total_warn = sum(1 for c in checks if c.status == "warn")
    total_fail = sum(1 for c in checks if c.status == "fail")

    for check in checks:
        if check.status == "ok":
            symbol = SYM_OK
        elif check.status == "warn":
            symbol = SYM_WARN
        else:
            symbol = SYM_FAIL

        print(f"  {symbol} {bold(check.name):<28} {check.message}")
        if check.detail:
            print(f"      {dim(check.detail)}")
        if check.remedy:
            for r_line in check.remedy.splitlines():
                print(f"      {cyan('Fix:')} {r_line}")

    print("  " + HRULE * 68)
    summary_line = f"  Scorecard: {green(f'{total_ok} passed')}"
    if total_warn:
        summary_line += f", {yellow(f'{total_warn} warnings')}"
    if total_fail:
        summary_line += f", {red(f'{total_fail} failed')}"
    print(summary_line)

    if total_fail > 0:
        print(f"\n  {SYM_FAIL} {red('Action required:')} Fix the failed checks above before running automated tasks.")
        return 1
    elif total_warn > 0:
        print(f"\n  {SYM_WARN} {yellow('Ready with optional steps:')} Core tasks will work; optional steps (remux, subs, 10bit) will skip until prerequisites are added.")
        return 0
    else:
        print(f"\n  {SYM_OK} {green('All systems operational!')} This machine is fully provisioned for the complete Jellyfin media pipeline.")
        return 0


# =============================================================================
# SUBCOMMAND DELEGATIONS
# =============================================================================


def delegate_to_script(script_name: str, args: Sequence[str]) -> int:
    """Execute a standalone toolkit script via subprocess preserving arguments."""
    script_path = HERE / script_name
    if not script_path.is_file():
        print(f"Error: {script_name} not found at {script_path}", file=sys.stderr)
        return 2

    cmd = [sys.executable, str(script_path)] + list(args)
    try:
        proc = subprocess.run(cmd, cwd=str(HERE), check=False)
        return proc.returncode
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130
    except OSError as exc:
        print(f"Error launching {script_name}: {exc}", file=sys.stderr)
        return 2


def run_all_self_tests() -> int:
    """Run built-in self-tests across all scripts and return combined status."""
    print_hero_banner()
    print(bold("  RUNNING COMPREHENSIVE SELF-TEST SUITE"))
    print("  " + HRULE * 68)

    scripts = [
        ("organize.py", ["--internal-self-test"]),
        ("10bit.py", ["--self-test"]),
        ("library_auditor.py", ["--self-test"]),
        ("movie_standardizer.py", ["--self-test"]),
        ("subtitle_fetcher.py", ["--self-test"]),
        ("mkv_track_cleaner.py", ["--self-test"]),
        ("pipeline.py", ["--self-test"]),
    ]

    failed = 0
    started = time.monotonic()

    for script, test_args in scripts:
        script_path = HERE / script
        if not script_path.is_file():
            print(f"  {SYM_FAIL} {script:<24} Missing file!")
            failed += 1
            continue

        cmd = [sys.executable, str(script_path)] + test_args
        sub_start = time.monotonic()
        proc = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True, check=False)
        elapsed = time.monotonic() - sub_start

        if proc.returncode == 0:
            print(f"  {SYM_OK} {script:<24} {green('PASSED')} {dim(f'({elapsed:.2f}s)')}")
        else:
            print(f"  {SYM_FAIL} {script:<24} {red('FAILED')} (exit code {proc.returncode})")
            if proc.stdout:
                for line in proc.stdout.strip().splitlines()[-6:]:
                    print(f"      {dim(line)}")
            if proc.stderr:
                for line in proc.stderr.strip().splitlines()[-6:]:
                    print(f"      {red(line)}")
            failed += 1

    total_elapsed = time.monotonic() - started
    print("  " + HRULE * 68)
    if failed == 0:
        print(f"  {SYM_OK} {green('ALL SELF-TESTS PASSED')} {dim(f'in {total_elapsed:.2f}s')}\n")
        return 0
    else:
        print(f"  {SYM_FAIL} {red(f'{failed} SELF-TEST(S) FAILED')} {dim(f'in {total_elapsed:.2f}s')}\n")
        return 1


def run_unit_tests() -> int:
    """Run python3 -m unittest discover -s tests."""
    print(bold("  RUNNING REPOSITORY UNIT TESTS"))
    print("  " + HRULE * 68)
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
    proc = subprocess.run(cmd, cwd=str(HERE), check=False)
    return proc.returncode


# =============================================================================
# CLI PARSER & ENTRYPOINT
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="organize",
        description="Unified Jellyfin & Plex Media Management Toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python organize.py doctor                 # Check toolchain and filesystem setup\n"
            "  python organize.py run --dry-run          # Dry-run the pipeline\n"
            "  python organize.py run                    # Run full pipeline\n"
            "  python organize.py standardize '%F'       # qBittorrent post-torrent hook\n"
            "  python organize.py audit                  # Read-only health check\n"
            "  python organize.py test                   # Run all self-tests\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--self-test", action="store_true", help="Run self-tests and exit")
    parser.add_argument("--internal-self-test", action="store_true", help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # doctor
    p_doc = subparsers.add_parser("doctor", aliases=["check"], help="Diagnose environment, binaries, and hardlink compatibility")
    p_doc.add_argument("--source", type=Path, help="Override source download directory")
    p_doc.add_argument("--target", type=Path, help="Override target library directory")

    # run / pipeline
    subparsers.add_parser("run", aliases=["pipeline"], help="Run the automated maintenance pipeline", add_help=False)

    # standardize
    subparsers.add_parser("standardize", aliases=["std"], help="Rename & hardlink completed torrents into Title (Year)", add_help=False)

    # subtitles
    subparsers.add_parser("subtitles", aliases=["subs"], help="Fetch English human UTF-8 SRT sidecars from OpenSubtitles", add_help=False)

    # clean
    subparsers.add_parser("clean", aliases=["remux"], help="Lossless remux MKV: keep 1 best audio, strip commentary/DVS", add_help=False)

    # 10bit
    subparsers.add_parser("10bit", aliases=["probe"], help="FFprobe 8-bit vs 10-bit & native HDR compliance check", add_help=False)

    # audit
    subparsers.add_parser("audit", help="Read-only audit of library layout, naming, and SRT sidecars", add_help=False)

    # test
    p_test = subparsers.add_parser("test", aliases=["tests"], help="Run test suite (self-tests and/or unit tests)")
    p_test.add_argument("--unit", action="store_true", help="Run unit tests in addition to self-tests")

    return parser


def _reconfigure_stdio_for_windows() -> None:
    """Best-effort UTF-8 stdio so Unicode output never raises on Windows.

    Windows defaults non-interactive stdout/stderr to the ANSI code page
    (cp1252) or the OEM code page (cp437), neither of which can encode the
    box-drawing and symbol characters this CLI prints.  Reconfiguring to UTF-8
    with ``errors="replace"`` matches the sibling tools and guarantees the
    output path cannot crash on an unencodable character.
    """
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    _reconfigure_stdio_for_windows()
    raw_args = list(argv) if argv is not None else sys.argv[1:]

    # Handle internal self-test flag
    if "--internal-self-test" in raw_args:
        # Run verify of organize itself
        assert (HERE / "common.py").is_file(), "common.py missing"
        assert (HERE / "pipeline.py").is_file(), "pipeline.py missing"
        print("organize.py internal self-test: OK")
        return 0

    if not raw_args:
        print_dashboard()
        return 0

    # If first argument is --self-test, run test suite
    if raw_args[0] == "--self-test":
        return run_all_self_tests()

    # Parse primary command
    command = raw_args[0]
    sub_args = raw_args[1:]

    if command in {"doctor", "check"}:
        p = argparse.ArgumentParser(prog="organize doctor")
        p.add_argument("--source", type=Path, default=None)
        p.add_argument("--target", type=Path, default=None)
        parsed = p.parse_args(sub_args)
        return run_doctor(library_path=parsed.target, source_path=parsed.source)

    elif command in {"run", "pipeline"}:
        return delegate_to_script("pipeline.py", sub_args)

    elif command in {"standardize", "std"}:
        return delegate_to_script("movie_standardizer.py", sub_args)

    elif command in {"subtitles", "subs"}:
        return delegate_to_script("subtitle_fetcher.py", sub_args)

    elif command in {"clean", "remux"}:
        return delegate_to_script("mkv_track_cleaner.py", sub_args)

    elif command in {"10bit", "probe"}:
        return delegate_to_script("10bit.py", sub_args)

    elif command in {"audit"}:
        return delegate_to_script("library_auditor.py", sub_args)

    elif command in {"test", "tests"}:
        code = run_all_self_tests()
        if "--unit" in sub_args or "-u" in sub_args:
            code = code or run_unit_tests()
        return code

    elif command in {"-h", "--help", "help"}:
        parser = build_parser()
        parser.print_help()
        return 0

    elif command in {"-v", "--version"}:
        print(f"organize {VERSION}")
        return 0

    else:
        # Unknown command; show parser error
        parser = build_parser()
        parser.print_help()
        print(f"\n{red(f'Unknown command:')} {command}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
