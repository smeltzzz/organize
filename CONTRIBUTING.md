# Contributing to Organize

Thank you for your interest in contributing to **Organize**! This toolkit is engineered to be an impeccably safe, high-reliability media pipeline for Jellyfin and Plex movie libraries.

To maintain its bulletproof stability, all contributions must respect the project's core invariants and architectural principles.

---

## Architectural Invariants (Non-Negotiable)

1. **Zero Runtime Third-Party Dependencies (Stdlib Only)**
   Every runtime script must use **only the Python 3.11+ standard library** — no PyPI packages at runtime. Development-only tooling (e.g. `pytest`) lives in the `dev` extra.

2. **Tests Live in `tests/`, Not in the Shipped Tool**
   A tool's `--self-test` is a *field smoke test*: a handful of checks that answer "does this copy work on this machine?" in under a second (see `organizekit/core/smoke.py`). Exhaustive assertions belong in `tests/` — either as ordinary unit tests, or in `tests/selftests/` for the suites that were lifted out of the tools verbatim. Self-test code inside a production file is production code that the unit suite never runs: it inflated the shipped files by 2,229 lines and depressed measured coverage by about nine points while testing nothing extra.

3. **Shared Behaviour Lives in `organizekit/core/`, Exactly Once**
   Report rendering, atomic writes, locking, the subtitle contract and library-root resolution are defined once and imported by every tool. Do not copy them back into a tool: `tests/test_shared_core.py` fails the build if a tool defines a top-level name that the core already provides.

   The tools remain ordinary scripts — `python3 bitdepth.py` works straight out of a clone with no install and no `PYTHONPATH`, because `organizekit/` sits beside them at the repository root.

   This replaces an older rule that said the opposite: every tool used to carry its own copy of all of it, and contributors were asked to keep the copies byte-identical by hand. That failed exactly as you would expect. `atomic_write_text` drifted into two versions and the safer one (which `fsync`s before publishing, so a report survives a power cut and not merely a process crash) existed only in `subtitle_fetcher.py` — the tool that rewrites your movie files had the weakest writer in the repo. 4,325 lines of duplication bought that bug. A helper that must behave identically everywhere should exist in one place.

4. **Hardlink-Only Ingest (Zero Extra Disk Usage)**
   Ingestion into the organized library uses `os.link()`. It never silently degrades to a copy or move. Completed torrents remain fully seedable on their original filesystem without taking twice the space.

5. **Strict Order: Subtitles BEFORE Remux**
   `subtitle_fetcher.py` queries OpenSubtitles using release OSHash (`moviehash_match=only`). Remuxing rewrites the MKV headers, permanently invalidating the OSHash. Subtitles must always be fetched before track cleaning.

6. **Fail-Closed Concurrency Locks**
   All tools coordinate across processes and schedulers via advisory locks (`organizekit.core.CoordinationLock`). A tool refuses to touch a file rather than risk racing another process.

7. **Atomic Staging and Verification**
   Reports, journals, manifests, and remuxed MKVs are written to sibling temporary files and atomically swapped (`os.replace` / `os.link`). Mid-operation crashes or power outages never corrupt existing media.

8. **100% Offline Testability**
   The test suite must run completely offline without internet connectivity, without OpenSubtitles or SubDL API keys, and without requiring external binaries (`mkvmerge` or `ffprobe`).

---

## Development Setup

Clone the repository and run the test suite:

```bash
git clone https://github.com/smeltzzz/organize.git
cd organize

# Run every tool's field smoke test (fast; verifies this machine)
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
