# Organize — Jellyfin/Plex Movie Library Toolkit

A collection of five small, dependency-free **Python 3.11+** scripts that build
and maintain a canonical Jellyfin/Plex movie library on a Windows torrent box
(from a `E:\torrents\...` tree). They are designed to be run together as a
pipeline and to be **safe by default**: they never delete unique data, never
follow a failed operation with a data-destroying fallback, and every tool that
touches the library coordinates with the others through a shared advisory lock.

All five tools are **stdio-only** — no third-party Python packages. The only
external binaries are optional and per-tool (`mkvmerge`, `ffprobe`).

---

## The five tools

| Script | What it does | Needs |
| --- | --- | --- |
| `movie_standardizer.py` | Renames/hardlinks a finished torrent into the canonical **`Title (Year)/Title (Year).mkv`** layout with English subtitles only. The default qBittorrent completion hook (`"%F"`). | none |
| `mkv_track_cleaner.py` | Remux-only (no re-encode) cleanup: keeps one best English audio track, drops commentary/DVS, keeps SDH/forced subs, and prefers a validated exact `.en.srt`. | `mkvmerge` (MKVToolNix) |
| `10bit.py` | Uses `ffprobe` to report which movies are **8-bit** (queue for x265 10-bit), and which are already HDR / 10-bit. Fail-closed classification. | `ffprobe` (FFmpeg) |
| `library_auditor.py` | **Read-only** audit of the direct container file per movie folder: canonical MKV, missing English SRT, stem mismatch, non-canonical SRT sidecar, multiple/other/no movie file. | none |
| `subtitle_fetcher.py` | Fetches English human-authored UTF-8 SRTs from OpenSubtitles, hash-gated, with a coordination lock and transaction guards. **Run before `mkv_track_cleaner.py`.** | `OPENSUBTITLES_API_KEY` env var |

### Recommended ordering

```
torrent done ──▶ movie_standardizer.py    (name + hardlink into library)
        ──▶ subtitle_fetcher.py           (fetch SRTs while the MKV bytes are still pristine)
        ──▶ mkv_track_cleaner.py          (remux; the validated .en.srt becomes the sole subtitle)
        ──▶ 10bit.py                      (decide whether to re-encode 8-bit → 10-bit)
        ──▶ library_auditor.py            (periodic read-only health check)
```

#### Why subtitles are fetched *before* the remux

This is the one ordering rule that is load-bearing rather than cosmetic, so it is
worth explaining.

`subtitle_fetcher.py` searches OpenSubtitles by **moviehash** first — the
OpenSubtitles OSHash, defined as the file size plus the sum of the first and
last 64 KiB of the file, read as little-endian `uint64`s. It submits that hash
with `moviehash_match=only`, so the provider returns *only* subtitles uploaded
against a byte-identical release. That is the high-confidence path: a hash hit
is a genuine exact match, and the download is trusted without any guesswork.

A remux changes the container bytes. `mkv_track_cleaner.py` rewrites the MKV —
reordering tracks, dropping commentary audio, rewriting the cues and headers —
so the file size and those first/last 64 KiB all change. **The moment a file is
remuxed, its moviehash matches nothing in the provider's database.** Running the
cleaner first quietly demotes every movie to the fallback path: a title/year
search whose results must clear extra rating, vote-count and
edition-marker thresholds, and which is deliberately *held for review* rather
than downloaded when it is not confidently the same cut. In practice that means
fewer subtitles fetched, more manual review, and a real chance of never getting
a sidecar at all.

Fetching first keeps the pristine release hash available for the one operation
that can use it.

Two properties make this safe rather than merely preferable:

- **External sidecars survive everything downstream.** The `.en.srt` lives
  beside the MKV, not inside it, so it is untouched by the track cleaner's remux
  and by any later 10-bit re-encode. Fetch it once and it stays correct.
- **Plain external SRT is the most direct-play-safe subtitle format for
  Jellyfin.** It is plain text, so Jellyfin hands it to the client as-is.
  Image-based PGS/VobSub and heavily styled ASS/SSA routinely force a burn-in —
  a full video transcode — on clients that cannot render them, and even an
  embedded SRT track must be extracted from the container before playback.
  UTF-8 SRT beside the MKV is what keeps the video stream untouched.
- **The cleaner then does more with it.** `mkv_track_cleaner.py` treats a
  validated exact `.en.srt` as the authoritative Jellyfin subtitle choice and
  removes every embedded subtitle track, including SDH, forced and non-English
  ones. Fetch first and the cleaner can act on it in the same pass instead of
  waiting for a second run.

If you have already run the cleaner over a library, the hash advantage is gone
for those files; the fetcher still works, it just leans on identity matching.
`mkv_track_cleaner.py` warns when it remuxes a movie that has no validated
external SRT, so the mistake is visible rather than silent.

---

## Shared infrastructure

The duplicated helpers that all five tools used to copy-paste now live in one
module: [`common.py`](common.py). It provides:

- `atomic_write_text(path, text)` — stage a sibling temp file then `os.replace`,
  so a report is never truncated and a failed write keeps the previous version.
- `path_is_within(candidate, parent)` — path containment checks (prevents a tool
  from running outside its configured library).
- `path_norm(path)` / `paths_equal(a, b)` — the precise normalization contract
  the tools use to agree on lock keys and identity (with `samefile` when both
  paths exist).
- `CoordinationLock` — the cross-platform (Windows `msvcrt` / POSIX `fcntl`),
  **fail-closed** advisory lock that `movie_standardizer`, `mkv_track_cleaner`
  and `subtitle_fetcher` contend on. It is deterministic on the *normalized*
  target path and lives in the system temp dir so no media-library file is
  created merely to coordinate. Preserves the exact byte-range/byte-materialize
  protocol each tool previously implemented, and raises `LockTimeoutError`
  (a subclass of `TimeoutError`) on timeout so existing callers keep working.
- `try_file_lock(handle, *, strict_non_contention)` — the shared low-level
  non-blocking lock attempt used by both the coordination lock and the tools'
  own single-instance run locks.

Importing `common` writes nothing to disk.

---

## External prerequisites

- **Python 3.11+** (uses modern typing, `match`-free but `|` annotations, etc.).
- **MKVToolNix** (`mkvmerge`) — only for `mkv_track_cleaner.py`. Found on `PATH`
  or in the standard install locations; override with `--mkvmerge`.
- **FFmpeg** (`ffprobe`) — only for `10bit.py`; override with `--ffprobe`.
- **OpenSubtitles consumer API key** — only for the *daily queue* mode of
  `subtitle_fetcher.py`, via `OPENSUBTITLES_API_KEY` (and optionally
  `OPENSUBTITLES_USERNAME` / `OPENSUBTITLES_PASSWORD`).

No Python packages are required at runtime.

---

## Usage

Every script prints a timestamped `[LEVEL] log` to the console and appends to
an append-only log file, and writes **exactly one** replaceable report. Run
each with `--self-test` to verify its internal invariants offline (no media,
no binaries, no credentials required).

### movie_standardizer.py

```bash
# qBittorrent completion hook (content path)
python movie_standardizer.py "%F"

# old "%D" "%N" form, and batch-scan SOURCE_DIR when no path is given
python movie_standardizer.py --target "E:\torrents\final_organized" --source "E:\torrents\final"

# preview without touching anything
python movie_standardizer.py --dry-run --target "E:\torrents\final_organized"
```

Key flags: `--target`, `--source`, `--min-size MB`, `--lock-timeout SECS`,
`--allow-tv`, `--category`, `--dry-run`, `--verbose`, `--self-test`.

### mkv_track_cleaner.py

```bash
python mkv_track_cleaner.py --dry-run --dir "E:\torrents\final_organized"
python mkv_track_cleaner.py --dir "E:\torrents\final_organized" --nice
```

Key flags: `--dir`, `--only MKV` (repeatable), `--dry-run`, `--mkvmerge PATH`,
`--log`, `--report`, `--standardizer-lock-timeout SECS`, `--no-color`,
`--nice`, `--min-size MB`, `--limit N`, `--self-test`.

### 10bit.py

```bash
python 10bit.py --ffprobe ffprobe --source "E:\torrents\final_organized"
python 10bit.py --dry-run --fail-if-queue   # exit code for CI/automation
```

Key flags: `--source`, `--min-size MB`, `--workers N`, `--timeout SECS`,
`--ffprobe PATH`, `--fail-if-queue`, `--fail-if-review`, `--fail-if-error`,
`--dry-run`, `--verbose`, `--self-test`.

### library_auditor.py

```bash
python library_auditor.py --source "E:\torrents\final_organized"
```

Key flags: `--source`, `--log`, `--report`, `--lock-timeout SECS`, `--self-test`.

### subtitle_fetcher.py

```bash
export OPENSUBTITLES_API_KEY="..."
python subtitle_fetcher.py --source "E:\torrents\final_organized" --dry-run
```

Key flags: `--source`, `--report`, `--log`, `--daily-cap N`, `--min-size MB`,
`--lock-timeout SECS`, `--limit N`, `--dry-run`, `--no-identity-fallback`,
`--retry-review`, `--self-test`, `--version`.

---

## Config

Each script has sensible built-in defaults for a Windows `E:\torrents\...`
library and exposes them via CLI flags. `movie_standardizer.py` additionally
honors a small set of environment variables (`MOVIE_STD_TARGET`,
`MOVIE_STD_SOURCE`, `MOVIE_STD_LOG`, `MOVIE_STD_MIN_SIZE`, `MOVIE_STD_REPORT`,
`MOVIE_STD_LOCK_TIMEOUT`, `MOVIE_STD_DRY_RUN`). `subtitle_fetcher.py` reads its
OpenSubtitles credentials from the environment.

---

## Safety model

These tools are intentionally conservative:

- **Atomic writes** everywhere — a crash never leaves a half-written report.
- **Fail-closed locks** — if the coordination lock cannot be taken, the tool
  refuses to proceed rather than race a hardlink placement or a remux.
- **Path containment / preflight validation** — each run rejects recursive
  source/target layouts, reports inside the library, wrong filesystems, and
  other misconfigurations before touching media.
- **Idempotent and non-destructive** cleanup logs, quarantines, or (only when
  explicitly selected) deletes candidates; it never deletes unique data blindly.
- **Verification-after-remux** for `mkv_track_cleaner.py` fingerprints tracks,
  chapters, attachments, duration and frames before swapping output over the
  original; a mismatch leaves the original untouched.
- **Read-only** for `library_auditor.py` — it only inspects files and writes a
  report outside the library.

---

## Testing

Every tool ships a built-in `--self-test`. There is also a proper unit-test
suite (stdlib `unittest`, also runnable under `pytest`) in [`tests/`](tests/).

```bash
# Run every built-in self-test plus the unit suite (offline):
bash run_tests.sh

# Or run just the unit tests:
python3 -m unittest discover -s tests -p 'test_*.py'

# With pytest (optional):
python3 -m pytest
```

The tests are all offline — no media, no `mkvmerge`, no `ffprobe`, no API key.

---

## Layout

```
.
├── 10bit.py
├── library_auditor.py
├── mkv_track_cleaner.py
├── movie_standardizer.py
├── subtitle_fetcher.py
├── common.py              # shared infra (atomic write, paths, locking)
├── tests/                 # stdlib unit tests
├── run_tests.sh           # runs all self-tests + the unit suite
├── requirements.txt       # runtime: none (stdlib only)
├── requirements-dev.txt   # optional pytest for development
├── pyproject.toml         # pytest discovery config
└── README.md
```
