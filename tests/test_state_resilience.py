"""The state cache is a side-channel: it may never fail a run that worked.

Every tool publishes what it worked out - a remux verdict, a bit-depth answer,
an audit row, an OpenSubtitles quota reservation - into one SQLite file, and
``organize status`` reads it back. That file lives in the user's state
directory, which on a NAS can be a share that goes read-only, a database
another process holds a write lock on, or a file somebody replaced with
something that is not a database at all.

None of those may cost a movie. A sweep that has just remuxed three hundred
files has to report success even if it could not remember what it did, and
``status`` has to answer "unknown" rather than raise. So every write path here
swallows its own failure - and a swallow nobody has ever seen execute is a
swallow nobody can trust.

Each test below injects the failure at the statement that would raise and
asserts the answer the caller gets, which is the part a tool builds on:

* the write methods return normally, with the count they intended;
* a movie that cannot be ``stat``\\ ed is still recorded, unstamped, so its
  verdicts read as stale instead of as answers;
* a transaction that cannot be committed is rolled back, because the next
  ``BEGIN IMMEDIATE`` would otherwise fail and every later write would quietly
  run outside a transaction;
* and the one place that fails *closed* on purpose: a quota reservation that
  cannot be written refuses the download, because guessing "there is budget
  left" is how an API key gets banned.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organizekit.core import state as state_mod
from organizekit.core.state import KIND_BITDEPTH, KIND_REMUX, StateStore, open_state, path_norm

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class BrokenConnection:
    """A proxy for ``StateStore._db`` that fails on chosen statements.

    ``sqlite3.Connection`` attributes are read-only, so a failure cannot be
    patched onto the real object; this stands in for it, forwards everything
    else, and records every statement it saw so a test can assert what the
    recovery did and not merely that it did not raise.
    """

    def __init__(self, real: sqlite3.Connection, fail_on: tuple[str, ...] = (),
                 *, fail_close: bool = False, error: type[Exception] = sqlite3.OperationalError
                 ) -> None:
        self._real = real
        self.fail_on = tuple(token.upper() for token in fail_on)
        self.fail_close = fail_close
        self.error = error
        self.statements: list[str] = []

    def _guard(self, sql: str) -> None:
        self.statements.append(" ".join(sql.split())[:60])
        if any(token in sql.upper() for token in self.fail_on):
            if self.error is sqlite3.OperationalError:
                raise sqlite3.OperationalError("database is locked")
            raise self.error("not a database problem at all")

    def execute(self, sql: str, *args: object, **kwargs: object) -> object:
        self._guard(sql)
        return self._real.execute(sql, *args, **kwargs)

    def executemany(self, sql: str, *args: object, **kwargs: object) -> object:
        self._guard(sql)
        return self._real.executemany(sql, *args, **kwargs)

    def executescript(self, sql: str) -> object:
        return self._real.executescript(sql)

    def close(self) -> None:
        if self.fail_close:
            raise sqlite3.ProgrammingError("cannot close: another thread is using it")
        self._real.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class StateStoreFixture:
    """A store on a temp database, plus the handle that breaks it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="state_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.db_path = self.root / "state.db"
        self.store = StateStore(self.db_path, tool="tests")
        self.addCleanup(self.store.close)
        self.library = self.root / "lib"
        self.library.mkdir()

    def _movie(self, title: str, size: int = 4096) -> Path:
        folder = self.library / title
        folder.mkdir(parents=True, exist_ok=True)
        movie = folder / f"{title}.mkv"
        movie.write_bytes(b"x" * size)
        return movie

    def break_statements(self, *tokens: str, fail_close: bool = False,
                         error: type[Exception] = sqlite3.OperationalError) -> BrokenConnection:
        """Replace the store's connection with one that fails on ``tokens``."""
        broken = BrokenConnection(self.store._db, tokens, fail_close=fail_close, error=error)
        self.store._db = broken
        return broken


class CacheWritesNeverEscapeTests(StateStoreFixture, unittest.TestCase):
    """One injected failure per write path; the caller must get an answer."""

    def test_a_movie_insert_that_fails_still_returns_the_key(self) -> None:
        """The key is what the caller uses to record verdicts afterwards.

        Raising here would abort an audit that had already scanned the whole
        library, for a cache row that only makes the next one faster.
        """
        movie = self._movie("Alpha (2001)")
        self.break_statements("INSERT INTO movie")
        self.assertEqual(self.store.see_movie(movie), path_norm(movie))

    def test_a_batch_of_movies_that_cannot_be_written_still_returns_their_keys(self) -> None:
        movies = [self._movie("Bravo (2002)"), self._movie("Charlie (2003)")]
        self.break_statements("INSERT INTO movie")
        self.assertEqual(self.store.see_movies([(movie, None) for movie in movies]),
                         [path_norm(movie) for movie in movies])

    def test_a_verdict_that_cannot_be_written_does_not_fail_the_tool_that_computed_it(self) -> None:
        movie = self._movie("Delta (2004)")
        self.break_statements("INSERT INTO verdict")
        self.store.record(movie, KIND_REMUX, "cleaned", "the remux verified")  # must not raise
        self.assertEqual(self.store.record_many([(movie, KIND_BITDEPTH, "SKIP_HDR", "")]), 1,
                         "the count is what the tool reports, and the rows were built")

    def test_an_event_that_cannot_be_written_is_dropped_quietly(self) -> None:
        self.break_statements("INSERT INTO event")
        self.store.note("quota", "the provider answered 429")
        self.store.note("skipped", "no English audio", movie=self._movie("Echo (2005)"))

    def test_a_prune_that_fails_leaves_the_events_alone(self) -> None:
        self.store.note("one")
        self.break_statements("DELETE FROM event")
        self.store.prune_events(keep=1)  # must not raise
        self.assertEqual(len(self.store.recent_events(limit=10)), 1)

    def test_a_forget_sweep_whose_deletes_fail_reports_what_it_tried_to_drop(self) -> None:
        """Pruning rows for deleted movies is housekeeping, not the run's result.

        A cache that only grows eventually describes a library nobody has, so
        the sweep matters - but a locked database during it must cost the
        housekeeping, not the audit that asked for it.
        """
        gone = self._movie("Foxtrot (2006)")
        kept = self._movie("Golf (2007)")
        self.store.see_movies([(gone, None), (kept, None)])
        self.break_statements("DELETE FROM")
        self.assertEqual(self.store.forget_missing([path_norm(kept)]), 1)

    def test_closing_a_connection_that_refuses_to_close_is_not_an_error(self) -> None:
        """``close`` runs from ``__exit__``, so a raise would replace the real outcome.

        Whatever the ``with`` body raised is what the operator needs to see;
        a cache handle that will not close must not become the story instead.
        """
        self.break_statements(fail_close=True)
        self.store.close()  # must not raise
        self.store.close()  # and it is safe to ask twice


class TransactionHygieneTests(StateStoreFixture, unittest.TestCase):
    """A transaction that cannot be ended must still be ended."""

    def test_a_commit_that_fails_is_followed_by_a_rollback(self) -> None:
        """The scar: a failed COMMIT left the transaction open.

        From there the next ``BEGIN IMMEDIATE`` fails with "cannot start a
        transaction within a transaction", that failure is swallowed by design,
        and every later write in the run happens outside a transaction - a
        cache that stops being atomic without ever saying so.
        """
        movie = self._movie("Hotel (2008)")
        broken = self.break_statements("COMMIT")
        self.store.record(movie, KIND_REMUX, "cleaned")
        self.assertIn("ROLLBACK", broken.statements,
                      "a failed COMMIT must be followed by a ROLLBACK")

    def test_a_commit_and_a_rollback_that_both_fail_still_return(self) -> None:
        movie = self._movie("India (2009)")
        self.break_statements("COMMIT", "ROLLBACK")
        self.store.record(movie, KIND_REMUX, "cleaned")  # must not raise

    def test_a_body_that_raises_something_else_still_ends_the_transaction(self) -> None:
        """The other half of the same scar: ``sqlite3.Error`` is not the only way out.

        A ``TypeError`` from a bad argument unwinds the generator with the
        transaction still open unless the rollback runs for *every* exception,
        so the write is made to fail with something that is not a database
        error at all - and the rollback that follows is itself made to fail,
        because that is the branch that has to stay quiet.
        """
        movie = self._movie("Juliett (2010)")
        broken = self.break_statements("INSERT INTO movie", "ROLLBACK", error=TypeError)
        with self.assertRaises(TypeError):
            self.store.see_movie(movie)
        self.assertIn("ROLLBACK", broken.statements,
                      "a non-database failure still has to end the transaction")


class UnstampedMovieTests(StateStoreFixture, unittest.TestCase):
    """A movie that cannot be ``stat``\\ ed is recorded as unknown, never guessed."""

    def _unwinnable_path(self) -> Path:
        """A path whose parent is a regular file: ``stat`` answers ENOTDIR."""
        a_file = self.root / "not-a-directory"
        a_file.write_text("x", encoding="utf-8")
        return a_file / "Movie (2011)" / "Movie (2011).mkv"

    def test_seeing_a_movie_that_cannot_be_stamped_still_records_it(self) -> None:
        missing = self._unwinnable_path()
        key = self.store.see_movie(missing)
        stored = self.store.movies()[key]
        self.assertIsNone(stored.size)
        self.assertIsNone(stored.mtime_ns)
        self.assertEqual(stored.path, str(missing))

    def test_a_verdict_about_an_unstamped_movie_is_stale_not_current(self) -> None:
        """The invariant the whole staleness rule exists for.

        ``is_current_for`` answers False whenever either side is unknown, so an
        unstamped row makes every verdict about it read as stale. The opposite
        - treating "cannot tell" as "still true" - is how a cache starts lying
        about a library: ``organize status`` would report a movie as finished
        when nobody can prove the bytes it was measured on are still there.
        """
        movie = self._movie("Kilo (2012)")
        self.store.record(movie, KIND_REMUX, "cleaned")
        verdict = self.store.verdicts(KIND_REMUX)[(path_norm(movie), KIND_REMUX)]
        info = movie.stat()
        self.assertTrue(verdict.is_current_for(info.st_size, info.st_mtime_ns))
        self.assertFalse(verdict.is_current_for(None, info.st_mtime_ns))
        self.assertFalse(verdict.is_current_for(info.st_size, None))
        self.assertFalse(verdict.is_current_for(None, None))

    def test_recording_a_verdict_for_a_file_that_is_gone_stores_it_unstamped(self) -> None:
        gone = self.root / "vanished" / "Movie (2013).mkv"
        self.store.record(gone, KIND_BITDEPTH, "SKIP_HDR")
        verdict = self.store.verdicts(KIND_BITDEPTH)[(path_norm(gone), KIND_BITDEPTH)]
        self.assertIsNone(verdict.size)
        self.assertFalse(verdict.is_current_for(1, 1), "an unstamped answer proves nothing")

    def test_a_batch_stamp_failure_leaves_the_row_but_not_the_stamp(self) -> None:
        gone = self.root / "vanished" / "Movie (2014).mkv"
        self.assertEqual(self.store.record_many([(gone, KIND_REMUX, "cleaned", "")]), 1)
        verdict = self.store.verdicts(KIND_REMUX)[(path_norm(gone), KIND_REMUX)]
        self.assertEqual(verdict.verdict, "cleaned")
        self.assertIsNone(verdict.mtime_ns)

    def test_a_batch_of_unstatable_movies_is_still_seen(self) -> None:
        gone = self._unwinnable_path()
        keys = self.store.see_movies([(gone, None)])
        self.assertEqual(keys, [path_norm(gone)])
        self.assertIsNone(self.store.movies()[keys[0]].nlink)


class QuotaReservationTests(StateStoreFixture, unittest.TestCase):
    """The one cache write that fails closed, because it protects a real budget."""

    DAY = "2026-10-04"

    def test_a_reservation_is_taken_before_the_request_goes_out(self) -> None:
        """Concurrent callers cannot overspend a daily cap between them.

        This is what makes fetching subtitles in parallel safe at all: the
        budget is spent in the database, atomically, not in each worker's
        memory.
        """
        self.assertTrue(self.store.reserve_quota("opensubtitles", self.DAY, cap=2))
        self.assertEqual(self.store.quota_used("opensubtitles", self.DAY), 1)
        self.assertTrue(self.store.reserve_quota("opensubtitles", self.DAY, cap=2))
        self.assertFalse(self.store.reserve_quota("opensubtitles", self.DAY, cap=2),
                         "the third download would exceed the cap")
        self.assertEqual(self.store.quota_used("opensubtitles", self.DAY), 2)

    def test_a_reservation_of_nothing_is_not_a_refusal(self) -> None:
        """``count=0`` is "I am not about to download", not "let me download".

        Returning False here would make a caller that reserves nothing report a
        spent allowance and stop working for the rest of the day.
        """
        self.assertTrue(self.store.reserve_quota("opensubtitles", self.DAY, cap=0, count=0))
        self.assertEqual(self.store.quota_used("opensubtitles", self.DAY), 0)

    def test_a_reservation_that_cannot_be_written_is_refused(self) -> None:
        """Fail closed: an uncounted download is how an API key gets banned.

        Every other cache write in this module answers optimistically, because
        the worst case is a slower next run. This one is different - the worst
        case is exceeding a provider's daily allowance without ever recording
        it, and OpenSubtitles revokes keys that do. So a locked database means
        "no", and the movie is reported for a human instead of fetched.
        """
        self.break_statements("SELECT used FROM quota")
        self.assertFalse(self.store.reserve_quota("opensubtitles", self.DAY, cap=10))

    def test_a_reservation_that_fails_while_writing_is_refused(self) -> None:
        self.break_statements("INSERT INTO quota")
        self.assertFalse(self.store.reserve_quota("opensubtitles", self.DAY, cap=10))

    def test_a_quota_read_that_fails_reports_zero_used(self) -> None:
        """The display side may be optimistic; only the reservation may not."""
        self.store.reserve_quota("opensubtitles", self.DAY, cap=5)
        self.break_statements("SELECT used FROM quota")
        self.assertEqual(self.store.quota_used("opensubtitles", self.DAY), 0)


class OpenStateTests(unittest.TestCase):
    """``open_state`` is documented as "never raises", so it is worth pinning."""

    def test_a_database_that_is_not_a_database_becomes_the_null_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            path.write_text("this is a report, not a database", encoding="utf-8")
            store = open_state(path, tool="tests")
            self.addCleanup(store.close)
            self.assertFalse(store.enabled)
            movie = Path(tmp) / "Movie (2020).mkv"
            movie.write_bytes(b"x")
            self.assertEqual(store.see_movie(movie), path_norm(movie))
            store.record(movie, KIND_REMUX, "cleaned")
            self.assertEqual(store.verdicts(), {})

    def test_a_state_directory_that_cannot_be_created_becomes_the_null_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "a-file"
            blocked.write_text("in the way", encoding="utf-8")
            store = open_state(blocked / "nested" / "state.db", tool="tests")
            self.addCleanup(store.close)
            self.assertFalse(store.enabled)

    def test_the_environment_switch_turns_the_cache_off_entirely(self) -> None:
        """``ORGANIZE_NO_STATE`` is what the zipapp tests run under.

        An archive started on somebody else's machine must not create a state
        database in their home directory as a side effect of ``--version``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            with mock.patch.dict("os.environ", {"ORGANIZE_NO_STATE": "1"}):
                store = open_state(path, tool="tests")
            self.addCleanup(store.close)
            self.assertFalse(store.enabled)
            self.assertFalse(path.exists(), "nothing is created when the cache is off")

    def test_the_null_store_answers_every_question_the_real_one_does(self) -> None:
        """Callers never branch on whether state is available, so the surfaces match."""
        null = state_mod.NullStateStore()
        real_methods = {name for name in dir(StateStore) if not name.startswith("_")}
        null_methods = {name for name in dir(state_mod.NullStateStore) if not name.startswith("_")}
        self.assertEqual(real_methods - null_methods, set(),
                         "a caller could reach a method the null store does not have")
        self.assertTrue(null_methods)
        movie = Path("Movie (2020).mkv")
        self.assertEqual(null.see_movie(movie), path_norm(movie))
        self.assertEqual(null.see_movies([(movie, None)]), [path_norm(movie)])
        self.assertEqual(null.movies(), {})
        self.assertEqual(null.verdicts(), {})
        self.assertEqual(null.record_many([(movie, KIND_REMUX, "cleaned", "")]), 0)
        self.assertTrue(null.reserve_quota("opensubtitles", "2026-10-04", cap=0))
        self.assertEqual(null.quota_used("opensubtitles", "2026-10-04"), 0)
        self.assertEqual(null.forget_missing([]), 0)
        self.assertEqual(null.recent_events(), [])
        null.note("anything")
        null.prune_events()
        null.record(movie, KIND_REMUX, "cleaned")
        null.close()

    def test_an_explicit_path_never_consults_the_default_location(self) -> None:
        """A caller that says where the cache goes must not have it moved anyway.

        ``default_state_db`` reaches into the environment and the platform
        defaults; a tool that was given ``--state-db`` has already answered
        that question, and consulting the default too is how a test suite ends
        up writing into the developer's own ``~/.local/state``.
        """
        def tripwire() -> Path:
            raise AssertionError("the default location was consulted")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(state_mod, "default_state_db", tripwire):
                store = open_state(Path(tmp) / "state.db", tool="tests")
            self.addCleanup(store.close)
            self.assertTrue(store.enabled)
            self.assertEqual(store.path, Path(tmp) / "state.db")


if __name__ == "__main__":
    unittest.main()
