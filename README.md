<div align="center">

# Organize

**A rock-solid, dependency-free set of Python tools that turns finished
torrents into a perfectly organized, 100% Direct Play Jellyfin &amp; Plex
movie library — with zero duplicate disk usage, exact-match subtitles, and
lossless track cleanup.**

[![CI](https://github.com/smeltzzz/organize/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/smeltzzz/organize/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Zero runtime dependencies](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-2EA44F.svg?style=flat-square)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-934%20passing%20(offline)-2EA44F.svg?style=flat-square)](.github/workflows/ci.yml)
[![Jellyfin & Plex](https://img.shields.io/badge/jellyfin%20%7C%20plex-compatible-00A4DC.svg?style=flat-square)](https://jellyfin.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-4B5563.svg?style=flat-square)](LICENSE)

[Quickstart](#-quickstart) ·
[What's in this repo](#-whats-in-this-repo) ·
[The tools](#-the-tools) ·
[One file, no install](#-one-file-no-install) ·
[Safety invariants](#-safety-invariants) ·
[Documentation](#-documentation)

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
- **No Docker, no daemon, no database you have to keep.** Nine files, the
  standard library, and binaries you already have. Every run is stateless,
  idempotent, and safe to Ctrl-C at any point. There is a SQLite *cache* of
  what each tool last decided (stdlib `sqlite3`, so still zero dependencies),
  but every tool re-derives its verdict from the live filesystem: delete
  `state.db` and you lose one fast summary, never correctness.

---

## 📁 What's in this repo

One file, one purpose. Nothing else.

| File | What it is |
| :--- | :--- |
| `organize.py` | **The front door.** Unified CLI, system doctor, progress summary, and test runner: `organize.py doctor`, `organize.py status`, `organize.py run`, `organize.py test`, plus one subcommand per tool. |
| `subtitle_fetcher.py` | Tool 1 — one validated English `.eng.srt` per movie: extract the movie's own embedded track first, else OpenSubtitles + SubDL + 7 scraping fallbacks. |
| `mkv_track_cleaner.py` | Tool 2 — lossless remux: keep one best audio, strip commentary/dubs/embedded subs. |
| `bitdepth.py` | Tool 3 — ffprobe sweep: queue 8-bit SDR for HandBrake, protect HDR. |
| `library_auditor.py` | Tool 4 — read-only health check of layout, naming, and subtitles. |
| `movie_standardizer.py` | Tool 5 — the torrent-completion hook: parse scene names, hardlink into `Title (Year)/`. |
| `sync_subtitles.py` | Tool 6 — ffsubsync timing sync of every `.srt` sidecar against its movie; the pipeline's last content step. Sidecars extracted from the movie itself are skipped (they are already frame-accurate). |
| `pipeline.py` | Runs the maintenance tools in the one correct order. |
| `jellyfin_one_shot.py` | **The "never stop" completer** — runs the whole toolchain pass after pass until the auditor reports 100% canonical, with UTC-rollover pacing, retry, and guaranteed-finish edge-case handling. |
| `organizekit/` | The shared core, defined exactly once: report rendering, atomic + durable writes, cross-platform locking, the subtitle contract, probe caching, library-root resolution, `toolchain.py` — the one table describing what the five steps are and how to call them — `state.py`, the rebuildable SQLite cache of what each tool last decided, and `ratelimit.py`, the per-host token buckets that keep every provider inside its published rate. `runlog.py` is the run log itself — one timestamped line to the console and the log file, written under one lock. |
| `tests/` | Fully offline unit tests (934), including `tests/selftests/` — each tool's own suite, moved out of the shipped file — and `fake_mkvmerge.py`, a stand-in multiplexer real enough to drive an end-to-end remux. |
| `docs/` | The long-form documentation this page links to: the [tool reference](docs/tools.md), [the pipeline](docs/pipeline.md), [configuration](docs/configuration.md) and [testing & development](docs/development.md). |
| `benchmarks/` | The scripts behind every speed claim in this repo — stdlib-only, offline, re-runnable. |
| `.env.example` | Every supported environment variable, annotated. |
| `pyproject.toml` | Packaging metadata; `pip install -e .[dev]` gives you `pytest`. It is also the single source for what the single-file build ships. |
| `scripts/build_pyz.py` | Builds `dist/organize.pyz` — the entire toolkit as one stdlib-only file you can copy to a NAS. |
| `__main__.py` | The archive's entry point: the CLI, plus the hidden `run-tool` verb it uses to start its own tools as child processes. |

**How the files relate** (this is the whole architecture):

- **One shared core, imported — never copied.** Everything more than one tool
  needs (report rendering, atomic writes, locking, the subtitle contract,
  library-root resolution) lives exactly once in `organizekit/core/`. The tools
  import it. Until recently each tool carried its own copy of all of it: 4,325
  lines of literal duplication that had already drifted — `atomic_write_text`
  existed in a durable `fsync`ing version *and* a weaker one, and the tool that
  rewrites your movie files had the weaker one. A test
  (`tests/test_shared_core.py`) now fails the build if a tool redefines
  anything the core already provides. The last copy to go was the run log:
  four tools had written the same twenty lines and three had quietly diverged
  on whether an unencodable character in a filename should end the run.
- **The tools are still plain scripts.** `python3 bitdepth.py` out of a clone
  needs no install, no PYTHONPATH and no virtualenv — the package sits beside
  them at the repository root.
- `organize.py` never reimplements anything — it launches the tool scripts as
  subprocesses.
- `pipeline.py` does the same, but hard-codes the safe execution order.
- **One toolkit, two deployments.** Everything above also builds into a single
  `organize.pyz` you can copy to a NAS that has nothing on it but Python. The
  archive runs the *same* modules and still starts each step as its own
  process — inside it there is no `bitdepth.py` to point an interpreter at, so
  it re-enters itself (`python organize.pyz run-tool bitdepth.py …`). That is
  the only difference between the two, and it is stated once, in
  `organizekit/core/toolchain.py`.

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

### 4 · Ask what is left

```bash
python3 organize.py status                        # one screen: done vs. remaining
python3 organize.py status --library /path/to/movies
```

`status` re-scans layout and subtitles live (they are cheap, and they are the
two things you can change by moving a file), then joins the expensive verdicts
— bit depth, sync, remux — from the shared state cache each tool writes as it
runs. A cached verdict is shown **only while it still describes the bytes on
disk**: replace a movie and its old verdict is reported as `stale`, never as an
answer. `--no-state` ignores the cache entirely and shows just the live half.

```console
Library   /srv/media/Movies
          412 movie(s), 3.1 TiB
Layout    408 CANONICAL_MKV   4 MISSING_SIDECAR
Subtitles 408 present   4 missing
Remux     not recorded yet
Bit depth 388 SKIP_HDR   21 QUEUE_FOR_HANDBRAKE   3 stale
Sync      401 synced   1 review   10 unmeasured

Nothing to do for 388 movie(s) - the next pass will touch 24.
```

### 5 · Point Jellyfin at the organized folder

Done — every movie is canonically named, subtitle-complete, and direct-play
safe. For the fully automatic flow (torrent finishes → standardized →
pipeline on a schedule), see [The pipeline](docs/pipeline.md) — the
qBittorrent hook, the step order, and how to read the reports.

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
provides. That includes the toolchain itself: which binary a step needs, and
the reason printed when it is missing, come from `organizekit/core/toolchain.py`,
so `pipeline.py`, `jellyfin_one_shot.py` and `organize.py doctor` cannot
disagree about whether this machine is provisioned.

---

## 🧰 The tools

Six tools do the work; two orchestrators run them in the one correct order.
Each is a single file with no imports from this repository, so you can adopt
one and ignore the rest. **[Full reference → `docs/tools.md`](docs/tools.md)**

| Tool | What it does | Needs |
| :--- | :--- | :--- |
| [`subtitle_fetcher.py`](docs/tools.md#1--subtitle_fetcherpy--validated-english-subtitles) | One validated English `.eng.srt` per movie: the movie's **own embedded track** first (exact, free, already in sync), then OpenSubtitles + SubDL + seven scraping fallbacks. Parallel local triage, strictly serial spending. | nothing; API keys optional |
| [`mkv_track_cleaner.py`](docs/tools.md#2--mkv_track_cleanerpy--lossless-remux) | Lossless remux: keep the one best audio track, strip commentary, dubs and embedded subtitles. Video untouched; seeding movies deferred. | `mkvmerge` |
| [`bitdepth.py`](docs/tools.md#3--bitdepthpy--bit-depth--hdr-inspector) | Queue 8-bit SDR for HandBrake, protect native HDR10 / HDR10+ / Dolby Vision fail-closed, flag anything ambiguous for review. | `ffprobe` |
| [`library_auditor.py`](docs/tools.md#4--library_auditorpy--read-only-health-check) | Strictly read-only health check of layout, naming and subtitles, with gating exit codes for cron. | nothing |
| [`movie_standardizer.py`](docs/tools.md#5--movie_standardizerpy--the-ingest-hook) | The torrent-completion hook: parse scene names and hardlink one canonical MKV per `Title (Year)/`. Zero extra bytes. | `ffprobe` (optional) |
| [`sync_subtitles.py`](docs/tools.md#6--sync_subtitlespy--subtitle-timing-sync-ffsubsync) | Measure every sidecar against the actual audio and apply only trustworthy drift; anything doubtful is held for review, never applied. | `ffsubsync` + `ffmpeg` |
| [`pipeline.py`](docs/pipeline.md) | The five maintenance steps in the one safe order. | — |
| [`jellyfin_one_shot.py`](docs/tools.md#7--jellyfin_one_shotpy--the-never-stop-completer) | Loops the whole toolchain, pass after pass, until the auditor reports 100% canonical — quota-aware, resumable, safe to leave running for days. | whatever its steps need |

---

## 📦 One file, no install

For the machine this toolkit is actually for — a NAS or a home server with
Python and nothing else — build the whole thing into one file and copy it
across:

```bash
python3 scripts/build_pyz.py          # writes dist/organize.pyz (~270 KiB)
scp dist/organize.pyz nas:/volume1/
ssh nas 'cd /volume1 && python3 organize.pyz doctor'
```

It is the same toolkit, not a cut-down one: `organize.pyz test` runs all nine
field smoke tests, `organize.pyz run-tool pipeline.py --source …` runs the full
five-step pass, and each step is still its own process with its own locks, log,
report and exit code. Logs and reports land *beside* the archive, never inside
it. [How it is built and tested →](docs/development.md#one-file-no-install)

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

## 📚 Documentation

| Document | What's in it |
| :--- | :--- |
| [Tool reference](docs/tools.md) | Every tool in detail — what it decides, why, and the flags worth knowing. |
| [The pipeline](docs/pipeline.md) | The qBittorrent hook, the five steps, the order that is load-bearing, and how to read the reports. |
| [Configuration](docs/configuration.md) | Environment variables, the `.env` file, platform-aware path defaults. |
| [Testing & development](docs/development.md) | The offline suite, the `organize.pyz` build, the field smoke tests, the crash tests. |
| [CHANGELOG](CHANGELOG.md) · [OVERHAUL](OVERHAUL.md) | What changed and why; the measured plan the recent work follows. |

Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: see
[SECURITY.md](SECURITY.md).

---

## 📄 License

MIT — see [LICENSE](LICENSE).
