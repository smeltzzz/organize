"""Regression tests for the defects the 2026-09-30 correctness sweep fixed.

Every test here pins a bug that shipped and was found by reading the code and
fuzzing it, not by an example anybody had thought to write. They are grouped
by the shared property they defend, because the property is the reason to keep
them, not the individual call site.

The three running themes:

* **A renderer is never allowed to fail.** ``format_duration``,
  ``format_size`` and ``format_left`` exist to turn a number into text for a
  report that is written *after* the work is done. Every one of them used to
  raise ``ValueError``/``OverflowError`` on a NaN or an infinity - and those
  are reachable, because ``json.loads`` accepts the non-standard ``NaN`` and
  ``Infinity`` literals and hands back real floats.
* **A resolver is stated once.** ``organize.py`` kept an inline copy of the
  library-root precedence rules and drifted from the shared one.
* **A cache write is all-or-nothing and all-at-once.** The transaction helper
  leaked an open transaction, and the batch writers opened one per row.
"""

from __future__ import annotations

import errno
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import audio_standardizer as aus  # noqa: E402
import bitdepth  # noqa: E402
import mkv_track_cleaner as tc  # noqa: E402
import organize  # noqa: E402
from organizekit.core import config, state, subtitles  # noqa: E402
from organizekit.core.live import format_left  # noqa: E402

SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n"

#: Every non-finite float, plus the shapes that reach a formatter from JSON.
NON_FINITE = (float("nan"), float("inf"), float("-inf"))
HOSTILE_SCALARS = NON_FINITE + (None, "", " ", "abc", "N/A", "1e400", -1, 0, 2**70)


# =============================================================================
# A renderer must not be able to fail
# =============================================================================

class RenderersAreTotal(unittest.TestCase):
    """Formatting a number may not raise, whatever the number is.

    Each of these three helpers used to die on a non-finite input:
    ``mkv_track_cleaner.format_duration(inf)`` and ``format_size(nan)`` both
    reached an ``int()`` conversion; ``bitdepth.format_duration`` did the same
    and additionally raised ``TypeError`` on a non-numeric field; and
    ``format_left`` did too. All three are called while building a report for
    a run that has already done its work, so each was a way to lose a finished
    sweep over a probe payload that lied.
    """

    def test_cleaner_duration_survives_non_finite(self) -> None:
        for value in NON_FINITE:
            with self.subTest(value=value):
                self.assertEqual(tc.format_duration(value), "0s")
        # A negative elapsed time is equally meaningless.
        self.assertEqual(tc.format_duration(-5), "0s")
        # ... and the ordinary values are untouched.
        self.assertEqual(tc.format_duration(0), "0.0s")
        self.assertEqual(tc.format_duration(9.5), "9.5s")
        self.assertEqual(tc.format_duration(3600), "1h 00m 00s")
        self.assertEqual(tc.format_duration(86399), "23h 59m 59s")

    def test_cleaner_size_survives_non_finite(self) -> None:
        for value in NON_FINITE:
            with self.subTest(value=value):
                self.assertEqual(tc.format_size(value), "0 Bytes")
        self.assertEqual(tc.format_size(-1), "0 Bytes")
        self.assertEqual(tc.format_size(0), "0 Bytes")
        self.assertEqual(tc.format_size(1), "1 Bytes")
        self.assertEqual(tc.format_size(1024), "1.00 KB")
        # An infinity used to render as the literal text "inf TB".
        self.assertNotIn("inf", tc.format_size(float("inf")))

    def test_bitdepth_duration_survives_non_finite_and_junk(self) -> None:
        for value in NON_FINITE:
            with self.subTest(value=value):
                self.assertEqual(bitdepth.format_duration(value), "-")
        # ffprobe's duration is a *string* field, so a non-numeric one is
        # ordinary input, not an exotic one.
        for value in ("", " ", "N/A", "abc", "1e400", None):
            with self.subTest(value=value):
                self.assertEqual(bitdepth.format_duration(value), "-")
        self.assertEqual(bitdepth.format_duration(0), "-")
        self.assertEqual(bitdepth.format_duration(0.4), "0:00")
        self.assertEqual(bitdepth.format_duration(59.6), "1:00")
        self.assertEqual(bitdepth.format_duration(3600), "1:00:00")

    def test_format_left_survives_non_finite(self) -> None:
        for value in NON_FINITE:
            with self.subTest(value=value):
                self.assertEqual(format_left(value), "0s")
        self.assertEqual(format_left("nonsense"), "0s")
        self.assertEqual(format_left(-5), "0s")
        self.assertEqual(format_left(0), "0s")
        self.assertEqual(format_left(45), "45s")
        self.assertEqual(format_left(7500), "2h05m")

    def test_the_non_finite_values_are_really_reachable_from_json(self) -> None:
        """The guard is not paranoia: this is the input that reaches it."""
        import json

        payload = json.loads('{"format": {"duration": NaN}, "streams": [{"size": Infinity}]}')
        self.assertTrue(math.isnan(payload["format"]["duration"]))
        self.assertEqual(payload["streams"][0]["size"], float("inf"))
        # And the old code really did raise on exactly these.
        with self.assertRaises((ValueError, OverflowError)):
            int(round(payload["format"]["duration"]))
        with self.assertRaises((ValueError, OverflowError)):
            int(payload["streams"][0]["size"])

    def test_every_ordinary_value_still_formats(self) -> None:
        """A guard that swallowed real values would pass the tests above."""
        for value in (0, 1, 7, 59, 60, 3599, 3600, 86400, 90061.5):
            self.assertIsInstance(tc.format_duration(value), str)
            self.assertIsInstance(bitdepth.format_duration(value), str)
            self.assertIsInstance(format_left(value), str)
        for value in (1, 1023, 1024, 1024 ** 2, 1024 ** 3, 1024 ** 4, 5 * 1024 ** 4):
            self.assertRegex(tc.format_size(value), r"^[\d.]+ (Bytes|KB|MB|GB|TB)$")


# =============================================================================
# The library-root resolver is stated once
# =============================================================================

class TheLibraryRootIsResolvedInOnePlace(unittest.TestCase):
    """``organize.py`` re-implemented the precedence rules and drifted.

    It reached the shared resolver by ``import bitdepth`` at call time - a
    full import of a 1,300-line sibling, on a path that had already imported
    ``organizekit.core`` - and carried an inline copy as a fallback. That copy
    accepted a whitespace-only ``--source`` where the shared resolver falls
    through to ``ORGANIZE_LIBRARY``, and it never called ``load_dotenv()``, so
    a ``.env`` silently stopped working whenever that import failed.
    """

    def test_it_agrees_with_the_shared_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ORGANIZE_LIBRARY", None)
                os.environ.pop("MOVIE_STD_TARGET", None)
                for explicit in (None, Path(root / "explicit"), Path("   ")):
                    with self.subTest(explicit=explicit):
                        self.assertEqual(
                            organize._resolve_library_path(explicit),
                            config.resolve_library(explicit),
                        )

    def test_an_explicit_path_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            chosen = Path(td) / "chosen"
            with mock.patch.dict(os.environ, {"ORGANIZE_LIBRARY": "/from/env"}):
                self.assertEqual(organize._resolve_library_path(chosen), chosen)

    def test_it_does_not_import_a_tool_sibling_to_do_it(self) -> None:
        """The whole point of the shared core is that this is not a sibling call."""
        source = (REPO / "organize.py").read_text(encoding="utf-8")
        body = source.split("def _resolve_library_path", 1)[1].split("def ", 1)[0]
        self.assertNotIn("import bitdepth", body,
                         "the library root must come from organizekit.core, not "
                         "from importing a whole tool at call time")

    def test_a_dotenv_is_honoured_by_the_cli_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".env").write_text("ORGANIZE_LIBRARY=/from/dotenv\n", encoding="utf-8")
            (root / "lib").mkdir()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ORGANIZE_LIBRARY", None)
                with mock.patch.object(Path, "cwd", staticmethod(lambda: root)):
                    self.assertEqual(
                        organize._resolve_library_path(None), Path("/from/dotenv")
                    )
            os.environ.pop("ORGANIZE_LIBRARY", None)


class DotenvCandidatesCoverBothShippedLayouts(unittest.TestCase):
    """The installation-root candidate pointed one directory too high.

    ``_dotenv_candidates`` asked for ``Path(__file__).resolve().parents[3]``,
    written for a ``src/`` tree. This package ships as
    ``<root>/organizekit/core/config.py``, so ``parents[3]`` is the directory
    *above* the checkout: the repository root was never a candidate at all,
    and the documented "the repository root for a clone" behaviour only
    worked by accident when the tool happened to be launched from there.
    """

    def test_the_repository_root_is_a_candidate(self) -> None:
        candidates = config._dotenv_candidates()
        self.assertIn(REPO / ".env", candidates,
                      "a .env at the repository root must be found from a clone")

    def test_the_zipapp_directory_is_still_a_candidate(self) -> None:
        """In the single-file build ``parents[2]`` is the archive itself."""
        import organizekit.core.config as cfg

        fake = REPO / "dist" / "organize.pyz" / "organizekit" / "core" / "config.py"
        with mock.patch.object(cfg, "__file__", str(fake)):
            candidates = cfg._dotenv_candidates()
        self.assertIn(REPO / "dist" / "organize.pyz" / ".env", candidates)
        self.assertIn(REPO / "dist" / ".env", candidates)

    def test_candidates_are_unique_and_ordered(self) -> None:
        candidates = config._dotenv_candidates()
        self.assertEqual(len(candidates), len(set(candidates)))
        self.assertIn(Path.cwd() / ".env", candidates)


# =============================================================================
# A cache write is all-or-nothing
# =============================================================================

class StateTransactionsAlwaysClose(unittest.TestCase):
    """``_write`` rolled back only for a ``sqlite3.Error``.

    Any other exception out of the body unwound the generator with the
    transaction still open. The next ``BEGIN IMMEDIATE`` then failed with
    "cannot start a transaction within a transaction", the guard swallowed it,
    and every later write silently ran outside a transaction - a cache that
    quietly stopped being atomic without ever saying so.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = state.StateStore(Path(self._tmp.name) / "state.db", tool="t")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_a_foreign_exception_closes_the_transaction(self) -> None:
        with self.assertRaises(ValueError), self.store._write() as db:
            db.execute("INSERT INTO movie (path_key,path,folder) VALUES ('a','a','a')")
            raise ValueError("caller bug")
        self.assertFalse(self.store._db.in_transaction)
        # And the store is still usable afterwards.
        self.store.note("still", "working")
        self.assertFalse(self.store._db.in_transaction)

    def test_a_failed_begin_does_not_commit_an_enclosing_transaction(self) -> None:
        """The two failure paths must stay separate, or nesting corrupts it."""
        with self.store._write() as outer:
            outer.execute("INSERT INTO movie (path_key,path,folder) VALUES ('o','o','o')")
            with self.store._write() as inner:
                inner.execute("INSERT INTO movie (path_key,path,folder) VALUES ('i','i','i')")
            self.assertTrue(
                self.store._db.in_transaction,
                "an inner _write committed the outer transaction out from under it",
            )
        self.assertFalse(self.store._db.in_transaction)
        self.assertEqual(sorted(self.store.movies()), ["i", "o"])

    def test_a_rolled_back_batch_leaves_nothing_behind(self) -> None:
        with self.assertRaises(RuntimeError), self.store._write() as db:
            db.execute("INSERT INTO movie (path_key,path,folder) VALUES ('x','x','x')")
            raise RuntimeError("fail")
        self.assertEqual(self.store.movies(), {})


class BatchedStateWrites(unittest.TestCase):
    """One transaction per row, for a run's worth of rows.

    ``forget_missing`` opened a ``BEGIN IMMEDIATE``/``COMMIT`` pair per
    removed movie and ``record_many`` looped through ``record``, so publishing
    an audit of a 3,000-movie library meant thousands of separate transactions
    where three batched statements do the same work. The batching must not
    change a single stored value.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = state.StateStore(self.root / "state.db", tool="audit")
        self.movies = []
        for index in range(12):
            movie = self.root / f"Movie {index} (2020)" / "Movie (2020).mkv"
            movie.parent.mkdir()
            movie.write_bytes(b"x" * 100)
            self.movies.append(movie)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_record_many_stores_every_verdict(self) -> None:
        rows = [(m, state.KIND_LAYOUT, "CANONICAL_MKV", "ok") for m in self.movies]
        self.assertEqual(self.store.record_many(rows), len(rows))
        stored = self.store.verdicts(state.KIND_LAYOUT)
        self.assertEqual(len(stored), len(rows))
        for movie in self.movies:
            key = state.path_norm(movie)
            # verdicts() is keyed by (path_key, kind).
            self.assertIn((key, state.KIND_LAYOUT), stored)
            verdict = stored[(key, state.KIND_LAYOUT)]
            self.assertEqual(verdict.verdict, "CANONICAL_MKV")
            self.assertEqual(verdict.detail, "ok")
            # The size/mtime stamp is taken from the file, exactly as record().
            self.assertEqual(verdict.size, 100)
            self.assertTrue(verdict.is_current_for(100, movie.stat().st_mtime_ns))

    def test_record_many_is_idempotent(self) -> None:
        rows = [(self.movies[0], state.KIND_REMUX, "cleaned", "")]
        self.store.record_many(rows)
        self.store.record_many(rows)
        self.assertEqual(len(self.store.verdicts(state.KIND_REMUX)), 1)

    def test_record_many_twice_beats_record_one_at_a_time(self) -> None:
        rows = [(self.movies[0], state.KIND_REMUX, "cleaned", "")]
        self.store.record_many(rows)
        batched = self.store.verdicts(state.KIND_REMUX)
        other = state.StateStore(self.root / "other.db", tool="audit")
        try:
            for row in rows:
                other.record(*row)
            self.assertEqual(
                {(k, v.verdict, v.size) for k, v in batched.items()},
                {(k, v.verdict, v.size) for k, v in other.verdicts().items()},
            )
        finally:
            other.close()

    def test_see_movies_matches_see_movie(self) -> None:
        pairs = [(m, m.parent) for m in self.movies]
        keys = self.store.see_movies(pairs)
        self.assertEqual(len(keys), len(pairs))
        self.assertEqual(keys, [state.path_norm(m) for m, _ in pairs])
        self.store.see_movies(pairs)  # idempotent
        self.assertEqual(len(self.store.movies()), len(pairs))
        stored = self.store.movies()
        for movie in self.movies:
            row = stored[state.path_norm(movie)]
            self.assertEqual(row.path, str(movie))
            self.assertEqual(row.folder, str(movie.parent))
            self.assertEqual(row.size, 100)
            self.assertEqual(row.nlink, 1)

    def test_see_movies_keeps_first_seen_and_moves_last_seen(self) -> None:
        key = state.path_norm(self.movies[0])
        self.store.see_movies([(self.movies[0], self.movies[0].parent)])
        first = self.store.movies()[key].first_seen
        self.store.see_movies([(self.movies[0], self.movies[0].parent)])
        again = self.store.movies()[key]
        self.assertEqual(again.first_seen, first)
        self.assertEqual(again.path, str(self.movies[0]))

    def test_forget_missing_removes_movies_and_their_verdicts(self) -> None:
        self.store.see_movies([(m, m.parent) for m in self.movies])
        self.store.record_many([(m, state.KIND_LAYOUT, "CANONICAL_MKV", "") for m in self.movies])
        keep, gone = self.movies[0], self.movies[1:]
        self.assertEqual(self.store.forget_missing([state.path_norm(keep)]), len(gone))
        self.assertEqual(list(self.store.movies()), [state.path_norm(keep)])
        self.assertEqual(
            list(self.store.verdicts()),
            [(state.path_norm(keep), state.KIND_LAYOUT)],
            "a dropped movie must not leave its verdicts behind",
        )

    def test_forget_missing_on_a_matching_library_removes_nothing(self) -> None:
        self.store.see_movies([(m, m.parent) for m in self.movies])
        self.assertEqual(
            self.store.forget_missing([state.path_norm(m) for m in self.movies]), 0
        )
        self.assertEqual(len(self.store.movies()), len(self.movies))

    def test_the_null_store_answers_the_batch_api_too(self) -> None:
        """A caller must never branch on whether state is available."""
        null = state.NullStateStore()
        self.assertEqual(null.record_many([(Path("/x"), "k", "v", "")]), 0)
        self.assertEqual(null.see_movies([(Path("/x"), None)]), [state.path_norm("/x")])
        self.assertEqual(null.forget_missing(["a"]), 0)

    def test_a_missing_file_does_not_break_a_batch(self) -> None:
        ghost = self.root / "Nope (1999)" / "Nope (1999).mkv"
        rows = [(self.movies[0], state.KIND_LAYOUT, "CANONICAL_MKV", ""),
                (ghost, state.KIND_LAYOUT, "CANONICAL_MKV", "")]
        self.assertEqual(self.store.record_many(rows), 2)
        self.assertEqual(self.store.see_movies([(ghost, ghost.parent)]),
                         [state.path_norm(ghost)])


class TheAuditorPublishesInBatches(unittest.TestCase):
    """``publish_state`` is three writes per movie; it should be three writes."""

    def test_the_auditor_records_the_same_things_either_way(self) -> None:
        import io

        import library_auditor as la

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # One of each: a complete folder, one missing its sidecar, and one
            # whose sidecar is present but unusable.
            for index, sidecar in enumerate((SRT, None, "not a subtitle")):
                folder = root / f"Movie {index} (2020)"
                folder.mkdir()
                (folder / f"Movie {index} (2020).mkv").write_bytes(b"x")
                if sidecar is not None:
                    (folder / f"Movie {index} (2020).eng.srt").write_text(
                        sidecar, encoding="utf-8"
                    )
            cfg = la.Config(source_dir=root, workers=1, use_state=True,
                            state_db=root / "state.db")
            audit = la.audit_library(cfg)
            self.assertEqual(
                [item.state for item in audit.folders],
                ["CANONICAL_MKV", "MISSING_SIDECAR", "INVALID_SIDECAR"],
            )
            la.log.stream = io.StringIO()
            la.log.file = None
            self.assertEqual(la.publish_state(audit, cfg), 3)

            with state.open_state(root / "state.db", tool="check") as store:
                self.assertEqual(len(store.movies()), 3)
                layout = store.verdicts(state.KIND_LAYOUT)
                subtitle = store.verdicts(state.KIND_SUBTITLE)
                self.assertEqual(len(layout), 3)
                # All three of these states have a subtitle answer, so the
                # batched publish must write all three.
                self.assertEqual(len(subtitle), 3)
                self.assertEqual(
                    sorted(v.verdict for v in subtitle.values()),
                    ["invalid", "missing", "present"],
                )
                for verdict in layout.values():
                    self.assertEqual(verdict.size, 1)


# =============================================================================
# A published sidecar is never clobbered
# =============================================================================

class SidecarPromotionIsAtomic(unittest.TestCase):
    """``promote_legacy_external_english_srt`` promised never to overwrite.

    It published with ``os.replace``, which overwrites unconditionally, and
    the existence check in front of it was not atomic with the publish. The
    extractor holds a ``CoordinationLock`` on the library while the auditor
    holds only its own per-directory run lock, so a freshly-extracted sidecar
    really can land in that window - and it used to be silently destroyed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.movie = self.root / "Movie (2020).mkv"
        self.movie.write_bytes(b"x")
        self.legacy = self.root / "Movie (2020).en.srt"
        self.canonical = self.root / "Movie (2020).eng.srt"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_legacy_sidecar_is_promoted(self) -> None:
        self.legacy.write_text(SRT, encoding="utf-8")
        promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual(promoted, self.canonical)
        self.assertEqual(reason, "")
        self.assertTrue(self.canonical.is_file())
        self.assertFalse(self.legacy.exists(), "the legacy name must be gone")
        self.assertEqual(self.canonical.read_text(encoding="utf-8"), SRT)

    def test_promotion_is_idempotent(self) -> None:
        self.legacy.write_text(SRT, encoding="utf-8")
        subtitles.promote_legacy_external_english_srt(self.movie)
        promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual(promoted, self.canonical)
        self.assertEqual(reason, "")

    def test_a_concurrently_published_sidecar_is_never_destroyed(self) -> None:
        """The bug: the loser's sidecar was overwritten by the promotion."""
        self.legacy.write_text(SRT, encoding="utf-8")
        winner = "1\n00:00:09,000 --> 00:00:11,000\nThe other one.\n\n"

        def racing_link(_src, dst):  # noqa: ANN001 - os.link's shape
            Path(dst).write_text(winner, encoding="utf-8")
            raise FileExistsError(errno.EEXIST, "lost the race", str(dst))

        with mock.patch.object(subtitles.os, "link", racing_link):
            promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)

        self.assertIsNone(promoted)
        self.assertIn("concurrently", reason)
        self.assertEqual(self.canonical.read_text(encoding="utf-8"), winner,
                         "the concurrently published sidecar must survive intact")
        self.assertTrue(self.legacy.exists(), "nothing was consumed, so the legacy stays")

    def test_an_existing_canonical_is_left_alone(self) -> None:
        self.legacy.write_text(SRT, encoding="utf-8")
        self.canonical.write_text(SRT, encoding="utf-8")
        promoted, _reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual(promoted, self.canonical)
        self.assertTrue(self.legacy.exists())

    def test_a_filesystem_without_hardlinks_still_promotes(self) -> None:
        """FAT32/exFAT/SMB have no hard links; the fallback must still work."""
        self.legacy.write_text(SRT, encoding="utf-8")

        def unsupported(_src, _dst):  # noqa: ANN001
            raise OSError(errno.ENOTSUP, "hard links not supported")

        with mock.patch.object(subtitles.os, "link", unsupported):
            promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual(promoted, self.canonical)
        self.assertEqual(reason, "")
        self.assertFalse(self.legacy.exists())

    def test_the_fallback_still_refuses_an_occupied_destination(self) -> None:
        self.legacy.write_text(SRT, encoding="utf-8")
        self.canonical.write_text(SRT, encoding="utf-8")

        def unsupported(_src, _dst):  # noqa: ANN001
            raise OSError(errno.ENOTSUP, "hard links not supported")

        with mock.patch.object(subtitles.os, "link", unsupported):
            _promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertNotIn("could not promote", reason)

    def test_a_real_error_is_reported_not_swallowed(self) -> None:
        self.legacy.write_text(SRT, encoding="utf-8")

        def exploding(_src, _dst):  # noqa: ANN001
            raise OSError(errno.EIO, "disk on fire")

        with mock.patch.object(subtitles.os, "link", exploding):
            promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertIn("could not promote", reason)
        self.assertTrue(self.legacy.exists())

    def test_an_unusable_legacy_sidecar_is_never_promoted(self) -> None:
        self.legacy.write_text("not a subtitle at all", encoding="utf-8")
        promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertIn("unusable", reason)
        self.assertFalse(self.canonical.exists())


# =============================================================================
# Crash debris
# =============================================================================

class SafeDeleteRemovesDanglingSymlinks(unittest.TestCase):
    """``safe_delete`` guarded on ``Path.exists()``, which follows symlinks.

    A dangling symlink is exactly the shape a half-finished transaction leaves
    behind, and ``exists()`` is ``False`` for one - so the guard turned "delete
    it if it is there" into "never delete a broken symlink", and the artifact
    was re-reported as an orphan on every subsequent run.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_dangling_symlink_is_removed(self) -> None:
        dangling = self.root / "temp_clean_deadbeef__Movie.mkv"
        dangling.symlink_to(self.root / "never-existed")
        self.assertFalse(dangling.exists(), "the fixture must actually be dangling")
        self.assertTrue(dangling.is_symlink())
        tc.safe_delete(dangling)
        self.assertFalse(dangling.is_symlink())
        self.assertFalse(dangling.exists())

    def test_a_regular_file_and_a_missing_path_are_both_tolerated(self) -> None:
        real = self.root / "real.bin"
        real.write_text("x")
        tc.safe_delete(real)
        self.assertFalse(real.exists())
        tc.safe_delete(self.root / "never-existed.bin")  # must not raise

    def test_a_directory_is_still_not_removed(self) -> None:
        """`unlink` on a directory is an OSError, which is swallowed: unchanged."""
        folder = self.root / "temp_clean_x__Movie.mkv"
        folder.mkdir()
        tc.safe_delete(folder)
        self.assertTrue(folder.is_dir(), "safe_delete must not recurse into directories")


# =============================================================================
# One resolver for the worker count
# =============================================================================

class WorkerCountsAreDecidedInOnePlace(unittest.TestCase):
    """``audio_standardizer`` expanded ``--workers 0`` inline, uncapped.

    It used ``os.cpu_count() or 4``, ignoring the cap every other tool applies,
    so on a 16- or 64-core host the value in the config was the raw core count
    and the tool's own ``--workers ... max 8`` help text was untrue. The limit
    was only applied later, by accident, where the run happened to call
    ``resolve_workers`` again.
    """

    def test_zero_means_the_capped_shared_default(self) -> None:
        self.assertEqual(
            aus.resolve_workers(0, cap=aus.MAX_CPU_WORKERS),
            min(max(1, (os.cpu_count() or 2) // 2), aus.MAX_CPU_WORKERS),
        )

    def test_the_cap_is_actually_applied(self) -> None:
        self.assertEqual(aus.resolve_workers(10_000, cap=aus.MAX_CPU_WORKERS),
                         aus.MAX_CPU_WORKERS)
        self.assertEqual(aus.resolve_workers(1, cap=aus.MAX_CPU_WORKERS), 1)

    def test_the_argparse_path_does_not_build_an_uncapped_config(self) -> None:
        source = (REPO / "audio_standardizer.py").read_text(encoding="utf-8")
        self.assertIn("resolve_workers(args.workers, cap=MAX_CPU_WORKERS)", source,
                      "the worker default must go through resolve_workers, "
                      "which is where the cap lives")
        self.assertNotIn(
            "os.cpu_count() or 4",
            "\n".join(line for line in source.splitlines()
                      if not line.lstrip().startswith("#")),
            "the uncapped inline expansion must be gone from the code, not just "
            "from the comment that explains why it was",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
