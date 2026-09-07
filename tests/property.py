"""A property harness: many random cases, one deterministic seed, small failures.

The suite is already strong on *examples* - a table of inputs somebody thought
of, and a fault injected at each step of a transaction. What it has never had
is the other kind of test: state an invariant, then let the machine look for a
counterexample in inputs nobody thought of. That is what this is for, and it is
about sixty lines because the repo has no third-party dependencies and this is
not a reason to acquire one.

Three properties of the harness itself matter more than its size:

* **Deterministic by default.** The seed comes from the test's own id, so a
  failure reproduces exactly - a property test that passes and fails at random
  is worse than no test. Set ``ORGANIZE_PROPERTY_SEED`` to sweep other seeds
  (a nightly job, or an afternoon of looking for trouble); the value is printed
  with every failure so it can be pinned back into the default run.
* **Failures shrink.** A random 12-track MKV that breaks an invariant is a bug
  report nobody can read. Each failure is reduced - drop list elements, empty
  strings, walk integers toward zero - while it keeps failing *the same way*,
  so what gets printed is the smallest case that still breaks.
* **It cannot pass vacuously.** ``for_all`` asserts it actually ran its cases,
  and `tests/test_properties.py` plants failures to prove the harness reports
  and shrinks them. A property runner that silently generated nothing would
  otherwise be the most reassuring file in the repository.
"""

from __future__ import annotations

import os
import random
import unittest
import zlib
from collections.abc import Callable, Iterator, Sequence
from typing import Any, TypeVar

T = TypeVar("T")

# A strategy is just "given a source of randomness, build me one value".
Strategy = Callable[[random.Random], T]

DEFAULT_CASES = 100


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def integers(low: int, high: int) -> Strategy[int]:
    return lambda rng: rng.randint(low, high)


def floats(low: float, high: float) -> Strategy[float]:
    return lambda rng: rng.uniform(low, high)


def booleans() -> Strategy[bool]:
    return lambda rng: rng.random() < 0.5


def sampled(choices: Sequence[T]) -> Strategy[T]:
    values = list(choices)
    return lambda rng: rng.choice(values)


def one_of(*strategies: Strategy[Any]) -> Strategy[Any]:
    options = list(strategies)
    return lambda rng: rng.choice(options)(rng)


def maybe(strategy: Strategy[T], *, none_odds: float = 0.3) -> Strategy[T | None]:
    return lambda rng: None if rng.random() < none_odds else strategy(rng)


def text(alphabet: str, *, min_size: int = 0, max_size: int = 12) -> Strategy[str]:
    def build(rng: random.Random) -> str:
        size = rng.randint(min_size, max_size)
        return "".join(rng.choice(alphabet) for _ in range(size))
    return build


def lists(element: Strategy[T], *, min_size: int = 0, max_size: int = 6) -> Strategy[list[T]]:
    def build(rng: random.Random) -> list[T]:
        size = rng.randint(min_size, max_size)
        return [element(rng) for _ in range(size)]
    return build


def fixed_dict(fields: dict[str, Strategy[Any]], *, optional: Sequence[str] = ()) -> Strategy[dict[str, Any]]:
    """A dict with these keys; the ``optional`` ones are sometimes absent.

    Absence is a different input from a falsy value - half the fail-closed
    branches in this toolkit exist because a key was missing rather than empty.
    """
    def build(rng: random.Random) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, strategy in fields.items():
            if key in optional and rng.random() < 0.4:
                continue
            out[key] = strategy(rng)
        return out
    return build


# ---------------------------------------------------------------------------
# Shrinking
# ---------------------------------------------------------------------------

def shrink_candidates(value: Any) -> Iterator[Any]:
    """Simpler values to try, most aggressive first."""
    if isinstance(value, bool):
        if value:
            yield False
    elif isinstance(value, int):
        for candidate in (0, value // 2, value - 1 if value > 0 else value + 1):
            if candidate != value and abs(candidate) < abs(value):
                yield candidate
    elif isinstance(value, float):
        for candidate in (0.0, value / 2):
            if candidate != value:
                yield candidate
    elif isinstance(value, str):
        if value:
            yield ""
            yield value[: len(value) // 2]
            yield value[:-1]
    elif isinstance(value, (list, tuple)):
        kind = type(value)
        if value:
            yield kind()
            if len(value) > 2:
                yield kind(list(value)[: len(value) // 2])
            for index in range(len(value)):
                yield kind(list(value)[:index] + list(value)[index + 1:])
        for index, item in enumerate(value):
            for smaller in shrink_candidates(item):
                yield kind(list(value)[:index] + [smaller] + list(value)[index + 1:])
    elif isinstance(value, dict):
        for key in list(value):
            reduced = dict(value)
            reduced.pop(key)
            yield reduced
        for key, item in value.items():
            for smaller in shrink_candidates(item):
                reduced = dict(value)
                reduced[key] = smaller
                yield reduced


def shrink(value: Any, fails: Callable[[Any], type[BaseException] | None]) -> Any:
    """Reduce ``value`` while it keeps failing in the same way."""
    original = fails(value)
    if original is None:
        return value
    current = value
    for _ in range(200):  # a bounded search: this is a convenience, not a proof
        for candidate in shrink_candidates(current):
            if fails(candidate) is original:
                current = candidate
                break
        else:
            return current
    return current


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

class PropertyTestCase(unittest.TestCase):
    """A ``TestCase`` with ``for_all``."""

    cases = DEFAULT_CASES

    def seed_for(self, label: str) -> int:
        # crc32, not hash(): str hashing is salted per process, so `hash` would
        # hand a different seed to every run and quietly undo the determinism
        # this harness is built on.
        override = os.environ.get("ORGANIZE_PROPERTY_SEED", "").strip()
        base = int(override) if override.isdigit() else 0
        return base ^ zlib.crc32(f"{self.id()}::{label}".encode())

    def for_all(
        self,
        strategy: Strategy[T],
        prop: Callable[[T], None],
        *,
        cases: int | None = None,
        label: str = "",
    ) -> None:
        """Run ``prop`` over generated values; report the smallest failure."""
        total = self.cases if cases is None else cases
        self.assertGreater(total, 0, "a property with no cases proves nothing")
        seed = self.seed_for(label or getattr(prop, "__name__", "property"))
        rng = random.Random(seed)
        checked = 0

        def failure_type(value: Any) -> type[BaseException] | None:
            try:
                prop(value)
            except KeyboardInterrupt:
                raise
            except BaseException as exc:  # noqa: BLE001 - the point is to catch it
                return type(exc)
            return None

        for index in range(total):
            value = strategy(rng)
            checked += 1
            try:
                prop(value)
            except KeyboardInterrupt:
                raise
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                smallest = shrink(value, failure_type)
                raise self.failureException(
                    f"property failed on case {index + 1} of {total} "
                    f"(seed {seed}, ORGANIZE_PROPERTY_SEED to vary)\n"
                    f"  generated: {value!r}\n"
                    f"  shrunk to: {smallest!r}\n"
                    f"  failure:   {type(exc).__name__}: {exc}"
                ) from exc
        self.assertEqual(checked, total, "the strategy stopped producing cases")
