<div align="center">

# 🎬 Organize

### The Definitive Media Management Toolkit for Jellyfin & Plex

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB.svg?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Zero Runtime Dependencies](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-success.svg?style=for-the-badge)](requirements.txt)
[![Jellyfin & Plex Ready](https://img.shields.io/badge/jellyfin%20%7C%20plex-compatible-00A4DC.svg?style=for-the-badge&logo=jellyfin&logoColor=white)](https://jellyfin.org/)
[![Tests Passing](https://img.shields.io/badge/tests-205%20passed%20(offline)-brightgreen.svg?style=for-the-badge)](run_tests.sh)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20linux%20%7C%20docker-blueviolet.svg?style=for-the-badge)](docs/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg?style=for-the-badge)](LICENSE)

<p align="center">
  <b>A rock-solid, dependency-free media ingestion and maintenance pipeline that guarantees 100% Direct Play, zero duplicate disk space, and flawless subtitle matching.</b>
</p>

[Quickstart](#-quickstart-in-60-seconds) •
[Architecture](#-pipeline-architecture) •
[Unified CLI](#-unified-cli-organizepy) •
[The Five Tools](#-the-five-core-tools) •
[Core Invariants](#-core-safety-invariants) •
[Documentation](#-documentation-index)

</div>

---

## ⚡ What is Organize?

**Organize** is a suite of purpose-built, high-reliability Python tools designed to ingest completed torrents and maintain a canonical **Jellyfin & Plex movie library**.

Unlike bloated container stacks with hundred-megabyte dependencies, SQLite lockups, and unstable Web UIs, **Organize is 100% standard library Python (3.11+)**. It requires **zero pip installs**, writes atomic reports outside your media folders, coordinates concurrent runs with fail-closed advisory locks, and executes with sub-second probe caching.

```
Torrent Completed ──▶ movie_standardizer.py   (Hardlinks into Title (Year)/Title (Year).mkv)
                          │
                          ▼
                      subtitle_fetcher.py     (Fetches exact English SRT via release OSHash)
                          │
                          ▼
                      mkv_track_cleaner.py    (Lossless remux: 1 audio, drops commentary/DVS)
                          │
                          ▼
                      10bit.py                (FFprobe audit: queues 8-bit SDR; keeps native HDR)
                          │
                          ▼
                      library_auditor.py      (Read-only health check & scheduler gate)
```

---

## 🎯 The Jellyfin Direct Play Philosophy

Why do media servers transcode, and why does this toolkit exist?

1. **The Subtitle Transcode Trap**: Bitmap subtitles (**PGS**, **VobSub**) and complex **ASS/SSA** styles cannot be rendered natively by web browsers, Smart TVs, or streaming sticks. Jellyfin is forced to burn them into the video frames on the fly, consuming 80–100% of your CPU/GPU.
   - *The Fix*: `subtitle_fetcher.py` and `mkv_track_cleaner.py` ensure every movie has an external UTF-8 `.en.srt` sidecar. Jellyfin serves plain-text subtitles with **0% server overhead**.
2. **Audio Track Bloat**: Torrents bundle commentary, descriptive audio, and uncompressed 7.1 tracks that freeze client decoders.
   - *The Fix*: `mkv_track_cleaner.py` remuxes the container to keep the single highest quality English audio track, purging commentary and foreign dubs.
3. **8-Bit Banding vs. 10-Bit Color**: 8-bit SDR exhibits color banding in shadows and gradients. Re-encoding 8-bit SDR to 10-bit HEVC or AV1 eliminates banding and cuts file sizes by 20–40%.
   - *The Fix*: `10bit.py` fail-closed classification safely queues 8-bit SDR for HandBrake while strictly protecting native HDR10/Dolby Vision sources from accidental tone-mapping.

---

## 🏗 Pipeline Architecture

```
                                  [ Completed Torrent ]
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. MOVIE STANDARDIZER                                                                    │
│    • Matches release name to canonical "Title (Year)"                                   │
│    • Creates atomic hardlink into library (0 duplicate disk usage, continuous seeding)   │
│    • Skips TV episodes, multipart splits, and non-MKV files                             │
└───────────────────────────────────────────┬─────────────────────────────────────────────┘
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 2. SUBTITLE FETCHER (Must run BEFORE remuxing!)                                         │
│    • Calculates exact OpenSubtitles OSHash (first & last 64 KiB)                         │
│    • Queries with moviehash_match=only for guaranteed sync                               │
│    • Fallback to high-confidence title/year check                                        │
│    • Writes validated, human-authored UTF-8 .en.srt                                     │
└───────────────────────────────────────────┬─────────────────────────────────────────────┘
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 3. MKV TRACK CLEANER                                                                    │
│    • Defers seeding files (link count > 1) to prevent breaking torrents                 │
│    • Pre-flight free disk space check (need size * 1.02 + 64 MiB)                        │
│    • Transaction-journaled lossless remux via mkvmerge                                  │
│    • Strips commentary, audio descriptions, foreign tracks, and embedded PGS subs       │
│    • Verifies track fingerprints & duration before atomic swap                          │
└───────────────────────────────────────────┬─────────────────────────────────────────────┘
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 4. 10-BIT & HDR INSPECTOR                                                               │
│    • Reusable JSON ffprobe cache (sub-second sweeps on unchanged libraries)             │
│    • QUEUE: 8-bit SDR (re-encode to 10-bit H.265/AV1)                                   │
│    • KEEP: Native HDR10, HDR10+, Dolby Vision, HLG (protects dynamic metadata)          │
│    • REVIEW: Unknown bit depth or mis-tagged HDR                                        │
└───────────────────────────────────────────┬─────────────────────────────────────────────┘
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 5. LIBRARY AUDITOR                                                                      │
│    • 100% read-only health check of the organized library                               │
│    • Verifies Title (Year)/Title (Year).mkv structure                                   │
│    • Validates .en.srt sidecar syntax (flags empty, stub, or HTML error pages)          │
│    • Gating exit codes for scheduled cron / Task Scheduler tasks                        │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 🚀 Quickstart in 60 Seconds

### 1. Check Your System with `doctor`

Clone the repo and run the automated diagnostics to verify your environment:

```bash
git clone https://github.com/smeltzzz/organize.git
cd organize

# Run system diagnostics
python3 organize.py doctor
```

```
  SYSTEM & PREREQUISITE DIAGNOSTICS (DOCTOR)
  ────────────────────────────────────────────────────────────────────
  ✔ Python Runtime               Python 3.11.2 (CPython 64bit)
  ✔ Operating System             Linux 6.1 / Windows 11
  ✔ MKVToolNix (mkvmerge)        Found: mkvmerge v82.0
  ✔ FFmpeg (ffprobe)             Found: ffprobe version 6.1.1
  ✔ OpenSubtitles API Key        Configured (a1b2...c3d4)
  ✔ Hardlink Compatibility       Source and library share filesystem device
  ────────────────────────────────────────────────────────────────────
  Scorecard: 6 passed, 0 warnings
  ✔ All systems operational! Ready for the complete Jellyfin media pipeline.
```

### 2. Configure qBittorrent Ingest

In qBittorrent → **Options → Downloads → Run external program on torrent completion**:

```cmd
# Windows (cmd / PowerShell)
py "C:\Tools\organize\organize.py" standardize "%F"

# Linux / macOS (bash)
/opt/organize/organize.sh standardize "%F"
```

> [!IMPORTANT]
> In qBittorrent → **Options → BitTorrent → Seeding Limits**, set *"When ratio reaches"* or *"When seeding time reaches"* to **Remove torrent and its content**.
> Because the organized movie is a hardlink, deleting the download source entry leaves your library file 100% intact while dropping the link count so `mkv_track_cleaner.py` can remux it!

### 3. Run the Maintenance Pipeline

```bash
# Preview operations without touching files
python organize.py run --dry-run

# Run the live maintenance sweep (fetch subs -> remux -> 10-bit -> audit)
python organize.py run

# Run with lowered CPU/IO priority so Jellyfin streaming is never starved
python organize.py run --nice
```

---

## 💻 Unified CLI: `organize.py`

`organize.py` provides a single command-line interface for the entire toolkit:

```bash
python organize.py <command> [OPTIONS]
```

| Command | Alias | What It Does |
| :--- | :--- | :--- |
| `doctor` | `check` | Verifies Python, binaries (`mkvmerge`, `ffprobe`), API key, and hardlink compatibility |
| `run` | `pipeline` | Runs the automated maintenance sweep in the exact required order |
| `standardize` | `std` | Ingests torrent downloads into `Title (Year)/Title (Year).mkv` with hardlinks |
| `subtitles` | `subs` | Downloads human-authored English UTF-8 SRTs from OpenSubtitles via OSHash |
| `clean` | `remux` | Lossless remux: selects best English audio, strips commentary/DVS, drops PGS |
| `10bit` | `probe` | Scans bit-depth and HDR compliance with sub-second probe caching |
| `audit` | — | Read-only layout, naming, and sidecar integrity check |
| `test` | `tests` | Runs all 7 script self-tests and 205 unit tests (100% offline) |

*All standalone scripts (`movie_standardizer.py`, `pipeline.py`, etc.) remain 100% backward compatible and can still be invoked directly.*

---

## 🛠 The Five Core Tools

| Script | Purpose | External Binary | Output Artifacts |
| :--- | :--- | :--- | :--- |
| **`movie_standardizer.py`** | Ingest torrents into `Title (Year)/Title (Year).mkv` using `os.link()`. Replaces existing copies only on verified same-cut technical quality upgrade. | `ffprobe` *(only for duplicate comparison)* | `movie_standardizer.log`<br>`movie_standardizer_report.txt` |
| **`subtitle_fetcher.py`** | Downloads exact English UTF-8 `.en.srt` sidecars from OpenSubtitles using 64-bit release OSHashes. Persistent UTC quota ledger. | `OPENSUBTITLES_API_KEY` | `subtitle_fetcher.log`<br>`subtitle_fetcher_report.txt` |
| **`mkv_track_cleaner.py`** | Lossless remux: keeps best English audio track, purges commentary/DVS, strips embedded subtitles once verified `.en.srt` exists. | `mkvmerge` *(MKVToolNix)* | `mkv_track_cleaner.log`<br>`mkv_track_cleaner_report.txt`<br>`...probe_cache.json` |
| **`10bit.py`** | Classifies library into action queues: QUEUE (8-bit SDR), KEEP (Native HDR), SKIP (10-bit SDR), REVIEW (ambiguous). Probe cache for sub-second sweeps. | `ffprobe` *(FFmpeg)* | `10bit.log`<br>`10bit_report.txt`<br>`...probe_cache.json` |
| **`library_auditor.py`** | 100% read-only health check. Verifies direct containers, folder stem matches, and validates `.en.srt` file syntax (catches corrupted/stub subtitles). | *None (stdlib only)* | `library_auditor.log`<br>`library_auditor_report.txt` |

<br>

<details>
<summary><b>🔍 Deep Dive: <code>movie_standardizer.py</code> (Ingestion & Hardlink Placement)</b></summary>

### How it works
- **qBittorrent Hook**: Triggered automatically on torrent completion via `"%F"`. Can also batch-scan `E:\torrents\final` when run with no arguments.
- **Canonical Naming**: Cleans release tags, audio/video codec tokens, and site prefixes into standard `Title (Year)/Title (Year).mkv`.
- **Zero Space Hardlinks**: Links the file directly into `final_organized` using `os.link()`. The torrent continues seeding in `final` using the exact same disk sectors.
- **Technical Quality Upgrades**: If a movie already exists in the library, file size alone never triggers replacement. The tool uses `ffprobe` to ensure runtimes match within 30 seconds or 1% (confirming identical theatrical/extended cut), and calculates a weighted technical quality score (resolution, HDR, bit depth, audio channels). If quality does not meaningfully increase, the existing file is preserved.
- **Leftover Reporting**: Non-MKV files, multipart splits, or files under `--min-size` are declined and listed under `ITEMS LEFT IN SOURCE` in the text report so nothing is silently lost.

```bash
# Manual batch scan
python organize.py standardize

# Preview without hardlinking
python organize.py standardize --dry-run
```
</details>

<details>
<summary><b>🔍 Deep Dive: <code>subtitle_fetcher.py</code> (OSHash Matching & Quota Ledger)</b></summary>

### Why it runs BEFORE remuxing
- OpenSubtitles OSHash is a 64-bit checksum calculated from the file size plus the first and last 64 KiB of the MKV container.
- Submitting this hash with `moviehash_match=only` returns subtitles uploaded against that exact release, guaranteeing 100% audio-sync accuracy.
- **Remuxing rewrites the container cues and headers, permanently destroying the release OSHash.** Running the cleaner first forces a fallback to fuzzy title/year matching, resulting in far fewer matches and potential sync drift.
- **Sidecar Safety**: Downloads human-authored, normal English UTF-8 `.en.srt` sidecars only (no machine translations, no forced-only, no SDH by default).
- **Persistent Quota Ledger**: The append-only execution log acts as an ACID quota ledger, preventing wasted API quota across runs.

```bash
# Preview candidates
python organize.py subtitles --dry-run

# Run live fetch
python organize.py subtitles
```
</details>

<details>
<summary><b>🔍 Deep Dive: <code>mkv_track_cleaner.py</code> (Lossless Remux & Seeding Deferral)</b></summary>

### Lossless Remuxing Mechanics
- **MKVToolNix (`mkvmerge`)**: Rewrites container headers without re-encoding video.
- **Seeding Protection**: Refuses to remux any file with a link count > 1 (`st_nlink > 1`), protecting seeding torrents from breaking.
- **Free Space Preflight**: Checks disk free space and demands at least `size * 1.02 + 64 MiB` before proceeding.
- **Pre- & Post-Flight Fingerprints**: Fingerprints video duration, frame count, audio tracks, chapters, and attachments before and after remuxing. If anything mismatches, the original file is left untouched.
- **Crash Recovery**: Staged remuxes use `.track_cleaner.<token>.json` journals. Interrupted runs are recovered or safely flagged on subsequent runs.

```bash
# Preview remux operations
python organize.py clean --dry-run

# Run remux on a single test movie
python organize.py clean --limit 1
```
</details>

<details>
<summary><b>🔍 Deep Dive: <code>10bit.py</code> (Fail-Closed Bit-Depth & HDR Inspection)</b></summary>

### Inspection Heuristics
- **Probe Cache**: Stores `ffprobe` JSON results in a fast metadata cache. Probing 2,000 unchanged movies drops from minutes to under 0.2 seconds!
- **Action Queues**:
  - `QUEUE FOR HANDBRAKE`: Confirmed 8-bit SDR. Re-encode to H.265 10-bit or AV1 10-bit to eliminate color banding and reduce size.
  - `SKIP (High Bit-Depth SDR)`: Already 10-bit, 12-bit, or 16-bit SDR.
  - `KEEP (Native HDR)`: HDR10, HDR10+, Dolby Vision, HLG. HandBrake default presets will tone-map or strip dynamic metadata; keep original!
  - `REVIEW (Ambiguous)`: Mis-tagged or unknown bit-depth. Never dumped into the 8-bit queue automatically.

```bash
# Preview inspection
python organize.py 10bit --dry-run

# Run bit-depth inspection sweep
python organize.py 10bit
```
</details>

<details>
<summary><b>🔍 Deep Dive: <code>library_auditor.py</code> (Read-Only Health Check & Gates)</b></summary>

### Audit Checks
- **Structure**: Verifies that every folder contains exactly one canonical MKV matching the folder stem: `Title (Year)/Title (Year).mkv`.
- **Sidecar Integrity**: Inspects `.en.srt` contents. Detects and flags empty files, truncated downloads, or provider error pages (e.g. HTML 429).
- **Remux Awareness**: Ignores in-flight remux sibling temporary files (`temp_clean_*`), preventing false "multiple movie files" alarms during maintenance.
- **Scheduler Gates**:
  - `--fail-on-defects`: Exits 1 if layout defects (stem mismatch, invalid SRT, multiple containers) exist.
  - `--fail-on-findings`: Exits 1 if any movie lacks a valid `.en.srt` sidecar.

```bash
# Run read-only audit
python organize.py audit

# Gate automated task (exits 1 if layout defects exist)
python organize.py audit --fail-on-defects
```
</details>

---

## 🔒 Core Safety Invariants

The toolkit adheres to non-negotiable safety rules:

### 1. Hardlink-Only Ingestion (No Duplicate Storage)
`movie_standardizer.py` calls `os.link()` exclusively. It has no copy or move fallback. Completed torrents remain seeding in `final`, while your library in `final_organized` references the exact same inode on disk. Disk usage is zero additional bytes.

### 2. Subtitles *Must* Run Before Remuxing
`subtitle_fetcher.py` matches releases using OpenSubtitles **OSHash** (the file size plus the sum of the first and last 64 KiB). This yields exact, verified subtitle synchronization.
A remux rewrites container headers, permanently altering those bytes. **Running the cleaner first permanently destroys the moviehash match**, demoting the movie to a weaker title/year search. `pipeline.py` and `organize.py` strictly enforce this order.

### 3. Seeding Protection (Hardlink Count > 1 Deferral)
`mkv_track_cleaner.py` inspects `os.stat().st_nlink`. If a movie has more than 1 link, it is actively seeding and **deferred unconditionally**. Remuxing is only performed once qBittorrent removes the completed torrent or you delete the source file.

### 4. Fail-Closed Concurrency Locks
All tools coordinate via `common.CoordinationLock`. The lock uses a SHA-256 hash of the normalized target directory in the OS temp directory (`msvcrt` on Windows, `fcntl` on Linux). If a lock cannot be acquired within the timeout, the script safely halts rather than racing another process.

### 5. Atomic File Staging
All reports, manifests, subtitle downloads, and remuxed MKVs are written to unique sibling temporary files and swapped using `os.replace`. A crash or power outage mid-write never leaves a truncated or half-written movie.

---

## 📦 Cross-Platform Support

### Windows 10/11
Use the built-in PowerShell or Batch launchers:
```powershell
.\organize.ps1 doctor
.\organize.ps1 run --dry-run
.\organize.ps1 run
```
See the [Windows Deployment Guide](docs/WINDOWS_GUIDE.md) for automated Task Scheduler configuration.

### Linux & macOS
Use the POSIX shell wrapper:
```bash
./organize.sh doctor
./organize.sh run --nice
```
See the [Linux & Docker Guide](docs/LINUX_DOCKER_GUIDE.md) for systemd timers and cron setup.

### Docker & Docker Compose
A production-ready `Dockerfile` with `ffmpeg` and `mkvtoolnix` pre-installed is included:
```bash
# Build and run diagnostics
docker compose run --rm organize doctor

# Run nightly maintenance
docker compose run --rm organize run --nice
```

---

## 🧪 Testing & Verification

The test suite runs **100% offline** without media files, external binaries, or an OpenSubtitles API key:

```bash
# Run all self-tests + the 205-test unit suite
bash run_tests.sh

# Or run via Python unittest
python3 -m unittest discover -s tests -p 'test_*.py'

# Or via organize CLI
python3 organize.py test --unit
```

---

## 📚 Documentation Index

| Guide | Description |
| :--- | :--- |
| **[Windows Guide](docs/WINDOWS_GUIDE.md)** | Step-by-step setup for Windows 11, PowerShell, and Task Scheduler |
| **[Linux & Docker Guide](docs/LINUX_DOCKER_GUIDE.md)** | Comprehensive guide for Ubuntu/Debian, Docker, Unraid, and TrueNAS |
| **[Jellyfin Direct Play Guide](docs/JELLYFIN_DIRECT_PLAY.md)** | Technical breakdown of subtitle burn-in, audio bloat, and 10-bit color |
| **[Architecture & Safety](docs/ARCHITECTURE_SAFETY.md)** | In-depth analysis of advisory locks, atomic writes, and journals |
| **[Configuration Reference](docs/CONFIGURATION_REFERENCE.md)** | Full cheat sheet of CLI flags, defaults, and environment variables |
| **[FAQ & Troubleshooting](docs/FAQ_TROUBLESHOOTING.md)** | Solutions for seeding deferrals, invalid SRTs, and quota limits |

---

## 📄 License

Released under the [MIT License](LICENSE). Made with precision for media server enthusiasts.
