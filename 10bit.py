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

    python 10bit.py
    python 10bit.py --source "E:\\torrents\\final_organized" --dry-run
    python 10bit.py --self-test
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
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Sequence

# =============================================================================
# CONFIGURATION  (CLI flags override these)
# =============================================================================

SOURCE_DIR = r"E:\torrents\final_organized"
OUTPUT_DIR = r"E:\torrents\10bit"
LOG_FILE = r"E:\torrents\10bit\10bit.log"
REPORT_FILE = r"E:\torrents\10bit\10bit_report.txt"

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

VERSION = "2.2.0"
# This inspector is read-only: it writes one append-only log plus one report and never changes media.
CREATE_NO_WINDOW = 0x08000000
LOCK_NAME = ".jellyfin_10bit_inspector.lock"

# PQ (SMPTE ST 2084) and HLG are HDR transfer signals. SMPTE ST 428-1 is
# digital-cinema gamma, not an HDR signal for this Jellyfin movie workflow.
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67", "hlg"})
# Primaries/matrix alone are wide color, not HDR.
WCG_PRIMARIES = frozenset({"bt2020", "bt2020nc", "bt2020-10", "bt2020-12"})

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
    codec: str = ""
    pix_fmt: str = ""
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    duration_sec: float | None = None
    error: str = ""


CFG = Config()
PRINT_LOCK = Lock()
_ACTIVE_LOG_FILE: Path | None = None


def log(msg: str, level: str = "INFO", log_file: Path | None = None) -> None:
    """Print a timestamped event and append the identical event to this script's log."""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
    target = log_file if log_file is not None else _ACTIVE_LOG_FILE
    with PRINT_LOCK:
        print(line, flush=True)
        if target is None:
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8", errors="replace") as fh:
                fh.write(line + "\n")
        except OSError:
            # Logging must never make this read-only inspector alter or abandon media work.
            pass


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


def _atomic_write_text(path: Path, text: str) -> None:
    """Write complete text through a unique sibling then atomically replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(str(temp), str(path))
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _path_is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


class LockUnavailable(RuntimeError):
    """Raised when another inspector instance owns the run lock."""


class ExclusiveRunLock:
    """A fail-closed advisory lock compatible with Windows and POSIX hosts."""

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.handle: Any | None = None

    def _try_lock(self) -> bool:
        assert self.handle is not None
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                if self.handle.tell() == 0:
                    self.handle.write("0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __enter__(self) -> "ExclusiveRunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if self._try_lock():
                self.handle.seek(0)
                self.handle.truncate()
                self.handle.write(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}\n")
                self.handle.flush()
                return self
            if time.monotonic() >= deadline:
                self.handle.close()
                self.handle = None
                raise LockUnavailable(f"another inspector run holds {self.path}")
            time.sleep(0.2)

    def __exit__(self, exc_type, exc, traceback_obj) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.handle.close()
            self.handle = None


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


def _iter_side_data(stream: dict[str, Any]) -> list[dict[str, Any]]:
    raw = stream.get("side_data_list") or []
    return [sd for sd in raw if isinstance(sd, dict)]


def _tag_blob(stream: dict[str, Any], fmt: dict[str, Any] | None) -> str:
    parts: list[str] = []
    for src in (stream.get("tags"), (fmt or {}).get("tags")):
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
    if "hdr10+" in tags or "hdr10plus" in tags or "smpte2094" in tags.replace(" ", ""):
        if "HDR10+" not in flavors:
            flavors.append("HDR10+")
        evidence.append("stream/container tag: HDR10+ signature")
    hdr_fmt = ""
    stream_tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    fmt_tags = (fmt or {}).get("tags") if isinstance((fmt or {}).get("tags"), dict) else {}
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
    real = [
        s for s in streams
        if str(s.get("codec_type") or "video") == "video"
        and (s.get("disposition") or {}).get("attached_pic") != 1
    ]
    if not real:
        return None
    real.sort(
        key=lambda s: (
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
    is_hdr, flavors, hdr_evidence = classify_hdr(stream, fmt)
    status = categorize(bit_depth, is_hdr)

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
    hdr_label = "/".join(flavors) if flavors else ("HDR" if is_hdr else "SDR")
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
    if re.search(r"(?i)(?:^|[._\-\s])(sample|trailer|teaser)(?:[._\-\s]|$)", Path(name).stem):
        return True
    return False


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


def inspect_movie(file_path: Path, cfg: Config) -> ProbeResult:
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return ProbeResult(
            path=str(file_path),
            status=STATUS_ERROR,
            category=CATEGORY_LABELS[STATUS_ERROR],
            info=str(exc),
            error=str(exc),
        )
    try:
        payload = run_ffprobe(cfg.ffprobe, file_path, cfg)
        return result_from_probe(str(file_path), payload, size_bytes=size)
    except Exception as exc:
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
    if n <= 0:
        return "—"
    for unit, div in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def fmt_dur(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "—"
    s = int(round(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def build_report(results: Sequence[ProbeResult], cfg: Config, elapsed: float) -> str:
    groups = {
        STATUS_QUEUE: [],
        STATUS_SKIP_SDR: [],
        STATUS_SKIP_HDR: [],
        STATUS_REVIEW_8BIT_HDR: [],
        STATUS_REVIEW_UNKNOWN_DEPTH: [],
        STATUS_ERROR: [],
    }
    for item in results:
        groups.setdefault(item.status, []).append(item)
    for bucket in groups.values():
        bucket.sort(key=lambda r: r.path.casefold())

    q = groups[STATUS_QUEUE]
    sdr = groups[STATUS_SKIP_SDR]
    hdr = groups[STATUS_SKIP_HDR]
    rev_hdr = groups[STATUS_REVIEW_8BIT_HDR]
    rev_unknown = groups[STATUS_REVIEW_UNKNOWN_DEPTH]
    review = rev_hdr + rev_unknown
    err = groups[STATUS_ERROR]

    lines: list[str] = []
    a = lines.append
    a("=" * 79)
    a("HANDBRAKE WORKFLOW ACTION REPORT")
    a(f"Generated                 : {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    a(f"Target directory          : {cfg.source_dir}")
    a(f"Movies inspected          : {len(results)}")
    a(f"Elapsed                   : {elapsed:.1f}s")
    a(f"8-bit SDR (QUEUE)         : {len(q)}   ← HandBrake these")
    a(f"10/12/16-bit SDR (SKIP)   : {len(sdr)}")
    a(f"Native HDR (KEEP)         : {len(hdr)}")
    a(f"8-bit tagged HDR (REVIEW) : {len(rev_hdr)}")
    a(f"Unknown bit depth (REVIEW): {len(rev_unknown)}")
    a(f"Total review required     : {len(review)}")
    a(f"Errors                    : {len(err)}")
    a("=" * 79)
    a("")
    a("Rules:")
    a("  • QUEUE   = 8-bit SDR. Re-encode H.265 10-bit (or AV1 10-bit) in HandBrake.")
    a("  • SKIP    = already 10-bit+ SDR. Re-encoding only loses quality.")
    a("  • KEEP    = HDR10 / HDR10+ / Dolby Vision / HLG. HandBrake will tone-map")
    a("              or strip dynamic metadata unless you know exactly what you are doing.")
    a("  • REVIEW  = HDR-tagged 8-bit or metadata-uncertain. Never queue automatically.")
    a("  • BT.2020 primaries without PQ/HLG is wide-gamut SDR, not HDR.")
    a("")

    def dump(title: str, action: str, items: list[ProbeResult]) -> None:
        a("=" * 79)
        a(f"{title}  ({len(items)})")
        a(f"Action: {action}")
        a("=" * 79)
        if not items:
            a("None found.")
            a("")
            return
        for idx, item in enumerate(items, 1):
            a(f"{idx:4d}. {Path(item.path).name}")
            a(f"      Path : {item.path}")
            a(f"      Info : {item.info}")
            extra = f"      Size : {fmt_size(item.size_bytes)}    Duration : {fmt_dur(item.duration_sec)}"
            a(extra)
            if item.hdr_flavors:
                a(f"      HDR  : {', '.join(item.hdr_flavors)}")
            if item.hdr_evidence:
                a(f"      HDR evidence   : {'; '.join(item.hdr_evidence)}")
            if item.bit_depth_evidence:
                a(f"      Depth evidence : {item.bit_depth_evidence}")
            a("")

    dump(
        CATEGORY_LABELS[STATUS_QUEUE],
        "Re-encode in HandBrake → H.265 (x265 / NVEnc / QSV) 10-bit, or AV1 10-bit.",
        q,
    )
    dump(
        CATEGORY_LABELS[STATUS_SKIP_SDR],
        "Do nothing. Already high bit-depth SDR.",
        sdr,
    )
    dump(
        CATEGORY_LABELS[STATUS_SKIP_HDR],
        "Keep the original. Do not run a default HandBrake preset.",
        hdr,
    )
    dump(
        CATEGORY_LABELS[STATUS_REVIEW_8BIT_HDR],
        "Inspect manually. Do not dump these into the 8-bit SDR queue.",
        rev_hdr,
    )
    dump(
        CATEGORY_LABELS[STATUS_REVIEW_UNKNOWN_DEPTH],
        "Inspect metadata manually. Bit depth was not established with confidence.",
        rev_unknown,
    )
    dump(
        CATEGORY_LABELS[STATUS_ERROR],
        "ffprobe could not read these. Corrupt, incomplete, or unsupported.",
        err,
    )
    return "\n".join(lines) + "\n"


def write_report(results: Sequence[ProbeResult], cfg: Config, elapsed: float) -> bool:
    """Publish the sole inspector artifact as a complete atomic text report."""
    try:
        _atomic_write_text(cfg.report_file, build_report(results, cfg, elapsed))
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

    if _path_is_within(cfg.report_file, cfg.source_dir):
        errors.append(f"Report path must be outside --source: {cfg.report_file}")
    if _path_is_within(cfg.log_file, cfg.source_dir):
        errors.append(f"Log path must be outside --source: {cfg.log_file}")
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

    if files:
        workers = max(1, min(cfg.workers, len(files)))
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
                futs = {pool.submit(inspect_movie, p, cfg): p for p in files}
                for fut in as_completed(futs):
                    path = futs[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:
                        res = ProbeResult(
                            path=str(path),
                            status=STATUS_ERROR,
                            category=CATEGORY_LABELS[STATUS_ERROR],
                            info=str(exc),
                            error=str(exc),
                        )
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
    )


# =============================================================================
# SELF-TEST
# =============================================================================


def _assert(cond: bool, msg: str, errors: list[str]) -> None:
    if not cond:
        errors.append(msg)


def run_self_tests() -> int:
    errors: list[str] = []

    def probe(name: str, stream: dict[str, Any], fmt: dict[str, Any] | None = None) -> ProbeResult:
        payload: dict[str, Any] = {"streams": [stream]}
        if fmt is not None:
            payload["format"] = fmt
        return result_from_probe(name, payload, size_bytes=1_000_000)

    sdr8 = probe("a.mkv", {
        "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920, "height": 1080,
        "color_transfer": "bt709", "color_primaries": "bt709",
        "disposition": {"attached_pic": 0},
    })
    _assert(sdr8.status == STATUS_QUEUE, f"8-bit SDR should queue, got {sdr8.status}", errors)
    _assert(sdr8.bit_depth == 8, f"8-bit depth, got {sdr8.bit_depth}", errors)
    _assert(not sdr8.hdr, "8-bit SDR must not be HDR", errors)

    sdr10 = probe("b.mkv", {
        "codec_name": "hevc", "profile": "Main 10", "pix_fmt": "yuv420p10le",
        "width": 1920, "height": 1080, "color_transfer": "bt709",
        "disposition": {"attached_pic": 0},
    })
    _assert(sdr10.status == STATUS_SKIP_SDR, f"10-bit SDR should skip, got {sdr10.status}", errors)
    _assert(sdr10.bit_depth == 10, f"10-bit, got {sdr10.bit_depth}", errors)

    hdr10 = probe("c.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p10le", "bits_per_raw_sample": "10",
        "width": 3840, "height": 2160,
        "color_transfer": "smpte2084", "color_primaries": "bt2020",
        "side_data_list": [
            {"side_data_type": "Mastering display metadata"},
            {"side_data_type": "Content light level metadata"},
        ],
        "disposition": {"attached_pic": 0},
    })
    _assert(hdr10.status == STATUS_SKIP_HDR, f"HDR10 should keep, got {hdr10.status}", errors)
    _assert("HDR10" in hdr10.hdr_flavors, f"flavors {hdr10.hdr_flavors}", errors)

    dv = probe("d.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p10le",
        "width": 3840, "height": 2160,
        "side_data_list": [{"side_data_type": "DOVI configuration record"}],
        "disposition": {"attached_pic": 0},
    })
    _assert(dv.status == STATUS_SKIP_HDR, f"DV should keep, got {dv.status}", errors)
    _assert("Dolby Vision" in dv.hdr_flavors, f"DV flavors {dv.hdr_flavors}", errors)

    hlg = probe("e.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p10le",
        "color_transfer": "arib-std-b67", "color_primaries": "bt2020",
        "disposition": {"attached_pic": 0},
    })
    _assert(hlg.status == STATUS_SKIP_HDR and "HLG" in hlg.hdr_flavors, f"HLG {hlg}", errors)

    plus = probe("f.mkv", {
        "codec_name": "hevc", "pix_fmt": "p010le",
        "color_transfer": "smpte2084",
        "side_data_list": [{"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}],
        "disposition": {"attached_pic": 0},
    })
    _assert("HDR10+" in plus.hdr_flavors, f"HDR10+ flavors {plus.hdr_flavors}", errors)
    _assert(plus.status == STATUS_SKIP_HDR, "HDR10+ keep", errors)

    # The original script's worst bug: BT.2020 primaries ≠ HDR
    wcg = probe("g.mkv", {
        "codec_name": "h264", "pix_fmt": "yuv420p",
        "color_transfer": "bt709", "color_primaries": "bt2020",
        "disposition": {"attached_pic": 0},
    })
    _assert(wcg.status == STATUS_QUEUE, f"WCG SDR 8-bit should QUEUE, got {wcg.status}", errors)
    _assert(not wcg.hdr, "BT.2020 + bt709 is not HDR", errors)
    _assert("WCG" in wcg.info, f"should mention WCG: {wcg.info}", errors)

    # The original's other bug: 8-bit + PQ dumped into the SDR HandBrake queue
    bad = probe("h.mkv", {
        "codec_name": "hevc", "pix_fmt": "yuv420p",
        "color_transfer": "smpte2084", "color_primaries": "bt2020",
        "disposition": {"attached_pic": 0},
    })
    _assert(bad.status == STATUS_REVIEW_8BIT_HDR, f"8-bit HDR must REVIEW, got {bad.status}", errors)

    # Cover art must not win
    mixed = result_from_probe("i.mkv", {"streams": [
        {"codec_name": "mjpeg", "pix_fmt": "yuvj420p", "width": 600, "height": 900,
         "disposition": {"attached_pic": 1}},
        {"codec_name": "hevc", "pix_fmt": "yuv420p10le", "width": 1920, "height": 800,
         "color_transfer": "bt709", "disposition": {"attached_pic": 0}},
    ]})
    _assert(mixed.bit_depth == 10 and mixed.status == STATUS_SKIP_SDR, f"cover-art mix {mixed}", errors)

    # bits_per_raw_sample wins over a misleading 8-bit-looking default
    raw = probe("j.mkv", {
        "codec_name": "ffv1", "pix_fmt": "something_custom",
        "bits_per_raw_sample": "12",
        "disposition": {"attached_pic": 0},
    })
    _assert(raw.bit_depth == 12 and raw.status == STATUS_SKIP_SDR, f"12-bit raw {raw}", errors)

    unknown = probe("k.mkv", {
        "codec_name": "unknown", "pix_fmt": "custom_layout", "profile": "",
        "width": 1920, "height": 1080, "disposition": {"attached_pic": 0},
    })
    _assert(unknown.status == STATUS_REVIEW_UNKNOWN_DEPTH, f"unknown depth must review, got {unknown.status}", errors)
    _assert(unknown.bit_depth is None, f"unknown depth should remain None, got {unknown.bit_depth}", errors)

    multi_feature = result_from_probe("l.mkv", {"streams": [
        {"index": 0, "codec_type": "video", "pix_fmt": "yuv420p10le", "width": 64, "height": 64, "disposition": {"attached_pic": 0}},
        {"index": 1, "codec_type": "video", "pix_fmt": "yuv420p", "width": 1920, "height": 1080, "disposition": {"attached_pic": 0}},
        {"index": 2, "codec_type": "video", "pix_fmt": "yuv420p", "width": 4000, "height": 4000, "disposition": {"attached_pic": 1}},
    ]})
    _assert(multi_feature.status == STATUS_QUEUE and multi_feature.width == 1920, f"main feature selection {multi_feature}", errors)

    _assert(bit_depth_from_pix_fmt("yuv420p") == 8, "yuv420p", errors)
    _assert(bit_depth_from_pix_fmt("yuv420p10le") == 10, "p10le", errors)
    _assert(bit_depth_from_pix_fmt("p010le") == 10, "p010", errors)
    _assert(bit_depth_from_pix_fmt("yuv420p12le") == 12, "p12", errors)

    # Discovery skips extras / samples
    tmp = Path(tempfile.mkdtemp(prefix="hb_ins_"))
    try:
        movie = tmp / "Movie (1999)"
        extra = movie / "Featurettes"
        extra.mkdir(parents=True)
        (movie / "Movie (1999).mkv").write_bytes(b"x" * (120 * 1024 * 1024))
        (extra / "Making-Of.mkv").write_bytes(b"y" * (120 * 1024 * 1024))
        (movie / "Movie (1999)-sample.mkv").write_bytes(b"z" * (120 * 1024 * 1024))
        cfg = Config(source_dir=tmp, min_file_size_mb=100)
        found = discover_videos(tmp, cfg)
        names = {p.name for p in found}
        _assert(names == {"Movie (1999).mkv"}, f"canonical discovery {names}", errors)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Report includes errors (original dropped them)
    fake_cfg = Config(source_dir=Path("X:\\lib"))
    text = build_report([
        ProbeResult("a.mkv", STATUS_QUEUE, CATEGORY_LABELS[STATUS_QUEUE], "info", size_bytes=1),
        ProbeResult("e.mkv", STATUS_ERROR, CATEGORY_LABELS[STATUS_ERROR], "boom", error="boom"),
    ], fake_cfg, 1.0)
    _assert("UNREADABLE" in text and "boom" in text, "report must include errors", errors)
    _assert("QUEUE FOR HANDBRAKE" in text, "report queue heading", errors)

    report_dir = Path(tempfile.mkdtemp(prefix="hb_report_"))
    try:
        report_cfg = Config(source_dir=Path("X:\\lib"), report_file=report_dir / "report.txt")
        _assert(write_report([sdr8], report_cfg, 0.1), "atomic report write", errors)
        _assert(report_cfg.report_file.is_file(), "single report exists", errors)
        _assert(not list(report_dir.glob("*.json")), "no JSON side output", errors)
        _assert(not list(report_dir.glob("*.tmp")), "no staged report remains", errors)
    finally:
        shutil.rmtree(report_dir, ignore_errors=True)

    if errors:
        print("SELF-TEST FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print("SELF-TEST PASSED (fail-closed classification + HDR rules + discovery + single report)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_tests()
    try:
        if os.name == "nt":
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        cfg = cfg_from_args(args)
        errors = validate_config(cfg)
        if errors:
            for error in errors:
                log(f"Configuration error: {error}", level="CRITICAL", log_file=None)
            return 2
        global _ACTIVE_LOG_FILE
        _ACTIVE_LOG_FILE = cfg.log_file
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
            with ExclusiveRunLock(run_lock_path(cfg.source_dir), cfg.lock_timeout_seconds):
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


if __name__ == "__main__":
    sys.exit(main())
