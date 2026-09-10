"""The toolchain: what the five steps are, and how to call them.

Everything about a step that the pipeline needs lives here exactly once: the
order, the script, the flag each tool spells differently, the flags it
understands, and the binaries it needs before it can run.

It used to live twice - ``pipeline.py`` had a ``Step`` table and a set of
prerequisite checks, and a second runner (``jellyfin_one_shot.py``, removed
in 4.0.0) carried a parallel table of its own. The two had already drifted in
a way that cost real work: the other runner asked ``shutil.which("mkvmerge")``
while every other caller asked the track cleaner's own resolver, so on a
standard Windows MKVToolNix install (which does not put itself on PATH)
``organize.py doctor`` printed a green tick while the remux step was silently
skipped. The tables merged before the second runner was deleted, and this is
the one table that survived it.

The order is load-bearing and is documented at ``STEP_ORDER``.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# The tools are scripts next to the package, not modules inside it: they are
# launched as subprocesses so each keeps its own locks, logs and reports.
#
# In a checkout this is the directory holding them. In the zipapp build it is
# the archive itself - ``__file__`` is then ``.../organize.pyz/organizekit/
# core/toolchain.py`` - which is why nothing below joins a script name onto it
# without going through ``tool_command`` or ``tool_is_available``.
TOOLS_DIR = Path(__file__).resolve().parents[2]

# The hidden verb the zipapp's entry point answers to when the toolkit needs to
# run one of its own tools as a child process. Steps stay separate processes in
# every deployment: each tool keeps its own locks, logs, reports and exit code,
# and a crash in one cannot take the run down with it.
RUN_TOOL_VERB = "run-tool"

# The canonical order. Index order is the execution order; do not reorder
# without re-reading the pipeline notes in the module docstring and each
# tool's own documentation.
STEP_ORDER = ("extractor", "cleaner", "10bit", "sync", "auditor")

@dataclass(frozen=True)
class Step:
    """One toolchain step: what to run, how to call it, and what it needs.

    This is the *only* description of the toolchain in the repo. It used to be
    three: this table, a parallel ``STEP_PLANS`` table in a second runner with
    its own script names and titles, and that runner's own prerequisite
    checks - which had already drifted (it asked ``shutil.which`` while
    everything else asked the tool that owns the binary, so a standard Windows
    MKVToolNix install made it silently skip every remux). The second runner
    is gone; the one table remains, and ``pipeline.py`` binds it.
    """

    key: str
    script: str
    title: str
    # The flag each tool uses for the movie-library root; they are not uniform.
    root_flag: str
    supports_dry_run: bool = True
    supports_limit: bool = True
    supports_nice: bool = False

STEPS: dict[str, Step] = {
    "extractor": Step(
        key="extractor", script="subtitle_extractor.py",
        title="Extract embedded English SRT subtitles",
        root_flag="--source",
    ),
    "cleaner": Step(
        key="cleaner", script="mkv_track_cleaner.py",
        title="Clean MKV tracks (remux)",
        root_flag="--dir", supports_nice=True,
    ),
    "10bit": Step(
        key="10bit", script="bitdepth.py", title="Check 8-bit vs 10-bit / HDR",
        root_flag="--source",
    ),
    "sync": Step(
        key="sync", script="sync_subtitles.py", title="Sync subtitle timing (ffsubsync)",
        root_flag="--source",
    ),
    "auditor": Step(
        key="auditor", script="library_auditor.py", title="Audit library layout",
        root_flag="--source", supports_dry_run=False, supports_limit=False,
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

def mkvtoolnix_installed() -> bool:
    """mkvmerge AND mkvextract: the extractor drives both, the cleaner one."""
    try:
        import subtitle_extractor as sx
        return bool(sx.find_mkvtoolnix_binary("mkvmerge")
                    and sx.find_mkvtoolnix_binary("mkvextract"))
    except Exception:  # noqa: BLE001 - a sibling tool that will not import
        # must degrade to the plain PATH lookup, not take the caller down.
        return shutil.which("mkvmerge") is not None and shutil.which("mkvextract") is not None


def mkvmerge_installed() -> bool:
    """Delegate to the track cleaner's resolver: PATH plus known install dirs."""
    try:
        import mkv_track_cleaner as tc
        tc.resolve_mkvmerge_path()
        return True
    except Exception:  # noqa: BLE001 - a sibling tool that will not import
        # must degrade to the plain PATH lookup, not take the caller down.
        return shutil.which("mkvmerge") is not None


def mkvextract_installed() -> bool:
    """Delegate to the extractor's resolver: PATH plus known install dirs."""
    try:
        import subtitle_extractor as sx
        return sx.find_mkvtoolnix_binary("mkvextract") is not None
    except Exception:  # noqa: BLE001 - a sibling tool that will not import
        # must degrade to the plain PATH lookup, not take the caller down.
        return shutil.which("mkvextract") is not None


def ffprobe_installed() -> bool:
    """Delegate to the inspector's resolver (PATH plus known install dirs)."""
    try:
        import bitdepth
        return bitdepth.find_ffprobe() is not None
    except Exception:  # noqa: BLE001 - a sibling tool that will not import
        # must degrade to the plain PATH lookup, not take the caller down.
        return shutil.which("ffprobe") is not None


def ffsubsync_installed() -> bool:
    """ffsubsync under any of its three interchangeable entry points.

    Checking only ``ffsubsync`` reports a working install (``ffs``, ``subsync``)
    as missing.
    """
    try:
        import sync_subtitles as ss
        return ss.find_ffsubsync() is not None
    except Exception:  # noqa: BLE001 - a sibling tool that will not import
        # must degrade to the plain PATH lookup, not take the caller down.
        return any(shutil.which(name) for name in ("ffsubsync", "ffs", "subsync"))


def ffmpeg_installed() -> bool:
    return shutil.which("ffmpeg") is not None


def ffsubsync_ready() -> bool:
    """ffsubsync *and* the ffmpeg it shells out to: syncing needs both."""
    return ffsubsync_installed() and ffmpeg_installed()


PREREQUISITES: dict[str, tuple[Callable[[], bool], str]] = {
    "extractor": (
        mkvtoolnix_installed,
        "MKVToolNix (mkvmerge and mkvextract) not found on PATH or in the standard "
        "install locations; the extractor needs both",
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


def prerequisite_issue(step: Step) -> str | None:
    """Return a reason to skip ``step``, or ``None`` when it can run."""
    if not tool_is_available(step.script):
        return f"{step.script} is missing from this directory"
    check, reason = PREREQUISITES.get(step.key, (lambda: True, ""))
    try:
        if not check():
            return reason
    except Exception:  # noqa: BLE001 - check() is an arbitrary caller-supplied
        # probe; whatever it raises, the answer is "cannot run this step".
        return reason or "prerequisite check failed"
    return None


# ---------------------------------------------------------------------------
# Where the tools are, and how to start one
#
# There are two deployments and they answer these questions differently: a
# checkout, where each tool is a file you can point an interpreter at, and the
# single-file zipapp, where the same tools are modules inside an archive and
# the way to run one is to re-enter the archive. Everything that starts a tool
# goes through here so that difference is stated once.
# ---------------------------------------------------------------------------

def zipapp_path() -> Path | None:
    """The ``.pyz`` this toolkit is running from, or None in a checkout."""
    return TOOLS_DIR if TOOLS_DIR.is_file() else None


def tools_home() -> Path:
    """A real directory to run children in, in either deployment."""
    archive = zipapp_path()
    return archive.parent if archive is not None else TOOLS_DIR


def tool_module_name(script: str) -> str:
    """``bitdepth.py`` -> ``bitdepth``."""
    return script[:-3] if script.endswith(".py") else script


def child_cwd() -> Path:
    """A real working directory for a child tool, in either deployment."""
    return tools_home()


def tool_is_available(script: str) -> bool:
    """Can this deployment run ``script``?

    A missing tool is a skipped step, not a crash, so the pipeline and
    ``doctor`` ask this instead of testing for a file that only exists in one
    of the two layouts.
    """
    if zipapp_path() is not None:
        try:
            return importlib.util.find_spec(tool_module_name(script)) is not None
        except (ImportError, ValueError):  # a name that is not importable at all
            return False
    return (TOOLS_DIR / script).is_file()


def tool_command(script: str, args: Sequence[str] = ()) -> list[str]:
    """The full command that runs one tool as a child process.

    ``[interpreter, script, *args]`` out of a checkout; out of the zipapp,
    ``[interpreter, archive, "run-tool", script, *args]`` - the archive is the
    only file there is, so it re-enters itself and dispatches by module name.
    """
    archive = zipapp_path()
    if archive is not None:
        return [sys.executable, str(archive), RUN_TOOL_VERB, script, *args]
    return [sys.executable, str(TOOLS_DIR / script), *args]
