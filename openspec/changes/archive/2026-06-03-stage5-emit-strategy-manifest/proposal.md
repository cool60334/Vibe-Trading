## Why

The dashboard backend at `dashboard/server/main.py::list_strategies` reads `research/manifests/*/manifest.json` via `dashboard/server/artifacts.py::list_strategy_manifests`. Every dashboard endpoint downstream of that — `GET /api/strategies`, `GET /api/strategies/{id}`, `POST /api/strategies/{id}/promote`, `POST /api/testnet/{id}/start` — depends on `manifest.json` existing for the strategy.

Today, **no pipeline stage automatically writes that file**. The emitter (`research/emit_manifest.py::build_strategy_manifest`) exists and is importable, but it's only wired into a standalone `main()` that has to be invoked manually. Real consequence: `eth_s5_half_size` is `selected=True` per `selection.json`, but the dashboard shows zero strategies because no `manifest.json` was emitted. The user can't promote anything to testnet without first running a separate manual command they don't know about. Same blast for `btc_s9_stablecoin_funding_gated_tuned`, `eth_s3_dynamic_exit`, and every future strategy.

## What Changes

- **stage5_select.py** MUST, after writing `selection.json`, iterate every eligible strategy (those that entered the ranking) and call `build_strategy_manifest()`, persisting the result to `research/manifests/<strategy_id>/manifest.json`.
- Emission MUST be **fail-soft per strategy**: if one strategy's manifest build raises, the runner logs the failure, continues with the rest, and the overall stage 5 exit code reflects manifest-write success/failure (non-zero only if all eligible writes failed; otherwise warn and stay 0).
- `emit_manifest.py` may need minor refactor to expose a per-strategy entry point (`emit_one(strategy_id, ...)` or similar) if `build_strategy_manifest` alone is insufficient — keep the existing `main()` so the standalone CLI still works.
- The standalone `python -m research.emit_manifest` CLI MUST continue to work for backfilling historical strategies that didn't go through the new stage 5.
- Stage 5 stdout summary MUST report manifest emission results alongside the ranking table.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `backtest-selection`: stage 5 selection runner. Existing capability owns `selection.json` writing + ranking + score formula. This change adds the responsibility to also emit `manifest.json` per eligible strategy. (If no spec exists for this capability yet, create one as part of this change with the existing requirements plus the new one.)

## Impact

**Affected files** (research/ subtree only):
- `research/pipeline/stage5_select.py` — hook manifest emission after `selection.json` write
- `research/emit_manifest.py` — possibly extract a per-strategy `emit_for_strategy()` helper for clean importability
- `research/tests/test_stage5_select.py` — add scenarios for manifest emission integration
- `research/PIPELINE.md` — note the new stage 5 behaviour

**Not changed**:
- `dashboard/server/*` — schema and routes unchanged
- `StrategyManifest` Pydantic schema
- Other pipeline stages
- The standalone `research/emit_manifest.py::main()` CLI continues to work

**Breaking changes**: None for downstream consumers. The only observable change: every selected/eligible strategy after a stage 5 run will now have a `manifest.json` it didn't have before. Existing `manifest.json` files (if present) get overwritten with the latest version, which is the correct behaviour.

**Risks**:
- `build_strategy_manifest` can fail if upstream artifacts (diagnosis / optimization / metrics) are partially corrupt; fail-soft must not silently hide systemic problems. Mitigation: log to stderr + non-zero stdout warning per failed emission.
- A strategy in `strategy_runs.json` but skipped from `selection.json` (because `recommended_action == back_to_stage_2`) won't be in the eligible list. Decision: still emit manifest for it (so the dashboard can show "rejected" strategies). This matches `emit_manifest.py::main()`'s existing behaviour, which iterates all strategies.
