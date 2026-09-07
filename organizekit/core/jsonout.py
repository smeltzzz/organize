"""The shape every ``--json`` document in this toolkit shares.

Three commands now answer in JSON — ``doctor``, ``status`` and ``audit`` — and
two of them live in different files from the third. Without one definition of
the envelope they would drift the way the four copies of the run log did:
same idea, different key names, and a consumer writing a branch per command.

The rules, stated once:

* **One envelope.** ``schema``, ``tool``, ``version``, ``command`` come first
  in every document, so a parser can identify a file it is holding and refuse
  a shape it does not understand.
* **One schema number.** It is bumped when the shape changes, not when a
  value does.
* **No timestamps.** The caller already knows when it ran. Leaving the clock
  out means two runs over an unchanged machine or library produce identical
  bytes, so a scheduled job can diff today's document against yesterday's and
  alert only when something really changed.
* **The document owns stdout.** Progress lines, warnings and logs go to
  stderr; one decorative line on stdout breaks every parser downstream.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from typing import TextIO

__all__ = ["JSON_SCHEMA", "json_document", "print_json", "slug_id"]

#: Bumped when the shape of a document changes, never when a value does.
JSON_SCHEMA = 1


def slug_id(name: str) -> str:
    """A stable machine-readable id derived from a human label.

    Labels are prose - ``MKVToolNix (mkvmerge)``, ``Bit depth`` - and a
    consumer needs something to match on that survives rewording of the
    punctuation and capitalisation: ``mkvtoolnix-mkvmerge``, ``bit-depth``.
    """
    slug = "".join(char.lower() if char.isalnum() else "-" for char in name)
    return "-".join(part for part in slug.split("-") if part)


def json_document(command: str, version: str, **payload: object) -> dict[str, object]:
    """Wrap a command's payload in the envelope every JSON document shares."""
    return {
        "schema": JSON_SCHEMA,
        "tool": "organize",
        "version": version,
        "command": command,
        **payload,
    }


def print_json(document: dict[str, object], stream: TextIO | None = None) -> None:
    """Print the document and nothing else, so stdout stays parseable."""
    print(json.dumps(document, indent=2, ensure_ascii=False),
          file=stream if stream is not None else sys.stdout)
