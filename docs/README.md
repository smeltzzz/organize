# Documentation

The [README](../README.md) is the front door: what this is, why it exists, and
how to get a library organized in five commands. Everything that needs more
than a screen lives here.

| Document | What's in it |
| :--- | :--- |
| [Tool reference](tools.md) | Every tool in detail — what it decides, why, and the flags worth knowing. |
| [The pipeline](pipeline.md) | The qBittorrent hook, the five maintenance steps, the order that is load-bearing, and how to read the reports. |
| [Configuration](configuration.md) | Environment variables, the `.env` file, and the platform-aware path defaults. |
| [Testing & development](development.md) | The offline suite, the single-file `organize.pyz` build, the field smoke tests, and the crash tests. |
| [Merging & releasing](merge-and-release.md) | The steps that need a permission the branch bot does not have: merging into main, applying the held workflow patch, and tagging the release. |

Also in this folder:

- [`ci-workflow.patch`](ci-workflow.patch) — changes to
  `.github/workflows/ci.yml` held as a patch rather than a commit, because
  the bot that pushes this branch has no `workflows` permission: the syntax
  gate's byte-compile list still names `subtitle_fetcher.py` (renamed to
  `subtitle_extractor.py` in 4.0.0) and `jellyfin_one_shot.py` (deleted with
  the second runner — the pipeline is the only thing that runs the
  toolchain), and the coverage floor drops 88% → 85% to follow the code that
  was deleted. Apply with `git apply docs/ci-workflow.patch`. (The
  release-workflow patch that used to sit beside it has been applied and
  committed; `release.yml` is live.)
- [`ci-provisioned-job.patch`](ci-provisioned-job.patch) — adds a `provisioned`
  job to `.github/workflows/ci.yml`, held for the same missing `workflows`
  permission. It runs the suite on Windows with MKVToolNix, FFmpeg and
  ffsubsync actually installed: every other job runs on a machine that has none
  of them, so a test that consulted the host toolchain looked green nine times
  while failing on a real workstation. The suite now pins those lookups
  ([`tests/hermetic.py`](../tests/hermetic.py)); this job is what keeps them
  pinned. Apply with `git apply docs/ci-provisioned-job.patch`.

Elsewhere in the repo: [`CHANGELOG.md`](../CHANGELOG.md) (what changed and
why), [`OVERHAUL.md`](../OVERHAUL.md) (the measured plan the recent work
follows), [`CONTRIBUTING.md`](../CONTRIBUTING.md),
[`SECURITY.md`](../SECURITY.md), and [`benchmarks/`](../benchmarks/README.md)
(the scripts behind every speed claim).
