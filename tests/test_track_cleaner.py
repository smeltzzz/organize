"""Tests for the pure classification helpers in ``mkv_track_cleaner.py``."""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import pathlib
import tempfile
import unittest
from pathlib import Path

from reporttext import scorecard

import mkv_track_cleaner as tc
from organizekit import core

MediaProbeCache = tc.MediaProbeCache


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
        from mkv_track_cleaner import EXTERNAL_SRT_SUFFIX
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


class ProbeCacheAfterRemuxTests(unittest.TestCase):
    """A remux preserves the mtime but changes the size.

    The probe cache is keyed on ``(size, mtime)``, so the entry written from
    the pre-remux stat can never match the file that replaced it: without
    re-keying, the next run re-scans every movie the previous run just
    cleaned, which is the whole cost of a "nothing to do" maintenance pass.
    """

    SOURCE = {
        "container": {"recognized": True, "supported": True,
                      "properties": {"duration": 6_000_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "AVC/H.264/MPEG-4p10", "properties": {
                "codec_id": "V_MPEG4/ISO/AVC", "pixel_dimensions": "1920x1080",
                "display_dimensions": "1920x1080", "tag_number_of_frames": "144000",
                "flag_default": True}},
            {"id": 1, "type": "audio", "codec": "TrueHD", "properties": {
                "codec_id": "A_TRUEHD", "language": "eng", "language_ietf": "en",
                "track_name": "English TrueHD 7.1", "audio_channels": 8,
                "audio_sampling_frequency": 48000, "flag_default": True}},
            {"id": 2, "type": "audio", "codec": "AC-3", "properties": {
                "codec_id": "A_AC3", "language": "eng", "language_ietf": "en",
                "track_name": "Director Commentary", "audio_channels": 2,
                "audio_sampling_frequency": 48000, "flag_commentary": True}},
        ],
        "attachments": [], "chapters": [],
    }
    # What mkvmerge -J reports for the remuxed output: one audio track, the
    # retained one, carrying the default flag the remux sets.
    OUTPUT = {
        "container": {"recognized": True, "supported": True,
                      "properties": {"duration": 6_000_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "AVC/H.264/MPEG-4p10", "properties": {
                "codec_id": "V_MPEG4/ISO/AVC", "pixel_dimensions": "1920x1080",
                "display_dimensions": "1920x1080", "tag_number_of_frames": "144000",
                "flag_default": True}},
            {"id": 1, "type": "audio", "codec": "TrueHD", "properties": {
                "codec_id": "A_TRUEHD", "language": "eng", "language_ietf": "en",
                "track_name": "English TrueHD 7.1", "audio_channels": 8,
                "audio_sampling_frequency": 48000, "flag_default": True}},
        ],
        "attachments": [], "chapters": [],
    }

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="tc_remux_cache_")
        self.root = pathlib.Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.movie = self.root / "Film (2020).mkv"
        self.movie.write_bytes(b"x" * 4096)
        stamp = 1_600_000_000
        os.utime(self.movie, (stamp, stamp))
        self.calls: list[str] = []
        self.cache = MediaProbeCache(self.root / "cache.json",
                                     tool="mkv_track_cleaner")

        def fake_mkvmerge(cmd, on_progress=None):
            self.calls.append(" ".join(str(part) for part in cmd))
            if "-J" in cmd:
                target = pathlib.Path(cmd[cmd.index("-J") + 1])
                # The transacted temp holds the finished output; the movie
                # itself still holds the dirty source until it is swapped in.
                info = self.OUTPUT if target.name.startswith(tc.TEMP_PREFIX) else self.SOURCE
                return 0, json.dumps(info), ""
            out = pathlib.Path(cmd[cmd.index("-o") + 1])
            # A remux rewrites the container: the result is a different size.
            out.write_bytes(b"z" * 3000)
            return 0, "", ""

        self._real = tc._run_mkvmerge
        self._real_root = tc._target_root
        tc._run_mkvmerge = fake_mkvmerge
        tc._target_root = None  # skip the canonical-layout gate in this unit test
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tc._run_mkvmerge = self._real
        tc._target_root = self._real_root

    def _run(self) -> dict:
        stats = {"cleaned": [], "already_clean": [], "skipped_no_english": [],
                 "skipped_layout": [], "deferred_hardlinked": [], "errors": [],
                 "remux_without_srt": [], "total_scanned": 0,
                 "total_space_saved_bytes": 0}
        with contextlib.redirect_stdout(io.StringIO()):
            tc.process_mkv(self.movie, stats, "mkvmerge", dry_run=False,
                           log_file_path=None, probe_cache=self.cache)
        return stats

    def test_remux_is_reported_and_the_file_is_replaced(self) -> None:
        stats = self._run()
        self.assertEqual([item["name"] for item in stats["cleaned"]], ["Film (2020).mkv"])
        self.assertEqual(stats["errors"], [])
        self.assertEqual(self.movie.stat().st_size, 3000)

    def test_next_run_reuses_the_cache_instead_of_rescanning(self) -> None:
        """The pass after the remux is the one that has to be cheap."""
        first = self._run()
        self.assertEqual(len(first["cleaned"]), 1, "the first pass must do the work")
        calls_after_remux = len(self.calls)
        misses_after_remux = self.cache.misses

        second = self._run()
        self.assertEqual(len(self.calls), calls_after_remux,
                         "an unchanged, already-clean movie must not respawn mkvmerge")
        self.assertEqual(self.cache.misses, misses_after_remux, "the re-keyed entry must hit")
        self.assertEqual(second["already_clean"], ["Film (2020).mkv"])
        self.assertEqual(second["cleaned"], [], "nothing left to clean")

    def test_cache_holds_the_post_remux_size(self) -> None:
        self._run()
        key = core.path_norm(self.movie)
        entry = self.cache._entries[key]
        self.assertEqual(entry["size"], 3000)
        self.assertEqual(entry["mtime_ns"], self.movie.stat().st_mtime_ns)


class ExternalSrtRecordPathTests(unittest.TestCase):
    """A validated sidecar record must name the file it was validated from.

    ``COVERING_ENGLISH_SRT_SUFFIXES`` accepts ``<stem>.eng.sdh.srt`` when no
    canonical ``<stem>.eng.srt`` exists.  The record returned by
    ``validate_exact_external_english_srt`` has to carry that actual path:
    ``external_srt_snapshot_matches`` re-stats ``record["path"]`` before the
    atomic swap, and a stale canonical path that never existed would make an
    untouched, valid sidecar look "changed" and refuse every remux forever.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="tc_srt_record_")
        self.root = pathlib.Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.folder = self.root / "Film (2020)"
        self.folder.mkdir()
        self.movie = self.folder / "Film (2020).mkv"
        self.movie.write_bytes(b"x" * 4096)

    def test_canonical_record_path_is_the_real_file(self) -> None:
        srt = self.folder / "Film (2020).eng.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n", encoding="utf-8")
        record = tc.validate_exact_external_english_srt(self.movie)
        self.assertTrue(record.get("valid"), f"record: {record}")
        self.assertEqual(record.get("path"), str(srt))
        self.assertTrue(tc.external_srt_snapshot_matches(record))

    def test_sdh_only_record_path_is_the_real_file(self) -> None:
        srt = self.folder / "Film (2020).eng.sdh.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nEnglish SDH dialogue\n", encoding="utf-8")
        record = tc.validate_exact_external_english_srt(self.movie)
        self.assertTrue(record.get("valid"), f"record: {record}")
        self.assertEqual(
            record.get("path"), str(srt),
            "an .eng.sdh.srt sidecar must be recorded under its own name, not a nonexistent .eng.srt",
        )
        self.assertTrue(
            tc.external_srt_snapshot_matches(record),
            "an untouched .eng.sdh.srt must keep matching its snapshot",
        )
        # Content changes must still be detected through the real path.
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nEdited\n", encoding="utf-8")
        self.assertFalse(tc.external_srt_snapshot_matches(record))

    def test_live_remux_with_sdh_only_sidecar_completes(self) -> None:
        """End-to-end: the post-remux snapshot gate must accept a stable sidecar."""
        (self.folder / "Film (2020).eng.sdh.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nEnglish SDH dialogue\n", encoding="utf-8",
        )
        source = {
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
                {"id": 2, "type": "subtitles", "codec": "HDMV PGS", "properties": {
                    "codec_id": "S_HDMV/PGS", "language": "eng", "flag_default": False}},
            ],
            "attachments": [], "chapters": [],
        }
        output = json.loads(json.dumps(source))
        output["tracks"] = [t for t in output["tracks"] if t["id"] in (0, 1)]

        def fake_mkvmerge(cmd, on_progress=None):
            cmd = [str(part) for part in cmd]
            if "-J" in cmd:
                target = pathlib.Path(cmd[cmd.index("-J") + 1])
                info = output if target.name.startswith(tc.TEMP_PREFIX) else source
                return 0, json.dumps(info), ""
            out = pathlib.Path(cmd[cmd.index("-o") + 1])
            out.write_bytes(b"z" * 3000)
            return 0, "", ""

        real = tc._run_mkvmerge
        real_root = tc._target_root
        tc._run_mkvmerge = fake_mkvmerge
        tc._target_root = None
        self.addCleanup(self._restore_state, real, real_root)
        stats = {"cleaned": [], "already_clean": [], "skipped_no_english": [],
                 "skipped_layout": [], "deferred_hardlinked": [], "errors": [],
                 "remux_without_srt": [], "total_scanned": 1, "total_space_saved_bytes": 0}
        with contextlib.redirect_stdout(io.StringIO()):
            tc.process_mkv(self.movie, stats, "mkvmerge", dry_run=False, log_file_path=None)
        self.assertEqual(
            stats["errors"], [],
            "an untouched validated sidecar must never block the atomic swap",
        )
        self.assertEqual([item["name"] for item in stats["cleaned"]], ["Film (2020).mkv"])
        self.assertEqual(self.movie.stat().st_size, 3000, "the remux must have been swapped in")

    def test_broken_plain_falls_through_to_valid_sdh(self) -> None:
        """A broken .eng.srt must not hide a valid .eng.sdh.srt."""
        srt = self.folder / "Film (2020).eng.srt"
        srt.write_text("<html>not a subtitle</html>", encoding="utf-8")
        sdh = self.folder / "Film (2020).eng.sdh.srt"
        sdh.write_text("1\n00:00:00,000 --> 00:00:01,000\nEnglish SDH dialogue\n", encoding="utf-8")
        record = tc.validate_exact_external_english_srt(self.movie)
        self.assertTrue(record.get("valid"), f"record: {record}")
        self.assertEqual(record.get("path"), str(sdh))
        self.assertTrue(tc.external_srt_snapshot_matches(record))

    def test_both_covering_sidecars_broken_stays_invalid(self) -> None:
        (self.folder / "Film (2020).eng.srt").write_text("<html>bad</html>", encoding="utf-8")
        (self.folder / "Film (2020).eng.sdh.srt").write_text("also bad", encoding="utf-8")
        record = tc.validate_exact_external_english_srt(self.movie)
        self.assertFalse(record.get("valid"))
        self.assertNotEqual(record.get("reason"), "external SRT is absent")

    def _restore_state(self, real, real_root) -> None:
        tc._run_mkvmerge = real
        tc._target_root = real_root


def _audio(track_id: int, language: str = "eng", *, name: str = "", codec: str = "AAC",
           channels: int = 2, commentary: bool = False) -> dict:
    """A minimal audio track dict in the shape mkvmerge -J returns."""
    props = {
        "language": language,
        "language_ietf": language,
        "codec_id": "A_AAC" if codec == "AAC" else "A_MLP",
        "audio_channels": channels,
    }
    if name:
        props["track_name"] = name
    if commentary:
        props["flag_commentary"] = True
    return {"id": track_id, "type": "audio", "codec": codec, "properties": props}


def _sub(track_id: int, language: str = "eng", *, name: str = "") -> dict:
    props = {"language": language, "language_ietf": language}
    if name:
        props["track_name"] = name
    return {"id": track_id, "type": "subtitles", "properties": props}


def _media_info(*tracks: dict) -> dict:
    return {"container": {"recognized": True, "supported": True}, "tracks": list(tracks)}


class CleanupPlanTests(unittest.TestCase):
    """The track-retention decision is pure, so every branch is table-testable."""

    def test_single_english_audio_is_already_clean(self) -> None:
        plan, reason = tc.plan_cleanup(_media_info(_audio(1)))
        self.assertEqual(reason, "")
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.best_audio_id, 1)
        self.assertTrue(plan.is_clean)
        self.assertEqual(plan.keep_sub_ids, [])
        self.assertEqual(plan.removed_audio, [])
        self.assertFalse(plan.foreign_with_srt)

    def test_best_english_audio_and_english_subs_are_kept(self) -> None:
        info = _media_info(
            _audio(1, name="English AAC"),
            _audio(2, codec="TrueHD", channels=8, name="Atmos"),
            _audio(3, name="Director Commentary", commentary=True),
            _audio(4, language="spa", name="Spanish"),
            _sub(5),
            _sub(6, name="English SDH"),
            _sub(7, language="spa", name="Spanish"),
            _sub(8, language="eng", name="English Forced"),
        )
        plan, reason = tc.plan_cleanup(info)
        self.assertEqual(reason, "")
        assert plan is not None
        # TrueHD beats AAC, commentary and dubs are dropped.
        self.assertEqual(plan.best_audio_id, 2)
        self.assertEqual([t["id"] for t in plan.removed_audio], [1, 3, 4])
        # English subtitles survive (including SDH/forced); Spanish is dropped.
        self.assertEqual(plan.keep_sub_ids, [5, 6, 8])
        self.assertEqual([t["id"] for t in plan.removed_subs], [7])
        self.assertFalse(plan.is_clean)
        self.assertFalse(plan.foreign_with_srt)

    def test_external_srt_makes_every_embedded_subtitle_removable(self) -> None:
        srt = {"path": "/movies/Film (2020).eng.srt", "sha256": "a" * 64, "size": 100}
        plan, reason = tc.plan_cleanup(
            _media_info(_audio(1), _sub(5), _sub(6, name="English Forced")),
            external_srt=srt,
        )
        self.assertEqual(reason, "")
        assert plan is not None
        self.assertEqual(plan.keep_sub_ids, [])
        self.assertEqual([t["id"] for t in plan.removed_subs], [5, 6])
        self.assertIs(plan.external_srt, srt)
        self.assertFalse(plan.foreign_with_srt)

    def test_foreign_film_with_srt_keeps_best_non_commentary_audio(self) -> None:
        srt = {"path": "/movies/Film (2020).eng.srt", "sha256": "a" * 64, "size": 100}
        plan, reason = tc.plan_cleanup(
            _media_info(
                _audio(1, language="spa", codec="TrueHD", channels=8),
                _audio(2, language="spa"),
                _audio(3, language="und", name="Director Commentary", commentary=True),
                _sub(4, language="spa"),
            ),
            external_srt=srt,
        )
        self.assertEqual(reason, "")
        assert plan is not None
        self.assertTrue(plan.foreign_with_srt)
        self.assertEqual(plan.best_audio_id, 1)
        self.assertEqual([t["id"] for t in plan.removed_audio], [2, 3])
        # The external sidecar is the sole subtitle option.
        self.assertEqual(plan.keep_sub_ids, [])
        self.assertEqual([t["id"] for t in plan.removed_subs], [4])

    def test_foreign_film_without_srt_is_left_alone(self) -> None:
        plan, reason = tc.plan_cleanup(_media_info(_audio(1, language="spa")))
        self.assertIsNone(plan)
        self.assertIn("foreign film", reason)

    def test_all_english_audio_commentary_is_left_alone(self) -> None:
        plan, reason = tc.plan_cleanup(
            _media_info(_audio(1, name="Director Commentary", commentary=True))
        )
        self.assertIsNone(plan)
        self.assertIn("commentary", reason)

    def test_foreign_film_with_srt_but_no_usable_audio_is_left_alone(self) -> None:
        srt = {"path": "/movies/Film (2020).eng.srt", "sha256": "a" * 64, "size": 100}
        plan, reason = tc.plan_cleanup(
            _media_info(_audio(1, name="Director Commentary", commentary=True)),
            external_srt=srt,
        )
        self.assertIsNone(plan)
        self.assertIn("no non-commentary audio", reason)

    def test_untagged_audio_named_english_is_retained(self) -> None:
        plan, reason = tc.plan_cleanup(
            _media_info(_audio(1, language="und", name="English Audio"))
        )
        self.assertEqual(reason, "")
        assert plan is not None
        self.assertEqual(plan.best_audio_id, 1)

    def test_bare_untagged_audio_is_never_guessed_english(self) -> None:
        plan, reason = tc.plan_cleanup(_media_info(_audio(1, language="und")))
        self.assertIsNone(plan)
        self.assertIn("foreign film", reason)

    def test_remove_commentary_false_keeps_commentary_in_the_pool(self) -> None:
        # remove_commentary=False is the operator's explicit override: the
        # commentary track stays a candidate and can even win on quality.
        comm = _audio(2, codec="TrueHD", channels=8, name="Director Commentary", commentary=True)
        plan, reason = tc.plan_cleanup(
            _media_info(_audio(1), comm), remove_commentary=False
        )
        self.assertEqual(reason, "")
        assert plan is not None
        self.assertEqual(plan.best_audio_id, 2)
        # The weaker AAC is what goes; commentary 2 is kept and wins.
        self.assertEqual([t["id"] for t in plan.removed_audio], [1])


def _video(track_id: int, *, frames: int | None = None,
           dimensions: str = "1920x1080", name: str = "") -> dict:
    props = {"pixel_dimensions": dimensions, "display_dimensions": dimensions}
    if frames is not None:
        props["tag_number_of_frames"] = frames
    if name:
        props["track_name"] = name
    return {"id": track_id, "type": "video", "codec": "HEVC", "properties": props}


class FingerprintAndVerificationTests(unittest.TestCase):
    """The fail-closed verifier contract: wrong streams can never be swapped in."""

    def _info(self, *tracks: dict) -> dict:
        return {"container": {"recognized": True, "supported": True}, "tracks": list(tracks)}

    def test_audio_fingerprint_normalizes_flags_and_language(self) -> None:
        fp = tc.track_fingerprint(_audio(1, name="Atmos", channels=8))
        self.assertEqual(fp["type"], "audio")
        self.assertEqual(fp["codec"], "AAC")
        self.assertEqual(fp["codec_id"], "A_AAC")
        self.assertEqual(fp["language"], "eng")
        # A BCP-47 tag is materialized from the legacy ISO-639 code.
        # A legacy ISO-639 tag that is explicitly present is preserved as-is.
        self.assertEqual(fp["language_ietf"], "eng")
        self.assertEqual(fp["channels"], 8)
        self.assertIn("flag_default", fp["flags"])
        self.assertFalse(fp["flags"]["flag_default"])

    def test_video_fingerprint_includes_dimensions(self) -> None:
        fp = tc.track_fingerprint(_video(1, frames=12345, dimensions="3840x2160"))
        self.assertEqual(fp["type"], "video")
        self.assertEqual(fp["pixel_dimensions"], "3840x2160")
        self.assertEqual(fp["default_duration"], None)

    def test_flag_aliases_and_overrides(self) -> None:
        track = {"id": 3, "type": "subtitles",
                 "properties": {"language": "eng", "default_track": True, "forced_track": True}}
        fp = tc.track_fingerprint(
            track, default_override=False, forced_override=False,
        )
        self.assertFalse(fp["flags"]["flag_default"])
        self.assertFalse(fp["flags"]["flag_forced"])

    def test_aac_7_to_8_channels_is_tolerated(self) -> None:
        expected = tc.track_fingerprint(
            {"id": 1, "type": "audio", "codec": "AAC",
             "properties": {"codec_id": "A_AAC", "audio_channels": 7, "language": "eng"}},
            default_override=True,
        )
        actual = dict(expected)
        actual["channels"] = 8
        self.assertTrue(tc.retained_audio_fingerprint_matches(actual, expected))

        actual["codec_id"] = "A_MPEG/L3"
        self.assertFalse(tc.retained_audio_fingerprint_matches(actual, expected))

    def test_verification_plan_contract(self) -> None:
        info = self._info(
            _video(1, frames=100),
            _audio(2, name="Atmos", channels=8),
            _sub(3, name="English SDH"),
            {"id": 4, "type": "buttons", "properties": {}},
        )
        plan = tc.build_verification_plan(
            info, _audio(2, name="Atmos", channels=8), [_sub(3, name="English SDH")], source_size=5000,
        )
        self.assertEqual(plan["source_size"], 5000)
        self.assertEqual(plan["attachment_count"], 0)
        self.assertEqual(plan["chapter_entries"], 0)
        self.assertEqual(plan["video_frame_counts"], [100])
        self.assertEqual([t["type"] for t in plan["preserved_tracks"]["video"]], ["video"])
        self.assertEqual([t["type"] for t in plan["preserved_tracks"]["buttons"]], ["buttons"])
        self.assertGreaterEqual(len(plan["audio"]), 8)
        self.assertEqual(len(plan["subtitles"]), 1)

    def test_verify_remux_info_accepts_identical_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="verify_ok_") as td:
            out = Path(td) / "remuxed.mkv"
            out.write_bytes(b"x" * 3000)
            # The output an mkvmerge remux of this source would produce: the
            # retained audio is marked default; the subtitle keeps its flags.
            source_audio = _audio(2, name="Atmos", channels=8)
            kept_sub = _sub(3, name="English SDH")
            source = self._info(source_audio, kept_sub)
            output_audio = dict(source_audio)
            output_audio["properties"] = dict(source_audio["properties"])
            output_audio["properties"]["default_track"] = True
            output_sub = dict(kept_sub)
            output_sub["properties"] = dict(kept_sub["properties"])
            output_info = self._info(output_audio, output_sub)
            plan = tc.build_verification_plan(
                source, source_audio, [kept_sub], source_size=3000,
            )
            ok, reason = tc._verify_remux_info(out, output_info, plan)
            self.assertTrue(ok, f"identical remux rejected: {reason}")

    def test_verify_remux_info_rejects_tiny_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="verify_tiny_") as td:
            out = Path(td) / "tiny.mkv"
            out.write_bytes(b"x" * 100)
            plan = {"source_size": 3000, "audio": {}, "subtitles": [],
                    "preserved_tracks": {}, "attachment_count": 0, "chapter_entries": 0,
                    "video_frame_counts": [], "source_duration_ns": None}
            ok, reason = tc._verify_remux_info(
                out,
                {"container": {"recognized": True, "supported": True}, "tracks": []},
                plan,
            )
            self.assertFalse(ok)
            self.assertIn("tiny", reason)

    def test_verify_remux_info_rejects_missing_audio_track(self) -> None:
        with tempfile.TemporaryDirectory(prefix="verify_noaudio_") as td:
            out = Path(td) / "no-audio.mkv"
            out.write_bytes(b"x" * 3000)
            plan = {"source_size": 3000, "audio": {}, "subtitles": [],
                    "preserved_tracks": {}, "attachment_count": 0, "chapter_entries": 0,
                    "video_frame_counts": [], "source_duration_ns": None}
            ok, reason = tc._verify_remux_info(
                out,
                {"container": {"recognized": True, "supported": True}, "tracks": []},
                plan,
            )
            self.assertFalse(ok)
            self.assertIn("audio track", reason)

    def test_verify_remux_info_rejects_wrong_subtitles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="verify_sub_") as td:
            out = Path(td) / "subs.mkv"
            out.write_bytes(b"x" * 3000)
            audio = _audio(1)
            audio["properties"]["default_track"] = True
            plan = tc.build_verification_plan(self._info(audio), _audio(1), [], source_size=3000)
            # Output carries an extra Spanish subtitle the plan did not retain.
            output_audio = _audio(1)
            output_audio["properties"] = dict(output_audio["properties"])
            output_audio["properties"]["default_track"] = True
            output_info = self._info(
                output_audio,
                _sub(9, language="spa", name="Español"),
            )
            ok, reason = tc._verify_remux_info(out, output_info, plan)
            self.assertFalse(ok)
            self.assertIn("subtitle", reason)

    def test_diagnostic_records_reason_and_fingerprints(self) -> None:
        source = self._info(_audio(1), _video(2))
        output = {"tracks": [_audio(1)]}
        diag = tc.build_verification_diagnostic(
            source, output, {"audio": {"x": 1}}, "retained audio fingerprint differs",
        )
        self.assertEqual(diag["reason"], "retained audio fingerprint differs")
        self.assertEqual(diag["expected_audio_fingerprint"], {"x": 1})
        self.assertEqual(len(diag["source_video_tracks"]), 1)
        self.assertIn("normalized_fingerprint", diag["output_audio_tracks"][0])


class TransactionAndCleanupTests(unittest.TestCase):
    """The crash-recovery journal: evidence is durable before any swap."""

    def test_new_transaction_paths_recover_the_journal_path(self) -> None:
        original = Path("/media/Film (2020).mkv")
        temp, journal, token = tc.new_transaction_paths(original)
        self.assertEqual(temp.parent, original.parent)
        self.assertEqual(journal.parent, original.parent)
        self.assertTrue(journal.name.startswith(tc.TRANSACTION_MARKER))
        self.assertTrue(journal.name.endswith(tc.TRANSACTION_JOURNAL_SUFFIX))
        self.assertEqual(tc._transaction_token_from_temp_name(temp.name), token)
        self.assertEqual(tc._transaction_journal_path(original.parent, token), journal)

    def test_transaction_roundtrip_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="txn_") as td:
            root = Path(td)
            movie = root / "Film (2020).mkv"
            movie.write_bytes(b"m")
            temp, journal, _token = tc.new_transaction_paths(movie)
            temp.write_bytes(b"staged")
            payload = tc.create_transaction(movie, temp, _token, movie.stat())
            tc.write_transaction(journal, payload)
            loaded = tc.read_transaction(journal)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["phase"], "remuxing")
            self.assertEqual(loaded["source_name"], movie.name)
            self.assertEqual(loaded["source_snapshot"]["size"], 1)

            tc.cleanup_transaction_artifacts(temp, journal)
            self.assertFalse(temp.exists())
            self.assertFalse(journal.exists())

    def test_read_transaction_rejects_wrong_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="txn_bad_") as td:
            journal = Path(td) / "txn.json"
            journal.write_text("{\"schema\": \"wrong\"}", encoding="utf-8")
            self.assertIsNone(tc.read_transaction(journal))
            journal.write_text("not json", encoding="utf-8")
            self.assertIsNone(tc.read_transaction(journal))

    def test_safe_replace_and_safe_delete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="replace_") as td:
            root = Path(td)
            src, dst = root / "src.mkv", root / "dst.mkv"
            src.write_bytes(b"new")
            dst.write_bytes(b"old")
            self.assertTrue(tc.safe_replace(src, dst, max_retries=1))
            self.assertEqual(dst.read_bytes(), b"new")
            self.assertFalse(src.exists())
            tc.safe_delete(dst, max_retries=1)
            self.assertFalse(dst.exists())

    def test_describe_track_includes_channels_and_name(self) -> None:
        desc = tc.describe_track(_audio(7, channels=6, name="Atmos"))
        self.assertIn("ID 7", desc)
        self.assertIn("6ch", desc)
        self.assertIn("'Atmos'", desc)
        bare = tc.describe_track(_audio(1))
        self.assertIn("[eng]", bare)


class LayoutAndCountTests(unittest.TestCase):
    def test_chapter_and_video_frame_counts(self) -> None:
        info = {"chapters": [{"num_entries": 3}, {"other": 1}], "tracks": []}
        self.assertEqual(tc._chapter_entry_count(info), 3)
        tracks = [_video(1, frames=None), _video(2, frames=100), _video(3, frames=50)]
        # None sorts below every real count (container may count only some).
        self.assertEqual(tc._video_frame_counts(tracks), [None, 50, 100])

    def test_bool_flag_and_normal_int_parsing(self) -> None:
        self.assertTrue(tc._bool_flag("1"))
        self.assertTrue(tc._bool_flag("true"))
        self.assertFalse(tc._bool_flag("0"))
        self.assertFalse(tc._bool_flag(""))
        self.assertEqual(tc._normal_int("42"), 42)
        self.assertEqual(tc._normal_int("nope"), None)
        self.assertEqual(tc._normal_int(None), None)


if __name__ == "__main__":
    unittest.main()
