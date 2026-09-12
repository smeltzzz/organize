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


def _is_the_same_program(found: str | None, expected: Path) -> bool:
    """Whether ``shutil.which`` answered with the program we installed.

    Compared through ``os.path.normcase``, never with ``==`` on the raw
    strings. On Windows ``shutil.which`` builds its candidate names from
    ``PATHEXT`` - which is spelled ``.COM;.EXE;.BAT;...`` - and returns
    ``os.path.join(directory, candidate)``, so it reports ``ffprobe.BAT`` for a
    file this module created as ``ffprobe.bat``. An exact string comparison
    passes on Linux and macOS and fails on Windows, where the filesystem does
    not care about the difference. ``normcase`` is a no-op on POSIX and folds
    case and separators on Windows, which is the comparison both want.
    """
    if not found:
        return False
    return (os.path.normcase(str(Path(found).resolve()))
            == os.path.normcase(str(expected.resolve())))


class ThePinHoldsTests(unittest.TestCase):
    def test_every_toolchain_lookup_answers_not_installed(self) -> None:
        """The whole point: one answer, on a bare runner and on a workstation."""
        with hermetic.no_media_tools():
            with self.assertRaises(FileNotFoundError):
                mkv_track_cleaner.resolve_mkvmerge_path()
            self.assertIsNone(subtitle_extractor.find_mkvtoolnix_binary("mkvextract"))
            self.assertIsNone(subtitle_extractor.find_mkvtoolnix_binary("mkvmerge"))
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
    """The pin hides the toolchain and nothing else - and then lets go."""

    def _install_on_path(self, name: str) -> Path:
        """Put a real program called ``name`` on the PATH for this test only."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        program = _program(Path(td.name), name)
        saved = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{td.name}{os.pathsep}{saved}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", saved))
        return program

    def test_an_unrelated_program_is_still_found(self) -> None:
        """The pin must not blind the suite to every program on the machine."""
        program = self._install_on_path("not_a_media_tool")
        with hermetic.no_media_tools():
            self.assertTrue(_is_the_same_program(shutil.which("not_a_media_tool"), program),
                            "the pin hid a program that is not part of the toolchain")

    def test_a_media_tool_on_the_path_is_invisible_while_pinned(self) -> None:
        """The same PATH, one name that is the toolchain's: not found."""
        installed = self._install_on_path("ffprobe")
        self.assertTrue(_is_the_same_program(shutil.which("ffprobe"), installed),
                        "the fixture did not install")
        with hermetic.no_media_tools():
            self.assertIsNone(shutil.which("ffprobe"))
            self.assertIsNone(shutil.which("ffprobe.exe"))

    def test_the_pin_does_not_outlive_its_block(self) -> None:
        """A leak would silently neuter every test that runs afterwards."""
        installed = self._install_on_path("ffprobe")
        with hermetic.no_media_tools():
            self.assertIsNone(shutil.which("ffprobe"), "the pin did not take hold")
        self.assertTrue(_is_the_same_program(shutil.which("ffprobe"), installed),
                        "the pin outlived its block")


class TheMixinPinsTheWholeTestTests(hermetic.HermeticToolsMixin, unittest.TestCase):
    def test_a_class_can_take_the_pin_for_every_test_it_has(self) -> None:
        with self.assertRaises(FileNotFoundError):
            mkv_track_cleaner.resolve_mkvmerge_path()
        self.assertIsNone(bitdepth.find_ffprobe())


if __name__ == "__main__":
    unittest.main()
