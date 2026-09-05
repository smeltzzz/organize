"""Unit tests for ``organizekit/core/runlog.py``.

The run log is the one thing every tool touches on every item, and it is the
only record an operator has of a sweep that ran overnight. So the tests here
are about its two promises rather than its formatting: the console and the
file get the *same* line, and a logging failure never propagates into the
work. The last group proves the property the copies did not all have — that a
line written from a worker thread arrives whole.
"""

from __future__ import annotations

import io
import re
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import mock

from organizekit.core import RunLog


class RunLogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.log = RunLog()

    def capture(self, *args: object, **kwargs: object) -> str:
        """Call the logger, returning what it printed."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.log(*args, **kwargs)  # type: ignore[arg-type]
        return buffer.getvalue()


class FormatTests(RunLogTestCase):
    def test_default_form_is_stamp_level_message(self) -> None:
        line = self.log.format("hello", "WARNING")
        self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[WARNING\] hello$")

    def test_level_defaults_to_info(self) -> None:
        self.assertIn("[INFO] hello", self.log.format("hello"))

    def test_bracketed_form_is_opt_in(self) -> None:
        line = RunLog(brackets=True).format("hello", "ERROR")
        self.assertRegex(line, r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] \[ERROR\] hello$")


class ConsoleAndFileTests(RunLogTestCase):
    def test_console_and_file_receive_the_identical_line(self) -> None:
        target = self.tmp / "run.log"
        self.log.file = target
        printed = self.capture("moved 3 files")
        self.assertEqual(printed, target.read_text(encoding="utf-8"))

    def test_no_file_configured_prints_only(self) -> None:
        printed = self.capture("nowhere to write this")
        self.assertIn("nowhere to write this", printed)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_explicit_log_file_overrides_the_default(self) -> None:
        default, override = self.tmp / "default.log", self.tmp / "step.log"
        self.log.file = default
        self.capture("step detail", "INFO", override)
        self.assertIn("step detail", override.read_text(encoding="utf-8"))
        self.assertFalse(default.exists())

    def test_lines_accumulate_in_order(self) -> None:
        self.log.file = self.tmp / "run.log"
        for message in ("first", "second", "third"):
            self.capture(message)
        written = self.log.file.read_text(encoding="utf-8").splitlines()
        self.assertEqual([line.split("] ", 1)[1] for line in written],
                         ["first", "second", "third"])

    def test_missing_parent_directory_is_created(self) -> None:
        self.log.file = self.tmp / "logs" / "nested" / "run.log"
        self.capture("first line of a fresh install")
        self.assertTrue(self.log.file.exists())

    def test_to_file_writes_without_printing(self) -> None:
        target = self.tmp / "run.log"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.log.to_file("already shown by the progress bar", log_file=target)
        self.assertEqual(buffer.getvalue(), "")
        self.assertIn("already shown", target.read_text(encoding="utf-8"))

    def test_to_file_honours_the_default_target(self) -> None:
        self.log.file = self.tmp / "run.log"
        self.log.to_file("quiet note")
        self.assertIn("quiet note", self.log.file.read_text(encoding="utf-8"))

    def test_to_file_without_a_target_is_a_no_op(self) -> None:
        self.log.to_file("into the void")
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_unencodable_characters_are_replaced_not_raised(self) -> None:
        # A filename read off a foreign filesystem can carry a lone surrogate;
        # utf-8 cannot encode one, and strict mode would abort the sweep.
        self.log.file = self.tmp / "run.log"
        self.capture("subtitle for \ud800.mkv")
        self.assertIn("subtitle for", self.log.file.read_text(encoding="utf-8"))


class FailureIsNeverFatalTests(RunLogTestCase):
    def test_unwritable_log_directory_does_not_raise(self) -> None:
        # A read-only share, a full disk, a path that is now a file: all OSError.
        self.log.file = self.tmp / "run.log"
        with mock.patch.object(Path, "open", side_effect=OSError("read-only file system")):
            printed = self.capture("the work itself succeeded")
        self.assertIn("the work itself succeeded", printed)

    def test_mkdir_failure_does_not_raise(self) -> None:
        self.log.file = self.tmp / "logs" / "run.log"
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            self.capture("still fine")
        self.assertFalse(self.log.file.exists())

    def test_a_log_target_that_is_a_directory_is_survived(self) -> None:
        directory = self.tmp / "run.log"
        directory.mkdir()
        self.log.file = directory
        self.capture("cannot write into a directory")

    def test_console_failure_still_reaches_the_file(self) -> None:
        # print_text degrades a console that cannot encode the line; the file
        # copy must not be lost to it.
        self.log.file = self.tmp / "run.log"
        with mock.patch("organizekit.core.runlog.print_text",
                        side_effect=lambda text: None) as printer:
            self.log("recorded")
        printer.assert_called_once()
        self.assertIn("recorded", self.log.file.read_text(encoding="utf-8"))

    def test_non_oserror_from_the_log_write_is_not_swallowed(self) -> None:
        # The rule is "a logging *failure* costs a record", not "hide bugs":
        # a programming error here should still surface.
        self.log.file = self.tmp / "run.log"
        with mock.patch.object(Path, "open", side_effect=ValueError("bad mode")), \
                self.assertRaises(ValueError):
            self.capture("boom")


class ThreadSafetyTests(RunLogTestCase):
    def test_a_line_from_a_worker_thread_arrives_whole(self) -> None:
        # Writing a line to a console is not one atomic operation: the
        # interpreter can switch threads part-way through it. That is
        # simulated here — a printer that emits each line in two pieces with a
        # yield between them — so the serialisation is tested rather than
        # assumed to fall out of the GIL.
        workers, per_worker = 6, 20
        barrier = Barrier(workers)
        pieces: list[str] = []

        def torn_printer(text: str) -> None:
            head, tail = text[:12], text[12:]
            pieces.append(head)
            time.sleep(0.0005)  # invite a thread switch mid-line
            pieces.append(tail + "\n")

        def emit(index: int) -> None:
            barrier.wait()
            for n in range(per_worker):
                self.log(f"worker {index} item {n:02d}")

        self.log.file = self.tmp / "run.log"
        with mock.patch("organizekit.core.runlog.print_text", torn_printer), \
                ThreadPoolExecutor(workers) as pool:
            list(pool.map(emit, range(workers)))

        whole = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[INFO\] worker \d item \d\d$")
        expected = sorted(f"worker {i} item {n:02d}"
                          for i in range(workers) for n in range(per_worker))
        for source in ("".join(pieces), self.log.file.read_text(encoding="utf-8")):
            lines = source.splitlines()
            for line in lines:  # a torn line fails here, not in the tally
                self.assertRegex(line, whole)
            self.assertEqual(sorted(line.split("] ", 1)[1] for line in lines), expected)

    def test_the_lock_is_public_so_other_writers_can_share_it(self) -> None:
        # jellyfin_one_shot echoes child-tool output from reader threads while
        # the run log writes status lines; they take this one lock.
        self.assertTrue(hasattr(self.log, "lock"))
        with self.log.lock:
            self.assertFalse(self.log.lock.acquire(blocking=False))


class ToolAdoptionTests(unittest.TestCase):
    """The tools that used to carry their own copy now share this one."""

    def test_every_adopter_logs_through_the_shared_implementation(self) -> None:
        import bitdepth
        import jellyfin_one_shot
        import library_auditor
        import subtitle_fetcher
        import sync_subtitles

        for module in (bitdepth, library_auditor, sync_subtitles, subtitle_fetcher):
            with self.subTest(module=module.__name__):
                self.assertIsInstance(module.log, RunLog)
        self.assertIsInstance(jellyfin_one_shot._RUN_LOG, RunLog)

    def test_the_orchestrator_keeps_its_bracketed_transcript_form(self) -> None:
        import jellyfin_one_shot

        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "runtime.log"
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                jellyfin_one_shot.log(target, "INFO", "starting")
                jellyfin_one_shot.log_to_file(target, "DEBUG", "detail")
            written = target.read_text(encoding="utf-8").splitlines()
        self.assertRegex(written[0], r"^\[[\d\- :]+\] \[INFO\] starting$")
        self.assertRegex(written[1], r"^\[[\d\- :]+\] \[DEBUG\] detail$")
        self.assertIn("[INFO] starting", buffer.getvalue())
        self.assertNotIn("detail", buffer.getvalue())

    def test_the_fetcher_writes_nowhere_unless_told_to(self) -> None:
        # Unlike its siblings the fetcher passes cfg.log_file at every call
        # site and never sets a default; adopting one would start writing to a
        # file its callers did not ask for.
        import subtitle_fetcher

        self.assertIsNone(subtitle_fetcher.log.file)


if __name__ == "__main__":
    unittest.main()
