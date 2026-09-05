"""The failures these tools are built to survive, one boundary at a time.

Every `except` in the toolkit is now either narrow or argued for in place —
no file has a blanket exemption from ruff's `BLE001` any more. A narrow
`except` is only an improvement if it still catches what actually happens, so
these tests inject the real failure at each boundary that was narrowed and
assert the degradation the code promises: a hostname nobody can resolve, a
volume that will not report its free space, a console that has been closed
under us, a log file on a read-only share, a child process that is already
gone.

The point is not coverage. It is that "this cannot fail the run" stays true
after the catch stopped being `except Exception`.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import mkv_track_cleaner as tc  # noqa: E402
import organize as cli  # noqa: E402
import sync_subtitles as ss  # noqa: E402


class HostAndVolumeTests(unittest.TestCase):
    """The machine itself refusing to answer a question."""

    def test_unresolvable_hostname_becomes_unknown(self) -> None:
        with mock.patch.object(tc.socket, "gethostname", side_effect=OSError("no DNS")):
            self.assertEqual(tc._this_hostname(), "unknown")

    def test_blank_hostname_becomes_unknown(self) -> None:
        with mock.patch.object(tc.socket, "gethostname", return_value="   "):
            self.assertEqual(tc._this_hostname(), "unknown")

    def test_free_space_that_cannot_be_queried_does_not_block_the_remux(self) -> None:
        # A network share that will not answer statvfs must not stop work: the
        # disk guard reports the doubt and lets the run continue.
        with mock.patch.object(tc.shutil, "disk_usage", side_effect=OSError("share gone")):
            ok, free, required, note = tc.check_free_space(Path("/nonexistent"), 1_000_000)
        self.assertTrue(ok)
        self.assertEqual(free, 0)
        self.assertGreater(required, 1_000_000)
        self.assertIn("could not query free space", note or "")

    def test_a_full_volume_is_still_refused(self) -> None:
        usage = SimpleNamespace(total=100, used=99, free=1)
        with mock.patch.object(tc.shutil, "disk_usage", return_value=usage):
            ok, free, required, note = tc.check_free_space(Path("."), 1_000_000)
        self.assertFalse(ok)
        self.assertEqual(free, 1)
        self.assertIsNone(note)


class FileMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_timestamps_the_filesystem_refuses_are_not_fatal(self) -> None:
        target = self.tmp / "movie.mkv"
        target.write_bytes(b"x")
        stat = target.stat()
        with mock.patch.object(tc.os, "utime", side_effect=OSError("read-only")):
            tc.restore_file_times(target, stat)  # must not raise

    def test_a_timestamp_out_of_range_is_not_fatal(self) -> None:
        target = self.tmp / "movie.mkv"
        target.write_bytes(b"x")
        stat = target.stat()
        with mock.patch.object(tc.os, "utime", side_effect=OverflowError("too large")):
            tc.restore_file_times(target, stat)

    def test_timestamps_are_restored_when_the_filesystem_allows_it(self) -> None:
        target = self.tmp / "movie.mkv"
        target.write_bytes(b"x")
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns + 10**9, stat.st_mtime_ns + 10**9))
        tc.restore_file_times(target, stat)
        self.assertEqual(target.stat().st_mtime_ns, stat.st_mtime_ns)

    def test_deleting_a_file_the_os_will_not_release_gives_up_quietly(self) -> None:
        target = self.tmp / "locked.mkv"
        target.write_bytes(b"x")
        with mock.patch.object(tc.time, "sleep") as slept, \
                mock.patch.object(Path, "unlink", side_effect=OSError("in use")):
            tc.safe_delete(target, max_retries=3, delay=0.01)
        self.assertEqual(slept.call_count, 3)  # retried, then let go

    def test_a_lock_file_that_cannot_be_removed_is_not_fatal(self) -> None:
        with mock.patch.object(Path, "unlink", side_effect=OSError("read-only")):
            tc.release_lock(self.tmp / "cleaner.lock")


class ConsoleTests(unittest.TestCase):
    """stdout closing under a run that is still working."""

    def test_printing_to_a_closed_stream_is_not_fatal(self) -> None:
        broken = mock.Mock()
        broken.write.side_effect = ValueError("I/O operation on closed file")
        with mock.patch.object(tc.sys, "stdout", broken):
            tc._print_safe("a line nobody will read")
            tc._write_raw("\rprogress")

    def test_a_broken_pipe_is_not_fatal(self) -> None:
        broken = mock.Mock()
        broken.write.side_effect = BrokenPipeError("head closed the pipe")
        with mock.patch.object(tc.sys, "stdout", broken):
            tc._print_safe("into a closed pipe")

    def test_an_unencodable_line_falls_back_to_the_byte_stream(self) -> None:
        stream = mock.Mock()
        stream.write.side_effect = UnicodeEncodeError("ascii", "x", 0, 1, "nope")
        stream.encoding = "ascii"
        stream.buffer = io.BytesIO()
        with mock.patch.object(tc.sys, "stdout", stream), mock.patch("builtins.print",
                               side_effect=UnicodeEncodeError("ascii", "x", 0, 1, "nope")):
            tc._print_safe("commentary — dash")
        self.assertIn(b"commentary", stream.buffer.getvalue())

    def test_a_terminal_that_will_not_report_its_width_gets_the_default(self) -> None:
        console = tc.LiveConsole(use_color=False)
        with mock.patch.object(tc.shutil, "get_terminal_size", side_effect=OSError("not a tty")):
            self.assertEqual(console._cols(), 100)

    def test_a_console_that_cannot_encode_the_bar_gets_ascii(self) -> None:
        stream = mock.Mock()
        stream.encoding = "cp437-not-a-codec"
        stream.isatty.return_value = False
        with mock.patch.object(tc.sys, "stdout", stream):
            console = tc.LiveConsole(use_color=False)
        self.assertEqual((console._bar_fill, console._bar_empty), ("#", "-"))

    def test_a_stream_that_raises_on_isatty_is_treated_as_not_a_tty(self) -> None:
        stream = mock.Mock()
        stream.isatty.side_effect = ValueError("detached")
        stream.encoding = "utf-8"
        with mock.patch.object(tc.sys, "stdout", stream):
            console = tc.LiveConsole(use_color=False)
        self.assertFalse(console.is_tty)


class LogFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(tc.close_log_fp)
        tc.close_log_fp()

    def test_a_log_file_that_cannot_be_opened_does_not_stop_the_run(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("read-only share")):
            self.assertIsNone(tc._open_log_fp(str(self.tmp / "run.log")))

    def test_the_console_line_survives_an_unwritable_log(self) -> None:
        buffer = io.StringIO()
        with mock.patch("builtins.open", side_effect=OSError("read-only share")), \
                mock.patch.object(tc.sys, "stdout", buffer):
            tc.log("cleaned one movie", log_file_path=str(self.tmp / "run.log"))
        self.assertIn("cleaned one movie", buffer.getvalue())

    def test_a_write_to_a_closed_handle_is_not_fatal(self) -> None:
        target = self.tmp / "run.log"
        handle = tc._open_log_fp(str(target))
        self.assertIsNotNone(handle)
        assert handle is not None
        handle.close()  # the handle the module still believes in
        tc.log("after the handle died", log_file_path=str(target))

    def test_closing_a_dead_handle_is_not_fatal(self) -> None:
        tc._open_log_fp(str(self.tmp / "run.log"))
        assert tc._log_fp is not None
        tc._log_fp.close()
        tc.close_log_fp()  # flush() on a closed file raises ValueError
        self.assertIsNone(tc._log_fp)


class ChildProcessTests(unittest.TestCase):
    def test_a_version_probe_that_hangs_reports_unknown(self) -> None:
        with mock.patch.object(tc, "_run_mkvmerge",
                               side_effect=subprocess.TimeoutExpired("mkvmerge", 5)):
            self.assertEqual(tc.get_mkvmerge_version("mkvmerge"), "unknown version")

    def test_a_missing_binary_reports_unknown(self) -> None:
        with mock.patch.object(tc, "_run_mkvmerge", side_effect=FileNotFoundError("mkvmerge")):
            self.assertEqual(tc.get_mkvmerge_version("mkvmerge"), "unknown version")

    def test_killing_a_child_that_is_already_gone_is_not_fatal(self) -> None:
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.kill.side_effect = OSError("no such process")
        with mock.patch.object(tc, "_active_proc", proc):
            tc._kill_active_child()
        proc.kill.assert_called_once()

    def test_verification_that_cannot_run_fails_closed(self) -> None:
        # The safety rule: a temp file is only promoted on a *passed*
        # verification, so a probe that raises must read as "did not pass".
        with mock.patch.object(tc, "_run_mkvmerge", side_effect=OSError("mkvmerge vanished")):
            ok, reason, info = tc.verify_remux_output(Path("temp.mkv"), "mkvmerge", {})
        self.assertFalse(ok)
        self.assertIn("could not re-inspect", reason)
        self.assertIsNone(info)


class CliBoundaryTests(unittest.TestCase):
    def test_a_binary_version_probe_that_times_out_is_cosmetic(self) -> None:
        with mock.patch.object(cli.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("ffprobe", 5)):
            self.assertEqual(cli.get_binary_version("ffprobe"), "")

    def test_a_missing_binary_has_no_version(self) -> None:
        with mock.patch.object(cli.subprocess, "run", side_effect=FileNotFoundError("ffprobe")):
            self.assertEqual(cli.get_binary_version("ffprobe"), "")

    def test_a_damaged_extraction_ledger_reads_as_no_record(self) -> None:
        with mock.patch("subtitle_fetcher.find_extracted_record",
                        side_effect=RuntimeError("ledger is corrupt")):
            self.assertIsNone(ss._extracted_sidecar_record(Path("x.eng.srt"), "0" * 64))

    def test_a_record_that_is_not_a_mapping_is_ignored(self) -> None:
        with mock.patch("subtitle_fetcher.find_extracted_record", return_value=["not", "a", "dict"]):
            self.assertIsNone(ss._extracted_sidecar_record(Path("x.eng.srt"), "0" * 64))


if __name__ == "__main__":
    unittest.main()
