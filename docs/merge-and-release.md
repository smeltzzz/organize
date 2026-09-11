# Merging the subtitle-fetcher removal, and cutting 4.0.0

Everything in this file needs a permission the bot that wrote the branch does
not have: pushing to `main`, pushing a file under `.github/workflows/`, and
moving a tag. That is the only reason these steps are yours rather than
already done.

Run them in order. Each one either passes or tells you what is wrong; nothing
here is destructive, and nothing before step 4 is visible outside the
repository.

---

## 1. Merge the branch into `main`

The branch is [PR #36](https://github.com/smeltzzz/organize/pull/36) — merge
it with the green button (a normal merge, not squash: the seven commits carry
the story), or locally:

```bash
git fetch origin
git checkout main && git pull
git merge --no-ff arena/01a08710-organize \
  -m "Merge the subtitle-fetcher removal: extraction from the movie's own tracks only (4.0.0)"
git push origin main
```

**Expect the `Byte-compile` job on that merge to be red, and push anyway.**
The workflow file as it exists on the branch still names `subtitle_fetcher.py`
and `jellyfin_one_shot.py` in the syntax gate — files the merge deletes —
and the bot cannot fix a workflow file; the fix is the patch you apply in
step 2. Every other check is green: the whole suite (1,229 tests) on Linux,
macOS and Windows across Python 3.11–3.13, packaging, the single-file build
and the doctor smoke test.

(If you would rather the merge commit be born green, apply step 2's patch to
the branch and push it first — your push carries the permission the bot's
does not — then merge.)

## 2. Apply the held workflow patch

```bash
git checkout main && git pull
git apply docs/ci-workflow.patch
git add .github/workflows/ci.yml
git commit -m "CI: byte-compile the 4.0.0 file list; coverage floor follows the code down (88 -> 85)"
git push
```

Three changes, all explained in the patch header:

- the byte-compile list follows the `subtitle_fetcher.py` →
  `subtitle_extractor.py` rename and drops `jellyfin_one_shot.py`, which the
  merge deleted;
- the coverage floor drops 88% → 85%. The 4.0.0 deletions (the fetching code,
  then the one-shot runner and its tests) were 100%-covered, so the suite's
  overall number settled at 86%; the floor keeps its usual point of slack
  below the real figure. It is a ratchet, not a target — raise it again in a
  later testing pass, never lower it further.

The push needs a credential with `workflows` scope (your normal PAT or
`gh auth login` as the owner; editing the file in the GitHub web editor works
too). The suite stays green after this: the test that guards held patches
accepts "already applied" as a pass. Once you no longer want the patch
around, delete it and its entry in `docs/README.md` — the test skips itself
when there are none.

## 3. PyPI publisher — already done

`organizekit` is already live on PyPI (3.6.0 was published through it), so
Trusted Publishing is registered and the `pypi` environment exists. For the
record, the registration at <https://pypi.org/manage/account/publishing/> is:
owner `smeltzzz`, repository `organize`, workflow `release.yml`, environment
`pypi`. Nothing to do here unless that was undone.

## 4. Tag it

```bash
git tag -a v4.0.0 -m "4.0.0"
git push origin v4.0.0
```

The tag is what triggers `release.yml`. It runs the whole offline suite
*before* building anything, refuses a tag that disagrees with
`organizekit.VERSION` (currently `4.0.0`), builds the wheel, the sdist and
the zipapp, installs the wheel into a clean virtualenv and runs it from
outside the source tree, runs the sdist's own test suite, publishes to PyPI,
and attaches `organize.pyz` to a GitHub release. A version on PyPI is
immutable, which is why the order is that pedantic.

## 5. Check what people will actually get

```bash
pip install --upgrade organizekit     # or: pipx install organizekit
organize doctor
organize --version                    # 4.0.0
```

And the no-install path, which is the one that matters on a NAS:

```bash
curl -LO https://github.com/smeltzzz/organize/releases/download/v4.0.0/organize.pyz
python3 organize.pyz doctor
```

Worth saying in whatever you announce with the release: **subtitle
downloading is gone**, and **`organize run` is the one runner** (the separate
one-shot completer was deleted with it — one pass of the five steps, and
re-running is the loop). `OPENSUBTITLES_API_KEY` / `SUBDL_API_KEY` are ignored
(and unknown) now, the tool answers to `subtitle_extractor.py` /
`organize extract`, and an existing `.eng.srt` is authoritative — never
re-checked, never re-synced. A library that came through the fetching era
keeps its sidecars, and its old `subtitle_fetcher_extracted.json` provenance
ledger is still read (never written), so a sidecar extracted back then and
never synced gets its one measurement.

---

## If something goes wrong

**The tag was pushed with the wrong version.** Delete it and tag again;
nothing was published, because the version gate fails before the build:

```bash
git push --delete origin v4.0.0 && git tag -d v4.0.0
```

**PyPI rejects the upload as "not configured".** Only possible if the
registration in step 3 was undone; the environment name `pypi` is the field
most often wrong.

**A patch will not apply in step 2.** Something changed under
`.github/workflows/` since it was written. `git apply --3way
docs/ci-workflow.patch` resolves the common cases; otherwise the patch header
says what the change is meant to achieve, and it is two small edits to redo
by hand.
