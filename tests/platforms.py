"""Ask the platform-only branches what they would answer on the other platform.

A third of this toolkit's remaining untested code is the half of an
``if os.name == "nt":`` that a POSIX runner never reaches - and the CI coverage
job runs on Ubuntu, so on every measurement those branches looked dead while
being the ones a Windows operator actually executes. They are not dead: they
are the documented defaults (``E:\\torrents\\final``), the ``msvcrt`` locking
protocol, and the cp1252 console fallbacks.

Two things have to be true for such a branch to be exercised honestly:

* the switch the code reads has to say the other platform, and
* the rest of the interpreter has to keep working while it does.

``os.name`` is the switch every one of these branches reads, so that is what
gets patched. The second part is the trap: :class:`pathlib.Path` picks its
concrete class from ``os.name`` at construction time, so under a bare patch
every ``Path(...)`` inside the branch raises ``NotImplementedError`` instead of
building a Windows path - and the tests would be asserting a crash. Aliasing
``WindowsPath`` to the host's own class keeps construction working; the
resulting paths are compared as the literal strings the branch produced, which
is exactly the claim under test ("a Windows host with nothing configured gets
``E:\\torrents\\final``").

Import the modules *before* entering the context. A lazy import inside it would
fail for real: ``shutil`` imports ``nt`` when ``os.name`` says Windows.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
from collections.abc import Iterator
from unittest import mock

#: The concrete ``Path`` class this interpreter can actually instantiate. On
#: POSIX ``pathlib.PosixPath``; on Windows ``WindowsPath``. Aliasing the *other*
#: name to it is what keeps ``Path(...)`` working while ``os.name`` lies.
_HOST_PATH = type(pathlib.Path("."))


@contextlib.contextmanager
def windows() -> Iterator[None]:
    """Hold ``os.name`` at ``"nt"`` while keeping ``Path`` constructible."""
    with mock.patch.object(os, "name", "nt"), \
            mock.patch.object(pathlib, "WindowsPath", _HOST_PATH):
        yield


@contextlib.contextmanager
def posix() -> Iterator[None]:
    """The same, for the branch a Windows runner would otherwise skip."""
    with mock.patch.object(os, "name", "posix"), \
            mock.patch.object(pathlib, "WindowsPath", _HOST_PATH):
        yield
