"""The pin that keeps the host toolchain out of the suite has to actually hold.

``tests/hermetic.py`` exists because a dozen tests asked the machine what was
installed and took a different branch depending on the answer - which is why the
suite was green on nine CI runners that have nothing installed while failing on
a workstation that has everything. A guard nobody tests is a guard that rots:
if a tool grows a second resolver, or a check stops going through the one that
is pinned, the leak comes back silently and CI stays green.

So these assert the two properties the guard depends on, on any machine:

* with the pin held, every toolchain lookup answers "not installed";
* the pin is surgical - an unrelated program is still found, and the pin does
  not outlive the block it was taken in.

Each test builds its own fake program on a private PATH rather than asking what
the host happens to have, so the assertions mean the same thing everywhere.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

import hermetic

import bitdepth
import mkv_track_cleaner
import movie_standardizer
import subtitle_extractor
import sync_subtitles


def _program(directory: Path, name: str) -> Path:
    """A real executable named ``name`` in ``directory``, whatever the platform."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # shutil.which honours PATHEXT there, so the name has to carry it.
        program = directory / f"{name}.bat"
        program.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
        return program
    program = directory / name
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(program.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return program


class ThePinHoldsTests(unittest.TestCase):
    def test_every_toolchain_lookup_answers_not_installed(self) -> None:
        """The whole point: one answer, on a bare runner and on a workstation."""
        with hermetic.no_media_tools():
            with self.assertRaises(FileNotFoundError):
                mkv_track_cleaner.resolve_mkvmerge_path()
            self.assertIsNone(subtitle_extractor.find_mkvtoolnix_binary("mkvextract"))
            self.assertIsNone(subtitle_extractor.find_mkvtoolnix_binary("mkvmerge"))
            self.assertIsNone(sync_subtitles.find_ffsubsync())
            self.assertIsNone(bitdepth.find_ffprobe())
            self.assertFalse(bitdepth.ffprobe_works("/nonexistent/ffprobe"))
            self.assertIsNone(movie_standardizer.find_ffprobe())

    def test_a_standard_install_location_is_not_a_back_door(self) -> None:
        """MKVToolNix on Windows lives in Program Files and is never on PATH.

        Pinning ``shutil.which`` alone would leave the tools' own search of the
        standard install locations wide open, which is the exact drift the
        toolchain module documents.
        """
        with tempfile.TemporaryDirectory() as td:
            fake = _program(Path(td), "mkvmerge")
            saved = mkv_track_cleaner.KNOWN_MKVMERGE_PATHS
            mkv_track_cleaner.KNOWN_MKVMERGE_PATHS = [str(fake)]
            try:
                with hermetic.no_media_tools(), self.assertRaises(
                        FileNotFoundError,
                        msg="a real install in a standard location leaked through the pin"):
                    mkv_track_cleaner.resolve_mkvmerge_path()
            finally:
                mkv_track_cleaner.KNOWN_MKVMERGE_PATHS = saved


class ThePinIsSurgicalTests(unittest.TestCase):
    def test_an_unrelated_program_is_still_found(self) -> None:
        """The pin must not blind the suite to every program on the machine."""
        with tempfile.TemporaryDirectory() as td:
            program = _program(Path(td), "not_a_media_tool")
            saved = os.environ.get("PATH")
            os.environ["PATH"] = f"{td}{os.pathsep}{saved}"
            self.addCleanup(lambda: os.environ.__setitem__("PATH", saved))
            with hermetic.no_media_tools():
                self.assertEqual(shutil.which("not_a_media_tool"), str(program))

    def test_a_media_tool_on_the_path_is_invisible_while_pinned(self) -> None:
        """The same PATH, one name that is the toolchain's: not found."""
        with tempfile.TemporaryDirectory() as td:
            _program(Path(td), "ffprobe")
            saved = os.environ.get("PATH")
            os.environ["PATH"] = f"{td}{os.pathsep}{saved}"
            self.addCleanup(lambda: os.environ.__setitem__("PATH", saved))
            self.assertIsNotNone(shutil.which("ffprobe"), "the fixture did not install")
            with hermetic.no_media_tools():
                self.assertIsNone(shutil.which("ffprobe"))
                self.assertIsNone(shutil.which("ffprobe.exe"))

    def test_the_pin_does_not_outlive_its_block(self) -> None:
        """A leak would silently neuter every test that runs afterwards."""
        with tempfile.TemporaryDirectory() as td:
            installed = _program(Path(td), "ffprobe")
            saved = os.environ.get("PATH")
            os.environ["PATH"] = f"{td}{os.pathsep}{saved}"
            self.addCleanup(lambda: os.environ.__setitem__("PATH", saved))
            with hermetic.no_media_tools():
                pass
            self.assertEqual(shutil.which("ffprobe"), str(installed))


class TheMixinPinsTheWholeTestTests(hermetic.HermeticToolsMixin, unittest.TestCase):
    def test_a_class_can_take_the_pin_for_every_test_it_has(self) -> None:
        with self.assertRaises(FileNotFoundError):
            mkv_track_cleaner.resolve_mkvmerge_path()
        self.assertIsNone(bitdepth.find_ffprobe())


if __name__ == "__main__":
    unittest.main()
