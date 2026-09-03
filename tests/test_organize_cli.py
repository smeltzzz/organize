"""Unit tests for the unified CLI and diagnostics in ``organize.py``."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import bitdepth
import organize


class OrganizeCliTests(unittest.TestCase):
    def test_dashboard_when_no_arguments(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = organize.main([])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("ORGANIZE", output)
        self.assertIn("standardize", output)
        self.assertIn("doctor", output)

    def test_version_flag(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = organize.main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(f"organize {organize.VERSION}", buf.getvalue())

    def test_help_flag(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = organize.main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Unified Jellyfin & Plex Media Management Toolkit", buf.getvalue())

    def test_unknown_command_exits_2(self) -> None:
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            code = organize.main(["definitely-unknown-command"])
        self.assertEqual(code, 2)
        self.assertIn("Unknown command", buf_err.getvalue())

    def test_doctor_with_valid_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "source"
            lib = root / "library"
            src.mkdir()
            lib.mkdir()

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = organize.run_doctor(library_path=lib, source_path=src)

            # Both exist on the same temp filesystem, so hardlink check passes
            output = buf.getvalue()
            self.assertIn("Python Runtime", output)
            self.assertIn("Hardlink Compatibility", output)
            self.assertEqual(code, 0)

    def test_doctor_detects_cross_device_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "source"
            lib = root / "library"
            src.mkdir()
            lib.mkdir()

            orig_stat = Path.stat

            def fake_stat(self_path, *args, **kwargs):
                st = orig_stat(self_path, *args, **kwargs)
                if self_path == src:
                    # Fake different st_dev
                    return os.stat_result((
                        st.st_mode, st.st_ino, 99999, st.st_nlink,
                        st.st_uid, st.st_gid, st.st_size,
                        st.st_atime, st.st_mtime, st.st_ctime,
                    ))
                return st

            with patch.object(Path, "stat", fake_stat):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = organize.run_doctor(library_path=lib, source_path=src)

                output = buf.getvalue()
                self.assertIn("DIFFERENT filesystems", output)
                self.assertEqual(code, 1)

    def test_doctor_reports_ffprobe_when_the_inspector_finds_it(self) -> None:
        """doctor must ask bitdepth (not a stale '10bit' module) about ffprobe."""
        fake = "/fake/ffprobe"
        with patch.object(bitdepth, "find_ffprobe", return_value=fake), \
                patch.object(bitdepth, "ffprobe_works", return_value=True), \
                patch.object(organize, "get_binary_version", return_value="ffprobe 6.0"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = organize.run_doctor(library_path=Path("/tmp/lib"), source_path=Path("/tmp/src"))
        output = buf.getvalue()
        self.assertIn("FFmpeg (ffprobe)", output)
        self.assertIn("Found:", output)
        self.assertIn(fake, output)
        self.assertEqual(code, 0)

    def test_doctor_resolution_honours_organize_library(self) -> None:
        saved = {var: os.environ.pop(var, None)
                 for var in ("ORGANIZE_LIBRARY", "MOVIE_STD_TARGET", "MOVIE_STD_SOURCE")}
        try:
            os.environ["ORGANIZE_LIBRARY"] = "/env/library"
            os.environ["MOVIE_STD_SOURCE"] = "/env/source"
            buf = io.StringIO()
            with redirect_stdout(buf):
                organize.run_doctor()
            output = buf.getvalue()
            # The doctor prints str(Path(...)), which is drive-relative
            # ("\\env\\library") on Windows and POSIX-style on Linux/macOS.
            # Assert on the Path-formatted value so this test runs everywhere.
            self.assertIn(str(Path("/env/library")), output)
            self.assertIn(str(Path("/env/source")), output)
            self.assertNotIn(r"E:\torrents", output)
        finally:
            for var, value in saved.items():
                if value is not None:
                    os.environ[var] = value
                else:
                    os.environ.pop(var, None)

    def test_doctor_resolution_is_platform_aware(self) -> None:
        saved = {var: os.environ.pop(var, None)
                 for var in ("ORGANIZE_LIBRARY", "MOVIE_STD_TARGET", "MOVIE_STD_SOURCE")}
        try:
            expected_lib = (
                Path(r"E:\torrents\final_organized") if os.name == "nt"
                else Path.home() / "Media" / "Movies"
            )
            expected_src = (
                Path(r"E:\torrents\final") if os.name == "nt"
                else Path.home() / "torrents" / "final"
            )
            self.assertEqual(organize._resolve_library_path(None), expected_lib)
            self.assertEqual(organize._resolve_source_path(None), expected_src)
        finally:
            for var, value in saved.items():
                if value is not None:
                    os.environ[var] = value
                else:
                    os.environ.pop(var, None)

    def test_self_test_flag(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = organize.main(["--self-test"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("ALL SELF-TESTS PASSED", output)
        self.assertIn("organize.py", output)
        self.assertIn("bitdepth.py", output)
        self.assertIn("movie_standardizer.py", output)

    def test_delegate_to_script_nonexistent(self) -> None:
        buf_err = io.StringIO()
        with redirect_stderr(buf_err):
            code = organize.delegate_to_script("nonexistent_script.py", [])
        self.assertEqual(code, 2)
        self.assertIn("not found", buf_err.getvalue())


if __name__ == "__main__":
    unittest.main()
