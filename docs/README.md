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

Also in this folder — nothing is held any more. The two CI patches that used
to live here (`ci-workflow.patch`, which renamed `subtitle_fetcher.py` to
`subtitle_extractor.py` in the syntax gate's byte-compile list, dropped
`jellyfin_one_shot.py`, and lowered the coverage floor 88% → 85%; and
`ci-workflow-sync-removal.patch`, which dropped the deleted `sync_subtitles.py`
and the `ffsubsync` requirements) have both been applied and committed to
`.github/workflows/ci.yml`, and the files are gone — exactly like
`release-workflow.patch` before them. What each changed is recorded in
[Merging & releasing](merge-and-release.md) and in
[`CHANGELOG.md`](../CHANGELOG.md), and `tests/test_docs.py` skips its
held-patch class while `docs/` holds no patches. If a future branch needs a
`workflows` change the bot cannot push, hold it as a new patch here and list
it in this file again.

Elsewhere in the repo: [`CHANGELOG.md`](../CHANGELOG.md) (what changed and
why), [`OVERHAUL.md`](../OVERHAUL.md) (the measured plan the recent work
follows), [`CONTRIBUTING.md`](../CONTRIBUTING.md),
[`SECURITY.md`](../SECURITY.md), and [`benchmarks/`](../benchmarks/README.md)
(the scripts behind every speed claim).
