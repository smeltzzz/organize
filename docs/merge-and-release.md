# Merging the overhaul, and cutting 3.6.0

Everything in this file needs a permission the bot that wrote the branch does
not have: pushing a file under `.github/workflows/`, and publishing to PyPI.
That is the only reason these steps are yours rather than already done.

Run them in order. Each one either passes or tells you what is wrong; nothing
here is destructive, and nothing before step 4 is visible outside the
repository.

---

## 1. Merge the pull request

```bash
gh pr checks 34                    # 15 jobs; `Packaging` is red, see below
gh pr merge 34 --merge             # or --squash, if you prefer one commit
```

**Expect `Packaging (install + console script)` to be red, and merge anyway.**
That job runs the workflow *as it exists on `main`*, which asks
`importlib.metadata` for a distribution called `organize`. The project is
`organizekit` on PyPI (`organize` has been taken since 2011); the corrected
step is in the patch you apply in step 2. Every other check — the whole suite
on Linux, macOS and Windows across Python 3.11, 3.12 and 3.13, plus lint,
coverage and both CLI smoke tests — is green.

## 2. Apply the two held workflow patches

```bash
git checkout main && git pull
git apply docs/ci-workflow.patch
git apply docs/release-workflow.patch
git add .github/workflows/ci.yml .github/workflows/release.yml
git commit -m "CI: apply the held workflow patches (needs the workflows permission)"
git push
```

`ci.yml` gains a `single-file` job (builds and runs `organize.pyz`), a
`doctor-smoke` job, byte-compilation of the `organizekit` package, an import
check against the installed wheel, the corrected distribution name, and a
coverage floor of 88% (measured: 91%). `release.yml` is new.

The suite stays green after this: the test that guards these patches accepts
"already applied" as a pass. Once you no longer want the patches around,
delete them and their two entries in `docs/README.md` — the test skips itself
when there are none.

## 3. Register the PyPI publisher (once, before the first release)

The release workflow authenticates with **Trusted Publishing (OIDC)**, so
there is no API token to store or rotate. It has to be registered on PyPI
first, and `organizekit` does not exist there yet, so use the *pending*
publisher form:

<https://pypi.org/manage/account/publishing/>

| Field | Value |
| :--- | :--- |
| PyPI project name | `organizekit` |
| Owner | `smeltzzz` |
| Repository name | `organize` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

Then create the `pypi` environment in the repository settings
(<https://github.com/smeltzzz/organize/settings/environments>) so the job can
reference it. Protection rules are optional; a required reviewer on that
environment means no release ever publishes without a human clicking approve.

## 4. Tag it

```bash
git tag -a v3.6.0 -m "3.6.0"
git push origin v3.6.0
```

The tag is what triggers `release.yml`. It runs the whole offline suite
*before* building anything, refuses a tag that disagrees with
`organizekit.VERSION` (currently `3.6.0`), builds the wheel, the sdist and the
zipapp, installs the wheel into a clean virtualenv and runs it from outside
the source tree, runs the sdist's own test suite, publishes to PyPI, and
attaches `organize.pyz` to a GitHub release. A version on PyPI is immutable,
which is why the order is that pedantic.

If you would rather not publish yet, skip this step entirely: nothing else
depends on it.

## 5. Check what people will actually get

```bash
pipx install organizekit        # or: pip install organizekit
organize doctor
organize --version              # 3.6.0
```

And the no-install path, which is the one that matters on a NAS:

```bash
curl -LO https://github.com/smeltzzz/organize/releases/download/v3.6.0/organize.pyz
python3 organize.pyz doctor
```

---

## If something goes wrong

**The tag was pushed with the wrong version.** Delete it and tag again;
nothing was published, because the version gate fails before the build:

```bash
git push --delete origin v3.6.0 && git tag -d v3.6.0
```

**PyPI rejects the upload as "not configured".** The pending publisher in
step 3 has not been created, or one of its five fields does not match exactly
— the environment name `pypi` is the one most often left blank.

**A patch will not apply in step 2.** Something changed under
`.github/workflows/` since it was written. `git apply --3way docs/…patch`
resolves the common cases; otherwise the patch header says what the change is
meant to achieve, and it is short enough to redo by hand.
