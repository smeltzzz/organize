"""The chain table itself: one hardware chain, classified once, used everywhere.

These tests pin the *facts* the hardware research established (every row has a
source in ``organizekit.core.playbackchain.SOURCES``), and the policies the
tools derive from them. A future hardware change lands here first: edit the
table and let the failures name every tool assumption that moved with it.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from organizekit.core import playbackchain as pc


class DeviceFactTests(unittest.TestCase):
    """The three devices, as the research established them."""

    def test_the_player_is_the_g454v(self) -> None:
        self.assertEqual(pc.PLAYER.model_id, "G454V")
        self.assertIn("S805X2", pc.PLAYER.soc)
        self.assertEqual(pc.PLAYER.max_resolution, (1920, 1080))
        # AV1 decode IS the HD model's distinguishing feature (the 4K model
        # lacks it); its absence here would silently retarget the library.
        self.assertIn("av1", pc.PLAYER.video_codecs)

    def test_the_player_never_emits_lossless_hd_audio(self) -> None:
        for codec in ("truehd", "dts-hd", "dtshd", "mlp"):
            with self.subTest(codec=codec):
                self.assertNotIn(codec, pc.PLAYER.passthrough_audio)
        self.assertIn("ac3", pc.PLAYER.passthrough_audio)
        self.assertIn("eac3", pc.PLAYER.passthrough_audio)

    def test_the_player_has_no_dolby_vision_license(self) -> None:
        # The HD model decodes HDR10/HDR10+/HLG only. Calibrated against the
        # 4K model, which does carry Dolby Vision.
        self.assertFalse(pc.PLAYER.dolby_vision)
        self.assertIn("HDR10", pc.PLAYER.hdr_formats)
        self.assertIn("HLG", pc.PLAYER.hdr_formats)

    def test_the_soundbar_decodes_everything_the_player_emits(self) -> None:
        joined = " ".join(pc.SINK.decoders).lower()
        for needed in ("dolby digital", "dolby atmos", "dts", "multichannel pcm"):
            with self.subTest(needed=needed):
                self.assertIn(needed, joined)
        # The upgrade wiring (soundbar-hdmi-in) plugs the Chromecast into
        # THIS port, not into the TV.
        self.assertTrue(any("hdmi in" in p.lower() for p in pc.SINK.ports))

    def test_the_display_is_the_2013_sdr_panel(self) -> None:
        self.assertEqual(pc.DISPLAY.resolution, (1920, 1080))
        self.assertFalse(pc.DISPLAY.hdr)

    def test_every_fact_is_sourced(self) -> None:
        """Every row cites a manufacturer page, a review, or a reading of the
        actual unit. The last kind is not a URL, so both forms are allowed —
        what may not happen is an uncited claim."""
        self.assertGreaterEqual(len(pc.SOURCES), 5)
        for source in pc.SOURCES:
            with self.subTest(source=source[:60]):
                self.assertTrue(source.startswith("http") or source.startswith("USER-CONFIRMED"),
                                source)
        # The PCM-only reading that drives the default wiring is recorded as
        # what it is — an observation on the actual unit, not a datasheet.
        self.assertTrue(any(s.startswith("USER-CONFIRMED") and "PCM" in s
                            for s in pc.SOURCES))


class AudioClassificationTests(unittest.TestCase):
    """Every codec string a real library carries lands in one class."""

    NATIVE = (
        "AC-3", "A_AC3", "ac3", "E-AC-3", "EAC3", "A_EAC3", "eac3",
        "E-AC-3 JOC Atmos", "Dolby Digital Plus", "Dolby Digital",
    )
    DTS_CORE = ("DTS", "A_DTS", "dts", "DTS Core 5.1")
    PCM_DECODE = (
        "AAC", "A_AAC", "aac", "FLAC", "A_FLAC", "MP3", "Opus", "Vorbis",
        "PCM", "A_PCM/INT/BIG", "ALAC", "WAV",
    )
    BOUND = (
        "TrueHD", "A_TRUEHD", "TRUEHD A_MLP", "TrueHD Atmos",
        "DTS-HD MA", "DTS-HD Master Audio", "DTS-HD High Resolution Audio",
        "DTS-HD HRA", "DTS:X", "DTS-X", "A_DTS/LOSSLESS", "WMAPRO", "WMA Pro",
    )

    def test_native_family(self) -> None:
        for blob in self.NATIVE:
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_NATIVE)
                self.assertTrue(pc.is_chain_native(blob))

    def test_dts_core_family(self) -> None:
        for blob in self.DTS_CORE:
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_DTS_CORE)
                self.assertTrue(pc.is_chain_native(blob))

    def test_decode_to_pcm_family(self) -> None:
        for blob in self.PCM_DECODE:
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_DECODE_PCM)
                self.assertTrue(pc.is_chain_native(blob))

    def test_transcode_bound_family(self) -> None:
        for blob in self.BOUND:
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_TRANSCODE_BOUND)
                self.assertFalse(pc.is_chain_native(blob))

    def test_dts_hd_is_never_confused_with_dts_core(self) -> None:
        # Substring traps: "DTS-HD MA" contains "DTS"; the HD family must
        # match first, every time.
        for blob in ("DTS-HD MA", "DTS-HD HRA", "DTS:X", "DTS-HD Master Audio 7.1"):
            with self.subTest(blob=blob):
                self.assertNotEqual(pc.classify_audio_blob(blob), pc.AUDIO_DTS_CORE)

    def test_unknown_is_fail_closed(self) -> None:
        for blob in ("", "   ", "gsm_ms", "atrac3", "musepack"):
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_UNKNOWN)
                self.assertFalse(pc.is_chain_native(blob))

    def test_titles_never_demote_a_proven_codec(self) -> None:
        """Real tracks narrate history in their titles; the codec fields rule.

        The audio standardizer itself titles its output "Dolby Digital 5.1
        (from TrueHD)" — reclassifying that file must still see AC-3, and a
        release titled "TrueHD 7.1" on a DTS core track is still core DTS.
        """
        cases = (
            ("AC3  DOLBY DIGITAL 5.1 640K (FROM TRUEHD; G454V CHAIN)", pc.AUDIO_NATIVE),
            ("E-AC-3 A_EAC3 DD+ ATMOS 7.1 TRUEHD MASTER EDITION", pc.AUDIO_NATIVE),
            ("DTS A_DTS DTS-HD MA 7.1 LOSSLESS SURROUND", pc.AUDIO_DTS_CORE),
            ("AAC A_AAC/LC RESYNC FROM TRUEHD 7.1", pc.AUDIO_DECODE_PCM),
            ("DTS-HD MA", pc.AUDIO_TRANSCODE_BOUND),  # human codec label, no ID: title rules
            ("TrueHD Atmos", pc.AUDIO_TRANSCODE_BOUND),
        )
        for blob, wanted in cases:
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), wanted)

    def test_a_title_cannot_schedule_a_native_stream_for_transcode(self) -> None:
        """The E-AC-3 false positive, pinned at the classifier.

        ``ffprobe`` reports an empty (or "unknown") profile for E-AC-3
        streams, so the *title* was previously the second whitespace token.
        Titles like "TrueHD 7.1" or "DTS-HD MA 7.1" then turned a real
        ``codec_name=eac3`` stream into a transcode candidate — one ffmpeg
        run per movie that never needed one.
        """
        cases = (
            # (codec_name, profile, title, wanted)
            ("eac3", "unknown", "TrueHD 7.1", pc.AUDIO_NATIVE),
            ("eac3", "", "TrueHD 7.1", pc.AUDIO_NATIVE),
            ("eac3", "", "DTS-HD MA 7.1", pc.AUDIO_NATIVE),
            ("eac3", "", "DTS:X 7.1 (from DTS-HD MA)", pc.AUDIO_NATIVE),
            ("eac3", "Dolby Digital Plus", "TrueHD 7.1 Surround Sound", pc.AUDIO_NATIVE),
            ("ac3", "", "TrueHD 7.1", pc.AUDIO_NATIVE),
            ("ac3", "unknown", "DTS-HD MA 7.1", pc.AUDIO_NATIVE),
            ("aac", "", "TrueHD 7.1", pc.AUDIO_DECODE_PCM),
            ("mp3", "", "DTS-HD MA 7.1", pc.AUDIO_DECODE_PCM),
            # A DTS core track whose title narrates a TrueHD source stays core.
            ("dts", "", "TrueHD 7.1", pc.AUDIO_DTS_CORE),
            # ... and a DTS profile that really IS HD still wins.
            ("dts", "DTS-HD MA", "Dolby Digital 5.1 (from DTS-HD MA)", pc.AUDIO_TRANSCODE_BOUND),
        )
        for codec, profile, title, wanted in cases:
            with self.subTest(codec=codec, profile=profile, title=title):
                # The ffprobe fields alone (title-free) must decide.
                self.assertEqual(pc.classify_audio_ffprobe(codec, profile), wanted,
                                 f"{codec}/{profile} must classify on its codec fields")
                # The same, through the shape audio_standardizer builds: the
                # title is present but occupies the field after the profile.
                blob = f"{codec} {profile or '-'} {title}".upper()
                self.assertEqual(pc.classify_audio_blob(blob), wanted)
        # A title may not *upgrade* a core track either: with the profile slot
        # rendered ("DTS - DTS-HD MA 7.1") the title is past field 2 and is
        # ignored, while a blob whose field 2 IS the HD profile is upgraded.
        self.assertEqual(pc.classify_audio_blob("DTS - DTS-HD MA 7.1"), pc.AUDIO_DTS_CORE)
        self.assertEqual(pc.classify_audio_blob("DTS DTS-HD MA 7.1"),
                         pc.AUDIO_TRANSCODE_BOUND)

    def test_dts_hd_profiles_still_upgrade_a_bare_dts_core(self) -> None:
        for profile in ("DTS-HD MA", "DTS-HD HRA", "DTS:X", "DTS 96/24 "):
            wanted = (pc.AUDIO_TRANSCODE_BOUND if "DTS-HD" in profile or "DTS:X" in profile
                      else pc.AUDIO_DTS_CORE)
            with self.subTest(profile=profile):
                self.assertEqual(pc.classify_audio_ffprobe("dts", profile), wanted)
        # A codec ID carries the answer itself; the title cannot refine it.
        self.assertEqual(pc.classify_audio_blob("A_DTS/HD_MA DTS-HD"), pc.AUDIO_TRANSCODE_BOUND)
        self.assertEqual(pc.classify_audio_blob("DTS A_DTS DTS-HD MA"), pc.AUDIO_DTS_CORE)

    def test_tiers_implement_the_philosophy_native_above_lossless(self) -> None:
        self.assertGreater(pc.tier_for_blob("E-AC-3"), pc.tier_for_blob("TrueHD"))
        self.assertGreater(pc.tier_for_blob("AC-3"), pc.tier_for_blob("DTS-HD MA"))
        self.assertGreater(pc.tier_for_blob("DTS"), pc.tier_for_blob("DTS:X"))
        self.assertGreater(pc.tier_for_blob("AAC"), pc.tier_for_blob("TRUEHD"))

    def test_ffprobe_fields_classify_identically(self) -> None:
        self.assertEqual(pc.classify_audio_ffprobe("truehd"), pc.AUDIO_TRANSCODE_BOUND)
        self.assertEqual(pc.classify_audio_ffprobe("dts", "DTS-HD MA"), pc.AUDIO_TRANSCODE_BOUND)
        self.assertEqual(pc.classify_audio_ffprobe("dts", ""), pc.AUDIO_DTS_CORE)
        self.assertEqual(pc.classify_audio_ffprobe("eac3"), pc.AUDIO_NATIVE)
        self.assertEqual(pc.classify_audio_ffprobe("aac", "HE-AAC"), pc.AUDIO_DECODE_PCM)


class TargetAudioTests(unittest.TestCase):
    """What gets synthesized, per source channel count."""

    def test_surround_sources_normalize_to_51_at_the_ac3_ceiling(self) -> None:
        for ch in (6, 7, 8, 9):
            with self.subTest(ch=ch):
                target = pc.target_audio_for(ch)
                self.assertEqual(target.codec, "ac3")
                self.assertEqual(target.channels, 6)
                self.assertEqual(target.bitrate, "640k")
                self.assertEqual(target.sample_rate, 48000)

    def test_stereo_and_mono_keep_their_layout(self) -> None:
        self.assertEqual(pc.target_audio_for(2).channels, 2)
        self.assertEqual(pc.target_audio_for(2).bitrate, "192k")
        self.assertEqual(pc.target_audio_for(1).channels, 1)


class VideoClassificationTests(unittest.TestCase):
    def test_native_codecs_at_1080p_direct_play(self) -> None:
        for codec in ("h264", "hevc", "vp9", "av1", "mpeg2video"):
            with self.subTest(codec=codec):
                self.assertEqual(
                    pc.classify_video(codec, width=1920, height=1080),
                    pc.VIDEO_NATIVE,
                )

    def test_hdr10_family_direct_plays_tonemapped(self) -> None:
        for flavors in (["HDR10"], ["HDR10+"], ["HLG"]):
            with self.subTest(flavors=flavors):
                self.assertEqual(
                    pc.classify_video("hevc", width=1920, height=1080, hdr_flavors=flavors),
                    pc.VIDEO_TONEMAPPED,
                )

    def test_dolby_vision_is_flagged_not_assumed_playable(self) -> None:
        self.assertEqual(
            pc.classify_video("hevc", width=1920, height=1080, dv_profile="8.1"),
            pc.VIDEO_DV_FLAG,
        )
        self.assertEqual(
            pc.classify_video("hevc", width=1920, height=1080, hdr_flavors=["Dolby Vision"]),
            pc.VIDEO_DV_FLAG,
        )

    def test_oversize_beats_hdr_and_codec(self) -> None:
        self.assertEqual(
            pc.classify_video("hevc", width=3840, height=2160, hdr_flavors=["HDR10"]),
            pc.VIDEO_OVERSIZE,
        )
        self.assertEqual(
            pc.classify_video("av1", width=3840, height=2160),
            pc.VIDEO_OVERSIZE,
        )

    def test_unknown_codecs_do_not_direct_play(self) -> None:
        for codec in ("vc1", "prores", "wmv3", "msmpeg4v3"):
            with self.subTest(codec=codec):
                self.assertEqual(
                    pc.classify_video(codec, width=1920, height=1080),
                    pc.VIDEO_UNSUPPORTED,
                )

    def test_every_verdict_has_an_explanation(self) -> None:
        for verdict in (pc.VIDEO_NATIVE, pc.VIDEO_TONEMAPPED, pc.VIDEO_DV_FLAG,
                        pc.VIDEO_OVERSIZE, pc.VIDEO_UNSUPPORTED, pc.VIDEO_UNKNOWN):
            with self.subTest(verdict=verdict):
                self.assertTrue(pc.video_chain_note(verdict))


class WiringTests(unittest.TestCase):
    """`soundbar-hdmi-in` is the default; `tv-arc` is the explicit alternative."""

    def setUp(self) -> None:
        # No ambient override may leak into the "no flag, no env" assertions.
        saved = os.environ.pop(pc.WIRING_ENV_VAR, None)
        self.addCleanup(self._restore_env, saved)

    @staticmethod
    def _restore_env(saved: str | None) -> None:
        os.environ.pop(pc.WIRING_ENV_VAR, None)
        if saved is not None:
            os.environ[pc.WIRING_ENV_VAR] = saved

    def test_the_chain_as_cabled_is_the_default_with_no_flag_or_env(self) -> None:
        # The Chromecast feeds the soundbar's HDMI IN and the bar passes the
        # picture through to the TV: this is the actual wiring, and the
        # default every tool must assume when nothing overrides it.
        self.assertEqual(pc.DEFAULT_WIRING, pc.WIRING_SOUNDBAR_HDMI_IN)
        self.assertEqual(pc.WIRING_ENV_VAR, "ORGANIZE_PLAYBACK_WIRING")
        self.assertNotIn(pc.WIRING_ENV_VAR, os.environ)
        self.assertEqual(pc.resolve_wiring(None), pc.WIRING_SOUNDBAR_HDMI_IN)
        self.assertEqual(pc.resolve_wiring(""), pc.WIRING_SOUNDBAR_HDMI_IN)

    def test_the_environment_variable_selects_the_arc_alternative(self) -> None:
        with mock.patch.dict(os.environ, {pc.WIRING_ENV_VAR: pc.WIRING_TV_ARC}):
            self.assertEqual(pc.resolve_wiring(None), pc.WIRING_TV_ARC)
        # ... and an explicit flag still beats the environment.
        with mock.patch.dict(os.environ, {pc.WIRING_ENV_VAR: pc.WIRING_TV_ARC}):
            self.assertEqual(pc.resolve_wiring(pc.WIRING_SOUNDBAR_HDMI_IN),
                             pc.WIRING_SOUNDBAR_HDMI_IN)

    def test_wiring_resolution_accepts_only_known_modes(self) -> None:
        self.assertEqual(pc.resolve_wiring(pc.WIRING_TV_ARC), pc.WIRING_TV_ARC)
        self.assertEqual(pc.resolve_wiring(pc.WIRING_SOUNDBAR_HDMI_IN),
                         pc.WIRING_SOUNDBAR_HDMI_IN)
        self.assertEqual(pc.resolve_wiring("nonsense"), pc.DEFAULT_WIRING)
        with mock.patch.dict(os.environ, {pc.WIRING_ENV_VAR: "nonsense"}):
            self.assertEqual(pc.resolve_wiring(None), pc.DEFAULT_WIRING)

    def test_multichannel_pcm_is_native_by_default_and_stereo_only_over_arc(self) -> None:
        note_default = pc.audio_chain_note("FLAC", 6)
        note_arc = pc.audio_chain_note("FLAC", 6, pc.WIRING_TV_ARC)
        self.assertIn("multichannel", note_default.lower())
        self.assertNotIn("2.0", note_default)
        self.assertIn("2.0", note_arc)
        self.assertIn("ARC", note_arc)

    def test_dts_core_carries_the_arc_warning(self) -> None:
        self.assertIn("DTS", pc.audio_chain_note("DTS", 6, pc.WIRING_TV_ARC))
        # On the default wiring DTS core is simply accepted.
        self.assertNotIn("cannot be relied on", pc.audio_chain_note("DTS", 6))


class SummaryTests(unittest.TestCase):
    def test_the_chain_summary_names_all_three_devices(self) -> None:
        text = "\n".join(pc.chain_summary_lines())
        self.assertIn("G454V", text)
        self.assertIn("AX3125H", text)
        self.assertIn("UN60F6350AF", text)

    def test_the_chain_summary_states_the_default_wiring(self) -> None:
        default_text = "\n".join(pc.chain_summary_lines())
        self.assertIn("HDMI IN", default_text)
        self.assertIn("default", default_text)
        arc_text = "\n".join(pc.chain_summary_lines(pc.WIRING_TV_ARC))
        self.assertIn("ARC", arc_text)
        self.assertIn("alternative", arc_text)


if __name__ == "__main__":
    unittest.main()
