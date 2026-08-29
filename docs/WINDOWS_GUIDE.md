# Windows 11 Deployment & Automation Guide

<div align="center">

[← Back to the documentation index](../README.md#-documentation)

</div>

A complete runbook for deploying, automating, and running the `organize`
toolkit on Windows 10/11 where media lives on `E:\torrents\...`. Every path
below is the value the scripts already default to, so if your layout matches,
**you never pass a path flag.**

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
│       └── Dune (2021).eng.srt
├── movie_standardizer\         ← movie_standardizer.log / _report.txt
├── subtitle_fetcher\           ← subtitle_fetcher.log (also the UTC quota ledger) / _report.txt
├── mkv_track_cleaner\          ← mkv_track_cleaner.log / _report.txt
├── 10bit\                      ← 10bit.log / _report.txt
└── library_auditor\            ← library_auditor.log / _report.txt
```

Put the repository in a dedicated tools directory such as `C:\Tools\organize\`.
All toolkit files must stay **in the same folder as each other**, because every
script does `from common import ...`. Reports and logs are always written
*outside* the library so Jellyfin never indexes them.

## 2. Install prerequisites

Open PowerShell as Administrator:

```powershell
# Python 3.11+ (if not already installed)
winget install Python.Python.3.11 --accept-package-agreements --accept-source-agreements

# FFmpeg (provides ffprobe.exe) — used by 10bit.py and upgrade checks
winget install Gyan.FFmpeg --accept-package-agreements --accept-source-agreements

# MKVToolNix (provides mkvmerge.exe) — used by mkv_track_cleaner.py
winget install MKVToolNix.MKVToolNix --accept-package-agreements --accept-source-agreements
```

Restart your terminal so the new PATH entries take effect.

| What | Needed by | Notes |
| :--- | :--- | :--- |
| Python 3.11+ | everything | Tick **Add python.exe to PATH** in the installer |
| MKVToolNix | `mkv_track_cleaner.py` | Also auto-found at `C:\Program Files\MKVToolNix\mkvmerge.exe` |
| FFmpeg | `10bit.py`, upgrade checks | Or drop `ffprobe.exe` at `C:\ffmpeg\bin\ffprobe.exe` |
| OpenSubtitles API key | `subtitle_fetcher.py` | Free: <https://www.opensubtitles.com/en/consumers> |

No `pip install` — the runtime is stdlib-only (`requirements.txt` is empty on
purpose).

## 3. Set the OpenSubtitles API key

Set it once as a **user** environment variable so qBittorrent and PowerShell
both see it, then **restart qBittorrent and any open terminal** — a process
only reads the environment it was started with:

```powershell
[Environment]::SetEnvironmentVariable("OPENSUBTITLES_API_KEY", "your-key-here", "User")
```

## 4. Run the system doctor

```powershell
cd C:\Tools\organize
py organize.py doctor
```

All items should report green `✔` (yellow `⚠` means an optional step will be
skipped, with the fix printed).

## 5. Configure qBittorrent

1. **Options → Downloads → Default Save Path** → `E:\torrents\final`
   This matters more than it looks: placement is **hardlink-only** (`os.link()`,
   no copy/move fallback), so the download folder and `final_organized` must be
   on the same NTFS volume. If downloads sit on `C:`, the standardizer refuses
   outright with
   `--source and --target must be on the same filesystem for hardlink-only placement` (exit 2).

2. **Options → Downloads → Run external program on torrent completion** →

   ```cmd
   py "C:\Tools\organize\organize.py" standardize "%F"
   ```

   (Use the full path to `python.exe` if `py` is not on PATH for the account
   qBittorrent runs as. The hook also accepts the older `movie_standardizer.py "%F"`
   form and the `"%D" "%N"` pair.)

3. **Options → BitTorrent → Seeding Limits** → set *"When ratio reaches"* /
   *"When seeding time reaches"* to **Remove torrent and its content**.
   This is the single most important qBittorrent setting for this pipeline:
   the default action only **pauses** the torrent and leaves the file behind,
   so the hardlink count never drops and `mkv_track_cleaner.py` defers that
   movie *forever* — there is deliberately no override flag. Deleting the
   source is safe precisely because it is a hardlink: your library copy keeps
   the data.

## 6. First run — ramp up gradually

Start read-only, then try a single movie you do not mind losing:

```powershell
py pipeline.py --list-steps            # which of the 4 manual steps can run on this box
py library_auditor.py                  # read-only health check of E:\torrents\final_organized
py movie_standardizer.py --dry-run     # what would get hardlinked from E:\torrents\final
py movie_standardizer.py               # do it for real
py pipeline.py --steps fetcher,10bit,auditor --dry-run   # preview the safe steps
py pipeline.py --steps fetcher,10bit,auditor             # run them live
py mkv_track_cleaner.py --limit 1 --dry-run              # preview ONE remux
py mkv_track_cleaner.py --limit 1                       # remux ONE movie
```

After the first remux, play the movie in Jellyfin, then read the
`DEFERRED (STILL HARDLINKED)` and `ERRORS ENCOUNTERED` sections of the report
at `E:\torrents\mkv_track_cleaner\mkv_track_cleaner_report.txt`. Only then
remove `--limit 1` and run the full sweep: `py pipeline.py`.

Finally, point Jellyfin at `E:\torrents\final_organized` as a **Movies** library.

## 7. Automate nightly maintenance

### Option A — one-line PowerShell (elevated)

```powershell
$action    = New-ScheduledTaskAction -Execute "py.exe" -Argument "C:\Tools\organize\organize.py run --nice" -WorkingDirectory "C:\Tools\organize"
$trigger   = New-ScheduledTaskTrigger -Daily -At 3:30AM
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "Jellyfin Library Maintenance" -Action $action -Trigger $trigger -Settings $settings -Description "Runs the Organize Jellyfin maintenance pipeline nightly"
```

### Option B — Task Scheduler GUI

1. Open **Task Scheduler** (`taskschd.msc`) → **Create Basic Task…** → name it
   `Jellyfin Library Maintenance`.
2. Trigger: **Daily** at `03:30 AM`.
3. Action: **Start a program**
   - Program/script: `py.exe`
   - Add arguments: `C:\Tools\organize\organize.py run --nice`
   - Start in: `C:\Tools\organize\`
4. Finish.

### A scheduled health gate

The auditor exits 0 by default, so a scheduled run can silently pass over a
broken library. Give it a gate:

```powershell
py library_auditor.py --fail-on-defects
```

Exit 1 means a real layout defect. A missing subtitle alone still exits 0,
because a movie that has only just been standardized legitimately has no
sidecar yet. Use `--fail-on-findings` if missing sidecars should fail too.

## 8. Things that will surprise you

- **Movies under 300 MB are skipped.** `movie_standardizer.py` and
  `subtitle_fetcher.py` both default to a 300 MB minimum (`10bit.py` and
  `mkv_track_cleaner.py` default to 0). A 238 MB file logs
  `Skipping small file (238.4 MB)` and is left in `final`. Lower it with
  `--min-size 100` if you keep older or smaller releases.
- **Only `.mkv` is organized.** MP4/AVI releases are skipped, never renamed with
  a fake extension. There is no transcoding anywhere in this toolkit.
- **Multipart and disc releases are skipped** (`-cd1`/`-cd2`, `BDMV`,
  `VIDEO_TS`) — canonical output is one complete MKV per folder.
- **The cleaner demands the canonical layout.** It skips anything that is not
  `Title (Year)/Title (Year).mkv` with exactly one MKV in the folder. Run the
  standardizer first.
- **A remux needs free space.** The cleaner requires `size × 1.02 + 64 MiB`
  free on the destination volume and refuses otherwise: *not enough free disk
  space to remux. Original file left untouched.* There is no in-place mode —
  `mkvmerge` cannot rewrite a container, and `mkvpropedit` can only edit
  metadata, not remove tracks. If disk churn matters more than cleanup, skip
  the step: `py pipeline.py --steps fetcher,10bit,auditor`.
- **A subtitle sidecar can be present and worthless.** An empty or truncated
  `.eng.srt` is reported by `library_auditor.py` as `INVALID_SIDECAR` with the
  reason. Nothing in the pipeline repairs it on its own — delete the bad file
  and re-run `subtitle_fetcher.py`.
- **`subtitle_fetcher.py --dry-run` still needs the API key.** It exits 2 with
  `Configuration error: an OpenSubtitles API key is required` if the variable
  is missing. Every other tool's `--dry-run` works with nothing installed.
- **The fetcher is quota-limited per UTC day:** 100 download requests in the
  default `development-anonymous` mode, 20 with `--auth-mode user`. The
  append-only log at `E:\torrents\subtitle_fetcher\subtitle_fetcher.log` *is*
  the durable ledger — don't delete it or you lose your quota accounting.

## 9. Everyday commands

```powershell
py organize.py doctor               # is everything still healthy?
py organize.py run                  # the whole manual sweep (steps 2 → 5)
py organize.py run --nice           # lower remux priority so Jellyfin keeps streaming
py organize.py audit                # always safe, never writes media
py organize.py standardize          # re-scan E:\torrents\final by hand
```

Useful escapes: `--limit 5` (process only 5 items), `--continue-on-error`
(pipeline keeps going after a failed step; this is the default), `--retry-review`
(reconsider movies previously held for manual subtitle review).

**Reading what was left behind** — `E:\torrents\final` keeps anything the
standardizer declines: a non-MKV release, a multipart or disc set, or a movie
under the 300 MB minimum. These are listed at the end of
`E:\torrents\movie_standardizer\movie_standardizer_report.txt` under
`ITEMS LEFT IN SOURCE`, each with its reason. Nothing cleans them up for you;
that section is the to-do list.

**Verifying your install** — `py pipeline.py --list-steps` on a fully
provisioned box prints:

```
  fetcher   Fetch English SRT subtitles      ready
  cleaner   Clean MKV tracks (remux)         ready
  10bit     Check 8-bit vs 10-bit / HDR      ready
  auditor   Audit library layout             ready
```

Anything else prints `blocked: <exact reason>`. Missing prerequisites are
**skipped with a reason, never a crash.**

## 10. Tests

```powershell
py -m unittest discover -s tests -p "test_*.py"
py organize.py test --unit
```

208 unit tests plus one `--self-test` per script, all offline — no media, no
binaries, no API key. (`run_tests.sh` is the bash equivalent.)

---

<div align="center">

[← Documentation index](../README.md#-documentation) · [Configuration reference →](CONFIGURATION_REFERENCE.md) · [FAQ & troubleshooting →](FAQ_TROUBLESHOOTING.md)

</div>
