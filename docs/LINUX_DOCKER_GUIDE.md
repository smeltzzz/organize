# Linux & Docker Deployment Guide

While the default paths in `organize` cater to Windows conventions (`E:\torrents\...`), the entire codebase is cross-platform and extensively tested on Linux (`ext4`, `zfs`, `btrfs`, `xfs`) and Docker.

This guide details setting up `organize` on Debian/Ubuntu, Arch, Fedora, Docker Compose, Unraid, and TrueNAS SCALE.

---

## 1. Prerequisites Installation

### Debian / Ubuntu / Linux Mint
```bash
sudo apt update
sudo apt install -y python3 ffmpeg mkvtoolnix
```

### Arch Linux / Manjaro
```bash
sudo pacman -S python ffmpeg mkvtoolnix-cli
```

### Fedora / RHEL / Rocky Linux
```bash
sudo dnf install -y python3 ffmpeg mkvtoolnix
```

---

## 2. Directory Layout & Hardlink Invariant

> [!IMPORTANT]
> **Hardlink Placement Requires the Same Mount/Volume!**
> `movie_standardizer.py` uses `os.link()` to place movies into your library. Hardlinks **cannot** cross filesystem mounts or ZFS datasets. Both your torrent download folder and your organized library must reside on the same filesystem.

Recommended directory layout:

```
/data/media/
├── torrents/
│   └── final/                  # qBittorrent download directory
├── movies/                     # Jellyfin library root (Title (Year)/Title (Year).mkv)
│   └── Dune (2021)/
│       ├── Dune (2021).mkv
│       └── Dune (2021).en.srt
└── organize_logs/              # Logs, reports, and probe caches
```

Verify your setup with the built-in diagnostic tool:

```bash
python3 organize.py doctor --source /data/media/torrents/final --target /data/media/movies
```

---

## 3. Configuring Environment Variables

Create `/etc/organize.env` or export in your shell profile (`~/.bashrc`):

```bash
export OPENSUBTITLES_API_KEY="your-opensubtitles-consumer-api-key"
export MOVIE_STD_SOURCE="/data/media/torrents/final"
export MOVIE_STD_TARGET="/data/media/movies"
export MOVIE_STD_LOG="/data/media/organize_logs/movie_standardizer.log"
export MOVIE_STD_REPORT="/data/media/organize_logs/movie_standardizer_report.txt"
```

---

## 4. Configuring qBittorrent on Linux

1. In qBittorrent Web UI / GUI: **Options → Downloads → Default Save Path** → `/data/media/torrents/final`
2. **Options → Downloads → Run external program on torrent completion**:
   ```bash
   /opt/organize/organize.sh standardize "%F"
   ```
3. **Options → BitTorrent → Seeding Limits**:
   Set *"When ratio reaches"* or *"When seeding time reaches"* to **Remove torrent and its content**.
   *(Because the library file is a hardlink, removing the download source entry leaves the library copy 100% intact while freeing the cleaner to remux it.)*

---

## 5. Automated Nightly Maintenance (Cron or Systemd)

### Option A: Cron Job

Edit your user crontab (`crontab -e`):

```cron
# Run the Organize maintenance pipeline every night at 3:00 AM
0 3 * * * /opt/organize/organize.sh run --nice >> /data/media/organize_logs/cron.log 2>&1
```

### Option B: Systemd Service & Timer

Create `/etc/systemd/system/organize.service`:

```ini
[Unit]
Description=Organize Jellyfin Media Maintenance Pipeline
After=network.target

[Service]
Type=oneshot
User=media
Group=media
EnvironmentFile=/etc/organize.env
ExecStart=/opt/organize/organize.sh run --nice
```

Create `/etc/systemd/system/organize.timer`:

```ini
[Unit]
Description=Run Organize Jellyfin Pipeline Nightly

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable and start the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now organize.timer
```

---

## 6. Docker & Docker Compose Setup

Run `organize` containerized without installing Python, FFmpeg, or MKVToolNix on the host.

### Docker Compose (`docker-compose.yml`)

```yaml
services:
  organize:
    build:
      context: .
      dockerfile: Dockerfile
    image: organize:latest
    container_name: organize
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=UTC
      - OPENSUBTITLES_API_KEY=${OPENSUBTITLES_API_KEY}
      - MOVIE_STD_SOURCE=/media/torrents/final
      - MOVIE_STD_TARGET=/media/movies
    volumes:
      # Mount the common root so hardlinks work inside the container
      - /data/media:/media
      - ./config:/config
    command: ["doctor"]
```

Run commands inside Docker:

```bash
# Run system doctor
docker compose run --rm organize doctor

# Preview pipeline
docker compose run --rm organize run --dry-run

# Run nightly pipeline
docker compose run --rm organize run --nice
```

---

## 7. Unraid & TrueNAS SCALE Setup

### Unraid
1. Place the repo in `/boot/config/plugins/organize` or on your appdata share `/mnt/user/appdata/organize`.
2. Install the **User Scripts** plugin from Community Applications.
3. Create a new script named `Jellyfin Movie Organizer`:
   ```bash
   #!/bin/bash
   export OPENSUBTITLES_API_KEY="your-key"
   /mnt/user/appdata/organize/organize.sh run --nice
   ```
4. Set schedule to **Custom (Daily at 03:00)**.

### TrueNAS SCALE
1. In TrueNAS Apps or Docker Compose, deploy the container with your ZFS pool mounted under a single root mount (e.g. `/mnt/tank/media:/media`) to preserve hardlink capability.
2. In TrueNAS System Settings → Advanced → **Cron Jobs**, add a job to execute `docker exec organize python /app/organize.py run --nice`.
