"""Tests for the pure classification helpers in ``mkv_track_cleaner.py``."""

from __future__ import annotations

import contextlib
import datetime
import io
import tempfile
import unittest
from pathlib import Path

import mkv_track_cleaner as tc


class LanguageAndCommentaryTests(unittest.TestCase):
    def test_normalize_language(self) -> None:
        self.assertEqual(tc.normalize_language("fre"), "fr")
        self.assertEqual(tc.normalize_language("eng"), "en")
        self.assertEqual(tc.normalize_language("en"), "en")

    def test_matching_language(self) -> None:
        eng = {"id": 1, "type": "audio", "properties": {"language": "eng", "language_ietf": "en"}}
        self.assertTrue(tc.is_matching_language(eng, {"en", "eng"}))
        self.assertFalse(tc.is_matching_language(eng, {"fr"}))

    def test_sdh_subtitle_is_kept(self) -> None:
        sdh = {"type": "subtitles", "properties": {
            "language": "eng", "track_name": "English SDH",
            "flag_hearing_impaired": True, "flag_visual_impaired": True,
        }}
        self.assertFalse(tc.is_commentary_track(sdh, True))

    def test_commentary_and_dvs_dropped(self) -> None:
        comm = {"type": "audio", "properties": {"language": "eng", "track_name": "Director Commentary", "flag_commentary": True}}
        self.assertTrue(tc.is_commentary_track(comm, True))
        dvs = {"type": "audio", "properties": {"language": "eng", "track_name": "English Audio Description", "flag_visual_impaired": True}}
        self.assertTrue(tc.is_commentary_track(dvs, True))

    def test_forced_subtitle(self) -> None:
        forced = {"type": "subtitles", "properties": {"language": "eng", "track_name": "English Forced", "flag_forced": True}}
        self.assertTrue(tc.is_forced_subtitle(forced))


class AudioQualityTests(unittest.TestCase):
    def test_truehd_beats_aac(self) -> None:
        truehd = {"codec": "TrueHD", "properties": {"codec_id": "A_MLP", "audio_channels": 8, "track_name": "Atmos"}}
        aac = {"codec": "AAC", "properties": {"codec_id": "A_AAC", "audio_channels": 6}}
        self.assertGreater(tc.get_audio_quality_score(truehd), tc.get_audio_quality_score(aac))


class ProgressParsingTests(unittest.TestCase):
    def test_progress_forms(self) -> None:
        self.assertEqual(tc._parse_mkvmerge_progress("Progress: 45%"), 45)
        self.assertEqual(tc._parse_mkvmerge_progress("#GUI#progress 80%"), 80)
        self.assertEqual(tc._parse_mkvmerge_progress("#GUI#progress#parts=1/4"), 25)
        self.assertIsNone(tc._parse_mkvmerge_progress("hello"))


def _empty_stats() -> dict:
    """A stats dict shaped like the one ``main()`` builds before scanning."""
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


class HardlinkDeferralTests(unittest.TestCase):
    """Hardlinked movies defer by default; --allow-hardlinked overrides that.

    movie_standardizer.py is hardlink-only, so every freshly completed movie
    shares an inode with its qBittorrent source. qBittorrent's default "stop
    seeding" action only pauses the torrent and leaves the file, so that link
    can persist indefinitely and the cleaner would defer the movie forever.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="cleaner_hl_test_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.calls: list = []

        self._root = tc._target_root
        self._run = tc._run_mkvmerge
        tc._target_root = self.tmp

        def boom(*_args, **_kwargs):
            self.calls.append(_args)
            raise RuntimeError("mkvmerge must not run in this unit test")

        tc._run_mkvmerge = boom
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._target_root = self._root
        tc._run_mkvmerge = self._run

    def _hardlinked_movie(self, name: str) -> Path:
        """A canonical movie folder whose MKV has a second link (the seed source)."""
        folder = self.tmp / name
        folder.mkdir()
        movie = folder / f"{name}.mkv"
        movie.write_bytes(b"x" * 2048)
        (self.tmp / f"{name}.source.mkv").hardlink_to(movie)
        self.assertGreaterEqual(tc.hardlink_count(movie), 2)
        return movie

    def _run_it(self, movie: Path, **kwargs) -> dict:
        stats = _empty_stats()
        with contextlib.redirect_stdout(io.StringIO()):
            tc.process_mkv(movie, stats, "stub-mkvmerge", log_file_path=None, **kwargs)
        return stats

    def test_defers_a_hardlinked_movie_by_default(self) -> None:
        stats = self._run_it(self._hardlinked_movie("Film (2000)"))
        self.assertEqual(len(stats["deferred_hardlinked"]), 1)
        self.assertGreaterEqual(stats["deferred_hardlinked"][0]["hardlinks"], 2)
        self.assertEqual(stats["errors"], [])
        self.assertEqual(self.calls, [], "mkvmerge must not be invoked on a deferred movie")

    def test_allow_hardlinked_remuxes_anyway(self) -> None:
        stats = self._run_it(self._hardlinked_movie("Other (2001)"), allow_hardlinked=True)
        self.assertEqual(stats["deferred_hardlinked"], [])
        self.assertTrue(self.calls, "mkvmerge should be reached with --allow-hardlinked")


class RemuxWithoutSrtReportTests(unittest.TestCase):
    """Remuxing with no external SRT invalidates the moviehash; say so in the report."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="cleaner_report_test_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _render(self, stats: dict) -> str:
        report_path = self.tmp / "report.txt"
        with contextlib.redirect_stdout(io.StringIO()):
            tc.generate_and_save_report(
                stats, dry_run=True, report_file=str(report_path), log_file_path=None,
            )
        return report_path.read_text(encoding="utf-8")

    def test_section_appears_when_a_movie_had_no_srt(self) -> None:
        stats = _empty_stats()
        stats["remux_without_srt"] = ["Film (2000).mkv"]
        text = self._render(stats)
        self.assertIn("REMUXED WITH NO EXTERNAL SRT", text)
        self.assertIn("moviehash", text)
        self.assertIn("Film (2000).mkv", text)
        self.assertIn("Remuxed Without SRT       : 1", text)

    def test_section_is_absent_when_every_movie_had_an_srt(self) -> None:
        text = self._render(_empty_stats())
        self.assertNotIn("REMUXED WITH NO EXTERNAL SRT", text)
        self.assertIn("Remuxed Without SRT       : 0", text)


if __name__ == "__main__":
    unittest.main()
