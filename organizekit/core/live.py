"""The overwritable status line, defined once.

``mkv_track_cleaner.py`` has always drawn a live line: one row per movie that
is rewritten in place as the remux advances, so an hours-long queue says what
it is doing right now instead of going quiet. The other four sweeps went quiet.
They print a line *after* each movie, which is the right permanent record and
tells you nothing while a 40 GB file is being probed, an ffsubsync run is
correlating audio, or a 3,000-folder walk is halfway through.

This module is the part of that renderer that is not about remuxing: what the
terminal will accept (already answered by :mod:`organizekit.core.console`), how
wide it is, how to draw over the previous line, and how to erase it again. What
each tool draws with it stays in that tool.

Two rules make it safe to adopt everywhere:

* **A live line exists only on a terminal.** Without a TTY every drawing method
  returns immediately, so a redirected run, a cron job, a pipe into ``tee`` and
  every captured test see byte-identical output to the day before adoption. A
  progress line in a log file is noise, not progress.
* **A permanent line always erases the temporary one first.** ``RunLog`` clears
  the open live line under the same lock it prints with, so a status line can
  never be left stranded above a real log line, nor half-overwritten by one.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from threading import RLock

from .console import Ansi, color_enabled, print_text, stream_can_encode, style, write_raw

__all__ = ["LiveLine", "ellipsize", "format_left", "strip_ansi"]

_ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")

#: Terminal width assumed when the console will not report one.
DEFAULT_COLUMNS = 100
#: Never draw into a window narrower than this; below it, truncation is worse
#: than a wrapped line.
MIN_COLUMNS = 40


def strip_ansi(text: str) -> str:
    """Return ``text`` without SGR escapes, for measuring its printed width."""
    return _ANSI_RE.sub("", text)


def ellipsize(text: str, max_len: int) -> str:
    """Shorten ``text`` to ``max_len`` from the left, keeping the tail.

    Paths are ellipsized from the front on purpose: ``...Movie (2020).mkv`` is
    the part that identifies the file, and the shared prefix of every path in a
    library is the part that does not.
    """
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return "..." + text[-(max_len - 3):]


def format_left(seconds: float) -> str:
    """A compact ``2h05m`` / ``7m30s`` / ``45s`` estimate for a live line.

    Deliberately not one of the three duration formats the reports use: this
    string is never written to a report or a log, so it is free to be short.
    """
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


class LiveLine:
    """One line of terminal output that is redrawn in place.

    ``use_color`` follows the tool's ``--no-color`` flag: ``None`` means "ask
    the console". ``stream`` defaults to stdout and is resolved once, at
    construction, exactly like the capability questions it asks about it.
    """

    def __init__(self, *, use_color: bool | None = None, stream: object | None = None) -> None:
        self.stream = stream
        out = stream if stream is not None else sys.stdout
        try:
            self.is_tty = bool(out and out.isatty())
        except (OSError, ValueError, AttributeError):
            self.is_tty = False
        self.use_color = color_enabled(use_color=use_color, stream=stream)
        # An erase escape needs a console that takes escapes. Colour proves it;
        # otherwise trust a real terminal anywhere but Windows, where a console
        # without VT mode would print the escape literally.
        self.can_erase = self.use_color or (os.name != "nt" and self.is_tty)
        if stream_can_encode("█░", stream):
            self.bar_fill, self.bar_empty = "█", "░"
        else:
            self.bar_fill, self.bar_empty = "#", "-"
        self._open = False
        # Re-entrant: a tool may clear the line from inside its own draw path.
        self._lock = RLock()

    # -- capability -------------------------------------------------------

    def style(self, text: str, *codes: str) -> str:
        return style(text, *codes, enabled=self.use_color)

    def columns(self) -> int:
        try:
            return max(MIN_COLUMNS, int(shutil.get_terminal_size((DEFAULT_COLUMNS, 24)).columns))
        except (OSError, ValueError):
            return DEFAULT_COLUMNS

    def fit(self, text: str, reserved: int = 0) -> str:
        """Shorten ``text`` so it and ``reserved`` other columns fit the width."""
        return ellipsize(text, max(8, self.columns() - 1 - reserved))

    def bar(self, percent: float, width: int = 22) -> str:
        """A ``[████░░░░]``-style bar, coloured when the console allows it."""
        percent = 0.0 if percent < 0 else 100.0 if percent > 100 else float(percent)
        filled = max(0, min(width, int(round(width * percent / 100.0))))
        if self.use_color:
            return (self.style(self.bar_fill * filled, Ansi.CYAN)
                    + self.style(self.bar_empty * (width - filled), Ansi.DIM))
        return self.bar_fill * filled + self.bar_empty * (width - filled)

    # -- drawing ----------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """True when a temporary line is on screen and not yet cleared."""
        return self._open

    def overwrite(self, text: str) -> None:
        """Draw ``text`` over the current line. Caller has checked the TTY.

        Used directly by ``mkv_track_cleaner``'s renderer, which draws lines
        this class does not know how to compose.
        """
        cols = self.columns()
        visible = strip_ansi(text)
        if len(visible) > cols - 1:
            keep = max(1, cols - 1)
            text = visible[:keep] if keep <= 3 else "..." + visible[-(keep - 3):]
            visible = text
        try:
            if self.can_erase:
                write_raw("\r" + text + "\033[K", self.stream)
            else:
                write_raw("\r" + text + (" " * max(0, cols - 1 - len(visible))), self.stream)
        except (OSError, ValueError):
            # write_raw already swallows a dead stdout; this is the last resort
            # for a stream that fails in some other way. The line is lost, the
            # run is not.
            try:
                print_text(strip_ansi(text), self.stream)
            except (OSError, ValueError):
                pass

    def update(self, text: str) -> None:
        """Show ``text`` as the live status line. A no-op off a terminal."""
        if not self.is_tty:
            return
        with self._lock:
            self.overwrite(text)
            self._open = True

    def progress(self, done: int, total: int, *, label: str = "", detail: str = "",
                 started: float | None = None, width: int = 22) -> None:
        """Draw ``label [bar] 42%  12/340  ~3m left  detail``.

        ``started`` is a ``time.monotonic()`` stamp from the beginning of the
        run; with it the line carries an estimate, which is the number an
        operator actually wants from a sweep of a few thousand movies. The
        estimate is deliberately naive — remaining items times the mean so far
        — and it says ``~`` because it is.
        """
        if not self.is_tty:
            return
        total = max(0, int(total))
        done = max(0, min(int(done), total)) if total else max(0, int(done))
        percent = (100.0 * done / total) if total else 0.0
        counter = f"{done}/{total}" if total else str(done)
        left = ""
        if started is not None and done > 0 and total > done:
            elapsed = time.monotonic() - started
            if elapsed > 0.5:
                left = f"  ~{format_left(elapsed / done * (total - done))} left"
        head = f"{label} " if label else ""
        tail = f"  {percent:3.0f}%  {counter}{left}"
        # head + "[" + bar + "]" + tail + two spaces before the detail.
        reserved = len(head) + len(tail) + width + 4
        fitted = ellipsize(detail, max(0, self.columns() - 1 - reserved)) if detail else ""
        body = f"  {fitted}" if fitted else ""
        if self.use_color:
            text = (self.style(head, Ansi.CYAN) + "[" + self.bar(percent, width) + "]"
                    + self.style(tail, Ansi.DIM) + body)
        else:
            text = f"{head}[{self.bar(percent, width)}]{tail}{body}"
        self.update(text)

    def clear(self) -> None:
        """Erase the live line if one is showing. A no-op off a terminal."""
        if not self.is_tty:
            return
        with self._lock:
            if not self._open:
                return
            self._open = False
            cols = self.columns()
            if self.can_erase:
                write_raw("\r\033[K", self.stream)
            else:
                write_raw("\r" + " " * max(0, cols - 1) + "\r", self.stream)

    def commit(self) -> None:
        """Keep the live line by ending it with a newline."""
        if not self.is_tty:
            return
        with self._lock:
            if not self._open:
                return
            self._open = False
            write_raw("\n", self.stream)
