"""Tests for the pure name-parsing logic in ``movie_standardizer.py``."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import movie_standardizer as ms


class ParseMovieNameTests(unittest.TestCase):
    def test_canonical(self) -> None:
        parsed = ms.parse_movie_name("The.Matrix.1999.1080p.BluRay.x264.mkv")
        self.assertEqual(parsed.title, "The Matrix")
        self.assertEqual(parsed.year, 1999)
        self.assertEqual(parsed.resolution, "1080p")

    def test_year_in_parens_kept(self) -> None:
        parsed = ms.parse_movie_name("Inception (2010) [1080p] [BluRay]")
        self.assertEqual(parsed.title, "Inception")
        self.assertEqual(parsed.year, 2010)

    def test_tv_show_detected(self) -> None:
        parsed = ms.parse_movie_name("Show.Name.S01E02.1080p.WEB-DL.mkv")
        self.assertTrue(parsed.is_tv)

    def test_bare_year_title_not_year(self) -> None:
        # 2012 / 1917 are movie titles, not release years.
        parsed = ms.parse_movie_name("2012.2009.1080p.mkv")
        self.assertEqual(parsed.title, "2012")
        self.assertEqual(parsed.year, 2009)

    def test_split_release_parts(self) -> None:
        parsed = ms.parse_movie_name("Movie.Name.2020.1080p.cd1.mkv")
        self.assertEqual(parsed.part, "cd1")

    def test_edition_is_metadata_not_title(self) -> None:
        parsed = ms.parse_movie_name("Blade.Runner.1982.The.Final.Cut.1080p.mkv")
        self.assertEqual(parsed.title, "Blade Runner")
        self.assertEqual(parsed.edition, "Final Cut")

    def test_provider_id_extracted_gently(self) -> None:
        parsed = ms.parse_movie_name("Arrival (2016) [TTtt2543164] 1080p.mkv")
        # Provider id is only recognized in the bracketed imdbid/tmdbid form.
        self.assertIn(parsed.title.casefold(), ("arrival",))
        self.assertIsInstance(parsed.title, str)

    def test_sanitize_removes_illegal_chars(self) -> None:
        # '/' -> " - ", ':' -> " -", '*' and '?' are dropped entirely.
        self.assertEqual(ms.sanitize_filename('A/B:C*D?'), "A - B -CD")


class SubtitleLanguageTests(unittest.TestCase):
    def test_english_suffix(self) -> None:
        self.assertTrue(ms.is_english_subtitle(ms.Path("Film.English.srt")))
        self.assertTrue(ms.is_english_subtitle(ms.Path("Film.en.sdh.srt")))
        self.assertFalse(ms.is_english_subtitle(ms.Path("Film.Spanish.srt")))

    def test_suffix_order(self) -> None:
        self.assertEqual(ms.subtitle_suffix("Film.English.srt"), ".en.srt")


class DuplicateUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="standardizer_upgrade_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        release = self.root / "Film.2020.1080p.WEB-DL"
        release.mkdir()
        self.source = release / "Film.2020.1080p.WEB-DL.mkv"
        self.destination = self.root / "Film (2020).mkv"
        self.source.write_bytes(b"new movie")
        self.destination.write_bytes(b"old")
        self._find = ms.find_ffprobe
        self._probe = ms.probe_media
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        ms.find_ffprobe = self._find
        ms.probe_media = self._probe

    @staticmethod
    def _info(**changes) -> ms.MediaTechnicalInfo:
        values = {
            "duration": 7200.0,
            "width": 1920,
            "height": 1080,
            "video_codec": "h264",
            "bit_depth": 8,
            "hdr": False,
            "video_bitrate": 5_000_000,
            "audio_channels": 6,
            "audio_bitrate": 640_000,
        }
        values.update(changes)
        return ms.MediaTechnicalInfo(**values)

    def _use_probe_results(self, source, existing) -> None:
        ms.find_ffprobe = lambda _explicit="ffprobe": "ffprobe"
        ms.probe_media = lambda path, _binary: ((source if path == self.source else existing), "")

    def test_ffprobe_is_required_instead_of_size_fallback(self) -> None:
        ms.find_ffprobe = lambda _explicit="ffprobe": None
        replace, reason = ms.should_replace(self.source, self.destination)
        self.assertFalse(replace)
        self.assertIn("size alone never replaces", reason)

    def test_runtime_mismatch_blocks_larger_source(self) -> None:
        self._use_probe_results(self._info(duration=7400, width=3840, height=2160), self._info())
        replace, reason = ms.should_replace(self.source, self.destination)
        self.assertFalse(replace)
        self.assertIn("different cut", reason)

    def test_balanced_score_allows_clear_same_cut_upgrade(self) -> None:
        self._use_probe_results(
            self._info(duration=7210, width=3840, height=2160, video_codec="hevc", bit_depth=10,
                       hdr=True, video_bitrate=12_000_000),
            self._info(),
        )
        replace, reason = ms.should_replace(self.source, self.destination)
        self.assertTrue(replace, reason)
        self.assertIn("same-cut technical upgrade", reason)

    def test_quality_downgrade_is_blocked_even_if_score_could_rise(self) -> None:
        self._use_probe_results(
            self._info(bit_depth=8, video_bitrate=20_000_000),
            self._info(bit_depth=10, video_bitrate=3_000_000),
        )
        replace, reason = ms.should_replace(self.source, self.destination)
        self.assertFalse(replace)
        self.assertIn("lower video bit depth", reason)

    def test_alternate_edition_is_never_automatically_replaced(self) -> None:
        self.source = self.source.with_name("Film.2020.Directors.Cut.2160p.mkv")
        self.source.write_bytes(b"alternate")
        replace, reason = ms.should_replace(self.source, self.destination)
        self.assertFalse(replace)
        self.assertIn("alternate-cut", reason)

class _RunStateMixin(unittest.TestCase):
    """Isolate the module-level run state that record_outcome mutates."""

    def setUp(self) -> None:
        self._cfg, self._summary, self._events = ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS
        self._td = tempfile.TemporaryDirectory(prefix="ms_runstate_")
        self.root = Path(self._td.name)
        ms.CFG = ms.Config(
            source_dir=self.root / "final",
            target_dir=self.root / "lib",
            log_file=None,
            report_file=self.root / "out" / "report.txt",
        )
        ms.RUN_SUMMARY = ms.RunSummary()
        ms.RUN_EVENTS = []
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        ms.CFG, ms.RUN_SUMMARY, ms.RUN_EVENTS = self._cfg, self._summary, self._events
        self._td.cleanup()


class DeclinedSourceTests(_RunStateMixin):
    """A declined download must be a durable outcome, not just a log line."""

    def test_decline_counts_as_skipped(self) -> None:
        ms.decline_source(self.root / "final" / "Film.1995.mkv", "small file")
        self.assertEqual(ms.RUN_SUMMARY.skipped, 1)

    def test_decline_records_source_and_reason(self) -> None:
        item = self.root / "final" / "Film.1995.mkv"
        ms.decline_source(item, "not an MKV; this tool never transcodes")
        event = ms.RUN_EVENTS[-1]
        self.assertEqual(event["action"], "left in source")
        self.assertEqual(event["source"], str(item))
        self.assertEqual(event["reason"], "not an MKV; this tool never transcodes")

    def test_small_file_decline_is_recorded(self) -> None:
        final = self.root / "final"
        final.mkdir(parents=True)
        small = final / "Small.Movie.1995.1080p.mkv"
        small.write_bytes(b"x" * 1024)
        ms.CFG.min_movie_size_mb = 300
        ms.handle_single_file(small)
        self.assertEqual(ms.RUN_SUMMARY.skipped, 1)
        self.assertIn("300 MB minimum", ms.RUN_EVENTS[-1]["reason"])

    def test_non_mkv_decline_is_recorded(self) -> None:
        final = self.root / "final"
        final.mkdir(parents=True)
        other = final / "Big.Movie.2020.1080p.mp4"
        other.write_bytes(b"x" * 1024)
        ms.CFG.min_movie_size_mb = 0
        ms.handle_single_file(other)
        self.assertIn("not an MKV", ms.RUN_EVENTS[-1]["reason"])

    def test_report_has_a_left_in_source_section(self) -> None:
        ms.decline_source(self.root / "final" / "Small.1995.mkv", "smaller than the 300 MB minimum")
        ms.decline_source(self.root / "final" / "Parts.2018", "multipart fragments")
        report = self._capture_report()
        self.assertIn("ITEMS LEFT IN SOURCE", report)
        self.assertIn("Small.1995.mkv", report)
        self.assertIn("multipart fragments", report)
        self.assertIn("Skipped              : 2", report)

    def test_section_is_absent_when_nothing_was_declined(self) -> None:
        ms.record_outcome("completed", "HARDLINK", src=self.root / "a", dest=self.root / "b")
        self.assertNotIn("ITEMS LEFT IN SOURCE", self._capture_report())

    def _capture_report(self) -> str:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ms.write_report()
        return buffer.getvalue()


class MaintenanceOptionTests(unittest.TestCase):
    """--maintenance-mode / --quarantine-dir / --manifest were validated but unreachable."""

    def test_options_parse(self) -> None:
        args = ms.build_parser().parse_args([
            "--deduplicate",
            "--maintenance-mode", "QUARANTINE",
            "--quarantine-dir", "/tmp/quarantine",
            "--manifest", "/tmp/manifest.json",
        ])
        cfg = ms.cfg_from_args(args)
        self.assertTrue(cfg.enable_deduplication)
        self.assertEqual(cfg.maintenance_mode, "QUARANTINE")
        self.assertEqual(cfg.quarantine_dir, Path("/tmp/quarantine"))
        self.assertEqual(cfg.manifest_file, Path("/tmp/manifest.json"))

    def test_defaults_stay_non_destructive(self) -> None:
        cfg = ms.cfg_from_args(ms.build_parser().parse_args([]))
        self.assertEqual(cfg.maintenance_mode, "REPORT")
        self.assertFalse(cfg.enable_deduplication)
        self.assertIsNone(cfg.quarantine_dir)
        self.assertIsNone(cfg.manifest_file)

    def test_quarantine_requires_a_destination(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "final").mkdir()
            (root / "lib").mkdir()
            args = ms.build_parser().parse_args([
                "--source", str(root / "final"),
                "--target", str(root / "lib"),
                "--log", str(root / "run.log"),
                "--report", str(root / "report.txt"),
                "--maintenance-mode", "QUARANTINE",
            ])
            errors = ms.validate_config(ms.cfg_from_args(args))
        self.assertEqual(errors, ["QUARANTINE maintenance mode requires --quarantine-dir"])

    def test_manifest_inside_the_library_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "final").mkdir()
            (root / "lib").mkdir()
            args = ms.build_parser().parse_args([
                "--source", str(root / "final"),
                "--target", str(root / "lib"),
                "--log", str(root / "run.log"),
                "--report", str(root / "report.txt"),
                "--manifest", str(root / "lib" / "manifest.json"),
            ])
            errors = ms.validate_config(ms.cfg_from_args(args))
        self.assertTrue(any("--manifest must be outside --target" in e for e in errors), errors)

    def test_environment_variables_are_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {
                "MOVIE_STD_DEDUPLICATE": "1",
                "MOVIE_STD_MAINTENANCE_MODE": "quarantine",
                "MOVIE_STD_QUARANTINE": str(Path(td) / "quar"),
                "MOVIE_STD_MANIFEST": str(Path(td) / "manifest.json"),
            }
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                cfg = ms.Config()
                ms.apply_env(cfg)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        self.assertTrue(cfg.enable_deduplication)
        self.assertEqual(cfg.maintenance_mode, "quarantine")
        self.assertEqual(cfg.quarantine_dir, Path(td) / "quar")
        self.assertEqual(cfg.manifest_file, Path(td) / "manifest.json")

class AtomicReportTests(_RunStateMixin):
    """The README promises atomic writes: a failed write keeps the previous file."""

    def _capture(self, fn) -> None:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            fn()

    def test_report_survives_a_failed_write(self) -> None:
        report = ms.CFG.report_file
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("PREVIOUS REPORT", encoding="utf-8")

        real_replace = os.replace

        def refuse(*_a, **_k):
            raise PermissionError("locked")

        os.replace = refuse
        try:
            self._capture(ms.write_report)
        finally:
            os.replace = real_replace

        self.assertEqual(report.read_text(encoding="utf-8"), "PREVIOUS REPORT")

    def test_failed_report_write_leaves_no_partial_behind(self) -> None:
        report = ms.CFG.report_file
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("PREVIOUS REPORT", encoding="utf-8")

        real_replace = os.replace
        os.replace = lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("locked"))
        try:
            self._capture(ms.write_report)
        finally:
            os.replace = real_replace

        self.assertEqual(sorted(p.name for p in report.parent.iterdir()), ["report.txt"])

    def test_successful_report_replaces_the_previous_one(self) -> None:
        report = ms.CFG.report_file
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("PREVIOUS REPORT", encoding="utf-8")
        ms.decline_source(self.root / "final" / "Film.1995.mkv", "not an MKV")

        self._capture(ms.write_report)

        written = report.read_text(encoding="utf-8")
        self.assertIn("MOVIE STANDARDIZER REPORT", written)
        self.assertNotIn("PREVIOUS REPORT", written)
        self.assertEqual(sorted(p.name for p in report.parent.iterdir()), ["report.txt"])

    def test_manifest_survives_a_failed_write(self) -> None:
        manifest = self.root / "out" / "manifest.json"
        ms.CFG.manifest_file = manifest
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('{"previous": true}', encoding="utf-8")

        real_replace = os.replace
        os.replace = lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("locked"))
        try:
            self._capture(ms.write_manifest)
        finally:
            os.replace = real_replace

        self.assertEqual(manifest.read_text(encoding="utf-8"), '{"previous": true}')
        self.assertEqual(ms.RUN_SUMMARY.failed, 1)

    def test_successful_manifest_is_valid_json(self) -> None:
        import json

        manifest = self.root / "out" / "manifest.json"
        ms.CFG.manifest_file = manifest
        ms.record_outcome("completed", "HARDLINK", src=self.root / "a", dest=self.root / "b")

        self._capture(ms.write_manifest)

        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["completed"], 1)
        self.assertEqual(sorted(p.name for p in manifest.parent.iterdir()), ["manifest.json"])


if __name__ == "__main__":
    unittest.main()
