"""The field smoke test every tool exposes as ``--self-test``.

A tool used to carry its *entire* offline test suite inside the shipped file —
2,229 lines across the toolkit, 1,047 of them in ``subtitle_fetcher.py``, on
top of the 1,811-line ``tests/test_subtitle_fetcher.py`` that covers the same
code. Those assertions now live in ``tests/selftests/`` where they run as part
of the unit suite (and count towards coverage).

What remains in the tools is what ``--self-test`` is actually *for*: answering
"is this copy of the tool working on this machine?" without the repository, a
media library, or a network. Every tool therefore checks the same three things
this module owns —

* the shared report renderer produces a bounded, non-empty report,
* the atomic writer publishes exact bytes and leaves no staging debris,
* the library root resolves and is describable,

— and then adds a handful of checks of its own decision logic. Uniform output,
one implementation, ~10 lines per tool.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import describe_library_origin, resolve_library
from .fsio import atomic_write_text
from .report import Report

Check = tuple[str, Callable[[], bool]]


def _shared_checks() -> list[Check]:
    """The three guarantees every tool inherits from the shared core."""

    def report_renders() -> bool:
        report = Report("SMOKE TEST", "organize")
        report.meta("Library", "(none)")
        report.paragraph("The renderer must produce bounded, non-empty text.")
        text = report.render()
        return bool(text.strip()) and all(len(line) <= report.width for line in text.splitlines())

    def atomic_write_round_trips() -> bool:
        with tempfile.TemporaryDirectory(prefix="organize_smoke_") as td:
            target = Path(td) / "smoke.txt"
            payload = "line one\nline two\n"
            atomic_write_text(target, payload)
            published = target.read_text(encoding="utf-8") == payload
            no_debris = [p.name for p in Path(td).iterdir()] == ["smoke.txt"]
            return published and no_debris

    def library_root_resolves() -> bool:
        return bool(str(resolve_library(None))) and bool(describe_library_origin(None))

    return [
        ("shared report renderer", report_renders),
        ("atomic + durable write", atomic_write_round_trips),
        ("library-root resolution", library_root_resolves),
    ]


def run_field_smoke_test(tool: str, checks: Sequence[Check] = ()) -> int:
    """Run ``checks`` plus the shared ones and print a uniform result block.

    Returns a process exit code: 0 when everything passed, 1 otherwise. A check
    that raises counts as a failure and its exception is shown — a smoke test
    that crashes has still told you the answer you asked for.
    """
    started = time.perf_counter()
    all_checks = [*_shared_checks(), *checks]
    failures: list[str] = []

    print(f"{tool} — field smoke test")
    for label, probe in all_checks:
        try:
            ok = bool(probe())
            detail = ""
        except Exception as exc:  # noqa: BLE001 - a crashing check is a failed check
            ok, detail = False, f"  ({type(exc).__name__}: {exc})"
        print(f"  {'OK  ' if ok else 'FAIL'}  {label}{detail}")
        if not ok:
            failures.append(label)

    elapsed = time.perf_counter() - started
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} of {len(all_checks)} checks "
              f"({', '.join(failures)}) in {elapsed:.2f}s")
        return 1
    print(f"SELF-TEST PASSED: {len(all_checks)} checks in {elapsed:.2f}s")
    print("Full offline suite: python -m unittest discover -s tests")
    return 0
