<div align="center">

# Organize

**A rock-solid, dependency-free set of Python tools that turns finished
torrents into a perfectly organized, 100% Direct Play Jellyfin &amp; Plex
movie library — with zero duplicate disk usage, exact-match subtitles, and
lossless track cleanup.**

[![CI](https://github.com/smeltzzz/organize/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/smeltzzz/organize/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Zero runtime dependencies](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-2EA44F.svg?style=flat-square)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-274%20passing%20(offline)-2EA44F.svg?style=flat-square)](.github/workflows/ci.yml)
[![Jellyfin & Plex](https://img.shields.io/badge/jellyfin%20%7C%20plex-compatible-00A4DC.svg?style=flat-square)](https://jellyfin.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-4B5563.svg?style=flat-square)](LICENSE)

[Quickstart](#-quickstart) ·
[What's in this repo](#-whats-in-this-repo) ·
[Use only the tools you need](#-use-only-the-tools-you-need) ·
[The five tools](#-the-five-tools) ·
[The pipeline](#-the-pipeline) ·
[Safety invariants](#-safety-invariants) ·
[Testing & development](#-testing--development)

</div>

---

## 🧭 What is Organize?

Five purpose-built Python 3.11+ tools that maintain a canonical movie library
for Jellyfin / Plex:

```
Title (Year)/
├── Title (Year).mkv        ← one losslessly-cleaned MKV per movie
└── Title (Year).eng.srt     ← one validated English subtitle (hash-matched when available)
```

Every tool is **100% standard-library Python**: no pip installs, no venv, no
containers, no daemons. The only things some tools need are the usual media
binaries (`mkvmerge`, `ffprobe`) and, for subtitle fetching, a free API key.

| | |
| :--- | :--- |
| 🫧 **Zero pip installs** | A tool is a single file. Copy it, run it, done. |
| 🔗 **Hardlink-only ingest** | Organized movies share disk sectors with your seeds — **0 extra bytes**, seeding never interrupted. |
| 💬 **Exact-match subtitles** | OpenSubtitles moviehash matching while container bytes are still pristine, then SubDL's release-aware filename match (score ≥ 0.80 only) with a strict title/year fallback. |
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
| `subtitle_fetcher.py` | Tool 1 — fetch validated English `.eng.srt` sidecars (OpenSubtitles + SubDL). |
| `mkv_track_cleaner.py` | Tool 2 — lossless remux: keep one best audio, strip commentary/dubs/embedded subs. |
| `10bit.py` | Tool 3 — ffprobe sweep: queue 8-bit SDR for HandBrake, protect HDR. |
| `library_auditor.py` | Tool 4 — read-only health check of layout, naming, and subtitles. |
| `movie_standardizer.py` | Tool 5 — the torrent-completion hook: parse scene names, hardlink into `Title (Year)/`. |
| `pipeline.py` | Runs the four maintenance tools (1→4) in the one correct order. |
| `tests/` | Fully offline unit tests (274) + per-tool built-in self-tests. |
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
python3 organize.py run               # subtitles -> remux -> 10-bit -> audit
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
| `subtitle_fetcher.py` | — | `OPENSUBTITLES_API_KEY` and/or `SUBDL_API_KEY` |
| `mkv_track_cleaner.py` | `mkvmerge` (MKVToolNix) | — |
| `10bit.py` | `ffprobe` (FFmpeg) | — |
| `library_auditor.py` | — | — |
| `movie_standardizer.py` | `ffprobe` (optional, for duplicate upgrades) | — |

If you ever modify a vendored helper in one tool, keep the copies in the
other tools byte-identical — the test suite compares them against each other
and fails if they drift.

---

## 🧰 The five tools

### 1 · `subtitle_fetcher.py` — validated English subtitles

Fetches one external `.eng.srt` per movie. OpenSubtitles exact-release
moviehash matching runs first (while the MKV bytes are still pristine); SubDL
enters only when no hash-safe result exists, and its automatic picks require a
match score ≥ 0.80. Every download is re-validated (regular file, size cap,
decodable text, at least one well-formed cue) before it is written.

```bash
python3 subtitle_fetcher.py --source /path/to/movies --dry-run   # preview
python3 subtitle_fetcher.py --source /path/to/movies --limit 10  # first 10
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

Four maintenance tools, one fixed order. The order between **subtitles** and
**remux** is load-bearing — a remux rewrites the container bytes that
OpenSubtitles hashes, so fetching subtitles *after* cleaning permanently
destroys the exact-match search. `pipeline.py` exists so you cannot get this
wrong.

```
 torrent finishes
        │
        ▼
┌───────────────────────┐   hardlink into Title (Year)/Title (Year).mkv
│ 1 · standardize       │   parse scene names, skip TV / discs / splits
└───────────┬───────────┘
            ▼
┌───────────────────────┐   exact OSHash match (moviehash_match=only),
│ 2 · subtitles         │   OpenSubtitles hash first, score-gated SubDL fallback
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
┌───────────────────────┐   100% read-only layout + subtitle health check
│ 5 · audit             │   gating exit codes for cron / Task Scheduler
└───────────────────────┘
```

`1 · standardize` fires automatically from the qBittorrent hook; `organize.py
run` (or `pipeline.py`) executes steps 2 → 5 in order. Every step skips
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

---

## ⚙️ Configuration

Everything is overridable per run with CLI flags (see each tool's
`--help`). Environment variables (with defaults and annotations) live in
[`.env.example`](.env.example). In short:

| Variable | Used by | Purpose |
| :--- | :--- | :--- |
| `OPENSUBTITLES_API_KEY` | subtitle_fetcher | Exact-moviehash subtitle source (recommended) |
| `SUBDL_API_KEY` | subtitle_fetcher | Optional score-gated fallback provider |
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
python3 -m unittest discover -s tests -p "test_*.py"   # 274 unit tests
pip install -e .[dev] && pytest                   # same suite under pytest
```

Every tool also carries its own `--self-test`, so a single copied file can
verify itself anywhere: `python3 library_auditor.py --self-test`.

Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: see
[SECURITY.md](SECURITY.md).

---

## 📄 License

MIT — see [LICENSE](LICENSE).
