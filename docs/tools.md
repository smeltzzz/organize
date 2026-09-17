# Tool reference

Every tool in detail: what it decides, why it decides it that way, and the
flags worth knowing. Each one is a single file that runs out of a clone with
no install (`python3 subtitle_extractor.py --help`), and each is also a verb on
the front door (`organize.py extract …`).

For the order they run in and why that order is load-bearing, see
[The pipeline](pipeline.md). For the environment variables they share, see
[Configuration](configuration.md).

| Tool | One line | Needs |
| :--- | :--- | :--- |
| [`subtitle_extractor.py`](#1--subtitle_extractorpy--validated-english-subtitles) | One validated English `.eng.srt` per movie: the movie's own embedded text track, or the OpenSubtitles SRT matching the movie's hash exactly (image-only movies) | `mkvmerge` + `mkvextract` (OpenSubtitles account for image-only movies) |
| [`mkv_track_cleaner.py`](#2--mkv_track_cleanerpy--lossless-remux) | Lossless remux: keep one best audio, strip commentary, dubs and embedded subtitles | `mkvmerge` |
| [`bitdepth.py`](#3--bitdepthpy--bit-depth--hdr-inspector) | Queue 8-bit SDR for HandBrake, protect HDR fail-closed | `ffprobe` |
| [`library_auditor.py`](#4--library_auditorpy--read-only-health-check) | Read-only health check of layout, naming and subtitles | nothing |
| [`movie_standardizer.py`](#5--movie_standardizerpy--the-ingest-hook) | The torrent-completion hook: parse scene names, hardlink MKV/MP4 into `Title (Year)/` | `ffprobe` (optional) |

---

## 1 · `subtitle_extractor.py` — validated English subtitles

Creates one external `.eng.srt` per movie, and the goal is a subtitle beside
every movie. The preference order is the whole policy:

1. **The movie's own embedded English text track** (SRT/ASCII/SSA/ASS/
   WebVTT/USF). The movie's own track is the best source there is: it is
   exact for this release (it cannot be the wrong cut), it costs nothing,
   and its cues come from the container's own timeline. It is extracted
   with `mkvextract` and converted to canonical SRT in-process.
2. **The OpenSubtitles exact-hash download, for image-only movies.** A
   movie whose only English subtitle tracks are image-based (PGS/SUP,
   VobSub, DVB) is not OCR'd — OCR was unreliable in the field, and a bad
   sidecar is worse than none. Instead the tool hashes the movie the way
   OpenSubtitles keys its uploads and downloads the English SRT whose hash
   matches *exactly*: a hit is a subtitle for this exact release, with no
   title, no year and no guesswork. It passes the same quality gate as an
   extraction before it is written.
3. **A human decision.** A movie with no usable text track and no
   exact-hash match (or no account configured) lands in the report's
   "needs attention" section with the reason, not with a half-subtitle
   beside it.

A track that is forced/signs-only, commentary, non-English, or too short to
be the whole film is refused at every stage. **MP4s are read through a
temporary MKV bridge** (`mkvmerge` wraps the MP4, `mkvextract` reads the
bridge, the bridge is discarded): mkvextract cannot read an MP4's
`mov_text` tracks directly, and the bridge means the same validation path
covers both containers.

**The one rule that never bends: an existing `.eng.srt` is authoritative.**
If a movie already has a validated sidecar — placed by hand, carried over,
or extracted last week — this tool leaves it, and the movie, completely
untouched. It is never re-extracted and never rewritten. Extraction runs
only for movies with no sidecar at all; that is why it must run *before*
the track cleaner, which removes every embedded subtitle from every
movie.

**No offline timing pass.** A sidecar built from the movie's own track
carries the container's own timestamps, so it is already frame-accurate
for that exact file and there is nothing for this toolkit to correct.
Any drift a client does notice is a playback-time concern — Jellyfin's
own subtitle-offset support, or a plugin such as Lapse, handles it
without rewriting a byte of the library. (An offline `ffsubsync` stage
used to sit at the end of the pipeline for exactly this; it was removed,
because a second program measuring and rewriting sidecars bought nothing
the container's own timeline had not already given.)

**Every sidecar this tool writes is recorded.** The provenance ledger
outside the library (`ReportsAndLogs/subtitle_extractor_extracted.json`)
holds the movie, the track (or, for a download, the OpenSubtitles file
and the movie hash), the method (`text` or `download`), the cue count,
the SHA-256 of the bytes written and the timestamp — the durable answer
to "did this tool write this sidecar?", and what makes a re-run cheap.
Replace the bytes by hand and the record no longer matches, so the file
is not "ours" any more. Coverage failures (no track, no exact match, no
credentials, unreadable container) are not errors — the movie is simply
reported as uncovered; exit code is `1` only when a movie genuinely
errored, `2` for configuration problems.

```bash
python3 subtitle_extractor.py --source /path/to/movies --dry-run   # preview
python3 subtitle_extractor.py --source /path/to/movies --limit 10  # first 10
python3 subtitle_extractor.py --source /path/to/movies --no-open-subtitles  # text tracks only
python3 subtitle_extractor.py --source /path/to/movies --workers 8           # library on a NAS
```

**Parallel triage.** Before a movie can cost an extraction attempt the tool
answers three local questions about it — is the folder canonical, is there
already a usable English sidecar, what is the file's identity — and on a
mostly-covered library that pre-flight *is* the run: one directory listing
and a couple of small reads per movie. Those reads happen in a worker pool
(`--workers`, default half the CPUs capped at 8; `1` restores the exact
serial run). Measured on 600 movies
(`benchmarks/bench_triage_workers.py`): from a warm page cache the threads
cost more than they save (0.06 s → 0.23 s, and it is 0.23 s); with a 5 ms
round trip per folder — an HDD seek, or a library on SMB/NFS — it is
**3.2 s serial → 0.50 s at 8 workers (6.3×)**. The verdicts come back in
input order, so the console, the log and the report are byte-identical to
the serial run.

**Every sweep says what it is doing.** On a terminal the auditor, the 10-bit
inspector, the extractor and the standardizer each draw one
status line — `auditing [████░░░░] 42%  1,204/2,860  ~4m30s left  Movie
(2020)` — that is rewritten in place and erased before every permanent
line, the same renderer the track cleaner has always used for its remux bar
(`organizekit/core/live.py`). It is **strictly a terminal effect**: off a
TTY — redirected, piped, under cron, or with `--json` — nothing is drawn at
all, so log files and captured output are byte-for-byte what they were.

**OpenSubtitles exact-hash download.** Image-only movies (the common case:
a Blu-ray release whose English subtitle is PGS) are served by an
[OpenSubtitles](https://www.opensubtitles.com) download, configured with a
free account. The API key authorises the search; the username and password
are exchanged for a bearer token that authorises the download:

```bash
# credentials from the environment (a .env beside the scripts works)
export OPENSUBTITLES_API_KEY=... OPENSUBTITLES_USERNAME=... OPENSUBTITLES_PASSWORD=...
python3 subtitle_extractor.py --source /path/to/movies

# or per run
python3 subtitle_extractor.py --source /path/to/movies \
        --osdb-api-key ... --osdb-username ... --osdb-password ...

# image-only movies stay reported, never downloaded
python3 subtitle_extractor.py --source /path/to/movies --no-open-subtitles
```

The identification is deliberately an **exact match on the movie's hash** —
the same MD5 (leading bytes plus file size) OpenSubtitles computes over
uploaded subtitle files. There is no title or year search: a fuzzy match is
how the wrong cut, the wrong language or a trailer ends up on a movie. The
downloaded file is re-rendered and passes the same quality gate an
extraction does (valid SRT cues, at least `--extract-min-cues` of them,
Latin script, English) before it is written create-only beside the movie.
One login serves the whole run, a dry run performs the search (so it can
say whether a match exists) but never downloads, and the run stops asking
when the service rejects the credentials, rate-limits it, or stops
answering three times in a row. Without credentials the fallback simply
does not run, and the report and `organize.py doctor` say so.

## 2 · `mkv_track_cleaner.py` — lossless remux

Every movie ends up with exactly one audio track — the best-scoring one in
the movie's own (native) language — and video is never re-encoded. Dubs,
commentary and every other language go. The native language is decided by
the file's own markers, in order: a track flagged *original*, a single
shared language, the default-flagged track, then track order (dubs are
conventionally appended last); a track titled "dub"/"dubbed" is never the
keeper whatever its language. Every embedded subtitle is removed on every
remux, sidecar or not — the external `.eng.srt` is the library's only
subtitle, which is why `subtitle_extractor.py` runs first in the pipeline.
A movie remuxed with no sidecar is named in the report (it has no subtitle
now), and a movie with a **broken** `.eng.srt` beside it is skipped
entirely: an existing sidecar is authoritative even when it is unusable —
fix or delete it, and the next run cleans the movie.
**MP4s are converted to MKV** in the same remux (a lossless container swap
with the transactional replace, free-space check and seeding deferral the
MKV path already had). A movie remuxed *without* a validated sidecar keeps
its embedded English subtitle tracks, so `subtitle_extractor.py` can still
build one on a later run. Movies still hardlinked to their torrent source
are always deferred.

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

Parses scene release names and places one hardlinked movie file (plus any
validated subtitle) per `Title (Year)/` folder. Hardlink-only: the download
folder keeps seeding, the library uses 0 extra bytes. Skips TV, disc rips,
and splits. Also finds duplicate folders of the same movie on request
(`--deduplicate`, non-destructive by default).

**MKV and MP4 are both placed; MKV is canonical.** Nothing is ever transcoded,
so accepting a container means hardlinking it under its own extension: an MP4
release lands as `Title (Year)/Title (Year).mp4`. Every other container
(`.avi`, `.m4v`, `.ts`, disc images, …) is left in the download folder and
named in the report, because renaming a file to a container it is not would be
a lie about its contents. When one movie arrives as both an MKV and an MP4 the
MKV is placed, and when a library folder already holds one of the two the other
is declined and reported rather than added beside it — two features in one
folder is exactly what `library_auditor.py` flags as
`MULTIPLE_DIRECT_MOVIE_FILES`, and nothing here deletes the copy that is
already there.

An MP4 in the library is a guest that the pipeline converts: the
`subtitle_extractor.py` step lifts any embedded subtitles out of it through
a temporary MKV bridge, and the `mkv_track_cleaner.py` step swaps the
container itself for a canonical MKV — losslessly, in the same remux that
cleans the tracks. `library_auditor.py` reports a still-unconverted MP4 as
`SINGLE_OTHER_CONTAINER`, and `bitdepth.py` only ever sees MKVs in a
maintained library. An MKV release still lands as an MKV, and when one
movie arrives as both the MKV is placed.

```bash
python3 movie_standardizer.py --source /path/to/downloads --target /path/to/movies --dry-run
```

[← Back to the README](../README.md) · [The pipeline](pipeline.md) ·
[Configuration](configuration.md) · [Development](development.md)
