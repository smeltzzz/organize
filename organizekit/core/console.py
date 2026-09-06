"""Console encoding, colour capability, and safe printing.

One decision, made once: *can this console take colour, and can it take these
characters?* It used to be made twice, differently. ``organize.py`` asked
``isatty() and not NO_COLOR and TERM != "dumb"`` and then poked Windows into
VT mode by **overwriting** the console mode with the literal ``7``;
``mkv_track_cleaner.py`` also honoured ``FORCE_COLOR`` and its own
``--no-color`` flag, and enabled VT by reading the existing mode and OR-ing the
one bit it needs. The same environment could therefore get colour from one tool
and not the other, and the tool that clobbered the mode discarded whatever else
the console had set.

The rendering above these helpers is still each tool's own: a scorecard and a
live remux progress bar want different things. What is shared is the question
of what the terminal will accept.
"""

from __future__ import annotations

import os
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


# =============================================================================
# COLOUR CAPABILITY
# =============================================================================


class Ansi:
    """The SGR codes the toolkit uses, named once.

    A class rather than module constants so a caller can write
    ``style(text, Ansi.BOLD, Ansi.RED)`` and read it back a year later.
    """

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"


def enable_windows_vt() -> bool:
    """Turn on ANSI escape handling for this console. True if it is available.

    Non-Windows consoles already understand escapes, so this is ``True`` there
    without touching anything.

    On Windows the mode is read, OR-ed with ``ENABLE_VIRTUAL_TERMINAL_
    PROCESSING`` (``0x0004``) and written back. Assigning a literal mode
    instead - which one tool used to do - silently drops every other flag the
    console had set, and colour is not worth that.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if not windll:
            return False
        kernel32 = windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        if not handle or handle == -1:
            return False
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001 - ctypes reports a bad call as ArgumentError,
        # OSError or AttributeError depending on where it fails, and colour is
        # optional: a console that will not take VT mode simply does not get it.
        return False


def color_enabled(*, use_color: bool | None = None, stream: object | None = None) -> bool:
    """Should this run print colour?

    In precedence order: an explicit ``--no-color`` (``use_color=False``) wins
    over everything, then an explicit ``--color`` or ``FORCE_COLOR``, then
    ``NO_COLOR`` and ``TERM=dumb``, and otherwise colour is on only for a real
    terminal. ``FORCE_COLOR`` matters more than it looks: it is how CI logs and
    ``script``-wrapped runs keep their colour when stdout is a pipe.

    The terminal must also accept the escapes, which on Windows means VT mode
    was available.
    """
    out = stream if stream is not None else sys.stdout
    try:
        is_tty = bool(out and out.isatty())
    except (OSError, ValueError, AttributeError):
        is_tty = False
    forced = (os.environ.get("FORCE_COLOR", "") or "").strip() not in ("", "0")
    if use_color is False:
        return False
    if use_color is True or forced:
        return enable_windows_vt()
    if os.environ.get("NO_COLOR") or (os.environ.get("TERM", "") or "").lower() == "dumb":
        return False
    return is_tty and enable_windows_vt()


def stream_can_encode(text: str, stream: object | None = None) -> bool:
    """Can this console represent ``text``? Used to pick glyphs over ASCII.

    A tick, a box-drawing rule and a progress bar's blocks are all nicer than
    ``[OK]``, ``-`` and ``#`` - and all unprintable on a cp437 console. Asking
    the stream's own encoding to encode the characters is the only reliable
    test; guessing from the encoding *name* is what made one tool print boxes
    on a console that could not show them.
    """
    out = stream if stream is not None else sys.stdout
    try:
        text.encode(getattr(out, "encoding", None) or "utf-8")
    except (LookupError, UnicodeError, AttributeError, ValueError):
        return False
    return True


def style(text: str, *codes: str, enabled: bool = True) -> str:
    """Wrap ``text`` in ANSI codes, or return it untouched.

    ``enabled`` is passed per call rather than read from a module global
    because the two callers decide differently: the CLI decides once at import,
    and the cleaner's live console decides per run from ``--no-color``.
    """
    if not enabled or not codes:
        return text
    return "".join(codes) + text + Ansi.RESET


def write_raw(text: str, stream: object | None = None) -> None:
    """Write without a newline and flush; never raise.

    This is what draws an overwriting progress line (``\r`` and no newline),
    so it cannot use ``print``. A stdout that has been closed under a
    long-running queue must not take the queue down with it: the line is lost,
    the work is not.
    """
    out = stream if stream is not None else sys.stdout
    try:
        out.write(text)
        out.flush()
    except (OSError, ValueError, AttributeError):
        pass
