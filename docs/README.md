# Documentation

The [README](../README.md) is the front door: what this is, why it exists, and
how to get a library organized in four commands. Everything that needs more
than a screen lives here.

| Document | What's in it |
| :--- | :--- |
| [Tool reference](tools.md) | Every tool in detail — what it decides, why, and the flags worth knowing. |
| [The pipeline](pipeline.md) | The qBittorrent hook, the four maintenance steps, the order that is load-bearing, and how to read the reports. |
| [Configuration](configuration.md) | Environment variables, the `.env` file, and the platform-aware path defaults. |
| [Testing & development](development.md) | The offline suite, the single-file `organize.pyz` build, the field smoke tests, and the crash tests. |
| [Merging & releasing](merge-and-release.md) | The steps that need a permission the branch bot does not have: merging into main, applying the held workflow patch, and tagging the release. |

Also in this folder — two sets of changes to `.github/workflows/ci.yml`, held
as patches rather than commits because the bot that pushes these branches has
no `workflows` permission:

- [`ci-workflow.patch`](ci-workflow.patch) — **already applied and committed**;
  kept for the record. It renamed `subtitle_fetcher.py` to
  `subtitle_extractor.py` in the syntax gate's byte-compile list, dropped
  `jellyfin_one_shot.py` (deleted with the second runner), and lowered the
  coverage floor 88% → 85% to follow the code that was deleted. `git apply`
  fails on it and `git apply --reverse` succeeds, which is how the suite knows
  it is done. (The release-workflow patch that used to sit beside it has been
  applied and committed too; `release.yml` is live.)
- [`ci-workflow-sync-removal.patch`](ci-workflow-sync-removal.patch) —
  **waiting to be applied.** It drops the deleted `sync_subtitles.py` from the
  byte-compile list, stops the `provisioned` job from installing and requiring
  ffsubsync, and follows the tool count down from five steps to four. Until it
  is applied the `Byte-compile` job is red on a branch that has deleted that
  file. Apply with `git apply docs/ci-workflow-sync-removal.patch`; see
  [Merging & releasing](merge-and-release.md).
Elsewhere in the repo: [`CHANGELOG.md`](../CHANGELOG.md) (what changed and
why), [`OVERHAUL.md`](../OVERHAUL.md) (the measured plan the recent work
follows), [`CONTRIBUTING.md`](../CONTRIBUTING.md),
[`SECURITY.md`](../SECURITY.md), and [`benchmarks/`](../benchmarks/README.md)
(the scripts behind every speed claim).
