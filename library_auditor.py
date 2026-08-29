#!/usr/bin/env python3
"""Read-only Jellyfin movie-folder container audit.

Inspects only direct movie-container files inside each top-level folder under
E:\\torrents\\final_organized, prints one report, and atomically saves that
same text outside the library. It never changes media, walks extras, or creates
JSON/cache files.

A folder holding a canonical MKV but no English ``.eng.srt`` is reported as
``MISSING_SIDECAR`` instead of ``CANONICAL_MKV``. That is the one finding in
this report that another tool in the pipeline can act on, so it is called out
in its own count and listed as an actionable section. A validated legacy
``.en.srt`` is renamed to ``.eng.srt`` during the audit so the library is not
stuck on the pre-cutover name.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Sequence

from common import (
    EXTERNAL_SRT_SUFFIX,
    LEGACY_EXTERNAL_SRT_SUFFIX,
    atomic_write_text,
    path_is_within,
    promote_legacy_external_english_srt,
    try_file_lock,
    validate_srt_sidecar,
)

SOURCE_DIR = r"E:\torrents\final_organized"
# Logs and reports live under tools\ReportsAndLogs so the root of E:\torrents
# stays media-only.
OUTPUT_DIR = r"E:\torrents\tools\ReportsAndLogs\library_auditor"
LOG_FILE = r"E:\torrents\tools\ReportsAndLogs\library_auditor\library_auditor.log"
REPORT_FILE = r"E:\torrents\tools\ReportsAndLogs\library_auditor\library_auditor_report.txt"
VERSION = "2.1.0"
LOCK_NAME = ".jellyfin_movie_folder_auditor.lock"

# .mkv is canonical movie_standardizer.py output. Other extensions are reported
# only as direct-folder exceptions; an extension is a container label, not a
# codec or Jellyfin direct-play guarantee.
MOVIE_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".webm",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".vob", ".flv",
    ".ogv", ".3gp", ".asf", ".rm", ".rmvb", ".m2v", ".divx",
    ".f4v", ".mxf", ".dv", ".wtv", ".dvr-ms", ".iso", ".img", ".nrg",
})
JUNK_SUFFIXES = (".!qb", ".parts", ".part", ".crdownload", ".tmp", ".temp")

# mkv_track_cleaner.py stages a remux as a full-size sibling named
# ``temp_clean_<token>__<original>.mkv`` and atomically swaps it over the
# original only after verification. An audit that happens to run mid-remux
# would otherwise count that staged copy as a second feature and report
# MULTIPLE_DIRECT_MOVIE_FILES for a perfectly healthy folder. It is a
# transaction artifact, not library content, so it is not counted.
# (The matching journal, ``.track_cleaner.<token>.json``, is already excluded
# by the leading-dot rule in is_junk_filename.) Orphaned staging files are the
# cleaner's own orphan-recovery responsibility, not a library-layout finding.
TRACK_CLEANER_TEMP_PREFIX = "temp_clean_"

# Folder states that mean the layout itself is wrong. MISSING_SIDECAR is
# deliberately absent: a freshly standardized movie has no sidecar until
# subtitle_fetcher.py runs, so counting it as a defect would make the exit-code
# gate fail on every healthy new library.
DEFECT_STATES = frozenset({
    "SINGLE_OTHER_CONTAINER",
    "MULTIPLE_DIRECT_MOVIE_FILES",
    "NO_DIRECT_MOVIE_FILE",
    "MKV_STEM_MISMATCH",
    "NONCANONICAL_SIDECAR",
    "INVALID_SIDECAR",
    "INACCESSIBLE",
})

PRINT_LOCK = Lock()


@dataclass
class Config:
    source_dir: Path = field(default_factory=lambda: Path(SOURCE_DIR))
    log_file: Path = field(default_factory=lambda: Path(LOG_FILE))
    report_file: Path = field(default_factory=lambda: Path(REPORT_FILE))
    lock_timeout_seconds: float = 60.0
    fail_on_findings: bool = False
    fail_on_defects: bool = False


@dataclass(frozen=True)
class MovieFile:
    name: str
    extension: str
    size_bytes: int


@dataclass
class FolderAudit:
    folder: Path
    state: str
    movie_files: list[MovieFile] = field(default_factory=list)
    detail: str = ""


@dataclass
class Audit:
    source_dir: Path
    folders: list[FolderAudit]
    elapsed_sec: float = 0.0


_ACTIVE_LOG_FILE: Path | None = None


def log(message: str, level: str = "INFO", log_file: Path | None = None) -> None:
    """Print a timestamped event and append the identical event to this script's log."""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}"
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
            pass


def is_junk_filename(name: str) -> bool:
    lower = name.casefold()
    return lower.startswith(".") or lower in {"thumbs.db", "desktop.ini"} or any(lower.endswith(s) for s in JUNK_SUFFIXES)


def is_in_flight_remux(name: str) -> bool:
    """True for a mkv_track_cleaner.py staging file written during a remux."""
    return name.casefold().startswith(TRACK_CLEANER_TEMP_PREFIX)


class LockUnavailable(RuntimeError):
    pass


class ExclusiveRunLock:
    """Fail-closed advisory lock compatible with Windows and POSIX."""

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path, self.timeout_seconds, self.handle = path, timeout_seconds, None

    def _try_lock(self) -> bool:
        assert self.handle is not None
        if os.name == "nt":
            # Materialize a byte before the Windows byte-range lock.
            self.handle.seek(0)
            self.handle.write("0")
            self.handle.flush()
        return try_file_lock(self.handle, strict_non_contention=False)

    def __enter__(self) -> "ExclusiveRunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while not self._try_lock():
            if time.monotonic() >= deadline:
                self.handle.close()
                self.handle = None
                raise LockUnavailable(f"another audit owns {self.path}")
            time.sleep(0.2)
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}\n")
        self.handle.flush()
        return self

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


def run_lock_path(source: Path) -> Path:
    key = hashlib.sha256(str(source.resolve(strict=False)).encode("utf-8", errors="surrogatepass")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"{LOCK_NAME}.{key}"


# =============================================================================
# DIRECT FOLDER AUDIT
# =============================================================================

def direct_movie_files(folder: Path) -> tuple[list[MovieFile], str]:
    """Inspect direct files only; nested extras, subtitles, artwork, and NFOs are ignored."""
    try:
        entries = list(folder.iterdir())
    except OSError as exc:
        return [], str(exc)
    found: list[MovieFile] = []
    for entry in entries:
        if not entry.is_file() or is_junk_filename(entry.name):
            continue
        if is_in_flight_remux(entry.name):
            continue
        extension = entry.suffix.casefold()
        if extension not in MOVIE_EXTENSIONS:
            continue
        try:
            found.append(MovieFile(entry.name, extension, entry.stat().st_size))
        except OSError as exc:
            return [], f"cannot stat {entry.name}: {exc}"
    return sorted(found, key=lambda item: item.name.casefold()), ""


def classify_folder(folder: Path) -> FolderAudit:
    files, error = direct_movie_files(folder)
    if error:
        return FolderAudit(folder, "INACCESSIBLE", detail=error)
    if not files:
        return FolderAudit(folder, "NO_DIRECT_MOVIE_FILE")
    if len(files) != 1:
        return FolderAudit(folder, "MULTIPLE_DIRECT_MOVIE_FILES", files)

    feature = files[0]
    if feature.extension != ".mkv":
        return FolderAudit(folder, "SINGLE_OTHER_CONTAINER", files)
    if feature.name != f"{folder.name}.mkv":
        return FolderAudit(folder, "MKV_STEM_MISMATCH", files, "expected exact folder-name MKV stem")

    expected_srt = f"{folder.name}{EXTERNAL_SRT_SUFFIX}"
    legacy_srt = f"{folder.name}{LEGACY_EXTERNAL_SRT_SUFFIX}"
    # Promote a validated legacy .en.srt to the canonical .eng.srt before the
    # naming check so a library cut over from the previous convention is not
    # stuck as NONCANONICAL_SIDECAR forever.
    mkv_path = folder / feature.name
    promoted, promote_reason = promote_legacy_external_english_srt(mkv_path)
    if promoted is None and promote_reason and "absent" not in promote_reason and "unusable" not in promote_reason:
        return FolderAudit(
            folder, "NONCANONICAL_SIDECAR", files,
            f"legacy {legacy_srt} could not be promoted ({promote_reason})",
        )
    try:
        srt_names = sorted(
            entry.name for entry in folder.iterdir()
            if entry.is_file() and entry.suffix.casefold() == ".srt" and not is_junk_filename(entry.name)
        )
    except OSError as exc:
        return FolderAudit(folder, "INACCESSIBLE", files, str(exc))
    unexpected_srt = [name for name in srt_names if name != expected_srt]
    if unexpected_srt:
        return FolderAudit(folder, "NONCANONICAL_SIDECAR", files, "; ".join(unexpected_srt))
    if not srt_names:
        # Structurally fine, but no external English subtitle yet. This is the
        # kind of audit finding another tool can act on, so it is reported as
        # its own state instead of being folded into CANONICAL_MKV.
        return FolderAudit(
            folder, "MISSING_SIDECAR", files,
            f"no English {EXTERNAL_SRT_SUFFIX} sidecar; subtitle_fetcher.py can still fetch one",
        )
    # The name is right, so check the contents. A sidecar that is empty, an
    # error page, or a truncated download looks perfectly healthy to a
    # filename-only audit, but it silently blocks every downstream tool: the
    # fetcher will not replace a file it thinks is already there and the
    # cleaner will not trust it. That dead end has to be visible here.
    usable, reason = validate_srt_sidecar(folder / expected_srt)
    if not usable:
        return FolderAudit(
            folder, "INVALID_SIDECAR", files,
            f"{expected_srt} is unusable ({reason}); delete it and re-run subtitle_fetcher.py",
        )
    return FolderAudit(folder, "CANONICAL_MKV", files)


def audit_library(cfg: Config) -> Audit:
    try:
        folders = sorted((p for p in cfg.source_dir.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name.casefold())
    except OSError as exc:
        log(f"Cannot enumerate library: {exc}", level="ERROR")
        folders = []
    log(f"Found {len(folders)} top-level movie folder(s).")
    audited: list[FolderAudit] = []
    for index, folder in enumerate(folders, 1):
        result = classify_folder(folder)
        audited.append(result)
        if result.state == "MISSING_SIDECAR":
            # Advisory, not a defect: the layout is correct, only the subtitle
            # is absent. Logged at INFO so a library that simply has not been
            # fetched yet does not drown the console in warnings.
            log(f"[{index}/{len(folders)}] {result.state}: {folder.name}", level="INFO")
        elif result.state != "CANONICAL_MKV":
            log(f"[{index}/{len(folders)}] {result.state}: {folder.name}", level="WARNING")
        elif index == len(folders) or index % 25 == 0:
            log(f"[{index}/{len(folders)}] audited: {folder.name}")
    return Audit(cfg.source_dir, audited)


# =============================================================================
# REPORT
# =============================================================================

def types_for(item: FolderAudit) -> str:
    return ", ".join(sorted({file.extension.upper() for file in item.movie_files})) if item.movie_files else "—"


def names_for(item: FolderAudit) -> str:
    return "; ".join(file.name for file in item.movie_files) if item.movie_files else "—"


def build_report(audit: Audit, cfg: Config) -> str:
    counts = Counter(item.state for item in audit.folders)
    type_counts = Counter(file.extension.upper() for item in audit.folders for file in item.movie_files)
    lines: list[str] = []
    add = lines.append
    add("=" * 116)
    add("JELLYFIN MOVIE FOLDER FILE-TYPE AUDIT")
    add(f"Generated       : {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    add(f"Library         : {audit.source_dir}")
    add(f"Report file     : {cfg.report_file}")
    add(f"Folders checked : {len(audit.folders)}")
    add(f"Canonical MKV   : {counts['CANONICAL_MKV']}")
    add(f"Missing Eng SRT : {counts['MISSING_SIDECAR']}")
    add(f"Invalid Eng SRT : {counts['INVALID_SIDECAR']}")
    add(f"MKV stem mismatch: {counts['MKV_STEM_MISMATCH']}")
    add(f"Noncanonical SRT: {counts['NONCANONICAL_SIDECAR']}")
    add(f"Single other    : {counts['SINGLE_OTHER_CONTAINER']}")
    add(f"Multiple files  : {counts['MULTIPLE_DIRECT_MOVIE_FILES']}")
    add(f"No movie file   : {counts['NO_DIRECT_MOVIE_FILE']}")
    add(f"Inaccessible    : {counts['INACCESSIBLE']}")
    add(f"Elapsed         : {audit.elapsed_sec:.2f}s")
    add("=" * 116)
    add("Scope: direct feature containers plus direct SRT sidecar names. Artwork, NFO files, and nested extras are ignored.")
    add("Container labels are file extensions only; they do not verify codecs or Jellyfin client direct-play support.")
    add("")
    add("SUMMARY BY DIRECT MOVIE FILE TYPE")
    add("-" * 116)
    if type_counts:
        add(f"{'Type':<14} {'Files':>8}")
        add("-" * 116)
        for extension, count in sorted(type_counts.items(), key=lambda item: (-item[1], item[0])):
            add(f"{extension:<14} {count:>8}")
    else:
        add("No direct movie-container files found.")
    add("")
    add("FOLDER-BY-FOLDER RESULTS")
    add("-" * 116)
    add(f"{'Folder':<36} {'Status':<30} {'Type(s)':<18} Movie file(s) / detail")
    add("-" * 116)
    for item in audit.folders:
        detail = item.detail if item.detail else names_for(item)
        add(f"{item.folder.name:<36.36} {item.state.replace('_', ' '):<30.30} {types_for(item):<18.18} {detail}")
    if not audit.folders:
        add("No top-level movie folders found.")

    missing = [
        (item.folder.name, item.detail)
        for item in audit.folders
        if item.state in ("MISSING_SIDECAR", "INVALID_SIDECAR")
    ]
    if missing:
        add("")
        add("MOVIES WITH NO USABLE EXTERNAL ENGLISH SRT (ACTIONABLE)")
        add("-" * 116)
        add(
            f"These folders have a canonical MKV but no working English {EXTERNAL_SRT_SUFFIX}. Run"
            " subtitle_fetcher.py before mkv_track_cleaner.py: fetching first keeps the"
            " pristine release moviehash, which is what makes an exact subtitle match possible."
            " An INVALID entry means a sidecar exists but is unusable - delete that file first,"
            " because no tool will replace a sidecar it believes is already present."
        )
        for name, detail in missing:
            add(f"  [ ] {name}  ({detail})")
    return "\n".join(lines) + "\n"


# =============================================================================
# EXECUTION + CLI
# =============================================================================

def validate_config(cfg: Config) -> list[str]:
    errors: list[str] = []
    if not cfg.source_dir.is_dir():
        errors.append(f"--source is not an accessible directory: {cfg.source_dir}")
    if cfg.lock_timeout_seconds < 0:
        errors.append("--lock-timeout must be zero or greater")
    if path_is_within(cfg.report_file, cfg.source_dir):
        errors.append(f"--report must be outside --source: {cfg.report_file}")
    if path_is_within(cfg.log_file, cfg.source_dir):
        errors.append(f"--log must be outside --source: {cfg.log_file}")
    if os.path.normcase(os.path.normpath(str(cfg.log_file))) == os.path.normcase(os.path.normpath(str(cfg.report_file))):
        errors.append("--log and --report must be different files")
    return errors


def exit_code_for(counts: Counter, cfg: Config) -> int:
    """Translate audit results into a scheduler-usable exit status.

    The auditor used to return 0 no matter what it found, so a Task Scheduler
    or cron run could never report that the library had gone unhealthy. Both
    gates are opt-in and the default stays 0, so existing automation that only
    reads the report is unaffected.
    """
    findings = sum(n for state, n in counts.items() if state != "CANONICAL_MKV")
    defects = sum(n for state, n in counts.items() if state in DEFECT_STATES)
    if cfg.fail_on_findings and findings:
        log(f"FAIL-ON-FINDINGS: {findings} folder(s) are not canonical.", level="ERROR")
        return 1
    if cfg.fail_on_defects and defects:
        log(f"FAIL-ON-DEFECTS: {defects} folder(s) have a layout defect.", level="ERROR")
        return 1
    return 0


def run(cfg: Config) -> int:
    errors = validate_config(cfg)
    if errors:
        for error in errors:
            log(error, level="ERROR", log_file=None)
        return 2
    global _ACTIVE_LOG_FILE
    _ACTIVE_LOG_FILE = cfg.log_file
    log(f"Starting read-only library audit; source={cfg.source_dir}")
    log(f"Log={cfg.log_file}; report={cfg.report_file}")
    try:
        with ExclusiveRunLock(run_lock_path(cfg.source_dir), cfg.lock_timeout_seconds):
            started = time.perf_counter()
            audit = audit_library(cfg)
            audit.elapsed_sec = time.perf_counter() - started
            report = build_report(audit, cfg)
            atomic_write_text(cfg.report_file, report)
            counts = Counter(item.state for item in audit.folders)
            log(f"Audit complete: canonical_mkv={counts['CANONICAL_MKV']}; exceptions={len(audit.folders) - counts['CANONICAL_MKV']}; elapsed={audit.elapsed_sec:.2f}s")
            log(f"Report published: {cfg.report_file}")
            print(report, end="")
        return exit_code_for(counts, cfg)
    except LockUnavailable as exc:
        log(f"Audit lock unavailable: {exc}", level="ERROR")
        return 3
    except OSError as exc:
        log(f"Could not save report {cfg.report_file}: {exc}", level="ERROR")
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print and save a direct movie-file type report for Jellyfin movie folders.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--source", type=Path, default=Path(SOURCE_DIR), help="Top-level canonical Jellyfin movie-folder library")
    parser.add_argument("--log", type=Path, default=Path(LOG_FILE), help="Append-only execution log outside the media library")
    parser.add_argument("--report", type=Path, default=Path(REPORT_FILE), help="The sole replaceable plain-text output report")
    parser.add_argument("--lock-timeout", type=float, default=60.0, metavar="SECONDS", help="Maximum wait for another audit run")
    parser.add_argument("--fail-on-findings", action="store_true",
                        help="Exit 1 when any folder is not CANONICAL_MKV (includes missing sidecars)")
    parser.add_argument("--fail-on-defects", action="store_true",
                        help="Exit 1 only on layout defects; a missing sidecar alone still exits 0")
    parser.add_argument("--self-test", action="store_true")
    return parser


def cfg_from_args(args: argparse.Namespace) -> Config:
    return Config(
        source_dir=args.source,
        log_file=args.log,
        report_file=args.report,
        lock_timeout_seconds=args.lock_timeout,
        fail_on_findings=bool(args.fail_on_findings),
        fail_on_defects=bool(args.fail_on_defects),
    )


# =============================================================================
# SELF-TEST
# =============================================================================

def run_self_tests() -> int:
    errors: list[str] = []
    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    check(is_junk_filename("movie.mkv.!qB"), "torrent temporary suffix")
    root = Path(tempfile.mkdtemp(prefix="jellyfin_auditor_"))
    try:
        library, output = root / "library", root / "reports"
        library.mkdir(); output.mkdir()
        valid_srt = "1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n"
        (library / "Movie One (2020)").mkdir()
        (library / "Movie One (2020)" / "Movie One (2020).mkv").write_bytes(b"mkv")
        (library / "Movie One (2020)" / f"Movie One (2020){EXTERNAL_SRT_SUFFIX}").write_text(valid_srt, encoding="utf-8")
        (library / "Movie One (2020)" / "Featurettes").mkdir()
        (library / "Movie One (2020)" / "Featurettes" / "making-of.mp4").write_bytes(b"extra")
        (library / "Legacy (1999)").mkdir()
        (library / "Legacy (1999)" / "Legacy (1999).AVI").write_bytes(b"avi")
        (library / "Multiple (2001)").mkdir()
        (library / "Multiple (2001)" / "Multiple (2001).mkv").write_bytes(b"mkv")
        (library / "Multiple (2001)" / "Multiple (2001).mp4").write_bytes(b"mp4")
        (library / "No Movie (2002)").mkdir()
        (library / "No Movie (2002)" / f"No Movie (2002){EXTERNAL_SRT_SUFFIX}").write_text("sub", encoding="utf-8")
        (library / "Stem Mismatch (2003)").mkdir()
        (library / "Stem Mismatch (2003)" / "wrong-name.mkv").write_bytes(b"mkv")
        # A forced/flagged English SRT is not the canonical plain .eng.srt.
        (library / "Sidecar Mismatch (2004)").mkdir()
        (library / "Sidecar Mismatch (2004)" / "Sidecar Mismatch (2004).mkv").write_bytes(b"mkv")
        (library / "Sidecar Mismatch (2004)" / "Sidecar Mismatch (2004).eng.forced.srt").write_text(valid_srt, encoding="utf-8")
        # A correctly named sidecar whose contents are unusable is a real defect:
        # nothing downstream will replace a subtitle it believes is present.
        (library / "Broken Subs (2005)").mkdir()
        (library / "Broken Subs (2005)" / "Broken Subs (2005).mkv").write_bytes(b"mkv")
        (library / "Broken Subs (2005)" / f"Broken Subs (2005){EXTERNAL_SRT_SUFFIX}").write_text("sub", encoding="utf-8")
        # A validated legacy .en.srt is promoted to .eng.srt during the audit.
        (library / "Legacy En (2006)").mkdir()
        (library / "Legacy En (2006)" / "Legacy En (2006).mkv").write_bytes(b"mkv")
        (library / "Legacy En (2006)" / f"Legacy En (2006){LEGACY_EXTERNAL_SRT_SUFFIX}").write_text(valid_srt, encoding="utf-8")
        cfg = Config(source_dir=library, log_file=output / "audit.log", report_file=output / "report.txt", lock_timeout_seconds=0)
        audit = audit_library(cfg)
        states = {item.folder.name: item.state for item in audit.folders}
        check(states == {
            "Legacy (1999)": "SINGLE_OTHER_CONTAINER",
            "Movie One (2020)": "CANONICAL_MKV",
            "Multiple (2001)": "MULTIPLE_DIRECT_MOVIE_FILES",
            "No Movie (2002)": "NO_DIRECT_MOVIE_FILE",
            "Stem Mismatch (2003)": "MKV_STEM_MISMATCH",
            "Sidecar Mismatch (2004)": "NONCANONICAL_SIDECAR",
            "Broken Subs (2005)": "INVALID_SIDECAR",
            "Legacy En (2006)": "CANONICAL_MKV",
        }, f"folder states {states}")
        check(
            (library / "Legacy En (2006)" / f"Legacy En (2006){EXTERNAL_SRT_SUFFIX}").is_file(),
            "legacy .en.srt was promoted to .eng.srt",
        )
        check(
            not (library / "Legacy En (2006)" / f"Legacy En (2006){LEGACY_EXTERNAL_SRT_SUFFIX}").exists(),
            "legacy .en.srt removed after promote",
        )
        report = build_report(audit, cfg)
        check(f"Movie One (2020){EXTERNAL_SRT_SUFFIX}" not in report and "making-of.mp4" not in report, "non-direct media leaked into report")
        check("MKV stem mismatch: 1" in report and "Noncanonical SRT: 1" in report, "canonical exception counts")
        check("Invalid Eng SRT : 1" in report and "MOVIES WITH NO USABLE EXTERNAL ENGLISH SRT" in report,
              "unusable sidecar reported as actionable")
        atomic_write_text(cfg.report_file, report)
        check(cfg.report_file.read_text(encoding="utf-8") == report, "saved report differs")
        check(not list(output.glob("*.json")), "JSON output exists")
        check(bool(validate_config(Config(source_dir=library, log_file=output / "audit.log", report_file=library / "bad.txt", lock_timeout_seconds=0))), "report within library accepted")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("SELF-TEST PASSED (direct folders + types + single report)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_tests()
    try:
        if os.name == "nt":
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        return run(cfg_from_args(args))
    except KeyboardInterrupt:
        log("Interrupted")
        return 130
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
