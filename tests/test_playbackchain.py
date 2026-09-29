"""The chain table itself: one hardware chain, classified once, used everywhere.

These tests pin the *facts* the hardware research established (every row has a
source in ``organizekit.core.playbackchain.SOURCES``), and the policies the
tools derive from them. A future hardware change lands here first: edit the
table and let the failures name every tool assumption that moved with it.
"""

from __future__ import annotations

import unittest

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
        self.assertGreaterEqual(len(pc.SOURCES), 5)
        for source in pc.SOURCES:
            self.assertTrue(source.startswith("http"), source)


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
    def test_the_shipped_arc_wiring_is_the_default(self) -> None:
        # The soundbar hangs off the TV's HDMI (ARC) port in this setup.
        self.assertEqual(pc.DEFAULT_WIRING, pc.WIRING_TV_ARC)
        self.assertEqual(pc.resolve_wiring(None), pc.WIRING_TV_ARC)
        self.assertNotEqual(pc.resolve_wiring("nonsense"), pc.WIRING_SOUNDBAR_HDMI_IN)

    def test_wiring_resolution_accepts_only_known_modes(self) -> None:
        self.assertEqual(pc.resolve_wiring(pc.WIRING_TV_ARC), pc.WIRING_TV_ARC)
        self.assertEqual(pc.resolve_wiring("nonsense"), pc.DEFAULT_WIRING)

    def test_multichannel_pcm_is_stereo_only_over_arc(self) -> None:
        note_hdmi = pc.audio_chain_note("FLAC", 6, pc.WIRING_SOUNDBAR_HDMI_IN)
        note_arc = pc.audio_chain_note("FLAC", 6, pc.WIRING_TV_ARC)
        self.assertIn("multichannel", note_hdmi.lower())
        self.assertIn("2.0", note_arc)

    def test_dts_core_carries_the_arc_warning(self) -> None:
        self.assertIn("DTS", pc.audio_chain_note("DTS", 6, pc.WIRING_TV_ARC))


class SummaryTests(unittest.TestCase):
    def test_the_chain_summary_names_all_three_devices(self) -> None:
        text = "\n".join(pc.chain_summary_lines())
        self.assertIn("G454V", text)
        self.assertIn("AX3125H", text)
        self.assertIn("UN60F6350AF", text)


if __name__ == "__main__":
    unittest.main()
