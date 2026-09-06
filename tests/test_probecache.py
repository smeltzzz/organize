"""The probe cache: what it stores, where, and what it refuses to get wrong.

A probe is a pure function of a file's bytes, so reusing one is only safe while
those bytes are unchanged — and a cache that is wrong is worse than no cache at
all, because it makes a tool blind to a change it exists to react to. These
tests hold that line from both directions: an unchanged file is a hit, anything
else is a miss, and every failure mode of the storage itself (missing, corrupt,
foreign, unwritable) is a miss rather than an error.

The payloads now live in the ``probe`` table of the shared ``state.db``. The
per-tool JSON file earlier versions wrote is still read and written when a run
asks for one by name, and is imported once when the database is empty, so
upgrading costs nobody a full re-probe.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from organizekit.core import MediaProbeCache, open_probe_cache, path_norm, probe_cache_path

PAYLOAD = {"streams": [{"codec_type": "video", "bits_per_raw_sample": "8"}]}


class ProbeCacheFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="probe_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.db = self.root / "state.db"
        self.legacy = self.root / "10bit_probe_cache.json"

    def cache(self, path: Path | None = None, **kw: object) -> MediaProbeCache:
        return MediaProbeCache(path or self.db, tool=kw.pop("tool", "10bit"), **kw)  # type: ignore[arg-type]

    def rows(self) -> list[tuple[str, str]]:
        if not self.db.exists():
            return []
        with sqlite3.connect(self.db) as db:
            return [(row[0], row[1]) for row in
                    db.execute("SELECT tool, path_key FROM probe ORDER BY path_key")]


class WhatItStoresTests(ProbeCacheFixture):
    """The rule: the same bytes get the stored answer, anything else does not."""

    def test_a_cold_cache_is_a_miss(self) -> None:
        self.assertIsNone(self.cache().get("/m/a.mkv", 10, 1))

    def test_an_unchanged_file_is_a_hit(self) -> None:
        cache = self.cache()
        cache.put("/m/a.mkv", 10, 1, PAYLOAD)
        self.assertEqual(cache.get("/m/a.mkv", 10, 1), PAYLOAD)
        self.assertEqual((cache.hits, cache.misses), (1, 0))

    def test_a_resized_file_is_a_miss(self) -> None:
        cache = self.cache()
        cache.put("/m/a.mkv", 10, 1, PAYLOAD)
        self.assertIsNone(cache.get("/m/a.mkv", 11, 1))

    def test_a_touched_file_is_a_miss(self) -> None:
        cache = self.cache()
        cache.put("/m/a.mkv", 10, 1, PAYLOAD)
        self.assertIsNone(cache.get("/m/a.mkv", 10, 2))

    def test_the_payload_survives_the_run_that_stored_it(self) -> None:
        cache = self.cache()
        cache.put("/m/a.mkv", 10, 1, PAYLOAD)
        cache.save()
        self.assertEqual(self.cache().get("/m/a.mkv", 10, 1), PAYLOAD)

    def test_it_is_stored_in_the_databases_probe_table(self) -> None:
        cache = self.cache()
        cache.put("/m/a.mkv", 10, 1, PAYLOAD)
        cache.save()
        self.assertEqual(self.rows(), [("10bit", path_norm("/m/a.mkv"))])

    def test_one_tool_never_reads_another_tools_payload(self) -> None:
        """ffprobe output and `mkvmerge -J` output are not the same document."""
        cache = self.cache(tool="10bit")
        cache.put("/m/a.mkv", 10, 1, PAYLOAD)
        cache.save()
        other = self.cache(tool="mkv_track_cleaner")
        self.assertIsNone(other.get("/m/a.mkv", 10, 1))
        other.put("/m/a.mkv", 10, 1, {"tracks": []})
        other.save()
        self.assertEqual(self.cache(tool="10bit").get("/m/a.mkv", 10, 1), PAYLOAD)
        self.assertEqual(len(self.rows()), 2, "both are kept, side by side")

    def test_saving_the_whole_cache_drops_what_was_evicted(self) -> None:
        cache = self.cache(max_entries=2)
        for index in range(4):
            cache.put(f"/m/{index}.mkv", 10, 1, PAYLOAD)
        cache.save()
        self.assertEqual([Path(key).name for _tool, key in self.rows()],
                         ["2.mkv", "3.mkv"], "the two oldest were evicted")

    def test_a_row_the_cache_no_longer_holds_is_dropped_from_the_database(self) -> None:
        """Saving publishes the cache as it stands, so eviction is durable.

        Otherwise the table only ever grows: every run would evict in memory
        and write the survivors back on top of everything it had dropped.
        """
        first = self.cache(max_entries=2)
        first.put("/m/a.mkv", 10, 1, PAYLOAD)
        first.put("/m/b.mkv", 10, 1, PAYLOAD)
        first.save()
        second = self.cache(max_entries=2)
        second.put("/m/c.mkv", 10, 1, PAYLOAD)
        second.save()
        self.assertEqual([Path(key).name for _tool, key in self.rows()],
                         ["b.mkv", "c.mkv"])

    def test_a_disabled_cache_stores_nothing_and_creates_nothing(self) -> None:
        cache = self.cache(enabled=False)
        cache.put("/m/a.mkv", 10, 1, PAYLOAD)
        cache.save()
        self.assertIsNone(cache.get("/m/a.mkv", 10, 1))
        self.assertFalse(self.db.exists())

    def test_nothing_learned_means_nothing_written(self) -> None:
        self.cache().save()
        self.assertFalse(self.db.exists())

    def test_many_threads_may_share_one_cache(self) -> None:
        """Both tools probe in a worker pool; the cache is shared across it."""
        cache = self.cache()

        def work(index: int) -> None:
            cache.put(f"/m/{index}.mkv", 10, 1, PAYLOAD)
            cache.get(f"/m/{index}.mkv", 10, 1)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(cache), 16)
        self.assertEqual((cache.hits, cache.misses), (16, 0))


class WhenTheStorageIsBrokenTests(ProbeCacheFixture):
    """Every one of these is a cache miss. None of them is an error."""

    def test_a_file_that_is_not_a_database_is_a_miss(self) -> None:
        self.db.write_bytes(b"this is not a database")
        cache = self.cache()
        self.assertIsNone(cache.get("/m/a.mkv", 10, 1))
        cache.put("/m/a.mkv", 10, 1, PAYLOAD)
        cache.save()  # must not raise

    def test_a_corrupt_json_cache_is_a_miss(self) -> None:
        self.legacy.write_text("{not json at all", encoding="utf-8")
        self.assertIsNone(self.cache(self.legacy).get("/m/a.mkv", 10, 1))

    def test_a_json_cache_from_another_tool_is_ignored(self) -> None:
        warm = MediaProbeCache(self.legacy, tool="mkv_track_cleaner")
        warm.put("/m/a.mkv", 10, 1, PAYLOAD)
        warm.save()
        self.assertIsNone(MediaProbeCache(self.legacy, tool="10bit").get("/m/a.mkv", 10, 1))

    def test_a_row_whose_payload_is_not_json_is_skipped(self) -> None:
        cache = self.cache()
        cache.put("/m/a.mkv", 10, 1, PAYLOAD)
        cache.save()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE probe SET payload='<not json>'")
        self.assertIsNone(self.cache().get("/m/a.mkv", 10, 1))

    def test_a_directory_where_the_database_should_be_is_a_miss(self) -> None:
        self.db.mkdir()
        cache = self.cache()
        cache.put("/m/a.mkv", 10, 1, PAYLOAD)
        cache.save()  # must not raise
        self.assertEqual(len(self.cache()), 0)


class TheLegacyJsonCacheTests(ProbeCacheFixture):
    """Upgrading must not cost a warm library a full re-probe."""

    def warm_legacy(self, tool: str = "10bit") -> None:
        warm = MediaProbeCache(self.legacy, tool=tool)
        warm.put("/m/a.mkv", 10, 1, PAYLOAD)
        warm.save()

    def test_a_json_path_still_writes_the_json_document(self) -> None:
        self.warm_legacy()
        document = json.loads(self.legacy.read_text(encoding="utf-8"))
        self.assertEqual(document["tool"], "10bit")
        self.assertEqual(document["schema"], MediaProbeCache.SCHEMA)
        self.assertIn(path_norm("/m/a.mkv"), document["entries"])

    def test_an_empty_database_adopts_the_old_file(self) -> None:
        self.warm_legacy()
        cache = self.cache(legacy=self.legacy)
        self.assertEqual(cache.imported, 1)
        self.assertEqual(cache.get("/m/a.mkv", 10, 1), PAYLOAD)
        cache.save()
        self.assertEqual(self.rows(), [("10bit", path_norm("/m/a.mkv"))])
        self.assertTrue(self.legacy.is_file(), "the old file is derived data, not litter")

    def test_it_is_imported_once_and_then_the_database_is_the_truth(self) -> None:
        self.warm_legacy()
        first = self.cache(legacy=self.legacy)
        first.save()
        stale = MediaProbeCache(self.legacy, tool="10bit")
        stale.put("/m/a.mkv", 10, 1, {"streams": ["stale"]})
        stale.save()
        second = self.cache(legacy=self.legacy)
        self.assertEqual(second.imported, 0)
        self.assertEqual(second.get("/m/a.mkv", 10, 1), PAYLOAD)

    def test_the_other_tools_json_cache_is_not_adopted(self) -> None:
        self.warm_legacy(tool="mkv_track_cleaner")
        self.assertEqual(self.cache(tool="10bit", legacy=self.legacy).imported, 0)

    def test_a_missing_old_file_is_simply_a_cold_start(self) -> None:
        self.assertEqual(self.cache(legacy=self.root / "nope.json").imported, 0)

    def test_a_json_cache_never_imports_anything(self) -> None:
        """--cache old.json means \"use this file\", not \"copy it somewhere\"."""
        self.warm_legacy()
        self.assertEqual(self.cache(self.root / "other.json", legacy=self.legacy).imported, 0)


class ChoosingTheCacheTests(ProbeCacheFixture):
    """`open_probe_cache` decides where a run's payloads go, and whether."""

    def setUp(self) -> None:
        super().setUp()
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        os.environ.pop("ORGANIZE_NO_STATE", None)
        os.environ["ORGANIZE_STATE_DB"] = str(self.db)

    def test_without_a_cache_flag_it_follows_the_state_cache(self) -> None:
        self.assertEqual(probe_cache_path(), self.db)
        self.assertEqual(open_probe_cache(tool="10bit").path, self.db)

    def test_an_explicit_state_db_wins_over_the_default(self) -> None:
        other = self.root / "elsewhere.db"
        self.assertEqual(open_probe_cache(tool="10bit", state_db=other).path, other)

    def test_an_explicit_cache_path_wins_over_both(self) -> None:
        self.assertEqual(
            open_probe_cache(self.legacy, tool="10bit", state_db=self.db).path, self.legacy)

    def test_no_cache_disables_it_wherever_it_lives(self) -> None:
        self.assertFalse(open_probe_cache(tool="10bit", enabled=False).enabled)
        self.assertFalse(
            open_probe_cache(self.legacy, tool="10bit", enabled=False).enabled)

    def test_no_state_disables_the_database_backed_cache(self) -> None:
        """It is the same file, so one switch cannot half-disable it."""
        self.assertFalse(open_probe_cache(tool="10bit", state_enabled=False).enabled)

    def test_but_a_json_cache_asked_for_by_name_still_works(self) -> None:
        self.assertTrue(
            open_probe_cache(self.legacy, tool="10bit", state_enabled=False).enabled)

    def test_the_environment_kill_switch_disables_it_too(self) -> None:
        os.environ["ORGANIZE_NO_STATE"] = "1"
        self.assertFalse(open_probe_cache(tool="10bit").enabled)
        self.assertTrue(open_probe_cache(self.legacy, tool="10bit").enabled)


if __name__ == "__main__":
    unittest.main()
