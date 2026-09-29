# Documentation

The [README](../README.md) is the front door: what this is, why it exists, and
how to get a library organized in four commands. Everything that needs more
than a screen lives here.

| Document | What's in it |
| :--- | :--- |
| [The playback-chain dossier](hardware.md) | The one hardware chain the toolkit is tuned for — Chromecast HD G454V → Hisense AX3125H → Samsung UN60F6350AF — the codec matrix, the wiring guide, and the sources behind every default. |
| [Tool reference](tools.md) | Every tool in detail — what it decides, why, and the flags worth knowing. |
| [The pipeline](pipeline.md) | The qBittorrent hook, the five maintenance steps, the order that is load-bearing, and how to read the reports. |
| [Configuration](configuration.md) | Environment variables, the `.env` file, and the platform-aware path defaults. |
| [Testing & development](development.md) | The offline suite, the single-file `organize.pyz` build, the field smoke tests, and the crash tests. |
| [Merging & releasing](merge-and-release.md) | How a release is cut: merging the prepared PR into main and pushing the tag that publishes to PyPI. |

Nothing is held back in this folder any more: the branch bot's token now has
the `workflows` scope, so changes under `.github/workflows/` ship directly on
the branch like any other file. The two patches that were held at the start
of the 8.0.0 cycle — `ci-workflow-comment.patch` and
`ci-workflow-audiofit.patch` — were applied to the PR's branch on
2026-09-29 and their files removed, like the three before them
(`ci-workflow.patch`, `ci-workflow-sync-removal.patch`, `release-workflow.patch`);
what each changed is recorded in [Merging & releasing](merge-and-release.md)
and [`CHANGELOG.md`](../CHANGELOG.md). `tests/test_docs.py` still checks any
patch held here in future: that it applies (or is already committed), that
its header says why
it is held, that it names the distribution this repo actually builds, and that
it is listed on this page.

Elsewhere in the repo: [`CHANGELOG.md`](../CHANGELOG.md) (what changed and
why), [`OVERHAUL.md`](../OVERHAUL.md) (the measured plan the recent work
follows), [`CONTRIBUTING.md`](../CONTRIBUTING.md),
[`SECURITY.md`](../SECURITY.md), and [`benchmarks/`](../benchmarks/README.md)
(the scripts behind every speed claim).
