"""Tests for the pure classification helpers in ``mkv_track_cleaner.py``."""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import pathlib
import tempfile
import unittest
from pathlib import Path

from reporttext import scorecard

import mkv_track_cleaner as tc
from common import MediaProbeCache


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
    """A movie being seeded is never touched, and there is no override.

    movie_standardizer.py is hardlink-only, so every freshly completed movie
    shares an inode with its qBittorrent source. qBittorrent's default "stop
    seeding" action only pauses the torrent and leaves the file, so that link
    can persist indefinitely - which is why the deferral message and the report
    have to tell the operator exactly how to release it.
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

    def test_there_is_no_override_flag(self) -> None:
        """The policy is absolute: no flag may remux a movie being seeded."""
        import subprocess

        help_text = subprocess.run(
            [__import__("sys").executable, str(pathlib.Path(tc.__file__)), "--help"],
            capture_output=True, check=False, encoding="utf-8", errors="replace",
        ).stdout
        self.assertNotIn("--allow-hardlinked", help_text)
        self.assertNotIn("allow_hardlinked", tc.process_mkv.__code__.co_varnames)


class ForeignFilmWithExternalSrtTests(unittest.TestCase):
    """A foreign film with a validated ``.eng.srt`` is cleaned, not skipped.

    Without English audio the cleaner used to bail entirely, which left PGS
    embeds beside a perfectly good external SRT (e.g. Parasite). With a
    validated sidecar the original-language audio is kept and every embedded
    subtitle is stripped.
    """

    FOREIGN_INFO = {
        "container": {"recognized": True, "supported": True,
                      "properties": {"duration": 6_000_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {
                "codec_id": "V_MPEGH/ISO/HEVC", "pixel_dimensions": "1920x1080",
                "display_dimensions": "1920x1080", "flag_default": True}},
            {"id": 1, "type": "audio", "codec": "AAC", "properties": {
                "codec_id": "A_AAC", "language": "kor",
                "audio_channels": 2, "audio_sampling_frequency": 48000,
                "flag_default": True}},
            {"id": 2, "type": "audio", "codec": "AC-3", "properties": {
                "codec_id": "A_AC3", "language": "kor", "track_name": "Commentary",
                "flag_commentary": True, "audio_channels": 2,
                "audio_sampling_frequency": 48000}},
            {"id": 3, "type": "subtitles", "codec": "HDMV PGS", "properties": {
                "codec_id": "S_HDMV/PGS", "language": "eng", "flag_default": False}},
        ],
        "attachments": [], "chapters": [],
    }

    UNTAGGED_INFO = {
        "container": {"recognized": True, "supported": True,
                      "properties": {"duration": 6_000_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {
                "codec_id": "V_MPEGH/ISO/HEVC", "pixel_dimensions": "1920x1080",
                "display_dimensions": "1920x1080", "flag_default": True}},
            {"id": 1, "type": "audio", "codec": "AAC", "properties": {
                "codec_id": "A_AAC", "language": "und",
                "audio_channels": 2, "audio_sampling_frequency": 48000,
                "flag_default": True}},
            {"id": 2, "type": "subtitles", "codec": "SubRip/SRT", "properties": {
                "codec_id": "S_TEXT/UTF8", "language": "eng", "track_name": "ENG"}},
            {"id": 3, "type": "subtitles", "codec": "SubRip/SRT", "properties": {
                "codec_id": "S_TEXT/UTF8", "language": "eng", "track_name": "ENG SDH"}},
        ],
        "attachments": [], "chapters": [],
    }

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="cleaner_foreign_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.folder = self.root / "Film (2019)"
        self.folder.mkdir()
        self.movie = self.folder / "Film (2019).mkv"
        self.movie.write_bytes(b"x" * 4096)
        self.calls: list[list[str]] = []
        self.info = self.FOREIGN_INFO

        def fake_mkvmerge(cmd, on_progress=None):
            self.calls.append([str(part) for part in cmd])
            if "-J" in cmd:
                return 0, json.dumps(self.info), ""
            return 1, "", "stub refuses to remux"

        self._real = tc._run_mkvmerge
        self._real_root = tc._target_root
        tc._run_mkvmerge = fake_mkvmerge
        tc._target_root = self.root
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._run_mkvmerge = self._real
        tc._target_root = self._real_root

    def _write_srt(self) -> None:
        from common import EXTERNAL_SRT_SUFFIX
        (self.folder / f"Film (2019){EXTERNAL_SRT_SUFFIX}").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n", encoding="utf-8",
        )

    def _run(self) -> dict:
        stats = _empty_stats()
        with contextlib.redirect_stdout(io.StringIO()):
            tc.process_mkv(self.movie, stats, "mkvmerge", dry_run=True, log_file_path=None)
        return stats

    def test_foreign_without_srt_is_still_skipped(self) -> None:
        stats = self._run()
        self.assertEqual(len(stats["skipped_no_english"]), 1)
        self.assertEqual(stats["cleaned"], [])
        self.assertIn("foreign film", stats["skipped_no_english"][0]["reason"])

    def test_foreign_with_validated_srt_is_cleaned(self) -> None:
        self._write_srt()
        stats = self._run()
        self.assertEqual(stats["skipped_no_english"], [])
        self.assertEqual(len(stats["cleaned"]), 1)
        cleaned = stats["cleaned"][0]
        self.assertEqual(cleaned["kept_subs_count"], 0)
        self.assertEqual(cleaned["removed_subs_count"], 1)
        self.assertEqual(cleaned["removed_audio_count"], 1)  # commentary dropped
        self.assertIsNotNone(cleaned.get("external_srt"))
        self.assertIn("[kor]", cleaned["kept_audio"])

    def test_untagged_audio_with_srt_is_cleaned(self) -> None:
        """IT (2017)-style: und audio + external SRT + embedded SRTs."""
        self.info = self.UNTAGGED_INFO
        self._write_srt()
        stats = self._run()
        self.assertEqual(stats["skipped_no_english"], [])
        self.assertEqual(len(stats["cleaned"]), 1)
        cleaned = stats["cleaned"][0]
        self.assertEqual(cleaned["kept_subs_count"], 0)
        self.assertEqual(cleaned["removed_subs_count"], 2)
        self.assertEqual(cleaned["removed_audio_count"], 0)


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
        self.assertEqual(scorecard(text)["Remuxed without SRT"], 1)

    def test_section_is_absent_when_every_movie_had_an_srt(self) -> None:
        text = self._render(_empty_stats())
        self.assertNotIn("REMUXED WITH NO EXTERNAL SRT", text)
        self.assertEqual(scorecard(text)["Remuxed without SRT"], 0)


class MetadataCacheWiringTests(unittest.TestCase):
    """process_mkv must actually consult the cache, not just accept one."""

    INFO = {
        "container": {"recognized": True, "supported": True,
                      "properties": {"duration": 6_000_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "AVC/H.264/MPEG-4p10", "properties": {
                "codec_id": "V_MPEG4/ISO/AVC", "pixel_dimensions": "1920x1080",
                "display_dimensions": "1920x1080", "tag_number_of_frames": "144000",
                "flag_default": True}},
            {"id": 1, "type": "audio", "codec": "AC-3", "properties": {
                "codec_id": "A_AC3", "language": "eng", "language_ietf": "en",
                "track_name": "English 5.1", "audio_channels": 6,
                "audio_sampling_frequency": 48000, "flag_default": True}},
        ],
        "attachments": [], "chapters": [],
    }

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="tc_cache_")
        self.root = pathlib.Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.movie = self.root / "Film (2020).mkv"
        self.movie.write_bytes(b"x" * 4096)
        self.calls: list[str] = []

        def fake_mkvmerge(cmd, on_progress=None):
            self.calls.append(" ".join(str(part) for part in cmd))
            if "-J" in cmd:
                return 0, json.dumps(self.INFO), ""
            return 1, "", "stub refuses to remux"

        self._real = tc._run_mkvmerge
        self._real_root = tc._target_root
        tc._run_mkvmerge = fake_mkvmerge
        tc._target_root = None  # skip the canonical-layout gate in this unit test
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._run_mkvmerge = self._real
        tc._target_root = self._real_root

    def _run(self, cache=None) -> dict:
        stats = {"cleaned": [], "already_clean": [], "skipped_no_english": [],
                 "skipped_layout": [], "deferred_hardlinked": [], "errors": [],
                 "total_scanned": 0, "bytes_freed": 0}
        with contextlib.redirect_stdout(io.StringIO()):
            tc.process_mkv(self.movie, stats, "mkvmerge", dry_run=True,
                           log_file_path=None, probe_cache=cache)
        return stats

    def _cache(self, enabled: bool = True):
        return MediaProbeCache(self.root / "cache.json", tool="mkv_track_cleaner",
                               enabled=enabled)

    def test_second_call_reuses_the_metadata(self) -> None:
        cache = self._cache()
        self._run(cache)
        first_calls = len(self.calls)
        self._run(cache)
        self.assertEqual(len(self.calls), first_calls, "warm run must not respawn mkvmerge")
        self.assertGreater(first_calls, 0)

    def test_changed_file_is_reread(self) -> None:
        cache = self._cache()
        self._run(cache)
        before = len(self.calls)
        self.movie.write_bytes(b"y" * 8192)
        self._run(cache)
        self.assertGreater(len(self.calls), before)

    def test_no_cache_rereads_every_time(self) -> None:
        cache = self._cache(enabled=False)
        self._run(cache)
        before = len(self.calls)
        self._run(cache)
        self.assertGreater(len(self.calls), before)

    def test_a_deferred_movie_is_never_read_from_cache(self) -> None:
        # The hardlink gate runs before the metadata read, so a seeded movie
        # must stay deferred even when its metadata is cached.
        cache = self._cache()
        self._run(cache)
        linked = self.root / "hardlink.mkv"
        linked.hardlink_to(self.movie)
        cache.put(self.movie, self.movie.stat().st_size,
                  self.movie.stat().st_mtime_ns, self.INFO)
        before = len(self.calls)
        stats = self._run(cache)
        self.assertEqual(len(self.calls), before, "deferred movie must not be probed")
        self.assertEqual(len(stats["deferred_hardlinked"]), 1)


class RemuxVerificationGuardsTests(unittest.TestCase):
    """The guards that stop a bad remux replacing a good movie.

    ``_verify_remux_output`` is the last thing standing between a truncated
    mkvmerge output and ``os.replace()`` overwriting the original. The repo's
    own self-test covers the fingerprint and transaction paths, but the size and
    duration truncation guards — the ones that catch a remux that silently lost
    most of the film — had no coverage at all. A logic error there would destroy
    real movies, so each rejection path is pinned here.
    """

    SOURCE_SIZE = 10_000_000  # 10 MB source

    def _fixtures(self):
        video = {"type": "video", "codec": "AVC/H.264/MPEG-4p10", "properties": {
            "codec_id": "V_MPEG4/ISO/AVC", "pixel_dimensions": "1920x1080",
            "display_dimensions": "1920x1080", "tag_number_of_frames": "240",
            "flag_default": True}}
        audio = {"type": "audio", "codec": "AC-3", "properties": {
            "codec_id": "A_AC3", "language": "eng", "language_ietf": "en",
            "track_name": "English 5.1", "audio_channels": 6,
            "audio_sampling_frequency": 48000, "flag_default": False}}
        source = {
            "container": {"recognized": True, "supported": True,
                          "properties": {"duration": 10_000_000_000}},
            "tracks": [video, audio], "attachments": [], "chapters": [],
        }
        plan = tc.build_verification_plan(source, audio, [], self.SOURCE_SIZE)
        return source, plan

    def _write(self, size: int) -> Path:
        temp = Path(self._td.name) / "remuxed.mkv"
        temp.write_bytes(b"x" * size)
        return temp

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="remux_verify_")
        self.addCleanup(self._td.cleanup)

    def _output(self, source):
        """Model what mkvmerge produces: the retained audio becomes default."""
        out_info = json.loads(json.dumps(source))
        out_info["tracks"][1]["properties"]["flag_default"] = True
        return out_info

    def _check(self, out_info, size):
        return tc._verify_remux_info(self._write(size), out_info, self.plan)

    def test_baseline_clean_remux_is_accepted(self) -> None:
        self.source, self.plan = self._fixtures()
        ok, reason = self._check(self._output(self.source), int(self.SOURCE_SIZE * 0.98))
        self.assertTrue(ok, reason)

    def test_tiny_output_is_rejected(self) -> None:
        self.source, self.plan = self._fixtures()
        ok, reason = self._check(self._output(self.source), 512)
        self.assertFalse(ok)
        self.assertIn("tiny", reason)

    def test_truncated_output_below_half_is_rejected(self) -> None:
        """The primary data-loss guard: a remux that lost most of the film."""
        self.source, self.plan = self._fixtures()
        ok, reason = self._check(self._output(self.source), int(self.SOURCE_SIZE * 0.20))
        self.assertFalse(ok)
        self.assertIn("shrank too much", reason)

    def test_output_just_above_the_ratio_is_accepted(self) -> None:
        self.source, self.plan = self._fixtures()
        ok, reason = self._check(self._output(self.source), int(self.SOURCE_SIZE * 0.75))
        self.assertTrue(ok, reason)

    def test_unrecognized_container_is_rejected(self) -> None:
        self.source, self.plan = self._fixtures()
        bad = self._output(self.source)
        bad["container"]["recognized"] = False
        ok, reason = self._check(bad, int(self.SOURCE_SIZE * 0.98))
        self.assertFalse(ok)
        self.assertIn("not a recognized", reason)

    def test_wrong_audio_track_count_is_rejected(self) -> None:
        self.source, self.plan = self._fixtures()
        bad = self._output(self.source)
        bad["tracks"].append(json.loads(json.dumps(bad["tracks"][1])))
        ok, reason = self._check(bad, int(self.SOURCE_SIZE * 0.98))
        self.assertFalse(ok)
        self.assertIn("exactly 1 audio track", reason)

    def test_attachment_count_change_is_rejected(self) -> None:
        self.source, self.plan = self._fixtures()
        bad = self._output(self.source)
        bad["attachments"] = [{"id": 1}]
        ok, reason = self._check(bad, int(self.SOURCE_SIZE * 0.98))
        self.assertFalse(ok)
        self.assertIn("attachment count", reason)

    def test_chapter_count_change_is_rejected(self) -> None:
        self.source, self.plan = self._fixtures()
        bad = self._output(self.source)
        # mkvmerge -J reports chapter *atoms*, each carrying num_entries.
        bad["chapters"] = [{"num_entries": 12}]
        ok, reason = self._check(bad, int(self.SOURCE_SIZE * 0.98))
        self.assertFalse(ok)
        self.assertIn("chapter count", reason)

    def test_duration_growing_is_rejected(self) -> None:
        self.source, self.plan = self._fixtures()
        bad = self._output(self.source)
        bad["container"]["properties"]["duration"] = 20_000_000_000
        ok, reason = self._check(bad, int(self.SOURCE_SIZE * 0.98))
        self.assertFalse(ok)
        self.assertIn("duration grew", reason)

    def test_duration_collapse_without_frame_counts_is_rejected(self) -> None:
        """A remux that silently lost 90% of the runtime must not be accepted."""
        source, self.plan = self._fixtures()
        # Drop the source frame count so the guard cannot be satisfied by
        # frame statistics and must fall back to the conservative floor.
        for track in source["tracks"]:
            track["properties"].pop("tag_number_of_frames", None)
        self.plan = tc.build_verification_plan(
            source, source["tracks"][1], [], self.SOURCE_SIZE)
        bad = self._output(source)
        bad["container"]["properties"]["duration"] = 1_000_000_000
        ok, reason = self._check(bad, int(self.SOURCE_SIZE * 0.98))
        self.assertFalse(ok)
        self.assertIn("duration shrank too much", reason)

    def test_small_legitimate_duration_shrink_is_tolerated(self) -> None:
        """Dropping padded commentary legitimately shortens the container."""
        source, self.plan = self._fixtures()
        for track in source["tracks"]:
            track["properties"].pop("tag_number_of_frames", None)
        self.plan = tc.build_verification_plan(
            source, source["tracks"][1], [], self.SOURCE_SIZE)
        ok_info = self._output(source)
        ok_info["container"]["properties"]["duration"] = 9_500_000_000
        ok, reason = self._check(ok_info, int(self.SOURCE_SIZE * 0.98))
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
