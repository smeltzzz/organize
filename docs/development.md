# Testing & development

The suite, the single-file build, and the tests that execute the safety
claims instead of restating them.

---

The whole suite is **offline**: no media files, no `mkvmerge`, no `ffprobe`,
no API keys, no network.

```bash
python3 organize.py test                          # built-in self-tests (one per script)
python3 -m unittest discover -s tests -p "test_*.py"   # 1,676 unit tests, ~25 s
pip install -e ".[dev]" && pytest                 # same suite under pytest
ruff check .                                      # lint (configured in pyproject.toml)
```

Installing the package also provides an `organize` console script, so the CLI
works from any directory:

```bash
pip install .          # from a clone
pip install organizekit
organize doctor
```

The distribution is called **`organizekit`** — the name the shared package
already has on disk — because `organize` on PyPI has belonged to an unrelated
tabular-data parser since 2011, and `organize-media` to a media copier. The
command you type is unaffected: `pip install organizekit` gives you `organize`.

## Cutting a release

A version on PyPI is immutable. A broken 3.6.0 cannot be fixed, only answered
with 3.6.1, so everything that can be checked before the upload is checked
before the upload — by
[`docs/release-workflow.patch`](release-workflow.patch) in CI, and by this list
if you are doing it by hand:

```bash
# 1. One version, in one place. Everything else reads organizekit.VERSION.
$EDITOR organizekit/__init__.py           # bump VERSION
$EDITOR CHANGELOG.md                      # move [Unreleased] to the new version

# 2. The suite, the linter and the field smoke tests.
python3 -m unittest discover -s tests -p "test_*.py" && ruff check . && python3 organize.py test

# 3. Build both artifacts and check the metadata PyPI will render.
rm -rf dist build *.egg-info && python3 -m build && python3 -m twine check dist/*

# 4. The wheel must work from outside the source tree, in a clean environment.
python3 -m venv /tmp/checkinstall && /tmp/checkinstall/bin/pip install dist/*.whl
cd /tmp && /tmp/checkinstall/bin/organize --version && /tmp/checkinstall/bin/organize doctor

# 5. The sdist must carry a suite that actually runs.
mkdir /tmp/sdist && tar xzf dist/*.tar.gz -C /tmp/sdist --strip-components=1
cd /tmp/sdist && python3 -m unittest discover -s tests -p "test_*.py"

# 6. Tag it. The workflow does the rest; the tag must match VERSION.
git tag -a v3.6.0 -m "3.6.0" && git push origin v3.6.0
```

Steps 4 and 5 are not ceremony. The wheel ships nine top-level modules and a
package, and a tool added at the repository root without a line in
`py-modules` is missing from it while working perfectly in the checkout;
`tests/test_packaging.py` catches that one offline, but only running the thing
proves the console script resolves. And setuptools' default sdist ships
`tests/test_*.py` while leaving behind the fixtures they import, which produces
a distribution whose tests cannot be collected — `MANIFEST.in` fixes it and
step 5 is what notices when it stops.

Publishing itself uses **Trusted Publishing**: GitHub mints a short-lived OIDC
credential for that exact repository and workflow, so there is no PyPI token in
the repository secrets to rotate, leak or forget.

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

## The property tests

Most of the suite is examples: inputs somebody thought of, and a fault injected
at each step of a transaction. `tests/test_properties.py` is the other kind —
it states an invariant and lets `tests/property.py` hunt for a counterexample
across a hundred generated cases. The rules it protects are the ones an
unforeseen input would be expensive to get wrong: an HDR file is never queued
for re-encoding, the remux plan never keeps a commentary track or invents one
that is not in the file, arbitrary bytes are never mistaken for a subtitle, and
a folder the ingest hook writes is one the auditor calls canonical.

```bash
python3 -m pytest tests/test_properties.py            # the default seeds
ORGANIZE_PROPERTY_SEED=7 python3 -m pytest tests/test_properties.py   # sweep another
```

The harness is about sixty lines of standard library, and three of its
properties matter more than its size:

- **Deterministic by default.** Each test's seed comes from its own id, so a
  failure reproduces exactly. `ORGANIZE_PROPERTY_SEED` sweeps other seeds and
  the value used is printed with every failure, so a counterexample found at
  3 a.m. can be pinned into the default run.
- **Failures shrink.** A random twelve-track MKV that breaks an invariant is
  not a bug report. Each failure is reduced — drop list elements, empty
  strings, walk integers toward zero — while it keeps failing the same way.
- **It cannot pass vacuously.** The harness is made to fail on purpose in
  `HarnessTests`, and `MutationTests` breaks the *implementation* — queue an
  HDR file, keep the worst audio track, accept any text as a subtitle — and
  asserts the matching property notices. An oracle that quietly re-derives its
  expectation from the function it is judging looks exactly like a passing
  test; that is how the audio-ranking property was caught being tautological
  and given an independent oracle instead.

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
