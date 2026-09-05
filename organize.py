#!/usr/bin/env python3
"""
Organize — The Definitive Media Management Toolkit for Jellyfin
===============================================================
A unified CLI, system doctor, and orchestrator for the dependency-free
Python 3.11+ Jellyfin movie library toolkit.

Commands:
    doctor       Diagnose environment, external binaries, paths, and hardlink capability
    status       Summarise what is done and what the next pass will touch (read-only)
    run          Run the automated maintenance pipeline (subtitles -> remux -> 10-bit -> sync -> audit)
    standardize  Rename and hardlink completed downloads into Title (Year)/Title (Year).mkv
    subtitles    Fetch validated English human UTF-8 SRT sidecars (OpenSubtitles + SubDL)
    clean        Lossless remux: keep single best English audio, strip commentary/DVS
    10bit        ffprobe inspection: queue 8-bit SDR for HandBrake; protect HDR & 10-bit
    sync         ffsubsync timing sync of every .srt sidecar against its movie (pre-audit)
    audit        Read-only health check of library layout, MKV naming, and subtitle sidecars
    one-shot     Run the whole toolchain until the auditor reports 100% canonical
    test         Run the test suite across all tools

Quickstart:
    python organize.py doctor           # Check if your system is ready
    python organize.py status           # See what is left to do
    python organize.py run --dry-run    # Preview the pipeline commands
    python organize.py run              # Run the full pipeline

Zero runtime dependencies. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Ensure the workspace/script directory is always on sys.path
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from organizekit import VERSION  # noqa: E402  (needs the sys.path bootstrap above)

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
except (AttributeError, ValueError):
    pass  # a replaced stream with no .encoding, or a detached one

# On Windows 10/11, enable VT mode for ANSI color support in cmd/powershell
if os.name == "nt" and _SUPPORTS_COLOR:
    try:
        import ctypes
        windll = getattr(ctypes, "windll", None)
        if windll:
            windll.kernel32.SetConsoleMode(windll.kernel32.GetStdHandle(-11), 7)
    except Exception:  # noqa: BLE001 - ctypes reports a bad call as ArgumentError,
        # OSError or AttributeError depending on where it fails, and colour is
        # optional: a console that refuses VT mode simply does not get it.
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
    print(f"    {cyan('2. subtitles')}   {SYM_ARROW} OpenSubtitles + SubDL equal sources (SubDL release match scored ≥ 0.80): English UTF-8 SRT (pre-remux)")
    print(f"    {cyan('3. clean')}       {SYM_ARROW} MKVToolNix lossless remux: keeps 1 audio, drops commentary & bloat")
    print(f"    {cyan('4. 10bit')}       {SYM_ARROW} FFprobe inspection: queue 8-bit SDR for HandBrake, protect native HDR")
    print(f"    {cyan('5. sync')}        {SYM_ARROW} ffsubsync subtitle-timing sync: trust window applies, bad syncs held for review")
    print(f"    {cyan('6. audit')}       {SYM_ARROW} Read-only health check: verifies container, naming, and SRT health")
    print()

    print(bold("  QUICK COMMANDS:"))
    print(f"    {green('python organize.py doctor')}             Run comprehensive environment & prerequisite diagnostics")
    print(f"    {green('python organize.py status')}             Summarise progress: what is done, what the next pass touches")
    print(f"    {green('python organize.py run')}                Run manual maintenance pipeline (steps 2 -> 3 -> 4 -> 5 -> 6)")
    print(f"    {green('python organize.py run --dry-run')}      Preview pipeline commands without executing")
    print(f"    {green('python organize.py standardize [PATH]')} Standardize a specific torrent download or batch scan")
    print(f"    {green('python organize.py audit')}              Audit current library layout and subtitle coverage")
    print(f"    {green('python organize.py one-shot')}           Run every tool until the library is 100% canonical")
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
            # Children pin their own stdio to UTF-8, so decode it explicitly
            # rather than with the locale encoding (cp1252 on Windows).
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        first_line = (proc.stdout or proc.stderr or "").strip().splitlines()
        return first_line[0] if first_line else ""
    except (OSError, subprocess.SubprocessError, ValueError):
        # Missing, unrunnable, hung past the timeout, or answering with bytes
        # that are not text: the version string is cosmetic either way.
        return ""


def _resolve_library_path(explicit: Path | None) -> Path:
    """The library root doctor checks, resolved exactly like the tools do.

    ``doctor`` must never disagree with the tools about which folder is the
    library. Precedence is the repo-wide contract: an explicit flag, then
    ``ORGANIZE_LIBRARY``, then the legacy ``MOVIE_STD_TARGET``, then the
    platform default - and a ``.env`` next to the scripts is honoured too.
    Delegate to the tools' shared resolver; the inline fallback keeps a copied
    ``organize.py`` usable when its siblings are not present.
    """
    if explicit is not None:
        return explicit.expanduser()
    try:
        import bitdepth as probe_mod
        return probe_mod.resolve_library(None)
    except Exception:  # noqa: BLE001 - a sibling that will not import must not stop
        # the CLI; fall through to the environment and the documented default.
        pass
    for value in (os.environ.get("ORGANIZE_LIBRARY"), os.environ.get("MOVIE_STD_TARGET")):
        if value and value.strip():
            return Path(value).expanduser()
    if os.name == "nt":
        return Path(r"E:\torrents\final_organized")
    return Path.home() / "Media" / "Movies"


def _resolve_source_path(explicit: Path | None) -> Path:
    """The completed-download root doctor checks, resolved like the standardizer.

    Same contract as :func:`_resolve_library_path`, using ``MOVIE_STD_SOURCE``
    for the source directory and a platform-aware default so a POSIX machine
    never sees a literal ``E:\\torrents\\final`` warning.
    """
    if explicit is not None:
        return explicit.expanduser()
    try:
        import movie_standardizer as standardizer
        return standardizer.resolve_source_root(None)
    except Exception:  # noqa: BLE001 - as above: the CLI still has a default
        pass
    value = os.environ.get("MOVIE_STD_SOURCE")
    if value and value.strip():
        return Path(value).expanduser()
    if os.name == "nt":
        return Path(r"E:\torrents\final")
    return Path.home() / "torrents" / "final"


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
    except Exception:  # noqa: BLE001 - a doctor check reports what is wrong; a
        # probe that raises anything at all is the "not usable here" answer.
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

    # 4. FFmpeg (ffprobe) — needed by bitdepth.py and standardizer duplicate check
    try:
        import bitdepth as probe_mod
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
    except Exception:  # noqa: BLE001 - a doctor check reports what is wrong; a
        # probe that raises anything at all is the "not usable here" answer.
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

    # 4b. FFmpeg (ffmpeg) — needed by sync_subtitles.py (ffsubsync shells out to it)
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        ffmpeg_ver = get_binary_version(ffmpeg_bin, "-version")
        checks.append(DiagnosticCheck(
            name="FFmpeg (ffmpeg)",
            status="ok",
            message=f"Found: {ffmpeg_ver or 'ffmpeg'}",
            detail=ffmpeg_bin,
        ))
    else:
        checks.append(DiagnosticCheck(
            name="FFmpeg (ffmpeg)",
            status="warn",
            message="Not found on PATH",
            detail="ffsubsync needs ffmpeg to extract audio; the subtitle-sync step will be skipped",
            remedy=(
                "Windows: winget install Gyan.FFmpeg\n"
                "Debian/Ubuntu: sudo apt install -y ffmpeg\n"
                "macOS: brew install ffmpeg"
            ),
        ))

    # 4c. ffsubsync — needed by sync_subtitles.py (pip-installed program)
    try:
        import sync_subtitles as ss_sync
        ffsubsync_bin = ss_sync.find_ffsubsync()
    except Exception:  # noqa: BLE001 - a doctor check reports what is wrong; a
        # probe that raises anything at all is the "not usable here" answer.
        ffsubsync_bin = None
    if ffsubsync_bin:
        ffsubsync_ver = get_binary_version(ffsubsync_bin, "--version")
        checks.append(DiagnosticCheck(
            name="ffsubsync",
            status="ok",
            message=f"Found: {ffsubsync_ver or 'ffsubsync'}",
            detail=ffsubsync_bin,
        ))
    else:
        checks.append(DiagnosticCheck(
            name="ffsubsync",
            status="warn",
            message="Not found on PATH",
            detail="The subtitle-sync step is skipped until ffsubsync is installed",
            remedy=(
                "Install once:  pip install ffsubsync   (needs ffmpeg on the PATH)\n"
                "Alternative:   pipx install ffsubsync"
            ),
        ))

    # 4d. mkvextract — needed to extract the movie's own embedded subtitle tracks
    #     (mkvmerge is already reported above for the track cleaner)
    try:
        import subtitle_fetcher as sf_extract
        mkvmerge_bin = sf_extract.find_mkvtoolnix_binary("mkvmerge")
        mkvextract_bin = sf_extract.find_mkvtoolnix_binary("mkvextract")
    except Exception:  # noqa: BLE001 - a doctor check reports what is wrong; a
        # probe that raises anything at all is the "not usable here" answer.
        mkvmerge_bin = None
        mkvextract_bin = None
    if mkvmerge_bin and mkvextract_bin:
        checks.append(DiagnosticCheck(
            name="mkvextract (embedded subs)",
            status="ok",
            message=f"Found: {get_binary_version(mkvextract_bin, '--version') or 'mkvextract'}",
            detail=f"{mkvextract_bin} · mkvmerge {mkvmerge_bin}",
        ))
    else:
        checks.append(DiagnosticCheck(
            name="mkvextract (embedded subs)",
            status="warn",
            message="Not found on PATH",
            detail="subtitle_fetcher.py cannot read the movie's own embedded subtitle tracks, "
                   "so those movies are downloaded from the provider sources instead",
            remedy=(
                "Windows: winget install MoritzBunkus.MKVToolNix\n"
                "Debian/Ubuntu: sudo apt install -y mkvtoolnix\n"
                "macOS: brew install mkvtoolnix"
            ),
        ))

    # 4e. Image-subtitle OCR — optional, only for PGS/VobSub embedded tracks
    try:
        import subtitle_fetcher as sf_ocr
        ocr_backend, ocr_note = sf_ocr.detect_ocr_backend(sf_ocr.OCR_BACKEND_AUTO)
    except Exception as exc:  # noqa: BLE001 - a doctor check reports what is wrong; a
        # probe that raises anything at all is the "not usable here" answer.
        ocr_backend, ocr_note = None, f"subtitle_fetcher is unavailable ({exc})"
    if ocr_backend is not None:
        checks.append(DiagnosticCheck(
            name="OCR (image subtitles)",
            status="ok",
            message=f"Found: {ocr_backend.label}",
            detail="Embedded PGS/VobSub image tracks can be converted to SRT",
        ))
    else:
        checks.append(DiagnosticCheck(
            name="OCR (image subtitles)",
            status="warn",
            message="No OCR backend found",
            detail=(
                (f"{ocr_note}. " if ocr_note else "")
                + "Text tracks (SRT/SSA/ASS) are still extracted; image-only movies fall "
                  "through to the download sources"
            ),
            remedy=(
                "pgsrip: pip install pgsrip  (needs MKVToolNix, tesseract and tessdata)\n"
                "sup2srt + Tesseract: https://github.com/retrontology/sup2srt\n"
                "Subtitle Edit: https://www.nikse.dk/subtitleedit\n"
                "PgsToSrt: set PGSTOSRT_DLL to the dll path (needs dotnet)\n"
                "Or point subtitle_fetcher.py at your own tool: --ocr-backend custom "
                "--ocr-bin <program> --ocr-args \"{input}\" \"{output}\""
            ),
        ))

    # 5. Subtitle provider API configuration
    try:
        import subtitle_fetcher as sf
        opensubtitles_key = (os.environ.get("OPENSUBTITLES_API_KEY") or sf.OPENSUBTITLES_API_KEY).strip()
        subdl_key = (os.environ.get("SUBDL_API_KEY") or sf.SUBDL_API_KEY).strip()
    except Exception:  # noqa: BLE001 - a doctor check reports what is wrong; a
        # probe that raises anything at all is the "not usable here" answer.
        opensubtitles_key = None
        subdl_key = None

    def mask_key(value: str) -> str:
        return value[:4] + "..." + value[-4:] if len(value) > 8 else "***"

    if opensubtitles_key:
        checks.append(DiagnosticCheck(
            name="OpenSubtitles API Key",
            status="ok",
            message=f"Configured ({mask_key(opensubtitles_key)})",
            detail="Enables byte-identical OSHash subtitle matching (an equal source to SubDL)",
        ))
    if subdl_key:
        checks.append(DiagnosticCheck(
            name="SubDL API Key",
            status="ok",
            message=f"Configured ({mask_key(subdl_key)})",
            detail="Enables score-gated release-aware matching (score ≥ 0.80) as an equal source; can also run as the sole provider",
        ))
    if not opensubtitles_key and not subdl_key:
        checks.append(DiagnosticCheck(
            name="Subtitle Provider API Key",
            status="warn",
            message="Neither OPENSUBTITLES_API_KEY nor SUBDL_API_KEY is set",
            detail="Subtitle fetching will be skipped until at least one provider is configured",
            remedy=(
                "OpenSubtitles (recommended exact-match source): https://www.opensubtitles.com/en/consumers\n"
                "SubDL (score-gated release-aware fallback): https://subdl.com/panel/api\n"
                "Windows (PowerShell): [Environment]::SetEnvironmentVariable('OPENSUBTITLES_API_KEY', 'your-key', 'User')\n"
                "or: [Environment]::SetEnvironmentVariable('SUBDL_API_KEY', 'your-key', 'User')\n"
                "Linux/macOS (bash): export OPENSUBTITLES_API_KEY='your-key'  # or export SUBDL_API_KEY='your-key'"
            ),
        ))

    # 6. Library directories and Hardlink check
    lib_path = _resolve_library_path(library_path)
    src_path = _resolve_source_path(source_path)

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
            remedy=f"Create it, set ORGANIZE_LIBRARY, or pass --target: mkdir {lib_path}",
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
        except OSError as exc:
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
    if total_warn > 0:
        print(f"\n  {SYM_WARN} {yellow('Ready with optional steps:')} Core tasks will work; one or more optional pipeline steps will skip until prerequisites are added.")
        return 0
    print(f"\n  {SYM_OK} {green('All systems operational!')} This machine is fully provisioned for the complete Jellyfin media pipeline.")
    return 0


# =============================================================================
# STATUS
# =============================================================================

# What each step calls "settled". A movie is only counted as needing nothing
# when every step has an answer about *these exact bytes* and that answer is in
# the settled set; anything else - never measured, measured before the file
# changed, queued, failed, held for review - is work the next pass will do.
# The vocabularies are imported from the tools that own them rather than
# re-spelled here, so a renamed status can never silently stop matching.
SETTLED_LAYOUT = frozenset({"CANONICAL_MKV"})
SETTLED_SUBTITLE = frozenset({"present"})


@dataclass(frozen=True)
class StepStatus:
    """One step's answer for the whole library.

    ``stale`` and ``unmeasured`` are kept apart on purpose: "measured, then the
    file changed" and "never measured" both mean the next pass will do the
    work, but only the first says the cache is being kept honest.
    """

    label: str
    counts: dict[str, int]
    settled: int
    stale: int = 0
    unmeasured: int = 0

    @property
    def recorded(self) -> bool:
        """Has this step ever published a verdict about this library?"""
        return bool(self.counts) or self.stale > 0


@dataclass(frozen=True)
class LibraryStatus:
    library: Path
    movies: int
    total_bytes: int
    steps: tuple[StepStatus, ...]
    settled: int
    elapsed_sec: float = 0.0

    @property
    def pending(self) -> int:
        return max(0, self.movies - self.settled)


def human_bytes(size: int) -> str:
    """1 TiB as ``1.0 TiB``; the report style the tools already print."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def _tally(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def collect_status(audit, verdicts: dict, stamps: dict) -> LibraryStatus:
    """Join a live audit with the stored verdicts into one summary.

    ``audit`` is authoritative and fresh - it was just measured from the
    filesystem - so layout and subtitles are read straight from it. Bit depth
    and sync cost an ffprobe and an ffsubsync run respectively, so those come
    from the cache, and each stored answer is checked against the movie's
    current ``(size, mtime_ns)``: an answer about bytes that have since changed
    is reported as unknown, never as a verdict.

    A cached step with no usable answer anywhere in the library is treated as
    "not recorded yet" and left out of the settled tally rather than marking
    every movie pending. Otherwise a step that nobody has ever run - or that
    does not publish verdicts at all - would permanently report a library of
    zero finished movies, which is the sort of always-red number people learn
    to ignore. Which steps were left out is printed, so the figure is never
    quietly optimistic.
    """
    import bitdepth as probe_mod
    import sync_subtitles as sync_mod
    from organizekit.core import KIND_BITDEPTH, KIND_REMUX, KIND_SYNC, path_norm

    settled_verdicts: dict[str, frozenset[str] | None] = {
        KIND_REMUX: None,  # any current answer means the remux question is closed
        KIND_BITDEPTH: frozenset({probe_mod.STATUS_SKIP_SDR, probe_mod.STATUS_SKIP_HDR}),
        KIND_SYNC: frozenset({sync_mod.STATUS_SYNCED, sync_mod.STATUS_IN_SYNC,
                              sync_mod.STATUS_REMEMBERED}),
    }
    cached_kinds = tuple(settled_verdicts)

    layout: dict[str, int] = {}
    subtitle: dict[str, int] = {}
    cached: dict[str, dict[str, int]] = {kind: {} for kind in cached_kinds}
    stale: dict[str, int] = dict.fromkeys(cached_kinds, 0)
    unmeasured: dict[str, int] = dict.fromkeys(cached_kinds, 0)
    settled: dict[str, int] = {"layout": 0, "subtitle": 0, **dict.fromkeys(cached_kinds, 0)}
    per_movie: list[dict[str, bool]] = []

    movies = 0
    total_bytes = 0
    subtitle_states = _subtitle_states()

    for item in audit.folders:
        if len(item.movie_files) != 1:
            # No single feature file: a layout defect, and nothing to key a
            # per-movie verdict on. Counted in the layout line only.
            _tally(layout, item.state)
            continue
        movies += 1
        total_bytes += item.movie_files[0].size_bytes
        key = path_norm(item.folder / item.movie_files[0].name)
        size, mtime_ns = stamps.get(key, (None, None))

        _tally(layout, item.state)
        sub_state = subtitle_states.get(item.state)
        if sub_state is not None:
            _tally(subtitle, sub_state)

        step_ok = {
            "layout": item.state in SETTLED_LAYOUT,
            "subtitle": sub_state in SETTLED_SUBTITLE,
        }
        for kind in cached_kinds:
            stored = verdicts.get((key, kind))
            if stored is None:
                unmeasured[kind] += 1
                step_ok[kind] = False
                continue
            if not stored.is_current_for(size, mtime_ns):
                stale[kind] += 1
                step_ok[kind] = False
                continue
            _tally(cached[kind], stored.verdict)
            allowed = settled_verdicts[kind]
            step_ok[kind] = True if allowed is None else stored.verdict in allowed
        for name, ok in step_ok.items():
            settled[name] += int(ok)
        per_movie.append(step_ok)

    counted = {"layout", "subtitle"} | {
        kind for kind in cached_kinds if cached[kind] or stale[kind]
    }
    fully_settled = sum(
        1 for step_ok in per_movie if all(ok for name, ok in step_ok.items() if name in counted)
    )

    steps = (
        StepStatus("Layout", layout, settled["layout"]),
        StepStatus("Subtitles", subtitle, settled["subtitle"]),
        *(
            StepStatus(label, cached[kind], settled[kind], stale[kind], unmeasured[kind])
            for label, kind in (("Remux", KIND_REMUX), ("Bit depth", KIND_BITDEPTH),
                                ("Sync", KIND_SYNC))
        ),
    )
    return LibraryStatus(
        library=audit.source_dir, movies=movies, total_bytes=total_bytes,
        steps=steps, settled=fully_settled, elapsed_sec=getattr(audit, "elapsed_sec", 0.0),
    )


def _subtitle_states() -> dict[str, str]:
    """The auditor's own mapping from folder state to subtitle state."""
    try:
        import library_auditor
        return dict(library_auditor.SUBTITLE_STATE_FOR_AUDIT)
    except Exception:  # noqa: BLE001 - without the auditor there are no states to map
        return {}


def format_status(status: LibraryStatus) -> list[str]:
    """Render the summary as plain lines (no colour): what `status` prints."""
    lines = [
        f"{'Library':<10}{status.library}",
        f"{'':<10}{status.movies} movie(s), {human_bytes(status.total_bytes)}",
    ]
    for step in status.steps:
        if not step.recorded:
            lines.append(f"{step.label:<10}not recorded yet")
            continue
        parts = [f"{count} {name}" for name, count in
                 sorted(step.counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        if step.stale:
            parts.append(f"{step.stale} stale")
        if step.unmeasured:
            parts.append(f"{step.unmeasured} unmeasured")
        lines.append(f"{step.label:<10}" + "   ".join(parts))
    lines.append("")
    lines.append(
        f"Nothing to do for {status.settled} movie(s) - "
        f"the next pass will touch {status.pending}."
    )
    missing = [step.label for step in status.steps if not step.recorded]
    if missing:
        lines.append(
            f"({' and '.join(missing)} not counted: no cached verdicts to count.)"
        )
    return lines


def add_status_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Define `status`'s flags in one place, for both parsers that offer them."""
    parser.add_argument("--library", type=Path, default=None, metavar="PATH",
                        help="Library root to summarise (default: the one every tool resolves)")
    parser.add_argument("--state-db", type=Path, default=None, metavar="PATH",
                        help="Where the shared state cache lives (default: beside the logs)")
    parser.add_argument("--no-state", action="store_true",
                        help="Ignore the cache: show only the live layout and subtitle scan")
    parser.add_argument("--workers", type=int, default=0, metavar="N",
                        help="Folder scan workers (0 = decide from the CPU count, 1 = serial)")
    parser.add_argument("--verbose", action="store_true",
                        help="Also print the scan log the summary is built from")
    return parser


def run_status(
    library_path: Path | None = None,
    *,
    state_db: Path | None = None,
    use_state: bool = True,
    workers: int = 0,
    verbose: bool = False,
) -> int:
    """Answer "what is left to do?" with one live scan and the state cache.

    Layout and subtitles are re-measured here rather than read back from the
    cache: they are cheap (a stat per file) and they are the two things a user
    can change behind the toolkit's back by moving a file. The expensive
    verdicts - bit depth, sync, remux - are read from the cache and shown only
    while they still describe the bytes on disk.

    This runs the auditor itself rather than a second, subtly different scan,
    so it inherits exactly one side effect: a validated legacy ``Title.en.srt``
    is renamed to the canonical ``Title.eng.srt``, as ``organize audit`` does.
    No movie file is ever touched.
    """
    import io
    from contextlib import redirect_stdout

    import library_auditor
    from organizekit.core import open_state, path_norm

    library = _resolve_library_path(library_path)
    if not library.is_dir():
        print(f"{SYM_FAIL} {red('Library not found:')} {library}", file=sys.stderr)
        return 2

    cfg = library_auditor.Config(
        source_dir=library, workers=workers, use_state=use_state, state_db=state_db,
    )
    print(f"{SYM_ARROW} Scanning {cyan(str(library))} ...")
    scan_log = io.StringIO()
    started = time.perf_counter()
    try:
        # The audit narrates every non-canonical folder, which is the auditor's
        # job and not this summary's. Keep the log, print it only on --verbose
        # or if the scan fails, so `status` stays one screen.
        with redirect_stdout(scan_log):
            audit = library_auditor.audit_library(cfg)
    except Exception as exc:  # noqa: BLE001 - the scan is the whole command: any
        # failure is reported on stderr with the captured log, then exit 2.
        print(scan_log.getvalue(), end="")
        print(f"{SYM_FAIL} {red('Scan failed:')} {exc}", file=sys.stderr)
        return 2
    audit.elapsed_sec = time.perf_counter() - started
    if verbose:
        print(scan_log.getvalue(), end="")

    # Publishing here is the same write-through the auditor performs, reusing
    # its function so there is exactly one definition of what an audit means to
    # the cache - and it prunes rows for movies that have since been deleted.
    with redirect_stdout(scan_log):
        library_auditor.publish_state(audit, cfg)

    stamps: dict[str, tuple[int | None, int | None]] = {}
    for item in audit.folders:
        if len(item.movie_files) != 1:
            continue
        movie = item.folder / item.movie_files[0].name
        try:
            info = movie.stat()
            stamps[path_norm(movie)] = (info.st_size, info.st_mtime_ns)
        except OSError:
            stamps[path_norm(movie)] = (None, None)

    store = open_state(state_db, enabled=use_state, tool="organize status")
    try:
        verdicts = store.verdicts()
    finally:
        store.close()

    status = collect_status(audit, verdicts, stamps)
    print()
    for line in format_status(status):
        print(f"  {line}".rstrip())
    measured = any(step.recorded for step in status.steps[2:])
    if not store.enabled:
        print(f"\n  {SYM_WARN} {yellow('State cache disabled')} - only layout and subtitles are live.")
    elif not measured:
        print(f"\n  {SYM_BULLET} No cached verdicts yet: run {cyan('organize 10bit')} and "
              f"{cyan('organize sync')} to fill in the remaining rows.")
    print(f"  {dim(f'Scanned in {status.elapsed_sec:.2f}s. No movie file was modified.')}")
    return 0


# =============================================================================
# SUBCOMMAND DELEGATIONS
# =============================================================================


def delegate_to_script(script_name: str, args: Sequence[str]) -> int:
    """Execute a standalone toolkit script via subprocess preserving arguments.

    A step is always its own process - it keeps its own locks, log, report and
    exit code - and how one is started differs between a checkout and the
    single-file build, so the command comes from the toolchain rather than
    being assembled here.
    """
    from organizekit.core import tool_command, tool_is_available, tools_home

    if not tool_is_available(script_name):
        print(f"Error: {script_name} not found at {HERE}", file=sys.stderr)
        return 2

    cmd = tool_command(script_name, list(args))
    try:
        proc = subprocess.run(cmd, cwd=str(tools_home()), check=False)
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
        ("bitdepth.py", ["--self-test"]),
        ("library_auditor.py", ["--self-test"]),
        ("movie_standardizer.py", ["--self-test"]),
        ("subtitle_fetcher.py", ["--self-test"]),
        ("mkv_track_cleaner.py", ["--self-test"]),
        ("sync_subtitles.py", ["--self-test"]),
        ("pipeline.py", ["--self-test"]),
        ("jellyfin_one_shot.py", ["--self-test"]),
    ]

    failed = 0
    started = time.monotonic()

    from organizekit.core import tool_command, tool_is_available, tools_home

    for script, test_args in scripts:
        if not tool_is_available(script):
            print(f"  {SYM_FAIL} {script:<24} Missing file!")
            failed += 1
            continue

        cmd = tool_command(script, test_args)
        sub_start = time.monotonic()
        proc = subprocess.run(cmd, cwd=str(tools_home()), capture_output=True, check=False,
                              encoding="utf-8", errors="replace")
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
    print(f"  {SYM_FAIL} {red(f'{failed} SELF-TEST(S) FAILED')} {dim(f'in {total_elapsed:.2f}s')}\n")
    return 1


def run_unit_tests() -> int:
    """Run python3 -m unittest discover -s tests."""
    print(bold("  RUNNING REPOSITORY UNIT TESTS"))
    print("  " + HRULE * 68)
    from organizekit.core import tools_home, zipapp_path

    if zipapp_path() is not None:
        # The offline suite is developer equipment: it is not in the archive,
        # and shipping it there would mean shipping its fixtures too.
        print("  The unit test suite is not part of the single-file build.")
        print("  Clone the repository and run: python3 -m unittest discover -s tests")
        return 0
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
    proc = subprocess.run(cmd, cwd=str(tools_home()), check=False)
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
            "  python organize.py status                 # What is done and what is left\n"
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

    # status
    add_status_arguments(subparsers.add_parser(
        "status", help="Summarise library progress: what is done, what the next pass will touch"))

    # run / pipeline
    subparsers.add_parser("run", aliases=["pipeline"], help="Run the automated maintenance pipeline", add_help=False)

    # standardize
    subparsers.add_parser("standardize", aliases=["std"], help="Rename & hardlink completed torrents into Title (Year)", add_help=False)

    # subtitles
    subparsers.add_parser("subtitles", aliases=["subs"], help="Fetch English human UTF-8 SRT sidecars from OpenSubtitles + SubDL", add_help=False)

    # clean
    subparsers.add_parser("clean", aliases=["remux"], help="Lossless remux MKV: keep 1 best audio, strip commentary/DVS", add_help=False)

    # 10bit
    subparsers.add_parser("10bit", aliases=["probe"], help="FFprobe 8-bit vs 10-bit & native HDR compliance check", add_help=False)

    # sync
    subparsers.add_parser("sync", aliases=["sync-subtitles"], help="ffsubsync subtitle-timing sync of every .srt sidecar (pre-audit)", add_help=False)

    # audit
    subparsers.add_parser("audit", help="Read-only audit of library layout, naming, and SRT sidecars", add_help=False)

    # one-shot
    subparsers.add_parser("one-shot", aliases=["oneshot", "complete"],
                          help="Run the whole toolchain until the auditor reports 100%% canonical", add_help=False)

    # test
    p_test = subparsers.add_parser("test", aliases=["tests"], help="Run test suite (self-tests and/or unit tests)")
    p_test.add_argument("--unit", action="store_true", help="Run unit tests in addition to self-tests")

    return parser


def _reconfigure_stdio_for_windows() -> None:
    """Best-effort UTF-8 stdio so Unicode output never raises.

    Windows defaults non-interactive stdout/stderr to the ANSI code page
    (cp1252) or the OEM code page (cp437), and a macOS/Linux runner with no
    locale set can land on ASCII - none of which can encode the box-drawing
    and symbol characters this CLI and the tool reports print.  Reconfiguring
    to UTF-8 with ``errors="replace"`` on every platform matches the sibling
    tools and guarantees the output path cannot crash on an unencodable
    character.

    Kept inline so this entrypoint stays importable with nothing but the
    standard library (the tool scripts pin their own stdio the same way).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # closed or detached stream
            pass


def main(argv: Sequence[str] | None = None) -> int:
    _reconfigure_stdio_for_windows()
    raw_args = list(argv) if argv is not None else sys.argv[1:]

    # Handle internal self-test flag
    if "--internal-self-test" in raw_args:
        # Run verify of organize itself
        from organizekit.core import tool_is_available
        assert tool_is_available("pipeline.py"), "pipeline.py missing"
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

    if command == "status":
        # Same definition the top-level parser advertises, so `organize --help`
        # and `organize status --help` can never describe different flags.
        parsed = add_status_arguments(
            argparse.ArgumentParser(prog="organize status",
                                    description="What is done, and what the next pass will touch.")
        ).parse_args(sub_args)
        return run_status(
            parsed.library,
            state_db=parsed.state_db,
            use_state=not parsed.no_state,
            workers=int(parsed.workers),
            verbose=bool(parsed.verbose),
        )

    if command in {"run", "pipeline"}:
        return delegate_to_script("pipeline.py", sub_args)

    if command in {"standardize", "std"}:
        return delegate_to_script("movie_standardizer.py", sub_args)

    if command in {"subtitles", "subs"}:
        return delegate_to_script("subtitle_fetcher.py", sub_args)

    if command in {"clean", "remux"}:
        return delegate_to_script("mkv_track_cleaner.py", sub_args)

    if command in {"10bit", "probe"}:
        return delegate_to_script("bitdepth.py", sub_args)

    if command in {"sync", "sync-subtitles"}:
        return delegate_to_script("sync_subtitles.py", sub_args)

    if command in {"audit"}:
        return delegate_to_script("library_auditor.py", sub_args)

    if command in {"one-shot", "oneshot", "complete"}:
        return delegate_to_script("jellyfin_one_shot.py", sub_args)

    if command in {"test", "tests"}:
        code = run_all_self_tests()
        if "--unit" in sub_args or "-u" in sub_args:
            code = code or run_unit_tests()
        return code

    if command in {"-h", "--help", "help"}:
        parser = build_parser()
        parser.print_help()
        return 0

    if command in {"-v", "--version"}:
        print(f"organize {VERSION}")
        return 0

    # Unknown command; show parser error
    parser = build_parser()
    parser.print_help()
    print(f"\n{red('Unknown command:')} {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
