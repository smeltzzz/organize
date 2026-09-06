"""The contract three commands share: one envelope, one schema, one version.

`doctor`, `status` and `audit` answer in JSON, and two of them live in a
different file from the third. These tests are the reason that is safe: they
compare the *documents*, so a command that quietly invents its own envelope -
a different key, a different version, a stray timestamp - fails here rather
than in somebody's parser six months from now.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import library_auditor as la
import organize
from organizekit import VERSION
from organizekit.core import JSON_SCHEMA, json_document, print_json, slug_id

ENVELOPE = ("schema", "tool", "version", "command")
VALID_SRT = "1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n"


class EnvelopeTests(unittest.TestCase):
    def test_the_envelope_comes_first_and_in_a_fixed_order(self) -> None:
        """A human reading a saved document should see what it is on line two."""
        document = json_document("example", "9.9.9", payload=1)
        self.assertEqual(tuple(document)[:4], ENVELOPE)
        self.assertEqual(document["schema"], JSON_SCHEMA)
        self.assertEqual(document["tool"], "organize")
        self.assertEqual(document["version"], "9.9.9")
        self.assertEqual(document["command"], "example")

    def test_a_payload_key_cannot_silently_shadow_the_envelope(self) -> None:
        """``command="audit"`` in a payload is a mistake, and it is loud."""
        with self.assertRaises(TypeError):
            json_document("example", "9.9.9", command="hijacked")

    def test_print_json_writes_one_parseable_document(self) -> None:
        buf = io.StringIO()
        print_json(json_document("example", "1.0.0", value="ü"), buf)
        self.assertEqual(json.loads(buf.getvalue())["value"], "ü")
        self.assertIn("ü", buf.getvalue())  # not escaped to \u00fc

    def test_print_json_defaults_to_stdout(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_json(json_document("example", "1.0.0"))
        self.assertEqual(json.loads(buf.getvalue())["command"], "example")

    def test_slug_id_is_the_one_rule_every_command_uses(self) -> None:
        self.assertEqual(slug_id("Bit depth"), "bit-depth")
        self.assertEqual(slug_id("MKVToolNix (mkvmerge)"), "mkvtoolnix-mkvmerge")


class ThreeCommandsOneShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="json_contract_")
        self.root = Path(self._td.name)
        self.library = self.root / "library"
        folder = self.library / "Alpha (2001)"
        folder.mkdir(parents=True)
        (folder / "Alpha (2001).mkv").write_bytes(b"x" * 4096)
        (folder / "Alpha (2001).eng.srt").write_text(VALID_SRT, encoding="utf-8")
        self.addCleanup(self._td.cleanup)
        self.addCleanup(setattr, la.log, "stream", None)
        self.addCleanup(setattr, la.log, "file", None)

    def _capture(self, argv: list[str]) -> dict:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            organize.main(argv)
        return json.loads(out.getvalue())

    def _capture_audit(self) -> dict:
        """`organize audit` is a subprocess, so call the auditor's own main."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            la.main(["--json", "--source", str(self.library),
                     "--log", str(self.root / "run.log"),
                     "--report", str(self.root / "report.txt"),
                     "--state-db", str(self.root / "state.db")])
        return json.loads(out.getvalue())

    def all_three(self) -> dict[str, dict]:
        return {
            "doctor": self._capture(["doctor", "--json", "--target", str(self.library),
                                     "--source", str(self.library)]),
            "status": self._capture(["status", "--json", "--library", str(self.library),
                                     "--state-db", str(self.root / "state.db")]),
            "audit": self._capture_audit(),
        }

    def test_organize_audit_passes_the_flag_through_to_the_tool(self) -> None:
        """`organize audit` is a subprocess; prove the document survives the trip."""
        proc = subprocess.run(
            [sys.executable, "organize.py", "audit", "--json",
             "--source", str(self.library), "--log", str(self.root / "run.log"),
             "--report", str(self.root / "report.txt"),
             "--state-db", str(self.root / "state.db")],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, encoding="utf-8", check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        document = json.loads(proc.stdout)
        self.assertEqual(document["command"], "audit")
        self.assertEqual(document["canonical"], 1)
        self.assertIn("Starting read-only library audit", proc.stderr)

    def test_all_three_open_with_the_same_envelope(self) -> None:
        for command, document in self.all_three().items():
            with self.subTest(command=command):
                self.assertEqual(tuple(document)[:4], ENVELOPE)
                self.assertEqual(document["schema"], JSON_SCHEMA)
                self.assertEqual(document["tool"], "organize")
                self.assertEqual(document["command"], command)

    def test_one_install_reports_one_version(self) -> None:
        """A tool with its own version number must not leak it into the envelope."""
        versions = {document["version"] for document in self.all_three().values()}
        self.assertEqual(versions, {VERSION})

    def test_every_document_carries_its_exit_code(self) -> None:
        for command, document in self.all_three().items():
            with self.subTest(command=command):
                self.assertEqual(document["exit_code"], 0)

    def test_no_command_stamps_the_clock_into_its_document(self) -> None:
        """The property that lets a scheduled job diff yesterday against today."""
        for command, document in self.all_three().items():
            with self.subTest(command=command):
                text = json.dumps(document)
                for stamp in ("timestamp", "generated", "elapsed", "started_at"):
                    self.assertNotIn(stamp, text)


if __name__ == "__main__":
    unittest.main()
