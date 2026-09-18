# Cutting a release, and the recorded 5.0.0 run

> **6.0.0 is prepared and waiting on a tag.** The version bump, the changelog
> entry and the release guard are committed with the OCR-removal work in
> [PR #41](https://github.com/smeltzzz/organize/pull/41). Once that is merged:
>
> ```bash
> git checkout main && git pull
> python3 -c "import organizekit; print(organizekit.VERSION)"   # must print 6.0.0
> git tag -a v6.0.0 -m "6.0.0" && git push origin v6.0.0
> ```
>
> `release.yml` then re-runs the whole offline suite, refuses the tag if it
> disagrees with `organizekit.VERSION`, builds the wheel, the sdist and the
> zipapp, publishes to PyPI, and attaches `organize.pyz` to the GitHub release.
> Afterwards, an installed copy updates with
> `python3 -m pip install --upgrade organizekit`
> ([Testing & development](development.md#cutting-a-release)).
>
> **6.0.0, not 5.1.0**, for the same reason 4.0.0 and 5.0.0 were majors: the
> five OCR flags no longer exist (all five are listed in the changelog's
> `[6.0.0]` section), so a script that passed one now fails loudly instead of
> quietly doing something else. This page is a live document, so it deliberately
> does not quote flags that no tool accepts any more - `tests/test_docs.py`
> fails when a live page names a flag that was deleted, which is how this
> sentence came to be reworded. What the release contains is
> [`CHANGELOG.md`](../CHANGELOG.md)'s `[6.0.0]` section.

> **The rest of this file is the record of the 5.0.0 release.** v5.0.0 was tagged and published on
> 2026-09-12, and the CI patches it held were applied and committed afterwards
> ([PR #40](https://github.com/smeltzzz/organize/pull/40)). It is kept as the
> record of how that release was made, because the next one follows the same
> shape - and because two of its steps needed a permission the bot does not
> have, which is worth knowing before the next tag.
>
> Two things in the steps below were true at the time and are **no longer**:
> the merge is done, and `docs/ci-workflow-sync-removal.patch` no longer exists
> because it was applied. Both are marked inline.
>
> ### The one thing held right now
>
> `docs/ci-workflow-comment.patch` - a comment-only change to the header of
> `.github/workflows/ci.yml`, held because the branch bot has no `workflows`
> permission. CI behaves identically before and after it. Apply it whenever
> convenient:
>
> ```bash
> git checkout main && git pull
> git apply docs/ci-workflow-comment.patch
> git add .github/workflows/ci.yml
> git commit -m "CI: the hermeticity header names the fake transport"
> git push
> ```
>
> Nothing else is outstanding: no other patch is held, and no other step here
> is waiting on you.

Everything in this file needs a permission the bot that wrote the branch does
not have: pushing to `main`, pushing a file under `.github/workflows/`, and
moving a tag. That is the only reason these steps are yours rather than
already done.

Run them in order. Each one either passes or tells you what is wrong; nothing
here is destructive, and nothing before step 4 is visible outside the
repository.

---

## 1. Merge the branch into `main`

The branch is `arena/01a093c2-organize` —
[PR #39](https://github.com/smeltzzz/organize/pull/39). Merge it with the green
button (a normal merge, not squash: the commits carry the story), or locally:

```bash
git fetch origin
git checkout main && git pull
git merge --no-ff arena/01a093c2-organize \
  -m "Merge the subtitle-sync removal: organize run is four steps (5.0.0)"
git push origin main
```

**At the time, the `Byte-compile` job on that merge was red, and the merge was
pushed anyway** — the workflow file still named `sync_subtitles.py` in the
syntax gate, a file the merge deleted, and the `provisioned` job still did
`pip install ffsubsync` then `import sync_subtitles`. The bot cannot fix a
workflow file, so that was step 2's patch. **This is history: PR #40 applied
the patch, and CI on `main` has been green since.** Everything else in that
run was green on the same merge: the whole suite (1,174 tests) on Linux, macOS
and Windows across Python 3.11–3.13, packaging, the single-file build, the lint
job, the coverage floor and the doctor smoke test.

(For a future release that hits the same wall: apply step 2's patch to the
branch and push it first — your push carries the permission the bot's does not
— then merge, and the merge is born green.)

## 2. Apply the held workflow patch — **done**

Applied and committed in [PR #40](https://github.com/smeltzzz/organize/pull/40);
`docs/ci-workflow-sync-removal.patch` is gone because there was nothing left to
hold. The command was:

```bash
git checkout main && git pull
git apply docs/ci-workflow-sync-removal.patch
git add .github/workflows/ci.yml
git commit -m "CI: drop the deleted sync stage from the byte-compile list and the provisioned job"
git push
```

Three changes, all explained in the patch header:

- the byte-compile list drops `sync_subtitles.py`, which the merge deleted;
- the `provisioned` job stops installing ffsubsync, stops importing
  `sync_subtitles`, and stops requiring an `ffsubsync` binary on the machine.
  `ffmpeg` stays installed: `ffprobe` ships in the same distribution and the
  10-bit step needs it;
- two comments follow the tool count down from five steps to four.

The push needs a credential with `workflows` scope (your normal PAT or
`gh auth login` as the owner; editing the file in the GitHub web editor works
too). The suite stays green after this: the test that guards held patches
accepts "already applied" as a pass.

The older `docs/ci-workflow.patch` was applied in the same way. Both are gone;
should a future branch need a `workflows` change the bot cannot push, hold it in
`docs/` as a patch, list it in `docs/README.md`, and `tests/test_docs.py` picks
it up automatically - a held patch is a failing test if it has rotted, and no
test at all when there are none. `docs/ci-workflow-comment.patch` (see the top
of this file) is the one being held today.

## 3. PyPI publisher — already done

`organizekit` is already live on PyPI (3.6.0 was published through it), so
Trusted Publishing is registered and the `pypi` environment exists. For the
record, the registration at <https://pypi.org/manage/account/publishing/> is:
owner `smeltzzz`, repository `organize`, workflow `release.yml`, environment
`pypi`. Nothing to do here unless that was undone.

## 4. Tag it

```bash
git tag -a v5.0.0 -m "5.0.0"
git push origin v5.0.0
```

The tag is what triggers `release.yml`. It runs the whole offline suite
*before* building anything, refuses a tag that disagrees with
`organizekit.VERSION` (now `5.0.0`), builds the wheel, the sdist and the
zipapp, installs the wheel into a clean virtualenv and runs it from outside the
source tree, runs the sdist's own test suite, publishes to PyPI, and attaches
`organize.pyz` to a GitHub release. A version on PyPI is immutable, which is
why the order is that pedantic.

Why 5.0.0 and not 4.1.0: a documented command (`organize sync`) and a shipped
module (`sync_subtitles.py`) stopped existing. Anything that scripted the old
five-step sweep breaks, which is what a major version is for. It is the same
call 4.0.0 made when subtitle downloading and the second runner were deleted.

## 5. Check what people will actually get

```bash
pip install --upgrade organizekit     # or: pipx install organizekit
organize doctor
organize --version                    # 5.0.0
organize run --dry-run                # four steps: extractor, cleaner, 10bit, auditor
```

And the no-install path, which is the one that matters on a NAS:

```bash
curl -LO https://github.com/smeltzzz/organize/releases/download/v5.0.0/organize.pyz
python3 organize.pyz doctor
```

Worth saying in whatever you announce with the release: **subtitle timing sync
is gone**, and **`organize run` is four steps** — extract → clean → 10-bit →
audit. `organize sync` is no longer a command, `organize doctor` no longer
looks for `ffsubsync` or for the `ffmpeg` binary (it still looks for `ffprobe`),
and `organize status` no longer prints a `Sync` row. Nothing is migrated and
nothing needs cleaning up on an existing library: sidecars already on disk are
untouched, the extractor's provenance ledger keeps working (it simply no longer
records a `synced_utc` stamp), and any `sync` verdicts left in `state.db` are
never queried — delete the file if you want them gone, which costs one slow
pass and nothing else. Uninstalling ffsubsync is now safe and, on a library
whose sidecars come from the movies themselves, was always optional.

---

## If something goes wrong

**The tag was pushed with the wrong version.** Delete it and tag again;
nothing was published, because the version gate fails before the build:

```bash
git push --delete origin v5.0.0 && git tag -d v5.0.0
```

**PyPI rejects the upload as "not configured".** Only possible if the
registration in step 3 was undone; the environment name `pypi` is the field
most often wrong.

**A held patch will not apply.** Something changed under `.github/workflows/`
since it was written. `git apply --3way <the patch>` resolves the common cases;
otherwise the patch header says what the change is meant to achieve, and the
change is small enough to redo by hand - a comment, for the patch held today,
and three edits for the 5.0.0 one. `tests/test_docs.py` fails while a patch
neither applies nor is already committed, so this cannot rot unnoticed.

**`organize run` still prints a `sync` step.** You are running an installed
copy, not the checkout. `pip show -f organizekit | grep sync_subtitles` should
print nothing; if it does, `pip install --upgrade organizekit` (or reinstall
the `.pyz`) and check `organize --version` says `5.0.0`.
