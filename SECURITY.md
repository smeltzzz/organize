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
- **Off by default, identity-based, and hardened when on.** The toolkit makes no
  network request at all unless `OPENSUBTITLES_API_KEY` is set. The one tier
  that uses it runs only for a movie whose own tracks prove it carries English
  subtitles and whose English subtitles exist *only* as bitmaps, and it asks for
  `moviehash_match=only` — subtitles the provider itself matched to the hash of
  the exact file on disk. There is no title, year or release-name search
  anywhere, so a wrong-cut subtitle cannot be installed.
- **Untrusted provider answers are treated as untrusted.** A download link is
  dereferenced only over HTTPS and only on `opensubtitles.com` /
  `opensubtitles.org` (a lookalike host such as
  `opensubtitles.com.evil.example` is refused); the payload is byte-capped and
  gzip-aware, must decode as subtitle text, must parse into well-formed cues
  with a plausible cue floor, and must read as English. The movie is re-checked
  between the search and the write, so a file that changed under the hash is
  never given a subtitle for the bytes it used to be, and the sidecar is
  published create-only — a hand-placed subtitle is never overwritten.
  Credentials are sent only to the provider's own `/login` endpoint, and a
  failed attempt is reported without echoing them.
- **Credentials stay out of the command line.** The OpenSubtitles API key and
  the optional account name and password are read only from the environment or
  from a `.env` file beside the scripts — never from a flag, where they would
  land in shell history and process lists. They are never printed, logged, or
  written to a report or the provenance ledger, and `organize.py doctor` reports
  whether the key works, not what it is.

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
