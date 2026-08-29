## Description
Briefly describe the purpose of this PR and what problem it solves.

## Changes Proposed
- Bullet point list of changes made

## Invariant Checklist
Please verify your changes adhere to the project's non-negotiable core invariants:
- [ ] **Standard Library Only**: No third-party Python dependencies added to `requirements.txt`.
- [ ] **Hardlink Safety**: Never converts hardlinks to copies or breaks seeding torrents without deferral.
- [ ] **Moviehash Ordering**: Subtitle fetching preserves pristine MKV release hashes before remuxing.
- [ ] **Atomic Operations**: All state writes (reports, manifests, ledgers, remux files) use atomic staging.
- [ ] **Test Coverage**: All unit tests (`bash run_tests.sh`) pass 100% offline.
