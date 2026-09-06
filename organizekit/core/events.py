"""An append-only JSONL record of a run, for something that is watching.

``doctor``, ``status`` and ``audit`` answer with one document because they
describe a *state*: the machine, or the library, as it is right now. A pipeline
run is not a state. It is a sequence of things that happened over an hour, and
the interesting questions about it - which step is running, how long did the
remux take, did the fetcher fail at 03:12 - cannot be answered by a file that
only exists once the run is over.

So a run writes a stream: one JSON object per line, appended as it goes, which
``tail -f`` follows, `jq` filters and a log shipper ingests without knowing
anything about this toolkit.

Two rules differ from :mod:`organizekit.core.jsonout`, on purpose:

* **Event lines carry the clock.** The no-timestamp rule exists so that two
  reads of an unchanged library produce identical bytes; a run has no such
  property to protect, and a monitoring stream without times is useless for
  the one job it has.
* **The stream is best-effort, never fatal.** A full disk or a read-only
  directory costs the operator a record, not the remux queue that is already
  running - the same rule the run log has followed since it was consolidated.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .jsonout import json_document

__all__ = ["EventStream"]


class EventStream:
    """Append one JSON line per event to ``path``; do nothing when disabled.

    A disabled stream (``path=None``) is the default everywhere, so a run that
    nobody is watching costs nothing and behaves exactly as it did before this
    existed.
    """

    def __init__(self, path: Path | None, version: str, command: str) -> None:
        self.path = path
        self._version = version
        self._command = command
        #: Set once a write has failed, so a broken destination is reported
        #: once rather than on every event for the rest of the run.
        self._broken = False

    @property
    def enabled(self) -> bool:
        return self.path is not None and not self._broken

    def line(self, event: str, **fields: object) -> dict[str, object]:
        """The document a call to :meth:`emit` would write."""
        return json_document(
            self._command,
            self._version,
            event=event,
            time=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **fields,
        )

    def emit(self, event: str, **fields: object) -> None:
        """Append one event. A *write* failure here never ends the run.

        An ``OSError`` costs the operator a record and nothing more. Anything
        else - a field named ``command``, say, which the envelope already owns
        - is a bug in the caller and is allowed to surface, exactly as the run
        log's non-``OSError`` failures are.
        """
        if not self.enabled:
            return
        assert self.path is not None  # narrowed by .enabled
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                # One line, one write: a reader tailing the file must never see
                # half an event, and a crash must not leave a torn record.
                handle.write(json.dumps(self.line(event, **fields), ensure_ascii=False) + "\n")
        except OSError as exc:
            self._broken = True
            print(f"  note: run events disabled ({self.path}: {exc})", file=sys.stderr, flush=True)
