## Description
Briefly describe the purpose of this PR and what problem it solves.

## Changes Proposed
- Bullet point list of changes made

## Invariant Checklist
Please verify your changes adhere to the project's non-negotiable core invariants:
- [ ] **Standard Library Only**: No third-party Python dependencies added to runtime code.
- [ ] **Self-Contained Tools**: No tool imports from a shared module; any new shared helper is vendored into every script that uses it (copies kept byte-identical).
- [ ] **Hardlink Safety**: Never converts hardlinks to copies or breaks seeding torrents without deferral.
- [ ] **Moviehash Ordering**: Subtitle fetching preserves pristine MKV release hashes before remuxing.
- [ ] **Atomic Operations**: All state writes (reports, manifests, ledgers, remux files) use atomic staging.
- [ ] **Test Coverage**: Self-tests (`python organize.py test`) and unit tests (`python -m unittest discover -s tests -p "test_*.py"`) pass 100% offline.
