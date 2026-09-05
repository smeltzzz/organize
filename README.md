<div align="center">

# Organize

**A rock-solid, dependency-free set of Python tools that turns finished
torrents into a perfectly organized, 100% Direct Play Jellyfin &amp; Plex
movie library — with zero duplicate disk usage, exact-match subtitles, and
lossless track cleanup.**

[![CI](https://github.com/smeltzzz/organize/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/smeltzzz/organize/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Zero runtime dependencies](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-2EA44F.svg?style=flat-square)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-556%20passing%20(offline)-2EA44F.svg?style=flat-square)](.github/workflows/ci.yml)
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

### Why this and not Bazarr / Tdarr / Radarr?

That stack is excellent and, for many people, the right answer. Choose this if
you want any of the following, which the usual containers do not give you:

- **Subtitles are fetched *before* the remux, on purpose.** OpenSubtitles
  matches on a hash of the file's bytes. A remux rewrites those bytes, so a
  library that transcodes first is permanently downgraded to fuzzy title/year
  matching. This ordering constraint is the reason `pipeline.py` exists, and
  it is enforced by a test, not just documented.
- **Seeding torrents are never touched.** A movie still hardlinked to its
  qBittorrent source is always deferred, with no override flag.
- **HDR is protected fail-closed.** Anything uncertain is never queued for
  re-encoding — the tool would rather do nothing than turn your Dolby Vision
  master into a green-and-purple mess.
- **No Docker, no daemon, no database.** Nine files, the standard library, and
  binaries you already have. Every run is stateless, idempotent, and safe to
  Ctrl-C at any point.
| 🛡 **Safety invariants** | Advisory locks, atomic staging, and crash recovery — engineered so a power cut can never corrupt your library. |

---

## 📁 What's in this repo

One file, one purpose. Nothing else.

| File | What it is |
| :--- | :--- |
| `organize.py` | **The front door.** Unified CLI, system doctor, and test runner: `organize.py doctor`, `organize.py run`, `organize.py test`, plus one subcommand per tool. |
| `subtitle_fetcher.py` | Tool 1 — one validated English `.eng.srt` per movie: extract the movie's own embedded track first, else OpenSubtitles + SubDL + 7 scraping fallbacks. |
| `mkv_track_cleaner.py` | Tool 2 — lossless remux: keep one best audio, strip commentary/dubs/embedded subs. |
| `bitdepth.py` | Tool 3 — ffprobe sweep: queue 8-bit SDR for HandBrake, protect HDR. |
| `library_auditor.py` | Tool 4 — read-only health check of layout, naming, and subtitles. |
| `movie_standardizer.py` | Tool 5 — the torrent-completion hook: parse scene names, hardlink into `Title (Year)/`. |
| `sync_subtitles.py` | Tool 6 — ffsubsync timing sync of every `.srt` sidecar against its movie; the pipeline's last content step. Sidecars extracted from the movie itself are skipped (they are already frame-accurate). |
| `pipeline.py` | Runs the maintenance tools in the one correct order. |
| `jellyfin_one_shot.py` | **The "never stop" completer** — runs the whole toolchain pass after pass until the auditor reports 100% canonical, with UTC-rollover pacing, retry, and guaranteed-finish edge-case handling. |
| `organizekit/` | The shared core, defined exactly once: report rendering, atomic + durable writes, cross-platform locking, the subtitle contract, probe caching, library-root resolution. |
| `tests/` | Fully offline unit tests (556), including `tests/selftests/` — each tool's own suite, moved out of the shipped file. |
| `.env.example` | Every supported environment variable, annotated. |
| `pyproject.toml` | Packaging metadata; `pip install -e .[dev]` gives you `pytest`. |

**How the files relate** (this is the whole architecture):

- **One shared core, imported — never copied.** Everything more than one tool
  needs (report rendering, atomic writes, locking, the subtitle contract,
  library-root resolution) lives exactly once in `organizekit/core/`. The tools
  import it. Until recently each tool carried its own copy of all of it: 4,325
  lines of literal duplication that had already drifted — `atomic_write_text`
  existed in a durable `fsync`ing version *and* a weaker one, and the tool that
  rewrites your movie files had the weaker one. A test
  (`tests/test_shared_core.py`) now fails the build if a tool redefines
  anything the core already provides.
- **The tools are still plain scripts.** `python3 bitdepth.py` out of a clone
  needs no install, no PYTHONPATH and no virtualenv — the package sits beside
  them at the repository root.
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

### 3 · Let the one-shot completer get the library to 100%

The one-shot completer runs the entire toolchain pass after pass — fetch
subtitles, clean tracks, inspect bit depth, sync timing, audit — until the
auditor reports **100% canonical**. It handles API quota exhaustion (waits
for the UTC day rollover), retries transient failures every pass, and paces
itself: two passes with no progress means it sleeps until the daily caps
reset instead of hot-looping. It fails fast with the exact fix when
misconfigured (empty library, log dir inside the library) instead of
looping, and every step is idempotent, so an interrupted run resumes where
it left off.

```bash
python3 jellyfin_one_shot.py                                     # default library
python3 jellyfin_one_shot.py --source /path/to/movies --dry-run  # one-pass preview
python3 jellyfin_one_shot.py --source /path/to/movies            # run to 100%
python3 organize.py one-shot --source /path/to/movies --nice     # same, lower priority
```

With no `--source` it uses the same library root every other tool defaults to
— `ORGANIZE_LIBRARY` if set, else the legacy `MOVIE_STD_TARGET`, else the
platform default (`E:\torrents\final_organized` on Windows, `~/Media/Movies`
elsewhere). An explicit `--source` always wins.

All logs, per-tool reports, and full output transcripts land in one place
(`--log-dir`, default `./logs`, which must be outside the library).

### 4 · Point Jellyfin at the organized folder

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
| `bitdepth.py` | `ffprobe` (FFmpeg) | — |
| `library_auditor.py` | — | — |
| `movie_standardizer.py` | `ffprobe` (optional, for duplicate upgrades) | — |
| `sync_subtitles.py` | `ffsubsync` (`pip install ffsubsync`) + `ffmpeg` (FFmpeg) | — |
| `jellyfin_one_shot.py` | whatever the tools it runs need (missing binaries are skipped, not fatal) | optional |

Shared behaviour belongs in `organizekit/core/` and is imported, not copied.
The test suite fails the build if a tool defines a helper the core already
provides.

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

**Before any of that, it looks inside the movie.** A Jellyfin MKV very often
already carries the English subtitle as an embedded track, and that track is
exact for this release: it costs no provider request, it cannot be the wrong
cut, and its cues come from the container's own timeline, so it needs no
timing correction. Text tracks (SRT/SSA/ASS/WebVTT) are extracted with
`mkvextract` and converted in-process; image tracks (PGS/SUP, VobSub, DVB) are
OCR'd by an external backend when one is installed. A track that is
forced/signs-only, commentary, non-English, or too short to be the whole film
is refused, and the movie falls through to the providers as before.

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
python3 subtitle_fetcher.py --source /path/to/movies --no-extract            # never use embedded tracks
python3 subtitle_fetcher.py --source /path/to/movies --ocr-limit 5           # OCR at most 5 movies per run
```

**Embedded extraction in detail.** It is on by default and always attempted
first; it is skipped only when it cannot help, and the report names the
sidecars that came from the movie itself. It needs
[MKVToolNix](https://mkvtoolnix.download/) (`mkvmerge` + `mkvextract`) for
every track type, plus one OCR backend for image-based (PGS/VobSub) tracks —
`pgsrip`, `sup2srt` + Tesseract, Subtitle Edit, or PgsToSrt, auto-detected
in that order:

```bash
python3 subtitle_fetcher.py --source /path/to/movies --ocr-backend auto     # default (pgsrip first)
python3 subtitle_fetcher.py --source /path/to/movies --ocr-backend pgsrip   # pip install pgsrip
python3 subtitle_fetcher.py --source /path/to/movies --ocr-backend none     # text tracks only
python3 subtitle_fetcher.py --source /path/to/movies --ocr-backend custom \
        --ocr-bin /opt/my-ocr --ocr-args "{input}" "{output}"                # your own tool
```

Extracted sidecars are recorded outside the library
(`ReportsAndLogs/subtitle_fetcher_extracted.json`), and `sync_subtitles.py`
reads that record: a subtitle taken from the movie's own container timeline is
already frame-accurate, so **it is never handed to ffsubsync**. Replace it with
a download and it is measured like any other sidecar again.
`mkv_track_cleaner.py` still strips every embedded subtitle afterwards, so the
external `.eng.srt` remains the sole subtitle option.

### 2 · `mkv_track_cleaner.py` — lossless remux

Keeps the single best English audio track (or, for foreign films with a
validated `.eng.srt`, the best non-commentary audio of any language) and
strips commentary, dubs, and embedded subtitles. Video is never re-encoded.
Movies still hardlinked to their torrent source are always deferred.

```bash
python3 mkv_track_cleaner.py --dir /path/to/movies --dry-run
python3 mkv_track_cleaner.py --dir /path/to/movies --nice --only "Some Movie (2020).mkv"
```

### 3 · `bitdepth.py` — bit-depth & HDR inspector

Probes every movie with ffprobe and classifies it: 8-bit SDR goes into a
HandBrake queue, native HDR10 / HDR10+ / Dolby Vision is protected, ambiguous
metadata is flagged for review. Nothing is ever re-encoded by this tool — it
only tells you what is worth re-encoding.

It reads the technical label each file declares about itself (bit depth,
transfer function, HDR metadata) rather than decoding the picture, so a file
whose labels are consistent but untrue reads as what it claims. What it never
does is guess: a conflicting label, a missing one, or an 8-bit file carrying
HDR metadata all land in REVIEW instead of a queue. Dolby Vision is reported
by profile — `profile 8.1 · HDR10 base` falls back to HDR10 on a client without
Dolby Vision, while `profile 5 · no SDR/HDR10 fallback` does not play correctly
on one.

```bash
python3 bitdepth.py --source /path/to/movies
python3 bitdepth.py --source /path/to/movies --fail-if-queue   # for schedulers
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
quality-gate refusal, or a plain failure — triggers another qualifying
subtitle download. The synchronizer tests up to **10 replacement downloads
per movie**, stopping as soon as one is already aligned or can be safely
corrected. If none works (or fetching stops), the entry-time sidecar is
restored byte-for-byte and **held for review, never applied**. Movie bytes are
never touched, so the OpenSubtitles moviehash is undisturbed and the audit that
follows sees the finished sidecars.

```bash
python3 sync_subtitles.py --source /path/to/movies --dry-run    # preview
python3 sync_subtitles.py --source /path/to/movies --limit 10   # first 10
python3 sync_subtitles.py --source /path/to/movies --fail-on-review  # cron gating
```

**Re-running costs nothing.** A sidecar measured "in sync", or corrected and
swapped in, is recorded outside the library (`sync_state.json`, override with
`--sync-ledger` or `SUBTITLE_SYNC_LEDGER`) with the subtitle's SHA-256 and the
movie's size and mtime. The record is honoured only while **both** still
match: re-download, re-extract, hand-edit or replace the subtitle, or remux
the movie, and it is measured again. Held-for-review and failed syncs are
never recorded — those still need another look. Delete the file to
re-measure everything.

### 7 · `jellyfin_one_shot.py` — the "never stop" completer

The orchestrator that gets a library to the end result no matter what: it
loops the pipeline (fetch → clean → 10bit → sync → audit) until the auditor
reports 100% canonical, then exits 0. What makes it safe to leave running for
days:

- **Quota-aware pacing** — a stuck library (all sources out of daily cap)
  sleeps until the next UTC midnight, when the caps reset and the scraping
  tier re-offers held movies, instead of burning full passes for nothing.
- **Guaranteed finish** — an empty library (no movie folders) exits 2 with
  the canonical-layout reminder; a log dir inside the library is rejected up
  front (the tools refuse to write inside the media tree, and the auditor
  would count a log folder as a movie folder); three passes in a row without
  a usable audit report exits 1 with the transcript paths to debug.
- **It narrates itself** — before the first pass it prints the plan (which
  steps will run, which are skipped and why), every step announces what it
  does and why it runs in that position, and each tool's output streams to
  the console as it happens, tagged with the step it came from
  (`[clean] remuxed Movie (2020).mkv`). A tool that goes quiet is not a tool
  that has died: every `--heartbeat` seconds (60 by default) the runner
  reports how long it has been running and how long since its last line.
  `--quiet` turns the streaming off and keeps the banners, decisions,
  heartbeats and summaries.
- **Two files, and only two** — a run writes one log and one report, both
  under `--log-dir`, both with a fixed name:
  `jellyfin_one_shot.log` (appended by every run and by all five tools, each
  run starting with a banner) and `jellyfin_one_shot_report.txt` (rewritten
  after *every step*, so it is always the current state of the run even
  while it is still going or if it is killed). The report holds the full
  detail of the current pass plus a one-line history of every pass before
  it, with every tool's own report folded in verbatim. Per-tool report files
  are staged in a hidden folder, folded in, and deleted — so there is never a
  pile of per-run artifacts to sift through.
- **Honest reporting** — if `mkvmerge`/`ffprobe`/`ffsubsync` are missing the
  affected steps are skipped and the completion banner says exactly which
  guarantees were not checked.
- **A finished library is left alone** — the auditor runs *first*, and a
  library that already reports 100% canonical exits 0 without a fetch, remux,
  inspection or sync sweep. Every step is idempotent, so re-running a finished
  library used to cost a full pass for nothing. Use `--force-pass` to sweep
  anyway: the auditor's verdict is the library contract (canonical folder
  layout plus a validated `.eng.srt` sidecar) and never inspects the MKV's own
  tracks, so a movie still carrying extra audio or embedded subtitles audits
  as canonical.

```bash
python3 jellyfin_one_shot.py                                          # default library
python3 jellyfin_one_shot.py --source /path/to/movies --dry-run    # one-pass preview
python3 jellyfin_one_shot.py --source /path/to/movies              # run until 100%
python3 jellyfin_one_shot.py --source /path/to/movies --max-passes 5   # bound a run
python3 jellyfin_one_shot.py --source /path/to/movies --force-pass     # sweep anyway
python3 jellyfin_one_shot.py --source /path/to/movies --quiet          # no live streaming
```

> [!NOTE]
> The two files are the only *artifacts*. A run also maintains durable state
> beside them, which is what makes the next run cheap and keeps the provider
> quotas honest: `subtitle_fetcher_ledger.log` (the fetcher's daily-quota
> ledger — that tool parses its own log back, so it cannot share a file with
> anything else), the two probe caches, and `sync_state.json`.

`--source` is the Jellyfin movie-library root. Every tool in the repo resolves
it through one shared resolver — `--source`, then `ORGANIZE_LIBRARY`, then the
legacy `MOVIE_STD_TARGET`, then the platform default — so `python3
jellyfin_one_shot.py` with no arguments finishes the same library the rest of
the toolchain maintains. The library it resolved, and where that value came
from, is written to the runtime log at the start of every run.

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
destroys the exact-match search. It also matters that extraction happens
*before* the remux: once the embedded tracks are stripped, a subtitle that was
already in the file can only be downloaded. Subtitle **sync** runs last of the content
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
┌───────────────────────┐   extract the movie's own embedded English track
│ 2 · subtitles         │   first (exact, free, in sync); else OpenSubtitles
└───────────┬───────────┘   moviehash + SubDL release match + 7 scrapers
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
| `ORGANIZE_LIBRARY` | **every tool** | The movie-library root. Set this one variable and no tool needs a path flag. |
| `MOVIE_STD_SOURCE` | movie_standardizer / `doctor` | Completed-download root to ingest from (platform default: `E:\torrents\final` on Windows, `~/torrents/final` elsewhere) |
| `MOVIE_STD_TARGET` | every tool (legacy) | Older name for `ORGANIZE_LIBRARY`; still honoured, lower precedence |
| `MOVIE_STD_LOCK_TIMEOUT` | movie_standardizer | Coordination-lock wait (default 60 s) |
| `MOVIE_STD_MAINTENANCE_MODE` | movie_standardizer | `REPORT` (default) / `QUARANTINE` / `DELETE` for duplicates |

A `.env` file next to the scripts is read automatically at startup by every
tool; anything already exported in the environment wins over the file.

Path defaults are platform-aware. On Windows they follow the documented
`E:\torrents\...` layout; on Linux/macOS the library defaults to
`~/Media/Movies`, the completed-download batch-scan root to `~/torrents/final`,
and logs, reports and probe caches to `$XDG_STATE_HOME/organize`
(`~/.local/state/organize`). Source and target **must be on the same
filesystem** (hardlink-only ingest). `organize.py doctor` resolves both roots
through the same rules, so it can never disagree with the tools about which
folder needs attention.

---

## 🧪 Testing & development

The whole suite is **offline**: no media files, no `mkvmerge`, no `ffprobe`,
no API keys, no network.

```bash
python3 organize.py test                          # built-in self-tests (one per script)
python3 -m unittest discover -s tests -p "test_*.py"   # 556 unit tests
pip install -e ".[dev]" && pytest                 # same suite under pytest
ruff check .                                      # lint (configured in pyproject.toml)
```

Installing the package also provides an `organize` console script, so the CLI
works from any directory:

```bash
pip install .
organize doctor
```

Every tool also carries a `--self-test` **field smoke test** — it answers "does
this copy work on this machine?" in under a second, without the repository, a
media library, or a network: `python3 library_auditor.py --self-test`. It
checks the shared report renderer, the atomic writer and the library-root
resolution, plus a few of that tool's own decisions (the auditor audits a
temporary library; `bitdepth` confirms 8-bit SDR is queued and Dolby Vision is
protected; the standardizer verifies this filesystem actually supports
hardlinks). The exhaustive suites those flags used to run now live in
`tests/selftests/`, where they are part of the offline unit run and count
towards coverage.

Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: see
[SECURITY.md](SECURITY.md).

---

## 📄 License

MIT — see [LICENSE](LICENSE).
