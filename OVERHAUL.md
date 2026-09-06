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
> **Still open from this section:** ~~the two JSON probe caches are not yet
> folded into the DB~~ (**done in phase 5c, below**); the fetcher still rebuilds its quota ledger by re-parsing
> its own log (`reserve_quota` is written and tested, ready for it, and is what
> W4b needs); `core/scan.py` was **rejected** rather than deferred — once
> `organize status` delegates to `library_auditor.audit_library`, the one
> parallel sweep already exists on top of `core/parallel.py`, and a second scan
> module would be exactly the duplication this repo's tests forbid. One scan
> shared across all five steps therefore stays a `pipeline.py` question, not a
> new module. ~~`mkv_track_cleaner.py` does not publish verdicts yet, so
> `organize status` prints `Remux  not recorded yet` and leaves that step out
> of the "nothing to do" tally instead of quietly counting it.~~ **done in
> phase 5b, below.**

> **Update — phase 5b (W2): the remux step reports what it did.**
>
> The last of the five steps to publish. Until now a library could be
> completely remuxed and `organize status` would still print `Remux  not
> recorded yet` and leave the step out of the settled tally, because the one
> tool that knew never wrote it down.
>
> Two things make the cleaner different from the other publishers, and both
> shaped the design. It *rewrites* movies and takes hours doing it, so a
> publish-at-the-end pass would throw away every verdict of an interrupted run:
> a verdict is therefore written **per movie**, the moment that movie is
> finished, and one small SQLite write next to a remux measured in minutes is
> free. And its per-movie outcome is not a return value - `process_mkv` is a
> 400-line procedure that reports by appending to one of six buckets - so
> rather than thread a store through all of it, `remux_verdict()` reads the
> outcome back out of those buckets by comparing them before and after. That is
> a pure function of two dicts, and therefore testable without a movie, an
> mkvmerge or a filesystem.
>
> **The vocabulary is the tool's, not the summary's.** `cleaned`,
> `already-clean` and `skipped` mean there is nothing left to do; `deferred`
> (still seeding), `skipped-layout` (waiting for the standardizer) and `failed`
> are pending work and are counted as such. `organize status` imports
> `SETTLED_REMUX` from the cleaner instead of keeping its own list - the entry
> it replaced treated *any* recorded verdict as settled, which was harmless
> only while nothing recorded one.
>
> **The stamp is taken after the swap**, so a cleaned movie's verdict describes
> the remuxed bytes and the next `status` reports it as current rather than
> stale. A dry run publishes nothing, `--no-state` publishes nothing, and a
> cache write that fails is a warning: the remux already happened and a cache
> is not allowed to undo it.
>
> Found on the way past: `tests/test_zipapp.py` ran real tools as child
> processes without `--state-db`, so the offline suite had been quietly writing
> to the developer's own `~/.local/state/organize/state.db`. It now sets
> `ORGANIZE_NO_STATE` for those children. 1,152 -> 1,178 tests, eight mutations
> checked.

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

### W4 · Concurrency and connection reuse — **landed for sync, audit, rate limiting and the fetcher's local triage**

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

> **Update — phase 4c: the fetcher's parallel half, and where it stops.**
> Reading the code to move the quota ledger into the state store showed the
> plan below was wrong on one point, and it is worth writing down. The
> fetcher's durable quota ledger **is the append-only log**: state is rebuilt
> by replaying ledger events (`recover_state_from_log`) and checkpointed with
> `fsync` before each spend. `core/state.py` is the opposite kind of thing — a
> derived cache that fails *open* (`--no-state`, `ORGANIZE_NO_STATE=1`, an
> unwritable directory all give you a null store). A spend guard must fail
> *closed*, so moving quota into SQLite would trade a correct guarantee for a
> convenient one. Cross-process exclusion is already the `CoordinationLock`;
> what threading the loop actually needs is an in-process lock, not a table.
>
> So the fetcher was parallelised where there is nothing to overspend. Every
> movie starts with three local questions — canonical folder? usable sidecar
> already? what is its identity? — and on a mostly-covered library that
> pre-flight *is* the run: a directory listing and a couple of small reads per
> movie, thousands of round trips before a single request is spent.
> `triage_movie()` answers all three for one movie and `TriageQueue` runs them
> in the shared pool (`--workers`, default half the CPUs capped at 8), handing
> verdicts back in input order. Measured on 600 movies
> (`benchmarks/bench_triage_workers.py`): 3.16 s → 0.50 s at 8 workers
> (**6.3×**) with a 5 ms-per-folder round trip; on tmpfs the threads cost
> 0.17 s and the README says so.
>
> The line is drawn at the money: the quota ledger, the provider tiers, every
> download and every checkpoint stay on the single main thread, and a test
> asserts no transport call is ever made from a worker. The pool works at most
> `TRIAGE_LOOKAHEAD = 32` movies ahead, so a run that stops on an exhausted
> quota has not read the library it never got to. Equivalence is tested by
> running the same library at 1 and 8 workers and diffing the results *and*
> the log lines. Running the *providers* concurrently remains open, and now
> reads as a smaller prize than it did: the per-host token buckets already took
> the 3.2×, and this took the local sweep.

**Problem.** Only `bitdepth.py` has `--workers`. The two slowest steps are serial.

| Step | Bound by | Today | Proposed default | Expected wall-clock |
| :--- | :--- | :--- | :--- | :--- |
| `subtitle_fetcher` | ~~network RTT~~ **provider rate limit** | ✅ per-host token buckets; ✅ parallel local triage; provider tiers serial | keep spending serial; the ledger stays in the log, not the DB | **3.2×** on the scraping tier, **6.3×** on triage (5 ms/folder) |
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

Still open from this slice: ~~the remaining tier orchestration inside
`queue_run` (712 lines)~~ **narrowed in phase 6j** — the loop is 653 lines and
everything in it now either performs I/O or decides what to do with its
result; see the note below. ~~The `run_doctor` check table.~~ **done in phase 6f.**
~~The property-based tests — `hypothesis` cannot be assumed present in this
offline environment, so those would need stdlib `random` with fixed seeds.~~
**done in phase 6e.** Both notes follow.

> **Update — phase 6w (W5): the edges, and the end of the coverage campaign.**
>
> What was left in the largest file was not provider work. It was the plumbing
> either side of it: the configuration validator, the library walk, the
> one-movie-per-folder contract, the ledger read back out of the log, the
> inspection of what is already sitting next to a movie, and the two toolchain
> helpers whose entire job is to fail politely.
>
> Sixty-six tests. Every refusal in `validate_compact_config` — and the fact
> that they are all reported at once, because an operator should not fix a
> configuration one attempt at a time. The walk skipping samples, non-MKVs,
> undersized files, symlinked files, symlinked folders, extras and disc trees,
> and a file that vanishes between the scan and the size check. The ledger read
> back with a half-written event in the middle of it (the shape an interrupted
> run leaves), a checkpoint belonging to another library, a payload of the
> wrong shape — and a log that cannot be read at all, which stops the run,
> because guessing an empty ledger silently re-spends yesterday's allowance.
> Sidecar inspection when the folder will not list, the file will not read, the
> file is empty, over the limit, a symlink, or simply named something else.
>
> One helper was not keeping its promise: `run_external_command` documents that
> it never raises, and an empty command list raised `IndexError` from inside
> `subprocess`. No caller passes one; it answers 127 now.
>
> Thirty-seven mutations, all killed. One equivalent mutant recorded: pruning
> symlinked directories in the walk is redundant while `os.walk` runs with
> `followlinks=False` — defence in depth, kept.
>
> **This closes the coverage work on `subtitle_fetcher.py`**: 78% → **94%**
> across phases 6o–6w, with the remaining 200 uncovered lines scattered across
> forty functions in ones and twos rather than sitting in any block worth a
> phase of its own. 1,734 → 1,800 tests, the repo at 87%.

> **Update — phase 6v (W5): the metered provider, either side of the search.**
>
> SubDL's search parsing and scoring were well covered; the request that
> carries them and the bytes that come back were not. That is the part that
> costs something when it is wrong: the request is authenticated and counted
> against a daily allowance, and its answer becomes a file in the user's
> library.
>
> `tests/test_subdl_client.py` (58 tests) pins all three. **The request**: the
> key rides in an `Authorization` header and never in a URL; a 429 or a 5xx is
> retried, a 401 is not (asking again spends the quota twice); `Retry-After` is
> honoured, capped at 30 seconds, ignored when it is not a number, and applied
> to the *host* so the next movie inherits the pause. **The answer**: a
> declared length over the limit is refused before the read, a body that lies
> about its length is still bounded, and every non-subtitle shape — a list, an
> error document, HTML, a truncated archive, a gzip bomb — is refused by name.
> **The file**: a relative URL is resolved against `dl.subdl.com`, an
> identifier builds the documented v2 endpoint locally, a JSON redirect is
> followed exactly once and never to another host, and nothing is written for a
> movie that changed during the lookup or on top of a sidecar that appeared
> while the download was in flight.
>
> **One more pattern that could not match.** Where an archive holds several
> SRTs the tool prefers the plain one over the SDH/HI copy, but the pattern
> read `hi\\b` — an escaped backslash, not a word boundary — so it could only
> match the literal characters `hi\b`. `SDH` and `hearing` were recognised;
> the very common `...HI.srt` was not, and the HI copy won on filename order.
>
> Thirty mutations, all killed. One is recorded as equivalent: the length check
> after reading a zip member cannot fire, because the member was already
> filtered on its declared size and `zipfile` never returns more than that —
> defence in depth, kept and now documented. 1,676 → 1,734 tests;
> `subtitle_fetcher.py` 91% → 92%.

> **Update — phase 6u (W5): seven sites that change without telling anybody.**
>
> The scraped tier is seven HTML pages written for humans. Nothing about them
> is a contract: a table gains a column, a status moves into its own cell, a
> search page starts listing sidebar links, and a parser written against last
> year's markup quietly returns nothing — or worse, the wrong row. The chain
> around those parsers was tested end to end; the parsers themselves had only
> ever seen one good page per site.
>
> `tests/test_scrape_sources.py` (66 tests) hands them the pages a site serves
> on a bad day. The transport turns every network outcome into one printable
> sentence — a non-2xx answer that does not raise, an `HTTPError`, a DNS
> failure named by host and *not* by query, a timeout, a body over the size
> limit. Podnapisi gets a payload that is not a list, entries that are not
> objects, a duplicate id, a year that is not a number, a second page, and
> pagination fields that are nonsense. Addic7ed gets an empty result page that
> still carries sidebar movie links, a draft at 80%, a row with no download
> link, a hearing-impaired row and both cell layouts. SubSource gets a guessed
> URL that misses, a search page listing a different film, and a slug page that
> 500s. YIFY gets a downvoted row, a row with no link, and rows out of order.
> Then the chain: what counts as a hard failure, what counts as a parse
> failure, what a refused candidate must *not* cost, and every reason string
> `run_scrape_chain` reports.
>
> **The Addic7ed row parser had a real fragility.** It read the language name
> as "everything in the row before the word `Completed`", so a layout that put
> the download count before the status produced the language `English 1200
> Downloads` and the row was dropped as non-English — silently, and for the
> wrong reason, which for a scraped source looks exactly like "the site has
> nothing". The name now comes from the language cell itself, which also makes
> the two completion checks below it reachable: they, not an accident of string
> splitting, are what refuses a draft.
>
> Thirty-one mutations, all killed. 1,610 → 1,676 tests; `subtitle_fetcher.py`
> 89% → 91%, the repo 87%.

> **Update — phase 6t (W5): the movie's own subtitle track, and two dead
> branches.**
>
> Extraction is the cheapest way to cover a movie — no provider, no quota, no
> network — and after the queue work it was what was left uncovered: the USF
> converter, the OCR backend lookup, `run_ocr`, and the inside of
> `_extract_one_track`, where a container's bytes become a sidecar.
>
> Thirty-nine tests: USF (the rare XML subtitle format) converting, dropping
> empty cues and refusing unreadable timings; the four OCR backends resolving
> from a path handed in, a name on PATH, a known install location or a `.exe`
> under mono; a custom OCR command validated before it is trusted; `run_ocr`
> collecting output written where it was asked, or beside the input, or
> nowhere; and the eight ways one track fails — mkvextract exiting non-zero,
> mkvextract exiting *zero* and writing nothing, a track that converts to no
> cues, a sidecar that appears mid-extraction, a read-only folder, a backend
> that cannot read the codec, an OCR failure, and an OCR result that cannot be
> read back.
>
> **Two branches turned out to be unreachable, and both were bugs.** Subtitle
> Edit's `mono` wrapper sat *below* the program lookup — but the lookup already
> knows Subtitle Edit's Linux install locations, so it always resolved the
> `.exe` first and the wrapper never ran: a Linux install was exec'd as a bare
> .NET binary. The wrapper is now applied after the lookup, and no `mono` means
> the backend counts as not installed rather than failing minutes into a movie.
> And `--ocr-args` accepted a template naming only *one* of `{input}` and
> `{output}`, while its own error message says both are required — without
> `{output}` the tool cannot know where the OCR result landed.
>
> Twelve mutations, all killed. Two more mutants turned out to be equivalent
> (a BOM strip after a `utf-8-sig` decode, and the empty-cue guard in the ASS
> converter, which the USF one shares a shape with) — worth knowing, and left
> alone. 1,571 → 1,610 tests; `subtitle_fetcher.py` 86% → 89%.

> **Update — phase 6s (W5): the queue's twenty bad days, and a bug in the ledger.**
>
> `queue_run` is mostly not the happy path. It is the twenty-odd places where
> something goes wrong for *one* movie and the run has to decide what that
> movie's status is, write it down, and go to the next one — the property a
> nightly job over 900 movies lives or dies by. The end-to-end suite covered
> the paths where a provider says no; the paths where something *breaks* were
> the largest uncovered region left in the file.
>
> Eighteen tests now drive them: a movie that cannot be hashed, a movie that
> changes while it is being hashed, a movie that vanishes between the scan and
> the read, a title search that goes down between the two providers (twice —
> once with somewhere else to ask, once without), a SubDL title search that
> fails, a crash inside a scraped adapter, bytes an adapter should have
> rejected, a subtitle that appears from somewhere else mid-download, and the
> two ways the run can be told to be stricter (`--no-identity-fallback`, and a
> filename with no `Title (Year)` in it). Each asks the same two questions:
> what happened to *this* movie, and did the next one still get its subtitle?
>
> **Writing them found a real bug.** The ledger is append-only and each
> checkpoint carries only the records that changed since the last one, which
> is what keeps a long night's log small. A movie that gets as far as a
> download is checkpointed twice — once to reserve the request before it is
> spent, once for the outcome — and the first checkpoint empties the
> changed-record set while recording an outcome does not put the record back
> in. So **every outcome after a reservation was dropped**: the durable ledger
> was left saying the download was still reserved for a movie that had long
> since been downloaded, already covered, or failed. The scraping branch four
> hundred lines above already re-marks its record for exactly this reason,
> with a comment explaining why; the download branch now does the same.
>
> Eleven mutations of the queue's failure handling, all killed. 1,552 → 1,571
> tests; `subtitle_fetcher.py` 84% → 86%.

> **Update — phase 6r (W5): the one function allowed to overwrite a subtitle.**
>
> `refetch_english_srt` is the seam between the two tools: `sync_subtitles.py`
> calls it when ffsubsync cannot trust the sidecar it was handed, and it is the
> only code in this repo permitted to replace a subtitle file a user already
> has. Everywhere else a sidecar is created or left alone. It was also the
> largest completely uncovered block in the largest file — 84 lines, no
> end-to-end test — because reaching it needs a library *and* a provider.
>
> Twenty-seven tests now drive the real function against the fake providers at
> `urlopen`, and every one of them ends by asking what is in the movie folder
> now. The live sidecar survives no key, no candidate, every candidate already
> tried, a search outage, a download outage, an HTML error page served as a
> subtitle, the movie changing mid-fetch, and a failure of the rename itself;
> it is replaced only by bytes that arrived complete and validated, in one
> atomic swap, with nothing left behind either way; a symlink is never
> followed; and the refusals stay refusals.
>
> Two things the tests had to be taught. The provider-namespaced id
> (`subdl:sub123`) is what makes the caller's "I already tried that one" list
> work, so the exclusion tests use the id the caller was actually given. And a
> same-size in-place edit of the movie inside one filesystem timestamp tick is
> not detectable by a (device, inode, size, mtime) snapshot — the first version
> of the concurrent-change test passed alone and failed in the full suite, which
> is the flake that teaches you what the invariant really says.
>
> Ten deliberate mutations of the function, all killed. Two of them needed a
> test that could only be written as fault injection: the staged-bytes
> re-validation and the publish step are unreachable failures from outside, and
> they are the last two things between a provider hiccup and a movie whose
> subtitle is now an error page. 1,525 → 1,552 tests; `subtitle_fetcher.py`
> 82% -> 84%, total 84% -> 85%.

> **Update — phase 6q (W5): the fetcher's planners, against inputs nobody wrote.**
>
> Phase 6e gave the repo a property harness and pointed it at four areas; the
> fetcher was not one of them, even though it is the only tool that acts on
> input from strangers. Its planners are pure functions of what a provider
> said, and its quota arithmetic is a promise to that provider — the two shapes
> a table of examples covers worst.
>
> Twenty properties now hold for every generated answer list: the chosen
> subtitle is always one that was offered; a machine-translated, AI-translated,
> foreign-parts-only or non-English entry is never chosen by *any* of the three
> selection routes; the hash route only ever installs a hash match whose
> release names the movie; the order a provider listed its answers in cannot
> change the pick; adding an entry the policy refuses changes nothing; an
> unbroken tie is a review rather than a guess; a SubDL release match is never
> below the documented 0.80; pooling two providers never invents a third
> answer and never picks the worse-ranked of the two; a scraped shortlist is a
> subset that names the movie and respects its own limit; a download URL is
> `https://dl.subdl.com/subtitle/...` or it is refused; a daily cap is never
> exceeded however many movies ask; the two SubDL allowances are metered apart;
> a reservation is recorded before it can be used; and a movie whose download
> was reserved today is never reserved again today.
>
> **The generator had to be made to produce hard cases, twice.** A first pass
> at random candidates almost never produced an *acceptable* one, so every
> property passed by refusing everything; weighting the strategy towards the
> movie being asked about took the accept rate from 0.1% to 16-43% per route.
> Then a mutation sweep showed two properties still passing with the code
> broken — resolving a tie by guessing, and dropping the sort's tiebreakers —
> because purely random candidates never collide on a rank key. The list
> strategy now sometimes clones a candidate, which is what the real providers
> return anyway, and both mutants die.
>
> Twelve planner mutations, all killed **by the property file alone**. Four are
> kept as `MutationTests`; one of those found the same oracle bug phase 6e hit
> — the URL property was reading the expected host out of the module it was
> judging, so pointing the constant at `evil.example` left it green. It now
> spells the host out. 1,501 → 1,525 tests.

> **Update — phase 5c (W2): the probe payloads move into the same database.**
>
> The store shipped in phase 5 held verdicts, quotas and events, while the
> expensive thing a run actually reuses — the `mkvmerge -J` and `ffprobe`
> output for a file that has not changed — stayed in two ad-hoc JSON files with
> two different layouts, one per tool, loaded and rewritten whole. They are now
> rows in a `probe` table keyed by `(path_key, tool)`, in the one file the
> tools already open.
>
> **The safety rule is unchanged and is what makes this a cache at all**: a
> payload is reused only while the file's size *and* `st_mtime_ns` are both
> unchanged, and only the probe *output* is stored — never a verdict. Every
> tool still re-derives its decision from live filesystem state, so a cached
> entry cannot make a run blind to a sidecar that appeared, a hardlink count
> that dropped, or a remux that landed.
>
> **Nobody loses a warm cache.** `--cache` still takes a path: a `.json` one
> keeps the old per-tool file format, so an existing scheduler line keeps
> working. Without one the payloads follow the run's state cache, and the JSON
> file an earlier version left behind is imported once, the first time the
> database is found empty — the old file is left exactly where it was, because
> deleting somebody's cache is not this code's call.
>
> **One file means one switch.** `--no-state` and `ORGANIZE_NO_STATE` now turn
> the probe cache off with everything else, because it is the same database;
> `--no-cache` remains the way to skip probe reuse without touching the rest.
> Reading never creates the database, so a run that learns nothing leaves the
> disk as it found it, and every storage failure — missing, corrupt, foreign,
> a directory where the file should be, a row whose payload is not JSON — is a
> miss rather than an error, exactly as the JSON version behaved.
>
> Twelve mutations, all killed. 1,463 → 1,501 tests; `probecache.py` **94%**,
> toolkit **84%**.

> **Update — phase 6p (W5): the other two tiers of the fetcher, end to end.**
>
> Phase 6o ran the fetcher against OpenSubtitles. The other two tiers — SubDL,
> and the seven keyless scraped sites behind it — were still only unit tested,
> which meant the *pooling* rules between them had never been executed: which
> source wins, what each one costs, and what happens when one of them is out.
>
> `tests/fake_provider.py` grew a SubDL that answers both v2 routes per movie
> (the release-aware `/files/search` and the weaker title route), a `FakeSites`
> standing in for the scraped tier, and a router that puts all of them behind
> one `urlopen` — which is the only way to test sources that are documented as
> equals. Twenty-five tests pin the result:
>
> * **Equal means equal.** The most-downloaded qualifying release wins whoever
>   has it: a 400-download SubDL match beats a 10-download OpenSubtitles hash
>   match, and a 9,000-download hash match beats the same SubDL entry. One
>   provider's exhausted cap is not the library's problem — the run covers one
>   movie from each.
> * **SubDL's claims are checked.** A `match_score` below 0.80 is not a release
>   match, subtitles filed under a different movie are refused, and a
>   non-English or explicitly non-SRT entry never reaches a download. SubDL
>   meters searches *and* downloads separately, and both ledgers survive into
>   the next run; an exhausted download cap defers the next movie **before** the
>   lookup, because a search it cannot use is waste.
> * **The scraped tier is a chain, not a scattergun.** It stops at the first
>   site that answers (one site contacted, six left alone); when nothing has the
>   movie all seven are asked, once each, and each search is reserved in the
>   durable ledger under its own field. `--skip-source` is honoured, a zero cap
>   turns the tier off, a dry run asks nothing, and a site that fails three
>   times is dropped for the rest of the run.
>
> Twelve mutations, eleven killed on the first pass; the twelfth — deleting
> SubDL's English gate — survived because a second gate downstream also refuses
> it, and removing both together is killed. 1,438 → 1,463 tests;
> `subtitle_fetcher.py` 80% → **82%**, toolkit **84%**.

> **Update — phase 6o (W5): the fetcher's run loop, against a provider.**
>
> `subtitle_fetcher.py` is the largest tool here and the only one that reaches
> the internet. The planners are tested as pure functions and the clients
> against canned payloads; `queue_run` - triage, quotas, provider calls,
> validation, report - was barely covered, because reaching it needs a library
> and a provider.
>
> `tests/fake_provider.py` supplies the provider one layer below the client, at
> `urllib.request.urlopen`, so the real client builds the real request and the
> real run decides what to do with the answer. Thirty-nine tests pin what an
> operator depends on: a hash match is downloaded, validated and written; a
> covered movie costs nothing on the next run; a legacy `.en.srt` is promoted
> rather than bought again. Every refusal is checked through the whole program
> - no Blu-ray keyword, machine-translated, not English, bytes that are not an
> SRT (rejected *after* the download, with the reservation still recorded
> because the provider counted it), a plain-HTTP link, and a movie rewritten
> while its subtitle is in flight.
>
> The daily cap is a promise to the provider and it survives a restart: the
> ledger in the log defers the second movie today and still defers it when the
> run is repeated. `--allow-missing` downgrades an uncovered library to exit 0
> but does not forgive an error.
>
> Eleven mutations, all killed. 1,399 → 1,438 tests; `subtitle_fetcher.py` 78%
> → **80%**, toolkit **84%**. SubDL and the scraping tier are still only unit
> tested; a fake for SubDL's v2 shapes is the next slice of this file.

> **Update — phase 6n (W5): the first step of the pipeline, end to end.**
>
> `movie_standardizer.py` decides what a movie is called and where it lives,
> and every later tool works on the folders it creates. The parsing rules were
> heavily tested and the deleting code had a suite of its own; the *run* had
> never been executed by a test, because it needs a download tree, a library
> tree and a filesystem with hardlinks.
>
> Fifty-three tests now drive `main(argv)` against two real directories and
> then look at what is on disk. The invariant behind most of them is that
> **ingest is additive**: the placed file and the seeding file are proved to be
> one file (`samefile`, `st_nlink == 2`), the download folder is untouched, and
> a second run changes nothing. Everything the tool refuses - TV, non-MKV,
> undersized, multipart, disc trees, symlinks - is declined with its reason in
> the report rather than silently. With no working ffprobe there is no evidence
> of an upgrade, so an occupied destination keeps its inode and an existing
> canonical `.eng.srt` is never overwritten. Configuration that would corrupt
> the library exits 2 before anything is placed.
>
> An end-to-end run also shows what the tool never does: `process_disc_folder`,
> `copy_extras_into`, `copy_artwork_into` and `pair_idx_files` had no callers
> anywhere, and two config flags existed only to feed them. 54 lines deleted,
> no behaviour change - the canonical-library contract has been "one MKV and
> English subtitles" for a long time, and the code now says so too.
>
> Twelve mutations, two of which survived the first pass and were worth the
> trouble: disabling the TV guard passed because a different refusal also says
> "TV", and disabling the junk-file guard passed because the batch scan filters
> `.part` files before that code is reached. 1,346 → 1,399 tests;
> `movie_standardizer.py` 71% → **85%**, toolkit **83%**.

> **Update — phase 6m (W5): the inspector's worklist, end to end.**
>
> `bitdepth.py` never modifies a movie, which made it look like the safe tool
> here. What it produces is a *worklist* - "send these through HandBrake" - and
> a wrong row is an HDR master re-encoded into SDR by hand. The verdicts were
> unit-tested against payload dictionaries; the program around them was not
> tested at all. Launching ffprobe, reading what comes back, containing a probe
> that fails, reusing yesterday's answers, and turning a library of verdicts
> into the exit code a nightly job acts on: none of it had been executed by a
> test, because reaching it needs a library and an installed FFmpeg.
>
> `tests/fake_ffprobe.py` supplies the FFmpeg - a real executable, launched as
> a real subprocess by the unmodified tool, where each "movie" carries its own
> ffprobe answer as a JSON header line. A test writes the technical properties
> it wants; the tool discovers, probes and files them. Both fail-closed rules
> are checked through the whole program rather than at the function: a PQ
> transfer on 8-bit video and a bit depth nothing in the file states are held
> for review, never queued.
>
> The exit codes are the interface to whatever started the run, so they are
> pinned: 3 queued, 4 review, 5 unreadable, each only when the matching
> `--fail-if-*` flag asks, and the most serious finding wins when several
> apply. A missing library, an output path inside the media library, a log and
> a report that are the same file, and an ffprobe that will not run are all
> refused before anything is probed - better no report than a report full of
> ERROR rows. A failed probe is never cached (a test proves the next run
> reaches the real answer), Ctrl-C still publishes what was learned, and a
> report that cannot be written fails the run.
>
> Nine mutations, all killed. 1,306 → 1,346 tests; `bitdepth.py` 67% →
> **93%**, toolkit 80% → **81%**.

> **Update — phase 6l (W5): the library does not hold still for a remux.**
>
> A remux takes minutes, and the window between reading a movie and swapping
> the rebuilt copy over it is exactly when a download client finishes writing
> to that movie, the fetcher replaces the sidecar the plan was built around, or
> the operator presses Ctrl-C. The cleaner has always had a guard for each of
> those - and not one of them had ever been executed by a test, because
> reaching them means changing a file while a remux is in flight. They were the
> last untested branches in the swap sequence, in the tool that rewrites
> movies.
>
> The e2e suite (a real child process, a real journal, a real `os.replace`) now
> arms a callback in one of the two windows - after verification, and in the
> pause immediately before the swap - and changes the library from inside it.
> Ten tests, one rule: **the original is left exactly as it was, the staging
> file and its journal are swept up, and the movie is reported rather than
> silently skipped.** A movie appended to mid-remux keeps the other writer's
> bytes. A sidecar replaced or deleted mid-remux stops the swap, because the
> embedded subtitles were dropped *precisely* on the strength of that file. A
> Ctrl-C in either window discards a verified temp file rather than promoting
> it in a hurry. A full disk is refused before mkvmerge is launched, and a
> journal that cannot be written skips the movie entirely - fail closed means
> the remux is never *started*, not started and abandoned.
>
> Six mutations, two of which survived the first pass and were worth the
> trouble: deleting the first sidecar check passed because the second one
> catches the same file and the assertion matched both messages, and demoting
> the journal failure to a warning passed because the run happened to die
> later for a different reason. The tests now name the message they mean and
> assert that no remux was attempted. 1,296 → 1,306 tests;
> `mkv_track_cleaner.py` 79% → **81%**, toolkit **80%** - the cleaner is over
> the 80% target set at the start of this plan, from 41%.

> **Update — phase 6k (W5): the one decision that lets a movie overwrite a movie.**
>
> Everything else `movie_standardizer.py` does is additive - it hardlinks an
> incoming file into a canonical folder and leaves what it finds alone.
> `should_replace` is where that stops being true, and it was the least-tested
> code in the repo's most destructive tool: 68% file coverage, with the entire
> probe-and-compare path (~150 lines) never executed by a test. Reaching it
> required a library, an installed `ffprobe`, and two real movies with the
> right technical properties.
>
> The guard chain is now `upgrade_verdict(source_info, existing_info)` - a
> function of two probe results, with the probing left in
> `_movie_upgrade_decision` around it. Nothing about the decision changed; the
> refusal strings are the same strings. What changed is that the rules can be
> read in one place and checked in a table:
>
> - **Runtime first**, because a different runtime means a different cut. A
>   theatrical release and an extended edition are two movies, not two copies
>   of one, and no technical superiority makes overwriting one with the other
>   safe. The comparison is symmetric (`abs`), and the tolerance scales, so
>   100 seconds of drift on a four-hour epic is still the same cut.
> - **Then the four one-way regressions** - resolution tier, HDR, bit depth,
>   audio channels - each of which loses something the library will not get
>   back. Every one is a *veto*: the test that matters here is a 1440p HDR AV1
>   remux that beats a plain 4K copy on every other axis and on the score, and
>   is still refused, because the pixels are already on disk.
> - **Then the margin.** Only what survives all of that is scored, and it must
>   win by `DUPLICATE_MIN_SCORE_GAIN`; a rounding-error improvement is not
>   worth rewriting a movie for. Size is not an input at any point - the rule
>   the old size heuristic broke.
>
> The probe reader got the same treatment, against a faked `ffprobe`: an
> attached 4000x6000 sleeve scan is not the feature video stream (it has three
> times the pixels of the 1080p film it is embedded in, and "largest stream"
> alone would call that movie 4K), a container with no duration falls back to
> the stream, and every way ffprobe can be useless - non-zero exit, HTML on
> stdout, a hang, a missing binary - produces a reason and *keeps the existing
> movie*. Fail-closed is the whole policy: no probe, no replacement.
>
> 1,238 → 1,296 tests; `movie_standardizer.py` 68% → **71%**, toolkit 79%.
> Checked against fifteen deliberate mutations, two of which survived the
> first pass and are the reason two of the tests above look the way they do:
> a one-tier downgrade needs a candidate that wins on everything else to be
> visible, and cover art has to be *bigger* than the feature to be mistaken
> for it.

> **Update — phase 6j (W5): the fetcher's report is computed, not narrated.**
>
> `queue_run` was 726 lines and it *ended* by building a 40-key summary dict
> inline: the day's caps and reservations, the per-scraping-source tallies,
> `quota_reached`, and the coverage numbers the report leads with. That dict is
> what the report, `--summary-json` and the exit code are all read from, and
> the only way to see one was to run the whole fetcher against a library.
>
> It is a function of a ledger and a result list, so it is one now:
> `run_summary()`, with `providers_with_capacity()` (what decides "come back
> tomorrow" - and SubDL needs all three of its gates open to count, because a
> download allowance it cannot search against is not capacity) and
> `coverage_count()` (the product promise: an already-covered movie counts
> exactly as much as one downloaded this run; a dry run counts its forecast).
>
> Three more decisions came out of the loop's interior for the same reason -
> they were reachable only by driving a provider:
>
> - **`subdl_unavailable_reason()` / `opensubtitles_unavailable_reason()` /
>   `subdl_defer_detail()`** — the phrases a review hold is assembled from,
>   previously if/elif chains three levels deep. This is the vocabulary the
>   operator acts on: *"daily search cap exhausted"* means wait until tomorrow,
>   *"identity fallback disabled"* means turn a switch back on, and the two
>   must never be printed for each other's situation. The order is now stated
>   where it can be read: a scraping retry explains the miss even with quota to
>   spare, and a spent download cap is reported ahead of a spent search cap
>   because there is no point searching for a subtitle that cannot be
>   downloaded today.
> - **`candidate_from_scrape()`** — a scraping hit presented as the same
>   `Candidate` the APIs return, so the tiers below it need no branch. The
>   fields with no scraping equivalent are stated honestly in one place: no
>   moviehash match, no votes, nothing trusted. The chain validated bytes,
>   which is a weaker claim than a provider's metadata and must not be dressed
>   up as one.
> - **`selection_note()`** — the single line recording *why this subtitle*,
>   which is written into the durable record and is how a bad pick is traced
>   back to its tier months later.
>
> No behaviour change, and again checked rather than asserted: five
> configurations (each cap exhausted in turn, identity fallback off, a dry run)
> were run against the same fake library on the previous tree and this one, and
> the results, the summary dict and the rendered report are byte-identical.
> 1,210 → 1,238 tests, checked against fourteen deliberate mutations - swapping
> the two SubDL cap phrases, letting a scraped candidate claim a moviehash
> match or provider trust, counting a dry run's forecast as real coverage,
> dropping already-covered movies from the tally, and letting a SubDL with no
> search allowance keep a run alive.

> **Update — phase 8d (W7): one answer to "what will this terminal take?"**
>
> Two tools asked the same two questions - *can this console take colour, and
> can it take these characters?* - and answered them differently, so the same
> terminal could get colour from `mkv_track_cleaner.py` and plain text from
> `organize.py`. The CLI ignored `FORCE_COLOR` (so every CI log it wrote was
> colourless whatever the operator asked for), and it enabled Windows VT mode
> by **assigning** the console mode the literal `7`, which turns on the three
> bits in it and silently clears every other flag the console had set. The
> cleaner read the mode and OR-ed in the one bit it needs, which is the correct
> way to do it.
>
> `organizekit/core/console.py` - which already held `enable_utf8_stdio()` and
> `print_text()` - now also holds `Ansi`, `enable_windows_vt()`,
> `color_enabled()`, `stream_can_encode()`, `style()` and `write_raw()`. The
> precedence is stated once, in one function: an explicit `--no-color` beats
> everything, then `--color`/`FORCE_COLOR`, then `NO_COLOR`/`TERM=dumb`, then
> `isatty()` - and every "yes" is gated on VT actually being available, because
> a Windows console that refuses the mode would otherwise be sent escape codes
> it prints literally.
>
> **What is shared is the capability, not the rendering.** A scorecard and a
> live remux progress bar want different things drawn; both want the same
> answer about the terminal. So each tool keeps its own palette
> (`organize.py`'s `green()`, `LiveConsole`'s bar) over the shared primitive,
> and each keeps its own *enablement model* too: the CLI decides once at
> import, the cleaner decides per run from `--no-color`.
>
> **The one intended behaviour change** is that the CLI now honours
> `FORCE_COLOR` and no longer clobbers the Windows console mode. Everything
> else is byte-identical, and that was checked rather than assumed: the old
> tree was extracted to a scratch directory with `git archive`, both trees were
> run against the same fake two-movie library, and the cleaner's output
> differed only in the PID line. 1,178 → 1,210 tests, checked against thirteen
> deliberate mutations - dropping the `FORCE_COLOR` branch, inverting
> `--no-color`, letting a pipe fill with escape codes, restoring the literal
> mode `7`, calling an unprintable glyph printable, and letting a closed stdout
> end a six-hour remux queue.

> **Update — phase 8c (W7, part two): the release, checked before it is made.**
>
> A version on PyPI is immutable: a broken 3.6.0 cannot be fixed, only answered
> with 3.6.1. So this phase is mostly about what is verified before the upload.
>
> **The distribution had to be renamed.** `organize` on PyPI has belonged to an
> unrelated tabular-data parser since 2011, and `organize-media` to a media
> copier. It is published as **`organizekit`** - the name the shared package
> already has on disk - and the console script is still `organize`, so
> `pip install organizekit` gives you `organize doctor`.
>
> **Three real defects, found by actually installing the thing.** The wheel's
> `organize test --unit` handed the operator an `ImportError` traceback out of
> unittest's discoverer: the guard asked *"am I a zipapp?"* when the question is
> *"is the suite here?"* - it now asks the second, which covers both
> deployments. The sdist carried `tests/test_*.py` and none of the fixtures they
> import, so its suite could not even be collected; `MANIFEST.in` now ships the
> whole suite, its `selftests/`, the docs the link tests check, `__main__.py`
> and the zipapp builder - and the unpacked sdist runs all 1,152 tests green.
> The default sdist also omitted `__main__.py`, which meant it could not build
> its own zipapp.
>
> **`tests/test_packaging.py`** reads the declarations and holds them against
> the files on disk, offline and in milliseconds: every root-level tool appears
> in `py-modules` (the drift that works in a checkout and vanishes from the
> wheel), every subpackage is listed, the console script is `organize` whatever
> the distribution is called, the version is single-sourced, the Python floor
> matches the one `doctor` enforces, and nothing in `MANIFEST.in` points at a
> file that no longer exists. `tests/test_docs.py` now also runs `git apply
> --check` on every held-back workflow patch, because a patch nobody can apply
> any more looks like finished work.
>
> **`docs/release-workflow.patch`** adds a tag-triggered release workflow using
> PyPI Trusted Publishing, so no API token exists to leak: it re-runs the suite
> before building, refuses a tag that disagrees with `organizekit.VERSION`,
> installs the wheel into a clean venv and runs it from outside the source tree,
> executes the sdist's own suite, and attaches the wheel, the sdist and
> `organize.pyz` to the GitHub release. It is a patch rather than a commit for
> the usual reason: this branch's bot has no `workflows` permission.
> 1,135 -> 1,152 tests, nine mutations checked.

> **Update — phase 8c (W7): a run reports as it happens.**
>
> The three `--json` commands describe states, and a run is not one. `doctor`,
> `status` and `audit` answer a question that can be read in one shot; a full
> pipeline is an hour of work across five child processes, and the questions
> worth asking about it - which step is running now, how long did the remux
> take, what failed at 03:12 - cannot be answered by a document that only
> exists once it is over. A run also cannot print to stdout: stdout belongs to
> the tools it launches. So it reports to files, and it reports twice.
>
> **`--events PATH`** appends one JSON object per line as the run happens
> (`organizekit/core/events.py`, ~60 lines): `run_started`, then a
> `step_started`/`step_finished` pair per step, then `run_finished`. Every line
> carries the full shared envelope, because a reader tailing the file may only
> ever see one of them. Two rules differ from `jsonout.py` and the module says
> so: an event carries a UTC `time` - the one place a clock belongs, since a run
> *is* an occurrence - and the stream is best-effort, so the first `OSError`
> disables it with one note on stderr and the run continues. **Every step emits
> both events**, including one skipped for a missing prerequisite, which meant
> hoisting `build_command` above the prerequisite check so a skipped step can
> still report the `argv` it would have run; a consumer pairs them with no
> special cases. The file is append-only: a run killed halfway still says how
> far it got, and the missing `run_finished` is how you know it was killed.
>
> **`--summary-json PATH`** writes the closing scorecard once. Its numbers and
> the printed summary's now come from one `run_outcome()` - completed, failed,
> not run, exit code - because three renderings of one run that each derive
> "did it work" separately will eventually disagree, and the one that decides
> the process exit code is the one you cannot afford to have wrong.
>
> Both flags are off by default and the human output is unchanged. The version
> trap from 6i sprang again on the way past - `pipeline.py` has its own
> `VERSION = "1.0.0"` and put it in the envelope - so the cross-command suite
> now compares four documents rather than three. 1,101 -> 1,135 tests, ten
> mutations checked.

> **Update — phase 6i (W7): the audit answers in JSON, and the envelope moves
> into the core.**
>
> The third and last read-only command, and the one that made the envelope a
> shared thing rather than a convention. `doctor` and `status` live in
> `organize.py`; the audit lives in `library_auditor.py`, which is a separate
> program with its own report renderer. Two copies of "schema, tool, version,
> command" in two files is exactly the shape the four copies of the run log
> had, so `JSON_SCHEMA`, `slug_id()`, `json_document()` and `print_json()` now
> live in `organizekit/core/jsonout.py`, whose docstring states the four rules
> (one envelope, one schema number, no timestamps, the document owns stdout).
>
> **The repo's own guard caught the first attempt.** Keeping a thin
> `json_document()` wrapper in `organize.py` failed
> `tests/test_shared_core.py::NothingMayReVendorTheCore` - *"a second
> definition is how atomic_write_text lost its fsync in five of six tools"* -
> so the version became a parameter instead and there is exactly one
> definition.
>
> Two smaller things had to move for the audit to speak JSON at all. `RunLog`
> gained a `stream`, because the auditor logs twenty lines to the console
> during a run and they would have landed on top of the document;
> `print_text()` gained the matching destination. Under `--json` the log goes
> to stderr, so the operator still sees it and the log file is still written -
> and a test asserts both.
>
> **The audit document is one row per folder** (`name`, `folder`, `state`,
> `subtitle`, `detail`, `movie_files`) in report order, plus the tallies and
> the path of the plain-text report, which is still written: a JSON run is not
> a different audit. `subtitle` is the auditor's own split of a folder state
> into a subtitle verdict, exported rather than left for a consumer to
> re-derive. Every failure is a document too - invalid config (2), a busy lock
> (3), an unwritable report (2).
>
> A cross-command suite (`tests/test_json_output.py`) now runs all three and
> compares the documents, so a command that invents its own envelope fails
> there rather than in somebody's parser. It caught a real one: the auditor
> first reported its own `VERSION` (2.1.0) where `doctor` and `status` report
> the toolkit's (3.5.0). One install now reports one version. 1,067 → 1,101
> tests, ten mutations checked.

> **Update — phase 6h (W7): `status --json`, and the envelope becomes shared.**
>
> The second machine-readable command, and the one that turned 6g's one-off
> into an interface. `JSON_SCHEMA`, `json_document()` (schema, tool, version,
> command) and `print_json()` now sit above both commands, so every document
> this CLI emits identifies its producer the same way and a consumer can refuse
> a shape it does not understand. `check_id` became `slug_id` because step
> labels want it too: `Bit depth` → `bit-depth`.
>
> `status` reports the library rather than the machine - `movies`,
> `total_bytes`, `settled`, `pending`, and one row per step with its counts.
> The field worth arguing about is **`recorded`**: it is how a consumer tells
> *"nothing left to do"* from *"nobody has measured this yet"*, which the
> printed report only says in a parenthetical footnote. A dashboard that missed
> that distinction would show a green library that has never been probed.
>
> **A failed run is still a document.** A missing library or a scan that raises
> used to be one red line on stderr and exit 2; under `--json` that would have
> forced a caller to parse two formats and guess which arrived. Now `error` is
> `{kind, message}`, `exit_code` is `2`, and every other field is present and
> empty. Progress and the `--verbose` scan log moved to stderr for the same
> reason: stdout holds the document and nothing else.
>
> Also left out on purpose: the scan duration. It describes the run, not the
> library, and with it in the document no two runs would ever be byte-identical
> - which is the property that lets a nightly job diff today against yesterday.
> 1,051 → 1,067 tests, checked against eight mutations (a duration field, a
> stdout progress line, a hard-coded cache flag, both error paths reverting to
> bare stderr lines, `recorded` pinned true, a zeroed stale count, an unslugged
> id). The status suite was split into a fixture plus one class per rendering,
> so the printed report's tests are not silently re-run against the JSON one.

> **Update — phase 6g (W7): `doctor --json`, the first machine-readable command.**
>
> The check table from 6f made this a renderer rather than a feature: the
> verdicts were already structured data, so `--json` is `diagnostics_document()`
> (rows plus a summary plus the exit code) and a `json.dumps`. No check knows
> which renderer is running, which is why the human scorecard is still
> byte-identical.
>
> Three decisions worth recording. **Match on `id`, not `name`**: one probe can
> emit two rows (both provider keys), so the table key is not unique per row -
> the id is a slug of the row name, unique within a run and stable if the
> printed label is reworded. **`exit_code` is in the document**, so a consumer
> parsing stdout does not also have to capture `$?`. And **there is no
> timestamp**: the caller knows when it ran, and leaving it out means two runs
> on an unchanged machine produce identical bytes, so a nightly job can diff
> today's output against yesterday's and alert only on a real change.
>
> `doctor`'s flags were also defined twice - once in `build_parser()` for
> `organize --help`, once inline in `main()` for dispatch - so `--json` would
> have been easy to add to one and not the other. They are now one
> `add_doctor_arguments()`, matching what `status` already did, and a test
> asserts the two parsers advertise exactly the same options. 1,028 → 1,051
> tests; the seven JSON mutations (a timestamp, a hard-coded exit code, escaped
> Unicode, a banner on stdout, an untrimmed slug, a dropped flag, a miscounted
> summary) plus a deliberate help/dispatch drift were each caught.

> **Update — phase 6f (W5): the doctor becomes a table.**
>
> `run_doctor` was 350 lines: twelve prerequisite checks inlined into one
> function body, five copy-pasted broad-except justifications, and the printing
> tangled into the probing. The cost was not ugliness. **A check could not be
> run without running all twelve**, so in a suite of a thousand tests exactly
> six touched the doctor and every one of them worked by grepping a printed
> page — nothing asserted what any individual check *says* when the program it
> looks for is missing.
>
> Each check is now a function from a `DoctorContext` (the two resolved roots,
> and nothing else) to its verdicts, registered in a `DOCTOR_CHECKS` table that
> is the single place naming which checks exist and in what order they report.
> `run_doctor` is three lines of work: run the table, render, summarise. The
> five duplicated except-blocks collapsed into one `probe_outcome()` whose
> docstring explains — once — why an environment probe catches everything: the
> ways of not finding a program are unbounded, and every one of them means
> "not usable here".
>
> Two behaviours that were implicit are now guaranteed. A check that *itself*
> raises becomes a failed row naming the exception, because doctor is the
> command you run when the machine is in an unknown state and is the last one
> that should die of one. And the exit rule is a named function
> (`diagnostics_exit_code`) rather than three scattered `return`s: any failure
> is 1, warnings alone are 0, since a warning is a step that will skip, not a
> reason to refuse to start.
>
> **The output is byte-identical** — captured before the change and diffed
> after, down to the column padding and the blank line before the scorecard.
> What changed is testability: `tests/test_doctor.py` adds 56 tests that ask
> each check the question it actually answers — a missing mkvmerge, an ffprobe
> that exists but cannot answer, an unimportable sibling, an OCR detector that
> raises, zero/one/both provider keys (and that a key is *never* printed
> unmasked, because doctor output gets pasted into bug reports), a library that
> is a file, cross-device roots, an unreadable device ID. Eight deliberate
> mutations — lowering the Python floor to 3.10, unmasking the key, downgrading
> the cross-device failure to a warning, hard-coding exit 0, accepting a broken
> ffprobe, letting mkvextract pass on one binary of two, dropping multi-row
> results — were each caught by the test named for that rule. `organize.py`
> coverage 78% → **83%**, repo 77% → **78%**, 972 → 1,028 tests.

> **Update — phase 6e (W5): the properties, and what they found.**
>
> The suite was strong on examples and had nothing of the other kind: state an
> invariant, then let the machine look for a counterexample. `tests/property.py`
> is that harness in sixty lines of standard library (no `hypothesis` in an
> offline repo with zero dependencies), and `tests/test_properties.py` is
> thirty-eight properties over the four places an unforeseen input would be
> expensive: the fail-closed HDR rule, the remux plan that decides which
> tracks survive a rewrite of a movie, the shared subtitle contract, and the
> naming handshake between the ingest hook and the auditor.
>
> The harness is deterministic (each test seeds from its own id;
> `ORGANIZE_PROPERTY_SEED` sweeps), shrinks failures to the smallest case that
> still fails the same way, and is itself made to fail on purpose — plus a
> `MutationTests` class that breaks the *implementation* (queue an HDR file,
> keep the worst audio track, accept any text as a subtitle) and asserts the
> matching property notices. That last part earned its keep immediately: the
> audio-ranking property was re-deriving "best" from the very scoring function
> it was judging, so inverting the ranking left it green. It now plants a
> lossless English Atmos 7.1 track among the random ones and asserts that
> *that* is what survives — an oracle the implementation does not get a vote in.
>
> Two real findings, both from the wide name generator: a title whose own
> words are scene tags (`…_EXTENDED_…`) is not a fixed point under re-parsing,
> because the second pass reads the surviving tag as an edition. Nobody owns a
> film called *Extended*, so the parser was left alone and the two exact
> shapes are pinned in a named test, with the property scoped to the realistic
> names the tool is for — 4,000 of which are fixed points. 934 → 972 tests,
> +0.2 s.

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

> **Update — phase 8b (W7): the front page is a front page again.** The README
> had reached 780 lines, which is where a reader stops. It is now 340: the
> pitch, the quickstart, the repo map, one line per tool, the single-file
> build and the safety invariants — everything else moved verbatim into
> `docs/tools.md`, `docs/pipeline.md`, `docs/configuration.md` and
> `docs/development.md`, each linked from the front page and each linking
> back. No prose was cut; a check confirms every line of the old page still
> exists somewhere.
>
> Splitting a document is how link rot starts, so `tests/test_docs.py` walks
> every Markdown file and resolves every relative link and every `#anchor`
> against the real headings, using GitHub's slug rules (an emoji heading is
> reached as `#-quickstart`; a test that missed that would have passed on
> links a reader cannot follow). The front page also has a size budget now —
> 450 lines, asserted — so "does this belong on the front page?" has an
> answer rather than a default. 927 → 934 tests.

### W7 · Ops, UX, distribution

- **`organize status`** (W2) — the missing verb.
- **Machine-readable output** — ~~`--json` on every command~~ **done for the
  three read-only commands** (phases 6g, 6h, 6i): `doctor`, `status` and
  `audit` print versioned, timestamp-free documents on one shared envelope in
  `organizekit/core/jsonout.py` (see the notes below). Still to come: one JSONL
  event stream per *run* plus `run_summary.json`, which is a different problem
  — those describe work being done, not a library being read.
  Cron/Healthchecks/Grafana integration becomes trivial; the human reports stay
  exactly as they are.
- **One shared console layer** — ~~currently only in the cleaner~~ **the
  *capability* half is done** (phase 8d): what colour and which characters a
  terminal will take is now one decision in `organizekit/core/console.py`, used
  by both the CLI and the cleaner's `LiveConsole`. Adopting the renderer itself
  in the other four tools is deliberately *not* done here — that is a
  user-visible UI change, and this phase was a no-behaviour-change one.
- **Distribution:** publish to PyPI (`pipx install organize` / `uvx organize`),
  attach `organize.pyz` and the generated standalone scripts to each GitHub
  release, and ship systemd-timer and Task-Scheduler templates in `docs/`.
- ~~**Docs:** the 585-line README becomes a ~120-line front door plus
  `docs/{install,tools,pipeline,troubleshooting,design}.md`.~~ **done** — see
  the phase 8b note below. The *why* prose is the repo's best asset — it
  deserves to be findable, not scrolled past.
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
| ~~**4c**~~ | ~~W4 the fetcher's local pre-flight parallelised (`triage_movie` + `TriageQueue`, `--workers`)~~ **done**; the quota ledger stays in the log — spending is still serial, by decision | +300, +18 tests | Low | ✅ |
| ~~**5**~~ | ~~W2 SQLite state cache + write-through + `organize status`~~ **done** (probe caches and the fetcher's quota ledger not yet moved in; `core/scan.py` rejected — see the W2 note) | +841 | Med-High | ✅ |
| ~~**5b**~~ | ~~W2 the remux step publishes per-movie verdicts; `organize status` stops printing `Remux  not recorded yet`~~ **done** | +120, +26 tests | Low | ✅ |
| ~~**6a**~~ | ~~W5 fault-injection, end-to-end and destructive-path suites, coverage 69% → 75%, gate → 72~~ **done** | +1,240 | Low | ✅ |
| ~~**6b**~~ | ~~W5 the fetcher's spending planners extracted from `queue_run` and tabled~~ **done** (the property tests followed in phase 6q) | +460 | Low | ✅ |
| ~~**6c**~~ | ~~one shared `RunLog`, the last duplicated implementation; `core/toolchain.py` off the blanket `BLE001` ignore~~ **done** (the nine tool files' 84 broad catches are still blanket-ignored) | −62, +200 tests | Low | ✅ |
| ~~**6d**~~ | ~~every `except Exception` in the toolkit narrowed or justified in place; no file-wide `BLE001` exemption left~~ **done** | 27 narrowed, +290 tests | Low | ✅ |
| ~~**6e**~~ | ~~W5 property-based tests on seeded stdlib `random`: a shrinking harness, 38 properties over the fail-closed, destructive and cross-tool rules, and mutation tests that prove they notice~~ **done** | +38 tests | Low | ✅ |
| ~~**6f**~~ | ~~W5 `run_doctor` split into a table of twelve named check functions; `doctor`'s output byte-identical~~ **done** | +132 lines, +56 tests | Low | ✅ |
| ~~**6g**~~ | ~~W7 `organize doctor --json`: a versioned, deterministic document over the same check table, with the flags defined once for both parsers~~ **done** | +90 lines, +23 tests | Low | ✅ |
| ~~**6h**~~ | ~~W7 `organize status --json` on the same envelope; the JSON helpers generalised out of `doctor`; a failed run is still a document~~ **done** | +110 lines, +16 tests | Low | ✅ |
| ~~**6i**~~ | ~~W7 `library_auditor.py --json`; the envelope moved into `organizekit/core/jsonout.py`; `RunLog` learns where its console lines go~~ **done** | +150 lines, +34 tests | Low | ✅ |
| ~~**6j**~~ | ~~W5 the fetcher's run summary, coverage tally, review-hold vocabulary and scraping-candidate conversion extracted from `queue_run` (726 → 653 lines) and tabled~~ **done** | +150 lines, +28 tests | Low | ✅ |
| ~~**6k**~~ | ~~W5 the standardizer's replacement policy: the upgrade guard chain split from the probing (`upgrade_verdict`), and the probe reader, the score and the gate tabled~~ **done** | +40 lines, +58 tests | Low | ✅ |
| ~~**6l**~~ | ~~W5 the cleaner's concurrent-change refusals proved end to end: the library changed inside the remux window (source, sidecar, Ctrl-C), plus the free-space and journal fail-closed paths~~ **done** | +10 tests, cleaner 79% → 81% | Low | ✅ |
| ~~**6m**~~ | ~~W5 the inspector run end to end against a fake ffprobe: classification through the whole program, probe failures, cache reuse, config refusals and the `--fail-if-*` exit codes~~ **done** | +40 tests, bitdepth 67% → 93% | Low | ✅ |
| ~~**6n**~~ | ~~W5 the standardizer run end to end: hardlink ingest proved additive, every refusal reported, config refusals — and the four unreachable helpers it exposed, deleted~~ **done** | +53 tests, −54 lines, standardizer 71% → 85% | Low | ✅ |
| ~~**6o**~~ | ~~W5 the fetcher's run loop against a fake provider at the `urlopen` seam: validated writes, every refusal, the cross-run daily cap and the coverage exit code~~ **done** | +39 tests, fetcher 78% → 80% | Low | ✅ |
| ~~**6p**~~ | ~~W5 the fetcher's other two tiers end to end: SubDL's two v2 routes, the seven scraped sites behind one `urlopen`, and the pooling, metering and failover rules between all three~~ **done** | +25 tests, fetcher 80% → 82% | Low | ✅ |
| ~~**5c**~~ | ~~W2 the two JSON probe caches folded into `state.db`'s `probe` table, with the old files imported once and `--cache` still honoured~~ **done** | +38 tests, probecache 94% | Low | ✅ |
| ~~**6q**~~ | ~~W5 property-based tests over the fetcher's planners: selection, pooling, the SubDL threshold, the download-URL guard and the quota arithmetic~~ **done** | +24 tests, 12 planner mutations killed | Low | ✅ |
| ~~**6r**~~ | ~~W5 end-to-end tests for `refetch_english_srt`, the cross-tool seam that is the only code allowed to overwrite an existing sidecar~~ **done** | +27 tests, 10 mutations killed, fetcher 82% → 84% | Low | ✅ |
| ~~**6s**~~ | ~~W5 the queue's failure branches end to end: hash failures, provider outages, an adapter crash, a sidecar appearing mid-download; fixed a ledger checkpoint that dropped every outcome after a reservation~~ **done** | +19 tests, 11 mutations killed, fetcher 84% → 86% | Low | ✅ |
| ~~**6t**~~ | ~~W5 the extraction/OCR toolchain: USF, backend lookup, `run_ocr`, and the inside of `_extract_one_track`; fixed an unreachable mono wrapper and a half-checked `--ocr-args`~~ **done** | +39 tests, 12 mutations killed, fetcher 86% → 89% | Low | ✅ |
| ~~**6u**~~ | ~~W5 the seven scraped adapters as parsers: hostile HTML/JSON, the transport's failure translation, and the chain's breaker bookkeeping; fixed an Addic7ed language read that dropped valid rows when the row layout moved~~ **done** | +66 tests, 31 mutations killed, fetcher 89% → 91% | Low | ✅ |
| ~~**6v**~~ | ~~W5 the SubDL client either side of its search: auth, retry/backoff, bounded reads, the download leg and the archive reader; fixed an HI-copy pattern that could never match~~ **done** | +58 tests, 30 mutations killed, fetcher 91% → 92% | Low | ✅ |
| ~~**6w**~~ | ~~W5 the fetcher's edges: config validation, the library walk, the layout contract, the ledger reader, sidecar inspection and the toolchain helpers; closes the coverage campaign on the largest file~~ **done** | +66 tests, 37 mutations killed, fetcher 92% → 94% | Low | ✅ |
| **7** | ~~W6 direct-play verification, HandBrake queue, multi-language~~ **out of scope** — code health only, by decision | +1,500 | Med | — |
| ~~**8a**~~ | ~~W7 `organize.pyz` single-file build; one launch rule for both deployments~~ **done** | +330, +15 tests | Low | ✅ |
| ~~**8b**~~ | ~~W7 docs split: a 780-line README becomes a 340-line front page plus `docs/{tools,pipeline,configuration,development}.md`, with the links and the size budget tested~~ **done** | +7 tests | Low | ✅ |
| ~~**8c**~~ | ~~W7 the rest of the machine-readable output (a JSONL run stream, `run_summary.json`); the PyPI release: distribution `organizekit`, a complete sdist, a tag-triggered Trusted-Publishing workflow~~ **done** (the upload itself needs a maintainer with PyPI access) | +190, +51 tests | Low | ✅ |
| ~~**8d**~~ | ~~W7 the shared console layer: colour capability, Windows VT mode, glyph support and safe raw writes decided once in `core/console.py` for both the CLI and `LiveConsole`~~ **done** (adopting the *renderer* in the other tools is a UI change, deferred) | −60, +32 tests | Low | ✅ |

> **Where this ends (as of phase 6w).** Every numbered phase in the table
> above is done except phase 7, which was taken out of scope by decision (code
> health only, no new product features). What is deliberately *not* being done,
> and why:
>
> - **Adopting `LiveConsole`'s renderer in the other four tools** — a
>   user-visible UI change, not code health. The *capability* half (colour, VT
>   mode, glyphs, safe writes) is already shared.
> - **Concurrent provider requests** — deprioritised; the per-host token
>   buckets that would make it safe exist, but a nightly run over a settled
>   library is not request-bound.
> - **HTTP keep-alive** — worth 100–300 ms per request and the cheapest
>   remaining speed item, but it means holding a connection pool open across a
>   run, which is state where there is currently none.
> - **The last ~200 uncovered lines in `subtitle_fetcher.py`** — scattered in
>   ones and twos across forty functions; there is no block left worth a phase.
>
> The plan is, in other words, finished. Anything after this is maintenance:
> keeping the suite green, and the two optional items above if they are ever
> wanted.

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
| Production lines | 26,458 | 21,462 (20,011 after phase 3; W2/W4b added back) | ~23,000 |
| Duplicated lines | 4,325 | ~0 | **0** (generated) |
| Coverage | 58% | **84%** (cleaner 81%, inspector 93%, standardizer 85%, fetcher 82%) | ≥75%, cleaner ≥80% ✅ |
| Test runtime | 6.7 s | 22.5 s (1,800 tests, incl. building and running the zipapp) | ≤15 s (with property + fault-injection tests) ⚠ just over |
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
