"""Tests for the run stream: `pipeline.py --events` and `--summary-json`.

A run is not a state. `doctor`, `status` and `audit` describe something that
can be read in one shot; a pipeline run is an hour of work, and the questions
worth asking about it - which step is running now, how long did the remux take,
what failed at 03:12 - are answered by a stream written as it goes, not by a
file that appears once it is over.

These tests hold the stream to the promises that make it worth having: the
lines are whole and parseable, started and finished always pair up, a crash
does not cost the lines already written, and a broken destination costs a
record rather than the run.
"""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pipeline as pl
from organizekit import VERSION
from organizekit.core import JSON_SCHEMA, EventStream


class EventStreamTests(unittest.TestCase):
    """The writer itself, apart from any pipeline."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="events_")
        self.root = Path(self._td.name)
        self.path = self.root / "events.jsonl"
        self.addCleanup(self._td.cleanup)

    def stream(self, path: Path | None = None) -> EventStream:
        return EventStream(self.path if path is None else path, "9.9.9", "run")

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_each_event_is_one_line_and_one_document(self) -> None:
        stream = self.stream()
        stream.emit("first", value=1)
        stream.emit("second", value=2)
        self.assertEqual([line["event"] for line in self.lines()], ["first", "second"])
        self.assertEqual([line["value"] for line in self.lines()], [1, 2])

    def test_every_line_carries_the_shared_envelope(self) -> None:
        """A reader tailing the file may only ever see one line of it."""
        self.stream().emit("first")
        line = self.lines()[0]
        self.assertEqual(tuple(line)[:4], ("schema", "tool", "version", "command"))
        self.assertEqual(line["schema"], JSON_SCHEMA)
        self.assertEqual(line["tool"], "organize")
        self.assertEqual(line["command"], "run")

    def test_events_are_stamped_with_the_clock(self) -> None:
        """The one place a timestamp belongs: a record of when things happened."""
        self.stream().emit("first")
        self.assertRegex(self.lines()[0]["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_appending_never_rewrites_what_is_already_there(self) -> None:
        self.stream().emit("first")
        self.stream().emit("second")  # a second writer, same path
        self.assertEqual([line["event"] for line in self.lines()], ["first", "second"])

    def test_a_missing_parent_directory_is_created(self) -> None:
        nested = self.root / "a" / "b" / "events.jsonl"
        EventStream(nested, "9.9.9", "run").emit("first")
        self.assertTrue(nested.is_file())

    def test_a_disabled_stream_writes_nothing_and_is_free(self) -> None:
        stream = EventStream(None, "9.9.9", "run")
        self.assertFalse(stream.enabled)
        stream.emit("first")  # must not raise
        self.assertFalse(self.path.exists())

    def test_an_unwritable_destination_disables_the_stream_and_warns_once(self) -> None:
        """A full disk costs the operator a record, not the run."""
        blocked = self.root / "not-a-directory" / "events.jsonl"
        blocked.parent.write_text("I am a file", encoding="utf-8")
        stream = EventStream(blocked, "9.9.9", "run")
        buf = io.StringIO()
        with redirect_stderr(buf):
            stream.emit("first")
            stream.emit("second")
        self.assertFalse(stream.enabled)
        self.assertEqual(buf.getvalue().count("run events disabled"), 1)

    def test_a_caller_bug_is_not_swallowed(self) -> None:
        """Same rule as the run log: a *write* failure is survivable, a bug is not."""
        with self.assertRaises(TypeError):
            self.stream().emit("first", command="the envelope already owns this")


class PipelineEventTests(unittest.TestCase):
    """The stream a real (offline) pipeline run produces."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="pipeline_events_")
        self.root = Path(self._td.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.events = self.root / "events.jsonl"
        self.summary = self.root / "run_summary.json"
        self.addCleanup(self._td.cleanup)

    def _run(self, *, steps: tuple[str, ...] = ("auditor",), dry_run: bool = False,
             exit_code: int = 0, **config: object) -> list[dict]:
        cfg = pl.Config(library=self.library, steps=steps, dry_run=dry_run,
                        events_file=self.events, **config)
        with patch.object(pl.subprocess, "run",
                          side_effect=lambda command, *a, **k: subprocess.CompletedProcess(command, exit_code)), \
                patch.object(pl, "prerequisite_issue", return_value=None), \
                redirect_stdout(io.StringIO()):
            self.run_result = pl.run_pipeline(cfg, dry_run=dry_run,
                                              events=EventStream(self.events, VERSION, "run"))
        return self.lines()

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.events.read_text(encoding="utf-8").splitlines()]

    def test_a_run_opens_and_closes_with_one_event_each(self) -> None:
        events = [line["event"] for line in self._run()]
        self.assertEqual(events[0], "run_started")
        self.assertEqual(events[-1], "run_finished")
        self.assertEqual(events.count("run_started"), 1)
        self.assertEqual(events.count("run_finished"), 1)

    def test_the_opening_event_describes_the_run(self) -> None:
        opening = self._run(steps=("auditor",), limit=7, nice=True)[0]
        self.assertEqual(opening["library"], str(self.library))
        self.assertEqual(opening["steps"], ["auditor"])
        self.assertEqual(opening["limit"], 7)
        self.assertTrue(opening["nice"])

    def test_every_step_started_has_a_matching_finished(self) -> None:
        lines = self._run(steps=("10bit", "auditor"))
        started = [line["step"] for line in lines if line["event"] == "step_started"]
        finished = [line["step"] for line in lines if line["event"] == "step_finished"]
        self.assertEqual(started, ["10bit", "auditor"])
        self.assertEqual(started, finished)

    def test_a_step_started_before_its_child_so_a_watcher_can_see_it_running(self) -> None:
        lines = self._run(steps=("auditor",))
        events = [line["event"] for line in lines]
        self.assertLess(events.index("step_started"), events.index("step_finished"))

    def test_the_started_event_carries_the_argv(self) -> None:
        started = next(line for line in self._run() if line["event"] == "step_started")
        self.assertIn(str(self.library), started["argv"])
        self.assertTrue(any("library_auditor" in part for part in started["argv"]))

    def test_a_skipped_step_still_reports_what_it_would_have_run(self) -> None:
        """A run that skipped everything must still say what it was going to do."""
        cfg = pl.Config(library=self.library, steps=("cleaner",), events_file=self.events)
        with patch.object(pl, "prerequisite_issue", return_value="mkvmerge not installed"), \
                redirect_stdout(io.StringIO()):
            pl.run_pipeline(cfg, events=EventStream(self.events, VERSION, "run"))
        started = next(line for line in self.lines() if line["event"] == "step_started")
        finished = next(line for line in self.lines() if line["event"] == "step_finished")
        self.assertTrue(started["argv"])
        self.assertEqual(finished["status"], "skipped")
        self.assertEqual(finished["detail"], "mkvmerge not installed")

    def test_a_failing_step_records_its_exit_code(self) -> None:
        lines = self._run(exit_code=3)
        finished = next(line for line in lines if line["event"] == "step_finished")
        self.assertEqual(finished["status"], "ran")
        self.assertEqual(finished["exit_code"], 3)
        self.assertEqual(lines[-1]["failed"], ["auditor"])
        self.assertEqual(lines[-1]["exit_code"], 1)

    def test_the_closing_event_agrees_with_the_results(self) -> None:
        closing = self._run(steps=("10bit", "auditor"))[-1]
        self.assertEqual(closing["completed"], ["10bit", "auditor"])
        self.assertEqual(closing["failed"], [])
        self.assertEqual(closing["not_run"], [])
        self.assertEqual(closing["exit_code"], 0)
        self.assertGreaterEqual(closing["elapsed_sec"], 0.0)

    def test_a_crash_mid_run_keeps_the_events_already_written(self) -> None:
        """Append-only is the point: a killed run still says how far it got."""
        cfg = pl.Config(library=self.library, steps=("10bit", "auditor"), events_file=self.events)
        calls: list[str] = []

        def explode(step, config, dry_run_pipeline=False, events=None):
            calls.append(step.key)
            if len(calls) == 1:
                return pl.StepResult(step.key, step.title, "ran", returncode=0)
            raise KeyboardInterrupt

        with patch.object(pl, "run_step", side_effect=explode), \
                redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            pl.run_pipeline(cfg, events=EventStream(self.events, VERSION, "run"))

        events = [line["event"] for line in self.lines()]
        self.assertEqual(events, ["run_started", "step_finished"])
        self.assertNotIn("run_finished", events, "an interrupted run must not claim to have finished")

    def test_no_events_file_means_no_events_and_no_cost(self) -> None:
        cfg = pl.Config(library=self.library, steps=("auditor",))
        with patch.object(pl.subprocess, "run",
                          side_effect=lambda command, *a, **k: subprocess.CompletedProcess(command, 0)), \
                patch.object(pl, "prerequisite_issue", return_value=None), \
                redirect_stdout(io.StringIO()):
            run = pl.run_pipeline(cfg, events=EventStream(None, VERSION, "run"))
        self.assertFalse(self.events.exists())
        self.assertEqual([r.status for r in run.results], ["ran"])


class SummaryDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="pipeline_summary_")
        self.root = Path(self._td.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.summary = self.root / "run_summary.json"
        self.addCleanup(self._td.cleanup)

    def _run(self, exit_code: int = 0, steps: tuple[str, ...] = ("auditor",)) -> dict:
        cfg = pl.Config(library=self.library, steps=steps, summary_file=self.summary)
        with patch.object(pl.subprocess, "run",
                          side_effect=lambda command, *a, **k: subprocess.CompletedProcess(command, exit_code)), \
                patch.object(pl, "prerequisite_issue", return_value=None), \
                redirect_stdout(io.StringIO()):
            run = pl.run_pipeline(cfg)
            pl.write_summary_json(run, cfg)
        self.run = run
        self.cfg = cfg
        return json.loads(self.summary.read_text(encoding="utf-8"))

    def test_the_summary_is_written_where_it_was_asked_for(self) -> None:
        document = self._run()
        self.assertEqual(document["command"], "run")
        self.assertEqual(document["library"], str(self.library))

    def test_the_envelope_reports_the_toolkit_version(self) -> None:
        self.assertEqual(self._run()["version"], VERSION)

    def test_top_level_keys_are_the_documented_ones(self) -> None:
        self.assertEqual(
            sorted(self._run()),
            ["command", "completed", "dry_run", "elapsed_sec", "exit_code", "failed",
             "library", "not_run", "results", "schema", "steps", "tool", "version"],
        )

    def test_one_row_per_step_in_pipeline_order(self) -> None:
        rows = self._run(steps=("10bit", "auditor"))["results"]
        self.assertEqual([row["step"] for row in rows], ["10bit", "auditor"])
        self.assertEqual(rows[0]["status"], "ran")
        self.assertEqual(rows[0]["exit_code"], 0)

    def test_a_failed_run_says_so_in_the_document_and_the_exit_code(self) -> None:
        document = self._run(exit_code=2)
        self.assertEqual(document["failed"], ["auditor"])
        self.assertEqual(document["exit_code"], 1)
        self.assertEqual(document["results"][0]["exit_code"], 2)

    def test_the_document_agrees_with_the_printed_summary(self) -> None:
        """Two renderings of one run must not disagree about whether it failed."""
        document = self._run(exit_code=2)
        printed = pl.build_summary(self.run, self.cfg)
        self.assertIn("Failed steps : auditor", printed)
        self.assertEqual(document["failed"], ["auditor"])

    def test_no_summary_file_means_none_is_written(self) -> None:
        cfg = pl.Config(library=self.library, steps=("auditor",))
        pl.write_summary_json(pl.Run(), cfg)
        self.assertFalse(self.summary.exists())

    def test_an_unwritable_summary_never_fails_a_finished_run(self) -> None:
        """The work is done by then; a read-only directory is not a pipeline failure."""
        cfg = pl.Config(library=self.library, steps=("auditor",), summary_file=self.summary)
        buf = io.StringIO()
        with patch.object(pl, "atomic_write_text", side_effect=OSError("read-only")), \
                redirect_stderr(buf):
            pl.write_summary_json(pl.Run(), cfg)  # must not raise
        self.assertIn("could not write", buf.getvalue())

    def test_the_document_needs_no_custom_encoder(self) -> None:
        json.dumps(pl.summary_document(pl.Run(), pl.Config(library=self.library)))


class OneDefinitionOfSuccessTests(unittest.TestCase):
    """`run_outcome` exists so three renderings cannot disagree."""

    def result(self, key: str, status: str, code: int | None = None) -> pl.StepResult:
        return pl.StepResult(key, key.title(), status, returncode=code)

    def test_a_clean_run_exits_zero(self) -> None:
        run = pl.Run(results=[self.result("auditor", "ran", 0)])
        self.assertEqual(pl.run_outcome(run), {"completed": ["auditor"], "failed": [],
                                               "not_run": [], "exit_code": 0})

    def test_a_skipped_step_is_not_a_failure(self) -> None:
        """A missing binary means a step could not run, not that the run broke."""
        run = pl.Run(results=[self.result("cleaner", "skipped"), self.result("auditor", "ran", 0)])
        outcome = pl.run_outcome(run)
        self.assertEqual(outcome["not_run"], ["cleaner"])
        self.assertEqual(outcome["exit_code"], 0)

    def test_any_failed_step_exits_one(self) -> None:
        run = pl.Run(results=[self.result("auditor", "ran", 5)])
        self.assertEqual(pl.run_outcome(run)["exit_code"], 1)

    def test_a_run_that_did_nothing_exits_zero(self) -> None:
        self.assertEqual(pl.run_outcome(pl.Run())["exit_code"], 0)


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="pipeline_cli_")
        self.root = Path(self._td.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.addCleanup(self._td.cleanup)

    def test_the_flags_reach_the_config(self) -> None:
        events, summary = self.root / "e.jsonl", self.root / "s.json"
        with patch.object(pl, "run_pipeline", return_value=pl.Run()) as runner, \
                patch.object(pl, "write_summary_json") as writer, \
                redirect_stdout(io.StringIO()):
            pl.main(["--source", str(self.library), "--dry-run",
                     "--events", str(events), "--summary-json", str(summary)])
        cfg = runner.call_args[0][0]
        self.assertEqual(cfg.events_file, events)
        self.assertEqual(cfg.summary_file, summary)
        writer.assert_called_once()

    def test_a_real_dry_run_writes_both_files(self) -> None:
        events, summary = self.root / "e.jsonl", self.root / "s.json"
        with redirect_stdout(io.StringIO()):
            code = pl.main(["--source", str(self.library), "--dry-run",
                            "--events", str(events), "--summary-json", str(summary)])
        self.assertEqual(code, 0)
        stream = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(stream[0]["event"], "run_started")
        self.assertTrue(stream[0]["dry_run"])
        self.assertEqual(json.loads(summary.read_text(encoding="utf-8"))["exit_code"], 0)

    def test_neither_file_is_written_unless_asked_for(self) -> None:
        """The default run behaves exactly as it did before any of this existed."""
        with redirect_stdout(io.StringIO()):
            pl.main(["--source", str(self.library), "--dry-run"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["library"])


if __name__ == "__main__":
    unittest.main()
