"""Running the same job over many movies, with a worker pool when it pays.

Three of the tools walk a library and do one independent unit of work per
movie: probe it with ffprobe, measure a sidecar against it with ffsubsync,
classify its folder. Each of those is bound by something that is not the CPU -
a subprocess, a network share, a disk seek - so doing them one at a time leaves
the machine idle for most of a run.

``bitdepth.py`` already had a thread pool, hand-rolled inside its ``run()``.
This module is that pattern, extracted once and given the two properties the
hand-rolled version lacked:

* **A worker count of 1 means no pool at all.** The work runs inline in the
  calling thread, in input order, with no executor, no extra threads and no
  reordering. ``--workers 1`` is therefore an exact escape hatch: it reproduces
  the single-threaded behaviour these tools had before they were parallelised,
  which is what you want when a run misbehaves and what the tests use to assert
  exact output.
* **Ordered results are available.** ``map_ordered`` runs the work in parallel
  but hands the results back in *input* order, so a tool whose console output
  is a numbered list stays byte-identical to its serial self while still using
  every core. ``iter_completed`` is the other trade: results as soon as they
  exist, for long jobs where live feedback matters more than tidy ordering.

An exception raised by the work function is captured on the outcome rather than
propagated: one unreadable movie must never abort a sweep that has already done
real work. ``KeyboardInterrupt`` is the deliberate exception to that - it
cancels whatever has not started yet and propagates, so each tool's existing
handler can write its partial report exactly as it does today.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
R = TypeVar("R")

# A ceiling that applies whatever the machine claims. These pools drive
# subprocesses (ffprobe, ffsubsync/ffmpeg) that are themselves multi-threaded,
# so "one worker per core" oversubscribes badly on a big box and thrashes a
# small one.
DEFAULT_WORKER_CAP = 8


@dataclass(frozen=True)
class JobOutcome(Generic[T, R]):
    """One unit of work: what it was, what it produced, how it failed."""

    index: int
    item: T
    value: R | None = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def resolve_workers(requested: int, *, items: int = 0, cap: int = DEFAULT_WORKER_CAP) -> int:
    """How many workers to actually start.

    ``requested`` of 0 or less means "decide for me": half the CPUs, which
    leaves room for the subprocesses the workers spawn. Never more workers than
    there are jobs, and never more than ``cap``.
    """
    if requested <= 0:
        requested = max(1, (os.cpu_count() or 2) // 2)
    workers = max(1, min(requested, cap))
    if items > 0:
        workers = min(workers, items)
    return workers


def map_ordered(
    items: Sequence[T],
    work: Callable[[T], R],
    *,
    workers: int = 1,
) -> list[JobOutcome[T, R]]:
    """Run ``work`` over ``items`` and return the outcomes in *input* order.

    Use this when the tool's output is a numbered list: the run is parallel but
    the reporting is identical to a serial run, so nothing about the console,
    the log or the report changes when the worker count does.
    """
    return list(_run(items, work, workers=workers, ordered=True))


def iter_completed(
    items: Sequence[T],
    work: Callable[[T], R],
    *,
    workers: int = 1,
) -> Iterator[JobOutcome[T, R]]:
    """Yield outcomes as they finish (in input order when ``workers`` is 1).

    Use this when a single job can take minutes and silence would read as a
    hang. Each outcome carries its input ``index``, so a caller that wants to
    re-sort for its report still can.
    """
    yield from _run(items, work, workers=workers, ordered=False)


def _run(
    items: Sequence[T],
    work: Callable[[T], R],
    *,
    workers: int,
    ordered: bool,
) -> Iterator[JobOutcome[T, R]]:
    jobs = list(items)
    if not jobs:
        return
    if workers <= 1 or len(jobs) == 1:
        # No pool: the work happens in this thread, in this order. This is the
        # path `--workers 1` takes, and it must stay indistinguishable from the
        # plain `for` loop it replaced.
        for index, item in enumerate(jobs):
            yield _call(index, item, work)
        return

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="organize")
    try:
        futures: dict[Future[JobOutcome[T, R]], int] = {
            pool.submit(_call, index, item, work): index
            for index, item in enumerate(jobs)
        }
        if ordered:
            # Iterating the submission order blocks on each future in turn; the
            # others keep running in the background, so this costs no
            # throughput and buys deterministic output.
            for future in list(futures):
                yield future.result()
        else:
            yield from _as_they_finish(futures)
    except KeyboardInterrupt:
        # Whatever has not started will never start; what is already running is
        # left to finish so the tool can report on it. The tools' own handlers
        # write the partial report.
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=False)


def _as_they_finish(
    futures: dict[Future[JobOutcome[T, R]], int],
) -> Iterator[JobOutcome[T, R]]:
    for future in as_completed(futures):
        yield future.result()


def _call(index: int, item: T, work: Callable[[T], R]) -> JobOutcome[T, R]:
    """Run one job, turning a failure into data instead of a crashed sweep."""
    try:
        return JobOutcome(index=index, item=item, value=work(item))
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad movie must not end the run
        return JobOutcome(index=index, item=item, error=exc)


def describe_workers(workers: int, unit: str = "job") -> str:
    """The one-line explanation a tool prints in its banner."""
    if workers <= 1:
        return "1 (serial)"
    return f"{workers} ({unit}s run in parallel)"
