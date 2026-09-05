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


def print_text(text: str) -> None:
    """Print report text without ever raising on a legacy console encoding.

    Reports contain box-drawing characters.  On a console or pipe whose
    encoding cannot represent them, ``print`` raises ``UnicodeEncodeError``,
    which used to surface as a crash *after* the work was already done.  The
    fallback writes the same text with unrepresentable characters replaced.
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        try:
            encoding = sys.stdout.encoding or "utf-8"
            sys.stdout.buffer.write((text + "\n").encode(encoding, errors="replace"))
            sys.stdout.buffer.flush()
        except Exception:  # noqa: BLE001  # pragma: no cover
            # Last-resort console fallback: whatever the stream did wrong, the
            # line still has to reach the user. Re-raising here would abort a
            # sweep because of a console encoding quirk.
            print(text.encode("ascii", errors="replace").decode("ascii"), flush=True)
