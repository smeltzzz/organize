<div align="center">

# Organize

**A rock-solid, dependency-free set of Python tools that turns finished
torrents into a perfectly organized, 100% Direct Play Jellyfin &amp; Plex
movie library — with zero duplicate disk usage, exact-match subtitles, and
lossless track cleanup.**

[![CI](https://github.com/smeltzzz/organize/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/smeltzzz/organize/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Zero runtime dependencies](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-2EA44F.svg?style=flat-square)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-356%20passing%20(offline)-2EA44F.svg?style=flat-square)](.github/workflows/ci.yml)
[![Jellyfin & Plex](https://img.shields.io/badge/jellyfin%20%7C%20plex-compatible-00A4DC.svg?style=flat-square)](https://jellyfin.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-4B5563.svg?style=flat-square)](LICENSE)

[Quickstart](#-quickstart) ·
[What's in this repo](#-whats-in-this-repo) ·
[Use only the tools you need](#-use-only-the-tools-you-need) ·
[The six tools](#-the-six-tools) ·
[The pipeline](#-the-pipeline) ·
[Safety invariants](#-safety-invariants) ·
[Testing & development](#-testing--development)

</div>

---

## 🧭 What is Organize?

Six purpose-built Python 3.11+ tools that maintain a canonical movie
library for Jellyfin / Plex:

```
Title (Year)/
├── Title (Year).mkv        ← one losslessly-cleaned MKV per movie
└── Title (Year).eng.srt     ← one validated English subtitle (hash-matched when available)
```

Every tool is **100% standard-library Python**: no pip installs, no venv, no
containers, no daemons. The only things some tools need are the usual media
binaries (`mkvmerge`, `ffprobe`, `ffsubsync`) and, for subtitle fetching, a
free API key.

| | |
| :--- | :--- |
| 🫧 **Zero pip installs** | A tool is a single file. Copy it, run it, done. |
| 🔗 **Hardlink-only ingest** | Organized movies share disk sectors with your seeds — **0 extra bytes**, seeding never interrupted. |
| 💬 **Exact-match subtitles** | OpenSubtitles and SubDL are equal sources: both providers' release-identifying routes are consulted (SubDL's release match needs score ≥ 0.80) and the qualifying release with the most downloads wins, with a strict title/year fallback. |
| ✂ **Lossless track cleanup** | `mkvmerge` remux keeps the single best English audio track (or best non-commentary audio on foreign films with a validated `.eng.srt`) and drops commentary, dubs, and embedded bitmap subtitles — video untouched. |
| 🎨 **Bit-depth intelligence** | A fail-closed inspector queues 8-bit SDR for HandBrake while strictly protecting native HDR10 / HDR10+ / Dolby Vision. |
| 🩺 **Read-only health checks** | A 100% read-only auditor validates layout and subtitle integrity with scheduler-friendly exit codes. |
| 🛡 **Safety invariants** | Advisory locks, atomic staging, and crash recovery — engineered so a power cut can never corrupt your library. |

---

## 📁 What's in this repo

One file, one purpose. Nothing else.

| File | What it is |
| :--- | :--- |
| `organize.py` | **The front door.** Unified CLI, system doctor, and test runner: `organize.py doctor`, `organize.py run`, `organize.py test`, plus one subcommand per tool. |
| `subtitle_fetcher.py` | Tool 1 — fetch validated English `.eng.srt` sidecars (OpenSubtitles + SubDL + 7 scraping fallbacks). |
| `mkv_track_cleaner.py` | Tool 2 — lossless remux: keep one best audio, strip commentary/dubs/embedded subs. |
| `10bit.py` | Tool 3 — ffprobe sweep: queue 8-bit SDR for HandBrake, protect HDR. |
| `library_auditor.py` | Tool 4 — read-only health check of layout, naming, and subtitles. |
| `movie_standardizer.py` | Tool 5 — the torrent-completion hook: parse scene names, hardlink into `Title (Year)/`. |
| `sync_subtitles.py` | Tool 6 — ffsubsync timing sync of every `.srt` sidecar against its movie; the pipeline's last content step. |
| `pipeline.py` | Runs the five maintenance tools (1→5) in the one correct order. |
| `tests/` | Fully offline unit tests (356) + per-tool built-in self-tests. |
| `.env.example` | Every supported environment variable, annotated. |
| `pyproject.toml` | Packaging metadata; `pip install -e .[dev]` gives you `pytest`. |

**How the files relate** (this is the whole architecture):

- Each tool is **self-contained**. The small amount of shared plumbing (report
  rendering, file locking, subtitle validation) is copied into every tool that
  needs it, in a clearly marked `Shared helpers (vendored inline)` section at
  the top of the file. That is deliberate: it is what lets you copy *one* file
  into your own setup and run it.
- `organize.py` never reimplements anything — it launches the tool scripts as
  subprocesses.
- `pipeline.py` does the same, but hard-codes the safe execution order.

---

## 🚀 Quickstart

### 1 · Check your machine

```bash
git clone https://github.com/smeltzzz/organize.git
cd organize
python3 organize.py doctor
```

`doctor` verifies Python, `mkvmerge` (MKVToolNix), `ffprobe` (FFmpeg), your
OpenSubtitles and/or SubDL key, and — crucially — that your download folder
and library sit on the **same filesystem** so hardlinks work. Missing pieces
are reported with the exact fix, never a crash.

### 2 · Run the maintenance pipeline

```bash
python3 organize.py run --dry-run     # preview every command first
python3 organize.py run               # subtitles -> remux -> 10-bit -> sync -> audit
python3 organize.py run --nice        # low priority: Jellyfin streaming is never starved
```

### 3 · Point Jellyfin at the organized folder

Done — every movie is canonically named, subtitle-complete, and direct-play
safe. For the fully automatic flow (torrent finishes → standardized →
pipeline on a schedule), see [qBittorrent ingestion](#-qbittorrent-ingestion)
and [the pipeline](#-the-pipeline).

> [!TIP]
> Set `OPENSUBTITLES_API_KEY` (free, <https://www.opensubtitles.com/en/consumers>)
> and optionally `SUBDL_API_KEY` (<https://subdl.com/panel/api>) in your
> environment or a `.env` file. See `.env.example` for every variable.

---

## 🧩 Use only the tools you need

You do **not** have to adopt the whole toolkit. Every tool is a single
standalone file with zero imports from this repository:

```bash
# All you need subtitles:
cp subtitle_fetcher.py /path/to/your/media-tools/
python3 /path/to/media-tools/subtitle_fetcher.py --source /path/to/movies

# All you need is a library health check:
python3 library_auditor.py --source /path/to/movies
```

Prerequisites per tool:

| Tool | External binary | API key |
| :--- | :--- | :--- |
| `subtitle_fetcher.py` | — | optional — the scraping sources work with no keys at all |
| `mkv_track_cleaner.py` | `mkvmerge` (MKVToolNix) | — |
| `10bit.py` | `ffprobe` (FFmpeg) | — |
| `library_auditor.py` | — | — |
| `movie_standardizer.py` | `ffprobe` (optional, for duplicate upgrades) | — |
| `sync_subtitles.py` | `ffsubsync` (`pip install ffsubsync`) + `ffmpeg` (FFmpeg) | — |

If you ever modify a vendored helper in one tool, keep the copies in the
other tools byte-identical — the test suite compares them against each other
and fails if they drift.

---

## 🧰 The six tools

### 1 · `subtitle_fetcher.py` — validated English subtitles

Fetches one external `.eng.srt` per movie, and the goal is a subtitle beside
**every** movie. Nine sources are consulted in tiers:

1. **OpenSubtitles + SubDL as equal sources** (API keys, `subtitle_fetcher.py`
   itself): both providers' release-identifying routes are consulted for
   every movie — the exact OpenSubtitles moviehash (while the MKV bytes are
   still pristine) and SubDL's release-aware filename match, whose automatic
   picks require a score ≥ 0.80 — and the qualifying release with the
   **most downloads** is fetched, whichever provider it came from. When
   neither release route yields a pick, both providers' strict title/year
   routes are pooled the same way.
2. **Seven scraping fallbacks** (vendored in `subtitle_fetcher.py`,
   no keys, no accounts), offered in failover order to any movie the API
   tiers miss:
   **Subf2m.co → Podnapisi.NET → Addic7ed.com → SubSource.net →
   Subsunacs.net → YIFY Subtitles → Subs.sab.bz**. A scraped candidate wins
   only when it names the movie, matches its release year, and decodes to a
   valid English SRT. Each source has a per-run circuit breaker (3 hard or 3
   parse failures disable it for the rest of the run) and a UTC daily search
   cap (20 by source, `--scrape-daily-cap`), metered in the same durable
   ledger as the API quotas.

Every download — API or scraped — is re-validated (regular file, size cap,
decodable text, at least one well-formed cue) before it is written. The
report opens with a coverage scorecard (`17/18 (94.4%) · goal: 100%`) and
names every uncovered movie with the verdict of **each** source; uncovered
movies are re-offered to the scraping sources on every later UTC day. Until
every movie is covered the process exits **1** (override with
`--allow-missing`), so a gap is always loud. Works with zero API keys: with
none configured, every movie goes straight to the scraping tier.

```bash
python3 subtitle_fetcher.py --source /path/to/movies --dry-run   # preview
python3 subtitle_fetcher.py --source /path/to/movies --limit 10  # first 10
python3 subtitle_fetcher.py --source /path/to/movies --skip-source subf2me   # drop one site
```

### 2 · `mkv_track_cleaner.py` — lossless remux

Keeps the single best English audio track (or, for foreign films with a
validated `.eng.srt`, the best non-commentary audio of any language) and
strips commentary, dubs, and embedded subtitles. Video is never re-encoded.
Movies still hardlinked to their torrent source are always deferred.

```bash
python3 mkv_track_cleaner.py --dir /path/to/movies --dry-run
python3 mkv_track_cleaner.py --dir /path/to/movies --nice --only "Some Movie (2020).mkv"
```

### 3 · `10bit.py` — bit-depth & HDR inspector

Probes every movie with ffprobe and classifies it: 8-bit SDR goes into a
HandBrake queue, native HDR10 / HDR10+ / Dolby Vision is protected, ambiguous
metadata is flagged for review. Nothing is ever re-encoded by this tool — it
only tells you what is worth re-encoding.

```bash
python3 10bit.py --source /path/to/movies
python3 10bit.py --source /path/to/movies --fail-if-queue   # for schedulers
```

### 4 · `library_auditor.py` — read-only health check

Validates the `Title (Year)/Title (Year).mkv` + `.eng.srt` layout, flags
foreign artifacts, misnamed sidecars, and missing subtitles. Strictly
read-only; exit codes are designed for cron / Task Scheduler gating.

```bash
python3 library_auditor.py --source /path/to/movies --fail-on-findings
```

### 5 · `movie_standardizer.py` — the ingest hook

Parses scene release names and places one canonical hardlinked MKV (plus any
validated subtitle) per `Title (Year)/` folder. Hardlink-only: the download
folder keeps seeding, the library uses 0 extra bytes. Skips TV, disc rips,
and splits. Also finds duplicate folders of the same movie on request
(`--deduplicate`, non-destructive by default).

```bash
python3 movie_standardizer.py --source /path/to/downloads --target /path/to/movies --dry-run
```

### 6 · `sync_subtitles.py` — subtitle-timing sync (ffsubsync)

The pipeline's final content step, right before the audit. Walks the whole
library, pairs every `.srt` sidecar with its movie, and measures the drift
against the actual audio with [`ffsubsync`](https://github.com/smacke/ffsubsync)
(install once: `pip install ffsubsync`; it needs `ffmpeg` on the PATH).
Trustworthy drift is applied by atomically swapping in the corrected sidecar;
sub-threshold drift (`--min-offset`, default 0.1 s) leaves the file
byte-identical; anything untrustworthy — beyond the trust window
(`--max-offset`, default 30 s), anti-correlated scores, ffsubsync's own
quality-gate refusal, or a plain failure — is **held for review, never
applied**. Movie bytes are never touched, so the OpenSubtitles moviehash is
undisturbed and the audit that follows sees the finished sidecars.

```bash
python3 sync_subtitles.py --source /path/to/movies --dry-run    # preview
python3 sync_subtitles.py --source /path/to/movies --limit 10   # first 10
python3 sync_subtitles.py --source /path/to/movies --fail-on-review  # cron gating
```

---

## 🔗 qBittorrent ingestion

In qBittorrent → **Options → Downloads → Run external program on torrent
completion**, enter:

```bash
# Windows (cmd / PowerShell)
py "C:\Tools\organize\organize.py" standardize "%F"

# Linux / macOS
/opt/organize/organize.py standardize "%F"
```

> [!IMPORTANT]
> In **Options → BitTorrent → Seeding Limits**, set *When ratio reaches* /
> *When seeding time reaches* to **Remove torrent and its content**. The
> organized movie is a hardlink, so deleting the download entry leaves your
> library file 100% intact while dropping the link count — which is exactly
> what unblocks the track cleaner on its next sweep.

Then schedule the pipeline (cron, systemd timer, Task Scheduler, or whatever
runs on your box):

```bash
python3 /opt/organize/pipeline.py --source /path/to/movies
```

---

## 🔄 The pipeline

Five maintenance tools, one fixed order. The order between **subtitles** and
**remux** is load-bearing — a remux rewrites the container bytes that
OpenSubtitles hashes, so fetching subtitles *after* cleaning permanently
destroys the exact-match search. Subtitle **sync** runs last of the content
steps on purpose: it rewrites subtitle bytes only (never movie bytes), so the
moviehash is undisturbed — but it must finish before the audit so the audit
validates the finished sidecars. `pipeline.py` exists so you cannot get this
wrong.

```
 torrent finishes
        │
        ▼
┌───────────────────────┐   hardlink into Title (Year)/Title (Year).mkv
│ 1 · standardize       │   parse scene names, skip TV / discs / splits
└───────────┬───────────┘
            ▼
┌───────────────────────┐   OpenSubtitles moviehash + SubDL release match
│ 2 · subtitles         │   as equal sources; most downloads wins
└───────────┬───────────┘
            ▼
┌───────────────────────┐   lossless mkvmerge remux: 1 best audio,
│ 3 · clean             │   strip commentary / dubs / embedded subs
└───────────┬───────────┘
            ▼
┌───────────────────────┐   ffprobe sweep: QUEUE 8-bit SDR, KEEP native HDR,
│ 4 · 10bit             │   REVIEW ambiguous metadata — never guess
└───────────┬───────────┘
            ▼
┌───────────────────────┐   ffsubsync timing sync of every .srt sidecar;
│ 5 · sync              │   bad syncs held for review, originals never lost
└───────────┬───────────┘
            ▼
┌───────────────────────┐   100% read-only layout + subtitle health check
│ 6 · audit             │   gating exit codes for cron / Task Scheduler
└───────────────────────┘
```

`1 · standardize` fires automatically from the qBittorrent hook; `organize.py
run` (or `pipeline.py`) executes steps 2 → 6 in order. Every step skips
cleanly (with the reason printed) when its prerequisite is missing.

```bash
python3 pipeline.py --source /path/to/movies --list-steps   # what's ready, what's blocked
python3 pipeline.py --source /path/to/movies --steps cleaner,auditor
```

---

## 📄 Reading the reports

Every tool writes exactly one replaceable plain-text report, plus an
append-only log, to `E:\torrents\tools\ReportsAndLogs\<tool>\` on Windows
(defaults are documented in each tool's `--help`; override with
`--report` / `--log`). All reports share one layout: a boxed header, a
right-aligned scorecard, then titled sections ordered by how cheap the fix
is. Start at the scorecard; it tells you what needs your attention.

---

## 🔒 Safety invariants

Non-negotiable rules every tool obeys:

1. **Hardlink-only ingestion** — `movie_standardizer.py` calls `os.link()`
   exclusively. No copy, no move, no symlink, no cross-device fallback. Your
   seeds keep seeding on the same bytes.
2. **Subtitles before remuxing** — a remux permanently rewrites the
   OpenSubtitles moviehash. The pipeline enforces the order; the cleaner
   warns per-file when it must remux without a sidecar.
3. **Seeding movies are inviolable** — link count > 1 means *deferred,
   unconditionally*. No override flag exists.
4. **Fail-closed concurrency** — all tools coordinate through advisory locks
   keyed by a SHA-256 of the normalized library path. Lock contention halts a
   tool; it never races.
5. **Atomic staging everywhere** — reports, manifests, subtitles, probe
   caches, and remuxed MKVs are written to unique sibling temporaries and
   swapped with `os.replace`. A crash or power cut never leaves a
   half-written movie.
6. **Unique data is never deleted** — declines are reported, duplicates
   default to `REPORT` mode, and destructive maintenance modes
   (`QUARANTINE`, `DELETE`) are strictly opt-in.
7. **A bad subtitle sync is worse than none** — `sync_subtitles.py` applies a
   drift only when it is measurable and inside the trust window; anything
   untrustworthy (huge offsets, anti-correlated scores, ffsubsync's own
   quality-gate refusal, or a plain failure) is held for review with the
   original sidecar byte-identical.

---

## ⚙️ Configuration

Everything is overridable per run with CLI flags (see each tool's
`--help`). Environment variables (with defaults and annotations) live in
[`.env.example`](.env.example). In short:

| Variable | Used by | Purpose |
| :--- | :--- | :--- |
| `OPENSUBTITLES_API_KEY` | subtitle_fetcher | Subtitle source (exact-moviehash matching) |
| `SUBDL_API_KEY` | subtitle_fetcher | Equal subtitle source (release match scored ≥ 0.80) |
| `MOVIE_STD_SOURCE` / `MOVIE_STD_TARGET` | movie_standardizer, pipeline | Download / library roots |
| `MOVIE_STD_LOCK_TIMEOUT` | movie_standardizer | Coordination-lock wait (default 60 s) |
| `MOVIE_STD_MAINTENANCE_MODE` | movie_standardizer | `REPORT` (default) / `QUARANTINE` / `DELETE` for duplicates |

Defaults assume the documented Windows layout (`E:\torrents\...`); on
Linux/macOS pass `--source` / `--target` or set the variables. Source and
target **must be on the same filesystem** (hardlink-only ingest).

---

## 🧪 Testing & development

The whole suite is **offline**: no media files, no `mkvmerge`, no `ffprobe`,
no API keys, no network.

```bash
python3 organize.py test                          # built-in self-tests (one per script)
python3 -m unittest discover -s tests -p "test_*.py"   # 356 unit tests
pip install -e .[dev] && pytest                   # same suite under pytest
```

Every tool also carries its own `--self-test`, so a single copied file can
verify itself anywhere: `python3 library_auditor.py --self-test`.

Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: see
[SECURITY.md](SECURITY.md).

---

## 📄 License

MIT — see [LICENSE](LICENSE).
