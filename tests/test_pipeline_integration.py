"""End-to-end integration for the subtitle toolchain.

Chains the *real* orchestration code of ``subtitle_fetcher.py``,
``mkv_track_cleaner.py`` and ``sync_subtitles.py`` down the canonical pipeline
(``fetcher -> cleaner -> sync``), faking only the external binaries
(``mkvmerge`` / ``ffsubsync``) exactly the way the individual tool suites do.

The properties pinned here are the cross-tool contracts that keep the pipeline
lossless end to end:

* the fetcher's canonical ``<stem>.eng.srt`` naming is what the cleaner
  validates before it dares to drop embedded subtitles;
* a successful remux swaps the MKV but leaves the sidecar byte-identical, so
  the fetcher's extraction record (keyed on sidecar SHA-256) still round-trips
  and the sync tool can short-circuit an extracted subtitle;
* with no extraction record the sync tool runs ffsubsync and atomically swaps
  the aligned sidecar;
* a failed sync can ask the fetcher for a replacement download through a stable
  import contract that degrades gracefully when no API key is configured.
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
import subtitle_fetcher as sf
import sync_subtitles as ss

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
SYNCED_SRT_TEXT = "1\n00:00:01,500 --> 00:00:02,500\nSynced dialogue\n"


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
        # The fetcher writes every sidecar as UTF-8 with LF newlines
        # (atomic_write_text uses newline="\n"), so the on-disk bytes match
        # sha256_text() on Windows too. Mirror that here or the
        # extraction-ledger shortcut under test would mismatch on Windows
        # (\r\n on disk vs \n in the stored hash).
        self.srt.write_text(SRT_TEXT, encoding="utf-8", newline="\n")
        self.srt_sha = sf.sha256_text(SRT_TEXT)

        self._real_mkvmerge = tc._run_mkvmerge
        self._real_target_root = tc._target_root
        self._real_sync = ss.run_ffsubsync
        self._saved_ledger_env = os.environ.get(sf.EXTRACTED_LEDGER_ENV)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._run_mkvmerge = self._real_mkvmerge
        tc._target_root = self._real_target_root
        ss.run_ffsubsync = self._real_sync
        if self._saved_ledger_env is None:
            os.environ.pop(sf.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sf.EXTRACTED_LEDGER_ENV] = self._saved_ledger_env

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

    def _sync_config(self) -> ss.Config:
        return ss.Config(
            library=self.lib, log_file=self.out / "sync.log",
            report_file=self.out / "sync.txt",
            sync_ledger=self.out / "sync_state.json",
            min_offset_seconds=0.1, max_offset_seconds=30.0,
            timeout_seconds=30.0, ffsubsync_binary="ffsubsync",
        )

    def test_fetcher_sidecar_is_the_cleaner_contract(self) -> None:
        """The fetcher's canonical sidecar is what the cleaner validates."""
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
        # fetcher's SHA-keyed extraction record stays valid across the remux.
        self.assertEqual(self.movie.read_bytes(), b"y" * 8192)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SRT_TEXT)
        self.assertEqual(sf.sha256_text(self.srt.read_text(encoding="utf-8")), self.srt_sha)

        # No transaction debris left in the folder.
        names = {p.name for p in self.folder.iterdir()}
        self.assertFalse(any(n.startswith("temp_clean_") for n in names))
        self.assertFalse(any(n.startswith(".track_cleaner.") for n in names))

    def test_sync_short_circuits_extracted_sidecar(self) -> None:
        """After a remux, the fetcher's extraction record still short-circuits sync."""
        self._install_fake_mkvmerge()
        stats = _empty_stats()
        with contextlib.redirect_stdout(io.StringIO()):
            tc.process_mkv(self.movie, stats, "mkvmerge", dry_run=False, log_file_path=None)
        self.assertEqual(stats["errors"], [])

        ledger = self.out / "subtitle_fetcher_extracted.json"
        os.environ[sf.EXTRACTED_LEDGER_ENV] = str(ledger)
        track = sf.EmbeddedSubtitleTrack(
            track_id=3, codec_id="S_HDMV/PGS", language="eng", name="",
            kind="image", extension=".sup", default=False, forced=False, sdh=False, rank=0,
        )
        self.assertTrue(sf.record_extracted_sidecar(
            self.movie, self.srt, track=track, method="ocr", cue_count=1,
            sha256=self.srt_sha, ocr_backend="tesseract", path=ledger,
        ))
        self.assertIsNotNone(sf.find_extracted_record(self.srt, self.srt_sha))

        jobs, skips, video_count = ss.discover_jobs(self.lib)
        self.assertEqual((len(jobs), skips, video_count), (1, [], 1))
        self.assertEqual((jobs[0].srt, jobs[0].video), (self.srt, self.movie))

        launched: list[list[str]] = []

        def never_run(cfg, command):
            launched.append(list(command))
            raise AssertionError("ffsubsync must not run for an extracted sidecar")

        ss.run_ffsubsync = never_run
        result = ss.sync_one(jobs[0], self._sync_config(), "ffsubsync",
                             ss.FfsubsyncFeatures(), state={})

        self.assertEqual(launched, [])
        self.assertEqual(result.status, ss.STATUS_EXTRACTED)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SRT_TEXT)

    def test_sync_runs_ffsubsync_without_ledger(self) -> None:
        """No extraction record: the sidecar goes through ffsubsync and is swapped."""
        jobs, skips, video_count = ss.discover_jobs(self.lib)
        self.assertEqual((len(jobs), skips, video_count), (1, [], 1))

        def fake_ffsubsync(cfg, command):
            argv = [str(part) for part in command]
            staging = Path(argv[argv.index("-o") + 1])
            staging.write_text(SYNCED_SRT_TEXT, encoding="utf-8", newline="\n")
            return 0, "", (
                "INFO: offset seconds: 1.500\n"
                "INFO: framerate scale factor: 1.0\n"
                "INFO: score: 4.2\n"
            )

        ss.run_ffsubsync = fake_ffsubsync
        result = ss.sync_one(jobs[0], self._sync_config(), "ffsubsync",
                             ss.FfsubsyncFeatures(), state={})

        self.assertEqual(result.status, ss.STATUS_SYNCED)
        self.assertAlmostEqual(result.offset_seconds or 0.0, 1.5)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SYNCED_SRT_TEXT)

    def test_refetch_contract_degrades_gracefully(self) -> None:
        """The sync tool's replacement-fetch import degrades without an API key."""
        os.environ.pop("OPENSUBTITLES_API_KEY", None)
        os.environ.pop("SUBDL_API_KEY", None)
        ok, file_id, detail = ss._refetch_sidecar(self.movie, self.srt, exclude_ids=[], log_file=None)
        self.assertFalse(ok)
        self.assertEqual(file_id, "")
        self.assertIn("API key", detail)


if __name__ == "__main__":
    unittest.main()
