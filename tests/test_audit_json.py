"""Tests for ``library_auditor.py --json``: the audit, read by a machine.

The printed report is written for a person deciding what to fix next. This is
the same audit without the advice, and the rules it has to keep are the ones a
consumer would otherwise learn the hard way: stdout carries the document and
nothing else, a failure is still a document, and the human report is not
altered by asking for JSON.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import library_auditor as la
from organizekit.core import LockUnavailable

VALID_SRT = "1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n"


class AuditJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="auditor_json_")
        self.root = Path(self._td.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.report = self.root / "report.txt"
        self.log = self.root / "run.log"
        self.db = self.root / "state.db"
        self.addCleanup(self._td.cleanup)
        self.addCleanup(setattr, la.log, "stream", None)
        self.addCleanup(setattr, la.log, "file", None)

    # -- fixtures ----------------------------------------------------------

    def _canonical(self, name: str = "Alpha (2001)", size: int = 4096) -> Path:
        folder = self.library / name
        folder.mkdir()
        (folder / f"{name}.mkv").write_bytes(b"x" * size)
        (folder / f"{name}.eng.srt").write_text(VALID_SRT, encoding="utf-8")
        return folder

    def _missing_sidecar(self, name: str = "Bravo (2002)") -> Path:
        folder = self.library / name
        folder.mkdir()
        (folder / f"{name}.mkv").write_bytes(b"x" * 4096)
        return folder

    def _other_container(self, name: str = "Charlie (2003)") -> Path:
        folder = self.library / name
        folder.mkdir()
        (folder / f"{name}.avi").write_bytes(b"x" * 4096)
        return folder

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = la.main(["--source", str(self.library), "--log", str(self.log),
                            "--report", str(self.report), "--state-db", str(self.db), *args])
        return code, out.getvalue(), err.getvalue()

    def _document(self, *args: str) -> dict:
        code, out, _ = self._run("--json", *args)
        document = json.loads(out)
        self.assertEqual(code, document["exit_code"])
        return document

    # -- the envelope ------------------------------------------------------

    def test_stdout_is_nothing_but_the_document(self) -> None:
        """The run log alone would put twenty lines in front of the JSON."""
        self._canonical()
        code, out, err = self._run("--json")
        self.assertEqual(code, 0)
        json.loads(out)
        self.assertNotIn("JELLYFIN MOVIE LIBRARY AUDIT", out)
        self.assertIn("Starting read-only library audit", err)

    def test_the_envelope_is_the_one_every_json_command_shares(self) -> None:
        from organizekit.core import JSON_SCHEMA

        self._canonical()
        document = self._document()
        self.assertEqual(document["schema"], JSON_SCHEMA)
        self.assertEqual(document["tool"], "organize")
        self.assertEqual(document["command"], "audit")

    def test_the_version_is_the_toolkit_release_not_this_scripts_own(self) -> None:
        """Three documents from one install must not report three versions."""
        from organizekit import VERSION

        self._canonical()
        self.assertEqual(self._document()["version"], VERSION)

    def test_top_level_keys_are_the_documented_ones(self) -> None:
        self._canonical()
        self.assertEqual(
            sorted(self._document()),
            ["canonical", "canonical_pct", "command", "containers", "defects", "error",
             "exit_code", "findings", "folders", "items", "library", "report", "schema",
             "states", "tool", "version"],
        )

    # -- the numbers -------------------------------------------------------

    def test_the_tallies_match_the_folders_on_disk(self) -> None:
        self._canonical()
        self._missing_sidecar()
        self._other_container()
        document = self._document()
        self.assertEqual(document["folders"], 3)
        self.assertEqual(document["canonical"], 1)
        self.assertEqual(document["findings"], 2)
        self.assertEqual(document["canonical_pct"], 33.3)
        self.assertEqual(document["states"],
                         {"CANONICAL_MKV": 1, "MISSING_SIDECAR": 1, "SINGLE_OTHER_CONTAINER": 1})

    def test_defects_count_only_layout_defects_not_a_missing_subtitle(self) -> None:
        """The distinction --fail-on-defects exists for, as a field."""
        self._canonical()
        self._missing_sidecar()
        self.assertEqual(self._document()["defects"], 0)
        self._other_container()
        self.assertEqual(self._document()["defects"], 1)

    def test_an_empty_library_is_a_hundred_percent_canonical(self) -> None:
        """Zero of zero is not a failing library; the report says so too."""
        document = self._document()
        self.assertEqual(document["folders"], 0)
        self.assertEqual(document["canonical_pct"], 100.0)
        self.assertEqual(document["items"], [])

    def test_container_types_are_counted(self) -> None:
        self._canonical()
        self._other_container()
        self.assertEqual(self._document()["containers"], {".AVI": 1, ".MKV": 1})

    # -- the rows ----------------------------------------------------------

    def test_one_row_per_folder_in_the_order_the_report_lists_them(self) -> None:
        self._other_container("Charlie (2003)")
        self._canonical("Alpha (2001)")
        self._missing_sidecar("Bravo (2002)")
        names = [item["name"] for item in self._document()["items"]]
        self.assertEqual(names, ["Alpha (2001)", "Bravo (2002)", "Charlie (2003)"])

    def test_a_row_carries_the_state_the_detail_and_the_movie_files(self) -> None:
        self._canonical(size=8192)
        item = self._document()["items"][0]
        self.assertEqual(item["state"], "CANONICAL_MKV")
        self.assertEqual(item["folder"], str(self.library / "Alpha (2001)"))
        self.assertEqual(item["movie_files"],
                         [{"name": "Alpha (2001).mkv", "extension": ".mkv", "size_bytes": 8192}])

    def test_the_detail_explaining_a_finding_is_not_dropped(self) -> None:
        """Why a folder is flagged is the whole value of the row."""
        self._missing_sidecar()
        item = self._document()["items"][0]
        self.assertEqual(item["state"], "MISSING_SIDECAR")
        self.assertIn("sidecar", item["detail"])

    def test_the_subtitle_state_is_reported_alongside_the_folder_state(self) -> None:
        """The auditor owns that vocabulary; a consumer should not re-derive it."""
        self._canonical("Alpha (2001)")
        self._missing_sidecar("Bravo (2002)")
        self._other_container("Charlie (2003)")
        states = {item["name"]: item["subtitle"] for item in self._document()["items"]}
        self.assertEqual(states["Alpha (2001)"], "present")
        self.assertEqual(states["Bravo (2002)"], "missing")
        self.assertIsNone(states["Charlie (2003)"])  # no sidecar question worth naming

    # -- exit codes and failures ------------------------------------------

    def test_the_gates_still_decide_the_exit_code(self) -> None:
        self._canonical()
        self._missing_sidecar()
        self.assertEqual(self._document()["exit_code"], 0)
        self.assertEqual(self._document("--fail-on-findings")["exit_code"], 1)
        self.assertEqual(self._document("--fail-on-defects")["exit_code"], 0)
        self._other_container()
        self.assertEqual(self._document("--fail-on-defects")["exit_code"], 1)

    def test_an_unreadable_source_is_reported_as_json_not_a_bare_stderr_line(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = la.main(["--source", str(self.root / "gone"), "--log", str(self.log),
                            "--report", str(self.report), "--json"])
        document = json.loads(out.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(document["exit_code"], 2)
        self.assertEqual(document["error"]["kind"], "invalid-config")
        self.assertIn("not an accessible directory", document["error"]["message"])
        self.assertEqual(document["items"], [])

    def test_a_busy_library_is_reported_as_json_too(self) -> None:
        """Another audit holding the lock exits 3; a parser should still see it."""
        self._canonical()
        with patch.object(la, "ExclusiveRunLock", side_effect=LockUnavailable("another audit owns it")):
            code, out, _ = self._run("--json")
        document = json.loads(out)
        self.assertEqual(code, 3)
        self.assertEqual(document["exit_code"], 3)
        self.assertEqual(document["error"]["kind"], "lock-unavailable")

    def test_an_unwritable_report_is_reported_as_json_too(self) -> None:
        self._canonical()
        with patch.object(la, "atomic_write_text", side_effect=OSError("read-only filesystem")):
            code, out, _ = self._run("--json")
        document = json.loads(out)
        self.assertEqual(code, 2)
        self.assertEqual(document["error"]["kind"], "report-write-failed")
        self.assertIn("read-only filesystem", document["error"]["message"])

    def test_a_healthy_run_carries_no_error(self) -> None:
        self._canonical()
        self.assertIsNone(self._document()["error"])

    # -- the human report is untouched -------------------------------------

    def test_the_report_file_is_still_written_and_named_in_the_document(self) -> None:
        """A JSON run is not a different audit; it is the same one, read differently."""
        self._canonical()
        document = self._document()
        self.assertEqual(document["report"], str(self.report))
        self.assertIn("JELLYFIN MOVIE LIBRARY AUDIT", self.report.read_text(encoding="utf-8"))

    def test_the_report_is_the_same_text_in_both_modes(self) -> None:
        self._canonical()
        self._missing_sidecar()
        self._run("--json")
        from_json_run = self.report.read_text(encoding="utf-8").splitlines()
        self._run()
        from_human_run = self.report.read_text(encoding="utf-8").splitlines()
        # Only the generated-at stamp may differ between two runs.
        differing = [a for a, b in zip(from_json_run, from_human_run, strict=True) if a != b]
        self.assertTrue(all("Generated" in line for line in differing), differing)

    def test_the_human_run_still_prints_the_report_to_stdout(self) -> None:
        self._canonical()
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("JELLYFIN MOVIE LIBRARY AUDIT", out)

    def test_the_log_file_is_written_in_both_modes(self) -> None:
        """Routing the console to stderr must not cost the operator the log."""
        self._canonical()
        self._run("--json")
        self.assertIn("Starting read-only library audit", self.log.read_text(encoding="utf-8"))

    def test_the_state_cache_is_still_published(self) -> None:
        from organizekit.core import open_state

        self._canonical()
        self._run("--json")
        with open_state(self.db, tool="tests") as store:
            self.assertEqual(len(store.movies()), 1)

    # -- determinism -------------------------------------------------------

    def test_two_runs_over_an_unchanged_library_are_byte_identical(self) -> None:
        self._canonical()
        self._missing_sidecar()
        first = json.dumps(self._document())
        second = json.dumps(self._document())
        self.assertEqual(first, second)
        for stamp in ("elapsed", "generated", "timestamp"):
            self.assertNotIn(stamp, first)

    def test_the_document_needs_no_custom_json_encoder(self) -> None:
        self._canonical()
        audit = la.audit_library(la.Config(source_dir=self.library, use_state=False))
        cfg = la.Config(source_dir=self.library, report_file=self.report)
        json.dumps(la.audit_document(audit, cfg, 0))


if __name__ == "__main__":
    unittest.main()
