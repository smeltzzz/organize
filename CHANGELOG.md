# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- Canonical movie-and-English-subtitle contract (`Title (Year)/Title (Year).mkv` + `.en.srt`).
- OpenSubtitles moviehash prioritization in `subtitle_fetcher.py`.
- Shared external subtitle validation contract in `common.py`.

## [2.5.0]

### Added
- Reusable `MediaProbeCache` in `common.py` eliminating repetitive subprocess probes across runs.
- Lossless MKV remux verification with pre- and post-flight track fingerprinting in `mkv_track_cleaner.py`.
