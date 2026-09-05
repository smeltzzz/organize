"""Tests for ``sync_subtitles.py``, the ffsubsync subtitle-timing step.

Everything here is offline: ``run_ffsubsync`` is replaced with a
deterministic fake so no real binary is ever launched, and every file the
tool writes goes to a temp directory. The property that matters most is
fail-closed behaviour: an untrusted or failed sync must leave the original
sidecar byte-identical.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sync_subtitles as ss
from organizekit import core

# A valid SRT the shared contract accepts.
GOOD_SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello.\n\n2\n00:00:04,000 --> 00:00:05,000\nWorld.\n"
# The "corrected" version the fake ffsubsync writes (all cues shifted).
SHIFTED_SRT = "1\n00:00:05,000 --> 00:00:06,000\nHello.\n\n2\n00:00:08,000 --> 00:00:09,000\nWorld.\n"


# The tool publishes its verdicts to the shared state cache, whose default
# location is the developer's real state directory. A test suite must never
# write there, so the whole module is pointed at a temp database - which also
# means the write-through path is exercised rather than switched off.
_STATE_DIR: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _STATE_DIR
    _STATE_DIR = tempfile.TemporaryDirectory(prefix="sync_state_")
    os.environ["ORGANIZE_STATE_DB"] = str(Path(_STATE_DIR.name) / "state.db")


def tearDownModule() -> None:
    os.environ.pop("ORGANIZE_STATE_DB", None)
    if _STATE_DIR is not None:
        _STATE_DIR.cleanup()


def _cfg(tmp: Path, **overrides: object) -> ss.Config:
    base: dict = {
        "library": tmp / "lib",
        "log_file": tmp / "out" / "sync_subtitles.log",
        "report_file": tmp / "out" / "sync_subtitles_report.txt",
        "state_db": tmp / "out" / "state.db",
    }
    base.update(overrides)
    return ss.Config(**base)


class ParseTests(unittest.TestCase):
    """The measurement lines are the only trustworthy signal: ffsubsync
    exits 0 even when a sync fails, so the tool must parse them out of the
    log (rich console layout in current releases, plain lines in old ones)."""

    def test_rich_formatted_lines(self) -> None:
        text = (
            "           INFO     score: 551.000                              ffsubsync.py:255\n"
            "           INFO     offset seconds: -3.950                      ffsubsync.py:256\n"
            "           INFO     framerate scale factor: 1.000               ffsubsync.py:257\n"
        )
        parsed = ss.parse_ffsubsync_output(text)
        self.assertAlmostEqual(parsed.offset_seconds, -3.950)
        self.assertAlmostEqual(parsed.scale_factor, 1.0)
        self.assertAlmostEqual(parsed.score, 551.0)
        self.assertFalse(parsed.failed_marker)
        self.assertFalse(parsed.leaving_unmodified)

    def test_plain_lines(self) -> None:
        text = (
            "INFO:ffsubsync:score: 12.345\n"
            "INFO:ffsubsync:offset seconds: 2.5\n"
            "INFO:ffsubsync:framerate scale factor: 1.042\n"
        )
        parsed = ss.parse_ffsubsync_output(text)
        self.assertAlmostEqual(parsed.offset_seconds, 2.5)
        self.assertAlmostEqual(parsed.scale_factor, 1.042)
        self.assertAlmostEqual(parsed.score, 12.345)

    def test_no_measurements(self) -> None:
        parsed = ss.parse_ffsubsync_output("hello\nno numbers\n")
        self.assertIsNone(parsed.offset_seconds)
        self.assertIsNone(parsed.scale_factor)
        self.assertIsNone(parsed.score)
        self.assertFalse(parsed.failed_marker)

    def test_failure_marker(self) -> None:
        parsed = ss.parse_ffsubsync_output("offset seconds: 1.0\nERROR:ffsubsync:failed to sync x.srt\n")
        self.assertTrue(parsed.failed_marker)

    def test_quality_gate_marker(self) -> None:
        parsed = ss.parse_ffsubsync_output("WARNING: low-quality alignment; leaving subtitles unmodified\n")
        self.assertTrue(parsed.leaving_unmodified)

    def test_last_measurement_wins(self) -> None:
        parsed = ss.parse_ffsubsync_output("offset seconds: 1.0\noffset seconds: 2.0\n")
        self.assertAlmostEqual(parsed.offset_seconds, 2.0)

    def test_error_tail_keeps_the_last_lines(self) -> None:
        tail = ss.error_tail_from("line one\nline two\nline three\nline four\nline five\n")
        self.assertIn("line five", tail)
        self.assertIn("line two", tail)
        self.assertNotIn("line one", tail)
        self.assertEqual(ss.error_tail_from(""), "")


class FeatureFlagTests(unittest.TestCase):
    def test_current_release_flags(self) -> None:
        feats = ss.parse_feature_flags("usage: ffs [--strict] [--skip-sync-on-low-quality] [-o SRTOUT]")
        self.assertTrue(feats.strict)
        self.assertTrue(feats.quality_gate)
        self.assertTrue(feats.help_ok)

    def test_old_release_flags(self) -> None:
        feats = ss.parse_feature_flags("usage: ffs [-o SRTOUT] [--encoding ENCODING]")
        self.assertFalse(feats.strict)
        self.assertFalse(feats.quality_gate)

    def test_no_help_means_no_flags(self) -> None:
        feats = ss.FfsubsyncFeatures()
        self.assertFalse(feats.strict)
        self.assertFalse(feats.quality_gate)
        self.assertFalse(feats.help_ok)


class CommandTests(unittest.TestCase):
    def test_plain_argv_order(self) -> None:
        cmd = ss.build_ffsubsync_command("ffs", Path("v.mkv"), Path("s.srt"), Path("st.srt"))
        self.assertEqual(
            cmd,
            ["ffs", "v.mkv", "-i", "s.srt", "-o", "st.srt", "--output-encoding", "utf-8"],
        )

    def test_optional_flags_only_when_supported(self) -> None:
        cmd = ss.build_ffsubsync_command(
            "ffs", Path("v.mkv"), Path("s.srt"), Path("st.srt"),
            ss.FfsubsyncFeatures(strict=True, quality_gate=True, help_ok=True),
        )
        self.assertEqual(cmd[-2:], ["--strict", "--skip-sync-on-low-quality"])

    def test_no_optional_flags_when_unsupported(self) -> None:
        cmd = ss.build_ffsubsync_command(
            "ffs", Path("v.mkv"), Path("s.srt"), Path("st.srt"), ss.FfsubsyncFeatures(),
        )
        self.assertNotIn("--strict", cmd)
        self.assertNotIn("--skip-sync-on-low-quality", cmd)


class DecisionTableTests(unittest.TestCase):
    """classify_outcome is the fail-closed heart of the tool: every
    untrusted or failed path must NOT return STATUS_SYNCED."""

    def setUp(self) -> None:
        self.cfg = _cfg(Path("/tmp/wherever"))

    def _parsed(self, **kw: object) -> ss.ParsedSync:
        return ss.ParsedSync(**kw)  # type: ignore[arg-type]

    def test_nonzero_exit_is_failure_even_with_output(self) -> None:
        status, _ = ss.classify_outcome(1, True, True, "", self._parsed(
            score=551.0, offset_seconds=-3.95, scale_factor=1.0), self.cfg)
        self.assertEqual(status, ss.STATUS_FAILED)

    def test_missing_output_is_failure(self) -> None:
        status, _ = ss.classify_outcome(0, False, False, "", self._parsed(
            score=551.0, offset_seconds=-3.95, scale_factor=1.0), self.cfg)
        self.assertEqual(status, ss.STATUS_FAILED)

    def test_invalid_output_is_failure(self) -> None:
        status, detail = ss.classify_outcome(0, True, False, "no valid SRT cue", self._parsed(
            score=551.0, offset_seconds=-3.95, scale_factor=1.0), self.cfg)
        self.assertEqual(status, ss.STATUS_FAILED)
        self.assertIn("no valid SRT cue", detail)

    def test_ffsubsync_quality_gate_refusal_is_review(self) -> None:
        status, _ = ss.classify_outcome(0, True, True, "", self._parsed(
            score=5.0, offset_seconds=1.0, scale_factor=1.0, leaving_unmodified=True), self.cfg)
        self.assertEqual(status, ss.STATUS_REVIEW)

    def test_unmeasured_offset_is_review_not_replace(self) -> None:
        status, _ = ss.classify_outcome(0, True, True, "", ss.ParsedSync(), self.cfg)
        self.assertEqual(status, ss.STATUS_REVIEW)

    def test_failed_marker_is_review_not_replace(self) -> None:
        status, _ = ss.classify_outcome(0, True, True, "", self._parsed(
            score=5.0, offset_seconds=1.0, scale_factor=1.0, failed_marker=True), self.cfg)
        self.assertEqual(status, ss.STATUS_REVIEW)

    def test_negative_score_is_review(self) -> None:
        status, detail = ss.classify_outcome(0, True, True, "", self._parsed(
            score=-12.0, offset_seconds=1.0, scale_factor=1.0), self.cfg)
        self.assertEqual(status, ss.STATUS_REVIEW)
        self.assertIn("anti-correlated", detail)

    def test_offset_beyond_trust_window_is_review(self) -> None:
        status, detail = ss.classify_outcome(0, True, True, "", self._parsed(
            score=10.0, offset_seconds=45.0, scale_factor=1.0), self.cfg)
        self.assertEqual(status, ss.STATUS_REVIEW)
        self.assertIn("trust window", detail)

    def test_offset_at_trust_window_edge_is_applied(self) -> None:
        status, _ = ss.classify_outcome(0, True, True, "", self._parsed(
            score=10.0, offset_seconds=30.0, scale_factor=1.0), self.cfg)
        self.assertEqual(status, ss.STATUS_SYNCED)

    def test_tiny_offset_is_in_sync(self) -> None:
        status, detail = ss.classify_outcome(0, True, True, "", self._parsed(
            score=10.0, offset_seconds=0.02, scale_factor=1.0), self.cfg)
        self.assertEqual(status, ss.STATUS_IN_SYNC)
        self.assertIn("already aligned", detail)

    def test_real_framerate_correction_is_applied_despite_tiny_offset(self) -> None:
        status, _ = ss.classify_outcome(0, True, True, "", self._parsed(
            score=10.0, offset_seconds=0.02, scale_factor=1.041667), self.cfg)
        self.assertEqual(status, ss.STATUS_SYNCED)

    def test_trusted_drift_is_applied(self) -> None:
        status, _ = ss.classify_outcome(0, True, True, "", self._parsed(
            score=551.0, offset_seconds=-3.95, scale_factor=1.0), self.cfg)
        self.assertEqual(status, ss.STATUS_SYNCED)


class StagingNameTests(unittest.TestCase):
    def test_staging_is_junk_to_the_other_tools(self) -> None:
        import uuid

        name = f"{ss.STAGING_PREFIX}{os.getpid()}.{uuid.uuid4().hex}.srt"
        self.assertTrue(name.startswith("."), "dot-prefixed so is_junk_filename excludes it")
        self.assertTrue(name.endswith(".srt"))
        self.assertTrue(ss.is_junk_filename(name))


class PickVideoTests(unittest.TestCase):
    def test_language_tagged_sidecar_pairs_with_video(self) -> None:
        names = ["Film (2000).mkv", "Film (2000).eng.srt"]
        self.assertEqual(ss.pick_video_for("Film (2000).eng.srt", names), "Film (2000).mkv")

    def test_plain_sidecar_pairs_with_same_stem(self) -> None:
        names = ["Film (2000).mkv", "Film (2000).srt"]
        self.assertEqual(ss.pick_video_for("Film (2000).srt", names), "Film (2000).mkv")

    def test_exact_stem_beats_prefixed(self) -> None:
        names = ["Film (2000).mkv", "Film (2000).eng.mkv", "Film (2000).eng.srt"]
        self.assertEqual(ss.pick_video_for("Film (2000).eng.srt", names), "Film (2000).eng.mkv")

    def test_mkv_preferred_among_equal_matches(self) -> None:
        names = ["Film (2000).mp4", "Film (2000).mkv", "Film (2000).eng.srt"]
        self.assertEqual(ss.pick_video_for("Film (2000).eng.srt", names), "Film (2000).mkv")

    def test_no_match(self) -> None:
        names = ["Other (2001).mkv", "Film (2000).eng.srt"]
        self.assertIsNone(ss.pick_video_for("Film (2000).eng.srt", names))

    def test_subtitle_never_pairs_with_a_subtitle(self) -> None:
        names = ["Film (2000).eng.srt", "Film (2000).fra.srt"]
        self.assertIsNone(ss.pick_video_for("Film (2000).eng.srt", names))


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sync_discover_")
        self.tmp = Path(self._td.name)
        self.lib = self.tmp / "lib"
        self.lib.mkdir()

    def tearDown(self) -> None:
        self._td.cleanup()

    def _write(self, rel: str, content: str | bytes) -> Path:
        path = self.lib / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def test_canonical_pairing_and_counts(self) -> None:
        self._write("Film (2000)/Film (2000).mkv", b"video")
        self._write("Film (2000)/Film (2000).eng.srt", GOOD_SRT)
        self._write("Orphan (2001)/Orphan (2001).eng.srt", GOOD_SRT)
        jobs, skips, video_count = ss.discover_jobs(self.lib)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].srt.name, "Film (2000).eng.srt")
        self.assertEqual(jobs[0].video.name, "Film (2000).mkv")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0].srt.name, "Orphan (2001).eng.srt")
        self.assertEqual(video_count, 1)

    def test_junk_and_hidden_files_are_ignored(self) -> None:
        self._write("Film (2000)/Film (2000).mkv", b"video")
        self._write("Film (2000)/Film (2000).eng.srt", GOOD_SRT)
        self._write("Film (2000)/.hidden.srt", GOOD_SRT)
        self._write("Film (2000)/Film (2000).eng.srt.tmp", GOOD_SRT)
        self._write("Film (2000)/thumbs.db", b"x")
        jobs, skips, video_count = ss.discover_jobs(self.lib)
        self.assertEqual(len(jobs), 1, "hidden and .tmp sidecars must not be jobs")
        self.assertEqual(len(skips), 0, "junk sidecars must not even be skips")

    def test_dry_run_and_limit_do_not_change_discovery(self) -> None:
        self._write("A (2000)/A (2000).mkv", b"v")
        self._write("A (2000)/A (2000).eng.srt", GOOD_SRT)
        jobs, _skips, _videos = ss.discover_jobs(self.lib)
        self.assertEqual(len(jobs), 1)


class FakeFfsubsync:
    """A deterministic stand-in for one ffsubsync invocation.

    Mirrors the real tool's contract: writes the (shifted) subtitle to the
    ``-o`` path, logs the three measurements to "stderr", and exits 0 -
    even in the failure cases, exactly like ffsubsync does.
    """

    def __init__(
        self,
        offset: float = -4.0,
        scale: float = 1.0,
        score: float = 551.0,
        rc: int = 0,
        write_output: bool = True,
        output_content: str | None = None,
        leaving_unmodified: bool = False,
        failed_marker: bool = False,
    ) -> None:
        self.offset = offset
        self.scale = scale
        self.score = score
        self.rc = rc
        self.write_output = write_output
        self.output_content = output_content
        self.leaving_unmodified = leaving_unmodified
        self.failed_marker = failed_marker
        self.calls: list[list[str]] = []

    def __call__(self, cfg: ss.Config, command: os.PathLike[str] | str | list[str]) -> tuple[int, str, str]:
        self.calls.append(list(map(str, command)))
        lines: list[str] = []
        if self.leaving_unmodified:
            lines.append("WARNING: low-quality alignment; leaving subtitles unmodified")
        elif self.failed_marker:
            lines.append("ERROR: ffsubsync failed to sync the input")
        else:
            lines.append(f"INFO: score: {self.score:.3f}")
            lines.append(f"INFO: offset seconds: {self.offset:.3f}")
            lines.append(f"INFO: framerate scale factor: {self.scale:.3f}")
        if self.write_output:
            index = command.index("-o")
            Path(str(command[index + 1])).write_text(
                self.output_content if self.output_content is not None else SHIFTED_SRT,
                encoding="utf-8",
            )
        return self.rc, "", "\n".join(lines)


class EndToEndTests(unittest.TestCase):
    """Full main() runs with run_ffsubsync faked: real discovery, real
    staging, real os.replace, real report and log files."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sync_e2e_")
        # Canonical form: main() resolves the library path (macOS:
        # /var/folders -> /private/var/folders; Windows: 8.3 temp dirs),
        # so path-equality assertions need the resolved form here too.
        self.tmp = Path(self._td.name).resolve()
        self.lib = self.tmp / "lib"
        self.movie_dir = self.lib / "Film (2000)"
        self.movie_dir.mkdir(parents=True)
        self.mkv = self.movie_dir / "Film (2000).mkv"
        self.mkv.write_bytes(b"fake video")
        self.srt = self.movie_dir / "Film (2000).eng.srt"
        self.srt.write_text(GOOD_SRT, encoding="utf-8")
        self.log = self.tmp / "out" / "sync_subtitles.log"
        self.report = self.tmp / "out" / "sync_subtitles_report.txt"

    def tearDown(self) -> None:
        self._td.cleanup()

    def _run(self, fake: FakeFfsubsync, *extra: str) -> int:
        # Hermetic: pretend ffmpeg is on PATH regardless of the host image
        # (CI's macos runner ships without it). Every other lookup keeps
        # its real answer.
        real_which = shutil.which

        def which(name: str) -> str | None:
            if name == "ffmpeg":
                return "/usr/bin/ffmpeg"
            return real_which(name)

        with mock.patch.object(ss, "run_ffsubsync", fake), \
                mock.patch.object(ss, "find_ffsubsync", lambda explicit=None: "fake-ffsubsync"), \
                mock.patch.object(ss, "ffsubsync_version", lambda binary: "ffsubsync 9.9.9"), \
                mock.patch.object(ss, "detect_ffsubsync_features",
                                  lambda binary: ss.FfsubsyncFeatures(True, True, True)), \
                mock.patch("shutil.which", side_effect=which):
            return ss.main([
                "--source", str(self.lib),
                "--log", str(self.log),
                "--report", str(self.report),
                *extra,
            ])

    def test_trusted_drift_replaces_the_sidecar(self) -> None:
        fake = FakeFfsubsync(offset=-4.0)
        code = self._run(fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SHIFTED_SRT)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("SUBTITLES SYNCED (TIMING CORRECTED)", report)
        self.assertTrue(self.log.is_file(), "the run log must exist")

    def test_invocation_contract(self) -> None:
        fake = FakeFfsubsync(offset=-4.0)
        self._run(fake)
        cmd = fake.calls[0]
        self.assertEqual(cmd[0], "fake-ffsubsync")
        self.assertEqual(cmd[1], str(self.mkv))
        self.assertEqual(cmd[2:5], ["-i", str(self.srt), "-o"])
        staging = Path(cmd[5])
        self.assertEqual(staging.parent, self.srt.parent, "staging sits beside the sidecar")
        self.assertTrue(staging.name.startswith(ss.STAGING_PREFIX))
        self.assertTrue(staging.name.endswith(".srt"))
        self.assertEqual(cmd[-2:], ["--strict", "--skip-sync-on-low-quality"])
        self.assertFalse(staging.exists(), "staging is cleaned up")

    def test_tiny_offset_leaves_original_byte_identical(self) -> None:
        before = self.srt.read_bytes()
        fake = FakeFfsubsync(offset=0.02)
        code = self._run(fake)
        self.assertEqual(code, 0)
        self.assertEqual(self.srt.read_bytes(), before)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("ALREADY IN SYNC", report)

    def test_large_offset_is_review_and_original_untouched(self) -> None:
        before = self.srt.read_bytes()
        fake = FakeFfsubsync(offset=45.0)
        code = self._run(fake)
        self.assertEqual(code, 0, "a review alone does not fail the run")
        self.assertEqual(self.srt.read_bytes(), before)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("SUBTITLES HELD FOR REVIEW", report)

    def test_large_offset_fails_the_run_with_flag(self) -> None:
        fake = FakeFfsubsync(offset=45.0)
        code = self._run(fake, "--fail-on-review")
        self.assertEqual(code, 3)

    def test_failure_keeps_original_and_fails_run(self) -> None:
        before = self.srt.read_bytes()
        fake = FakeFfsubsync(rc=1, write_output=False)
        code = self._run(fake)
        self.assertEqual(code, 1)
        self.assertEqual(self.srt.read_bytes(), before)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("FAILED SYNC ATTEMPTS", report)

    def test_invalid_ffsubsync_output_is_failure(self) -> None:
        before = self.srt.read_bytes()
        fake = FakeFfsubsync(offset=-4.0, output_content="<html>not srt</html>\n")
        code = self._run(fake)
        self.assertEqual(code, 1)
        self.assertEqual(self.srt.read_bytes(), before)

    def test_ffsubsync_quality_gate_is_review(self) -> None:
        before = self.srt.read_bytes()
        fake = FakeFfsubsync(leaving_unmodified=True)
        code = self._run(fake)
        self.assertEqual(code, 0)
        self.assertEqual(self.srt.read_bytes(), before)
        self.assertIn("SUBTITLES HELD FOR REVIEW", self.report.read_text(encoding="utf-8"))

    def test_rejected_refetch_restores_entry_time_original(self) -> None:
        before = self.srt.read_bytes()
        candidate = GOOD_SRT.replace("Hello.", "Replacement candidate.")
        fake = FakeFfsubsync(offset=45.0)

        def refetch(_video: Path, dest: Path, _excluded: list[str], _log: Path | None):
            dest.write_text(candidate, encoding="utf-8")
            return True, "123", "candidate release"

        with mock.patch.object(ss, "run_ffsubsync", fake), \
                mock.patch.object(ss, "_refetch_sidecar", side_effect=refetch), \
                mock.patch.object(ss, "MAX_SYNC_REFETCHES", 1):
            result = ss.sync_one(
                ss.Job(self.srt, self.mkv), _cfg(self.tmp), "fake-ffsubsync",
                ss.FfsubsyncFeatures(True, True, True),
            )

        self.assertEqual(result.status, ss.STATUS_REVIEW)
        self.assertEqual(self.srt.read_bytes(), before)
        self.assertEqual(len(fake.calls), 2)

    def test_failed_refetch_restores_original_even_if_fetcher_removed_it(self) -> None:
        before = self.srt.read_bytes()
        fake = FakeFfsubsync(offset=45.0)

        def destructive_failure(_video: Path, dest: Path, _excluded: list[str], _log: Path | None):
            dest.unlink()
            return False, "456", "HTTP 406 quota exceeded"

        with mock.patch.object(ss, "run_ffsubsync", fake), \
                mock.patch.object(ss, "_refetch_sidecar", side_effect=destructive_failure):
            result = ss.sync_one(
                ss.Job(self.srt, self.mkv), _cfg(self.tmp), "fake-ffsubsync",
                ss.FfsubsyncFeatures(True, True, True),
            )

        self.assertEqual(result.status, ss.STATUS_REVIEW)
        self.assertEqual(self.srt.read_bytes(), before)
        self.assertIn("HTTP 406", result.detail)

    def test_tenth_replacement_download_can_be_the_one_that_syncs(self) -> None:
        """The per-movie retry budget is inclusive: candidate ten is tested."""
        before = self.srt.read_bytes()
        rejected = FakeFfsubsync(offset=45.0)
        accepted = FakeFfsubsync(offset=-4.0)
        sync_calls = 0
        download_ids: list[str] = []

        def run(cfg: ss.Config, command: list[str]):
            nonlocal sync_calls
            sync_calls += 1
            # The entry-time sidecar and the first nine downloads are bad;
            # the tenth replacement is accepted and atomically activated.
            fake = accepted if sync_calls == 11 else rejected
            return fake(cfg, command)

        def refetch(_video: Path, dest: Path, excluded: list[str], _log: Path | None):
            candidate_id = str(len(download_ids) + 1)
            self.assertEqual(excluded, download_ids)
            download_ids.append(candidate_id)
            dest.write_text(GOOD_SRT.replace("Hello.", f"Candidate {candidate_id}."), encoding="utf-8")
            return True, candidate_id, f"candidate release {candidate_id}"

        with mock.patch.object(ss, "run_ffsubsync", side_effect=run), \
                mock.patch.object(ss, "_refetch_sidecar", side_effect=refetch):
            result = ss.sync_one(
                ss.Job(self.srt, self.mkv), _cfg(self.tmp), "fake-ffsubsync",
                ss.FfsubsyncFeatures(True, True, True),
            )

        self.assertEqual(ss.MAX_SYNC_REFETCHES, 10)
        self.assertEqual(download_ids, [str(number) for number in range(1, 11)])
        self.assertEqual(sync_calls, 11)
        self.assertEqual(result.status, ss.STATUS_SYNCED)
        self.assertNotEqual(self.srt.read_bytes(), before)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SHIFTED_SRT)

    def test_dry_run_never_launches_ffsubsync(self) -> None:
        before = self.srt.read_bytes()
        fake = FakeFfsubsync(offset=-4.0)
        code = self._run(fake, "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [], "dry run must not launch ffsubsync")
        self.assertEqual(self.srt.read_bytes(), before)
        self.assertIn("DRY-RUN PREVIEW", self.report.read_text(encoding="utf-8"))

    def test_limit_stops_after_n(self) -> None:
        other_dir = self.lib / "Other (2001)"
        other_dir.mkdir()
        (other_dir / "Other (2001).mkv").write_bytes(b"v")
        (other_dir / "Other (2001).eng.srt").write_text(GOOD_SRT, encoding="utf-8")
        fake = FakeFfsubsync(offset=-4.0)
        code = self._run(fake, "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("not yet checked", self.report.read_text(encoding="utf-8"))

    def test_missing_library_is_config_error(self) -> None:
        with mock.patch.object(ss, "find_ffsubsync", lambda explicit=None: "fake-ffsubsync"):
            code = ss.main([
                "--source", str(self.tmp / "nope"),
                "--log", str(self.log),
                "--report", str(self.report),
            ])
        self.assertEqual(code, 2)

    def test_report_inside_library_is_config_error(self) -> None:
        with mock.patch.object(ss, "find_ffsubsync", lambda explicit=None: "fake-ffsubsync"):
            code = ss.main([
                "--source", str(self.lib),
                "--log", str(self.log),
                "--report", str(self.lib / "report.txt"),
            ])
        self.assertEqual(code, 2)

    def test_live_run_without_ffsubsync_is_exit_2(self) -> None:
        with mock.patch.object(ss, "find_ffsubsync", lambda explicit=None: None):
            code = ss.main([
                "--source", str(self.lib),
                "--log", str(self.log),
                "--report", str(self.report),
            ])
        self.assertEqual(code, 2)
        self.assertFalse(self.report.is_file(), "no report without a live run")

    def test_dry_run_without_ffsubsync_still_works(self) -> None:
        with mock.patch.object(ss, "find_ffsubsync", lambda explicit=None: None):
            code = ss.main([
                "--source", str(self.lib),
                "--log", str(self.log),
                "--report", str(self.report),
                "--dry-run",
            ])
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN PREVIEW", self.report.read_text(encoding="utf-8"))

    def test_explicit_missing_ffsubsync_path_is_exit_2(self) -> None:
        with mock.patch.object(ss, "find_ffsubsync", lambda explicit=None: None):
            code = ss.main([
                "--source", str(self.lib),
                "--log", str(self.log),
                "--report", str(self.report),
                "--ffsubsync", str(self.tmp / "does-not-exist"),
            ])
        self.assertEqual(code, 2)

    def test_no_sidecars_reports_nothing_found(self) -> None:
        (self.srt).unlink()
        fake = FakeFfsubsync(offset=-4.0)
        code = self._run(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [])
        self.assertIn("NOTHING FOUND", self.report.read_text(encoding="utf-8"))

    def test_unreadable_video_is_a_failure(self) -> None:
        # chmod 000 makes the video unreadable to ffsubsync's permission check.
        # Skip on platforms where the test user can still read it (e.g. root),
        # and on Windows, where chmod cannot clear the read bit at all.
        if os.name == "nt":
            self.skipTest("Windows chmod cannot revoke read access")
        if getattr(os, "geteuid", lambda: 1)() == 0:
            self.skipTest("running as root; permission bits do not apply")
        self.mkv.chmod(0)
        try:
            fake = FakeFfsubsync(rc=1, write_output=False)
            code = self._run(fake)
            self.assertEqual(code, 1)
            self.assertIn("FAILED SYNC ATTEMPTS", self.report.read_text(encoding="utf-8"))
        finally:
            self.mkv.chmod(stat.S_IRUSR | stat.S_IWUSR)


class _SyncedLibraryFixture(unittest.TestCase):
    """One movie, one good sidecar, a fake ffsubsync: the shared end-to-end bed.

    Holds no tests of its own - the suites below inherit it so that "run the
    tool over a real library" is written once.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sync_state_")
        self.tmp = Path(self._td.name).resolve()
        self.addCleanup(self._td.cleanup)
        self.lib = self.tmp / "lib"
        self.movie_dir = self.lib / "Film (2000)"
        self.movie_dir.mkdir(parents=True)
        self.mkv = self.movie_dir / "Film (2000).mkv"
        self.mkv.write_bytes(b"fake video")
        self.srt = self.movie_dir / "Film (2000).eng.srt"
        self.srt.write_text(GOOD_SRT, encoding="utf-8")
        self.log = self.tmp / "out" / "sync_subtitles.log"
        self.report = self.tmp / "out" / "sync_subtitles_report.txt"
        self.ledger = self.tmp / "out" / "sync_state.json"

    def _run(self, fake: FakeFfsubsync, *extra: str) -> int:
        real_which = shutil.which

        def which(name: str) -> str | None:
            if name == "ffmpeg":
                return "/usr/bin/ffmpeg"
            return real_which(name)

        with mock.patch.object(ss, "run_ffsubsync", fake), \
                mock.patch.object(ss, "find_ffsubsync", lambda explicit=None: "fake-ffsubsync"), \
                mock.patch.object(ss, "ffsubsync_version", lambda binary: "ffsubsync 9.9.9"), \
                mock.patch.object(ss, "detect_ffsubsync_features",
                                  lambda binary: ss.FfsubsyncFeatures(True, True, True)), \
                mock.patch("shutil.which", side_effect=which):
            return ss.main([
                "--source", str(self.lib),
                "--log", str(self.log),
                "--report", str(self.report),
                "--sync-ledger", str(self.ledger),
                *extra,
            ])

    def _second_run_calls(self, first: FakeFfsubsync, *extra: str) -> int:
        self.assertEqual(self._run(first), 0)
        second = FakeFfsubsync(offset=-4.0)
        self.assertEqual(self._run(second, *extra), 0)
        return len(second.calls)


class SyncStateTests(_SyncedLibraryFixture):
    """Remembered verdicts: a library that has already been synced must not
    pay for another ffsubsync run, and any change to either file must send
    the sidecar back through ffsubsync."""

    def test_second_run_does_not_remeasure(self) -> None:
        """The whole point: an unchanged, already-synced library costs nothing."""
        fake = FakeFfsubsync(offset=0.01)  # below --min-offset: "in sync"
        calls = self._second_run_calls(fake)
        self.assertEqual(calls, 0, "a remembered verdict must not respawn ffsubsync")
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("REMEMBERED IN SYNC (NOT RE-MEASURED)", report)
        self.assertIn("Remembered in sync", report)

    def test_a_corrected_sidecar_is_remembered_by_its_new_bytes(self) -> None:
        fake = FakeFfsubsync(offset=-4.0)  # trusted drift: sidecar is replaced
        self.assertEqual(self._run(fake), 0)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SHIFTED_SRT)

        second = FakeFfsubsync(offset=-4.0)
        self.assertEqual(self._run(second), 0)
        self.assertEqual(len(second.calls), 0)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SHIFTED_SRT)

    def test_edited_subtitle_is_measured_again(self) -> None:
        """A hand-edited or re-downloaded sidecar has new bytes: re-measure."""
        self.assertEqual(self._run(FakeFfsubsync(offset=0.01)), 0)
        self.srt.write_text(GOOD_SRT + "3\n00:00:09,000 --> 00:00:10,000\nExtra line.\n",
                            encoding="utf-8")
        second = FakeFfsubsync(offset=0.01)
        self.assertEqual(self._run(second), 0)
        self.assertEqual(len(second.calls), 1)

    def test_replaced_movie_is_measured_again(self) -> None:
        """A remux changes the movie's size and/or mtime: re-measure."""
        self.assertEqual(self._run(FakeFfsubsync(offset=0.01)), 0)
        self.mkv.write_bytes(b"a completely different movie file, remuxed")
        second = FakeFfsubsync(offset=0.01)
        self.assertEqual(self._run(second), 0)
        self.assertEqual(len(second.calls), 1)

    def test_a_held_sidecar_is_never_remembered(self) -> None:
        """Review and failure still need another attempt, so nothing is recorded."""
        self.assertEqual(self._run(FakeFfsubsync(offset=45.0)), 0)
        second = FakeFfsubsync(offset=45.0)
        self.assertEqual(self._run(second), 0)
        self.assertEqual(len(second.calls), 1, "an untrusted sync is re-measured next run")

    def test_dry_run_reads_but_never_writes_the_memory(self) -> None:
        self.assertEqual(self._run(FakeFfsubsync(offset=0.01)), 0)
        self.assertTrue(self.ledger.is_file())
        before = self.ledger.read_text(encoding="utf-8")

        fake = FakeFfsubsync(offset=-4.0)
        self.assertEqual(self._run(fake, "--dry-run"), 0)
        self.assertEqual(fake.calls, [], "a dry run never launches ffsubsync")
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), before,
                         "a dry run measured nothing, so it must not write")

    def test_dry_run_shows_what_a_live_run_would_skip(self) -> None:
        self.assertEqual(self._run(FakeFfsubsync(offset=0.01)), 0)
        self.assertEqual(self._run(FakeFfsubsync(offset=-4.0), "--dry-run"), 0)
        self.assertIn("REMEMBERED IN SYNC (NOT RE-MEASURED)",
                      self.report.read_text(encoding="utf-8"))

    def test_corrupt_memory_is_simply_forgotten(self) -> None:
        self.assertEqual(self._run(FakeFfsubsync(offset=0.01)), 0)
        self.ledger.write_text("{not json at all", encoding="utf-8")
        second = FakeFfsubsync(offset=0.01)
        self.assertEqual(self._run(second), 0)
        self.assertEqual(len(second.calls), 1, "an unreadable ledger must fail open, not crash")

    def test_ledger_outside_the_library_is_required(self) -> None:
        cfg = _cfg(self.tmp, sync_ledger=self.lib / "sync_state.json")
        self.assertIn("--sync-ledger must be outside the Jellyfin media library",
                      ss.validate_config(cfg))

    def test_a_sidecar_that_no_longer_exists_is_forgotten(self) -> None:
        self.assertEqual(self._run(FakeFfsubsync(offset=0.01)), 0)
        self.srt.unlink()
        second_srt = self.movie_dir / "Film (2000).en.srt"
        second_srt.write_text(GOOD_SRT, encoding="utf-8")

        fake = FakeFfsubsync(offset=0.01)
        self.assertEqual(self._run(fake), 0)
        self.assertEqual(len(fake.calls), 1, "a new sidecar is measured")
        saved = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertNotIn(ss.sync_state_key(self.srt), saved["entries"])


class StateCacheTests(_SyncedLibraryFixture):
    """The verdicts this tool publishes for ``organize status`` to read.

    Inherits the end-to-end fixture above: the sync ledger stays the authority
    for "must I re-measure?", and this cache is only ever a summary.
    """

    def _verdicts(self) -> dict:
        store = core.open_state(self.tmp / "out" / "state.db", tool="tests")
        try:
            return store.verdicts(core.KIND_SYNC)
        finally:
            store.close()

    def _run(self, fake: FakeFfsubsync, *extra: str) -> int:  # noqa: D102 - see parent
        return super()._run(fake, "--state-db", str(self.tmp / "out" / "state.db"), *extra)

    def test_a_measured_sidecar_is_published(self) -> None:
        self.assertEqual(self._run(FakeFfsubsync(offset=-4.0)), 0)
        verdict = self._verdicts()[(core.path_norm(self.mkv), core.KIND_SYNC)]
        self.assertEqual(verdict.verdict, ss.STATUS_SYNCED)
        self.assertIn("Film (2000).eng.srt", verdict.detail)
        info = self.mkv.stat()
        self.assertTrue(verdict.is_current_for(info.st_size, info.st_mtime_ns))

    def test_a_held_sidecar_is_published_as_review(self) -> None:
        self.assertEqual(self._run(FakeFfsubsync(offset=45.0)), 0)
        verdict = self._verdicts()[(core.path_norm(self.mkv), core.KIND_SYNC)]
        self.assertEqual(verdict.verdict, ss.STATUS_REVIEW)

    def test_an_orphan_sidecar_publishes_nothing_and_breaks_nothing(self) -> None:
        # A .srt with no movie beside it has nothing to key a verdict on; the
        # rest of the run must still be published.
        (self.movie_dir.parent / "Orphan (1999)").mkdir()
        (self.movie_dir.parent / "Orphan (1999)" / "Orphan (1999).eng.srt").write_text(
            GOOD_SRT, encoding="utf-8")
        self.assertEqual(self._run(FakeFfsubsync(offset=-4.0)), 0)
        self.assertEqual(list(self._verdicts()), [(core.path_norm(self.mkv), core.KIND_SYNC)])

    def test_a_dry_run_publishes_nothing(self) -> None:
        self.assertEqual(self._run(FakeFfsubsync(offset=-4.0), "--dry-run"), 0)
        self.assertEqual(self._verdicts(), {})

    def test_no_state_publishes_nothing(self) -> None:
        self.assertEqual(self._run(FakeFfsubsync(offset=-4.0), "--no-state"), 0)
        self.assertEqual(self._verdicts(), {})

    def test_the_run_survives_an_unwritable_cache(self) -> None:
        # A cache is a convenience; it can never turn a good sync into a bad run.
        (self.tmp / "out").mkdir(parents=True, exist_ok=True)
        (self.tmp / "out" / "state.db").write_bytes(b"not a database" * 50)
        self.assertEqual(self._run(FakeFfsubsync(offset=-4.0)), 0)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), SHIFTED_SRT)

    def test_a_state_db_inside_the_library_is_refused(self) -> None:
        code = self._run(FakeFfsubsync(offset=-4.0), "--state-db", str(self.lib / "state.db"))
        self.assertEqual(code, 2)


class ExitCodeTests(unittest.TestCase):
    def test_mapping(self) -> None:
        cfg = _cfg(Path("/tmp/wherever"))
        synced = ss.SyncResult(srt=Path("a.srt"), video=None, status=ss.STATUS_SYNCED)
        review = ss.SyncResult(srt=Path("b.srt"), video=None, status=ss.STATUS_REVIEW)
        failed = ss.SyncResult(srt=Path("c.srt"), video=None, status=ss.STATUS_FAILED)
        self.assertEqual(ss.exit_code_for([synced], cfg), 0)
        self.assertEqual(ss.exit_code_for([review], cfg), 0)
        self.assertEqual(ss.exit_code_for([failed], cfg), 1)
        strict = _cfg(Path("/tmp/wherever"), fail_on_review=True)
        self.assertEqual(ss.exit_code_for([review], strict), 3)
        self.assertEqual(ss.exit_code_for([review, failed], strict), 1,
                         "a failure dominates a review")


class VendoredContractTests(unittest.TestCase):
    """The subtitle contract vendored here must match the other tools."""

    def test_constants(self) -> None:
        self.assertEqual(core.EXTERNAL_SRT_SUFFIX, ".eng.srt")
        self.assertEqual(core.LEGACY_EXTERNAL_SRT_SUFFIX, ".en.srt")
        self.assertEqual(core.EXTERNAL_SRT_MAX_BYTES, 4 * 1024 * 1024)
        self.assertEqual(core.EXTERNAL_SRT_ENCODINGS, ("utf-8-sig", "utf-8", "cp1252"))

    def test_lock_shares_the_standardizer_key(self) -> None:
        import hashlib

        lock = ss.CoordinationLock(Path("/some/library"))
        normalized = os.path.normcase(os.path.normpath("/some/library"))
        key = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()[:20]
        self.assertEqual(lock.path.name, f".movie_standardizer.lock.{key}")

    def test_report_defaults_live_under_the_platform_reports_root(self) -> None:
        r"""Logs and reports default outside the library, on every platform.

        They used to hardcode the Windows tools directory, so a POSIX run with
        default config wrote a literal `E:\torrents\...` file into the CWD -
        which .gitignore carried an `E:*` rule to sweep up.
        """
        expected = ss.default_tool_dir("sync_subtitles")
        self.assertEqual(Path(ss.LOG_FILE).parent, expected)
        self.assertEqual(Path(ss.REPORT_FILE).parent, expected)
        self.assertEqual(Path(ss.SYNC_STATE_FILE).parent, expected)


class ReportShapeTests(unittest.TestCase):
    def _results(self) -> list[ss.SyncResult]:
        return [
            ss.SyncResult(srt=Path("/lib/A (2000)/A (2000).eng.srt"),
                          video=Path("/lib/A (2000)/A (2000).mkv"),
                          status=ss.STATUS_SYNCED, detail="offset -3.950s",
                          offset_seconds=-3.95, scale_factor=1.0, score=551.0,
                          seconds=12.3, original_sha="a" * 64, new_sha="b" * 64),
            ss.SyncResult(srt=Path("/lib/B (2001)/B (2001).eng.srt"),
                          video=Path("/lib/B (2001)/B (2001).mkv"),
                          status=ss.STATUS_REVIEW, detail="offset +45.000s beyond window",
                          offset_seconds=45.0),
            ss.SyncResult(srt=Path("/lib/C (2002)/C (2002).eng.srt"),
                          video=Path("/lib/C (2002)/C (2002).mkv"),
                          status=ss.STATUS_FAILED, detail="ffsubsync exited with code 1",
                          error_tail="ffmpeg not found"),
            ss.SyncResult(srt=Path("/lib/D (2003)/D (2003).eng.srt"),
                          video=None, status=ss.STATUS_SKIPPED,
                          detail="no matching movie file beside the subtitle"),
            ss.SyncResult(srt=Path("/lib/E (2004)/E (2004).eng.srt"),
                          video=Path("/lib/E (2004)/E (2004).mkv"),
                          status=ss.STATUS_IN_SYNC, detail="already aligned (offset +0.020s)",
                          offset_seconds=0.02),
        ]

    def test_report_fits_the_page_and_keeps_urgency_order(self) -> None:
        cfg = _cfg(Path("/tmp/wherever"))
        text = ss.build_report(self._results(), cfg, video_count=5,
                               ffsubsync_info="ffs ffsubsync 0.5.1",
                               features=ss.FfsubsyncFeatures(True, True, True),
                               elapsed_sec=12.3, truncated=False)
        lines = text.splitlines()
        self.assertTrue(text.endswith("\n"))
        self.assertTrue(all(not line.endswith(" ") for line in lines))
        self.assertTrue(all(len(line) <= core.REPORT_WIDTH for line in lines))
        self.assertIn("JELLYFIN SUBTITLE SYNCHRONIZER", text)
        for title in ("SUBTITLES HELD FOR REVIEW", "FAILED SYNC ATTEMPTS",
                      "SUBTITLES SYNCED (TIMING CORRECTED)", "SKIPPED (NOTHING SYNCED)",
                      "ALREADY IN SYNC"):
            self.assertIn(title, text)
        self.assertLess(text.index("SUBTITLES HELD FOR REVIEW"), text.index("FAILED SYNC ATTEMPTS"))
        self.assertLess(text.index("FAILED SYNC ATTEMPTS"), text.index("SUBTITLES SYNCED"))
        self.assertIn("Start here:", text)
        self.assertIn("ffmpeg not found", text)

    def test_empty_report_names_the_fix(self) -> None:
        cfg = _cfg(Path("/tmp/wherever"))
        text = ss.build_report([], cfg, video_count=3, ffsubsync_info="ffs",
                               features=ss.FfsubsyncFeatures(),
                               elapsed_sec=0.1, truncated=False)
        self.assertIn("NOTHING FOUND", text)
        self.assertIn("subtitle_fetcher.py", text)


class ParallelMeasurementTests(unittest.TestCase):
    """ffsubsync is the slowest thing the toolchain does, and each sidecar is
    an independent measurement, so they run in parallel. Two things must
    survive that: every sidecar is still measured exactly once, and the shared
    remembered-verdict ledger does not lose an entry to a lost update.
    """

    MOVIES = 12

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sync_parallel_")
        self.tmp = Path(self._td.name).resolve()
        self.addCleanup(self._td.cleanup)
        self.lib = self.tmp / "lib"
        for index in range(self.MOVIES):
            name = f"Film {index:02d} (2000)"
            folder = self.lib / name
            folder.mkdir(parents=True)
            (folder / f"{name}.mkv").write_bytes(b"fake video")
            (folder / f"{name}.eng.srt").write_text(GOOD_SRT, encoding="utf-8")
        self.log = self.tmp / "out" / "sync.log"
        self.report = self.tmp / "out" / "sync_report.txt"
        self.ledger = self.tmp / "out" / "sync_state.json"

    def _reset_library(self) -> None:
        """Put the library back exactly as setUp left it, in place.

        Rebuilding it in a *new* temporary directory would make the two
        reports differ by path, which is not the difference under test.
        """
        for index in range(self.MOVIES):
            name = f"Film {index:02d} (2000)"
            (self.lib / name / f"{name}.eng.srt").write_text(GOOD_SRT, encoding="utf-8")
        self.ledger.unlink(missing_ok=True)

    def _run(self, workers: int) -> int:
        real_which = shutil.which

        def which(name: str) -> str | None:
            return "/usr/bin/ffmpeg" if name == "ffmpeg" else real_which(name)

        fake = FakeFfsubsync()
        with mock.patch.object(ss, "run_ffsubsync", fake), \
                mock.patch.object(ss, "find_ffsubsync", lambda explicit=None: "fake-ffsubsync"), \
                mock.patch.object(ss, "ffsubsync_version", lambda binary: "ffsubsync 9.9.9"), \
                mock.patch.object(ss, "detect_ffsubsync_features",
                                  lambda binary: ss.FfsubsyncFeatures(True, True, True)), \
                mock.patch("shutil.which", side_effect=which):
            code = ss.main([
                "--source", str(self.lib),
                "--log", str(self.log),
                "--report", str(self.report),
                "--sync-ledger", str(self.ledger),
                "--workers", str(workers),
            ])
        self.calls = fake.calls
        return code

    def test_every_sidecar_is_measured_exactly_once(self) -> None:
        self.assertEqual(0, self._run(workers=4))
        self.assertEqual(self.MOVIES, len(self.calls),
                         "a sidecar was measured twice or not at all")
        for index in range(self.MOVIES):
            name = f"Film {index:02d} (2000)"
            self.assertEqual(
                SHIFTED_SRT,
                (self.lib / name / f"{name}.eng.srt").read_text(encoding="utf-8"),
            )

    def test_the_shared_ledger_keeps_every_verdict(self) -> None:
        """The lost-update case: twelve workers writing one dict."""
        self._run(workers=4)
        entries = json.loads(self.ledger.read_text(encoding="utf-8"))["entries"]
        self.assertEqual(self.MOVIES, len(entries))
        self.assertTrue(all(entry["status"] == ss.STATUS_SYNCED for entry in entries.values()))

    def test_the_report_is_the_same_whatever_the_worker_count(self) -> None:
        """Results are sorted before rendering, so scheduling cannot show up."""
        self._run(workers=1)
        serial = self.report.read_text(encoding="utf-8")
        self._reset_library()  # the first run rewrote every sidecar
        self._run(workers=4)
        parallel_text = self.report.read_text(encoding="utf-8")

        def comparable(text: str) -> list[str]:
            skip = ("Generated", "Elapsed", "Report", "Log", "Library", "Workers")
            return [line for line in text.splitlines()
                    if not any(f"{word} " in line for word in skip)]

        self.assertEqual(comparable(serial), comparable(parallel_text))

    def test_workers_must_not_be_negative(self) -> None:
        cfg = _cfg(self.tmp, library=self.lib, workers=-2)
        self.assertTrue(any("--workers" in error for error in ss.validate_config(cfg)))


if __name__ == "__main__":
    unittest.main()
