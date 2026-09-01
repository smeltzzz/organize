# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`subtitle_fetcher.py` now has nine sources and a hard coverage goal: a validated English SRT beside every movie.** Seven scraping fallback sources join OpenSubtitles and SubDL in a new tier-3 failover chain — **Subf2m.co → Podnapisi.NET → Addic7ed.com → SubSource.net → Subsunacs.net → YIFY Subtitles → Subs.sab.bz** — vendored into `subtitle_fetcher.py` itself (stdlib-only, no keys, no accounts), so the fetcher remains a single self-contained file. A scraped candidate is accepted only when it names the movie, matches its release year, and decodes to a valid English SRT; each source carries a per-run circuit breaker (3 hard or 3 repeated parse failures disable it for the rest of the run) and a UTC daily search cap (20 per source, `--scrape-daily-cap`) metered in the same durable ledger as the API quotas. The report now opens with a coverage scorecard and names the verdict of every source per uncovered movie; uncovered movies are re-offered to the scraping tier on every later UTC day (same-day exhaustion is never re-spent), and the process exits 1 while any movie is uncovered unless `--allow-missing` is given. A run with no API keys is valid: every movie goes straight to the scraping tier. New flags: `--scrape-daily-cap`, `--skip-source` (repeatable), `--allow-missing`.
- **`sync_subtitles.py` — subtitle-timing sync with ffsubsync, the pipeline's final content step.** Walks the whole movie library, pairs every non-junk `.srt` sidecar with its movie file (`.mkv` preferred; ffsubsync's own sibling-detection naming rule), and measures the subtitle drift against the actual audio with `ffsubsync` (`pip install ffsubsync`, needs `ffmpeg` on the PATH). Trustworthy drift is applied by atomically swapping in the corrected sidecar; sub-threshold drift (`--min-offset`, default 0.1 s) leaves the file byte-identical; untrustworthy drift — beyond the trust window (`--max-offset`, default 30 s), anti-correlated scores, ffsubsync's own `--skip-sync-on-low-quality` refusal, or any ffsubsync failure — is held for review with the original untouched. The tool writes its own detailed report and append-only log (`E:\torrents\tools\ReportsAndLogs\sync_subtitles\` by default), participates in the shared coordination lock, and exits 0 / 1 (failures) / 2 (config or missing ffsubsync) / 3 (reviews, with `--fail-on-review`) for scheduler gating. It is stdlib-only: ffsubsync is launched as a subprocess, exactly like mkvmerge and ffprobe, so the zero-pip-dependency invariant holds.
- **`pipeline.py` now has five steps: `fetcher → cleaner → 10bit → sync → auditor`.** Subtitle sync runs just before the library audit on purpose: it rewrites subtitle bytes only (never movie bytes, so the OpenSubtitles moviehash is undisturbed) but must finish first so the audit validates the finished sidecars. The step skips cleanly — with the exact fix printed — when ffsubsync or ffmpeg is not installed, matching every other step.
- **`organize.py doctor` checks `ffsubsync` and `ffmpeg`** and reports the one-line install (`pip install ffsubsync`) when they are missing; a `sync` subcommand delegates to the new tool.
- **`jellyfin_one_shot.py` version 1.0.0 — the "never stop" one-shot completer, hardened to actually finish.** The orchestrator that loops fetch → clean → 10bit → sync → audit until the auditor reports 100% canonical is now the only script in the repo that cannot quietly spin forever: an empty library (no movie folders) exits 2 with the canonical-layout reminder instead of looping on 0/0; a `--log-dir` inside the library is rejected up front (every tool refuses to write inside the media tree, and the auditor would count a log folder as a movie folder — that combination could never reach 100%); three passes in a row without a usable audit report exit 1 with the transcript paths to debug; and two passes with zero coverage improvement sleep until the next UTC midnight, when the provider daily caps reset and the scraping tier re-offers held movies, instead of hot-looping full pipeline passes. `--dry-run` now previews exactly one pass and exits 0 (a dry run writes nothing, so a second pass would be identical); `--version` and `--timeout-scale` (0 = no timeout) join the CLI. All per-tool logs, reports, probe caches, and bounded rolling full-output transcripts are pinned under `--log-dir`, so a run is self-contained on any platform. It is wired into the unified CLI as `organize.py one-shot` (aliases `oneshot`, `complete`), and `organize.py test` now runs its self-test too.
- **`library_auditor.py` reports a machine-readable verdict.** Every report now ends with a stable `AUDIT SUMMARY: canonical=N; total=M; pct=P%` footer line, so orchestrators parse the contract instead of the layout.
- **`tests/test_one_shot.py` — 35 new offline tests for the one-shot completer.** Coverage parsing (including a round-trip against the auditor's real rendered report), completeness rules, log-dir validation, transcript bounding, and the orchestrator's end-state behaviour (complete/partial/empty library, dry-run single pass, max-passes, persistent audit failure, lock-contention retry, UTC-rollover pacing, stagnation reset) — all with `run_tool` faked so no subprocess is launched.

### Changed
- **`jellyfin_one_shot.py` now decodes tool output as UTF-8 and pins its own console to UTF-8.** `run_tool` captured sibling tools with bare `text=True` (the locale encoding — cp1252 on Windows), which raises `UnicodeDecodeError` on the first box-drawing character of any report, and the script printed `✓`/`—` without pinning its own streams, which raises `UnicodeEncodeError` on exactly the success banner on a legacy Windows console. Both now follow the repo-wide contract every other tool already uses.
- **`movie_standardizer.py` self-test no longer writes into the CWD.** The self-test's config kept the Windows default report path, which on POSIX materialized as a literal `E:\...` file in the working directory; the self-test now passes `report_file=None` like it already does for the log. Its ffprobe invocation also decodes with explicit UTF-8 for consistency with the other tools.
- **CI byte-compile gate compiles the scripts that exist.** The syntax gate referenced `common.py` (removed in 3.3.0) and missed `sync_subtitles.py` and `jellyfin_one_shot.py`; it now compiles all nine runnable scripts, runs the one-shot self-test explicitly, and the stale "208 tests" comment is gone.

- **`subtitle_fetcher.py` version 2.10.0.** The tier-1/tier-2 API routing is unchanged in semantics; the new tier-3 scraping chain is entered only after both API tiers miss (and never in dry-runs, which spend no scraping requests), so existing API-key configurations behave exactly as before apart from the new report lines and the coverage-based exit code.
- **`subtitle_fetcher.py` picks the Blu-ray release that names the movie with the most downloads.** Automatic selection on both OpenSubtitles routes (exact-moviehash and the conservative title/year fallback) now requires the candidate's release name to name the movie and to carry an explicit Blu-ray keyword (`BluRay`, `Blu-ray`, `BLU RAY`, ...), and ranks the qualifying candidates by download count instead of trusted/rating/votes — those quality signals remain as tiebreakers. A download-count tie that the quality signals cannot break is still held for manual review. The score-gated SubDL fallback is unchanged.
- **`subtitle_fetcher.py` widens auto-selection to the release year and drops the quality floor.** Supersedes the bullet above: the release name must now carry the movie title, **the release year as a standalone number**, and a Blu-ray keyword, and among the qualifying candidates the one with the **most downloads** wins. The rating/votes/download minimums on the title/year route are gone, so popular-but-unvoted subtitles for big-name movies fetch automatically; trusted/rating/votes remain tiebreakers only, and an unbroken download tie is still held for manual review. Applies to both OpenSubtitles routes and SubDL's strict title/year fallback; the score-gated SubDL release route is unchanged.
- **`subtitle_fetcher.py` treats OpenSubtitles and SubDL as equal sources.** Previously OpenSubtitles always won the routing: SubDL ran only after OpenSubtitles had no safe candidate or was out of quota. Now both providers' release-identifying routes are consulted for every movie — the exact OpenSubtitles moviehash and SubDL's score-gated release-aware filename match (score ≥ 0.80) — and the qualifying release with the **most downloads** is downloaded, whichever provider it came from. When neither release route yields a pick, both providers' strict title/year routes are pooled the same way. In a cross-provider comparison a candidate must also carry the release-name policy (title, release year, Blu-ray keyword), so a high-download WEB release on one provider can never beat a qualifying release on the other. SubDL's no-weakening rule is kept (a low-score release match never falls back to its generic title route), `--no-identity-fallback` still disables SubDL (it has no hash route), per-provider daily caps are enforced independently as before, and an unbroken cross-provider tie is held for manual review.

### Fixed
- **`jellyfin_one_shot.py` dry-run and empty-library infinite loops** (see Added above): a `--dry-run` without `--max-passes` previously looped forever because nothing it does can move the auditor's verdict, and an empty (or wrong) `--source` looped on 0/0 coverage.
- **`jellyfin_one_shot.py` hot loop when the audit keeps failing** — a broken or lock-blocked auditor left the runner on "unknown" coverage with no backoff; per-pass audit retries (5/15/45 s) plus a three-pass hard stop make the failure mode visible and bounded.

## [3.3.0] - 2026-08-30

### Changed
- **The repo is now as simple as the tools are standalone.** The shared module `common.py` is gone: every helper it provided (report rendering, fail-closed locks, the SRT sidecar contract, probe cache, atomic writes) is now **vendored inline** in a clearly marked section at the top of each script that uses it. Each of the five tools is a single self-contained file — copy just the one(s) you need into your own setup and run them with nothing but Python 3.11+ and the standard library. `organize.py` (unified CLI / doctor) and `pipeline.py` (ordered sweep) remain as the convenience layer on top.
- `tests/test_common.py` was dissolved: its suites now live next to the code they exercise (`test_10bit.py`, `test_movie_standardizer.py`, `test_subtitle_fetcher.py`, `test_reports.py`), including a rewritten "no tool keeps a divergent copy of the subtitle contract" suite that compares the vendored copies against each other.
- README rewritten as a single document: what the toolkit does, the file map, per-tool quickstarts, and how to adopt just one tool.

### Removed
- Docker support (`Dockerfile`, `docker-compose.yml`, `.dockerignore`) — the toolkit never needed a container to begin with.
- Windows launcher scripts (`organize.bat`, `organize.ps1`) and shell wrappers (`organize.sh`, `run_tests.sh`) — `python organize.py ...` is the one documented way to run everything.
- The `docs/` folder (architecture, configuration, FAQ, Docker and OS-specific guides) — the README is now the only documentation.
- `requirements.txt` / `requirements-dev.txt` — there are no runtime dependencies; `pytest` moved to the `dev` extra in `pyproject.toml`.

### Compatibility note
- If you import `common` directly from an external script, update it to import the helpers from a tool instead (e.g. `from library_auditor import Report`); every tool exports the shared helpers it vendors. All documented CLI behaviour is unchanged.

## [3.2.0]

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
