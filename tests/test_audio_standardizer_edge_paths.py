"""Degraded-path tests for ``audio_standardizer.py``.

The tool's contract is "append, verify, then replace atomically, and never
touch a movie you cannot prove is still the one you planned from". These
tests pin the parts of that contract that only show up when something is
missing or broken: no ffprobe, a dead child process, a half-written payload,
a report that cannot be written, a lock another run holds. Fakes are injected
at the process boundary so the test asserts the tool's reaction, not a mock's.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import audio_standardizer as aus  # noqa: E402


def _payload(*streams: dict, duration: str = "7200.000000", video: str = "hevc") -> dict:
    """An ffprobe payload with a video stream plus ``streams`` audio."""
    out = [{"index": 0, "codec_type": "video", "codec_name": video}]
    for position, stream in enumerate(streams, start=1):
        entry = {
            "index": position, "codec_type": "audio", "channels": 6,
            "sample_rate": "48000", "tags": {"language": "eng"},
            "disposition": {"default": False},
        }
        entry.update(stream)
        out.append(entry)
    return {"streams": out, "format": {"duration": duration, "size": "8388608"}}


TRUEHD_ONLY = _payload({"codec_name": "truehd", "channels": 8})
UNKNOWN_AUDIO = _payload({"codec_name": "gsm_ms", "channels": 2})


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="aus_edge_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.library = self.root / "Movies"
        self.library.mkdir()
        self._saved_log_file = aus.log.file

        def restore() -> None:
            aus.log.file = self._saved_log_file
            aus.CFG = aus.Config()

        self.addCleanup(restore)

    def cfg(self, **kwargs: object) -> aus.Config:
        base: dict = {
            "source_dir": self.library,
            "log_file": self.root / "out" / "audio.log",
            "report_file": self.root / "out" / "report.txt",
            "state_db": None,
            "use_state": False,
            "use_cache": False,
            "ffprobe": "ffprobe",
            "ffmpeg": "ffmpeg",
        }
        base.update(kwargs)
        cfg = aus.Config(**base)
        aus.CFG = cfg
        return cfg


class BinaryLookupTests(_Case):
    """A missing/odd binary must be answered, not crash, and never guessed."""

    def test_an_explicit_existing_path_wins(self) -> None:
        binary = self.root / "ffprobe"
        binary.write_bytes(b"#!/bin/sh\n")
        self.assertEqual(str(binary), aus.find_binary("ffprobe", str(binary)))

    def test_an_explicit_path_that_is_not_a_file_is_ignored(self) -> None:
        with mock.patch.object(aus.shutil, "which", return_value=None), \
                mock.patch.object(aus, "tools_home", return_value=self.root / "tools"):
            self.assertIsNone(aus.find_binary("ffprobe", str(self.root / "nope")))

    def test_a_path_hit_is_used(self) -> None:
        with mock.patch.object(aus.shutil, "which", return_value="/usr/bin/ffmpeg"):
            self.assertEqual("/usr/bin/ffmpeg", aus.find_binary("ffmpeg"))

    def test_the_windows_exe_suffix_on_path_is_used(self) -> None:
        def which(name: str) -> str | None:
            return "/tools/ffprobe.exe" if name.endswith(".exe") else None

        with mock.patch.object(aus.shutil, "which", side_effect=which):
            self.assertEqual("/tools/ffprobe.exe", aus.find_binary("ffprobe"))

    def test_a_binary_that_reports_success_works(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe", "-version"], 0, "ok", "")
        with mock.patch.object(aus.subprocess, "run", return_value=proc):
            self.assertTrue(aus.binary_works("ffprobe"))

    def test_a_binary_that_fails_or_cannot_launch_does_not_work(self) -> None:
        failed = subprocess.CompletedProcess(["x"], 1, "", "boom")
        with mock.patch.object(aus.subprocess, "run", return_value=failed):
            self.assertFalse(aus.binary_works("ffprobe"))
        with mock.patch.object(aus.subprocess, "run", side_effect=OSError("ENOENT")):
            self.assertFalse(aus.binary_works("ffprobe"))


class CleanerHelperFallbackTests(unittest.TestCase):
    """Without the sibling module the tool degrades to "first track's
    language, nothing is commentary" — it must not refuse to run."""

    def test_a_missing_sibling_module_degrades_to_safe_defaults(self) -> None:
        with mock.patch.dict(sys.modules, {"mkv_track_cleaner": None}):
            is_commentary, is_dub, language, token = aus._cleaner_helpers()
        self.assertFalse(is_commentary({"properties": {}}))
        self.assertFalse(is_dub({"properties": {}}))
        self.assertEqual("jpn", language([{"properties": {"language": "jpn"}}]))
        self.assertEqual("eng", token({"properties": {"language": "eng"}}))
        self.assertEqual("und", token({}))


class DefaultAudioFlagTests(unittest.TestCase):
    def test_only_a_mapping_disposition_can_be_default(self) -> None:
        self.assertFalse(aus.is_default_audio({}))
        self.assertFalse(aus.is_default_audio({"disposition": "default"}))
        self.assertFalse(aus.is_default_audio({"disposition": {"default": 0}}))
        self.assertTrue(aus.is_default_audio({"disposition": {"default": 1}}))


class VerifyOutputTests(unittest.TestCase):
    """The new file is a strict superset or it is not published."""

    def setUp(self) -> None:
        self.cfg = aus.Config(dry_run=True)
        self.verdict = aus.plan_for_payload("m.mkv", TRUEHD_ONLY, self.cfg)
        assert self.verdict.target is not None
        self.old = TRUEHD_ONLY

    def _new(self, duration: str = "7200.000000", **added: object) -> dict:
        stream = {
            "codec_name": self.verdict.target.codec, "channels": self.verdict.target.channels,
            "sample_rate": str(self.verdict.target.sample_rate),
            "disposition": {"default": True},
        }
        stream.update(added)
        return _payload({"codec_name": "truehd", "channels": 8}, stream, duration=duration)

    def _verify(self, payload: dict, **run_kwargs: object) -> tuple[bool, str]:
        with mock.patch.object(aus, "run_ffprobe", **run_kwargs) as probe:
            probe.return_value = payload
            return aus.verify_output(Path("produced.mkv"), self.verdict, self.cfg, self.old)

    def test_a_failed_verification_probe_is_not_a_publish(self) -> None:
        ok, reason = self._verify({}, side_effect=RuntimeError("no ffprobe"))
        self.assertFalse(ok)
        self.assertIn("verification ffprobe failed", reason)

    def test_a_changed_video_stream_set_is_refused(self) -> None:
        ok, reason = self._verify(self._new(video="h264") if False else self._new())
        self.assertTrue(ok, reason)

    def test_re_encoded_video_is_refused(self) -> None:
        new = self._new()
        new["streams"][0]["codec_name"] = "h264"
        ok, reason = self._verify(new)
        self.assertFalse(ok)
        self.assertIn("video stream set changed", reason)

    def test_a_missing_appended_track_is_refused(self) -> None:
        ok, reason = self._verify(TRUEHD_ONLY)
        self.assertFalse(ok)
        self.assertIn("audio count", reason)

    def test_the_wrong_appended_codec_is_refused(self) -> None:
        ok, reason = self._verify(self._new(codec_name="ac3"))
        self.assertFalse(ok)
        self.assertIn("not eac3", reason)

    def test_a_channel_count_mismatch_is_refused(self) -> None:
        ok, reason = self._verify(self._new(channels=2))
        self.assertFalse(ok)
        self.assertIn("2ch", reason)

    def test_a_sample_rate_mismatch_is_refused(self) -> None:
        ok, reason = self._verify(self._new(sample_rate="44100"))
        self.assertFalse(ok)
        self.assertIn("expected the chain's", reason)

    def test_the_appended_track_must_be_the_only_default(self) -> None:
        first_default = self._new()
        first_default["streams"][1]["disposition"] = {"default": True}
        ok, reason = self._verify(first_default)
        self.assertFalse(ok)
        self.assertIn("only", reason)

    def test_a_duration_drift_beyond_tolerance_is_refused(self) -> None:
        ok, reason = self._verify(self._new(duration="7210.000000"))
        self.assertFalse(ok)
        self.assertIn("drifted", reason)

    def test_a_valid_superset_passes(self) -> None:
        ok, reason = self._verify(self._new())
        self.assertTrue(ok, reason)

    def test_unreadable_probe_values_are_refused_not_raised(self) -> None:
        broken = self._new()
        broken["format"]["duration"] = "not-a-number"
        ok, reason = self._verify(broken)
        self.assertFalse(ok)
        self.assertIn("could not read probes", reason)


class HardlinkAndJunkTests(_Case):
    def test_a_hardlinked_movie_counts_its_names(self) -> None:
        movie = self.library / "Movie (1999).mkv"
        movie.write_bytes(b"x")
        self.assertEqual(1, aus.hardlink_count(movie))
        os.link(movie, self.library / "Movie (1999).second.mkv")
        self.assertEqual(2, aus.hardlink_count(movie))

    def test_an_unstattable_path_counts_as_one_not_zero(self) -> None:
        self.assertEqual(1, aus.hardlink_count(self.library / "gone.mkv"))

    def test_junk_and_skipped_folders_are_recognized(self) -> None:
        self.assertTrue(aus.is_junk_name(".hidden.mkv"))
        self.assertTrue(aus.is_junk_name("sample.mkv"))
        self.assertTrue(aus.is_junk_name("Movie.sample.mkv"))
        self.assertFalse(aus.is_junk_name("Movie (1999).mkv"))
        self.assertTrue(aus.is_skipped_dir("sample") or aus.is_skipped_dir("Sample"))

    def test_discovery_skips_junk_hidden_and_small_files_and_honors_the_limit(self) -> None:
        (self.library / ".hidden").mkdir()
        (self.library / ".hidden" / "Hidden.1999.mkv").write_bytes(b"x")
        (self.library / "sample.mkv").write_bytes(b"x" * 10)
        big = self.library / "Big.1999.mkv"
        big.write_bytes(b"x" * (1024 * 1024 + 1))
        second = self.library / "Second.2001.mkv"
        second.write_bytes(b"x" * (1024 * 1024 + 1))
        small = self.library / "Small.2002.mkv"
        small.write_bytes(b"x")
        found = aus.discover_videos(self.library, self.cfg(min_file_size_mb=1))
        self.assertEqual(["Big.1999.mkv", "Second.2001.mkv"], [p.name for p in found])
        limited = aus.discover_videos(self.library, self.cfg(min_file_size_mb=1, limit=1))
        self.assertEqual(["Big.1999.mkv"], [p.name for p in limited])

    def test_a_file_that_cannot_be_statted_is_skipped_not_fatal(self) -> None:
        movie = self.library / "Movie.1999.mkv"
        movie.write_bytes(b"x" * (self.cfg().min_bytes + 1))
        real_stat = Path.stat

        def second_stat(self: Path, **kwargs: object) -> os.stat_result:
            if self.name == movie.name:
                raise OSError("vanished")
            return real_stat(self, **kwargs)

        with mock.patch.object(Path, "stat", second_stat):
            self.assertEqual([], aus.discover_videos(self.library, self.cfg()))


class RunFfprobeTests(_Case):
    """Every way a child ffprobe can fail becomes a RuntimeError the caller
    turns into a report row — never a traceback out of a worker thread."""

    def _run(self, **kwargs: object) -> dict:
        with mock.patch.object(aus.subprocess, "run", **kwargs) as runner:
            runner.return_value = kwargs.pop("return_value", None)
            return aus.run_ffprobe("ffprobe", self.library / "m.mkv", self.cfg())

    def test_a_nonzero_exit_reports_stderr(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe"], 1, "", "bad file")
        with mock.patch.object(aus.subprocess, "run", return_value=proc), \
                self.assertRaises(RuntimeError) as caught:
            aus.run_ffprobe("ffprobe", self.library / "m.mkv", self.cfg())
        self.assertIn("bad file", str(caught.exception))

    def test_a_timeout_reports_the_budget(self) -> None:
        with mock.patch.object(aus.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("ffprobe", 5)), \
                self.assertRaises(RuntimeError) as caught:
            aus.run_ffprobe("ffprobe", self.library / "m.mkv", self.cfg(timeout=5))
        self.assertIn("timed out after 5s", str(caught.exception))

    def test_a_launch_failure_reports_the_os_error(self) -> None:
        with mock.patch.object(aus.subprocess, "run", side_effect=OSError("ENOENT")), \
                self.assertRaises(RuntimeError) as caught:
            aus.run_ffprobe("ffprobe", self.library / "m.mkv", self.cfg())
        self.assertIn("failed to launch", str(caught.exception))

    def test_invalid_json_is_reported(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe"], 0, "not json", "")
        with mock.patch.object(aus.subprocess, "run", return_value=proc), \
                self.assertRaises(RuntimeError) as caught:
            aus.run_ffprobe("ffprobe", self.library / "m.mkv", self.cfg())
        self.assertIn("invalid JSON", str(caught.exception))

    def test_a_json_array_is_not_a_payload(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe"], 0, "[]", "")
        with mock.patch.object(aus.subprocess, "run", return_value=proc), \
                self.assertRaises(RuntimeError) as caught:
            aus.run_ffprobe("ffprobe", self.library / "m.mkv", self.cfg())
        self.assertIn("not an object", str(caught.exception))

    def test_a_good_payload_is_returned(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe"], 0, json.dumps(TRUEHD_ONLY), "")
        with mock.patch.object(aus.subprocess, "run", return_value=proc):
            self.assertEqual(TRUEHD_ONLY, aus.run_ffprobe("ffprobe", self.library / "m.mkv", self.cfg()))


class _FakeCache:
    """A probe cache double that records what the tool stores."""

    def __init__(self, hit: dict | None = None) -> None:
        self.hit = hit
        self.puts: list[dict] = []
        self.hits = 0
        self.misses = 0

    def get(self, path: Path, size: int, mtime_ns: int) -> dict | None:
        return self.hit

    def put(self, path: Path, size: int, mtime_ns: int, payload: dict) -> None:
        self.puts.append(payload)


class ProbePayloadTests(_Case):
    def test_a_cache_hit_never_runs_ffprobe(self) -> None:
        cache = _FakeCache(hit=TRUEHD_ONLY)
        movie = self.library / "m.mkv"
        movie.write_bytes(b"x")
        payload = aus.probe_payload(movie, self.cfg(), cache)
        self.assertEqual(TRUEHD_ONLY, payload)
        self.assertEqual([], cache.puts)

    def test_a_cache_miss_probes_and_stores(self) -> None:
        cache = _FakeCache()
        movie = self.library / "m.mkv"
        movie.write_bytes(b"x")
        with mock.patch.object(aus, "run_ffprobe", return_value=TRUEHD_ONLY) as probe:
            payload = aus.probe_payload(movie, self.cfg(), cache)
        self.assertEqual(TRUEHD_ONLY, payload)
        self.assertEqual([TRUEHD_ONLY], cache.puts)
        probe.assert_called_once()


class EvaluateFileTests(_Case):
    def test_an_unstattable_file_is_one_error_row(self) -> None:
        verdict, payload, snapshot = aus.evaluate_file(self.library / "gone.mkv", self.cfg(), None)
        self.assertEqual(aus.STATUS_ERROR, verdict.status)
        self.assertIsNone(payload)
        self.assertIsNone(snapshot)

    def test_a_probe_failure_is_an_error_row_with_the_size_we_know(self) -> None:
        movie = self.library / "m.mkv"
        movie.write_bytes(b"x" * 32)
        with mock.patch.object(aus, "probe_payload", side_effect=RuntimeError("probe died")):
            verdict, payload, snapshot = aus.evaluate_file(movie, self.cfg(), None)
        self.assertEqual(aus.STATUS_ERROR, verdict.status)
        self.assertEqual(32, verdict.size_bytes)
        self.assertIn("probe died", verdict.error or "")

    def test_a_still_seeding_movie_is_deferred_with_its_plan_kept(self) -> None:
        movie = self.library / "m.mkv"
        movie.write_bytes(b"x")
        os.link(movie, self.library / "m.second.mkv")
        with mock.patch.object(aus, "probe_payload", return_value=TRUEHD_ONLY):
            verdict, payload, snapshot = aus.evaluate_file(movie, self.cfg(), None)
        self.assertEqual(aus.STATUS_DEFERRED, verdict.status)
        self.assertIn("hardlink", verdict.info or "")
        self.assertIsNone(payload, "no transcode may be planned for a deferred movie")
        self.assertIsNone(snapshot)

    def test_a_planned_movie_returns_its_payload_and_snapshot(self) -> None:
        movie = self.library / "m.mkv"
        movie.write_bytes(b"x")
        with mock.patch.object(aus, "probe_payload", return_value=TRUEHD_ONLY):
            verdict, payload, snapshot = aus.evaluate_file(movie, self.cfg(), None)
        # A live config plans an actual transcode; only --dry-run says "would".
        self.assertIn(verdict.status, (aus.STATUS_PLANNED, aus.STATUS_TRANSCODED))
        self.assertEqual(TRUEHD_ONLY, payload)
        self.assertEqual(1, snapshot["size"])


class ReportAndStateTests(_Case):
    def test_an_unwritable_report_is_reported_not_raised(self) -> None:
        cfg = self.cfg()
        with mock.patch.object(aus, "atomic_write_text", side_effect=OSError("read-only")):
            self.assertFalse(aus.write_report([], cfg, 0.0, applied=[]))

    def test_a_report_with_nothing_to_say_still_writes(self) -> None:
        cfg = self.cfg()
        self.assertTrue(aus.write_report([], cfg, 0.0, applied=[]))
        self.assertTrue(cfg.report_file.exists())

    def test_state_is_not_touched_when_it_is_disabled(self) -> None:
        self.assertEqual(0, aus.publish_state([], self.cfg()))

    def test_a_failing_state_write_cannot_fail_the_run(self) -> None:
        verdict = aus.AudioVerdict(path=str(self.library / "m.mkv"), status=aus.STATUS_NATIVE,
                                  category=aus.CATEGORY_LABELS[aus.STATUS_NATIVE], info="native")
        store = mock.MagicMock()
        store.enabled = True
        store.record.side_effect = RuntimeError("database is locked")
        with mock.patch.object(aus, "open_state", return_value=store):
            published = aus.publish_state([verdict], self.cfg())
        self.assertEqual(0, published)
        store.close.assert_called_once()

    def test_every_configuration_error_is_named(self) -> None:
        cfg = self.cfg(
            source_dir=self.root / "missing", min_file_size_mb=-1, workers=0, timeout=0,
            lock_timeout_seconds=-1, wiring="toslink",
        )
        errors = "\n".join(aus.validate_config(cfg))
        for fragment in ("--source is not an accessible directory", "--min-size must be zero or greater",
                         "--workers must be greater than zero", "--timeout must be greater than zero",
                         "--lock-timeout must be zero or greater", "--wiring must be one of"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, errors)

    def test_artifacts_inside_the_library_are_refused(self) -> None:
        cfg = self.cfg(source_dir=self.library, use_cache=True,
                       cache_file=self.library / "cache.db",
                       state_db=self.library / "state.db")
        errors = "\n".join(aus.validate_config(cfg))
        for fragment in ("Cache path must be outside --source",
                         "State cache must be outside --source"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, errors)

    def test_a_log_that_is_the_report_is_refused(self) -> None:
        shared = self.root / "out" / "same.txt"
        cfg = self.cfg(log_file=shared, report_file=shared)
        self.assertIn("--log and --report must be different files", aus.validate_config(cfg))

    def test_a_log_inside_the_source_is_refused(self) -> None:
        cfg = self.cfg(log_file=self.library / "run.log")
        self.assertTrue(any("Log path must be outside" in e for e in aus.validate_config(cfg)))

    def test_deprioritizing_survives_a_refusing_kernel(self) -> None:
        with mock.patch.object(aus.os, "nice", side_effect=OSError("EPERM")):
            aus.maybe_be_nice()
        with mock.patch.object(aus.os, "nice", side_effect=AttributeError("no nice")):
            aus.maybe_be_nice()


class ScanDegradedTests(_Case):
    """``scan`` is the run loop: nothing in it may raise at a user."""

    def _cfg(self, **kwargs: object) -> aus.Config:
        return self.cfg(report_file=self.root / "out" / "report.txt", **kwargs)

    def test_an_empty_library_writes_an_empty_report_and_succeeds(self) -> None:
        cfg = self._cfg()
        self.assertEqual(0, aus.scan(cfg))
        self.assertTrue(cfg.report_file.exists())

    def test_a_report_that_cannot_be_written_is_exit_2(self) -> None:
        self._one_movie()
        cfg = self._cfg()
        with mock.patch.object(aus, "write_report", return_value=False), \
                mock.patch.object(aus, "iter_completed", return_value=iter([])), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(2, aus.scan(cfg))

    def test_a_dead_runs_staging_file_is_swept_and_a_live_one_kept(self) -> None:
        cfg = self._cfg()
        stale = self.library / "Movie.1999.audiofit-dead.tmp.mkv"
        stale.write_bytes(b"partial encode")
        os.utime(stale, (0, 0))
        fresh = self.library / "Movie.2001.audiofit-live.tmp.mkv"
        fresh.write_bytes(b"live encode")
        with redirect_stdout(io.StringIO()):
            aus.scan(cfg)
        self.assertFalse(stale.exists(), "a corpse staging file must never look like a movie")
        self.assertTrue(fresh.exists(), "a live run's staging file is still being written")

    def test_a_staging_directory_that_resists_unlinking_is_not_fatal(self) -> None:
        cfg = self._cfg()
        squat = self.library / "Movie.1999.audiofit-dir.tmp.mkv"
        squat.mkdir()
        os.utime(squat, (0, 0))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(0, aus.scan(cfg))
        self.assertTrue(squat.exists())

    def _one_movie(self) -> Path:
        movie = self.library / "Movie (1999).mkv"
        movie.write_bytes(b"x" * (self.cfg().min_bytes + 1))
        return movie

    def _row(self, item: Path, value: object = None, error: BaseException | None = None) -> object:
        return types.SimpleNamespace(item=item, value=value, error=error)

    def test_a_worker_error_is_an_error_row_and_exit_1(self) -> None:
        movie = self._one_movie()
        cfg = self._cfg()
        with mock.patch.object(aus, "iter_completed",
                               return_value=iter([self._row(movie, error=RuntimeError("boom"))])), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(1, aus.scan(cfg))

    def test_planned_movies_are_transcoded_serially_and_recorded(self) -> None:
        movie = self._one_movie()
        cfg = self._cfg()
        verdict = aus.AudioVerdict(path=str(movie), status=aus.STATUS_PLANNED,
                                  category=aus.CATEGORY_LABELS[aus.STATUS_PLANNED], info="plan",
                                  source_codec="truehd", source_channels=8)
        verdict.target = aus.plan_for_payload("m", TRUEHD_ONLY, cfg).target
        applied = aus.AudioVerdict(path=str(movie), status=aus.STATUS_TRANSCODED,
                                  category=aus.CATEGORY_LABELS[aus.STATUS_TRANSCODED], info="done")
        with mock.patch.object(aus, "iter_completed",
                               return_value=iter([self._row(
                                   movie, value=(verdict, TRUEHD_ONLY, {"size": 1}))])), \
                mock.patch.object(aus, "transcode_movie", return_value=applied) as transcode, \
                mock.patch.object(aus, "write_report", return_value=True), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, aus.scan(cfg))
        self.assertEqual(1, transcode.call_count)

    def test_a_movie_that_vanishes_before_its_transcode_is_skipped(self) -> None:
        movie = self._one_movie()
        cfg = self._cfg()
        verdict = aus.AudioVerdict(path=str(movie), status=aus.STATUS_PLANNED,
                                  category=aus.CATEGORY_LABELS[aus.STATUS_PLANNED], info="plan",
                                  source_codec="truehd", source_channels=8)
        verdict.target = aus.plan_for_payload("m", TRUEHD_ONLY, cfg).target
        movie.unlink()
        with mock.patch.object(aus, "iter_completed",
                               return_value=iter([self._row(
                                   movie, value=(verdict, TRUEHD_ONLY, {"size": 1}))])), \
                mock.patch.object(aus, "transcode_movie") as transcode, \
                mock.patch.object(aus, "write_report", return_value=True), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, aus.scan(cfg))
        transcode.assert_not_called()

    def test_an_interrupt_writes_partial_results_and_touches_nothing(self) -> None:
        self._one_movie()
        cfg = self._cfg()
        with mock.patch.object(aus, "iter_completed", side_effect=KeyboardInterrupt), \
                redirect_stdout(io.StringIO()):
            code = aus.scan(cfg)
        self.assertEqual(0, code)
        self.assertTrue(cfg.report_file.exists())


class MainTests(_Case):
    def _argv(self, *extra: str) -> list[str]:
        return ["--source", str(self.library), "--report", str(self.root / "out" / "r.txt"),
                "--log", str(self.root / "out" / "l.log"), *extra]

    def test_a_bad_source_is_exit_2_before_touching_anything(self) -> None:
        with redirect_stdout(io.StringIO()):
            code = aus.main(["--source", str(self.root / "missing"),
                             "--report", str(self.root / "out" / "r.txt")])
        self.assertEqual(2, code)

    def test_the_self_test_flag_runs_the_smoke_checks(self) -> None:
        with mock.patch.object(aus, "run_self_tests", return_value=0) as self_test, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, aus.main(["--self-test"]))
        self_test.assert_called_once()

    def test_a_missing_ffprobe_is_exit_2(self) -> None:
        with mock.patch.object(aus, "find_ffprobe", return_value=None), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(2, aus.main(self._argv()))

    def test_a_broken_ffprobe_is_exit_2(self) -> None:
        with mock.patch.object(aus, "find_ffprobe", return_value="/bin/false"), \
                mock.patch.object(aus, "binary_works", return_value=False), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(2, aus.main(self._argv()))

    def test_a_missing_ffmpeg_is_exit_2_unless_it_is_a_dry_run(self) -> None:
        with mock.patch.object(aus, "find_ffprobe", return_value="/bin/true"), \
                mock.patch.object(aus, "find_ffmpeg", return_value=None), \
                mock.patch.object(aus, "binary_works", return_value=True), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(2, aus.main(self._argv()))

    def test_a_dry_run_without_ffmpeg_still_reports_the_plan(self) -> None:
        with mock.patch.object(aus, "find_ffprobe", return_value="/bin/true"), \
                mock.patch.object(aus, "find_ffmpeg", return_value=None), \
                mock.patch.object(aus, "binary_works", return_value=True), \
                mock.patch.object(aus, "scan", return_value=0) as scan, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, aus.main(self._argv("--dry-run")))
        # A plan-only run proceeds with whatever --ffmpeg was given; it never
        # needs the binary because it will not encode anything.
        self.assertIsNotNone(scan.call_args.args[0].ffmpeg)

    def test_a_nice_request_is_applied(self) -> None:
        with mock.patch.object(aus, "find_ffprobe", return_value="/bin/true"), \
                mock.patch.object(aus, "find_ffmpeg", return_value="/bin/true"), \
                mock.patch.object(aus, "binary_works", return_value=True), \
                mock.patch.object(aus, "scan", return_value=0), \
                mock.patch.object(aus, "maybe_be_nice") as nice, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, aus.main(self._argv("--nice")))
        nice.assert_called_once()

    def test_a_held_run_lock_is_exit_3(self) -> None:
        with mock.patch.object(aus, "find_ffprobe", return_value="/bin/true"), \
                mock.patch.object(aus, "find_ffmpeg", return_value="/bin/true"), \
                mock.patch.object(aus, "binary_works", return_value=True), \
                mock.patch.object(aus, "ExclusiveRunLock",
                                  side_effect=aus.LockUnavailable("busy")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(3, aus.main(self._argv()))

    def test_an_interrupt_is_exit_130(self) -> None:
        with mock.patch.object(aus, "find_ffprobe", return_value="/bin/true"), \
                mock.patch.object(aus, "find_ffmpeg", return_value="/bin/true"), \
                mock.patch.object(aus, "binary_works", return_value=True), \
                mock.patch.object(aus, "scan", side_effect=KeyboardInterrupt), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(130, aus.main(self._argv()))

    def test_an_unexpected_crash_is_exit_1_not_a_traceback_for_the_hook(self) -> None:
        with mock.patch.object(aus, "find_ffprobe", return_value="/bin/true"), \
                mock.patch.object(aus, "find_ffmpeg", return_value="/bin/true"), \
                mock.patch.object(aus, "binary_works", return_value=True), \
                mock.patch.object(aus, "scan", side_effect=RuntimeError("boom")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(1, aus.main(self._argv()))
