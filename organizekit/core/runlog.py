"""The run log line, defined once.

Four tools had written the same twenty lines: stamp the message, print it,
append it to this run's log file, and never let a logging problem end a sweep
that has already done real work. They agreed on all of that and differed only
in accidents — one held a print lock, one did not; one replaced unencodable
characters on the way to the file, one raised on them — which is the usual
shape of a copy that has been maintained four times.

The rules, now stated in one place:

* **A logging failure is never a run failure.** A full disk, a read-only log
  directory or a console that cannot encode an em dash must not abort a remux
  queue or a subtitle sweep. Every write here is best-effort.
* **The console and the file get the identical line.** Support questions are
  answered from the log file, so it has to say exactly what the operator saw.
* **One line is one line.** The lock makes a worker pool's output readable:
  without it, two threads interleave mid-line and the log becomes evidence of
  nothing.
* **A permanent line erases the temporary one.** A tool that draws a live
  status line (``organizekit/core/live.py``) hands it to :attr:`RunLog.live`,
  and it is cleared before every logged line — otherwise "probing Movie.mkv"
  would be left stranded on screen with a real log line printed over half of
  it. Off a terminal there is no live line and this costs nothing.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Lock

from .console import print_text
from .live import LiveLine

__all__ = ["RunLog"]


class RunLog:
    """A timestamped line to the console and, if one is set, to a log file.

    Set :attr:`file` once the configuration is parsed and every later call can
    omit the destination; a call may still pass ``log_file=`` to override it
    (some tools log to a per-step file before the run log exists).

    ``brackets`` selects the ``[time] [LEVEL] message`` variant used by the
    orchestrator's transcripts; the default is the ``time [LEVEL] message``
    form the individual tools write.
    """

    def __init__(self, *, brackets: bool = False) -> None:
        self.file: Path | None = None
        #: Where console lines go; ``None`` means stdout. A tool whose output
        #: is a JSON document points this at stderr, so the log is still there
        #: for the operator without corrupting what the parser reads.
        self.stream: object | None = None
        self._brackets = brackets
        #: Held while a line is written. Anything else that prints to the same
        #: console should take it too, or its output will interleave with ours.
        self.lock = Lock()
        #: The tool's overwritable status line, if it draws one. A permanent
        #: line always erases the temporary one first — under this lock, so a
        #: worker cannot slip a redraw between the erase and the print. Off a
        #: terminal every method on it is a no-op and nothing here changes.
        self.live: LiveLine | None = None

    def attach_live(self, *, use_color: bool | None = None) -> LiveLine:
        """Give this run an overwritable status line and return it.

        Call it after :attr:`stream` is settled, so a tool whose stdout is a
        JSON document draws its status on stderr with the rest of its log.
        Off a terminal the returned object is inert, which is what keeps a
        redirected run byte-identical to one that never had a live line.
        """
        self.live = LiveLine(use_color=use_color, stream=self.stream)
        return self.live

    def format(self, message: str, level: str = "INFO") -> str:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self._brackets:
            return f"[{stamp}] [{level}] {message}"
        return f"{stamp} [{level}] {message}"

    def __call__(self, message: str, level: str = "INFO",
                 log_file: Path | None = None) -> None:
        """Print the line and append it to the log file."""
        line = self.format(message, level)
        with self.lock:
            if self.live is not None:
                self.live.clear()
            print_text(line, self.stream)
            self._append(line, log_file)

    def to_file(self, message: str, level: str = "INFO",
                log_file: Path | None = None) -> None:
        """Append a line the console has already shown, or should not show."""
        line = self.format(message, level)
        with self.lock:
            self._append(line, log_file)

    def _append(self, line: str, log_file: Path | None) -> None:
        target = log_file if log_file is not None else self.file
        if target is None:
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(line + "\n")
        except OSError:
            # Deliberate: see the module docstring. A log that cannot be
            # written costs the operator a record, not the work in flight.
            pass
