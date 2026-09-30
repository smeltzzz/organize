## Description
Briefly describe the purpose of this PR and what problem it solves.

## Changes Proposed
- Bullet point list of changes made

## Invariant Checklist
Please verify your changes adhere to the project's non-negotiable core invariants:
- [ ] **Standard Library Only**: No third-party Python dependencies added to runtime code.
- [ ] **Shared Core, Not Copies**: Shared helpers (report rendering, atomic writes, locking, the subtitle contract, library-root resolution) live exactly once in `organizekit/core/` and are **imported** by the tools. A tool that re-defines a core helper fails the build — `tests/test_shared_core.py::NothingMayReVendorTheCore` makes the duplication unrepresentable rather than merely discouraged.
- [ ] **Hardlink Safety**: Never converts hardlinks to copies or breaks seeding torrents without deferral.
- [ ] **Moviehash Ordering**: subtitles are extracted — and image-only movies are hash-matched — *before* the lossless remux, so a lookup sees the release's own bytes.
- [ ] **Atomic Operations**: All state writes (reports, manifests, ledgers, remux files) use atomic staging.
- [ ] **Test Coverage**: Self-tests (`python organize.py test`) and unit tests (`python -m unittest discover -s tests -p "test_*.py"`) pass 100% offline.
