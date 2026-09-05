# The pipeline

How a finished torrent becomes a canonical movie: the ingest hook, the five
maintenance steps and the order they must run in, and how to read what they
write.

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

## 🔄 The order, and why it is fixed

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

[← Back to the README](../README.md) · [Tool reference](tools.md) ·
[Configuration](configuration.md)
