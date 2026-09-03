# Code & Workflow Review — `organize`

> **Status: the "Now" and "Next" tiers of §5 are implemented** (commits on
> `arena/01a067a9-organize`). Items 1–9 are done; §5's "Then" tier is
> deliberately bounded: each remaining item is a large behavioural change that
> wants its own review, and this session has taken the highest-value one of
> them —
> * `process_mkv` is now split into a pure `plan_cleanup()` decision plus the
>   I/O executor (§5.10), which pushed the riskiest tool's coverage up
>   substantially.
>
> The items deliberately left open remain: collapsing one-shot into a loop over
> `pipeline`, moving the self-tests, adding `--workers`, and the SQLite state
> store.
>
> Also landed in this session (beyond §5): `organize.py doctor` no longer
> imports the deleted `10bit` module (it always claimed ffprobe was missing)
> and now resolves the library/source roots through the same shared resolvers
> as the tools, the standardizer's batch-scan source root is platform-aware,
> and the CI workflow changes are committed directly (the old
> `ci-improvements.patch` is gone).
>
> Note: the CI workflow changes shipped as `ci-improvements.patch` rather than
> as a committed `.github/workflows/ci.yml` edit, because the Arena GitHub App
> was not permitted to push workflow files. That patch has since been applied
> and committed, and the file removed.

An outside read of all ~25,000 lines of Python (plus tests, CI, packaging, docs),
looking at the repo as a *workflow* rather than as nine separate scripts.

---

## 0. Verdict up front

This is genuinely good work, and unusually so in the places that normally rot:

- **503 tests, green, in 6 seconds**, fully offline. No media, no binaries, no network.
- **`ruff check .` passes clean** on 25k lines.
- **67% line coverage** without a single mock library.
- The **safety engineering is real**: atomic writes via `os.replace`, transaction
  journals in the remuxer, fail-closed HDR detection, advisory run locks that work
  on both Windows and POSIX, hardlink-aware deferral so a seeding torrent is never
  remuxed.
- The **domain reasoning is documented where it matters**. The moviehash ordering
  constraint (fetch before remux, because a remux rewrites the bytes the hash is
  computed over) is explained in `pipeline.py`'s docstring, restated in the step
  hints, and *enforced by an ordered tuple*. That is exactly right.

So the criticism below is not "this is bad." It is "this is a well-built thing that
has hit the specific ceiling that the single-file-script architecture imposes,"
plus a handful of concrete defects.

The one-line answer to *"is there a better way to do what this repo is trying to
do?"*: **the goal is right, the tools are right, but "nine self-contained files
with vendored copies of shared helpers" is now costing more than it buys** — and
I can show you that with numbers rather than taste.

---

## 1. Bugs and concrete defects

### 1.1 The declared console entry point is broken 🔴

`pyproject.toml` declares:

```toml
[project.scripts]
organize = "organize:main"
```

But there is no `build-system` table and no package — just loose `.py` files at the
repo root. The install "succeeds" and then the command is dead:

```console
$ pip install -e .
Successfully installed organize-3.4.0
$ organize --help
ModuleNotFoundError: No module named 'organize'
```

Setuptools' auto-discovery finds no package or module to include, so it ships a
wheel with metadata and a script shim pointing at nothing. The README tells people
to run `pip install -e .[dev]` (line 572), so this is on the documented path.

CI never catches it because every job invokes `python organize.py` by path, never
the installed console script.

**Fix (minimal):** add a build backend and explicitly declare the modules.

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
py-modules = ["organize", "pipeline", "library_auditor", "mkv_track_cleaner",
              "movie_standardizer", "subtitle_fetcher", "sync_subtitles",
              "jellyfin_one_shot"]
```

Note `10bit.py` **cannot** be a `py-module` — an identifier can't start with a
digit, which is why `pipeline.py` already has this wart:

```python
try:
    import _10bit          # type: ignore  # module name starts with a digit
except Exception:
    probe = importlib.import_module("10bit")   # fallback
```

That two-branch dance, in production code, is the filename apologising for itself.
**Rename `10bit.py` → `bitdepth.py`** (keep a thin shim if you care about muscle
memory). It's the single cheapest structural win in the repo.

Then add the regression guard CI is missing:

```yaml
- run: pip install .
- run: organize doctor          # exercises the console script, not the path
```

### 1.2 `jellyfin_one_shot.py` will skip steps on a working machine 🟠

Three different modules answer "is mkvmerge installed?" and they don't agree.

`mkv_track_cleaner.resolve_mkvmerge_path()` — the real one — checks `PATH` *and*
a fallback list:

```python
KNOWN_MKVMERGE_PATHS = [
    r"C:\Program Files\MKVToolNix\mkvmerge.exe",
    r"C:\Program Files (x86)\MKVToolNix\mkvmerge.exe",
    "/usr/bin/mkvmerge", "/usr/local/bin/mkvmerge", "/opt/homebrew/bin/mkvmerge",
]
```

`pipeline.py` correctly delegates to it. `organize.py doctor` correctly delegates
to it. But `jellyfin_one_shot.py` rolls its own:

```python
def check_prerequisites(runtime_log: Path) -> dict[str, bool]:
    tools = {
        "mkvmerge":  shutil.which("mkvmerge")  is not None,
        "ffprobe":   shutil.which("ffprobe")   is not None,
        "ffsubsync": shutil.which("ffsubsync") is not None,
        "ffmpeg":    shutil.which("ffmpeg")    is not None,
    }
```

`shutil.which` only. So the **standard Windows MKVToolNix install** — which does
not add itself to `PATH` — reports `mkvmerge: NOT FOUND` in one-shot, while
`organize.py doctor` on the same machine prints a green ✔ and the version string.
Windows is the primary documented platform. Same divergence for `ffsubsync`, where
`sync_subtitles.find_ffsubsync()` additionally accepts the `ffs` and `subsync`
entry points that `which("ffsubsync")` misses.

The tell that this is drift rather than intent: the returned dict is computed at
line 1838 and, apart from logging, **never read again**. It's a vestigial check
that now only misinforms the log.

**Fix:** one `prerequisites.py` with one implementation; all three callers import it.
This is the concrete cash value of §2.

### 1.3 Generated runtime artifacts are committed 🟠

`.gitignore` carefully excludes `*.log`, `*_report.txt`, `*probe_cache*.json`, and
even `ReportsAndLogs/`. But 3.9 MB of exactly those files are **already tracked**,
so the ignore rules do nothing:

```console
$ git ls-files ReportsAndLogs | wc -l
13
$ du -sh ReportsAndLogs
3.9M
```

Worse than clutter — it's a privacy leak. 7,397 lines of report contain a real
library's full contents and absolute paths:

```
E:\torrents\final_organized\Backrooms
```

Someone's actual movie collection and directory layout are in the public history.

**Fix:** `git rm -r --cached ReportsAndLogs/`. If you want committed samples for
the docs, hand-write a small redacted fixture under `docs/examples/` and
force-add that one file. (History rewriting is a separate, bigger decision —
but stop the bleeding now.)

### 1.4 Version numbers have no single source of truth 🟡

Eight independent, mutually inconsistent version constants:

| Location | Version |
|---|---|
| `pyproject.toml` | **3.4.0** |
| `organize.py` | **3.5.0** |
| `10bit.py` | 2.4.0 |
| `mkv_track_cleaner.py` | 2.6.2 |
| `library_auditor.py` | 2.1.0 |
| `sync_subtitles.py` | 1.2.0 |
| `jellyfin_one_shot.py` | 1.3.1 |
| `pipeline.py` | 1.0.0 |
| `movie_standardizer.py`, `subtitle_fetcher.py` | *(none at all)* |

The packaged version already disagrees with the CLI's own `--version`. Per-tool
versions are defensible *if* the tools are genuinely distributed standalone — but
then the two that lack one are the bug, and the wheel should still match `organize.py`.

### 1.5 86 bare `except Exception` blocks 🟡

42 of them in `mkv_track_cleaner.py` alone — the one tool that *moves and deletes
user data*. Many are correctly narrow-in-spirit (probe parsing, version banners),
but a swallowed `MemoryError`, `KeyboardInterrupt`-adjacent failure, or a genuine
`OSError` mid-transaction in a remuxer is a different risk class. Worth auditing
that file's handlers specifically and tightening to `OSError` /
`json.JSONDecodeError` / `subprocess.SubprocessError` where the intent is
actually "tolerate a bad probe."

Add to ruff's select list to keep score: `["E","F","W","B","I","UP","BLE","SIM","RET","PTH"]`.
`BLE` (blind-except) is exactly this.

### 1.6 `ruff` is configured but never runs 🟡

`pyproject.toml` has a full `[tool.ruff]` section. `ci.yml` mentions ruff **zero**
times. It currently passes — so wire it in now, while it's free, and it stays passing.

---

## 2. The architectural issue: vendored helpers have already drifted

This is the main event, and the repo is explicit about the policy:

> ```python
> # This script is self-contained on purpose: every helper it needs is copied
> # below instead of imported from a shared module... The other scripts in this
> # repo carry byte-identical copies of the same helpers; if you change one,
> # keep the others in sync.
> ```

**The claim is no longer true.** I parsed every top-level `def`/`class` and
compared bodies:

```
exact duplicate top-level def/class lines (beyond the first copy): 3,420
```

That's **13.5% of the codebase** as literal copy-paste. Per file:

| File | Lines | Duplicated | % |
|---|---:|---:|---:|
| `pipeline.py` | 997 | 446 | **44%** |
| `library_auditor.py` | 1,384 | 526 | **38%** |
| `mkv_track_cleaner.py` | 4,086 | 736 | 18% |
| `movie_standardizer.py` | 3,739 | 639 | 17% |
| `sync_subtitles.py` | 2,301 | 389 | 16% |
| `subtitle_fetcher.py` | 7,804 | 684 | 8% |

`pipeline.py` is a **997-line file that is 44% boilerplate** — its actual job,
orchestrating five subprocesses, is about 300 lines.

And the "byte-identical" invariant has already failed. Same name, divergent body:

| Symbol | Similarity | Nature of drift |
|---|---:|---|
| `atomic_write_text` (`10bit` vs `subtitle_fetcher`) | **0.39** | one adds `fsync`, `newline="\n"`, `O_EXCL`, and a no-clobber `os.link` mode |
| `ExclusiveRunLock` (`10bit` vs `library_auditor`) | 0.84 | different Windows byte-materialisation logic + restructured retry loop |
| `enable_utf8_stdio` (`10bit` vs `one_shot`) | 0.50 | docstring only, but proves nobody is diffing |
| `log` (`10bit` vs `library_auditor`/`sync_subtitles`) | 0.91 | small real differences |

The `atomic_write_text` one is the serious case. `subtitle_fetcher`'s copy is
**strictly safer** — it fsyncs before rename (survives power loss, not just crash)
and can refuse to clobber an existing sidecar. Every other tool, including the one
that rewrites your movie files, uses the weaker version that skips `fsync`. A
durability fix was made once and never propagated. That is precisely the failure
mode the vendoring policy was supposed to prevent, and it happened anyway — because
"keep 7 copies in sync by hand" is not a process, it's a hope.

The self-tests embedded in production files add another **2,214 lines (8%)**:
`subtitle_fetcher.py` carries 1,047 lines of `run_self_tests` — a **475-line single
function** — inside the shipped file, *on top of* the 1,811-line `tests/test_subtitle_fetcher.py`
that tests the same module.

### What I'd actually do

Keep the zero-dependency, stdlib-only promise. Keep the tools independently runnable.
Just stop copy-pasting:

```
organize/
├── organize/
│   ├── __init__.py
│   ├── _common/
│   │   ├── report.py        # Report, wrap_text, clip_text, wrap_path_text  (~450 lines, once)
│   │   ├── io.py            # atomic_write_text (the GOOD one), enable_utf8_stdio, print_text
│   │   ├── locking.py       # ExclusiveRunLock, LockUnavailable, try_file_lock
│   │   ├── probecache.py    # the mtime+size cache duplicated in 10bit & track_cleaner
│   │   ├── prereqs.py       # ONE mkvmerge/ffprobe/ffsubsync resolver  → fixes §1.2
│   │   └── config.py        # ONE library-root resolution              → fixes §3.1
│   ├── bitdepth.py          # was 10bit.py                             → fixes §1.1
│   ├── subtitles/ fetch.py sync.py
│   ├── mkv/ clean.py
│   ├── library/ standardize.py audit.py
│   ├── pipeline.py
│   └── cli.py               # was organize.py
└── tests/
```

Every tool keeps `if __name__ == "__main__": main()` and `python -m organize.bitdepth`
still works standalone. **Projected: ~25,200 → ~19,500 lines, with strictly more
safety** (everyone inherits the fsync-ing writer).

**If the single-file property is genuinely non-negotiable** — e.g. you drop
`subtitle_fetcher.py` onto a NAS by itself — then don't hand-maintain the copies.
Keep `_common/` as the one source of truth and add a `build.py` that inlines it
into standalone distributables, plus a CI check that the committed copies match
what the generator produces. Machine-enforced vendoring is fine; manual vendoring
is what produced the `atomic_write_text` divergence.

---

## 3. Workflow-level observations

### 3.1 The library root is resolved four different ways

`DEFAULT_LIBRARY = r"E:\torrents\final_organized"` is hardcoded in at least three
files, with a comment in `jellyfin_one_shot.py` explaining that all six tools
*must* keep the literal in sync — the same fragile pact as §2.

Meanwhile `MOVIE_STD_TARGET` (the escape hatch) is honoured by **only 4 call sites
across 2 files**. So on Linux, some tools follow your env var and others cheerfully
default to a Windows drive letter that doesn't exist. The `.gitignore` even has a
rule for the resulting damage:

```gitignore
# Running the tools on a non-Windows host with default config writes literal
# filenames such as `E:\torrents\tools\ReportsAndLogs\10bit\10bit_report.txt`
# into the CWD.
E:*
```

A `.gitignore` rule that exists to catch the fallout of a config bug is a strong
signal to fix the config bug. And note there's a `.env.example` but **nothing in
the codebase ever reads a `.env` file** — the template documents variables that
only take effect if the user manually exports them.

**Fix:** one `resolve_library()` in `_common/config.py`, precedence
`--source` → `ORGANIZE_LIBRARY` (with `MOVIE_STD_TARGET` as deprecated alias) →
platform default (`E:\...` on Windows, `~/Media/Movies` elsewhere) → **fail loudly
if it doesn't exist**, rather than writing `E:*` into the CWD. Add a tiny stdlib
`.env` loader (~15 lines) or delete `.env.example`.

### 3.2 Three orchestrators, one pipeline

There are three layers that each independently know the tool order and how to
build argv:

1. `pipeline.py` — `STEPS` dict, `build_command()`, `PREREQUISITES` table
2. `jellyfin_one_shot.py` — its own `TOOL_SCRIPTS` tuple, its own step table, its own arg construction, its own `check_prerequisites()`
3. `organize.py` — `delegate_to_script()`, plus its own doctor checks
4. (`jellyfin_completer.sh` — a fourth, wrapping #2 with bash arg parsing)

`jellyfin_one_shot.py` **never imports `pipeline.py`**. It is "run the pipeline in
a loop until the auditor says 100%," which is `pipeline.run()` inside a `while`,
but it's a 2,024-line reimplementation containing a **662-line `run_one_shot()`**
and a 207-line `main()`.

**Fix:** one `Step` registry. `pipeline` = one pass; `one-shot` = `while not
audit.is_clean(): pipeline.run_pass()` with the convergence policy (max passes,
bad-audit backoff, UTC-midnight wait) layered on top. That convergence logic is
genuinely good and worth keeping — it just shouldn't come with a duplicate copy
of the step table. Realistically one-shot drops to ~600 lines.

The bash wrapper adds a fifth arg-parsing surface for flags Python already parses;
I'd reduce it to a thin `exec python3 jellyfin_one_shot.py "$@"` or delete it.

### 3.3 Parallelism is inconsistent

`10bit.py` has a proper `ThreadPoolExecutor` with `--workers`. `subtitle_fetcher.py`
(network-bound — the *most* parallelisable step, and the slowest in wall-clock
terms) is serial. `sync_subtitles.py` (CPU-bound ffsubsync, embarrassingly parallel
per-file) is serial. On a 500-movie library that's the difference between an
afternoon and a coffee break.

Standardising `--workers` via a shared helper is a large real-world speedup, though
the fetcher needs it coupled to its rate-limit accounting.

### 3.4 Function sizes

```
770  subtitle_fetcher.queue_run
662  jellyfin_one_shot.run_one_shot
475  subtitle_fetcher.run_self_tests
446  mkv_track_cleaner.process_mkv
343  organize.run_doctor
```

A 770-line function can't be unit-tested except end-to-end, which is why
`mkv_track_cleaner.py` sits at **39% coverage** — the lowest in the repo and the
tool with the most destructive potential. `process_mkv` alone is 446 lines mixing
probe → decide → build argv → execute → verify → journal → rename. Split into
`plan_remux(info) -> RemuxPlan` (pure, trivially testable, table-driven tests) and
`execute_remux(plan)` (I/O) and coverage of the decision logic goes to ~95% almost
for free.

### 3.5 Testing

Strong foundation — 503 tests, offline, 6 seconds. Gaps:

- **No coverage gate.** Add `coverage run` + `--fail-under=65` so it ratchets up.
- **Self-tests duplicate `tests/`.** Pick one home. If `--self-test` exists for
  field diagnostics on a machine without the repo, make it a thin smoke check
  (~20 lines/tool), not 1,047 lines shipped inside the fetcher.
- **No property-based tests** where they'd shine: filename parsing, `wrap_path_text`,
  SRT timestamp math. `hypothesis` as a dev-only dep doesn't touch the runtime
  zero-dependency promise.
- **CI runs `unittest discover`** while `pyproject.toml` configures pytest with
  `testpaths`/`addopts`. Config that CI ignores drifts. Pick one.
- **No `pip install .` + entry-point test** — the gap that hid §1.1.

### 3.6 Documentation

585-line README, 165-line CHANGELOG, `SECURITY.md`, `CONTRIBUTING.md`, issue
templates. This is better documented than most commercial code and the prose
genuinely explains *why* (the moviehash constraint especially). Two notes:

- A 585-line README is where users stop reading. Split into `docs/` (install /
  per-tool reference / troubleshooting) with the README as a 100-line front door.
- The moviehash ordering constraint is the single most important invariant. It's
  in a docstring and a hint string — but there's no test asserting
  `STEP_ORDER.index("fetcher") < STEP_ORDER.index("cleaner")`. Three lines, and
  the invariant can never silently regress.

---

## 4. "Is there a better way to do this?"

Taking the question seriously at the strategy level:

**Should this be Sonarr/Radarr + Bazarr + Tdarr instead?** For most people, yes —
that stack does subtitle fetching (Bazarr), transcode queueing (Tdarr), and
naming/hardlinks (Radarr). But your repo does things they don't: the **moviehash-
before-remux ordering** is a genuinely sharp insight most setups get wrong, the
**hardlink-aware seeding deferral** is more careful than Radarr's default, and
**fail-closed HDR protection** beats Tdarr's heuristics. The zero-dependency,
no-Docker, single-file property is a real feature for people who don't want to run
five containers. So: not obsolete — but the README should say *why you'd choose
this over Bazarr*, because that's the first question any new user has.

**Should the tools be a library + thin CLIs rather than scripts?** Yes — that's §2.

**Should orchestration be a real DAG runner?** No. Five sequential steps with one
ordering constraint doesn't need Airflow. `pipeline.py`'s explicit ordered tuple is
the right call. Resist scope creep here.

**Should it use a database for state?** There are already JSON ledgers, probe
caches, and transaction journals scattered per-tool. A single SQLite file
(**stdlib!**) as the library state store — one row per movie: last probe, subtitle
status, remux status, sync status, content hash — would unify five ad-hoc state
files, make "what's left to do?" a query instead of a filesystem walk, and make the
whole pipeline incrementally resumable. This is the highest-leverage *new* idea
here and it costs zero dependencies.

**Should it be a long-running service?** Tempting (watch the library, react to
events) but it would trade the current excellent property — *stateless, idempotent,
safe to Ctrl-C at any point* — for a daemon you have to babysit. Keep it batch. The
qBittorrent completion hook already covers the event-driven half.

---

## 5. Suggested order of work

**Now (hours, high value)**
1. Fix the broken entry point + add `build-system` (§1.1); add `pip install .` to CI
2. `git rm -r --cached ReportsAndLogs/` (§1.3) — privacy leak
3. Wire `ruff check` into CI (§1.6); add `BLE`, `SIM`, `RET`, `PTH`
4. Add the `STEP_ORDER` invariant test (§3.6)
5. Single-source the version (§1.4)

**Next (days)**

6. Rename `10bit.py` → `bitdepth.py`, delete the `importlib` fallback (§1.1)
7. Extract `_common/` — start with `io.py`, propagating the **fsync-ing**
   `atomic_write_text` everywhere (§2). Biggest safety win in the list.
8. One `prereqs.py`; delete one-shot's `check_prerequisites` (§1.2)
9. One `resolve_library()`; make `E:*` in `.gitignore` unnecessary (§3.1)

**Then (weeks)**

10. Split `process_mkv` into plan/execute; drive coverage of the riskiest file up (§3.4)
11. Make one-shot a loop over `pipeline` (§3.2) — deletes ~1,400 lines
12. Move self-tests into `tests/`, leave thin smoke checks (§3.5)
13. Add `--workers` to the fetcher and sync steps (§3.3)
14. Consider the SQLite state store (§4)

---

## 6. What to keep

Do not let a refactor erode these — they're the best parts:

- The moviehash ordering constraint, documented *and* enforced
- Fail-closed defaults everywhere (uncertain → never queued, never cleaned)
- Hardlink-aware deferral for seeding torrents
- Transaction journals + atomic replace in the remuxer
- Cross-platform locking that actually handles Windows byte-range semantics
- The UTF-8/cp1252 console handling — unglamorous, and clearly learned from real CI failures
- Offline, hermetic, 6-second tests
- Prose that explains *why*, not *what*

The instinct behind the single-file design — "a user should be able to copy one
file and run it" — is a good instinct serving real users. The recommendation isn't
to abandon it. It's that you've outgrown *hand-maintained* vendoring, and the
proof is that the copies already disagree in a way that leaves your most dangerous
tool with your least durable file writer.
