# Architecture & Safety Model

<div align="center">

[← Documentation index](../README.md#-documentation)

</div>

The `organize` toolkit is engineered with a strict safety-first philosophy: **it must never lose, truncate, or corrupt user media**, even across power cuts, process crashes, network failures, or concurrent multi-client torrent events.

This document details the architectural mechanisms that enforce these guarantees.

---

## 1. The Five Core Invariants

1. **Never delete unique data**: The standardizer is hardlink-only. Ingest never removes the source file.
2. **Never overwrite without atomic staging**: All files (reports, journals, manifests, subtitles, and remuxed MKVs) are staged as unique sibling temporary files and swapped using `os.replace`.
3. **Fail-closed advisory locking**: If a lock cannot be acquired within the timeout window, the tool halts immediately with an error code rather than racing another process.
4. **Subtitles before remux**: OSHash computation requires pristine original container bytes. Remuxing must wait until subtitles are secured.
5. **Seeding movies are inviolable**: Any movie with more than one hardlink is deferred by `mkv_track_cleaner.py` to prevent breaking active torrent seeding.

---

## 2. Cross-Platform Advisory Locking (`CoordinationLock`)

### Problem
qBittorrent may fire `movie_standardizer.py` at any moment when a download completes, while a scheduled task is running `subtitle_fetcher.py` or `mkv_track_cleaner.py` over the organized library. If two processes touch the same movie folder simultaneously, data corruption could occur.

### Solution
`common.CoordinationLock` implements a fail-closed advisory lock:
- Lives in the system temporary directory (`tempfile.gettempdir()`), so no metadata or lock artifacts ever pollute the media library.
- Lock path is deterministic based on a SHA-256 hash of the normalized target directory:
  ```python
  key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
  lock_path = Path(tempfile.gettempdir()) / f".movie_standardizer.lock.{key}"
  ```
- **Windows implementation**: Uses `msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)` on a 1-byte file range.
- **POSIX implementation**: Uses `fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)`.
- If another process holds the lock, the waiting process retries every 100ms until `timeout_seconds` elapses, then raises `LockTimeoutError` (a subclass of `TimeoutError`).

---

## 3. Atomic Text & Media Writes (`atomic_write_text`)

Writing directly to a target file risks leaving a truncated or corrupted file if the process is killed or the disk runs out of space mid-write.

`atomic_write_text` in `common.py` enforces:
1. Creates a sibling file with a unique name: `.{target_name}.{pid}.{random_hex}.tmp`.
2. Writes the complete text content in UTF-8.
3. Flushes and syncs to disk.
4. Calls `os.replace(staged, target)`. On POSIX and modern Windows (NTFS), `os.replace` is an atomic directory rename operation.
5. If an exception occurs, the temporary file is deleted in a `finally` block.

---

## 4. Remux Transaction Journaling (`mkv_track_cleaner.py`)

A remux involves writing gigabytes of data to create a newly optimized container. `mkv_track_cleaner.py` uses full transaction journaling:

### Remux Lifecycle
```
[Original MKV]
      │
      ▼
1. Free Disk Space Preflight Check (need size * 1.02 + 64MB)
      │
      ▼
2. Write Transaction Journal (.track_cleaner.<token>.json)
      │
      ▼
3. Remux to sibling temp file (temp_clean_<token>__<original>.mkv)
      │
      ▼
4. Pre- & Post-Flight Fingerprint Verification
   (Checks tracks, chapters, attachments, video duration, frame counts)
      │
      ▼
5. Atomic Swap (os.replace) & Timestamp Restoration
      │
      ▼
6. Purge Transaction Journal
```

### Crash Recovery
If power is lost mid-remux:
- On the next run, `mkv_track_cleaner.py` inspects the folder for orphaned `temp_clean_*` files and matching `.track_cleaner.*.json` journals.
- If a journal confirms the remux was 100% finished and verified, the tool completes the swap.
- If the remux was partial or interrupted, the orphan is cleaned up or flagged for manual review. The original MKV is **always left 100% intact**.

---

## 5. Media Probe Caching (`MediaProbeCache`)

### Problem
Running `ffprobe` (in `10bit.py`) and `mkvmerge -J` (in `mkv_track_cleaner.py`) spawns a child process for every movie in the library. For a library of 2,000 movies, spawning 2,000 processes takes several minutes, even when nothing has changed.

### Solution
`common.MediaProbeCache` stores the JSON probe output keyed by normalized path:
- An entry is valid **only if both `file_size` and `st_mtime_ns` match exactly**.
- If a file's size or timestamp changes by even 1 nanosecond, the entry is invalidated.
- **Only the probe output is cached, never a decision.** Decisions are computed fresh on every run against live filesystem state.
- Measured performance:
  - Cold run (30 movies): ~2.2s (spawns 30 processes)
  - Warm run (30 movies): **~0.10s** (spawns 0 processes)

---

## 6. OSHash Algorithm Specification

The OpenSubtitles OSHash is defined as:
$$\text{OSHash} = \text{FileSize} + \sum_{i=0}^{8191} \text{uint64le}(\text{chunk}_{\text{first}}) + \sum_{i=0}^{8191} \text{uint64le}(\text{chunk}_{\text{last}})$$

Where $\text{chunk}_{\text{first}}$ and $\text{chunk}_{\text{last}}$ are the first and last 64 KiB ($65,536$ bytes) of the file, read as 64-bit unsigned integers in little-endian byte order, with addition modulo $2^{64}$.

Because this hash relies on the exact first and last 64 KiB of the release, `subtitle_fetcher.py` must run before any remuxing touches the Matroska headers!

---

<div align="center">

[← Documentation index](../README.md#-documentation) · [Configuration reference →](CONFIGURATION_REFERENCE.md) · [FAQ & troubleshooting →](FAQ_TROUBLESHOOTING.md)

</div>
