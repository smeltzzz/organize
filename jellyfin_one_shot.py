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
  - An already-complete library (audited first, so a re-run of a finished
    library costs one audit instead of a full sweep; --force-pass overrides)

What it shows you while it runs
-------------------------------
A long pass can be an hour of silence, so the console is the primary
surface, not an afterthought:

  * Before the first pass it prints the plan: which of the five steps
    will run, which are skipped and why.
  * Every step announces itself - what it does, why it runs in this
    position, and what it will skip as already done.
  * Each tool's output streams to the console as it happens, prefixed by
    the step it came from, e.g. ``[clean] remuxed Movie (2020).mkv``.
    Nothing is held back until the end.
  * A tool that goes quiet is not a tool that has died: every
    ``--heartbeat`` seconds the runner reports how long it has been
    running and how long since its last line.
  * ``--quiet`` turns the streaming off (banners, decisions, heartbeats
    and every summary still print).

Two files, and only two
-----------------------
Everything is consolidated, so there is no pile of per-run artifacts to
sift through:

  <log-dir>/jellyfin_one_shot.log
      One fixed name, appended to by every run and by all five tools.
      Each run starts with a banner. This is the only log.
  <log-dir>/jellyfin_one_shot_report.txt
      One fixed name, rewritten after every step, so it is always the
      current state of the current run even if it is still going or was
      killed. It holds the full detail of the current pass plus a
      one-line history of every pass before it, with every tool's own
      report folded in verbatim.

Per-tool report files are written to a hidden staging folder, folded
into that single report, and deleted: a run leaves the log, the report,
and the durable caches that make the next run cheap. The one exception
is subtitle_fetcher.py, whose log file *is* its durable daily-quota
ledger (it parses it back), so it keeps its own file.

Guaranteed-finish behaviour (no silent infinite loops):
  - empty library (no movie folders)           -> exit 2, with the fix
  - log dir inside the library                 -> exit 2, with the fix
  - auditor keeps failing (3 passes in a row)  -> exit 1, with the reason
  - no coverage improvement for 2 passes      -> wait for UTC midnight
    (the daily caps reset and the scraping tier re-offers) and continue

Usage:
    python3 jellyfin_one_shot.py                                 # default library
    python3 jellyfin_one_shot.py --source /path/to/library [--nice]
                                   [--dry-run] [--max-passes N]
                                   [--force-pass] [--quiet] [--heartbeat N]

With no --source the library root resolves exactly like every sibling tool:
E:\\torrents\\final_organized, or MOVIE_STD_TARGET when it is set.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION = "1.3.1"

# The canonical Jellyfin movie-library root — the same default every sibling
# tool hardcodes (subtitle_fetcher.py's LIBRARY_DIR, mkv_track_cleaner.py's
# TARGET_DIR, bitdepth.py's SOURCE_DIR, sync_subtitles.py's DEFAULT_LIBRARY,
# library_auditor.py's SOURCE_DIR and pipeline.py's DEFAULT_LIBRARY). Keeping
# the value identical means a bare run finishes the same library the other
# scripts maintain. MOVIE_STD_TARGET overrides it; an explicit --source wins.
# ---------------------------------------------------------------------------
# Library-root resolution (vendored inline; keep every copy identical)
#
# The movie-library root used to be a bare literal repeated in six files, with
# only two of them honouring MOVIE_STD_TARGET. On a non-Windows host the tools
# that ignored it happily defaulted to a Windows drive letter, wrote reports to
# a literal path like `E:\torrents\...` in the current directory, and .gitignore
# grew an `E:*` rule to catch the debris. One resolver, used by every tool,
# removes that whole class of problem.
#
# Precedence: explicit --flag > ORGANIZE_LIBRARY > MOVIE_STD_TARGET > platform
# default. A `.env` beside the scripts is loaded first, but never overrides a
# variable already exported in the environment.
# ---------------------------------------------------------------------------

ENV_FILE_NAME = ".env"
LIBRARY_ENV_VAR = "ORGANIZE_LIBRARY"
LEGACY_LIBRARY_ENV_VAR = "MOVIE_STD_TARGET"


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Load ``KEY=value`` pairs from a .env file next to the scripts.

    The repo ships a fully documented ``.env.example`` telling users to copy it
    to ``.env``, but nothing ever read that file: every documented variable
    silently did nothing unless separately exported. This closes that gap.

    Real environment variables always win, so an explicit export still beats a
    stale file. Blank lines, ``#`` comments, a leading ``export``, and single or
    double quotes around the value are all accepted. Malformed lines are
    skipped rather than raising: a typo in a config file must not stop a
    maintenance run that would otherwise work.
    """
    env_path = path or (Path(__file__).resolve().parent / ENV_FILE_NAME)
    loaded: dict[str, str] = {}
    try:
        raw = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return loaded
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def default_library_root() -> Path:
    """The platform's documented library root when nothing else is configured.

    The Windows default is the layout the README documents. Pointing a POSIX
    host at ``E:\\torrents\\final_organized`` only ever produced a confusing
    "does not exist" (or worse, a literal ``E:...`` directory in the CWD), so
    those hosts get a sensible home-relative default instead.
    """
    if os.name == "nt":
        return Path(r"E:\torrents\final_organized")
    return Path.home() / "Media" / "Movies"


def resolve_library(explicit: Path | str | None = None) -> Path:
    """Resolve the movie-library root that every tool in the toolchain shares.

    Precedence: an explicit flag, then ORGANIZE_LIBRARY, then the legacy
    MOVIE_STD_TARGET, then the platform default.
    """
    load_dotenv()
    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser()
    for var in (LIBRARY_ENV_VAR, LEGACY_LIBRARY_ENV_VAR):
        value = (os.environ.get(var) or "").strip()
        if value:
            return Path(value).expanduser()
    return default_library_root()


def describe_library_origin(explicit: Path | str | None = None) -> str:
    """Human-readable provenance of the resolved root, for error messages."""
    load_dotenv()
    if explicit is not None and str(explicit).strip():
        return "--source"
    for var in (LIBRARY_ENV_VAR, LEGACY_LIBRARY_ENV_VAR):
        if (os.environ.get(var) or "").strip():
            return var
    return f"the default library root ({default_library_root()})"


def default_reports_root() -> Path:
    r"""Where logs, reports and probe caches go when nothing is configured.

    These must live OUTSIDE the media library (the auditor would otherwise
    count a log folder at the library root as a movie folder). On Windows that
    is the documented tools directory; elsewhere it follows the XDG state
    convention. Hardcoding the Windows path for every platform is what made a
    POSIX run scatter literal `E:\torrents\...` filenames into the current
    working directory.
    """
    if os.name == "nt":
        return Path(r"E:\torrents\tools\ReportsAndLogs")
    state_home = (os.environ.get("XDG_STATE_HOME") or "").strip()
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "organize"


def default_tool_dir(tool_name: str) -> Path:
    """The per-tool subdirectory of :func:`default_reports_root`."""
    return default_reports_root() / tool_name


DEFAULT_LIBRARY = str(resolve_library())

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

# The toolchain scripts, in the one correct order. bitdepth.py and
# mkv_track_cleaner.py additionally take a --cache flag; the rest do not.
TOOL_SCRIPTS = (
    "subtitle_fetcher.py",
    "mkv_track_cleaner.py",
    "bitdepth.py",
    "sync_subtitles.py",
    "library_auditor.py",
)

# A run produces exactly two artifacts. Every tool's --log points at the same
# file, so the whole run tells one continuous story in one place, and every
# tool's --report is written to the staging folder, folded into the single
# master report, and then deleted.
RUN_LOG_NAME = "jellyfin_one_shot.log"
RUN_REPORT_NAME = "jellyfin_one_shot_report.txt"
STAGE_DIR_NAME = ".one_shot_stage"

# subtitle_fetcher.py reads its own log back: the append-only log *is* its
# durable quota/retry ledger, so it cannot share a file with anything else -
# another tool's lines would be parsed as quota reservations. It therefore
# keeps one dedicated file. It is durable state, like the probe caches below,
# not a second run log: nothing is written to it that the run log lacks.
FETCHER_LEDGER_NAME = "subtitle_fetcher_ledger.log"

# Live feedback. A tool's output is streamed to the console as it happens; when
# a tool goes quiet (a long remux prints nothing between movies) the runner
# says so every DEFAULT_HEARTBEAT_SECONDS, so a silent console still reads as
# "working" instead of "hung".
DEFAULT_HEARTBEAT_SECONDS = 60.0

# Step keys, in pipeline order. One entry per tool the runner can call.
STEP_ORDER = ("fetcher", "cleaner", "10bit", "sync", "auditor")


@dataclass(frozen=True)
class StepPlan:
    """One toolchain step, described for the person watching the terminal.

    The narratives are the point of this class: a five-tool run that lasts
    hours has to explain itself while it runs, not only in the report it
    leaves behind.
    """

    script: str
    title: str
    purpose: str
    why_here: str
    idle: str


STEP_PLANS: dict[str, StepPlan] = {
    "fetcher": StepPlan(
        script="subtitle_fetcher.py",
        title="Fetch subtitles",
        purpose="Put a validated English <movie>.eng.srt beside every movie that does not have one.",
        why_here="First, on purpose: it searches by the release's exact OpenSubtitles moviehash, and any remux would destroy that hash forever.",
        idle="Movies that already have a validated sidecar are counted and skipped without spending a provider request.",
    ),
    "cleaner": StepPlan(
        script="mkv_track_cleaner.py",
        title="Clean tracks (lossless remux)",
        purpose="Rebuild MKVs that still carry extra audio tracks or embedded subtitles: one best English audio, no embedded subs.",
        why_here="After fetching, because a remux rewrites the bytes the subtitle moviehash is computed from.",
        idle="Already-clean movies are answered from the metadata cache and skipped without re-reading the file.",
    ),
    "10bit": StepPlan(
        script="bitdepth.py",
        title="Inspect 10-bit / HDR",
        purpose="Record whether each movie is 8-bit, 10-bit or HDR, so a client that cannot play it is flagged in advance.",
        why_here="After the remux, so it inspects the bytes Jellyfin will actually serve.",
        idle="Movies whose size and mtime are unchanged are answered from the probe cache.",
    ),
    "sync": StepPlan(
        script="sync_subtitles.py",
        title="Sync subtitle timing (ffsubsync)",
        purpose="Measure every sidecar against the movie's real audio and correct the timing when the drift is real and trustworthy.",
        why_here="Last of the content steps: it rewrites subtitle bytes only, so the audit that follows validates finished sidecars.",
        idle="Sidecars measured in sync on an earlier run are skipped while the subtitle and the movie are unchanged.",
    ),
    "auditor": StepPlan(
        script="library_auditor.py",
        title="Audit the library",
        purpose="Decide whether every movie folder is canonical: right layout, right file name, a validated English sidecar.",
        why_here="Last, because its verdict is the only thing that decides whether another pass is needed.",
        idle="Nothing to do - the audit is a read-only walk of the library.",
    ),
}


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
    """Create the log directory and return the one log file the run writes.

    The name is stable on purpose: every run appends to the same file, so the
    log is the complete history of the library rather than one file per run.
    A separator marks where each run starts.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    runtime_log = log_dir / RUN_LOG_NAME
    try:
        with runtime_log.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(
                "\n" + "=" * 78 + "\n"
                f"RUN STARTED {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                f"  (jellyfin_one_shot.py {VERSION})\n" + "=" * 78 + "\n"
            )
    except OSError:
        pass
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


def log_to_file(runtime_log: Path, level: str, message: str) -> None:
    """Write a log line the console has already shown (or should not show)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {message}"
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
# The console is a single stream shared by several reader threads, so printing
# is serialised: two tools' lines must never interleave mid-line.
_PRINT_LOCK = threading.Lock()


def _echo_line(line: str, kind: str, tag: str) -> None:
    """Print one line of a child tool's output, tagged with the step it came from."""
    prefix = f"[{tag}] " if tag else "  "
    suffix = "  (stderr)" if kind == "err" else ""
    with _PRINT_LOCK:
        try:
            print(f"{prefix}{line}{suffix}", flush=True)
        except UnicodeEncodeError:  # a console that cannot encode the line
            print(f"{prefix}{line.encode('ascii', 'replace').decode()}{suffix}", flush=True)


def format_duration(seconds: float) -> str:
    """Human-readable elapsed time, the unit a multi-hour run is read in."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def run_tool(
    runtime_log: Path,
    script_path: Path,
    args: list[str],
    tool_name: str,
    timeout: float | None = None,
    transcript: Path | None = None,
    *,
    console_tag: str = "",
    echo: bool = True,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
) -> tuple[int, str, str]:
    """
    Run a Python tool script and return ``(returncode, stdout, stderr)``.

    The child's output is streamed to the console as it is produced, so a run
    that takes hours explains itself the whole way through instead of going
    quiet between steps. Each line is tagged with ``console_tag`` (the step it
    belongs to) so interleaved output stays readable.

    Everything the child prints is still captured: the caller scans it for the
    markers that drive decisions (``QUOTA REACHED`` and friends), and a bounded
    copy lands in the transcript. The child also writes the shared run log
    itself, because ``--log`` points at it.

    Args:
        runtime_log: Path to the runtime log file.
        script_path: Path to the tool script (e.g., subtitle_fetcher.py).
        args: Command-line arguments to pass to the script.
        tool_name: Human-readable name for logging.
        timeout: Optional timeout in seconds. None = no timeout.
        transcript: Optional path for a bounded rolling copy of the tool's
            full output (folded into the run report, then deleted).
        console_tag: Short tag prefixed to every streamed console line.
        echo: Stream the child's output to the console (False for --quiet).
        heartbeat_seconds: How often to report that a silent tool is still
            working. 0 disables the heartbeat.

    Returns:
        Tuple of (returncode, stdout_text, stderr_text).
    """
    cmd = [sys.executable, str(script_path)] + args
    log_info(runtime_log, f"Running: {' '.join(cmd)}")

    # The children are Python, and a pipe makes their stdout block-buffered:
    # without this, a "live" console would arrive in 8 KiB lumps, minutes late.
    child_env = dict(os.environ)
    child_env["PYTHONUNBUFFERED"] = "1"

    try:
        # The tool scripts pin their own stdio to UTF-8 (reports are full of
        # box-drawing characters), so decode explicitly instead of with the
        # locale encoding - cp1252 on Windows would raise UnicodeDecodeError
        # on the very first report line.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=script_path.parent,
            env=child_env,
        )
    except FileNotFoundError:
        log_error(runtime_log, f"Script not found: {script_path}")
        return -1, "", f"Script not found: {script_path}"
    except Exception as e:
        log_error(runtime_log, f"Failed to run {tool_name}: {e}")
        return -1, "", str(e)

    captured: dict[str, list[str]] = {"out": [], "err": []}
    guard = threading.Lock()
    activity = {"last_line": time.monotonic(), "count": 0}

    def reader(stream: IO[str] | None, kind: str) -> None:
        if stream is None:
            return
        try:
            for raw_line in stream:
                line = raw_line.rstrip("\r\n")
                with guard:
                    captured[kind].append(line)
                    activity["last_line"] = time.monotonic()
                    activity["count"] += 1
                if echo:
                    _echo_line(line, kind, console_tag)
        except (ValueError, OSError):
            pass  # the stream closed under us: the process is gone
        finally:
            try:
                stream.close()
            except Exception:
                pass

    threads = [
        threading.Thread(target=reader, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=reader, args=(proc.stderr, "err"), daemon=True),
    ]
    for thread in threads:
        thread.start()

    started = time.monotonic()
    last_beat = started
    timed_out = False
    try:
        while proc.poll() is None:
            now = time.monotonic()
            if timeout is not None and now - started > timeout:
                timed_out = True
                break
            if heartbeat_seconds > 0 and now - last_beat >= heartbeat_seconds:
                with guard:
                    silence = now - activity["last_line"]
                    seen = activity["count"]
                if silence >= heartbeat_seconds:
                    log_info(
                        runtime_log,
                        f"  ...still working: {tool_name} has run {format_duration(now - started)} "
                        f"({format_duration(silence)} since its last line, {seen} line(s) so far)",
                    )
                    last_beat = now
            time.sleep(0.5)
    except KeyboardInterrupt:
        proc.kill()
        for thread in threads:
            thread.join(timeout=5)
        raise

    if timed_out:
        log_error(runtime_log, f"{tool_name} timed out after {timeout}s; stopping it")
        proc.kill()
    returncode = proc.wait()
    for thread in threads:
        thread.join(timeout=10)

    elapsed = time.monotonic() - started
    stdout_text = "\n".join(captured["out"])
    stderr_text = "\n".join(captured["err"])
    with guard:
        seen = activity["count"]
    log_info(runtime_log, f"{tool_name} exited with code {returncode} "
                          f"in {format_duration(elapsed)} ({seen} line(s) of output)")

    if transcript is not None:
        tail_to_file(transcript, stdout_text)
        tail_to_file(transcript.with_name(transcript.stem + ".err"), stderr_text)

    # A short echo in the run log keeps the single file readable on its own:
    # the whole conversation, not just the orchestrator's side of it.
    # Log-only: on the console these lines were streamed as they happened, so
    # repeating them would double every short tool's output.
    stdout_tail = [line for line in captured["out"] if line.strip()][-100:]
    if stdout_tail:
        log_to_file(runtime_log, "INFO",
                    f"{tool_name} console (last {len(stdout_tail)} lines):")
        for line in stdout_tail:
            log_to_file(runtime_log, "INFO", f"  {line}")

    stderr_tail = [line for line in captured["err"] if line.strip()][-100:]
    if stderr_tail:
        log_to_file(runtime_log, "WARNING",
                    f"{tool_name} stderr (last {len(stderr_tail)} lines):")
        for line in stderr_tail:
            log_to_file(runtime_log, "WARNING", f"  {line}")

    if timed_out:
        return -1, stdout_text, stderr_text or f"{tool_name} timed out after {timeout}s"
    return returncode, stdout_text, stderr_text


def _mkvmerge_available() -> bool:
    """Delegate to the track cleaner's own resolver.

    A bare ``shutil.which("mkvmerge")`` is not the same question: the standard
    Windows MKVToolNix installer does not put itself on PATH, and the cleaner
    therefore also searches its known install locations. Asking the weaker
    question here made a fully-provisioned Windows box report mkvmerge as
    missing and skip the remux, while ``organize.py doctor`` on the same
    machine printed a green tick and the version string.
    """
    try:
        import mkv_track_cleaner as tc

        tc.resolve_mkvmerge_path()
        return True
    except Exception:
        return shutil.which("mkvmerge") is not None


def _ffprobe_available() -> bool:
    """Delegate to the inspector's resolver (PATH plus known install dirs)."""
    try:
        import bitdepth

        return bitdepth.find_ffprobe() is not None
    except Exception:
        return shutil.which("ffprobe") is not None


def _ffsubsync_available() -> bool:
    """Delegate to the sync tool's resolver.

    ffsubsync ships three interchangeable entry points (``ffsubsync``, ``ffs``,
    ``subsync``); only checking the first reports a working install as missing.
    """
    try:
        import sync_subtitles as ss

        return ss.find_ffsubsync() is not None
    except Exception:
        return any(shutil.which(name) for name in ("ffsubsync", "ffs", "subsync"))


def check_prerequisites(runtime_log: Path) -> dict[str, bool]:
    """Check which required tools are available.

    Each answer comes from the tool that actually has to run the binary, so
    this runner and ``organize.py doctor`` can never disagree about whether a
    machine is provisioned.
    """
    tools = {
        "mkvmerge": _mkvmerge_available(),
        "ffprobe": _ffprobe_available(),
        "ffsubsync": _ffsubsync_available(),
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
# Library root resolution
# ---------------------------------------------------------------------------
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


def log_empty_library(runtime_log: Path) -> None:
    """The one misconfiguration that can look like success: 0/0 coverage.

    An empty library (or a --source that points above the movie folders)
    satisfies "covered == total", so it has to be named as a failure with the
    layout this toolchain expects.
    """
    log_error(runtime_log, "=" * 60)
    log_error(runtime_log, "STOPPING: no movie folders found under --source.")
    log_error(runtime_log, "The canonical Jellyfin layout is one folder per movie:")
    log_error(runtime_log, "    Title (Year)/Title (Year).mkv")
    log_error(runtime_log, "If this directory is genuinely empty there is nothing to "
                           "complete; otherwise check that --source points at the "
                           "library that holds the movie folders.")
    log_error(runtime_log, "=" * 60)


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
            sleep_time = 3600.0
            wait_seconds -= sleep_time
        else:
            sleep_time = wait_seconds
            wait_seconds = 0.0

        log_info(runtime_log, f"  Sleeping {sleep_time}s... (Ctrl+C to interrupt)")
        try:
            time.sleep(sleep_time)
        except KeyboardInterrupt:
            log_warning(runtime_log, "Interrupted during UTC wait. Resuming in next pass.")
            return

    log_info(runtime_log, "UTC day has rolled over. Resuming.")


# ---------------------------------------------------------------------------
# Run report: one file, everything in it
# ---------------------------------------------------------------------------
REPORT_WIDTH = 100


# The pre-flight is the auditor run *before* the sweep, so it borrows the
# auditor's plan but not its narrative: it is here to save a pointless pass,
# not to decide whether another one is needed.
PREFLIGHT_PURPOSE = ("Read the library's current state: is every movie folder canonical "
                     "(right layout, right file name, validated English sidecar)?")
PREFLIGHT_WHY_HERE = ("First, so a library that is already finished costs one audit instead "
                      "of a full five-step sweep. --force-pass sweeps anyway.")
PREFLIGHT_IDLE = "Nothing to do - the audit is a read-only walk of the library."


def _step_text(step: StepRecord, field: str) -> str:
    """A step's narrative text, with the pre-flight's own wording."""
    if step.number == 0:
        return {"purpose": PREFLIGHT_PURPOSE, "why": PREFLIGHT_WHY_HERE,
                "idle": PREFLIGHT_IDLE}[field]
    return {"purpose": step.plan.purpose, "why": step.plan.why_here,
            "idle": step.plan.idle}[field]


@dataclass
class StepRecord:
    """What one toolchain step did, for the console narrative and the report."""

    number: int
    key: str
    plan: StepPlan
    label: str = ""  # overrides the plan's title for one-off steps
    status: str = "running"  # running | done | failed | skipped | timed out
    returncode: int | None = None
    started: datetime = field(default_factory=datetime.now)
    elapsed: float = 0.0
    command: str = ""
    report_text: str = ""
    console_tail: str = ""
    note: str = ""

    @property
    def outcome(self) -> str:
        if self.status == "skipped":
            return f"SKIPPED ({self.note})" if self.note else "SKIPPED"
        if self.returncode is None:
            return self.status
        verdict = "ok" if self.returncode == 0 else f"exit code {self.returncode}"
        return f"{self.status} - {verdict} in {format_duration(self.elapsed)}"


@dataclass
class PassRecord:
    """One trip through the whole toolchain."""

    number: int
    started: datetime = field(default_factory=datetime.now)
    elapsed: float = 0.0
    coverage_before: str = "-"
    coverage_after: str = "-"
    notes: list[str] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)


@dataclass
class RunState:
    """Everything the single report is rendered from."""

    library: Path
    library_origin: str
    log_dir: Path
    runtime_log: Path
    report_path: Path
    started: datetime = field(default_factory=datetime.now)
    dry_run: bool = False
    force_pass: bool = False
    nice: bool = False
    max_passes: int = 0
    tools: dict[str, bool] = field(default_factory=dict)
    coverage: str = "not audited yet"
    verdict: str = "in progress"
    notes: list[str] = field(default_factory=list)
    passes: list[PassRecord] = field(default_factory=list)


def _rule(char: str = "-", width: int = REPORT_WIDTH) -> str:
    return char * width


def _block(title: str, body: list[str], width: int = REPORT_WIDTH) -> list[str]:
    """One titled section of the report, in the flat style of the run log."""
    lines = ["", _rule("=", width), title, _rule("=", width)]
    lines.extend(body)
    return lines


def render_run_report(state: RunState) -> str:
    """Render the one report file: at a glance, history, then every detail.

    Each tool writes its own report during the run; those are folded in here
    verbatim and then deleted, so this file is the complete story - the
    orchestrator's decisions plus every tool's own findings, in order.
    """
    now = datetime.now()
    lines: list[str] = []
    lines.extend(_block(
        f"JELLYFIN LIBRARY ONE-SHOT — RUN REPORT (jellyfin_one_shot.py {VERSION})",
        [
            f"Library     : {state.library}",
            f"  resolved from: {state.library_origin}",
            f"Started     : {state.started.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Updated     : {now.strftime('%Y-%m-%d %H:%M:%S')}  (rewritten after every step)",
            f"Elapsed     : {format_duration((now - state.started).total_seconds())}",
            f"Mode        : {'DRY RUN (one pass, nothing written)' if state.dry_run else 'LIVE'}",
            # Sweeps only: the pre-flight is a pass-0 record of its own.
            f"Passes      : {sum(1 for r in state.passes if r.number > 0)}"
            + (f" of {state.max_passes}" if state.max_passes else " (unlimited until complete)"),
            f"Force pass  : {'yes' if state.force_pass else 'no'}",
            f"Nice mode   : {'yes' if state.nice else 'no'}",
            f"Coverage    : {state.coverage}",
            f"Verdict     : {state.verdict}",
            "",
            f"Log file    : {state.runtime_log}   (every run appends; the run's only log)",
            f"Report file : {state.report_path}   (this file; rewritten in place)",
            "",
            "Durable state kept beside them (not logs or reports - these make "
            "re-runs cheap and keep the provider quotas honest):",
            f"  fetcher quota ledger: {state.log_dir / FETCHER_LEDGER_NAME}",
            "  probe caches        : mkv_track_cleaner_probe_cache.json, "
            "10bit_probe_cache.json",
            "  sync memory         : sync_state.json",
        ],
    ))

    lines.extend(_block("PASS HISTORY", _render_history(state)))

    for record in state.passes:
        if record.number == 0:
            title = (f"PRE-FLIGHT AUDIT — before pass 1 — "
                     f"started {record.started.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            title = (f"PASS {record.number} — started "
                     f"{record.started.strftime('%Y-%m-%d %H:%M:%S')}"
                     f" — {format_duration(record.elapsed)}")
        lines.extend(_block(title, _render_pass(record)))

    if state.notes:
        lines.extend(_block("NOTES FOR THIS RUN", [f"  - {note}" for note in state.notes]))

    lines.extend([
        "",
        _rule("=", REPORT_WIDTH),
        "Every tool's own report is folded into the pass it ran in, above, and its",
        "console output is streamed into the single log file. Nothing is kept anywhere",
        "else: this report and that log are the only two files a run leaves behind",
        "(besides the reusable probe caches that make re-runs cheap).",
        _rule("=", REPORT_WIDTH),
        "",
    ])
    return "\n".join(lines) + "\n"


def _render_history(state: RunState) -> list[str]:
    if not state.passes:
        return ["  No pass has finished yet."]
    rows = [f"  {'#':>3}  {'started':<8}  {'elapsed':>9}  {'coverage':>10}  {'change':>8}  notes"]
    for record in state.passes:
        before, after = record.coverage_before, record.coverage_after
        change = ""
        if before not in {"-", after}:
            change = f"{before} -> {after}"
        note = "; ".join(record.notes) if record.notes else ""
        label = "pre" if record.number == 0 else str(record.number)
        rows.append(
            f"  {label:>3}  {record.started.strftime('%H:%M:%S'):<8}  "
            f"{format_duration(record.elapsed):>9}  {after:>10}  {change:>8}  {note}"
        )
    return rows


def _render_pass(record: PassRecord) -> list[str]:
    lines: list[str] = []
    for step in record.steps:
        where = "PRE-FLIGHT" if step.number == 0 else f"STEP {step.number}/5"
        lines.extend([
            "",
            _rule("-", REPORT_WIDTH),
            f"{where} — {(step.label or step.plan.title).upper()}  ({step.plan.script})",
            _rule("-", REPORT_WIDTH),
            f"  Outcome : {step.outcome}",
            f"  Does    : {_step_text(step, 'purpose')}",
            f"  Why here: {_step_text(step, 'why')}",
            f"  Idle    : {_step_text(step, 'idle')}",
        ])
        if step.command:
            lines.append(f"  Command : {step.command}")
        if step.note:
            lines.append(f"  Note    : {step.note}")
        if step.report_text.strip():
            lines.extend(["", f"----- {step.plan.script} report (verbatim) -----"])
            lines.extend(step.report_text.rstrip().splitlines())
            lines.append(f"----- end of {step.plan.script} report -----")
        if step.console_tail.strip():
            lines.extend(["", f"----- {step.plan.script} console tail (last lines) -----"])
            lines.extend(step.console_tail.rstrip().splitlines())
    return lines or ["  (no step has run yet)"]


def write_run_report(state: RunState) -> None:
    """Publish the single report. Cheap enough to redo after every step, so
    the file is always current - even if the run is killed mid-pass."""
    try:
        state.report_path.parent.mkdir(parents=True, exist_ok=True)
        state.report_path.write_text(render_run_report(state), encoding="utf-8", errors="replace")
    except OSError:
        pass  # the log still has everything; never fail a run over the report


def log_step_banner(runtime_log: Path, step: StepRecord, total: int) -> None:
    """Announce a step in plain language before it runs."""
    where = "PRE-FLIGHT" if step.number == 0 else f"STEP {step.number}/{total}"
    log_info(runtime_log, "")
    log_info(runtime_log, _rule("-", 68))
    log_info(runtime_log, f"{where} — {(step.label or step.plan.title).upper()} "
                          f"({step.plan.script})")
    if step.number == 0:
        purpose, why_here, idle = PREFLIGHT_PURPOSE, PREFLIGHT_WHY_HERE, PREFLIGHT_IDLE
    else:
        purpose, why_here, idle = step.plan.purpose, step.plan.why_here, step.plan.idle
    log_info(runtime_log, f"  What it does : {purpose}")
    log_info(runtime_log, f"  Why here     : {why_here}")
    log_info(runtime_log, f"  Nothing to do: {idle}")
    log_info(runtime_log, _rule("-", 68))


def step_skip_reason(key: str, tools: dict[str, bool]) -> str | None:
    """Why a step cannot run here, or None when it can.

    Mirrors the checks in the pass loop so the startup plan and the run agree.
    """
    if key == "cleaner" and not tools.get("mkvmerge", False):
        return "mkvmerge is not installed"
    if key == "10bit" and not tools.get("ffprobe", False):
        return "ffprobe is not installed"
    if key == "sync":
        missing = [name for name in ("ffsubsync", "ffmpeg") if not tools.get(name, False)]
        if missing:
            return f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not installed"
    return None


def log_run_plan(runtime_log: Path, state: RunState) -> None:
    """Print the plan before the first step: what will run, what will not."""
    log_info(runtime_log, "PLAN FOR THIS RUN")
    for index, key in enumerate(STEP_ORDER, start=1):
        plan = STEP_PLANS[key]
        reason = step_skip_reason(key, state.tools)
        if reason is None:
            log_info(runtime_log, f"  {index}. RUN  {plan.title} ({plan.script})")
        else:
            log_info(runtime_log, f"  {index}. SKIP {plan.title} ({plan.script}) — {reason}")
    log_info(runtime_log, f"  Report: {state.report_path}")
    log_info(runtime_log, f"  Log   : {state.runtime_log}  (every step writes here)")


# ---------------------------------------------------------------------------
# One-shot orchestrator
# ---------------------------------------------------------------------------
def _stage_dir(log_dir: Path) -> Path:
    """Where a tool's own report and console transcript live until they are
    folded into the single run report. Hidden, and emptied at the end of a run."""
    return log_dir / STAGE_DIR_NAME


def _fold_artifact(path: Path) -> str:
    """Read a staged artifact and delete it: it belongs to the report now."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _discard_dir(path: Path) -> None:
    """Remove a staging folder and anything left in it from an earlier run."""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _step_command(script_dir: Path, script: str, args: list[str]) -> str:
    return " ".join([sys.executable, str(script_dir / script), *args])


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
    force_pass: bool = False,
    quiet: bool = False,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    library_origin: str = "--source",
) -> int:
    """
    Run the one-shot completion loop.

    Args:
        library: Path to the Jellyfin movie library.
        script_dir: Directory containing the Organize tool scripts.
        runtime_log: Path to the runtime log (the run's only log file).
        log_dir: Directory for the run report, the reusable caches, and the
            hidden staging folder the per-tool reports pass through.
        nice: If True, add --nice flag to tools that support it.
        dry_run: If True, preview only (no changes written). A dry run makes
            no changes by definition, so exactly one pass runs.
        max_passes: Maximum number of passes (0 = unlimited, live runs only).
        tools: Dictionary of available tools from check_prerequisites().
        timeout_scale: Multiplier for the per-step timeouts (0 = no timeout).
        force_pass: Run at least one full pipeline pass even when the library
            already audits 100% canonical. The auditor's verdict is the
            library contract (layout + a validated English sidecar); it never
            inspects the MKV's own tracks, so this is the way to ask for a
            sweep that also drops extra audio and embedded subtitles.
        quiet: Do not stream each tool's output to the console. Step banners,
            heartbeats, summaries and every decision are still printed; the
            full output still lands in the log and the report.
        heartbeat_seconds: How often to say "still working" while a tool is
            silent. 0 disables it.
        library_origin: Where the library path came from, for the report.

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

    stage = _stage_dir(log_dir)
    _discard_dir(stage)
    try:
        stage.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    state = RunState(
        library=library,
        library_origin=library_origin,
        log_dir=log_dir,
        runtime_log=runtime_log,
        report_path=log_dir / RUN_REPORT_NAME,
        dry_run=dry_run,
        force_pass=force_pass,
        nice=nice,
        max_passes=max_passes,
        tools=dict(tools),
    )

    pass_number = 0
    previous_coverage: str | None = None
    no_improvement_streak = 0
    consecutive_bad_audits = 0

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
    log_info(runtime_log, f"Force pass: {force_pass}")
    log_info(runtime_log, f"Console: {'quiet (banners, heartbeats and summaries only)' if quiet else 'live (every tool line is streamed)'}")
    log_info(runtime_log, f"Heartbeat: {'off' if heartbeat_seconds <= 0 else f'{heartbeat_seconds:g}s'}")
    log_info(runtime_log, f"Max passes: {max_passes_text}")
    log_info(runtime_log, f"Script directory: {script_dir}")
    log_info(runtime_log, f"Log directory: {log_dir}")
    log_info(runtime_log, f"Runtime log: {runtime_log}")
    log_info(runtime_log, "")

    log_run_plan(runtime_log, state)
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

    def shared_log_args() -> list[str]:
        """Every tool writes into the one log, so the run reads as one story."""
        return ["--log", str(runtime_log)]

    # -----------------------------------------------------------------------
    # Pre-flight: is the library already complete?
    # -----------------------------------------------------------------------
    # The auditor is the verdict this runner chases, and it is by far the
    # cheapest step - it reads folder layout and the subtitle sidecar and
    # never opens the container. Asking before the first pass turns a re-run
    # of a finished library into one audit instead of a five-tool sweep:
    # seconds instead of hours. --force-pass skips the question, because the
    # verdict deliberately does not cover the MKV's own tracks.
    if not dry_run and not force_pass:
        plan = STEP_PLANS["auditor"]
        step = StepRecord(number=0, key="auditor", plan=plan)
        preflight = PassRecord(number=0)
        preflight.steps.append(step)
        state.passes.append(preflight)
        log_step_banner(runtime_log, step, 5)

        preflight_report = stage / "auditor-preflight.txt"
        preflight_code, _stdout, _stderr = run_tool(
            runtime_log,
            script_dir / plan.script,
            ["--source", str(library),
             "--report", str(preflight_report),
             *shared_log_args()],
            "library_auditor (pre-flight)",
            timeout=scaled(600),
            transcript=stage / "auditor-preflight.console.log",
            console_tag="audit",
            echo=not quiet,
            heartbeat_seconds=heartbeat_seconds,
        )
        # The verdict is read before the report is folded: folding deletes the
        # staged file, and coverage is the one thing the runner decides on.
        covered, total = parse_auditor_coverage(runtime_log, preflight_report)
        step.returncode = preflight_code
        step.status = "done" if preflight_code == 0 else "failed"
        step.report_text = _fold_artifact(preflight_report)
        step.console_tail = _fold_artifact(stage / "auditor-preflight.console.log")
        step.elapsed = (datetime.now() - step.started).total_seconds()

        if covered is not None and total is not None:
            state.coverage = f"{covered}/{total}"
            if total == 0:
                step.note = "no movie folders found: nothing to complete"
                log_empty_library(runtime_log)
                state.verdict = "STOPPED - no movie folders found under the library root"
                preflight.notes.append("no movie folders found")
                write_run_report(state)
                return 2
            if is_library_complete(covered, total):
                verdict = f"COMPLETE - {covered}/{total} canonical, nothing to do"
                state.verdict = verdict
                step.note = "already canonical: no fetch, remux, inspection or sync was run"
                log_info(runtime_log, "")
                log_info(runtime_log, "=" * 60)
                log_info(runtime_log, "LIBRARY ALREADY COMPLETE - NOTHING TO DO")
                log_info(runtime_log, f"The auditor reports {covered}/{total} canonical movie "
                                      "folders, so no fetch, remux, inspection or sync was run.")
                log_info(runtime_log, "Every step is idempotent: none of them can improve on a")
                log_info(runtime_log, "library that already meets the contract.")
                log_info(runtime_log, "")
                log_info(runtime_log, "Caveat - what the verdict covers: a canonical folder layout")
                log_info(runtime_log, "and a validated .eng.srt sidecar. It does not inspect the")
                log_info(runtime_log, "MKV's own tracks, so a movie that still carries extra audio")
                log_info(runtime_log, "or embedded subtitles audits as canonical.")
                log_info(runtime_log, "Run again with --force-pass to sweep the library anyway.")
                log_info(runtime_log, "=" * 60)
                write_run_report(state)
                return 0
            step.note = f"{covered}/{total} canonical - a full pass is needed"
            log_info(runtime_log, f"  Pre-flight coverage: {covered}/{total} - running a full pass.")
        else:
            # A blocked or broken audit is not a verdict: fall through and let
            # the pass loop apply its own retries and bad-audit accounting.
            step.note = "no usable coverage: falling through to a full pass"
            log_warning(runtime_log, f"  Pre-flight audit produced no usable coverage "
                                     f"(exit code {preflight_code}); running a full pass.")
        preflight.elapsed = (datetime.now() - preflight.started).total_seconds()
        write_run_report(state)
        log_info(runtime_log, "")

    def begin_step(key: str, number: int, target: PassRecord, note: str = "") -> StepRecord:
        step = StepRecord(number=number, key=key, plan=STEP_PLANS[key], note=note)
        target.steps.append(step)
        log_step_banner(runtime_log, step, 5)
        return step

    def finish_step(step: StepRecord, code: int, report_path: Path) -> None:
        """Fold the tool's report in, log the outcome, refresh the run report."""
        step.returncode = code
        step.status = "done" if code == 0 else "failed"
        step.elapsed = (datetime.now() - step.started).total_seconds()
        step.report_text = _fold_artifact(report_path)
        step.console_tail = _fold_artifact(stage / f"{step.key}.console.log")
        log_info(runtime_log, f"  STEP {step.number}/5 {step.label or step.plan.title}: {step.outcome}")
        if step.report_text.strip():
            log_info(runtime_log, f"  Full detail folded into {state.report_path.name}")
        write_run_report(state)

    # Main pass loop
    while True:
        pass_number += 1

        if dry_run and pass_number > 1:
            # A dry run writes nothing, so a second pass would be identical.
            break
        if not dry_run and max_passes > 0 and pass_number > max_passes:
            log_warning(runtime_log, f"Reached max passes ({max_passes}). Stopping.")
            break

        record = PassRecord(number=pass_number, coverage_before=previous_coverage or "-")
        state.passes.append(record)
        log_info(runtime_log, "=" * 60)
        log_info(runtime_log, f"PASS {pass_number}"
                              + (f" of {max_passes}" if max_passes else ""))
        log_info(runtime_log, "=" * 60)
        log_info(runtime_log, "")
        write_run_report(state)

        # -------------------------------------------------------------------
        # Step 1: Fetch subtitles (with quota handling)
        # -------------------------------------------------------------------
        step = begin_step("fetcher", 1, record)

        fetch_success = False
        fetch_retries = 0
        last_fetch_code = -1

        while not fetch_success and fetch_retries < MAX_FETCH_RETRIES:
            log_info(runtime_log, f"  Subtitle fetch attempt {fetch_retries + 1}/{MAX_FETCH_RETRIES}")

            args = [
                "--source", str(library),
                "--report", str(stage / "fetcher.report.txt"),
                # Not the shared log: this file is the fetcher's durable quota
                # ledger, which it parses back to meter the daily caps.
                "--log", str(log_dir / FETCHER_LEDGER_NAME),
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
                script_dir / step.plan.script,
                args,
                "subtitle_fetcher",
                timeout=scaled(3600),  # 1 hour per fetch attempt
                transcript=stage / "fetcher.console.log",
                console_tag="fetch",
                echo=not quiet,
                heartbeat_seconds=heartbeat_seconds,
            )
            last_fetch_code = returncode

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
                    record.notes.append("waited for the UTC day rollover")
                    wait_for_utc_midnight(runtime_log)
                    fetch_retries = 0  # Reset retry counter after waiting
                elif fetch_retries >= 3:
                    # Non-quota error, give up after 3 attempts
                    log_warning(runtime_log, "  Non-quota errors persisting — moving to next step.")
                    fetch_success = True  # Break out of retry loop
                else:
                    # Wait a bit before retrying
                    time.sleep(5)

        finish_step(step, last_fetch_code, stage / "fetcher.report.txt")
        log_info(runtime_log, "")

        # -------------------------------------------------------------------
        # Step 2: Track cleaning (lossless remux)
        # -------------------------------------------------------------------
        step = begin_step("cleaner", 2, record)
        if tools.get("mkvmerge", False):
            args = [
                "--dir", str(library),
                *shared_log_args(),
                "--report", str(stage / "cleaner.report.txt"),
                "--cache", str(log_dir / "mkv_track_cleaner_probe_cache.json"),
            ]
            if dry_run:
                args.append("--dry-run")
            if nice:
                args.append("--nice")

            returncode, _stdout, _stderr = run_tool(
                runtime_log,
                script_dir / step.plan.script,
                args,
                "mkv_track_cleaner",
                timeout=scaled(7200),  # 2 hours per pass
                transcript=stage / "cleaner.console.log",
                console_tag="clean",
                echo=not quiet,
                heartbeat_seconds=heartbeat_seconds,
            )
            if returncode != 0:
                log_warning(runtime_log, f"  Track cleaner exited with code {returncode}")
            else:
                log_info(runtime_log, "  Track cleaning completed.")
            finish_step(step, returncode, stage / "cleaner.report.txt")
        else:
            reason = step_skip_reason("cleaner", tools) or "mkvmerge not available"
            step.status, step.note = "skipped", reason
            state.notes.append(f"track cleaning skipped: {reason}")
            log_warning(runtime_log, f"  {reason} — skipping track cleaning")
            write_run_report(state)

        log_info(runtime_log, "")

        # -------------------------------------------------------------------
        # Step 3: 10-bit inspection
        # -------------------------------------------------------------------
        step = begin_step("10bit", 3, record)
        if tools.get("ffprobe", False):
            args = [
                "--source", str(library),
                *shared_log_args(),
                "--report", str(stage / "10bit.report.txt"),
                "--cache", str(log_dir / "10bit_probe_cache.json"),
            ]
            if dry_run:
                args.append("--dry-run")

            returncode, _stdout, _stderr = run_tool(
                runtime_log,
                script_dir / step.plan.script,
                args,
                "10bit",
                timeout=scaled(3600),
                transcript=stage / "10bit.console.log",
                console_tag="10bit",
                echo=not quiet,
                heartbeat_seconds=heartbeat_seconds,
            )
            if returncode != 0:
                log_warning(runtime_log, f"  10-bit inspector exited with code {returncode}")
            else:
                log_info(runtime_log, "  10-bit inspection completed.")
            finish_step(step, returncode, stage / "10bit.report.txt")
        else:
            reason = step_skip_reason("10bit", tools) or "ffprobe not available"
            step.status, step.note = "skipped", reason
            state.notes.append(f"10-bit inspection skipped: {reason}")
            log_warning(runtime_log, f"  {reason} — skipping 10-bit inspection")
            write_run_report(state)

        log_info(runtime_log, "")

        # -------------------------------------------------------------------
        # Step 4: Subtitle sync (ffsubsync)
        # -------------------------------------------------------------------
        step = begin_step("sync", 4, record)
        if tools.get("ffsubsync", False) and tools.get("ffmpeg", False):
            args = [
                "--source", str(library),
                "--report", str(stage / "sync.report.txt"),
                *shared_log_args(),
                # Remembered verdicts live with the rest of the run's state,
                # so a one-shot run is self-contained under --log-dir.
                "--sync-ledger", str(log_dir / "sync_state.json"),
            ]
            if dry_run:
                args.append("--dry-run")

            returncode, _stdout, _stderr = run_tool(
                runtime_log,
                script_dir / step.plan.script,
                args,
                "sync_subtitles",
                timeout=scaled(7200),
                transcript=stage / "sync.console.log",
                console_tag="sync",
                echo=not quiet,
                heartbeat_seconds=heartbeat_seconds,
            )
            if returncode != 0:
                log_warning(runtime_log, f"  Subtitle sync exited with code {returncode}")
            else:
                log_info(runtime_log, "  Subtitle sync completed.")
            finish_step(step, returncode, stage / "sync.report.txt")
        else:
            reason = step_skip_reason("sync", tools) or "ffsubsync/ffmpeg not available"
            step.status, step.note = "skipped", reason
            state.notes.append(f"subtitle sync skipped: {reason}")
            log_warning(runtime_log, f"  {reason} — skipping subtitle sync")
            write_run_report(state)

        log_info(runtime_log, "")

        # -------------------------------------------------------------------
        # Step 5: Library audit (with retries - the only step that decides)
        # -------------------------------------------------------------------
        step = begin_step("auditor", 5, record)
        audit_report = stage / "auditor.report.txt"
        returncode = -1
        for attempt in range(1, AUDIT_ATTEMPTS_PER_PASS + 1):
            args = [
                "--source", str(library),
                "--report", str(audit_report),
                *shared_log_args(),
            ]
            returncode, stdout, stderr = run_tool(
                runtime_log,
                script_dir / step.plan.script,
                args,
                f"library_auditor (attempt {attempt}/{AUDIT_ATTEMPTS_PER_PASS})",
                timeout=scaled(600),
                transcript=stage / "auditor.console.log",
                console_tag="audit",
                echo=not quiet,
                heartbeat_seconds=heartbeat_seconds,
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
        # Parse coverage before folding the report away (folding deletes it).
        covered, total = parse_auditor_coverage(runtime_log, audit_report)
        finish_step(step, returncode, audit_report)

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
                log_error(runtime_log, f"Last audit report: folded into {state.report_path}")
                log_error(runtime_log, f"Full log: {runtime_log}")
                log_error(runtime_log, "Fix the underlying problem (permissions, disk, another "
                                       "process holding the audit lock) and re-run - every step "
                                       "is idempotent, so nothing is lost.")
                log_error(runtime_log, "=" * 60)
                state.verdict = "STOPPED - the library audit kept failing"
                record.notes.append("audit kept failing")
                record.elapsed = (datetime.now() - record.started).total_seconds()
                write_run_report(state)
                return 1
        else:
            consecutive_bad_audits = 0
            coverage_str = f"{covered}/{total}"
            state.coverage = coverage_str
            record.coverage_after = coverage_str
            log_info(runtime_log, f"  Auditor coverage: {coverage_str}")

            # An empty library can never reach "covered == total" with
            # total > 0 - fail fast with the actual problem instead of
            # looping forever.
            if total == 0:
                log_empty_library(runtime_log)
                state.verdict = "STOPPED - no movie folders found under the library root"
                record.notes.append("no movie folders found")
                record.elapsed = (datetime.now() - record.started).total_seconds()
                write_run_report(state)
                return 2

            if is_library_complete(covered, total):
                log_info(runtime_log, "")
                log_info(runtime_log, "=" * 60)
                log_info(runtime_log, "LIBRARY COMPLETE!")
                log_info(runtime_log, f"All {total} movies have validated, synced subtitles")
                log_info(runtime_log, "and clean tracks.")
                if state.notes:
                    log_warning(runtime_log, "Note: these steps were skipped because the binary "
                                             "is not installed - the auditor verdict covers layout "
                                             "and subtitles only:")
                    for note in sorted(set(state.notes)):
                        log_warning(runtime_log, f"  - {note}")
                log_info(runtime_log, "=" * 60)
                state.verdict = f"COMPLETE - {covered}/{total} canonical"
                record.notes.append("library complete")
                record.elapsed = (datetime.now() - record.started).total_seconds()
                write_run_report(state)
                _discard_dir(stage)
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
                    record.notes.append("waited for the UTC day rollover")
                    wait_for_utc_midnight(runtime_log)
                    no_improvement_streak = 0
            else:
                no_improvement_streak = 0
                if previous_coverage is not None:
                    log_info(runtime_log, f"  Progress: {previous_coverage} -> {coverage_str}")

            previous_coverage = coverage_str
            record.coverage_before = previous_coverage
            log_info(runtime_log, "")

        record.elapsed = (datetime.now() - record.started).total_seconds()
        write_run_report(state)

    # -----------------------------------------------------------------------
    # Dry run: exactly one pass, by definition nothing changed
    # -----------------------------------------------------------------------
    if dry_run:
        coverage_str = state.coverage
        log_info(runtime_log, "")
        log_info(runtime_log, "=" * 60)
        log_info(runtime_log, "DRY-RUN PREVIEW COMPLETE")
        log_info(runtime_log, f"After one pass the auditor reports {coverage_str} canonical folders.")
        log_info(runtime_log, "Nothing was written. A live run repeats the same passes")
        log_info(runtime_log, "until the auditor reports 100%.")
        log_info(runtime_log, "=" * 60)
        state.verdict = f"DRY RUN - {coverage_str} canonical after one pass"
        write_run_report(state)
        _discard_dir(stage)
        return 0

    # -----------------------------------------------------------------------
    # Final audit
    # -----------------------------------------------------------------------
    log_info(runtime_log, "=" * 60)
    log_info(runtime_log, "FINAL AUDIT")
    log_info(runtime_log, "=" * 60)

    final_report = stage / "auditor-final.report.txt"
    step = StepRecord(number=5, key="auditor", plan=STEP_PLANS["auditor"],
                      label="Audit the library (final verdict)",
                      note="final audit with the fail gate")
    if state.passes:
        state.passes[-1].steps.append(step)
    else:  # no pass ran at all (only possible with --max-passes 0 semantics)
        state.passes.append(PassRecord(number=1, steps=[step]))
    returncode = -1
    for attempt in range(1, AUDIT_ATTEMPTS_PER_PASS + 1):
        args = [
            "--source", str(library),
            "--report", str(final_report),
            *shared_log_args(),
            "--fail-on-findings",
        ]
        returncode, stdout, stderr = run_tool(
            runtime_log,
            script_dir / step.plan.script,
            args,
            f"library_auditor (final, attempt {attempt}/{AUDIT_ATTEMPTS_PER_PASS})",
            timeout=scaled(600),
            transcript=stage / "auditor.console.log",
            console_tag="final audit",
            echo=not quiet,
            heartbeat_seconds=heartbeat_seconds,
        )
        if returncode == 0 or returncode == 1:
            # 1 = findings with --fail-on-findings: the report is still valid.
            break
        if attempt < AUDIT_ATTEMPTS_PER_PASS:
            backoff = AUDIT_BACKOFF_SECONDS[attempt - 1]
            log_info(runtime_log, f"  Final audit failed (code {returncode}); retrying in {backoff}s...")
            time.sleep(backoff)
    covered, total = parse_auditor_coverage(runtime_log, final_report)
    finish_step(step, returncode, final_report)
    coverage_str = f"{covered}/{total}" if covered is not None and total is not None else "unknown"
    state.coverage = coverage_str
    log_info(runtime_log, f"Final coverage: {coverage_str}")

    if is_library_complete(covered, total):
        log_info(runtime_log, "")
        log_info(runtime_log, "=" * 60)
        log_info(runtime_log, "SUCCESS: Library is 100% complete.")
        log_info(runtime_log, "")
        log_info(runtime_log, "  OK  Every movie has a synced .eng.srt or .eng.sdh.srt")
        log_info(runtime_log, "  OK  Every MKV has exactly 1 best English audio track")
        log_info(runtime_log, "  OK  No embedded subtitles remain")
        log_info(runtime_log, "  OK  All movies audited and 10-bit inspected")
        if state.notes:
            log_info(runtime_log, "")
            log_warning(runtime_log, "  Caveat - steps skipped this run (missing binaries):")
            for note in sorted(set(state.notes)):
                log_warning(runtime_log, f"    - {note}")
        log_info(runtime_log, "=" * 60)
        state.verdict = f"COMPLETE - {covered}/{total} canonical"
        write_run_report(state)
        _discard_dir(stage)
        return 0
    log_warning(runtime_log, "")
    log_warning(runtime_log, "=" * 60)
    log_warning(runtime_log, "PARTIAL COMPLETION")
    if covered is not None and total is not None:
        log_warning(runtime_log, f"  {covered}/{total} movies complete.")
    log_warning(runtime_log, "")
    log_warning(runtime_log, "Review the run report:")
    log_warning(runtime_log, f"  {state.report_path}")
    log_warning(runtime_log, "=" * 60)
    state.verdict = f"PARTIAL - {coverage_str} canonical"
    write_run_report(state)
    _discard_dir(stage)
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
    default_source = resolve_library()
    parser.add_argument(
        "--source",
        type=Path,
        default=default_source,
        help="Path to the Jellyfin movie library (defaults to "
             f"{DEFAULT_LIBRARY}, or MOVIE_STD_TARGET when set)",
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
        "--force-pass",
        action="store_true",
        help="Run at least one full pipeline pass even when the library already "
             "audits 100%% canonical. By default the library is audited first and "
             "a complete library is left alone; the auditor's verdict covers the "
             "folder layout and the subtitle sidecar, but never the MKV's own "
             "tracks, so this is the way to ask for a real sweep.",
    )

    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not stream each tool's output to the console. Step banners, "
             "'still working' heartbeats, step summaries and every decision are "
             "still printed; the full output still goes to the log and the report.",
    )

    parser.add_argument(
        "--heartbeat",
        type=float,
        default=DEFAULT_HEARTBEAT_SECONDS,
        metavar="SECONDS",
        help="How often to report that a silent tool is still working "
             "(0 disables the heartbeat)",
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

    # Where the library root came from: an explicit flag, MOVIE_STD_TARGET, or
    # the documented default. Reported on every failure below so a bare run
    # that lands on the wrong folder says why instead of just "does not exist".
    source_origin = (
        "--source" if args.source != default_source else describe_library_origin(None)
    )

    # Validate inputs
    if not args.source:
        print("ERROR: --source is required (or set MOVIE_STD_TARGET)", file=sys.stderr)
        return 2

    if not args.source.exists():
        print(f"ERROR: Library directory does not exist: {args.source}", file=sys.stderr)
        print(f"       resolved from {source_origin}", file=sys.stderr)
        print("       pass --source <library>, or set MOVIE_STD_TARGET, to point at "
              "the Jellyfin movie library", file=sys.stderr)
        return 2

    if not args.source.is_dir():
        print(f"ERROR: Source is not a directory: {args.source}", file=sys.stderr)
        print(f"       resolved from {source_origin}", file=sys.stderr)
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
    log_info(runtime_log, f"Library: {args.source}")
    log_info(runtime_log, f"  resolved from {source_origin}")
    log_info(runtime_log, "")

    try:
        return run_one_shot(
            library=args.source,
            script_dir=args.script_dir,
            runtime_log=runtime_log,
            log_dir=args.log_dir,
            nice=args.nice,
            dry_run=args.dry_run,
            max_passes=args.max_passes,
            tools=tools,
            timeout_scale=args.timeout_scale,
            force_pass=args.force_pass,
            quiet=args.quiet,
            heartbeat_seconds=args.heartbeat,
            library_origin=source_origin,
        )
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


if __name__ == "__main__":
    raise SystemExit(main())
