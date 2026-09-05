"""Unit tests for the shared state cache in ``organizekit/core/state.py``.

The cache is a *derived* store: every property tested here is about it being
safe to delete, safe to fail, and incapable of reporting an answer about bytes
that are no longer on disk.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from organizekit.core import (
    KIND_BITDEPTH,
    KIND_LAYOUT,
    KIND_SUBTITLE,
    KIND_SYNC,
    NullStateStore,
    StateStore,
    Verdict,
    default_state_db,
    open_state,
    path_norm,
)


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "state.db"
        self.library = self.root / "lib"
        self.library.mkdir()
        self.movie = self.library / "Alpha (2001)" / "Alpha (2001).mkv"
        self.movie.parent.mkdir(parents=True)
        self.movie.write_bytes(b"x" * 2048)
        self.addCleanup(self._tmp.cleanup)

    def _store(self) -> StateStore:
        store = StateStore(self.db, tool="tests")
        self.addCleanup(store.close)
        return store

    # -- identity and freshness -------------------------------------------

    def test_verdict_round_trips_with_its_stamp(self) -> None:
        store = self._store()
        store.record(self.movie, KIND_BITDEPTH, "SKIP_HDR", "HDR10")
        stored = store.verdicts()[(path_norm(self.movie), KIND_BITDEPTH)]
        info = self.movie.stat()
        self.assertEqual(stored.verdict, "SKIP_HDR")
        self.assertEqual(stored.detail, "HDR10")
        self.assertEqual(stored.tool, "tests")
        self.assertEqual(stored.size, info.st_size)
        self.assertEqual(stored.mtime_ns, info.st_mtime_ns)
        self.assertTrue(stored.is_current_for(info.st_size, info.st_mtime_ns))

    def test_verdict_is_stale_once_the_bytes_change(self) -> None:
        store = self._store()
        store.record(self.movie, KIND_BITDEPTH, "SKIP_HDR")
        self.movie.write_bytes(b"y" * 4096)
        info = self.movie.stat()
        stored = store.verdicts()[(path_norm(self.movie), KIND_BITDEPTH)]
        self.assertFalse(stored.is_current_for(info.st_size, info.st_mtime_ns))

    def test_unknown_stamp_is_never_reported_as_current(self) -> None:
        # "Cannot tell" must read as stale on either side of the comparison:
        # an optimistic guess is how a cache starts lying about a library.
        verdict = Verdict(path_key="k", kind=KIND_SYNC, verdict="synced")
        self.assertFalse(verdict.is_current_for(10, 20))
        stamped = Verdict(path_key="k", kind=KIND_SYNC, verdict="synced", size=10, mtime_ns=20)
        self.assertFalse(stamped.is_current_for(None, None))
        self.assertTrue(stamped.is_current_for(10, 20))

    def test_record_prefers_the_callers_stamp(self) -> None:
        store = self._store()
        store.record(self.movie, KIND_SYNC, "synced", size=11, mtime_ns=22)
        stored = store.verdicts()[(path_norm(self.movie), KIND_SYNC)]
        self.assertEqual((stored.size, stored.mtime_ns), (11, 22))

    def test_second_record_replaces_the_first_for_the_same_kind(self) -> None:
        store = self._store()
        store.record(self.movie, KIND_LAYOUT, "MISSING_SIDECAR")
        store.record(self.movie, KIND_LAYOUT, "CANONICAL_MKV")
        rows = store.verdicts(KIND_LAYOUT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[(path_norm(self.movie), KIND_LAYOUT)].verdict, "CANONICAL_MKV")

    def test_kinds_are_independent(self) -> None:
        # The reason verdicts are one row per kind: a current bit-depth answer
        # must not make a stale sync answer look fresh.
        store = self._store()
        store.record(self.movie, KIND_SYNC, "synced")
        self.movie.write_bytes(b"z" * 8192)
        store.record(self.movie, KIND_BITDEPTH, "SKIP_HDR")
        info = self.movie.stat()
        rows = store.verdicts()
        self.assertTrue(rows[(path_norm(self.movie), KIND_BITDEPTH)]
                        .is_current_for(info.st_size, info.st_mtime_ns))
        self.assertFalse(rows[(path_norm(self.movie), KIND_SYNC)]
                         .is_current_for(info.st_size, info.st_mtime_ns))

    def test_verdicts_can_be_filtered_by_kind(self) -> None:
        store = self._store()
        store.record(self.movie, KIND_LAYOUT, "CANONICAL_MKV")
        store.record(self.movie, KIND_SUBTITLE, "present")
        self.assertEqual(len(store.verdicts()), 2)
        self.assertEqual(list(store.verdicts(KIND_SUBTITLE)),
                         [(path_norm(self.movie), KIND_SUBTITLE)])

    # -- movie rows --------------------------------------------------------

    def test_see_movie_records_the_live_stat(self) -> None:
        store = self._store()
        key = store.see_movie(self.movie)
        row = store.movies()[key]
        info = self.movie.stat()
        self.assertEqual(row.path, str(self.movie))
        self.assertEqual(row.folder, str(self.movie.parent))
        self.assertEqual(row.size, info.st_size)
        self.assertEqual(row.mtime_ns, info.st_mtime_ns)
        self.assertEqual(row.nlink, info.st_nlink)

    def test_see_movie_is_idempotent(self) -> None:
        store = self._store()
        store.see_movie(self.movie)
        store.see_movie(self.movie)
        self.assertEqual(len(store.movies()), 1)

    def test_forget_missing_drops_deleted_movies_and_their_verdicts(self) -> None:
        store = self._store()
        gone = self.library / "Bravo (2002)" / "Bravo (2002).mkv"
        gone.parent.mkdir(parents=True)
        gone.write_bytes(b"q")
        keep_key = store.see_movie(self.movie)
        store.see_movie(gone)
        store.record(gone, KIND_LAYOUT, "CANONICAL_MKV")
        removed = store.forget_missing([keep_key])
        self.assertEqual(removed, 1)
        self.assertEqual(list(store.movies()), [keep_key])
        self.assertEqual(store.verdicts(), {})

    # -- quota -------------------------------------------------------------

    def test_reserve_quota_refuses_to_go_past_the_cap(self) -> None:
        store = self._store()
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        self.assertTrue(store.reserve_quota("opensubtitles", day, cap=2))
        self.assertTrue(store.reserve_quota("opensubtitles", day, cap=2))
        self.assertFalse(store.reserve_quota("opensubtitles", day, cap=2))
        self.assertEqual(store.quota_used("opensubtitles", day), 2)

    def test_quota_is_per_provider_and_per_day(self) -> None:
        store = self._store()
        self.assertTrue(store.reserve_quota("subdl", "2026-01-01", cap=1))
        self.assertFalse(store.reserve_quota("subdl", "2026-01-01", cap=1))
        self.assertTrue(store.reserve_quota("subdl", "2026-01-02", cap=1))
        self.assertTrue(store.reserve_quota("opensubtitles", "2026-01-01", cap=1))

    def test_quota_of_an_unused_provider_is_zero(self) -> None:
        self.assertEqual(self._store().quota_used("nobody", "2026-01-01"), 0)

    # -- events ------------------------------------------------------------

    def test_note_is_readable_back_newest_first(self) -> None:
        store = self._store()
        store.note("audit", "first")
        store.note("audit", "second")
        details = [row["detail"] for row in store.recent_events(limit=5)]
        self.assertEqual(details[0], "second")

    def test_prune_events_keeps_the_newest(self) -> None:
        store = self._store()
        for index in range(12):
            store.note("audit", f"event {index}")
        store.prune_events(keep=5)
        rows = store.recent_events(limit=50)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["detail"], "event 11")

    # -- durability and failure modes --------------------------------------

    def test_state_survives_reopening(self) -> None:
        store = self._store()
        store.record(self.movie, KIND_SYNC, "synced")
        store.close()
        reopened = self._store()
        self.assertIn((path_norm(self.movie), KIND_SYNC), reopened.verdicts())

    def test_deleting_the_database_costs_data_not_correctness(self) -> None:
        store = self._store()
        store.record(self.movie, KIND_SYNC, "synced")
        store.close()
        self.db.unlink()
        rebuilt = self._store()
        self.assertEqual(rebuilt.verdicts(), {})
        rebuilt.record(self.movie, KIND_SYNC, "synced")
        self.assertEqual(len(rebuilt.verdicts()), 1)

    def test_open_state_downgrades_instead_of_raising(self) -> None:
        junk = self.root / "not-a-database.db"
        junk.write_bytes(b"this is not sqlite" * 100)
        store = open_state(junk, tool="tests")
        self.addCleanup(store.close)
        self.assertFalse(store.enabled)
        self.assertEqual(store.verdicts(), {})

    def test_open_state_disabled_returns_the_null_store(self) -> None:
        store = open_state(self.db, enabled=False)
        self.assertIsInstance(store, NullStateStore)
        self.assertFalse(self.db.exists())

    def test_env_switch_disables_the_cache_everywhere(self) -> None:
        os.environ["ORGANIZE_NO_STATE"] = "1"
        self.addCleanup(os.environ.pop, "ORGANIZE_NO_STATE", None)
        store = open_state(self.db)
        self.assertIsInstance(store, NullStateStore)

    def test_env_override_moves_the_default_location(self) -> None:
        os.environ["ORGANIZE_STATE_DB"] = str(self.db)
        self.addCleanup(os.environ.pop, "ORGANIZE_STATE_DB", None)
        self.assertEqual(default_state_db(), self.db)

    def test_default_location_is_outside_the_library(self) -> None:
        os.environ.pop("ORGANIZE_STATE_DB", None)
        self.assertNotIn("final_organized", str(default_state_db()))

    def test_null_store_answers_every_call_without_a_database(self) -> None:
        store = NullStateStore()
        with store:
            self.assertFalse(store.enabled)
            self.assertEqual(store.see_movie(self.movie), path_norm(self.movie))
            self.assertEqual(store.movies(), {})
            self.assertEqual(store.forget_missing(["a"]), 0)
            store.record(self.movie, KIND_SYNC, "synced")
            self.assertEqual(store.record_many([(self.movie, KIND_SYNC, "synced", "")]), 0)
            self.assertEqual(store.verdicts(), {})
            self.assertEqual(store.quota_used("subdl", "2026-01-01"), 0)
            # No ledger to reserve against, so the caller's own accounting rules.
            self.assertTrue(store.reserve_quota("subdl", "2026-01-01", cap=0))
            store.note("audit", "ignored")
            self.assertEqual(store.recent_events(), [])
            store.prune_events()

    def test_a_broken_connection_never_raises_through_the_api(self) -> None:
        store = self._store()
        store.record(self.movie, KIND_SYNC, "synced")
        store._db.close()  # simulate the database vanishing mid-run
        store.record(self.movie, KIND_SYNC, "review")  # must not raise
        self.assertEqual(store.verdicts(), {})
        self.assertEqual(store.movies(), {})
        self.assertEqual(store.quota_used("subdl", "2026-01-01"), 0)
        self.assertEqual(store.recent_events(), [])

    def test_schema_is_versioned_and_in_wal_mode(self) -> None:
        store = self._store()
        store.record(self.movie, KIND_SYNC, "synced")
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")


if __name__ == "__main__":
    unittest.main()
