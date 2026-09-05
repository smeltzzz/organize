# Overhaul Plan — `organize`

> **Status: Phases 1–5 are landed (W2 in part), and W4b's rate limiting with them** on `arena/01a07259-organize` — the
> shared core (`organizekit/`) replaced 4,325 lines of vendored copies, the
> tools' 2,229 lines of self-test code moved to `tests/selftests/`, and the
> toolchain is now described exactly once in `organizekit/core/toolchain.py`
> instead of once per orchestrator. Production Python is down from 26,458 to
> 20,011 lines (−24%), coverage is up from 58% to 67%, the suite is green at
> 697 tests, and the two slowest read paths now run in parallel
> (`sync_subtitles.py` 3.6×, `library_auditor.py` up to 7.7× on network
> storage). Phases 5–8 below
> are still open; the numbers in the tables are the pre-work baseline unless
> marked otherwise.
>
> **Phase 4 shipped for two of the four steps, and corrected the estimate for a
> third.** The fetcher cannot be made 5–8× faster by threading it: every
> provider client sleeps to a documented rate limit (1.0–1.1 s between
> requests), so it is rate-limit bound, not RTT bound, and eight threads would
> queue behind the same gap. Its real win is per-source token buckets - letting
> OpenSubtitles, SubDL and the scraping tier each run at their own permitted
> rate concurrently instead of one at a time - and that needs the
> concurrency-safe quota ledger from W2. Parallel remuxing is also still not
> done, deliberately: the track cleaner is the tool that rewrites movie files,
> and disk throughput is the bound anyway.
>
> **Phase 3 came in flat on line count, and that is the honest result.** The
> −1,400 estimate below assumed `jellyfin_one_shot.py` was largely a
> reimplementation of `pipeline.py`. Reading it properly, the duplication was
> the *description* of the toolchain (two step tables, two sets of binary
> probes, two skip-reason functions, six hand-written argv lists) — about 450
> lines, now replaced by a 352-line shared module that carries more per step
> than either copy did. What the runner does *around* those calls — streaming a
> subprocess with heartbeats, folding each tool's report into one narrative
> document, the convergence and UTC-rollover policy — is not duplicated
> anywhere, so there was nothing there to delete. The win is that a flag,
> a binary check or a step can no longer exist in one runner and not the other;
> see the drift bug in W3 for what that cost in practice.
>
> A ground-up plan for what this repo should become, written after reading all
> 26,458 production lines and measuring the codebase rather than eyeballing it.
> Companion to [`REVIEW.md`](REVIEW.md), which fixed the defects. This document
> is about the **ceiling**: what "as good as it can possibly be" actually means
> here, and the order to get there.

**Baseline, measured today** (`arena/01a07259-organize`, Python 3.11.2):

| Metric | Value |
| :--- | ---: |
| Production Python | 26,458 lines across 9 files |
| Test Python | 8,026 lines across 13 files |
| Tests | 549, offline, **6.7 s**, green |
| `ruff check .` | clean |
| Coverage | **58%** (gate: 55%) |
| Exact copy-paste across tools | **4,325 lines** |
| Self-test code shipped inside production files | **2,229 lines** |
| Tools with parallelism | **1 of 6** (`bitdepth.py`) |
| `except Exception` handlers | 84 (42 in the tool that deletes data) |

The repo is in good shape. Everything below is about the *remaining* ceiling,
and it is a real one: **~30% of the production code is redundant, the two
slowest steps in a real run are single-threaded, and the toolchain does five
full library walks and up to two subprocess probes per movie per pass.**

---

## 0. Verdict up front

Three structural facts drive everything else:

1. **The single-file promise is still paid for by hand.** `tests/test_vendored_helpers.py`
   now *detects* drift by AST — a genuine improvement — but detection is not
   deduplication. `Report` is 339 lines copied into 7 files (2,034 redundant
   lines from one class). Every future shared-helper change is a 7-file edit
   that CI merely refuses to let you get wrong.

2. **Every tool rediscovers the world from scratch.** Five tools each walk the
   library, each `stat()` every file, each shell out to `ffprobe`/`mkvmerge -J`.
   Two ad-hoc JSON probe caches and one `sync_state.json` partially mitigate
   this; the subtitle fetcher's durable ledger is **reconstructed by parsing its
   own append-only log file on every run**. There is no answer to "what is left
   to do?" that does not involve a filesystem sweep.

3. **The wall clock is dominated by work that is embarrassingly parallel and
   isn't parallelised.** `subtitle_fetcher.py` is network-bound and serial, with
   a fresh TCP+TLS handshake per request. `sync_subtitles.py` runs one
   `ffsubsync` at a time (30–90 s of CPU each). On a 500-movie library that is
   the difference between an afternoon and a coffee break — the prior review
   said this and it is still true.

Fix those three and the codebase gets ~30% smaller, several times faster, and
strictly safer. Everything else in this document is upside on top.

---

## 1. Where the fat is (measured, not guessed)

Exact duplicate top-level definitions, beyond the first copy:

| Symbol | Copies | Redundant lines |
| :--- | ---: | ---: |
| `Report` | 7 | 2,034 |
| `try_file_lock` | 6 | 250 |
| `CoordinationLock` | 4 | 246 |
| `load_dotenv` | 8 | 245 |
| `atomic_write_text` | 6 | 185 |
| `wrap_path_text` | 7 | 132 |
| `MediaProbeCache` | 2 | 121 |
| `_pack_on_separators` | 7 | 114 |
| `default_reports_root` | 8 | 105 |
| `print_text` | 7 | 102 |
| `resolve_library` | 8 | 98 |
| `wrap_text` | 7 | 96 |
| everything else | — | 597 |
| **Total** | | **4,325** |

Plus:

- **2,229 lines** of `run_self_tests` living inside shipped production files —
  1,047 of them in `subtitle_fetcher.py`, which *also* has a 1,811-line test
  file covering the same code.
- **~1,400 lines** in `jellyfin_one_shot.py` that reimplement `pipeline.py`'s
  step table, argv construction, and prerequisite checks. It never imports
  `pipeline`. Both files independently define
  `STEP_ORDER = ("fetcher", "cleaner", "10bit", "sync", "auditor")`.
  *(Corrected while doing the work: the genuinely duplicated description is
  ~450 lines, not 1,400 — the rest of that file is the streaming runner, the
  narrative report and the convergence policy, none of which exists twice. See
  the status note at the top.)*

**Removable without losing a single behaviour: ~7,950 lines (30%).**

Remaining god-functions, all of them untestable except end-to-end:

```
768  subtitle_fetcher.queue_run
661  jellyfin_one_shot.run_one_shot
475  subtitle_fetcher.run_self_tests
399  mkv_track_cleaner.process_mkv     (already split once; still 399)
342  organize.run_doctor
```

Coverage tracks that shape exactly — `mkv_track_cleaner.py` is **41%**, the
lowest in the repo and the only tool that moves and deletes user data.

---

## 2. The seven workstreams

### W1 · One shared core, machine-generated single-file builds

**Problem.** The vendoring policy costs 4,325 lines and makes every shared
change a 7-file edit. The policy exists for one real user story: *"copy one
`.py` onto a NAS and run it."*

**Change.** Keep the story, stop paying for it by hand.

```
src/organize/
├── __init__.py              # VERSION (single source, already done)
├── core/
│   ├── report.py            # Report, wrap_text, clip_text, wrap_path_text, print_text
│   ├── io.py                # atomic_write_text (fsync'ing), enable_utf8_stdio
│   ├── locking.py           # ExclusiveRunLock, CoordinationLock, try_file_lock
│   ├── config.py            # resolve_library, load_dotenv, default_* roots
│   ├── prereqs.py           # ONE mkvmerge/ffprobe/ffsubsync/ffmpeg resolver
│   ├── subtitles.py         # validate_srt_sidecar, decode_srt_bytes, path contract
│   ├── scan.py              # LibraryScan  (see W2)
│   └── state.py             # SQLite store (see W2)
├── tools/                   # fetch.py clean.py bitdepth.py audit.py standardize.py sync.py
├── pipeline.py              # one Step registry (see W3)
└── cli.py
tools/build_standalone.py    # inlines core/ into dist/standalone/<tool>.py
```

`tools/build_standalone.py` emits byte-reproducible single-file scripts; CI runs
it and fails if `dist/standalone/` would change, and runs the **full test suite
against the generated files too**. That is machine-enforced vendoring: the
single-file promise survives, the maintenance cost goes to zero, and the safest
implementation of every helper is automatically the one every tool gets.

A second distribution channel makes the point moot for most users: ship a
**stdlib-only zipapp** (`organize.pyz`, `python -m zipapp`). One file, no
install, no pip, runs the entire toolchain. That is strictly better than
copying `subtitle_fetcher.py` around.

- **Payoff:** −4,325 lines; shared fixes propagate by construction.
- **Risk:** medium — mechanical, but touches every file. Mitigated by the
  existing 549 tests plus the generated-output diff gate.
- **Effort:** 2–3 days.

### W2 · One state store, one scan — **landed in part (see the status note)**

> **What shipped:** `organizekit/core/state.py` (stdlib `sqlite3`, WAL,
> `BEGIN IMMEDIATE` writes), write-through from `library_auditor.py`,
> `bitdepth.py` and `sync_subtitles.py`, and the `organize status` command the
> store exists to make possible. `--no-state` / `ORGANIZE_NO_STATE` / a
> corrupt database all downgrade to a null store with the same API, so a cache
> problem cannot fail a run.
>
> **One deliberate deviation from the schema below: verdicts are one row per
> `(movie, kind)`, not columns on a wide `movie` row.** Each verdict carries
> its own `(size, mtime_ns)`. A movie's bit depth can be current while its
> sync verdict is stale — a single wide row with one stamp would have to call
> the stale one fresh, which is the precise failure mode "derived cache, never
> authority" exists to prevent.
>
> **Still open from this section:** the two JSON probe caches are not yet
> folded into the DB; the fetcher still rebuilds its quota ledger by re-parsing
> its own log (`reserve_quota` is written and tested, ready for it, and is what
> W4b needs); `core/scan.py` was **rejected** rather than deferred — once
> `organize status` delegates to `library_auditor.audit_library`, the one
> parallel sweep already exists on top of `core/parallel.py`, and a second scan
> module would be exactly the duplication this repo's tests forbid. One scan
> shared across all five steps therefore stays a `pipeline.py` question, not a
> new module. `mkv_track_cleaner.py` does not publish verdicts yet, so
> `organize status` prints `Remux  not recorded yet` and leaves that step out
> of the "nothing to do" tally instead of quietly counting it.

**Problem.** Five independent library walks per pass; two JSON probe caches with
different schemas; `sync_state.json`; a ledger reconstructed by re-parsing an
append-only log; no way to ask "what's left?" without touching the disk.

**Change.** One SQLite file (`state.db`, WAL mode — **stdlib**), treated as a
*derived cache and never as authority*:

```sql
CREATE TABLE movie (
  path_key      TEXT PRIMARY KEY,   -- path_norm(), same identity the locks use
  size          INTEGER, mtime_ns INTEGER, nlink INTEGER, inode INTEGER,
  probe_json    TEXT,               -- ffprobe / mkvmerge -J payload, keyed by (size, mtime_ns)
  moviehash     TEXT,
  sub_status    TEXT, sub_source TEXT, sub_sha256 TEXT,
  remux_status  TEXT, bitdepth_verdict TEXT, sync_status TEXT,
  first_seen    TEXT, last_seen TEXT, last_error TEXT
);
CREATE TABLE quota (provider TEXT, utc_day TEXT, used INTEGER, PRIMARY KEY (provider, utc_day));
CREATE TABLE event (ts TEXT, tool TEXT, path_key TEXT, kind TEXT, detail TEXT);
```

Rules that keep the current safety properties intact:

- The DB is **rebuildable from the filesystem at any time**; deleting it costs
  one slow pass, never correctness. Every tool still re-derives its verdict from
  live filesystem state (`nlink`, sidecar presence) exactly as today.
- Cache entries are keyed by `(size, mtime_ns)` — the rule `MediaProbeCache`
  already uses correctly.
- One writer at a time via the existing advisory lock; readers use WAL.
- Quota rows replace log re-parsing, which is both fragile and O(log size).

Then add `core/scan.py`: **one** parallel `os.scandir` sweep per pass that
populates the DB, and which all five steps consume. Probes run once per changed
file, not once per tool.

New capability that falls out for free:

```console
$ organize status
Library   /srv/media/Movies          412 movies    3.1 TiB
Subtitles 403 ✔   6 pending   3 manual-review
Remux     410 ✔   2 deferred (seeding)
Bit depth 388 keep   21 queued for HandBrake   3 review
Sync      401 ✔   1 held for review
Nothing to do for 388 movies — next pass will touch 24.
```

That query runs in milliseconds and touches no media. It also makes
`jellyfin_one_shot`'s convergence loop cheap: pass N+1 only visits rows that
are pending or whose `(size, mtime_ns)` changed, instead of re-walking
everything five times.

- **Payoff:** unifies 5 ad-hoc state files; converts repeat passes from O(library)
  to O(work remaining); enables `status`, resumability, and honest progress bars.
- **Risk:** medium-high — new subsystem. Mitigated by "derived cache, never
  authority" and a `--no-state` flag that bypasses it entirely.
- **Effort:** 3–4 days including tests.

### W3 · One orchestration model — **landed (partly; see the status note)**

**Problem.** Four places know the step order and how to build argv:
`pipeline.py`, `jellyfin_one_shot.py`, `organize.py`, and `jellyfin_completer.sh`.
`jellyfin_one_shot.py` is a 2,172-line reimplementation of "run the pipeline in
a loop", including its own `check_prerequisites()`.

**Change.**

```python
# organize/pipeline.py — the only step table in the repo
@dataclass(frozen=True)
class Step:
    key: str; run: Callable[[Context], StepResult]; requires: tuple[str, ...]

STEP_ORDER = ("fetcher", "cleaner", "bitdepth", "sync", "auditor")  # order is load-bearing

def run_pass(ctx: Context, steps=STEP_ORDER) -> PassResult: ...

# organize/oneshot.py — convergence policy only, ~400 lines
def converge(ctx, policy) -> int:
    while not (result := run_pass(ctx)).audit_clean:
        if policy.exhausted(result): return policy.exit_code(result)
        policy.wait(result)          # UTC rollover on quota, backoff on no progress
```

Two further wins in the same move:

- **In-process step execution.** Steps become importable callables; the default
  becomes one process for a whole pass instead of five subprocess spawns per
  pass. Steps then share the scan, the probe cache, and the DB connection.
  Keep `--isolate` to run steps as subprocesses when you want crash isolation.
- `jellyfin_completer.sh` collapses to `exec python3 -m organize one-shot "$@"`
  (31 lines → 3), removing a fifth argument-parsing surface.

- **Payoff:** −~1,400 lines; the prerequisite-divergence bug class becomes
  unrepresentable; convergence policy becomes unit-testable in isolation.
- **Risk:** medium. The convergence logic is good and must be preserved verbatim
  in behaviour — port it with its tests first, then delete the old file.
- **Effort:** 2–3 days.

**What actually shipped.** The step table, the binary probes, the skip reasons
and the argv builder are one module (`organizekit/core/toolchain.py`); both
orchestrators bind the same object and `tests/test_shared_core.py` asserts
identity, so a second table cannot be written. `run_one_shot` is 542 lines
instead of 661 (the three "clean / inspect / sync" blocks are one loop),
`pipeline.py` is 349 instead of 448, and `jellyfin_completer.sh` is a single
`exec`. Behaviour was verified by replaying a full completer run before and
after and diffing every subprocess argv, the run log and the rendered report.

**Deliberately not done, and why.** In-process step execution was dropped from
this phase: each tool currently owns its own run lock, log file, report and
exit code, and a shared process would have to reproduce all four *and* give up
the crash isolation that makes a multi-day unattended run safe. It belongs with
W2's single scan and shared probe cache, where it pays for itself; on its own it
is risk without reward. The prerequisite-divergence bug — the thing W3 was
really for — is fixed either way.

### W4 · Concurrency and connection reuse — **landed for sync, audit and rate limiting**

> **W4b update — the pacing half shipped, the threading half did not, and the
> measurement says that was the right order.** The fetcher's scraping tier put
> seven different sites behind *one* "last request" timestamp, so a request to
> Subf2m waited a second because the previous one went to Podnapisi. Per-host
> token buckets (`organizekit/core/ratelimit.py`) remove exactly that wait and
> nothing else: measured over a 200-movie pass (1,800 requests) the throttling
> drops from 1,800 s to 571 s — **3.2×** — while the busiest single host is
> still paced to its full 257 s of gaps, which `benchmarks/bench_scrape_gaps.py`
> asserts before it prints. A `Retry-After` now penalises the host's bucket
> rather than one request.
>
> **Still open:** running the providers *concurrently*. The bucket is the piece
> that makes it safe (taking a token is atomic; waits are reserved, so N
> workers cannot overspend a rate the way N readers of a timestamp can), but
> the fetcher's per-movie loop persists a durable ledger after every step, and
> that ledger — not the pacing — is what has to move into the state store
> first. HTTP keep-alive (a pooled `http.client.HTTPSConnection` per host,
> worth 100–300 ms per request) is also still open and is now the cheapest
> remaining item in this section.

**Problem.** Only `bitdepth.py` has `--workers`. The two slowest steps are serial.

| Step | Bound by | Today | Proposed default | Expected wall-clock |
| :--- | :--- | :--- | :--- | :--- |
| `subtitle_fetcher` | ~~network RTT~~ **provider rate limit** | ✅ per-host token buckets; still serial | + concurrency once the quota ledger moves to the DB | ~~5–8×~~ **3.2× measured on the scraping tier** |
| `sync_subtitles` | CPU (`ffsubsync`) | ✅ `cpu_count//2`, cap 4 | done | **3.6× measured** |
| `mkv_track_cleaner` | disk throughput | serial | 2 (opt-in higher) | 1.3–2× (not done: it rewrites movies) |
| `library_auditor` | `stat()` | ✅ threaded, cap 8 | done | **7.7× measured** at 5 ms/folder; ~1× locally |
| `bitdepth` | `ffprobe` | ✅ threaded | now uses the shared pool | — |

Two details that matter:

- **Rate limits become a shared token bucket, not serialism.** The fetcher's
  daily-quota accounting is the reason it is serial today. Move quota to the DB
  (W2) and gate requests through one `TokenBucket` per provider; parallelism
  then cannot overspend a quota, because reservation happens before dispatch.
  *Measured correction:* the gain from threading the fetcher alone is ~1.2×,
  not 5–8×, because `REQUEST_GAP_SEC = 1.1` already dominates the round trip.
  The multiple comes from running the *sources* concurrently at their own
  permitted rates, which is the token-bucket half of this item, not the thread
  half - so this work is now sequenced after W2 rather than before it.
- **HTTP keep-alive.** Every provider call is a bare `urllib.request.urlopen`,
  i.e. a fresh TCP + TLS handshake. A pooled `http.client.HTTPSConnection` per
  host (stdlib) removes 100–300 ms per request. On 400 movies × ~3 requests
  that alone is minutes.

Ordering safety is unaffected: parallelism is **within** a step, never across
steps. The fetch-before-remux invariant lives at the step boundary.

- **Payoff:** the single biggest user-visible improvement in the plan.
- **Risk:** medium — concurrency plus quotas. Mitigate with `--workers 1` as an
  escape hatch and deterministic tests using a fake clock and fake transport.
- **Effort:** 3–4 days.

### W5 · Decompose the dangerous code, then prove it

**Problem.** `mkv_track_cleaner.py` at 41% coverage is the tool that rewrites
and deletes movie files. `queue_run` is 768 lines.

**Change.** Apply the `plan/execute` split that already worked for
`process_mkv`, everywhere it is missing:

- `queue_run` → `plan_fetch(movie, state) -> FetchPlan` (pure) + `execute_fetch(plan)`.
- `run_one_shot` → dissolved by W3.
- `run_doctor` → a table of `Check` objects; the renderer is generic.

Then raise the floor with tests that match the risk:

- **Table-driven decision tests** on the pure planners — cheap, exhaustive.
- **Property-based tests** (`hypothesis`, dev-only, runtime stays zero-dep) on
  scene-name parsing, SRT timestamp arithmetic, and `wrap_path_text`.
- **Fault-injection suite** — the one this repo's entire thesis deserves.
  Simulate a crash at each atomic-write point (between staging and `os.replace`,
  mid-`fsync`, after journal write) and assert the library invariants hold: no
  half-written MKV, no lost sidecar, no orphaned staging file that a re-run
  won't clean. Today those guarantees are argued in prose; make them executed.
- Ratchet the coverage gate 55 → 65 → 75 as each lands.

- **Payoff:** the destructive tool becomes the best-tested one, which is the
  correct inversion of today's state.
- **Risk:** low. Pure refactor plus new tests.
- **Effort:** 3–5 days.

#### W5 update — the tests landed; the planner split did not (yet)

Shipped: the fault-injection suite (`tests/test_crash_safety.py`), an
end-to-end remux suite driven by a real fake mkvmerge binary
(`tests/fake_mkvmerge.py` + `tests/test_track_cleaner_e2e.py`), and a
destructive-path suite for the only tool that deletes folders
(`tests/test_standardizer_destructive.py`). 697 → 779 tests, coverage 69% →
**75%**, `mkv_track_cleaner.py` 47% → **75%**, `movie_standardizer.py` 56% →
67%. The CI floor moves 65 → 72 (a few points of slack because the end-to-end
remux tests are skipped off POSIX, where the fake binary's shebang does not
work).

Two decisions worth recording:

- **The crash is a `BaseException`.** Every tool wraps per-movie work in
  `except Exception` so one bad file cannot kill a run — which means an
  ordinary exception exercises the tidy-up path and never the state a power
  cut actually leaves on disk. The suite raises something `except Exception`
  cannot catch, and keeps separate tests for the two handled cases (`Ctrl-C`,
  which does get to clean up, and an in-process error, which is reported).
- **Staging files are aged by moving the clock, not the file.** Orphan
  recovery fingerprints the temp by mtime, so back-dating it with `utime`
  would fail the tamper check for the wrong reason and hide what the recovery
  logic would really have done.

**Then the planner split, in the form that was actually worth doing.** A full
`plan_fetch`/`execute_fetch` decomposition of `queue_run` would move ~600
lines of provider-tier orchestration that is inseparable from its I/O — high
risk, on the tool that spends money. The two decisions that are genuinely pure
came out instead, and they are the ones that matter: `plan_from_history`
(what a movie's durable record says before any provider is asked) and
`plan_sources` (which tiers may be offered it, given the day's reservations).
Together they are the fetcher's spending policy, they were untestable except
by running the whole fetcher, and they are now a 37-case table. A further 29
tests cover the OpenSubtitles transport and download safety rules against a
fake `urlopen`. 779 → 845 tests, coverage 75% → **76%**, `subtitle_fetcher.py`
74% → 77%.

Still open from this slice: the remaining tier orchestration inside
`queue_run` (712 lines), the `run_doctor` check table, and the property-based
tests — `hypothesis` cannot be assumed present in this offline environment, so
those would need stdlib `random` with fixed seeds.

> **Update — phase 6c (code health): one run log, and a lint ratchet.**
>
> The last verbatim duplicate in the toolkit was the logger. Four tools had
> copied the same twenty lines — stamp, print, append, swallow the `OSError` —
> and, as copies do, had drifted: three took a print lock and one did not; three
> wrote the file with `errors="replace"` and the fetcher would have raised
> `UnicodeEncodeError` on a lone surrogate in a filename; the orchestrator used
> a bare `print`, so an unencodable line could have ended a five-hour run inside
> the logging call. `organizekit/core/runlog.py` now defines it once
> (`RunLog`), the tools hold an instance, and the differences that were real —
> the orchestrator's bracketed transcript form, its three-argument signature,
> the fetcher's deliberate lack of a default log file — are parameters or thin
> wrappers rather than copies. −62 lines net.
>
> Two things got *better*, not just shorter. The lock is now public, and
> `jellyfin_one_shot.py` echoes its child tools' output under the same one, so
> a status line can no longer split a tool's line in half. And the guarantee
> that used to be assumed is now tested: `tests/test_runlog.py` drives six
> threads through a printer that deliberately emits each line in two pieces
> with a yield between them, and asserts every line — console and file — comes
> out whole. Without the lock that test fails, which is the only reason to
> trust it (all six mutations of the module are caught).
>
> `mkv_track_cleaner.py` keeps its own logger and that is the right answer: it
> holds the log file open for the length of a remux queue, routes lines through
> the live console when one is attached, and suppresses `PROGRESS` from the
> file. Those are three real behaviours, not drift.
>
> The `except Exception` audit started at the other end. `core/toolchain.py`
> no longer has a file-wide `BLE001` exemption; its five broad catches carry
> the justification on the line itself, so a *new* blind except in shared code
> is now flagged. The nine tool files still have the blanket ignore — 72 sites,
> most of them the per-item handlers that keep a sweep alive — and narrowing
> them one at a time is the remaining code-health work.

> **Update — phase 6d (code health): no blind excepts left.** All 80 of them
> were read. 27 turned out to be guarding something with bounded failure modes
> and now name it — `socket.gethostname()` raises `OSError`, `shutil.disk_usage`
> raises `OSError`, `subprocess.run` raises `OSError`/`SubprocessError`/
> `ValueError`, `str.encode` on an unknown codec raises `LookupError`, a closed
> stdout raises `OSError`/`ValueError`. The other 53 stay broad and say why on
> the line, and they fall into four honest kinds:
>
> | Kind | Rule | Example |
> | :--- | :--- | :--- |
> | per-item | one bad movie is an error row, never the end of a sweep | `process_mkv`, the ffprobe wrapper |
> | fail-closed | anything unexpected means *do not proceed* | `verify_remux_output`, the journal write, `acquire_lock` |
> | last resort | `main()` leaves through one exit code, not a traceback | every tool |
> | foreign | ctypes, a sibling tool's import, a scraped page, a display callback | the Windows probes, `doctor` |
>
> No file has a blanket exemption any more, in the tools or in the core, so a
> *new* blind except anywhere in the toolkit fails the lint job.
>
> A narrowed `except` is only an improvement if it still catches what actually
> happens, so `tests/test_error_boundaries.py` (27 tests) injects the real
> failure at each narrowed boundary and asserts the documented degradation: a
> hostname that will not resolve, a volume that will not report free space, a
> `Path.unlink` that keeps raising, a log file on a read-only share, a version
> probe that hangs past its timeout, a console closed under a run in progress.
> Six mutations that make a clause *too* narrow are each caught by a named
> test. `mkv_track_cleaner.py` 75% → 77%.

### W6 · Close the product gaps

**Out of scope by decision.** The owner's instruction for this stretch of work
was code health only — no new product features. The gaps below are recorded
because they are real, not because they are queued; behaviour stays exactly as
it is, and the effort goes into size, speed and test rigour instead.

Code quality aside, there are real functional gaps against the README's own
promises:

1. **"100% Direct Play" is never verified.** `library_auditor.py` explicitly
   documents that it checks names and sidecars only —
   *"Container labels are file extensions only; they do not verify codecs or
   Jellyfin client direct-play support."* The headline claim of the project is
   the one thing nothing tests. Add `core/directplay.py`: a codec/container/
   channel-layout matrix (HEVC 10-bit, AV1, H.264 High@L4.1, E-AC-3 5.1, …)
   evaluated against a configurable client profile, reported by the auditor.
   The probe data needed is already in the DB from W2 — this is nearly free
   once W2 lands, and it turns a marketing claim into a checked invariant.
2. **HandBrake queueing stops at a report.** `bitdepth.py` decides *what* to
   re-encode and then hands you a text file. Emit a real HandBrake queue
   (`.json` queue import) and optionally execute it with the same fail-closed
   HDR guard and hardlink deferral the rest of the toolchain uses.
3. **One language, one flavour.** The subtitle contract is hardcoded to
   `.eng.srt` (+ `.eng.sdh.srt`). `--languages en,es,fr` and proper
   `.eng.forced.srt` handling for foreign-dialogue-only tracks is a
   contract-level generalisation, best done while W1 is consolidating the
   subtitle path into one module.
4. **Subtitle *quality*, not just validity.** Today a sidecar passes if it
   parses. Score candidates on characters-per-second, line length, OCR-artifact
   density, and ad/spam-line detection, and prefer the best — a measurable
   viewing-experience improvement for ~150 lines.
5. **Seeding-aware scheduling.** The cleaner defers `nlink > 1` files forever
   until the torrent is removed. With W2 the tool can *predict* and report
   "17 movies unblock when seeding completes" instead of silently deferring
   every pass.

> **Update — phase 8a (W7): the single file, built and tested.**
>
> The distribution question from §6 is settled the way the owner chose: a
> normal package *plus* a stdlib-only `organize.pyz`, and no standalone-file
> generator. `scripts/build_pyz.py` reads the module list out of
> `pyproject.toml` — the same list the wheel ships — stages it and writes a
> ~270 KiB archive with sorted entries and pinned timestamps, so two builds of
> one source are the same bytes.
>
> The interesting part was not the packaging; it was that **a zipapp has no
> script files in it**, and this toolkit runs its five steps as child
> processes on purpose (own locks, own log, own report, own exit code — one
> tool's crash cannot take the run with it). Making the archive run them
> in-process would have been simpler and would have quietly given up that
> property. Instead the launch rule moved into `core/toolchain.py` —
> `tool_command()`, `tool_is_available()`, `tools_home()`, `child_cwd()` — and
> the archive re-enters itself: `python organize.pyz run-tool bitdepth.py …`.
> Four hand-rolled `[sys.executable, script_path]` sites collapsed into it.
>
> Three paths were resolving *inside* the file rather than beside it (the
> completer's log directory, the fetcher's extraction ledger, the "is ffprobe
> next to the script?" probe). Same behaviour in a checkout; the difference
> between working and `NotADirectoryError` in the archive.
>
> `tests/test_zipapp.py` builds it and uses it: nine field smoke tests run out
> of the archive, `pipeline.py` launches a real auditor pass from inside it,
> and an AST walk asserts that every import in every shipped file resolves to
> the standard library or to another member — the zero-dependency claim,
> checked. 894 → 909 tests. The cross-platform half (build and run the
> archive on Windows too, and publish it as an artifact) is in
> `docs/ci-workflow.patch` along with the coverage floor moving 55 → 74:
> the bot that pushes this branch may not edit workflow files.

### W7 · Ops, UX, distribution

- **`organize status`** (W2) — the missing verb.
- **Machine-readable output** — one JSONL event stream per run plus
  `run_summary.json`, and `--json` on every command. Cron/Healthchecks/Grafana
  integration becomes trivial; the human reports stay exactly as they are.
- **One shared `LiveConsole`** (currently only in the cleaner) so every step has
  the same progress UI, and it degrades to plain lines when not a TTY.
- **Distribution:** publish to PyPI (`pipx install organize` / `uvx organize`),
  attach `organize.pyz` and the generated standalone scripts to each GitHub
  release, and ship systemd-timer and Task-Scheduler templates in `docs/`.
- **Docs:** the 585-line README becomes a ~120-line front door plus
  `docs/{install,tools,pipeline,troubleshooting,design}.md`. The *why* prose is
  the repo's best asset — it deserves to be findable, not scrolled past.
- **CI:** add CodeQL, Dependabot for actions, a `--fail-under` ratchet, the
  standalone-build diff gate, and one job that runs the suite against the
  *generated* single-file tools. Drop `unittest discover` in favour of the
  pytest config the repo already declares, so CI and `pyproject.toml` agree.

---

## 3. Sequenced roadmap

Each phase is independently shippable and leaves the repo green.

| Phase | Work | Lines | Risk | Days |
| :--- | :--- | ---: | :--- | ---: |
| ~~**1**~~ | ~~W1 core extraction + CI gate~~ **done** | −4,663 | Med | ✅ |
| ~~**2**~~ | ~~self-tests → `tests/`, thin smoke checks remain~~ **done** | −1,842 | Low | ✅ |
| ~~**3**~~ | ~~W3 one Step registry; one argv builder; `.sh` → one `exec`~~ **done** | +58 (see note) | Med | ✅ |
| ~~**4a**~~ | ~~W4 shared worker pool; `sync_subtitles` + `library_auditor` parallel~~ **done** | +330 | Med | ✅ |
| ~~**4b**~~ | ~~W4 per-source token buckets for the fetcher~~ **done** (concurrent fetching and HTTP keep-alive still open) | +230 | Med | ✅ |
| ~~**5**~~ | ~~W2 SQLite state cache + write-through + `organize status`~~ **done** (probe caches and the fetcher's quota ledger not yet moved in; `core/scan.py` rejected — see the W2 note) | +841 | Med-High | ✅ |
| ~~**6a**~~ | ~~W5 fault-injection, end-to-end and destructive-path suites, coverage 69% → 75%, gate → 72~~ **done** | +1,240 | Low | ✅ |
| ~~**6b**~~ | ~~W5 the fetcher's spending planners extracted from `queue_run` and tabled~~ **done** (the remaining tier orchestration and the property tests are still open — see the W5 update) | +460 | Low | ✅ |
| ~~**6c**~~ | ~~one shared `RunLog`, the last duplicated implementation; `core/toolchain.py` off the blanket `BLE001` ignore~~ **done** (the nine tool files' 84 broad catches are still blanket-ignored) | −62, +200 tests | Low | ✅ |
| ~~**6d**~~ | ~~every `except Exception` in the toolkit narrowed or justified in place; no file-wide `BLE001` exemption left~~ **done** | 27 narrowed, +290 tests | Low | ✅ |
| **7** | ~~W6 direct-play verification, HandBrake queue, multi-language~~ **out of scope** — code health only, by decision | +1,500 | Med | — |
| ~~**8a**~~ | ~~W7 `organize.pyz` single-file build; one launch rule for both deployments~~ **done** | +330, +15 tests | Low | ✅ |
| **8b** | W7 docs split, JSON/JSONL output, PyPI release | +300 | Low | 2 |

**Net: ~26,500 → ~23,000 production lines** (phases 1–3 measured: 26,458 →
20,011) that do substantially more, run
several times faster, and are provably rather than rhetorically safe.

Phases 1–3 are pure deletion and are worth doing even if nothing else is. Phase
4 is the one users will *feel*. Phase 5 is the one that unlocks phases 6–7.

---

## 4. Invariants the overhaul must not break

Non-negotiable, and each should gain an explicit test if it lacks one:

1. Subtitles are fetched **before** the remux (moviehash ordering).
2. Ingest is `os.link()` only — never copy, move, or symlink.
3. `nlink > 1` ⇒ deferred, unconditionally, with no override flag.
4. HDR is fail-closed: uncertain metadata is never queued for re-encode.
5. Every publish is staged + `os.replace` + `fsync`.
6. Unique data is never deleted; destructive modes stay opt-in.
7. A bad subtitle sync is worse than none.
8. Stateless, idempotent, safe to Ctrl-C — **the SQLite store must never
   compromise this.** It is a cache; the filesystem remains the truth.
9. Zero runtime third-party dependencies.

Rule of thumb for the whole overhaul: *if a change makes a guarantee harder to
state in one sentence, it is the wrong change.*

---

## 5. Success metrics

| | Today | Now | Target |
| :--- | ---: | ---: | ---: |
| Production lines | 26,458 | 21,516 (20,011 after phase 3; W2/W4b added back) | ~23,000 |
| Duplicated lines | 4,325 | ~0 | **0** (generated) |
| Coverage | 58% | **76%** (cleaner 75%) | ≥75%, cleaner ≥80% |
| Test runtime | 6.7 s | 14.6 s (909 tests, incl. building and running the zipapp) | ≤15 s (with property + fault-injection tests) |
| 500-movie cold pass | hours | not re-measured | **≤ 1/4 of today** |
| 500-movie no-op pass | full 5-tool sweep | `organize status`, one audit | **< 5 s** (DB query) |
| Sources of truth for step order | 4 | 1 (`core/toolchain.py`) | 1 |
| Direct-play claim | documented | documented | **verified per file** |

---

## 6. The fork in the road

One decision gates Phase 1 and therefore everything else:

**Is "copy a single `.py` file anywhere and run it" a hard product requirement,
or a nice-to-have?**

- **Hard requirement** → W1 as written: shared `core/`, plus a generator that
  emits the standalone files, plus a CI gate. Slightly more machinery, promise
  fully preserved.
- **Nice-to-have** → skip the generator; ship a package plus `organize.pyz`
  (one file, zero install, whole toolchain). Simpler, and arguably a better
  answer to the same user story.

Everything downstream — W2 through W7 — is identical either way.
