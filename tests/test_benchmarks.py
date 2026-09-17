"""The benchmarks are documentation, so they have to run.

The README says every script in ``benchmarks/`` is re-runnable and the tool
reference quotes the numbers they produce. A benchmark that crashes is therefore
a documentation defect - and one the unit suite would never notice, because
nothing else imports those scripts.

These tests drive each one at a tiny size (a handful of folders, a fraction of a
millisecond of simulated latency) and assert the invariant each benchmark
asserts about itself: the answer does not change with the worker count. That is
the property the numbers are *for*; a benchmark that reports a speed-up by
quietly changing its verdicts is worse than no benchmark at all.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
BENCHMARKS = REPO / "benchmarks"


def load_benchmark(filename: str):
    """Import a benchmark script by path - they are scripts, not a package."""
    path = BENCHMARKS / filename
    spec = importlib.util.spec_from_file_location(f"benchmark_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BenchmarkScriptsRunTests(unittest.TestCase):
    """Each script ends with its own verdict, and the verdict must be "it ran"."""

    def test_every_benchmark_is_a_script_with_a_main(self) -> None:
        scripts = sorted(BENCHMARKS.glob("bench_*.py"))
        self.assertTrue(scripts, "the benchmarks directory is not empty")
        for script in scripts:
            with self.subTest(script=script.name):
                source = script.read_text(encoding="utf-8")
                self.assertIn("def main() -> int:", source)
                self.assertIn("raise SystemExit(main())", source)

    def test_the_audit_workers_benchmark_runs_and_keeps_its_verdicts(self) -> None:
        """A benchmark of the auditor must not crash the auditor.

        This is a regression test with a specific history: the script used to
        silence the audit by replacing ``library_auditor.log`` with a plain
        function, but the audit reads ``log.live`` as well as calling it, so the
        whole benchmark died with ``AttributeError`` before printing a number.
        """
        import library_auditor as la

        module = load_benchmark("bench_audit_workers.py")
        module.FOLDERS = 6
        module.LATENCY_SECONDS = 0.001
        # The script points the run log at a buffer; put it back afterwards so
        # the rest of the suite keeps the object it expects.
        self.addCleanup(setattr, la.log, "stream", la.log.stream)
        self.addCleanup(setattr, la.log, "file", la.log.file)

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = module.main()
        output = captured.getvalue()
        self.assertEqual(code, 0, f"the benchmark reported failure:\n{output}")
        self.assertIn("identical audit", output)
        self.assertIn("workers=8", output, "every worker count was measured")
        self.assertTrue(hasattr(la.log, "live"), "the audit's log object survived")

    def test_the_triage_workers_benchmark_runs_and_keeps_its_verdicts(self) -> None:
        import subtitle_extractor as sx

        module = load_benchmark("bench_triage_workers.py")
        module.MOVIES = 6
        module.LATENCY_SECONDS = 0.001
        self.addCleanup(setattr, sx, "inspect_existing_sidecars", sx.inspect_existing_sidecars)

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = module.main()
        output = captured.getvalue()
        self.assertEqual(code, 0, f"the benchmark reported failure:\n{output}")
        self.assertIn("identical verdicts", output)
        self.assertIn("workers=8", output, "every worker count was measured")

    def test_the_triage_benchmark_really_exercises_the_extractor(self) -> None:
        """It must keep measuring triage, not something that merely looks like it."""
        module = load_benchmark("bench_triage_workers.py")
        with mock.patch.object(module, "MOVIES", 3), \
                mock.patch.object(module, "LATENCY_SECONDS", 0.0), \
                mock.patch.object(module.sx, "TriageQueue", wraps=module.sx.TriageQueue) as queue, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(), 0)
        self.assertTrue(queue.called, "the benchmark drove the real TriageQueue")


if __name__ == "__main__":
    unittest.main()
