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

Also in this folder: one held patch, **`ci-workflow-comment.patch`** — a
comment-only change to the header of `.github/workflows/ci.yml`, held because
the branch bot has no `workflows` permission (see
[Merging & releasing](merge-and-release.md), which starts with how to apply
it). The three earlier patches — `ci-workflow.patch`,
`ci-workflow-sync-removal.patch` and `release-workflow.patch` — have all been
applied and committed, and their files are gone; what each changed is recorded
in [Merging & releasing](merge-and-release.md) and
[`CHANGELOG.md`](../CHANGELOG.md). `tests/test_docs.py` checks every patch held
here: that it still applies (or is already committed), that its header says why
it is held, that it names the distribution this repo actually builds, and that
it is listed on this page.

Elsewhere in the repo: [`CHANGELOG.md`](../CHANGELOG.md) (what changed and
why), [`OVERHAUL.md`](../OVERHAUL.md) (the measured plan the recent work
follows), [`CONTRIBUTING.md`](../CONTRIBUTING.md),
[`SECURITY.md`](../SECURITY.md), and [`benchmarks/`](../benchmarks/README.md)
(the scripts behind every speed claim).
