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
*both* concrete classes to the host's own class keeps construction working in
either direction (a POSIX host asked to behave like Windows, and a Windows host
asked to behave like POSIX); the resulting paths are compared as the literal
strings the branch produced, which is exactly the claim under test ("a Windows
host with nothing configured gets ``E:\\torrents\\final``").

Both namespaces are patched because Python 3.13 split ``pathlib`` into a
package: ``Path.__new__`` resolves ``WindowsPath``/``PosixPath`` from
``pathlib._local``'s globals there, so aliasing only the names re-exported on
the package leaves 3.13 building the real class - which raises
``NotImplementedError`` as soon as the branch divides a path.

Import the modules *before* entering the context. A lazy import inside it would
fail for real: ``shutil`` imports ``nt`` when ``os.name`` says Windows.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import sys
from collections.abc import Iterator
from unittest import mock

#: The concrete ``Path`` class this interpreter can actually instantiate. On
#: POSIX ``pathlib.PosixPath``; on Windows ``WindowsPath``. Aliasing the *other*
#: name to it is what keeps ``Path(...)`` working while ``os.name`` lies.
_HOST_PATH = type(pathlib.Path("."))


def _pathlib_namespaces() -> list[object]:
    """Every namespace ``Path`` looks its concrete classes up in."""
    namespaces: list[object] = [pathlib]
    local = sys.modules.get("pathlib._local")  # Python 3.13+
    if local is not None and local is not pathlib:
        namespaces.append(local)
    return namespaces


@contextlib.contextmanager
def _host_flavours() -> Iterator[None]:
    """Make both concrete path classes buildable on this interpreter."""
    with contextlib.ExitStack() as stack:
        for namespace in _pathlib_namespaces():
            for name in ("WindowsPath", "PosixPath"):
                if hasattr(namespace, name):
                    stack.enter_context(
                        mock.patch.object(namespace, name, _HOST_PATH))
        yield


@contextlib.contextmanager
def windows() -> Iterator[None]:
    """Hold ``os.name`` at ``"nt"`` while keeping ``Path`` constructible."""
    with mock.patch.object(os, "name", "nt"), _host_flavours():
        yield


@contextlib.contextmanager
def posix() -> Iterator[None]:
    """The same, for the branch a Windows runner would otherwise skip."""
    with mock.patch.object(os, "name", "posix"), _host_flavours():
        yield
