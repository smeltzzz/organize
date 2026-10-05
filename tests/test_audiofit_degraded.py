"""The audio standardizer when the machine, the probe or the encode says no.

``audio_standardizer.py`` is the one tool that runs a real encoder, so its
failure paths are the ones that decide whether a movie survives: a transcode
that times out, a probe that answers with garbage, a volume with no room for the
staging file, a source that changed while the encode ran, and a verification
probe that cannot run at all. Every one of them has to end the same way - the
original untouched, the staging file gone, and a verdict in the report that says
what happened.

Also here:

* the degraded policy used when the sibling cleaner cannot be imported. It is
  deliberately *less* clever, and the direction matters: nothing is commentary,
  nothing is a titled dub, and an unknown language stays ``und``. A fallback
  that guessed would drop tracks.
* ``verify_output``, which is the only thing standing between an ffmpeg exit
  code of 0 and a movie being replaced. ffmpeg's command line is never trusted
  as the definition of success; the probe of the result is.
* ``validate_config``, whose every rule exists because getting it wrong means
  this tool reading its own report as a movie.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from test_audio_standardizer import (
    EAC3_DONE,
    TRUEHD_ONLY,
    WINDOWS,
    ChainFixture,
    _payload,
)

import audio_standardizer as aus
from organizekit.core import KIND_AUDIOFIT

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class TempFixture(unittest.TestCase):
    """A library, an output directory, and a config pointed at both."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="audiofit_degraded_")
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        self.library = self.root / "Movies"
        self.library.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()
        self._cfg = aus.CFG
        self._log_file = aus.log.file
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        aus.CFG = self._cfg
        aus.log.file = self._log_file

    def config(self, **kwargs: object) -> aus.Config:
        settings: dict[str, object] = {
            "source_dir": self.library,
            "report_file": self.out / "audiofit_report.txt",
            "log_file": self.out / "audiofit.log",
            "ffprobe": "ffprobe",
            "ffmpeg": "ffmpeg",
            "timeout": 5.0,
            "transcode_timeout": 5.0,
            "state_db": self.out / "state.db",
        }
        settings.update(kwargs)
        cfg = aus.Config(**settings)  # type: ignore[arg-type]
        aus.CFG = cfg
        return cfg

    def movie(self, name: str = "TrueHD Film (2001)", payload: dict | None = None,
              size: int = 2 * 1024 * 1024) -> Path:
        import fake_ffprobe as fakeff

        path = self.library / name / f"{name}.mkv"
        fakeff.write_movie(path, payload if payload is not None else TRUEHD_ONLY, size=size)
        return path


class BinaryDiscoveryTests(TempFixture):
    def test_an_explicit_binary_that_is_not_on_path_is_used(self) -> None:
        """``--ffprobe /opt/ffmpeg/bin/ffprobe`` is the documented workaround."""
        binary = self.root / "ffprobe"
        binary.write_bytes(b"#!/bin/sh\n")
        with mock.patch.object(aus.shutil, "which", lambda name: None):
            self.assertEqual(aus.find_ffprobe(str(binary)), str(binary))

    def test_a_binary_beside_the_tools_is_found(self) -> None:
        """The single-file build and a portable install keep ffmpeg next to them.

        ``find_binary`` appends the host's executable suffix to the bundled-tools
        candidate, so the stand-in has to carry it too or the search is being
        asked about a file it was never going to look at.
        """
        binary = self.root / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        binary.write_bytes(b"#!/bin/sh\n")
        with mock.patch.object(aus.shutil, "which", lambda name: None), \
                mock.patch.object(aus, "tools_home", lambda: self.root):
            self.assertEqual(aus.find_ffmpeg(), str(binary))

    def test_no_binary_anywhere_is_none_not_an_exception(self) -> None:
        with mock.patch.object(aus.shutil, "which", lambda name: None), \
                mock.patch.object(aus, "tools_home", lambda: self.root):
            self.assertIsNone(aus.find_ffprobe())

    def test_an_explicit_path_that_is_only_the_name_is_ignored(self) -> None:
        """``--ffprobe ffprobe`` means "look it up", not "a file called ffprobe"."""
        with mock.patch.object(aus.shutil, "which", lambda name: "/usr/bin/ffprobe"):
            self.assertEqual(aus.find_ffprobe("ffprobe"), "/usr/bin/ffprobe")

    def test_a_binary_that_cannot_be_run_does_not_work(self) -> None:
        with mock.patch.object(aus.subprocess, "run", side_effect=OSError("no such file")):
            self.assertFalse(aus.binary_works("/nope/ffprobe"))

    def test_a_binary_that_hangs_is_a_subprocess_error_not_a_crash(self) -> None:
        with mock.patch.object(aus.subprocess, "run",
                               side_effect=subprocess.SubprocessError("timed out")):
            self.assertFalse(aus.binary_works("ffprobe"))

    def test_a_binary_that_answers_with_a_failure_code_does_not_work(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe", "-version"], 1)
        with mock.patch.object(aus.subprocess, "run", lambda *a, **k: proc):
            self.assertFalse(aus.binary_works("ffprobe"))


class DegradedPolicyTests(TempFixture):
    """The fallback used when the sibling cleaner cannot be imported."""

    def helpers(self, *, sibling: bool) -> tuple:
        with mock.patch.dict(sys.modules, {"mkv_track_cleaner": None if not sibling else
                                           sys.modules["mkv_track_cleaner"]}):
            return aus._cleaner_helpers()

    def test_with_the_sibling_available_the_cleaners_own_policy_is_used(self) -> None:
        import mkv_track_cleaner as tc

        commentary, dub, native, token = self.helpers(sibling=True)
        self.assertIs(commentary, tc.is_commentary_track)
        self.assertIs(dub, tc.is_named_dub_track)
        self.assertIs(native, tc.native_audio_language)
        self.assertIs(token, tc.audio_language_token)

    def test_without_the_sibling_nothing_is_commentary_and_nothing_is_a_dub(self) -> None:
        """The degraded direction is "keep the track", never "drop the track".

        A stand-alone copy of this tool that started calling tracks commentary
        would delete audio on the strength of a guess it has no data for. So the
        fallback answers False for both questions and the ranking falls back to
        the codec tier - which ``test_properties.py`` pins separately.
        """
        commentary, dub, _native, _token = self.helpers(sibling=False)
        track = {"type": "audio", "codec": "TrueHD",
                 "properties": {"track_name": "Director Commentary", "language": "eng"}}
        self.assertFalse(commentary(track, True))
        self.assertFalse(dub(track))

    def test_without_the_sibling_the_native_language_is_the_first_tracks_own(self) -> None:
        """No file markers to read, so the answer comes from the tracks themselves."""
        _commentary, _dub, native, token = self.helpers(sibling=False)
        candidates = [
            {"properties": {"language": "jpn"}},
            {"properties": {"language": "eng"}},
        ]
        self.assertEqual(native(candidates), "jpn")
        self.assertEqual(token(candidates[1]), "eng")

    def test_without_the_sibling_an_unlabelled_track_stays_und(self) -> None:
        _commentary, _dub, native, token = self.helpers(sibling=False)
        self.assertEqual(native([{"properties": {}}]), "und")
        self.assertEqual(token({}), "und")

    def test_the_degraded_helpers_still_produce_a_plan(self) -> None:
        """A stand-alone copy plans rather than refuses."""
        cfg = self.config()
        with mock.patch.dict(sys.modules, {"mkv_track_cleaner": None}):
            verdict = aus.plan_for_payload(str(self.library / "film.mkv"), TRUEHD_ONLY, cfg)
        self.assertIn(verdict.status, (aus.STATUS_PLANNED, aus.STATUS_TRANSCODED,
                                       aus.STATUS_NATIVE, aus.STATUS_REVIEW,
                                       aus.STATUS_DTS, aus.STATUS_PCM))


class IsDefaultAudioTests(TempFixture):
    def test_a_stream_without_a_disposition_block_is_not_default(self) -> None:
        """A probe payload from an older ffprobe may not carry the field at all."""
        self.assertFalse(aus.is_default_audio({"codec_name": "eac3"}))
        self.assertFalse(aus.is_default_audio({"disposition": "not-a-mapping"}))

    def test_the_container_default_flag_is_read_from_the_disposition(self) -> None:
        self.assertTrue(aus.is_default_audio({"disposition": {"default": 1}}))
        self.assertFalse(aus.is_default_audio({"disposition": {"default": 0}}))


class VerificationTests(TempFixture):
    """``verify_output``: the gate between an ffmpeg exit code and a publish."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.config()
        self.produced = self.library / "Film (2001)" / "Film (2001).mkv"
        self.verdict = aus.plan_for_payload(str(self.produced), TRUEHD_ONLY, self.cfg,
                                            size_bytes=2 * 1024 * 1024)

    def _verify(self, new_payload: object, *, raises: Exception | None = None) -> tuple:
        def probe(binary: str, path: Path, cfg: object) -> object:
            if raises is not None:
                raise raises
            return new_payload

        with mock.patch.object(aus, "run_ffprobe", probe):
            return aus.verify_output(self.produced, self.verdict, self.cfg, TRUEHD_ONLY)

    def test_a_probe_that_cannot_run_refuses_the_publish(self) -> None:
        """Fail closed: a verification that could not run did not pass.

        Without this an unrunnable ffprobe would be indistinguishable from a
        good one, and the tool would replace a movie it never inspected.
        """
        ok, reason = self._verify(None, raises=RuntimeError("ffprobe failed: no such file"))
        self.assertFalse(ok)
        self.assertIn("verification ffprobe failed", reason)

    def test_a_video_stream_that_changed_refuses_the_publish(self) -> None:
        """The video is copied, never re-encoded; a different video is a different movie."""
        reencoded = _payload({"codec_name": "truehd", "channels": 8},
                             {"codec_name": "eac3", "channels": 6})
        reencoded["streams"][0] = {"index": 0, "codec_type": "video", "codec_name": "h264"}
        ok, reason = self._verify(reencoded)
        self.assertFalse(ok)
        self.assertIn("video stream set changed", reason)

    def test_a_payload_that_cannot_be_read_refuses_the_publish(self) -> None:
        """A duration of "unknown" or a missing format block is not a pass."""
        appended = _payload({"codec_name": "truehd", "channels": 8},
                            {"codec_name": self.verdict.target.codec,
                             "channels": self.verdict.target.channels,
                             "sample_rate": self.verdict.target.sample_rate,
                             "disposition": {"default": 1}})
        appended["streams"][1]["disposition"] = {"default": 0}
        appended["format"] = {"duration": "not-a-number"}
        ok, reason = self._verify(appended)
        self.assertFalse(ok)
        self.assertIn("verification could not read probes", reason)

    def test_a_correctly_appended_track_is_accepted(self) -> None:
        appended = _payload({"codec_name": "truehd", "channels": 8},
                            {"codec_name": self.verdict.target.codec,
                             "channels": self.verdict.target.channels,
                             "sample_rate": self.verdict.target.sample_rate,
                             "disposition": {"default": 1}})
        appended["streams"][1]["disposition"] = {"default": 0}
        ok, reason = self._verify(appended)
        self.assertTrue(ok, reason)


class HardlinkTests(TempFixture):
    def test_a_count_the_filesystem_will_not_give_is_read_as_one(self) -> None:
        self.assertEqual(aus.hardlink_count(self.root / "absent.mkv"), 1)

    def test_a_seeding_release_is_counted(self) -> None:
        movie = self.movie()
        os.link(movie, self.root / "seed-copy.mkv")
        self.assertEqual(aus.hardlink_count(movie), 2)


class TranscodeFailureTests(TempFixture):
    """Every way an encode can fail, and the state of the library afterwards."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.config()
        self.movie_path = self.movie()
        self.before = self.movie_path.read_bytes()
        self.verdict = aus.plan_for_payload(str(self.movie_path), TRUEHD_ONLY, self.cfg,
                                            size_bytes=len(self.before))

    def _staging_files(self) -> list[Path]:
        return sorted(self.movie_path.parent.glob("*.audiofit-*.tmp.mkv"))

    def test_a_volume_without_room_for_the_staging_file_is_refused_before_encoding(self) -> None:
        """The encode writes a whole second copy of the movie beside the first.

        Starting one on a nearly full volume produces a truncated file and an
        ENOSPC halfway through, so the space is checked first and the answer is
        a report row, not a partial movie.
        """
        usage = os.terminal_size if False else None
        with mock.patch.object(aus.shutil, "disk_usage",
                               lambda path: mock.Mock(free=1024)):
            result = aus.transcode_movie(self.verdict, TRUEHD_ONLY, self.cfg)
        self.assertEqual(result.status, aus.STATUS_ERROR)
        self.assertEqual(result.category, aus.CATEGORY_LABELS[aus.STATUS_ERROR])
        self.assertIn("not enough free space beside the movie", result.error)
        self.assertEqual(self.movie_path.read_bytes(), self.before)
        self.assertEqual(self._staging_files(), [])
        self.assertIsNone(usage)

    def test_an_encode_that_times_out_is_an_error_and_leaves_nothing_behind(self) -> None:
        """A hung ffmpeg on a network share is a real state, and it has an exit."""
        with mock.patch.object(aus.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["ffmpeg"], 5.0)):
            result = aus.transcode_movie(self.verdict, TRUEHD_ONLY, self.cfg)
        self.assertEqual(result.status, aus.STATUS_ERROR)
        self.assertIn("ffmpeg timed out after 5s", result.error)
        self.assertEqual(self.movie_path.read_bytes(), self.before)
        self.assertEqual(self._staging_files(), [])

    def test_an_encoder_that_cannot_be_launched_is_an_error(self) -> None:
        with mock.patch.object(aus.subprocess, "run",
                               side_effect=OSError("ffmpeg vanished")):
            result = aus.transcode_movie(self.verdict, TRUEHD_ONLY, self.cfg)
        self.assertEqual(result.status, aus.STATUS_ERROR)
        self.assertIn("ffmpeg vanished", result.error)
        self.assertEqual(self.movie_path.read_bytes(), self.before)

    def test_a_publish_the_volume_refuses_leaves_the_original(self) -> None:
        with mock.patch.object(aus.subprocess, "run", self._successful_encode), \
                mock.patch.object(aus.os, "replace", side_effect=OSError("read-only library")):
            result = aus.transcode_movie(self.verdict, TRUEHD_ONLY, self.cfg)
        self.assertEqual(result.status, aus.STATUS_ERROR)
        self.assertEqual(self.movie_path.read_bytes(), self.before, "the original survived")
        self.assertEqual(self._staging_files(), [], "and the staging file is gone")

    def _successful_encode(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        output = Path(cmd[-1])
        output.write_bytes(b"the encoded movie")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def test_an_encode_that_produces_nothing_is_a_failure(self) -> None:
        """ffmpeg can exit 0 and write nothing; the file is the evidence."""
        with mock.patch.object(aus.subprocess, "run", lambda cmd, **kw:
                               subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")):
            result = aus.transcode_movie(self.verdict, TRUEHD_ONLY, self.cfg)
        self.assertEqual(result.status, aus.STATUS_ERROR)
        self.assertEqual(self.movie_path.read_bytes(), self.before)

    def test_a_hardlink_count_the_filesystem_refuses_does_not_stop_a_deferral_check(self) -> None:
        cfg = self.config()
        movie = self.movie()
        with mock.patch.object(Path, "stat", side_effect=OSError("share went away")):
            verdict, payload, snapshot = aus.evaluate_file(movie, cfg, None)
        self.assertEqual(verdict.status, aus.STATUS_ERROR)
        self.assertIsNone(payload)
        self.assertIsNone(snapshot)


class ProbeFailureTests(TempFixture):
    """``run_ffprobe``: every wrong answer becomes one RuntimeError with the reason."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.config()

    def _probe(self, **kwargs: object) -> str:
        with mock.patch.object(aus.subprocess, "run", **kwargs), \
                self.assertRaises(RuntimeError) as caught:  # type: ignore[arg-type]
            aus.run_ffprobe("ffprobe", self.library / "movie.mkv", self.cfg)
        return str(caught.exception)

    def test_a_probe_that_hangs_says_how_long_it_waited(self) -> None:
        message = self._probe(side_effect=subprocess.TimeoutExpired(["ffprobe"], 5.0))
        self.assertIn("ffprobe timed out after 5s", message)

    def test_a_probe_that_cannot_be_launched_says_so(self) -> None:
        self.assertIn("failed to launch ffprobe", self._probe(side_effect=OSError("no such file")))

    def test_a_probe_that_fails_carries_its_own_stderr(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe"], 1, stdout="", stderr="Invalid data found")
        self.assertIn("ffprobe failed: Invalid data found", self._probe(return_value=proc))

    def test_a_probe_that_fails_silently_reports_its_exit_code(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe"], 3, stdout="", stderr="")
        self.assertIn("ffprobe failed: exit 3", self._probe(return_value=proc))

    def test_a_probe_that_answers_with_something_that_is_not_json_says_so(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe"], 0, stdout="not json", stderr="")
        self.assertIn("ffprobe returned invalid JSON", self._probe(return_value=proc))

    def test_a_probe_that_answers_with_a_json_array_says_so(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe"], 0, stdout="[]", stderr="")
        self.assertIn("ffprobe JSON was not an object", self._probe(return_value=proc))

    def test_a_probe_of_an_empty_answer_is_an_empty_document(self) -> None:
        proc = subprocess.CompletedProcess(["ffprobe"], 0, stdout="", stderr="")
        with mock.patch.object(aus.subprocess, "run", lambda *a, **k: proc):
            self.assertEqual(aus.run_ffprobe("ffprobe", self.library / "m.mkv", self.cfg), {})

    def test_the_payload_is_taken_from_one_stat_when_the_caller_has_one(self) -> None:
        """The cache key, the reported size and the publish check must agree.

        Three separate ``stat`` calls can straddle a change; one cannot.
        """
        movie = self.movie()
        info = movie.stat()
        proc = subprocess.CompletedProcess(["ffprobe"], 0, stdout=json.dumps(TRUEHD_ONLY), stderr="")
        with mock.patch.object(aus.subprocess, "run", lambda *a, **k: proc), \
                mock.patch.object(Path, "stat", side_effect=AssertionError("statted again")):
            payload = aus.probe_payload(movie, self.cfg, None, stat=info)
        self.assertEqual(payload, TRUEHD_ONLY)

    def test_without_a_snapshot_the_payload_stats_the_file_itself(self) -> None:
        movie = self.movie()
        proc = subprocess.CompletedProcess(["ffprobe"], 0, stdout=json.dumps(TRUEHD_ONLY), stderr="")
        with mock.patch.object(aus.subprocess, "run", lambda *a, **k: proc):
            self.assertEqual(aus.probe_payload(movie, self.cfg, None), TRUEHD_ONLY)


class EvaluateFileTests(TempFixture):
    def test_a_movie_that_cannot_be_stat_ed_is_a_report_row(self) -> None:
        """One unreadable file is a row in the report, never the end of the sweep."""
        cfg = self.config()
        with mock.patch.object(Path, "stat", side_effect=OSError("share went away")):
            verdict, payload, snapshot = aus.evaluate_file(self.library / "gone.mkv", cfg, None)
        self.assertEqual(verdict.status, aus.STATUS_ERROR)
        self.assertIn("share went away", verdict.error)
        self.assertIsNone(payload)
        self.assertIsNone(snapshot)

    def test_a_movie_whose_probe_raises_is_a_report_row_that_keeps_its_size(self) -> None:
        cfg = self.config()
        movie = self.movie()
        with mock.patch.object(aus, "run_ffprobe", mock.Mock(side_effect=RuntimeError("no probe"))):
            verdict, _payload, _snapshot = aus.evaluate_file(movie, cfg, None)
        self.assertEqual(verdict.status, aus.STATUS_ERROR)
        self.assertIn("no probe", verdict.error)
        self.assertEqual(verdict.size_bytes, movie.stat().st_size)

    def test_a_seeded_movie_is_deferred_rather_than_planned(self) -> None:
        """Hard policy: an encode would rewrite a file a torrent is still serving."""
        cfg = self.config()
        movie = self.movie()
        os.link(movie, self.root / "seed-copy.mkv")
        with mock.patch.object(aus, "run_ffprobe", lambda binary, path, config: TRUEHD_ONLY):
            verdict, _payload, snapshot = aus.evaluate_file(movie, cfg, None)
        self.assertEqual(verdict.status, aus.STATUS_DEFERRED)
        self.assertIn("hardlinks", verdict.info)
        self.assertIsNone(snapshot,
                          "a deferred movie is never planned, so there is nothing to publish")
        self.assertTrue(movie.is_file())


@unittest.skipIf(WINDOWS, "the fakes are launched through a POSIX shebang")
class ScanLevelTests(ChainFixture):
    """Whole runs of ``scan``/``main`` against the fake toolchain."""

    def test_a_movie_that_vanishes_between_the_plan_and_the_encode_is_skipped(self) -> None:
        """Probing and applying are two phases, hours apart on a big library.

        The ingest hook can replace or remove a movie in between, so the apply
        loop re-checks that the file is still there before encoding. Encoding a
        path that no longer exists would report a failure for a movie that is
        perfectly fine somewhere else.
        """
        movie = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        real_evaluate = aus.evaluate_file

        def planned_then_gone(path: Path, cfg: object, cache: object) -> object:
            outcome = real_evaluate(path, cfg, cache)  # type: ignore[arg-type]
            movie.unlink()
            return outcome

        with mock.patch.object(aus, "evaluate_file", planned_then_gone):
            code = self._run()
        self.assertEqual(code, 0, "a movie that is no longer there is not an error")
        self.assertFalse(movie.exists())
        self.assertIn("0   Errors", self.report_text())
        self.assertEqual(self.ffmpeg_invocations(), [],
                         "no encoder was started for a file that had already gone")

    def test_a_probe_that_raises_in_a_worker_is_one_error_row_and_the_rest_still_runs(self) -> None:
        """One unreadable file is a row in the report, and the run says so by exiting 1.

        Two claims, both load-bearing: the sweep does not die on the bad file -
        every other movie is still probed and planned - and the exit code is
        non-zero, because a wrapper that reads 0 as "the library is fine" would
        never look at the error section.
        """
        self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        ready = self.movie("Ready Film (2003)", EAC3_DONE)
        real_evaluate = aus.evaluate_file

        def explode_on_one(path: Path, cfg: object, cache: object) -> object:
            if "TrueHD" in path.name:
                raise RuntimeError("ffprobe died on this file")
            return real_evaluate(path, cfg, cache)  # type: ignore[arg-type]

        with mock.patch.object(aus, "evaluate_file", explode_on_one):
            code = self._run("--dry-run")
        self.assertEqual(code, 1)
        report = self.report_text()
        self.assertIn("TrueHD Film (2001).mkv", report)
        self.assertIn("ffprobe died on this file", report)
        self.assertIn("Ready Film (2003).mkv", report, "the good movie was still inspected")
        self.assertTrue(ready.is_file())

    def test_a_stale_staging_file_that_cannot_be_stat_ed_is_left_alone(self) -> None:
        """The sweep of old staging files must not die on one unreadable debris file."""
        self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        stray = self.library / ".Film.audiofit-1.tmp.mkv"
        stray.write_bytes(b"debris")
        real_stat = Path.stat

        def flaky(path: Path, **kwargs: object) -> object:
            if path == stray:
                raise OSError("share went away")
            return real_stat(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(Path, "stat", flaky):
            code = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(stray.is_file())

    def test_an_interrupt_writes_the_partial_results_and_changes_nothing(self) -> None:
        """Nothing was modified yet, so the report is still worth writing."""
        movie = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        before = movie.read_bytes()

        def interrupted(*args: object, **kwargs: object) -> object:
            raise KeyboardInterrupt

        with mock.patch.object(aus, "iter_completed", interrupted):
            code = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(movie.read_bytes(), before)
        self.assertTrue(self.report.is_file())

    def test_a_report_nobody_can_write_is_a_failed_run(self) -> None:
        """Exit 2 is what tells the caller the run's answer was not recorded."""
        self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        with mock.patch.object(aus, "atomic_write_text",
                               side_effect=OSError("read-only share")):
            code = self._run("--dry-run")
        self.assertEqual(code, 2)

    def test_a_state_cache_that_refuses_a_write_does_not_fail_the_run(self) -> None:
        from organizekit.core.state import StateStore

        self.movie("TrueHD Film (2001)", TRUEHD_ONLY)

        def exploding(self: object, *args: object, **kwargs: object) -> None:
            raise RuntimeError("the cache is locked")

        with mock.patch.object(StateStore, "record", exploding):
            code = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(self.report.is_file())

    def test_a_run_with_no_movies_still_writes_a_report(self) -> None:
        self.assertEqual(self._run(), 0)
        self.assertTrue(self.report.is_file())

    def test_a_limit_stops_after_the_first_movie(self) -> None:
        self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        self.movie("DTS HD Film (2002)", TRUEHD_ONLY)
        self.assertEqual(self._run("--dry-run", "--limit", "1"), 0)
        self.assertIn("Found 1 MKV movie file(s)", self.log.read_text(encoding="utf-8"))
        self.assertIn("Movies inspected  1", self.report_text())

    def test_the_run_lock_is_taken_and_a_second_run_is_refused_with_its_own_code(self) -> None:
        """3, not 1: a wrapper must be able to tell "busy" from "broken"."""
        self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        lock_path = aus.run_lock_path(self.library)
        with aus.ExclusiveRunLock(lock_path, 1.0, busy_message="held by {path}"):
            code = self._run("--dry-run", "--lock-timeout", "0")
        self.assertEqual(code, 3)


class ConfigurationGateTests(TempFixture):
    def assertError(self, fragment: str, **kwargs: object) -> None:
        errors = aus.validate_config(self.config(**kwargs))
        self.assertTrue(any(fragment in error for error in errors),
                        f"{fragment!r} not in {errors}")

    def test_a_workable_configuration_has_no_errors(self) -> None:
        self.assertEqual(aus.validate_config(self.config()), [])

    def test_a_source_that_is_not_a_directory_is_refused(self) -> None:
        self.assertError("--source is not an accessible directory",
                         source_dir=self.root / "nope")

    def test_a_negative_size_floor_is_refused(self) -> None:
        self.assertError("--min-size must be zero or greater", min_file_size_mb=-1.0)

    def test_zero_workers_is_refused(self) -> None:
        self.assertError("--workers must be greater than zero", workers=0)

    def test_a_zero_timeout_is_refused(self) -> None:
        self.assertError("--timeout must be greater than zero", timeout=0.0)

    def test_a_negative_lock_timeout_is_refused(self) -> None:
        self.assertError("--lock-timeout must be zero or greater", lock_timeout_seconds=-1.0)

    def test_an_unknown_wiring_is_refused(self) -> None:
        self.assertError("--wiring must be one of", wiring="bluetooth")

    def test_a_report_inside_the_library_is_refused(self) -> None:
        """The auditor would count a report folder at the library root as a movie."""
        self.assertError("Report path must be outside --source",
                         report_file=self.library / "report.txt")

    def test_a_log_inside_the_library_is_refused(self) -> None:
        self.assertError("Log path must be outside --source", log_file=self.library / "run.log")

    def test_a_cache_inside_the_library_is_refused(self) -> None:
        self.assertError("Cache path must be outside --source",
                         cache_file=self.library / "cache.json")

    def test_a_state_database_inside_the_library_is_refused(self) -> None:
        self.assertError("State cache must be outside --source",
                         state_db=self.library / "state.db")

    def test_a_log_and_a_report_that_are_the_same_file_are_refused(self) -> None:
        """One would truncate the other mid-run and both would look corrupted."""
        same = self.out / "output.txt"
        self.assertError("--log and --report must be different files",
                         log_file=same, report_file=same)


class NiceTests(TempFixture):
    def test_the_sweep_lowers_its_own_priority_when_asked(self) -> None:
        with mock.patch.object(aus.os, "nice", mock.Mock(), create=True) as nice:
            aus.maybe_be_nice()
        nice.assert_called_once_with(10)

    def test_a_platform_without_nice_is_not_a_failure(self) -> None:
        with mock.patch.object(aus.os, "nice", create=True,
                               new=mock.Mock(side_effect=AttributeError)):
            aus.maybe_be_nice()
        with mock.patch.object(aus.os, "nice", create=True,
                               new=mock.Mock(side_effect=OSError("not permitted"))):
            aus.maybe_be_nice()


@unittest.skipIf(WINDOWS, "the fakes are launched through a POSIX shebang")
class MainGateTests(ChainFixture):
    def test_a_configuration_error_is_logged_and_exits_2_before_any_probing(self) -> None:
        code = self._run("--min-size", "-1")
        self.assertEqual(code, 2)

    def test_a_missing_ffprobe_exits_2_with_the_install_hint(self) -> None:
        with mock.patch.object(aus, "find_ffprobe", lambda explicit=None: None):
            code = self._run()
        self.assertEqual(code, 2)

    def test_an_ffprobe_that_will_not_run_exits_2(self) -> None:
        with mock.patch.object(aus, "binary_works", lambda binary: False):
            code = self._run()
        self.assertEqual(code, 2)

    def test_a_missing_ffmpeg_exits_2_unless_the_run_is_plan_only(self) -> None:
        self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        with mock.patch.object(aus, "find_ffmpeg", lambda explicit=None: None):
            self.assertEqual(self._run(), 2)
            self.assertEqual(self._run("--dry-run"), 0,
                             "a plan needs only ffprobe, and the report says so")

    def test_the_nice_flag_is_honoured(self) -> None:
        self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        with mock.patch.object(aus, "maybe_be_nice", mock.Mock()) as nice:
            self.assertEqual(self._run("--nice", "--dry-run"), 0)
        nice.assert_called_once_with()

    def test_an_unexpected_failure_is_one_exit_code_not_a_traceback_for_the_caller(self) -> None:
        with mock.patch.object(aus, "scan", mock.Mock(side_effect=RuntimeError("boom"))), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = self._run()
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError", err.getvalue())

    def test_an_interrupt_is_130(self) -> None:
        with mock.patch.object(aus, "scan", mock.Mock(side_effect=KeyboardInterrupt)):
            self.assertEqual(self._run(), 130)

    def test_publishing_nothing_when_the_cache_is_off_is_not_an_error(self) -> None:
        cfg = aus.Config(source_dir=self.library, state_db=None, use_state=False)
        self.assertEqual(aus.publish_state([], cfg), 0)

    def test_a_published_verdict_is_what_organize_status_counts(self) -> None:
        from organizekit.core import open_state

        movie = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        self.assertEqual(self._run("--dry-run"), 0)
        with open_state(self.state_db, tool="tests") as store:
            verdicts = store.verdicts(KIND_AUDIOFIT)
        self.assertTrue(verdicts)
        key = next(iter(verdicts))
        self.assertEqual(Path(key[0]).name, movie.name)


class DiscoveryFaultTests(TempFixture):
    """What the sweep walks when the library is not entirely readable."""

    def test_a_movie_that_cannot_be_statted_is_skipped_not_fatal(self) -> None:
        """A leftover symlink whose target is gone must not stop the run.

        The audio sweep transcodes, so it walks the whole library; one dead link
        that raised would cost every other movie its verdict. Skipping it is safe
        because the link is not a movie the chain can play either.
        """
        good = self.movie("TrueHD Film (2001)")
        (good.parent / "Gone (2003).mkv").symlink_to(self.root / "nowhere" / "Gone (2003).mkv")
        cfg = self.config(min_file_size_mb=0)

        self.assertEqual(aus.discover_videos(self.library, cfg), [good])

    def test_a_limit_caps_the_sweep_after_the_first_n_movies(self) -> None:
        first = self.movie("Alpha (2001)")
        self.movie("Beta (2002)")
        cfg = self.config(min_file_size_mb=0, limit=1)
        self.assertEqual(aus.discover_videos(self.library, cfg), [first])

    def test_a_file_below_the_size_floor_is_not_swept(self) -> None:
        """The floor is what keeps the sweep off samples and stubs.

        Transcoding is expensive and destructive, so a file that is not a feature
        is not a candidate: it is skipped by size before anything reads its tracks.
        """
        feature = self.movie("Feature (2001)", size=8 * 1024 * 1024)
        self.movie("Sample (2001)", size=64 * 1024)
        cfg = self.config(min_file_size_mb=1)
        self.assertEqual(aus.discover_videos(self.library, cfg), [feature])


class ShippedSelfTestTests(unittest.TestCase):
    """``audio_standardizer.py --self-test`` as shipped, on a machine with no FFmpeg."""

    def pristine(self) -> object:
        import importlib.util
        import types

        spec = importlib.util.spec_from_file_location(
            "audio_standardizer_pristine", REPO / "audio_standardizer.py")
        assert spec is not None and spec.loader is not None
        module: types.ModuleType = importlib.util.module_from_spec(spec)
        # Registered while it executes: ``@dataclass`` resolves its own annotations
        # through ``sys.modules[cls.__module__]``.
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise
        return module

    def test_the_field_smoke_test_passes_without_ffmpeg_installed(self) -> None:
        """This is what an operator runs when a verdict looks wrong on their own gear.

        ``tests/selftests`` rebinds ``run_self_tests`` on the imported module, so the
        shipped body is only reachable through a second copy loaded from source.
        """
        tool = self.pristine()
        out = io.StringIO()
        with redirect_stdout(out):
            code = tool.main(["--self-test"])
        printed = out.getvalue()
        self.assertEqual(code, 0, printed)
        self.assertIn("self-test passed", printed.lower())


if __name__ == "__main__":
    unittest.main()
