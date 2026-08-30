# Configuration & CLI Reference

<div align="center">

[← Documentation index](../README.md#-documentation)

</div>

This document is an exhaustive reference of all configuration options, command-line flags, environment variables, and exit codes for every tool in the `organize` suite.

---

## 1. Unified CLI: `organize.py`

```bash
python organize.py [COMMAND] [OPTIONS]
```

### Commands

| Command | Aliases | Description |
| :--- | :--- | :--- |
| `doctor` | `check` | Run environment, binary, API key, and hardlink compatibility diagnostics |
| `run` | `pipeline` | Run the automated maintenance pipeline (`pipeline.py`) |
| `standardize` | `std` | Organize downloads into `Title (Year)/Title (Year).mkv` (`movie_standardizer.py`) |
| `subtitles` | `subs` | Fetch English human UTF-8 SRT sidecars: OpenSubtitles hash-first, SubDL fallback (`subtitle_fetcher.py`) |
| `clean` | `remux` | Clean tracks via lossless remux (`mkv_track_cleaner.py`) |
| `10bit` | `probe` | Check 8-bit vs 10-bit & native HDR compliance (`10bit.py`) |
| `audit` | — | Read-only layout and subtitle health audit (`library_auditor.py`) |
| `test` | `tests` | Run the complete test suite across all tools |

---

## 2. Pipeline Runner: `pipeline.py`

```bash
python pipeline.py [OPTIONS]
```

### Flags

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--source PATH` | `E:\torrents\final_organized` | Path to the organized movie library root |
| `--steps STEPS` | `fetcher,cleaner,10bit,auditor` | Comma-separated subset of steps to run |
| `--dry-run` | `False` | Preview commands without executing |
| `--limit N` | `0` (all) | Limit number of items processed by supporting steps |
| `--nice` | `False` | Lower remux process priority so Jellyfin streaming is never starved |
| `--continue-on-error` | `True` (default) | Continue remaining steps even if one step exits non-zero |
| `--stop-on-error` | `False` | Stop pipeline on first step failure |
| `--list-steps` | — | Display readiness status of all steps and exit |
| `--self-test` | — | Run pipeline offline self-tests and exit |

---

## 3. Movie Standardizer: `movie_standardizer.py`

```bash
python movie_standardizer.py [PATH] [OPTIONS]
```

### Flags

| Flag | Default | Environment Variable | Description |
| :--- | :--- | :--- | :--- |
| `PATH` | None (batch mode) | — | Positional path passed by qBittorrent (`"%F"` or `"%D" "%N"`) |
| `--source PATH` | `E:\torrents\final` | `MOVIE_STD_SOURCE` | Source directory for batch scans |
| `--target PATH` | `E:\torrents\final_organized` | `MOVIE_STD_TARGET` | Organized movie library root |
| `--min-size MB` | `300` | `MOVIE_STD_MIN_SIZE` | Minimum file size to consider a movie feature |
| `--lock-timeout S` | `60.0` | `MOVIE_STD_LOCK_TIMEOUT` | Advisory coordination lock timeout in seconds |
| `--log PATH` | `E:\torrents\tools\ReportsAndLogs\movie_standardizer\movie_standardizer.log` | `MOVIE_STD_LOG` | Execution log file |
| `--report PATH` | `E:\torrents\tools\ReportsAndLogs\movie_standardizer\movie_standardizer_report.txt` | `MOVIE_STD_REPORT` | Plain text output report |
| `--manifest PATH` | None | `MOVIE_STD_MANIFEST` | Optional JSON run manifest for automation |
| `--ffprobe PATH` | `ffprobe` | `MOVIE_STD_FFPROBE` | Custom path to `ffprobe` binary |
| `--allow-tv` | `False` | — | Allow movies whose title triggers TV pattern matching |
| `--category CAT` | None | — | Torrent category from qBittorrent (skips if TV) |
| `--dry-run` | `False` | `MOVIE_STD_DRY_RUN` | Preview organization without hardlinking |
| `--deduplicate` | `False` | `MOVIE_STD_DEDUPLICATE` | Scan organized library for duplicate movie folders |
| `--maintenance-mode` | `REPORT` | `MOVIE_STD_MAINTENANCE_MODE` | Maintenance action: `REPORT`, `QUARANTINE`, or `DELETE` |
| `--quarantine-dir` | None | `MOVIE_STD_QUARANTINE` | Destination directory for quarantined items |

---

## 4. Subtitle Fetcher: `subtitle_fetcher.py`

```bash
python subtitle_fetcher.py [OPTIONS]
```

### Flags

| Flag | Default | Environment Variable | Description |
| :--- | :--- | :--- | :--- |
| `--source PATH` | `E:\torrents\final_organized` | — | Organized movie library root |
| `--log PATH` | `E:\torrents\tools\ReportsAndLogs\subtitle_fetcher\subtitle_fetcher.log` | — | Execution log & durable quota ledger |
| `--report PATH` | `E:\torrents\tools\ReportsAndLogs\subtitle_fetcher\subtitle_fetcher_report.txt` | — | Replaceable summary report |
| `--auth-mode MODE` | `development-anonymous` | — | `development-anonymous` (API key only) or `user` |
| `--daily-cap N` | `100` (anon) / `20` (user) | — | Maximum **OpenSubtitles** download requests per UTC day |
| `--subdl-daily-cap N` | `50` | — | Maximum **SubDL** download requests per UTC day; set a higher plan-specific value explicitly |
| `--min-size MB` | `300` | — | Minimum file size in MB |
| `--lock-timeout S`| `60.0` | — | Coordination lock timeout |
| `--limit N` | `0` (all) | — | Maximum movies to process in this run |
| `--dry-run` | `False` | — | Query API for candidates without downloading |
| `--no-identity-fallback` | `False` | — | Disable conservative title/year fallback after hash miss |
| `--retry-review` | `False` | — | Reconsider items previously held for manual review |

### Providers and Environment Variables

At least one provider key is required. When both are set, OpenSubtitles is
always queried first for an exact moviehash match; SubDL runs only after no safe
OpenSubtitles hash/title-year candidate is available. SubDL can be used by
itself, but it is necessarily limited to the same conservative title/year
matching policy.

- `OPENSUBTITLES_API_KEY`: OpenSubtitles Consumer API Key (recommended for exact release matching).
- `OPENSUBTITLES_USERNAME`: Username only when `OPENSUBTITLES_API_KEY` is set and using `--auth-mode user`.
- `OPENSUBTITLES_PASSWORD`: Password only when `OPENSUBTITLES_API_KEY` is set and using `--auth-mode user`.
- `SUBDL_API_KEY`: SubDL API key. Read only from the environment; get one at <https://subdl.com/panel/api>.

The log ledger records reservations separately for both providers. `--daily-cap`
continues to control OpenSubtitles; `--subdl-daily-cap` controls SubDL.

---

## 5. MKV Track Cleaner: `mkv_track_cleaner.py`

```bash
python mkv_track_cleaner.py [OPTIONS]
```

### Flags

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--dir PATH` | `E:\torrents\final_organized` | Organized movie library root |
| `--only PATH` | None | Restrict cleaning to a specific MKV file (repeatable) |
| `--mkvmerge PATH` | `mkvmerge` | Path to `mkvmerge` binary |
| `--log PATH` | `E:\torrents\tools\ReportsAndLogs\mkv_track_cleaner\mkv_track_cleaner.log` | Execution log file |
| `--report PATH` | `E:\torrents\tools\ReportsAndLogs\mkv_track_cleaner\mkv_track_cleaner_report.txt` | Replaceable summary report |
| `--cache PATH` | `E:\torrents\tools\ReportsAndLogs\mkv_track_cleaner\mkv_track_cleaner_probe_cache.json` | Reusable JSON probe cache |
| `--no-cache` | `False` | Bypass probe cache and probe every movie |
| `--nice` | `False` | Lower remux process priority |
| `--min-size MB` | `0` | Minimum file size to process |
| `--limit N` | `0` (all) | Limit number of files remuxed |
| `--dry-run` | `False` | Analyze tracks without remuxing |

---

## 6. 10-Bit & HDR Inspector: `10bit.py`

```bash
python 10bit.py [OPTIONS]
```

### Flags

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--source PATH` | `E:\torrents\final_organized` | Organized movie library root |
| `--ffprobe PATH` | `ffprobe` | Path to `ffprobe` binary |
| `--workers N` | `8` | Concurrent ffprobe inspection worker threads |
| `--timeout S` | `45.0` | ffprobe execution timeout per movie |
| `--min-size MB` | `0` | Minimum file size to process |
| `--log PATH` | `E:\torrents\tools\ReportsAndLogs\10bit\10bit.log` | Execution log file |
| `--report PATH` | `E:\torrents\tools\ReportsAndLogs\10bit\10bit_report.txt` | Replaceable action queue report |
| `--cache PATH` | `E:\torrents\tools\ReportsAndLogs\10bit\10bit_probe_cache.json` | Reusable ffprobe metadata cache |
| `--no-cache` | `False` | Bypass probe cache and re-probe all files |
| `--fail-if-queue` | `False` | Exit non-zero (3) if 8-bit SDR movies are queued |
| `--fail-if-review`| `False` | Exit non-zero (4) if movies require metadata review |
| `--fail-if-error` | `False` | Exit non-zero (5) if any files failed probing |
| `--dry-run` | `False` | List discovered MKVs without probing |

---

## 7. Library Auditor: `library_auditor.py`

```bash
python library_auditor.py [OPTIONS]
```

### Flags

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--source PATH` | `E:\torrents\final_organized` | Organized movie library root |
| `--log PATH` | `E:\torrents\tools\ReportsAndLogs\library_auditor\library_auditor.log` | Execution log file |
| `--report PATH` | `E:\torrents\tools\ReportsAndLogs\library_auditor\library_auditor_report.txt` | Replaceable audit report |
| `--lock-timeout S`| `60.0` | Coordination lock timeout |
| `--fail-on-defects`| `False` | Exit 1 on layout defects (stem mismatch, invalid SRT, etc.) |
| `--fail-on-findings`| `False` | Exit 1 on any non-canonical state (including missing SRT) |

---

## 8. Common Exit Codes

| Code | Meaning |
| :--- | :--- |
| `0` | Success / All operations completed cleanly |
| `1` | General error / Defects encountered under `--fail-on-*` flags |
| `2` | Configuration error (missing directory, cross-device error, missing required flag) |
| `3` | Coordination lock unavailable / Timed out waiting for other process |
| `130` | Interrupted by user (SIGINT / Ctrl+C) |

---

<div align="center">

[← Documentation index](../README.md#-documentation) · [Architecture & safety →](ARCHITECTURE_SAFETY.md) · [FAQ & troubleshooting →](FAQ_TROUBLESHOOTING.md)

</div>
