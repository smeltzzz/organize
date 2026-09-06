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

None of those three documents carries a **timestamp**, deliberately — nor does
`status` report its scan duration or `audit` its elapsed time. Two runs over an
unchanged library produce byte-identical output, so a nightly job can diff
today's against yesterday's and alert only when something really changed.

### Watching a run

A run is the one thing here that is not a state. `doctor`, `status` and `audit`
describe something that can be read in one shot; a full pipeline is an hour of
work, and the questions worth asking about it — *which step is running now, how
long did the remux take, what failed at 03:12* — cannot be answered by a file
that only appears once it is over. It also cannot print to stdout: stdout
belongs to the five tools the run launches. So the run reports to files, and it
reports twice.

```bash
python3 pipeline.py --source /path/to/movies \
    --events   /var/log/organize/run.jsonl \
    --summary-json /var/log/organize/run_summary.json
```

**`--events PATH`** appends one JSON object per line ([JSONL][jsonl]) as the run
happens — a tail-able stream, flushed line by line, so a dashboard sees a step
start rather than learning about it an hour later:

```bash
tail -f run.jsonl | jq -r 'select(.event == "step_finished") | "\(.step)\t\(.status)\t\(.seconds)s"'
jq -r 'select(.event == "step_finished" and .exit_code != 0) | .step' run.jsonl   # what broke
jq -s 'map(select(.event == "step_finished")) | sort_by(-.seconds) | .[0]' run.jsonl  # slowest step
```

```json
{"schema":1,"tool":"organize","version":"3.5.0","command":"run","event":"step_finished","time":"2026-09-06T03:12:44Z","step":"cleaner","status":"ran","exit_code":0,"seconds":812.4,"detail":""}
```

Every line carries the same envelope as the other commands, because a reader
tailing the file may only ever see one line of it, plus `event` and a UTC
`time`. The events, in order, are `run_started` (`library`, `steps`, `dry_run`,
`limit`, `nice`, `continue_on_error`), then per step a `step_started` (`step`,
`title`, `argv`) and a `step_finished` (`status`, `exit_code`, `seconds`,
`detail`), then `run_finished` (`completed`, `failed`, `not_run`, `exit_code`,
`elapsed_sec`). **Every step emits both**, including one skipped for a missing
prerequisite — a skipped step still reports the `argv` it would have run — so a
consumer can pair them without special cases. The file is append-only and never
rewritten: a run killed halfway still says exactly how far it got, and the
absence of `run_finished` is how you know it was killed.

**`--summary-json PATH`** writes the closing scorecard once, at the end — the
same numbers as the printed summary, as one document: `library`, `steps`,
`dry_run`, `elapsed_sec`, `completed`, `failed`, `not_run`, `exit_code`, and
`results`, one row per step with `step`, `title`, `status`, `exit_code`,
`seconds` and `detail`.

```bash
jq -r 'if .exit_code == 0 then "ok" else "FAILED: \(.failed | join(", "))" end' run_summary.json
```

Both flags are off by default and neither changes a byte of the human output.
Neither can fail a run, either: if the events file cannot be opened the stream
switches itself off with one note on stderr and the run continues, and a summary
that cannot be written is a warning, not a failure — by then the work is done,
and a read-only log directory is not a reason to report an hour of successful
remuxing as broken. This is also the one place a clock appears in this repo's
JSON, for the reason the others avoid it: a run *is* an occurrence, and when it
happened and how long it took are the point.

[jsonl]: https://jsonlines.org/

---

[← Back to the README](../README.md) · [Tool reference](tools.md) ·
[Configuration](configuration.md)
