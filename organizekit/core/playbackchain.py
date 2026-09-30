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
  Dolby Digital (AC-3), Dolby Digital Plus (E-AC-3, including DD+ Atmos
  via HDMI pass-through), base 5.1 DTS (chipset-level, unofficial), and
  decoded PCM. It cannot pass through TrueHD, DTS-HD or DTS:X — so a
  "better" lossless track in a file is not free quality, it is a
  guaranteed server-side audio transcode on every single play.
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
    # hence "unofficial". No TrueHD / DTS-HD / DTS:X ever leaves this box.
    passthrough_audio: tuple[str, ...] = ("ac3", "eac3", "eac3-joc")
    passthrough_audio_unofficial: tuple[str, ...] = ("dts-core",)

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
    "https://www.manualowl.com/m/Samsung/UN60F6350AF/Manual/347300 (UN60F6350AF e-manual: 'ARC is only available through the HDMI (ARC) port'; Digital Audio Output (SPDIF) formats 'may vary depending on the input source')",
    "https://www.samsung.com/sg/support/tv-audio-video/how-to-use-the-hdmi-arc-port-on-a-samsung-tv/ (Samsung support: HDMI-ARC carries PCM 2ch, Dolby Digital up to 5.1 and DTS Digital Surround up to 5.1; 2013-2014 F/H-series sound-output path)",
    "USER-CONFIRMED 2026-09 on the actual UN60F6350AF: with HDMI sources connected, the TV offers PCM only as its digital audio output format, so the ARC/optical path delivers multichannel content as stereo PCM",
    "https://jellyfin.org/docs/general/clients/codec-support/ (Jellyfin Android-TV codec support matrix: AAC/AC3/EAC3 direct)",
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
# AUDIO: what happens to a track on THIS chain
# =============================================================================

# Every audio format a movie can carry lands in exactly one of these classes.
# The string values are the stored vocabulary (reports, JSON, state cache) —
# treat them as part of the toolkit's on-disk format, not as display text.
AUDIO_NATIVE = "native-passthrough"       # AC-3 / E-AC-3(+Atmos): bitstreamed end-to-end
AUDIO_DTS_CORE = "dts-core-passthrough"   # base DTS: works here, but unofficially (chipset, not Google spec)
AUDIO_DECODE_PCM = "decode-to-pcm"        # AAC/FLAC/MP3/Opus/Vorbis/PCM: player decodes; PCM into the bar
AUDIO_TRANSCODE_BOUND = "transcode-bound" # TrueHD/DTS-HD/DTS:X/WMA Pro: the player can never emit these
AUDIO_UNKNOWN = "unknown"                 # fail-closed: reported, never auto-touched

# Tier an audio class contributes to "which track does the cleaner keep".
# The ordering IS the rework philosophy: on this chain, the *best* track is
# the best one that plays natively. Lossless HD formats sit below every
# chain-native lossy format because the G454V cannot emit them — a TrueHD
# track here is a promise that Jellyfin re-encodes the audio on every play.
CLASS_TIERS: dict[str, int] = {
    AUDIO_NATIVE: 100,
    AUDIO_DTS_CORE: 80,
    AUDIO_DECODE_PCM: 65,
    AUDIO_TRANSCODE_BOUND: 30,
    AUDIO_UNKNOWN: 10,
}


def _classify_audio_segment(b: str) -> str | None:
    """Classify authoritative codec text (never a free-form title), or None.

    Ditto-confidence rules, in order: lossless-HD markers first (a DTS-HD MA
    label contains "DTS" and a TrueHD Atmos track contains "ATMOS", so HD must
    be proven before any core), the chain-native Dolby family next, base DTS,
    then the client-decodable PCM family.
    """
    if not b.strip():
        return None
    if "TRUEHD" in b or "A_MLP" in b or b.strip() == "MLP":
        return AUDIO_TRANSCODE_BOUND
    if any(k in b for k in ("DTS-HD", "DTS/HD", "DTS:X", "DTS-X", "DTS_X",
                            "DTS HD", "DTSHD", "A_DTS/LOSSLESS")):
        return AUDIO_TRANSCODE_BOUND
    if any(k in b for k in ("WMAPRO", "WMA PRO", "WMA_LOSSLESS", "WMALOSSLESS")):
        # Android/ExoPlayer has no WMA Pro decoder; server transcodes.
        return AUDIO_TRANSCODE_BOUND
    if any(k in b for k in ("E-AC-3", "EAC3", "E_AC3", "EC-3", "EC3",
                            "DOLBY DIGITAL PLUS", "DD+", "DDPLUS")):
        return AUDIO_NATIVE
    if any(k in b for k in ("AC-3", "AC3", "A_AC3", "DOLBY DIGITAL", "DD ")):
        return AUDIO_NATIVE
    if any(t in b.split() for t in ("DTS", "DCA")) or "A_DTS" in b:
        return AUDIO_DTS_CORE
    if any(k in b for k in ("AAC", "A_AAC", "MP4A")):
        return AUDIO_DECODE_PCM
    if any(k in b for k in ("FLAC", "A_FLAC")):
        return AUDIO_DECODE_PCM
    if any(k in b for k in ("PCM", "A_PCM", "WAV", "ALAC", "WAVPACK")):
        return AUDIO_DECODE_PCM
    if any(k in b for k in ("OPUS", "A_OPUS", "VORBIS", "A_VORBIS",
                            "MP3", "MPEG/L", "MPEG AUDIO", "A_MPEG")):
        return AUDIO_DECODE_PCM
    return None


#: Codec-NAME tokens a profile field may legitimately refine. Only a bare
#: DTS codec name (mkvmerge's "DTS", ffprobe's "DTS"/"DCA") needs the second
#: field: "DTS" + profile "DTS-HD MA" is an HD master, "DTS" + "A_DTS" is a
#: core track. A codec ID ("A_DTS/HD_MA") or a full name ("DTS-HD") already
#: carries the answer, so the field after it is never consulted.
_DTS_CODEC_NAME_TOKENS = frozenset({"DTS", "DCA"})

#: Substrings that make a DTS *profile* field an HD/DTS:X variant rather than
#: a plain core. Deliberately DTS-specific: "DTS TRUEHD 7.1" is a core track
#: whose title narrates a source, and must not be promoted to an HD master.
_DTS_HD_PROFILE_MARKERS = (
    "DTS-HD", "DTS/HD", "DTS:X", "DTS-X", "DTS_X",
    "DTS HD", "DTSHD", "A_DTS/LOSSLESS", "DTS LOSSLESS",
)


def _dts_profile_is_hd(*segments: str) -> bool:
    """True when any DTS profile segment names an HD / DTS:X format."""
    return any(marker in segment
               for segment in segments if segment
               for marker in _DTS_HD_PROFILE_MARKERS)


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
    2. the **second field** may only *refine* a bare DTS codec name into an
       HD/DTS:X master (ffprobe's ``profile``, mkvmerge's codec ID);
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
    # 1. The codec-NAME field decides, in isolation. A title word can never
    #    enter this step: with no profile field present, the old code joined
    #    the first two whitespace tokens, which put the title's first word
    #    next to the codec and let "TrueHD 7.1" turn an E-AC-3 stream into a
    #    transcode candidate.
    codec_class = _classify_audio_segment(tokens[0])
    if codec_class == AUDIO_DTS_CORE and tokens[0] in _DTS_CODEC_NAME_TOKENS:
        # Only a bare DTS codec name is refined by the second field, and only
        # by DTS-specific HD vocabulary (never "TRUEHD", so a DTS core track
        # titled "TrueHD 7.1 (source)" stays core).
        second = tokens[1] if len(tokens) > 1 else ""
        if _dts_profile_is_hd(second, f"{tokens[0]} {second}".strip()):
            return AUDIO_TRANSCODE_BOUND
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
    """True when this track plays without any server-side audio transcode."""
    return classify_audio_blob(blob) in (AUDIO_NATIVE, AUDIO_DTS_CORE, AUDIO_DECODE_PCM)


# =============================================================================
# TARGET SPEC: what audio_standardizer.py synthesizes when a track is
# transcode-bound. AC-3 (Dolby Digital) is chosen deliberately:
#   * the G454V passes it through on every firmware (official Google list);
#   * the AX3125H decodes it on every input (HDMI IN, ARC, and even optical
#     fallback — the bar's only guaranteed surround path if the wiring ever
#     regresses to the 2013 TV);
#   * 5.1 @ 640 kbps is the AC-3 ceiling and the maximum the ETSI/ATSC spec
#     allows — indistinguishable here from any lossy deliverable this chain
#     could play, and stereo fold-downs stay exact.
# =============================================================================

@dataclass(frozen=True)
class TargetAudio:
    codec: str
    bitrate: str
    channels: int
    channel_name: str
    sample_rate: int = 48000  # the Chromecast's HDMI output rate; no resample


def target_audio_for(source_channels: int) -> TargetAudio:
    """The AC-3 variant this chain should get for a source channel count."""
    ch = max(1, int(source_channels or 6))
    if ch >= 5:
        # 5.1, 6.1 and 7.1 all normalize to 5.1: the bar is a 3.1.2 and the
        # back channels carry nothing here anyway.
        return TargetAudio(codec="ac3", bitrate="640k", channels=6, channel_name="5.1")
    if ch in (3, 4):
        return TargetAudio(codec="ac3", bitrate="448k", channels=ch, channel_name=f"{ch}ch")
    if ch == 2:
        return TargetAudio(codec="ac3", bitrate="192k", channels=2, channel_name="2.0")
    return TargetAudio(codec="ac3", bitrate="96k", channels=1, channel_name="1.0")


def audio_chain_note(blob: str, channels: int, wiring: str = DEFAULT_WIRING) -> str:
    """One human sentence describing where this track actually ends up."""
    cls = classify_audio_blob(blob)
    ch = channels or 2
    if cls == AUDIO_NATIVE:
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
    if cls == AUDIO_DECODE_PCM:
        if ch > 2 and wiring == WIRING_TV_ARC:
            return ("the Chromecast decodes this to PCM, but this TV's digital audio "
                    "output offers PCM 2.0 for HDMI sources - over ARC/optical this "
                    "5.1+ track arrives as stereo unless the Chromecast plugs into "
                    "the soundbar's HDMI IN (the default wiring)")
        return ("decoded by the Chromecast to PCM " +
                ("(multichannel; the bar accepts it over HDMI IN)" if ch > 2
                 else "(stereo)") )
    if cls == AUDIO_TRANSCODE_BOUND:
        return ("cannot leave the G454V (no TrueHD/DTS-HD/WMA-Pro passthrough "
                "on Android TV) - Jellyfin re-encodes this audio on every "
                "play; run audio_standardizer.py to bake in native AC-3")
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
    """
    codec = (codec_name or "").lower()
    flavors = {f.strip().lower() for f in hdr_flavors}
    dv = bool(dv_profile) or any("dolby vision" in f for f in flavors)

    if dv:
        return VIDEO_DV_FLAG
    if width > PLAYER.max_resolution[0] or height > PLAYER.max_resolution[1]:
        # A 4K HEVC stream is beyond the S805X2's decode block outright; the
        # server transcodes every play. (Checked before codec support: an
        # oversized file transcodes regardless.)
        return VIDEO_OVERSIZE
    if codec not in PLAYER.video_codecs:
        return VIDEO_UNSUPPORTED
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
        f"(+ {', '.join(PLAYER.passthrough_audio_unofficial)} unofficial); "
        "decodes AAC/FLAC/Opus/MP3 to PCM; NEVER TrueHD / DTS-HD / DTS:X",
        f"Sink   : {SINK.model} {SINK.description} — decodes "
        "DD/DD+ Atmos/TrueHD/DTS/DTS-HD/multi-PCM",
        f"Display: {DISPLAY.model} ({DISPLAY.resolution[0]}x{DISPLAY.resolution[1]} SDR, "
        f"{DISPLAY.panel_hz} Hz panel, plain ARC — no eARC)",
        wiring_line,
    ]
