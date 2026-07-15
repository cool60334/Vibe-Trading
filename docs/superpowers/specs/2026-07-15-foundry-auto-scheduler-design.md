# Foundry Auto-Scheduler (斷点 A) — Design (decoupled)

> Date: 2026-07-15 · Branch: `quant-trading-dashboard`
> Status: design approved, pending implementation plan
> Reviewers: self + agy adversarial review ×3 (A3→A4→decoupled design pivot, then two spec-file passes; every point adjudicated against real code — incl. rejecting agy's "delete candidate after pack" as architecture-breaking, §5.7).
> Depends on: `2026-07-14-foundry-pipeline-bridge-design.md` (bridge, breakpoint B — landed)

## 1. Problem

The Foundry→pipeline bridge (breakpoint B) lets a Foundry factor reach strategy building, but nothing runs the loop unattended. The only autonomous scheduler today, `freshness_scheduler`, hourly enqueues `live_refresh` — it does not run discovery. This spec designs the autonomous discovery loop (breakpoint A) so `Foundry → bridge → pipeline` runs on a cadence. It does **not** deploy to the server (breakpoint C — needs docker + LLM key + root).

## 2. Key architectural decision: DECOUPLE, do not couple

An earlier draft (A4) made Foundry a **step inside a compound pipeline job**. An adversarial review (agy) plus a user question ("replace stage0 with Foundry?") surfaced that Foundry must stay **off the pipeline's critical path**:

- Foundry is paid, non-deterministic, docker-dependent — the antithesis of stage0 (free, deterministic, CI-safe, reproducible). Putting it on the critical path destroys the pipeline's reproducibility and burns money on every run.
- Foundry's gatekeeper **deliberately rejects factors ≥0.7 correlated with the library** (`nearest_correlate` vs `features`), so it is structurally orthogonal to the proven library factors (funding_z, basis_rel, …). It is an R&D *explorer*, not the pipeline's raw-material source.

So Foundry and the pipeline run on **two independent cadences** (the "augment + background-miner" model):

- **Foundry miner** mines candidates into `candidate_features/` on its own schedule (paid, spend-guarded).
- **The pipeline** runs on its own schedule over `library ∪ current-Foundry-overlay` (via the bridge). It always has the library factors, so it never starves.

The long-term payoff (a Foundry factor becoming a free/deterministic library factor via a **graduation** mechanism) is B's follow-on, out of scope here.

## 3. Locked decisions

| 決策 | 值 |
|---|---|
| Loop scope | 全迴圈 Foundry(付費)→bridge→pipeline，但**解耦成兩 cadence** |
| Foundry 位置 | 離線背景挖礦，**不在 pipeline critical path** |
| Spend guard | 現有單日呼叫上限(共享 ledger) + kill-switch pause 檔 + 迴圈間查 |
| 0 candidate | 無短路——pipeline 永遠跑庫內因子；overlay 空就空 |
| 架構 | 兩薄排程器 + 兩 job kind；命令-step model；env 快照進 job payload |

## 4. Architecture

```
[foundry_miner_scheduler]  (極輕, write-only)  每 FOUNDRY_MINE_INTERVAL_SEC
   killswitch pause 檔在 → 跳過
   每幣無 active foundry_mine job → enqueue kind="foundry_mine" (steps=[foundry])

[discovery_pipeline_scheduler]  (極輕, write-only, 獨立 cadence)  每 DISCOVERY_PIPELINE_INTERVAL_SEC
   每幣無 active discovery_pipeline job → enqueue kind="discovery_pipeline" (steps=[bridge]+0a..5)

pipeline_manager  (Global-serial executor, 兩 kind 都跑, dashboard 可見):
   foundry_mine:        foundry step  → run_foundry_job
                          (付費; --daily-max-llm-calls 共享 ledger; --pause-file 迴圈間查;
                           killswitch 停 → exit 201 → manager 終止 job)
   discovery_pipeline:  bridge step   → foundry_bridge (落 per-job overlay; 空 overlay = 正常)
                        → 0a..5 (env snapshot: RESEARCH_INCLUDE_FOUNDRY=1 + overlay dir)
                        → 建策略/回測/OOS 選拔 (research)
   on SUCCESS only: cleanup_overlay(<job dir>)   (失敗保留供 debug)

   選出策略 → 上線仍走 B 的 promote guard (foundry 因子碼未進 production library → REFUSE)
```

## 5. Components

**Global killswitch (agy #7).** Beyond the money-specific `FOUNDRY_PAUSE_FILE`, a `GLOBAL_PAUSE_FILE` (default `runs/discovery_global.pause`) hard-stops the whole loop for ops emergencies (data-source outage, disk trouble). BOTH schedulers skip enqueue when it exists, and the manager refuses to start a new `foundry_mine`/`discovery_pipeline` job while it exists. `FOUNDRY_PAUSE_FILE` stops only mining (money); `GLOBAL_PAUSE_FILE` stops everything.

### §5.1 `foundry_miner_scheduler` (`dashboard/server/foundry_miner_scheduler.py`)
Mirrors `freshness_scheduler` (write-only):
- `tick(repo_root, symbols, now, interval_sec) -> list[str]`: per symbol, if no active (`queued`/`running`, non-zombie) `kind="foundry_mine"` job exists, enqueue one; return ids.
- `main()`: sleep-loop on `FOUNDRY_MINE_INTERVAL_SEC`, symbols from `FOUNDRY_MINE_SYMBOLS`, try/except so a bad tick never kills the loop.
- **Killswitch**: if `FOUNDRY_PAUSE_FILE` (default `runs/foundry_auto.pause`) OR `GLOBAL_PAUSE_FILE` exists, enqueue nothing (log once).

### §5.2 `discovery_pipeline_scheduler` (`dashboard/server/discovery_pipeline_scheduler.py`)
Same shape, independent cadence:
- `tick`: per symbol, if no active **pipeline-family** job (`discovery_pipeline` OR a manually-queued `kind="pipeline"`) exists, enqueue one — so the timer never collides with an operator's manual pipeline run (agy #3b).
- `main()`: `DISCOVERY_PIPELINE_INTERVAL_SEC`, `DISCOVERY_PIPELINE_SYMBOLS`.
- **Killswitch**: it spends nothing, so it ignores `FOUNDRY_PAUSE_FILE`, but it DOES honour `GLOBAL_PAUSE_FILE` (ops hard-stop).
- **Cost note (agy #3a)**: it runs 0a→5 every cadence even when the overlay is empty. This is not wasted — each run incorporates fresh candles (stage0 IC ranking shifts, backtests extend), and it always has the library factors. Manage cost by choosing the cadence (e.g. run it less often than mining), not by skipping.

### §5.3 Job kinds + command-step model (`dashboard/server/pipeline_jobs.py`)
- Extend the step model: a step is either a **pipeline stage** (`{"stage": id}`) or a **command step** (`{"stage": "foundry"}` / `{"stage": "bridge"}`) resolved via a new `_STEP_COMMAND` map to a full argv.
- `kind="foundry_mine"` → steps `["foundry"]`.
- `kind="discovery_pipeline"` → steps `["bridge"] + _PIPELINE_SEQUENCE` (bridge, then 0a…5).
- `create_job(...)` stamps a per-job **overlay dir** (= job log dir `runs/pipeline_jobs/<job_id>/`) and an **`env` snapshot** (see §5.6).
- `step_command(step_id, symbol, *, overlay_dir, runs_dir, manifests_dir, zoo_dir, image, llm, model, daily_max, pause_file) -> list[str]`:
  - foundry: `-m research.hermes.foundry_runner run --runs-dir <runs_dir> --manifests-dir <md> --zoo-dir <zd> --image <img> --llm <llm> --model <model> --i-will-spend-real-money --daily-max-llm-calls <n> --pause-file <pause_file>`
  - bridge: `-m research.hermes.foundry_bridge --symbol <sym> --overlay-dir <overlay_dir> --image <img> --manifests-dir <md>`
- **Two distinct dirs (do not conflate):**
  - `runs_dir` (foundry `--runs-dir`) = a **stable, shared** path (`FOUNDRY_RUNS_DIR`, default `runs/foundry_auto/`), SAME for every symbol every night. Spend ledger (`foundry_spend.jsonl`) + single-instance lock live here → `--daily-max-llm-calls` is a real global cross-symbol cap.
  - `overlay_dir` (bridge `--overlay-dir`) = **per-job** (`runs/pipeline_jobs/<job_id>/`), unique, cleaned on success only.

### §5.4 `pipeline_manager` (extend `execute_job` / `_default_runner` / `_oldest_queued`)
- **Run command steps**: `foundry`/`bridge` steps run their argv (not `stage_command`).
- **`EXIT_PAUSED = 201`**: if the foundry step returns it (killswitch), mark the job `paused` (a terminal, non-failed status) and stop — do NOT proceed. (There is no "skip-remainder success" code; the decoupled model needs no short-circuit.)
- **Env carry**: `discovery_pipeline`'s stage steps get the job's non-secret `config` snapshot merged via existing `stage_env` (includes `RESEARCH_INCLUDE_FOUNDRY=1`, `RESEARCH_FOUNDRY_OVERLAY_DIR=<job dir>`). Never `os.environ.update()`. (Secrets: see §5.6.)
- **Priority — `foundry_mine` runs last (agy #2)**: extend `_oldest_queued`'s sort key so `foundry_mine` sorts AFTER `discovery_pipeline`/`live_refresh`/`pipeline`. A long paid mine (tens of minutes) must not starve the daily production line; it fills idle time. (The manager is serial, so an in-flight mine still blocks until it finishes — acceptable at a nightly cadence; a second executor is a future optimisation, out of scope.)
- **Cleanup on success only + GC (agy #5, #8)**: move `cleanup_overlay(<job dir>)` out of the unconditional `finally`; call it only when the job finished `succeeded`. On failure the overlay is retained for debugging. To stop failed overlays accumulating, `reconcile_startup` runs a GC (reuse `dashboard/server/retention.py`): delete retained overlays older than `OVERLAY_FAILED_RETENTION_DAYS` (default 7). This revises B's Task-9 unconditional-`finally` behaviour.

### §5.5 Foundry killswitch + pause-file (`research/hermes/foundry_runner.py`, `orchestrator.run_foundry`)
- Add `--pause-file <path>` to `foundry_runner run`; thread it into `run_foundry`.
- In `run_foundry`'s hypotheses loop, at the budget-check point, also check the pause file; if present, stop the sweep cleanly and set a `killswitch_paused` summary flag.
- The CLI returns **exit 201** when the run stopped due to the killswitch, so the manager (§5.4) treats it as `paused`, not success. The pause-file path is **passed in**, never hardcoded in the library (keeps tests isolated).

### §5.6 Config snapshot at enqueue — non-secret only (agy #6 + #4-security)
- The two schedulers and the manager are **separate daemons**; the manager must NOT read `FOUNDRY_*`/`DISCOVERY_*` from its own host env at run time (the two hosts can drift).
- At enqueue the scheduler snapshots **non-secret configuration only** — `image`, `llm`, `model`, `daily_max`, `runs_dir`, `zoo_dir`, `pause_file`, and the stage env overrides (`RESEARCH_INCLUDE_FOUNDRY`, `RESEARCH_FOUNDRY_OVERLAY_DIR`) — into the job's `config` dict. The manager applies it blindly.
- **Secrets are never serialised (agy #4)**: `job.json` is a readable file. The LLM API key is NOT in the snapshot and NOT a CLI flag — the foundry subprocess reads it from `agent/.env` via dotenv (existing behaviour), so it lives only in process env, never on disk in a job payload. Any future secret follows the same rule: injected by the subprocess/manager from host env at run time, never snapshotted.

### §5.7 Bridge step: soft-failure + recompute cache (agy #6, #8)
- **Soft-failure (agy #6)**: the bridge is a step *inside* `discovery_pipeline`, before 0a…5. If it hard-failed (e.g. no docker daemon), the whole job would fail and the pipeline would never run its **library** factors — breaking the core "never starves" promise. So `foundry_bridge.main` must, on any bridge-level failure (no docker, image unresolved), log a warning, write an **empty** overlay, and **exit 0** — the stages then run on library factors alone. (Per-factor recompute failures already degrade individually in B; this covers the whole-step infra failure.)
- **Recompute cache (agy #8)**: A3's nightly cadence would otherwise re-run the docker recompute for *every* candidate *every* night. Cache each recomputed full-span series keyed by `code_sha256` (under `candidate_features/recompute_cache/`); on a run, reuse the cache when the sha matches and skip the sandbox. This removes the redundant compute without deleting the candidate (the candidate is the persistent source; the per-job overlay is ephemeral — deleting the source would break the next run). Candidate **archival / TTL** (a candidate's full lifecycle) is decided by the graduation spec (§10), not here.

## 6. Spend + killswitch (three layers)

1. **Daily ceiling**: the foundry step passes `--daily-max-llm-calls <N>` against a **single shared** `foundry_spend.jsonl` under `FOUNDRY_RUNS_DIR` (same for all symbols). Serial manager + shared ledger = a real global cap.
2. **Killswitch pause file**: `foundry_miner_scheduler` skips enqueue when present (stops new mines); `run_foundry` checks it between hypotheses (stops an in-flight run within one hypothesis) and exits 201 so the manager stops the job.
3. **Job cancel**: `execute_job` already checks `job["cancel"]` between steps.

## 7. Failure isolation

- A foundry/bridge/stage step failing (non-zero, non-201) → that job fails; its symbol only. Other symbols are separate jobs; the serial manager runs the next queued job.
- 201 → job `paused` (not failed), no downstream steps.
- Both scheduler ticks are wrapped in try/except; a bad tick never kills the loop.
- A failed `discovery_pipeline` job **keeps** its overlay dir for debugging.

## 8. Error handling

- Foundry paid-run failure (bad key/quota) already aborts (`LLMUnavailable`); job fails, logged, no spend leak (spend recorded in `finally`).
- Bridge recompute failures degrade per-factor (B); an empty overlay is normal — the pipeline still runs on library factors.
- Stale lock: existing `single_instance_lock` staleness handling applies (a crashed miner's lock is reclaimed).
- Killswitch mid-run: clean stop, exit 201, job `paused`.

## 9. Testing (TDD)

- **foundry_miner_scheduler**: `tick` enqueues one `foundry_mine` per symbol; dedups an active job; zombie (>`2*interval`) ignored; `FOUNDRY_PAUSE_FILE` or `GLOBAL_PAUSE_FILE` present → enqueues nothing.
- **discovery_pipeline_scheduler**: `tick` enqueues one `discovery_pipeline` per symbol; dedups against an active `discovery_pipeline` **or** manual `pipeline` job (agy #3b); `GLOBAL_PAUSE_FILE` present → enqueues nothing; ignores `FOUNDRY_PAUSE_FILE`.
- **manager priority (agy #2)**: a queued `discovery_pipeline` is selected before a queued `foundry_mine`.
- **bridge soft-failure (agy #6)**: with docker unavailable, the bridge step writes an empty overlay and exits 0; the discovery_pipeline job still runs 0a→5 and succeeds on library factors.
- **recompute cache (agy #8)**: a second bridge run with an unchanged `code_sha256` reuses the cache and does not invoke the sandbox.
- **failed-overlay GC (agy #5)**: a retained overlay older than the retention window is removed on manager startup; a fresh failed overlay is kept.
- **job model**: `create_job(kind="foundry_mine")` → `["foundry"]`; `create_job(kind="discovery_pipeline")` → `["bridge",0a..5]`; each stamps overlay dir + env snapshot; `step_command` builds correct foundry/bridge argv incl. `--pause-file` and the shared `--runs-dir`.
- **manager**: command steps run their argv; a foundry step returning 201 → job `paused`, no further steps; `discovery_pipeline` stages receive `RESEARCH_INCLUDE_FOUNDRY=1` + overlay dir via `stage_env`; daemon `os.environ` never mutated; `cleanup_overlay` runs on success and is **skipped on failure** (overlay retained).
- **foundry killswitch**: pause file appearing mid-sweep stops the loop within one hypothesis; CLI exits 201; `--pause-file` path is honoured (not hardcoded).
- **env snapshot**: a job carries its config; the manager uses the payload, not its own `FOUNDRY_*` env (test by setting a different value in the manager's env and asserting the payload wins).
- **spend ledger**: two symbols' foundry steps against one shared ledger accumulate; the cap refuses once exhausted.

## 10. Out of scope

- **Graduation mechanism** (Foundry code → production factor library, making a proven factor free/deterministic) — B's follow-on; the real long-term payoff, its own spec.
- **Candidate archival / TTL / consume**: a candidate's full lifecycle (when to retire or graduate a factor out of `candidate_features/`) is decided by the graduation spec. This spec only removes the *redundant recompute* cost via the sha-keyed recompute cache (§5.7); it does not delete or archive candidates (the candidate is the persistent source the per-run overlay is rebuilt from).
- **Server deployment (斷点 C)**: docker + LLM key + root + running the schedulers/manager as services. This spec is local, testable code only.
- **Multi-symbol parallelism**: serial execution is sufficient and sidesteps the global Foundry lock.
- **Real-money authorization UI**: the operator provisions the key + sets the daily cap once via env.

## 11. Files touched

| 檔 | 動作 |
|---|---|
| `dashboard/server/foundry_miner_scheduler.py` | new: tick + loop, killswitch skip |
| `dashboard/server/discovery_pipeline_scheduler.py` | new: tick + loop (independent cadence) |
| `dashboard/server/pipeline_jobs.py` | `foundry_mine`/`discovery_pipeline` kinds, command-step model, `_STEP_COMMAND`, `step_command`, per-job overlay dir + `env` snapshot |
| `dashboard/server/pipeline_manager.py` | run command steps; `EXIT_PAUSED=201`; carry job `config` into stages via `stage_env`; `foundry_mine` low priority; cleanup on success only + failed-overlay GC; honour `GLOBAL_PAUSE_FILE` |
| `dashboard/server/retention.py` | reuse for failed-overlay GC (`OVERLAY_FAILED_RETENTION_DAYS`) |
| `research/hermes/foundry_runner.py` / `orchestrator.py` | `--pause-file`; killswitch check between hypotheses; exit 201 |
| `research/hermes/foundry_bridge.py` | soft-failure (no docker → empty overlay, exit 0); sha-keyed recompute cache |
| `dashboard/server/test_*`, `research/tests/test_*` | §9 tests (two pytest scopes: research + dashboard) |
