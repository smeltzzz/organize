# Security Policy

## Scope

`organize` operates directly on your media library and torrent downloads, so
its safety model is treated as a security concern. The guarantees below are
enforced in code and covered by the offline test suite:

- **Never destroys unique data.** Ingestion is hardlink-only (`os.link`), the
  track cleaner verifies a remux before atomically swapping it in, and every
  report/manifest/sidecar write is staged and atomically replaced.
- **Never follows symlinks out of your library.** Symlinked movies, subtitle
  sidecars, and nested files inside torrent folders are skipped, never linked
  or rewritten.
- **Fail-closed concurrency.** Every tool coordinates through advisory locks;
  a lock that cannot be acquired halts the tool instead of racing.
- **Provider hardening.** The subtitle fetcher only dereferences absolute
  HTTPS download links from OpenSubtitles; SubDL raw URLs are restricted to
  `https://dl.subdl.com/subtitle/...` and opaque v2 IDs use a locally-built
  API path. Both providers are byte-capped, archive/gzip-aware, cue-validated,
  snapshot-checked, and atomically published only after validation.
- **Credentials stay out of the command line.** OpenSubtitles and SubDL keys,
  plus optional OpenSubtitles user credentials, are read only from environment
  variables.

## Reporting a vulnerability

If you find a way to make any tool in this repository lose, truncate, or
corrupt media, leak credentials, or execute untrusted content, please report
it privately:

1. Open a [GitHub security advisory](https://github.com/smeltzzz/organize/security/advisories/new)
   (**Security → Report a vulnerability**), or
2. Email the repository owner via the email on the commit history.

Please include the tool, the exact command line, the relevant report/log
output, and — if possible — the smallest reproduction case. You will receive a
response within a few days. Please avoid opening a public issue for anything
that could put other users' libraries at risk.

## Supported versions

| Version | Supported |
| :-- | :-- |
| `main` (latest release line) | ✅ |
| Older releases | ❌ — please update before reporting |
