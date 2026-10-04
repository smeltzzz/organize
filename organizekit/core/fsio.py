"""Atomic, durable filesystem primitives."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

#: How old a tool's own staging debris must be before a later run may delete
#: it. Both tools that rewrite a movie (``mkv_track_cleaner.py`` and
#: ``audio_standardizer.py``) stage their output in a sibling temporary and
#: sweep whatever an interrupted run left behind on the next pass. The sweep
#: cannot tell "abandoned by a process that died" from "being written right
#: now" by name alone, and the two runs are not always mutually excluded: the
#: run lock is keyed by the library path, so a sweep over ``/movies`` and a
#: live transcode under ``/movies/4K`` hold different locks and overlap. Age
#: is the discriminator - a staging file this young is somebody's work in
#: flight, so it is left alone and the next run picks it up.
ORPHAN_MIN_AGE_SECONDS = 60.0


def orphan_is_abandoned(path: Path | str, *, now: float | None = None) -> bool:
    """True when a staging file is old enough to be debris, not work in flight.

    The one rule both sweeping tools follow, in one place: they used to differ,
    and the difference was that ``audio_standardizer.py`` deleted any temp
    carrying its marker name however fresh, so an overlapping sibling run lost
    the file ffmpeg was writing into and reported a failure for a movie that
    was perfectly healthy.

    A file that cannot be statted is **not** abandoned. "I could not read it"
    is not evidence that nobody owns it, and the safe answer to both is the
    same: leave it where it is.
    """
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return False
    return (time.time() if now is None else now) - mtime >= ORPHAN_MIN_AGE_SECONDS


def atomic_write_text(dest: Path, text: str, *, replace: bool = True) -> None:
    r"""Publish ``text`` to ``dest`` atomically and durably.

    Writes through a unique sibling file, ``fsync``\ s it, then publishes it
    with a single atomic operation, so a crash never leaves a truncated file
    and a reader always sees either the previous contents or the complete new
    ones. On failure the staged file is removed and the prior file is kept.

    The ``fsync`` is what makes this survive power loss rather than only a
    process crash: without it the rename can land while the bytes it points at
    are still only in the page cache, publishing an empty or partial file.
    ``newline="\n"`` keeps output byte-identical across platforms instead of
    silently gaining CRLFs on Windows.

    With ``replace=False`` the publish uses ``os.link``, an atomic
    create-if-absent, so an existing file is never clobbered. The subtitle
    fetcher needs this: a concurrent or hand-placed English sidecar must win
    over a download rather than be silently overwritten.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = dest.with_name(f".{dest.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp")
    try:
        with stage.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(str(stage), str(dest))
        else:
            try:
                os.link(str(stage), str(dest))
            except FileExistsError:
                # Destination already exists: the create-if-absent contract
                # says the existing file wins, so clean up the stage and
                # propagate the error for the caller to handle.
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            # Link succeeded: dest is now published. The stage is a second
            # name for the same inode; its removal is best-effort — a failure
            # here must not turn a successful publish into an error, it just
            # leaves a harmless duplicate that the next run's housekeeping
            # or the OS will clean up.
            try:
                stage.unlink()
            except OSError:
                pass
    except OSError:
        try:
            stage.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def path_norm(path: Path | str) -> str:
    """Normalize a path the same way every tool compares them.

    ``normcase`` lower-cases on Windows and is a no-op on POSIX; ``normpath``
    collapses ``..`` and duplicate separators.  Matching this exactly is what
    lets the standardizer, cleaner and subtitle fetcher agree on a lock key and
    on whether two paths are the same file.
    """
    return os.path.normcase(os.path.normpath(str(path)))


def path_is_within(candidate: Path, parent: Path) -> bool:
    """True when ``candidate`` is ``parent`` or a descendant after normalization.

    Uses ``resolve(strict=False)`` so it also works for paths that have not been
    created yet (e.g. the report/log files in a not-yet-existing output dir).
    """
    try:
        candidate.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
