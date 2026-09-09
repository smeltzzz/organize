# Configuration

Where every tool gets its paths, keys and defaults from — and the one variable
that makes the path flags unnecessary.

---

Everything is overridable per run with CLI flags (see each tool's
`--help`). Environment variables (with defaults and annotations) live in
[`.env.example`](../.env.example). In short:

| Variable | Used by | Purpose |
| :--- | :--- | :--- |
| `ORGANIZE_LIBRARY` | **every tool** | The movie-library root. Set this one variable and no tool needs a path flag. |
| `MOVIE_STD_SOURCE` | movie_standardizer / `doctor` | Completed-download root to ingest from (platform default: `E:\torrents\final` on Windows, `~/torrents/final` elsewhere) |
| `MOVIE_STD_TARGET` | every tool (legacy) | Older name for `ORGANIZE_LIBRARY`; still honoured, lower precedence |
| `MOVIE_STD_LOCK_TIMEOUT` | movie_standardizer | Coordination-lock wait (default 60 s) |
| `MOVIE_STD_MAINTENANCE_MODE` | movie_standardizer | `REPORT` (default) / `QUARANTINE` / `DELETE` for duplicates |
| `ORGANIZE_STATE_DB` | auditor / cleaner / 10-bit / sync / `status` | Where the shared state cache lives (default: beside the logs and reports, never inside the library) |
| `ORGANIZE_NO_STATE` | the same tools | Set to `1` to turn the cache off everywhere at once (equivalent to passing `--no-state`) |
| `SUBTITLE_EXTRACTED_LEDGER` | subtitle_extractor / sync | Where the extraction provenance ledger lives (default: `ReportsAndLogs/subtitle_extractor_extracted.json`, outside the library). `sync_subtitles.py` reads it to know which sidecars it may measure |
| `PGSTOSRT_DLL` | subtitle_extractor | Path to the PgsToSrt `.dll` (it runs as `dotnet <dll>`), when that OCR backend is wanted |

A `.env` file next to the scripts is read automatically at startup by every
tool; anything already exported in the environment wins over the file.

Path defaults are platform-aware. On Windows they follow the documented
`E:\torrents\...` layout; on Linux/macOS the library defaults to
`~/Media/Movies`, the completed-download batch-scan root to `~/torrents/final`,
and logs, reports and the state cache to `$XDG_STATE_HOME/organize`
(`~/.local/state/organize`). Source and target **must be on the same
filesystem** (hardlink-only ingest). `organize.py doctor` resolves both roots
through the same rules, so it can never disagree with the tools about which
folder needs attention.

---

[← Back to the README](../README.md) · [Tool reference](tools.md) ·
[The pipeline](pipeline.md)
