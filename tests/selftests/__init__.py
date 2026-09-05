"""The tools' own offline self-tests, moved out of the shipped files.

Every tool used to carry its complete offline suite inside the production file
— 2,229 lines across the toolkit, and in ``subtitle_fetcher.py``'s case 1,047
lines on top of the 1,811-line unit-test module covering the same code. That
made the shipped files longer, depressed the measured coverage of the tools
(self-test code is production code that the unit suite never runs), and split
the assertions for one behaviour across two homes.

The bodies moved here unchanged. What stays in each tool is a genuine field
smoke test (``--self-test``, see ``organizekit.core.smoke``) that answers "does
this copy work on this machine?" without the repository.

The one subtlety is namespaces: a moved body still expects to resolve — and
sometimes assign — the *tool's* module globals. :func:`bind_to_tool` gives each
function the tool module's ``__dict__`` as its globals, so the semantics are
identical to before the move rather than merely similar.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import Any


def bind_to_tool(tool: Any, func: Callable[..., Any]) -> Callable[..., Any]:
    """Return ``func`` with ``tool``'s module namespace as its globals."""
    rebound = types.FunctionType(
        func.__code__,
        tool.__dict__,
        func.__name__,
        func.__defaults__,
        func.__closure__,
    )
    rebound.__kwdefaults__ = func.__kwdefaults__
    rebound.__doc__ = func.__doc__
    return rebound
