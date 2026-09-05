"""Probe payload cache keyed by (size, mtime)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .fsio import atomic_write_text, path_norm


class MediaProbeCache:
    """Best-effort ``(path, size, mtime) -> probe payload`` cache.

    ``bitdepth.py`` spawns one ``ffprobe`` per movie and ``mkv_track_cleaner.py``
    spawns one ``mkvmerge -J`` per movie, on every single run, even for a
    library that has not changed since the last sweep. Those subprocesses
    dominate the cost of a maintenance run.

    A probe is a pure function of a file's bytes, so a stored payload is reused
    only while both the size and ``st_mtime_ns`` are unchanged. Crucially, only
    the *probe output* is cached and never a tool's verdict: every consumer
    still re-derives its own decision from live filesystem state. A cached
    entry therefore cannot make a tool blind to a change it must react to — a
    sidecar appearing next to a movie, a hardlink count dropping when seeding
    stops, or a remux landing.

    Deliberately fail-open on reads and fail-silent on writes: a missing,
    unreadable, truncated, corrupt, foreign or stale cache is a miss rather
    than an error, and a cache that cannot be saved costs only the next run's
    speed. Nothing here can turn a correct run into an incorrect one.

    ``path_norm`` keys mean the two tools agree on identity the same way they
    already agree on lock keys.
    """

    SCHEMA = 1

    def __init__(
        self,
        path: Path | str,
        *,
        tool: str = "probe",
        enabled: bool = True,
        max_entries: int = 20000,
    ) -> None:
        self.path = Path(path)
        self.tool = tool
        self.enabled = bool(enabled)
        self.max_entries = max(1, int(max_entries))
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._dirty = False
        if self.enabled:
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        if raw.get("schema") != self.SCHEMA or raw.get("tool") != self.tool:
            # A different tool's cache or an older format: start clean rather
            # than guess at a layout we do not understand.
            return
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return
        self._entries = {
            str(key): value for key, value in entries.items() if isinstance(value, dict)
        }

    def get(self, file_path: Path | str, size: int, mtime_ns: int) -> dict[str, Any] | None:
        """Return a stored payload for an unchanged file, else ``None``."""
        if not self.enabled:
            self.misses += 1
            return None
        key = path_norm(file_path)
        with self._lock:
            entry = self._entries.get(key)
            if (
                entry is not None
                and entry.get("size") == int(size)
                and entry.get("mtime_ns") == int(mtime_ns)
            ):
                payload = entry.get("payload")
                if isinstance(payload, dict):
                    self.hits += 1
                    return payload
            self.misses += 1
            return None

    def put(self, file_path: Path | str, size: int, mtime_ns: int, payload: dict[str, Any]) -> None:
        """Store a probe payload, evicting oldest entries past ``max_entries``."""
        if not self.enabled:
            return
        key = path_norm(file_path)
        with self._lock:
            # Pop-then-insert refreshes recency: a plain dict preserves
            # insertion order but has no OrderedDict.move_to_end.
            self._entries.pop(key, None)
            self._entries[key] = {
                "size": int(size),
                "mtime_ns": int(mtime_ns),
                "payload": payload,
            }
            while len(self._entries) > self.max_entries:
                self._entries.pop(next(iter(self._entries)), None)
            self._dirty = True

    def save(self) -> None:
        """Persist the cache atomically. Failures are swallowed by design."""
        if not self.enabled or not self._dirty:
            return
        with self._lock:
            snapshot = dict(self._entries)
            self._dirty = False
        document = {"schema": self.SCHEMA, "tool": self.tool, "entries": snapshot}
        try:
            atomic_write_text(
                self.path,
                json.dumps(document, separators=(",", ":"), ensure_ascii=False) + "\n",
            )
        except OSError:
            pass

    def __len__(self) -> int:
        return len(self._entries)
