# ==============================================================================
# Organize — Dockerfile
# Fully containerized Jellyfin Media Management with MKVToolNix & FFmpeg built-in
# ==============================================================================
FROM python:3.11-slim-bookworm

# Avoid interactive prompts during package install
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install system dependencies: MKVToolNix (mkvmerge) and FFmpeg (ffprobe)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    mkvtoolnix \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Default application directory
WORKDIR /app

# Copy toolkit files
COPY organize.py \
     10bit.py \
     common.py \
     library_auditor.py \
     mkv_track_cleaner.py \
     movie_standardizer.py \
     pipeline.py \
     subtitle_fetcher.py \
     requirements.txt \
     /app/

# Make scripts executable
RUN chmod +x /app/*.py

# Volume definitions:
# /downloads  -> Finished torrent downloads (qBittorrent save directory)
# /movies     -> Canonical Jellyfin movie library (Title (Year)/Title (Year).mkv)
# /config     -> Persistent logs, reports, and probe cache
VOLUME ["/downloads", "/movies", "/config"]

# Default environment variables
ENV MOVIE_STD_SOURCE="/downloads" \
    MOVIE_STD_TARGET="/movies" \
    MOVIE_STD_LOG="/config/movie_standardizer.log" \
    MOVIE_STD_REPORT="/config/movie_standardizer_report.txt"

ENTRYPOINT ["python3", "/app/organize.py"]
CMD ["doctor"]
