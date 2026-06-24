# Live L/S hourly refresh (Phase-1, SOL) — Design

Date: 2026-06-24
Status: Approved (pre-implementation)
Scope: SOL only (`sol_s1`, contrarian long/short positioning strategy)

## Problem

`sol_s1` trades SOL off long/short positioning factors. The live trader already
has a freshness **guard** wired:

- `dashboard/trader/signal.py:103` — refuses to trade (returns `signal=0, stale=True`)
  when the factor store's newest bar (`index_end`) is older than `FACTOR_MAX_AGE_DAYS`.
- `dashboard/trader/loop.py:357` — emits a **warning alert** (no trading change)
  when the store's `generated_at` is older than `REFRESH_STALL_HOURS`.

Nothing currently refreshes `research/manifests/factor_values_sol.parquet`, so the
trader **correctly refuses** to trade. Phase-1 = build the hourly refresher that
keeps `index_end` within the strategy's tolerance.

### Why the trading gate is `index_end`, not `generated_at`

A design review (agy/Gemini) flagged a feared "fail-soft stamps fresh
`generated_at` → trader trades on stale data" hole. **Verified false against
code**: the *trading* decision (`signal.py:103`) keys on `index_end` (true data
age). `generated_at` only drives a loop.py alert. So `dump_oi --live` staying
fail-soft is correct — a stale live tail ages `index_end`, and the trader pauses.
A fail-hard `dump_oi` would be strictly worse (a transient endpoint blip would
fail the whole refresh).

The real dependency is **`FACTOR_MAX_AGE_DAYS` must be tight enough** to bite
before the edge decays. `sol_s1`'s edge half-life is ~12–16h
(`project_sol_paper_forward_phase0`), and the per-strategy control.json already
tightens `FACTOR_MAX_AGE_DAYS=0.5` (12h) for sol_s1 (`manager.py:86`,
`test_manager.py:56`). This is a **deploy-config check**, not code.

## Existing infra (reused, not redesigned)

- `pipeline_manager` (own container, `docker-compose.pipeline.yml`, repo mounted
  **read-write**) watches `runs/pipeline_jobs/<id>/job.json` and runs queued jobs
  **one at a time** (serial). Each job has `steps`; each step runs as
  `python -m research.pipeline.<module>` with `RESEARCH_ONLY_SYMBOL` + interval env.
- `pipeline_jobs.create_job(kind, symbol, interval)` builds `steps` from `kind`
  (today: `stage`, `pipeline`).
- `dump_oi.py --live --symbols sol` overlays a fresh Binance L/S tail onto the OI
  cache. **Fail-soft**: live fetch failure → archive-only, exits 0.
- Ordering: `dump_oi` must run before `stage0a`. `stage0a` writes
  `factor_values_<sym>.parquet` + `.meta.json` (`index_end`, `generated_at`).
- The **trader runs in a separate container** (read-only repo mount) and reads
  the same parquet off the shared host filesystem — hence the cross-container
  write/read race below.

## Design

### 1. New job kind `live_refresh`
`create_job(kind="live_refresh", symbol="sol", interval="1H")` → steps `[0a, 1]`
(both existing, valid stages). No pseudo-stage; the runner stays uniform.

The runner, for a `live_refresh` job's `0a` step, sets `LIVE_OI_REFRESH=1` (in
addition to `RESEARCH_ONLY_SYMBOL=sol` + interval). `stage0a`, when
`LIVE_OI_REFRESH` is truthy, runs `dump_oi --live --symbols <RESEARCH_ONLY_SYMBOL>`
before building features. Default (env unset) = archive-only, deterministic — so
normal pipeline runs are unchanged.

### 2. `factor_io` atomic writes (cross-container race fix)
`research/lib/factor_io.py` `dump_factor_values` (and `dump_features`) currently
`df.to_parquet(path)` **in place**. The separate trader container reads that file
concurrently → every hourly refresh can race a live read → partial/corrupt parquet.

Fix: write parquet to a temp path, `os.replace` (atomic on the same volume), then
write meta to a temp path and `os.replace`. Parquet first, meta second — meta is
the readiness signal (`signal.py` prefers `meta.index_end`).

### 3. Scheduler sidecar `dashboard/server/freshness_scheduler.py`
A loop with an extracted, testable `tick(now)`:
- If no **active** (`queued` or `running`) `live_refresh` job for sol exists,
  `create_job(kind="live_refresh", symbol="sol", interval="1H")`.
- Idempotent: never enqueue while one is active (no pileup if a refresh runs >1h
  or the pipeline is busy).
- **Staleness bound**: an "active" job older than `2 × interval` is treated as
  dead (zombie), so a stuck job can't permanently block enqueues.
- Config via env: `FRESHNESS_SYMBOLS=sol`, `FRESHNESS_INTERVAL_SEC=3600`.
- New service `freshness-scheduler` in `docker-compose.pipeline.yml` (same image,
  RW `runs` mount, `restart: unless-stopped`). Writes `job.json` only;
  `pipeline_manager` executes.

### 4. Queue priority
`pipeline_manager._oldest_queued` picks the oldest `live_refresh` job before any
research job, so the hourly refresh never starves behind a manually-queued full
pipeline. (An already-**running** long job is not preempted — accepted for
paper-forward; see Tradeoffs.)

### 5. Atomic `write_job`
`pipeline_jobs.write_job` → temp file + `os.replace`. Low-impact (the reader
already catches `JSONDecodeError` and rescans), but removes the race entirely and
also hardens the existing dashboard enqueue path.

### 6. Deploy check (no code)
Confirm sol_s1's `control.json` sets `FACTOR_MAX_AGE_DAYS=0.5`. Document in the
deploy runbook.

## Error handling & self-healing

- `dump_oi --live` fail-soft → `0a` still rewrites parquet from archive;
  `index_end` reflects true data age. If repeated live failures age `index_end`
  past 12h, the trader pauses (correct).
- A stage `rc != 0` → `pipeline_manager` marks the job failed; the scheduler
  enqueues a fresh one next hour. Trader keeps refusing while stale.
- `pipeline_manager` crash mid-run → `reconcile_startup` marks the interrupted
  job failed; scheduler's staleness bound covers a hung (non-crashed) manager.

## Tradeoffs (accepted for Phase-1)

- **Serial queue, no preemption of in-flight jobs.** A long manual pipeline that
  is *already running* delays the refresh by its remaining runtime → `index_end`
  may age → trader pauses sol_s1 until the refresh runs. Correct-by-design for a
  paper-forward signal (`sol_s1` is a paper signal, not real-money). **Upgrade
  path for real money:** a dedicated live queue dir + a second
  `live_pipeline_manager`, or true preemption.
- SOL only, hourly, env-tunable. ETH / multi-symbol / dynamic-from-`runs/testnet`
  are out of scope.

## Out of scope (YAGNI)

- No new UI button — `live_refresh` jobs appear in JobsPanel for free.
- No new API endpoint.
- No second pipeline container / separate queue (Phase-1).

## Testing (TDD)

1. `create_job(kind="live_refresh", symbol="sol")` → steps `[0a, 1]`, symbol +
   interval set, interval validated.
2. Runner sets `LIVE_OI_REFRESH=1` only on a `live_refresh` job's `0a` step (not
   on `stage`/`pipeline` jobs).
3. `stage0a` with `LIVE_OI_REFRESH=1` invokes `dump_oi --live --symbols <sym>`
   before feature build; unset → no dump (archive-only, unchanged).
4. `factor_io.dump_factor_values` / `dump_features` atomic: no temp file left
   behind, parquet + meta content intact, meta written after parquet.
5. `freshness_scheduler.tick()`: enqueues when none active; skips when an active
   `live_refresh` exists; re-enqueues when the active one is older than the
   staleness bound (zombie reap).
6. `pipeline_manager._oldest_queued`: returns a `live_refresh` job ahead of an
   older research job (priority).
7. `pipeline_jobs.write_job`: atomic (no partial file observable; `os.replace`).

## Components & boundaries

- `pipeline_jobs.py` — job/step model + `live_refresh` kind + atomic `write_job`
  (data layer; no execution).
- `pipeline_manager.py` — execution: `live_refresh` priority + `LIVE_OI_REFRESH`
  env on the `0a` step.
- `research/pipeline/stage0a_features.py` — `LIVE_OI_REFRESH`-gated live dump
  (domain logic stays in the stage; runner stays generic).
- `research/lib/factor_io.py` — atomic parquet/meta writes (benefits all writers).
- `dashboard/server/freshness_scheduler.py` — the only new top-level unit; pure
  `tick()` + thin loop; depends on `pipeline_jobs` for enqueue.
- `docker-compose.pipeline.yml` — new `freshness-scheduler` service.
