"""Tests for the shared live status line and the four tools that adopted it.

The contract this file exists to hold is one sentence: **a live line is a
terminal effect and nothing else.** Every sweep in the toolkit writes its
permanent record with ``print``/``logging`` and its progress with
``organizekit/core/live.py``; if the second one ever leaked into a redirected
run, every log file, every captured test, every ``| tee`` and every ``--json``
document in the project would have picked up carriage returns and escape
sequences. So the tests below check the drawing *and* check, tool by tool, that
a non-terminal run contains no trace of it.
"""

from __future__ import annotations

import contextlib
import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import bitdepth as bd
import library_auditor as la
import mkv_track_cleaner as tc
import movie_standardizer as ms
import sync_subtitles as ss
from organizekit.core import RunLog
from organizekit.core.live import LiveLine, ellipsize, format_left, strip_ansi

VALID_SRT = "1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n"


def close_standardizer_log() -> None:
    """Release the log file the standardizer's stdlib logger holds open.

    Windows refuses to delete a file another handle still has open, so a
    temporary directory that outlived an ``ms.main()`` call cannot be cleaned
    up until the handler is closed. Everywhere else this is invisible, which
    is exactly why it has to be done deliberately.
    """
    for handler in ms.LOG.handlers[:]:
        ms.LOG.removeHandler(handler)
        with contextlib.suppress(OSError):
            handler.close()


class FakeTTY(io.StringIO):
    """A captured stream that claims to be a terminal."""

    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


def tty_line(*, use_color: bool = False) -> tuple[LiveLine, FakeTTY]:
    stream = FakeTTY()
    line = LiveLine(use_color=use_color, stream=stream)
    # color_enabled() answers from the real environment; the stream decides the
    # rest. Assert the fixture rather than trusting the terminal running these
    # tests, which may be a pipe.
    line.is_tty = True
    line.can_erase = True
    return line, stream


class NoTerminalIsNoOutputTests(unittest.TestCase):
    """Off a terminal every drawing method writes exactly nothing."""

    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.line = LiveLine(use_color=False, stream=self.stream)

    def test_a_plain_stream_is_not_a_terminal(self) -> None:
        self.assertFalse(self.line.is_tty)

    def test_nothing_is_drawn(self) -> None:
        self.line.update("drawing")
        self.line.progress(3, 9, label="probing", detail="Movie (2020).mkv")
        self.line.clear()
        self.line.commit()
        self.assertEqual(self.stream.getvalue(), "")

    def test_the_line_is_never_considered_open(self) -> None:
        self.line.update("drawing")
        self.assertFalse(self.line.is_open)

    def test_a_stream_that_raises_on_isatty_is_not_a_terminal(self) -> None:
        stream = mock.Mock()
        stream.isatty.side_effect = ValueError("detached")
        stream.encoding = "utf-8"
        self.assertFalse(LiveLine(use_color=False, stream=stream).is_tty)


class DrawingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.line, self.stream = tty_line()

    def test_update_returns_the_cursor_and_erases_to_the_end(self) -> None:
        self.line.update("working")
        self.assertEqual(self.stream.getvalue(), "\rworking\033[K")
        self.assertTrue(self.line.is_open)

    def test_clear_erases_and_closes_the_line(self) -> None:
        self.line.update("working")
        self.stream.truncate(0), self.stream.seek(0)
        self.line.clear()
        self.assertEqual(self.stream.getvalue(), "\r\033[K")
        self.assertFalse(self.line.is_open)

    def test_clearing_a_line_that_is_not_open_draws_nothing(self) -> None:
        self.line.clear()
        self.assertEqual(self.stream.getvalue(), "")

    def test_commit_keeps_the_line_with_a_newline(self) -> None:
        self.line.update("kept")
        self.stream.truncate(0), self.stream.seek(0)
        self.line.commit()
        self.assertEqual(self.stream.getvalue(), "\n")
        self.assertFalse(self.line.is_open)

    def test_a_console_without_escapes_pads_instead_of_erasing(self) -> None:
        """A Windows console with no VT mode would print the escape literally."""
        self.line.can_erase = False
        with mock.patch.object(self.line, "columns", return_value=20):
            self.line.update("short")
        drawn = self.stream.getvalue()
        self.assertTrue(drawn.startswith("\rshort"))
        self.assertNotIn("\033", drawn)
        self.assertEqual(len(drawn), 1 + 19)

    def test_an_over_wide_line_is_truncated_to_the_window(self) -> None:
        with mock.patch.object(self.line, "columns", return_value=20):
            self.line.update("x" * 200)
        self.assertEqual(len(strip_ansi(self.stream.getvalue()).lstrip("\r").rstrip("\033[K")), 19)

    def test_commit_on_a_closed_line_draws_nothing(self) -> None:
        self.line.commit()
        self.assertEqual(self.stream.getvalue(), "")

    def test_a_console_without_escapes_wipes_the_line_on_clear(self) -> None:
        self.line.can_erase = False
        with mock.patch.object(self.line, "columns", return_value=20):
            self.line.update("working")
            self.stream.truncate(0), self.stream.seek(0)
            self.line.clear()
        self.assertEqual(self.stream.getvalue(), "\r" + " " * 19 + "\r")

    def test_fit_reserves_room_for_the_rest_of_the_line(self) -> None:
        with mock.patch.object(self.line, "columns", return_value=40):
            self.assertEqual(self.line.fit("A" * 100, 20), "..." + "A" * 16)
            # Never squeezed below a readable minimum, however wide the rest is.
            self.assertEqual(len(self.line.fit("A" * 100, 999)), 8)

    def test_a_write_that_fails_falls_back_to_a_plain_line(self) -> None:
        with mock.patch("organizekit.core.live.write_raw", side_effect=OSError("gone")), \
             mock.patch("organizekit.core.live.print_text") as printed:
            self.line.update("\033[36mcolourful\033[0m")
        printed.assert_called_once()
        self.assertEqual(printed.call_args[0][0], "colourful")

    def test_a_fallback_that_also_fails_does_not_end_the_run(self) -> None:
        with mock.patch("organizekit.core.live.write_raw", side_effect=OSError("gone")), \
             mock.patch("organizekit.core.live.print_text", side_effect=OSError("also gone")):
            self.line.update("lost")  # must not raise


class ProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.line, self.stream = tty_line()

    def _drawn(self) -> str:
        return strip_ansi(self.stream.getvalue())

    def test_the_bar_carries_the_count_and_the_percentage(self) -> None:
        self.line.progress(5, 20, label="probing", detail="Movie (2020).mkv")
        drawn = self._drawn()
        self.assertIn("probing", drawn)
        self.assertIn("25%", drawn)
        self.assertIn("5/20", drawn)
        self.assertIn("Movie (2020).mkv", drawn)

    def test_the_bar_fills_in_proportion(self) -> None:
        self.assertEqual(self.line.bar(0, width=10), self.line.bar_empty * 10)
        self.assertEqual(self.line.bar(100, width=10), self.line.bar_fill * 10)
        self.assertEqual(self.line.bar(50, width=10),
                         self.line.bar_fill * 5 + self.line.bar_empty * 5)

    def test_a_percentage_outside_the_range_is_clamped(self) -> None:
        self.assertEqual(self.line.bar(-40, width=4), self.line.bar_empty * 4)
        self.assertEqual(self.line.bar(180, width=4), self.line.bar_fill * 4)

    def test_an_empty_queue_does_not_divide_by_zero(self) -> None:
        self.line.progress(0, 0, label="probing")
        self.assertIn("0%", self._drawn())

    def test_an_estimate_appears_once_there_is_something_to_extrapolate(self) -> None:
        with mock.patch("organizekit.core.live.time.monotonic", return_value=100.0):
            self.line.progress(10, 20, label="syncing", started=90.0)
        # 10 items in 10 s, 10 to go.
        self.assertIn("~10s left", self._drawn())

    def test_no_estimate_before_the_first_item_or_at_the_end(self) -> None:
        with mock.patch("organizekit.core.live.time.monotonic", return_value=100.0):
            self.line.progress(0, 20, label="syncing", started=90.0)
            self.line.progress(20, 20, label="syncing", started=90.0)
        self.assertNotIn("left", self._drawn())

    def test_a_long_name_is_shortened_from_the_front(self) -> None:
        with mock.patch.object(self.line, "columns", return_value=100):
            self.line.progress(1, 2, label="auditing", detail="A" * 200 + "Movie (2020).mkv")
        drawn = self._drawn()
        self.assertIn("Movie (2020).mkv", drawn)
        self.assertIn("...", drawn)
        # Composed to fit: `overwrite` never has to truncate it a second time.
        self.assertLessEqual(len(drawn.lstrip("\r").rstrip("\033[K")), 99)
        self.assertNotIn("...ting", drawn)

    def test_colour_is_applied_only_when_the_console_takes_it(self) -> None:
        plain, stream = tty_line(use_color=False)
        plain.progress(1, 2, label="probing", detail="Movie.mkv")
        # Only the erase escape, which is a cursor move, not colour.
        self.assertEqual(stream.getvalue().count("\033["), 1)
        self.assertTrue(stream.getvalue().endswith("\033[K"))

        coloured, stream = tty_line(use_color=True)
        coloured.use_color = True
        coloured.progress(1, 2, label="probing", detail="Movie.mkv")
        drawn = stream.getvalue()
        self.assertIn("\033[36m", drawn)          # the label and the filled bar
        self.assertIn("Movie.mkv", strip_ansi(drawn))

    def test_the_estimate_is_read_in_the_unit_the_wait_deserves(self) -> None:
        self.assertEqual(format_left(0), "0s")
        self.assertEqual(format_left(45), "45s")
        self.assertEqual(format_left(90), "1m30s")
        self.assertEqual(format_left(7500), "2h05m")
        self.assertEqual(format_left(-5), "0s")


class WidthAndGlyphTests(unittest.TestCase):
    def test_a_terminal_that_will_not_report_its_width_gets_the_default(self) -> None:
        line = LiveLine(use_color=False, stream=io.StringIO())
        with mock.patch("organizekit.core.live.shutil.get_terminal_size",
                        side_effect=OSError("not a tty")):
            self.assertEqual(line.columns(), 100)

    def test_a_very_narrow_window_still_gets_a_usable_width(self) -> None:
        line = LiveLine(use_color=False, stream=io.StringIO())
        with mock.patch("organizekit.core.live.shutil.get_terminal_size",
                        return_value=type("Size", (), {"columns": 5, "lines": 24})()):
            self.assertEqual(line.columns(), 40)

    def test_a_console_that_cannot_encode_the_blocks_gets_ascii(self) -> None:
        stream = mock.Mock()
        stream.encoding = "cp437-not-a-codec"
        stream.isatty.return_value = False
        line = LiveLine(use_color=False, stream=stream)
        self.assertEqual((line.bar_fill, line.bar_empty), ("#", "-"))

    def test_a_path_is_ellipsized_from_the_left(self) -> None:
        self.assertEqual(ellipsize("abcdefghij", 6), "...hij")
        self.assertEqual(ellipsize("abc", 10), "abc")
        self.assertEqual(ellipsize("abcdef", 2), "ab")
        self.assertEqual(ellipsize("abcdef", 0), "")


class RunLogIntegrationTests(unittest.TestCase):
    """A permanent line always erases the temporary one, in that order."""

    def test_attach_live_follows_the_logs_stream(self) -> None:
        log = RunLog()
        stream = FakeTTY()
        log.stream = stream
        live = log.attach_live()
        self.assertIs(live.stream, stream)
        self.assertIs(log.live, live)

    def test_a_logged_line_erases_the_live_line_first(self) -> None:
        log = RunLog()
        stream = FakeTTY()
        log.stream = stream
        live = log.attach_live()
        live.is_tty = live.can_erase = True
        live.update("probing Movie (2020).mkv")
        log("PROBED Movie (2020).mkv")
        drawn = stream.getvalue()
        self.assertLess(drawn.index("\r\033[K"), drawn.index("PROBED"))
        self.assertFalse(live.is_open)

    def test_a_log_without_a_live_line_is_untouched(self) -> None:
        log = RunLog()
        stream = io.StringIO()
        log.stream = stream
        log("plain line")
        self.assertNotIn("\r", stream.getvalue())


class CleanerRendererTests(unittest.TestCase):
    """The cleaner's own renderer now sits on the shared line, unchanged."""

    def test_the_cleaner_console_uses_the_shared_line(self) -> None:
        console = tc.LiveConsole(use_color=False)
        self.assertIsInstance(console.line, LiveLine)
        self.assertEqual(console.is_tty, console.line.is_tty)
        self.assertEqual(console._can_erase, console.line.can_erase)
        self.assertEqual((console._bar_fill, console._bar_empty),
                         (console.line.bar_fill, console.line.bar_empty))

    def test_the_cleaner_off_a_terminal_prints_no_escapes(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            console = tc.LiveConsole(use_color=False)
            console.begin_file("[clean] ", "Movie (2020).mkv", 1024)
            console.detail("-> audio       : keeping track 1")
            console.remux_progress(50, 0.0)
            console.end_file_inline("done", kind="success")
        printed = out.getvalue()
        self.assertIn("Movie (2020).mkv", printed)
        self.assertNotIn("\r", printed)
        self.assertNotIn("\033", printed)


class NonTerminalRunsAreUnchangedTests(unittest.TestCase):
    """Each adopting tool, run into a pipe: no carriage return, no escape.

    This is the whole point of TTY-gating. A regression here would corrupt log
    files and every captured comparison in the rest of the suite.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="live_tool_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.addCleanup(close_standardizer_log)
        self.library = self.root / "library"
        self.library.mkdir()
        for name in ("Alpha (2001)", "Bravo (2002)", "Charlie (2003)"):
            folder = self.library / name
            folder.mkdir()
            (folder / f"{name}.mkv").write_bytes(b"x" * 4096)
            (folder / f"{name}.eng.srt").write_text(VALID_SRT, encoding="utf-8")

    def _assert_plain(self, text: str) -> None:
        self.assertNotIn("\r", text)
        self.assertNotIn("\033", text)

    def test_the_auditor_prints_no_terminal_control(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = la.main(["--source", str(self.library),
                            "--log", str(self.root / "audit.log"),
                            "--report", str(self.root / "audit.txt"),
                            "--state-db", str(self.root / "state.db")])
        self.assertEqual(code, 0)
        self.assertIn("audited", out.getvalue())
        self._assert_plain(out.getvalue())
        self._assert_plain((self.root / "audit.log").read_text(encoding="utf-8"))

    def test_the_standardizer_prints_no_terminal_control(self) -> None:
        source = self.root / "downloads"
        (source / "Delta.2004.1080p.BluRay.x264-GRP").mkdir(parents=True)
        (source / "Delta.2004.1080p.BluRay.x264-GRP"
                / "Delta.2004.1080p.BluRay.x264-GRP.mkv").write_bytes(b"x" * 4096)
        out = io.StringIO()
        with redirect_stdout(out):
            ms.main(["--source", str(source), "--target", str(self.library),
                     "--log", str(self.root / "ms.log"),
                     "--report", str(self.root / "ms.txt"), "--min-size", "0"])
        self._assert_plain(out.getvalue())
        self._assert_plain((self.root / "ms.log").read_text(encoding="utf-8"))

    def test_the_live_line_of_each_tool_is_inert_off_a_terminal(self) -> None:
        non_tty = io.StringIO()
        for module in (la, bd, ss):
            with self.subTest(tool=module.__name__):
                saved = module.log.stream
                try:
                    module.log.stream = non_tty
                    live = module.log.attach_live()
                    self.assertFalse(live.is_tty)
                finally:
                    module.log.stream = saved
        self.assertFalse(LiveLine(stream=io.StringIO()).is_tty)
        self.assertFalse(LiveLine(stream=non_tty).is_tty)


class TerminalRunsDrawTests(unittest.TestCase):
    """On a terminal the same runs draw a status line and erase it again."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="live_tty_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.addCleanup(close_standardizer_log)
        self.library = self.root / "library"
        self.library.mkdir()
        for name in ("Alpha (2001)", "Bravo (2002)"):
            folder = self.library / name
            folder.mkdir()
            (folder / f"{name}.mkv").write_bytes(b"x" * 4096)
            (folder / f"{name}.eng.srt").write_text(VALID_SRT, encoding="utf-8")

    def test_the_auditor_draws_progress_on_a_terminal(self) -> None:
        stream = FakeTTY()
        with redirect_stdout(stream):
            code = la.main(["--source", str(self.library),
                            "--log", str(self.root / "audit.log"),
                            "--report", str(self.root / "audit.txt"),
                            "--state-db", str(self.root / "state.db"),
                            "--workers", "1"])
        self.assertEqual(code, 0)
        drawn = stream.getvalue()
        self.assertIn("auditing", drawn)
        self.assertIn("\r", drawn)
        # ... and the log file, which has no cursor, never sees any of it.
        self._assert_log_is_plain()

    def _assert_log_is_plain(self) -> None:
        text = (self.root / "audit.log").read_text(encoding="utf-8")
        self.assertNotIn("\r", text)
        self.assertNotIn("auditing ", text)

    def test_the_standardizer_erases_its_line_before_a_log_line(self) -> None:
        source = self.root / "downloads"
        source.mkdir()
        (source / "Echo.2005.1080p.mkv").write_bytes(b"x" * 4096)
        stream = FakeTTY()
        with redirect_stdout(stream):
            ms.main(["--source", str(source), "--target", str(self.library),
                     "--log", str(self.root / "ms.log"),
                     "--report", str(self.root / "ms.txt"), "--min-size", "0"])
        drawn = stream.getvalue()
        self.assertIn("organizing", drawn)
        self.assertIn("\r", drawn)
        self.assertNotIn("\r", (self.root / "ms.log").read_text(encoding="utf-8"))

    def test_the_standardizers_log_filter_erases_even_without_a_batch(self) -> None:
        """The filter runs on the handler, so any log line clears the line."""
        stream = FakeTTY()
        handler = logging.StreamHandler(stream)
        handler.addFilter(ms._EraseLiveLine())
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "done", None, None)
        live = ms.LIVE
        try:
            ms.LIVE = LiveLine(use_color=False, stream=stream)
            ms.LIVE.is_tty = ms.LIVE.can_erase = True
            ms.LIVE.update("organizing Echo.2005.mkv")
            handler.handle(record)
        finally:
            ms.LIVE = live
        drawn = stream.getvalue()
        self.assertLess(drawn.index("\r\033[K"), drawn.index("done"))


if __name__ == "__main__":
    unittest.main()
