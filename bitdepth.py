#!/usr/bin/env python3
"""
Jellyfin 10-Bit Movie Compliance Inspector
===========================================
Walk a movie library with ffprobe and split every real feature into
action queues that match how HandBrake should actually be used:

  1. 8-bit SDR        → QUEUE   (re-encode H.265/AV1 10-bit)
  2. 10/12/16-bit SDR → SKIP    (already high bit-depth SDR)
  3. HDR (10-bit+)    → KEEP    (HDR10 / HDR10+ / Dolby Vision / HLG)
  4. 8-bit "HDR"      → REVIEW  (mis-tagged or broken; do NOT treat as SDR)
  5. Unknown bit depth → REVIEW (never assume this is 8-bit SDR)
  6. Errors            → listed, never silently dropped

BT.2020 primaries alone are *not* HDR (wide-gamut SDR exists).
8-bit + PQ/HLG is *not* an SDR HandBrake candidate.

Requires ``ffprobe`` on PATH (or next to this script / common install dirs).
Zero Python third-party dependencies.

    python bitdepth.py
    python bitdepth.py --source "E:\\torrents\\final_organized" --dry-run
    python bitdepth.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Shared implementation: everything imported here is defined exactly once,
# in organizekit/core/. See tests/test_shared_core.py for the rule that
# keeps it that way.
from organizekit.core import (
    KIND_BITDEPTH,
    ExclusiveRunLock,
    LockUnavailable,
    MediaProbeCache,
    Report,
    RunLog,
    atomic_write_text,
    default_tool_dir,
    enable_utf8_stdio,
    iter_completed,
    open_state,
    path_is_within,
    resolve_library,
    resolve_workers,
    run_field_smoke_test,
)


def format_bytes(size: int | float | None) -> str:
    """Human file size with the unit spacing the reports use."""
    if size is None or size <= 0:
        return "0 B"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"  # pragma: no cover - unreachable

def format_duration(seconds: float | None) -> str:
    """``H:MM:SS`` (or ``M:SS`` under an hour); an em-dash-free ``-`` when unknown."""
    if not seconds or seconds <= 0:
        return "-"
    total = int(round(seconds))
    hours, total = divmod(total, 3600)
    minutes, secs = divmod(total, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# =============================================================================
# CONFIGURATION  (CLI flags override these)
# =============================================================================

SOURCE_DIR = str(resolve_library())
# Logs, reports, and the probe cache live under tools\ReportsAndLogs so the
# root of E:\torrents stays media-only.
OUTPUT_DIR = str(default_tool_dir("10bit"))
LOG_FILE = str(default_tool_dir("10bit") / "10bit.log")
REPORT_FILE = str(default_tool_dir("10bit") / "10bit_report.txt")
# Reused ffprobe output for files whose size and mtime have not changed, so a
# re-scan of an unchanged library does not respawn ffprobe per movie.
CACHE_FILE = str(default_tool_dir("10bit") / "10bit_probe_cache.json")

# movie_standardizer.py emits canonical MKV feature files only.
VIDEO_EXTENSIONS = {".mkv"}
# Do not silently omit a valid movie because it is unusually short or small.
MIN_FILE_SIZE_MB = 0
MAX_CPU_WORKERS = 8
PROBE_TIMEOUT_SEC = 45
PROBE_SIZE = "8M"
ANALYZE_DURATION = "10M"

# Skip Plex/Jellyfin extra folders and disc-structure internals.
SKIP_DIR_NAMES = frozenset({
    "featurettes", "extras", "specials", "shorts", "bonus",
    "behind the scenes", "deleted scenes", "interviews", "scenes",
    "trailers", "other", "samples", "sample", "clips",
    "bdmv", "certificate", "video_ts", "audio_ts",
    "subs", "subtitles", "proof", "screens", "screenshots",
})

# =============================================================================
# CONSTANTS
# =============================================================================

VERSION = "2.4.0"
# This inspector never changes media. It writes one append-only log, one
# replaceable report, and (unless --no-cache) one reusable probe cache; all
# three live outside the media library.
CREATE_NO_WINDOW = 0x08000000
LOCK_NAME = ".jellyfin_10bit_inspector.lock"

# PQ (SMPTE ST 2084) and HLG are HDR transfer signals. SMPTE ST 428-1 is
# digital-cinema gamma, not an HDR signal for this Jellyfin movie workflow.
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67", "hlg"})
# Primaries/matrix alone are wide color, not HDR.
WCG_PRIMARIES = frozenset({"bt2020", "bt2020nc", "bt2020-10", "bt2020-12"})

# Dolby Vision base-layer compatibility, from dv_bl_signal_compatibility_id.
# "Dolby Vision" on its own does not answer the practical question - whether a
# client that cannot decode Dolby Vision can still play the file.
DOVI_BASE_LAYER = {
    0: "no SDR/HDR10 fallback",
    1: "HDR10 base",
    2: "SDR base",
    3: "base layer not specified",
    4: "HLG base",
}
# Sample-entry names that mean Dolby Vision by definition, used when a file
# carries no DV configuration record to read.
DV_CODEC_NAMES = frozenset({"dvh1", "dvhe", "dvav", "dva1", "dv1e", "dvh1e", "dva1e"})

# Explicit 8-bit names (no trailing bit count in the token).
PIX_FMT_BITS: dict[str, int] = {
    "yuv420p": 8, "yuv422p": 8, "yuv444p": 8, "yuv410p": 8, "yuv411p": 8,
    "yuvj420p": 8, "yuvj422p": 8, "yuvj444p": 8,
    "nv12": 8, "nv21": 8, "nv16": 8, "nv24": 8,
    "rgb24": 8, "bgr24": 8, "rgba": 8, "bgra": 8, "argb": 8, "abgr": 8,
    "gbrp": 8, "gray": 8, "ya8": 8, "pal8": 8, "uyvy422": 8, "yuyv422": 8,
    "p010le": 10, "p010be": 10, "p210le": 10, "p210be": 10,
    "p410le": 10, "p410be": 10, "v210": 10, "v410": 10,
    "p012le": 12, "p012be": 12,
    "p016le": 16, "p016be": 16, "p216le": 16, "p216be": 16,
    "v216": 16, "rgb48le": 16, "rgb48be": 16, "rgba64le": 16, "rgba64be": 16,
    "gray16le": 16, "gray16be": 16,
}

STATUS_QUEUE = "QUEUE_FOR_HANDBRAKE"
STATUS_SKIP_SDR = "SKIP_SDR_HIBIT"
STATUS_SKIP_HDR = "SKIP_HDR"
STATUS_REVIEW_8BIT_HDR = "REVIEW_8BIT_HDR"
STATUS_REVIEW_UNKNOWN_DEPTH = "REVIEW_UNKNOWN_BIT_DEPTH"
STATUS_ERROR = "ERROR"
STATUS_SKIPPED = "SKIPPED"

CATEGORY_LABELS = {
    STATUS_QUEUE: "1. 8-BIT SDR  —  QUEUE FOR HANDBRAKE",
    STATUS_SKIP_SDR: "2. HIGH BIT-DEPTH SDR  —  SKIP (already 10/12/16-bit)",
    STATUS_SKIP_HDR: "3. NATIVE HDR  —  KEEP (do not send through HandBrake)",
    STATUS_REVIEW_8BIT_HDR: "4. 8-BIT TAGGED HDR  —  REVIEW (do not treat as SDR)",
    STATUS_REVIEW_UNKNOWN_DEPTH: "5. UNKNOWN BIT DEPTH  —  REVIEW (do not queue)",
    STATUS_ERROR: "6. UNREADABLE / ERRORS",
}

# =============================================================================
# DATA
# =============================================================================

@dataclass
class Config:
    source_dir: Path = field(default_factory=lambda: Path(SOURCE_DIR))
    log_file: Path = field(default_factory=lambda: Path(LOG_FILE))
    report_file: Path = field(default_factory=lambda: Path(REPORT_FILE))
    cache_file: Path = field(default_factory=lambda: Path(CACHE_FILE))
    use_cache: bool = True
    min_file_size_mb: float = MIN_FILE_SIZE_MB
    workers: int = MAX_CPU_WORKERS
    timeout: float = PROBE_TIMEOUT_SEC
    dry_run: bool = False
    verbose: bool = False
    ffprobe: str = "ffprobe"
    lock_timeout_seconds: float = 60.0
    fail_if_queue: bool = False
    fail_if_review: bool = False
    fail_if_error: bool = False
    use_state: bool = True        # publish verdicts to the shared state cache
    state_db: Path | None = None  # None = the documented default location

    @property
    def min_bytes(self) -> int:
        return int(self.min_file_size_mb * 1024 * 1024)

@dataclass
class ProbeResult:
    path: str
    status: str
    category: str
    info: str
    bit_depth: int | None = None
    bit_depth_evidence: str = ""
    hdr: bool = False
    hdr_flavors: list[str] = field(default_factory=list)
    hdr_evidence: list[str] = field(default_factory=list)
    dv_profile: str = ""
    codec: str = ""
    pix_fmt: str = ""
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    duration_sec: float | None = None
    error: str = ""

CFG = Config()
# The console/file logger every tool shares: see organizekit/core/runlog.py
# for why a logging failure is never allowed to end a run.
log = RunLog()

# =============================================================================
# FFPROBE LOCATION
# =============================================================================

def find_ffprobe(explicit: str | None = None) -> str | None:
    candidates: list[str] = []
    if explicit and explicit != "ffprobe":
        candidates.append(explicit)
    which = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if which:
        candidates.append(which)
    here = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
    candidates.extend([
        str(here / "ffprobe.exe"),
        str(here / "ffprobe"),
        str(here / "ffmpeg" / "ffprobe.exe"),
        r"C:\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files\FFmpeg\bin\ffprobe.exe",
    ])
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        path = Path(cand)
        if path.is_file():
            return str(path)
    return which

def ffprobe_works(binary: str) -> bool:
    try:
        r = subprocess.run(
            [binary, "-version"],
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# =============================================================================
# BIT DEPTH / HDR CLASSIFICATION  (pure — unit-tested)
# =============================================================================

def _as_int(value: Any) -> int | None:
    if value is None or value == "" or value == "N/A":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None

def _as_float(value: Any) -> float | None:
    if value is None or value == "" or value == "N/A":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None

def bit_depth_from_pix_fmt(pix_fmt: str) -> int | None:
    fmt = (pix_fmt or "").lower().strip()
    if not fmt:
        return None
    if fmt in PIX_FMT_BITS:
        return PIX_FMT_BITS[fmt]
    if "p010" in fmt:
        return 10
    if "p012" in fmt:
        return 12
    if "p016" in fmt:
        return 16
    match = re.search(r"(\d{1,2})(?:le|be)?$", fmt)
    if match:
        n = int(match.group(1))
        if n in {8, 9, 10, 12, 14, 16}:
            return n
    return None

def bit_depth_from_profile(profile: str) -> int | None:
    p = (profile or "").lower()
    if not p:
        return None
    if re.search(r"\b(?:main|high|professional)\s*12\b", p) or "main 12" in p:
        return 12
    if re.search(r"\b(?:main|high)\s*10\b", p) or "main10" in p or "main 10" in p:
        return 10
    if "main 8" in p or p in {"main", "high", "baseline", "constrained baseline"}:
        return None  # not decisive
    return None

def resolve_bit_depth(stream: dict[str, Any]) -> tuple[int | None, str]:
    """Return confirmed bit depth and evidence; never assume unknown is 8-bit."""
    raw = _as_int(stream.get("bits_per_raw_sample")) or _as_int(stream.get("bits_per_component"))
    if raw and 8 <= raw <= 16:
        return raw, "raw-sample metadata"
    pix_fmt = str(stream.get("pix_fmt") or "")
    from_fmt = bit_depth_from_pix_fmt(pix_fmt)
    if from_fmt:
        return from_fmt, f"pixel format {pix_fmt}"
    profile = str(stream.get("profile") or "")
    from_prof = bit_depth_from_profile(profile)
    if from_prof:
        return from_prof, f"codec profile {profile}"
    return None, "no reliable raw-sample, pixel-format, or profile bit-depth metadata"

def dolby_vision_detail(stream: dict[str, Any]) -> str:
    """Describe a Dolby Vision stream's profile, or ``""`` if it is not reported.

    ffprobe surfaces the configuration record carried in the bitstream or the
    container, and ``dv_profile`` plus ``dv_bl_signal_compatibility_id``
    together say what the plain label cannot: which profile it is, and whether
    there is a base layer for a client that has no Dolby Vision support.
    Older ffprobe builds report Dolby Vision without the profile; that is
    simply left undescribed rather than guessed at.
    """
    for sd in _iter_side_data(stream):
        kind = str(sd.get("side_data_type") or "").lower()
        if not ("dovi" in kind or "dolby vision" in kind or "dvcc" in kind or "dvvc" in kind):
            continue
        prof = _as_int(sd.get("dv_profile"))
        compat = _as_int(sd.get("dv_bl_signal_compatibility_id"))
        if prof is None:
            return ""
        # Profile 8 with a base-layer id is what the industry calls 8.1 / 8.2 /
        # 8.4 - spell it the way anyone looking it up would search for it.
        if prof == 8 and compat in (1, 2, 4):
            parts = [f"profile 8.{compat}"]
        else:
            parts = [f"profile {prof}"]
        if compat is not None:
            parts.append(DOVI_BASE_LAYER.get(compat, f"base-layer id {compat}"))
        if _as_int(sd.get("el_present_flag")) == 1:
            parts.append("dual layer (enhancement layer present)")
        return " \u00b7 ".join(parts)
    return ""


def bit_depth_conflict(stream: dict[str, Any]) -> tuple[int, int] | None:
    """Return ``(raw-sample bits, pixel-format bits)`` when the two disagree.

    Two independent fields claim a bit depth and they can contradict each
    other - a 10-bit pixel format behind an 8-bit raw-sample count, most
    often. Neither wins here: the caller turns a disagreement into a review,
    because acting on half a label means deciding whether to re-encode.
    """
    raw = _as_int(stream.get("bits_per_raw_sample")) or _as_int(stream.get("bits_per_component"))
    if not raw or not (8 <= raw <= 16):
        return None
    from_fmt = bit_depth_from_pix_fmt(str(stream.get("pix_fmt") or ""))
    if from_fmt is None or from_fmt == raw:
        return None
    return raw, from_fmt


def _iter_side_data(stream: dict[str, Any]) -> list[dict[str, Any]]:
    raw = stream.get("side_data_list") or []
    return [sd for sd in raw if isinstance(sd, dict)]

def _tag_blob(stream: dict[str, Any], fmt: dict[str, Any] | None) -> str:
    parts: list[str] = []
    fmt_tags = fmt.get("tags") if fmt is not None else None
    for src in (stream.get("tags"), fmt_tags):
        if not isinstance(src, dict):
            continue
        for key, val in src.items():
            parts.append(f"{key}={val}")
    return " ".join(parts).lower()

def classify_hdr(stream: dict[str, Any], fmt: dict[str, Any] | None = None) -> tuple[bool, list[str], list[str]]:
    """Return HDR status, labels, and evidence. BT.2020 primaries alone are not HDR."""
    flavors: list[str] = []
    evidence: list[str] = []
    transfer = str(stream.get("color_transfer") or "").lower()
    tags = _tag_blob(stream, fmt)

    for sd in _iter_side_data(stream):
        kind = str(sd.get("side_data_type") or "").lower()
        if "dovi" in kind or "dolby vision" in kind or "dvcc" in kind or "dvvc" in kind:
            flavors.append("Dolby Vision")
            evidence.append(f"side data: {kind}")
        elif "2094" in kind or "hdr10+" in kind or "dynamic hdr" in kind or "hdr dynamic" in kind:
            flavors.append("HDR10+")
            evidence.append(f"side data: {kind}")
        elif "mastering display" in kind or "content light" in kind:
            flavors.append("HDR10")
            evidence.append(f"side data: {kind}")

    if "dolby vision" in tags or "dvhe" in tags or "dvav" in tags or "dvh1" in tags:
        if "Dolby Vision" not in flavors:
            flavors.append("Dolby Vision")
        evidence.append("stream/container tag: Dolby Vision signature")
    # A stream whose codec *is* a Dolby Vision sample entry is Dolby Vision
    # even when the file carries no configuration record to read.
    dv_codec = str(stream.get("codec_name") or "").lower().strip()
    if dv_codec in DV_CODEC_NAMES:
        if "Dolby Vision" not in flavors:
            flavors.append("Dolby Vision")
        evidence.append(f"codec name: {dv_codec} (Dolby Vision)")
    if "hdr10+" in tags or "hdr10plus" in tags or "smpte2094" in tags.replace(" ", ""):
        if "HDR10+" not in flavors:
            flavors.append("HDR10+")
        evidence.append("stream/container tag: HDR10+ signature")
    hdr_fmt = ""
    st_raw = stream.get("tags")
    stream_tags = st_raw if isinstance(st_raw, dict) else {}
    ft_raw = fmt.get("tags") if fmt is not None else None
    fmt_tags = ft_raw if isinstance(ft_raw, dict) else {}
    for key in ("HDR_Format", "hdr_format", "HDR_Format_String", "HDR_Format_Compatibility"):
        hdr_fmt += " " + str(stream_tags.get(key) or fmt_tags.get(key) or "")
    hf = hdr_fmt.lower()
    if "dolby" in hf and "Dolby Vision" not in flavors:
        flavors.append("Dolby Vision")
        evidence.append("HDR format tag: Dolby Vision")
    if "hdr10+" in hf and "HDR10+" not in flavors:
        flavors.append("HDR10+")
        evidence.append("HDR format tag: HDR10+")
    if "hdr10" in hf and "HDR10+" not in hf and "HDR10" not in flavors:
        flavors.append("HDR10")
        evidence.append("HDR format tag: HDR10")
    if "hlg" in hf and "HLG" not in flavors:
        flavors.append("HLG")
        evidence.append("HDR format tag: HLG")

    if transfer == "smpte2084":
        if not any(f in flavors for f in ("HDR10", "HDR10+", "Dolby Vision")):
            flavors.append("HDR10")
        evidence.append(f"transfer: {transfer}")
    elif transfer in {"arib-std-b67", "hlg"}:
        if "HLG" not in flavors:
            flavors.append("HLG")
        evidence.append(f"transfer: {transfer}")

    # De-dupe, stable order
    order = ["Dolby Vision", "HDR10+", "HDR10", "HLG"]
    seen: set[str] = set()
    unique: list[str] = []
    for name in order + [f for f in flavors if f not in order]:
        if name in flavors and name not in seen:
            seen.add(name)
            unique.append(name)
    is_hdr = bool(unique) or transfer in HDR_TRANSFERS
    return is_hdr, unique, list(dict.fromkeys(evidence))

def pick_video_stream(payload: dict[str, Any]) -> dict[str, Any] | None:
    streams = [s for s in payload.get("streams", []) if isinstance(s, dict)]
    def is_attached_picture(stream: dict[str, Any]) -> bool:
        disposition = stream.get("disposition") or {}
        tags = stream.get("tags") or {}
        mimetype = str(tags.get("mimetype") or "").strip().casefold()
        # Matroska attachments are not consistently exposed by ffprobe with
        # disposition.attached_pic=1.  In the live-run cache, cover.jpg files
        # appeared as full-duration MJPEG video streams with no disposition,
        # causing four 10-bit features to be falsely queued as 8-bit.  The
        # attachment MIME tag is the container's unambiguous signal.
        return disposition.get("attached_pic") == 1 or mimetype.startswith("image/")

    real = [
        s for s in streams
        if str(s.get("codec_type") or "video") == "video"
        and not is_attached_picture(s)
    ]
    if not real:
        return None
    # The container's own "this is the main feature" mark outranks picture
    # size: a scope main feature (1920x816) is shorter than a full-frame bonus
    # featurette (1920x1080), and reading the featurette would report the
    # wrong movie's bit depth and HDR. Size decides only between streams the
    # file does not distinguish.
    real.sort(
        key=lambda s: (
            int((s.get("disposition") or {}).get("default") or 0),
            (_as_int(s.get("width")) or 0) * (_as_int(s.get("height")) or 0),
            _as_int(s.get("bit_rate")) or 0,
            int((s.get("disposition") or {}).get("default") or 0),
            -(_as_int(s.get("index")) or 0),
        ),
        reverse=True,
    )
    return real[0]

def categorize(bit_depth: int | None, is_hdr: bool) -> str:
    if bit_depth is None:
        return STATUS_REVIEW_UNKNOWN_DEPTH
    if bit_depth <= 8 and is_hdr:
        return STATUS_REVIEW_8BIT_HDR
    if bit_depth <= 8:
        return STATUS_QUEUE
    if is_hdr:
        return STATUS_SKIP_HDR
    return STATUS_SKIP_SDR

def result_from_probe(
    path: str,
    payload: dict[str, Any],
    *,
    size_bytes: int = 0,
) -> ProbeResult:
    stream = pick_video_stream(payload)
    if stream is None:
        return ProbeResult(
            path=path,
            status=STATUS_ERROR,
            category=CATEGORY_LABELS[STATUS_ERROR],
            info="No main video stream (cover-art-only or empty)",
            size_bytes=size_bytes,
            error="no video stream",
        )

    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    bit_depth, bit_depth_evidence = resolve_bit_depth(stream)
    conflict = bit_depth_conflict(stream)
    is_hdr, flavors, hdr_evidence = classify_hdr(stream, fmt)
    status = categorize(bit_depth, is_hdr)
    if conflict is not None:
        # Two independent fields disagree, so the bit depth is not known -
        # only claimed twice, differently. A queue is a re-encode, so the
        # file waits for a human instead of being decided on half a label.
        raw_bits, fmt_bits = conflict
        status = STATUS_REVIEW_UNKNOWN_DEPTH
        bit_depth_evidence = (
            f"conflicting metadata: raw sample says {raw_bits}-bit but pixel "
            f"format {stream.get('pix_fmt')} says {fmt_bits}-bit - not queued"
        )

    codec = str(stream.get("codec_name") or "unknown").upper()
    profile = str(stream.get("profile") or "").strip()
    pix_fmt = str(stream.get("pix_fmt") or "unknown")
    width = _as_int(stream.get("width")) or 0
    height = _as_int(stream.get("height")) or 0
    res = f"{width}x{height}" if width and height else "unknown-res"
    duration = _as_float(stream.get("duration")) or _as_float((fmt or {}).get("duration"))
    transfer = str(stream.get("color_transfer") or "")
    primaries = str(stream.get("color_primaries") or "")
    wcg = primaries.lower() in WCG_PRIMARIES and not is_hdr

    bits_label = f"{bit_depth}-bit" if bit_depth is not None else "unknown-bit"
    if conflict is not None:
        bits_label += "?"
    # "Dolby Vision" alone does not say whether anything else can play it.
    dv_detail = dolby_vision_detail(stream)
    shown = [f"{name} ({dv_detail})" if name == "Dolby Vision" and dv_detail else name
             for name in flavors]
    hdr_label = "/".join(shown) if shown else ("HDR" if is_hdr else "SDR")
    if wcg:
        hdr_label = "SDR WCG (BT.2020 primaries, not HDR)"
    prof = f" {profile}" if profile else ""
    info = f"{res} | {codec}{prof} | {bits_label} {hdr_label} | {pix_fmt}"
    if transfer:
        info += f" | trc={transfer}"
    info += f" | depth={bit_depth_evidence}"

    return ProbeResult(
        path=path,
        status=status,
        category=CATEGORY_LABELS[status],
        info=info,
        bit_depth=bit_depth,
        bit_depth_evidence=bit_depth_evidence,
        hdr=is_hdr,
        hdr_flavors=flavors,
        hdr_evidence=hdr_evidence,
        dv_profile=dv_detail,
        codec=codec,
        pix_fmt=pix_fmt,
        width=width,
        height=height,
        size_bytes=size_bytes,
        duration_sec=duration,
    )

# =============================================================================
# FILE DISCOVERY
# =============================================================================

def is_skipped_dir(name: str) -> bool:
    return name.strip().lower() in SKIP_DIR_NAMES

def is_junk_name(name: str) -> bool:
    lower = name.lower()
    if lower.startswith("."):
        return True
    if any(lower.endswith(s) for s in (".!qb", ".parts", ".part", ".crdownload", ".tmp")):
        return True
    return bool(re.search(r"(?i)(?:^|[._\-\s])(sample|trailer|teaser)(?:[._\-\s]|$)", Path(name).stem))

def discover_videos(root: Path, cfg: Config) -> list[Path]:
    found: list[Path] = []
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_skipped_dir(d) and not d.startswith(".")]
        for name in filenames:
            if is_junk_name(name):
                continue
            ext = Path(name).suffix.lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size < cfg.min_bytes:
                    continue
            except OSError:
                continue
            found.append(path)
    found.sort(key=lambda p: str(p).casefold())
    return found

# =============================================================================
# PROBE
# =============================================================================

def run_ffprobe(binary: str, file_path: Path, cfg: Config) -> dict[str, Any]:
    cmd = [
        binary,
        "-v", "error",
        "-hide_banner",
        "-probesize", PROBE_SIZE,
        "-analyzeduration", ANALYZE_DURATION,
        "-show_entries",
        "stream=index,codec_name,codec_type,profile,pix_fmt,bit_rate,"
        "bits_per_raw_sample,bits_per_component,width,height,"
        "color_space,color_transfer,color_primaries,color_range,"
        "duration,disposition,side_data_list:"
        "stream_tags:"
        "format=duration,size,bit_rate:"
        "format_tags",
        "-of", "json",
        str(file_path),
    ]
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=cfg.timeout,
            creationflags=flags,
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

def inspect_movie(
    file_path: Path,
    cfg: Config,
    cache: MediaProbeCache | None = None,
) -> ProbeResult:
    try:
        file_stat = file_path.stat()
        size = file_stat.st_size
    except OSError as exc:
        return ProbeResult(
            path=str(file_path),
            status=STATUS_ERROR,
            category=CATEGORY_LABELS[STATUS_ERROR],
            info=str(exc),
            error=str(exc),
        )
    try:
        # Only the ffprobe output is cached, never the classification below:
        # result_from_probe runs on every call so a change in the rules or in
        # the file is never masked by a stale verdict.
        payload = cache.get(file_path, size, file_stat.st_mtime_ns) if cache is not None else None
        if payload is None:
            payload = run_ffprobe(cfg.ffprobe, file_path, cfg)
            if cache is not None:
                cache.put(file_path, size, file_stat.st_mtime_ns, payload)
        return result_from_probe(str(file_path), payload, size_bytes=size)
    except Exception as exc:
        # Failures are deliberately not cached: a transient ffprobe problem
        # must not become a sticky verdict for that movie.
        return ProbeResult(
            path=str(file_path),
            status=STATUS_ERROR,
            category=CATEGORY_LABELS[STATUS_ERROR],
            info=str(exc),
            size_bytes=size,
            error=str(exc),
        )

# =============================================================================
# REPORT
# =============================================================================

def fmt_size(n: int) -> str:
    """Human file size, formatted the same way in every tool's report."""
    return format_bytes(n)

def fmt_dur(seconds: float | None) -> str:
    """Human duration, formatted the same way in every tool's report."""
    return format_duration(seconds)

@dataclass(frozen=True)
class ActionGroup:
    """One classification bucket as the report presents it.

    ``order`` is the tuple order of :data:`ACTION_GROUPS`: the work to do comes
    first, then the things a human must decide, then the categories that only
    confirm nothing should be touched.
    """

    status: str
    title: str
    scorecard_label: str
    scorecard_hint: str
    action: str

ACTION_GROUPS: tuple[ActionGroup, ...] = (
    ActionGroup(
        STATUS_QUEUE,
        "QUEUE FOR HANDBRAKE (8-BIT SDR)",
        "8-bit SDR (QUEUE)",
        "re-encode these to 10-bit",
        "Re-encode in HandBrake with H.265 (x265 / NVENC / QSV) 10-bit, or AV1 10-bit.",
    ),
    ActionGroup(
        STATUS_REVIEW_8BIT_HDR,
        "8-BIT TAGGED HDR (REVIEW)",
        "8-bit tagged HDR (REVIEW)",
        "never queue automatically",
        "Inspect manually. These carry HDR metadata on 8-bit video, so dumping them "
        "into the SDR queue would tone-map a film that was never mastered that way.",
    ),
    ActionGroup(
        STATUS_REVIEW_UNKNOWN_DEPTH,
        "UNKNOWN BIT DEPTH (REVIEW)",
        "Unknown bit depth (REVIEW)",
        "inspect the metadata",
        "Bit depth could not be established with confidence. Read the evidence line "
        "below each file before deciding anything.",
    ),
    ActionGroup(
        STATUS_ERROR,
        "UNREADABLE / ERRORS",
        "Errors",
        "ffprobe could not read them",
        "ffprobe could not read these. Corrupt, incomplete, or an unsupported container.",
    ),
    ActionGroup(
        STATUS_SKIP_HDR,
        "NATIVE HDR (KEEP - DO NOT RE-ENCODE)",
        "Native HDR (KEEP)",
        "protected from tone-mapping",
        "Keep the original. HandBrake tone-maps or strips HDR10 / HDR10+ / Dolby "
        "Vision dynamic metadata unless you know exactly what you are doing.",
    ),
    ActionGroup(
        STATUS_SKIP_SDR,
        "HIGH BIT-DEPTH SDR (SKIP - NOTHING TO DO)",
        "10/12/16-bit SDR (SKIP)",
        "already high bit depth",
        "Do nothing. Re-encoding an already high bit-depth file only loses quality.",
    ),
)

def build_report(results: Sequence[ProbeResult], cfg: Config, elapsed: float) -> str:
    """Render the inspector report: what to re-encode first, what never to touch last."""
    groups: dict[str, list[ProbeResult]] = {group.status: [] for group in ACTION_GROUPS}
    for item in results:
        groups.setdefault(item.status, []).append(item)
    for bucket in groups.values():
        bucket.sort(key=lambda r: Path(r.path).name.casefold())

    report = Report(
        "HANDBRAKE WORKFLOW ACTION REPORT",
        "Bit-depth and HDR classification for every movie \u00b7 fail-closed: anything "
        "uncertain is never queued",
    )
    report.metas([
        ("Generated", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")),
        ("Target directory", cfg.source_dir),
        ("Movies inspected", len(results)),
        ("Elapsed", f"{elapsed:.1f}s"),
        ("Report", cfg.report_file),
    ])

    rows: list[tuple[object, str, str]] = [
        (len(groups[group.status]), group.scorecard_label, group.scorecard_hint)
        for group in ACTION_GROUPS
    ]
    rows.append((len(results), "Movies inspected", "every movie-sized video found"))
    report.blank()
    report.scorecard(rows)

    queue = groups[STATUS_QUEUE]
    review = groups[STATUS_REVIEW_8BIT_HDR] + groups[STATUS_REVIEW_UNKNOWN_DEPTH]
    if queue or review:
        report.paragraph(
            f"Start here: {len(queue)} movie(s) to re-encode"
            + (f" and {len(review)} needing a human decision" if review else "")
            + " \u00b7 both groups are listed first, below."
        )
    else:
        report.paragraph(
            "Nothing to queue: no 8-bit SDR movie was found, and nothing is uncertain "
            "enough to need a decision."
        )

    for group in ACTION_GROUPS:
        items = groups.get(group.status) or []
        report.section(
            group.title,
            count=len(items),
            total=len(results),
            intro=f"Action: {group.action}",
        )
        if not items:
            report.paragraph("None found.")
            continue
        for position, item in enumerate(items, start=1):
            fields: list[tuple[str, str]] = [
                ("Path", item.path),
                ("Info", item.info),
                ("Size", f"{fmt_size(item.size_bytes)}   \u00b7   duration {fmt_dur(item.duration_sec)}"),
            ]
            if item.hdr_flavors:
                fields.append(("HDR", ", ".join(item.hdr_flavors)))
            if item.dv_profile:
                fields.append(("Dolby Vision", item.dv_profile))
            if item.hdr_evidence:
                fields.append(("HDR evidence", "; ".join(item.hdr_evidence)))
            if item.bit_depth_evidence:
                fields.append(("Depth evidence", item.bit_depth_evidence))
            if item.error:
                fields.append(("Error", item.error))
            report.entry(Path(item.path).name, ordinal=position, fields=fields)

    report.footer([
        "QUEUE = 8-bit SDR. Re-encode to H.265 10-bit (or AV1 10-bit) in HandBrake.",
        "SKIP = already 10-bit or better SDR. Re-encoding only loses quality.",
        "KEEP = HDR10 / HDR10+ / Dolby Vision / HLG. HandBrake tone-maps or strips "
        "dynamic metadata.",
        "A Dolby Vision profile ending in 'no fallback' needs a Dolby Vision "
        "client; one listing a base layer still plays as HDR10, HLG or SDR.",
        "REVIEW = HDR-tagged 8-bit, or metadata too uncertain to trust. Never queued "
        "automatically.",
        "BT.2020 primaries without PQ or HLG is wide-gamut SDR, not HDR.",
    ])
    return report.render()

def publish_state(results: list[ProbeResult], cfg: Config) -> int:
    """Record each movie's bit-depth verdict in the shared state cache.

    Separate from the probe cache next door, and for a different reader: the
    probe cache stores ffprobe's raw payload so *this* tool can skip work, while
    this stores the verdict so ``organize status`` can answer "how many movies
    are queued for HandBrake?" without probing anything. Both are keyed to the
    file's size and mtime, and neither is ever consulted as authority.
    """
    store = open_state(cfg.state_db, enabled=cfg.use_state, tool="bitdepth")
    if not store.enabled:
        return 0
    published = 0
    try:
        for result in results:
            movie = Path(result.path)
            detail = result.info or result.category
            store.record(movie, KIND_BITDEPTH, result.status, detail)
            published += 1
        store.note("bitdepth", f"{published} movie(s) inspected")
    except Exception as exc:  # noqa: BLE001 - a cache write can never fail a run
        log(f"state cache not updated: {exc}", level="WARNING")
    finally:
        store.close()
    return published


def write_report(results: Sequence[ProbeResult], cfg: Config, elapsed: float) -> bool:
    """Publish the sole inspector artifact as a complete atomic text report."""
    try:
        atomic_write_text(cfg.report_file, build_report(results, cfg, elapsed))
        return True
    except OSError as exc:
        log(f"[ERROR] Cannot write report {cfg.report_file}: {exc}")
        return False

# =============================================================================
# DRIVER
# =============================================================================

def validate_config(cfg: Config) -> list[str]:
    """Return actionable safety errors before probing or writing any outputs."""
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

    if path_is_within(cfg.report_file, cfg.source_dir):
        errors.append(f"Report path must be outside --source: {cfg.report_file}")
    if path_is_within(cfg.log_file, cfg.source_dir):
        errors.append(f"Log path must be outside --source: {cfg.log_file}")
    if cfg.use_cache and path_is_within(cfg.cache_file, cfg.source_dir):
        errors.append(f"Cache path must be outside --source: {cfg.cache_file}")
    if cfg.state_db is not None and path_is_within(cfg.state_db, cfg.source_dir):
        errors.append(f"State cache must be outside --source: {cfg.state_db}")
    if os.path.normcase(os.path.normpath(str(cfg.log_file))) == os.path.normcase(os.path.normpath(str(cfg.report_file))):
        errors.append("--log and --report must be different files")
    return errors

def run_lock_path(source_dir: Path) -> Path:
    key = hashlib.sha256(str(source_dir.resolve(strict=False)).encode("utf-8", errors="surrogatepass")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"{LOCK_NAME}.{key}"

def scan(cfg: Config) -> int:
    log("=" * 79)
    log("HANDBRAKE BIT-DEPTH INSPECTOR")
    log("=" * 79)
    log(f"Library   : {cfg.source_dir}")
    log(f"Workers   : {cfg.workers}")
    log(f"Min size  : {cfg.min_file_size_mb:g} MB")
    log(f"ffprobe   : {cfg.ffprobe}")
    log(f"Log       : {cfg.log_file}")
    log(f"Report    : {cfg.report_file}")
    log("")

    if not cfg.source_dir.exists():
        log(f"[ERROR] Directory does not exist: {cfg.source_dir}")
        return 2

    files = discover_videos(cfg.source_dir, cfg)
    log(f"Found {len(files)} canonical MKV movie file(s).")
    if cfg.dry_run:
        for p in files:
            log(f"  {p}")
        log("(dry-run — not probing)")
        return 0
    if not files:
        if not write_report([], cfg, 0.0):
            return 2
        log("Nothing to inspect.")
        return 0

    results: list[ProbeResult] = []
    started = time.perf_counter()
    done = 0
    total = len(files)

    def _store(res: ProbeResult) -> None:
        results.append(res)

    cache = MediaProbeCache(cfg.cache_file, tool="10bit", enabled=cfg.use_cache)
    if cfg.use_cache:
        log(f"Probe cache: {cfg.cache_file} ({len(cache)} entries loaded)")

    if files:
        workers = resolve_workers(cfg.workers, items=len(files), cap=MAX_CPU_WORKERS)
        try:
            for outcome in iter_completed(files, lambda p: inspect_movie(p, cfg, cache),
                                          workers=workers):
                path = outcome.item
                if outcome.error is not None:
                    res = ProbeResult(
                        path=str(path),
                        status=STATUS_ERROR,
                        category=CATEGORY_LABELS[STATUS_ERROR],
                        info=str(outcome.error),
                        error=str(outcome.error),
                    )
                else:
                    res = outcome.value
                _store(res)
                done += 1
                tag = {
                    STATUS_QUEUE: "QUEUE",
                    STATUS_SKIP_SDR: "SKIP-SDR",
                    STATUS_SKIP_HDR: "KEEP-HDR",
                    STATUS_REVIEW_8BIT_HDR: "REVIEW-HDR",
                    STATUS_REVIEW_UNKNOWN_DEPTH: "REVIEW-DEPTH",
                    STATUS_ERROR: "ERROR",
                }.get(res.status, res.status)
                log(f"[{done}/{total}] {tag:<9} {path.name}")
        except KeyboardInterrupt:
            log("\nInterrupted — writing partial results.")

    cache.save()
    if cfg.use_cache:
        log(f"Probe cache: {cache.hits} reused, {cache.misses} probed.")
    publish_state(results, cfg)

    elapsed = time.perf_counter() - started
    if not write_report(results, cfg, elapsed):
        return 2

    q = sum(1 for r in results if r.status == STATUS_QUEUE)
    sdr = sum(1 for r in results if r.status == STATUS_SKIP_SDR)
    hdr = sum(1 for r in results if r.status == STATUS_SKIP_HDR)
    rev_hdr = sum(1 for r in results if r.status == STATUS_REVIEW_8BIT_HDR)
    rev_unknown = sum(1 for r in results if r.status == STATUS_REVIEW_UNKNOWN_DEPTH)
    rev = rev_hdr + rev_unknown
    err = sum(1 for r in results if r.status == STATUS_ERROR)

    log("")
    log("=" * 79)
    log("SCAN COMPLETE")
    log("=" * 79)
    log(f"  8-bit SDR (QUEUE)         : {q}")
    log(f"  10/12/16-bit SDR (SKIP)   : {sdr}")
    log(f"  Native HDR (KEEP)         : {hdr}")
    log(f"  8-bit tagged HDR (REVIEW) : {rev_hdr}")
    log(f"  Unknown bit depth (REVIEW): {rev_unknown}")
    log(f"  Total review required     : {rev}")
    log(f"  Errors                    : {err}")
    log("=" * 79)
    log(f"Report : {cfg.report_file}")
    if cfg.fail_if_error and err:
        return 5
    if cfg.fail_if_review and rev:
        return 4
    if cfg.fail_if_queue and q:
        return 3
    return 0

# =============================================================================
# CLI
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Categorize a movie library for HandBrake 10-bit re-encodes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--source", type=Path, default=Path(SOURCE_DIR), help="Canonical Jellyfin movie-library root")
    p.add_argument("--log", type=Path, default=Path(LOG_FILE), help="Append-only execution log outside the media library")
    p.add_argument("--report", type=Path, default=Path(REPORT_FILE), help="The sole replaceable plain-text output report")
    p.add_argument("--min-size", type=float, default=MIN_FILE_SIZE_MB, metavar="MB")
    p.add_argument("--workers", type=int, default=MAX_CPU_WORKERS)
    p.add_argument("--timeout", type=float, default=PROBE_TIMEOUT_SEC, help="ffprobe timeout seconds")
    p.add_argument("--lock-timeout", type=float, default=60.0, metavar="SECONDS", help="Maximum wait for another inspector run")
    p.add_argument("--ffprobe", default="ffprobe", help="ffprobe binary")
    p.add_argument("--cache", type=Path, default=Path(CACHE_FILE), metavar="PATH",
                   help="Reusable ffprobe output for unchanged files, outside the media library")
    p.add_argument("--no-cache", dest="use_cache", action="store_false",
                   help="Probe every movie again and do not read or write the cache")
    p.add_argument("--no-state", action="store_true",
                   help="Do not record these verdicts in the shared state cache "
                        "that `organize status` reads (the probe cache is unaffected)")
    p.add_argument("--state-db", type=Path, default=None, metavar="PATH",
                   help="Where that cache lives (default: beside the logs and reports)")
    p.set_defaults(use_cache=True)
    p.add_argument("--fail-if-queue", action="store_true", help="Exit non-zero if known 8-bit SDR movies are queued")
    p.add_argument("--fail-if-review", action="store_true", help="Exit non-zero if a movie requires metadata review")
    p.add_argument("--fail-if-error", action="store_true", help="Exit non-zero if FFprobe cannot inspect a movie")
    p.add_argument("--dry-run", action="store_true", help="List files that would be probed")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--self-test", action="store_true")
    return p

def cfg_from_args(args: argparse.Namespace) -> Config:
    workers = args.workers if args.workers > 0 else (os.cpu_count() or 4)
    return Config(
        source_dir=args.source,
        log_file=args.log,
        report_file=args.report,
        min_file_size_mb=args.min_size,
        workers=workers,
        timeout=args.timeout,
        lock_timeout_seconds=args.lock_timeout,
        fail_if_queue=bool(args.fail_if_queue),
        fail_if_review=bool(args.fail_if_review),
        fail_if_error=bool(args.fail_if_error),
        dry_run=bool(args.dry_run),
        verbose=bool(args.verbose),
        ffprobe=args.ffprobe,
        cache_file=args.cache,
        use_cache=bool(args.use_cache),
        use_state=not bool(args.no_state),
        state_db=args.state_db,
    )

# =============================================================================
# SELF-TEST
# =============================================================================


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
        log(f"Starting read-only 10-bit inspection; source={cfg.source_dir}")
        binary = find_ffprobe(args.ffprobe)
        if not binary or not ffprobe_works(binary):
            log("[CRITICAL] ffprobe not found or not runnable.")
            log("Install FFmpeg and add it to PATH, or pass --ffprobe C:\\path\\to\\ffprobe.exe")
            return 2
        cfg.ffprobe = binary
        global CFG
        CFG = cfg
        try:
            with ExclusiveRunLock(run_lock_path(cfg.source_dir),
                                  cfg.lock_timeout_seconds,
                                  busy_message="another inspector run holds {path}"):
                return scan(cfg)
        except LockUnavailable as exc:
            log(f"[CRITICAL] Inspector lock unavailable: {exc}")
            return 3
    except KeyboardInterrupt:
        log("Interrupted")
        return 130
    except Exception:
        traceback.print_exc()
        return 1


def run_self_tests() -> int:
    """Field smoke test: is this copy of the inspector working on this machine?

    The exhaustive decision tables live in ``tests/selftests/`` and run with
    the rest of the offline suite. What matters here is that the fail-closed
    HDR contract holds in this interpreter, on this filesystem: 8-bit SDR is
    queued, anything that smells like HDR is protected.
    """
    def sdr_8bit_is_queued() -> bool:
        stream = {"codec_type": "video", "bits_per_raw_sample": "8", "pix_fmt": "yuv420p"}
        is_hdr, _flavors, _evidence = classify_hdr(stream)
        return not is_hdr

    def pq_transfer_is_protected() -> bool:
        stream = {"codec_type": "video", "pix_fmt": "yuv420p10le",
                  "color_transfer": "smpte2084", "color_primaries": "bt2020"}
        is_hdr, flavors, _evidence = classify_hdr(stream)
        return is_hdr and bool(flavors)

    def dolby_vision_is_protected() -> bool:
        stream = {"codec_type": "video", "pix_fmt": "yuv420p10le",
                  "side_data_list": [{"side_data_type": "DOVI configuration record"}]}
        is_hdr, flavors, _evidence = classify_hdr(stream)
        return is_hdr and any("Dolby Vision" in f for f in flavors)

    return run_field_smoke_test("bitdepth.py", [
        ("8-bit SDR is queued for HandBrake", sdr_8bit_is_queued),
        ("PQ transfer is protected as HDR", pq_transfer_is_protected),
        ("Dolby Vision is protected", dolby_vision_is_protected),
    ])

if __name__ == "__main__":
    sys.exit(main())
