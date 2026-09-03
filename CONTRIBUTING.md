# Contributing to Organize

Thank you for your interest in contributing to **Organize**! This toolkit is engineered to be an impeccably safe, high-reliability media pipeline for Jellyfin and Plex movie libraries.

To maintain its bulletproof stability, all contributions must respect the project's core invariants and architectural principles.

---

## Architectural Invariants (Non-Negotiable)

1. **Zero Runtime Third-Party Dependencies (Stdlib Only)**
   Every runtime script must use **only the Python 3.11+ standard library** — no PyPI packages at runtime. Development-only tooling (e.g. `pytest`) lives in the `dev` extra.

2. **Each Tool Is a Self-Contained Script**
   Any single tool (`subtitle_fetcher.py`, `mkv_track_cleaner.py`, `bitdepth.py`, `movie_standardizer.py`, `library_auditor.py`) must stay runnable on its own: the shared helpers it relies on (report rendering, file locking, subtitle validation, library-root resolution) are **vendored inline** in a clearly marked section at the top of the file, not imported from a shared module. That is what lets a user copy one file into another project and run it.

   If you change a vendored helper, **change every copy**. This is no longer an honour-system rule: `tests/test_vendored_helpers.py` compares the copies by AST and fails the build on any divergence. Docstrings are excluded from the comparison, so a helper may still explain itself in terms of the tool it lives in, but behaviour must be identical.

   This rule used to be enforced by hand, and hand-enforcement failed: `atomic_write_text` had drifted into two versions, and the safer one (which `fsync`s before publishing, so a report survives power loss and not merely a process crash) existed only in `subtitle_fetcher.py`. The tool that rewrites your movie files had the weakest writer in the repo. Hence the test.

3. **Hardlink-Only Ingest (Zero Extra Disk Usage)**
   Ingestion into the organized library uses `os.link()`. It never silently degrades to a copy or move. Completed torrents remain fully seedable on their original filesystem without taking twice the space.

4. **Strict Order: Subtitles BEFORE Remux**
   `subtitle_fetcher.py` queries OpenSubtitles using release OSHash (`moviehash_match=only`). Remuxing rewrites the MKV headers, permanently invalidating the OSHash. Subtitles must always be fetched before track cleaning.

5. **Fail-Closed Concurrency Locks**
   All tools coordinate across processes and schedulers via advisory locks (`CoordinationLock`, vendored into each tool). A tool refuses to touch a file rather than risk racing another process.

6. **Atomic Staging and Verification**
   Reports, journals, manifests, and remuxed MKVs are written to sibling temporary files and atomically swapped (`os.replace` / `os.link`). Mid-operation crashes or power outages never corrupt existing media.

7. **100% Offline Testability**
   The test suite must run completely offline without internet connectivity, without OpenSubtitles or SubDL API keys, and without requiring external binaries (`mkvmerge` or `ffprobe`).

---

## Development Setup

Clone the repository and run the test suite:

```bash
git clone https://github.com/smeltzzz/organize.git
cd organize

# Run every tool's built-in self-test
python3 organize.py test

# Run the unit test suite
python3 -m unittest discover -s tests -p 'test_*.py'
```

Optionally install `pytest` for development:

```bash
pip install -e .[dev]
pytest
```

---

## Code Style & Guidelines

- **Type Annotations**: Use modern Python 3.11+ type annotations (`int | None`, `tuple[str, ...]`, etc.).
- **Docstrings**: Clear, informative docstrings explaining why decisions were made, not just what code does.
- **Cross-Platform Compatibility**: Code must run identically on Windows (NTFS), Linux (ext4, zfs, btrfs), and macOS (APFS).
- **Self-Tests**: Every tool script provides a `--self-test` CLI option that verifies core logic hermetically.
