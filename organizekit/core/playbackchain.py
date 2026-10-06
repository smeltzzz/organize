"""The one playback chain this toolkit is tuned for, as data.

Everything the tools decide about codecs, resolutions and audio tracks is
grounded in exactly one chain of three physical devices, wired like this::

                                 HDMI (video+audio)            HDMI OUT -> TV
    Chromecast with Google TV ───────────────────────► Hisense ─────────────────────────► Samsung
    (HD), model G454V "boreal"        plug into the   AX3125H  video passes through;    UN60F6350AF
                                      soundbar's      3.1.2ch  audio is decoded here    60" 1080p SDR
                                      HDMI IN         440W

That is the wiring this install actually runs (``soundbar-hdmi-in``), and it
is the toolkit's default: the Chromecast feeds the soundbar's HDMI IN, the
bar decodes the audio, and its HDMI OUT passes the picture through to the TV.
The alternative (``tv-arc``: Chromecast into the TV, TV --ARC/optical--> bar)
is still fully supported, and on this display it is the degraded one — see
``Display.arc`` and docs/hardware.md §3–§4.

This file is the single source of truth for what that chain can do, so
``mkv_track_cleaner.py``, ``bitdepth.py`` and ``audio_standardizer.py`` never
disagree about it. The tables below are not guesses; each row carries the
source it was checked against (see ``SOURCES``).

The short version of the physics, and why it drives every default:

* The Chromecast with Google TV (HD) is the *only* player. It decodes
  H.264, HEVC, VP9 and AV1 up to 1080p60, and it can only ever EMIT
  Dolby Digital (AC-3, up to 5.1), Dolby Digital Plus (E-AC-3, up to 7.1,
  including DD+ Atmos via HDMI pass-through), base 5.1 DTS (chipset-level,
  unofficial), the backward-compatible DTS core extracted out of a DTS-HD
  MA/HRA or DTS:X track, and decoded PCM. The only families it genuinely
  cannot get any audio out of are TrueHD and WMA (Pro / Lossless), plus DTS
  Express — so a TrueHD track in a file is not free quality, it is a
  guaranteed server-side audio transcode on every single play.
* The PCM it decodes is itself bounded: the Android TV media framework on
  this box decodes AAC (LC / HE-AAC v1 / v2), MP3, FLAC, Opus, Vorbis and
  WAV, and it does so up to 24-bit / 96 kHz (``PLAYER.max_decoded_bit_depth``
  and ``PLAYER.max_decoded_sample_rate``). Two consequences, both modelled:
  a 24/192 FLAC is not "better audio" but a stream past the ceiling, which
  :func:`exceeds_decode_ceiling` reports instead of promising; and ALAC and
  WavPack have no decoder here at all, so they are :data:`AUDIO_UNKNOWN` —
  fail-closed, like every other codec outside this chain's confirmed set.
* A DTS-HD track is NOT in that bucket. The player cannot emit the HD
  layer, but DTS-HD is backward compatible by design: the player extracts
  the DTS core (5.1, lossy) that every DTS-HD bitstream carries and
  bitstreams THAT, so the movie Direct Plays as DTS — the AX3125H decodes
  it and its panel reads DTS, not DTS-HD/DTS:X. What is lost is the HD
  layer (and any DTS:X object metadata), not the audio. Converting such a
  track to DD+ would therefore destroy the lossless master to buy nothing
  (see ``DTS_PASSTHROUGH_DEFAULT``); the recorded device fact is
  ``PLAYER.dts_hd_core_fallback``, measured on this chain 2026-10. What
  reaches the bar is a DTS core either way, so the WHOLE DTS family — base
  DTS included — is width-capped at the core's 5.1
  (``DTS_CORE_MAX_CHANNELS``): no DTS-labelled track is ever credited with
  achieving 7.1 on this chain.
* The AX3125H is fed through its HDMI IN port, which decodes everything
  the Chromecast can emit — AC-3, DD+ Atmos, DTS, and multichannel PCM —
  before the video passes through to the TV. That is what makes the
  default audio rule permissive: a 5.1 AAC/FLAC/PCM track arrives as
  multichannel PCM at the bar and needs no transcode.
* The TV is a 2013 panel whose own audio return path is the weak leg: its
  Digital Audio Out menu offers per-input formats, and on this unit only
  PCM is selectable for HDMI sources (user-confirmed; see ``Display.arc``),
  so plain ARC/optical would deliver multichannel content as stereo PCM.
  The default wiring simply never asks the TV to carry sound.
* The TV tops out at 1920x1080 SDR. HDR10/HDR10+/HLG files still Direct
  Play — the Chromecast decodes them and tone-maps to SDR for this
  display — but Dolby Vision is NOT licensed on the G454V, so any
  Dolby-Vision-only file is flagged rather than assumed playable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# =============================================================================
# THE THREE DEVICES (researched facts; see SOURCES)
# =============================================================================

@dataclass(frozen=True)
class Player:
    model: str = "Google Chromecast with Google TV (HD)"
    model_id: str = "G454V"
    codename: str = "boreal"
    soc: str = "Amlogic S805X2 (quad Cortex-A35 @ 1.8 GHz, Mali-G31 MP2)"
    ram: str = "1.5 GB"
    storage: str = "8 GB"
    os: str = "Google TV (Android 12, upgradeable to 14)"
    max_resolution: tuple[int, int] = (1920, 1080)
    # The CODED ceiling, which is what a resolution check has to compare
    # against. 1080 is not a multiple of 16, so a 1080p H.264 stream is
    # routinely *stored* as 1920x1088 with the extra 8 lines carried as
    # frame_crop_bottom_offset padding (MediaInfo calls these Stored_Height
    # 1088 / Sampled_Height 1080). Depending on the container, the muxer and
    # whether the crop rectangle survived, ffprobe can report either 1080 or
    # 1088 for the very same picture - so comparing the stored height against
    # 1080 brands ordinary 1080p movies "oversize" and queues a downscale they
    # do not need. Rounded up to the macroblock grid the ceiling is
    # ceil(1920/16)*16 = 1920 by ceil(1080/16)*16 = 1088, and 1920x1088 is
    # inside the S805X2's decode block (H.264 level 4.2 covers 2048x1088@60)
    # as well as inside the TV's, which scales it to its 1920x1080 panel.
    max_coded_resolution: tuple[int, int] = (1920, 1088)
    max_fps: int = 60
    # Hardware video decoders (Amlogic S805X2 media block). AV1 decode is the
    # one silicon advantage the HD model holds over the 2020 4K model.
    video_codecs: tuple[str, ...] = ("h264", "hevc", "vp9", "av1", "mpeg2video", "mpeg1video")
    # HDR it decodes. NO Dolby Vision licence on the HD model (the 4K model
    # has one; this one outputs HDR10/HDR10+/HLG only), and because the
    # display below is SDR, supported HDR is tone-mapped to SDR at output.
    hdr_formats: tuple[str, ...] = ("HDR10", "HDR10+", "HLG")
    dolby_vision: bool = False
    # What the device can pass through over HDMI. Official Google list is
    # DD/DD+/Atmos(DD+); base 5.1 DTS passes at the Android/Amlogic firmware
    # layer (every Amlogic TV build since 8.1) but is NOT on Google's list,
    # hence "unofficial". No TrueHD and no DTS-HD *HD layer* ever leaves this
    # box - but the DTS core inside DTS-HD MA/HRA/DTS:X does (see
    # ``dts_hd_core_fallback`` immediately below).
    passthrough_audio: tuple[str, ...] = ("ac3", "eac3", "eac3-joc")
    passthrough_audio_unofficial: tuple[str, ...] = ("dts-core",)
    #: DTS-HD MA / DTS-HD HRA / DTS:X are not passed through as HD
    #: bitstreams - the G454V cannot emit the lossless layer - but unlike
    #: TrueHD the audio survives: DTS-HD is backward compatible by design,
    #: and the player extracts the DTS core (up to 5.1, lossy) that every
    #: such bitstream carries and bitstreams THAT. The bar decodes it as
    #: DTS - its front panel reads DTS, never DTS-HD/DTS:X - and the movie
    #: Direct Plays with no server transcode. What is lost is the HD layer
    #: and any DTS:X object metadata, not the playback. USER-CONFIRMED
    #: 2026-10 on the actual G454V; drives :data:`AUDIO_DTS_HD_CORE`.
    #: DTS-HD LBR (DTS Express) is the exception: it is a separate
    #: low-bitrate decoder with no backward-compatible core, so it stays
    #: transcode-bound.
    dts_hd_core_fallback: bool = True
    #: The ceiling of the software decode path, i.e. what happens to every
    #: track that is NOT bitstreamed. The Android TV media framework on this
    #: SoC decodes AAC (AAC-LC, HE-AAC v1/v2), MP3, FLAC, Opus, Vorbis and
    #: WAV (linear PCM) - and the format table for this chain bounds that
    #: decode at 24-bit / 96 kHz, for LPCM 2.0 exactly as for FLAC.
    #:
    #: The ceiling is modelled because it is a real boundary of the promise
    #: this toolkit makes: "the player decodes it, so nothing transcodes" is
    #: only true of a stream the decoder is specified to handle. What a
    #: 24/192 FLAC actually does here is not documented anywhere (a hard
    #: decode failure, or a silent resample by AudioFlinger to the mixer's
    #: rate - which then discards the extra resolution on its way out
    #: anyway), and the toolkit does not guess about either. So
    #: :func:`exceeds_decode_ceiling` *reports* such a track and nothing
    #: else: no synthesized replacement, and above all no irreversible
    #: keep-one-track decision taken on an unmeasured premise. A dropped
    #: master cannot be un-dropped; a flagged movie costs one manual look.
    max_decoded_sample_rate: int = 96000
    max_decoded_bit_depth: int = 24
    #: Codecs a rip can legitimately carry that this player has NO decoder
    #: for, and that it cannot bitstream either (unlike DTS-HD, there is no
    #: backward-compatible core to fall back to): ALAC and WavPack. They are
    #: absent from the chain's confirmed decode set, and Jellyfin's own
    #: codec table lists ALAC as unsupported on Android TV while the FFmpeg
    #: decoder ExoPlayer would need is not in the shipped client. They are
    #: therefore never classed ``decode-to-pcm``; they are
    #: :data:`AUDIO_UNKNOWN`, which is reported and left alone.
    undecodable_codecs: tuple[str, ...] = ("ALAC", "A_ALAC", "WAVPACK", "A_WAVPACK")

@dataclass(frozen=True)
class Sink:
    model: str = "Hisense AX3125H"
    description: str = "3.1.2ch soundbar + wireless subwoofer, 440 W"
    channels: str = "3.1.2"
    # Manufacturer spec sheet (files.hisense-usa.com): every decoder onboard.
    decoders: tuple[str, ...] = (
        "Dolby Atmos", "Dolby TrueHD", "Dolby Digital Plus", "Dolby Digital",
        "DTS:X", "DTS-HD Master Audio", "DTS (core decoder)", "PCM",
        "Multichannel PCM",
    )
    ports: tuple[str, ...] = (
        "1x HDMI IN (4K/3D pass-through)",
        "1x HDMI OUT to TV (eARC/ARC + CEC)",
        "1x optical in",
        "USB (music/FW)",
        "3.5mm AUX",
        "Bluetooth 5.3",
    )
    note: str = (
        "Fed from the Chromecast's HDMI OUT into this HDMI IN, the bar "
        "decodes DD/DD+ Atmos/DTS/multichannel PCM directly and passes the "
        "picture to the TV, so the 2013 TV never has to carry audio. This is "
        "the wiring this toolkit assumes by default (soundbar-hdmi-in)."
    )
    #: Hisense's own PER-PORT input matrix (user manual §1.3, "Supported Input
    #: Audio Formats"), which is the hard evidence the default wiring rests on.
    #: Two rows are load-bearing: multichannel LPCM is accepted on HDMI IN
    #: (so a 5.1/7.1 AAC/FLAC/PCM track the Chromecast decodes arrives intact),
    #: and it is NOT accepted on HDMI ARC or optical (so the same track over
    #: the tv-arc alternative cannot). Verified against the manufacturer PDF
    #: 2026-10; see SOURCES.
    hdmi_in_accepts: tuple[str, ...] = (
        "LPCM 2ch", "LPCM 5.1ch", "LPCM 7.1ch",
        "Dolby Digital", "Dolby Digital Plus",
        "Dolby Atmos - Dolby Digital Plus",
        "Dolby TrueHD", "Dolby Atmos - Dolby TrueHD",
        "DTS", "DTS-ES Discrete 6.1", "DTS-ES Matrix 6.1", "DTS 96/24",
        "DTS-HD High Resolution Audio", "DTS-HD Master Audio",
        "DTS-HD LBR", "DTS:X",
    )
    #: The same matrix, read the other way: formats the bar's ARC/optical path
    #: does NOT take, which is why ``tv-arc`` is the degraded alternative and
    #: never the default.
    arc_cannot_carry: tuple[str, ...] = (
        "LPCM 5.1ch", "LPCM 7.1ch",
        "Dolby TrueHD", "Dolby Atmos - Dolby TrueHD",
        "DTS-HD High Resolution Audio", "DTS-HD Master Audio",
        "DTS-HD LBR", "DTS:X",
    )

@dataclass(frozen=True)
class Display:
    model: str = "Samsung UN60F6350AF"
    description: str = '60" LED-LCD Smart TV (2013, F6350 series)'
    resolution: tuple[int, int] = (1920, 1080)
    hdr: bool = False
    panel_hz: int = 120  # "Clear Motion Rate 240" marketing; native 120 Hz
    hdmi_ports: int = 4  # one labelled (ARC); Anynet+ CEC on all
    arc: str = (
        "Plain ARC on the HDMI port labelled (ARC); no eARC. Samsung's "
        "F-series e-manual states the available Digital Audio Output (SPDIF) "
        "formats 'may vary depending on the input source', and on this unit "
        "only PCM is selectable for HDMI sources (user-confirmed, 2026-09): "
        "with a Chromecast plugged into the TV, multichannel audio is "
        "downmixed to stereo PCM before it ever reaches an ARC/optical "
        "soundbar. Samsung's own ARC article lists PCM 2.0 / Dolby Digital "
        "5.1 / DTS 5.1 as the formats ARC can carry, but what this TV "
        "actually offers is input-dependent, which is why the toolkit's "
        "default wiring (soundbar-hdmi-in) does not route audio through the "
        "TV at all."
    )
    optical_out: bool = True

PLAYER = Player()
SINK = Sink()
DISPLAY = Display()

# Sources every table above was checked against (see docs/hardware.md for the
# full write-up with the same references).
SOURCES: tuple[str, ...] = (
    "https://support.google.com/chromecast/answer/3046409 (official CCwGTV HD specs: 1080p60, HDR10/HDR10+/HLG, DD/DD+/Atmos passthrough)",
    "https://www.androidtv-guide.com/streaming-gaming/chromecast-google-tv-hd/ (G454V 'boreal': S805X2, 1.5GB/8GB, AV1/VP9/H.264/HEVC)",
    "https://www.androidpolice.com/chromecast-with-google-tv-hd-review/ (no Dolby Vision on the HD model; 1.5 GB RAM; audio passthrough list)",
    "https://www.reddit.com/r/googlehome/comments/j2ggur/ (Amlogic Android TV >= 8.1 passthrough: 5.1 DTS, DD, DD+, DD+/Atmos; NO TrueHD/DTS-HD)",
    "https://files.hisense-usa.com/download/f25648883914883a (AX3125H official spec sheet: 1x HDMI IN + 1x HDMI OUT eARC, Dolby Atmos/TrueHD/DD+/DD, DTS:X/DTS-HD/DTS decoders, PCM and Multich PCM)",
    "https://manuals.plus/hisense/ax3125h-3-1-2ch-440w-dolby-atmos-soundbar-with-wireless-subwoofer-manual (AX3125H user manual: HDMI IN socket for HDMI source devices; HDMI OUT (TV eARC/ARC); input-format table PCM / Dolby Digital / DD+ / TrueHD -> MPCM)",
    "https://files.hisense-usa.com/download/f25642eeb4a386bb (Hisense 3.1.2ch manual, section 1.3 'Supported Input Audio Formats' - the PER-PORT matrix that decides the default wiring: LPCM 5.1ch and LPCM 7.1ch are supported on HDMI IN and HDMI eARC but NOT on HDMI ARC or OPTICAL; 'Dolby Atmos - Dolby Digital Plus' is supported on HDMI ARC, eARC and HDMI IN; 'Dolby Atmos - Dolby TrueHD', Dolby TrueHD, DTS-HD HR/MA/LBR and DTS:X are eARC and HDMI IN only. Verified 2026-10)",
    "https://www.manualowl.com/m/Samsung/UN60F6350AF/Manual/347300 (UN60F6350AF e-manual: 'ARC is only available through the HDMI (ARC) port'; Digital Audio Output (SPDIF) formats 'may vary depending on the input source')",
    "https://www.samsung.com/sg/support/tv-audio-video/how-to-use-the-hdmi-arc-port-on-a-samsung-tv/ (Samsung support: HDMI-ARC carries PCM 2ch, Dolby Digital up to 5.1 and DTS Digital Surround up to 5.1; 2013-2014 F/H-series sound-output path)",
    "USER-CONFIRMED 2026-09 on the actual UN60F6350AF: with HDMI sources connected, the TV offers PCM only as its digital audio output format, so the ARC/optical path delivers multichannel content as stereo PCM",
    "USER-CONFIRMED 2026-10 on the actual G454V -> AX3125H chain: DTS-HD MA, DTS-HD HRA and DTS:X are never passed through as HD bitstreams, but the player does not drop the audio - it automatically extracts the backward-compatible DTS CORE (5.1) every such bitstream carries and bitstreams that, so the bar still decodes surround and its front panel reads DTS (not DTS-HD/DTS:X). These tracks therefore Direct Play with no server transcode; what is lost is the HD layer and any DTS:X object metadata. DTS-HD LBR / DTS Express is the exception (no backward-compatible core), so it stays transcode-bound",
    "https://developer.android.com/guide/topics/media/media-formats (Android Developers, *Supported media formats*: the platform decoder set every Android TV device shares - AAC-LC / HE-AAC v1 / v2, MP3, FLAC, Opus, Vorbis and linear PCM, bounded at 24-bit / 96 kHz on this chain's read of the table; TrueHD / DTS-HD / DTS:X absent, which is why the DTS-core fallback above had to be measured rather than read off a spec sheet, and ALAC / WavPack absent too)",
    "USER-CONFIRMED 2026-10 for this chain (G454V into the AX3125H's HDMI IN): the bitstream set is AC-3 (5.1), E-AC-3 (7.1), E-AC-3 JOC (Atmos, decoded by the bar's 3.1.2 array), DTS core 5.1, and LPCM - stereo up to 24-bit/96 kHz on the PCM path and multichannel LPCM 5.1/7.1 accepted by both the Chromecast HAL and the bar's HDMI IN; the software-decode set is AAC/MP3/FLAC/Opus/Vorbis/WAV up to 24-bit/96 kHz; DTS may additionally need the manual surround selection in the Google TV sound settings or an app with its own bitstreamer (Kodi, VLC, Nova, Plex); TrueHD, DTS-HD MA/HRA (core only) and DTS:X (core only) never bitstream as lossless",
    "https://jellyfin.org/docs/general/clients/codec-support/ (ALAC listed unsupported for Android and Android TV: the client reports the format, the platform decoder is what is missing - the reason ALAC is fail-closed here rather than 'decoded to PCM')",
)

# =============================================================================
# WIRING MODES
# =============================================================================

#: Chromecast -> soundbar HDMI IN -> TV. Audio never crosses the 2013 TV:
#: the bar decodes AC-3/DD+ Atmos/DTS/multichannel PCM itself and passes the
#: picture through. THE DEFAULT — this is how the chain is actually cabled.
WIRING_SOUNDBAR_HDMI_IN = "soundbar-hdmi-in"
#: Chromecast -> TV HDMI, TV --ARC/optical--> soundbar. Explicit alternative
#: only: on this 2013 TV the digital audio output offers PCM for HDMI sources,
#: so multichannel content arrives at the bar as stereo PCM (docs/hardware.md
#: §3-§4). Kept supported and tested; never assumed.
WIRING_TV_ARC = "tv-arc"

WIRING_ENV_VAR = "ORGANIZE_PLAYBACK_WIRING"
#: The wiring this chain is actually cabled with: the Chromecast feeds the
#: soundbar's HDMI IN and the bar passes video through to the TV. Set by
#: explicit decision (supersedes the earlier tv-arc default); the flag
#: ``--wiring`` and ``ORGANIZE_PLAYBACK_WIRING`` override it per run.
DEFAULT_WIRING = WIRING_SOUNDBAR_HDMI_IN


def resolve_wiring(explicit: str | None = None) -> str:
    """Which wiring to assume: flag, then environment, then the default.

    Defaults to ``soundbar-hdmi-in`` (``DEFAULT_WIRING``); an unrecognized
    value (from either source) falls back to that default rather than
    guessing, and ``tv-arc`` remains available as an explicit alternative.
    """
    raw = (explicit or os.environ.get(WIRING_ENV_VAR) or "").strip().lower()
    if raw in (WIRING_SOUNDBAR_HDMI_IN, WIRING_TV_ARC):
        return raw
    return DEFAULT_WIRING


# =============================================================================
# BASE DTS: accepted, or treated as transcode-bound?
# =============================================================================

#: Whether DTS-family tracks should be left alone, per wiring.
#:
#: Both wirings answer ``True``: the table is uniform, because on this chain
#: the whole DTS family is accepted on evidence rather than on faith.
#:
#: DTS is still not on Google's published passthrough list for the G454V (that
#: list is Dolby Digital / Dolby Digital Plus / Atmos-via-DD+); both halves of
#: the family play because the Amlogic firmware passes DTS bitstreams through
#: at the HDMI layer, and both were tested on the actual chain rather than
#: argued about:
#:
#: * **base 5.1 DTS core** - a base-DTS movie was played through Jellyfin to
#:   the G454V and two independent indicators agreed: Jellyfin reported
#:   **Direct Play** (no transcode at either end), and the AX3125H's front
#:   panel lit its **DTS** indicator, which it only does for a real DTS
#:   bitstream arriving at its decoder.
#: * **DTS-HD MA / DTS-HD HRA / DTS:X** - user-confirmed 2026-10 on the same
#:   chain: the G454V cannot emit the lossless HD layer, but it does not drop
#:   the audio either. It automatically extracts the backward-compatible DTS
#:   core (5.1, lossy) and passes THAT through, so the bar still decodes
#:   surround and its panel reads **DTS** instead of DTS-HD/DTS:X. No server
#:   transcode happens, which is exactly what makes converting pointless:
#:   the delivered audio would be the same DTS (or, after conversion, a
#:   *smaller* 640 kbps DD+ bed).
#:
#: Measured facts, then, not assumptions. Given that, converting is
#: irreversible loss buying nothing: 1509 kbps DTS core becomes a 640 kbps
#: DD+ bed, a difference of ~870 kbps - about 780 MB on a two-hour movie -
#: and on a DTS-HD master it would also destroy the lossless layer to replace
#: audio the bar already decodes; in both cases the 3.1.2 array folds the mix
#: whatever carries it, so the trade never had a quality argument either.
#: 8.5.0's only real argument was availability risk, and that risk is cheap to
#: self-insure against: this decision is reversible *at playback time, from
#: the untouched source* - if a firmware update ever ends the passthrough,
#: ``audiofit`` converts then, from the same bytes, to the identical DD+ result
#: it would have written today. Waiting costs one rerun; converting up front
#: costs the DTS track forever on a bet that turned out to be wrong on this
#: hardware.
#:
#: Either way this is only a DEFAULT: ``--no-dts-passthrough`` and
#: ``--dts-passthrough`` state the policy explicitly and win over the table.
#: ``--no-dts-passthrough`` covers the WHOLE family - a DTS-HD master it is
#: asked to convert is decoded from its lossless layer and re-encoded, which
#: is the one way to spend the master deliberately.
DTS_PASSTHROUGH_DEFAULT: dict[str, bool] = {
    WIRING_SOUNDBAR_HDMI_IN: True,
    WIRING_TV_ARC: True,
}


#: Widest layout the DTS family can ever reach on this chain: 5.1 (6
#: channels). DTS-ES extends the core family to 6.1 on some discs, but 5.1 is
#: both the ceiling of a plain DTS Digital Surround bitstream and the layout a
#: DTS-HD MA/HRA or DTS:X stream carries as its backward-compatible core - so
#: :func:`achievable_channels` caps the family here wherever a track claims to
#: be wider, whether it is the base core or the core extracted from an HD
#: master. The cap is what keeps an over-credited DTS label from outranking a
#: real DD+ 5.1 (with Atmos) in a keep-one-track decision.
DTS_CORE_MAX_CHANNELS = 6


def dts_passthrough_default(wiring: str | None = None) -> bool:
    """Whether DTS-family tracks are accepted as-is on ``wiring``, absent a flag.

    Covers base DTS core AND the DTS core the player extracts from a DTS-HD
    MA/HRA/DTS:X track (see :data:`DTS_PASSTHROUGH_DEFAULT` for the evidence
    that settled both). An unrecognized wiring resolves through
    :func:`resolve_wiring` first, so this can never answer for a chain that
    does not exist.
    """
    return DTS_PASSTHROUGH_DEFAULT[resolve_wiring(wiring)]


# =============================================================================
# AUDIO: what happens to a track on THIS chain
# =============================================================================

# Every audio format a movie can carry lands in exactly one of these classes.
# The string values are the stored vocabulary (reports, JSON, state cache) —
# treat them as part of the toolkit's on-disk format, not as display text.
AUDIO_NATIVE = "native-passthrough"       # AC-3 / E-AC-3(+Atmos): bitstreamed end-to-end
AUDIO_DTS_CORE = "dts-core-passthrough"   # base DTS: works here, but unofficially (chipset, not Google spec)
AUDIO_DTS_HD_CORE = "dts-hd-core-passthrough"  # DTS-HD MA/HRA, DTS:X: player extracts the DTS core it carries
AUDIO_DECODE_PCM = "decode-to-pcm"        # AAC/FLAC/MP3/Opus/Vorbis/PCM: player decodes; PCM into the bar
AUDIO_TRANSCODE_BOUND = "transcode-bound" # TrueHD/DTS-HD LBR/WMA Pro: the player can never emit these
AUDIO_UNKNOWN = "unknown"                 # fail-closed: reported, never auto-touched

# Tier an audio class contributes to "which track does the cleaner keep".
# The ordering IS the rework philosophy: on this chain, the *best* track is
# the best one that plays natively. Lossless HD formats sit below every
# chain-native lossy format because the G454V cannot emit them — a TrueHD
# track here is a promise that Jellyfin re-encodes the audio on every play.
# A DTS-HD track is tiered with base DTS core because that is precisely what
# reaches the bar: the extracted core, not the HD layer.
CLASS_TIERS: dict[str, int] = {
    AUDIO_NATIVE: 100,
    AUDIO_DTS_CORE: 80,
    AUDIO_DTS_HD_CORE: 80,
    AUDIO_DECODE_PCM: 65,
    AUDIO_TRANSCODE_BOUND: 30,
    AUDIO_UNKNOWN: 10,
}


#: Placeholder rendered for an ABSENT field of a classification blob.
#: ``classify_audio_blob`` reads field 2 as the codec's profile/ID, so a blob
#: whose field 2 is simply missing hands the title the profile's position -
#: "DTS  DTS-HD MA 7.1" (empty codec ID) splits to ["DTS", "DTS-HD", ...] and a
#: plain DTS core track gets promoted to an HD master by its own name. Every
#: blob builder must therefore go through :func:`codec_blob`, which fills the
#: gap instead of collapsing it.
BLOB_FIELD_ABSENT = "-"


def codec_blob(*fields: object) -> str:
    """Build an upper-cased classification blob with STABLE field positions.

    ``classify_audio_blob`` is positional: field 1 is the codec name (the only
    authoritative one), field 2 may refine a bare DTS name into an HD/DTS:X
    master, and everything after that is a title that must never decide. That
    contract only holds if the fields cannot slide, so an absent field is
    rendered as :data:`BLOB_FIELD_ABSENT` rather than dropped::

        codec_blob("DTS", "", "DTS-HD MA 7.1")  ->  "DTS - DTS-HD MA 7.1"

    Two tools reading the same track must reach the same verdict, and building
    the blob here is what makes that structural instead of a convention each
    caller has to remember (``audio_standardizer._stream_blob`` and
    ``mkv_track_cleaner.get_audio_quality_score`` both call this).
    """
    rendered = [
        str(field).strip() if str(field if field is not None else "").strip()
        else BLOB_FIELD_ABSENT
        for field in fields
    ]
    return " ".join(rendered).upper()


#: Codec-NAME spellings of Dolby Digital Plus. Kept in one place because three
#: decisions depend on it: the class table, the cleaner's sub-tier inside
#: AUDIO_NATIVE, and whether a track may carry Atmos at all.
_DOLBY_DIGITAL_PLUS_MARKERS = (
    "E-AC-3", "EAC3", "E_AC3", "EC-3", "EC3",
    "DOLBY DIGITAL PLUS", "DD+", "DDPLUS",
)


def is_dolby_digital_plus(blob: str) -> bool:
    """True when this blob names a Dolby Digital Plus (E-AC-3) stream.

    The Dolby family's two bitstreams are both chain-native, but only DD+ can
    carry Atmos (as JOC) - plain AC-3 has no Atmos variant at all, so an
    "Atmos" title on an AC-3 track is a release-group flourish and must never
    be credited as height audio.
    """
    return any(marker in blob.upper() for marker in _DOLBY_DIGITAL_PLUS_MARKERS)


def _classify_audio_segment(b: str) -> str | None:
    """Classify authoritative codec text (never a free-form title), or None.

    Ditto-confidence rules, in order: lossless-HD markers first (a DTS-HD MA
    label contains "DTS" and a TrueHD Atmos track contains "ATMOS", so HD must
    be proven before any core), the chain-native Dolby family next, base DTS,
    then the client-decodable PCM family.

    The DTS-HD family splits in two here, and the split is the whole point:
    DTS-HD MA/HRA and DTS:X are backward compatible, so the player extracts
    the DTS core they carry and bitstreams that (:data:`AUDIO_DTS_HD_CORE`),
    while DTS-HD LBR / DTS Express is a separate low-bitrate decoder with no
    core to fall back to (:data:`AUDIO_TRANSCODE_BOUND`).
    """
    if not b.strip():
        return None
    if "TRUEHD" in b or "A_MLP" in b or b.strip() == "MLP":
        return AUDIO_TRANSCODE_BOUND
    dts_hd = _dts_hd_class(b)
    if dts_hd is not None:
        return dts_hd
    if any(k in b for k in ("WMAPRO", "WMA PRO", "WMA_LOSSLESS", "WMALOSSLESS")):
        # Android/ExoPlayer has no WMA Pro decoder; server transcodes.
        return AUDIO_TRANSCODE_BOUND
    if is_dolby_digital_plus(b):
        return AUDIO_NATIVE
    if any(k in b for k in ("AC-3", "AC3", "A_AC3", "DOLBY DIGITAL", "DD ")):
        return AUDIO_NATIVE
    if any(t in b.split() for t in ("DTS", "DCA")) or "A_DTS" in b:
        return AUDIO_DTS_CORE
    if any(k in b for k in ("AAC", "A_AAC", "MP4A")):
        return AUDIO_DECODE_PCM
    if any(k in b for k in ("FLAC", "A_FLAC")):
        return AUDIO_DECODE_PCM
    # The decodable PCM family. ALAC and WavPack are deliberately NOT here:
    # this player has no decoder for either (see
    # ``Player.undecodable_codecs``), and "WAVPACK" must not be read as "WAV"
    # - the same substring trap that makes ATRAC3 need its own guard.
    if any(k in b for k in ("PCM", "A_PCM", "WAV")):
        return AUDIO_DECODE_PCM
    if any(k in b for k in ("OPUS", "A_OPUS", "VORBIS", "A_VORBIS",
                            "MP3", "MPEG/L", "MPEG AUDIO", "A_MPEG")):
        return AUDIO_DECODE_PCM
    return None


#: Codec-NAME tokens a profile field may legitimately refine. Only a bare
#: DTS codec name (mkvmerge's "DTS", ffprobe's "DTS"/"DCA") needs the second
#: field: "DTS" + profile "DTS-HD MA" is a core-fallback HD track, "DTS" +
#: "A_DTS" is a plain core, and "DTS" + "A_DTS/EXPRESS" is transcode-bound
#: (no core). A codec ID ("A_DTS/HD_MA") or a full name ("DTS-HD") already
#: carries the answer, so the field after it is never consulted.
_DTS_CODEC_NAME_TOKENS = frozenset({"DTS", "DCA"})

#: Substrings that make a DTS *profile* field a DTS-HD-family variant rather
#: than a plain core. Deliberately DTS-specific: "DTS TRUEHD 7.1" is a core
#: track whose title narrates a source, and must not be promoted to an HD
#: master.
#:
#: The separator-free spelling ("DTSX") is here because it is what real rips
#: write when an extractor flattens the colon, and unlike a spaced-out
#: "DTS X" it is one token - so it can be matched without ever reading past
#: the field it appeared in, which is the line the title must not cross. A
#: "DTS X" spelling still lands in the DTS family (as the base core), and since
#: 8.6.1 that is a mislabel rather than a mistake: both DTS classes deliver a
#: core, rank tier 80, and are capped at the core's 5.1 by
#: :data:`DTS_CORE_MAX_CHANNELS`, so neither the band, the tier nor the width
#: a track is credited with depends on which of the two the blob happened to
#: spell. What the cap is worth is the case it fixes: crediting a 7.1 DTS
#: stream with eight channels let it outrank a genuine DD+ 5.1 Atmos track in
#: a keep-one-track decision that cannot be undone.
_DTS_HD_CORE_MARKERS = (
    "DTS-HD", "DTS/HD", "DTS:X", "DTS-X", "DTS_X",
    "DTS HD", "DTSHD", "DTSX", "A_DTS/LOSSLESS", "DTS LOSSLESS",
)

#: DTS-HD spellings with NO backward-compatible core. DTS-HD LBR (DTS
#: Express) is a separate low-bitrate decoder - used for secondary audio -
#: and a plain DTS decoder cannot play it, so these stay transcode-bound.
_DTS_HD_NO_CORE_MARKERS = ("LBR", "EXPRESS")


def has_no_platform_decoder(blob: str) -> bool:
    """True when this text names a codec the G454V cannot decode or bitstream.

    ALAC and WavPack are the two a movie can plausibly carry: both are lossless,
    both look like members of the FLAC/PCM family, and neither has a decoder in
    the Android TV media framework (nor in the client decoders Jellyfin's
    Android-TV app can use), while DTS-HD - the other "HD" label in this table -
    does have a backward-compatible core to fall back to. They therefore belong
    to no class at all, and this predicate exists so that decision is stated
    once: :func:`classify_audio_blob` fails closed on it, and
    :func:`mkv_track_cleaner.chain_audio_tier` must not hand these names the
    lossless sub-tier it gives FLAC.

    The check is substring-wide on purpose: ``WAVPACK`` contains ``WAV``, and a
    family that is matched by substring has to be rejected by substring.
    """
    return any(marker in (blob or "").upper() for marker in PLAYER.undecodable_codecs)

#: Bare DTS spellings that can *start* a two-word DTS name, so the word after
#: them may still belong to the codec rather than to a title ("DTS Express",
#: "DTS-HD LBR"). Only these may be read through one more token: a codec ID
#: such as "A_DTS" already carries its own answer, and everything after it is
#: a title (the pinned case: "DTS A_DTS DTS-HD MA 7.1" stays core DTS).
_DTS_NAME_LEAD_WORDS = frozenset({"DTS", "DTS-HD", "DTSHD", "DTS:X", "DTS-X", "DTS_X"})


def _dts_hd_class(*segments: str) -> str | None:
    """Classify DTS-HD vocabulary in profile/codec-ID text, or None.

    Both halves of the DTS-HD family must be proven before the plain-DTS
    branch can see them (every one of these labels contains "DTS"), and they
    land in different classes:

    * ``DTS-HD MA`` / ``DTS-HD HRA`` / ``DTS:X`` / ``A_DTS/HD_MA`` ->
      :data:`AUDIO_DTS_HD_CORE`: not emittable as HD, but the player extracts
      the backward-compatible DTS core and bitstreams that instead;
    * ``DTS-HD LBR`` / ``DTS Express`` -> :data:`AUDIO_TRANSCODE_BOUND`:
      DTS Express is its own decoder with no core, so there is nothing to
      fall back to and the server must re-encode.

    Never called on a free-form title: only on codec-name/profile fields,
    which is why the shorter markers cannot collide with release-group prose.
    """
    text = " ".join(segment for segment in segments if segment).upper()
    if not text:
        return None
    if "DTS" in text and any(k in text for k in _DTS_HD_NO_CORE_MARKERS):
        return AUDIO_TRANSCODE_BOUND
    if any(k in text for k in _DTS_HD_CORE_MARKERS):
        return AUDIO_DTS_HD_CORE
    return None


def classify_audio_blob(blob: str) -> str:
    """Classify one audio track from an upper-cased description blob.

    The blob is codec name + codec ID (or profile) + track title, upper-cased
    — the convention ``mkv_track_cleaner``'s scorer has always used, which
    keeps the ffprobe names ("EAC3", "TRUEHD") and the MKVToolNix names
    ("E-AC-3", "AC-3", "A_AC3") equally readable in one string. Only the
    FIRST field is treated as authoritative:

    1. the **codec name** (``ffprobe``'s ``codec_name``, mkvmerge's codec
       column) decides on its own — so a real E-AC-3 stream is native even
       when its profile is empty/unknown and its title says "TrueHD 7.1";
    2. the **second field** may only *refine* a bare DTS codec name into a
       DTS-HD/DTS:X variant — the core-fallback class, or transcode-bound for
       DTS Express (ffprobe's ``profile``, mkvmerge's codec ID);
    3. everything after that is a **title**, and titles are consulted only
       when the codec fields say nothing at all.

    Titles describe the source or the release, not the encoded stream —
    release groups write "TrueHD 7.1" or "DTS-HD MA 7.1" on tracks that are
    plain AC-3/E-AC-3, and this tool itself appends "Dolby Digital 5.1 (from
    truehd)". No such title may ever schedule a chain-native stream for an
    AC-3 transcode, which is why the codec name is read in isolation here
    (see tests: ``test_titles_never_demote_a_proven_codec`` and the E-AC-3
    false-positive regressions in ``tests/test_audio_standardizer.py``).

    Because field 2 is read as the profile, every blob builder must keep the
    field positions stable: render an absent field with a placeholder rather
    than letting the title shift up (``audio_standardizer._stream_blob``
    uses ``-`` for exactly this reason).
    """
    b = blob.upper()
    tokens = b.split()
    if not tokens:
        return AUDIO_UNKNOWN
    if "ATRAC" in tokens[0]:
        # Sony ATRAC3 contains "AC3" as a substring; it is none of this chain.
        return AUDIO_UNKNOWN
    if has_no_platform_decoder(tokens[0]):
        # ALAC and WavPack, decided by the codec-NAME field alone: this player
        # has no decoder for them and no bitstream path to fall back on, so
        # they are fail-closed rather than "decoded to PCM". Checked here, and
        # not inside the segment classifier, for two reasons - "WAVPACK"
        # contains "WAV" (the ATRAC trap again), and a title narrating a source
        # ("DTS-HD MA (from ALAC)") must not reach this rule from the widened
        # scan, exactly as no title may decide anything else here.
        return AUDIO_UNKNOWN
    # 1. The codec-NAME field decides, in isolation. A title word can never
    #    enter this step: with no profile field present, the old code joined
    #    the first two whitespace tokens, which put the title's first word
    #    next to the codec and let "TrueHD 7.1" turn an E-AC-3 stream into a
    #    transcode candidate.
    codec_class = _classify_audio_segment(tokens[0])
    if (codec_class == AUDIO_DTS_HD_CORE and len(tokens) > 1
            and tokens[1] in _DTS_HD_NO_CORE_MARKERS):
        # mkvmerge prints the no-core variant as two words - "DTS-HD LBR" -
        # and the codec-NAME field alone is just "DTS-HD", which reads as the
        # core-fallback family. The marker is in field 2, so the pair is what
        # says DTS Express; a title cannot slide in here, because a blob built
        # by :func:`codec_blob` always has the profile in that slot.
        return AUDIO_TRANSCODE_BOUND
    if codec_class == AUDIO_DTS_CORE and tokens[0] in _DTS_CODEC_NAME_TOKENS:
        # Only a bare DTS codec name is refined by the second field, and only
        # by DTS-specific HD vocabulary (never "TRUEHD", so a DTS core track
        # titled "TrueHD 7.1 (source)" stays core). The refinement can land in
        # either DTS-HD class: the core-fallback one, or transcode-bound for
        # DTS Express (no backward-compatible core).
        second = tokens[1] if len(tokens) > 1 else ""
        third = tokens[2] if len(tokens) > 2 else ""
        hd_class = _dts_hd_class(second, f"{tokens[0]} {second}".strip())
        # The no-core markers trail a name instead of leading it ("DTS
        # Express", "DTS-HD LBR"), and a profile is not one word wide, so one
        # more token is read - but only while the field still holds a DTS name,
        # and it may only conclude transcode-bound: a later word can never
        # promote a core track into the HD family.
        if ((hd_class is None or hd_class == AUDIO_DTS_HD_CORE)
                and second in _DTS_NAME_LEAD_WORDS
                and _dts_hd_class(f"{second} {third}") == AUDIO_TRANSCODE_BOUND):
            return AUDIO_TRANSCODE_BOUND
        if hd_class is not None:
            return hd_class
    if codec_class is not None:
        return codec_class
    # 2. The codec-name field is not a name this table knows (mkvmerge's
    #    human label "Dolby Digital Plus", a raw codec ID): widen the lens,
    #    shortest prefix first, so "DOLBY DIGITAL" is proven native before a
    #    later title word such as "TRUEHD" can be reached.
    for width in range(1, min(5, len(tokens) + 1)):
        answer = _classify_audio_segment(" ".join(tokens[:width]))
        if answer is not None:
            return answer
    # 3. Full blob, last resort: for a human-readable codec field with no
    #    separating ID ("DTS-HD Master Audio") a substring is the only hint.
    answer = _classify_audio_segment(b)
    if answer is not None:
        return answer
    return AUDIO_UNKNOWN


def classify_audio_ffprobe(codec_name: str, profile: str = "") -> str:
    """Same classification from ffprobe's fields (codec_name + profile)."""
    return classify_audio_blob(f"{codec_name or ''} {profile or ''}")


def tier_for_blob(blob: str) -> int:
    """The codec-quality tier of one track for track-keeping decisions."""
    return CLASS_TIERS[classify_audio_blob(blob)]


def is_chain_native(blob: str) -> bool:
    """True when this track plays without any server-side audio transcode.

    Includes :data:`AUDIO_DTS_HD_CORE`: the player emits the DTS core inside
    the DTS-HD bitstream, so playback needs no server work even though the HD
    layer never leaves the box.
    """
    return classify_audio_blob(blob) in (
        AUDIO_NATIVE, AUDIO_DTS_CORE, AUDIO_DTS_HD_CORE, AUDIO_DECODE_PCM)


# =============================================================================
# TARGET SPEC: what audio_standardizer.py synthesizes when a track is
# transcode-bound — tuned for the wiring this chain actually runs.
#
# soundbar-hdmi-in (the DEFAULT — Chromecast -> AX3125H HDMI IN -> TV):
# the target is E-AC-3 (Dolby Digital Plus). It is one of the two Dolby
# bitstreams on the G454V's OFFICIAL passthrough list, and the AX3125H
# decodes it on its HDMI IN — the only two hops audio crosses on this
# wiring. E-AC-3 is strictly the better codec here: at the same bitrate it
# is a more efficient encode than AC-3. 640 kbps is the bitrate every sink
# in this chain handles with headroom; nothing below is needed and nothing
# above buys anything audible.
#
# tv-arc (the explicit alternative): the target stays AC-3 (Dolby Digital),
# because that path routes sound through the 2013 TV, and AC-3 is the one
# format licensed-and-supported at every hop of it (ARC and optical
# included) should the wiring ever regress further.
#
# Both wirings synthesize AT MOST a 5.1 bed — see the ceiling recorded
# immediately below. It is the encoder's limit, not the format's: E-AC-3 the
# bitstream can carry 7.1, and an existing 7.1 E-AC-3 track passes through
# this chain untouched. But nothing on the *synthesis* side can produce it.
# =============================================================================

#: Channel ceiling of everything :func:`target_audio_for` may name, because
#: it is the ceiling of the encoders that would have to build it. ffmpeg's
#: ``eac3`` and ``ac3`` encoders support layouts up to 5.1 and no further:
#: they write independent frames only and never the dependent substreams a
#: 7.1 E-AC-3 bitstream needs, and they do NOT downmix — asking for ``-ac 8``
#: fails ("Could not open encoder before EOF … Conversion failed!") and
#: leaves no output file at all. Verified against ffmpeg 7.0.2 (``ffmpeg -h
#: encoder=eac3``) and by running both commands: ``-ac 8`` fails, ``-ac 6``
#: produces a valid ``eac3, 48000 Hz, 5.1(side), fltp, 640 kb/s``.
#:
#: This is recorded as DATA rather than prose because two decisions read it:
#: :func:`target_audio_for` must never promise wider, and
#: :func:`achievable_channels` must never credit a transcode-bound master
#: with a layout no encoder can deliver. A track that needs no encoder is
#: unaffected — a FLAC 7.1 really does arrive as LPCM 7.1 on the bar's
#: HDMI IN (Hisense's per-port matrix) — so it still achieves its own layout.
FFMPEG_DOLBY_ENCODE_MAX_CHANNELS = 6

#: Human names for the two Dolby codecs this chain synthesizes (reports,
#: track titles, docs). Keyed by the ffmpeg codec name.
DOLBY_CODEC_NAMES: dict[str, str] = {
    "ac3": "Dolby Digital (AC-3)",
    "eac3": "Dolby Digital Plus (E-AC-3)",
}

#: Short display names, without the parenthetical codec id — for one-line
#: summaries and pipeline step titles where "Dolby Digital Plus (E-AC-3)" is
#: too long to read.
DOLBY_SHORT_NAMES: dict[str, str] = {
    "ac3": "Dolby Digital",
    "eac3": "Dolby Digital Plus",
}

def dolby_name(codec: str) -> str:
    """Display name of a synthesized Dolby codec ('eac3' / 'ac3')."""
    return DOLBY_CODEC_NAMES.get(codec, codec)

#: The chain's audio sample rate: the Chromecast's HDMI output rate, so a
#: synthesized track is written at it and never resampled. Recorded as data
#: because two decisions read it — the target table below and the fallback in
#: :func:`sample_rate_of` — and they must not drift apart.
CHAIN_AUDIO_SAMPLE_RATE = 48000


def sample_rate_of(value: object, default: int = CHAIN_AUDIO_SAMPLE_RATE) -> int:
    """Parse a reported sample rate, falling back to ``default`` on nonsense.

    The same trap 8.4.1 closed for channel counts, on the field next to it.
    Both mkvmerge and ffprobe report an *unknown* rate as the string ``"0"``,
    and ``int(float("0" or 48000))`` is 0 — the ``or`` never fires, because a
    non-empty string is truthy whatever it spells. The result was that one and
    the same fact ranked a track two different ways depending on which tool
    reported it: an int ``0`` hit the ``or`` and became 48000, a string ``"0"``
    did not and stayed 0, which is the LAST tie-break in
    ``get_audio_quality_score`` and can therefore decide which audio track
    survives an irreversible remux.

    Zero and unparseable are treated identically, as they are for channels:
    neither is a rate, so both fall back to the chain's.
    """
    try:
        rate = int(float(value))  # type: ignore[arg-type]
    except (ValueError, TypeError, OverflowError):
        return default
    return rate if rate > 0 else default


def bit_depth_of(value: object) -> int:
    """Parse a reported bit depth; anything unparseable is "not reported".

    Deliberately without a fallback constant: an unknown depth cannot breach a
    ceiling, and inventing one would turn a missing field into a claim about
    the file. Only a real number above :attr:`Player.max_decoded_bit_depth`
    counts, which keeps the ceiling honest about what the probe actually saw.
    """
    try:
        depth = int(float(value))  # type: ignore[arg-type]
    except (ValueError, TypeError, OverflowError):
        return 0
    return depth if depth > 0 else 0


def exceeds_decode_ceiling(sample_rate: object, bit_depth: object = 0) -> str:
    """A reason string when a decoded stream is past this player's ceiling.

    "" means the stream is within it (or the probe could not say). This is asked
    only of the ``decode-to-pcm`` family, where the player has to run a software
    decoder: the bitstreamed formats (AC-3, E-AC-3, the DTS family) never touch
    that decoder, so a 192 kHz sample rate on a Dolby track is not this
    question at all.

    The answer is deliberately a *report*, not a plan: the ceiling says the
    decoder is not specified beyond 24-bit/96 kHz, not that the file is
    unplayable, and nothing here has measured which of a decode failure or a
    silent resample to the mixer's 48 kHz actually happens. So no Dolby track is
    synthesized from it and no keep-one-track ranking is influenced by it - see
    :attr:`Player.max_decoded_sample_rate`.
    """
    rate = sample_rate_of(sample_rate, default=0)
    depth = bit_depth_of(bit_depth)
    if rate > PLAYER.max_decoded_sample_rate:
        return f"this stream's {rate // 1000} kHz rate is past it"
    if depth > PLAYER.max_decoded_bit_depth:
        return f"this stream's {depth}-bit depth is past it"
    return ""


@dataclass(frozen=True)
class TargetAudio:
    codec: str
    bitrate: str
    channels: int
    channel_name: str
    sample_rate: int = CHAIN_AUDIO_SAMPLE_RATE  # no resample on this chain


def target_audio_for(source_channels: int, wiring: str = DEFAULT_WIRING) -> TargetAudio:
    """The Dolby variant this chain should get for a source channel count.

    Wiring-aware: the default ``soundbar-hdmi-in`` chain (the one this
    install runs) gets Dolby Digital Plus, which both of its two audio hops
    handle natively; the explicit ``tv-arc`` alternative keeps the
    everywhere-compatible AC-3 target. Both wirings fold anything past 5.1
    down to 5.1, because the ceiling is the encoders, not the wiring: no
    Dolby encoder in ffmpeg can emit more than
    :data:`FFMPEG_DOLBY_ENCODE_MAX_CHANNELS` channels, and asking for 7.1 is
    not a downmix request — it is a failed encode that writes nothing.
    """
    try:
        ch = int(source_channels)
    except (ValueError, TypeError):
        ch = 0
    if ch <= 0:
        # An unparseable or zero-channel source cannot encode to anything
        # meaningful; default to a safe stereo Dolby bed rather than
        # accidentally promising a 5.1 bitstream a silent/absent track
        # cannot produce. `channels_of` in audio_standardizer also clamps
        # nonsense to stereo for the same reason.
        ch = 2
    if wiring == WIRING_SOUNDBAR_HDMI_IN:
        if ch >= 5:
            # 5.1, 6.1 and 7.1+ all land at 5.1 (a 6.1 fold keeps the LFE):
            # the format would carry more, ffmpeg's encoder cannot write it,
            # and a 7.1 target is exactly the bug that made every >=7.1
            # lossless master's audiofit fail since 8.1.0. Never upmixed.
            return TargetAudio(codec="eac3", bitrate="640k", channels=6, channel_name="5.1")
        if ch in (3, 4):
            return TargetAudio(codec="eac3", bitrate="448k", channels=ch, channel_name=f"{ch}ch")
        if ch == 2:
            return TargetAudio(codec="eac3", bitrate="192k", channels=2, channel_name="2.0")
        return TargetAudio(codec="eac3", bitrate="128k", channels=1, channel_name="1.0")
    # tv-arc: AC-3, licensed-and-supported on every hop of the old path.
    # Its encoder shares the ceiling, so 7.1/6.1 fold exactly as above.
    if ch >= 5:
        return TargetAudio(codec="ac3", bitrate="640k", channels=6, channel_name="5.1")
    if ch in (3, 4):
        return TargetAudio(codec="ac3", bitrate="448k", channels=ch, channel_name=f"{ch}ch")
    if ch == 2:
        return TargetAudio(codec="ac3", bitrate="192k", channels=2, channel_name="2.0")
    return TargetAudio(codec="ac3", bitrate="96k", channels=1, channel_name="1.0")


def synthesis_target_label(source_channels: int = 6,
                           wiring: str = DEFAULT_WIRING) -> str:
    """What ``audio_standardizer.py`` bakes in, named for a one-line summary.

    Derived from :func:`target_audio_for` and written down nowhere else, so a
    step title, a dashboard row or a report header cannot keep advertising a
    codec the chain stopped synthesizing. (The audiofit step said "AC-3 5.1"
    for a whole release after the default wiring moved the target to Dolby
    Digital Plus, and 5.1 after 7.1 sources started keeping their layout.)
    """
    codec = target_audio_for(source_channels, wiring).codec
    return DOLBY_SHORT_NAMES.get(codec, codec)


def achievable_channels(cls: str, channels: int,
                        wiring: str = DEFAULT_WIRING) -> int:
    """The channel layout this track can END UP at on this chain.

    The question ``mkv_track_cleaner`` has to answer is not "which track plays
    today" but "which track can this movie end up with", because the remux is
    irreversible: a track that is dropped is gone for good, while a track that
    merely needs converting can still be converted later.

    * A **chain-native** track is already at its final layout. Nothing converts
      it and nothing improves it, so it achieves exactly what it carries — with
      one exception, and it covers the whole DTS family: what reaches the bar is
      a DTS **core**, whether the file carries a plain DTS Digital Surround
      bitstream or the core the player extracts from a DTS-HD MA/HRA or DTS:X
      track. A core tops out at 5.1 (:data:`DTS_CORE_MAX_CHANNELS`), so a 7.1
      master achieves 6, not 8 - and so does any other DTS-labelled track,
      however it was labelled. Crediting a DTS stream with 8 channels is what
      let a DTS:X track whose HD-ness sits only in the *title* outrank a genuine
      DD+ 5.1 Atmos track and get it stripped by an irreversible remux.
    * A **transcode-bound** track is not a dead end - it is precisely the input
      ``audio_standardizer.py`` synthesizes a chain-native Dolby track from - so
      it achieves that target's layout, which is at most 5.1 on EITHER wiring:
      no Dolby encoder can write wider than that, so a 7.1 master achieves 6,
      not 8 (see :data:`FFMPEG_DOLBY_ENCODE_MAX_CHANNELS`; the cap comes
      through :func:`target_audio_for` and so lands here automatically).
      Tracks needing no encoder are unaffected and achieve their own layout —
      a FLAC 7.1 arrives as LPCM 7.1 because the bar takes that as-is.
    * An **unknown** track achieves nothing, because the toolkit never
      auto-touches one: fail closed, as everywhere else here.
    """
    try:
        ch = int(channels)
    except (ValueError, TypeError):
        ch = 0
    if ch <= 0:
        return 0
    if cls in (AUDIO_DTS_HD_CORE, AUDIO_DTS_CORE):
        # The DTS core is what reaches the bar in both cases - passed through
        # as-is, or extracted from an HD bitstream by the player - and a core
        # layout tops out at 5.1 (DTS-ES 6.1 at the very widest). A DTS-HD MA
        # 7.1 track therefore achieves 6 channels, not 8, and so does a track
        # the probe only managed to call "DTS" with 8 channels: the cap is a
        # property of the format, not of how confidently the label was read.
        # Neither case loses anything at playback - both are native-band, and
        # nothing has to encode anything.
        return min(ch, DTS_CORE_MAX_CHANNELS)
    if cls in (AUDIO_NATIVE, AUDIO_DECODE_PCM):
        return ch
    if cls == AUDIO_TRANSCODE_BOUND:
        return target_audio_for(ch, wiring).channels
    return 0


# The coarsest thing this chain knows about an audio track, and the first
# thing every ranking decision compares. Three bands, not a continuum: either
# it plays with no server work at all, or it can be converted into something
# that does, or the toolkit does not recognise it and refuses to choose it.
CHAIN_BAND_NATIVE = 2            # AC-3 / DD+ / DTS (core or extracted from DTS-HD) / decoded PCM
CHAIN_BAND_TRANSCODE_BOUND = 1   # TrueHD / DTS-HD LBR / WMA Pro: convertible by audiofit
CHAIN_BAND_UNKNOWN = 0           # fail closed: reported, never chosen if anything else exists

_ATMOS_MARKERS = ("ATMOS", "JOC")


def chain_band_for(cls: str) -> int:
    """Which band of this chain a classified audio class lands in.

    The band leads every ranking decision the toolkit makes about audio:
    *plays with no server work* beats *transcode-bound* beats *unknown*,
    whatever a codec's prestige inside its own band. That is the 8.4.0
    rebalance - before it, a codec sub-tier led, so AC-3 2.0 (tier 95)
    outranked FLAC 7.1 (tier 66) and, because the remux keeps exactly ONE
    audio track, a 7.1 master was permanently destroyed to keep a stereo one.

    It lives here rather than in either tool because two tools rank audio
    pools: :func:`mkv_track_cleaner.get_audio_quality_score` for the remux and
    :func:`audio_standardizer._pool_rank` for the transcode planner, whose
    degraded branch cannot import the scorer and has to reproduce its ordering
    conventions from shared parts. A track that sits in one band for one tool
    and another band for the other is how a movie gets settled by a track the
    remux is about to delete.
    """
    if cls in (AUDIO_NATIVE, AUDIO_DTS_CORE, AUDIO_DTS_HD_CORE, AUDIO_DECODE_PCM):
        return CHAIN_BAND_NATIVE
    if cls == AUDIO_TRANSCODE_BOUND:
        return CHAIN_BAND_TRANSCODE_BOUND
    return CHAIN_BAND_UNKNOWN


def atmos_credit_for(cls: str, blob: str) -> int:
    """1 when this blob is a Dolby Digital Plus stream actually claiming Atmos.

    Atmos only exists inside DD+ (as JOC) on anything this player can emit -
    plain AC-3 has no Atmos variant at all, so an "Atmos" in a track's title is
    a release-group flourish. Crediting the word alone used to let a stereo
    AC-3 titled "Dolby Atmos 5.1" outrank genuine surround, so the credit is
    gated on the classification first and the wording second.

    Expects a blob from :func:`codec_blob` (upper-cased, positions stable);
    upper-cases again so a hand-built blob cannot silently lose the credit.
    """
    if cls != AUDIO_NATIVE or not is_dolby_digital_plus(blob):
        return 0
    upper = blob.upper()
    return 1 if any(marker in upper for marker in _ATMOS_MARKERS) else 0


def audio_chain_note(blob: str, channels: int, wiring: str = DEFAULT_WIRING,
                     *, sample_rate: object = 0, bit_depth: object = 0) -> str:
    """One human sentence describing where this track actually ends up.

    ``sample_rate``/``bit_depth`` are optional and only ever read for the
    software-decoded family, where they decide whether the stream is inside the
    player's 24-bit/96 kHz decode ceiling (:func:`exceeds_decode_ceiling`).
    Callers that have a probed stream should pass them: a note that promises
    "the Chromecast decodes this" about a 24/192 FLAC is a promise this chain
    has no evidence for.
    """
    cls = classify_audio_blob(blob)
    ch = channels or 2
    if cls == AUDIO_NATIVE:
        if ch > 2 and wiring == WIRING_TV_ARC:
            # The Chromecast does pass a Dolby bitstream out over HDMI, but on
            # this TV it passes it to the 2013 panel, not to the bar: the TV
            # offers PCM only for HDMI sources (Display.arc, user-confirmed),
            # and docs/hardware.md §3 says it downmixes every HDMI source
            # before ARC/optical, whatever the source sent. Same hedged
            # register and same `ch > 2` gate as the decode-to-PCM branch
            # below - a stereo AC-3/DD+ track folded to stereo PCM loses
            # nothing, so only a 5.1+ track is worth warning about. The limit
            # is the TV's input-side behaviour, so the ARC path is unreliable
            # rather than provably dead. Whether that PCM-only limit also
            # applies to a Dolby bitstream the TV merely *received* (as opposed
            # to audio it decoded itself) has not been measured on this unit -
            # it is what §4's AC-3 compensation assumes, and §5 records it as
            # an open question rather than settling it by assertion.
            return ("Dolby bitstream (AC-3 / DD+ Atmos): the Chromecast passes it "
                    "through, but it passes it to this TV, which offers PCM only "
                    "for HDMI sources - the ARC/optical path cannot be relied on "
                    "to carry it and this 5.1+ track may reach the bar as stereo "
                    "PCM; the default wiring (Chromecast -> soundbar HDMI IN) "
                    "bitstreams it end-to-end")
        return ("bitstreams end-to-end: Chromecast HDMI passthrough -> "
                "AX3125H decodes (Dolby Digital / DD+ Atmos)")
    if cls == AUDIO_DTS_CORE:
        if wiring == WIRING_TV_ARC:
            return ("DTS core: the soundbar decodes it, but this TV offers PCM only "
                    "for HDMI sources, so the ARC/optical path cannot be relied on "
                    "to carry it - the default wiring (Chromecast -> soundbar "
                    "HDMI IN) can")
        return ("DTS core: chipset-level passthrough from the Chromecast "
                "(works on Amlogic Android TV builds; not on Google's "
                "official list) -> AX3125H DTS decoder")
    if cls == AUDIO_DTS_HD_CORE:
        if wiring == WIRING_TV_ARC:
            return ("DTS-HD / DTS:X: the player extracts the DTS core it carries and "
                    "the bar decodes it, but this TV offers PCM only for HDMI "
                    "sources, so the ARC/optical path cannot be relied on to carry "
                    "it - the default wiring (Chromecast -> soundbar HDMI IN) can")
        return ("DTS-HD MA/HRA or DTS:X: the Chromecast cannot pass the lossless HD "
                "layer, so it extracts the backward-compatible DTS core it carries "
                "and bitstreams that - the bar decodes DTS 5.1 and its panel reads "
                "DTS, not DTS-HD/DTS:X; no server transcode")
    if cls == AUDIO_DECODE_PCM:
        over = exceeds_decode_ceiling(sample_rate, bit_depth)
        if over:
            # Reported, never promised: the decoder this family depends on is
            # specified only up to 24-bit/96 kHz here, so whether a wider stream
            # fails or is quietly resampled to the mixer's rate is not something
            # the chain's evidence settles. audio_standardizer.py puts these in
            # its review bucket and leaves the file alone - see
            # Player.max_decoded_sample_rate.
            return (f"the Chromecast's software decoder has a documented decode ceiling of "
                    f"{PLAYER.max_decoded_bit_depth}-bit/"
                    f"{PLAYER.max_decoded_sample_rate // 1000} kHz, and {over}; whether "
                    "that means a failed decode or a silent resample to 48 kHz has not "
                    "been measured on this chain, so it is reported for review, and no "
                    "Direct Play is promised over it")
        if ch > 2 and wiring == WIRING_TV_ARC:
            return ("the Chromecast decodes this to PCM, but this TV's digital audio "
                    "output offers PCM 2.0 for HDMI sources - over ARC/optical this "
                    "5.1+ track arrives as stereo unless the Chromecast plugs into "
                    "the soundbar's HDMI IN (the default wiring)")
        return ("decoded by the Chromecast to PCM " +
                ("(multichannel; the bar accepts it over HDMI IN)" if ch > 2
                 else "(stereo)") )
    if cls == AUDIO_TRANSCODE_BOUND:
        target = dolby_name(target_audio_for(ch, wiring).codec)
        return ("cannot leave the G454V (no TrueHD/WMA-Pro/DTS-Express passthrough "
                "on Android TV, and unlike DTS-HD there is no backward-compatible "
                "core to fall back to) - Jellyfin re-encodes this audio on every "
                f"play; run audio_standardizer.py to bake in native {target}")
    return "unrecognized audio format - reported, never auto-touched"


# =============================================================================
# VIDEO: what happens to a picture on THIS chain
# =============================================================================

VIDEO_NATIVE = "direct-play"
VIDEO_TONEMAPPED = "direct-play-tonemapped"   # HDR10/HDR10+/HLG -> SDR output
VIDEO_DV_FLAG = "dolby-vision-flagged"        # no DV licence on the G454V
VIDEO_OVERSIZE = "oversize-needs-downscale"   # >1080p cannot even decode here
VIDEO_UNSUPPORTED = "unsupported-codec"       # VC-1, Xvid, ProRes, ...
VIDEO_UNKNOWN = "unknown"


def classify_video(
    codec_name: str,
    *,
    width: int = 0,
    height: int = 0,
    hdr_flavors: tuple[str, ...] | list[str] = (),
    dv_profile: str = "",
) -> str:
    """Classify the video side of a movie against the chain.

    ``hdr_flavors`` and ``dv_profile`` accept exactly what bitdepth.py's HDR
    classifier already derives, so the two tools share one reading of a file.

    Fail-closed like the audio side: a missing resolution or a missing codec
    name yields :data:`VIDEO_UNKNOWN` ("review manually"), never a confident
    ``direct-play``. The size check runs against the CODED ceiling
    (:attr:`Player.max_coded_resolution`), so a 1080p movie stored as
    1920x1088 of macroblock padding is not accused of being oversize.
    """
    codec = (codec_name or "").lower()
    flavors = {f.strip().lower() for f in hdr_flavors}
    dv = bool(dv_profile) or any("dolby vision" in f for f in flavors)

    if dv:
        return VIDEO_DV_FLAG
    if width <= 0 or height <= 0:
        # No resolution means nothing here can be checked against the 1080p
        # ceiling. Reporting "direct-play" would be a guess in the unsafe
        # direction, so this is the video analogue of AUDIO_UNKNOWN.
        return VIDEO_UNKNOWN
    if (width > PLAYER.max_coded_resolution[0]
            or height > PLAYER.max_coded_resolution[1]):
        # A 4K HEVC stream is beyond the S805X2's decode block outright; the
        # server transcodes every play. (Checked before codec support: an
        # oversized file transcodes regardless.)
        return VIDEO_OVERSIZE
    if not codec or codec not in PLAYER.video_codecs:
        # An empty codec name is unknown, not unsupported: "no hardware
        # decoder" is a factual claim that needs a codec to be about.
        return VIDEO_UNKNOWN if not codec else VIDEO_UNSUPPORTED
    if flavors and ("hdr10" in flavors or "hdr10+" in flavors or "hlg" in flavors
                    or any("hdr" in f or "hlg" in f or "pq" in f or "bt2020" in f
                           for f in flavors)):
        return VIDEO_TONEMAPPED
    return VIDEO_NATIVE


def video_chain_note(verdict: str) -> str:
    return {
        VIDEO_NATIVE: "Direct Plays: H.264/HEVC/VP9/AV1 at <=1080p on the G454V",
        VIDEO_TONEMAPPED: ("Direct Plays: the Chromecast decodes HDR10/HDR10+/HLG "
                           "and tone-maps to SDR for the UN60F6350AF (never "
                           "auto-re-encoded - HDR masters stay protected)"),
        VIDEO_DV_FLAG: ("Dolby Vision is not licensed on the G454V: profile 8 "
                        "files with an HDR10 base play as HDR10 (tone-mapped); "
                        "profile 5/7 files force a server transcode - review"),
        VIDEO_OVERSIZE: ("beyond the G454V's 1080p ceiling AND beyond the TV: "
                         "every play transcodes; queue a 1080p downscale"),
        VIDEO_UNSUPPORTED: ("no hardware decoder on the G454V (and Jellyfin's "
                            "Android-TV profile will transcode it) - queue a re-encode"),
        VIDEO_UNKNOWN: "video properties unknown - fail-closed, review manually",
    }.get(verdict, verdict)


# =============================================================================
# WHOLE-CHAIN SUMMARY (doctor, reports, docs)
# =============================================================================

def chain_summary_lines(wiring: str | None = None) -> list[str]:
    """The chain as doctor prints it: devices, and what reaches each one."""
    wiring = resolve_wiring(wiring)
    if wiring == WIRING_TV_ARC:
        wiring_line = (f"Wiring : Chromecast -> {DISPLAY.model} HDMI, TV --ARC/optical--> "
                       f"{SINK.model} (explicit alternative; this TV offers PCM for "
                       "HDMI sources, so multichannel PCM arrives stereo-only)")
    else:
        wiring_line = (f"Wiring : Chromecast -> {SINK.model} HDMI IN -> {DISPLAY.model} "
                       "(default; audio never crosses the 2013 TV)")
    return [
        f"Player : {PLAYER.model} ({PLAYER.model_id} '{PLAYER.codename}', {PLAYER.soc}, {PLAYER.ram} RAM)",
        f"         video  <= {PLAYER.max_resolution[0]}x{PLAYER.max_resolution[1]}p{PLAYER.max_fps}: "
        + "/".join(c.upper() for c in PLAYER.video_codecs[:4])
        + f"; HDR {('/'.join(PLAYER.hdr_formats))} tone-mapped to SDR here; Dolby Vision NOT supported",
        f"         audio  passthrough: {', '.join(PLAYER.passthrough_audio)} "
        f"(+ {', '.join(PLAYER.passthrough_audio_unofficial)} unofficial, and the DTS core "
        "inside DTS-HD/DTS:X; the whole DTS family reaches at most 5.1); decodes "
        f"AAC/MP3/FLAC/Opus/Vorbis/WAV to PCM up to {PLAYER.max_decoded_bit_depth}-bit/"
        f"{PLAYER.max_decoded_sample_rate // 1000} kHz; NEVER TrueHD / WMA Pro; "
        "ALAC and WavPack have no decoder here",
        f"Sink   : {SINK.model} {SINK.description} — decodes "
        "DD/DD+ Atmos/TrueHD/DTS/DTS-HD/multi-PCM",
        f"Display: {DISPLAY.model} ({DISPLAY.resolution[0]}x{DISPLAY.resolution[1]} SDR, "
        f"{DISPLAY.panel_hz} Hz panel, plain ARC — no eARC)",
        wiring_line,
    ]
