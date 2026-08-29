# The Jellyfin Direct Play Philosophy

<div align="center">

[← Documentation index](../README.md#-documentation)

</div>

Why does Jellyfin transcode, and why does this toolkit make such a profound difference in server performance and visual fidelity?

This document outlines the technical underpinnings of **Direct Play vs. Transcoding** and explains how each component in `organize` is purpose-built to eliminate unnecessary CPU/GPU work on your media server.

---

## 1. Direct Play vs. Direct Stream vs. Transcode

When a Jellyfin client (such as an Apple TV, Roku, Amazon Fire TV, LG webOS, Android TV, or Web Browser) requests media, Jellyfin selects one of three playback modes:

| Mode | What Happens | Server Resource Cost | Playback Quality |
| :--- | :--- | :--- | :--- |
| **Direct Play** | The client reads the container, video, audio, and subtitle streams natively without server intervention. | **~0% CPU / 0% GPU** | 100% Bit-for-bit pristine original |
| **Direct Stream** | The container is remuxed on the fly (e.g. MKV to MP4) or audio is transcoded, but the video stream is untouched. | Low CPU (~5-15%) | Video preserved; possible audio latency |
| **Video Transcode** | The server fully decodes the video and re-encodes it in real-time (often burning in subtitles). | **Massive CPU / 80-100% GPU** | Generation loss, tone-map banding, battery drain |

---

## 2. The Subtitle Transcoding Problem

The single most common cause of unexpected video transcoding in Jellyfin is **subtitles**.

### Bitmap & Styled Subtitles (PGS, VobSub, ASS/SSA)
- **PGS (Blu-ray)** and **VobSub (DVD)** are not text: they are bitmap images stored inside the container.
- Most web browsers, streaming sticks, and Smart TV apps lack hardware graphics pipelines to render bitmap subtitles over video frames.
- When a client cannot render PGS or complex stylized ASS subtitles, Jellyfin **forces a video transcode** to burn the subtitle images into the video frames on the fly.
- Even if your server has an Intel QuickSync or Nvidia NVENC GPU, tone-mapping HDR while burning in subtitles easily overwhelms hardware encoders and introduces stutter.

### The Solution: External UTF-8 `.en.srt`
- Plain text SubRip (`.srt`) encoded in UTF-8 is universally supported by **100% of modern Jellyfin clients**.
- Jellyfin serves external `.en.srt` files directly to the client as lightweight text packets over HTTP/WebSocket. The client renders the font natively using its own OS display layer.
- **Zero server transcoding required.**

> [!NOTE]
> That is why `subtitle_fetcher.py` exclusively downloads UTF-8 `.en.srt` files, and `mkv_track_cleaner.py` strips embedded PGS/VobSub tracks once a verified `.en.srt` sidecar is present.

---

## 3. The Audio Bloat Problem

Movie torrents and remuxes routinely bundle redundant audio tracks:
- Director / Cast Commentary tracks (often marked as default or stream 1).
- Descriptive Video Services (DVS / visual impaired).
- Multiple foreign language dubs (French, Spanish, German, Italian, Russian).
- 7.1 TrueHD / DTS-HD MA tracks without fallback 5.1/2.0 compatibility.

If a client attempts to play a multi-channel track it cannot decode, Jellyfin must transcode audio on the fly. Furthermore, high-bandwidth uncompressed audio streams consume substantial network bandwidth over local Wi-Fi.

### The Solution: Track Cleaner Remux
`mkv_track_cleaner.py` analyzes the track header layout with `mkvmerge`:
1. Selects the single highest quality primary English audio track (Dolby Atmos / TrueHD / DTS-HD MA / 5.1).
2. Explicitly purges commentary, director, and DVS tracks.
3. Purges unneeded foreign dub tracks.
4. Preserves chapters and metadata without re-encoding the video.

---

## 4. 8-Bit vs. 10-Bit Color Fidelity

### Color Banding in 8-bit SDR
Standard 8-bit video provides 256 discrete levels per color channel ($2^8 = 256$). In dark scenes, skies, or subtle gradients (shadows, fog, sunsets), 8-bit video frequently produces visible "color banding" or posterization steps.

### Why 10-Bit HEVC/AV1 Eliminates Banding
10-bit video provides 1,024 discrete levels per color channel ($2^{10} = 1024$) — four times the precision of 8-bit. Even for SDR content, encoding in 10-bit H.265 (HEVC) or AV1 dramatically reduces color banding and allows compression algorithms to achieve 20-40% smaller file sizes at equal or superior perceptual quality.

### Why `10bit.py` Distinguishes SDR from Native HDR
- **8-bit SDR**: Prime candidate for HandBrake 10-bit re-encode (`STATUS_QUEUE`).
- **Native HDR (HDR10, HDR10+, Dolby Vision, HLG)**: Must **NOT** be blindly re-encoded with default HandBrake presets! HandBrake will strip dynamic HDR metadata or inadvertently tone-map HDR down to SDR unless painstakingly configured. `10bit.py` marks these as `STATUS_SKIP_HDR` (KEEP).
- **8-bit tagged HDR / Ambiguous**: Flawed or mis-tagged releases that must be inspected manually (`STATUS_REVIEW`).

---

## Summary

By running `organize`:
1. Every movie is canonically named for instant metadata match.
2. Every movie gains a verified, external `.en.srt` for 100% Direct Play subtitles.
3. Every container is stripped of commentary and unnecessary tracks.
4. Your server runs cool, whisper-quiet, and at near 0% CPU.

---

<div align="center">

[← Documentation index](../README.md#-documentation) · [Architecture & safety →](ARCHITECTURE_SAFETY.md) · [Linux & Docker guide →](LINUX_DOCKER_GUIDE.md)

</div>
