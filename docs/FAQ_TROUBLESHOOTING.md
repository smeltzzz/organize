# Frequently Asked Questions & Troubleshooting

<div align="center">

[← Documentation index](../README.md#-documentation)

</div>

This document addresses common questions, edge cases, and troubleshooting steps when using the `organize` toolkit.

---

## 1. Hardlinks & Seeding

### Q: Why does `mkv_track_cleaner.py` report all my movies as `DEFERRED (STILL HARDLINKED)`?
**A:** This is a safety guarantee to protect active torrent seeding.
When `movie_standardizer.py` ingests a movie from qBittorrent, it creates a hardlink (`os.link`). The file in your organized library and the download file in `E:\torrents\final` share the exact same inode.
- Remuxing writes a new MKV file, which would break the link and briefly consume another full movie's worth of disk space.
- `mkv_track_cleaner.py` refuses to remux any file whose link count (`st_nlink`) is greater than 1.
- **How to resolve:**
  In qBittorrent, go to **Options → BitTorrent → Seeding Limits**. Set the action when the ratio/time limit is reached to **Remove torrent and its content**.
  *(Because the library file is a hardlink, deleting the source in `final` decrements the link count to 1 without losing any data in your library. On your next run, the cleaner will immediately pick it up!)*

### Q: Can my download folder and my movie library be on different hard drives?
**A:** **No.** Hardlinks (`os.link`) cannot span across multiple disk volumes or network mounts. Both directories must reside on the same drive letter (e.g. `E:`) or the same Linux mount/dataset.

---

## 2. Subtitles, OpenSubtitles & SubDL

### Q: Why does `subtitle_fetcher.py` skip some of my movies?
**A:** Check the following common causes:
1. **File size under 300 MB**: Movies under 300 MB are skipped by default (`--min-size 300`). Pass `--min-size 100` if you collect smaller rips.
2. **Not an MKV container**: Only canonical `.mkv` files are processed.
3. **Previous no-match or review state**: The fetcher records previous search outcomes in its log ledger so it doesn't waste daily quota re-searching movies with no match. Pass `--retry-review` to force a re-check.
4. **Daily quota reached**: The fetcher keeps OpenSubtitles and SubDL reservations separately. In free OpenSubtitles development mode, the default is 100 downloads per UTC day. [SubDL documents](https://subdl.com/developers) 2,000 searches and 50 downloads per day on its free tier; the fetcher keeps independent local UTC guards for both (`--subdl-search-daily-cap` and `--subdl-daily-cap`). A remaining provider can still be used after the other reaches its cap. Movies are deferred only once every configured usable provider is capped.

### Q: Should I configure both subtitle providers?
**A:** Yes, when possible. `OPENSUBTITLES_API_KEY` is preferred because its
moviehash can identify the byte-identical release and is the safest automatic
sync match. `SUBDL_API_KEY` adds coverage after OpenSubtitles has no safe
candidate. SubDL first uses its documented release-aware filename match; automatic
picks require the provider's `match` metadata to confirm the movie and a
`match_score` of at least `0.80`. Only a filename lookup with no usable
candidate may fall back to strict title/year matching; low-score or ambiguous results are held for
review. SubDL can run by itself but has no byte-identical moviehash match.
Keys are read only from environment variables:
```bash
export OPENSUBTITLES_API_KEY="your-opensubtitles-key"
export SUBDL_API_KEY="your-subdl-key"
```

### Q: How do I fix an `INVALID_SIDECAR` finding from `library_auditor.py`?
**A:** An `INVALID_SIDECAR` finding means a `.eng.srt` file exists next to the movie, but it is empty, corrupt, truncated, or contains provider error text (such as an HTML 429 page).
Because no tool will overwrite an existing sidecar, this dead end blocks downstream steps.
- **Fix:** Simply delete the corrupted `.eng.srt` file and re-run:
  ```bash
  python organize.py subtitles
  ```

---

## 3. Remuxing & Disk Space

### Q: Why does `mkv_track_cleaner.py` say "not enough free disk space to remux"?
**A:** `mkvmerge` cannot rewrite a file in place. It must write a temporary sibling file (`temp_clean_<token>__Movie.mkv`) while reading the original.
To prevent running out of disk space mid-remux, the cleaner checks available free space and requires at least:
$$\text{Free Space Needed} = (\text{Movie Size} \times 1.02) + 64\text{ MiB}$$
If your drive does not have enough free room, the cleaner refuses to remux that movie and leaves the original completely untouched. Free up disk space or process movies in smaller batches.

### Q: Can I run this without remuxing?
**A:** Yes! If you want to keep original multi-audio containers and skip the remux step:
```bash
python organize.py run --steps fetcher,10bit,auditor
```

---

## 4. HandBrake & 10-Bit Transcoding

### Q: Can `10bit.py` automatically encode my movies in HandBrake?
**A:** No. `10bit.py` is an **inspector and action-queue planner**, not a video encoder.
It categorizes files based on their bit-depth and color metadata:
- Files marked `QUEUE FOR HANDBRAKE` are confirmed 8-bit SDR. You can feed these into HandBrake or a batch transcoder (like Tdarr or FileFlows) using an H.265 10-bit or AV1 10-bit preset.
- Files marked `SKIP (already 10-bit)` or `KEEP (Native HDR)` should **not** be transcoded, protecting dynamic HDR metadata and preventing generational quality loss.

---

## 5. TV Shows & Anime

### Q: Can I use `movie_standardizer.py` for TV shows?
**A:** No. `movie_standardizer.py` is strictly designed for movies (`Title (Year)/Title (Year).mkv`).
TV episode names (`S01E02`, `1x04`, etc.) are detected specifically to **exclude** them so TV downloads are never accidentally ingested into your movie library. For TV show management, use dedicated tools like Sonarr.
*(If a movie is misdetected as TV, use the `--allow-tv` escape hatch).*

---

<div align="center">

[← Documentation index](../README.md#-documentation) · [Configuration reference →](CONFIGURATION_REFERENCE.md) · [Architecture & safety →](ARCHITECTURE_SAFETY.md)

</div>
