"""Console encoding and safe printing."""

from __future__ import annotations

import sys


def enable_utf8_stdio() -> None:
    """Pin this process's console streams to UTF-8 with replacement errors.

    The reports are full of box-drawing characters, and every tool now prints
    one.  Two failures follow from leaving the stream encoding to the locale:
    a console that cannot represent ``\u2550`` raises ``UnicodeEncodeError``
    half-way through a run, and a parent that captures a child's output with
    ``text=True`` decodes it with the *locale* encoding - cp1252 on Windows -
    which turns those same bytes into a ``UnicodeDecodeError``.

    So every tool pins its own output to UTF-8 at startup, and every caller
    that captures a child decodes it as UTF-8.  ``errors="replace"`` means a
    console that still cannot cope degrades to ``?`` instead of aborting work
    that has already been done.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # a replaced stream, e.g. under redirect_stdout
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # closed or detached stream
            pass


def print_text(text: str, stream: object | None = None) -> None:
    """Print report text without ever raising on a legacy console encoding.

    Reports contain box-drawing characters.  On a console or pipe whose
    encoding cannot represent them, ``print`` raises ``UnicodeEncodeError``,
    which used to surface as a crash *after* the work was already done.  The
    fallback writes the same text with unrepresentable characters replaced.

    ``stream`` sends the line somewhere other than stdout - what a tool
    printing a JSON document does with its progress log, so the document has
    stdout to itself.  It is resolved per call, because ``sys.stdout`` is
    replaced under ``redirect_stdout`` and captured tests.
    """
    out = stream if stream is not None else sys.stdout
    try:
        print(text, file=out, flush=True)
    except UnicodeEncodeError:
        try:
            encoding = getattr(out, "encoding", None) or "utf-8"
            out.buffer.write((text + "\n").encode(encoding, errors="replace"))
            out.buffer.flush()
        except Exception:  # noqa: BLE001  # pragma: no cover
            # Last-resort console fallback: whatever the stream did wrong, the
            # line still has to reach the user. Re-raising here would abort a
            # sweep because of a console encoding quirk.
            print(text.encode("ascii", errors="replace").decode("ascii"), file=out, flush=True)
