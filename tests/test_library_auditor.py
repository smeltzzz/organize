"""Tests for ``library_auditor.py`` direct-folder classification."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import library_auditor as la


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
        (folder / "Film (2000).en.srt").write_text("sub", encoding="utf-8")
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
        (folder / "No Movie (2002).eng.srt").write_text("sub", encoding="utf-8")
        self.assertEqual(la.classify_folder(folder).state, "NO_DIRECT_MOVIE_FILE")

    def test_stem_mismatch(self) -> None:
        folder = self.root / "Stem (2003)"
        folder.mkdir()
        (folder / "wrong-name.mkv").write_bytes(b"z")
        self.assertEqual(la.classify_folder(folder).state, "MKV_STEM_MISMATCH")

    def test_noncanonical_sidecar(self) -> None:
        folder = self._movie("Sidecar (2004)")
        (folder / "Sidecar (2004).eng.srt").write_text("sub", encoding="utf-8")
        self.assertEqual(la.classify_folder(folder).state, "NONCANONICAL_SIDECAR")


if __name__ == "__main__":
    unittest.main()
