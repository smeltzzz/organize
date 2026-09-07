"""Tests for ``library_auditor.py`` direct-folder classification."""

from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from pathlib import Path

from reporttext import scorecard, section

import library_auditor as la
from organizekit import core

# A minimal but genuinely well-formed SRT. Anything shorter is not a subtitle.
VALID_SRT = "1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n"


class FolderClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="auditor_test_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _movie(self, name: str, ext: str = ".mkv") -> Path:
        folder = self.root / name
        folder.mkdir()
        (folder / f"{name}{ext}").write_bytes(b"x")
        return folder

    def test_canonical_mkv(self) -> None:
        folder = self._movie("Film (2000)")
        (folder / "Film (2000).eng.srt").write_text(VALID_SRT, encoding="utf-8")
        result = la.classify_folder(folder)
        self.assertEqual(result.state, "CANONICAL_MKV")

    def test_single_other_container(self) -> None:
        folder = self._movie("Legacy (1999)", ".AVI")
        self.assertEqual(la.classify_folder(folder).state, "SINGLE_OTHER_CONTAINER")

    def test_multiple_direct_movies(self) -> None:
        folder = self._movie("Multiple (2001)")
        (folder / "Multiple (2001).mp4").write_bytes(b"y")
        self.assertEqual(la.classify_folder(folder).state, "MULTIPLE_DIRECT_MOVIE_FILES")

    def test_no_movie_file(self) -> None:
        folder = self.root / "No Movie (2002)"
        folder.mkdir()
        (folder / "No Movie (2002).eng.srt").write_text(VALID_SRT, encoding="utf-8")
        self.assertEqual(la.classify_folder(folder).state, "NO_DIRECT_MOVIE_FILE")

    def test_stem_mismatch(self) -> None:
        folder = self.root / "Stem (2003)"
        folder.mkdir()
        (folder / "wrong-name.mkv").write_bytes(b"z")
        self.assertEqual(la.classify_folder(folder).state, "MKV_STEM_MISMATCH")

    def test_noncanonical_sidecar(self) -> None:
        folder = self._movie("Sidecar (2004)")
        # A flagged/forced English SRT is not the plain canonical .eng.srt.
        (folder / "Sidecar (2004).eng.forced.srt").write_text(VALID_SRT, encoding="utf-8")
        self.assertEqual(la.classify_folder(folder).state, "NONCANONICAL_SIDECAR")

    def test_legacy_en_srt_is_promoted(self) -> None:
        """A validated pre-cutover ``.en.srt`` is renamed to ``.eng.srt``."""
        folder = self._movie("Legacy En (2008)")
        (folder / "Legacy En (2008).en.srt").write_text(VALID_SRT, encoding="utf-8")
        result = la.classify_folder(folder)
        self.assertEqual(result.state, "CANONICAL_MKV")
        self.assertTrue((folder / "Legacy En (2008).eng.srt").is_file())
        self.assertFalse((folder / "Legacy En (2008).en.srt").exists())

    def test_missing_sidecar(self) -> None:
        """A canonical MKV with no English SRT is its own actionable state."""
        folder = self._movie("No Subs (2005)")
        result = la.classify_folder(folder)
        self.assertEqual(result.state, "MISSING_SIDECAR")
        self.assertIn("subtitle_fetcher", result.detail)

    def test_missing_sidecar_is_not_canonical(self) -> None:
        without = la.classify_folder(self._movie("Bare (2006)"))
        with_srt = self._movie("Covered (2007)")
        (with_srt / "Covered (2007).eng.srt").write_text(VALID_SRT, encoding="utf-8")
        self.assertNotEqual(without.state, "CANONICAL_MKV")
        self.assertEqual(la.classify_folder(with_srt).state, "CANONICAL_MKV")


class InvalidSidecarTests(unittest.TestCase):
    """A correctly-named sidecar whose contents are unusable must be reported.

    A filename-only audit calls this CANONICAL_MKV, which silently blocks the
    whole pipeline: subtitle_fetcher.py refuses to replace a sidecar it thinks
    is present, and mkv_track_cleaner.py will not trust it either. The movie can
    never acquire a working external subtitle and nothing says why.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="auditor_srt_test_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _movie_with_sidecar(self, name: str, body: str) -> Path:
        folder = self.root / name
        folder.mkdir()
        (folder / f"{name}.mkv").write_bytes(b"x")
        (folder / f"{name}.eng.srt").write_text(body, encoding="utf-8")
        return folder

    def _state(self, name: str, body: str) -> str:
        return la.classify_folder(self._movie_with_sidecar(name, body)).state

    def test_empty_sidecar_is_invalid(self) -> None:
        self.assertEqual(self._state("Empty (2010)", ""), "INVALID_SIDECAR")

    def test_error_page_sidecar_is_invalid(self) -> None:
        html = "<html><body>429 Too Many Requests</body></html>"
        self.assertEqual(self._state("Ratelimited (2011)", html), "INVALID_SIDECAR")

    def test_stub_sidecar_is_invalid(self) -> None:
        self.assertEqual(self._state("Stub (2012)", "sub"), "INVALID_SIDECAR")

    def test_truncated_sidecar_is_invalid(self) -> None:
        # A real index and timecode but no dialogue line is not a usable cue.
        self.assertEqual(self._state("Cut (2013)", "1\n00:00:00,000 --> "), "INVALID_SIDECAR")

    def test_valid_sidecar_is_canonical(self) -> None:
        self.assertEqual(self._state("Good (2014)", VALID_SRT), "CANONICAL_MKV")

    def test_crlf_sidecar_is_canonical(self) -> None:
        crlf = "1\r\n00:00:00,000 --> 00:00:01,000\r\nEnglish dialogue\r\n"
        self.assertEqual(self._state("Windows (2015)", crlf), "CANONICAL_MKV")

    def test_indented_cue_is_canonical(self) -> None:
        indented = "  1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n"
        self.assertEqual(self._state("Indented (2016)", indented), "CANONICAL_MKV")

    def test_invalid_sidecar_detail_names_the_remedy(self) -> None:
        result = la.classify_folder(self._movie_with_sidecar("Detail (2017)", ""))
        self.assertEqual(result.state, "INVALID_SIDECAR")
        self.assertIn("delete", result.detail.lower())
        self.assertIn("subtitle_fetcher", result.detail)


class UnusableSidecarReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="auditor_report_test_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _movie(self, name: str, srt_body: str | None) -> Path:
        folder = self.root / name
        folder.mkdir()
        (folder / f"{name}.mkv").write_bytes(b"x")
        if srt_body is not None:
            (folder / f"{name}.eng.srt").write_text(srt_body, encoding="utf-8")
        return folder

    def test_report_lists_missing_and_invalid_together(self) -> None:
        self._movie("Bare (2008)", None)
        self._movie("Broken (2009)", "not a subtitle")
        self._movie("Covered (2010)", VALID_SRT)

        cfg = la.Config(source_dir=self.root)
        report = la.build_report(la.audit_library(cfg), cfg)

        self.assertEqual(scorecard(report)["Missing Eng SRT"], 1)
        self.assertEqual(scorecard(report)["Invalid Eng SRT"], 1)
        self.assertEqual(scorecard(report)["Canonical MKV"], 1)

        actionable = section(report, "MOVIES WITH NO USABLE EXTERNAL ENGLISH SRT (ACTIONABLE)")
        self.assertIn("Bare (2008)", actionable)
        self.assertIn("Broken (2009)", actionable)
        self.assertNotIn("Covered (2010)", actionable)

        # A covered movie is still inventoried, just not called actionable.
        self.assertIn("Covered (2010)", section(report, "EVERY FOLDER CHECKED"))


class InFlightRemuxTests(unittest.TestCase):
    """mkv_track_cleaner.py stages a remux beside the movie it is rewriting."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="auditor_remux_test_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _movie(self, name: str) -> Path:
        folder = self.root / name
        folder.mkdir()
        (folder / f"{name}.mkv").write_bytes(b"x")
        return folder

    def test_staging_file_is_not_counted_as_a_second_feature(self) -> None:
        folder = self._movie("Film (2000)")
        (folder / "Film (2000).eng.srt").write_text(VALID_SRT, encoding="utf-8")
        (folder / "temp_clean_deadbeef__Film (2000).mkv").write_bytes(b"y")
        self.assertEqual(la.classify_folder(folder).state, "CANONICAL_MKV")

    def test_staging_file_alone_is_not_a_feature(self) -> None:
        folder = self.root / "Bare (2001)"
        folder.mkdir()
        (folder / "temp_clean_deadbeef__Bare (2001).mkv").write_bytes(b"y")
        self.assertEqual(la.classify_folder(folder).state, "NO_DIRECT_MOVIE_FILE")

    def test_a_real_second_feature_is_still_reported(self) -> None:
        folder = self._movie("Two (2002)")
        (folder / "Two (2002).mp4").write_bytes(b"y")
        self.assertEqual(la.classify_folder(folder).state, "MULTIPLE_DIRECT_MOVIE_FILES")

    def test_transaction_journal_is_ignored(self) -> None:
        folder = self._movie("Three (2003)")
        (folder / "Three (2003).eng.srt").write_text(VALID_SRT, encoding="utf-8")
        (folder / ".track_cleaner.deadbeef.json").write_text("{}", encoding="utf-8")
        self.assertEqual(la.classify_folder(folder).state, "CANONICAL_MKV")

    def test_a_real_movie_named_like_the_prefix_is_still_counted(self) -> None:
        # The filter is prefix-anchored, not a substring match, so a genuine
        # feature whose title merely starts with the token is not hidden.
        folder = self.root / "Temp Clean (2004)"
        folder.mkdir()
        (folder / "Temp Clean (2004).mkv").write_bytes(b"x")
        (folder / "Temp Clean (2004).eng.srt").write_text(VALID_SRT, encoding="utf-8")
        self.assertEqual(la.classify_folder(folder).state, "CANONICAL_MKV")


class ExitCodeGateTests(unittest.TestCase):
    """The exit status is what a scheduler can act on; the report is for humans."""

    def test_default_is_zero_even_with_defects(self) -> None:
        counts = Counter({"CANONICAL_MKV": 3, "MKV_STEM_MISMATCH": 1})
        self.assertEqual(la.exit_code_for(counts, la.Config()), 0)

    def test_fail_on_defects_flags_layout_problems(self) -> None:
        counts = Counter({"CANONICAL_MKV": 3, "MKV_STEM_MISMATCH": 1})
        self.assertEqual(la.exit_code_for(counts, la.Config(fail_on_defects=True)), 1)

    def test_missing_sidecar_is_not_a_defect(self) -> None:
        # A freshly standardized movie has no sidecar until the fetcher runs,
        # so counting it would make the gate fail on every healthy new library.
        counts = Counter({"CANONICAL_MKV": 3, "MISSING_SIDECAR": 2})
        self.assertEqual(la.exit_code_for(counts, la.Config(fail_on_defects=True)), 0)
        self.assertEqual(la.exit_code_for(counts, la.Config(fail_on_findings=True)), 1)

    def test_invalid_sidecar_is_a_defect(self) -> None:
        counts = Counter({"CANONICAL_MKV": 3, "INVALID_SIDECAR": 1})
        self.assertEqual(la.exit_code_for(counts, la.Config(fail_on_defects=True)), 1)

    def test_clean_library_passes_both_gates(self) -> None:
        counts = Counter({"CANONICAL_MKV": 5})
        cfg = la.Config(fail_on_defects=True, fail_on_findings=True)
        self.assertEqual(la.exit_code_for(counts, cfg), 0)

    def test_every_defect_state_is_covered(self) -> None:
        for state in la.DEFECT_STATES:
            counts = Counter({state: 1})
            self.assertEqual(
                la.exit_code_for(counts, la.Config(fail_on_defects=True)), 1,
                f"{state} should trip --fail-on-defects",
            )


if __name__ == "__main__":
    unittest.main()


class ParallelAuditIsTheSameAudit(unittest.TestCase):
    """The audit reads thousands of folders; on a network share that is a
    round trip each. Folders are therefore classified in parallel - but the
    audit is the library's official verdict, so the worker count must not be
    able to change a single character of it.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="auditor_parallel_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        # A deliberately mixed library: canonical, missing sidecar, invalid
        # sidecar, wrong container, and an empty folder.
        for index in range(24):
            name = f"Film {index:02d} (20{index:02d})"
            folder = self.root / name
            folder.mkdir()
            if index % 5 == 4:
                continue  # empty folder
            extension = ".avi" if index % 7 == 6 else ".mkv"
            (folder / f"{name}{extension}").write_bytes(b"x")
            if index % 3 == 0:
                (folder / f"{name}.eng.srt").write_text(VALID_SRT, encoding="utf-8")
            elif index % 3 == 1:
                (folder / f"{name}.eng.srt").write_text("not a subtitle", encoding="utf-8")

    def _states(self, workers: int) -> list[tuple[str, str]]:
        cfg = la.Config(source_dir=self.root, workers=workers)
        audit = la.audit_library(cfg)
        return [(item.folder.name, item.state) for item in audit.folders]

    def test_worker_count_cannot_change_the_verdict(self) -> None:
        serial = self._states(1)
        self.assertEqual(24, len(serial))
        for workers in (2, 4, 8):
            with self.subTest(workers=workers):
                self.assertEqual(serial, self._states(workers),
                                 "the audit must not depend on how it was scheduled")

    def test_results_stay_in_folder_order(self) -> None:
        """The report is a numbered list; parallelism must not shuffle it."""
        names = [name for name, _state in self._states(8)]
        self.assertEqual(sorted(names, key=str.casefold), names)

    def test_negative_workers_is_a_config_error(self) -> None:
        cfg = la.Config(source_dir=self.root, workers=-1,
                        log_file=self.root.parent / "a.log",
                        report_file=self.root.parent / "a.txt")
        self.assertTrue(any("--workers" in error for error in la.validate_config(cfg)))


class StateCacheTests(unittest.TestCase):
    """The verdicts the audit publishes for ``organize status`` to read.

    The audit itself is unchanged by any of this: the cache is written after
    the report exists, is never read back, and a failure to write it is a
    logged warning and nothing more.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="auditor_state_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.library = self.root / "lib"
        self.library.mkdir()
        self.db = self.root / "state.db"

    def _movie(self, title: str, *, sidecar: str | None = None) -> Path:
        folder = self.library / title
        folder.mkdir(parents=True, exist_ok=True)
        movie = folder / f"{title}.mkv"
        movie.write_bytes(b"x" * 512)
        if sidecar is not None:
            (folder / sidecar).write_text(VALID_SRT, encoding="utf-8")
        return movie

    def _publish(self, **overrides: object) -> tuple[int, dict]:
        settings: dict = {"source_dir": self.library, "state_db": self.db}
        settings.update(overrides)
        cfg = la.Config(**settings)
        published = la.publish_state(la.audit_library(cfg), cfg)
        store = core.open_state(self.db, tool="tests")
        try:
            return published, store.verdicts()
        finally:
            store.close()

    def test_layout_and_subtitle_verdicts_are_recorded_separately(self) -> None:
        movie = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        published, verdicts = self._publish()
        key = core.path_norm(movie)
        self.assertEqual(published, 1)
        self.assertEqual(verdicts[(key, core.KIND_LAYOUT)].verdict, "CANONICAL_MKV")
        self.assertEqual(verdicts[(key, core.KIND_SUBTITLE)].verdict, "present")

    def test_a_missing_sidecar_is_published_as_missing(self) -> None:
        movie = self._movie("Bravo (2002)")
        _published, verdicts = self._publish()
        self.assertEqual(verdicts[(core.path_norm(movie), core.KIND_SUBTITLE)].verdict,
                         "missing")

    def test_every_audit_state_maps_to_at_most_one_subtitle_state(self) -> None:
        # The auditor owns this vocabulary; `organize status` reads it rather
        # than guessing, so an unmapped state must mean "no subtitle claim".
        self.assertEqual(set(la.SUBTITLE_STATE_FOR_AUDIT.values()),
                         {"present", "missing", "invalid", "noncanonical"})
        for state in la.SUBTITLE_STATE_FOR_AUDIT:
            self.assertIsInstance(state, str)

    def test_a_deleted_movie_is_forgotten(self) -> None:
        movie = self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self._publish()
        for path in sorted(movie.parent.iterdir()):
            path.unlink()
        movie.parent.rmdir()
        _published, verdicts = self._publish()
        self.assertEqual(verdicts, {})

    def test_no_state_publishes_nothing(self) -> None:
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        published, verdicts = self._publish(use_state=False)
        self.assertEqual(published, 0)
        self.assertEqual(verdicts, {})

    def test_an_unusable_cache_never_fails_the_audit(self) -> None:
        self._movie("Alpha (2001)", sidecar="Alpha (2001).eng.srt")
        self.db.write_bytes(b"not a database" * 50)
        published, _verdicts = self._publish()
        self.assertEqual(published, 0)

    def test_a_state_db_inside_the_library_is_a_config_error(self) -> None:
        cfg = la.Config(source_dir=self.library, state_db=self.library / "state.db",
                        log_file=self.root / "a.log", report_file=self.root / "a.txt")
        self.assertTrue(any("state" in error.casefold() for error in la.validate_config(cfg)))
