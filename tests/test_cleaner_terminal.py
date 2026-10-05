"""The cleaner's terminal rendering: one line per movie, redrawn in place.

A remux sweep runs for hours over a library, and the only thing an operator can
watch is this renderer. Its contract is narrow and easy to break:

* on a terminal, each movie occupies **one line** that is overwritten in place -
  inspecting, then the remux bar, then the outcome - and a newline is emitted
  only when the movie is finished;
* a permanent log line never lands on top of the live line: whatever is open is
  committed with a newline first, so the log and the progress bar cannot
  interleave into unreadable garbage;
* the bar is throttled, because mkvmerge reports progress far faster than a
  terminal can usefully redraw it, and off a terminal it is throttled harder -
  one line per ten percent - because there is nothing to overwrite there;
* colour is drawn only when the console takes it.

The non-terminal half of this is already asserted in ``test_live_console.py``
(no carriage return, no escape). These are the branches that only exist on a
real terminal, which is where the code was measured at zero.
"""

from __future__ import annotations

import io
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import mkv_track_cleaner as tc
from organizekit.core.live import strip_ansi

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class FakeTTY(io.StringIO):
    """A captured stream that claims to be a terminal."""

    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


class TerminalConsoleTests(unittest.TestCase):
    """``LiveConsole`` with a terminal underneath it."""

    def console(self, *, color: bool = False) -> tuple[tc.LiveConsole, FakeTTY]:
        stream = FakeTTY()
        redirect = redirect_stdout(stream)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        console = tc.LiveConsole(use_color=color)
        self.assertTrue(console.is_tty, "the fixture must be a terminal or the test proves nothing")
        return console, stream

    def test_a_movie_is_one_line_that_is_overwritten_until_it_finishes(self) -> None:
        console, stream = self.console()
        console.begin_file("[  1/3] ", "Alpha (2001)/Alpha (2001).mkv", 4096)
        console.end_file_inline("cleaned 1.2 GiB saved", kind="success")
        drawn = stream.getvalue()
        self.assertGreater(drawn.count("\r"), 1, "each redraw returns the cursor")
        self.assertEqual(drawn.count("\n"), 1, "and only the finished movie gets a newline")
        self.assertIn("Alpha (2001).mkv", strip_ansi(drawn))
        self.assertIn("cleaned 1.2 GiB saved", strip_ansi(drawn))

    def test_a_detail_arriving_under_a_live_bar_commits_the_bar_first(self) -> None:
        """A permanent line never lands on top of the progress bar.

        The bar is redrawn in place with no newline; if a detail were printed while
        it was open, the two would interleave into unreadable garbage on the one
        terminal the operator is watching during a multi-hour sweep.
        """
        console, stream = self.console()
        console.begin_file("[  1/3] ", "Alpha (2001)/Alpha (2001).mkv", 4096)
        console.remux_progress(50, time.monotonic() - 5)
        before = stream.getvalue().count("\n")
        console.detail("-> subtitles   : removing 2")
        drawn = strip_ansi(stream.getvalue())
        self.assertGreater(drawn.count("\n"), before, "the open bar was committed first")
        self.assertIn("-> subtitles   : removing 2", drawn)

    def test_a_bar_is_drawn_under_the_movie_line_it_belongs_to(self) -> None:
        """The first detail commits the file line so the bar cannot overwrite it.

        A remux bar painted on top of the movie's own line would leave the
        terminal showing a percentage with no name attached - which is the whole
        thing the renderer exists to prevent on a queue of a thousand movies.
        """
        console, stream = self.console()
        console.begin_file("[  1/3] ", "Alpha (2001)/Alpha (2001).mkv", 4096)
        console.remux_progress(50, time.monotonic() - 5)
        console.end_file_inline("cleaned", kind="success")
        plain = strip_ansi(stream.getvalue())
        self.assertIn("50%", plain)
        self.assertLess(plain.index("Alpha (2001).mkv"), plain.index("50%"),
                        "the movie's line is committed above the bar")
        self.assertEqual(plain.count("cleaned"), 1, "the outcome replaced the bar, not the name")

    def test_the_progress_bar_shows_the_elapsed_time_and_an_estimate(self) -> None:
        console, stream = self.console()
        console.begin_file("", "Alpha (2001).mkv", 4096)
        console.remux_progress(50, time.monotonic() - 20)
        drawn = strip_ansi(stream.getvalue())
        self.assertIn("-> remux", drawn)
        self.assertIn("50%", drawn)
        self.assertIn("elapsed", drawn)
        self.assertIn("left", drawn)

    def test_no_estimate_is_shown_before_there_is_something_to_extrapolate_from(self) -> None:
        console, stream = self.console()
        console.begin_file("", "Alpha (2001).mkv", 4096)
        console.remux_progress(1, time.monotonic())
        self.assertNotIn("left", strip_ansi(stream.getvalue()))

    def test_redraws_of_the_same_percentage_are_throttled(self) -> None:
        """mkvmerge reports far faster than a terminal can usefully redraw.

        Without the throttle a busy console spends the whole remux painting the
        same bar; with it, an unchanged percentage inside 80 ms is dropped - but
        the final 100% is always drawn, because that is the one a watcher waits
        for.
        """
        console, stream = self.console()
        console.begin_file("", "Alpha (2001).mkv", 4096)
        now = time.monotonic()
        console.remux_progress(40, now)
        first = stream.getvalue()
        console.remux_progress(40, now)
        self.assertEqual(stream.getvalue(), first, "the same percentage redrawn is dropped")
        console.remux_progress(100, now)
        self.assertIn("100%", strip_ansi(stream.getvalue()))

    def test_a_changed_percentage_is_always_drawn(self) -> None:
        console, stream = self.console()
        console.begin_file("", "Alpha (2001).mkv", 4096)
        now = time.monotonic()
        for percent in (10, 20, 30):
            console.remux_progress(percent, now)
        drawn = strip_ansi(stream.getvalue())
        for percent in (10, 20, 30):
            self.assertIn(f"{percent}%", drawn)

    def test_a_log_line_commits_the_open_progress_line_first(self) -> None:
        """The permanent record and the live line must not share a line.

        This is the branch that keeps a log readable when a warning arrives
        mid-remux: the bar is finished with a newline, then the log line is
        printed on its own.
        """
        console, stream = self.console()
        console.begin_file("", "Alpha (2001).mkv", 4096)
        console.remux_progress(40, time.monotonic())
        console.log_line("22:31:05", "WARNING", "the source changed underneath us")
        drawn = stream.getvalue()
        plain = strip_ansi(drawn)
        self.assertIn("[WARNING]", plain)
        self.assertIn("the source changed underneath us", plain)
        self.assertLess(plain.index("40%"), plain.index("[WARNING]"),
                        "the bar is committed above the log line, not overwritten by it")
        self.assertGreaterEqual(drawn.count("\n"), 1)

    def test_an_error_log_line_is_red_when_the_console_takes_colour(self) -> None:
        console, stream = self.console(color=True)
        console.log_line("22:31:05", "ERROR", "verification failed")
        drawn = stream.getvalue()
        self.assertIn("\033[", drawn)
        self.assertIn(tc.LiveConsole.RED, drawn)
        self.assertIn("verification failed", strip_ansi(drawn))

    def test_a_plain_console_draws_no_colour(self) -> None:
        """``--no-color`` means no SGR sequences; erasing the line is not colour."""
        console, stream = self.console(color=False)
        console.begin_file("", "Alpha (2001).mkv", 4096)
        console.log_line("22:31:05", "ERROR", "verification failed")
        console.end_file_inline("ERROR: verification failed", kind="error")
        drawn = stream.getvalue()
        self.assertNotIn(tc.LiveConsole.RED, drawn)
        self.assertNotIn("\033[0m", drawn)
        self.assertIn("verification failed", strip_ansi(drawn))

    def test_a_console_that_cannot_erase_pads_instead_of_escaping(self) -> None:
        """A terminal without VT mode would print the erase sequence as text."""
        console, stream = self.console(color=False)
        console.line.can_erase = False
        console.begin_file("", "Alpha (2001).mkv", 4096)
        console.end_file_inline("cleaned", kind="success")
        self.assertNotIn("\033", stream.getvalue())
        self.assertIn("cleaned", stream.getvalue())

    def test_details_are_committed_under_the_file_line_and_indented_to_it(self) -> None:
        console, stream = self.console()
        console.begin_file("[  1/3] ", "Alpha (2001).mkv", 4096)
        console.detail("-> audio       : keeping track 1")
        console.detail("-> subtitles   : removing 2", kind="warn")
        plain = strip_ansi(stream.getvalue())
        self.assertIn("-> audio       : keeping track 1", plain)
        self.assertIn("-> subtitles   : removing 2", plain)
        lines = [line for line in plain.splitlines() if "->" in line]
        self.assertEqual(len({len(line) - len(line.lstrip()) for line in lines}), 1,
                         "every detail lines up under the movie's own line")

    def test_a_scan_message_is_drawn_on_the_live_line(self) -> None:
        """Discovery takes minutes on a big library and must still look alive."""
        console, stream = self.console()
        console.progress_message("Scanning...  17 movie file(s) found")
        self.assertIn("Scanning...  17 movie file(s) found", strip_ansi(stream.getvalue()))
        self.assertIn("\r", stream.getvalue())
        self.assertNotIn("\n", stream.getvalue(), "a scan message is not a finished line")

    def test_finish_progress_commits_whatever_is_open(self) -> None:
        console, stream = self.console()
        console.begin_file("", "Alpha (2001).mkv", 4096)
        console.finish_progress()
        self.assertEqual(stream.getvalue().count("\n"), 1)
        console.finish_progress()
        self.assertEqual(stream.getvalue().count("\n"), 1, "committing twice draws nothing")

    def test_a_console_that_has_gone_away_costs_the_line_not_the_run(self) -> None:
        """Six hours in, stdout can close; the sweep has to keep going.

        ``_commit_open_line`` writes the newline through the raw writer, which
        is the call that fails first when the terminal is gone.
        """
        console, _stream = self.console()
        console.begin_file("", "Alpha (2001).mkv", 4096)
        with mock.patch.object(tc, "write_raw",
                               mock.Mock(side_effect=ValueError("I/O on closed file"))):
            console.log_line("22:31:05", "INFO", "still going")  # must not raise
        self.assertFalse(console._file_line_pending, "the open line was committed anyway")

    def test_an_outcome_with_no_open_file_line_is_printed_on_its_own(self) -> None:
        """``end_file_inline`` before ``begin_file``: a skip decided before the draw."""
        console, stream = self.console()
        console.end_file_inline("skipped (noncanonical layout)", kind="warn")
        self.assertIn("skipped (noncanonical layout)", strip_ansi(stream.getvalue()))
        self.assertIn("\n", stream.getvalue())


class PipeConsoleTests(unittest.TestCase):
    """The same renderer with its output redirected - the way CI and logs see it."""

    def console(self) -> tuple[tc.LiveConsole, io.StringIO]:
        stream = io.StringIO()
        redirect = redirect_stdout(stream)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        return tc.LiveConsole(use_color=False), stream

    def test_progress_is_one_line_per_ten_percent_when_there_is_nothing_to_overwrite(self) -> None:
        console, stream = self.console()
        for percent in (11, 12, 13, 19, 21, 22):
            console.remux_progress(percent, time.monotonic())
        lines = [line for line in stream.getvalue().splitlines() if "remux" in line]
        self.assertEqual(len(lines), 2, "the 10s and the 20s, and nothing between them")
        self.assertNotIn("\r", stream.getvalue())

    def test_the_final_percentage_is_always_printed(self) -> None:
        console, stream = self.console()
        console.remux_progress(100, time.monotonic())
        console.remux_progress(100, time.monotonic())
        lines = [line for line in stream.getvalue().splitlines() if "100%" in line]
        self.assertEqual(len(lines), 2, "100% is the one a watcher waits for")

    def test_a_scan_message_off_a_terminal_is_rate_limited_not_redrawn(self) -> None:
        """There is nothing to overwrite in a pipe, so the messages are spaced out."""
        console, stream = self.console()
        console.progress_message("Scanning...  1 movie file(s) found")
        self.assertIn("1 movie file(s) found", stream.getvalue())
        console.progress_message("Scanning...  2 movie file(s) found")
        self.assertNotIn("2 movie file(s) found", stream.getvalue(),
                         "the second one is inside the two-second window")
        console._last_progress_draw = time.monotonic() - 5
        console.progress_message("Scanning...  3 movie file(s) found")
        self.assertIn("3 movie file(s) found", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
