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

    def test_the_player_extracts_the_dts_core_from_dts_hd(self) -> None:
        """User-confirmed 2026-10 on the actual chain: the HD layer is lost,
        the audio is not. The player falls back to the backward-compatible
        DTS core inside DTS-HD MA/HRA and DTS:X and bitstreams THAT, so these
        tracks Direct Play (the bar reads DTS) while TrueHD stays bound."""
        self.assertTrue(pc.PLAYER.dts_hd_core_fallback)
        self.assertTrue(any("G454V" in source and "DTS-HD" in source
                            for source in pc.SOURCES if source.startswith("USER-CONFIRMED")))
        for blob in ("DTS-HD MA", "DTS-HD HRA", "DTS:X", "A_DTS/HD_MA", "A_DTS/LOSSLESS"):
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_DTS_HD_CORE)
        # ... and the variants with no core stay bound.
        for blob in ("DTS-HD LBR", "DTS Express", "A_DTS/EXPRESS"):
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_TRANSCODE_BOUND)

    def test_truehd_and_mat_have_explicit_app_and_transport_status(self) -> None:
        # TrueHD cannot be bitstreamed by this model, although some apps can
        # decode it to PCM; plain PCM does not retain its Atmos object layer.
        self.assertFalse(pc.PLAYER.truehd_bitstream_passthrough)
        self.assertTrue(pc.PLAYER.truehd_app_pcm_decode_possible)
        self.assertFalse(pc.PLAYER.truehd_atmos_survives_plain_pcm)
        # The bar's manual display map is not evidence that this Chromecast
        # outputs MAT; the model keeps the output status unverified.
        self.assertEqual(pc.SINK.dolby_mat_manual_display, "MPCM")
        self.assertEqual(pc.SINK.dolby_mat_atmos_manual_display, "DOLBY ATMOS")
        self.assertFalse(pc.PLAYER.dolby_mat_output_verified)

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

    def test_the_per_port_matrix_is_what_makes_the_default_wiring_default(self) -> None:
        """Hisense manual §1.3, read as data: the evidence for `soundbar-hdmi-in`.

        The bar's supported-input table is per PORT. It confirms HDMI IN
        accepts multichannel LPCM, but an app must first decode a 5.1/7.1
        AAC/FLAC/PCM source and the output layout is app/route-dependent. LPCM
        5.1/7.1 is NOT accepted on HDMI ARC or optical.
        """
        for layout in ("LPCM 5.1ch", "LPCM 7.1ch"):
            with self.subTest(layout=layout):
                self.assertIn(layout, pc.SINK.hdmi_in_accepts)
                self.assertIn(layout, pc.SINK.arc_cannot_carry)
        # DD+ JOC is the officially listed Atmos passthrough path used by this
        # toolkit. Dolby MAT output is unverified and is not promoted here.
        self.assertIn("Dolby Atmos - Dolby Digital Plus", pc.SINK.hdmi_in_accepts)
        self.assertNotIn("Dolby Atmos - Dolby Digital Plus", pc.SINK.arc_cannot_carry)
        # Everything the player can actually emit, the bar accepts on HDMI IN.
        for emitted in ("Dolby Digital", "Dolby Digital Plus", "DTS", "LPCM 2ch"):
            with self.subTest(emitted=emitted):
                self.assertIn(emitted, pc.SINK.hdmi_in_accepts)
        # The load-bearing invariant: re-cabling through HDMI IN recovers every
        # format the ARC/optical path refuses. If one of these were refused on
        # BOTH paths, the default wiring would not actually be the better one.
        for fmt in pc.SINK.arc_cannot_carry:
            with self.subTest(fmt=fmt):
                self.assertIn(fmt, pc.SINK.hdmi_in_accepts)

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
                self.assertTrue(source.startswith(("http", "USER-CONFIRMED")), source)
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
        "PCM", "A_PCM/INT/BIG", "WAV",
    )
    #: Lossless, and NOT playable by this player: no decoder in the Android TV
    #: media framework, no bitstream path either (unlike DTS-HD, which has its
    #: core). They must fail closed, and "WAVPACK" must never be read as "WAV".
    NO_PLATFORM_DECODER = ("ALAC", "A_ALAC", "WAVPACK", "A_WAVPACK",
                           "ALAC Apple Lossless 5.1", "WavPack 24-bit 48 kHz")
    DTS_HD_CORE = (
        "DTS-HD MA", "DTS-HD Master Audio", "DTS-HD High Resolution Audio",
        "DTS-HD HRA", "DTS:X", "DTS-X", "A_DTS/LOSSLESS", "DTS DTS-HD MA 7.1",
    )
    BOUND = (
        "TrueHD", "A_TRUEHD", "TRUEHD A_MLP", "TrueHD Atmos",
        "DTS-HD LBR", "DTS Express", "A_DTS/EXPRESS", "WMAPRO", "WMA Pro",
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

    def test_dts_hd_family_plays_via_the_extracted_core(self) -> None:
        for blob in self.DTS_HD_CORE:
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_DTS_HD_CORE)
                self.assertTrue(pc.is_chain_native(blob))
                # Delivered audio is a DTS core either way, so it shares base
                # DTS's band and tier.
                self.assertEqual(pc.chain_band_for(pc.AUDIO_DTS_HD_CORE),
                                 pc.chain_band_for(pc.AUDIO_DTS_CORE))
                self.assertEqual(pc.CLASS_TIERS[pc.AUDIO_DTS_HD_CORE],
                                 pc.CLASS_TIERS[pc.AUDIO_DTS_CORE])

    def test_transcode_bound_family(self) -> None:
        for blob in self.BOUND:
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_TRANSCODE_BOUND)
                self.assertFalse(pc.is_chain_native(blob))

    def test_dts_hd_is_never_confused_with_dts_core(self) -> None:
        # Substring traps: "DTS-HD MA" contains "DTS"; the HD family must
        # match first, every time - it is its own class, not core DTS (which
        # would hide that the HD layer is lost) and not transcode-bound
        # (which would schedule a pointless conversion).
        for blob in ("DTS-HD MA", "DTS-HD HRA", "DTS:X", "DTS-HD Master Audio 7.1"):
            with self.subTest(blob=blob):
                self.assertNotEqual(pc.classify_audio_blob(blob), pc.AUDIO_DTS_CORE)
                self.assertNotEqual(pc.classify_audio_blob(blob), pc.AUDIO_TRANSCODE_BOUND)

    def test_unknown_is_fail_closed(self) -> None:
        for blob in ("", "   ", "gsm_ms", "atrac3", "musepack"):
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_UNKNOWN)
                self.assertFalse(pc.is_chain_native(blob))

    def test_codecs_with_no_decoder_here_are_unknown_not_decoded(self) -> None:
        """ALAC and WavPack look like the FLAC family and are not in it.

        Crediting them with "decode-to-pcm" promised a Direct Play the chain
        cannot deliver (Android TV has no ALAC decoder; Jellyfin's own codec
        table says so), and the lossless sub-tier then ranked them ABOVE the
        codecs that do decode - which is how a track nobody can play ends up
        as the one track an irreversible remux keeps.
        """
        for blob in self.NO_PLATFORM_DECODER:
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_UNKNOWN)
                self.assertFalse(pc.is_chain_native(blob))
                self.assertEqual(pc.chain_band_for(pc.AUDIO_UNKNOWN), pc.CHAIN_BAND_UNKNOWN)
                self.assertEqual(pc.achievable_channels(pc.AUDIO_UNKNOWN, 8), 0)
        # The predicate is shared with the cleaner's tier table, and the
        # substring is what makes it necessary: "WAVPACK" contains "WAV".
        self.assertTrue(pc.has_no_platform_decoder("wavpack"))
        self.assertFalse(pc.has_no_platform_decoder("WAV"))
        self.assertFalse(pc.has_no_platform_decoder("FLAC"))

    def test_a_title_cannot_rescue_a_codec_with_no_decoder(self) -> None:
        # Field 1 decides, in both directions: a title narrating an HD source
        # may not promote an ALAC stream into the DTS family, exactly as it
        # may not demote a real E-AC-3 into TrueHD.
        self.assertEqual(pc.classify_audio_blob("ALAC - FROM DTS-HD MA 7.1"), pc.AUDIO_UNKNOWN)
        self.assertEqual(pc.classify_audio_blob("WAVPACK A_WAVPACK DTS:X"), pc.AUDIO_UNKNOWN)

    def test_dolby_mat_is_unknown_not_a_native_pcm_codec(self) -> None:
        """MAT is an HDMI transport; the G454V output path is unverified.

        In particular, the AX3125H manual's plain-MAT display result "MPCM"
        must not make Dolby MAT look like an ordinary decoded PCM track.
        """
        for blob in ("DOLBY MAT", "DOLBY MAT-ATMOS", "DOLBY_MAT", "MAT 2.0",
                     "MAT 2.1", "MPCM", "MPCM 5.1", "DOLBY MAT MPCM"):
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_UNKNOWN)
                note = pc.audio_chain_note(blob, 8)
                self.assertIn("not verified", note)
                self.assertIn("unknown", note)
        self.assertEqual(pc.classify_audio_blob("PCM"), pc.AUDIO_DECODE_PCM)
        # A title never overrides the real PCM codec name.
        self.assertEqual(pc.classify_audio_blob("PCM - Dolby MAT"), pc.AUDIO_DECODE_PCM)

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
            ("DTS-HD MA", pc.AUDIO_DTS_HD_CORE),  # human codec label, no ID: title rules
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
            ("dts", "DTS-HD MA", "Dolby Digital 5.1 (from DTS-HD MA)", pc.AUDIO_DTS_HD_CORE),
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
                         pc.AUDIO_DTS_HD_CORE)

    def test_dts_hd_profiles_still_upgrade_a_bare_dts_core(self) -> None:
        for profile in ("DTS-HD MA", "DTS-HD HRA", "DTS:X", "DTS 96/24 "):
            wanted = (pc.AUDIO_DTS_HD_CORE if "DTS-HD" in profile or "DTS:X" in profile
                      else pc.AUDIO_DTS_CORE)
            with self.subTest(profile=profile):
                self.assertEqual(pc.classify_audio_ffprobe("dts", profile), wanted)
        # The no-core members of the family are spoken as two words, and the
        # marker is in the second one: the pair has to be read.
        for profile in ("DTS Express", "DTS-HD LBR", "LBR"):
            with self.subTest(profile=profile):
                self.assertEqual(pc.classify_audio_ffprobe("dts", profile),
                                 pc.AUDIO_TRANSCODE_BOUND)
        # A codec ID carries the answer itself; the title cannot refine it.
        self.assertEqual(pc.classify_audio_blob("A_DTS/HD_MA DTS-HD"), pc.AUDIO_DTS_HD_CORE)
        self.assertEqual(pc.classify_audio_blob("A_DTS/HD_HRA HRA"), pc.AUDIO_DTS_HD_CORE)
        self.assertEqual(pc.classify_audio_blob("DTS A_DTS DTS-HD MA"), pc.AUDIO_DTS_CORE)

    def test_tiers_implement_the_philosophy_native_above_emittable(self) -> None:
        self.assertGreater(pc.tier_for_blob("E-AC-3"), pc.tier_for_blob("TrueHD"))
        self.assertGreater(pc.tier_for_blob("AC-3"), pc.tier_for_blob("TrueHD"))
        # The DTS-HD family is not "lossless-HD" in the ranking any more: the
        # player bits a DTS core out of it, so it ranks exactly with base DTS
        # and still above every format that needs a server transcode.
        self.assertEqual(pc.tier_for_blob("DTS"), pc.tier_for_blob("DTS:X"))
        self.assertEqual(pc.tier_for_blob("DTS"), pc.tier_for_blob("DTS-HD MA"))
        self.assertGreater(pc.tier_for_blob("DTS-HD MA"), pc.tier_for_blob("TrueHD"))
        self.assertGreater(pc.tier_for_blob("AAC"), pc.tier_for_blob("TRUEHD"))

    def test_ffprobe_fields_classify_identically(self) -> None:
        self.assertEqual(pc.classify_audio_ffprobe("truehd"), pc.AUDIO_TRANSCODE_BOUND)
        self.assertEqual(pc.classify_audio_ffprobe("dts", "DTS-HD MA"), pc.AUDIO_DTS_HD_CORE)
        self.assertEqual(pc.classify_audio_ffprobe("dts", "DTS-HD HRA"), pc.AUDIO_DTS_HD_CORE)
        self.assertEqual(pc.classify_audio_ffprobe("dts", "DTS Express"), pc.AUDIO_TRANSCODE_BOUND)
        self.assertEqual(pc.classify_audio_ffprobe("dts", ""), pc.AUDIO_DTS_CORE)
        self.assertEqual(pc.classify_audio_ffprobe("eac3"), pc.AUDIO_NATIVE)
        self.assertEqual(pc.classify_audio_ffprobe("aac", "HE-AAC"), pc.AUDIO_DECODE_PCM)


class TargetAudioTests(unittest.TestCase):
    """What gets synthesized, per source channel count and wiring.

    The default ``soundbar-hdmi-in`` wiring (Chromecast -> AX3125H HDMI IN
    -> TV) targets Dolby Digital Plus: both of its two audio hops carry it
    natively. What either wiring may NOT target is anything wider than 5.1:
    that is the ceiling of ffmpeg's Dolby encoders, recorded as
    :data:`playbackchain.FFMPEG_DOLBY_ENCODE_MAX_CHANNELS`, because promising
    a 7.1 bitstream made every >=7.1 master's transcode fail outright.
    """

    def test_default_wiring_folds_a_71_master_into_dolby_digital_plus_51(self) -> None:
        # The 8.4.0 regression: this branch once returned channels=8 and
        # ffmpeg's eac3 encoder cannot write 7.1 at all - it fails instead of
        # downmixing, so every >=7.1 lossless master's audiofit produced no
        # file. The layout a track *could* keep if an encoder existed is not
        # a target; the widest target is the widest encode.
        target = pc.target_audio_for(8)
        self.assertEqual(target.codec, "eac3")
        self.assertEqual(target.channels, 6)
        self.assertEqual(target.channel_name, "5.1")
        self.assertEqual(target.bitrate, "640k")
        self.assertEqual(target.sample_rate, 48000)

    def test_no_target_exceeds_the_encoder_ceiling_on_any_wiring(self) -> None:
        # Every source layout from mono to 7.1, on both wirings: whatever the
        # table answers, some real encoder must be able to answer it back.
        for wiring in (pc.WIRING_SOUNDBAR_HDMI_IN, pc.WIRING_TV_ARC):
            for ch in range(1, 9):
                with self.subTest(wiring=wiring, ch=ch):
                    target = pc.target_audio_for(ch, wiring)
                    self.assertLessEqual(
                        target.channels, pc.FFMPEG_DOLBY_ENCODE_MAX_CHANNELS,
                        f"{wiring} promised {target.channels} channels to a "
                        f"{ch}-channel source; the encoders cap at "
                        f"{pc.FFMPEG_DOLBY_ENCODE_MAX_CHANNELS}")
                    if ch >= 5:
                        # The cap is not "at most 6" on the wide end - the
                        # fold lands exactly on 5.1, never a lopsided bed.
                        self.assertEqual((target.channels, target.channel_name),
                                         (6, "5.1"))

    def test_the_encoder_ceiling_is_recorded_as_data(self) -> None:
        """The number lives in the shared core, not in each caller's prose.

        6 is not a taste: it is ffmpeg's documented 5.1 limit for BOTH Dolby
        encoders (verified against 7.0.2 - ``-h encoder=eac3`` lists no
        layout wider than 5.1, and ``-ac 8`` exits with "Conversion failed!").
        Exporting it from ``organizekit.core`` lets the docs, the tests and
        any tool reason from the same recorded fact instead of re-deriving -
        or re-forgetting - it.
        """
        self.assertEqual(pc.FFMPEG_DOLBY_ENCODE_MAX_CHANNELS, 6)
        from organizekit import core
        self.assertIs(core.FFMPEG_DOLBY_ENCODE_MAX_CHANNELS,
                      pc.FFMPEG_DOLBY_ENCODE_MAX_CHANNELS)
        self.assertIn("FFMPEG_DOLBY_ENCODE_MAX_CHANNELS", core.__all__)

    def test_default_wiring_normalizes_51_and_61_to_51_dolby_digital_plus(self) -> None:
        for ch in (5, 6, 7):
            with self.subTest(ch=ch):
                target = pc.target_audio_for(ch)
                self.assertEqual(target.codec, "eac3")
                self.assertEqual(target.channels, 6)
                self.assertEqual(target.bitrate, "640k")

    def test_default_wiring_stereo_and_mono_keep_their_layout(self) -> None:
        self.assertEqual(pc.target_audio_for(2).codec, "eac3")
        self.assertEqual(pc.target_audio_for(2).channels, 2)
        self.assertEqual(pc.target_audio_for(2).bitrate, "192k")
        self.assertEqual(pc.target_audio_for(1).channels, 1)

    def test_the_tv_arc_alternative_targets_ac3_folded_to_51(self) -> None:
        for ch in (6, 7, 8, 9):
            with self.subTest(ch=ch):
                target = pc.target_audio_for(ch, pc.WIRING_TV_ARC)
                self.assertEqual(target.codec, "ac3")
                self.assertEqual(target.channels, 6)
                self.assertEqual(target.bitrate, "640k")
                self.assertEqual(target.sample_rate, 48000)
        self.assertEqual(pc.target_audio_for(2, pc.WIRING_TV_ARC).bitrate, "192k")
        self.assertEqual(pc.target_audio_for(1, pc.WIRING_TV_ARC).channels, 1)

    def test_zero_or_unparseable_source_channels_defaults_to_stereo(self) -> None:
        """Defensive: a 0-channel or bogus source must NOT promise 5.1.

        ``int("0" or 6)`` used to evaluate to 0, not 6, because the string
        ``"0"`` is truthy in Python; ``ch = max(1, 0)`` then clamped to 1
        and the >=5 branch picked a 5.1 @ 640 kbps target from a track that
        had no audio. Worse, ``int(0 or 6) == 6`` landed on 5.1 for the
        integer 0. A probe payload with channels=0 (an image attachment
        mislabeled as audio, or an exotic codec ffprobe cannot describe)
        must therefore synthesize a conservative stereo bed, not fail the
        Dolby encoder ceiling assertion by targeting 5.1 from silence.
        """
        for bogus in (0, "0", -5, None, "", "bogus"):
            for wiring in (pc.WIRING_SOUNDBAR_HDMI_IN, pc.WIRING_TV_ARC):
                with self.subTest(bogus=bogus, wiring=wiring):
                    target = pc.target_audio_for(bogus, wiring)
                    self.assertEqual(
                        target.channels, 2,
                        f"target_audio_for({bogus!r}) on {wiring} promised "
                        f"{target.channels}ch instead of safe stereo",
                    )

    def test_achievable_channels_is_defensive_against_string_zero(self) -> None:
        """mkvmerge/ffprobe occasionally render channels as the string "0"."""
        for zero in (0, "0", None, "", "bogus"):
            with self.subTest(zero=zero):
                self.assertEqual(
                    pc.achievable_channels(pc.AUDIO_NATIVE, zero), 0,
                    "zero-channel native must achieve zero, not credit audio",
                )
                self.assertEqual(
                    pc.achievable_channels(pc.AUDIO_TRANSCODE_BOUND, zero), 0,
                    "zero-channel transcode source must achieve zero",
                )
                self.assertEqual(
                    pc.achievable_channels(pc.AUDIO_DTS_HD_CORE, zero), 0,
                    "zero-channel DTS-HD must achieve zero, not credit a core",
                )

    def test_a_dts_hd_master_reaches_its_cores_layout_not_its_own(self) -> None:
        """The HD layer never leaves the box; the extracted DTS core tops at 5.1.

        A DTS-HD MA 7.1 track therefore achieves 6 channels on this chain -
        exactly like the DTS core it becomes - while a plain 5.1 track of the
        family achieves its own 6 and a 2.0 one stays 2. Nothing is lost
        against the old transcode-bound reading (which also promised 6), but
        the track no longer needs the server to get there.
        """
        self.assertEqual(pc.DTS_CORE_MAX_CHANNELS, 6)
        for channels, wanted in ((8, 6), (7, 6), (6, 6), (2, 2), (1, 1)):
            with self.subTest(channels=channels):
                self.assertEqual(
                    pc.achievable_channels(pc.AUDIO_DTS_HD_CORE, channels), wanted)

    def test_base_dts_is_capped_at_the_same_layout_as_any_dts_core(self) -> None:
        """The cap is a property of the format, not of how well the label was read.

        A DTS Digital Surround bitstream tops out at 5.1 (DTS-ES reaches 6.1 on
        some discs), so a DTS-labelled track claiming eight channels is either a
        DTS-HD/DTS:X stream whose core the player extracts or a wrong label -
        and in both cases it reaches six. Before the cap, the *label* decided:
        a DTS:X 7.1 track that mkvmerge only managed to call "DTS" (its HD
        identity sitting in the track name, which the classifier rightly
        refuses to trust) was credited with achieving 7.1, and "achievable
        layout" is the FIRST key of the keeper ranking. That is how a movie
        could keep the DTS core the bar hears as 5.1 and strip its only
        DD+ 5.1 Atmos track, irreversibly, because a title said 7.1.
        """
        for channels, wanted in ((8, 6), (7, 6), (6, 6), (5, 5), (2, 2), (1, 1)):
            with self.subTest(channels=channels):
                self.assertEqual(
                    pc.achievable_channels(pc.AUDIO_DTS_CORE, channels), wanted)
        # Dolby is NOT capped: E-AC-3 really does carry 7.1 on this chain, and
        # AC-3 never claims more than the core layout anyway.
        self.assertEqual(pc.achievable_channels(pc.AUDIO_NATIVE, 8), 8)
        # The band and the tier are untouched by the cap: a DTS core is still
        # chain-native, and it still cannot outrank Dolby on width.
        atmos = pc.codec_blob("E-AC-3", "A_EAC3", "Atmos 5.1")
        dtsx = pc.codec_blob("DTS", "A_DTS", "DTS:X 7.1")
        self.assertEqual(pc.atmos_credit_for(pc.classify_audio_blob(atmos), atmos), 1)
        self.assertEqual(pc.atmos_credit_for(pc.classify_audio_blob(dtsx), dtsx), 0)
        self.assertEqual(pc.achievable_channels(pc.AUDIO_DTS_CORE, 8),
                         pc.achievable_channels(pc.AUDIO_NATIVE, 6))

    def test_dtsx_spellings_the_separator_can_hide(self) -> None:
        """"DTSX" is what an extractor writes when it flattens the colon."""
        for blob in ("DTSX", "DTS DTSX", "A_DTS/HD_MA", "DTS:X", "DTS-X 7.1",
                     "DTS DTS:X 5.1"):
            with self.subTest(blob=blob):
                self.assertEqual(pc.classify_audio_blob(blob), pc.AUDIO_DTS_HD_CORE)
        # A spaced-out "DTS X" is not chased across a token boundary, because
        # the next token after a one-word profile is the title - and the design
        # rule is that titles never promote. The width cap makes that a
        # mislabel rather than a mistake, which is the whole reason the
        # promotion rule stays strict.
        self.assertEqual(pc.classify_audio_blob("DTS DTS X 7.1"), pc.AUDIO_DTS_CORE)
        self.assertEqual(pc.achievable_channels(pc.AUDIO_DTS_CORE, 8),
                         pc.achievable_channels(pc.AUDIO_DTS_HD_CORE, 8))


class PlayerDecodeCeilingTests(unittest.TestCase):
    """The tool's 24-bit/48-kHz envelope is conservative, not a Google spec.

    Android's generic format table sets 48 kHz for built-in FLAC mono/stereo
    and says support varies by form factor. Multichannel decode is app/route-
    dependent; this boundary decides only what this toolkit safely promises.
    """

    def test_the_ceiling_is_recorded_on_the_player(self) -> None:
        self.assertEqual(pc.PLAYER.max_decoded_sample_rate, 48000)
        self.assertEqual(pc.PLAYER.max_decoded_bit_depth, 24)
        self.assertIn("ALAC", pc.PLAYER.undecodable_codecs)
        self.assertIn("WAVPACK", pc.PLAYER.undecodable_codecs)

    def test_within_the_ceiling_is_silent(self) -> None:
        for rate in (48000, "48000", 44100, 0, None, "", "bogus"):
            with self.subTest(rate=rate):
                self.assertEqual(pc.exceeds_decode_ceiling(rate, 24), "")
        # An unreported depth cannot breach a depth ceiling.
        for depth in (0, None, "", "unknown", 16, 24):
            with self.subTest(depth=depth):
                self.assertEqual(pc.exceeds_decode_ceiling(48000, depth), "")

    def test_past_it_names_the_number_that_is_past(self) -> None:
        self.assertIn("96 kHz", pc.exceeds_decode_ceiling(96000))
        self.assertIn("192 kHz", pc.exceeds_decode_ceiling(192000))
        self.assertIn("32-bit", pc.exceeds_decode_ceiling(48000, 32))
        # A rate is a rate in every shape the probes hand over.
        self.assertIn("192 kHz", pc.exceeds_decode_ceiling("192000.0", None))

    def test_the_note_promises_nothing_past_the_ceiling(self) -> None:
        flac = pc.codec_blob("FLAC", "A_FLAC", "24bit 192kHz")
        over = pc.audio_chain_note(flac, 6, pc.WIRING_SOUNDBAR_HDMI_IN,
                                   sample_rate=192000, bit_depth=24)
        self.assertIn("app/software-decode envelope", over)
        self.assertIn("review", over)
        under = pc.audio_chain_note(flac, 6, pc.WIRING_SOUNDBAR_HDMI_IN,
                                    sample_rate=48000, bit_depth=24)
        self.assertIn("decoded by a compatible player app", under)
        # A bitstreamed Dolby track never touches this decoder, so its sample
        # rate is not this question: 192 kHz is not a reason to doubt a DD+ or
        # a DTS core passthrough.
        for blob in ("E-AC-3 A_EAC3 7.1", "DTS A_DTS 5.1", "TRUEHD A_TRUEHD 7.1"):
            with self.subTest(blob=blob):
                note = pc.audio_chain_note(blob, 8, pc.WIRING_SOUNDBAR_HDMI_IN,
                                           sample_rate=192000, bit_depth=32)
                self.assertNotIn("decode ceiling", note)

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

    def test_the_coded_ceiling_tolerates_macroblock_padding(self) -> None:
        """1080p is routinely STORED as 1920x1088, and that is not oversize.

        1080 is not a multiple of 16, so an H.264 1080p stream carries 8 lines
        of frame_crop_bottom_offset padding; depending on container and muxer,
        ffprobe can report 1080 or 1088 for the same picture. Comparing the
        stored height against 1080 branded ordinary 1080p movies oversize and
        queued a downscale they do not need.
        """
        self.assertEqual(pc.PLAYER.max_resolution, (1920, 1080))
        self.assertEqual(pc.PLAYER.max_coded_resolution, (1920, 1088))
        for codec in ("h264", "hevc", "vp9", "av1"):
            with self.subTest(codec=codec):
                self.assertEqual(
                    pc.classify_video(codec, width=1920, height=1088), pc.VIDEO_NATIVE)
        # The tolerance is one macroblock, not a licence for anything bigger.
        self.assertEqual(pc.classify_video("h264", width=1920, height=1090),
                         pc.VIDEO_OVERSIZE)
        self.assertEqual(pc.classify_video("h264", width=2048, height=858),
                         pc.VIDEO_OVERSIZE)
        self.assertEqual(pc.classify_video("hevc", width=3840, height=2160),
                         pc.VIDEO_OVERSIZE)

    def test_an_unmeasurable_picture_is_never_called_playable(self) -> None:
        """Fail closed on the video side, exactly like AUDIO_UNKNOWN.

        VIDEO_UNKNOWN was defined, exported and explained but unreachable, so
        a stream with no readable dimensions was reported as direct-play.
        """
        for width, height in ((0, 0), (1920, 0), (0, 1080)):
            with self.subTest(size=(width, height)):
                self.assertEqual(pc.classify_video("h264", width=width, height=height),
                                 pc.VIDEO_UNKNOWN)
        # An empty codec name is unknown, not "no hardware decoder": that claim
        # needs a codec to be about.
        self.assertEqual(pc.classify_video("", width=1920, height=1080), pc.VIDEO_UNKNOWN)

    def test_every_video_verdict_is_reachable(self) -> None:
        reachable = {
            pc.classify_video("h264", width=1920, height=1080),
            pc.classify_video("hevc", width=1920, height=1080, hdr_flavors=["HDR10"]),
            pc.classify_video("hevc", width=1920, height=1080, dv_profile="8.1"),
            pc.classify_video("hevc", width=3840, height=2160),
            pc.classify_video("vc1", width=1920, height=1080),
            pc.classify_video("", width=1920, height=1080),
        }
        self.assertEqual(reachable, {pc.VIDEO_NATIVE, pc.VIDEO_TONEMAPPED,
                                     pc.VIDEO_DV_FLAG, pc.VIDEO_OVERSIZE,
                                     pc.VIDEO_UNSUPPORTED, pc.VIDEO_UNKNOWN})

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

    def test_dts_hd_note_says_core_extraction_not_transcode(self) -> None:
        """The note must not promise an HD bitstream, nor a re-encode."""
        for blob in ("DTS-HD MA", "DTS:X", "DTS-HD HRA 7.1"):
            with self.subTest(blob=blob):
                note = pc.audio_chain_note(blob, 8)
                self.assertIn("DTS core", note)
                self.assertNotIn("cannot be relied on", note)
                self.assertNotIn("re-encodes", note)
                # The panel reads DTS; the HD name is what does NOT appear.
                self.assertIn("reads", note)
        arc = pc.audio_chain_note("DTS-HD MA", 8, pc.WIRING_TV_ARC)
        self.assertIn("cannot be relied on", arc)
        self.assertIn("DTS core", arc)
        # TrueHD must never gain the core-fallback promise: it has no
        # backward-compatible core, so it still needs the offline transcode.
        truehd = pc.audio_chain_note("TrueHD", 8)
        self.assertIn("core to fall back to", truehd)
        self.assertIn("app-neutral", truehd)
        self.assertIn("Atmos", truehd)
        self.assertNotIn("every play", truehd)

    def test_native_dolby_51_carries_the_arc_warning_too(self) -> None:
        # AUDIO_NATIVE used to return before ever consulting `wiring`, so AC-3
        # and E-AC-3 printed the identical "bitstreams end-to-end" sentence on
        # the tv-arc alternative. This TV's PCM-only output for HDMI sources
        # establishes the stereo return for app-decoded PCM, but whether it
        # forwards or downmixes a received Dolby bitstream is unmeasured. Keep
        # the route caveat rather than promising surround over ARC.
        for blob in ("AC-3", "EAC3", "eac3 Dolby Digital Plus", "DOLBY DIGITAL"):
            for ch in (6, 8):
                tag = f"{blob} {ch}ch"
                note_default = pc.audio_chain_note(blob, ch)
                note_arc = pc.audio_chain_note(blob, ch, pc.WIRING_TV_ARC)
                self.assertNotEqual(note_default, note_arc, tag)
                self.assertTrue(note_default.startswith("bitstreams end-to-end"), tag)
                self.assertIn("PCM-only", note_arc, tag)
                self.assertIn("unmeasured", note_arc, tag)

    def test_stereo_native_dolby_is_not_warned_about_over_arc(self) -> None:
        # The warning is channel-gated exactly like the decode-to-PCM branch:
        # a stereo AC-3/DD+ track folded down to stereo PCM by the TV loses
        # nothing, so warning about it would be a false alarm.
        for blob in ("AC-3", "EAC3"):
            self.assertEqual(pc.audio_chain_note(blob, 2),
                             pc.audio_chain_note(blob, 2, pc.WIRING_TV_ARC), blob)
            self.assertTrue(
                pc.audio_chain_note(blob, 2, pc.WIRING_TV_ARC)
                .startswith("bitstreams end-to-end"), blob)


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


class BlobFieldTests(unittest.TestCase):
    """The blob is positional, so its field positions are part of the contract."""

    def test_an_absent_field_is_filled_not_collapsed(self) -> None:
        self.assertEqual(pc.codec_blob("DTS", "", "DTS-HD MA 7.1"), "DTS - DTS-HD MA 7.1")
        self.assertEqual(pc.codec_blob("DTS", None, "DTS-HD MA 7.1"), "DTS - DTS-HD MA 7.1")
        self.assertEqual(pc.codec_blob("", "", ""), f"{pc.BLOB_FIELD_ABSENT} " * 2
                         + pc.BLOB_FIELD_ABSENT)

    def test_a_title_can_never_occupy_the_profile_slot(self) -> None:
        for title in ("DTS-HD MA 7.1", "DTS:X 7.1", "DTS-HD High Resolution", "DTS-HD"):
            with self.subTest(title=title):
                self.assertEqual(
                    pc.classify_audio_blob(pc.codec_blob("DTS", "", title)),
                    pc.AUDIO_DTS_CORE)

    def test_a_real_profile_field_still_upgrades_a_bare_dts_name(self) -> None:
        self.assertEqual(pc.classify_audio_blob(pc.codec_blob("DTS", "DTS-HD MA", "")),
                         pc.AUDIO_DTS_HD_CORE)
        self.assertEqual(pc.classify_audio_blob(pc.codec_blob("DTS", "A_DTS/HD_MA", "")),
                         pc.AUDIO_DTS_HD_CORE)
        self.assertEqual(pc.classify_audio_blob(pc.codec_blob("DTS", "A_DTS", "")),
                         pc.AUDIO_DTS_CORE)

    def test_atmos_belongs_to_dolby_digital_plus_only(self) -> None:
        """Plain AC-3 has no Atmos variant, so a title cannot invent one."""
        for blob in ("E-AC-3 A_EAC3 DD+ Atmos", "EAC3 - ", "DOLBY DIGITAL PLUS -"):
            with self.subTest(blob=blob):
                self.assertTrue(pc.is_dolby_digital_plus(blob))
        for blob in ("AC-3 A_AC3 DOLBY ATMOS 5.1", "TRUEHD A_MLP ATMOS", "DTS A_DTS "):
            with self.subTest(blob=blob):
                self.assertFalse(pc.is_dolby_digital_plus(blob))
