# Contributing to Organize

Thank you for your interest in contributing to **Organize**! This toolkit is engineered to be an impeccably safe, high-reliability media pipeline for Jellyfin and Plex movie libraries.

To maintain its bulletproof stability, all contributions must respect the project's core invariants and architectural principles.

---

## Architectural Invariants (Non-Negotiable)

1. **Zero Runtime Third-Party Dependencies (Stdlib Only)**
   All runtime scripts (`organize.py`, `common.py`, `pipeline.py`, `movie_standardizer.py`, `mkv_track_cleaner.py`, `subtitle_fetcher.py`, `10bit.py`, `library_auditor.py`) must use **only the Python 3.11+ standard library**. No PyPI packages in `requirements.txt`.

2. **Hardlink-Only Ingest (Zero Extra Disk Usage)**
   Ingestion into the organized library uses `os.link()`. It never silently degrades to a copy or move. Completed torrents remain fully seedable on their original filesystem without taking twice the space.

3. **Strict Order: Subtitles BEFORE Remux**
   `subtitle_fetcher.py` queries OpenSubtitles using release OSHash (`moviehash_match=only`). Remuxing rewrites the MKV headers, permanently invalidating the OSHash. Subtitles must always be fetched before track cleaning.

4. **Fail-Closed Concurrency Locks**
   All tools coordinate across processes and schedulers via advisory locks (`CoordinationLock` in `common.py`). A tool refuses to touch a file rather than risk racing another process.

5. **Atomic Staging and Verification**
   Reports, journals, manifests, and remuxed MKVs are written to sibling temporary files and atomically swapped (`os.replace` / `os.link`). Mid-operation crashes or power outages never corrupt existing media.

6. **100% Offline Testability**
   The test suite (`run_tests.sh` and `unittest` / `pytest`) must run completely offline without internet connectivity, without an OpenSubtitles API key, and without requiring external binaries (`mkvmerge` or `ffprobe`).

---

## Development Setup

Clone the repository and run the test suite:

```bash
git clone https://github.com/smeltzzz/organize.git
cd organize

# Run all self-tests and unit tests
bash run_tests.sh

# Or with python unittest
python3 -m unittest discover -s tests -p 'test_*.py'
```

Optionally install `pytest` for development:

```bash
pip install -r requirements-dev.txt
pytest
```

---

## Code Style & Guidelines

- **Type Annotations**: Use modern Python 3.11+ type annotations (`int | None`, `tuple[str, ...]`, etc.).
- **Docstrings**: Clear, informative docstrings explaining why decisions were made, not just what code does.
- **Cross-Platform Compatibility**: Code must run identically on Windows (NTFS), Linux (ext4, zfs, btrfs), macOS (APFS), and Docker.
- **Self-Tests**: Every tool script provides a `--self-test` CLI option that verifies core logic hermetically.
