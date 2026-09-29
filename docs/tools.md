Every tool in detail: what it decides, why it decides it that way, and the
flags worth knowing. Each one is a single file that runs out of a clone with
no install (`python3 subtitle_extractor.py --help`), and each is also a verb on
the front door (`organize.py extract …`).

For the order they run in and why that order is load-bearing, see
[The pipeline](pipeline.md). For the environment variables they share, see
[Configuration](configuration.md).

| Tool | One line | Needs |
| :--- | :--- | :--- |
| [`subtitle_extractor.py`](#1--subtitle_extractorpy--validated-english-subtitles) | One validated English `.eng.srt` per movie: from the movie's own text track, or an exact-hash OpenSubtitles match when only bitmaps exist | `mkvmerge` + `mkvextract` (an OpenSubtitles API key for image-only movies) |
| [`audio_standardizer.py`](#2--audio_standardizerpy--chain-native-audio) | Chain-native audio for the G454V: bake one AC-3 5.1 @ 640 kbps in from every TrueHD/DTS-HD master, or report what's already native | `ffprobe` (+ `ffmpeg` when something needs the new track) |
| [`mkv_track_cleaner.py`](#3--mkv_track_cleanerpy--lossless-remux) | Lossless remux: keep the one best chain-playable audio, strip commentary, dubs and embedded subtitles | `mkvmerge` |
| [`bitdepth.py`](#4--bitdepthpy--bit-depth--hdr-inspector) | Queue 8-bit SDR for HandBrake, protect HDR fail-closed, report each file's chain fit | `ffprobe` |
| [`library_auditor.py`](#5--library_auditorpy--read-only-health-check) | Read-only health check of layout, naming and subtitles | nothing |
| [`movie_standardizer.py`](#6--movie_standardizerpy--the-ingest-hook) | The torrent-completion hook: hardlink MKV/MP4 into `Title (Year)/`, replacing an older matching movie | nothing |

Every tool runs on its own and accepts `--help`; `python3 <tool>.py --version`
prints the version, and `python3 <tool>.py --self-test` runs that tool's
built-in checks against a temporary library and exits non-zero on failure —
neither writes anything to your library.

### Flags the tools share

The same flag means the same thing wherever it appears. A tool simply omits
the ones that do not apply to it (the cleaner takes movie paths, not a library
root; the auditor has nothing to write so it has no `--dry-run`).

| Flag | Tools | Meaning |
| :--- | :--- | :--- |
| `--source PATH` | pipeline, extractor, audiofit, bit-depth, auditor, standardizer | The library (or, for the standardizer without paths, the batch-scan root) to work on. Equivalent to `ORGANIZE_LIBRARY`, which is the reason the flag is rarely needed. |
| `--log PATH` · `--report PATH` | extractor, audiofit, cleaner, bit-depth, auditor, standardizer | Where this run's log and its single replaceable report live. Both default outside the library — `$XDG_STATE_HOME/organize/<tool>/` on Linux/macOS, `E:\torrents\tools\ReportsAndLogs\<tool>` on Windows ([Configuration](configuration.md)). |
| `--dry-run` | pipeline, extractor, audiofit, cleaner, bit-depth, standardizer | Do everything except the mutation, and say what would have happened. The auditor needs no such flag: it never writes. |
| `--min-size MB` | extractor, audiofit, cleaner, bit-depth, standardizer | Ignore movies smaller than this. The point is to skip samples, extras and half-downloaded files rather than to filter a library. If it filters away *every* movie, the extractor says so — a report of "0 movies, 100% covered" would be a green page about a library nothing had looked at. |
| `--lock-timeout SECONDS` | extractor, audiofit, bit-depth, auditor, standardizer | How long to wait for a conflicting run before refusing to start, instead of racing it. The extractor and the standardizer share one cross-tool advisory lock — that is what stops the completion hook placing hardlinks under a sweep that is reading the library — while the auditor, audiofit and bit-depth each take a lock of their own. The cleaner passes the same idea through `--standardizer-lock-timeout`. |
| `--workers N` | extractor, audiofit, bit-depth, auditor | How many movies to inspect at once (`0` = decide from the CPU count, `1` = the serial run). Results come back in input order, so the output does not change with the number. (audiofit probes in parallel; transcodes run one at a time — an encode is the ceiling anyway.) |
| `--state-db PATH` · `--no-state` | audiofit, cleaner, bit-depth, auditor, `organize status` | The shared verdict cache (`--no-state` turns it off for this run, `ORGANIZE_NO_STATE=1` turns it off for every run). It is only ever a cache: every tool re-checks what it is about to act on. |
| `--verbose` | audiofit, bit-depth, standardizer, `organize.py` | Print the per-file decisions the summary folds away. |
| `--self-test` · `--version` | every tool | Shipped in the file so a machine with no checkout can still be asked "is this thing sane, and which build is it?". |

---

## 1 · `subtitle_extractor.py` — validated English subtitles

Writes one external `.eng.srt` per movie, **right beside the movie's own
`.mkv`**, in this order:

1. **An existing validated `.eng.srt` is authoritative** — the tool leaves the
   movie completely alone;
2. **a text-based embedded English track is extracted first.** SRT/SSA/ASS/
   WebVTT/USF tracks come out through `mkvextract` and are converted
   in-process. This is local, free, and exact for the release, so it always
   wins;
3. **when the only English subtitles are bitmaps** (PGS/SUP, VobSub, DVB —
   which this tool deliberately does not OCR), it asks OpenSubtitles for
   subtitles matching **this file's exact moviehash** and installs an English
   match;
4. anything still uncovered (no usable track at all, no API key, no hash
   match) is reported under "needs attention", with the reason naming the fix.

OCR was removed because a garbled transcription looks like success while
leaving the dialogue wrong — and unlike a name-matched download, it is not
verifiable. The replacement is the opposite kind of evidence: the moviehash is
calculated from the file's own bytes (size plus the first and last 64 KiB), and
the search asks for `moviehash_match=only`, so the provider itself confirms the
subtitle belongs to this exact release. There is **no title, year or
release-name search anywhere** — a wrong-cut subtitle is worse than none. With
no API key configured the fallback is skipped and the tool never touches the
network.

A track that is forced/signs-only, commentary, non-English, or too short to be
the whole film is refused — the movie lands in the report's "needs attention"
section with the reason, not with a half-subtitle beside it. **MP4s are read
through a temporary MKV bridge** (`mkvmerge` wraps the MP4, `mkvextract` reads
the bridge, the bridge is discarded): mkvextract cannot read an MP4's
`mov_text` tracks directly, and the bridge means the same validation path covers
both containers.

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
holds the movie, the method, the cue count, the SHA-256 of the bytes written
and the timestamp — plus, for an extracted sidecar, the track it came from,
and for a download, the moviehash, file_id and release the provider matched.
That is the durable answer to "did this tool write this sidecar, and where did
it come from?", and what makes a re-run cheap. Replace the bytes by hand and
the record no longer matches, so the file is not "ours" any more. A movie the
tool cannot cover (no track, no key, no hash match, unreadable container) is a
report entry, not an error; exit code is `1` only when a movie genuinely
errored, `2` for configuration problems.

```bash
python3 subtitle_extractor.py --source /path/to/movies --dry-run   # preview
python3 subtitle_extractor.py --source /path/to/movies --limit 10  # first 10
python3 subtitle_extractor.py --source /path/to/movies --download-limit 5  # at most 5 provider downloads per run
python3 subtitle_extractor.py --source /path/to/movies --no-download       # never touch the network
python3 subtitle_extractor.py --source /path/to/movies --workers 8         # library on a NAS
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

**The image-only fallback.** Text extraction needs
[MKVToolNix](https://mkvtoolnix.download/) (`mkvmerge` + `mkvextract`) and
nothing else. For movies whose English subtitles exist only as bitmaps, the
tool makes **one** OpenSubtitles search keyed on the file's exact moviehash,
then at most one download of the best match:

```bash
export OPENSUBTITLES_API_KEY=...        # or put it in .env; unset = fully offline
python3 subtitle_extractor.py --source /path/to/movies --dry-run        # preview: searches, never downloads
python3 subtitle_extractor.py --source /path/to/movies --download-limit 5
python3 subtitle_extractor.py --source /path/to/movies --no-download    # text tracks only
```

The search sends `languages=en`, the 16-hex `moviehash`, the filename as
`query`, and `moviehash_match=only`; a result is installed only if the
provider flags it as a hash match, is tagged English, is not a
forced/foreign-parts-only stream, and is not machine- or AI-translated. The
downloaded bytes then pass the same cue-count and English-text gate an
extracted track does, and are written create-only, so a sidecar that appears
mid-run is never overwritten. The provider is asked once per movie, serially,
with a quarter-second floor between requests and a backoff on `429` — nowhere
near the 5 requests/s limit.

Four flags tune that tier, and none of them is needed to run it:

| Flag | What it does |
| :--- | :--- |
| `--download-limit N` | At most `N` downloads this run (0 = no run cap). The provider's own daily allowance — 5 per IP without an account, 20 with a free one — still applies, and the run stops asking the moment the API says it is spent. A `--dry-run` preview counts what it previews, so a preview matches a live run with the same arguments. |
| `--download-min-cues N` | The cue floor for a *downloaded* subtitle (default 10). `--extract-min-cues N` is the separate floor for an *extracted* track; both exist because a signs-and-songs-only subtitle would otherwise look like success while leaving the dialogue missing. |
| `--download-timeout SEC` | Per-request time limit for the search, the download request and the file fetch (default 30). |
| `--no-download` | Never contact OpenSubtitles at all: image-only movies are reported for a human instead. |

A folder the sidecar cannot be written to is checked *before* the search, so a
read-only mount or a permissions mistake costs no download.

## 2 · `audio_standardizer.py` — chain-native audio

Every verdict derives from one fact the hardware dossier
([hardware.md](hardware.md)) establishes: the **Chromecast with Google TV
(HD) G454V can emit Dolby Digital (AC-3), Dolby Digital Plus (E-AC-3,
Atmos included) and decoded PCM — never TrueHD, DTS-HD MA, DTS:X or WMA
Pro.** A movie whose best track is one of those lossless-HD formats is
*audio-transcoded by the Jellyfin server on every single play* — for the
whole runtime of the file, forever. This tool is the one-time offline
answer. For each movie it probes (`ffprobe`) and classifies:

| Source situation | Verdict | What happens |
| :--- | :--- | :--- |
| AC-3 / E-AC-3 on board | `native-ok` | nothing — already bitstreams end-to-end |
| base 5.1 DTS core | `dts-core-ok` | accepted (the AX3125H has a DTS decoder and the player's Amlogic firmware passes core DTS — unofficial but real); `--no-dts-passthrough` transcodes these too |
| AAC / FLAC / PCM / MP3 / Opus | `pcm-decode-ok` | the player decodes to PCM; stereo variants are always fine; on the default ARC wiring multichannel variants become transcode candidates (plain ARC/optical = stereo PCM only), while `--wiring soundbar-hdmi-in` accepts them as-is |
| TrueHD / DTS-HD MA / DTS-HD HRA / DTS:X | `transcoded-ac3` | **one AC-3 track is synthesized and appended, video untouched** |
| unknown codec | `review-unknown` | fail-closed in the report; never auto-touched |
| still hardlinked to a seed | `deferred-seeding` | untouched until seeding stops |

The AC-3 bake-in is exactly one ffmpeg invocation per movie:
`ffmpeg -i movie.mkv -map 0 -c copy … -c:a:N ac3 -b:a 640k -ac 6 -ar 48000`.
Every original stream is copied; the new track is **appended, marked
default, and titled with its provenance** ("Dolby Digital 5.1 (from TrueHD)").
7.1 folds to 5.1 at 640 kbps; stereo sources stay stereo at 192 kbps, never
upmixed. The **source is the best track in the movie's own language** —
using `mkv_track_cleaner`'s own commentary/dub/native-language rules, so a
Japanese film is transcoded from its Japanese TrueHD even when an English
DTS dub sits next to it. Video streams are never re-encoded.

Publishing is fail-closed: the output is **re-probed** before the swap —
every original stream identical, the appended track AC-3 with the right
channel count, duration drift ≤ 3 s — and dropped otherwise. The temp file
carries the tool's marker name (`.*.audiofit-PID.tmp.mkv`), a stale one is
swept the next run, and `os.replace` publishes atomically. A movie hardlinked
to its torrent seed is deferred rather than replaced. `--dry-run` needs only
`ffprobe` (it prints the plan; ffmpeg is checked at run start when
transcodes are due and missing makes the step skip with a reason, like every
other prerequisite in the toolkit).

```bash
python3 audio_standardizer.py --source /path/to/movies --dry-run
python3 audio_standardizer.py --source /path/to/movies --wiring soundbar-hdmi-in  # upgrade wiring
```

## 3 · `mkv_track_cleaner.py` — lossless remux

Every movie ends up with exactly one audio track — the best-scoring one in
the movie's own (native) language — and video is never re-encoded. Which
track *is* best is the playback chain's table, not a golden-ear ranking: on
the G454V → AX3125H chain ([hardware.md](hardware.md)) the keeper is the best
track **the player can actually emit**. Scores in descending order: Dolby
Digital Plus (E-AC-3, Atmos included, 100) and Dolby Digital (AC-3, 95)
bitstream end-to-end; base DTS (80) is decoded by the soundbar; client-
decodable formats (AAC 60/62, FLAC/PCM 66) arrive as PCM; and the lossless-HD
formalities — TrueHD, DTS-HD MA, DTS-HD HRA, DTS:X — sit **below all of
them** (30–34) because the G454V can never emit them, so keeping one would
commit the server to an audio transcode on every play. `audio_standardizer.py`
runs *before* this tool in the pipeline and bakes the chain-native AC-3 in
from exactly those masters, so nothing of audible value is lost when they
leave.

The rest of the contract is unchanged: dubs, commentary and every other
language go. The native language is decided by the file's own markers, in
order: a track flagged *original*, a single shared language, the
default-flagged track, then track order (dubs are conventionally appended
last); a track titled "dub"/"dubbed" is never the keeper whatever its
language. Every embedded subtitle is removed on every remux, sidecar or
not — the external `.eng.srt` is the library's only subtitle, which is why
`subtitle_extractor.py` runs first in the pipeline. A movie remuxed with no
sidecar is named in the report (it has no subtitle now), and a movie with a
**broken** `.eng.srt` beside it is skipped entirely: an existing sidecar is
authoritative even when it is unusable — fix or delete it, and the next run
cleans the movie.
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

## 4 · `bitdepth.py` — bit-depth & HDR inspector

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

Beyond that bit-depth verdict, every report row also carries the file's
**playback-chain fit** for the G454V chain ([hardware.md](hardware.md)) —
informational, never an override of the fail-closed status above:

| File | What it does on the chain |
| :--- | :--- |
| ≤1080p H.264/HEVC/VP9/AV1(+MPEG-2), SDR or HDR10/HDR10+/HLG | Direct Play (HDR is tone-mapped to SDR for this 1080p SDR panel at playback time — zero generation loss, and the file still must NOT go through HandBrake) |
| Dolby Vision (no HDR10/SDR base layer) | the G454V is not DV-licensed: server transcodes every play → replace with HDR10+ or re-encode |
| >1080p (4K, 8K) | the player has no 4K output: server transcodes every play → a 1080p version belongs in this library |
| VC-1 / ProRes / unknown codecs | never work → replace |

The HandBrake guidance for anything queued is chain-shaped: H.265 Main10
10-bit at ≤1080p (or H.264 High@L4.1 8-bit) + AC-3 5.1 @ 640 kbps audio —
both Direct Play end-to-end.

```bash
python3 bitdepth.py --source /path/to/movies
python3 bitdepth.py --source /path/to/movies --fail-if-queue   # for schedulers
```

## 5 · `library_auditor.py` — read-only health check

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

## 6 · `movie_standardizer.py` — the ingest hook

Parses scene release names and places one hardlinked movie file (plus any
validated subtitle) per `Title (Year)/` folder. Hardlink-only: the download
folder keeps seeding, the library uses 0 extra bytes. Skips TV, disc rips,
and splits. Also finds duplicate folders of the same movie on request
(`--deduplicate`, non-destructive by default).

**MKV and MP4 are both placed; MKV wins within a single release.** Nothing is
transcoded: an MP4 lands as `Title (Year)/Title (Year).mp4`. Other containers
(`.avi`, `.m4v`, `.ts`, disc images, …) stay in the download folder. When a
*later* download has the same parsed title and year (and any edition/version
marker matches the canonical name), its hardlink replaces the library movie,
even if it is smaller or has a different container. No `ffprobe` or quality
score is used; the latest incoming release wins (`--ffprobe` is accepted but
ignored for compatibility with older hooks). The old file is not removed
until the new link is published and verified. If the container changed, the old
extension is removed afterwards, keeping one feature in the folder. The
download remains untouched, and existing `.eng.srt` sidecars remain
unchanged. Unmarked alternate cuts cannot be distinguished by filename alone;
a release marked with an edition is *not* allowed to overwrite an unmarked
canonical movie. The torrent-completion hook treats the incoming download as
the latest; batch scans process source items by modification time, oldest
first, so the newest source wins. The optional `--deduplicate` sweep is a
separate maintenance operation, not this incoming replacement rule.

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
