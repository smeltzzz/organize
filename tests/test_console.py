"""One console decision, shared by the CLI and the cleaner's live renderer.

This used to be asked twice and answered differently. `organize.py` looked at
`isatty()`, `NO_COLOR` and `TERM`, ignored `FORCE_COLOR`, and turned on Windows
VT mode by *assigning* the console mode the literal `7` - which enables the
three bits in it and silently clears every other flag the console had.
`mkv_track_cleaner.py` honoured `FORCE_COLOR` and its own `--no-color`, and
enabled VT by reading the mode and OR-ing in the one bit it needs. The same
terminal could get colour from one tool and plain text from the other.

These tests pin the answer, including the precedence between the four things
that can decide it.
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import subprocess
import sys
import types
import unittest
from unittest import mock

from organizekit.core import (
    Ansi,
    color_enabled,
    print_text,
    stream_can_encode,
    style,
    write_raw,
)
from organizekit.core import console as console_mod

COLOR_ENV = ("NO_COLOR", "FORCE_COLOR", "TERM")
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class FakeStream:
    """A console with the two properties these helpers ask about."""

    def __init__(self, tty: bool = True, encoding: str = "utf-8") -> None:
        self._tty = tty
        self.encoding = encoding
        self.closed = False
        self._text: list[str] = []

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        self._text.append(text)
        return len(text)

    def flush(self) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")

    def close(self) -> None:
        self.closed = True

    def getvalue(self) -> str:
        return "".join(self._text)


class ColorDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in COLOR_ENV:
            os.environ.pop(name, None)
        # Windows VT is a property of the machine, not of these rules.
        vt = mock.patch.object(console_mod, "enable_windows_vt", return_value=True)
        vt.start()
        self.addCleanup(vt.stop)

    def test_a_terminal_gets_colour(self) -> None:
        self.assertTrue(color_enabled(stream=FakeStream(tty=True)))

    def test_a_pipe_does_not(self) -> None:
        """Redirect the output to a file and it should not fill with escapes."""
        self.assertFalse(color_enabled(stream=FakeStream(tty=False)))

    def test_no_color_beats_a_terminal(self) -> None:
        os.environ["NO_COLOR"] = "1"
        self.assertFalse(color_enabled(stream=FakeStream(tty=True)))

    def test_a_dumb_terminal_is_not_a_terminal_for_this_purpose(self) -> None:
        os.environ["TERM"] = "dumb"
        self.assertFalse(color_enabled(stream=FakeStream(tty=True)))

    def test_force_color_beats_a_pipe(self) -> None:
        """How a CI log or a `script`-wrapped run keeps its colour."""
        os.environ["FORCE_COLOR"] = "1"
        self.assertTrue(color_enabled(stream=FakeStream(tty=False)))

    def test_force_color_zero_is_not_forcing_anything(self) -> None:
        os.environ["FORCE_COLOR"] = "0"
        self.assertFalse(color_enabled(stream=FakeStream(tty=False)))

    def test_an_explicit_no_beats_everything(self) -> None:
        """`--no-color` is the user saying it plainly; nothing may override it."""
        os.environ["FORCE_COLOR"] = "1"
        self.assertFalse(color_enabled(use_color=False, stream=FakeStream(tty=True)))

    def test_an_explicit_yes_beats_no_color(self) -> None:
        os.environ["NO_COLOR"] = "1"
        self.assertTrue(color_enabled(use_color=True, stream=FakeStream(tty=False)))

    def test_a_console_that_will_not_take_escapes_gets_none(self) -> None:
        with mock.patch.object(console_mod, "enable_windows_vt", return_value=False):
            self.assertFalse(color_enabled(use_color=True, stream=FakeStream(tty=True)))

    def test_a_stream_that_cannot_answer_is_not_a_terminal(self) -> None:
        class Awkward:
            def isatty(self):
                raise ValueError("detached")

        self.assertFalse(color_enabled(stream=Awkward()))


class FakeKernel32:
    """Enough of the Windows console API to watch what gets written to it."""

    def __init__(self, mode: int, *, get_ok: bool = True, set_ok: bool = True) -> None:
        self.mode = mode
        self._get_ok = get_ok
        self._set_ok = set_ok
        self.raise_on_get: Exception | None = None
        self.written: list[int] = []

    def GetStdHandle(self, which: int) -> int:  # noqa: N802 - the Win32 spelling
        return 11

    def GetConsoleMode(self, handle: int, out: FakeUint) -> int:  # noqa: N802
        if self.raise_on_get is not None:
            raise self.raise_on_get
        if not self._get_ok:
            return 0
        out.value = self.mode
        return 1

    def SetConsoleMode(self, handle: int, mode: int) -> int:  # noqa: N802
        self.written.append(mode)
        return 1 if self._set_ok else 0


class FakeUint:
    def __init__(self) -> None:
        self.value = 0


def fake_windows(kernel32: FakeKernel32):
    """Run a block as though it were on a Windows console."""
    fake_ctypes = types.SimpleNamespace(
        windll=types.SimpleNamespace(kernel32=kernel32),
        c_uint32=FakeUint,
        byref=lambda obj: obj,
    )
    return _fake_windows_stack(fake_ctypes)


@contextlib.contextmanager
def _fake_windows_stack(fake_ctypes: object):
    with (
        mock.patch.object(console_mod.os, "name", "nt"),
        mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}),
    ):
        yield


class WindowsVtTests(unittest.TestCase):
    def test_a_posix_console_needs_no_help(self) -> None:
        if os.name == "nt":  # pragma: no cover - the POSIX branch is what is tested
            self.skipTest("this asserts the non-Windows shortcut")
        self.assertTrue(console_mod.enable_windows_vt())

    def test_the_existing_console_mode_is_preserved(self) -> None:
        """Assigning `7` was the bug: it clears every flag it does not set."""
        kernel32 = FakeKernel32(mode=0x0080)  # some flag this code knows nothing about
        with fake_windows(kernel32):
            self.assertTrue(console_mod.enable_windows_vt())
        self.assertEqual(kernel32.written, [0x0080 | 0x0004])

    def test_a_console_already_in_vt_mode_is_left_alone(self) -> None:
        kernel32 = FakeKernel32(mode=0x0080 | 0x0004)
        with fake_windows(kernel32):
            self.assertTrue(console_mod.enable_windows_vt())
        self.assertEqual(kernel32.written, [], "nothing to change, so nothing is written")

    def test_a_console_that_refuses_the_mode_gets_no_colour(self) -> None:
        kernel32 = FakeKernel32(mode=0, set_ok=False)
        with fake_windows(kernel32):
            self.assertFalse(console_mod.enable_windows_vt())

    def test_a_handle_that_cannot_be_queried_is_not_a_crash(self) -> None:
        kernel32 = FakeKernel32(mode=0, get_ok=False)
        with fake_windows(kernel32):
            self.assertFalse(console_mod.enable_windows_vt())

    def test_a_ctypes_call_that_blows_up_is_not_a_crash(self) -> None:
        kernel32 = FakeKernel32(mode=0)
        kernel32.raise_on_get = OSError("no console")
        with fake_windows(kernel32):
            self.assertFalse(console_mod.enable_windows_vt())


class GlyphSupportTests(unittest.TestCase):
    def test_a_utf8_console_takes_the_nice_characters(self) -> None:
        self.assertTrue(stream_can_encode("█░✔─", FakeStream(encoding="utf-8")))

    def test_a_legacy_console_does_not(self) -> None:
        """cp437 has the block glyphs but not the tick; both are asked about."""
        self.assertFalse(stream_can_encode("✔", FakeStream(encoding="cp437")))
        self.assertFalse(stream_can_encode("█░", FakeStream(encoding="ascii")))

    def test_ascii_is_fine_anywhere(self) -> None:
        self.assertTrue(stream_can_encode("#-", FakeStream(encoding="ascii")))

    def test_an_unknown_encoding_is_a_no_not_a_crash(self) -> None:
        self.assertFalse(stream_can_encode("x", FakeStream(encoding="no-such-codec")))

    def test_a_stream_with_no_encoding_is_assumed_utf8(self) -> None:
        class Bare:
            pass

        self.assertTrue(stream_can_encode("█", Bare()))


class StyleTests(unittest.TestCase):
    def test_codes_wrap_the_text_and_reset_after_it(self) -> None:
        self.assertEqual(style("hi", Ansi.BOLD), f"{Ansi.BOLD}hi{Ansi.RESET}")

    def test_several_codes_combine(self) -> None:
        self.assertEqual(style("hi", Ansi.BOLD, Ansi.RED),
                         f"{Ansi.BOLD}{Ansi.RED}hi{Ansi.RESET}")

    def test_disabled_returns_the_text_untouched(self) -> None:
        self.assertEqual(style("hi", Ansi.BOLD, enabled=False), "hi")

    def test_no_codes_means_no_escapes(self) -> None:
        self.assertEqual(style("hi"), "hi")


class WriteRawTests(unittest.TestCase):
    def test_it_writes_without_a_newline(self) -> None:
        """A progress line is redrawn in place, so `print` cannot be used."""
        stream = FakeStream()
        write_raw("\r 42%", stream)
        self.assertEqual(stream.getvalue(), "\r 42%")

    def test_a_dead_stream_costs_a_line_not_the_run(self) -> None:
        stream = FakeStream()
        stream.close()
        write_raw("still here?", stream)  # must not raise

    def test_a_stream_that_is_not_one_is_survived(self) -> None:
        write_raw("x", object())  # must not raise


class PrintTextTests(unittest.TestCase):
    def test_a_console_that_cannot_encode_gets_replacements_not_an_exception(self) -> None:
        class Cp437(io.StringIO):
            encoding = "cp437"

            def write(self, text: str) -> int:
                text.encode("cp437")  # what a real cp437 console does
                return super().write(text)

        stream = Cp437()
        stream.buffer = io.BytesIO()
        print_text("tick ✔ here", stream)
        self.assertIn(b"tick", stream.buffer.getvalue())
        self.assertNotIn("✔", stream.getvalue(), "the raw write never landed")


class ForcedColourTests(unittest.TestCase):
    """An explicit request beats what the console will admit to."""

    def test_force_color_survives_a_console_that_cannot_take_vt_mode(self) -> None:
        """On Windows a redirected stdout is not a console at all.

        `GetConsoleMode` fails for a pipe, so asking whether VT mode could be
        enabled answers "no" - which is precisely the case FORCE_COLOR exists
        to override, and the CLI used to drop the colour anyway.
        """
        with mock.patch.object(console_mod, "enable_windows_vt", return_value=False), \
             mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=False):
            piped, terminal = FakeStream(tty=False), FakeStream(tty=True)
            self.assertTrue(color_enabled(stream=piped), "FORCE_COLOR into a pipe")
            self.assertTrue(color_enabled(use_color=True, stream=piped), "--color into a pipe")
            self.assertFalse(color_enabled(use_color=False, stream=piped), "--no-color wins")
            self.assertFalse(color_enabled(use_color=True, stream=terminal),
                             "a real console that refuses VT would show the escapes")


class CliWiringTests(unittest.TestCase):
    """The CLI reads these helpers at import time; check what it concluded.

    A subprocess, because the answer is baked into module-level constants and
    depends on the encoding of the interpreter's own stdout.
    """

    def _symbols(self, env_extra: dict[str, str]) -> str:
        env = {**os.environ, **env_extra}
        for key in COLOR_ENV:
            if key not in env_extra:
                env.pop(key, None)
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", "import organize; print(organize.SYM_OK, organize.HRULE)"],
            capture_output=True,
            text=True,
            # The child prints what its own PYTHONIOENCODING says; decoding it
            # with the parent's locale (cp1252 on Windows CI) makes a correct
            # tick look like mojibake.
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO_ROOT),
            env=env,
            check=True,
        )
        return proc.stdout.strip()

    def test_a_utf8_console_gets_the_tick(self) -> None:
        self.assertEqual(self._symbols({"PYTHONIOENCODING": "utf-8"}), "✔ ─")

    def test_a_legacy_console_gets_ascii_instead_of_a_crash(self) -> None:
        """This is the windows-latest CI failure that started all of it."""
        self.assertEqual(self._symbols({"PYTHONIOENCODING": "ascii"}), "[OK] -")

    def test_force_color_reaches_the_cli(self) -> None:
        """The CLI ignored FORCE_COLOR before the shared helper; a pipe is not a tty."""
        out = self._symbols({"PYTHONIOENCODING": "utf-8", "FORCE_COLOR": "1"})
        self.assertIn("\033[", out)
        self.assertNotIn("\033[", self._symbols({"PYTHONIOENCODING": "utf-8"}))


if __name__ == "__main__":
    unittest.main()
