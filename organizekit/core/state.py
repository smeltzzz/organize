"""The optional state cache: what the tools have already decided.

Every tool in this repo re-derives its verdict from the filesystem on every
run, and that is what makes them safe to interrupt, safe to run twice and safe
to run in any order. Nothing here changes that. This is a *cache of answers*,
never an authority, and it obeys three rules that keep it that way:

1. **It is rebuildable from the library at any time.** Deleting ``state.db``
   costs one slow pass, never correctness. No tool asks it for permission to
   act; the tools consult live filesystem state exactly as they always have.
2. **A stored verdict is keyed to the bytes it was measured against.** Every
   row carries the file's size and ``st_mtime_ns``; a verdict whose file has
   changed is reported as stale and is never presented as current. This is the
   same rule ``MediaProbeCache`` uses for probe payloads, for the same reason.
3. **It fails open and silently.** A missing, locked, corrupt or foreign
   database is a cache miss, not an error, and a write that cannot happen costs
   the next ``organize status`` its detail and nothing else. A maintenance run
   that would otherwise succeed must never fail because of a cache.

What it buys: ``organize status`` can answer "what is left to do?" in
milliseconds without touching a single media byte, and a long convergence run
can tell which movies are actually pending instead of re-deriving the whole
library five times per pass.

The schema deliberately differs from one wide ``movie`` row per file: verdicts
live in their own table keyed by ``(path_key, kind)`` and carry their own
size/mtime stamp, because the bit-depth answer for a movie can be current while
the sync answer for the same movie is stale. A single row per movie cannot
express that, and would quietly report a stale verdict as fresh.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import default_reports_root
from .fsio import path_norm

SCHEMA_VERSION = 1
STATE_DB_ENV = "ORGANIZE_STATE_DB"
STATE_OFF_ENV = "ORGANIZE_NO_STATE"
STATE_DB_NAME = "state.db"

# Verdict kinds. One per question a tool answers about a movie; the string is
# stored, so these are a vocabulary, not an implementation detail.
KIND_LAYOUT = "layout"
KIND_SUBTITLE = "subtitle"
KIND_REMUX = "remux"
KIND_BITDEPTH = "bitdepth"
KIND_SYNC = "sync"
KINDS = (KIND_LAYOUT, KIND_SUBTITLE, KIND_REMUX, KIND_BITDEPTH, KIND_SYNC)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS movie (
    path_key   TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    folder     TEXT NOT NULL,
    size       INTEGER,
    mtime_ns   INTEGER,
    nlink      INTEGER,
    inode      INTEGER,
    first_seen TEXT,
    last_seen  TEXT
);
CREATE TABLE IF NOT EXISTS verdict (
    path_key TEXT NOT NULL,
    kind     TEXT NOT NULL,
    verdict  TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT '',
    size     INTEGER,
    mtime_ns INTEGER,
    tool     TEXT NOT NULL DEFAULT '',
    recorded TEXT NOT NULL,
    PRIMARY KEY (path_key, kind)
);
CREATE TABLE IF NOT EXISTS quota (
    provider TEXT NOT NULL,
    utc_day  TEXT NOT NULL,
    used     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, utc_day)
);
CREATE TABLE IF NOT EXISTS event (
    ts        TEXT NOT NULL,
    tool      TEXT NOT NULL,
    path_key  TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT '',
    detail    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS verdict_kind ON verdict (kind);
CREATE INDEX IF NOT EXISTS event_ts ON event (ts);
"""


def default_state_db() -> Path:
    """Where the cache lives: env override, else beside the logs and reports.

    Never inside the media library - the auditor would count a database file at
    the library root as a stray artifact, and a media library is not a place
    for tool state.
    """
    override = (os.environ.get(STATE_DB_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return default_reports_root() / STATE_DB_NAME


def state_disabled_by_env() -> bool:
    """``ORGANIZE_NO_STATE=1`` turns the cache off everywhere at once."""
    return (os.environ.get(STATE_OFF_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Verdict:
    """One tool's answer about one movie, and the bytes it answered about."""

    path_key: str
    kind: str
    verdict: str
    detail: str = ""
    size: int | None = None
    mtime_ns: int | None = None
    tool: str = ""
    recorded: str = ""

    def is_current_for(self, size: int | None, mtime_ns: int | None) -> bool:
        """Does this answer still describe the file that is on disk now?

        An unknown stamp on either side means "cannot tell", which is reported
        as stale: guessing in the optimistic direction is how a cache starts
        lying about a library.
        """
        if self.size is None or self.mtime_ns is None or size is None or mtime_ns is None:
            return False
        return int(self.size) == int(size) and int(self.mtime_ns) == int(mtime_ns)


@dataclass(frozen=True)
class StoredMovie:
    path_key: str
    path: str
    folder: str
    size: int | None = None
    mtime_ns: int | None = None
    nlink: int | None = None
    inode: int | None = None
    first_seen: str = ""
    last_seen: str = ""


class StateStore:
    """A small SQLite cache of what the tools have already worked out.

    Open it with :func:`open_state`, which returns a :class:`NullStateStore`
    when the cache is disabled or cannot be opened, so callers never branch on
    whether state is available.
    """

    enabled = True

    def __init__(self, path: Path | str, *, tool: str = "organize") -> None:
        self.path = Path(path)
        self.tool = tool
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        # WAL lets a reader (organize status) run while a tool is writing, and
        # NORMAL sync is right for a cache that is rebuildable by definition.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One transaction, and never an exception out of a cache write."""
        try:
            self._db.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            yield self._db  # best effort: the statements below will no-op
            return
        try:
            yield self._db
        except sqlite3.Error:
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return
        try:
            self._db.execute("COMMIT")
        except sqlite3.Error:
            pass

    # -- movies ------------------------------------------------------------

    def see_movie(self, movie: Path, *, folder: Path | None = None) -> str:
        """Record that this file exists right now, and return its key."""
        key = path_norm(movie)
        try:
            info = movie.stat()
            size, mtime_ns, nlink, inode = info.st_size, info.st_mtime_ns, info.st_nlink, info.st_ino
        except OSError:
            size = mtime_ns = nlink = inode = None
        now = _now()
        with self._write() as db:
            try:
                db.execute(
                    "INSERT INTO movie (path_key, path, folder, size, mtime_ns, nlink, inode,"
                    " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(path_key) DO UPDATE SET path=excluded.path,"
                    " folder=excluded.folder, size=excluded.size, mtime_ns=excluded.mtime_ns,"
                    " nlink=excluded.nlink, inode=excluded.inode, last_seen=excluded.last_seen",
                    (key, str(movie), str(folder or movie.parent), size, mtime_ns, nlink,
                     inode, now, now),
                )
            except sqlite3.Error:
                pass
        return key

    def movies(self) -> dict[str, StoredMovie]:
        try:
            rows = self._db.execute("SELECT * FROM movie").fetchall()
        except sqlite3.Error:
            return {}
        return {
            row["path_key"]: StoredMovie(
                path_key=row["path_key"], path=row["path"], folder=row["folder"],
                size=row["size"], mtime_ns=row["mtime_ns"], nlink=row["nlink"],
                inode=row["inode"], first_seen=row["first_seen"] or "",
                last_seen=row["last_seen"] or "",
            )
            for row in rows
        }

    def forget_missing(self, live_keys: Iterable[str]) -> int:
        """Drop rows for movies that are no longer in the library.

        A cache that only ever grows eventually describes a library that no
        longer exists; ``organize status`` would then report movies nobody has.
        """
        live = set(live_keys)
        removed = 0
        for key in set(self.movies()) - live:
            with self._write() as db:
                try:
                    db.execute("DELETE FROM movie WHERE path_key=?", (key,))
                    db.execute("DELETE FROM verdict WHERE path_key=?", (key,))
                    removed += 1
                except sqlite3.Error:
                    pass
        return removed

    # -- verdicts ----------------------------------------------------------

    def record(
        self,
        movie: Path,
        kind: str,
        verdict: str,
        detail: str = "",
        *,
        size: int | None = None,
        mtime_ns: int | None = None,
    ) -> None:
        """Store one tool's answer about one movie.

        The size/mtime stamp is taken from the file unless the caller passes
        one it already has - the caller's is preferred precisely because it is
        the stamp the verdict was computed from.
        """
        key = path_norm(movie)
        if size is None or mtime_ns is None:
            try:
                info = movie.stat()
                size, mtime_ns = info.st_size, info.st_mtime_ns
            except OSError:
                size = mtime_ns = None
        with self._write() as db:
            try:
                db.execute(
                    "INSERT INTO verdict (path_key, kind, verdict, detail, size, mtime_ns,"
                    " tool, recorded) VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(path_key, kind) DO UPDATE SET verdict=excluded.verdict,"
                    " detail=excluded.detail, size=excluded.size, mtime_ns=excluded.mtime_ns,"
                    " tool=excluded.tool, recorded=excluded.recorded",
                    (key, kind, verdict, detail, size, mtime_ns, self.tool, _now()),
                )
            except sqlite3.Error:
                pass

    def record_many(self, verdicts: Iterable[tuple[Path, str, str, str]]) -> int:
        """Record a run's worth of answers. Returns how many were stored."""
        stored = 0
        for movie, kind, verdict, detail in verdicts:
            self.record(movie, kind, verdict, detail)
            stored += 1
        return stored

    def verdicts(self, kind: str | None = None) -> dict[tuple[str, str], Verdict]:
        sql = "SELECT * FROM verdict"
        params: tuple[str, ...] = ()
        if kind is not None:
            sql += " WHERE kind=?"
            params = (kind,)
        try:
            rows = self._db.execute(sql, params).fetchall()
        except sqlite3.Error:
            return {}
        return {
            (row["path_key"], row["kind"]): Verdict(
                path_key=row["path_key"], kind=row["kind"], verdict=row["verdict"],
                detail=row["detail"] or "", size=row["size"], mtime_ns=row["mtime_ns"],
                tool=row["tool"] or "", recorded=row["recorded"] or "",
            )
            for row in rows
        }

    # -- provider quota ----------------------------------------------------

    def quota_used(self, provider: str, utc_day: str) -> int:
        try:
            row = self._db.execute(
                "SELECT used FROM quota WHERE provider=? AND utc_day=?", (provider, utc_day)
            ).fetchone()
        except sqlite3.Error:
            return 0
        return int(row["used"]) if row else 0

    def reserve_quota(self, provider: str, utc_day: str, cap: int, count: int = 1) -> bool:
        """Take ``count`` from today's budget, or refuse. Atomic.

        Reservation happens *before* the request goes out, so concurrent
        callers cannot overspend a provider's daily cap between them - which is
        the property that makes fetching in parallel safe at all.
        """
        if count <= 0:
            return True
        granted = False
        with self._write() as db:
            try:
                row = db.execute(
                    "SELECT used FROM quota WHERE provider=? AND utc_day=?", (provider, utc_day)
                ).fetchone()
                used = int(row["used"]) if row else 0
                if used + count > cap:
                    return False
                db.execute(
                    "INSERT INTO quota (provider, utc_day, used) VALUES (?,?,?)"
                    " ON CONFLICT(provider, utc_day) DO UPDATE SET used=used+?",
                    (provider, utc_day, count, count),
                )
                granted = True
            except sqlite3.Error:
                return False
        return granted

    # -- events ------------------------------------------------------------

    def note(self, kind: str, detail: str = "", movie: Path | None = None) -> None:
        with self._write() as db:
            try:
                db.execute(
                    "INSERT INTO event (ts, tool, path_key, kind, detail) VALUES (?,?,?,?,?)",
                    (_now(), self.tool, path_norm(movie) if movie else "", kind, detail),
                )
            except sqlite3.Error:
                pass

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        try:
            return list(self._db.execute(
                "SELECT * FROM event ORDER BY ts DESC LIMIT ?", (int(limit),)
            ).fetchall())
        except sqlite3.Error:
            return []

    def prune_events(self, keep: int = 5000) -> None:
        with self._write() as db:
            try:
                db.execute(
                    "DELETE FROM event WHERE rowid NOT IN"
                    " (SELECT rowid FROM event ORDER BY ts DESC LIMIT ?)", (int(keep),)
                )
            except sqlite3.Error:
                pass


class NullStateStore:
    """The cache, turned off. Same surface, every answer empty.

    Callers get this from :func:`open_state` when ``--no-state`` is passed, the
    environment disables the cache, or SQLite cannot open the file at all - so
    no tool needs an ``if self.state is not None`` around a write.
    """

    enabled = False
    path: Path | None = None
    tool = ""

    def close(self) -> None:
        return None

    def __enter__(self) -> NullStateStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def see_movie(self, movie: Path, *, folder: Path | None = None) -> str:
        return path_norm(movie)

    def movies(self) -> dict[str, StoredMovie]:
        return {}

    def forget_missing(self, live_keys: Iterable[str]) -> int:
        return 0

    def record(self, movie: Path, kind: str, verdict: str, detail: str = "", **_kw: object) -> None:
        return None

    def record_many(self, verdicts: Iterable[tuple[Path, str, str, str]]) -> int:
        return 0

    def verdicts(self, kind: str | None = None) -> dict[tuple[str, str], Verdict]:
        return {}

    def quota_used(self, provider: str, utc_day: str) -> int:
        return 0

    def reserve_quota(self, provider: str, utc_day: str, cap: int, count: int = 1) -> bool:
        # With no ledger there is nothing to reserve against; the caller's own
        # in-process accounting still applies.
        return True

    def note(self, kind: str, detail: str = "", movie: Path | None = None) -> None:
        return None

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        return []

    def prune_events(self, keep: int = 5000) -> None:
        return None


def open_state(
    path: Path | str | None = None,
    *,
    enabled: bool = True,
    tool: str = "organize",
) -> StateStore | NullStateStore:
    """Open the cache, or return the null one. Never raises."""
    if not enabled or state_disabled_by_env():
        return NullStateStore()
    try:
        return StateStore(path or default_state_db(), tool=tool)
    except (sqlite3.Error, OSError):
        # An unwritable directory, a database from a newer schema, a file that
        # is not a database at all: a cache miss, not a failed run.
        return NullStateStore()
