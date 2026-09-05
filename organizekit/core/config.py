"""Environment and path resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_FILE_NAME = ".env"


LIBRARY_ENV_VAR = "ORGANIZE_LIBRARY"


LEGACY_LIBRARY_ENV_VAR = "MOVIE_STD_TARGET"


def _dotenv_candidates() -> list[Path]:
    """Directories that may hold a ``.env``, in precedence order.

    The helpers used to live inside each tool, so ``Path(__file__).parent`` was
    the directory the user had put the scripts in. Now that they live in a
    package that may be installed anywhere, the file has to be looked up
    relative to what the *user* ran: the entry-point script first, then the
    working directory, then the installation root (which is the repository
    root for a clone). First readable file wins; a real environment variable
    still beats every one of them.
    """
    seen: list[Path] = []
    def add(directory: Path) -> None:
        candidate = directory / ENV_FILE_NAME
        if candidate not in seen:
            seen.append(candidate)

    script = (sys.argv[0] or "").strip()
    if script:
        try:
            add(Path(script).resolve().parent)
        except OSError:
            pass
    try:
        add(Path.cwd())
    except OSError:
        pass
    add(Path(__file__).resolve().parents[3])  # <repo>/src/organize/core -> <repo>
    return seen


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
    loaded: dict[str, str] = {}
    for env_path in ([Path(path)] if path is not None else _dotenv_candidates()):
        try:
            raw = env_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        break
    else:
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
