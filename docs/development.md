# Testing & development

The suite, the single-file build, and the tests that execute the safety
claims instead of restating them.

---

The whole suite is **offline**: no media files, no `mkvmerge`, no `ffprobe`,
no API keys, no network.

```bash
python3 organize.py test                          # built-in self-tests (one per script)
python3 -m unittest discover -s tests -p "test_*.py"   # 934 unit tests, ~13 s
pip install -e ".[dev]" && pytest                 # same suite under pytest
ruff check .                                      # lint (configured in pyproject.toml)
```

Installing the package also provides an `organize` console script, so the CLI
works from any directory:

```bash
pip install .
organize doctor
```

## One file, no install

For the machine this toolkit is actually for — a NAS or a home server with
Python and nothing else — build the whole thing into one file and copy it
across:

```bash
python3 scripts/build_pyz.py          # writes dist/organize.pyz (~270 KiB)
scp dist/organize.pyz nas:/volume1/
ssh nas 'cd /volume1 && python3 organize.pyz doctor'
```

It is the same toolkit, not a cut-down one: `organize.pyz test` runs all nine
field smoke tests, `organize.pyz run-tool pipeline.py --source …` runs the full
five-step pass, and each step is still its own process with its own locks, log,
report and exit code. Logs and reports land *beside* the archive, never inside
it. The module list comes from `pyproject.toml`, so the archive and the wheel
cannot drift apart, and the build is reproducible — the same source always
produces the same bytes. A test (`tests/test_zipapp.py`) builds the archive,
runs real work out of it, and asserts that every import inside it resolves to
the standard library or to another file in the archive, which is the "zero
runtime dependencies" claim checked rather than repeated.

Every tool also carries a `--self-test` **field smoke test** — it answers "does
this copy work on this machine?" in under a second, without the repository, a
media library, or a network: `python3 library_auditor.py --self-test`. It
checks the shared report renderer, the atomic writer and the library-root
resolution, plus a few of that tool's own decisions (the auditor audits a
temporary library; `bitdepth` confirms 8-bit SDR is queued and Dolby Vision is
protected; the standardizer verifies this filesystem actually supports
hardlinks). The exhaustive suites those flags used to run now live in
`tests/selftests/`, where they are part of the offline unit run and count
towards coverage.

## The crash tests

The claim these tools live or die by is that a power cut cannot cost you a
movie. `tests/test_crash_safety.py` executes it rather than asserting it in
prose: it kills the remux at each step of its transaction — after the journal
is written, after mkvmerge finishes, after verification, between the staging
file and `os.replace` — and then checks the filesystem. The original must be
byte-identical or already fully replaced, with no third state, and the *next*
run must clean up whatever debris was left, without ever promoting a file that
was not verified. The same treatment is applied to the subtitle sync, to the
durable writers themselves, and to a hand-planted hostile recovery journal
pointing at `../precious.mkv`.

`tests/test_track_cleaner_e2e.py` runs the cleaner end to end against
`tests/fake_mkvmerge.py` — a real executable that speaks enough of the
mkvmerge command line to be driven by the unmodified tool, so the subprocess
launch, progress parsing, verification, atomic swap, locking and report are
all the real ones. `tests/test_standardizer_destructive.py` does the same for
the only tool that deletes folders, in all three maintenance modes.

Contributions: see [CONTRIBUTING.md](../CONTRIBUTING.md). Security reports: see
[SECURITY.md](../SECURITY.md).

---

[← Back to the README](../README.md) · [Tool reference](tools.md) ·
[Configuration](configuration.md)
