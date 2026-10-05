"""``library_auditor.py`` when the filesystem and its own cache misbehave.

The auditor is the only tool in the chain that touches nothing: it reads a
library and publishes a verdict per folder, and every other tool trusts that
verdict (``organize status`` reads the state cache; the report is what an
operator acts on). So its failure paths are about *what it claims*, not about
damage:

* **A folder it could not read is ``INACCESSIBLE``, never "nothing here".** An
  unreadable directory and an empty one look identical to a naive walk, and
  reporting the first as the second would tell the operator a movie is missing
  when in fact the share was down. The state is distinct, the OS error is in the
  report, and a movie that disappears between the directory read and its
  ``stat()`` - a remux replacing it, an rsync passing through - is an error for
  the same reason.
* **The promotion of a legacy ``.en.srt`` is refused, not forced.** When the
  canonical name is occupied by something that is not a plain file (a symlink, a
  directory) the folder is reported ``NONCANONICAL_SIDECAR`` with the reason:
  the auditor never overwrites a sidecar, because the extractor may have written
  it a moment earlier and the two tools do not share a lock.
* **A library it cannot enumerate audits as empty and says so loudly.** No
  report claiming zero folders without an ERROR line, because "0 findings" and
  "the mount was gone" are the same document otherwise.
* **A state cache that cannot be written never fails the run.** The cache is a
  convenience; the report and the exit code are the contract.
* **A defect inside the tool ends the run.** A worker exception is re-raised
  rather than swallowed, so a half-read library never publishes a clean report.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import library_auditor as la

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello there\n"


def pristine_tool() -> types.ModuleType:
    """A second copy of ``library_auditor``, loaded from source.

    ``tests/selftests`` rebinds ``run_self_tests`` on the imported module, so the
    shipped field smoke test - the one a real ``--self-test`` on a NAS runs - is
    unreachable through it. Loading the file again under another name gives the
    shipped function back without disturbing the module the rest of the suite
    holds a reference to.
    """
    spec = importlib.util.spec_from_file_location(
        "library_auditor_pristine", REPO / "library_auditor.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered while it executes: ``@dataclass`` resolves its own annotations
    # through ``sys.modules[cls.__module__]``, and this module builds a Config
    # dataclass at import time.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


class FaultyPath(type(Path())):
    """A ``Path`` that fails on demand, so children inherit the fault.

    ``iterdir()`` hands back objects of the folder's own class, which makes a
    subclass the only way to inject a filesystem failure into one specific path
    without patching ``Path.stat`` for the whole process - a patch every other
    tool in the same run would inherit.
    """

    # name -> how many stat() calls may still succeed before the file "goes".
    stat_budget: dict[str, int] = {}
    iterdir_budget: int | None = None

    def stat(self, *args: object, **kwargs: object):  # type: ignore[override]
        budget = type(self).stat_budget.get(self.name)
        if budget is not None:
            type(self).stat_budget[self.name] = budget - 1
            if budget <= 0:
                raise OSError(2, "No such file or directory", self.name)
        return super().stat(*args, **kwargs)  # type: ignore[arg-type]

    def iterdir(self):  # type: ignore[override]
        budget = type(self).iterdir_budget
        if budget is not None:
            type(self).iterdir_budget = budget - 1
            if budget <= 0:
                raise OSError(5, "Input/output error", str(self))
        return super().iterdir()


class AuditorFixture(unittest.TestCase):
    """A two-folder library plus the tool's log, captured."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="auditor_degraded_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.library = self.root / "Movies"
        self.library.mkdir()
        self.report = self.root / "audit-report.txt"
        self.log_file = self.root / "audit.log"
        self.stream = io.StringIO()
        la.log.stream = self.stream
        self.addCleanup(setattr, la.log, "stream", None)
        self.addCleanup(setattr, la.log, "file", None)
        FaultyPath.stat_budget = {}
        FaultyPath.iterdir_budget = None

    def movie(self, name: str) -> Path:
        folder = self.library / name
        folder.mkdir()
        (folder / f"{name}.mkv").write_bytes(b"x" * 2048)
        return folder

    def config(self, **settings: object) -> la.Config:
        base: dict[str, object] = {
            "source_dir": self.library,
            "report_file": self.report,
            "log_file": self.log_file,
            "use_state": False,
            "workers": 1,
        }
        base.update(settings)
        return la.Config(**base)  # type: ignore[arg-type]

    def logged(self) -> str:
        return self.stream.getvalue()


class UnreadableFolderTests(AuditorFixture):
    def test_a_path_that_is_not_a_directory_is_inaccessible_not_empty(self) -> None:
        """A stray file where a folder should be must not read as "no movie".

        ``NO_DIRECT_MOVIE_FILE`` is an actionable finding - it tells the operator
        a movie folder holds nothing. Reporting an unreadable path that way would
        have them delete or "fix" a folder the auditor never actually read.
        """
        stray = self.root / "not-a-folder.mkv"
        stray.write_bytes(b"x")
        files, error = la.direct_movie_files(stray)
        self.assertEqual(files, [])
        self.assertTrue(error, "the failure has to come back as a message")
        result = la.classify_folder(stray)
        self.assertEqual(result.state, "INACCESSIBLE")
        self.assertIn("not a directory", result.detail.lower())

    def test_a_movie_that_vanishes_before_its_stat_is_an_error(self) -> None:
        """The folder is read, then a file is gone: say so, do not drop it.

        This is the remux race - ``mkv_track_cleaner.py`` publishes a new file and
        unlinks the old one - and a silently dropped movie would let the audit
        report ``NO_DIRECT_MOVIE_FILE`` for a folder that has one.
        """
        folder = FaultyPath(self.movie("Vanishing (2011)"))
        # One successful stat is what ``is_file()`` spends; the file disappears
        # before the size read that follows it.
        armed = {"Vanishing (2011).mkv": 1}
        FaultyPath.stat_budget = dict(armed)
        files, error = la.direct_movie_files(folder)
        self.assertEqual(files, [], "the half-read folder yields no movie list")
        self.assertIn("cannot stat Vanishing (2011).mkv", error)
        FaultyPath.stat_budget = dict(armed)  # the fault is per read, so re-arm it
        result = la.classify_folder(folder)
        self.assertEqual(result.state, "INACCESSIBLE")
        self.assertIn("cannot stat", result.detail)

    def test_a_folder_that_fails_on_the_sidecar_read_is_inaccessible(self) -> None:
        """The second directory read is the sidecar scan; it can fail on its own.

        A network share dropping out mid-audit has to leave the folder reported
        with the files it did see and the error that stopped it, so the operator
        can tell a re-run is needed rather than believing the verdict.
        """
        folder = FaultyPath(self.movie("Flaky (2004)"))
        (folder / "Flaky (2004).mkv").write_bytes(b"x" * 10)
        FaultyPath.iterdir_budget = 1  # the first read succeeds, the second raises
        result = la.classify_folder(folder)
        self.assertEqual(result.state, "INACCESSIBLE")
        self.assertEqual([f.name for f in result.movie_files], ["Flaky (2004).mkv"],
                         "what was read is still reported")
        self.assertIn("Input/output error", result.detail)

    def test_an_occupied_canonical_sidecar_blocks_promotion_and_says_why(self) -> None:
        """A symlink on the canonical name is refused, never overwritten.

        The extractor and the auditor do not share a lock, so the canonical
        ``.eng.srt`` can be anything by the time this folder is read. Overwriting
        a symlink there would write through to whatever it points at - possibly
        the source download still seeding.
        """
        folder = self.movie("Linked (2013)")
        (folder / "Linked (2013).en.srt").write_text(SRT, encoding="utf-8")
        outside = self.root / "elsewhere.srt"
        outside.write_text(SRT, encoding="utf-8")
        (folder / "Linked (2013).eng.srt").symlink_to(outside)

        result = la.classify_folder(folder)

        self.assertEqual(result.state, "NONCANONICAL_SIDECAR")
        self.assertIn("could not be promoted", result.detail)
        self.assertIn("occupied", result.detail)
        self.assertTrue(outside.read_text(encoding="utf-8"), "the link target is untouched")
        self.assertEqual((folder / "Linked (2013).en.srt").read_text(encoding="utf-8"), SRT,
                         "and so is the legacy sidecar")

    def test_a_library_that_cannot_be_enumerated_says_so_before_reporting_nothing(
            self) -> None:
        """Zero folders is only ever published next to an ERROR line."""
        stray = self.root / "Movies.mkv"
        stray.write_bytes(b"x")
        audit = la.audit_library(self.config(source_dir=stray))
        self.assertEqual(audit.folders, [])
        self.assertIn("Cannot enumerate library", self.logged())


class DefectTests(AuditorFixture):
    def test_an_unexpected_failure_inside_a_worker_ends_the_run(self) -> None:
        """A defect is re-raised; a half-read library publishes no report.

        ``classify_folder`` swallows the filesystem errors it expects, so anything
        else arriving from a worker is a bug in this tool. Reporting the folders
        it managed to read as a complete audit would be the worst outcome: a clean
        looking report over a library nobody finished reading.
        """
        self.movie("Fine (2020)")
        stderr = io.StringIO()
        with mock.patch.object(la, "classify_folder",
                               side_effect=RuntimeError("worker defect")):
            with self.assertRaises(RuntimeError):
                la.audit_library(self.config(workers=2))
            with redirect_stderr(stderr):
                code = la.main(["--source", str(self.library), "--report", str(self.report),
                                "--log", str(self.log_file), "--no-state", "--workers", "2"])
        self.assertEqual(code, 1, "a defect leaves through the last-resort handler")
        self.assertIn("worker defect", stderr.getvalue(), "and the traceback is printed")
        self.assertFalse(self.report.exists(), "no report is published for a partial audit")

    def test_a_control_c_exits_130_and_writes_no_report(self) -> None:
        """An interrupted audit is a scheduler-visible 130, not a success."""
        self.movie("Interrupted (2015)")
        with mock.patch.object(la, "audit_library", side_effect=KeyboardInterrupt):
            code = la.main(["--source", str(self.library), "--report", str(self.report),
                            "--log", str(self.log_file), "--no-state"])
        self.assertEqual(code, 130)
        self.assertIn("Interrupted", self.logged())
        self.assertFalse(self.report.exists())

    def test_the_shipped_self_test_builds_a_library_and_verdicts_it(self) -> None:
        """``library_auditor.py --self-test`` on a NAS, as shipped.

        This is the check somebody runs when the auditor's verdicts look wrong on
        their own library, so it has to build its own two-folder fixture and pass
        without a library, a network share or any tool installed.
        """
        tool = pristine_tool()
        self.addCleanup(sys.modules.pop, "library_auditor_pristine", None)
        out = io.StringIO()
        with redirect_stdout(out):
            code = tool.main(["--self-test"])
        printed = out.getvalue()
        self.assertEqual(code, 0, printed)
        self.assertIn("self-test passed", printed.lower())


class StateCacheTests(AuditorFixture):
    def test_a_cache_that_cannot_be_written_never_fails_the_audit(self) -> None:
        """The verdict cache is a convenience; the report and exit code are not.

        ``organize status`` reads the cache and the operator reads the report. If
        a corrupt or full state database could fail the run, a library whose cache
        broke would stop being audited at all - and the audit is the thing that
        finds the library's real problems.
        """
        folder = self.movie("Cached (2009)")
        (folder / "Cached (2009).eng.srt").write_text(SRT, encoding="utf-8")
        cfg = self.config(use_state=True, state_db=self.root / "state.sqlite3")
        store = mock.Mock()
        store.enabled = True
        store.see_movies.side_effect = OSError("database disk image is malformed")
        with mock.patch.object(la, "open_state", return_value=store):
            code = la.main(["--source", str(self.library), "--report", str(self.report),
                            "--log", str(self.log_file),
                            "--state-db", str(cfg.state_db)])
        self.assertEqual(code, 0, "a failed cache write is not a failed audit")
        self.assertIn("state cache not updated", self.logged())
        self.assertIn("database disk image is malformed", self.logged())
        store.close.assert_called_once_with()
        self.assertTrue(self.report.exists(), "and the report was still published")
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("canonical=1", report, "the audit itself completed")
        self.assertIn("None. The library is fully canonical.", report,
                      "and it published no phantom finding")


class ReportRenderingTests(AuditorFixture):
    def test_a_state_the_scorecard_has_never_seen_still_renders(self) -> None:
        """A new audit state must not need a new label to appear in the report.

        The scorecard label comes from a table; a state added later (or one a
        future folder shape produces) falls back to the state name itself, so the
        report renders instead of raising inside ``build_report`` - which would
        turn a finding into a crash.
        """
        folder = self.movie("Unknown (2021)")
        audit = la.Audit(self.library, [la.FolderAudit(folder, "SOMETHING_NEW")], 0.1)
        report = la.build_report(audit, self.config())
        self.assertIn("SOMETHING NEW", report)
        self.assertEqual(la._state_label("SOMETHING_NEW"), "SOMETHING NEW")
        self.assertEqual(la._state_label("MISSING_SIDECAR"), "MISSING",
                         "a known state still gets its short label")


class ConfigGateTests(AuditorFixture):
    """Rules in ``validate_config`` that exist to stop the tool reading itself."""

    def setUp(self) -> None:
        super().setUp()
        self.movie("Gate (2022)")

    def test_a_negative_lock_timeout_is_refused(self) -> None:
        """A negative timeout would mean "never wait", i.e. always collide."""
        errors = la.validate_config(self.config(lock_timeout_seconds=-1.0))
        self.assertEqual(errors, ["--lock-timeout must be zero or greater"])

    def test_a_log_inside_the_library_is_refused(self) -> None:
        """The auditor walks every folder: its own log would become a finding.

        Worse than noise - the next run would audit the previous run's log file,
        and a report inside the library makes the library not canonical by its own
        rules.
        """
        inside = self.library / "audit.log"
        errors = la.validate_config(self.config(log_file=inside))
        self.assertEqual(errors, [f"--log must be outside --source: {inside}"])

    def test_a_log_and_a_report_that_are_the_same_file_are_refused(self) -> None:
        """Both are written; one of them would win, and which one is a race."""
        same = self.root / "both.txt"
        errors = la.validate_config(self.config(log_file=same, report_file=same))
        self.assertEqual(errors, ["--log and --report must be different files"])

    def test_a_refused_configuration_publishes_a_failure_document_and_exits_2(self) -> None:
        """``--json`` callers get the same shape for a refusal as for a success."""
        out = io.StringIO()
        with redirect_stdout(out):
            code = la.main(["--source", str(self.library), "--json",
                            "--report", str(self.library / "r.json"),
                            "--log", str(self.log_file), "--lock-timeout", "-5"])
        self.assertEqual(code, 2)
        import json
        document = json.loads(out.getvalue())
        self.assertEqual(document["error"]["kind"], "invalid-config")
        self.assertIn("--report must be outside --source", document["error"]["message"])
        self.assertIn("--lock-timeout must be zero or greater", document["error"]["message"])
        self.assertEqual(document["exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
