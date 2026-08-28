# Windows 11 setup for `E:\torrents`

A runbook for using this toolkit on a Windows 11 machine where the torrent data
lives on `E:\`. Every path below is the value the scripts already default to, so
if your layout matches, **you never pass a path flag.**

---

## 1. The folder layout the scripts expect

Create these on `E:` (the last five are created automatically on first run —
listed so you know where output goes):

```
E:\torrents\
├── final\                      ← qBittorrent's default save path (downloads land here)
├── final_organized\            ← THE JELLYFIN LIBRARY (point Jellyfin/Plex here)
│   └── Dune (2021)\
│       ├── Dune (2021).mkv
│       └── Dune (2021).en.srt
├── movie_standardizer\         ← movie_standardizer.log / _report.txt
├── subtitle_fetcher\           ← subtitle_fetcher.log (also the UTC quota ledger) / _report.txt
├── mkv_track_cleaner\          ← mkv_track_cleaner.log / _report.txt
├── 10bit\                      ← 10bit.log / _report.txt
└── library_auditor\            ← library_auditor.log / _report.txt
```

Put the seven `.py` files anywhere you like — `C:\Tools\organize\` is typical.
They must stay **in the same folder as each other**, because every script does
`from common import ...`. Reports and logs are always written *outside* the
library so Jellyfin never indexes them.

## 2. Install prerequisites

| What | Needed by | Notes |
| --- | --- | --- |
| Python 3.11+ | everything | python.org installer → tick **Add python.exe to PATH** |
| MKVToolNix | `mkv_track_cleaner.py` | also found at `C:\Program Files\MKVToolNix\mkvmerge.exe` automatically |
| FFmpeg | `10bit.py`, and `movie_standardizer.py` only when judging a duplicate | `winget install Gyan.FFmpeg`, or drop `ffprobe.exe` at `C:\ffmpeg\bin\ffprobe.exe` |
| OpenSubtitles API key | `subtitle_fetcher.py` | free: https://www.opensubtitles.com/en/consumers |

No `pip install` — the runtime is stdlib-only (`requirements.txt` is empty on
purpose).

Set the API key once, as a **user** environment variable so qBittorrent and
PowerShell both see it:

```powershell
[Environment]::SetEnvironmentVariable("OPENSUBTITLES_API_KEY", "your-key-here", "User")
```

Then **restart qBittorrent and any open terminal** — a process only reads the
environment it was started with.

## 3. Configure qBittorrent

1. **Options → Downloads → Default Save Path** → `E:\torrents\final`
   This matters more than it looks: placement is **hardlink-only** (`os.link()`,
   no copy/move fallback), so the download folder and `final_organized` must be
   on the same NTFS volume. If downloads sit on `C:`, the standardizer refuses
   outright:
   `--source and --target must be on the same filesystem for hardlink-only placement` (exit 2).

2. **Options → Downloads → Run external program on torrent completion** →

   ```
   python "C:\Tools\organize\movie_standardizer.py" "%F"
   ```

   (Use the full path to `python.exe` if `python` is not on PATH for the account
   qBittorrent runs as.) The script is imported-safe from any working directory
   because Python puts the script's own folder on `sys.path`.

3. **Options → BitTorrent → Seeding Limits** → set *"When ratio reaches"* /
   *"When seeding time reaches"* to **Remove torrent and its content**.
   This is the single most important qBittorrent setting for this pipeline.
   The default action only **pauses** the torrent and leaves the file behind,
   so the hardlink count never drops and `mkv_track_cleaner.py` defers that
   movie *forever* — there is deliberately no override flag. Deleting the source
   is safe precisely because it is a hardlink: your library copy keeps the data.

## 4. First run (do the dry runs)

Open PowerShell in the script folder and step through:

```powershell
cd C:\Tools\organize

py movie_standardizer.py --dry-run     # what would get hardlinked from E:\torrents\final
py movie_standardizer.py               # do it for real
py pipeline.py --list-steps            # which of the 4 manual steps can run on this box
py pipeline.py --dry-run               # print the exact commands, run nothing
py pipeline.py                         # fetcher → cleaner → 10bit → auditor
```

Then point Jellyfin at `E:\torrents\final_organized` as a **Movies** library.

## 5. The order, and why it is fixed

```
torrent completes ──▶ movie_standardizer.py    (qBittorrent hook, automatic)
         ──▶ subtitle_fetcher.py           (fetch SRT while the MKV bytes are pristine)
         ──▶ mkv_track_cleaner.py          (remux; validated .en.srt becomes the sole subtitle)
         ──▶ 10bit.py                      (which movies to re-encode to 10-bit)
         ──▶ library_auditor.py            (read-only health check)
```

`subtitle_fetcher.py` must precede `mkv_track_cleaner.py`. It matches on the
OpenSubtitles **moviehash** (file size + first/last 64 KiB) with
`moviehash_match=only`. A remux rewrites those bytes, so a remuxed file can
never reproduce its release hash and silently drops to the much weaker
title/year search. `pipeline.py` exists so you cannot get this wrong.

## 6. Things that will surprise you

**Movies under 300 MB are skipped.** `movie_standardizer.py` and
`subtitle_fetcher.py` both default to `MIN_MOVIE_SIZE_MB = 300` (`10bit.py` and
`mkv_track_cleaner.py` default to 0). A 238 MB file logs
`Skipping small file (238.4 MB)` and is left in `final`. Lower it with
`--min-size 100` if you keep older/small movies.

**Only `.mkv` is organized.** MP4/AVI releases are skipped, never renamed with a
fake extension. There is no transcoding anywhere in this toolkit.

**Multipart and disc releases are skipped** (`-cd1`/`-cd2`, `BDMV`, `VIDEO_TS`)
— canonical output is one complete MKV per folder.

**The cleaner demands the canonical layout.** It skips anything that is not
`Title (Year)/Title (Year).mkv` with exactly one MKV in the folder — e.g.
`noncanonical layout: MKV stem does not match its movie-folder name`. Run the
standardizer first.

**A remux needs free space.** The cleaner requires
`size × 1.02 + 64 MiB` free on the destination volume and refuses otherwise:
`not enough free disk space to remux (need X, have Y). Original file left
untouched.` There is no in-place mode — `mkvmerge` cannot rewrite a container,
and `mkvpropedit` can only edit metadata, not remove tracks. If disk churn
matters more than cleanup, skip that step:
`py pipeline.py --steps fetcher,10bit,auditor`.

**A subtitle sidecar can be present and worthless.** An empty or truncated
`.en.srt` is reported by `library_auditor.py` as `INVALID_SIDECAR` with the
reason. Nothing in the pipeline repairs it on its own — delete the bad file and
re-run `subtitle_fetcher.py`.

**`subtitle_fetcher.py --dry-run` still needs the API key.** It exits 2 with
`Configuration error: an OpenSubtitles API key is required` if the variable is
missing. Every other tool's `--dry-run` works with nothing installed.

**The fetcher is quota-limited per UTC day:** 100 download requests in the
default `development-anonymous` mode, 20 in `--auth-mode user`. The
append-only log at `E:\torrents\subtitle_fetcher\subtitle_fetcher.log` *is* the
durable ledger — don't delete it or you lose your quota accounting.

## 7. Everyday commands

```powershell
py pipeline.py                     # the whole manual sweep
py pipeline.py --steps auditor     # read-only health check only
py pipeline.py --nice              # lower remux priority so Jellyfin keeps streaming
py movie_standardizer.py           # re-scan E:\torrents\final by hand
py library_auditor.py              # always safe, never writes media
```

Useful escapes: `--limit 5` (process only 5 items), `--continue-on-error`
(pipeline keeps going after a failed step), `--retry-review` (reconsider movies
previously held for manual subtitle review).

### Reading what was left behind

`E:\torrents\final` keeps anything the standardizer declines — a non-MKV
release, a multipart or disc set, or a movie under the 300 MB minimum. These
are listed at the end of `E:\torrents\movie_standardizer\movie_standardizer_report.txt`
under `ITEMS LEFT IN SOURCE`, each with its reason. Nothing cleans them up for
you; that section is the to-do list.

### A scheduled health gate

The auditor exits 0 by default, so a scheduled run can silently pass over a
broken library. Give it a gate:

```powershell
py library_auditor.py --fail-on-defects
```

Exit 1 means a real layout defect. A missing subtitle alone still exits 0,
because a movie that has only just been standardized legitimately has no sidecar
yet. Use `--fail-on-findings` if you want missing sidecars to fail too.

## 8. Verifying your install

```powershell
py pipeline.py --list-steps
```

A fully provisioned box prints:

```
  fetcher   Fetch English SRT subtitles      ready
  cleaner   Clean MKV tracks (remux)         ready
  10bit     Check 8-bit vs 10-bit / HDR      ready
  auditor   Audit library layout             ready
```

Anything else prints `blocked: <exact reason>`. Missing prerequisites are
**skipped with a reason, never a crash.**

## 9. Tests

```powershell
py -m unittest discover -s tests -p "test_*.py"
py movie_standardizer.py --self-test
```

96 unit tests plus one `--self-test` per script, all offline — no media, no
binaries, no API key. (`run_tests.sh` is the bash equivalent.)
