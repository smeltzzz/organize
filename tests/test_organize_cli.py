"""Unit tests for the unified CLI and diagnostics in ``organize.py``."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

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
