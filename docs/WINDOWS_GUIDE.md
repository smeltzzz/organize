# Windows 11 Deployment & Automation Guide

A comprehensive runbook for deploying, automating, and running the `organize` toolkit on Windows 10/11 where media lives on `E:\torrents\...`.

---

## 1. Directory Structure

The scripts natively default to the following NTFS layout on `E:`:

```
E:\torrents\
├── final\                      ← qBittorrent completed downloads
├── final_organized\            ← THE JELLYFIN MOVIE LIBRARY (Point Jellyfin here)
│   └── Dune (2021)\
│       ├── Dune (2021).mkv
│       └── Dune (2021).en.srt
├── movie_standardizer\         ← Execution log & replaceable report
├── subtitle_fetcher\           ← Execution log (quota ledger) & replaceable report
├── mkv_track_cleaner\          ← Execution log, report & probe cache
├── 10bit\                      ← Execution log, report & probe cache
└── library_auditor\            ← Execution log & replaceable report
```

Place the repository scripts in a dedicated tools directory, such as `C:\Tools\organize\`.

---

## 2. Installing Prerequisites

Open PowerShell as Administrator:

```powershell
# 1. Install Python 3.11+ (if not already installed)
winget install Python.Python.3.11 --accept-package-agreements --accept-source-agreements

# 2. Install FFmpeg (provides ffprobe.exe)
winget install Gyan.FFmpeg --accept-package-agreements --accept-source-agreements

# 3. Install MKVToolNix (provides mkvmerge.exe)
winget install MKVToolNix.MKVToolNix --accept-package-agreements --accept-source-agreements
```

Restart your terminal so the new PATH environment variables take effect.

---

## 3. Configuring OpenSubtitles API Key

Sign up for a free consumer API key at [OpenSubtitles.com](https://www.opensubtitles.com/en/consumers).

Set the API key as a persistent Windows User environment variable:

```powershell
[Environment]::SetEnvironmentVariable("OPENSUBTITLES_API_KEY", "your_api_key_here", "User")
```

---

## 4. Run System Doctor

Verify everything is detected and configured properly:

```powershell
cd C:\Tools\organize
py organize.py doctor
```

All items should report green `✔` or yellow `⚠`.

---

## 5. Configuring qBittorrent

1. **Default Save Path**:
   Options → Downloads → Default Save Path → `E:\torrents\final`
   > [!WARNING]
   > `final` and `final_organized` must be on the same drive letter (`E:`) for hardlinks to work.

2. **Completion Hook**:
   Options → Downloads → Run external program on torrent completion:
   ```cmd
   py "C:\Tools\organize\organize.py" standardize "%F"
   ```

3. **Seeding Limits (Crucial!)**:
   Options → BitTorrent → Seeding Limits → set *"When ratio reaches"* or *"When seeding time reaches"* to **Remove torrent and its content**.
   - Why? `mkv_track_cleaner.py` defers any movie with more than 1 hardlink so it never breaks an active torrent. When qBittorrent deletes the source file upon reaching the seed limit, the link count drops to 1, and the movie is immediately eligible for cleaning.

---

## 6. Automating Nightly Maintenance with Task Scheduler

To keep your library clean without manual intervention, create a scheduled task to run `organize.py run --nice` nightly.

### Option A: One-line PowerShell Setup

Run in an elevated PowerShell:

```powershell
$action = New-ScheduledTaskAction -Execute "py.exe" -Argument "C:\Tools\organize\organize.py run --nice" -WorkingDirectory "C:\Tools\organize"
$trigger = New-ScheduledTaskTrigger -Daily -At 3:30AM
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "Jellyfin Library Maintenance" -Action $action -Trigger $trigger -Settings $settings -Description "Runs Organize Jellyfin maintenance pipeline nightly"
```

### Option B: Windows Task Scheduler GUI

1. Open **Task Scheduler** (`taskschd.msc`).
2. Click **Create Basic Task...** → Name: `Jellyfin Library Maintenance`.
3. Trigger: **Daily** at `03:30 AM`.
4. Action: **Start a program**.
   - Program/script: `py.exe`
   - Add arguments: `C:\Tools\organize\organize.py run --nice`
   - Start in: `C:\Tools\organize\`
5. Finish.
