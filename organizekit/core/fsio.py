"""Atomic, durable filesystem primitives."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


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


def source_snapshot(path: Path | str, stat_result: os.stat_result | None = None) -> dict[str, Any]:
    """A cheap identity snapshot, used to reject concurrent source changes.

    Both tools that replace a movie file — the remuxer and the audio
    standardizer — work for minutes or hours between reading a source and
    publishing over it. That window is not theoretical: the ingest hook
    (``movie_standardizer.py``) runs from the torrent client on completion, so
    a better release can land *beside* a sweep that is mid-encode, and
    ``os.replace`` would then destroy the freshly ingested movie and publish a
    transcode built from the bytes it replaced.

    The snapshot is deliberately size + ``st_mtime_ns`` + device + inode, i.e.
    everything a *replacement* changes, and deliberately NOT the hardlink
    count: linking a second name to a file changes none of them, so a seeding
    check has to be made separately (see ``hardlink_count`` in each tool).

    ``identity`` is a digest over the four fields so a journal or a verdict can
    carry one opaque value and a reader can still refuse a hand-edited one.
    """
    st = stat_result if stat_result is not None else os.stat(path)
    fields: dict[str, Any] = {
        "size": int(st.st_size),
        "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
        "device": int(getattr(st, "st_dev", 0)),
        "inode": int(getattr(st, "st_ino", 0)),
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fields["identity"] = hashlib.sha256(canonical).hexdigest()
    return fields


def source_snapshot_matches(path: Path | str, snapshot: dict[str, Any]) -> bool:
    """True only when ``path`` is still byte-for-byte the snapshotted file.

    An unreadable path answers False: "I cannot prove it is unchanged" must
    mean "refuse to replace it", never the other way round. Named fields are
    compared rather than the digest alone so a record missing one (an older
    journal, a truncated write) fails closed instead of comparing equal by
    both sides being empty.
    """
    try:
        observed = source_snapshot(path)
    except OSError:
        return False
    expected = dict(snapshot or {})
    for key in ("size", "mtime_ns", "device", "inode"):
        if key not in expected or expected.get(key) != observed.get(key):
            return False
    return bool(expected.get("identity") == observed.get("identity"))


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
