"""The external-subtitle contract."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

EXTERNAL_SRT_MAX_BYTES = 4 * 1024 * 1024


EXTERNAL_SRT_CUE_RE = re.compile(
    r"(?m)^\s*\d+\s*\n\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}"
)


EXTERNAL_SRT_LANG = "eng"


EXTERNAL_SRT_SUFFIX = f".{EXTERNAL_SRT_LANG}.srt"  # ".eng.srt"


LEGACY_EXTERNAL_SRT_SUFFIX = ".en.srt"


COVERING_ENGLISH_SRT_SUFFIXES: tuple[str, ...] = (
    EXTERNAL_SRT_SUFFIX,
    f".{EXTERNAL_SRT_LANG}.sdh.srt",
)


EXTERNAL_SRT_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "cp1252")


def normalize_srt_newlines(text: str) -> str:
    """Collapse CRLF and bare CR to LF so the cue pattern handles one form."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode_srt_bytes(raw: bytes) -> str | None:
    """Decode subtitle bytes in the agreed order, or ``None`` if none applies.

    Callers that need a best-effort string anyway (the fetcher inspects a
    rejected download to explain why it was rejected) decode with
    ``errors="replace"`` themselves rather than widening this contract.
    """
    for encoding in EXTERNAL_SRT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def srt_looks_valid(text: str) -> bool:
    """True when ``text`` contains at least one well-formed SRT cue.

    A file that fails this is not a subtitle: it is an error page, a stub, or a
    truncated download, and must never be treated as covering a movie.
    """
    return bool(EXTERNAL_SRT_CUE_RE.search(text))


def validate_srt_sidecar(path: Path) -> tuple[bool, str]:
    """Conservatively decide whether ``path`` is a usable external SRT.

    Returns ``(True, "")`` only for a regular, non-symlink, non-empty,
    size-bounded file that decodes as text and contains at least one
    well-formed cue.  Everything else returns ``(False, reason)`` with a
    human-readable explanation suitable for a report line.

    This never writes, follows symlinks, or deletes anything.
    """
    try:
        file_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        return False, f"could not stat subtitle ({exc.strerror or exc})"
    if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
        return False, "not a regular file (symlink or special file)"
    if file_stat.st_size <= 0:
        return False, "subtitle file is empty"
    if file_stat.st_size > EXTERNAL_SRT_MAX_BYTES:
        return False, f"subtitle exceeds {EXTERNAL_SRT_MAX_BYTES // (1024 * 1024)} MiB safety limit"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return False, f"could not read subtitle ({exc.strerror or exc})"
    text = decode_srt_bytes(raw)
    if text is None:
        return False, "subtitle has an unsupported text encoding"
    if not srt_looks_valid(normalize_srt_newlines(text)):
        return False, "subtitle contains no valid SRT cue"
    return True, ""


def exact_external_english_srt_path(media_path: Path) -> Path:
    """Return the canonical ``<stem>.eng.srt`` path beside a movie file."""
    return media_path.with_name(f"{media_path.stem}{EXTERNAL_SRT_SUFFIX}")


def legacy_external_english_srt_path(media_path: Path) -> Path:
    """Return the pre-cutover ``<stem>.en.srt`` path beside a movie file."""
    return media_path.with_name(f"{media_path.stem}{LEGACY_EXTERNAL_SRT_SUFFIX}")


def promote_legacy_external_english_srt(media_path: Path) -> tuple[Path | None, str]:
    """Rename a validated legacy ``.en.srt`` to the canonical ``.eng.srt``.

    Returns ``(canonical_path, "")`` when the canonical sidecar already exists
    or was just created by renaming the legacy file.  Returns ``(None, reason)``
    when there is nothing to promote or the rename is unsafe (e.g. both names
    exist, legacy is invalid, or the destination is occupied by a non-file).

    Never overwrites an existing ``.eng.srt``.  Never follows symlinks.
    """
    canonical = exact_external_english_srt_path(media_path)
    legacy = legacy_external_english_srt_path(media_path)
    try:
        if canonical.exists() and not canonical.is_symlink() and canonical.is_file():
            return canonical, ""
        if canonical.exists() or canonical.is_symlink():
            return None, f"canonical sidecar path is occupied: {canonical.name}"
    except OSError as exc:
        return None, f"could not inspect canonical sidecar: {exc}"
    try:
        if not legacy.exists() or legacy.is_symlink() or not legacy.is_file():
            return None, "legacy .en.srt is absent"
    except OSError as exc:
        return None, f"could not inspect legacy sidecar: {exc}"
    ok, reason = validate_srt_sidecar(legacy)
    if not ok:
        return None, f"legacy .en.srt is unusable ({reason})"
    try:
        os.replace(str(legacy), str(canonical))
    except OSError as exc:
        return None, f"could not rename legacy .en.srt to .eng.srt: {exc}"
    return canonical, ""
