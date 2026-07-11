# Talos Factor Foundry — First Real Run (Pre-flight)

**Date:** 2026-07-10
**Status:** design approved, pending implementation
**Scope:** the engineering needed to run `run_foundry()` for real for the first
time, verified end-to-end at **zero LLM cost** via a scripted fake LLM. The real
OpenRouter client and the paid small-budget run are a **separate follow-up spec**.

---

## Problem

The Talos Foundry (`research/hermes/`) is fully implemented and unit-tested
(155+ tests green), but a grep of non-test code finds **zero** `DockerSandbox()`
constructions and **zero** `run_foundry()` / `run_foundry_job()` call sites — only
`docs/` and `research/tests/`. **The Foundry has never actually run once.**

This is the same shape as the eight bugs this session already found and fixed:
*a guard exists, its unit test is green, and the production path never calls it /
never really ran.* A first real run is bug number nine waiting to happen unless
the production path is exercised deliberately.

Three concrete blockers, all verified empirically (not assumed):

- **B1 — no entry point.** No CLI, no cron, nobody writes the `job.json` that
  `run_foundry_job()` reconciles.
- **B2 — no runnable image.** `DEFAULT_IMAGE = "python:3.11-slim"` is marked
  `NON-FUNCTIONAL PLACEHOLDER` in its own comment (no pandas/numpy/pyarrow), and
  it carries no `@sha256:` digest, so `_assert_image_pinned()` refuses it.
- **B3 — the digest pin refuses every locally-built image, and waves through a
  string docker cannot run.** Measured against the guard's regex
  `@sha256:[0-9a-f]{64}$`:

  | reference | regex accepts | docker runs |
  |---|---|---|
  | `talos-sandbox:test` | ✗ | ✓ |
  | `sha256:<image-id>` (bare id) | ✗ | ✓ |
  | `talos-sandbox@sha256:<image-id>` | ✓ | ✗ `No such image` |

  A locally built image has empty `RepoDigests` (no registry digest), so the only
  reference that satisfies the regex is a stitched-together `name@sha256:<image-id>`
  that docker then rejects. The regex validates *shape*, not resolvability.

---

## Non-goals

- OpenRouter / real LLM client (next spec). `build_llm()` here raises
  `NotImplementedError` for the real provider.
- Any paid LLM run.
- OHLCV fetch/refresh job. The runner reads a parquet; keeping it fresh is a
  separate step (mirrors `scripts/refresh_factors.sh`).
- Dashboard wiring, cron, UI button.
- **Sampling strategy** (shuffle / stratified queue). Which factors the Foundry
  tries is a *research* decision, deferred to the real-run spec. This spec only
  makes the current queue composition **visible** (see N4).

---

## Approved decisions

- **D1** — dry-run first at zero cost (scripted fake LLM), then a separate spec
  wires the real LLM for a small paid run.
- **D2** — relax the pin guard to also accept a bare image ID `sha256:<64hex>`.
  An image ID is content-addressed; what it locks is equivalent to a registry
  digest for single-host use, with no network/account needed.
- **D3** — entry point is a standalone foundry runner CLI (`enqueue` writes
  job.json; `run` reconciles). Follows design law 2: *Talos only writes decision
  files; a separate runner picks them up; it never inline-runs a stage itself.*
- **D4** — the scripted LLM drives three paths: ① a legal factor → full happy
  path; ② bad code then good code → repair loop + stderr feedback; ③ `while True`
  → timeout reap on the **real production path**.
- **D5** — the dry-run is a permanent docker-gated integration test
  (`test_hermes_foundry_e2e.py`); the `ScriptedLLM` lives in `hermes_support.py`
  (test doubles do not enter production modules).
- **D6** — the runner reads an OHLCV parquet and **never touches the network**;
  the e2e uses a real ETH-1H slice committed into the repo.

---

## Adversarial review (agy) — dispositions

agy raised nine points against section 1. Verified empirically before accepting:

- **Accepted #1** — a missing image makes `docker run` exit **125** with
  `Unable to find image ... locally` (measured). Under the current classifier
  that becomes `SandboxRunFailed` (repairable) → 3 wasted retries → hypothesis
  buried with a *"your code failed"* death reason, when the truth is a config
  typo. Fix in N2. Also adopted agy's TOCTOU note (check then delete).
- **Accepted #2** — a bare image ID has *local immutability* but no
  cross-machine portability; writing it into job.json is an audit record, not a
  reproducibility handle. Documented, not designed around.
- **Rejected #3** — agy proposed tightening the regex to distinguish the good
  from the bad reference. Measured: its regex still **accepts** the broken
  `name@sha256:<image-id>` and additionally **rejects** a legal port-bearing
  registry ref `reg.io:5000/x@sha256:...`. A manifest digest and a config
  (image-id) digest are both 64 hex chars — a regex cannot tell them apart *in
  principle*. Only the daemon can, which N2's existence check already does.
- **Mostly rejected #4** — `_align_ohlcv` runs on the **full** feature index
  *before* the OOS cut ([orchestrator.py:283]), so a truncated price table
  already fails loudly at the 95%-coverage check. More fundamentally the Foundry
  only evaluates on the pre-OOS window, so a stale tail cannot change its
  verdict — unlike the trader, which needs *today's* factor value and therefore
  has `FACTOR_MAX_AGE_DAYS`. Porting that gate here is a mis-transplant. Adopted
  only the cheap half: record ohlcv/feature index ranges in the summary (N4).
- **Accepted #5 (strongest point)** — a scripted LLM that emits clean code
  bypasses `build_prompt()` and `extract_code()` — exactly where a real LLM most
  often breaks. Fix in N3: the fake emits **dirty** strings (markdown fence +
  preamble), and the e2e asserts the prompt handed to `complete()` actually
  contains the hypothesis description.
- **Problem accepted, fix rejected #6** — a small budget only reaches zoo LaTeX
  hypotheses (queue order is zoo → derived → academic → llm). agy proposed
  `random.shuffle`; that manufactures a different false-green and hides the fact.
  Instead make it **visible**: record `queue_composition` in the summary (N4).
  Sampling strategy is deferred (non-goal).
- **Problem accepted, fix rejected #7** — too-small an e2e slice. agy proposed
  lowering gate thresholds in the test; that is another false-green. Measured:
  the gatekeeper guards with `len(paired) < 20` / `< 10` and returns **NaN**
  rather than raising, so a tiny slice silently fails the gate and proves
  nothing. Fix: use the **real full pre-OOS slice** (~22k rows; not slow) and
  never touch thresholds.
- **Already handled #8** — the Windows timeout / zombie-container hazard was
  fixed earlier today (commit `0d9954b`); agy was restating this spec's own
  background.
- **Already handled #9** — job.json atomicity. Already `tmp + os.replace`
  ([orchestrator.py:368]).

---

## Section 1 — Architecture & data flow

```
[human/cron] refresh ohlcv parquet          (non-goal for this spec)
        |
enqueue ──► runs/foundry_jobs/<id>/job.json
            {symbol, oos_start, interval, horizon_h, val_frac, ohlcv_path, image}
        |
run (reconcile) ──► resolve_image_id(tag) ──► DockerSandbox(pinned by image id)
                    load_ohlcv(path)   (never networks)
                    run_foundry_job(...)
        |
run_foundry ─► OOS lock ─► build_queue[:max_factors]
                └─► forge: LLM → AST gate → sandbox → PIT check
                     └─► gatekeeper.evaluate → candidate/graveyard parquet + evidence card
        |
job.json  status = done | failed(+error)
```

### N1 — `resolve_image_id(tag) -> "sha256:<64hex>"`

The operator passes a human-readable tag (`--image talos-sandbox:test`). The
runner resolves it via `docker image inspect` to the immutable image ID, runs the
container by that ID, and writes the resolved ID back into job.json for audit. A
tag can be swapped under you; an image ID cannot.

### N2 — a missing/unresolvable image is infra, not "the LLM's code failed"

Two layers, because the pre-check has a TOCTOU window:

1. **Pre-check** in `resolve_image_id`: `docker image inspect` fails → raise a
   bare `SandboxError` (aborts the sweep) before the first container starts.
   Catches config typos.
2. **In-run promotion**: `docker run` **exit 125**, or stderr containing
   `Unable to find image` / `Cannot connect to the Docker daemon`, is promoted
   from `SandboxRunFailed` to a bare `SandboxError`. 125 is docker's own
   "couldn't start" code; LLM code cannot produce it, so this rule has no risk of
   misjudging a real code failure. Catches an image deleted after the pre-check,
   and a daemon that dies mid-run.

### N3 — dirty scripted output exercises the parser

`ScriptedLLM` returns strings wrapped in a markdown fence with a preamble, so
`build_prompt → extract_code` is actually walked. The e2e asserts the prompt
contains the hypothesis description.

### N4 — make queue composition and data ranges visible

`run_foundry`'s summary gains `queue_composition` (per-source counts) and
`ohlcv_range` / `features_range`. The former makes "a small budget only reaches
zoo" visible on the first real run instead of hiding it behind randomisation; the
latter is an audit trail.

---

## Section 2 — Components & files

### Modify `research/hermes/sandbox.py`

- `_assert_image_pinned` also accepts a bare `sha256:<64hex>`. Docstring must
  state: **the regex validates shape only; the daemon is the arbiter of
  resolvability.**
- New `_assert_image_exists(image)` → `docker image inspect`; unresolvable →
  bare `SandboxError`. The image is immutable per `DockerSandbox` instance, so
  verify **once** and cache (`self._image_verified`) — do not ask the daemon per
  hypothesis.
- Failure classifier gains one rule: `exit_code == 125` or stderr containing
  `Unable to find image` / `Cannot connect to the Docker daemon` → promote to
  bare `SandboxError`.

### New `research/hermes/foundry_runner.py` (entry point, no research logic)

| function | responsibility | depends on |
|---|---|---|
| `resolve_image_id(tag)` | tag → immutable image ID; unresolvable → `SandboxError` | docker CLI |
| `load_ohlcv(path)` | read parquet, validate `close` col + UTC index. Never networks | pandas |
| `reconcile_foundry_jobs(runs_dir, manifests_dir, llm, sandbox, zoo_dir, budget)` | scan `status=="queued"` jobs, order by `created_at` (deterministic), hand each to `run_foundry_job` | orchestrator |
| `build_llm(spec)` | raises `NotImplementedError` for `"openrouter"` (next spec) | — |
| `main()` | argparse: `enqueue` / `run` | above |

### Modify `research/tests/hermes_support.py`

- `ScriptedLLM(responses)`: returns dirty strings (preamble + markdown fence) in
  call order, and records each received prompt in `self.prompts` for assertions.

### New `research/tests/test_hermes_foundry_e2e.py`

- docker-gated, `call_with_deadline` wall-clock bound, `SandboxContainerGuard`
  cleanup. `tmp_path` as `manifests_dir`, with a real pre-OOS `features_eth`
  slice copied in.

### New `research/tests/fixtures/ohlcv_eth_1h.parquet`

Real ETH 1H candles from OKX public API (read-only, no auth), fetched **once**
and committed. ~22k rows × 5 cols, 1–2 MB. Approved one-time network access.

---

## Section 3 — Error handling

### Layer A — infra, aborts the whole sweep (bare `SandboxError`)

| trigger | detected at | why abort not retry |
|---|---|---|
| image tag unresolvable | `resolve_image_id` pre-check | every hypothesis would die; retry amplifies a config error 60× |
| image deleted after check (TOCTOU) | `docker run` exit 125 promotion | pre-check has a window; this is the second line |
| daemon dies mid-run | stderr `Cannot connect...` promotion | not the LLM's fault |
| LLM budget spent | `ForgeBudget.charge_call` → `BudgetExhausted` | already implemented |

### Layer B — repairable, handled inside forge (`SandboxRunFailed`)

Container-side non-zero exit, OOM, timeout. stderr fed back to the LLM, ≤3
attempts, then bury. Already implemented; untouched here.

### Layer C — job-level failure must not poison other jobs

`run_foundry_job` already writes `status=failed + error + finished_at` atomically
then re-raises ([orchestrator.py:409]). `reconcile_foundry_jobs` catches a single
job's exception, logs it, and continues to the next job — **except** a Layer-A
infra error, which propagates and stops the whole batch (the next job would hit
the same wall). Discriminator: a `SandboxError` that is **not** a
`SandboxRunFailed` subclass stops the batch; anything else marks that job failed
and continues. Locked by a test both ways.

**Known boundary (documented, not locked):** `reconcile` may read a stale
snapshot of a job.json another process is rewriting. The writer is atomic
(`tmp + os.replace`), so no half-file is ever observed; a stale snapshot is
reconciled on the next pass. No lock added.

---

## Section 4 — Test matrix

Principle: every guard gets one test that the **production path actually calls
it**. Left = guard, right = the test that really invokes it.

### Unit (mock, no docker) — `test_hermes_sandbox.py` / `test_hermes_foundry_runner.py`

| guard | test | assertion |
|---|---|---|
| pin accepts bare image ID | `test_bare_image_id_is_accepted_as_pinned` | `sha256:<64hex>` does not raise |
| pin still refuses a tag | `test_mutable_tag_still_refused` | `talos:test` raises SandboxError |
| missing image = infra | `test_missing_image_is_infra_not_repairable` | inspect fails → bare SandboxError, **not** SandboxRunFailed |
| exit 125 promoted | `test_exit_125_is_promoted_to_infra` | FakePopen returncode=125 → bare SandboxError |
| daemon-down stderr promoted | `test_daemon_down_midrun_is_infra` | stderr `Cannot connect...` → bare SandboxError |
| image check cached | `test_image_verified_once_not_per_hypothesis` | inspect called exactly once |
| resolve tag → id | `test_resolve_image_id_returns_immutable_id` | returns `sha256:...` |
| load_ohlcv rejects no-close | `test_load_ohlcv_rejects_missing_close` | raises ValueError |
| reconcile ordering | `test_jobs_run_in_created_at_order` | deterministic order |
| **Layer A aborts batch** | `test_infra_error_aborts_whole_queue` | job1 raises infra → job2 **not executed** |
| **Layer C continues** | `test_job_level_failure_continues_queue` | job1 fails (non-infra) → status=failed, job2 **still runs** |
| parser eats dirty output | `test_scripted_llm_dirty_output_is_extracted` | fence+preamble → extract_code returns clean code |

### Integration (docker-gated, wall-clock bound) — `test_hermes_foundry_e2e.py`

| path | test | assertion |
|---|---|---|
| ① happy path, full chain | `test_healthy_factor_runs_full_pipeline` | gatekeeper really ran; metrics all finite non-NaN; outcome ∈ {candidate, rejected}; evidence card written; prompt contains hypothesis description |
| ② repair loop | `test_bad_code_is_repaired_from_stderr` | attempt1 KeyError → attempt2 good; `fr.attempts==2`; 2nd prompt contains `KeyError` |
| ③ timeout + short-circuit | `test_infinite_loop_times_out_then_short_circuits` | `while True` times out → verbatim repeat → buried; `death_reason` contains `timed out`; `SandboxContainerGuard.new()==[]` |
| job reconcile close-out | `test_reconcile_marks_job_done` | job.json `status==done`; summary has `queue_composition` / `ohlcv_range` |

**The e2e calls the real `reconcile_foundry_jobs()`** — not a shortcut into
`run_foundry`. The whole production path (entry → reconcile → run_foundry → forge
→ sandbox → gatekeeper → evidence card) is walked once, which is the link the
session's eight bugs were all missing.

### Honestly-recorded gaps (documented, not pretended away)

1. `build_llm("openrouter")` and argparse `main()` wiring — not covered by the
   e2e (the real client is the next spec).
2. The candidate-parquet write path is only reached when a factor happens to pass
   the gate. It stays covered by the existing `test_hermes_candidate_store.py`,
   not left to e2e chance. **We do not assert "a factor passes the gate"** —
   hand-picking a factor that clears `gross_ic_min`/`dsr_min` would be
   manufacturing alpha. The e2e asserts engineering facts only.

---

## Self-review

- **Placeholders:** none. `build_llm` raising `NotImplementedError` is a named
  boundary, not a stub left to rot — it is in the test matrix's gap list.
- **Consistency:** the three failure layers in §3 match the classifier changes in
  §2 and the tests in §4. The pin/image-existence split is asserted in both the
  unit matrix and the e2e.
- **Scope:** one implementation plan. Real LLM client, paid run, and sampling
  strategy are explicit non-goals.
- **Ambiguity:** "infra vs repairable" is pinned to concrete signals (exit 125,
  two stderr substrings, `SandboxRunFailed` subclass check), not left to
  interpretation.
