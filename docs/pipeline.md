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

### Machine-readable output

`organize doctor --json`, `organize status --json` and `organize audit --json`
print the same information as one JSON document on stdout and nothing else — no
banner, no colour, no report — so any of them can be piped straight into a
parser. Progress lines, scan logs and the audit's run log go to stderr, where
they cannot corrupt the document:

```bash
organize doctor --json | jq -r '.checks[] | select(.status != "ok") | "\(.status)\t\(.name)\t\(.message)"'
```

```json
{
  "schema": 1,
  "tool": "organize",
  "version": "3.5.0",
  "command": "doctor",
  "library": "/srv/media/Movies",
  "source": "/srv/torrents/final",
  "summary": { "ok": 11, "warn": 1, "fail": 0, "total": 12 },
  "exit_code": 0,
  "checks": [
    {
      "id": "ffsubsync",
      "name": "ffsubsync",
      "status": "ok",
      "message": "Found: 0.4.25",
      "detail": "/usr/local/bin/ffsubsync",
      "remedy": ""
    }
  ]
}
```

Match on `id` rather than `name`: it is a slug of the row name
(`mkvtoolnix-mkvmerge`, `hardlink-compatibility`), stable against rewording of
the printed label, and unique within a run. `status` is `ok`, `warn` or `fail`;
`exit_code` is the process exit code, so a consumer reading the document does
not also have to capture `$?`. `schema` is versioned — it changes if the shape
does.

`organize status --json` follows the same envelope (`schema`, `tool`,
`version`, `command`) and reports the library instead of the machine:

```bash
organize status --json | jq '{movies, settled, pending}'
organize status --json | jq -r '.steps[] | select(.recorded) | "\(.id)\t\(.settled)/\(.stale + .unmeasured) pending"'
```

Each of the five steps is one row with `id`, `label`, `settled`, `stale`,
`unmeasured` and its `counts`, plus `recorded` — which is how a consumer tells
*"nothing left to do"* apart from *"nobody has measured this yet"*, the
distinction the printed report spells out in a footnote. `state_cache` reports
whether the cache was in use and whether it holds any verdict for this library.

A failed run is still a document: a missing library or a failed scan sets
`error` to `{"kind", "message"}` and `exit_code` to `2`, with every other field
present and empty, so a caller never has to parse two formats.

`organize audit --json` reports the library folder by folder:

```bash
organize audit --json | jq -r '.items[] | select(.state != "CANONICAL_MKV") | "\(.state)\t\(.name)"'
organize audit --json | jq '{folders, canonical, findings, defects, canonical_pct}'
```

Every folder is one row with `name`, `folder`, `state`, `subtitle`, `detail`
and its `movie_files`, in the order the report lists them, plus the tallies
(`states`, `containers`, `canonical_pct`) and the `report` path — because a
JSON run is not a different audit, it is the same one read differently, and the
plain-text report is still written and the state cache still published.
`subtitle` is the auditor's own split of a folder state into a subtitle verdict
(`present`, `missing`, `invalid`, `noncanonical`, or `null` where the folder has
no sidecar question worth naming), so a consumer never has to re-derive it.

**Every failure is a document too.** A missing library, a busy lock, an
unwritable report: `error` is `{"kind", "message"}`, `exit_code` carries the
process exit code (`2`, `3`, …) and every other field is present and empty. A
caller never has to parse two formats.

None of the three documents carries a **timestamp**, deliberately — nor does
`status` report its scan duration or `audit` its elapsed time. Two runs over an
unchanged library produce byte-identical output, so a nightly job can diff
today's against yesterday's and alert only when something really changed.

---

[← Back to the README](../README.md) · [Tool reference](tools.md) ·
[Configuration](configuration.md)
