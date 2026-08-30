# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Every report now shares one layout.** A new renderer in `common.py` (`Report`, `report_banner`, `print_text`) draws all six reports — `subtitle_fetcher.py`, `library_auditor.py`, `mkv_track_cleaner.py`, `movie_standardizer.py`, `10bit.py` and the `pipeline.py` summary — with the same boxed header, the same right-aligned scorecard, and the same titled sections. Each report leads with the counts, then a one-line "Start here:", then the groups ordered by how cheap the fix is, then the full inventory. Nothing overflows the page and no line carries trailing whitespace.
- **`subtitle_fetcher.py`'s report answers the two questions you actually ask.** Movies are no longer dumped into one flat list tagged with a status word: every result now carries a machine-readable reason, so the report splits cleanly into **MOVIES THAT NEED A SUBTITLE** (grouped by the fix — unusable sidecar, misnamed sidecar, layout defect, held for review, no provider match, deferred by quota, error) and **MOVIES THAT ALREADY HAVE AN EXTERNAL `.eng.srt`** (every covered movie listed with its sidecar file name). Movies the UTC cap cut off before they were scanned are now *named* in the report instead of only counted.
- **`library_auditor.py` leads with defects.** "Folders that need attention" now comes first, grouped by fix, with the full folder-by-folder inventory and the file-type table moved behind it.
- **`10bit.py` leads with the queue.** The HandBrake queue and the two REVIEW groups come first; native-HDR and already-high-bit-depth movies are listed last, as confirmation that nothing should be touched.
- **`mkv_track_cleaner.py` leads with what needs a decision** — errors, movies remuxed without a validated `.eng.srt` (moviehash now invalidated), hardlink-deferred movies and layout skips — before the per-movie remux detail.
- **`movie_standardizer.py` leads with `ITEMS LEFT IN SOURCE`**, the only section where inaction silently leaves files in the torrent folder forever.
- **Tool output no longer clutters the torrents root on Windows**: logs, reports, and probe-cache JSON now default to `E:\torrents\tools\ReportsAndLogs\<tool>\` (e.g. `E:\torrents\tools\ReportsAndLogs\mkv_track_cleaner\mkv_track_cleaner.log`) instead of five folders at the root of `E:\torrents`. Existing hooks scheduled with direct tool paths pick this up automatically; anything using explicit `--log`/`--report`/`--cache` paths is unaffected.
- **Foreign films with a validated `.eng.srt` are now cleaned** by `mkv_track_cleaner.py`: the best non-commentary audio of any language is kept, commentary/DVS is dropped, and every embedded subtitle is stripped so the external SRT is the sole subtitle option. Foreign films *without* a validated sidecar remain untouched.
- **Canonical external subtitle suffix is now `.eng.srt`** (ISO 639-2/B) instead of `.en.srt`.
  `subtitle_fetcher.py` writes `.eng.srt`, `mkv_track_cleaner.py` requires it before stripping embeds,
  `library_auditor.py` and `movie_standardizer.py` treat it as the sole canonical sidecar name.
  A validated legacy `.en.srt` is automatically renamed to `.eng.srt` on the next fetcher, cleaner, or auditor run.

### Fixed
- **`movie_standardizer.py --help` reported a stale default report path.** The `--report` help string claimed an old default of `E:\torrents\movie_standardizer\movie_standardizer_report.txt`, but the actual default (and the report that is written) is `E:\torrents\tools\ReportsAndLogs\movie_standardizer\movie_standardizer_report.txt`. The help now matches the real constant, consistent with every other tool's `ReportsAndLogs` default.
- **`mkv_track_cleaner.py --version` printed the old script name.** It hardcoded `track_cleaner.py {VERSION}`; every other tool derives its name from the invoked path (`%(prog)s`). It now prints `mkv_track_cleaner.py {VERSION}`.
- **Placeholder-free `f`-strings and an unused `except` binding were removed** (`organize.py`, `library_auditor.py`) — caught by the project's `ruff` rules.
- **Reports no longer depend on the console's encoding.** Every tool now pins its own stdout/stderr to UTF-8 with `errors="replace"` on *every* platform (`common.enable_utf8_stdio`; previously this ran on Windows only), the `log()` helpers print through the encoding-safe path, and every caller that captures a child process — `organize.py` and the test suite — decodes it as UTF-8 instead of with the locale encoding. Before this, a boxed report was a `UnicodeDecodeError` waiting to happen on Windows (cp1252) and a `UnicodeEncodeError` on a runner with no locale set. The report *file* is written UTF-8 either way, so a limited console degrades to `?` instead of losing data or aborting a run.
- **Report entries wrap instead of being cut off.** `Report.entry()` used to ellipsize an
  entry that overflowed the 96-column page, which silently destroyed exactly what the line
  was there to name: on macOS the standardizer's source paths run ~90 columns, so
  `.../final/Small.1995.mkv` printed as `.../final/Small...`. Entry text now wraps at path
  separators (`common.wrap_path_text`), so the movie folder or file name always lands whole
  on its own line; the boxed header's metadata rows break the same way instead of splitting a
  directory name mid-word. A detail that shares an entry's line now really does start at its
  column instead of drifting four columns right. Tables still clip — columns have to line up.

### Added
- **SubDL is now a first-class subtitle provider.** Configure `SUBDL_API_KEY` to add SubDL's documented release-aware `/api/v2/files/search` fallback after OpenSubtitles has no safe exact-hash or title/year candidate, or run SubDL by itself when OpenSubtitles is unavailable. Automatic release picks require the response `match` to confirm the canonical movie and `match_score >= 0.80`; strict title/year lookup occurs only when the filename route yields no usable candidate, never after a low-score or ambiguous one. The v2 client uses Bearer authentication, sends only the local basename, requests the documented `format=file` opaque-ID download route, validates raw download hosts, fails closed on multi-SRT archives, rechecks the movie snapshot, and never logs provider URLs or credentials. The append-only ledger separately reserves OpenSubtitles downloads plus SubDL's documented free-tier 2,000 searches and 50 downloads per day; each SubDL search request (including a retry) and selected download is reserved before it is sent.
- **Expanded provider coverage tests** exercise SubDL v2 authentication and documented response parsing, match-record identity validation, score gating, safe URL handling, multi-file archive rejection, SubDL-only operation, release-to-title fallback ordering, and separate search/download quota reservations.
- **28 new report tests** (244 total): `tests/reporttext.py` parses the scorecard and sections back out of a rendered report, `tests/test_reports.py` builds one report per tool and asserts the shared contract, and `tests/test_common.py` covers the renderer itself (width invariants, wrapping, clipping, partial-run tallies), and `tests/test_reports.py` runs the fetcher under `PYTHONIOENCODING=ascii` to prove a hostile console cannot abort a run or corrupt the report file.

## [3.1.0] - 2026-08-29

### Added
- **Continuous Integration is live**: `.github/workflows/ci.yml` runs the entire offline suite on every push and pull request across **Python 3.11 / 3.12 / 3.13 × Ubuntu / Windows / macOS**, plus a CLI smoke job (dashboard, doctor, `--list-steps`) on pristine machines.
- **Visual identity**: hand-crafted, resolution-independent `docs/assets/banner.svg` and `docs/assets/logo.svg` (stacked movie cases + play emblem), plus a ready-to-upload `docs/assets/social-preview.png` (set it in repo *Settings → Social preview*).
- **Real terminal screenshots**: `docs/assets/terminal-*.png` rendered from live `organize doctor`, dashboard, and audit output — embedded throughout the README.
- **`pipeline.py` now honors `MOVIE_STD_TARGET`** as the default library root when `--source` is not passed, so `docker compose run --rm organize run` works without retyping the path (covered by new unit tests and the pipeline self-test).
- **Community files**: `SECURITY.md` (scoped to the media-safety guarantees, with private reporting guidance), issue-template contact links (`.github/ISSUE_TEMPLATE/config.yml`), and `.editorconfig`.
- **Expanded `.env.example`**: every supported environment variable with annotations, not just the two most common.

### Changed
- **README fully restructured**: hero banner, live CI badge, contextual screenshots, consolidated pipeline diagram, tool table, "Why it's different" guarantees table, collapsible deep dives, FAQ quick-hits, and documentation navigation.
- **Documentation consolidated and navigable**: the duplicated root-level `WINDOWS_SETUP.md` runbook was merged into `docs/WINDOWS_GUIDE.md` (first-run ramp-up, "things that will surprise you", everyday commands included); every guide now carries breadcrumbs back to the index and cross-links to related guides.
- **Docker Compose is hardlink-foolproof**: mounts the shared parent volume (as the guide already recommended) instead of two sibling submounts, with explanatory comments; the Dockerfile gained OCI labels.
- `pyproject.toml` now declares project URLs and the author; version bumped to 3.1.0 alongside `organize.py`.
- Unit suite grew to **208 tests** (library-root resolution coverage for the pipeline).

### Fixed
- `organize doctor` suggested the wrong flag (`--source`) in the remedy for a missing *library* directory; it now correctly points at `--target` (and the source check mentions `--source`/`MOVIE_STD_SOURCE`).

## [3.0.0] - 2026-08-29

### Added
- **Unified CLI (`organize.py`)**: A single unified command-line entrypoint for the entire suite (`organize doctor`, `organize run`, `organize standardize`, `organize subtitles`, `organize clean`, `organize 10bit`, `organize audit`, `organize test`).
- **System Doctor & Diagnostics**: Comprehensive environment, binary (`mkvmerge`, `ffprobe`), API key, and hardlink filesystem compatibility check with remediation guidance.
- **Cross-Platform Launcher Scripts**: Added `organize.sh` for POSIX/Linux/macOS, and `organize.ps1` / `organize.bat` for Windows PowerShell and Command Prompt.
- **Docker Support**: Added official lightweight `Dockerfile` (with FFmpeg and MKVToolNix built-in) and ready-to-use `docker-compose.yml`.
- **Comprehensive Documentation Suite (`docs/`)**:
  - `docs/WINDOWS_GUIDE.md`: Deep dive into Windows 11, PowerShell, and Task Scheduler.
  - `docs/LINUX_DOCKER_GUIDE.md`: Step-by-step setup for Linux, Docker, Unraid, TrueNAS, and systemd/cron.
  - `docs/JELLYFIN_DIRECT_PLAY.md`: In-depth engineering rationale behind Jellyfin Direct Play.
  - `docs/ARCHITECTURE_SAFETY.md`: Concurrency, atomic operations, and invariant design documentation.
  - `docs/CONFIGURATION_REFERENCE.md`: Complete reference of CLI flags and environment variables.
  - `docs/FAQ_TROUBLESHOOTING.md`: Common operational questions and solutions.
- **Continuous Integration (CI)**: GitHub Actions workflow testing Python 3.11, 3.12, and 3.13 across Ubuntu and Windows.
- **Project Governance**: Added `LICENSE` (MIT), `CONTRIBUTING.md`, `.env.example`, and GitHub issue/PR templates.
- **New Unit Tests**: Added `tests/test_organize_cli.py` bringing the test suite to 205 unit tests.

## [2.7.0]

### Added
- Hardlink-only canonical placement contract in `movie_standardizer.py` (`os.link` with zero duplicate disk allocation).
- Fail-closed advisory cross-process locks via `common.CoordinationLock`.

## [2.6.0]

### Added
- Canonical movie-and-English-subtitle contract (`Title (Year)/Title (Year).mkv` + `.eng.srt`).
- OpenSubtitles moviehash prioritization in `subtitle_fetcher.py`.
- Shared external subtitle validation contract in `common.py`.

## [2.5.0]

### Added
- Reusable `MediaProbeCache` in `common.py` eliminating repetitive subprocess probes across runs.
- Lossless MKV remux verification with pre- and post-flight track fingerprinting in `mkv_track_cleaner.py`.
