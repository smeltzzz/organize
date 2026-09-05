"""The toolchain: what the five steps are, and how to call them.

Everything about a step that both orchestrators need lives here exactly once:
the order, the script, the flags each tool spells differently, the binaries it
needs, the caches it reuses between passes, and the sentences the long-running
completer prints while it works.

It used to live twice. ``pipeline.py`` had a ``Step`` table and a set of
prerequisite checks; ``jellyfin_one_shot.py`` had a parallel ``StepPlan`` table
with its own copy of the script names, its own titles, its own binary
detection and its own skip reasons. The two had already drifted in a way that
cost real work: one-shot asked ``shutil.which("mkvmerge")`` while every other
caller asked the track cleaner's own resolver, so on a standard Windows
MKVToolNix install (which does not put itself on PATH) ``organize.py doctor``
printed a green tick and the completer silently skipped every remux.

The order is load-bearing and is documented at ``STEP_ORDER``.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# The tools are scripts next to the package, not modules inside it: they are
# launched as subprocesses so each keeps its own locks, logs and reports.
TOOLS_DIR = Path(__file__).resolve().parents[2]

# The fetcher's per-source scrape budget for a long unattended run. It matches
# subtitle_fetcher.py's own default; it is stated here because the completer
# passes it explicitly rather than relying on the tool's default staying put.
SCRAPING_DAILY_CAP = 20

# The canonical order. Index order is the execution order; do not reorder
# without re-reading the moviehash note in the module docstring.
STEP_ORDER = ("fetcher", "cleaner", "10bit", "sync", "auditor")

@dataclass(frozen=True)
class Step:
    """One toolchain step: what to run, how to call it, and what it is for.

    This is the *only* description of the toolchain in the repo. It used to be
    three: this table, a parallel ``STEP_PLANS`` in ``jellyfin_one_shot.py``
    with its own script names and titles, and that runner's own prerequisite
    checks — which had already drifted (one-shot asked ``shutil.which`` while
    everything else asked the tool that owns the binary, so a standard Windows
    MKVToolNix install made the completer silently skip every remux).

    The narrative fields exist because a five-tool run that lasts hours has to
    explain itself while it runs, not only in the report it leaves behind. The
    one-shot completer renders them; ``pipeline.py`` shows the short form.
    """

    key: str
    script: str
    title: str
    # The flag each tool uses for the movie-library root; they are not uniform.
    root_flag: str
    supports_dry_run: bool = True
    supports_limit: bool = True
    supports_nice: bool = False

    # --- long-run execution (the one-shot completer) ----------------------
    # A shorter title for a step banner printed once per pass, the per-tool
    # probe cache to reuse between passes, a dedicated log file when the tool
    # cannot share one, the timeout a single pass gets, and any constant flags.
    banner_title: str = ""
    cache_name: str = ""
    cache_flag: str = "--cache"
    log_name: str = ""
    timeout_seconds: float = 0.0
    console_tag: str = ""
    tool_name: str = ""       # how the runner names the subprocess in the log
    agent_name: str = ""      # "Track cleaner exited with code 2"
    activity: str = ""        # "track cleaning skipped: mkvmerge is not installed"
    extra_args: tuple[str, ...] = ()

    # --- narrative --------------------------------------------------------
    purpose: str = ""
    why_here: str = ""
    idle: str = ""

    @property
    def label(self) -> str:
        """The title to print in a step banner."""
        return self.banner_title or self.title

STEPS: dict[str, Step] = {
    "fetcher": Step(
        key="fetcher", script="subtitle_fetcher.py", title="Fetch English SRT subtitles",
        root_flag="--source",
        banner_title="Fetch subtitles",
        # Not the shared run log: this file is the fetcher's durable quota
        # ledger, which it parses back to meter the daily caps. Another tool's
        # lines in it would be read as quota reservations.
        log_name="subtitle_fetcher_ledger.log",
        timeout_seconds=3600.0,
        console_tag="fetch",
        tool_name="subtitle_fetcher",
        agent_name="Subtitle fetch",
        activity="subtitle fetching",
        extra_args=("--scrape-daily-cap", str(SCRAPING_DAILY_CAP),
                    "--allow-missing"),  # one movie without a match must not fail the run
        purpose="Put a validated English <movie>.eng.srt beside every movie that does not have one.",
        why_here="First, on purpose: it searches by the release's exact OpenSubtitles moviehash, and any remux would destroy that hash forever.",
        idle="Movies that already have a validated sidecar are counted and skipped without spending a provider request.",
    ),
    "cleaner": Step(
        key="cleaner", script="mkv_track_cleaner.py", title="Clean MKV tracks (remux)",
        root_flag="--dir", supports_nice=True,
        banner_title="Clean tracks (lossless remux)",
        cache_name="mkv_track_cleaner_probe_cache.json",
        timeout_seconds=7200.0,  # 2 hours per pass
        console_tag="clean",
        tool_name="mkv_track_cleaner",
        agent_name="Track cleaner",
        activity="track cleaning",
        purpose="Rebuild MKVs that still carry extra audio tracks or embedded subtitles: one best English audio, no embedded subs.",
        why_here="After fetching, because a remux rewrites the bytes the subtitle moviehash is computed from.",
        idle="Already-clean movies are answered from the metadata cache and skipped without re-reading the file.",
    ),
    "10bit": Step(
        key="10bit", script="bitdepth.py", title="Check 8-bit vs 10-bit / HDR",
        root_flag="--source",
        banner_title="Inspect 10-bit / HDR",
        cache_name="10bit_probe_cache.json",
        timeout_seconds=3600.0,
        console_tag="10bit",
        tool_name="10bit",
        agent_name="10-bit inspector",
        activity="10-bit inspection",
        purpose="Record whether each movie is 8-bit, 10-bit or HDR, so a client that cannot play it is flagged in advance.",
        why_here="After the remux, so it inspects the bytes Jellyfin will actually serve.",
        idle="Movies whose size and mtime are unchanged are answered from the probe cache.",
    ),
    "sync": Step(
        key="sync", script="sync_subtitles.py", title="Sync subtitle timing (ffsubsync)",
        root_flag="--source",
        banner_title="Sync subtitle timing (ffsubsync)",
        # Remembered verdicts live with the rest of the run's state, so a
        # one-shot run is self-contained under --log-dir.
        cache_name="sync_state.json",
        cache_flag="--sync-ledger",
        timeout_seconds=7200.0,
        console_tag="sync",
        tool_name="sync_subtitles",
        agent_name="Subtitle sync",
        activity="subtitle sync",
        purpose="Measure every sidecar against the movie's real audio and correct the timing when the drift is real and trustworthy.",
        why_here="Last of the content steps: it rewrites subtitle bytes only, so the audit that follows validates finished sidecars.",
        idle="Sidecars measured in sync on an earlier run are skipped while the subtitle and the movie are unchanged.",
    ),
    "auditor": Step(
        key="auditor", script="library_auditor.py", title="Audit library layout",
        root_flag="--source", supports_dry_run=False, supports_limit=False,
        banner_title="Audit the library",
        timeout_seconds=600.0,
        console_tag="audit",
        tool_name="library_auditor",
        agent_name="Audit",
        activity="the audit",
        purpose="Decide whether every movie folder is canonical: right layout, right file name, a validated English sidecar.",
        why_here="Last, because its verdict is the only thing that decides whether another pass is needed.",
        idle="Nothing to do - the audit is a read-only walk of the library.",
    ),
}

# The tool scripts, in the one correct order. Derived from the registry so a
# new step cannot be added to one and forgotten in the other.
TOOL_SCRIPTS: tuple[str, ...] = tuple(STEPS[key].script for key in STEP_ORDER)

# ---------------------------------------------------------------------------
# Prerequisites
#
# Every answer comes from the tool that actually has to run the binary, so no
# two callers can disagree about whether a machine is provisioned.
# ---------------------------------------------------------------------------

def api_key_present() -> bool:
    """A subtitle provider key from either source, env or config file."""
    try:
        import subtitle_fetcher as sf
    except Exception:
        return bool(str(os.environ.get("OPENSUBTITLES_API_KEY") or "").strip()
                    or str(os.environ.get("SUBDL_API_KEY") or "").strip())
    keys = (
        os.environ.get("OPENSUBTITLES_API_KEY"),
        sf.OPENSUBTITLES_API_KEY,
        os.environ.get("SUBDL_API_KEY"),
        sf.SUBDL_API_KEY,
    )
    return any(str(key or "").strip() for key in keys)


def mkvmerge_installed() -> bool:
    """Delegate to the track cleaner's resolver: PATH plus known install dirs."""
    try:
        import mkv_track_cleaner as tc
        tc.resolve_mkvmerge_path()
        return True
    except Exception:
        return shutil.which("mkvmerge") is not None


def ffprobe_installed() -> bool:
    """Delegate to the inspector's resolver (PATH plus known install dirs)."""
    try:
        import bitdepth
        return bitdepth.find_ffprobe() is not None
    except Exception:
        return shutil.which("ffprobe") is not None


def ffsubsync_installed() -> bool:
    """ffsubsync under any of its three interchangeable entry points.

    Checking only ``ffsubsync`` reports a working install (``ffs``, ``subsync``)
    as missing.
    """
    try:
        import sync_subtitles as ss
        return ss.find_ffsubsync() is not None
    except Exception:
        return any(shutil.which(name) for name in ("ffsubsync", "ffs", "subsync"))


def ffmpeg_installed() -> bool:
    return shutil.which("ffmpeg") is not None


def ffsubsync_ready() -> bool:
    """ffsubsync *and* the ffmpeg it shells out to: syncing needs both."""
    return ffsubsync_installed() and ffmpeg_installed()


PREREQUISITES: dict[str, tuple[Callable[[], bool], str]] = {
    "fetcher": (
        api_key_present,
        "no subtitle-provider key; set OPENSUBTITLES_API_KEY and/or SUBDL_API_KEY to enable fetching",
    ),
    "cleaner": (
        mkvmerge_installed,
        "mkvmerge (MKVToolNix) not found on PATH or in the standard install locations",
    ),
    "10bit": (
        ffprobe_installed,
        "ffprobe (FFmpeg) not found on PATH or in the standard install locations",
    ),
    "sync": (
        ffsubsync_ready,
        "ffsubsync not found on PATH (install it with `pip install ffsubsync`) or ffmpeg "
        "missing; ffsubsync needs both to sync subtitles",
    ),
}


def prerequisite_issue(step: Step, script_dir: Path | None = None) -> str | None:
    """Return a reason to skip ``step``, or ``None`` when it can run."""
    base = TOOLS_DIR if script_dir is None else script_dir
    script_path = base / step.script
    if not script_path.is_file():
        return f"{step.script} is missing from this directory"
    check, reason = PREREQUISITES.get(step.key, (lambda: True, ""))
    try:
        if not check():
            return reason
    except Exception:
        return reason or "prerequisite check failed"
    return None


def detect_tools() -> dict[str, bool]:
    """Which external binaries this machine has.

    ffmpeg is reported separately from ffsubsync even though syncing needs
    both, because the operator has to be told which one to install.
    """
    return {
        "mkvmerge": mkvmerge_installed(),
        "ffprobe": ffprobe_installed(),
        "ffsubsync": ffsubsync_installed(),
        "ffmpeg": ffmpeg_installed(),
    }


# Which entries of a ``detect_tools()`` map each step needs before it can run.
STEP_BINARIES: dict[str, tuple[str, ...]] = {
    "cleaner": ("mkvmerge",),
    "10bit": ("ffprobe",),
    "sync": ("ffsubsync", "ffmpeg"),
}


def step_skip_reason(key: str, tools: dict[str, bool]) -> str | None:
    """Why a step cannot run on this machine, or None when it can.

    Phrased for a person reading a run log, and derived from the same table the
    pass loop consults, so the plan printed at startup and the run itself can
    never disagree.
    """
    missing = [name for name in STEP_BINARIES.get(key, ()) if not tools.get(name, False)]
    if not missing:
        return None
    verb = "is" if len(missing) == 1 else "are"
    return f"{' and '.join(missing)} {verb} not installed"


def missing_tool_scripts(script_dir: Path) -> list[str]:
    """Names of the toolchain scripts that are absent from ``script_dir``."""
    return [name for name in TOOL_SCRIPTS if not (script_dir / name).is_file()]


# ---------------------------------------------------------------------------
# Calling a step
# ---------------------------------------------------------------------------

def build_step_args(
    step: Step,
    *,
    library: Path,
    report: Path,
    run_log: Path,
    log_dir: Path | None = None,
    dry_run: bool = False,
    nice: bool = False,
    extra: Sequence[str] = (),
) -> list[str]:
    """The argv (minus interpreter and script) for one step of a long run.

    One builder for all five tools, because the differences between them are
    data on the ``Step``, not code: which flag names the library root, whether
    the tool keeps a cache between passes, and whether it can share the run
    log. ``extra`` is for the caller's one-off flags, such as the final audit's
    ``--fail-on-findings``.
    """
    log = log_dir / step.log_name if (step.log_name and log_dir is not None) else run_log
    args = [
        step.root_flag, str(library),
        "--report", str(report),
        "--log", str(log),
    ]
    if step.cache_name and log_dir is not None:
        args += [step.cache_flag, str(log_dir / step.cache_name)]
    args += [*step.extra_args, *extra]
    if dry_run and step.supports_dry_run:
        args.append("--dry-run")
    if nice and step.supports_nice:
        args.append("--nice")
    return args
