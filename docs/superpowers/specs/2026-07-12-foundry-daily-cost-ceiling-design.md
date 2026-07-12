# Talos Foundry — Cross-Run Daily Cost Ceiling

**Date:** 2026-07-12
**Status:** design approved, pending implementation
**Scope:** a durable, cross-invocation daily budget so a nightly cron running
`foundry run` repeatedly cannot spend without bound. Follow-up to the LLM-client
spec, which capped one reconcile pass but left each invocation a fresh budget.

---

## Problem

The Foundry already caps LLM calls **within one `foundry run`**: `reconcile`
builds one shared `ForgeBudget(max_llm_calls=batch_max_llm_calls)`, charges it
before every `llm.complete()` (so a failed job's spend still counts), and
pre-checks a projected per-job cost before launching the next job. That gap is
closed.

But **each invocation builds a fresh budget**. A nightly cron (the design's
stated trigger — talos-design.md "按需 + nightly cron", "token/時間/compute 硬預
算封頂") running `foundry run` repeatedly has no cumulative ceiling: N passes can
spend up to `N × batch_max_llm_calls`. Nothing persists spend across runs, and
`coder.total_tokens` is printed then lost.

This spec adds a durable daily ceiling in **call-count units** (consistent with
`ForgeBudget`; USD is out of scope — it needs a per-model pricing table).

---

## Non-goals

- USD / token-based budgeting (calls only; `max_tokens` already bounds per-call
  output).
- Rotating / compacting the ledger (a nightly cadence accrues a few thousand
  lines a year; whole-file sum is sub-millisecond — YAGNI).
- Multi-host coordination (single-host assumption, as with the candidate-parquet
  single-writer model, design C-8).

---

## Approved decisions

- **D1** — durable budget is a **UTC calendar day** window (resets at UTC
  midnight; matches nightly-cron semantics).
- **D2** — when today's remaining < the requested batch, **shrink** the batch to
  the remaining allowance (`effective = min(batch_max_llm_calls, remaining)`);
  refuse only when `remaining <= 0`.
- **D3** — units are **LLM call counts** (reuse `ForgeBudget`).
- **D4** — a **single-instance lock** (`os.mkdir`) prevents cron-misfire
  double-runs, with age-based stale takeover.

---

## Adversarial review (agy) — dispositions

Verified and adopted before writing:

- **Accepted (critical) — spend must be recorded even when reconcile aborts.** A
  batch abort (`LLMUnavailable` / bare `SandboxError`) raises out of `reconcile`;
  a `record_spend` placed after it would be skipped, so already-spent calls go
  unrecorded and the next run overspends. `main` holds the shared budget it built,
  so it records in a `try/finally`.
- **Accepted (correctness) — midnight boundary.** A run that starts 23:59 (Day A)
  and finishes 00:04 (Day B) must bill Day A, not "now". `main` pins
  `today = utcnow().date()` once at start and `record_spend` uses that exact date.
- **Accepted — reject non-positive budgets at argparse** (`batch_max_llm_calls > 0`,
  `daily_max_llm_calls > 0`), so `effective` is never 0 and `ForgeBudget(0)` never
  occurs.
- **Accepted — record failure exits non-zero.** After a loud log (the daily guard
  is compromised until fixed), `main` returns a non-zero code so cron/CI alarms;
  the run's already-persisted artifacts are untouched.
- **Accepted — single-instance lock via `os.mkdir`** rather than a file-granular
  read/write lock: atomic on POSIX and Windows, and it also prevents the doubled
  LLM/sandbox load of a misfire, not just the ledger TOCTOU. Added stale takeover.
- **OK, no change — ledger location/growth.** `<runs-dir>/foundry_spend.jsonl`,
  whole-file sum; rotation is YAGNI.

### Second review (agy, folder mode against the code) — dispositions

- **Accepted (critical) — `finally` must not mask reconcile's exception.** If
  reconcile raises and the `finally`'s `record_spend` then `return`s or raises,
  the operator loses the traceback of what killed the batch. The `finally` wraps
  recording in its own `try/except` that only logs; reconcile's exception
  propagates; a record failure returns non-zero only when reconcile succeeded.
- **Accepted — per-`--runs-dir` scoping is a bypass if unstated.** Different
  `--runs-dir` = independent ledgers = doubled cap. Documented (§3-2) and made
  overridable via `--spend-ledger` for a global budget.
- **Accepted — lock staleness from the dir's own `st_mtime`, not `meta.json`.**
  `mkdir` is atomic; the post-mkdir `meta.json` write is not, so a peer could
  read a half-written/absent file. Age comes from the directory stat; a
  `meta.json` read error is treated as "fresh", never as grounds to steal.
- **Accepted — a failed stale-lock `rmtree` logs loudly** (else a lock that can't
  be removed silently wedges every future run).
- **Accepted — no `assert` for arg validation.** `type=positive_int` matches the
  existing print+return-2 convention rather than throwing an `AssertionError`.
- **Accepted — `--daily-max-llm-calls` default None** (guard inactive, back-compat)
  so a missing value never hits `None - int`; the guard is opt-in and documented.
- **OK — wiring and `shared.used` semantics** verified against `forge.py`
  (charge before `complete`, so abort-time spend is counted) and the projected-cost
  pre-check (unaffected by an injected budget).

---

## Section 1 — Architecture & data flow

A cross-run daily guard wraps the existing per-reconcile budget; the shared
`ForgeBudget` mechanism is unchanged.

```
foundry run --batch-max-llm-calls 6 [--daily-max-llm-calls 40]
  argparse: --batch-max-llm-calls type=positive_int (existing default 6);
            --daily-max-llm-calls type=positive_int, default None
            (a bad value prints + returns 2 like the existing bad-arg path — no assert)
  # --daily omitted -> no cross-run guard (per-reconcile budget only, back-compat)
  with single_instance_lock(<runs-dir>/foundry.lock, stale_hours=6):
      if daily_max_llm_calls is not None:
          today = datetime.now(timezone.utc).date()      # pinned once (bill the day we budgeted)
          remaining = daily_max_llm_calls - todays_spend(ledger, today)
          if remaining <= 0:  refuse (return 2), reconcile never called
          effective = min(batch_max_llm_calls, remaining)
      else:
          effective, today = batch_max_llm_calls, None
      shared = ForgeBudget(max_llm_calls=effective)       # main holds the ref
      record_error = None
      try:
          reconcile_foundry_jobs(..., shared_budget=shared)   # abort (LLMUnavailable/SandboxError) may raise
      finally:
          if daily_max_llm_calls is not None:
              try:
                  record_spend(ledger, day=today, calls=shared.used,
                               tokens=getattr(llm, "total_tokens", 0))  # recorded even on abort
              except Exception as e:            # MUST NOT return/raise here: that would mask
                  log.error("daily guard compromised: spend not recorded: %s", e)  # reconcile's own traceback
                  record_error = e
      # reconcile's exception (if any) propagates naturally out of the finally.
      # Only when reconcile SUCCEEDED but recording failed do we signal it:
      if record_error is not None:  return 2
```

### N1 — spend ledger (`research/hermes/foundry_spend.py`)

Append-only JSONL, default `<runs-dir>/foundry_spend.jsonl`, one line per run.
The path is **overridable via `--spend-ledger`** so an operator who wants a
single global budget across several `--runs-dir` can point them all at one file
(see §3 for why the default is per-runs-dir and what that scoping means). Format:
`{"date": "2026-07-12", "ts": "<iso>", "calls": 6, "tokens": 2562}`.

- `todays_spend(path, day: date) -> int` — sum `calls` over lines whose `date`
  equals `day.isoformat()`. A malformed line is skipped (best-effort), never
  crashes the guard.
- `record_spend(path, day: date, calls: int, tokens: int)` — append one line with
  the **passed** `day` (not "now"), via an atomic append.

### N2 — single-instance lock

`single_instance_lock(runs_dir, stale_hours=6)` context manager:
`os.mkdir(<runs-dir>/foundry.lock)` is the atomic acquire. If it already exists,
decide staleness from **the lock directory's own `os.stat().st_mtime`**, not from
a `meta.json` inside it: `mkdir` stamps the directory atomically, whereas writing
a `meta.json` afterward is a second, non-atomic step — a peer that reads a
half-written or absent `meta.json` would `FileNotFoundError`/`JSONDecodeError` and
could crash or falsely declare the lock stale. (`meta.json` is still written with
`{pid, ts}` for human diagnostics only; a read failure there is treated as
"fresh", never as grounds to steal the lock.) If `now - dir_mtime > stale_hours`,
treat it as a crashed run, `rmtree` and retry the `mkdir` once; **if that `rmtree`
fails, `log.warning` loudly** (otherwise a lock that can't be removed — perms,
Windows file lock — silently wedges every future run until `stale_hours` elapses
again, with no operator signal) and raise `AlreadyRunning`. Otherwise (fresh)
raise `AlreadyRunning` immediately. `main` returns non-zero on `AlreadyRunning`.
The `finally` removes the lock dir. `stale_hours=6` is chosen `>>` a normal run,
so takeover almost never races a live long run.

### N3 — reconcile accepts a pre-built shared budget

`reconcile_foundry_jobs(..., batch_max_llm_calls=None, shared_budget=None)`: if
`shared_budget` is given, use it (so `main` keeps the reference for `finally`
recording); else build one from `batch_max_llm_calls` as today (back-compat). The
projected-cost pre-check reads `shared.used` / `jobs_attempted` unchanged.

---

## Section 2 — Components & files

| File | Responsibility |
|---|---|
| `research/hermes/foundry_spend.py` (create) | `todays_spend`, `record_spend`, `single_instance_lock`, `AlreadyRunning` |
| `research/hermes/foundry_runner.py` (modify) | `reconcile` gains `shared_budget=None`; `main` run gains `--daily-max-llm-calls` (type=positive_int, default None) + `--spend-ledger` (default `<runs-dir>/foundry_spend.jsonl`) + `--batch-max-llm-calls` becomes `type=positive_int`; lock, pinned UTC date, mask-safe `finally` recording, return 2 on refuse / record-failure |
| `research/tests/test_hermes_foundry_spend.py` (create) | ledger + lock unit tests |
| `research/tests/test_hermes_foundry_runner.py` (modify) | daily-guard integration tests |

---

## Section 3 — Error handling & honest boundaries

| situation | behaviour |
|---|---|
| `--daily-max-llm-calls` omitted | no cross-run guard; per-reconcile budget only (back-compat) |
| `remaining <= 0` | refuse, return 2, reconcile never called |
| reconcile aborts (LLMUnavailable / SandboxError) | `finally` records `shared.used`, then **reconcile's exception propagates** (the recording never masks it) |
| `record_spend` raises inside `finally` | log loudly, set `record_error`, **do not return/raise in `finally`**; return 2 only if reconcile itself succeeded |
| another instance holds a fresh lock | `AlreadyRunning`; return 2 without running |
| lock is stale (`dir mtime age > stale_hours`) | rmtree + retry mkdir once; if rmtree fails, `log.warning` + `AlreadyRunning` |
| lock exists, `meta.json` unreadable | treat as fresh (never steal on a read error) |
| non-positive `--batch`/`--daily` | `type=positive_int` → argparse error, before any side effect (not `assert`) |

**Honest boundaries (in docstrings):**

1. **Calls, not USD — and calls is a floor.** A call's cost varies by
   model/tokens; `max_tokens` bounds output. Moreover `ForgeBudget` counts one
   `llm.complete()`, but a transport-layer retry (HTTP 502/429, `MAX_RETRIES`)
   can issue more than one real API request per call, so the *actual* API spend
   can exceed the nominal `--daily-max-llm-calls`. Set the daily cap with head-
   room. Cross-model dollar accounting is deferred (needs a pricing table).
2. **The daily budget is per-`--runs-dir` by default.** The ledger and lock live
   under `<runs-dir>`, so two runs with **different** `--runs-dir` keep
   independent ledgers and the daily cap is effectively doubled. This is fine for
   the intended use (one cron, one runs-dir), and an operator who wants a single
   global budget across experiments points them all at one `--spend-ledger` (the
   lock stays per-runs-dir; the ledger is what bounds spend). Stated so no one
   assumes it protects an API key globally by default.
3. **Lock is single-host.** Two instances that call `os.mkdir` within the same
   instant: only one wins (mkdir is atomic), so this is safe on one host; it does
   not coordinate across machines.
4. **Stale takeover is age-based, not liveness-based.** A genuine run exceeding
   `stale_hours` could be taken over. `stale_hours=6` is set far above a normal
   run to make this vanishingly unlikely.
5. **Ledger is trusted.** A hand-edited or externally-corrupted ledger mis-states
   today's spend; malformed lines are skipped but a wrong `calls` value is taken
   at face value.

---

## Section 4 — Test matrix (no test spends money)

`foundry_spend.py` — `test_hermes_foundry_spend.py`:

| test | assertion |
|---|---|
| `test_todays_spend_sums_only_today_utc` | today's lines summed; yesterday/tomorrow excluded |
| `test_todays_spend_skips_malformed_line` | a corrupt line is skipped, sum still returned |
| `test_record_spend_appends_with_pinned_day` | appended line's `date` == the passed day, not `now` |
| `test_lock_blocks_a_second_instance` | fresh lock present → `AlreadyRunning` |
| `test_stale_lock_is_taken_over` | lock `ts` older than `stale_hours` → acquired |
| `test_lock_released_on_exit` | after the `with` block, the lock dir is gone |

`main` daily guard — `test_hermes_foundry_runner.py`:

| test | assertion |
|---|---|
| `test_remaining_zero_refuses_and_skips_reconcile` | ledger already at daily cap → non-zero return; monkeypatched `reconcile` **not called** |
| `test_effective_shrinks_to_remaining` | daily=40, ledger today=37, batch=6 → reconcile receives a `shared_budget` capped at 3 |
| `test_spend_recorded_even_when_reconcile_aborts` | reconcile raises `LLMUnavailable` after charging the shared budget → ledger still gets a line with those calls |
| `test_reconcile_abort_traceback_is_not_masked_by_recording` | reconcile raises `LLMUnavailable`; **that** exception propagates out (not swallowed by the `finally` recording), even if `record_spend` also fails |
| `test_argparse_rejects_nonpositive_budgets` | `--batch-max-llm-calls 0` / `--daily-max-llm-calls 0` → argparse error (SystemExit 2), no traceback |
| `test_daily_omitted_skips_the_guard` | no `--daily-max-llm-calls` → reconcile runs with the plain per-reconcile budget; no ledger read/write |
| `test_record_failure_returns_nonzero_when_reconcile_succeeded` | reconcile succeeds, monkeypatched `record_spend` raises → loud log, return 2 |

### Honestly-recorded gaps

- The real OpenAI/OpenRouter endpoint is still exercised only by the manual paid
  run; these tests monkeypatch reconcile and touch only files.
- Concurrency beyond the `os.mkdir` window is not defended (single-host
  assumption), per §3.

---

## Self-review

- **Placeholders:** none. `stale_hours=6` is a concrete default; the ledger schema
  is fixed.
- **Consistency:** the mask-safe `finally` recording (§1) matches the
  non-masking test and the error table (§3). `effective = min(batch, remaining)`
  appears identically in §1/D2/§4. `single_instance_lock` (dir-`st_mtime`
  staleness) is defined in §1-N2, tabled in §3, tested in §4. `--daily` default
  None (guard opt-in) is consistent across §1/§2/§3/§4.
- **Scope:** one implementation plan — a ledger+lock module + a `main` guard + a
  one-arg reconcile extension. No new subsystem.
- **Ambiguity:** the window is a UTC calendar day; enforcement is
  shrink-then-refuse; the lock is `os.mkdir` with dir-mtime age-based takeover;
  the guard is opt-in (`--daily` default None).
