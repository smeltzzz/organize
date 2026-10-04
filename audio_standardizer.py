#!/usr/bin/env python3
"""Normalize every movie's audio for the Chromecast HD -> AX3125H chain.

This toolkit serves exactly one playback chain (the researched facts live in
``organizekit/core/playbackchain.py``; the full write-up is docs/hardware.md):

    Chromecast with Google TV (HD) G454V --HDMI--> Hisense AX3125H
    (the player; the ONLY audio                   (decodes AC-3/DD+ Atmos/
     source this library is tuned for)             DTS/multichannel PCM;
                                                   audio never crosses the TV)
                                                        --HDMI OUT--> Samsung
                                                         UN60F6350AF (1080p SDR,
                                                         video pass-through)

That is ``soundbar-hdmi-in`` — the wiring this install actually runs, and the
toolkit's DEFAULT. The alternative (``tv-arc``: Chromecast into the TV, TV
--ARC/optical--> bar) stays supported via ``--wiring`` /
``ORGANIZE_PLAYBACK_WIRING``, but on this 2013 TV it is the degraded one: the
set offers PCM only for HDMI sources, so multichannel PCM arrives at the bar as
stereo.

The chain's hard rule about audio: the G454V can only ever EMIT Dolby Digital
(AC-3), Dolby Digital Plus (E-AC-3, Atmos included), base 5.1 DTS
(chipset-level, unofficial), or decoded PCM. TrueHD, DTS-HD MA/HRA, DTS:X and
WMA Pro cannot leave the box — so a movie whose only good track is lossless-HD
gets its audio re-encoded by the Jellyfin server on EVERY single play. That is
the one audio problem worth fixing offline, once, forever.

What this tool does, per movie:

  1. ffprobe the MKV (payload cached like every other tool).
  2. Classify the audio with the chain table:

       native-passthrough   AC-3 / E-AC-3            -> done, bitstreams as-is
       dts-core             base 5.1 DTS             -> done (see --no-dts-passthrough)
       decode-to-pcm        AAC/FLAC/MP3/Opus/PCM... -> done (see --wiring note)
       transcode-bound      TrueHD/DTS-HD/DTS:X/WMA  -> FIX, offline, now

  3. The fix: ffmpeg copies EVERY stream losslessly and appends one track,
     transcoded from the best source, to the wiring's chain-native Dolby
     codec at 48 kHz — Dolby Digital Plus (E-AC-3) on the default
     soundbar-hdmi-in wiring (both of its two audio hops carry it natively),
     AC-3 on the explicit tv-arc alternative; either way the surround target
     is 5.1 @ 640 kbps, capped at what ffmpeg's Dolby encoders can actually
     write (wider sources fold — a 7.1 target would fail the encode outright;
     see FFMPEG_DOLBY_ENCODE_MAX_CHANNELS). Video is never re-encoded; subtitles
     are never touched. The new track becomes
     the default, and mkv_track_cleaner.py — which the pipeline always runs
     right after this tool — then keeps exactly one audio track: the
     chain-native one, per the chain's track-tier table. Losing the lossless
     master at that point is deliberate: on this chain it is dead weight the
     player can never emit.

Safety, mirroring the cleaner's invariants:

  * read-only unless a transcode is actually required; ``--dry-run`` shows
    the plan instead;
  * a movie still hardlinked to its seeding source is ALWAYS deferred;
  * the transcode lands in a temp file beside the movie, is verified by a
    second ffprobe (video streams identical, the new Dolby track present with the
    right channel count, durations within 3 s), and only then atomically
    replaces the original — a crash mid-run leaves a stray temp file and an
    untouched original, never a half-written movie;
  * idempotent: a movie whose best-ranked (keeper) track is already
    chain-native is skipped — and because the pool is ranked with the
    cleaner's own scoring, "skipped" always means the track the cleanup
    will KEEP is one the chain can emit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Shared implementation: everything imported here is defined exactly once,
# in organizekit/core/. See tests/test_shared_core.py for the rule that
# keeps it that way.
from organizekit.core import (
    AUDIO_DECODE_PCM,
    AUDIO_DTS_CORE,
    AUDIO_NATIVE,
    AUDIO_TRANSCODE_BOUND,
    AUDIO_UNKNOWN,
    BLOB_FIELD_ABSENT,
    CLASS_TIERS,
    DEFAULT_WIRING,
    FFMPEG_DOLBY_ENCODE_MAX_CHANNELS,
    KIND_AUDIOFIT,
    PLAYER,
    SINK,
    WIRING_ENV_VAR,
    WIRING_SOUNDBAR_HDMI_IN,
    WIRING_TV_ARC,
    ExclusiveRunLock,
    LockUnavailable,
    MediaProbeCache,
    Report,
    RunLog,
    atomic_write_text,
    audio_chain_note,
    classify_audio_blob,
    codec_blob,
    default_tool_dir,
    dolby_name,
    enable_utf8_stdio,
    iter_completed,
    open_probe_cache,
    open_state,
    path_is_within,
    probe_cache_path,
    resolve_library,
    resolve_wiring,
    resolve_workers,
    run_field_smoke_test,
    target_audio_for,
    tools_home,
)

VERSION = "1.0.0"

# Logs, reports and the probe cache live next to every other tool's, outside
# the media library.
SOURCE_DIR = str(resolve_library())
LOG_FILE = str(default_tool_dir("audiofit") / "audiofit.log")
REPORT_FILE = str(default_tool_dir("audiofit") / "audiofit_report.txt")

VIDEO_EXTENSIONS = {".mkv"}  # canonical library files; MP4s carry AAC anyway
MIN_FILE_SIZE_MB = 0
MAX_CPU_WORKERS = 8
PROBE_TIMEOUT_SEC = 45
TRANSCODE_TIMEOUT_SEC = 60 * 60 * 3  # 3 h: long 7.1 TrueHD on a slow NAS disk
PROBE_SIZE = "8M"
ANALYZE_DURATION = "10M"
LOCK_NAME = ".organize_audiofit.lock"
CREATE_NO_WINDOW = 0x08000000

# Skip Plex/Jellyfin extras folders and disc-structure internals — the same
# list bitdepth.py uses, kept in sync on purpose.
SKIP_DIR_NAMES = frozenset({
    "featurettes", "extras", "specials", "shorts", "bonus",
    "behind the scenes", "deleted scenes", "interviews", "scenes",
    "trailers", "other", "samples", "sample", "clips",
    "bdmv", "certificate", "video_ts", "audio_ts",
    "subs", "subtitles", "proof", "screens", "screenshots",
})

# Stored verdict vocabulary (state cache + report; do not re-case).
STATUS_NATIVE = "native-ok"
STATUS_DTS = "dts-core-ok"
STATUS_PCM = "pcm-decode-ok"
STATUS_TRANSCODED = "transcoded-dolby"
STATUS_PLANNED = "would-transcode-dolby"   # dry-run only
STATUS_REVIEW = "review-unknown"
STATUS_DEFERRED = "deferred-seeding"
STATUS_ERROR = "error"

# The verdicts that mean "nothing further to do for this movie": what
# `organize status` counts as settled work.
SETTLED_AUDIOFIT = frozenset({STATUS_NATIVE, STATUS_DTS, STATUS_PCM, STATUS_TRANSCODED})

CATEGORY_LABELS = {
    STATUS_NATIVE: "1. CHAIN-NATIVE  —  AC-3 / E-AC-3 already present",
    STATUS_DTS: "2. DTS CORE  —  OK (bar decodes; chipset passthrough)",
    STATUS_PCM: "3. DECODE-TO-PCM  —  OK (Chromecast decodes to PCM)",
    STATUS_TRANSCODED: "4. TRANSCODED  —  chain-native Dolby baked in from a lossless track",
    STATUS_PLANNED: "4. WOULD TRANSCODE  —  dry-run plan only",
    STATUS_REVIEW: "5. REVIEW  —  unknown audio, fail-closed, untouched",
    STATUS_DEFERRED: "6. DEFERRED  —  still seeding, untouched",
    STATUS_ERROR: "7. ERRORS",
}

log = RunLog()

# =============================================================================
# CONFIG
# =============================================================================

@dataclass
class Config:
    source_dir: Path = field(default_factory=lambda: Path(SOURCE_DIR))
    log_file: Path = field(default_factory=lambda: Path(LOG_FILE))
    report_file: Path = field(default_factory=lambda: Path(REPORT_FILE))
    cache_file: Path | None = None
    use_cache: bool = True
    min_file_size_mb: float = MIN_FILE_SIZE_MB
    workers: int = MAX_CPU_WORKERS
    timeout: float = PROBE_TIMEOUT_SEC
    transcode_timeout: float = TRANSCODE_TIMEOUT_SEC
    dry_run: bool = False
    verbose: bool = False
    ffprobe: str = "ffprobe"
    ffmpeg: str = "ffmpeg"
    lock_timeout_seconds: float = 60.0
    use_state: bool = True
    state_db: Path | None = None
    wiring: str = DEFAULT_WIRING
    dts_passthrough_ok: bool = True
    limit: int = 0

    @property
    def min_bytes(self) -> int:
        return int(self.min_file_size_mb * 1024 * 1024)

@dataclass
class AudioVerdict:
    """One movie's audio verdict against the chain."""
    path: str
    status: str
    category: str
    info: str
    audio_class: str = AUDIO_UNKNOWN
    source_stream: int = -1          # ffprobe stream index the new Dolby track comes from
    source_codec: str = ""
    source_channels: int = 0
    source_lang: str = "eng"
    target: Any = None               # TargetAudio when a transcode is planned
    size_bytes: int = 0
    duration_sec: float | None = None
    error: str = ""
    elapsed_seconds: float = 0.0

CFG = Config()

# =============================================================================
# ffprobe/ffmpeg LOCATION
# =============================================================================

def find_binary(name: str, explicit: str | None = None) -> str | None:
    """Locate an ffmpeg-family binary: explicit path, PATH, then known dirs."""
    if explicit and explicit != name:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate)
    on_path = shutil.which(name) or shutil.which(f"{name}.exe")
    if on_path:
        return on_path
    here = tools_home()
    exe = ".exe" if os.name == "nt" else ""
    for candidate in (
        here / f"{name}{exe}",
        here / "ffmpeg" / f"{name}{exe}",
        Path(rf"C:\ffmpeg\bin\{name}.exe"),
        Path(rf"C:\Program Files\ffmpeg\bin\{name}.exe"),
        Path(rf"C:\Program Files\FFmpeg\bin\{name}.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def find_ffprobe(explicit: str | None = None) -> str | None:
    return find_binary("ffprobe", explicit)


def find_ffmpeg(explicit: str | None = None) -> str | None:
    return find_binary("ffmpeg", explicit)


def binary_works(binary: str) -> bool:
    try:
        proc = subprocess.run([binary, "-version"], capture_output=True, timeout=10,
                              creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

# =============================================================================
# ADAPTERS — mkvmerge-vocabulary views over ffprobe streams
# =============================================================================

def _audio_streams(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in (payload.get("streams") or []) if s.get("codec_type") == "audio"]


#: Placeholder for an absent ffprobe field in the classification blob. It
#: keeps the blob's field POSITIONS stable — the classifier reads field 1 as
#: the codec name and field 2 as the profile, so an empty slot would shift a
#: track title into the profile's place and could make a title decide the
#: codec (the E-AC-3-vs-"TrueHD 7.1" false positive).
#: Kept as a local name for the shared placeholder so the docstrings and
#: tests that talk about "-" stay readable; the value is defined once, in
#: organizekit.core.playbackchain.
_BLOB_FIELD_ABSENT = BLOB_FIELD_ABSENT


def _stream_blob(stream: dict[str, Any]) -> str:
    """Upper-case classification blob for one ffprobe audio stream.

    ``codec_name`` is authoritative, ``profile`` may refine it, and the title
    is never allowed to decide — so every field is rendered, absent ones as
    ``-``: ``"E-AC-3 - TrueHD 7.1"`` reads as E-AC-3, while an empty slot
    would make it ``"E-AC-3  TrueHD 7.1"`` and hand the title the profile's
    position.
    """
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    return codec_blob(stream.get("codec_name"), stream.get("profile"), tags.get("title"))


def _stream_language(stream: dict[str, Any]) -> str:
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    return str(tags.get("language") or "und").strip().lower() or "und"


def channels_of(stream: dict[str, Any]) -> int:
    try:
        return int(stream.get("channels") or 2)
    except (ValueError, TypeError):
        return 2


def to_cleaner_track(stream: dict[str, Any], audio_ordinal: int) -> dict[str, Any]:
    """Shape one ffprobe audio stream like an mkvmerge -J audio track.

    mkv_track_cleaner's commentary/dub/native-language helpers are the
    toolkit's single statement of *those* policies; feeding them ffprobe data
    through this adapter guarantees the two tools can never disagree about
    what a commentary track is or which language the movie speaks.
    """
    disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    codec_name = str(stream.get("codec_name") or "").lower()
    profile = str(stream.get("profile") or "")
    friendly = {
        "ac3": "AC-3", "eac3": "E-AC-3", "truehd": "TrueHD",
        "dts": f"DTS ({profile})" if profile else "DTS",
        "aac": "AAC", "flac": "FLAC", "mp3": "MP3", "opus": "Opus",
        "vorbis": "Vorbis", "alac": "ALAC",
    }.get(codec_name, (profile or codec_name or "unknown"))
    try:
        sample_rate = int(float(stream.get("sample_rate") or 48000))
    except (ValueError, TypeError):
        sample_rate = 48000
    return {
        # position among audio tracks (not the ffprobe stream index) so the
        # cleaner's helpers see the same shape mkvmerge gives them
        "id": audio_ordinal,
        "type": "audio",
        "codec": friendly,
        "properties": {
            "codec_id": (f"{codec_name} {profile}").strip(),
            "language": _stream_language(stream),
            "language_ietf": _stream_language(stream),
            "track_name": str(tags.get("title") or ""),
            "audio_channels": channels_of(stream),
            "audio_sampling_frequency": sample_rate,
            "tag_bitrate": str(stream.get("bit_rate") or "0"),
            "flag_original": bool(disposition.get("original")),
            "flag_default": bool(disposition.get("default")),
            "flag_commentary": bool(disposition.get("comment")),
            "flag_visual_impaired": bool(disposition.get("visual_impaired")),
            "flag_hearing_impaired": bool(disposition.get("hearing_impaired")),
        },
    }


def _cleaner_helpers() -> tuple[Any, Any, Any, Any]:
    """The cleaner's policy functions, import-failure-proof."""
    try:
        import mkv_track_cleaner as tc
        return (tc.is_commentary_track, tc.is_named_dub_track,
                tc.native_audio_language, tc.audio_language_token)
    except Exception:  # noqa: BLE001 - a stand-alone copy without the sibling
        # degrades rather than refuses: language = first track's, nothing is
        # commentary. A checkout always has the sibling; the .pyz packs it.
        def _never(*_a: Any, **_k: Any) -> bool:
            return False

        def _lang(candidates: list[dict[str, Any]]) -> str:
            return str((candidates[0].get("properties") or {}).get("language") or "und")

        def _token(track: dict[str, Any]) -> str:
            return str((track.get("properties") or {}).get("language") or "und")

        return _never, _never, _lang, _token


# =============================================================================
# PLANNING (pure: probe payload in, verdict out — no I/O)
# =============================================================================

def plan_for_payload(path: str, payload: dict[str, Any], cfg: Config,
                     *, size_bytes: int = 0) -> AudioVerdict:
    """Decide one movie's audio fate from its ffprobe payload.

    1. Find the movie's native-language audio pool with the *cleaner's* own
       logic (commentary/DVS and titled dubs excluded; the language comes
       from the file's own markers, never from a preference for English).
    2. The pool is ranked with the cleaner's own score function, so the
       FIRST entry is the track the cleanup will keep — and the whole verdict
       keys off that one track. If the keeper is chain-native Dolby
       (AC-3/E-AC-3), the movie bitstreams end-to-end — done. Never off
       "does some native track exist?": a native track the cleaner ranks
       below the keeper is a track the cleanup deletes, so answering for it
       would settle a movie while stripping its only playable audio (8.3.0).
       The invariant: if audiofit calls a file settled, the keeper is a
       track this chain can emit.
    3. Otherwise the pool's best track decides: base DTS is accepted (the
       AX3125H has a DTS decoder and the G454V's firmware passes core DTS)
       unless ``--no-dts-passthrough`` distrusts the unofficial passthrough;
       client-decodable tracks are fine — multichannel included, on the
       default wiring, because the soundbar's HDMI IN accepts multichannel
       PCM. Only under the explicit ``--wiring tv-arc`` alternative do the
       multichannel ones become transcode candidates (this TV's digital
       audio output offers PCM 2.0 for HDMI sources, so ARC/optical would
       deliver them as stereo);
    4. lossless-HD / WMA-Pro tracks get the wiring's chain-native Dolby
       codec synthesized from the pool's best track (Dolby Digital Plus on
       the default soundbar-hdmi-in wiring, AC-3 under tv-arc), preferring
       a lossless source over a lossy one;
    5. unknown codecs are reported, never touched (fail-closed).
    """
    is_commentary, is_named_dub, native_lang_fn, lang_token = _cleaner_helpers()
    audio = _audio_streams(payload)
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    try:
        duration = float(fmt.get("duration") or 0) or None
    except (ValueError, TypeError):
        duration = None

    if not audio:
        return AudioVerdict(path=path, status=STATUS_ERROR,
                            category=CATEGORY_LABELS[STATUS_ERROR],
                            info="no audio streams found", size_bytes=size_bytes,
                            duration_sec=duration, error="no audio streams")

    tracks = [to_cleaner_track(s, i) for i, s in enumerate(audio)]
    candidates = [t for t in tracks if not is_commentary(t, True) and not is_named_dub(t)]
    if not candidates:
        candidates = list(tracks)  # odd but real file: nothing excluded
    native = native_lang_fn(candidates)
    pool_indices = [int(t["id"]) for t in candidates if lang_token(t) == native] or \
                   [int(t["id"]) for t in candidates]
    pool = sorted(((audio[i], tracks[i]) for i in pool_indices),
                  key=_pool_rank, reverse=True)

    classified = [(s, t, classify_audio_blob(_stream_blob(s))) for s, t in pool]

    def _v(status: str, info: str, cls: str, **kw: Any) -> AudioVerdict:
        return AudioVerdict(path=path, status=status, category=CATEGORY_LABELS[status],
                            info=info, audio_class=cls, size_bytes=size_bytes,
                            duration_sec=duration, **kw)

    # Every branch below asks the same question of the same stream: the
    # RANKED BEST one, the track the cleaner's own scoring will retain. The
    # native branch must not be the exception — keying it off "any native
    # exists in the pool" let a Dolby track ranked *below* a lossless master
    # answer for the whole file, the cleanup then deleted that Dolby track,
    # and the movie was left with an audio stream this chain can never emit.
    best_stream, _best_track, best_cls = classified[0]
    best_blob = _stream_blob(best_stream)
    best_channels = channels_of(best_stream)

    if best_cls == AUDIO_NATIVE:
        return _v(STATUS_NATIVE,
                  audio_chain_note(best_blob, best_channels, cfg.wiring),
                  AUDIO_NATIVE)

    if best_cls == AUDIO_DTS_CORE and cfg.dts_passthrough_ok:
        return _v(STATUS_DTS,
                  f"{describe_stream(best_stream)} — {audio_chain_note(best_blob, best_channels, cfg.wiring)}",
                  AUDIO_DTS_CORE)

    pcm_acceptable = not (cfg.wiring == WIRING_TV_ARC and best_channels > 2)
    if best_cls == AUDIO_DECODE_PCM and pcm_acceptable:
        return _v(STATUS_PCM,
                  f"{describe_stream(best_stream)} — {audio_chain_note(best_blob, best_channels, cfg.wiring)}",
                  AUDIO_DECODE_PCM)

    if best_cls == AUDIO_UNKNOWN:
        return _v(STATUS_REVIEW,
                  f"{describe_stream(best_stream)} — {audio_chain_note(best_blob, best_channels, cfg.wiring)}",
                  AUDIO_UNKNOWN)

    # Transcode candidates: transcode-bound, plus DTS/decode-to-pcm when the
    # configuration declined them above. Prefer a lossless source track: the
    # pool is quality-ranked, and a DTS-HD MA source transcodes to better
    # Dolby than the fallback to a lossy core would.
    src_stream, _src_track, src_cls = classified[0]
    for s, t, cls in classified:
        if cls == AUDIO_TRANSCODE_BOUND:
            src_stream, _src_track, src_cls = s, t, cls
            break
    target = target_audio_for(channels_of(src_stream), cfg.wiring)
    status = STATUS_PLANNED if cfg.dry_run else STATUS_TRANSCODED
    info = (f"{describe_stream(src_stream)} -> {dolby_name(target.codec)} "
            f"{target.channel_name} @ {target.bitrate} "
            f"(video/subs untouched; {audio_chain_note(_stream_blob(src_stream), channels_of(src_stream), cfg.wiring)})")
    return _v(status, info, src_cls,
              source_stream=int(src_stream.get("index") or 0),
              source_codec=str(src_stream.get("codec_name") or ""),
              source_channels=channels_of(src_stream),
              source_lang=_stream_language(src_stream), target=target)


def _pool_rank(pair: tuple[dict[str, Any], dict[str, Any]]) -> Any:
    """Order the pool the way the cleaner would: its own score tuple first."""
    stream, track = pair
    try:
        import mkv_track_cleaner as tc
        return tc.get_audio_quality_score(track)
    except Exception:  # noqa: BLE001
        props = track.get("properties") or {}
        return (CLASS_TIERS.get(classify_audio_blob(_stream_blob(stream)), 0),
                props.get("audio_channels") or 2)


def describe_stream(stream: dict[str, Any]) -> str:
    codec = str(stream.get("profile") or stream.get("codec_name") or "unknown").upper()
    ch = channels_of(stream)
    layout = {6: "5.1", 8: "7.1", 2: "2.0", 1: "1.0", 7: "6.1"}.get(ch, f"{ch}ch")
    return f"{codec} {layout} [{_stream_language(stream)}]"

# =============================================================================
# TRANSCODE EXECUTION
# =============================================================================

def build_ffmpeg_command(cfg: Config, src: Path, dst: Path, verdict: AudioVerdict,
                         total_audio_streams: int) -> list[str]:
    """The one ffmpeg invocation per movie.

    Every stream is copied losslessly; the chosen source is ADDITIONALLY
    transcoded to a new chain-native Dolby track appended after the existing
    audio (its per-stream audio index is ``total_audio_streams``) — the
    codec comes from the wiring-aware target table: Dolby Digital Plus
    (E-AC-3) on the default soundbar-hdmi-in wiring, AC-3 under tv-arc —
    and made the container's default audio so players pick it immediately.
    """
    target = verdict.target
    out_a = total_audio_streams
    src_lang = verdict.source_lang or "eng"
    family = "Dolby Digital Plus" if target.codec == "eac3" else "Dolby Digital"
    return [
        cfg.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
        "-i", str(src),
        "-map", "0", "-c", "copy",
        "-map", f"0:{verdict.source_stream}",
        f"-c:a:{out_a}", target.codec,
        f"-b:a:{out_a}", target.bitrate,
        f"-ac:a:{out_a}", str(target.channels),
        f"-ar:a:{out_a}", str(target.sample_rate),
        "-disposition:a", "0",
        f"-disposition:a:{out_a}", "default",
        f"-metadata:s:a:{out_a}", f"language={src_lang}",
        f"-metadata:s:a:{out_a}",
        f"title={family} {target.channel_name} {target.bitrate} "
        f"(from {verdict.source_codec or 'source'}; G454V chain)",
        str(dst),
    ]


def verify_output(produced: Path, verdict: AudioVerdict, cfg: Config,
                  old_payload: dict[str, Any]) -> tuple[bool, str]:
    """Prove the new file is a strict superset before it may replace the old.

    Video streams must be description-identical (same codecs, same count),
    the appended audio track must be the Dolby codec that was asked for with
    the right channel count, and the duration may only drift within
    tolerance — the same contract the cleaner verifies on a remux.
    """
    try:
        new_payload = run_ffprobe(cfg.ffprobe, produced, cfg)
    except RuntimeError as exc:
        return False, f"verification ffprobe failed: {exc}"
    try:
        old_video = [s.get("codec_name") for s in old_payload.get("streams") or []
                     if s.get("codec_type") == "video"]
        new_video = [s.get("codec_name") for s in new_payload.get("streams") or []
                     if s.get("codec_type") == "video"]
        if old_video != new_video:
            return False, f"video stream set changed ({old_video} -> {new_video})"
        old_audio = _audio_streams(old_payload)
        new_audio = _audio_streams(new_payload)
        if len(new_audio) != len(old_audio) + 1:
            return False, f"audio count {len(old_audio)} -> {len(new_audio)}, expected +1"
        added = new_audio[-1]
        wanted = verdict.target.codec
        if str(added.get("codec_name")) != wanted:
            return False, f"appended track is {added.get('codec_name')}, not {wanted}"
        if channels_of(added) != verdict.target.channels:
            return False, (f"appended {wanted} track is {channels_of(added)}ch, "
                           f"expected {verdict.target.channels}ch")
        old_dur = float((old_payload.get("format") or {}).get("duration") or 0)
        new_dur = float((new_payload.get("format") or {}).get("duration") or 0)
        if old_dur and abs(new_dur - old_dur) > 3.0:
            return False, f"duration drifted {old_dur:.1f}s -> {new_dur:.1f}s"
    except (ValueError, TypeError, KeyError) as exc:
        return False, f"verification could not read probes: {exc}"
    return True, ""


def hardlink_count(path: Path) -> int:
    """Visible hardlink count; >1 means the movie is still seeded somewhere."""
    try:
        return max(1, int(path.stat().st_nlink))
    except (OSError, AttributeError):
        return 1


def transcode_movie(verdict: AudioVerdict, old_payload: dict[str, Any], cfg: Config) -> AudioVerdict:
    """Apply one planned transcode with the toolkit's safety invariants."""
    started = time.monotonic()
    src = Path(verdict.path)
    tmp = src.with_name(f".{src.stem}.audiofit-{os.getpid()}.tmp.mkv")
    tmp.unlink(missing_ok=True)
    try:
        free = shutil.disk_usage(str(src.parent)).free
        if free < int(verdict.size_bytes * 1.15):
            verdict.error = (f"not enough free space beside the movie "
                             f"({free} free, need ~{int(verdict.size_bytes * 1.15)})")
            verdict.status = STATUS_ERROR
            verdict.category = CATEGORY_LABELS[STATUS_ERROR]
            return verdict
        cmd = build_ffmpeg_command(cfg, src, tmp, verdict,
                                   total_audio_streams=len(_audio_streams(old_payload)))
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=cfg.transcode_timeout,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if proc.returncode != 0 or not tmp.is_file():
            verdict.error = f"ffmpeg failed: {(proc.stderr or proc.stdout or '').strip()[:400]}"
            verdict.status = STATUS_ERROR
            verdict.category = CATEGORY_LABELS[STATUS_ERROR]
            return verdict
        ok, why = verify_output(tmp, verdict, cfg, old_payload)
        if not ok:
            verdict.error = f"verification refused the transcode: {why}"
            verdict.status = STATUS_ERROR
            verdict.category = CATEGORY_LABELS[STATUS_ERROR]
            return verdict
        os.replace(tmp, src)  # atomic same-directory publish
        verdict.elapsed_seconds = round(time.monotonic() - started, 2)
        return verdict
    except subprocess.TimeoutExpired:
        verdict.error = f"ffmpeg timed out after {cfg.transcode_timeout:.0f}s"
        verdict.status = STATUS_ERROR
        verdict.category = CATEGORY_LABELS[STATUS_ERROR]
        return verdict
    except OSError as exc:
        verdict.error = str(exc)
        verdict.status = STATUS_ERROR
        verdict.category = CATEGORY_LABELS[STATUS_ERROR]
        return verdict
    finally:
        tmp.unlink(missing_ok=True)

# =============================================================================
# DISCOVERY + PROBE (the same discipline as bitdepth.py)
# =============================================================================

def is_skipped_dir(name: str) -> bool:
    return name.lower() in SKIP_DIR_NAMES


def is_junk_name(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith("."):
        return True
    stem = Path(lowered).stem
    return (stem == "sample" or lowered.startswith(("sample-", "-sample"))
            or lowered.endswith(".sample.mkv"))


def discover_videos(root: Path, cfg: Config) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not is_skipped_dir(d) and not d.startswith(".")
                       and not d.startswith(".aria_tmp")]
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() not in VIDEO_EXTENSIONS or is_junk_name(name):
                continue
            try:
                if p.stat().st_size < cfg.min_bytes:
                    continue
            except OSError:
                continue
            found.append(p)
    found.sort(key=lambda p: str(p).lower())
    if cfg.limit > 0:
        found = found[: cfg.limit]
    return found


def run_ffprobe(binary: str, file_path: Path, cfg: Config) -> dict[str, Any]:
    cmd = [
        binary, "-v", "error", "-hide_banner",
        "-probesize", PROBE_SIZE, "-analyzeduration", ANALYZE_DURATION,
        "-show_entries",
        "stream=index,codec_name,codec_type,profile,channels,channel_layout,"
        "sample_rate,bit_rate,duration,disposition:stream_tags:format=duration,size,bit_rate",
        "-of", "json", str(file_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=cfg.timeout,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffprobe timed out after {cfg.timeout:.0f}s") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to launch ffprobe: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"ffprobe failed: {err[:400]}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ffprobe JSON was not an object")
    return payload


def probe_payload(file_path: Path, cfg: Config, cache: MediaProbeCache | None) -> dict[str, Any]:
    """ffprobe JSON for one file, through the shared probe cache."""
    stat = file_path.stat()
    payload = cache.get(file_path, stat.st_size, stat.st_mtime_ns) if cache is not None else None
    if payload is None:
        payload = run_ffprobe(cfg.ffprobe, file_path, cfg)
        if cache is not None:
            cache.put(file_path, stat.st_size, stat.st_mtime_ns, payload)
    return payload

# =============================================================================
# SCAN DRIVER
# =============================================================================

def evaluate_file(file_path: Path, cfg: Config, cache: MediaProbeCache | None) -> tuple[AudioVerdict, dict[str, Any] | None]:
    """Probe + plan one movie. Transcodes happen in the caller, serially."""
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return (AudioVerdict(path=str(file_path), status=STATUS_ERROR,
                             category=CATEGORY_LABELS[STATUS_ERROR], info=str(exc),
                             error=str(exc)), None)
    try:
        payload = probe_payload(file_path, cfg, cache)
    except Exception as exc:  # noqa: BLE001 - one unreadable file is a report row, not a crash
        return (AudioVerdict(path=str(file_path), status=STATUS_ERROR,
                             category=CATEGORY_LABELS[STATUS_ERROR], info=str(exc),
                             size_bytes=size, error=str(exc)), None)
    verdict = plan_for_payload(str(file_path), payload, cfg, size_bytes=size)
    if verdict.status in (STATUS_PLANNED, STATUS_TRANSCODED):
        links = hardlink_count(file_path)
        if links > 1:
            verdict.status = STATUS_DEFERRED
            verdict.category = CATEGORY_LABELS[STATUS_DEFERRED]
            verdict.info = (f"{links} hardlinks — still hardlinked to a seeding source, so the "
                            "replace step is deferred until seeding stops (plan kept: "
                            f"{verdict.source_codec} -> {dolby_name(verdict.target.codec) if verdict.target else 'Dolby'} "
                            f"{verdict.target.channel_name if verdict.target else ''})")
            return verdict, None
    return verdict, payload


def scan(cfg: Config) -> int:
    log("=" * 79)
    log("AUDIO STANDARDIZER — chain-native Dolby audio for the Chromecast HD (G454V) chain")
    log("=" * 79)
    log(f"Library                : {cfg.source_dir}")
    log(f"Wiring                 : {cfg.wiring}"
        + (" (recommended)" if cfg.wiring == WIRING_SOUNDBAR_HDMI_IN
           else " (degraded: plain ARC/optical — see docs/hardware.md)"))
    log(f"DTS core accepted      : {'yes' if cfg.dts_passthrough_ok else 'no (transcode too)'}")
    log(f"Dry run                : {cfg.dry_run}")
    log(f"ffprobe                : {cfg.ffprobe}")
    log(f"ffmpeg                 : {cfg.ffmpeg}")
    log(f"Log                    : {cfg.log_file}")
    log(f"Report                 : {cfg.report_file}")
    log("")

    # Housekeeping: temp files from a run that died mid-transcode must never
    # masquerade as movies; they always carry this tool's marker name.
    for stray in cfg.source_dir.rglob("*.audiofit-*.tmp.mkv"):
        try:
            stray.unlink()
            log(f"Removed stale temp file: {stray.name}")
        except OSError:
            pass

    files = discover_videos(cfg.source_dir, cfg)
    log(f"Found {len(files)} MKV movie file(s).")
    if not files:
        write_report([], cfg, 0.0, applied=[])
        log("Nothing to inspect.")
        return 0

    cache = open_probe_cache(cfg.cache_file, tool="audiofit", enabled=cfg.use_cache,
                             state_enabled=cfg.use_state, state_db=cfg.state_db,
                             legacy=None)
    if cache.enabled:
        log(f"Probe cache: {cache.path} ({len(cache)} entries loaded)")

    results: list[AudioVerdict] = []
    plans: list[tuple[AudioVerdict, dict[str, Any]]] = []
    started = time.perf_counter()
    total = len(files)
    workers = resolve_workers(cfg.workers, items=len(files), cap=MAX_CPU_WORKERS)
    done = 0
    live = log.live
    live_started = time.monotonic()
    try:
        for outcome in iter_completed(files, lambda p: evaluate_file(p, cfg, cache),
                                      workers=workers):
            done += 1
            path = outcome.item
            if outcome.error is not None:
                results.append(AudioVerdict(path=str(path), status=STATUS_ERROR,
                                            category=CATEGORY_LABELS[STATUS_ERROR],
                                            info=str(outcome.error), error=str(outcome.error)))
            else:
                verdict, payload = outcome.value
                results.append(verdict)
                if verdict.status in (STATUS_PLANNED, STATUS_TRANSCODED) and payload is not None:
                    plans.append((verdict, payload))
            tag = {STATUS_NATIVE: "NATIVE", STATUS_DTS: "DTS", STATUS_PCM: "PCM",
                   STATUS_PLANNED: "PLAN", STATUS_TRANSCODED: "JOB", STATUS_REVIEW: "REVIEW",
                   STATUS_DEFERRED: "SEEDING", STATUS_ERROR: "ERROR"}.get(results[-1].status, "?")
            log(f"[{done}/{total}] {tag:<8} {path.name}")
            if live is not None:
                live.progress(done, total, label="probing", detail=path.name, started=live_started)
    except KeyboardInterrupt:
        log("\nInterrupted — writing partial results; nothing was modified.")
    finally:
        if live is not None:
            live.clear()
    cache.save()
    if cfg.use_cache:
        log(f"Probe cache: {cache.hits} reused, {cache.misses} probed.")

    applied: list[AudioVerdict] = []
    if plans and not cfg.dry_run:
        log("")
        log(f"Applying {len(plans)} transcode(s) (video is copied, never re-encoded):")
        for index, (verdict, payload) in enumerate(plans, 1):
            if not Path(verdict.path).is_file():
                continue
            log(f"  [{index}/{len(plans)}] {Path(verdict.path).name}: "
                f"{verdict.source_codec} {verdict.source_channels}ch -> "
                f"{dolby_name(verdict.target.codec)} "
                f"{verdict.target.channel_name} @ {verdict.target.bitrate}")
            result = transcode_movie(verdict, payload, cfg)
            for i, row in enumerate(results):
                if row.path == result.path:
                    results[i] = result
                    break
            applied.append(result)
            if result.error:
                log(f"      ERROR: {result.error}", level="ERROR")

    publish_state(results, cfg)
    elapsed = time.perf_counter() - started
    if not write_report(results, cfg, elapsed, applied=applied):
        return 2

    by = {s: sum(1 for r in results if r.status == s) for s in CATEGORY_LABELS}
    log("")
    log("=" * 79)
    log("SCAN COMPLETE")
    log("=" * 79)
    log(f"  Chain-native already     : {by[STATUS_NATIVE]}")
    log(f"  DTS core (OK here)       : {by[STATUS_DTS]}")
    log(f"  Decode-to-PCM (OK here)  : {by[STATUS_PCM]}")
    dolby_label = dolby_name(target_audio_for(6, cfg.wiring).codec)
    if cfg.dry_run:
        log(f"  Would transcode to Dolby : {by[STATUS_PLANNED]} ({dolby_label})")
    else:
        failed = [r for r in applied if r.error]
        log(f"  Transcoded to Dolby      : {len(applied) - len(failed)} done, {len(failed)} failed ({dolby_label})")
    log(f"  Review (unknown audio)   : {by[STATUS_REVIEW]}")
    log(f"  Deferred (still seeding) : {by[STATUS_DEFERRED]}")
    log(f"  Errors                   : {by[STATUS_ERROR]}")
    log("=" * 79)
    log(f"Report : {cfg.report_file}")
    return 1 if by[STATUS_ERROR] else 0

# =============================================================================
# REPORT + STATE
# =============================================================================

def build_report(results: Sequence[AudioVerdict], cfg: Config, elapsed: float,
                 applied: Sequence[AudioVerdict]) -> str:
    groups: dict[str, list[AudioVerdict]] = {status: [] for status in CATEGORY_LABELS}
    for item in results:
        groups.setdefault(item.status, []).append(item)
    for bucket in groups.values():
        bucket.sort(key=lambda r: Path(r.path).name.casefold())

    topology = (f"{PLAYER.model_id} Chromecast -> TV -> {SINK.model} (ARC/optical)"
                if cfg.wiring == WIRING_TV_ARC
                else f"{PLAYER.model_id} Chromecast -> {SINK.model} (HDMI IN) -> TV (default)")
    report = Report(
        "AUDIO STANDARDIZER — CHAIN REPORT",
        f"Chain: {topology} · can the player emit this movie's audio natively?",
    )
    from datetime import datetime
    report.metas([
        ("Generated", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")),
        ("Library", cfg.source_dir),
        ("Wiring", cfg.wiring),
        ("Movies inspected", len(results)),
        ("Mode", "dry-run (no file modified)" if cfg.dry_run else "apply"),
        ("Elapsed", f"{elapsed:.1f}s"),
        ("Report", cfg.report_file),
    ])

    by = {s: len(groups[s]) for s in CATEGORY_LABELS}
    report.blank()
    synth = dolby_name(target_audio_for(6, cfg.wiring).codec)
    report.scorecard([
        ((by[STATUS_PLANNED] if cfg.dry_run else sum(1 for r in applied if not r.error)),
         f"{synth} transcodes", "planned (dry-run)" if cfg.dry_run else "completed this run"),
        (by[STATUS_NATIVE], "Already chain-native", "AC-3/E-AC-3: bitstreams as-is"),
        (by[STATUS_DTS] + by[STATUS_PCM], "Native via decode/DTS", "no action on this chain"),
        (by[STATUS_REVIEW], "Human review", "unknown audio — fail-closed, untouched"),
        (by[STATUS_DEFERRED], "Deferred (seeding)", "still hardlinked to a seed"),
        (by[STATUS_ERROR], "Errors", "not modified"),
        (len(results), "Movies inspected", "every MKV in the library"),
    ])
    report.paragraph(
        "The chain's hard rule: the Chromecast with Google TV (HD) G454V can emit "
        "Dolby Digital (AC-3), Dolby Digital Plus (E-AC-3, Atmos included), base "
        "5.1 DTS (unofficial) and decoded PCM — never TrueHD, DTS-HD, DTS:X or "
        "WMA Pro. A movie whose best track is lossless-HD is audio-transcoded by "
        "the Jellyfin server on every play, which is why one "
        f"{synth} track is synthesized here, once, from the best lossless "
        "source (video copied untouched, subtitles untouched). mkv_track_cleaner.py "
        "runs next and keeps exactly that chain-native track; the foreign-language "
        "dubs, commentary and the lossless master leave the file there."
    )

    ordered = [STATUS_PLANNED if cfg.dry_run else STATUS_TRANSCODED,
               STATUS_REVIEW, STATUS_DEFERRED, STATUS_ERROR,
               STATUS_NATIVE, STATUS_DTS, STATUS_PCM]
    intros = {
        STATUS_TRANSCODED: "Action: none left — the chain-native Dolby track is in the file now; "
                           "the cleaner will keep it and drop the lossless master.",
        STATUS_PLANNED: "Action: run without --dry-run to bake these in, then let the cleaner keep the new track.",
        STATUS_REVIEW: "Action: inspect by hand. Unknown audio is never auto-touched.",
        STATUS_DEFERRED: "Action: nothing — rerun once seeding stops and these transcode normally.",
        STATUS_ERROR: "Action: read the error lines; no listed file was modified.",
        STATUS_NATIVE: "Action: none. Dolby Digital / Digital Plus already bitstreams end-to-end.",
        STATUS_DTS: "Action: none on this chain. Distrust the unofficial DTS passthrough? "
                    "Rerun with --no-dts-passthrough to transcode these to the "
                    "chain-native Dolby codec too.",
        STATUS_PCM: "Action: none. The Chromecast decodes these to PCM; over the soundbar's "
                    "HDMI IN even multichannel PCM plays.",
    }
    for status in ordered:
        items = groups.get(status) or []
        report.section(CATEGORY_LABELS[status], count=len(items), total=len(results),
                       intro=intros[status])
        if not items:
            report.paragraph("None found.")
            continue
        for position, item in enumerate(items, start=1):
            fields: list[tuple[str, str]] = [
                ("Path", item.path),
                ("Chain", item.info),
            ]
            if item.source_codec:
                fields.append(("Source", f"{item.source_codec} {item.source_channels}ch"
                                         + (f" -> {dolby_name(item.target.codec)} "
                                            f"{item.target.channel_name} @ {item.target.bitrate}"
                                            if item.target else "")))
            if item.elapsed_seconds:
                fields.append(("Transcode", f"{item.elapsed_seconds:.1f}s"))
            if item.error:
                fields.append(("Error", item.error))
            report.entry(Path(item.path).name, ordinal=position, fields=fields)

    footer = [
        "native-ok = AC-3/E-AC-3 on board: Dolby licenses every hop of this chain (official G454V passthrough).",
        "dts-core-ok = base 5.1 DTS: Amlogic firmware passes it, the AX3125H decodes it; unofficial but real.",
        "pcm-decode-ok = AAC/FLAC/MP3/Opus/PCM: the Chromecast decodes; HDMI IN accepts multichannel PCM.",
        f"transcoded-dolby = TrueHD/DTS-HD/DTS:X/WMA Pro can never leave the G454V, so {synth} was synthesized (@ 640 kbps surround).",
        "review-unknown = unrecognized audio; fail-closed, untouched. deferred-seeding = still hardlinked to a seed.",
        "Facts and wiring: organizekit/core/playbackchain.py · full write-up: docs/hardware.md.",
    ]
    report.footer(footer)
    return report.render()


def write_report(results: Sequence[AudioVerdict], cfg: Config, elapsed: float,
                 applied: Sequence[AudioVerdict]) -> bool:
    try:
        atomic_write_text(cfg.report_file, build_report(results, cfg, elapsed, applied))
        return True
    except OSError as exc:
        log(f"[ERROR] Cannot write report {cfg.report_file}: {exc}")
        return False


def publish_state(results: list[AudioVerdict], cfg: Config) -> int:
    """Store each movie's audio verdict for `organize status` to count."""
    store = open_state(cfg.state_db, enabled=cfg.use_state, tool="audiofit")
    if not store.enabled:
        return 0
    published = 0
    try:
        for result in results:
            store.record(Path(result.path), KIND_AUDIOFIT, result.status,
                         result.info or result.category)
            published += 1
        store.note("audiofit", f"{published} movie(s) audio-checked")
    except Exception as exc:  # noqa: BLE001 - a cache write can never fail a run
        log(f"state cache not updated: {exc}", level="WARNING")
    finally:
        store.close()
    return published

# =============================================================================
# CLI
# =============================================================================

def validate_config(cfg: Config) -> list[str]:
    errors: list[str] = []
    if not cfg.source_dir.is_dir():
        errors.append(f"--source is not an accessible directory: {cfg.source_dir}")
    if cfg.min_file_size_mb < 0:
        errors.append("--min-size must be zero or greater")
    if cfg.workers <= 0:
        errors.append("--workers must be greater than zero")
    if cfg.timeout <= 0:
        errors.append("--timeout must be greater than zero")
    if cfg.lock_timeout_seconds < 0:
        errors.append("--lock-timeout must be zero or greater")
    if cfg.wiring not in (WIRING_SOUNDBAR_HDMI_IN, WIRING_TV_ARC):
        errors.append(f"--wiring must be one of: {WIRING_SOUNDBAR_HDMI_IN}, {WIRING_TV_ARC}")
    if path_is_within(cfg.report_file, cfg.source_dir):
        errors.append(f"Report path must be outside --source: {cfg.report_file}")
    if path_is_within(cfg.log_file, cfg.source_dir):
        errors.append(f"Log path must be outside --source: {cfg.log_file}")
    cache_path = probe_cache_path(cfg.cache_file, state_db=cfg.state_db)
    if cfg.use_cache and path_is_within(cache_path, cfg.source_dir):
        errors.append(f"Cache path must be outside --source: {cache_path}")
    if cfg.state_db is not None and path_is_within(cfg.state_db, cfg.source_dir):
        errors.append(f"State cache must be outside --source: {cfg.state_db}")
    if os.path.normcase(os.path.normpath(str(cfg.log_file))) == \
            os.path.normcase(os.path.normpath(str(cfg.report_file))):
        errors.append("--log and --report must be different files")
    return errors


def run_lock_path(source_dir: Path) -> Path:
    key = hashlib.sha256(
        str(source_dir.resolve(strict=False)).encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"{LOCK_NAME}.{key}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio_standardizer.py",
        description=(
            "Make every movie's audio playable end-to-end on the Chromecast with "
            "Google TV (HD) G454V -> Hisense AX3125H chain: synthesize a native "
            "Dolby track (Dolby Digital Plus on the default soundbar-hdmi-in "
            "wiring, AC-3 under tv-arc) from the TrueHD/DTS-HD sources the "
            "player can never emit."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--source", type=Path, default=Path(SOURCE_DIR),
                        help=f"Movie library root (default: {SOURCE_DIR})")
    parser.add_argument("--log", type=Path, default=Path(LOG_FILE),
                        help=f"Append-only log file (default: {LOG_FILE})")
    parser.add_argument("--report", type=Path, default=Path(REPORT_FILE),
                        help=f"Text report output (default: {REPORT_FILE})")
    parser.add_argument("--cache", type=Path, default=None,
                        help="Probe cache location (default: inside the shared state cache dir)")
    parser.add_argument("--no-cache", dest="use_cache", action="store_false", default=True,
                        help="Do not reuse ffprobe output between runs")
    parser.add_argument("--state-db", type=Path, default=None,
                        help="Shared state cache location (default: beside the reports)")
    parser.add_argument("--no-state", dest="no_state", action="store_true", default=False,
                        help="Do not publish verdicts to the shared state cache")
    parser.add_argument("--min-size", type=float, default=MIN_FILE_SIZE_MB,
                        help=f"Minimum file size in MB (default: {MIN_FILE_SIZE_MB:g})")
    parser.add_argument("--workers", type=int, default=0,
                        help="Parallel ffprobe workers (default: CPU count, max 8)")
    parser.add_argument("--timeout", type=float, default=PROBE_TIMEOUT_SEC,
                        help=f"Per-file ffprobe timeout in seconds (default: {PROBE_TIMEOUT_SEC:g})")
    parser.add_argument("--transcode-timeout", type=float, default=TRANSCODE_TIMEOUT_SEC,
                        help=f"Per-file ffmpeg timeout in seconds (default: {TRANSCODE_TIMEOUT_SEC:g})")
    parser.add_argument("--lock-timeout", type=float, default=60.0,
                        help="Seconds to wait for the run lock (default: 60)")
    parser.add_argument("--ffprobe", default="ffprobe", help="Path to ffprobe")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="Path to ffmpeg")
    parser.add_argument("--wiring", choices=(WIRING_SOUNDBAR_HDMI_IN, WIRING_TV_ARC),
                        default=None,
                        help=(f"How the chain is cabled. '{WIRING_SOUNDBAR_HDMI_IN}' "
                              "(default, and how this chain is wired): Chromecast into the "
                              "soundbar's HDMI IN, so multichannel PCM (5.1+ AAC/FLAC/PCM) "
                              "decoded by the player plays as-is. "
                              f"'{WIRING_TV_ARC}' (explicit alternative): Chromecast into "
                              "the TV, audio returned to the soundbar over the TV's "
                              "ARC/optical lead — this TV offers PCM only for HDMI "
                              "sources, so multichannel PCM arrives stereo-only and 5.1+ "
                              f"decode-to-pcm movies are transcoded too. Env: "
                              f"{WIRING_ENV_VAR}."))
    parser.add_argument("--no-dts-passthrough", dest="dts_passthrough_ok",
                        action="store_false", default=True,
                        help="Treat base DTS as transcode-bound (some Android-TV builds "
                             "or apps decline the unofficial DTS passthrough)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Probe and show the plan; never modify any file")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N movies (testing)")
    parser.add_argument("--nice", action="store_true",
                        help="Lower CPU priority for the ffmpeg phase (best-effort)")
    parser.add_argument("--verbose", action="store_true", help="Extra logging")
    parser.add_argument("--self-test", action="store_true", help="Run the built-in field smoke test")
    return parser


def cfg_from_args(args: argparse.Namespace) -> Config:
    # `--workers 0` means "decide", and the decision is the *shared* one. This
    # used to expand it inline with `os.cpu_count() or 4`, which ignored the
    # cap: on a 16- or 64-core host `cfg.workers` became the raw core count and
    # this tool's own "--workers ... max 8" help text was simply untrue - the
    # limit was only applied later, by accident, at the point where the run
    # happened to call `resolve_workers` again. That is where the cap lives.
    workers = resolve_workers(args.workers, cap=MAX_CPU_WORKERS)
    return Config(
        source_dir=args.source,
        log_file=args.log,
        report_file=args.report,
        cache_file=args.cache,
        use_cache=bool(args.use_cache),
        min_file_size_mb=args.min_size,
        workers=workers,
        timeout=args.timeout,
        transcode_timeout=args.transcode_timeout,
        dry_run=bool(args.dry_run),
        verbose=bool(args.verbose),
        ffprobe=args.ffprobe,
        ffmpeg=args.ffmpeg,
        lock_timeout_seconds=args.lock_timeout,
        use_state=not bool(args.no_state),
        state_db=args.state_db,
        wiring=resolve_wiring(args.wiring),
        dts_passthrough_ok=bool(args.dts_passthrough_ok),
        limit=args.limit,
    )


def maybe_be_nice() -> None:
    """Optionally de-prioritize: ffmpeg is the only heavy step here."""
    try:
        os.nice(10)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_tests()
    try:
        enable_utf8_stdio()
        cfg = cfg_from_args(args)
        errors = validate_config(cfg)
        if errors:
            for error in errors:
                log(f"Configuration error: {error}", level="CRITICAL", log_file=None)
            return 2
        log.file = cfg.log_file
        log.attach_live()
        ffprobe_bin = find_ffprobe(args.ffprobe)
        if not ffprobe_bin or not binary_works(ffprobe_bin):
            log("[CRITICAL] ffprobe not found or not runnable.")
            log("Install FFmpeg and add it to PATH, or pass --ffprobe /path/to/ffprobe")
            return 2
        cfg.ffprobe = ffprobe_bin
        ffmpeg_bin = find_ffmpeg(args.ffmpeg)
        if not ffmpeg_bin or not binary_works(ffmpeg_bin):
            if cfg.dry_run:
                log("[WARNING] ffmpeg not found; dry-run continues (plan only).")
                cfg.ffmpeg = ffmpeg_bin or args.ffmpeg
            else:
                log("[CRITICAL] ffmpeg not found or not runnable.")
                log("Install FFmpeg and add it to PATH, or pass --ffmpeg /path/to/ffmpeg "
                    "— or use --dry-run for a plan-only run.")
                return 2
        else:
            cfg.ffmpeg = ffmpeg_bin
        if args.nice:
            maybe_be_nice()
        global CFG
        CFG = cfg
        try:
            with ExclusiveRunLock(run_lock_path(cfg.source_dir),
                                  cfg.lock_timeout_seconds,
                                  busy_message="another audio standardizer run holds {path}"):
                return scan(cfg)
        except LockUnavailable as exc:
            log(f"[CRITICAL] Run lock unavailable: {exc}")
            return 3
    except KeyboardInterrupt:
        log("Interrupted")
        return 130
    except Exception:  # noqa: BLE001 - last resort: one exit code, not a traceback
        traceback.print_exc()
        return 1


def run_self_tests() -> int:
    """Field smoke test: the chain audio table must hold on this machine."""
    return run_field_smoke_test("audio_standardizer.py", [
        ("TrueHD is transcode-bound on the G454V",
         lambda: classify_audio_blob("TRUEHD A_TRUEHD") == AUDIO_TRANSCODE_BOUND),
        ("E-AC-3 is chain-native",
         lambda: classify_audio_blob("E-AC-3 A_EAC3") == AUDIO_NATIVE),
        ("AC-3 is chain-native",
         lambda: classify_audio_blob("AC-3 A_AC3") == AUDIO_NATIVE),
        ("DTS-HD MA is transcode-bound while base DTS is not",
         lambda: classify_audio_blob("DTS-HD MA") == AUDIO_TRANSCODE_BOUND
         and classify_audio_blob("DTS A_DTS") == AUDIO_DTS_CORE),
        ("AAC/FLAC decode to PCM (still native on this chain)",
         lambda: classify_audio_blob("AAC") == AUDIO_DECODE_PCM
         and classify_audio_blob("FLAC") == AUDIO_DECODE_PCM),
        ("the default wiring folds a 7.1 master into Dolby Digital Plus 5.1 @ 640k",
         lambda: (lambda t: t.codec == "eac3" and t.channels == FFMPEG_DOLBY_ENCODE_MAX_CHANNELS
                  and t.bitrate == "640k")(target_audio_for(8))),
        ("the tv-arc alternative still targets AC-3 5.1 @ 640k",
         lambda: (lambda t: t.codec == "ac3" and t.channels == 6
                  and t.bitrate == "640k")(target_audio_for(8, WIRING_TV_ARC))),
        ("a TrueHD movie plans a chain-native Dolby transcode",
         _smoke_truehd_plans_transcode),
        ("unknown audio is reviewed, never touched",
         _smoke_unknown_is_reviewed),
        ("a chain-native movie needs nothing",
         _smoke_native_is_done),
    ])


def _smoke_plan(payload: dict[str, Any], status: str) -> Any:
    return plan_for_payload("x.mkv", payload, Config(dry_run=True))


def _smoke_truehd_plans_transcode() -> bool:
    verdict = _smoke_plan({
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            {"index": 1, "codec_type": "audio", "codec_name": "truehd", "channels": 8,
             "tags": {"language": "eng"}, "disposition": {"default": 1}},
        ],
        "format": {"duration": "7200.0"},
    }, STATUS_PLANNED)
    # Default wiring (soundbar-hdmi-in): the target is Dolby Digital Plus,
    # folded to the widest layout its encoder can write — the fixture's
    # 8-channel master becomes a 5.1 bed, never a promised-but-failing 7.1.
    return (verdict.status == STATUS_PLANNED and verdict.target is not None
            and verdict.target.codec == "eac3"
            and verdict.target.channels == FFMPEG_DOLBY_ENCODE_MAX_CHANNELS
            and verdict.source_stream == 1)


def _smoke_unknown_is_reviewed() -> bool:
    verdict = _smoke_plan({
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 1, "codec_type": "audio", "codec_name": "gsm_ms", "channels": 2,
             "tags": {"language": "eng"}, "disposition": {}},
        ],
        "format": {"duration": "3600.0"},
    }, STATUS_REVIEW)
    return verdict.status == STATUS_REVIEW


def _smoke_native_is_done() -> bool:
    verdict = _smoke_plan({
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6,
             "tags": {"language": "eng"}, "disposition": {"default": 1}},
        ],
        "format": {"duration": "3600.0"},
    }, STATUS_NATIVE)
    return verdict.status == STATUS_NATIVE


if __name__ == "__main__":
    sys.exit(main())
