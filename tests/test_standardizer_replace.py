"""The one decision that lets a movie overwrite a movie.

Everything else the standardizer does is additive: it hardlinks an incoming
file into a canonical folder and leaves what it found alone. ``should_replace``
is where that stops being true - it is the gate that allows an existing movie
in the library to be swapped for a different set of bytes. Getting it wrong in
the permissive direction means a theatrical cut silently overwriting an
extended one, or an HDR master replaced by an SDR encode, with no copy left to
go back to.

Until now it had no direct tests at all: reaching it meant building a library,
having ``ffprobe`` installed, and owning two real movies with the right
technical properties. The guard chain is now ``upgrade_verdict()``, a function
of two probe results, and the probing around it is patched.

The rule these tests exist to defend: **every guard is a veto, and anything
unknown keeps the existing movie.**
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import movie_standardizer as ms


def info(**overrides: object) -> ms.MediaTechnicalInfo:
    """A 1080p SDR 8-bit stereo baseline; each test changes one thing."""
    fields: dict = {
        "duration": 7200.0,
        "width": 1920,
        "height": 1080,
        "video_codec": "h264",
        "bit_depth": 8,
        "hdr": False,
        "video_bitrate": 8_000_000,
        "audio_channels": 2,
        "audio_bitrate": 192_000,
    }
    fields.update(overrides)
    return ms.MediaTechnicalInfo(**fields)  # type: ignore[arg-type]


class ResolutionTierTests(unittest.TestCase):
    """Tiers, not pixel counts: a 1920x800 scope film is still 1080p."""

    def tier(self, width: int, height: int) -> int:
        return info(width=width, height=height).resolution_tier

    def test_the_common_shapes_land_where_an_operator_expects(self) -> None:
        self.assertEqual(self.tier(3840, 2160), 4)
        self.assertEqual(self.tier(2560, 1440), 3)
        self.assertEqual(self.tier(1920, 1080), 2)
        self.assertEqual(self.tier(1280, 720), 1)
        self.assertEqual(self.tier(720, 480), 0)

    def test_a_scope_aspect_ratio_is_not_a_downgrade(self) -> None:
        """1920x800 has fewer pixels than 1920x1080 and the same tier."""
        self.assertEqual(self.tier(1920, 800), self.tier(1920, 1080))

    def test_a_portrait_encode_is_measured_on_its_longest_side(self) -> None:
        self.assertEqual(self.tier(1080, 1920), 2)


class QualityScoreTests(unittest.TestCase):
    """The score only ever breaks ties between candidates that passed the guards."""

    def test_more_of_a_good_thing_never_scores_lower(self) -> None:
        base = ms.technical_quality_score(info())
        for better in (
            info(width=3840, height=2160),
            info(hdr=True),
            info(bit_depth=10),
            info(video_bitrate=12_000_000),
            info(audio_channels=6),
            info(audio_bitrate=640_000),
            info(video_codec="hevc"),
        ):
            self.assertGreater(ms.technical_quality_score(better), base)

    def test_an_ancient_codec_is_penalised(self) -> None:
        self.assertLess(
            ms.technical_quality_score(info(video_codec="mpeg2video")),
            ms.technical_quality_score(info()),
        )

    def test_an_unknown_codec_is_neither_rewarded_nor_punished(self) -> None:
        self.assertEqual(
            ms.technical_quality_score(info(video_codec="prores_raw_9000")),
            ms.technical_quality_score(info(video_codec="h264")),
        )

    def test_a_remux_sized_bitrate_cannot_buy_a_resolution_tier(self) -> None:
        """The bitrate terms are capped so no encode outscores a real upgrade."""
        absurd = info(video_bitrate=400_000_000, audio_bitrate=50_000_000)
        self.assertLess(
            ms.technical_quality_score(absurd),
            ms.technical_quality_score(info(width=3840, height=2160)),
        )


class UpgradeVerdictTests(unittest.TestCase):
    """The guard chain, one veto at a time."""

    def verdict(self, **source: object) -> tuple[bool, str]:
        return ms.upgrade_verdict(info(**source), info())

    # -- the one case that says yes ----------------------------------------

    def test_a_genuine_upgrade_is_allowed_and_says_why(self) -> None:
        allowed, reason = self.verdict(width=3840, height=2160, bit_depth=10,
                                       hdr=True, video_codec="hevc")
        self.assertTrue(allowed)
        self.assertIn("verified same-cut technical upgrade", reason)
        self.assertIn("runtime gap 0.0s", reason)

    # -- runtime: a different cut is a different movie ---------------------

    def test_a_longer_cut_never_replaces_the_one_on_disk(self) -> None:
        allowed, reason = self.verdict(duration=8400.0, width=3840, height=2160)
        self.assertFalse(allowed)
        self.assertIn("likely a different cut", reason)

    def test_a_shorter_cut_is_refused_the_same_way(self) -> None:
        """abs(): the guard is symmetric, or an extended cut could be overwritten."""
        allowed, reason = self.verdict(duration=6000.0, width=3840, height=2160)
        self.assertFalse(allowed)
        self.assertIn("likely a different cut", reason)

    def test_encoder_level_runtime_drift_is_tolerated(self) -> None:
        allowed, _ = self.verdict(duration=7215.0, width=3840, height=2160)
        self.assertTrue(allowed, "15s on a two-hour movie is the same cut")

    def test_the_tolerance_scales_with_a_long_movie(self) -> None:
        """1% of a four-hour epic is 144s; a fixed 30s window would reject it."""
        long_existing = info(duration=14_400.0)
        long_source = info(duration=14_500.0, width=3840, height=2160)
        allowed, _ = ms.upgrade_verdict(long_source, long_existing)
        self.assertTrue(allowed)

    # -- the four one-way regressions --------------------------------------

    def test_a_lower_resolution_tier_is_refused(self) -> None:
        existing = info(width=3840, height=2160)
        allowed, reason = ms.upgrade_verdict(info(video_codec="av1"), existing)
        self.assertFalse(allowed)
        self.assertIn("lower resolution tier", reason)

    def test_even_one_tier_down_is_refused_when_everything_else_is_better(self) -> None:
        """The case that makes the guard worth having: a 1440p HDR AV1 remux.

        It beats a plain 4K SDR copy on every other axis and on the score, and
        it still loses the pixels the library already has.
        """
        existing = info(width=3840, height=2160)
        source = info(width=2560, height=1440, hdr=True, bit_depth=10,
                      video_codec="av1", video_bitrate=20_000_000, audio_channels=8,
                      audio_bitrate=3_000_000)
        self.assertGreater(
            ms.technical_quality_score(source), ms.technical_quality_score(existing),
            "if this stops being true the test no longer proves anything",
        )
        allowed, reason = ms.upgrade_verdict(source, existing)
        self.assertFalse(allowed)
        self.assertIn("lower resolution tier", reason)

    def test_sdr_never_replaces_hdr(self) -> None:
        """Fail-closed: the tone mapping is not recoverable from the SDR file."""
        existing = info(hdr=True)
        allowed, reason = ms.upgrade_verdict(info(width=3840, height=2160), existing)
        self.assertFalse(allowed)
        self.assertIn("HDR with SDR", reason)

    def test_hdr_may_replace_sdr(self) -> None:
        allowed, _ = self.verdict(hdr=True, video_bitrate=9_000_000)
        self.assertTrue(allowed)

    def test_a_lower_bit_depth_is_refused(self) -> None:
        existing = info(bit_depth=10)
        allowed, reason = ms.upgrade_verdict(info(width=3840, height=2160), existing)
        self.assertFalse(allowed)
        self.assertIn("lower video bit depth", reason)

    def test_fewer_audio_channels_are_refused(self) -> None:
        existing = info(audio_channels=6)
        allowed, reason = ms.upgrade_verdict(info(width=3840, height=2160), existing)
        self.assertFalse(allowed)
        self.assertIn("fewer audio channels", reason)

    def test_a_regression_is_refused_even_when_it_scores_higher(self) -> None:
        """The guards are vetoes, not weights: no score can buy past one."""
        existing = info(hdr=True)
        source = info(width=3840, height=2160, bit_depth=12, audio_channels=8,
                      video_codec="av1", video_bitrate=20_000_000)
        self.assertGreater(
            ms.technical_quality_score(source), ms.technical_quality_score(existing),
        )
        allowed, reason = ms.upgrade_verdict(source, existing)
        self.assertFalse(allowed)
        self.assertIn("HDR with SDR", reason)

    # -- the margin ---------------------------------------------------------

    def test_an_identical_file_is_not_an_upgrade(self) -> None:
        allowed, reason = self.verdict()
        self.assertFalse(allowed)
        self.assertIn("no clear technical upgrade", reason)

    def test_a_marginal_gain_is_not_worth_rewriting_a_movie(self) -> None:
        allowed, reason = self.verdict(video_bitrate=8_500_000)
        self.assertFalse(allowed)
        self.assertIn(f"need +{ms.DUPLICATE_MIN_SCORE_GAIN:.1f}", reason)

    def test_the_margin_is_the_boundary_it_claims_to_be(self) -> None:
        existing = info()
        wanted = ms.technical_quality_score(existing) + ms.DUPLICATE_MIN_SCORE_GAIN
        # +1.5 per audio channel, +2.0 per video Mbps: reach the margin exactly.
        source = info(video_bitrate=13_000_000)
        self.assertAlmostEqual(ms.technical_quality_score(source), wanted, places=6)
        allowed, _ = ms.upgrade_verdict(source, existing)
        self.assertTrue(allowed, "a gain of exactly the margin qualifies")
        just_under = info(video_bitrate=12_999_000)
        self.assertFalse(ms.upgrade_verdict(just_under, existing)[0])

    def test_every_refusal_is_labelled_a_conflict(self) -> None:
        """The report groups these by that prefix; an unlabelled one disappears."""
        refusals = [
            self.verdict(duration=9000.0),
            ms.upgrade_verdict(info(), info(width=3840, height=2160)),
            ms.upgrade_verdict(info(), info(hdr=True)),
            ms.upgrade_verdict(info(), info(bit_depth=10)),
            ms.upgrade_verdict(info(), info(audio_channels=6)),
            self.verdict(),
        ]
        for allowed, reason in refusals:
            self.assertFalse(allowed)
            self.assertTrue(reason.startswith("conflict: "), reason)


class ProbeMediaTests(unittest.TestCase):
    """Reading ffprobe's answer, including every way it can be useless."""

    def probe(self, payload: object = None, *, returncode: int = 0,
              stderr: str = "", stdout: str | None = None,
              side_effect: Exception | None = None) -> tuple[object, str]:
        completed = subprocess.CompletedProcess(
            args=["ffprobe"], returncode=returncode,
            stdout=json.dumps(payload) if stdout is None else stdout,
            stderr=stderr,
        )
        with mock.patch.object(ms.subprocess, "run", side_effect=side_effect,
                               return_value=completed):
            return ms.probe_media(Path("/library/Film (2020)/Film (2020).mkv"), "ffprobe")

    def stream(self, **overrides: object) -> dict:
        base: dict = {"codec_type": "video", "codec_name": "H264", "width": 1920,
                      "height": 1080, "bit_rate": "8000000"}
        base.update(overrides)
        return base

    def payload(self, *streams: dict, duration: str = "7200.5") -> dict:
        return {"streams": list(streams), "format": {"duration": duration}}

    def test_an_ordinary_movie_reads_cleanly(self) -> None:
        result, error = self.probe(self.payload(
            self.stream(),
            {"codec_type": "audio", "channels": 6, "bit_rate": "640000"},
        ))
        self.assertEqual(error, "")
        assert isinstance(result, ms.MediaTechnicalInfo)
        self.assertEqual(result.width, 1920)
        self.assertEqual(result.video_codec, "h264", "codecs are compared case-folded")
        self.assertEqual((result.audio_channels, result.audio_bitrate), (6, 640_000))
        self.assertAlmostEqual(result.duration, 7200.5)

    def test_cover_art_is_not_the_feature(self) -> None:
        """An attached poster is a video stream to ffprobe - and often a huge one.

        A 4000x6000 scan of a Blu-ray sleeve has three times the pixels of the
        movie it is embedded in, so "largest video stream" alone would call a
        1080p film 4K and let it overwrite a real UHD copy.
        """
        result, _ = self.probe(self.payload(
            {"codec_type": "video", "codec_name": "mjpeg", "width": 4000, "height": 6000,
             "disposition": {"attached_pic": 1}},
            self.stream(),
        ))
        assert isinstance(result, ms.MediaTechnicalInfo)
        self.assertEqual((result.width, result.height), (1920, 1080))
        self.assertEqual(result.resolution_tier, 2)

    def test_the_largest_video_stream_wins(self) -> None:
        result, _ = self.probe(self.payload(
            self.stream(width=640, height=360),
            self.stream(width=3840, height=2160),
        ))
        assert isinstance(result, ms.MediaTechnicalInfo)
        self.assertEqual(result.resolution_tier, 4)

    def test_the_best_audio_stream_wins(self) -> None:
        result, _ = self.probe(self.payload(
            self.stream(),
            {"codec_type": "audio", "channels": 2, "bit_rate": "192000"},
            {"codec_type": "audio", "channels": 8, "bit_rate": "3000000"},
        ))
        assert isinstance(result, ms.MediaTechnicalInfo)
        self.assertEqual(result.audio_channels, 8)

    def test_a_movie_with_no_audio_is_still_readable(self) -> None:
        result, error = self.probe(self.payload(self.stream()))
        self.assertEqual(error, "")
        assert isinstance(result, ms.MediaTechnicalInfo)
        self.assertEqual((result.audio_channels, result.audio_bitrate), (0, 0))

    def test_a_container_without_a_duration_falls_back_to_the_stream(self) -> None:
        result, _ = self.probe(self.payload(
            self.stream(duration="5400.0"), duration="",
        ))
        assert isinstance(result, ms.MediaTechnicalInfo)
        self.assertAlmostEqual(result.duration, 5400.0)

    def test_a_missing_video_bitrate_falls_back_to_the_container(self) -> None:
        payload = self.payload(self.stream(bit_rate=None))
        payload["format"]["bit_rate"] = "6000000"
        result, _ = self.probe(payload)
        assert isinstance(result, ms.MediaTechnicalInfo)
        self.assertEqual(result.video_bitrate, 6_000_000)

    # -- everything that must produce a reason instead of a guess -----------

    def test_a_file_with_no_video_stream_is_not_a_movie(self) -> None:
        result, error = self.probe(self.payload(
            {"codec_type": "audio", "channels": 2},
        ))
        self.assertIsNone(result)
        self.assertIn("no feature video stream", error)

    def test_missing_duration_or_resolution_is_refused_not_defaulted(self) -> None:
        for payload in (
            self.payload(self.stream(), duration="0"),
            self.payload(self.stream(width=0, height=0)),
        ):
            result, error = self.probe(payload)
            self.assertIsNone(result)
            self.assertIn("incomplete duration/resolution data", error)

    def test_a_nonzero_exit_reports_ffprobe_own_last_line(self) -> None:
        result, error = self.probe(
            {}, returncode=1, stderr="moov atom not found\nInvalid data found\n",
        )
        self.assertIsNone(result)
        self.assertIn("Invalid data found", error)

    def test_a_silent_failure_still_names_the_exit_code(self) -> None:
        result, error = self.probe({}, returncode=69, stderr="")
        self.assertIsNone(result)
        self.assertIn("exit 69", error)

    def test_garbage_on_stdout_is_a_reason_not_a_traceback(self) -> None:
        result, error = self.probe(stdout="<html>gateway timeout</html>")
        self.assertIsNone(result)
        self.assertIn("invalid JSON", error)

    def test_a_hung_probe_gives_up_and_says_so(self) -> None:
        result, error = self.probe(
            side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=30.0),
        )
        self.assertIsNone(result)
        self.assertIn("could not inspect", error)

    def test_a_missing_binary_is_a_reason_not_a_crash(self) -> None:
        result, error = self.probe(side_effect=OSError("No such file"))
        self.assertIsNone(result)
        self.assertIn("could not inspect", error)


class StreamFlagTests(unittest.TestCase):
    """Bit depth and HDR are read from whichever field the encoder filled in."""

    def test_an_explicit_raw_sample_size_is_believed(self) -> None:
        self.assertEqual(ms._stream_bit_depth({"bits_per_raw_sample": "10"}), 10)

    def test_otherwise_the_pixel_format_says_it(self) -> None:
        self.assertEqual(ms._stream_bit_depth({"pix_fmt": "yuv420p10le"}), 10)
        self.assertEqual(ms._stream_bit_depth({"pix_fmt": "yuv444p12be"}), 12)

    def test_an_ordinary_8_bit_stream_needs_no_marker(self) -> None:
        self.assertEqual(ms._stream_bit_depth({"pix_fmt": "yuv420p"}), 8)
        self.assertEqual(ms._stream_bit_depth({}), 8)

    def test_pq_and_hlg_transfers_are_hdr(self) -> None:
        self.assertTrue(ms._stream_is_hdr({"color_transfer": "smpte2084"}))
        self.assertTrue(ms._stream_is_hdr({"color_transfer": "arib-std-b67"}))

    def test_dolby_vision_side_data_is_hdr(self) -> None:
        self.assertTrue(ms._stream_is_hdr(
            {"side_data_list": [{"side_data_type": "DOVI configuration record"}]},
        ))

    def test_an_ordinary_rec709_stream_is_not(self) -> None:
        self.assertFalse(ms._stream_is_hdr({"color_transfer": "bt709"}))
        self.assertFalse(ms._stream_is_hdr({}))

    def test_malformed_side_data_is_not_hdr_and_does_not_raise(self) -> None:
        self.assertFalse(ms._stream_is_hdr({"side_data_list": ["not a dict", None]}))


class ShouldReplaceTests(unittest.TestCase):
    """The gate itself, on real files."""

    def setUp(self) -> None:
        self._saved_cfg = ms.CFG
        self._td = tempfile.TemporaryDirectory(prefix="ms_replace_")
        self.root = Path(self._td.name)
        ms.CFG = ms.Config(
            source_dir=self.root / "final",
            target_dir=self.root / "Movies",
            log_file=None,
            report_file=self.root / "out" / "report.txt",
        )
        self._logging = (ms.LOG.handlers[:], ms.LOG.propagate)
        ms.LOG.handlers = [logging.NullHandler()]
        ms.LOG.propagate = False
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        ms.LOG.handlers, ms.LOG.propagate = self._logging
        ms.CFG = self._saved_cfg
        self._td.cleanup()

    def file(self, relative: str, size: int) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.truncate(size)
        return path

    def movie(self, name: str, size: int = 4 * 1024 * 1024) -> Path:
        return self.file(f"{name}/{Path(name).name}.mkv", size)

    # -- the cheap answers, before anything is probed ----------------------

    def test_an_empty_destination_is_simply_filled(self) -> None:
        src = self.movie("final/Film (2020)")
        self.assertEqual(
            ms.should_replace(src, self.root / "Movies/Film (2020)/Film (2020).mkv"),
            (True, "missing"),
        )

    def test_a_file_never_replaces_itself(self) -> None:
        src = self.movie("final/Film (2020)")
        self.assertEqual(ms.should_replace(src, src), (False, "same-file"))

    def test_an_existing_hardlink_is_recognised_not_relinked(self) -> None:
        """Re-running an ingest must be a no-op, not a swap.

        Two paths, one inode: `paths_equal` resolves that with `samefile`, so
        this is refused as `same-file` before the `already-linked` guard is
        reached. Both answers are the same decision - the point of the test is
        that a second ingest of an already-placed movie touches nothing.
        """
        src = self.movie("final/Film (2020)")
        dest = self.root / "Movies/Film (2020)/Film (2020).mkv"
        dest.parent.mkdir(parents=True)
        try:
            dest.hardlink_to(src)
        except OSError:  # pragma: no cover - filesystem without hardlinks
            self.skipTest("this filesystem has no hardlinks")
        allowed, reason = ms.should_replace(src, dest)
        self.assertFalse(allowed)
        self.assertIn(reason, {"same-file", "already-linked"})

    def test_a_dangling_samefile_check_does_not_end_the_run(self) -> None:
        """`samefile` raises on a path that vanishes mid-scan; that is not fatal."""
        src = self.movie("final/Film (2020)")
        dest = self.movie("Movies/Film (2020)")
        with mock.patch.object(ms, "paths_equal", return_value=False), \
                mock.patch.object(Path, "samefile", side_effect=OSError("gone")), \
                mock.patch.object(ms, "find_ffprobe", return_value=None):
            allowed, reason = ms.should_replace(src, dest)
        self.assertFalse(allowed)
        self.assertIn("ffprobe unavailable", reason)

    # -- sidecars keep the old size rule -----------------------------------

    def test_a_bigger_sidecar_wins(self) -> None:
        src = self.file("final/Film (2020)/Film (2020).eng.srt", 4096)
        dest = self.file("Movies/Film (2020)/Film (2020).eng.srt", 1024)
        allowed, reason = ms.should_replace(src, dest)
        self.assertTrue(allowed)
        self.assertIn("src-larger", reason)

    def test_an_equal_sidecar_is_left_alone(self) -> None:
        src = self.file("final/Film (2020)/Film (2020).eng.srt", 2048)
        dest = self.file("Movies/Film (2020)/Film (2020).eng.srt", 2048)
        self.assertEqual(ms.should_replace(src, dest), (False, "same-size-exists"))

    def test_a_smaller_sidecar_never_truncates_a_bigger_one(self) -> None:
        src = self.file("final/Film (2020)/Film (2020).eng.srt", 512)
        dest = self.file("Movies/Film (2020)/Film (2020).eng.srt", 4096)
        allowed, reason = ms.should_replace(src, dest)
        self.assertFalse(allowed)
        self.assertIn("dest-larger", reason)

    # -- a movie is never decided on size ----------------------------------

    def test_a_much_bigger_movie_is_not_enough_on_its_own(self) -> None:
        """The rule the size heuristic used to break: bigger is not better."""
        src = self.movie("final/Film (2020)", 400 * 1024 * 1024)
        dest = self.movie("Movies/Film (2020)", 1024 * 1024)
        with mock.patch.object(ms, "find_ffprobe", return_value="ffprobe"), \
                mock.patch.object(ms, "probe_media", return_value=(info(), "")):
            allowed, reason = ms.should_replace(src, dest)
        self.assertFalse(allowed)
        self.assertIn("no clear technical upgrade", reason)

    def test_a_different_movie_is_a_conflict_and_is_never_probed(self) -> None:
        src = self.movie("final/Other Film (2019)")
        dest = self.movie("Movies/Film (2020)")
        with mock.patch.object(ms, "probe_media") as probe:
            allowed, reason = ms.should_replace(src, dest)
        self.assertFalse(allowed)
        self.assertIn("title/year identities differ", reason)
        probe.assert_not_called()

    def test_an_alternate_cut_is_refused_by_name_alone(self) -> None:
        src = self.movie("final/Film (2020) {edition-Extended Cut}")
        dest = self.movie("Movies/Film (2020)")
        with mock.patch.object(ms, "probe_media") as probe:
            allowed, reason = ms.should_replace(src, dest)
        self.assertFalse(allowed)
        self.assertIn("alternate-cut/version marker", reason)
        probe.assert_not_called()

    def test_without_ffprobe_the_library_keeps_what_it_has(self) -> None:
        """Fail-closed: no probe, no replacement - whatever the sizes say."""
        src = self.movie("final/Film (2020)", 400 * 1024 * 1024)
        dest = self.movie("Movies/Film (2020)", 1024 * 1024)
        with mock.patch.object(ms, "find_ffprobe", return_value=None):
            allowed, reason = ms.should_replace(src, dest)
        self.assertFalse(allowed)
        self.assertIn("ffprobe unavailable", reason)
        self.assertIn("size alone never replaces", reason)

    def test_an_unreadable_incoming_file_keeps_the_existing_movie(self) -> None:
        src = self.movie("final/Film (2020)")
        dest = self.movie("Movies/Film (2020)")
        probes = [(None, "ffprobe rejected Film (2020).mkv: moov atom not found"),
                  (info(), "")]
        with mock.patch.object(ms, "find_ffprobe", return_value="ffprobe"), \
                mock.patch.object(ms, "probe_media", side_effect=probes):
            allowed, reason = ms.should_replace(src, dest)
        self.assertFalse(allowed)
        self.assertIn("moov atom not found", reason)
        self.assertIn("keeping existing movie", reason)

    def test_an_unreadable_existing_file_is_also_kept(self) -> None:
        """A library file ffprobe cannot read is a problem to report, not overwrite."""
        src = self.movie("final/Film (2020)")
        dest = self.movie("Movies/Film (2020)")
        probes = [(info(width=3840, height=2160), ""),
                  (None, "ffprobe rejected Film (2020).mkv: truncated")]
        with mock.patch.object(ms, "find_ffprobe", return_value="ffprobe"), \
                mock.patch.object(ms, "probe_media", side_effect=probes):
            allowed, reason = ms.should_replace(src, dest)
        self.assertFalse(allowed)
        self.assertIn("keeping existing movie", reason)

    def test_a_verified_upgrade_is_allowed(self) -> None:
        src = self.movie("final/Film (2020)")
        dest = self.movie("Movies/Film (2020)")
        probes = [(info(width=3840, height=2160, bit_depth=10, video_codec="hevc"), ""),
                  (info(), "")]
        with mock.patch.object(ms, "find_ffprobe", return_value="ffprobe"), \
                mock.patch.object(ms, "probe_media", side_effect=probes):
            allowed, reason = ms.should_replace(src, dest)
        self.assertTrue(allowed)
        self.assertIn("verified same-cut technical upgrade", reason)


if __name__ == "__main__":
    unittest.main()
