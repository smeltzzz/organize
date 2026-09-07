"""Probe payload cache keyed by (size, mtime), stored in ``state.db``."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .fsio import atomic_write_text, path_norm
from .state import PROBE_SCHEMA, default_state_db, state_disabled_by_env

# A cache path that is one of these is the shared SQLite store; anything else
# is the legacy per-tool JSON file, which is still honoured when a run asks for
# it by name so an existing --cache in somebody's scheduler keeps working.
SQLITE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})


def is_state_db_path(path: Path | str) -> bool:
    return Path(path).suffix.casefold() in SQLITE_SUFFIXES


class _ProbeBackend(Protocol):
    """Where a cache's entries are kept between runs."""

    def load(self) -> dict[str, dict[str, Any]]: ...

    def save(self, entries: dict[str, dict[str, Any]]) -> None: ...


class _JsonBackend:
    """The original one-file-per-tool JSON cache."""

    def __init__(self, path: Path, tool: str, schema: int) -> None:
        self.path = path
        self.tool = tool
        self.schema = schema

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        if raw.get("schema") != self.schema or raw.get("tool") != self.tool:
            # A different tool's cache or an older format: start clean rather
            # than guess at a layout we do not understand.
            return {}
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return {}
        return {str(key): value for key, value in entries.items() if isinstance(value, dict)}

    def save(self, entries: dict[str, dict[str, Any]]) -> None:
        document = {"schema": self.schema, "tool": self.tool, "entries": entries}
        try:
            atomic_write_text(
                self.path,
                json.dumps(document, separators=(",", ":"), ensure_ascii=False) + "\n",
            )
        except OSError:
            pass


class _SqliteBackend:
    """The ``probe`` table of the shared state cache.

    A short-lived connection per load and per save, not one held open for the
    whole run: a maintenance pass can take hours, and a cache has no business
    holding a handle on a file another tool may want to write meanwhile. WAL
    mode means the two never block each other anyway.
    """

    def __init__(self, path: Path, tool: str) -> None:
        self.path = path
        self.tool = tool

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(PROBE_SCHEMA)
        except BaseException:
            # Connecting to a file that is not a database succeeds; the first
            # statement is what fails. The callers treat that as a cache miss,
            # so the handle must not outlive the attempt - on Windows an open
            # handle stops anything from deleting or replacing the file.
            with contextlib.suppress(sqlite3.Error, OSError):
                db.close()
            raise
        return db

    def load(self) -> dict[str, dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        if not self.path.is_file():
            # Reading is not a reason to create a database: a run that learns
            # nothing leaves the disk exactly as it found it.
            return {}
        try:
            db = self._connect()
        except (sqlite3.Error, OSError):
            return {}
        try:
            rows = db.execute(
                "SELECT path_key, size, mtime_ns, payload FROM probe WHERE tool=?",
                (self.tool,),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            with contextlib.suppress(sqlite3.Error, OSError):
                db.close()
        for key, size, mtime_ns, payload in rows:
            try:
                decoded = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, dict):
                entries[str(key)] = {"size": size, "mtime_ns": mtime_ns, "payload": decoded}
        return entries

    def save(self, entries: dict[str, dict[str, Any]]) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        rows = []
        for key, entry in entries.items():
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            rows.append((key, self.tool, entry.get("size"), entry.get("mtime_ns"),
                         json.dumps(payload, separators=(",", ":"), ensure_ascii=False), now))
        try:
            db = self._connect()
        except (sqlite3.Error, OSError):
            return
        try:
            db.execute("BEGIN IMMEDIATE")
            # The in-memory set is this tool's whole cache, eviction included,
            # so it replaces rather than merges - exactly what writing the JSON
            # file did.
            db.execute("DELETE FROM probe WHERE tool=?", (self.tool,))
            db.executemany(
                "INSERT OR REPLACE INTO probe "
                "(path_key, tool, size, mtime_ns, payload, recorded) VALUES (?,?,?,?,?,?)",
                rows,
            )
            db.execute("COMMIT")
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error, OSError):
                db.execute("ROLLBACK")
        finally:
            with contextlib.suppress(sqlite3.Error, OSError):
                db.close()


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

    Entries live in the ``probe`` table of the shared ``state.db``, keyed by
    ``(path_key, tool)`` so the two tools cannot read each other's payloads.
    A cache path that is not a database is still read and written as the
    original per-tool JSON file, and a JSON cache left over from an earlier
    version is imported once, so nobody loses a warm cache or a ``--cache``
    flag they already script around.

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
        legacy: Path | str | None = None,
    ) -> None:
        self.path = Path(path)
        self.tool = tool
        self.enabled = bool(enabled)
        self.max_entries = max(1, int(max_entries))
        self.hits = 0
        self.misses = 0
        self.imported = 0
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._backend: _ProbeBackend = (
            _SqliteBackend(self.path, self.tool) if is_state_db_path(self.path)
            else _JsonBackend(self.path, self.tool, self.SCHEMA)
        )
        self._legacy = Path(legacy) if legacy else None
        if self.enabled:
            self._load()

    def _load(self) -> None:
        self._entries = self._backend.load()
        if self._entries or self._legacy is None or not isinstance(self._backend, _SqliteBackend):
            return
        # Nothing stored yet and a JSON cache from an older version is sitting
        # there: adopt it rather than re-probe a library that has not changed.
        # The file is left alone; it is derived data either way, and deleting
        # somebody's cache is not this code's decision to make.
        adopted = _JsonBackend(self._legacy, self.tool, self.SCHEMA).load()
        if not adopted:
            return
        self._entries = adopted
        self.imported = len(adopted)
        self._dirty = True

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
        """Persist the cache. Failures are swallowed by design."""
        if not self.enabled or not self._dirty:
            return
        with self._lock:
            snapshot = dict(self._entries)
            self._dirty = False
        self._backend.save(snapshot)

    def __len__(self) -> int:
        return len(self._entries)


def probe_cache_path(
    path: Path | str | None = None, *, state_db: Path | str | None = None,
) -> Path:
    """Where the probe cache for this run lives: ``--cache``, else the state DB."""
    return Path(path) if path else Path(state_db or default_state_db())


def open_probe_cache(
    path: Path | str | None = None,
    *,
    tool: str,
    enabled: bool = True,
    state_enabled: bool = True,
    state_db: Path | str | None = None,
    legacy: Path | str | None = None,
) -> MediaProbeCache:
    """Open the probe cache a run should use, and never raise.

    ``path`` is what the operator asked for (``--cache``); without one the
    cache goes where the rest of this run's state goes. When that is the shared
    database, the switches that turn the state cache off — ``--no-state`` and
    ``ORGANIZE_NO_STATE`` — turn this off with it, because it is the same file.
    ``--no-cache`` remains the way to skip caching without touching the rest.
    """
    target = probe_cache_path(path, state_db=state_db)
    on = bool(enabled)
    if is_state_db_path(target) and (not state_enabled or state_disabled_by_env()):
        on = False
    return MediaProbeCache(target, tool=tool, enabled=on, legacy=legacy)
