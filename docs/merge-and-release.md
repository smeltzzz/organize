# Cutting a release, and the recorded 5.0.0 run

> **v8.6.1 was cut and published on 2026-10-06.** [PR #64](https://github.com/smeltzzz/organize/pull/64)
> merged into `main` as `e3db1c9`; the `v8.6.1` tag points at that merge commit,
> not at the branch head; and the tag's `release.yml` ran green end to end — the
> suite gate, the wheel/sdist/zipapp build, the **PyPI publish**
> ([organizekit 8.6.1](https://pypi.org/project/organizekit/8.6.1/)), and the
> three assets on the
> [GitHub release](https://github.com/smeltzzz/organize/releases/tag/v8.6.1)
> (`organize.pyz`, the wheel, the sdist). So `pip install --upgrade organizekit`
> already resolves to it; from a checkout, `python3 -m pip install --upgrade .`.
>
> What it shipped is in the [changelog](../CHANGELOG.md): a chain-fact audit that
> found the published facts correct and three of their consequences unmodelled —
> the DTS family not width-capped as a family (a DTS core tops out at 5.1, and
> achievable layout is the *first* key of an irreversible keep-one-track
> decision, so an uncapped `DTS:X` label could delete a file's only Atmos track),
> ALAC/WavPack credited with a software-decode path and a lossless ranking rung
> this player does not give them, and the 24-bit/96 kHz decode ceiling absent
> from the table entirely (it is a *report* — `review-unknown`, never a transcode
> and never a ranking key, because what a 24/192 stream does on this box has not
> been measured).
>
> **Nothing is outstanding.** No workflow file, held patch, tag or secret is
> waiting on a human, and the suite (2,359 tests, offline) plus `Lint (ruff)`,
> `Coverage gate`, `Packaging` and the single-file build are green on `main`.
>
> The next release follows the shape recorded below. One rule carries forward
> every time, because it is the only step that can be done wrong quietly: **tag
> the merged commit on `main`, never the open branch** — a tag on an unmerged
> head publishes a wheel while `main` still reports a different version, and
> leaves the changelog section unreachable from the released commit. A release is
> a *merge plus a tag*, not a merge.
>
> And when a release lands, replace this block. The note this one replaced said
> "8.0.1 is prepared in PR #45 — what remains is the merge and the tag", written
> the day before that tag was cut and left standing six releases too long, which
> is precisely how a release document starts pointing its reader at work already
> done.

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
run was green on the same merge: the whole suite (2,359 tests) on Linux, macOS
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
