"""End-to-end integration for the subtitle toolchain.

Chains the *real* orchestration code of ``subtitle_extractor.py`` and
``mkv_track_cleaner.py`` down the canonical pipeline (``extractor -> cleaner``),
faking only the external binary (``mkvmerge``) exactly the way the individual
tool suites do.

The properties pinned here are the cross-tool contracts that keep the pipeline
lossless end to end:

* the extractor's canonical ``<stem>.eng.srt`` naming is what the cleaner
  validates before it dares to drop embedded subtitles;
* a successful remux swaps the MKV but leaves the sidecar byte-identical, so
  the extractor's provenance record (keyed on sidecar SHA-256) still
  round-trips across the remux that strips the embedded tracks;
* a sidecar with no extraction record has no provenance: the ledger says so
  rather than guessing.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import mkv_track_cleaner as tc
import subtitle_extractor as sx

ORIG: dict = {
    "container": {"recognized": True, "supported": True,
                  "properties": {"duration": 5_400_000_000_000}},
    "tracks": [
        {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {
            "codec_id": "V_MPEGH/ISO/HEVC", "pixel_dimensions": "1920x1080",
            "display_dimensions": "1920x1080", "flag_default": True}},
        {"id": 1, "type": "audio", "codec": "TrueHD Atmos", "properties": {
            "codec_id": "A_TRUEHD", "language": "eng", "track_name": "TrueHD Atmos 7.1",
            "audio_channels": 8, "audio_sampling_frequency": 48000,
            "flag_default": True}},
        {"id": 2, "type": "audio", "codec": "AC-3", "properties": {
            "codec_id": "A_AC3", "language": "eng", "track_name": "Commentary",
            "flag_commentary": True, "audio_channels": 2,
            "audio_sampling_frequency": 48000}},
        {"id": 3, "type": "subtitles", "codec": "HDMV PGS", "properties": {
            "codec_id": "S_HDMV/PGS", "language": "eng"}},
    ],
    "attachments": [], "chapters": [],
}

CLEAN: dict = {
    "container": {"recognized": True, "supported": True,
                  "properties": {"duration": 5_400_000_000_000}},
    "tracks": [
        {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {
            "codec_id": "V_MPEGH/ISO/HEVC", "pixel_dimensions": "1920x1080",
            "display_dimensions": "1920x1080", "flag_default": True}},
        {"id": 1, "type": "audio", "codec": "TrueHD Atmos", "properties": {
            "codec_id": "A_TRUEHD", "language": "eng", "track_name": "TrueHD Atmos 7.1",
            "audio_channels": 8, "audio_sampling_frequency": 48000,
            "flag_default": True}},
    ],
    "attachments": [], "chapters": [],
}

SRT_TEXT = "1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n"


def _empty_stats() -> dict:
    return {
        "start_time": datetime.datetime.now(),
        "total_scanned": 0,
        "cleaned": [],
        "already_clean": [],
        "skipped_no_english": [],
        "skipped_layout": [],
        "deferred_hardlinked": [],
        "errors": [],
        "remux_without_srt": [],
        "diagnostics": [],
        "total_space_saved_bytes": 0,
    }


class SubtitlePipelineIntegrationTests(unittest.TestCase):
    """The three subtitle tools, chained exactly as the pipeline runs them."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="pipeline_itest_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

        self.lib = self.tmp / "library"
        self.out = self.tmp / "reports"
        self.lib.mkdir()
        self.out.mkdir()

        self.folder = self.lib / "Movie (2020)"
        self.folder.mkdir()
        self.movie = self.folder / "Movie (2020).mkv"
        self.movie.write_bytes(b"x" * 4096)
        self.srt = self.folder / "Movie (2020).eng.srt"
        # The extractor writes every sidecar as UTF-8 with LF newlines
        # (atomic_write_text uses newline="\n"), so the on-disk bytes match
        # sha256_text() on Windows too. Mirror that here or the
        # extraction-ledger shortcut under test would mismatch on Windows
        # (\r\n on disk vs \n in the stored hash).
        self.srt.write_text(SRT_TEXT, encoding="utf-8", newline="\n")
        self.srt_sha = sx.sha256_text(SRT_TEXT)

        self._real_mkvmerge = tc._run_mkvmerge
        self._real_target_root = tc._target_root
        self._saved_ledger_env = os.environ.get(sx.EXTRACTED_LEDGER_ENV)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._run_mkvmerge = self._real_mkvmerge
        tc._target_root = self._real_target_root
        if self._saved_ledger_env is None:
            os.environ.pop(sx.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sx.EXTRACTED_LEDGER_ENV] = self._saved_ledger_env

    def _install_fake_mkvmerge(self) -> list[list[str]]:
        """Fake mkvmerge: real remux file write + source/remuxed -J metadata."""
        calls: list[list[str]] = []

        def fake_mkvmerge(cmd, on_progress=None):
            argv = [str(part) for part in cmd]
            calls.append(argv)
            if "-J" in argv:
                target = argv[-1]
                info = CLEAN if "temp_clean_" in target else ORIG
                return 0, json.dumps(info), ""
            if "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_bytes(b"y" * 8192)
                return 0, "", ""
            return 0, "", ""

        tc._run_mkvmerge = fake_mkvmerge
        tc._target_root = self.lib
        return calls

    def test_extractor_sidecar_is_the_cleaner_contract(self) -> None:
        """The extractor's canonical sidecar is what the cleaner validates."""
        verdict = tc.validate_exact_external_english_srt(self.movie)
        self.assertTrue(verdict.get("valid"), verdict.get("reason"))
        self.assertEqual(Path(str(verdict["path"])).name, "Movie (2020).eng.srt")

    def test_cleaner_strips_embeds_and_preserves_sidecar(self) -> None:
        self._install_fake_mkvmerge()
        stats = _empty_stats()
        with contextlib.redirect_stdout(io.StringIO()):
            tc.process_mkv(self.movie, stats, "mkvmerge", dry_run=False, log_file_path=None)

        self.assertEqual(stats["errors"], [])
        self.assertEqual(len(stats["cleaned"]), 1)
        cleaned = stats["cleaned"][0]
        self.assertEqual(cleaned["kept_subs_count"], 0)
        self.assertEqual(cleaned["removed_subs_count"], 1)
        self.assertEqual(cleaned["removed_audio_count"], 1)  # commentary dropped
        self.assertTrue(cleaned.get("external_srt", {}).get("valid"))

        # The remux swapped the MKV but never touched the sidecar, so the
        # extractor's SHA-keyed record stays valid across the remux.
        self.assertEqual(self.movie.read_bytes(), b"y" * 8192)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SRT_TEXT)
        self.assertEqual(sx.sha256_text(self.srt.read_text(encoding="utf-8")), self.srt_sha)

        # No transaction debris left in the folder.
        names = {p.name for p in self.folder.iterdir()}
        self.assertFalse(any(n.startswith("temp_clean_") for n in names))
        self.assertFalse(any(n.startswith(".track_cleaner.") for n in names))

    def _record_extraction(self, ledger: Path) -> None:
        os.environ[sx.EXTRACTED_LEDGER_ENV] = str(ledger)
        track = sx.EmbeddedSubtitleTrack(
            track_id=3, codec_id="S_HDMV/PGS", language="eng", name="",
            kind="image", extension=".sup", default=False, forced=False, sdh=False, rank=0,
        )
        self.assertTrue(sx.record_extracted_sidecar(
            self.movie, self.srt, track=track, method="ocr", cue_count=1,
            sha256=self.srt_sha, ocr_backend="tesseract", path=ledger,
        ))

    def test_the_extraction_record_survives_the_remux(self) -> None:
        """The ledger is keyed on sidecar bytes, and the remux never touches them.

        This is the cross-tool half of the provenance contract: the cleaner
        rewrites the movie and strips the embedded track the sidecar came from,
        so if the record did not still describe the sidecar afterwards there
        would be no way left to tell an extracted sidecar from a hand-made one.
        """
        self._record_extraction(self.out / "subtitle_extractor_extracted.json")
        self.assertIsNotNone(sx.find_extracted_record(self.srt, self.srt_sha))

        self._install_fake_mkvmerge()
        stats = _empty_stats()
        with contextlib.redirect_stdout(io.StringIO()):
            tc.process_mkv(self.movie, stats, "mkvmerge", dry_run=False, log_file_path=None)
        self.assertEqual(stats["errors"], [])
        self.assertEqual(self.movie.read_bytes(), b"y" * 8192)  # the movie did change

        record = sx.find_extracted_record(self.srt, self.srt_sha)
        self.assertIsNotNone(record, "the record must still describe the sidecar")
        self.assertEqual(record["method"], "ocr")
        self.assertEqual(record["ocr_backend"], "tesseract")
        self.assertEqual(record["codec_id"], "S_HDMV/PGS")

    def test_a_sidecar_with_no_record_has_no_provenance(self) -> None:
        """No extraction record: the sidecar is somebody else's file."""
        os.environ[sx.EXTRACTED_LEDGER_ENV] = str(self.out / "empty_ledger.json")

        self._install_fake_mkvmerge()
        stats = _empty_stats()
        with contextlib.redirect_stdout(io.StringIO()):
            tc.process_mkv(self.movie, stats, "mkvmerge", dry_run=False, log_file_path=None)
        self.assertEqual(stats["errors"], [])

        self.assertIsNone(sx.find_extracted_record(self.srt, self.srt_sha))
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SRT_TEXT)


if __name__ == "__main__":
    unittest.main()
