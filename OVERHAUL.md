# Overhaul Plan — `organize`

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

### W2 · One state store, one scan (the highest-leverage new idea)

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

### W3 · One orchestration model

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

### W4 · Concurrency and connection reuse

**Problem.** Only `bitdepth.py` has `--workers`. The two slowest steps are serial.

| Step | Bound by | Today | Proposed default | Expected wall-clock |
| :--- | :--- | :--- | :--- | :--- |
| `subtitle_fetcher` | network RTT | serial | 8 threads + shared token bucket | **5–8×** |
| `sync_subtitles` | CPU (`ffsubsync`) | serial | `cpu_count//2` processes | **3–6×** |
| `mkv_track_cleaner` | disk throughput | serial | 2 (opt-in higher) | 1.3–2× |
| `library_auditor` | `stat()` | serial | threaded scandir | 2–4× |
| `bitdepth` | `ffprobe` | ✅ threaded | unchanged | — |

Two details that matter:

- **Rate limits become a shared token bucket, not serialism.** The fetcher's
  daily-quota accounting is the reason it is serial today. Move quota to the DB
  (W2) and gate requests through one `TokenBucket` per provider; parallelism
  then cannot overspend a quota, because reservation happens before dispatch.
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

### W6 · Close the product gaps

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
| **1** | W1 core extraction + generator + CI gate | −4,325 | Med | 2–3 |
| **2** | W5 self-tests → `tests/`, thin smoke checks remain | −2,000 | Low | 1 |
| **3** | W3 one Step registry; one-shot = convergence loop | −1,400 | Med | 2–3 |
| **4** | W4 workers + token bucket + HTTP keep-alive | +400 | Med | 3–4 |
| **5** | W2 SQLite state + single scan + `organize status` | +900 | Med-High | 3–4 |
| **6** | W5 planners split, property + fault-injection tests, gate → 75% | +1,200 | Low | 3–5 |
| **7** | W6 direct-play verification, HandBrake queue, multi-language | +1,500 | Med | 4–6 |
| **8** | W7 docs split, JSON output, PyPI + zipapp release | +300 | Low | 2 |

**Net: ~26,500 → ~23,000 production lines** that do substantially more, run
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

| | Today | Target |
| :--- | ---: | ---: |
| Production lines | 26,458 | ~23,000 |
| Duplicated lines | 4,325 | **0** (generated) |
| Coverage | 58% | ≥75%, cleaner ≥80% |
| Test runtime | 6.7 s | ≤15 s (with property + fault-injection tests) |
| 500-movie cold pass | hours | **≤ 1/4 of today** |
| 500-movie no-op pass | full 5-tool sweep | **< 5 s** (DB query) |
| Sources of truth for step order | 4 | 1 |
| Direct-play claim | documented | **verified per file** |

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
