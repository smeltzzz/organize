# Benchmarks

The speed claims in `README.md`, `CHANGELOG.md` and `OVERHAUL.md` are measured,
not estimated. These scripts are how. They are stdlib-only, offline, and leave
nothing behind outside `/tmp`.

```bash
python3 benchmarks/bench_sync_workers.py     # ffsubsync parallelism
python3 benchmarks/bench_audit_workers.py    # the audit, with and without latency
```

Neither is part of the test suite: they measure wall-clock time, which is the
one thing a test cannot assert without becoming flaky. Run them when you change
how work is scheduled, and put the numbers you get in the commit message.
