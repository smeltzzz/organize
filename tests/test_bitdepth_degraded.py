"""``bitdepth.py`` when a probe, a path or its own state cache misbehaves.

The inspector is read-only, so nothing it does can lose a file - but everything
it produces is a *worklist*, and a wrong row means somebody re-encodes a master
that was already fine or leaves a movie that will never Direct Play. These are
the paths where the answer could have been wrong silently:

* **Depth evidence is read, never assumed.** The packed pixel formats a hardware
  encoder emits (``p010``/``p012``/``p016``), a ``Main 12`` profile, and the
  ``HDR_Format`` tags some muxers write are each their own evidence; garbage in
  a numeric field has to fall through to the next source of truth rather than
  becoming 0 or raising.
* **Discovery refuses to probe debris.** A partial download, a sample, an NFO or
  a file under the size floor is not a movie, and probing one wastes an hour of
  a nightly run and can publish a QUEUE row for something that is not a film.
  A dangling symlink - a library on a share that went away - is skipped, not
  fatal.
* **One unreadable movie never ends the sweep.** A launch failure, a probe that
  answers with the wrong JSON shape, a file that vanishes before its ``stat()``,
  or a worker that raises all become an ERROR row for that movie while the rest
  of the library is still inspected and reported.
* **The state cache is a convenience, the report is the contract.** A cache that
  cannot be written warns and the run still exits with its real code.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fake_ffprobe as fake
import hermetic
from test_bitdepth_e2e import InspectorRunFixture

import bitdepth as bd
from organizekit.core import playbackchain as pc

DOVI_RECORD = {"side_data_type": "DOVI configuration record", "dv_profile": 8,
               "dv_bl_signal_compatibility_id": 1, "el_present_flag": 0,
               "bl_present_flag": 1}


class DepthEvidenceTests(unittest.TestCase):
    """The numeric fields ffprobe hands over, and what they are allowed to mean."""

    def test_the_packed_pixel_formats_carry_their_own_depth(self) -> None:
        """``p010``/``p012``/``p016`` are not in the explicit table by name.

        These are what hardware encoders and some remuxes emit; reading them as
        "unknown" would send a 10-bit file to HandBrake for a re-encode it does
        not need, and reading them as 8 would send a *12-bit* master there too.
        """
        self.assertEqual(bd.bit_depth_from_pix_fmt("p010le"), 10)
        self.assertEqual(bd.bit_depth_from_pix_fmt("p012le"), 12)
        self.assertEqual(bd.bit_depth_from_pix_fmt("p016le"), 16)
        self.assertEqual(bd.bit_depth_from_pix_fmt("P010"), 10, "the case is not the depth")

    def test_a_main_12_profile_is_read_as_12_bit(self) -> None:
        """With no pixel format and no raw-sample field, the profile is the evidence."""
        self.assertEqual(bd.bit_depth_from_profile("Main 12"), 12)
        self.assertEqual(bd.bit_depth_from_profile("High 12 Intra"), 12)
        self.assertIsNone(bd.bit_depth_from_profile("Main"),
                          "an undecided profile must stay undecided, not become 8")

    def test_garbage_in_a_numeric_field_falls_through_to_the_next_evidence(self) -> None:
        """A proxy or a broken muxer can put words where ffprobe puts numbers."""
        depth, evidence = bd.resolve_bit_depth(
            {"bits_per_raw_sample": "ten", "bits_per_component": "N/A",
             "pix_fmt": "yuv420p10le"})
        self.assertEqual(depth, 10)
        self.assertIn("pixel format", evidence, "the depth is attributed to what proved it")

    def test_a_duration_nobody_can_parse_leaves_the_verdict_alone(self) -> None:
        """Duration is display data; a bad one must not cost the movie its depth."""
        payload = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc",
                                "pix_fmt": "yuv420p10le", "duration": "not-a-number"}],
                   "format": {"duration": "also-not-a-number"}}
        result = bd.result_from_probe("/m/Movie.mkv", payload)
        self.assertIsNone(result.duration_sec)
        self.assertEqual(result.bit_depth, 10)

    def test_an_unspaced_dolby_tag_is_still_dolby_vision(self) -> None:
        """Muxers write ``DolbyVision``, and the label must not depend on a space.

        A file with no mastering-display side data and a bt709 transfer would
        otherwise be reported as SDR - and an SDR verdict on a Dolby Vision
        master is exactly the re-encode this tool exists to prevent. The generic
        tag scan looks for the spelled-out "Dolby Vision", so this value only
        reaches the HDR-format branch, which is the one that has to catch it.
        """
        stream = {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p10le",
                  "color_transfer": "bt709", "tags": {"HDR_Format": "DolbyVision"}}
        is_hdr, flavors, evidence = bd.classify_hdr(stream, None)
        self.assertIn("Dolby Vision", flavors)
        self.assertIn("HDR format tag: Dolby Vision", evidence)
        self.assertTrue(is_hdr)

    def test_an_hdr10_plus_tag_is_named_once_whichever_branch_reads_it(self) -> None:
        """The generic tag scan answers first, so the label is not duplicated."""
        stream = {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p10le",
                  "tags": {"HDR_Format": "HDR10+"}}
        is_hdr, flavors, evidence = bd.classify_hdr(stream, None)
        self.assertEqual(flavors.count("HDR10+"), 1)
        self.assertIn("stream/container tag: HDR10+ signature", evidence)
        self.assertTrue(is_hdr)


class ChainFitTests(unittest.TestCase):
    """The playback-chain verdict is read from the same probe as the depth."""

    def payload(self, *audio_codecs: str, **video: Any) -> dict[str, Any]:
        streams: list[dict[str, Any]] = [fake.video_stream(**video)]
        streams += [fake.audio_stream(codec_name=codec) for codec in audio_codecs]
        return fake.make_payload(*streams)

    def test_the_best_audio_track_present_decides_the_chain_verdict(self) -> None:
        """The cleaner keeps the best playable track, so that is what will play."""
        payload = self.payload("truehd", "eac3", width=1920, height=1080)
        stream = bd.pick_video_stream(payload)
        is_hdr, flavors, _ = bd.classify_hdr(stream, payload.get("format"))
        _video, audio, _note = bd.chain_fit(payload, stream, flavors, "")
        self.assertEqual(audio, pc.AUDIO_NATIVE,
                         "an E-AC-3 track beside a TrueHD one is what the chain gets")

    def test_audio_the_chain_can_never_emit_says_what_to_do_about_it(self) -> None:
        """A TrueHD-only file is not "unknown": the note names the fixing tool."""
        payload = self.payload("truehd", width=1920, height=1080)
        stream = bd.pick_video_stream(payload)
        is_hdr, flavors, _ = bd.classify_hdr(stream, payload.get("format"))
        _video, audio, note = bd.chain_fit(payload, stream, flavors, "")
        self.assertEqual(audio, pc.AUDIO_TRANSCODE_BOUND)
        self.assertIn("audio_standardizer.py", note)

    def test_a_video_stream_with_unreadable_dimensions_is_not_called_oversize(self) -> None:
        """>1080p forces a server transcode, so the claim has to survive bad data."""
        payload = self.payload("eac3", width="N/A", height="N/A")
        payload["streams"][0]["width"] = "N/A"
        payload["streams"][0]["height"] = "N/A"
        stream = bd.pick_video_stream(payload)
        video, _audio, _note = bd.chain_fit(payload, stream, [], "")
        self.assertNotEqual(video, pc.VIDEO_OVERSIZE)


class ToolDiscoveryTests(unittest.TestCase):
    """Where ffprobe comes from, and what happens when it is not there."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="bd_probe_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_a_probe_on_the_path_is_found_once_even_when_it_is_listed_twice(self) -> None:
        """The bundled-tools directory and PATH can name the same binary."""
        home = self.tmp / "tools"
        home.mkdir()
        binary = home / "ffprobe"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        with mock.patch.object(bd.shutil, "which", return_value=str(binary)), \
                mock.patch.object(bd, "tools_home", return_value=home):
            found = bd.find_ffprobe()
        self.assertEqual(found, str(binary))

    def test_a_binary_that_cannot_be_executed_is_not_a_working_probe(self) -> None:
        self.assertFalse(bd.ffprobe_works(str(self.tmp / "no-such-ffprobe")))

class NoProbeAtAllTests(hermetic.HermeticToolsMixin, unittest.TestCase):
    def test_a_machine_with_no_ffmpeg_at_all_is_refused_with_2(self) -> None:
        """The hermetic machine: no ffprobe on PATH, none bundled."""
        self.assertIsNone(shutil.which("ffprobe"), "the fixture must really be bare")
        with tempfile.TemporaryDirectory(prefix="bd_hermetic_") as td:
            library = Path(td) / "Movies"
            library.mkdir()
            argv = ["--source", str(library), "--log", str(Path(td) / "b.log"),
                    "--report", str(Path(td) / "b.txt")]
            stream = io.StringIO()
            with mock.patch.object(bd.log, "stream", stream):
                code = bd.main(argv)
        self.assertEqual(code, 2)
        self.assertIn("Install FFmpeg", stream.getvalue())


class FileDiscoveryTests(unittest.TestCase):
    """What the sweep walks, and what it must leave alone."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="bd_discover_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.cfg = bd.Config(source_dir=self.root, min_file_size_mb=1)

    def write(self, name: str, size: int = 4 * 1024 * 1024, folder: str = "Film (2001)") -> Path:
        target = self.root / folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
        return target

    def test_a_root_that_is_not_a_directory_yields_nothing(self) -> None:
        stray = self.write("stray.mkv", folder=".")
        self.assertEqual(bd.discover_videos(stray, self.cfg), [])

    def test_debris_partials_and_non_video_files_are_not_probed(self) -> None:
        """A partial download probes as a truncated movie, and looks like a defect.

        qBittorrent leaves ``.!qb`` and ``.part`` files in the library root while
        a torrent is still working, and a nightly inspector that probed them would
        report a broken movie that finishes downloading an hour later.
        """
        good = self.write("Film (2001).mkv")
        self.write("Film (2001).mkv.!qb")
        self.write("Film (2001).mkv.part")
        self.write(".hidden.mkv")
        self.write("Film (2001).nfo", size=4 * 1024 * 1024)
        self.write("Film (2001)-sample.mkv")
        self.write("Film (2001).eng.srt", size=4 * 1024 * 1024)
        self.assertEqual(bd.discover_videos(self.root, self.cfg), [good])

    def test_a_file_under_the_size_floor_is_not_a_feature(self) -> None:
        small = self.write("Tiny (2002).mkv", size=64 * 1024, folder="Tiny (2002)")
        big = self.write("Film (2001).mkv")
        found = bd.discover_videos(self.root, self.cfg)
        self.assertEqual(found, [big], f"{small.name} is under the floor")

    def test_a_dangling_symlink_is_skipped_rather_than_aborting_the_walk(self) -> None:
        """A library on a share that went away must not cost the whole sweep."""
        good = self.write("Film (2001).mkv")
        broken = self.root / "Film (2001)" / "Gone (2003).mkv"
        broken.symlink_to(self.root / "nowhere" / "Gone (2003).mkv")
        self.assertEqual(bd.discover_videos(self.root, self.cfg), [good])


class ProbeFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="bd_probefail_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.movie = self.tmp / "Film (2001).mkv"
        fake.write_movie(self.movie, fake.sdr_8bit(), size=2 * 1024 * 1024)

    def test_a_probe_that_cannot_be_launched_is_an_error_with_a_reason(self) -> None:
        cfg = bd.Config(ffprobe=str(self.tmp / "no-such-ffprobe"))
        with self.assertRaises(RuntimeError) as caught:
            bd.run_ffprobe(cfg.ffprobe, self.movie, cfg)
        self.assertIn("failed to launch ffprobe", str(caught.exception))

    def test_a_movie_that_vanishes_before_its_probe_is_reported_not_dropped(self) -> None:
        """The remux race: a file replaced between discovery and inspection.

        Dropping it would leave the report claiming every movie in the library was
        inspected, and nobody would ever look at that file again.
        """
        gone = self.tmp / "Gone (2003).mkv"
        result = bd.inspect_movie(gone, bd.Config())
        self.assertEqual(result.status, bd.STATUS_ERROR)
        self.assertEqual(result.category, bd.CATEGORY_LABELS[bd.STATUS_ERROR])
        self.assertIn("No such file", result.error or "")


class DegradedRunTests(InspectorRunFixture):
    """A real run against the fake probe, with one thing broken at a time."""

    def test_a_probe_that_answers_with_the_wrong_json_shape_is_refused(self) -> None:
        """Valid JSON, wrong shape: the movie is an ERROR row, the run finishes.

        A list where an object belongs would raise deep inside classification and
        take the whole sweep with it - or worse, be indexed as if it were a
        stream and produce a verdict about nothing.
        """
        self.movie("Array Answer (2005)", fake.sdr_8bit())
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        code = self._run("--fail-if-error", env={"FAKE_FFPROBE_JSON_ARRAY": "1"})
        self.assertEqual(code, 5, "--fail-if-error is the scheduler's own exit code")
        report = self.report_text()
        self.assertIn("Array Answer (2005).mkv", report)
        self.assertIn("ffprobe JSON was not an object", report)

    def test_a_dolby_vision_file_says_its_profile_and_its_bound_audio(self) -> None:
        """The rows an operator acts on, rendered from the same probe.

        The DV profile is what tells somebody whether the file has an HDR10 base
        layer worth keeping, and the bound-audio row is what points at
        ``audio_standardizer.py`` instead of a pointless video re-encode.
        """
        payload = fake.make_payload(
            fake.video_stream(codec_name="dvh1", side_data_list=[DOVI_RECORD]),
            fake.audio_stream(codec_name="truehd"),
        )
        self.movie("Dolby File (2006)", payload)
        self.assertEqual(self._run(), 0)
        report = self.report_text()
        self.assertIn("Dolby Vision", report)
        self.assertIn("profile 8.1", report)
        self.assertIn("PLAYBACK CHAIN FIT", report)
        self.assertIn("audio_standardizer.py", report)

    def test_a_worker_that_raises_becomes_an_error_row_and_the_sweep_continues(self) -> None:
        """One bad movie must not cost the other three thousand their verdicts."""
        good = self.movie("Plain SDR (2001)", fake.sdr_8bit())
        bad = self.movie("Defect (2007)", fake.sdr_8bit())
        real = bd.inspect_movie

        def inspect(path: Path, cfg: bd.Config, cache: Any = None) -> Any:
            if Path(path) == bad:
                raise RuntimeError("injected worker defect")
            return real(path, cfg, cache)

        with mock.patch.object(bd, "inspect_movie", side_effect=inspect):
            code = self._run("--fail-if-error")
        self.assertEqual(code, 5)
        report = self.report_text()
        self.assertIn("injected worker defect", report)
        self.assertIn(good.name, report, "the healthy movie was still inspected")
        self.assertEqual(self.verdicts()[good.name], bd.STATUS_QUEUE)

    def test_a_state_cache_that_cannot_be_written_never_fails_the_inspection(self) -> None:
        """The verdict cache feeds ``organize status``; the report feeds the human."""
        self.movie("Plain SDR (2001)", fake.sdr_8bit())
        store = mock.Mock()
        store.enabled = True
        store.record.side_effect = OSError("database is locked")
        stream = io.StringIO()
        with mock.patch.object(bd, "open_state", return_value=store), \
                mock.patch.object(bd.log, "stream", stream):
            code = self._run("--fail-if-queue")
        self.assertEqual(code, 3, "the real verdict still drives the exit code")
        self.assertIn("state cache not updated", stream.getvalue())
        store.close.assert_called_once_with()
        self.assertIn("Plain SDR (2001).mkv", self.report_text())

    def test_a_library_that_disappears_before_the_scan_exits_2_and_publishes_nothing(
            self) -> None:
        """Validated, then unmounted: that is not a library with zero movies."""
        cfg = bd.Config(source_dir=self.library, log_file=self.log, report_file=self.report,
                        use_cache=False, use_state=False, workers=1,
                        ffprobe=str(self.ffprobe))
        self.assertEqual(bd.validate_config(cfg), [], "the fixture must start valid")
        shutil.rmtree(self.library)
        stream = io.StringIO()
        with mock.patch.object(bd.log, "stream", stream):
            code = bd.scan(cfg)
        self.assertEqual(code, 2)
        self.assertIn("Directory does not exist", stream.getvalue())
        self.assertFalse(self.report.exists(), "no report may claim the library is empty")

    def test_an_unwritable_report_path_stops_a_run_that_found_nothing(self) -> None:
        """Exit 0 with no report would tell a scheduler the library is fine."""
        library = self.tmp / "Empty"
        library.mkdir()
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where the report's parent directory should be",
                           encoding="utf-8")
        cfg = bd.Config(source_dir=library, log_file=self.log,
                        report_file=blocker / "report.txt",
                        use_cache=False, use_state=False, workers=1,
                        ffprobe=str(self.ffprobe))
        stream = io.StringIO()
        with mock.patch.object(bd.log, "stream", stream):
            code = bd.scan(cfg)
        self.assertEqual(code, 2)
        self.assertIn("Cannot write report", stream.getvalue())


class ConfigGateTests(InspectorRunFixture):
    def test_a_worker_count_that_cannot_run_is_refused(self) -> None:
        """Zero workers would mean an empty pool: a sweep that probes nothing."""
        cfg = bd.Config(source_dir=self.library, workers=0)
        self.assertIn("--workers must be greater than zero", bd.validate_config(cfg))

    def test_the_inspector_reports_configuration_errors_before_probing(self) -> None:
        """A report inside the library is refused before a single file is probed.

        The inspector walks every folder: its own report would become a finding,
        and the next run would audit the previous run's output.
        """
        stream = io.StringIO()
        with mock.patch.object(bd.log, "stream", stream):
            code = bd.main(["--source", str(self.library), "--log", str(self.log),
                            "--report", str(self.library / "inside.txt")])
        self.assertEqual(code, 2)
        self.assertIn("Report path must be outside --source", stream.getvalue())
        self.assertFalse((self.library / "inside.txt").exists())
        self.assertFalse(self.report.exists(), "nothing was probed or published")


if __name__ == "__main__":
    unittest.main()
