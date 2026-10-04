<div align="center">

# Organize

**A rock-solid, dependency-free set of Python tools that turns finished
torrents into a perfectly organized, 100% Direct Play Jellyfin &amp; Plex
movie library — tuned for one exact living-room chain (Chromecast with
Google&nbsp;TV **HD** G454V → Hisense AX3125H soundbar (HDMI IN) → Samsung
UN60F6350AF),
with zero duplicate disk usage, subtitles extracted from each
movie's own tracks, and
lossless track cleanup.**

[![CI](https://github.com/smeltzzz/organize/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/smeltzzz/organize/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Zero runtime dependencies](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-2EA44F.svg?style=flat-square)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-1874%20passing%20(offline)-2EA44F.svg?style=flat-square)](.github/workflows/ci.yml)
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
library for Jellyfin / Plex, with one promise: **every movie Direct Plays on
the reference playback chain** (Chromecast with Google TV HD `G454V` →
Hisense `AX3125H` soundbar (HDMI IN) → Samsung `UN60F6350AF`) with
zero work from the server. Every codec default — which audio the cleaner
keeps, what the audio standardizer bakes in, what the bit-depth inspector
flags — derives from that chain's measured capabilities; the dossier with
every spec and source is [docs/hardware.md](docs/hardware.md), and
`organize.py doctor` prints the chain it is assuming:

```
Title (Year)/
├── Title (Year).mkv        ← one losslessly-cleaned MKV per movie
└── Title (Year).eng.srt     ← one validated English subtitle (from the movie's own tracks)
```

Every tool is **100% standard-library Python**: no pip installs, no venv, no
containers, no daemons. Subtitles come from the movie's own tracks first —
text-based embedded English subtitles are extracted locally, and the only
networked step is a deliberate one: a movie whose English subtitles exist
solely as bitmaps (PGS/VobSub) is looked up on OpenSubtitles by its **exact
file hash**, never by title. Without an API key the toolkit never touches the
network at all. The only things some tools need are the usual media binaries
(`mkvmerge` + `mkvextract`, `ffprobe`).

| | |
| :--- | :--- |
| 🫧 **Zero pip installs** | A tool is a single file. Copy it, run it, done. |
| 🔗 **Hardlink-only ingest** | Organized movies share disk sectors with your seeds — **0 extra bytes**, seeding never interrupted. |
| 💬 **Subtitles from the movie itself, first** | Each movie's own **text-based** embedded English track is extracted to a validated `.eng.srt` — via `mkvextract`, MP4s through a temporary MKV bridge. Image-based subtitles are never OCR'd. A movie whose English subtitles exist *only* as bitmaps gets one OpenSubtitles lookup keyed on the file's **exact moviehash**; there is no title search. An existing sidecar is always authoritative and never rewritten. |
| ✂ **Lossless track cleanup** | `mkvmerge` remux keeps exactly one audio track — the best one *that actually plays on the chain* in the movie's own (native) language: chain-native Dolby Digital/Digital Plus beats lossless TrueHD/DTS-HD, because the G454V can never emit those. Drops every dub and commentary track, and removes every embedded subtitle (the `.eng.srt` beside the movie is the only subtitle). Video untouched. |
| 🎛 **Chain-native audio, baked in once** | `audio_standardizer.py` scans with `ffprobe` and — for any movie whose best track the G454V can never emit (TrueHD, DTS-HD, DTS:X) — synthesizes one Dolby Digital Plus 5.1 track @ 640 kbps from the lossless master with `ffmpeg` — 5.1 because that is the widest layout ffmpeg's Dolby encoders can write; wider sources fold, they are never promised a bitstream that would fail (video and subtitles copied untouched, verified by a second probe, published atomically). One offline pass replaces a server transcode on every future play. |
| 🎨 **Bit-depth & chain-fit intelligence** | A fail-closed inspector queues 8-bit SDR for HandBrake while strictly protecting native HDR10 / HDR10+ / Dolby Vision — and tells you what each file does on this chain: tone-mapped Direct Play vs. "replaces or re-encodes" (Dolby Vision, >1080p). |
| 🩺 **Read-only health checks** | A 100% read-only auditor validates layout and subtitle integrity with scheduler-friendly exit codes. |
| 🛡 **Safety invariants** | Advisory locks, atomic staging, and crash recovery — engineered so a power cut can never corrupt your library. |

### Why this and not Bazarr / Tdarr / Radarr?

That stack is excellent and, for many people, the right answer. Choose this if
you want any of the following, which the usual containers do not give you:

- **Subtitles are extracted *before* the remux, on purpose.** Once a validated
  sidecar exists, the cleaner strips every embedded subtitle track — so
  extraction must happen while the track is still inside the container. The
  pipeline enforces this order, and a test enforces the pipeline. (Subtitle
  downloading is deliberately narrow: it runs only for movies whose English
  subtitles are image-based, and only against the file's exact moviehash — a
  title or release-name search was removed because it could not be made
  reliable. A movie with no usable track and no hash match is reported for a
  human decision instead of guessed at.)
- **Seeding torrents are never touched.** A movie still hardlinked to its
  qBittorrent source is always deferred, with no override flag.
- **HDR is protected fail-closed.** Anything uncertain is never queued for
  re-encoding — the tool would rather do nothing than turn your Dolby Vision
  master into a green-and-purple mess.
- **No Docker, no daemon, no database you have to keep.** Eight files, the
  standard library, and binaries you already have. Every run is stateless,
  idempotent, and safe to Ctrl-C at any point. There is a SQLite *cache* of
  what each tool last decided and of the `ffprobe`/`mkvmerge` output for files
  that have not changed (stdlib `sqlite3`, so still zero dependencies), but
  every tool re-derives its verdict from the live filesystem: delete
  `state.db` and you lose one fast summary and one slow pass, never
  correctness.

---

## 📁 What's in this repo

One file, one purpose. Nothing else.

| File | What it is |
| :--- | :--- |
| `organize.py` | **The front door.** Unified CLI, system doctor, progress summary, and test runner: `organize.py doctor`, `organize.py status`, `organize.py run`, `organize.py test`, plus one subcommand per tool. |
| `subtitle_extractor.py` | Tool 1 — one validated English `.eng.srt` per movie: the movie's own **text** track via `mkvextract` (MP4 through a temporary MKV bridge), or an exact-moviehash OpenSubtitles match when only bitmap subtitles exist. An existing sidecar is authoritative. |
| `audio_standardizer.py` | Tool 2 — **chain-native audio**: `ffprobe` sweep; bakes one Dolby Digital Plus track @ 640 kbps in from any TrueHD/DTS-HD/DTS:X master the G454V can never emit, via `ffmpeg` (video untouched, output verified, atomic swap). |
| `mkv_track_cleaner.py` | Tool 3 — lossless remux: keep the one best **chain-playable** audio (native Dolby over lossless-HD), strip commentary/dubs/embedded subs. |
| `bitdepth.py` | Tool 4 — ffprobe sweep: queue 8-bit SDR for HandBrake, protect HDR, say what each file does on the G454V chain. |
| `library_auditor.py` | Tool 5 — read-only health check of layout, naming, and subtitles. |
| `movie_standardizer.py` | Tool 6 — the torrent-completion hook: parse scene names, hardlink into `Title (Year)/` (movie plus a bundled English sidecar). |
| `pipeline.py` | **The one runner.** Runs the maintenance tools in the one correct order: extract → audio → clean → 10-bit → audit. |
| `organizekit/` | The shared core, defined exactly once: report rendering, atomic + durable writes, cross-platform locking, the subtitle contract, probe caching, library-root resolution, `playbackchain.py` — **the one hardware-truth table** the toolkit's codec decisions derive from (facts + sources in [docs/hardware.md](docs/hardware.md)) — `toolchain.py` — the one table describing what the five steps are and how to call them — `state.py`, the rebuildable SQLite cache of what each tool last decided. `runlog.py` is the run log itself — one timestamped line to the console and the log file, written under one lock — and `live.py` is the overwritable status line every sweep draws on a terminal and never anywhere else. |
| `tests/` | Fully offline unit tests (1,874), including `tests/selftests/` — each tool's own suite, moved out of the shipped file — plus the stand-ins: `fake_mkvmerge.py` / `fake_ffprobe.py` / `fake_ffmpeg.py` (real executables, enough to drive an end-to-end remux, extraction, transcode and inspection), `fakebin.py` (puts them on a PATH), `fakeprovider.py` (canned OpenSubtitles answers) and `hermetic.py` (pins the host toolchain and the network out). |
| `docs/` | The long-form documentation this page links to: the [playback-chain dossier](docs/hardware.md), the [tool reference](docs/tools.md), [the pipeline](docs/pipeline.md), [configuration](docs/configuration.md), [testing & development](docs/development.md) and, for maintainers, [merging & releasing](docs/merge-and-release.md). |
| `benchmarks/` | The scripts behind every speed claim in this repo — stdlib-only, offline, re-runnable. |
| `.env.example` | Every supported environment variable, annotated. |
| `pyproject.toml` | Packaging metadata; `pip install -e .[dev]` gives you `pytest`. It is also the single source for what the single-file build ships. The distribution is `organizekit` (`organize` on PyPI has been taken since 2011); the command it installs is still `organize`. |
| `MANIFEST.in` | What the source distribution carries beyond the code: the whole test suite *with* its fixtures, the docs, the zipapp builder. |
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

`doctor` verifies Python, the MKVToolNix pair (`mkvmerge` + `mkvextract`),
`ffprobe` (FFmpeg), the optional OpenSubtitles key for image-only movies, and —
crucially — that your download folder and library sit on the **same
filesystem** so hardlinks work. Missing pieces are reported with the exact
fix, never a crash. It exits `1` only if something
is actually broken; a missing optional tool is a warning, because that step
simply skips.

For monitoring, `organize.py doctor --json` prints the same verdicts as one
JSON document and nothing else — see
[Reading the reports](docs/pipeline.md#-reading-the-reports).

### 2 · Run the maintenance pipeline

```bash
python3 organize.py run --dry-run     # preview every command first
python3 organize.py run               # subtitles -> audio (Dolby Digital Plus) -> remux -> 10-bit -> audit
python3 organize.py run --nice        # low priority: Jellyfin streaming is never starved
```

### 3 · Ask what is left

```bash
python3 organize.py status                        # one screen: done vs. remaining
python3 organize.py status --library /path/to/movies
```

`status` re-scans layout and subtitles live (they are cheap, and they are the
two things you can change by moving a file), then joins the expensive verdicts
— bit depth and the remux — from the shared state cache each tool writes as it
runs. A cached verdict is shown **only while it still describes the bytes on
disk**: replace a movie and its old verdict is reported as `stale`, never as an
answer. `--no-state` ignores the cache entirely and shows just the live half.

```console
Library   /srv/media/Movies
          412 movie(s), 3.1 TiB
Layout    408 CANONICAL_MKV   4 MISSING_SIDECAR
Subtitles 408 present   4 missing
Remux     404 cleaned   6 already-clean   2 deferred
Bit depth 388 SKIP_HDR   21 QUEUE_FOR_HANDBRAKE   3 stale
Audio (chain) 400 native-ok   9 transcoded-dolby   1 dts-core-ok   2 review-unknown

Nothing to do for 388 movie(s) - the next pass will touch 24.
```

### 4 · Point Jellyfin at the organized folder

Done — every movie is canonically named, subtitle-complete, audio-chain
native, and direct-play safe on the reference hardware. For the fully
automatic flow (torrent finishes → standardized → pipeline on a schedule),
see [The pipeline](docs/pipeline.md) — the qBittorrent hook, the step order,
and how to read the reports.

> [!TIP]
> Subtitle extraction needs MKVToolNix (`mkvmerge` + `mkvextract`) and nothing
> else. Image-only movies (PGS/VobSub, no text track) additionally use a free
> OpenSubtitles API key — one exact-moviehash lookup per movie; without the key
> those movies are simply reported. The audio step wants `ffmpeg` next to its
> `ffprobe`; without it the step skips and TrueHD/DTS-HD movies stay
> server-transcoded (see `.env.example` for every supported variable).

---

## 🧩 Use only the tools you need

You do **not** have to adopt the whole toolkit. Every tool is a single
standalone file with zero imports from this repository:

```bash
# All you need subtitles:
cp subtitle_extractor.py /path/to/your/media-tools/
python3 /path/to/media-tools/subtitle_extractor.py --source /path/to/movies

# All you need is a library health check:
python3 library_auditor.py --source /path/to/movies
```

Prerequisites per tool:

| Tool | External binary | Notes |
| :--- | :--- | :--- |
| `subtitle_extractor.py` | `mkvmerge` + `mkvextract` (MKVToolNix) | image-only movies (PGS/VobSub) additionally use `OPENSUBTITLES_API_KEY`; without it they are reported for a human |
| `audio_standardizer.py` | `ffmpeg` + `ffprobe` (FFmpeg) | `--dry-run` needs only `ffprobe` |
| `mkv_track_cleaner.py` | `mkvmerge` (MKVToolNix) | — |
| `bitdepth.py` | `ffprobe` (FFmpeg) | — |
| `library_auditor.py` | — | — |
| `movie_standardizer.py` | — | A matching new download replaces the older library movie; no probe required. |

Shared behaviour belongs in `organizekit/core/` and is imported, not copied.
The test suite fails the build if a tool defines a helper the core already
provides. That includes the toolchain itself: which binary a step needs, and
the reason printed when it is missing, come from `organizekit/core/toolchain.py`,
so `pipeline.py` and `organize.py doctor` cannot disagree about whether
this machine is provisioned.

---

## 🧰 The tools

Six tools do the work; one runner runs them in the one correct order.
Each is a single file next to the shared `organizekit/` core, so you can adopt
one and ignore the rest. **[Full reference → `docs/tools.md`](docs/tools.md)**

| Tool | What it does | Needs |
| :--- | :--- | :--- |
| [`subtitle_extractor.py`](docs/tools.md#1--subtitle_extractorpy--validated-english-subtitles) | One validated English `.eng.srt` per movie: the movie's own **text** track via `mkvextract` (MP4s through a temporary MKV bridge), or an exact-moviehash OpenSubtitles match for image-only movies. An existing sidecar is authoritative and never touched; no title search, no OCR. | `mkvmerge` + `mkvextract` |
| [`audio_standardizer.py`](docs/tools.md#2--audio_standardizerpy--chain-native-audio) | Makes audio native to the G454V chain: `ffprobe` sweep; keeps what's native (AC-3/E-AC-3), accepts DTS core and client-decoded PCM, and synthesizes **Dolby Digital Plus 5.1 @ 640 kbps** (the encoder ceiling; wider sources fold) from every TrueHD/DTS-HD/DTS:X master (video/subs untouched, verified publish). | `ffmpeg` + `ffprobe` |
| [`mkv_track_cleaner.py`](docs/tools.md#3--mkv_track_cleanerpy--lossless-remux) | Lossless remux: keep the one best **chain-playable** audio track (the movie's own language — native Dolby over lossless-HD), strip every dub, commentary track and embedded subtitle. Video untouched; seeding movies deferred; a broken `.eng.srt` skips the movie. | `mkvmerge` |
| [`bitdepth.py`](docs/tools.md#4--bitdepthpy--bit-depth--hdr-inspector) | Queue 8-bit SDR for HandBrake, protect native HDR10 / HDR10+ / Dolby Vision fail-closed, and report what each file does on the chain (tone-mapped Direct Play vs. replace/re-encode). | `ffprobe` |
| [`library_auditor.py`](docs/tools.md#5--library_auditorpy--read-only-health-check) | Strictly read-only health check of layout, naming and subtitles, with gating exit codes for cron. | nothing |
| [`movie_standardizer.py`](docs/tools.md#6--movie_standardizerpy--the-ingest-hook) | The torrent-completion hook: hardlink one MKV/MP4 per `Title (Year)/` — plus a bundled English sidecar, renamed to `Title (Year).eng.srt`; the latest matching download replaces the older library file. Zero extra bytes. | nothing |
| [`pipeline.py`](docs/pipeline.md) | The five maintenance steps in the one safe order — subtitles extracted before the remux strips them, the chain-native Dolby track baked in before the cleaner keeps it, and the read-only audit closing the sweep. | — |

---

## 📦 One file, no install

For the machine this toolkit is actually for — a NAS or a home server with
Python and nothing else — build the whole thing into one file and copy it
across:

```bash
python3 scripts/build_pyz.py          # writes dist/organize.pyz (~210 KiB)
scp dist/organize.pyz nas:/volume1/
ssh nas 'cd /volume1 && python3 organize.pyz doctor'
```

It is the same toolkit, not a cut-down one: `organize.pyz test` runs every
field smoke test, `organize.pyz run-tool pipeline.py --source …` runs the full
four-step pass, and each step is still its own process with its own locks, log,
report and exit code. Logs and reports land *beside* the archive, never inside
it. [How it is built and tested →](docs/development.md#one-file-no-install)

---

## 🔒 Safety invariants

Non-negotiable rules every tool obeys:

1. **Hardlink-only ingestion** — `movie_standardizer.py` calls `os.link()`
   exclusively. No copy, no move, no symlink, no cross-device fallback. Your
   seeds keep seeding on the same bytes.
2. **Extraction before remuxing, audio-fit before remuxing** — the cleaner
   removes every embedded subtitle from every movie, so extraction must
   happen while the track is still in the container; and it keeps exactly one
   audio track — the best the chain can *end up with*, convertible masters
   included, because the remux is irreversible — so the Dolby track the audio
   standardizer bakes in must exist before the remux retires the lossless
   master it came from. A movie whose kept track still needs that conversion is
   named in the report rather than left silent.
   "The best the chain can end up with" has to mean the same track to both
   tools, so they classify a stream through one shared blob builder and one
   shared band table: a codec that is chain-native to the audio standardizer
   can never be unplayable to the cleaner, or the transcode would bake in a
   Dolby track the remux then throws away.
   The pipeline enforces both orders, and a test enforces the pipeline. A
   movie cleaned with no sidecar simply never had a usable English track,
   and the report names it: that one is a human decision.
3. **Seeding source data is never modified** — the track cleaner and the
   audio standardizer defer a hardlinked movie (link count > 1)
   unconditionally; the standardizer may replace the library's link, but
   never changes the torrent's file.
4. **Fail-closed concurrency** — all tools coordinate through advisory locks
   keyed by a SHA-256 of the normalized library path. Lock contention halts a
   tool; it never races.
5. **Atomic staging everywhere** — reports, manifests, subtitles, probe
   caches, transcoded audio-fit MKVs and remuxed MKVs are written to unique
   sibling temporaries and swapped with `os.replace`. A crash or power cut
   never leaves a half-written movie.
6. **Unmatched data is never deleted** — declines are reported, duplicate
   maintenance defaults to `REPORT` and destructive maintenance is opt-in.
   The explicit exception is a newly downloaded movie matching the library's
   title/year and version: its hardlink replaces the existing movie, even if
   the old library file was the only copy. Torrent sources are not removed.
7. **A sidecar is never rewritten behind your back** — the extractor creates
   `.eng.srt` files and nothing in this toolkit ever edits one afterwards. An
   existing sidecar is authoritative; timing is a playback-time concern, not
   an offline pass over your library.
8. **A transcoded movie publishes only behind a verified proof** — the audio
   standardizer re-probes its own output before swapping: every original
   stream must still be there, the appended track must be the requested Dolby
   codec at the promised channel count **and** sample rate, it must be the
   container's only default audio track (or a player would still pick the
   lossless master and transcode on every play), the source must be unchanged
   since it was planned, and duration drift over ~3 s refuses the publish.
   ffmpeg's command line is never trusted as the definition of success;
   ffprobe of the result is. Unknown codecs are fail-closed: reported, never
   touched.

---

## 📚 Documentation

| Document | What's in it |
| :--- | :--- |
| [The playback-chain dossier](docs/hardware.md) | The exact reference hardware (G454V, AX3125H, UN60F6350AF), the codec matrix, the wiring guide, and every source the toolkit's audio/video defaults derive from. |
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
