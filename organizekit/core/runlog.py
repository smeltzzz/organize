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
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Lock

from .console import print_text

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
        self._brackets = brackets
        #: Held while a line is written. Anything else that prints to the same
        #: console should take it too, or its output will interleave with ours.
        self.lock = Lock()

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
            print_text(line)
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
