"""Atomic, durable filesystem primitives."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


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
            os.link(str(stage), str(dest))
            stage.unlink()
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
