# Tool reference

Every tool in detail: what it decides, why it decides it that way, and the
flags worth knowing. Each one is a single file that runs out of a clone with
no install (`python3 subtitle_fetcher.py --help`), and each is also a verb on
the front door (`organize.py subtitles …`).

For the order they run in and why that order is load-bearing, see
[The pipeline](pipeline.md). For the environment variables they share, see
[Configuration](configuration.md).

| Tool | One line | Needs |
| :--- | :--- | :--- |
| [`subtitle_fetcher.py`](#1--subtitle_fetcherpy--validated-english-subtitles) | One validated English `.eng.srt` per movie: the movie's own embedded track first, then nine sources | nothing (API keys optional) |
| [`mkv_track_cleaner.py`](#2--mkv_track_cleanerpy--lossless-remux) | Lossless remux: keep one best audio, strip commentary, dubs and embedded subtitles | `mkvmerge` |
| [`bitdepth.py`](#3--bitdepthpy--bit-depth--hdr-inspector) | Queue 8-bit SDR for HandBrake, protect HDR fail-closed | `ffprobe` |
| [`library_auditor.py`](#4--library_auditorpy--read-only-health-check) | Read-only health check of layout, naming and subtitles | nothing |
| [`movie_standardizer.py`](#5--movie_standardizerpy--the-ingest-hook) | The torrent-completion hook: parse scene names, hardlink into `Title (Year)/` | `ffprobe` (optional) |
| [`sync_subtitles.py`](#6--sync_subtitlespy--subtitle-timing-sync-ffsubsync) | Measure every sidecar against the audio and correct trustworthy drift | `ffsubsync` + `ffmpeg` |
| [`jellyfin_one_shot.py`](#7--jellyfin_one_shotpy--the-never-stop-completer) | Loops the whole toolchain until the auditor reports 100% canonical | whatever its steps need |

---

## 1 · `subtitle_fetcher.py` — validated English subtitles

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

   **Politeness is metered per host, not per run.** Every provider gets its own
   token bucket (one request per second per site, the documented limit), so
   asking Subf2m never waits on the request that just went to Podnapisi. On a
   200-movie pass across the seven sources that is 30 minutes of sleeping
   reduced to 9.5 — with every individual site still paced exactly as before
   (`benchmarks/bench_scrape_gaps.py`). When a provider answers `429
   Retry-After`, the whole bucket for that host is held back, because that
   header is about the server, not about the one unlucky request.

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
python3 subtitle_fetcher.py --source /path/to/movies --workers 8             # library on a NAS
```

**Parallel triage, serial spending.** Before a movie can cost a provider
request the fetcher answers three local questions about it — is the folder
canonical, is there already a usable English sidecar, what is the file's
identity — and on a mostly-covered library that pre-flight *is* the run: one
directory listing and a couple of small reads per movie. Those reads happen in
a worker pool (`--workers`, default half the CPUs capped at 8; `1` restores the
exact serial run). Measured on 600 movies
(`benchmarks/bench_triage_workers.py`): from a warm page cache the threads cost
more than they save (0.06 s → 0.23 s, and it is 0.23 s); with a 5 ms round trip
per folder — an HDD seek, or a library on SMB/NFS — it is **3.2 s serial →
0.50 s at 8 workers (6.3×)**.

Everything downstream of triage stays on the single main thread: the quota
ledger, the provider tiers, every download, every state checkpoint. A worker
never spends a request, and the pool works at most 32 movies ahead of the loop,
so a run that stops on an exhausted quota has not read the whole library. The
verdicts come back in input order, so the console, the log and the report are
byte-identical to the serial run — the tests assert exactly that, by running
the same library both ways and diffing.

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

## 2 · `mkv_track_cleaner.py` — lossless remux

Keeps the single best English audio track (or, for foreign films with a
validated `.eng.srt`, the best non-commentary audio of any language) and
strips commentary, dubs, and embedded subtitles. Video is never re-encoded.
Movies still hardlinked to their torrent source are always deferred.

```bash
python3 mkv_track_cleaner.py --dir /path/to/movies --dry-run
python3 mkv_track_cleaner.py --dir /path/to/movies --nice --only "Some Movie (2020).mkv"
```

## 3 · `bitdepth.py` — bit-depth & HDR inspector

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

## 4 · `library_auditor.py` — read-only health check

Validates the `Title (Year)/Title (Year).mkv` + `.eng.srt` layout, flags
foreign artifacts, misnamed sidecars, and missing subtitles. Strictly
read-only; exit codes are designed for cron / Task Scheduler gating.

```bash
python3 library_auditor.py --source /path/to/movies --fail-on-findings
python3 library_auditor.py --source /path/to/movies --workers 8   # library on a NAS
```

The audit is thousands of directory reads and almost no computation, so
folders are read in parallel (`--workers`, `1` for one at a time). The win
scales with how far away the storage is. Measured on 600 folders
(`benchmarks/bench_audit_workers.py`): from a warm page cache the threads cost
more than they save (0.05 s → 0.14 s, and it is 0.14 s); with a 5 ms round trip
per folder — an HDD seek, or a library on SMB/NFS — it is **3.1 s serial → 0.40 s
at 8 workers (7.8×)**. The audit itself is identical either way: results are
returned in input order, so the report cannot tell how it was scheduled.

## 5 · `movie_standardizer.py` — the ingest hook

Parses scene release names and places one canonical hardlinked MKV (plus any
validated subtitle) per `Title (Year)/` folder. Hardlink-only: the download
folder keeps seeding, the library uses 0 extra bytes. Skips TV, disc rips,
and splits. Also finds duplicate folders of the same movie on request
(`--deduplicate`, non-destructive by default).

```bash
python3 movie_standardizer.py --source /path/to/downloads --target /path/to/movies --dry-run
```

## 6 · `sync_subtitles.py` — subtitle-timing sync (ffsubsync)

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
python3 sync_subtitles.py --source /path/to/movies --workers 4  # measure 4 at once
python3 sync_subtitles.py --source /path/to/movies --fail-on-review  # cron gating
```

**Sidecars are measured in parallel.** ffsubsync is the slowest thing the
toolchain does — it decodes the movie's audio and correlates it against the
subtitle — and each sidecar is an independent measurement, so they run
concurrently (`--workers`, default: half the CPUs capped at 4; `1` restores the
serial run). Measured on 8 movies with a 0.5 s-per-sync stand-in
(`benchmarks/bench_sync_workers.py`): **4.2 s serial → 1.2 s at 4 workers
(3.6×)**, same outcome for every sidecar. The cap
is low on purpose: each worker starts an ffmpeg that is itself multi-threaded
and reads a different movie, so a bigger fan-out turns a CPU bound into a disk
bound.

**Re-running costs nothing.** A sidecar measured "in sync", or corrected and
swapped in, is recorded outside the library (`sync_state.json`, override with
`--sync-ledger` or `SUBTITLE_SYNC_LEDGER`) with the subtitle's SHA-256 and the
movie's size and mtime. The record is honoured only while **both** still
match: re-download, re-extract, hand-edit or replace the subtitle, or remux
the movie, and it is measured again. Held-for-review and failed syncs are
never recorded — those still need another look. Delete the file to
re-measure everything.

## 7 · `jellyfin_one_shot.py` — the "never stop" completer

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
> anything else), the two probe caches, `sync_state.json`, and `state.db` —
> the rebuildable cache of verdicts that `organize status` reads.

`--source` is the Jellyfin movie-library root. Every tool in the repo resolves
it through one shared resolver — `--source`, then `ORGANIZE_LIBRARY`, then the
legacy `MOVIE_STD_TARGET`, then the platform default — so `python3
jellyfin_one_shot.py` with no arguments finishes the same library the rest of
the toolchain maintains. The library it resolved, and where that value came
from, is written to the runtime log at the start of every run.

---

[← Back to the README](../README.md) · [The pipeline](pipeline.md) ·
[Configuration](configuration.md) · [Development](development.md)
