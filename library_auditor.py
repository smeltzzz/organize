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
import sys
import tempfile
import time
import traceback
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Shared implementation: everything imported here is defined exactly once,
# in organizekit/core/. See tests/test_shared_core.py for the rule that
# keeps it that way.
from organizekit.core import (
    COVERING_ENGLISH_SRT_SUFFIXES,
    EXTERNAL_SRT_SUFFIX,
    KIND_LAYOUT,
    KIND_SUBTITLE,
    LEGACY_EXTERNAL_SRT_SUFFIX,
    ExclusiveRunLock,
    LockUnavailable,
    Report,
    RunLog,
    atomic_write_text,
    default_tool_dir,
    enable_utf8_stdio,
    map_ordered,
    open_state,
    path_is_within,
    print_text,
    promote_legacy_external_english_srt,
    resolve_library,
    resolve_workers,
    run_field_smoke_test,
    validate_srt_sidecar,
)

# The single agreed decode order. Every tool that turns subtitle bytes into
# text uses this tuple and nothing else, so a tool cannot quietly accept an
# encoding the others would reject. "utf-8-sig" first so a provider BOM does
# not make an otherwise valid file look binary; "cp1252" last because it
# decodes almost any byte sequence and would mask a genuine encoding problem.


SOURCE_DIR = str(resolve_library())
# Logs and reports live under tools\ReportsAndLogs so the root of E:\torrents
# stays media-only.
OUTPUT_DIR = str(default_tool_dir("library_auditor"))
LOG_FILE = str(default_tool_dir("library_auditor") / "library_auditor.log")
REPORT_FILE = str(default_tool_dir("library_auditor") / "library_auditor_report.txt")
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

# The audit is I/O bound, so more workers than CPUs still helps - it is waiting
# on the filesystem, not computing. The ceiling keeps a network share from
# being hammered by an unbounded fan-out.
MAX_AUDIT_WORKERS = 8

@dataclass
class Config:
    source_dir: Path = field(default_factory=lambda: Path(SOURCE_DIR))
    log_file: Path = field(default_factory=lambda: Path(LOG_FILE))
    report_file: Path = field(default_factory=lambda: Path(REPORT_FILE))
    lock_timeout_seconds: float = 60.0
    fail_on_findings: bool = False
    fail_on_defects: bool = False
    workers: int = 0  # 0 = decide from the CPU count; 1 = walk folders one by one
    use_state: bool = True       # publish the verdicts to the shared state cache
    state_db: Path | None = None  # None = the documented default location

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

# The console/file logger every tool shares: see organizekit/core/runlog.py
# for why a logging failure is never allowed to end a run.
log = RunLog()

def is_junk_filename(name: str) -> bool:
    lower = name.casefold()
    return lower.startswith(".") or lower in {"thumbs.db", "desktop.ini"} or any(lower.endswith(s) for s in JUNK_SUFFIXES)

def is_in_flight_remux(name: str) -> bool:
    """True for a mkv_track_cleaner.py staging file written during a remux."""
    return name.casefold().startswith(TRACK_CLEANER_TEMP_PREFIX)


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

    covering_srts = {f"{folder.name}{suffix}" for suffix in COVERING_ENGLISH_SRT_SUFFIXES}
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
    unexpected_srt = [name for name in srt_names if name not in covering_srts]
    if unexpected_srt:
        return FolderAudit(folder, "NONCANONICAL_SIDECAR", files, "; ".join(unexpected_srt))
    covering_present = [name for name in srt_names if name in covering_srts]
    if not covering_present:
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
    last_reason = ""
    for name in covering_present:
        usable, reason = validate_srt_sidecar(folder / name)
        if usable:
            return FolderAudit(folder, "CANONICAL_MKV", files)
        last_reason = f"{name} is unusable ({reason}); delete it and re-run subtitle_fetcher.py"
    return FolderAudit(folder, "INVALID_SIDECAR", files, last_reason)

def audit_library(cfg: Config) -> Audit:
    try:
        folders = sorted((p for p in cfg.source_dir.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name.casefold())
    except OSError as exc:
        log(f"Cannot enumerate library: {exc}", level="ERROR")
        folders = []
    log(f"Found {len(folders)} top-level movie folder(s).")

    # The audit is thousands of stat() calls and directory reads, and almost
    # none of it is CPU: on a network share the round trip dominates, and even
    # locally the process spends its time waiting on the filesystem. Folders
    # are independent and nothing here writes, so they are classified in
    # parallel - but the results come back in *input* order, so the numbered
    # console output, the log and the report are identical to the serial run
    # they replaced. --workers 1 is that serial run.
    workers = resolve_workers(cfg.workers, items=len(folders), cap=MAX_AUDIT_WORKERS)
    if workers > 1:
        log(f"Reading {len(folders)} folder(s) with {workers} workers (read-only).")
    audited: list[FolderAudit] = []
    for index, outcome in enumerate(map_ordered(folders, classify_folder, workers=workers), 1):
        if outcome.error is not None:
            # classify_folder swallows the filesystem errors it expects, so
            # anything arriving here is a real defect in this tool. Re-raise it
            # rather than quietly auditing a library that was never read.
            raise outcome.error
        folder, result = outcome.item, outcome.value
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

# What the audit's own vocabulary means to everything else. The audit decides
# two separate things about a folder - is the layout canonical, and is there a
# usable English sidecar - and reports them as one state, because a folder with
# no movie file cannot have a sidecar problem worth naming. `organize status`
# needs them apart, so the split is stated here, by the tool that owns the
# vocabulary, rather than being re-derived by a reader that would guess.
SUBTITLE_STATE_FOR_AUDIT = {
    "CANONICAL_MKV": "present",
    "MISSING_SIDECAR": "missing",
    "INVALID_SIDECAR": "invalid",
    "NONCANONICAL_SIDECAR": "noncanonical",
}


def publish_state(audit: Audit, cfg: Config) -> int:
    """Record this audit's verdicts in the shared state cache.

    Best effort by construction: the cache is a convenience for
    ``organize status`` and for skipping settled movies on a later pass. It is
    never read back as authority, and a failure to write it must not affect the
    audit's report, its exit code, or anything a scheduler keys on.
    """
    store = open_state(cfg.state_db, enabled=cfg.use_state, tool="library_auditor")
    if not store.enabled:
        return 0
    published = 0
    try:
        seen: list[str] = []
        for item in audit.folders:
            if len(item.movie_files) != 1:
                continue  # no single movie file: nothing to key a verdict on
            movie = item.folder / item.movie_files[0].name
            seen.append(store.see_movie(movie, folder=item.folder))
            store.record(movie, KIND_LAYOUT, item.state, item.detail)
            subtitle_state = SUBTITLE_STATE_FOR_AUDIT.get(item.state)
            if subtitle_state is not None:
                store.record(movie, KIND_SUBTITLE, subtitle_state, item.detail)
            published += 1
        store.forget_missing(seen)
        store.note("audit", f"{published} movie(s) audited")
        store.prune_events()
    except Exception as exc:  # noqa: BLE001 - a cache write can never fail a run
        log(f"state cache not updated: {exc}", level="WARNING")
    finally:
        store.close()
    return published


# =============================================================================
# REPORT
# =============================================================================

def types_for(item: FolderAudit) -> str:
    return ", ".join(sorted({file.extension.upper() for file in item.movie_files})) if item.movie_files else "—"

def names_for(item: FolderAudit) -> str:
    return "; ".join(file.name for file in item.movie_files) if item.movie_files else "—"

@dataclass(frozen=True)
class StateGuide:
    """How one audit state reads in the report: a label, a hint and a fix."""

    state: str
    label: str
    hint: str
    title: str
    fix: str

# Reading order is the order of the report: the two sidecar states share one
# actionable group (that is the group subtitle_fetcher.py exists to clear), and
# the remaining layout defects follow, cheapest fix first.
STATE_GUIDES: tuple[StateGuide, ...] = (
    StateGuide(
        "MISSING_SIDECAR", "Missing Eng SRT", "run subtitle_fetcher.py",
        "MOVIES WITH NO USABLE EXTERNAL ENGLISH SRT (ACTIONABLE)",
        "Run subtitle_fetcher.py before mkv_track_cleaner.py: fetching first keeps the "
        "pristine release moviehash, which is what makes an exact subtitle match possible.",
    ),
    StateGuide(
        "INVALID_SIDECAR", "Invalid Eng SRT", "delete the broken sidecar",
        "MOVIES WITH NO USABLE EXTERNAL ENGLISH SRT (ACTIONABLE)",
        "An INVALID entry means a sidecar exists but is unusable - delete that file first, "
        "because no tool will replace a sidecar it believes is already present.",
    ),
    StateGuide(
        "NONCANONICAL_SIDECAR", "Noncanonical SRT", f"rename it to {EXTERNAL_SRT_SUFFIX}",
        "SIDECAR NAME IS NOT CANONICAL",
        f"Rename the subtitle to \"<movie>{EXTERNAL_SRT_SUFFIX}\". Jellyfin and Plex only "
        "direct play that exact name beside the MKV.",
    ),
    StateGuide(
        "MKV_STEM_MISMATCH", "MKV stem mismatch", "rename the MKV to match its folder",
        "MKV NAME DOES NOT MATCH ITS FOLDER",
        "The movie file must be named exactly like the folder that holds it: "
        "\"Title (Year)/Title (Year).mkv\".",
    ),
    StateGuide(
        "SINGLE_OTHER_CONTAINER", "Single other container", "remux to MKV or accept it",
        "MOVIE IS NOT AN MKV",
        "MKV is the only container this toolkit cleans and the only one guaranteed to "
        "carry an external SRT plus every track type Jellyfin direct plays.",
    ),
    StateGuide(
        "MULTIPLE_DIRECT_MOVIE_FILES", "Multiple movie files", "keep one feature per folder",
        "MORE THAN ONE MOVIE FILE IN A FOLDER",
        "One folder holds one feature. Move extras into a \"extras\" subfolder or into "
        "their own \"Title (Year)\" folder.",
    ),
    StateGuide(
        "NO_DIRECT_MOVIE_FILE", "No movie file", "the folder holds no feature",
        "FOLDER HAS NO MOVIE FILE",
        "Nothing in this folder is a movie container. Delete the leftover or move the "
        "movie back in.",
    ),
    StateGuide(
        "INACCESSIBLE", "Inaccessible", "check permissions and the path",
        "FOLDER COULD NOT BE READ",
        "The folder could not be listed at all. Check permissions, ownership and that "
        "the path still exists.",
    ),
)
CANONICAL_LABEL = "Canonical MKV"
CANONICAL_HINT = f"one MKV + a validated {EXTERNAL_SRT_SUFFIX}"

def build_report(audit: Audit, cfg: Config) -> str:
    """Render the audit: what is wrong first, then the full inventory."""
    counts = Counter(item.state for item in audit.folders)
    type_counts = Counter(file.extension.upper() for item in audit.folders for file in item.movie_files)
    total = len(audit.folders)
    findings = total - counts["CANONICAL_MKV"]
    by_state: dict[str, list[FolderAudit]] = {}
    for item in audit.folders:
        by_state.setdefault(item.state, []).append(item)

    report = Report(
        "JELLYFIN MOVIE LIBRARY AUDIT",
        "Read-only health check \u00b7 canonical folder layout and external English "
        "subtitle integrity",
    )
    report.metas([
        ("Generated", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")),
        ("Library", audit.source_dir),
        ("Folders checked", total),
        ("Elapsed", f"{audit.elapsed_sec:.2f}s"),
        ("Report", cfg.report_file),
    ])

    rows: list[tuple[object, str, str]] = [(counts["CANONICAL_MKV"], CANONICAL_LABEL, CANONICAL_HINT)]
    for guide in STATE_GUIDES:
        rows.append((counts[guide.state], guide.label, guide.hint))
    rows.append((total, "Folders checked", "every top-level folder in the library"))
    report.blank()
    report.scorecard(rows)
    if findings:
        report.paragraph(
            f"Start here: {findings} of {total} folder(s) are not canonical \u00b7 every one is "
            "listed below with the fix that clears it."
        )
    else:
        report.paragraph(
            f"Nothing to do: all {total} folder(s) hold exactly one MKV named like their "
            f"folder, each with a validated {EXTERNAL_SRT_SUFFIX} beside it."
        )

    report.section(
        "FOLDERS THAT NEED ATTENTION",
        count=findings,
        total=total,
        intro="Grouped by the fix that clears them, cheapest first. A folder appears in exactly one group.",
    )
    if not findings:
        report.paragraph("None. The library is fully canonical.")
    else:
        # The two sidecar states share one group: both mean "no usable .eng.srt".
        sidecar_states = [guide for guide in STATE_GUIDES if guide.state in ("MISSING_SIDECAR", "INVALID_SIDECAR")]
        sidecar_items = [item for state in ("MISSING_SIDECAR", "INVALID_SIDECAR") for item in by_state.get(state, [])]
        if sidecar_items:
            report.subsection(sidecar_states[0].title, count=len(sidecar_items))
            for guide in sidecar_states:
                if counts[guide.state]:
                    report.paragraph(f"{guide.label}: {guide.fix}")
            report.blank()
            report.entries(
                [{"text": item.folder.name,
                  "detail": f"{_state_label(item.state)}  \u00b7  {item.detail or names_for(item)}"}
                 for item in sorted(sidecar_items, key=lambda entry: entry.folder.name.casefold())],
            )
        for guide in STATE_GUIDES:
            if guide.state in ("MISSING_SIDECAR", "INVALID_SIDECAR"):
                continue
            items = by_state.get(guide.state) or []
            if not items:
                continue
            report.subsection(guide.title, count=len(items))
            report.paragraph(guide.fix)
            report.blank()
            report.entries(
                [{"text": item.folder.name, "detail": item.detail or names_for(item)}
                 for item in sorted(items, key=lambda entry: entry.folder.name.casefold())],
            )

    report.section(
        "EVERY FOLDER CHECKED",
        count=total,
        intro="The complete inventory, healthy folders included.",
    )
    if not audit.folders:
        report.paragraph("No top-level movie folders found.")
    else:
        report.table(
            ["Folder", "Status", "Type(s)", "Movie file(s) / detail"],
            [[item.folder.name,
              item.state.replace("_", " "),
              types_for(item),
              item.detail or names_for(item)]
             for item in audit.folders],
            aligns="<<<<",
        )

    report.section("DIRECT MOVIE FILE TYPES", intro="Container labels are file extensions only.")
    if type_counts:
        report.table(
            ["Type", "Files"],
            [[extension, count]
             for extension, count in sorted(type_counts.items(), key=lambda pair: (-pair[1], pair[0]))],
            aligns="<>",
        )
    else:
        report.paragraph("No direct movie-container files found.")

    canonical = counts["CANONICAL_MKV"]
    pct = (100.0 * canonical / total) if total else 100.0
    report.footer([
        # Machine-readable verdict for orchestrators (jellyfin_one_shot.py):
        # one stable line, no layout assumptions.
        f"AUDIT SUMMARY: canonical={canonical}; total={total}; pct={pct:.1f}%",
        "Scope: direct feature containers plus direct SRT sidecar names. Artwork, NFO files, "
        "and nested extras are ignored.",
        "Container labels are file extensions only; they do not verify codecs or Jellyfin "
        "client direct-play support.",
    ])
    return report.render()

def _state_label(state: str) -> str:
    """The short scorecard label for a state (``MISSING`` for a missing sidecar)."""
    for guide in STATE_GUIDES:
        if guide.state == state:
            return guide.label.split(" ")[0].upper()
    return state.replace("_", " ")

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
    if cfg.workers < 0:
        errors.append("--workers must be non-negative (0 = decide from the CPU count)")
    if cfg.state_db is not None and path_is_within(cfg.state_db, cfg.source_dir):
        errors.append(f"--state-db must be outside --source: {cfg.state_db}")
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
    log.file = cfg.log_file
    log(f"Starting read-only library audit; source={cfg.source_dir}")
    log(f"Log={cfg.log_file}; report={cfg.report_file}")
    try:
        with ExclusiveRunLock(run_lock_path(cfg.source_dir),
                              cfg.lock_timeout_seconds,
                              busy_message="another audit owns {path}"):
            started = time.perf_counter()
            audit = audit_library(cfg)
            audit.elapsed_sec = time.perf_counter() - started
            publish_state(audit, cfg)
            report = build_report(audit, cfg)
            atomic_write_text(cfg.report_file, report)
            counts = Counter(item.state for item in audit.folders)
            log(f"Audit complete: canonical_mkv={counts['CANONICAL_MKV']}; exceptions={len(audit.folders) - counts['CANONICAL_MKV']}; elapsed={audit.elapsed_sec:.2f}s")
            log(f"Report published: {cfg.report_file}")
            print_text(report)
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
    parser.add_argument("--no-state", action="store_true",
                        help="Do not record this audit in the shared state cache "
                             "that `organize status` reads. The audit itself is "
                             "unaffected: the cache is never read as authority.")
    parser.add_argument("--state-db", type=Path, default=None, metavar="PATH",
                        help="Where that cache lives (default: beside the logs and reports)")
    parser.add_argument("--workers", type=int, default=0, metavar="N",
                        help=f"Read N movie folders at once (0 = decide from the CPU count, "
                             f"capped at {MAX_AUDIT_WORKERS}; 1 = one folder at a time). The "
                             f"output is identical either way.")
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
        workers=int(args.workers),
        use_state=not bool(args.no_state),
        state_db=args.state_db,
    )

# =============================================================================
# SELF-TEST
# =============================================================================


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_tests()
    try:
        enable_utf8_stdio()
        return run(cfg_from_args(args))
    except KeyboardInterrupt:
        log("Interrupted")
        return 130
    except Exception:  # noqa: BLE001 - last resort: whatever went wrong, this run
        # leaves through one exit code instead of an unhandled traceback.
        traceback.print_exc()
        return 1


def run_self_tests() -> int:
    """Field smoke test: can this copy of the auditor read a library?

    The full audit matrix lives in ``tests/selftests/``. Here we build a
    two-folder library in a temporary directory and check that the canonical
    one passes and the incomplete one does not.
    """
    def canonical_folder_passes() -> bool:
        with tempfile.TemporaryDirectory(prefix="auditor_smoke_") as td:
            folder = Path(td) / "Movie (2020)"
            folder.mkdir()
            (folder / "Movie (2020).mkv").write_bytes(b"x" * 1024)
            (folder / "Movie (2020).eng.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
            return classify_folder(folder).state == "CANONICAL_MKV"

    def missing_sidecar_is_flagged() -> bool:
        with tempfile.TemporaryDirectory(prefix="auditor_smoke_") as td:
            folder = Path(td) / "Movie (2020)"
            folder.mkdir()
            (folder / "Movie (2020).mkv").write_bytes(b"x" * 1024)
            return classify_folder(folder).state != "CANONICAL_MKV"

    def torrent_debris_is_junk() -> bool:
        return is_junk_filename("movie.mkv.!qB") and not is_junk_filename("movie.mkv")

    return run_field_smoke_test("library_auditor.py", [
        ("a canonical folder audits clean", canonical_folder_passes),
        ("a missing sidecar is reported", missing_sidecar_is_flagged),
        ("torrent debris is recognised", torrent_debris_is_junk),
    ])

if __name__ == "__main__":
    sys.exit(main())
