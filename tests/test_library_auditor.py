"""Tests for ``library_auditor.py`` direct-folder classification."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import library_auditor as la


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
        (folder / "Film (2000).en.srt").write_text(VALID_SRT, encoding="utf-8")
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
        (folder / "Sidecar (2004).eng.srt").write_text(VALID_SRT, encoding="utf-8")
        self.assertEqual(la.classify_folder(folder).state, "NONCANONICAL_SIDECAR")

    def test_missing_sidecar(self) -> None:
        """A canonical MKV with no English SRT is its own actionable state."""
        folder = self._movie("No Subs (2005)")
        result = la.classify_folder(folder)
        self.assertEqual(result.state, "MISSING_SIDECAR")
        self.assertIn("subtitle_fetcher", result.detail)

    def test_missing_sidecar_is_not_canonical(self) -> None:
        without = la.classify_folder(self._movie("Bare (2006)"))
        with_srt = self._movie("Covered (2007)")
        (with_srt / "Covered (2007).en.srt").write_text(VALID_SRT, encoding="utf-8")
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
        (folder / f"{name}.en.srt").write_text(body, encoding="utf-8")
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
            (folder / f"{name}.en.srt").write_text(srt_body, encoding="utf-8")
        return folder

    def test_report_lists_missing_and_invalid_together(self) -> None:
        self._movie("Bare (2008)", None)
        self._movie("Broken (2009)", "not a subtitle")
        self._movie("Covered (2010)", VALID_SRT)

        cfg = la.Config(source_dir=self.root)
        report = la.build_report(la.audit_library(cfg), cfg)

        self.assertIn("Missing Eng SRT : 1", report)
        self.assertIn("Invalid Eng SRT : 1", report)
        self.assertIn("MOVIES WITH NO USABLE EXTERNAL ENGLISH SRT", report)

        actionable = report.split("ACTIONABLE")[-1]
        self.assertIn("Bare (2008)", actionable)
        self.assertIn("Broken (2009)", actionable)
        self.assertNotIn("Covered (2010)", actionable)
